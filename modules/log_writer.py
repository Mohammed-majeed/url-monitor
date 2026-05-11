"""
log_writer.py
=============
Writes CheckResult objects as JSON Lines, one file per run, mirroring the
SSL inspecter convention:

    <log_dir>/url_monitor_YYYYMMDD_HHMMSS.log

Each line is one JSON object.

Also provides:
- setup_logging()     : configure Python logging (INFO to console + rotating file)
- write_detail_log()  : optional per-URL detail log (toggleable, separate from combined)
"""
from __future__ import annotations

import glob
import json
import logging
import logging.handlers
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional

from .checker import CheckResult


LOG_PREFIX = "url_monitor"
DETAIL_LOG_PREFIX = "url_detail"

logger = logging.getLogger("url_monitor")


# ─── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging(
    log_dir: Optional[str] = None,
    level: int = logging.INFO,
    log_filename: str = "url_monitor_run.log",
) -> None:
    """
    Configure the root 'url_monitor' logger.

    Outputs:
      - Console (StreamHandler)  : INFO and above, human-readable with timestamp
      - Rotating file (optional) : if log_dir is given, writes to
                                   <log_dir>/url_monitor_run.log
                                   Max 5 MB per file, keeps 3 backups.

    Call this once at startup in monitor_main.py before anything else runs.
    """
    fmt = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger("url_monitor")
    root.setLevel(level)
    root.handlers.clear()

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # File handler (optional)
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            filename=os.path.join(log_dir, log_filename),
            maxBytes=5 * 1024 * 1024,   # 5 MB
            backupCount=3,
            encoding="utf-8",
        )
        fh.setLevel(level)
        fh.setFormatter(fmt)
        root.addHandler(fh)

    logger.info("Logging initialised  level=%s  log_dir=%s",
                logging.getLevelName(level), log_dir or "(console only)")


# ─── JSON-Lines result log ──────────────────────────────────────────────────────

def _ensure_dir(path: str | os.PathLike) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def make_log_path(log_dir: str | os.PathLike) -> Path:
    d = _ensure_dir(log_dir)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return d / f"{LOG_PREFIX}_{ts}.log"


def write_results(
    log_path: str | os.PathLike,
    results: Iterable[CheckResult],
    echo: bool = True,
) -> int:
    """Append one JSON line per result. Returns count written."""
    n = 0
    with open(log_path, "a", encoding="utf-8") as f:
        for r in results:
            line = r.to_json()
            f.write(line + "\n")
            if echo:
                print(line)
            n += 1
    return n


# ─── Per-URL detail log (GitHub runner output) ─────────────────────────────────

def write_detail_log(
    log_dir: str | os.PathLike,
    results: List[CheckResult],
    enabled: bool = True,
) -> Optional[Path]:
    """
    Write a human-readable per-URL detail log. Separate from the combined
    Splunk log so Splunk ingestion is not affected.

    Toggled via [logging] url_detail_log = true/false in url_monitor_config.ini.

    Format per URL:
        [OK  / FAIL]  ENV  target_kind  url_id  url
                      http_code  response_time_ms ms  final_url
                      msg (on failure)

    File:  <log_dir>/url_detail_YYYYMMDD_HHMMSS.log
    """
    if not enabled:
        logger.debug("Per-URL detail log is disabled.")
        return None

    d = _ensure_dir(log_dir)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = d / f"{DETAIL_LOG_PREFIX}_{ts}.log"

    ok_count = sum(1 for r in results if r.exit_code == "0")
    fail_count = len(results) - ok_count

    with open(path, "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write(f"URL MONITOR — PER-URL DETAIL LOG\n")
        f.write(f"Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Total     : {len(results)}  OK: {ok_count}  Failed: {fail_count}\n")
        f.write("=" * 80 + "\n\n")

        for r in sorted(results, key=lambda x: (x.environment, x.app_name, x.url_id, x.target_kind)):
            status_tag = "OK  " if r.exit_code == "0" else "FAIL"
            f.write(
                f"[{status_tag}]  "
                f"{r.environment:<6}  {r.target_kind:<8}  {r.url_id:<20}  {r.url}\n"
            )
            f.write(
                f"        http={r.http_code}  "
                f"{r.response_time_ms} ms  "
                f"runner={r.runner}  "
                f"final={r.final_url or '—'}\n"
            )
            if r.exit_code == "1":
                # Print the first two comma-separated msg fields (most useful part)
                short_msg = ",".join(r.msg.split(",")[:3]) if r.msg else "no message"
                f.write(f"        ⚠  {short_msg}\n")
            f.write("\n")

    logger.info("Per-URL detail log written: %s  (%d entries)", path, len(results))
    return path


# ─── Cleanup ───────────────────────────────────────────────────────────────────

def cleanup_old(log_dir: str | os.PathLike, days: int = 14) -> int:
    """Delete .log files older than `days` days. Returns count removed."""
    d = Path(log_dir)
    if not d.is_dir():
        return 0
    cutoff = datetime.now() - timedelta(days=days)
    removed = 0
    for path in glob.glob(str(d / "*.log")):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
            if mtime < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info("Cleaned up %d old log file(s) from %s", removed, d)
    return removed


# ─── Console summary ───────────────────────────────────────────────────────────

def print_summary(results: List[CheckResult]) -> None:
    total = len(results)
    ok = sum(1 for r in results if r.exit_code == "0")
    bad = total - ok

    logger.info("=" * 70)
    logger.info("URL MONITOR SUMMARY")
    logger.info("=" * 70)
    logger.info("Total: %d  |  OK: %d  |  Failed: %d", total, ok, bad)

    if bad:
        logger.info("Failed:")
        for r in results:
            if r.exit_code == "1":
                first_msg = r.msg.split(",", 1)[0] if r.msg else ""
                logger.info(
                    "  [%s/%s]  %s  %s  ->  %s",
                    r.environment, r.target_kind, r.url_id, r.url, first_msg,
                )
