"""Cloud lab worker — GCP metadata or environment config."""

from __future__ import annotations

import logging
import os
import urllib.request
from dataclasses import dataclass

from lib.cloud_lab.worker.shell import log

METADATA_URL = "http://metadata.google.internal/computeMetadata/v1/instance/attributes"
@dataclass
class WorkerConfig:
    run_id: str
    sut_branch: str
    sut_commit: str
    sut_pr: str
    artifact_run_id: str
    artifact_repo: str
    pr_repo: str
    suite_ref: str
    backend: str
    reseller_scenarios: bool
    two_router: bool
    secondary_router_host: str
    secondary_router_port: str
    keep_vm_on_failure: bool
    publish: bool
    project: str
    zone: str
    vm_name: str
    gh_token: str
    mint: str
    portal: str
    quick: bool
    hwsim_enabled: bool
    vwifi_enabled: bool
    smoke: bool
    complete: bool
    wifi_plane: str
    lease_minutes: int = 60
    benchmark: bool = False  # Run optional post-test benchmarks
    runner_mode: bool = False  # True when running inside GitHub Actions self-hosted runner
def _metadata_get(key: str) -> str:
    req = urllib.request.Request(
        f"{METADATA_URL}/{key}",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode().strip()
def _metadata_get_optional(key: str, default: str = "") -> str:
    try:
        return _metadata_get(key)
    except Exception:
        return default
def load_config_from_metadata() -> WorkerConfig:
    cfg = WorkerConfig(
        run_id=_metadata_get("tollgate-run-id"),
        sut_branch=_metadata_get("tollgate-sut-branch"),
        sut_commit=_metadata_get("tollgate-sut-commit"),
        sut_pr=_metadata_get("tollgate-pr"),
        artifact_run_id=_metadata_get("tollgate-artifact-run-id"),
        artifact_repo=_metadata_get("tollgate-artifact-repo"),
        pr_repo=_metadata_get_optional("tollgate-pr-repo"),
        suite_ref=_metadata_get("tollgate-suite-ref"),
        backend=_metadata_get("tollgate-backend"),
        reseller_scenarios=_metadata_get_optional("tollgate-reseller-scenarios").lower() in ("true", "1", "yes"),
        two_router=_metadata_get_optional("tollgate-two-router").lower() in ("true", "1", "yes"),
        secondary_router_host=_metadata_get_optional("tollgate-secondary-router-host"),
        secondary_router_port=_metadata_get_optional("tollgate-secondary-router-port"),
        keep_vm_on_failure=_metadata_get_optional("tollgate-keep-vm-on-failure").lower() not in ("false", "0", "no"),
        publish=_metadata_get("tollgate-publish").lower() in ("true", "1", "yes"),
        project=_metadata_get("tollgate-project"),
        zone=_metadata_get("tollgate-zone"),
        vm_name=_metadata_get("tollgate-vm-name"),
        gh_token=_metadata_get("tollgate-gh-token"),
        mint=_metadata_get_optional("tollgate-mint", "auto"),
        portal=_metadata_get_optional("tollgate-portal", "builtin"),
        hwsim_enabled=_metadata_get_optional("tollgate-hwsim").lower() in ("true", "1", "yes"),
        vwifi_enabled=_metadata_get_optional("tollgate-vwifi").lower() in ("true", "1", "yes"),
        quick=_metadata_get_optional("tollgate-quick").lower() in ("true", "1", "yes"),
        smoke=_metadata_get_optional("tollgate-smoke").lower() in ("true", "1", "yes"),
        complete=_metadata_get_optional("tollgate-complete").lower() in ("true", "1", "yes"),
        wifi_plane=_metadata_get_optional("tollgate-wifi-plane", "tap"),
        lease_minutes=int(_metadata_get_optional("tollgate-lease-minutes", "60")),
        benchmark=_metadata_get_optional("tollgate-benchmark").lower() in ("true", "1", "yes"),
    )
    log.info(
        "Config: run=%s branch=%s repo=%s backend=%s pr=%s publish=%s keep_on_fail=%s mint=%s portal=%s hwsim=%s vwifi=%s quick=%s smoke=%s complete=%s wifi_plane=%s",
        cfg.run_id, cfg.sut_branch, cfg.artifact_repo, cfg.backend,
        cfg.sut_pr or "(none)", cfg.publish, cfg.keep_vm_on_failure, cfg.mint, cfg.portal, cfg.hwsim_enabled, cfg.vwifi_enabled, cfg.quick, cfg.smoke, cfg.complete, cfg.wifi_plane,
    )
    log.info(
        "Artifact: run_id=%s suite_ref=%s reseller=%s secondary=%s",
        cfg.artifact_run_id, cfg.suite_ref[:7], cfg.reseller_scenarios, cfg.secondary_router_host or "(none)",
    )
    return cfg


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_bool(key: str, default: bool = False) -> bool:
    return _env(key).lower() in ("true", "1", "yes") if _env(key) else default


def load_config_from_env() -> WorkerConfig:
    """Load config from environment variables (GitHub Actions runner mode).

    Maps TOLLGATE_* env vars to WorkerConfig fields. Falls back to sensible
    defaults for fields that GitHub Actions provides automatically (gh_token
    from GITHUB_TOKEN, project/zone from GCP defaults).
    """
    cfg = WorkerConfig(
        runner_mode=True,
        run_id=_env("TOLLGATE_RUN_ID", f"gh-{_env('GITHUB_RUN_ID', 'unknown')}"),
        sut_branch=_env("TOLLGATE_SUT_BRANCH", "main"),
        sut_commit=_env("TOLLGATE_SUT_COMMIT", ""),
        sut_pr=_env("TOLLGATE_SUT_PR", ""),
        artifact_run_id=_env("TOLLGATE_ARTIFACT_RUN_ID", ""),
        artifact_repo=_env("TOLLGATE_ARTIFACT_REPO", "OpenTollGate/tollgate-module-basic-go"),
        pr_repo=_env("TOLLGATE_PR_REPO", ""),
        suite_ref=_env("GITHUB_SHA", "HEAD"),
        backend=_env("TOLLGATE_BACKEND", "go"),
        reseller_scenarios=_env_bool("TOLLGATE_RESELLER_SCENARIOS"),
        two_router=_env_bool("TOLLGATE_TWO_ROUTER"),
        secondary_router_host=_env("TOLLGATE_SECONDARY_ROUTER_HOST", ""),
        secondary_router_port=_env("TOLLGATE_SECONDARY_ROUTER_PORT", ""),
        keep_vm_on_failure=_env_bool("TOLLGATE_KEEP_VM_ON_FAILURE", default=True),
        publish=_env_bool("TOLLGATE_PUBLISH", default=True),
        project=_env("TOLLGATE_GCP_PROJECT", "tollgate-test-lab"),
        zone=_env("TOLLGATE_GCP_ZONE", "europe-west1-b"),
        vm_name=_env("TOLLGATE_VM_NAME", _env("HOSTNAME", "runner-vm")),
        gh_token=_env("GITHUB_TOKEN", _env("GH_TOKEN", "")),
        mint=_env("TOLLGATE_MINT", "auto"),
        portal=_env("TOLLGATE_PORTAL", "builtin"),
        quick=_env_bool("TOLLGATE_QUICK"),
        hwsim_enabled=_env_bool("TOLLGATE_HWSIM"),
        vwifi_enabled=_env_bool("TOLLGATE_VWIFI"),
        smoke=_env_bool("TOLLGATE_SMOKE"),
        complete=_env_bool("TOLLGATE_COMPLETE"),
        wifi_plane=_env("TOLLGATE_WIFI_PLANE", "tap"),
        lease_minutes=int(_env("TOLLGATE_LEASE_MINUTES", "60")),
    )
    log.info(
        "Config (runner mode): run=%s branch=%s repo=%s backend=%s pr=%s publish=%s mint=%s hwsim=%s vwifi=%s",
        cfg.run_id, cfg.sut_branch, cfg.artifact_repo, cfg.backend,
        cfg.sut_pr or "(none)", cfg.publish, cfg.mint, cfg.hwsim_enabled, cfg.vwifi_enabled,
    )
    return cfg
