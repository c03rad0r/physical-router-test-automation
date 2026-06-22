#!/usr/bin/env python3
"""Manage the TollGate GCP nested-virtualization test lab."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, cast

# Ensure deploy.py log messages are visible during submit.
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.cloud_lab.constants import (
    DEFAULT_DISK_SIZE_GB,
    DEFAULT_MACHINE_TYPE,
    DEFAULT_ZONE,
    SSH_KEY,
    SUITE_REPO,
    VM_NAME,
)
from lib.cloud_lab.gcp import (
    cleanup_all,
    cleanup_stale,
    delete_by_run_id,
    extend_lease,
    get_project,
    status_run,
    submit_run,
    vm_down,
    vm_external_ip,
    vm_status,
    vm_up,
)
from lib.cloud_lab.resolve import resolve_target


def _warn_running_vms() -> None:
    project = get_project()
    r = subprocess.run(
        [
            "gcloud", "compute", "instances", "list",
            f"--project={project}",
            "--filter=labels.tollgate_run=true",
            "--format=json",
        ],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return
    import json
    instances = json.loads(r.stdout)
    if not instances:
        return

    now = time.time()
    stale_names = []
    running_names = []
    for inst in instances:
        name = inst.get("name", "?")
        created_str = inst.get("creationTimestamp", "")
        status = inst.get("status", "UNKNOWN")
        try:
            from datetime import datetime as _dt
            created = _dt.fromisoformat(created_str.replace("Z", "+00:00")).timestamp()
            age_hours = (now - created) / 3600
        except (ValueError, TypeError):
            age_hours = 0
        if age_hours >= 1:
            stale_names.append(name)
        else:
            running_names.append(f"{name} ({status}, created {created_str})")

    if stale_names:
        print(f"\n⚠  Auto-deleting {len(stale_names)} stale VM(s) (>1h old): {', '.join(stale_names)}", file=sys.stderr)
        cleanup_stale(max_age_hours=1)

    if running_names:
        print(f"\n⚠  {len(running_names)} recent TollGate VM(s) still running:", file=sys.stderr)
        for name in running_names:
            print(f"   - {name}", file=sys.stderr)
        print("", file=sys.stderr)


def cmd_up(args: argparse.Namespace) -> int:
    return vm_up(
        cast(str, getattr(args, "vm_name", VM_NAME) or VM_NAME),
        zone=cast(str, args.zone),
        machine_type=cast(str, args.machine_type),
        disk_size_gb=cast(int, args.disk_size),
    )


def cmd_down(args: argparse.Namespace) -> int:
    vm_name = cast(str, getattr(args, "vm_name", VM_NAME) or VM_NAME)
    project = get_project()
    if vm_status(project, cast(str, args.zone), vm_name) is None:
        print(f"VM {vm_name} does not exist")
        return 0
    print(f"Deleting VM {vm_name}...")
    return vm_down(vm_name, zone=cast(str, args.zone))


def cmd_status(args: argparse.Namespace) -> int:
    project = get_project()
    zone = cast(str, args.zone)
    vm_name = cast(str, getattr(args, "vm_name", VM_NAME) or VM_NAME)
    status = vm_status(project, zone, vm_name)
    if not status:
        print(f"VM {vm_name} does not exist")
        return 1
    ip = vm_external_ip(project, zone, vm_name)
    print(f"VM: {vm_name}")
    print(f"Status: {status}")
    print(f"IP: {ip or 'N/A'}")
    print(f"Zone: {zone}")
    print(f"Project: {project}")
    return 0


def cmd_ssh(args: argparse.Namespace) -> int:
    project = get_project()
    zone = cast(str, args.zone)
    vm_name = cast(str, getattr(args, "vm_name", VM_NAME) or VM_NAME)
    ip = vm_external_ip(project, zone, vm_name)
    if not ip:
        print(f"ERROR: VM {vm_name} has no external IP", file=sys.stderr)
        return 1
    user = getattr(args, "user", "root") or "root"
    os.execvp(
        "ssh",
        [
            "ssh", "-i", SSH_KEY,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{user}@{ip}",
        ],
    )
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    _warn_running_vms()
    target = resolve_target(
        pr=cast(str | None, args.pr),
        branch=cast(str | None, args.branch),
        commit=cast(str | None, args.commit),
        backend=cast(str, args.backend),
        repo_override=cast(str | None, args.repo),
    )
    info = submit_run(
        target,
        zone=cast(str, args.zone),
        publish=cast(bool, args.publish),
        artifact_timeout_s=cast(int, args.artifact_timeout),
        machine_type=cast(str, args.machine_type),
        disk_size_gb=cast(int, args.disk_size),
        reseller_scenarios=cast(bool, args.reseller_scenarios),
        two_router=cast(bool, args.two_router),
        secondary_router_host=cast(str, args.secondary_router_host or ""),
        secondary_router_port=cast(str, args.secondary_router_port or ""),
        keep_vm_on_failure=not getattr(args, "self_delete", False),
        mint=cast(str, args.mint),
        portal=cast(str, args.portal),
        quick=cast(bool, args.quick),
        smoke=cast(bool, getattr(args, "smoke", False)),
        complete=cast(bool, getattr(args, "complete", False)),
        hwsim=cast(bool, getattr(args, "hwsim", False)),
        vwifi=cast(bool, getattr(args, "vwifi", False)),
        wifi_plane=cast(str, getattr(args, "wifi_plane", "tap")),
        lease_minutes=cast(int, getattr(args, "lease", 60)),
        benchmark=cast(bool, getattr(args, "benchmark", False)),
    )
    pr_line = f"  PR:           {target.pr} ({target.repo}@{target.branch})\n" if target.pr else f"  Branch:       {target.repo}@{target.branch}\n"
    mint_line = f"  Mint:         {cast(str, args.mint)}\n" if cast(str, args.mint) != "auto" else ""
    portal_line = f"  Portal:       {cast(str, args.portal)}\n" if cast(str, args.portal) != "builtin" else ""
    quick_line = "  Mode:         QUICK (visual happy path only)\n" if cast(bool, args.quick) else ""
    smoke_line = "  Mode:         SMOKE (visual + smoke API + hwsim)\n" if cast(bool, getattr(args, "smoke", False)) and not cast(bool, args.quick) else ""
    complete_line = "  Mode:         COMPLETE (includes slow/exhaustive tests)\n" if cast(bool, getattr(args, "complete", False)) and not cast(bool, args.quick) and not cast(bool, getattr(args, "smoke", False)) else ""
    hwsim_line = "  hwsim:        enabled\n" if cast(bool, getattr(args, "hwsim", False)) else ""
    vwifi_line = "  vwifi:        enabled (cross-VM WiFi relay)\n" if cast(bool, getattr(args, "vwifi", False)) else ""
    wifi_plane_line = f"  WiFi plane:   {cast(str, getattr(args, 'wifi_plane', 'tap'))}\n" if cast(str, getattr(args, "wifi_plane", "tap")) != "tap" else ""
    lease_line = f"  Lease:        {cast(int, getattr(args, 'lease', 60))} min\n" if cast(int, getattr(args, 'lease', 60)) != 60 else ""
    print(f"""
