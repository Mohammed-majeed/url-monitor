"""
log_writer.py
=============
Writes CheckResult objects as JSON Lines, one file per run, mirroring the
SSL inspecter convention:

    <log_dir>/url_monitor_YYYYMMDD_HHMMSS.log

Each line is one JSON object.
"""
from __future__ import annotations

import json
import os
import glob
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List

from .checker import CheckResult


LOG_PREFIX = "url_monitor"


def _ensure_dir(path: str | os.PathLike) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def make_log_path(log_dir: str | os.PathLike) -> Path:
    d = _ensure_dir(log_dir)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return d / f"{LOG_PREFIX}_{ts}.log"


def write_results(log_path: str | os.PathLike,
                  results: Iterable[CheckResult],
                  echo: bool = True) -> int:
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
    return removed


def print_summary(results: List[CheckResult]) -> None:
    total = len(results)
    ok = sum(1 for r in results if r.exit_code == "0")
    bad = total - ok
    print()
    print("=" * 70)
    print("URL MONITOR SUMMARY")
    print("=" * 70)
    print(f"Total: {total}  |  OK: {ok}  |  Failed: {bad}")
    if bad:
        print("\nFailed:")
        for r in results:
            if r.exit_code == "1":
                first_msg = r.msg.split(",", 1)[0] if r.msg else ""
                print(f"  [{r.environment}/{r.target_kind:8s}] "
                      f"{r.url_id} {r.url} -> {first_msg}")
