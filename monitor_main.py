"""
monitor_main.py
===============
Config-driven orchestrator for URL monitoring.

Flow:
    1. Load inventory from Excel
    2. Split targets by runner:
         - InternalRunner -> run locally
         - ExternalRunner -> dispatch to GitHub Actions repository_dispatch
    3. Wait, fetch the GH artifact, extract logs
    4. Optionally delete the GH artifact
    5. Combine internal+external logs into one Splunk-ready file
    6. Write optional per-URL detail log (separate from combined log)

All operational parameters are controlled from url_monitor_config.ini.
"""
from __future__ import annotations

import configparser
import dataclasses
import json
import logging
import os
import time
import zipfile
from pathlib import Path
from typing import Iterable, List, Optional

import requests

from modules.checker import CheckResult
from modules.inventory import (
    CheckTarget,
    InventoryDefaults,
    load_inventory,
    split_by_runner,
)
from modules.internal_url_monitor import run_internal
from modules.log_writer import (
    cleanup_old,
    print_summary,
    setup_logging,
    write_detail_log,
)

logger = logging.getLogger("url_monitor.main")


# ─── Config helpers ────────────────────────────────────────────────────────────

def _getbool(parser: configparser.ConfigParser, section: str, key: str, default: bool) -> bool:
    if not parser.has_section(section):
        return default
    try:
        return parser.getboolean(section, key, fallback=default)
    except ValueError:
        return default


def _getint(parser: configparser.ConfigParser, section: str, key: str, default: int) -> int:
    if not parser.has_section(section):
        return default
    try:
        return parser.getint(section, key, fallback=default)
    except ValueError:
        return default


def _getstr(parser: configparser.ConfigParser, section: str, key: str, default: str = "") -> str:
    if not parser.has_section(section):
        return default
    return parser.get(section, key, fallback=default).strip()


# ─── Cfg ───────────────────────────────────────────────────────────────────────