Submitted run {info['run_id']}
{pr_line}  SUT commit:   {target.sut_commit or '(branch head)'}
{quick_line}{smoke_line}{complete_line}{mint_line}{portal_line}{hwsim_line}{vwifi_line}{wifi_plane_line}{lease_line}  VM:           {info['vm_name']} ({info['zone']})
  Artifact run: {info['artifact_run_id']}
  Suite ref:    {info['suite_ref']} (must exist on github.com/{SUITE_REPO})
  Logs:         {info['log_hint']}
""")
    if cast(bool, args.wait):
        return _wait_for_run(info["run_id"], cast(str, args.zone))
    return 0


def _wait_for_run(run_id: str, zone: str) -> int:
    print("Waiting for VM to finish (poll every 60s)...")
    while True:
        project = get_project()
        r = subprocess.run(
            [
                "gcloud", "compute", "instances", "list",
                f"--project={project}",
                f"--filter=metadata.tollgate-run-id={run_id}",
                "--format=value(name)",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if r.returncode != 0 or not r.stdout.strip():
            print(f"Run {run_id} finished. Check https://tests.tollgate.me/")
            return 0
        print(f"  VM still running: {r.stdout.strip()}")
        time.sleep(60)


def cmd_submit_all_mints(args: argparse.Namespace) -> int:
    """Submit 3 parallel runs, one per mint type."""
    _warn_running_vms()
    mint_types = ["cdk-v2", "nutshell-v2", "nutshell-v1"]
    target = resolve_target(
        pr=cast(str | None, args.pr),
        branch=cast(str | None, args.branch),
        commit=cast(str | None, args.commit),
        backend=cast(str, args.backend),
        repo_override=cast(str | None, args.repo),
    )
    infos = []
    for mint in mint_types:
        info = submit_run(
            target,
            zone=cast(str, args.zone),
            publish=cast(bool, args.publish),
            artifact_timeout_s=cast(int, args.artifact_timeout),
            machine_type=cast(str, args.machine_type),
            disk_size_gb=cast(int, args.disk_size),
            reseller_scenarios=cast(bool, args.reseller_scenarios),
            two_router=cast(bool, args.two_router),
            secondary_router_host=cast(str, args.secondary_router_host or ""),
            secondary_router_port=cast(str, args.secondary_router_port or ""),
            keep_vm_on_failure=not getattr(args, "self_delete", False),
            mint=mint,
            hwsim=cast(bool, getattr(args, "hwsim", False)),
            vwifi=cast(bool, getattr(args, "vwifi", False)),
            wifi_plane=cast(str, getattr(args, "wifi_plane", "tap")),
        )
        infos.append((mint, info))

    pr_line = f"  PR:           {target.pr} ({target.repo}@{target.branch})\n" if target.pr else f"  Branch:       {target.repo}@{target.branch}\n"
    print(f"""
