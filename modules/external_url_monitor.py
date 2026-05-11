"""
external_url_monitor.py
=======================
Runs URL checks from OUTSIDE the corporate network (GitHub Actions runner).
proxies is always None here — GitHub Actions has no corporate proxy.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, List, Optional, Tuple

from .checker import CheckResult, check_one
from .inventory import CheckTarget, InventoryDefaults, load_inventory
from .log_writer import make_log_path, print_summary, write_results

logger = logging.getLogger("url_monitor.external")

DEFAULT_LOG_DIR = "External_url_logs"
DEFAULT_CONCURRENCY = 25
DEFAULT_TIMEZONE = "Europe/Amsterdam"


def force_timezone(timezone_name: str = DEFAULT_TIMEZONE) -> None:
    os.environ["TZ"] = timezone_name or DEFAULT_TIMEZONE
    if hasattr(time, "tzset"):
        time.tzset()


def force_amsterdam_time() -> None:
    force_timezone(DEFAULT_TIMEZONE)


def run_external(
    targets: Iterable[CheckTarget],
    *,
    log_dir: str = DEFAULT_LOG_DIR,
    concurrency: int = DEFAULT_CONCURRENCY,
    proxies: Optional[dict] = None,
    verify_ssl: bool = True,
    application: str = "URLMonitor_v2",
    action: str = "URLMonitor_v2",
    user_agent: str = "URLMonitor",
    timezone: str = DEFAULT_TIMEZONE,
) -> List[CheckResult]:
    """
    Check external targets in parallel. proxies is always ignored here —
    the GitHub Actions runner is outside the corporate network.
    """
    force_timezone(timezone)

    targets = list(targets)
    if not targets:
        logger.info("No external targets to check.")
        return []

    logger.info("Starting external checks  targets=%d  concurrency=%d  verify_ssl=%s",
                len(targets), concurrency, verify_ssl)

    log_path = make_log_path(log_dir)
    logger.info("External log file: %s", log_path)

    def _do(t: CheckTarget) -> CheckResult:
        # Never use corporate proxy on external runner
        return check_one(
            t,
            proxies=None,
            verify_ssl=verify_ssl,
            application=application,
            action=action,
            user_agent=user_agent,
        )

    results: List[CheckResult] = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        for r in ex.map(_do, targets):
            results.append(r)

    results.sort(key=lambda r: (r.environment, r.app_name, r.url_id, r.target_kind))
    write_results(log_path, results, echo=False)

    ok = sum(1 for r in results if r.exit_code == "0")
    logger.info("External checks complete  total=%d  ok=%d  failed=%d  log=%s",
                len(results), ok, len(results) - ok, log_path)
    return results


def _payload_to_targets_and_settings(data) -> Tuple[List[CheckTarget], dict]:
    if isinstance(data, dict):
        target_data = data.get("targets", [])
        settings = data.get("settings", {}) or {}
    else:
        target_data = data
        settings = {}
    return [CheckTarget(**d) for d in target_data], settings


def _main() -> None:
    parser = argparse.ArgumentParser(description="Run URL checks from an external runner.")
    parser.add_argument("--targets-json", required=False)
    parser.add_argument("--targets-stdin", action="store_true")
    parser.add_argument("--inventory")
    parser.add_argument("--sheet-name", default="URL_monitoring")
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--no-verify-ssl", action="store_true")
    parser.add_argument("--application", default="URLMonitor_v2")
    parser.add_argument("--action", default="URLMonitor_v2")
    parser.add_argument("--user-agent", default="URLMonitor")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    args = parser.parse_args()

    if args.targets_json:
        with open(args.targets_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        targets, settings = _payload_to_targets_and_settings(data)
    elif args.targets_stdin:
        data = json.load(sys.stdin)
        targets, settings = _payload_to_targets_and_settings(data)
    elif args.inventory:
        from .inventory import split_by_runner
        all_targets = load_inventory(args.inventory, sheet_name=args.sheet_name,
                                     defaults=InventoryDefaults())
        _internal, targets = split_by_runner(all_targets)
        settings = {}
    else:
        print("Provide --targets-json, --targets-stdin, or --inventory", file=sys.stderr)
        sys.exit(2)

    log_dir    = settings.get("external_log_dir", args.log_dir)
    concurrency = int(settings.get("external_concurrency", args.concurrency))
    verify_ssl  = bool(settings.get("external_verify_ssl", not args.no_verify_ssl))
    application = settings.get("external_application", args.application)
    action      = settings.get("external_action", args.action)
    user_agent  = settings.get("user_agent", args.user_agent)
    timezone    = settings.get("external_timezone", args.timezone)

    results = run_external(
        targets,
        log_dir=log_dir,
        concurrency=concurrency,
        proxies=None,
        verify_ssl=verify_ssl,
        application=application,
        action=action,
        user_agent=user_agent,
        timezone=timezone,
    )
    print_summary(results)


if __name__ == "__main__":
    _main()