class Cfg:
    # Inventory
    inventory_path: str = "url_monitoring_inventory.xlsx"
    sheet_name: str = "URL_monitoring"
    inventory_defaults: InventoryDefaults = InventoryDefaults()

    # Paths
    combined_log_dir: str = "."
    internal_log_dir: str = "Internal_url_logs"
    external_log_dir: str = "External_url_logs"

    # Logging
    run_log_dir: str = "Run_logs"          # directory for url_monitor_run.log
    url_detail_log: bool = True            # per-URL detail log on/off
    url_detail_log_dir: str = "Detail_logs"  # directory for url_detail_*.log

    # Runner/runtime
    dispatch_external: bool = True
    wait_seconds: int = 60
    internal_concurrency: int = 25
    external_concurrency: int = 25
    internal_verify_ssl: bool = False
    external_verify_ssl: bool = True
    cleanup_days: int = 14
    internal_application: str = "URLMonitor"
    internal_action: str = "url_monitor"
    external_application: str = "URLMonitor_v2"
    external_action: str = "URLMonitor_v2"
    user_agent: str = "URLMonitor"
    external_timezone: str = "Europe/Amsterdam"

    # GitHub
    github_owner: str = ""
    github_repo: str = ""
    event_type: str = "url-monitor-check"
    api_base: str = "https://api.github.com"
    token: str = ""
    token_env_var: str = "GITHUB_TOKEN"
    artifact_name: str = "url-monitor-logs"
    delete_artifact_after_download: bool = True
    github_api_timeout_seconds: int = 30
    github_download_timeout_seconds: int = 60
    github_api_verify_ssl: bool = False

    # Proxy (corporate — for internal checks and GitHub API calls ONLY)
    proxies: dict = {}

    @classmethod
    def load(cls, path: str) -> "Cfg":
        parser = configparser.ConfigParser(interpolation=None)
        with open(path, "r", encoding="utf-8") as f:
            parser.read_file(f)

        c = cls()

        old_log_dir = _getstr(parser, "inventory", "log_directory", c.combined_log_dir)

        c.inventory_path = _getstr(parser, "inventory", "inventory_path", c.inventory_path)
        c.sheet_name = _getstr(parser, "inventory", "sheet_name", c.sheet_name)
        c.inventory_defaults = InventoryDefaults(
            expected_statuses=_getstr(parser, "inventory", "default_expected_statuses", "200-399;401-login"),
            read_body=_getbool(parser, "inventory", "default_read_body", True),
            max_body_bytes=_getint(parser, "inventory", "default_max_body_bytes", 200000),
            timeout_seconds=_getint(parser, "inventory", "default_timeout_seconds", 10),
            external_proxy=_getbool(parser, "inventory", "default_external_proxy", False),
            internal_proxy=_getbool(parser, "inventory", "default_internal_proxy", False),
            ingress_proxy=_getbool(parser, "inventory", "default_ingress_proxy", False),
            external_runner=_getstr(parser, "inventory", "default_external_runner", "ExternalRunner"),
            internal_runner=_getstr(parser, "inventory", "default_internal_runner", "InternalRunner"),
            ingress_runner=_getstr(parser, "inventory", "default_ingress_runner", "InternalRunner"),
            legacy_enabled_default=_getbool(parser, "inventory", "legacy_enabled_default", True),
            missing_enable_column_default=_getbool(parser, "inventory", "missing_enable_column_default", True),
            blank_enable_value_default=_getbool(parser, "inventory", "blank_enable_value_default", False),
        )

        c.combined_log_dir  = _getstr(parser, "paths", "combined_log_dir", old_log_dir)
        c.internal_log_dir  = _getstr(parser, "paths", "internal_log_dir", c.internal_log_dir)
        c.external_log_dir  = _getstr(parser, "paths", "external_log_dir", c.external_log_dir)

        # ── Logging section ──────────────────────────────────────────────────
        c.run_log_dir        = _getstr(parser, "logging", "run_log_dir", c.run_log_dir)
        c.url_detail_log     = _getbool(parser, "logging", "url_detail_log", c.url_detail_log)
        c.url_detail_log_dir = _getstr(parser, "logging", "url_detail_log_dir", c.url_detail_log_dir)

        c.dispatch_external     = _getbool(parser, "runner", "dispatch_external", True)
        c.wait_seconds          = _getint(parser, "runner", "wait_seconds", 60)
        c.internal_concurrency  = _getint(parser, "runner", "internal_concurrency", 25)
        c.external_concurrency  = _getint(parser, "runner", "external_concurrency", 25)
        c.internal_verify_ssl   = _getbool(parser, "runner", "internal_verify_ssl", False)
        c.external_verify_ssl   = _getbool(parser, "runner", "external_verify_ssl", True)
        c.cleanup_days          = _getint(parser, "runner", "cleanup_days", 14)
        c.internal_application  = _getstr(parser, "runner", "internal_application", "URLMonitor")
        c.internal_action       = _getstr(parser, "runner", "internal_action", "url_monitor")
        c.external_application  = _getstr(parser, "runner", "external_application", "URLMonitor_v2")
        c.external_action       = _getstr(parser, "runner", "external_action", "URLMonitor_v2")
        c.user_agent            = _getstr(parser, "runner", "user_agent", "URLMonitor")
        c.external_timezone     = _getstr(parser, "runner", "external_timezone", "Europe/Amsterdam")

        c.github_owner                    = _getstr(parser, "github", "owner", "")
        c.github_repo                     = _getstr(parser, "github", "repo", "")
        c.event_type                      = _getstr(parser, "github", "event_type", "url-monitor-check")
        c.api_base                        = _getstr(parser, "github", "api_base", "https://api.github.com").rstrip("/")
        raw_token                         = _getstr(parser, "github", "token", "")
        c.token_env_var                   = _getstr(parser, "github", "token_env_var", "GITHUB_TOKEN")
        c.token                           = raw_token or os.environ.get(c.token_env_var, "").strip()
        c.artifact_name                   = _getstr(parser, "github", "artifact_name", "url-monitor-logs")
        c.delete_artifact_after_download  = _getbool(parser, "github", "delete_artifact_after_download", True)
        c.github_api_timeout_seconds      = _getint(parser, "github", "api_timeout_seconds", 30)
        c.github_download_timeout_seconds = _getint(parser, "github", "artifact_download_timeout_seconds", 60)
        c.github_api_verify_ssl           = _getbool(parser, "github", "api_verify_ssl", False)

        proxies: dict = {}
        http_proxy  = _getstr(parser, "proxy", "http", "")
        https_proxy = _getstr(parser, "proxy", "https", "")
        if http_proxy:
            proxies["http"] = http_proxy
        if https_proxy:
            proxies["https"] = https_proxy
        c.proxies = proxies

        return c

    def gh_headers(self) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def external_runner_settings(self) -> dict:
        return {
            "external_log_dir":    self.external_log_dir,
            "external_concurrency": self.external_concurrency,
            "external_verify_ssl": self.external_verify_ssl,
            "external_application": self.external_application,
            "external_action":     self.external_action,
            "user_agent":          self.user_agent,
            "external_timezone":   self.external_timezone,
            # No proxy key: GitHub Actions has no corporate proxy.
        }


