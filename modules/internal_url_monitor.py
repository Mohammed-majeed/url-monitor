"""
internal_url_monitor.py
=======================
Runs URL checks LOCALLY from inside the corporate network.

Mirrors internal_ssl_cert_inspecter.py: takes a list of targets, returns a
list of CheckResults, writes a JSON-lines log file.

Use this for any CheckTarget whose runner is `InternalRunner` (or empty).
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, List, Optional

from .checker import CheckResult, check_one
from .inventory import CheckTarget, load_inventory
from .log_writer import make_log_path, print_summary, write_results


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
    Check the given targets in parallel and write a single JSON-lines log
    file under `log_dir`. Returns the list of CheckResults.
    """
    targets = list(targets)
    if not targets:
        print("No internal targets to check.")
        return []

    log_path = make_log_path(log_dir)

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

    # Stable sort: by env, app, url_id
    results.sort(key=lambda r: (r.environment, r.app_name, r.url_id, r.target_kind))
    write_results(log_path, results, echo=False)
    print(f"📝 Internal log: {log_path}  ({len(results)} entries)")
    return results


# ─── CLI ───────────────────────────────────────────────────────────────────────

def _main():
    parser = argparse.ArgumentParser(
        description="Run URL checks locally for internal targets.",
    )
    parser.add_argument("inventory", nargs="?",
                        help="Path to url_monitoring_inventory.xlsx")
    parser.add_argument("--targets-json",
                        help="Alternatively, path to a JSON file containing "
                             "a serialized target list (used by the orchestrator).")
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
