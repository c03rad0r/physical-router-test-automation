"""Cloud lab worker — orchestration pipeline."""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from lib.cloud_lab.constants import (
    CDK_MINT_URL,
    DEBIAN_IP,
    OPENWRT_IP,
    RESULTS_ROOT,
    TEST_DIR,
    WORKER_LOG,
)
from lib.cloud_lab.worker.config import WorkerConfig, load_config_from_metadata
from lib.cloud_lab.worker.deploy import deploy_portal_overlay, deploy_tollgate, wait_for_backend
from lib.cloud_lab.worker.logstream import (
    configure_openwrt_syslog,
    start_syslog_capture,
    start_vm_log_streaming,
    stop_vm_log_streaming,
)
from lib.cloud_lab.worker.mints import select_test_mint, start_local_mints, stop_local_mints
from lib.cloud_lab.worker.network import configure_two_router_payment
from lib.cloud_lab.worker.preflight import preflight_check
from lib.cloud_lab.worker.provision import (
    ensure_debian_client_deps,
    ensure_github_cli,
    ensure_outer_deps,
    ensure_suite_checkout,
    write_env_file,
)
from lib.cloud_lab.worker.report import (
    collect_and_render,
    create_minimal_run_json,
    post_pr_comment,
    publish_to_nostr,
    verify_nostr_publish,
)
from lib.cloud_lab.worker.dvm_events import (
    publish_feedback as dvm_feedback,
    publish_job_request as dvm_job_request,
    publish_job_result as dvm_job_result,
)
from lib.cloud_lab.worker.runner import run_tests
from lib.cloud_lab.worker.shell import _redact, _run, log
from lib.cloud_lab.worker.vms import delete_self, start_inner_vms, stop_inner_vms
from lib.cloud_lab.worker.wifi import setup_hwsim_wifi, setup_vwifi_guests, setup_vwifi_host

MAX_WALL_SECONDS = 7200

_pipeline_t0: float | None = None
_pipeline_steps: list[dict[str, Any]] = []
def _step_start(name: str) -> None:
    global _pipeline_t0
    now = time.monotonic()
    if _pipeline_t0 is None:
        _pipeline_t0 = now
    _pipeline_steps.append({
        "step": name,
        "start_offset_ms": round((now - _pipeline_t0) * 1000),
        "_start": now,
    })
def _step_end(name: str) -> None:
    """Record the end of the most recent pipeline step matching *name*."""
    now = time.monotonic()
    for entry in reversed(_pipeline_steps):
        if entry["step"] == name and "duration_ms" not in entry:
            entry["duration_ms"] = round((now - entry["_start"]) * 1000)
            entry.pop("_start", None)
            return
    # If no matching start found, add a bare entry
    if _pipeline_t0 is not None:
        _pipeline_steps.append({
            "step": name,
            "start_offset_ms": round((now - _pipeline_t0) * 1000),
            "duration_ms": 0,
        })
def _log_pipeline_summary() -> None:
    """Log a summary table of pipeline step durations."""
    if not _pipeline_steps:
        return
    label_w = max(len(s["step"]) for s in _pipeline_steps)
    lines = [f"  {'Step':<{label_w}}  {'Duration':>8}"]
    lines.append(f"  {'-' * label_w}  --------")
    for s in _pipeline_steps:
        dur_s = f"{s.get('duration_ms', 0) / 1000:.1f}s"
        lines.append(f"  {s['step']:<{label_w}}  {dur_s:>8}")
    log.info("Pipeline timing summary:\n%s", "\n".join(lines))
def _save_pipeline_timing(results_dir: str) -> None:
    """Save pipeline timing to pipeline_timing.json. Non-invasive — never raises."""
    try:
        out = [
            {k: v for k, v in s.items() if k != "_start"}
            for s in _pipeline_steps
        ]
        path = Path(results_dir) / "pipeline_timing.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=2) + "\n")
    except Exception as exc:
        log.warning("Failed to save pipeline_timing.json (non-fatal): %s", exc)
