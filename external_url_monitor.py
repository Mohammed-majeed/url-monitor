"""
external_url_monitor.py
=======================
Runs URL checks from OUTSIDE the corporate network. Designed to be invoked
by a GitHub Actions workflow (mirroring external_ssl_cert_inspecter.py).

The orchestrator dispatches this to GH Actions with a list of targets;
the workflow uploads the resulting log file as an artifact.

Usage from CLI (this is what GH Actions calls):
    python -m url_monitor.external_url_monitor --targets-json targets.json
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, List, Optional

from .checker import CheckResult, check_one
from .inventory import CheckTarget, load_inventory
from .log_writer import make_log_path, print_summary, write_results


DEFAULT_LOG_DIR = "External_url_logs"
DEFAULT_CONCURRENCY = 25


def run_external(
    targets: Iterable[CheckTarget],
    *,
    log_dir: str = DEFAULT_LOG_DIR,
    concurrency: int = DEFAULT_CONCURRENCY,
    proxies: Optional[dict] = None,        # usually None on GH runners
    verify_ssl: bool = True,               # external = trusted CA bundle
    application: str = "URLMonitor",
    action: str = "url_monitor",
) -> List[CheckResult]:
    """
    Check the given targets in parallel from the external runner perspective.
    Writes a JSON-lines log file to `log_dir`.
    """
    targets = list(targets)
    if not targets:
        print("No external targets to check.")
        return []

    log_path = make_log_path(log_dir)

    def _do(t: CheckTarget) -> CheckResult:
        # On GH Actions there's typically no corporate proxy. We honor
        # use_proxy if the caller passed one in.
        return check_one(
            t,
            proxies=proxies,
            verify_ssl=verify_ssl,
            application=application,
            action=action,
        )

    results: List[CheckResult] = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        for r in ex.map(_do, targets):
            results.append(r)

    results.sort(key=lambda r: (r.environment, r.app_name, r.url_id, r.target_kind))
    write_results(log_path, results, echo=False)
    print(f"📝 External log: {log_path}  ({len(results)} entries)")
    return results


# ─── CLI (for GitHub Actions) ──────────────────────────────────────────────────

def _main():
    parser = argparse.ArgumentParser(
        description="Run URL checks from an external runner (e.g. GitHub Actions).",
    )
    parser.add_argument("--targets-json", required=False,
                        help="Path to JSON file with a serialized target list.")
    parser.add_argument("--targets-stdin", action="store_true",
                        help="Read targets JSON from stdin instead of a file.")
    parser.add_argument("--inventory",
                        help="Alternatively, an inventory xlsx (uses ExternalRunner rows).")
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--no-verify-ssl", action="store_true")
    args = parser.parse_args()

    if args.targets_json:
        with open(args.targets_json, "r", encoding="utf-8") as f:
            data = json.load(f)
    elif args.targets_stdin:
        data = json.load(sys.stdin)
    elif args.inventory:
        from .inventory import split_by_runner
        all_targets = load_inventory(args.inventory)
        _internal, external = split_by_runner(all_targets)
        results = run_external(external,
                               log_dir=args.log_dir,
                               concurrency=args.concurrency,
                               verify_ssl=not args.no_verify_ssl)
        print_summary(results)
        return
    else:
        print("Provide --targets-json, --targets-stdin, or --inventory",
              file=sys.stderr)
        sys.exit(2)

    targets = [CheckTarget(**d) for d in data]
    results = run_external(
        targets,
        log_dir=args.log_dir,
        concurrency=args.concurrency,
        verify_ssl=not args.no_verify_ssl,
    )
    print_summary(results)


if __name__ == "__main__":
    _main()