# ─── GitHub Actions dispatch ───────────────────────────────────────────────────

def _serialize(targets: Iterable[CheckTarget]) -> List[dict]:
    """
    Serialize targets for GitHub Actions dispatch.
    Forces use_proxy=False: GitHub runner has no corporate proxy.
    """
    result = []
    for t in targets:
        d = dataclasses.asdict(t)
        d["use_proxy"] = False
        result.append(d)
    return result


def trigger_dispatch(cfg: Cfg, targets: List[CheckTarget]) -> None:
    url = f"{cfg.api_base}/repos/{cfg.github_owner}/{cfg.github_repo}/dispatches"
    payload = {
        "event_type": cfg.event_type,
        "client_payload": {
            "targets":  _serialize(targets),
            "settings": cfg.external_runner_settings(),
        },
    }
    logger.info("Dispatching to GitHub Actions  repo=%s/%s  targets=%d",
                cfg.github_owner, cfg.github_repo, len(targets))
    r = requests.post(
        url,
        headers=cfg.gh_headers(),
        json=payload,
        proxies=cfg.proxies or None,
        timeout=cfg.github_api_timeout_seconds,
        verify=cfg.github_api_verify_ssl,
    )
    r.raise_for_status()
    logger.info("Dispatch accepted  event=%s  http=%d", cfg.event_type, r.status_code)


def get_latest_run_id(cfg: Cfg) -> int:
    url = f"{cfg.api_base}/repos/{cfg.github_owner}/{cfg.github_repo}/actions/runs"
    params = {"event": "repository_dispatch", "per_page": 1}
    logger.info("Fetching latest workflow run id  repo=%s/%s", cfg.github_owner, cfg.github_repo)
    r = requests.get(
        url,
        headers=cfg.gh_headers(),
        params=params,
        proxies=cfg.proxies or None,
        timeout=cfg.github_api_timeout_seconds,
        verify=cfg.github_api_verify_ssl,
    )
    r.raise_for_status()
    runs = r.json().get("workflow_runs", [])
    if not runs:
        raise RuntimeError("No dispatch runs found")
    run_id = runs[0]["id"]
    logger.info("Latest run id=%d  status=%s  conclusion=%s",
                run_id, runs[0].get("status"), runs[0].get("conclusion"))
    return run_id


def get_artifact_info(cfg: Cfg, run_id: int) -> dict:
    url = f"{cfg.api_base}/repos/{cfg.github_owner}/{cfg.github_repo}/actions/runs/{run_id}/artifacts"
    logger.info("Fetching artifact list  run_id=%d  artifact_name=%s", run_id, cfg.artifact_name)
    r = requests.get(
        url,
        headers=cfg.gh_headers(),
        proxies=cfg.proxies or None,
        timeout=cfg.github_api_timeout_seconds,
        verify=cfg.github_api_verify_ssl,
    )
    r.raise_for_status()
    for art in r.json().get("artifacts", []):
        if art.get("name") == cfg.artifact_name:
            logger.info("Found artifact  id=%s  size_mb=%.2f",
                        art["id"], art.get("size_in_bytes", 0) / 1_048_576)
            return art
    raise RuntimeError(f"Artifact '{cfg.artifact_name}' not found in run {run_id}")