def _finish_pending_step() -> None:
    """Close any step that hasn't been ended yet (crash safety)."""
    now = time.monotonic()
    for entry in _pipeline_steps:
        if "duration_ms" not in entry and "_start" in entry:
            entry["duration_ms"] = round((now - entry["_start"]) * 1000)
            entry.pop("_start", None)

NDS_EXPECTED_GATEWAYPORT = 2050
NDS_EXPECTED_GATEWAYDOMAINNAME = "TollGate.lan"


def _fix_nodogsplash_gatewayport() -> None:
    from lib.cloud_lab.worker.inner_ssh import inner_ssh
    r = inner_ssh(
        OPENWRT_IP,
        f"uci set nodogsplash.@nodogsplash[0].gatewayport={NDS_EXPECTED_GATEWAYPORT} && "
        f"uci set nodogsplash.@nodogsplash[0].gatewaydomainname={NDS_EXPECTED_GATEWAYDOMAINNAME} && "
        f"uci commit nodogsplash",
        timeout=30,
    )
    if r.returncode != 0:
        log.warning(
            "nodogsplash gatewayport fix failed (rc=%d, stderr=%s) — portal tests may skip",
            r.returncode,
            r.stderr[:200] if r.stderr else "none",
        )
    else:
        rb = inner_ssh(
            OPENWRT_IP,
            "uci get nodogsplash.@nodogsplash[0].gatewayport",
            timeout=15,
        )
        if rb.returncode != 0:
            log.warning(
                "nodogsplash gatewayport read-back failed (rc=%d)",
                rb.returncode,
            )
        else:
            actual = rb.stdout.strip() if rb.stdout else ""
            if actual != str(NDS_EXPECTED_GATEWAYPORT):
                log.error(
                    "nodogsplash gatewayport mismatch: expected %d, got %r",
                    NDS_EXPECTED_GATEWAYPORT,
                    actual,
                )


