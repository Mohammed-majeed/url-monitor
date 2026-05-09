"""
external_url_monitor.py
=======================
Runs URL checks from OUTSIDE the corporate network. Designed to be invoked
by a GitHub Actions workflow.

The orchestrator dispatches this to GH Actions with a list of targets and
optional runner settings from url_monitor_config.ini. The workflow uploads the
resulting log file as an artifact.

Usage from CLI:
    python -m modules.external_url_monitor --targets-stdin
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, List, Optional, Tuple

from .checker import CheckResult, check_one
from .inventory import CheckTarget, InventoryDefaults, load_inventory
from .log_writer import make_log_path, print_summary, write_results


DEFAULT_LOG_DIR = "External_url_logs"
DEFAULT_CONCURRENCY = 25
DEFAULT_TIMEZONE = "Europe/Amsterdam"


def force_timezone(timezone_name: str = DEFAULT_TIMEZONE) -> None:
    """
    Force local process timezone.

    This is important on GitHub Actions because the runner normally uses UTC.
    Our checker and log_writer use local time, so setting TZ here makes:
      - starting_time
      - finish_time
      - log filename timestamp
    use the configured timezone.
    """
    os.environ["TZ"] = timezone_name or DEFAULT_TIMEZONE

    # Works on Linux/macOS. GitHub Actions ubuntu-latest supports this.
    if hasattr(time, "tzset"):
        time.tzset()


# Backward-compatible name used by older imports.
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
    Check the given targets in parallel from the external runner perspective.
    Writes a JSON-lines log file to `log_dir`.
    """
    force_timezone(timezone)

    targets = list(targets)
    if not targets:
        print("No external targets to check.")
        return []

    log_path = make_log_path(log_dir)

    def _do(t: CheckTarget) -> CheckResult:
        # On GitHub Actions there is normally no corporate proxy.
        # Proxy is only used if explicitly passed and target.use_proxy=True.
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
    print(f"📝 External log: {log_path}  ({len(results)} entries)")
    return results


def _payload_to_targets_and_settings(data) -> Tuple[List[CheckTarget], dict]:
    """
    Accept both the old payload shape and the new config-driven shape:
      old: [target, target, ...]
      new: {"targets": [...], "settings": {...}}
    """
    if isinstance(data, dict):
        target_data = data.get("targets", [])
        settings = data.get("settings", {}) or {}
    else:
        target_data = data
        settings = {}
    return [CheckTarget(**d) for d in target_data], settings


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Run URL checks from an external runner, e.g. GitHub Actions.",
    )
    parser.add_argument(
        "--targets-json",
        required=False,
        help="Path to JSON file with a serialized target list or payload object.",
    )
    parser.add_argument(
        "--targets-stdin",
        action="store_true",
        help="Read targets JSON/payload from stdin instead of a file.",
    )
    parser.add_argument(
        "--inventory",
        help="Alternatively, an inventory xlsx. Uses ExternalRunner rows.",
    )
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

        all_targets = load_inventory(
            args.inventory,
            sheet_name=args.sheet_name,
            defaults=InventoryDefaults(),
        )
        _internal, targets = split_by_runner(all_targets)
        settings = {}
    else:
        print(
            "Provide --targets-json, --targets-stdin, or --inventory",
            file=sys.stderr,
        )
        sys.exit(2)

    # Settings from monitor_main.py config override CLI defaults when present.
    log_dir = settings.get("external_log_dir", args.log_dir)
    concurrency = int(settings.get("external_concurrency", args.concurrency))
    verify_ssl = bool(settings.get("external_verify_ssl", not args.no_verify_ssl))
    application = settings.get("external_application", args.application)
    action = settings.get("external_action", args.action)
    user_agent = settings.get("user_agent", args.user_agent)
    timezone = settings.get("external_timezone", args.timezone)

    results = run_external(
        targets,
        log_dir=log_dir,
        concurrency=concurrency,
        verify_ssl=verify_ssl,
        application=application,
        action=action,
        user_agent=user_agent,
        timezone=timezone,
    )
    print_summary(results)


if __name__ == "__main__":
    _main()