Submitted 3 parallel runs for multi-mint validation
{pr_line}  SUT commit:   {target.sut_commit or '(branch head)'}
""")
    for mint, info in infos:
        print(f"  [{mint}] run={info['run_id']} vm={info['vm_name']}")
    print()
    return 0


def cmd_status_run(args: argparse.Namespace) -> int:
    return status_run(cast(str, args.run_id), zone=cast(str, args.zone))


def cmd_cleanup_stale(args: argparse.Namespace) -> int:
    return cleanup_stale(zone=cast(str, args.zone), max_age_hours=cast(int, args.max_age_hours))


def cmd_cleanup_all(args: argparse.Namespace) -> int:
    return cleanup_all(zone=cast(str, args.zone))


def cmd_delete(args: argparse.Namespace) -> int:
    return delete_by_run_id(cast(str, args.run_id), zone=cast(str, args.zone))


def cmd_extend(args: argparse.Namespace) -> int:
    return extend_lease(cast(str, args.run_id), minutes=cast(int, args.minutes), zone=cast(str, args.zone))


def cmd_install_reaper(args: argparse.Namespace) -> int:
    marker = "# tollgate-cloud-reaper"
    script = str(Path(__file__).resolve())
    max_age = args.max_age_hours
    cron_line = f"*/30 * * * * {script} cleanup-stale --max-age-hours {max_age} {marker}"

    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False).stdout or ""
    existing = [l for l in current.splitlines() if marker not in l]

    if args.uninstall:
        if not any(marker in l for l in current.splitlines()):
            print("No reaper cron job found.")
            return 0
        new_cron = "\n".join(existing).strip() + "\n"
        subprocess.run(["crontab", "-"], input=new_cron, text=True, check=True)
        print("Reaper cron job removed.")
        return 0

    if any(marker in l for l in current.splitlines()):
        print(f"Reaper already installed. Updating to {max_age}h max age.")
        existing_lines = existing
    else:
        existing_lines = current.splitlines()

    new_cron = "\n".join(existing_lines + [cron_line]).strip() + "\n"
    subprocess.run(["crontab", "-"], input=new_cron, text=True, check=True)
    print(f"Reaper installed: VMs older than {max_age}h will be auto-deleted every hour.")
    print(f"  Cron: {cron_line}")
    print(f"  Uninstall: {script} install-reaper --uninstall")
    return 0


def cmd_run_tests(args: argparse.Namespace) -> int:
    """Synchronous wrapper: submit + wait (legacy compatibility)."""
    args.wait = True
    args.publish = bool(getattr(args, "publish", False)) or not getattr(args, "no_publish", False)
    return cmd_submit(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--zone", default=DEFAULT_ZONE)
        p.add_argument("--vm-name", default=VM_NAME)
        p.add_argument("--cloud", default="gcp", choices=["gcp", "shc"],
                       help="Cloud provider: gcp (Google Cloud) or shc (Sovereign Hybrid Compute)")

    def target_flags(p: argparse.ArgumentParser) -> None:
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("--pr", default=None)
        g.add_argument("--branch", default=None)
        p.add_argument("--commit", default=None)
        p.add_argument("--backend", default="go", choices=["go", "rust"])
        p.add_argument("--repo", default=None,
                        help="Override the artifact repo (e.g. Amperstrand/tollgate-module-basic-go for fork branches)")

    up = sub.add_parser("up", help="Create/start GCP VM from snapshot")
    common(up)
    up.add_argument("--machine-type", default=DEFAULT_MACHINE_TYPE)
    up.add_argument("--disk-size", type=int, default=DEFAULT_DISK_SIZE_GB)
    up.set_defaults(func=cmd_up)

    down = sub.add_parser("down", help="Delete the GCP VM")
    common(down)
    down.set_defaults(func=cmd_down)

    status = sub.add_parser("status", help="Show VM status")
    common(status)
    status.set_defaults(func=cmd_status)

    ssh = sub.add_parser("ssh", help="SSH into the GCP VM")
    common(ssh)
    ssh.add_argument("--user", default="root")
    ssh.set_defaults(func=cmd_ssh)

    submit = sub.add_parser("submit", help="Fire-and-forget: wait for CI artifact, spawn autonomous test VM")
    submit.add_argument("--zone", default=DEFAULT_ZONE)
    submit.add_argument("--machine-type", default=DEFAULT_MACHINE_TYPE)
    submit.add_argument("--disk-size", type=int, default=DEFAULT_DISK_SIZE_GB)
    submit.add_argument("--smoke", action="store_true",
        help="Smoke mode: visual happy path + smoke API tests + hwsim (if --hwsim) (~8min)")
    submit.add_argument("--quick", action="store_true", help="Quick mode: only run visual happy path (~5min total)")
    submit.add_argument("--complete", action="store_true",
        help="Complete mode: include slow/exhaustive tests excluded from default runs (~45min)")
    submit.add_argument("--hwsim", action="store_true",
        help="Enable mac80211_hwsim virtual WiFi on the OpenWrt VM (experimental)")
    submit.add_argument("--vwifi", action="store_true",
        help="Enable vwifi cross-VM WiFi frame relay (experimental, enables real STA scan/association)")
    submit.add_argument("--wifi-plane", default="tap", choices=["tap", "hwsim-netns"],
        help="Radio-plane mode. Default tap keeps existing VM/TAP cloud lab; hwsim-netns runs optional shared-kernel Wi-Fi POC.")
    submit.add_argument("--publish", action="store_true", help="Publish report to gh-pages when done")
    submit.add_argument("--wait", action="store_true", help="Block until VM self-deletes")
    submit.add_argument("--reseller-scenarios", action="store_true", help="Run virtualizable reseller-mode scenario tests")
    submit.add_argument("--two-router", action="store_true", help="Boot second OpenWrt VM for two-router degraded-mode tests")
    submit.add_argument("--secondary-router-host", default="", help="Seller/secondary router IP or host for reseller scenarios")
    submit.add_argument("--secondary-router-port", default="", help="Optional SSH port for the seller/secondary router")
    submit.add_argument("--self-delete", action="store_true", help="Self-delete VM after tests complete (default: keep alive for debugging, 1h kill switch)")
    submit.add_argument("--lease", type=int, default=60, help="Minutes before idle VM auto-deletes after pipeline completes (default: 60)")
    submit.add_argument("--artifact-timeout", type=int, default=1800, help="Seconds to wait for CI artifact")
    submit.add_argument("--mint", default="auto", choices=["auto", "cdk-v2", "nutshell-v2", "nutshell-v1"],
        help="Force a specific mint type instead of auto-detection. Use 'submit-all-mints' for parallel runs.")
    submit.add_argument("--portal", default="builtin", choices=["builtin", "net4sats"],
        help="Captive portal to deploy (default: builtin). 'net4sats' deploys the configurationwizzard SPA.")
    submit.add_argument("--benchmark", action="store_true",
        help="Run optional post-test benchmarks (HTTP latency, throughput, system metrics). "
             "Publishes results separately via Nostr — never blocks test reports.")
    target_flags(submit)
    submit.set_defaults(func=cmd_submit)

    all_mints = sub.add_parser("submit-all-mints", help="Submit 3 parallel runs (one per mint type: cdk-v2, nutshell-v2, nutshell-v1)")
    all_mints.add_argument("--zone", default=DEFAULT_ZONE)
    all_mints.add_argument("--machine-type", default=DEFAULT_MACHINE_TYPE)
    all_mints.add_argument("--disk-size", type=int, default=DEFAULT_DISK_SIZE_GB)
    all_mints.add_argument("--publish", action="store_true", help="Publish reports to gh-pages when done")
    all_mints.add_argument("--reseller-scenarios", action="store_true", help="Run virtualizable reseller-mode scenario tests")
    all_mints.add_argument("--two-router", action="store_true", help="Boot second OpenWrt VM for two-router degraded-mode tests")
    all_mints.add_argument("--secondary-router-host", default="", help="Seller/secondary router IP or host for reseller scenarios")
    all_mints.add_argument("--secondary-router-port", default="", help="Optional SSH port for the seller/secondary router")
    all_mints.add_argument("--self-delete", action="store_true", help="Self-delete VM after tests complete (default: keep alive for debugging, 1h kill switch)")
    all_mints.add_argument("--artifact-timeout", type=int, default=1800, help="Seconds to wait for CI artifact")
    all_mints.add_argument("--hwsim", action="store_true",
        help="Enable mac80211_hwsim virtual WiFi on the OpenWrt VM (experimental)")
    all_mints.add_argument("--vwifi", action="store_true",
        help="Enable vwifi cross-VM WiFi frame relay (experimental)")
    all_mints.add_argument("--wifi-plane", default="tap", choices=["tap", "hwsim-netns"],
        help="Radio-plane mode. Default tap keeps existing VM/TAP cloud lab; hwsim-netns runs optional shared-kernel Wi-Fi POC.")
    target_flags(all_mints)
    all_mints.set_defaults(func=cmd_submit_all_mints)

    sr = sub.add_parser("status-run", help="Show status of a submitted run")
    sr.add_argument("--run-id", required=True)
    sr.add_argument("--zone", default=DEFAULT_ZONE)
    sr.set_defaults(func=cmd_status_run)

    delete = sub.add_parser("delete", help="Delete a running VM by run-id")
    delete.add_argument("--run-id", required=True)
    delete.add_argument("--zone", default=DEFAULT_ZONE)
    delete.set_defaults(func=cmd_delete)

    extend = sub.add_parser("extend", help="Extend the lease on a running VM")
    extend.add_argument("--run-id", required=True)
    extend.add_argument("--minutes", type=int, default=60, help="Minutes to extend from now (default: 60)")
    extend.add_argument("--zone", default=DEFAULT_ZONE)
    extend.set_defaults(func=cmd_extend)

    clean = sub.add_parser("cleanup-stale", help="Delete tollgate run VMs older than max age")
    clean.add_argument("--zone", default=DEFAULT_ZONE)
    clean.add_argument("--max-age-hours", type=int, default=1)
    clean.set_defaults(func=cmd_cleanup_stale)

    nuke = sub.add_parser("cleanup-all", help="Delete ALL tollgate VMs regardless of age")
    nuke.add_argument("--zone", default=DEFAULT_ZONE)
    nuke.set_defaults(func=cmd_cleanup_all)

    reaper = sub.add_parser("install-reaper", help="Install cron job to auto-delete VMs older than 1 hour")
    reaper.add_argument("--max-age-hours", type=int, default=1)
    reaper.add_argument("--uninstall", action="store_true", help="Remove the reaper cron job")
    reaper.set_defaults(func=cmd_install_reaper)

    run = sub.add_parser("run-tests", help="Submit cloud run and wait (alias for submit --wait --publish)")
    run.add_argument("--zone", default=DEFAULT_ZONE)
    run.add_argument("--machine-type", default=DEFAULT_MACHINE_TYPE)
    run.add_argument("--disk-size", type=int, default=DEFAULT_DISK_SIZE_GB)
    run.add_argument("--publish", action="store_true", help="Publish to gh-pages (default)")
    run.add_argument("--no-publish", action="store_true", help="Skip gh-pages publish")
    run.add_argument("--reseller-scenarios", action="store_true", help="Run virtualizable reseller-mode scenario tests")
    run.add_argument("--two-router", action="store_true", help="Boot second OpenWrt VM for two-router degraded-mode tests")
    run.add_argument("--secondary-router-host", default="", help="Seller/secondary router IP or host for reseller scenarios")
    run.add_argument("--secondary-router-port", default="", help="Optional SSH port for the seller/secondary router")
    run.add_argument("--self-delete", action="store_true", help="Self-delete VM after tests complete (default: keep alive for debugging, 1h kill switch)")
    run.add_argument("--artifact-timeout", type=int, default=1800)
    run.add_argument("--mint", default="auto", choices=["auto", "cdk-v2", "nutshell-v2", "nutshell-v1"],
                     help="Force a specific mint type instead of auto-detection")
    run.add_argument("--hwsim", action="store_true",
        help="Enable mac80211_hwsim virtual WiFi on the OpenWrt VM (experimental)")
    run.add_argument("--vwifi", action="store_true",
        help="Enable vwifi cross-VM WiFi frame relay (experimental)")
    run.add_argument("--wifi-plane", default="tap", choices=["tap", "hwsim-netns"],
        help="Radio-plane mode. Default tap keeps existing VM/TAP cloud lab; hwsim-netns runs optional shared-kernel Wi-Fi POC.")
    target_flags(run)
    run.set_defaults(func=cmd_run_tests)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    cloud = getattr(args, "cloud", "gcp")
    if cloud == "shc":
        os.environ["CLOUD_BIN"] = "shc-compute"
        os.environ.setdefault("SHC_API_KEY", "")
    func = cast(Callable[[argparse.Namespace], int], args.func)
    return func(args)


if __name__ == "__main__":
    raise SystemExit(main())