def run_worker(config: WorkerConfig) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    results_dir = f"{RESULTS_ROOT}/{config.run_id}"
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    test_exit = 1
    wall_t0 = time.monotonic()
    MAX_WALL_SECONDS = 7200  # 2h hard limit — VMs self-delete to prevent runaway costs
    vm_streams: list[tuple[threading.Thread, subprocess.Popen[str], Any]] = []
    local_mints: dict[str, subprocess.Popen[str]] = {}
    syslog_proc: subprocess.Popen[str] | None = None

    try:
        try:
            log.info("=== Pipeline start ===")

            # In runner mode, GH_TOKEN is set by the workflow (cross_repo_token
            # for artifact download). Don't overwrite it — publish-report.sh
            # detects GITHUB_ACTIONS=true and uses gh auth setup-git instead.
            # Export backend type so child code (select_test_mint, Router
            # instances in subprocess scripts) reads the correct value from
            # os.environ instead of falling back to "go".
            os.environ["TOLLGATE_BACKEND"] = config.backend

            if not config.runner_mode:
                os.environ["GH_TOKEN"] = config.gh_token

                _step_start("suite-checkout")
                log.info("[1/10] Suite checkout (ref=%s)", config.suite_ref[:7])
                ensure_suite_checkout(config)
                _step_end("suite-checkout")

                _step_start("outer-deps")
                log.info("[2/10] Outer deps (venv + cashu)")
                ensure_outer_deps()

                if not shutil.which("nak"):
                    log.info("Installing nak v0.16.2...")
                    _run(
                        'curl -sL "https://github.com/fiatjaf/nak/releases/download/v0.16.2/nak-v0.16.2-linux-amd64" '
                        '-o /usr/local/bin/nak && chmod +x /usr/local/bin/nak',
                        timeout=60, check=False,
                    )
                    if shutil.which("nak"):
                        log.info("nak installed")
                    else:
                        log.warning("nak install failed — Nostr publish will be skipped")

                nsec_hex = os.environ.get("BOT_NSEC_HEX", "")
                if nsec_hex:
                    Path(os.path.expanduser("~/nsec")).write_text(nsec_hex)
                    os.environ["NSEC_FILE"] = os.path.expanduser("~/nsec")
                    log.info("NSEC provisioned for Nostr publishing")

                _step_end("outer-deps")

                dvm_job_request(config)
                dvm_feedback("processing", f"Cloud lab pipeline starting (run_id={config.run_id})")

                if config.gh_token:
                    _step_start("gh-cli-auth")
                    log.info("[3/10] GitHub CLI auth (token=***%s)", config.gh_token[-4:])
                    ensure_github_cli(config.gh_token)
                    _step_end("gh-cli-auth")
                else:
                    log.info("[3/10] GitHub CLI auth skipped (no GH_TOKEN — Nostr-only mode)")
            else:
                log.info("[runner mode] Skipping steps 1-3 (GitHub Actions provides checkout/deps/auth)")

            if config.vwifi_enabled:
                _step_start("vwifi-host")
                log.info("[3.5/10] Starting vwifi-server on host for cross-VM WiFi relay")
                try:
                    setup_vwifi_host()
                except Exception as vwifi_exc:
                    log.warning("[vwifi] Host setup failed (non-fatal, WiFi tests may skip): %s", vwifi_exc)
                    config.vwifi_enabled = False
                _step_end("vwifi-host")
            else:
                log.info("[vwifi] Skipped (not enabled — use --vwifi to opt in)")

            _step_start("inner-vms")
            log.info("[4/10] Inner VMs (OpenWrt + Debian)")
            start_inner_vms(config)
            _step_end("inner-vms")

            _step_start("hwsim-wifi")
            log.info("[4.5/10] Setup hwsim virtual WiFi on Alpha (enabled=%s vwifi=%s)", config.hwsim_enabled, config.vwifi_enabled)
            if config.hwsim_enabled or config.vwifi_enabled:
                try:
                    setup_hwsim_wifi(OPENWRT_IP, vwifi_mode=config.vwifi_enabled)
                except Exception as hwsim_exc:
                    log.warning("[hwsim] Setup failed (non-fatal, WiFi tests may skip): %s", hwsim_exc)
            else:
                log.info("[hwsim] Skipped (not enabled — use --hwsim or --vwifi to opt in)")
            _step_end("hwsim-wifi")

            if config.vwifi_enabled:
                _step_start("vwifi-guests")
                log.info("[4.55/10] Setting up vwifi guests for cross-VM WiFi relay")
                try:
                    setup_vwifi_guests(OPENWRT_IP, DEBIAN_IP, config, results_dir)
                except Exception as vwifi_exc:
                    log.warning("[vwifi] Guest setup failed (non-fatal, WiFi tests may skip): %s", vwifi_exc)
                _step_end("vwifi-guests")

            _step_start("syslog-capture")
            log.info("[4.6/10] Start syslog capture + configure OpenWrt forwarding")
            try:
                syslog_proc = start_syslog_capture(results_dir)
            except Exception as exc:
                log.warning("Syslog capture start failed (non-fatal): %s", exc)
            try:
                configure_openwrt_syslog(OPENWRT_IP)
                if config.secondary_router_host:
                    configure_openwrt_syslog(config.secondary_router_host)
            except Exception as exc:
                log.warning("OpenWrt syslog config failed (non-fatal): %s", exc)
            _step_end("syslog-capture")

            _step_start("local-mints")
            log.info("[5/10] Start local mints (CDK + Nutshell)")
            local_mints = start_local_mints(config)
            _step_end("local-mints")

            _step_start("env-debian-deps")
            log.info("[6/10] Write .env + Debian client deps")
            write_env_file(config)
            ensure_debian_client_deps()
            _step_end("env-debian-deps")

            _step_start("deploy-tollgate")
            log.info("[7/10] Deploy TollGate (branch=%s, artifact_run=%s)", config.sut_branch, config.artifact_run_id)
            deploy_tollgate(config)
            _step_end("deploy-tollgate")

            _step_start("backend-health")
            log.info("[8/10] Wait for backend health")
            wait_for_backend()
            _step_end("backend-health")

            _step_start("fix-nodogsplash-port")
            _fix_nodogsplash_gatewayport()
            _step_end("fix-nodogsplash-port")

            if config.portal != "builtin":
                _step_start("portal-overlay")
                log.info("[8.1/10] Deploy portal overlay (%s)", config.portal)
                try:
                    deploy_portal_overlay(config)
                except Exception as portal_exc:
                    log.error("Portal overlay failed (non-fatal, tests may skip): %s", _redact(str(portal_exc))[:500])
                _step_end("portal-overlay")

            _step_start("select-mint")
            log.info("[8.5/11] Select test mint (forced=%s)", config.mint)
            chosen_mint = select_test_mint(forced_mint=config.mint)
            env_path = Path(f"{TEST_DIR}/.env")
            if env_path.exists():
                env_text = env_path.read_text()
                env_text = env_text.replace(f"TOLLGATE_TEST_MINT_URL={CDK_MINT_URL}", f"TOLLGATE_TEST_MINT_URL={chosen_mint}")
                env_path.write_text(env_text)
                log.info("Updated .env TOLLGATE_TEST_MINT_URL=%s", chosen_mint)
            _step_end("select-mint")

            if config.two_router:
                _step_start("two-router-payment")
                log.info("[8.6/11] Configure two-router payment (Beta merchant + Alpha reseller)")
                configure_two_router_payment(config, chosen_mint)
                _step_end("two-router-payment")

            _step_start("post-mint-health")
            log.info("[8.7/11] Wait for backend after mint config change")
            wait_for_backend()
            _step_end("post-mint-health")

            _step_start("preflight")
            log.info("[9/11] Pre-flight checks")
            preflight = preflight_check(config, chosen_mint, results_dir)
            if not preflight.get("ok"):
                log.error("Pre-flight checks FAILED — aborting test run to save time")
                raise RuntimeError(f"Pre-flight checks failed: {[k for k, v in preflight.items() if v is False]}")
            _step_end("preflight")

            _step_start("run-tests")
            log.info("[10/11] Run tests (results_dir=%s)", results_dir)
            vm_streams = start_vm_log_streaming(config, results_dir)
            try:
                test_exit = run_tests(config, results_dir)
            finally:
                stop_vm_log_streaming(vm_streams)
            log.info("Tests finished with exit=%d (%.1fs elapsed)", test_exit, time.monotonic() - wall_t0)
            _step_end("run-tests")

        except Exception as exc:
            elapsed = time.monotonic() - wall_t0
            import traceback
            log.error("Pipeline failed at step: %s (%.1fs elapsed)\n%s", _redact(str(exc))[:200], elapsed, traceback.format_exc())
            if elapsed >= MAX_WALL_SECONDS:
                log.warning("2h max lifetime exceeded — will force-delete after collect/publish")
            test_exit = 1

        _finish_pending_step()

        # ── Collect, render, publish (always attempted) ──────────────
        finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        _run(
            f"mkdir -p {results_dir}/raw && "
            f"cp {WORKER_LOG} {results_dir}/raw/worker.log 2>/dev/null || true",
            timeout=10, check=False,
        )

        if os.environ.get("TOLLGATE_VIRTUAL_LAB"):
            _run(
                f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                f"-o LogLevel=ERROR -o ConnectTimeout=5 "
                f"root@{OPENWRT_IP} 'logread' > {results_dir}/raw/openwrt-syslog.log 2>/dev/null || true",
                timeout=30, check=False,
            )
            _run(
                f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                f"-o LogLevel=ERROR -o ConnectTimeout=5 "
                f"root@{OPENWRT_IP} 'cat /tmp/tollgate-debug.log 2>/dev/null || true' "
                f"> {results_dir}/raw/tollgate-service.log 2>/dev/null || true",
                timeout=15, check=False,
            )

        for name, path in [
            ("cdk-v2", "/tmp/cdk-mintd.log"),
            ("nutshell-v2", "/tmp/nutshell-v2-mint.log"),
            ("nutshell-v1", "/tmp/nutshell-v1-mint.log"),
        ]:
            _run(f"cp {shlex.quote(str(path))} {shlex.quote(str(results_dir))}/raw/{shlex.quote(str(name))}.log 2>/dev/null || true", timeout=5, check=False)

        _step_start("collect-render")
        try:
            log.info("[11/11] Collect + render results")
            collect_and_render(config, results_dir, started_at, finished_at)
        except Exception as collect_exc:
            log.error("collect_and_render failed (non-fatal): %s", _redact(str(collect_exc))[:500])
        _step_end("collect-render")

        run_json = Path(results_dir) / "run.json"
        if not run_json.exists():
            create_minimal_run_json(config, results_dir, started_at, finished_at, test_exit)

        counts: dict[str, Any] = {}
        if run_json.exists():
            counts = json.loads(run_json.read_text()).get("counts", {})

        report_url = ""
        total_run = sum(counts.get(k, 0) for k in ("passed", "failed", "skipped", "error"))
        if config.publish and run_json.exists():
            try:
                log.info("Publishing results to Blossom + Nostr (total_tests=%d)...", total_run)
                manifest = publish_to_nostr(config, results_dir, counts)
                verify_nostr_publish(config)
                result_urls = [f["url"] for f in manifest.get("files", [])] if manifest else []
                dvm_job_result(config, counts, result_urls)
                report_url = "https://tests.tollgate.me/"
                if config.gh_token:
                    post_pr_comment(config, report_url, counts)
            except Exception as pub_exc:
                log.error("Publish failed (non-fatal): %s", _redact(str(pub_exc))[:500])

        # ── Optional post-test benchmarks (never blocks test reports) ─────
        if config.benchmark:
            _step_start("benchmarks")
            try:
                log.info("[bonus] Running post-test benchmarks...")
                from lib.cloud_lab.worker.benchmark import run_benchmarks, publish_benchmark_to_nostr
                bench_results = run_benchmarks(config)
                if config.publish:
                    publish_benchmark_to_nostr(config, bench_results)
            except Exception as bench_exc:
                log.error("Benchmarks failed (non-fatal): %s", _redact(str(bench_exc))[:500])
            _step_end("benchmarks")

        dvm_feedback("success" if test_exit == 0 else "error", f"exit={test_exit}")

        _save_pipeline_timing(results_dir)
        _log_pipeline_summary()

        log.info(
            "=== Pipeline complete: passed=%s failed=%s skipped=%s exit=%d (%.1fs) ===",
            counts.get("passed", "?"),
            counts.get("failed", "?"),
            counts.get("skipped", "?"),
            test_exit,
            time.monotonic() - wall_t0,
        )
        return test_exit
    finally:
        elapsed = time.monotonic() - wall_t0
        force_delete = elapsed >= MAX_WALL_SECONDS
        if force_delete:
            log.warning("2h max lifetime reached (%.1fs) — forcing VM deletion regardless of lease setting", elapsed)
        if syslog_proc and syslog_proc.poll() is None:
            syslog_proc.kill()
        stop_local_mints(local_mints)
        if force_delete:
            log.info("Force-deleting VM (2h lifetime exceeded)")
            stop_inner_vms()
            delete_self(config)
        elif config.keep_vm_on_failure:
            log.warning("Keeping VM + inner VMs alive for log inspection (keep_vm_on_failure=true). "
                        "Lease kill switch will delete at tollgate-delete-at timestamp (3h hard backstop).")
        else:
            stop_inner_vms()
            log.info("Self-deleting VM %s", config.vm_name)
            delete_self(config)
