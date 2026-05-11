"""
internal_url_monitor.py
=======================
Runs URL checks LOCALLY from inside the corporate network.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, List, Optional

from .checker import CheckResult, check_one
from .inventory import CheckTarget, load_inventory
from .log_writer import make_log_path, print_summary, write_results

logger = logging.getLogger("url_monitor.internal")

DEFAULT_LOG_DIR = "Internal_url_logs"
DEFAULT_CONCURRENCY = 25


def run_internal(
    targets: Iterable[CheckTarget],
    *,
    log_dir: str = DEFAULT_LOG_DIR,
    concurrency: int = DEFAULT_CONCURRENCY,
    proxies: Optional[dict] = None,
    verify_ssl: bool = False,
    application: str = "URLMonitor",
    action: str = "url_monitor",
    user_agent: str = "URLMonitor",
) -> List[CheckResult]:
    """
    Check internal targets in parallel and write a JSON-lines log file.
    """
    targets = list(targets)
    if not targets:
        logger.info("No internal targets to check.")
        return []

    logger.info("Starting internal checks  targets=%d  concurrency=%d  verify_ssl=%s",
                len(targets), concurrency, verify_ssl)

    log_path = make_log_path(log_dir)
    logger.info("Internal log file: %s", log_path)

    def _do(t: CheckTarget) -> CheckResult:
        return check_one(
            t,
            proxies=proxies,
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
    logger.info("Internal checks complete  total=%d  ok=%d  failed=%d  log=%s",
                len(results), ok, len(results) - ok, log_path)
    return results


# ─── CLI ───────────────────────────────────────────────────────────────────────

def _main():
    parser = argparse.ArgumentParser(description="Run URL checks locally for internal targets.")
    parser.add_argument("inventory", nargs="?", help="Path to url_monitoring_inventory.xlsx")
    parser.add_argument("--targets-json", help="Path to a JSON file with a serialized target list.")
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--verify-ssl", action="store_true")
    parser.add_argument("--application", default="URLMonitor")
    parser.add_argument("--action", default="url_monitor")
    parser.add_argument("--user-agent", default="URLMonitor")
    parser.add_argument("--proxy", default="",
                        help="Optional proxy URL. Only applied to targets with use_proxy=True.")
    args = parser.parse_args()

    if args.targets_json:
        with open(args.targets_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        targets = [CheckTarget(**d) for d in data]
    elif args.inventory:
        from .inventory import split_by_runner
        all_targets = load_inventory(args.inventory)
        internal, _external = split_by_runner(all_targets)
        targets = internal
    else:
        print("Provide either an inventory path or --targets-json", file=sys.stderr)
        sys.exit(2)

    proxies = {"http": args.proxy, "https": args.proxy} if args.proxy else None

    results = run_internal(
        targets,
        log_dir=args.log_dir,
        concurrency=args.concurrency,
        proxies=proxies,
        verify_ssl=args.verify_ssl,
        application=args.application,
        action=args.action,
        user_agent=args.user_agent,
    )
    print_summary(results)


if __name__ == "__main__":
    _main()
