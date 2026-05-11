"""
checker.py
==========
Core HTTP checker. One function `check_one(target, ...) -> CheckResult`.

Mirrors the structure of internal_ssl_cert_inspecter.fetch_certificate(): one
target in, one structured result out. The orchestrator decides where the
target runs (locally vs GitHub Actions runner).

Design notes:
- Synchronous, one URL per call. Parallelism is the orchestrator's job.
  Same shape as the SSL inspecters.
- HEAD then GET fallback (HEAD is cheap, but some servers reject it).
- expected_text: comma-separated list, ALL must appear in body (AND).
- expected_statuses: see status_spec.StatusSpec.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urlparse

import requests
from requests.exceptions import (
    Timeout,
    SSLError,
    ConnectionError as ReqConnectionError,
    RequestException,
)

from .inventory import CheckTarget
from .status_spec import StatusSpec, looks_like_login


# Suppress only the InsecureRequestWarning when verify=False
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass


@dataclass
class CheckResult:
    """Structured output for one URL check."""
    starting_time: str
    finish_time: str
    elapsed_time: str

    application: str       # "URLMonitor"
    action: str            # "url_monitor"
    method: str            # "HTTP"

    url_id: str
    target_kind: str       # external | internal | ingress
    environment: str
    location_type: str
    app_name: str
    runner: str

    url: str
    checked_url: str
    final_url: str

    http_code: int
    status_category: str   # success / redirect / client_error / server_error / error
    looks_like_login: bool

    text_check: str        # "skipped" | "passed" | "failed:<missing terms>"
    expected_statuses: str
    expected_text: List[str] = field(default_factory=list)

    response_time_ms: int = 0
    content_length_header: int = -1   # Content-Length header (-1 if absent)
    bytes_read: int = 0               # bytes actually read off the wire
    body_truncated: bool = False      # True if hit max_body_bytes cap

    msg: str = ""
    exit_code: str = "1"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


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


def _normalize_url(raw: str) -> str:
    """
    Normalize URL for checking.

    Important:
    - Add https:// when scheme is missing.
    - Lowercase only scheme + hostname.
    - Preserve path/query case, because paths can be case-sensitive.
    """
    u = (raw or "").strip()
    if not u:
        return u

    if not u.startswith(("http://", "https://")):
        u = "https://" + u

    parts = urlsplit(u)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()

    return urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))


def _strip_url_scheme(url: str) -> str:
    u = (url or "").strip()
    if u.startswith("https://"):
        return u[len("https://"):]
    if u.startswith("http://"):
        return u[len("http://"):]
    return u


def _status_category(code: int) -> str:
    if 200 <= code < 300: return "success"
    if 300 <= code < 400: return "redirect"
    if 400 <= code < 500: return "client_error"
    if 500 <= code < 600: return "server_error"
    return "unknown"


def _check_text(body: str, expected_text: List[str]) -> str:
    """Return 'skipped' | 'passed' | 'failed:term1|term2'."""
    if not expected_text:
        return "skipped"
    body_lc = (body or "").lower()
    missing = [t for t in expected_text if t.lower() not in body_lc]
    if not missing:
        return "passed"
    return "failed:" + "|".join(missing)


def _content_length_header(r) -> int:
    """Return Content-Length header as int, or -1 if absent / not numeric."""
    raw = r.headers.get("Content-Length")
    if raw is None:
        return -1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


def _do_request(url: str, *, timeout: int, verify: bool,
                proxies: Optional[dict], read_body: bool,
                max_body_bytes: int, user_agent: str):
    """
    Curl-based checker.

    This follows redirects like:
        curl -kL https://host/path

    It is often more reliable in corporate/RWS environments than Python requests,
    especially with proxy, SSL, Schannel/certificates, and redirect behaviour.
    """
    import os
    import subprocess
    import tempfile

    body = ""
    final_url = url
    status = 0
    content_length = -1
    bytes_read = 0
    truncated = False

    with tempfile.TemporaryDirectory() as td:
        body_path = os.path.join(td, "body.out")
        header_path = os.path.join(td, "headers.out")

        cmd = [
            "curl",
            "-sS",                 # silent, but still show errors
            "-L",                  # follow redirects
            "--connect-timeout", str(timeout),
            "--max-time", str(max(timeout * 3, timeout + 15)),
            "-A", user_agent,
            "-D", header_path,     # response headers
            "-o", body_path,       # response body
            "-w", "%{http_code}\n%{url_effective}\n",
        ]

        # Match curl -k only when SSL verification is disabled.
        if not verify:
            cmd.append("-k")

        # Important:
        # If proxies are explicitly passed, use them.
        # If not, prevent hidden environment proxy from changing the result.
        if proxies:
            proxy_url = proxies.get("https") or proxies.get("http")
            if proxy_url:
                cmd.extend(["--proxy", proxy_url])
        else:
            cmd.extend(["--noproxy", "*"])

        cmd.append(url)

        try:
            p = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=max(timeout * 3 + 10, timeout + 20),
            )
        except subprocess.TimeoutExpired:
            raise Timeout(f"curl timed out after {timeout}s")

        if p.returncode != 0:
            err = (p.stderr or "").strip()
            debug_cmd = " ".join(cmd)

            if p.returncode == 28:
                raise Timeout(f"curl timeout rc=28: {err}; cmd={debug_cmd}")
            if p.returncode in (35, 51, 58, 60):
                raise SSLError(f"curl SSL error rc={p.returncode}: {err}; cmd={debug_cmd}")

            raise ReqConnectionError(
                f"curl failed rc={p.returncode}: {err}; cmd={debug_cmd}"
            )

        lines = [x.strip() for x in (p.stdout or "").splitlines() if x.strip()]
        if len(lines) >= 1:
            try:
                status = int(lines[-2] if len(lines) >= 2 else lines[-1])
            except ValueError:
                status = 0

        if len(lines) >= 2:
            final_url = lines[-1]

        # Parse final Content-Length if available.
        try:
            with open(header_path, "r", encoding="utf-8", errors="replace") as hf:
                for line in hf:
                    if line.lower().startswith("content-length:"):
                        try:
                            content_length = int(line.split(":", 1)[1].strip())
                        except ValueError:
                            pass
        except OSError:
            content_length = -1

        if read_body:
            try:
                with open(body_path, "rb") as bf:
                    raw = bf.read(max_body_bytes + 1)
                if len(raw) > max_body_bytes:
                    truncated = True
                    raw = raw[:max_body_bytes]
                bytes_read = len(raw)
                body = raw.decode("utf-8", errors="replace")
            except OSError:
                body = ""
                bytes_read = 0

    return status, final_url, body, content_length, bytes_read, truncated


class _NeedsGet(Exception):
    """Internal sentinel; not exposed."""


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
    Check one URL and return a CheckResult.

    The caller should pass `proxies` only when target.use_proxy is True.
    """
    started = _now()
    checked_url = _normalize_url(target.url)
    log_url = _strip_url_scheme(target.url)
    log_checked_url = _strip_url_scheme(checked_url)

    err_type: Optional[str] = None
    err_msg: str = ""
    status = 0
    final_url = ""
    body = ""
    content_length = -1
    bytes_read = 0
    truncated = False

    try:
        (status, final_url, body,
         content_length, bytes_read, truncated) = _do_request(
            checked_url,
            timeout=target.timeout_seconds,
            verify=verify_ssl,
            proxies=proxies if target.use_proxy else None,
            read_body=target.read_body,
            max_body_bytes=target.max_body_bytes,
            user_agent=user_agent,
        )
    except Timeout:
        err_type = "TimeoutError"
        err_msg = f"Connection timed out after {target.timeout_seconds}s"
    except SSLError as e:
        err_type = "SSLError"
        err_msg = f"SSL error: {str(e)[:160]}"
    except ReqConnectionError as e:
        err_type = "ConnectionError"
        err_msg = f"Connection failed: {str(e)[:160]}"
    except RequestException as e:
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
