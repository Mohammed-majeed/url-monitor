"""
checker.py
==========
Core async HTTP checker. One function `check_one(target, ...) -> CheckResult`.

Uses aiohttp (async) with a cookie jar per request:
- Cookie jar fixes session-cookie redirect loops (e.g. GeoServer /web/?0).
- HEAD → GET fallback.
- Max 10 redirects (curl's 50 was too lenient).
- Proxy isolation: trust_env=False when no proxy configured.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import aiohttp
from aiohttp import ClientTimeout, TCPConnector

from .inventory import CheckTarget
from .status_spec import StatusSpec, looks_like_login

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

logger = logging.getLogger("url_monitor.checker")

MAX_REDIRECTS = 10


# ─── CheckResult ───────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    """Structured output for one URL check."""
    starting_time: str
    finish_time: str
    elapsed_time: str

    application: str
    action: str
    method: str

    url_id: str
    target_kind: str
    environment: str
    location_type: str
    app_name: str
    runner: str

    url: str
    checked_url: str
    final_url: str

    http_code: int
    status_category: str
    looks_like_login: bool

    text_check: str
    expected_statuses: str
    expected_text: List[str] = field(default_factory=list)

    response_time_ms: int = 0
    content_length_header: int = -1
    bytes_read: int = 0
    body_truncated: bool = False

    msg: str = ""
    exit_code: str = "1"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


# ─── Time helpers ──────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _fmt_elapsed(start: datetime, end: datetime) -> str:
    delta = end - start
    total = delta.total_seconds()
    h = int(total // 3600)
    m = int((total % 3600) // 60)
    s = total % 60
    return f"{h}:{m:02d}:{s:06.3f}"


# ─── URL helpers ───────────────────────────────────────────────────────────────

def _normalize_url(raw: str) -> str:
    u = (raw or "").strip()
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


def _strip_url_scheme(url: str) -> str:
    u = (url or "").strip()
    if u.startswith("https://"):
        return u[len("https://"):]
    if u.startswith("http://"):
        return u[len("http://"):]
    return u


# ─── HTTP helpers ──────────────────────────────────────────────────────────────

def _status_category(code: int) -> str:
    if 200 <= code < 300: return "success"
    if 300 <= code < 400: return "redirect"
    if 400 <= code < 500: return "client_error"
    if 500 <= code < 600: return "server_error"
    return "unknown"


def _check_text(body: str, expected_text: List[str]) -> str:
    if not expected_text:
        return "skipped"
    body_lc = (body or "").lower()
    missing = [t for t in expected_text if t.lower() not in body_lc]
    if not missing:
        return "passed"
    return "failed:" + "|".join(missing)


def _content_length_from_headers(headers) -> int:
    raw = headers.get("Content-Length")
    if raw is None:
        return -1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


def _build_ssl_context(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class _NeedGet(Exception):
    """HEAD rejected by server; retry with GET."""


# ─── Core async fetch ──────────────────────────────────────────────────────────

async def _fetch(
    url: str,
    *,
    timeout_seconds: int,
    verify_ssl: bool,
    proxies: Optional[dict],
    read_body: bool,
    max_body_bytes: int,
    user_agent: str,
) -> Tuple[int, str, str, int, int, bool]:
    """
    Async HTTP fetch. Returns:
        (status, final_url, body, content_length_header, bytes_read, body_truncated)

    Cookie jar: stores JSESSIONID etc. across the redirect chain so
    session-gated apps (GeoServer) don't loop infinitely.
    """
    ssl_ctx = _build_ssl_context(verify_ssl)
    connector = TCPConnector(ssl=ssl_ctx, limit=0)

    proxy_url: Optional[str] = None
    if proxies:
        proxy_url = proxies.get("https") or proxies.get("http")

    timeout = ClientTimeout(total=timeout_seconds)
    headers = {"User-Agent": user_agent}
    cookie_jar = aiohttp.CookieJar(unsafe=True)
    trust_env = proxy_url is not None

    body = ""
    final_url = url
    status = 0
    content_length = -1
    bytes_read = 0
    truncated = False

    logger.debug("Fetching  url=%s  proxy=%s  verify_ssl=%s", url, proxy_url, verify_ssl)

    async with aiohttp.ClientSession(
        connector=connector,
        cookie_jar=cookie_jar,
        headers=headers,
        trust_env=trust_env,
    ) as session:
        resp: Optional[aiohttp.ClientResponse] = None
        try:
            # Phase 1: HEAD
            try:
                resp = await session.head(
                    url,
                    timeout=timeout,
                    allow_redirects=True,
                    proxy=proxy_url,
                    max_redirects=MAX_REDIRECTS,
                )
                if resp.status in (405, 501):
                    logger.debug("HEAD rejected (%d), falling back to GET  url=%s",
                                 resp.status, url)
                    await resp.release()
                    resp = None
                    raise _NeedGet()
            except _NeedGet:
                pass
            except aiohttp.ClientResponseError:
                if resp is not None:
                    await resp.release()
                    resp = None
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if resp is not None:
                    await resp.release()
                    resp = None

            # Phase 2: GET
            if resp is None:
                logger.debug("GET  url=%s", url)
                resp = await session.get(
                    url,
                    timeout=timeout,
                    allow_redirects=True,
                    proxy=proxy_url,
                    max_redirects=MAX_REDIRECTS,
                )

            status = resp.status
            final_url = str(resp.url)
            content_length = _content_length_from_headers(resp.headers)

            logger.debug("Response  url=%s  status=%d  final_url=%s", url, status, final_url)

            if read_body:
                raw = await resp.content.read(max_body_bytes + 1)
                if len(raw) > max_body_bytes:
                    truncated = True
                    raw = raw[:max_body_bytes]
                bytes_read = len(raw)
                charset = resp.charset or "utf-8"
                try:
                    body = raw.decode(charset, errors="replace")
                except Exception:
                    body = raw.decode("utf-8", errors="replace")

        finally:
            if resp is not None:
                try:
                    await resp.release()
                except Exception:
                    pass

    return status, final_url, body, content_length, bytes_read, truncated


# ─── Public synchronous API ────────────────────────────────────────────────────

def check_one(
    target: CheckTarget,
    *,
    proxies: Optional[dict] = None,
    verify_ssl: bool = False,
    user_agent: str = "URLMonitor",
    application: str = "URLMonitor",
    action: str = "url_monitor",
) -> CheckResult:
    """
    Check one URL synchronously and return a CheckResult.
    Runs _fetch() in a fresh event loop — safe with ThreadPoolExecutor.
    """
    started = _now()
    checked_url = _normalize_url(target.url)
    log_url = _strip_url_scheme(target.url)
    log_checked_url = _strip_url_scheme(checked_url)

    logger.info("Checking  [%s/%s]  %s  %s",
                target.environment, target.target_kind, target.url_id, log_url)

    err_type: Optional[str] = None
    err_msg: str = ""
    status = 0
    final_url = ""
    body = ""
    content_length = -1
    bytes_read = 0
    truncated = False

    effective_proxies = proxies if target.use_proxy else None

    try:
        loop = asyncio.new_event_loop()
        try:
            (status, final_url, body,
             content_length, bytes_read, truncated) = loop.run_until_complete(
                _fetch(
                    checked_url,
                    timeout_seconds=target.timeout_seconds,
                    verify_ssl=verify_ssl,
                    proxies=effective_proxies,
                    read_body=target.read_body,
                    max_body_bytes=target.max_body_bytes,
                    user_agent=user_agent,
                )
            )
        finally:
            loop.close()

    except asyncio.TimeoutError:
        err_type = "TimeoutError"
        err_msg = f"Connection timed out after {target.timeout_seconds}s"
    except aiohttp.ClientSSLError as e:
        err_type = "SSLError"
        err_msg = f"SSL error: {str(e)[:160]}"
    except aiohttp.ClientConnectorError as e:
        err_type = "ConnectionError"
        err_msg = f"Connection failed: {str(e)[:160]}"
    except aiohttp.TooManyRedirects as e:
        err_type = "TooManyRedirects"
        err_msg = f"Redirect loop detected (>{MAX_REDIRECTS} redirects): {str(e)[:120]}"
    except aiohttp.ClientError as e:
        err_type = type(e).__name__
        err_msg = f"Client error: {str(e)[:160]}"
    except Exception as e:
        err_type = type(e).__name__
        err_msg = f"Error: {str(e)[:160]}"

    finished = _now()
    elapsed_ms = int((finished - started).total_seconds() * 1000)

    spec = StatusSpec.parse(target.expected_statuses)
    login = looks_like_login(final_url, body)
    text_check = _check_text(body, target.expected_text)

    if err_type is not None:
        category = "error"
        ok = False
        log_final_url = _strip_url_scheme(final_url)
        msg = (
            f"{err_type}:{err_msg},"
            f"url:{log_url},"
            f"checked_url:{log_checked_url},"
            f"final_url:{log_final_url},"
            f"env:{target.environment},"
            f"app:{target.app_name},"
            f"location:{target.location_type}"
        )
        logger.warning("FAIL  [%s/%s]  %s  %s  ->  %s: %s",
                       target.environment, target.target_kind,
                       target.url_id, log_url, err_type, err_msg[:120])
    else:
        category = _status_category(status)
        status_ok = spec.is_ok(status, login)
        text_ok = text_check in ("skipped", "passed")
        ok = status_ok and text_ok
        log_final_url = _strip_url_scheme(final_url)
        msg = (
            f"status:{category},"
            f"http_code:{status},"
            f"url:{log_url},"
            f"checked_url:{log_checked_url},"
            f"final_url:{log_final_url},"
            f"looks_like_login:{str(login).lower()},"
            f"text_check:{text_check},"
            f"env:{target.environment},"
            f"app:{target.app_name},"
            f"location:{target.location_type},"
            f"target_kind:{target.target_kind},"
            f"response_time_ms:{elapsed_ms},"
            f"bytes_read:{bytes_read},"
            f"content_length:{content_length},"
            f"body_truncated:{str(truncated).lower()}"
        )
        if ok:
            logger.info("OK    [%s/%s]  %s  %s  http=%d  %dms",
                        target.environment, target.target_kind,
                        target.url_id, log_url, status, elapsed_ms)
        else:
            logger.warning("FAIL  [%s/%s]  %s  %s  http=%d  text_check=%s",
                           target.environment, target.target_kind,
                           target.url_id, log_url, status, text_check)

    return CheckResult(
        starting_time=_fmt_dt(started),
        finish_time=_fmt_dt(finished),
        elapsed_time=_fmt_elapsed(started, finished),
        application=application,
        action=action,
        method="HTTP",
        url_id=target.url_id,
        target_kind=target.target_kind,
        environment=target.environment,
        location_type=target.location_type,
        app_name=target.app_name,
        runner=target.runner,
        url=log_url,
        checked_url=log_checked_url,
        final_url=log_final_url,
        http_code=status,
        status_category=category,
        looks_like_login=login,
        text_check=text_check,
        expected_statuses=target.expected_statuses,
        expected_text=list(target.expected_text),
        response_time_ms=elapsed_ms,
        content_length_header=content_length,
        bytes_read=bytes_read,
        body_truncated=truncated,
        msg=msg,
        exit_code="0" if ok else "1",
    )