def delete_artifact(cfg: Cfg, artifact_id: int) -> None:
    url = f"{cfg.api_base}/repos/{cfg.github_owner}/{cfg.github_repo}/actions/artifacts/{artifact_id}"
    logger.info("Deleting GitHub artifact  id=%d", artifact_id)
    r = requests.delete(
        url,
        headers=cfg.gh_headers(),
        proxies=cfg.proxies or None,
        timeout=cfg.github_api_timeout_seconds,
        verify=cfg.github_api_verify_ssl,
    )
    if r.status_code == 204:
        logger.info("Artifact deleted  id=%d", artifact_id)
        return
    if r.status_code == 404:
        logger.warning("Artifact already deleted or not found  id=%d", artifact_id)
        return
    r.raise_for_status()


def download_and_extract(cfg: Cfg, zip_url: str) -> None:
    os.makedirs(cfg.external_log_dir, exist_ok=True)
    logger.info("Downloading artifact  url=%s", zip_url)
    r = requests.get(
        zip_url,
        headers=cfg.gh_headers(),
        proxies=cfg.proxies or None,
        stream=True,
        timeout=cfg.github_download_timeout_seconds,
        verify=cfg.github_api_verify_ssl,
    )
    r.raise_for_status()
    zip_path = os.path.join(cfg.external_log_dir, "url-monitor-logs.zip")
    total_bytes = 0
    with open(zip_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
            total_bytes += len(chunk)
    logger.info("Downloaded %.1f KB", total_bytes / 1024)
    with zipfile.ZipFile(zip_path, "r") as z:
        names = z.namelist()
        z.extractall(cfg.external_log_dir)
    logger.info("Extracted %d file(s) into %s", len(names), cfg.external_log_dir)
    try:
        os.remove(zip_path)
    except OSError:
        pass


# ─── Combine logs ──────────────────────────────────────────────────────────────

def _read_jsonl(path: str) -> List[dict]:
    out = []
    if not os.path.isfile(path):
        return out
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _latest_log(folder: str) -> str:
    if not os.path.isdir(folder):
        return ""
    candidates = [os.path.join(folder, f) for f in os.listdir(folder)
                  if f.endswith(".log") and not f.startswith("url_monitor_run")]
    if not candidates:
        return ""
    return max(candidates, key=os.path.getmtime)


def combine_logs(cfg: Cfg) -> str:
    """Combine latest internal + external log files into one Splunk-ready file."""
    internal = _read_jsonl(_latest_log(cfg.internal_log_dir))
    external = _read_jsonl(_latest_log(cfg.external_log_dir))

    os.makedirs(cfg.combined_log_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(cfg.combined_log_dir, f"url_monitor_{ts}.log")

    with open(out_path, "w", encoding="utf-8") as f:
        for obj in internal + external:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    logger.info("Combined log written: %s  (%d internal + %d external = %d entries)",
                out_path, len(internal), len(external), len(internal) + len(external))

    cleanup_old(cfg.internal_log_dir, days=cfg.cleanup_days)
    cleanup_old(cfg.external_log_dir, days=cfg.cleanup_days)
    cleanup_old(cfg.combined_log_dir, days=cfg.cleanup_days)
    if cfg.url_detail_log:
        cleanup_old(cfg.url_detail_log_dir, days=cfg.cleanup_days)
    return out_path


# ─── Main flow ─────────────────────────────────────────────────────────────────

def _to_results(combined_path: str) -> List[CheckResult]:
    """Re-hydrate combined JSONL into CheckResult objects (best-effort)."""
    objs = _read_jsonl(combined_path)
    results = []
    field_names = {f.name for f in dataclasses.fields(CheckResult)}
    for o in objs:
        clean = {k: v for k, v in o.items() if k in field_names}
        try:
            results.append(CheckResult(**clean))
        except TypeError:
            pass
    return results


def main(
    config_path: str = "url_monitor_config.ini",
    dispatch_external: Optional[bool] = None,
    wait_seconds: Optional[int] = None,
) -> None:
    cfg = Cfg.load(config_path)

    # ── Initialise logging (first thing after config is loaded) ─────────────
    setup_logging(log_dir=cfg.run_log_dir)
    logger.info("Config loaded  path=%s", config_path)
    logger.info("Inventory: %s  sheet=%s", cfg.inventory_path, cfg.sheet_name)
    logger.info("Log dirs  internal=%s  external=%s  combined=%s",
                cfg.internal_log_dir, cfg.external_log_dir, cfg.combined_log_dir)
    logger.info("Detail log  enabled=%s  dir=%s",
                cfg.url_detail_log, cfg.url_detail_log_dir)

    if dispatch_external is not None:
        cfg.dispatch_external = dispatch_external
    if wait_seconds is not None:
        cfg.wait_seconds = wait_seconds

    # ── Load inventory ────────────────────────────────────────────────────────
    all_targets = load_inventory(
        cfg.inventory_path,
        sheet_name=cfg.sheet_name,
        defaults=cfg.inventory_defaults,
    )
    internal, external = split_by_runner(all_targets)
    logger.info("Targets loaded  total=%d  internal=%d  external=%d",
                len(all_targets), len(internal), len(external))

    can_dispatch = bool(
        cfg.dispatch_external and external
        and cfg.token and cfg.github_owner and cfg.github_repo
    )

    if can_dispatch:
        trigger_dispatch(cfg, external)
    elif external and cfg.dispatch_external:
        logger.warning("No complete GitHub config/token — running external targets locally.")

    # ── Internal checks ───────────────────────────────────────────────────────
    proxies = cfg.proxies or None
    internal_results = run_internal(
        internal,
        log_dir=cfg.internal_log_dir,
        concurrency=cfg.internal_concurrency,
        proxies=proxies,
        verify_ssl=cfg.internal_verify_ssl,
        application=cfg.internal_application,
        action=cfg.internal_action,
        user_agent=cfg.user_agent,
    )

    # ── Wait for and fetch GitHub artifact ───────────────────────────────────
    if can_dispatch:
        logger.info("Waiting %ds for external workflow to complete…", cfg.wait_seconds)
        time.sleep(cfg.wait_seconds)
        try:
            run_id   = get_latest_run_id(cfg)
            artifact = get_artifact_info(cfg, run_id)
            download_and_extract(cfg, artifact["archive_download_url"])
            if cfg.delete_artifact_after_download:
                delete_artifact(cfg, int(artifact["id"]))
        except Exception as e:
            logger.error("Could not fetch/delete external artifact: %s", e)
    else:
        from modules.external_url_monitor import run_external
        run_external(
            external,
            log_dir=cfg.external_log_dir,
            concurrency=cfg.external_concurrency,
            proxies=None,
            verify_ssl=cfg.external_verify_ssl,
            application=cfg.external_application,
            action=cfg.external_action,
            user_agent=cfg.user_agent,
            timezone=cfg.external_timezone,
        )

    # ── Combine logs (Splunk) ─────────────────────────────────────────────────
    combined_path = combine_logs(cfg)

    # ── Per-URL detail log (separate, toggleable) ─────────────────────────────
    all_results = _to_results(combined_path)
    write_detail_log(
        log_dir=cfg.url_detail_log_dir,
        results=all_results,
        enabled=cfg.url_detail_log,
    )

    print_summary(all_results)
    logger.info("Run complete.")


# ─── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="URL Monitor orchestrator")
    p.add_argument("--config", default="url_monitor_config.ini")
    p.add_argument("--no-dispatch", action="store_true",
                   help="Run external checks locally instead of dispatching to GitHub Actions.")
    p.add_argument("--wait", type=int, default=None,
                   help="Override [runner] wait_seconds for this run only.")
    args = p.parse_args()

    main(
        config_path=args.config,
        dispatch_external=False if args.no_dispatch else None,
        wait_seconds=args.wait,
    )
