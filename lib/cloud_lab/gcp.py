"""GCP VM lifecycle for fire-and-forget cloud lab runs."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
import time
import base64
import io
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from lib.cloud_lab.artifact import ensure_target_artifact
from lib.cloud_lab.constants import (
    DEFAULT_DISK_SIZE_GB,
    DEFAULT_MACHINE_TYPE,
    DEFAULT_ZONE,
    FIREWALL_RULE_SSH,
    SNAPSHOT_NAME,
    SUITE_REPO,
)
from lib.cloud_lab.resolve import RunTarget

_CLOUD_BIN = os.environ.get("CLOUD_BIN", "gcloud")


def _run_gcloud(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    markers = (
        "NameResolutionError", "Failed to resolve", "ConnectionError",
        "Max retries exceeded", "Network is unreachable", "timed out",
    )
    last = subprocess.CompletedProcess(args=[_CLOUD_BIN], returncode=1, stdout="", stderr="")
    for attempt in range(1, 4):
        last = subprocess.run(
            [_CLOUD_BIN, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        combined = f"{last.stderr}\n{last.stdout}"
        if last.returncode == 0 or not any(m in combined for m in markers):
            return last
        if attempt < 3:
            print(f"WARNING: transient gcloud failure, retrying ({attempt}/3): {last.stderr[:200]}", file=sys.stderr)
            time.sleep(5 * attempt)
    return last


def vm_status(project: str, zone: str, vm_name: str) -> str | None:
    r = _run_gcloud([
        "compute", "instances", "describe", vm_name,
        f"--project={project}", f"--zone={zone}", "--format=json",
    ], timeout=30)
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
        status = data.get("status")
        return status if isinstance(status, str) else None
    except json.JSONDecodeError:
        return None


def vm_external_ip(project: str, zone: str, vm_name: str) -> str | None:
    r = _run_gcloud([
        "compute", "instances", "describe", vm_name,
        f"--project={project}", f"--zone={zone}", "--format=json",
    ], timeout=30)
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
        for iface in data.get("networkInterfaces", []):
            for ac in iface.get("accessConfigs", []):
                ip = ac.get("natIP")
                if ip:
                    return ip
    except (json.JSONDecodeError, AttributeError):
        return None
    return None


# Machine type families that do NOT support GCP nested virtualization.
# E2 (shared-core), N2D (AMD), T2A (Arm), A2 (GPU) lack VMX support.
# See: https://cloud.google.com/compute/docs/instances/nested-virtualization/overview
_NO_NESTED_VIRT_PREFIXES = ("e2-", "n2d-", "t2a-", "a2-")

# Zones to try when the requested zone is exhausted (ZONE_RESOURCE_POOL_EXHAUSTED).
# Ordered by price (cheapest first). All support Intel CPUs + nested virt.
# Tier 1 ($0.0971/hr): us-central1, us-east1, us-east5, us-west1
# Tier 2 ($0.1068-$0.1069/hr): northamerica-northeast1/2, europe-west1/2/3/4
# Tier 3 ($0.1170-$0.1183/hr): us-east4, us-west2/3/4, europe-west6/8
# See: https://gcloud-compute.com/n2-standard-2.html
_ZONE_FALLBACKS = [
    # Tier 1 — $0.0971/hr (cheapest)
    "us-central1-a", "us-central1-b", "us-central1-c", "us-central1-f",
    "us-east1-b", "us-east1-c", "us-east1-d",
    "us-east5-a", "us-east5-b", "us-east5-c",
    "us-west1-a", "us-west1-b", "us-west1-c",
    # Tier 2 — $0.1068-$0.1069/hr (~10% more, still cheap)
    "northamerica-northeast1-a", "northamerica-northeast1-b", "northamerica-northeast1-c",
    "northamerica-northeast2-a", "northamerica-northeast2-b", "northamerica-northeast2-c",
    "europe-west1-b", "europe-west1-c", "europe-west1-d",
    "europe-west2-a", "europe-west2-b", "europe-west2-c",
    "europe-west3-a", "europe-west3-b", "europe-west3-c",
    "europe-west4-a", "europe-west4-b", "europe-west4-c",
    # Tier 3 — $0.1170-$0.1183/hr (~20% more, acceptable for peak hours)
    "us-east4-a", "us-east4-b", "us-east4-c",
    "us-west2-a", "us-west2-b", "us-west2-c",
    "us-west3-a", "us-west3-b", "us-west3-c",
    "us-west4-a", "us-west4-b", "us-west4-c",
    "europe-west6-a", "europe-west6-b", "europe-west6-c",
    "europe-west8-a", "europe-west8-b", "europe-west8-c",
]

# Machine types to try in order when the requested type is unavailable.
# All support nested virtualization and have >= 2 vCPU (Debian QEMU needs 2).
# Ordered by price: n2 ($0.0971/hr) → n1 ($0.0950/hr, older Intel).
_MACHINE_TYPE_FALLBACKS = ["n2-standard-2", "n1-standard-2"]

# Minimum CPU platform per machine family. N2 has Cascade Lake/Ice Lake;
# N1 maxes out at Skylake. Both support VMX (nested virt).
_MIN_CPU_PLATFORM = {
    "n2": "Intel Cascade Lake",
    "n1": "Intel Skylake",
}


def _validate_machine_type(machine_type: str) -> None:
    """Reject machine types that cannot run nested KVM."""
    if any(machine_type.startswith(p) for p in _NO_NESTED_VIRT_PREFIXES):
        raise ValueError(
            f"Machine type '{machine_type}' does not support nested virtualization. "
            f"Use an Intel-based type (n1-standard-*, n2-standard-*). "
            f"Nested virt requires Intel Haswell or later. "
            f"See: https://cloud.google.com/compute/docs/instances/nested-virtualization/overview"
        )


def _create_vm_with_fallback(
    vm_name: str,
    project: str,
    zone: str,
    machine_type: str,
    disk_size_gb: int,
    extra_args: list[str],
    timeout: int = 300,
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    """Try creating a VM, falling back through zones and machine types on RESOURCE_POOL_EXHAUSTED."""
    zones_to_try = [zone] + [z for z in _ZONE_FALLBACKS if z != zone]
    types_to_try = [machine_type] + [t for t in _MACHINE_TYPE_FALLBACKS if t != machine_type]

    last_err = ""
    for mt in types_to_try:
        _validate_machine_type(mt)
        for z in zones_to_try:
            cpu_platform = _MIN_CPU_PLATFORM.get(mt.split("-")[0], "Intel Skylake")
            r = _run_gcloud([
                "compute", "instances", "create", vm_name,
                f"--project={project}",
                f"--zone={z}",
                f"--machine-type={mt}",
                f"--source-snapshot={SNAPSHOT_NAME}",
                f"--boot-disk-size={disk_size_gb}GB",
                "--enable-nested-virtualization",
                f"--min-cpu-platform={cpu_platform}",
                *extra_args,
            ], timeout=timeout)
            if r.returncode == 0:
                if z != zone or mt != machine_type:
                    print(f"  → succeeded on {mt} in {z} (requested: {machine_type} in {zone})")
                return r, z, mt
            stderr = r.stderr + r.stdout
            if "ZONE_RESOURCE_POOL_EXHAUSTED" in stderr or "does not have enough resources" in stderr:
                print(f"  → {mt} in {z}: zone exhausted, trying next...")
                last_err = stderr.strip()
                continue
            if "does not exist in zone" in stderr or "does not exist" in stderr:
                print(f"  → {mt} in {z}: machine type unavailable in this zone, trying next...")
                last_err = stderr.strip()
                continue
            if "not compatible with CPU platform" in stderr:
                print(f"  → {mt} in {z}: CPU platform incompatibility, trying next type...")
                last_err = stderr.strip()
                break
            raise RuntimeError(f"Failed to create VM: {stderr.strip()}")

    raise RuntimeError(
        f"Failed to create VM in any zone/type combination. Last error:\n{last_err}"
    )


def vm_up(vm_name: str, zone: str = DEFAULT_ZONE, machine_type: str = DEFAULT_MACHINE_TYPE,
          disk_size_gb: int = DEFAULT_DISK_SIZE_GB) -> int:
    _validate_machine_type(machine_type)
    from lib.cloud_lab.constants import VM_NAME
    vm_name = vm_name or VM_NAME
    project = get_project()
    status = vm_status(project, zone, vm_name)
    if status == "RUNNING":
        ip = vm_external_ip(project, zone, vm_name)
        print(f"VM {vm_name} already RUNNING at {ip}")
        return 0
    if status and status != "RUNNING":
        print(f"VM {vm_name} exists ({status}), starting...")
        r = _run_gcloud(["compute", "instances", "start", vm_name, f"--project={project}", f"--zone={zone}"], timeout=120)
        return 0 if r.returncode == 0 else 1
    print(f"Creating VM from snapshot {SNAPSHOT_NAME}...")
    cpu_platform = _MIN_CPU_PLATFORM.get(machine_type.split("-")[0], "Intel Skylake")
    r = _run_gcloud([
        "compute", "instances", "create", vm_name,
        f"--project={project}", f"--zone={zone}",
        f"--machine-type={machine_type}",
        f"--source-snapshot={SNAPSHOT_NAME}",
        f"--boot-disk-size={disk_size_gb}GB",
        "--enable-nested-virtualization",
        f"--min-cpu-platform={cpu_platform}",
        "--tags=tollgate-runner",
    ], timeout=300)
    if r.returncode != 0 and vm_status(project, zone, vm_name) != "RUNNING":
        print(f"ERROR: {r.stderr}", file=sys.stderr)
        return 1
    ensure_firewall_rules(project)
    print(f"VM {vm_name} created")
    return 0


def vm_down(vm_name: str, zone: str = DEFAULT_ZONE) -> int:
    project = get_project()
    r = _run_gcloud([
        "compute", "instances", "delete", vm_name,
        f"--project={project}", f"--zone={zone}", "--delete-disks=all", "--quiet",
    ], timeout=120)
    return r.returncode


def get_project() -> str:
    r = _run_gcloud(["config", "get-value", "project"], timeout=30)
    if r.returncode != 0 or not r.stdout.strip():
        print("ERROR: No GCP project set. Run: gcloud config set project <PROJECT_ID>", file=sys.stderr)
        sys.exit(1)
    return r.stdout.strip()


def ensure_firewall_rules(project: str) -> None:
    r = _run_gcloud(["compute", "firewall-rules", "describe", FIREWALL_RULE_SSH, f"--project={project}"], timeout=30)
    if r.returncode == 0:
        return
    _run_gcloud([
        "compute", "firewall-rules", "create", FIREWALL_RULE_SSH,
        f"--project={project}", "--allow=tcp:22", "--source-ranges=0.0.0.0/0",
        "--description=Allow SSH for TollGate test runner",
    ], timeout=60)


def _sanitize_vm_name(run_id: str) -> str:
    safe = re.sub(r"[^a-z0-9-]", "-", run_id.lower())[:50].strip("-")
    return f"tollgate-run-{safe}"


def _suite_ref() -> str:
    repo_dir = Path(__file__).resolve().parents[2]
    r = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=15,
        check=False,
    )
    local = r.stdout.strip() if r.returncode == 0 else ""
    if not local:
        return "main"
    r2 = subprocess.run(
        ["git", "-C", str(repo_dir), "branch", "-r", "--contains", local],
        capture_output=True, text=True, timeout=15,
        check=False,
    )
    if r2.returncode != 0 or not r2.stdout.strip():
        print(f"WARNING: HEAD ({local[:7]}) not on any remote branch — falling back to 'main'. "
              "Push your changes first for the VM to use them.", file=sys.stderr)
        return "main"
    return local


def _gh_token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15, check=False)
        if r.returncode == 0 and r.stdout.strip():
            token = r.stdout.strip()
    if not token:
        raise RuntimeError("GH_TOKEN or GITHUB_TOKEN required (or run gh auth login)")
    return token


def _nsec_hex() -> str:
    nsec = os.environ.get("BOT_NSEC_HEX") or os.environ.get("NSEC_HEX", "")
    if nsec:
        return nsec.strip()
    for candidate in [
        os.path.expanduser("~/.config/prta/nsec"),
        os.path.expanduser("~/nsec"),
    ]:
        try:
            return Path(candidate).read_text().strip()
        except (OSError, FileNotFoundError):
            continue
    return ""


_OVERLAY_ALLOWLIST = {
    "docs/virtual-lab.md",
    "docs/virtual-wifi-architecture.md",
    "lib/backend.py",
    "lib/constants.py",
    "lib/cloud_lab/constants.py",
    "lib/cloud_lab/gcp.py",
    "lib/cloud_lab/worker/__init__.py",
    "lib/cloud_lab/worker/__main__.py",
    "lib/cloud_lab/worker/config.py",
    "lib/cloud_lab/worker/deploy.py",
    "lib/cloud_lab/worker/inner_ssh.py",
    "lib/cloud_lab/worker/logstream.py",
    "lib/cloud_lab/worker/mints.py",
    "lib/cloud_lab/worker/network.py",
    "lib/cloud_lab/worker/pipeline.py",
    "lib/cloud_lab/worker/preflight.py",
    "lib/cloud_lab/worker/provision.py",
    "lib/cloud_lab/worker/report.py",
    "lib/cloud_lab/worker/runner.py",
    "lib/cloud_lab/worker/shell.py",
    "lib/cloud_lab/worker/vms.py",
    "lib/cloud_lab/worker/wifi.py",
    "lib/deploy.py",
    "lib/portal.py",
    "lib/helpers.py",
    "lib/router.py",
    "lib/cashu.py",
    "lib/reseller_mode.py",
    "pytest.ini",
    "scripts/cloud-lab.py",
    "scripts/build-vwifi.sh",
    "scripts/hwsim-netns-poc.py",
    "scripts/virtual-lab.py",
    "scripts/collect-results.py",
    "scripts/render-report.py",
    "tests/conftest.py",
    "tests/api/test_concurrent_payments.py",
    "tests/api/test_degraded_mode.py",
    "tests/api/test_edge_tokens.py",
    "tests/api/test_e2e_portal_payment.py",
    "tests/api/test_lightning_portal.py",
    "tests/api/test_mint_startup_recovery_latency.py",
    "tests/api/test_portal_verify.py",
    "tests/api/test_mac80211_hwsim.py",
    "tests/api/test_try_all_mints.py",
    "tests/api/test_virtual_wifi_hwsim_netns.py",
    "tests/api/test_dual_mint.py",
    "tests/scenarios/test_reseller_mode.py",
    "tests/scenarios/test_two_router_cloud.py",
}


def _working_tree_overlay() -> str:
    """Return a base64 tar.gz overlay for local suite changes needed by cloud runs."""
    repo_dir = Path(__file__).resolve().parents[2]
    changed: list[str] = []
    status = subprocess.run(
        ["git", "-C", str(repo_dir), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if status.returncode != 0:
        return ""
    for line in status.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path in _OVERLAY_ALLOWLIST:
            changed.append(path)
    if not changed:
        return ""

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in sorted(set(changed)):
            full = repo_dir / rel
            if full.is_file():
                tar.add(full, arcname=rel)
    payload = base64.b64encode(buf.getvalue()).decode()
    print(f"Including local suite overlay for cloud run: {', '.join(sorted(set(changed)))}")
    return payload


def _build_startup_script(suite_overlay_b64: str = "") -> str:
    overlay_step = ""
    if suite_overlay_b64:
        overlay_step = textwrap.dedent(f"""\
            cat > /tmp/tollgate-suite-overlay.tar.gz.b64 <<'OVERLAY'
            {suite_overlay_b64}
            OVERLAY
            base64 -d /tmp/tollgate-suite-overlay.tar.gz.b64 > /tmp/tollgate-suite-overlay.tar.gz
            tar xzf /tmp/tollgate-suite-overlay.tar.gz -C /opt/tollgate-test
            echo "Applied local suite overlay"
        """)
    return textwrap.dedent(f"""\
        #!/bin/bash
        set -euo pipefail
        exec >> /var/log/tollgate-run.log 2>&1
        echo "=== TollGate cloud worker started $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

         # Lease-based kill switch: polls tollgate-delete-at metadata every 60s.
         # When the epoch timestamp in metadata is reached, VM self-deletes.
         # The host can extend the lease via `cloud-lab.py extend --run-id X`.
         # Hard backstop: 3h (10800s) from boot, regardless of lease extensions.
         KILL_SWITCH_PROJECT=$(curl -sf -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/project/project-id || true)
         KILL_SWITCH_ZONE=$(curl -sf -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/zone || true)
         KILL_SWITCH_ZONE_BASE=$(basename "$KILL_SWITCH_ZONE" 2>/dev/null || echo "")
         KILL_SWITCH_NAME=$(curl -sf -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/name || true)
         BOOT_TIME=$(date +%s)
         setsid bash -c '
           SELF=$0; PROJ=$1; ZONE=$2; BOOT=$3
           while true; do
             NOW=$(date +%s)
             HARD_LIMIT=$((BOOT + 10800))
             if [ "$NOW" -ge "$HARD_LIMIT" ]; then
               echo "3h hard limit reached — self-deleting VM" >> /var/log/tollgate-run.log
               gcloud compute instances delete "$SELF" --project="$PROJ" --zone="$ZONE" --delete-disks=all --quiet >> /var/log/tollgate-run.log 2>&1 || shutdown -h now "TollGate self-delete failed"
               exit 0
             fi
             DELETE_AT=$(curl -sf -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/attributes/tollgate-delete-at 2>/dev/null || echo "0")
             if [ "$DELETE_AT" != "0" ] && [ "$DELETE_AT" != "" ] && [ "$NOW" -ge "$DELETE_AT" ]; then
               echo "Lease expired (delete-at=$DELETE_AT, now=$NOW) — self-deleting VM" >> /var/log/tollgate-run.log
               gcloud compute instances delete "$SELF" --project="$PROJ" --zone="$ZONE" --delete-disks=all --quiet >> /var/log/tollgate-run.log 2>&1 || shutdown -h now "TollGate self-delete failed"
               exit 0
             fi
             sleep 60
           done
         ' "$KILL_SWITCH_NAME" "$KILL_SWITCH_PROJECT" "$KILL_SWITCH_ZONE_BASE" "$BOOT_TIME" </dev/null >/dev/null 2>&1 &
         KILL_SWITCH_PID=$!
         echo "Lease kill switch armed: PID=$KILL_SWITCH_PID (polling every 60s, 3h hard limit)"

        export HOME="/root"
        export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        export GH_TOKEN=$(curl -sf -H "Metadata-Flavor: Google" \\
            http://metadata.google.internal/computeMetadata/v1/instance/attributes/tollgate-gh-token)
        export BOT_NSEC_HEX=$(curl -sf -H "Metadata-Flavor: Google" \\
            http://metadata.google.internal/computeMetadata/v1/instance/attributes/tollgate-nsec-hex 2>/dev/null || echo "")

        cleanup() {{
            STATUS=$?
            ZONE=$(curl -sf -H "Metadata-Flavor: Google" \\
                http://metadata.google.internal/computeMetadata/v1/instance/attributes/tollgate-zone)
            PROJECT=$(curl -sf -H "Metadata-Flavor: Google" \\
                http://metadata.google.internal/computeMetadata/v1/instance/attributes/tollgate-project)
            NAME=$(curl -sf -H "Metadata-Flavor: Google" \\
                http://metadata.google.internal/computeMetadata/v1/instance/name)
            KEEP=$(curl -sf -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/attributes/tollgate-keep-vm-on-failure || true)
            echo "=== Teardown $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
            if [ "$KEEP" = "true" ]; then
                echo "Keeping VM for log inspection (keep-vm-on-failure=true, exit=$STATUS)"
                return
            fi
            gcloud compute instances delete "$NAME" --project="$PROJECT" --zone="$ZONE" \\
                --delete-disks=all --quiet 2>/dev/null || true
        }}
        trap cleanup EXIT

        apt-get update -qq 2>/dev/null || true
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv git curl sshpass \\
            qemu-utils iproute2 iptables gh 2>/dev/null || true

        if ! command -v gcloud >/dev/null; then
            echo "Installing Google Cloud SDK for self-delete..."
            apt-get install -y -qq apt-transport-https ca-certificates gnupg 2>/dev/null || true
            echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \\
                > /etc/apt/sources.list.d/google-cloud-sdk.list 2>/dev/null || true
            curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \\
                | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg 2>/dev/null || true
            apt-get update -qq && apt-get install -y -qq google-cloud-cli 2>/dev/null || true
        fi

        cd /opt
        rm -rf tollgate-test
        git clone --depth 50 https://github.com/{SUITE_REPO}.git tollgate-test
        cd tollgate-test
        git config --global --add safe.directory /opt/tollgate-test
        SUITE_REF=$(curl -sf -H "Metadata-Flavor: Google" \\
            http://metadata.google.internal/computeMetadata/v1/instance/attributes/tollgate-suite-ref)
        git fetch --depth 1 origin "$SUITE_REF" 2>/dev/null || true
        git checkout "$SUITE_REF"
        {overlay_step}

        if [ -d /opt/tollgate-venv ]; then
            /opt/tollgate-venv/bin/pip install -q -r requirements.txt 2>/dev/null || true
        else
            python3 -m venv /opt/tollgate-venv || true
            /opt/tollgate-venv/bin/pip install -q -r requirements.txt || true
        fi
        /opt/tollgate-venv/bin/python3 -m lib.cloud_lab.worker --from-metadata
        echo "=== Worker exited with code $? ==="
    """)


def _build_runner_startup_script() -> str:
    return textwrap.dedent("""\
        #!/bin/bash
        set -euo pipefail
        exec >> /var/log/runner-startup.log 2>&1
        echo "=== GitHub Actions runner started $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

        KILL_SWITCH_PROJECT=$(curl -sf -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/project/project-id || true)
        KILL_SWITCH_ZONE=$(curl -sf -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/zone || true)
        KILL_SWITCH_ZONE_BASE=$(basename "$KILL_SWITCH_ZONE" 2>/dev/null || echo "")
        KILL_SWITCH_NAME=$(curl -sf -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/name || true)
        BOOT_TIME=$(date +%s)
        setsid bash -c '
          SELF=$0; PROJ=$1; ZONE=$2; BOOT=$3
          while true; do
            NOW=$(date +%s)
            HARD_LIMIT=$((BOOT + 10800))
            if [ "$NOW" -ge "$HARD_LIMIT" ]; then
              echo "3h hard limit reached — self-deleting VM" >> /var/log/runner-startup.log
              gcloud compute instances delete "$SELF" --project="$PROJ" --zone="$ZONE" --delete-disks=all --quiet >> /var/log/runner-startup.log 2>&1 || shutdown -h now "TollGate self-delete failed"
              exit 0
            fi
            DELETE_AT=$(curl -sf -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/attributes/tollgate-delete-at 2>/dev/null || echo "0")
            if [ "$DELETE_AT" != "0" ] && [ "$DELETE_AT" != "" ] && [ "$NOW" -ge "$DELETE_AT" ]; then
              echo "Lease expired (delete-at=$DELETE_AT, now=$NOW) — self-deleting VM" >> /var/log/runner-startup.log
              gcloud compute instances delete "$SELF" --project="$PROJ" --zone="$ZONE" --delete-disks=all --quiet >> /var/log/runner-startup.log 2>&1 || shutdown -h now "TollGate self-delete failed"
              exit 0
            fi
            sleep 60
          done
        ' "$KILL_SWITCH_NAME" "$KILL_SWITCH_PROJECT" "$KILL_SWITCH_ZONE_BASE" "$BOOT_TIME" </dev/null >/dev/null 2>&1 &
        KILL_SWITCH_PID=$!
        echo "Lease kill switch armed: PID=$KILL_SWITCH_PID (polling every 60s, 3h hard limit)"

        export RUNNER_ALLOW_RUNASROOT=1
        JIT_CONFIG=$(curl -sf -H "Metadata-Flavor: Google" \\
            http://metadata.google.internal/computeMetadata/v1/instance/attributes/jit-config)

        cd /home/runner/actions-runner

        # JIT ephemeral runners exit with code 0 when no job is available yet.
        # Retry up to 30 times (5 min) to handle the race between workflow
        # dispatch and runner registration.
        MAX_ATTEMPTS=30
        ATTEMPT=0
        JOB_RAN=false
        while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
            ATTEMPT=$((ATTEMPT + 1))
            echo "Starting ephemeral runner agent (attempt $ATTEMPT/$MAX_ATTEMPTS)..."
            ./run.sh --jitconfig "$JIT_CONFIG" 2>&1 | tee /tmp/runner-output.log
            RUNNER_EXIT=${PIPESTATUS[0]}
            echo "Runner exited with code $RUNNER_EXIT"

            # Check if a job actually ran (runner prints "Job completed" on success)
            if grep -q "Job completed" /tmp/runner-output.log 2>/dev/null; then
                echo "Job executed successfully"
                JOB_RAN=true
                break
            fi

            # Non-zero exit = real error, don't retry
            if [ "$RUNNER_EXIT" -ne 0 ]; then
                echo "Runner exited with error code $RUNNER_EXIT, not retrying"
                break
            fi

            # Exit code 0 but no job ran = idle exit, retry after delay
            echo "Runner exited idle (no job), retrying in 10s..."
            sleep 10
        done

        if [ "$JOB_RAN" = "false" ] && [ $ATTEMPT -ge $MAX_ATTEMPTS ]; then
            echo "ERROR: Runner failed to pick up a job after $MAX_ATTEMPTS attempts (5 min)"
        fi

        ZONE=$(curl -sf -H "Metadata-Flavor: Google" \\
            http://metadata.google.internal/computeMetadata/v1/instance/attributes/tollgate-zone)
        PROJECT=$(curl -sf -H "Metadata-Flavor: Google" \\
            http://metadata.google.internal/computeMetadata/v1/instance/attributes/tollgate-project)
        NAME=$(curl -sf -H "Metadata-Flavor: Google" \\
            http://metadata.google.internal/computeMetadata/v1/instance/name)
        KEEP=$(curl -sf -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/attributes/tollgate-keep-vm-on-failure || true)

        if [ "$KEEP" = "true" ]; then
            echo "Keeping VM for inspection (tollgate-keep-vm-on-failure=true)"
            shutdown -h now "Runner finished, VM retained"
        else
            echo "Self-deleting VM..."
            gcloud compute instances delete "$NAME" --project="$PROJECT" --zone="$ZONE" \\
                --delete-disks=all --quiet || shutdown -h now "Self-delete failed"
        fi
    """)


def submit_run(
    target: RunTarget,
    *,
    zone: str = DEFAULT_ZONE,
    publish: bool = False,
    artifact_timeout_s: int = 1800,
    machine_type: str = DEFAULT_MACHINE_TYPE,
    disk_size_gb: int = DEFAULT_DISK_SIZE_GB,
    reseller_scenarios: bool = False,
    two_router: bool = False,
    secondary_router_host: str = "",
    secondary_router_port: str = "",
    keep_vm_on_failure: bool = False,
    mint: str = "auto",
    portal: str = "builtin",
    quick: bool = False,
    smoke: bool = False,
    complete: bool = False,
    hwsim: bool = False,
    vwifi: bool = False,
    wifi_plane: str = "tap",
    lease_minutes: int = 60,
    benchmark: bool = False,
) -> dict[str, str]:
    """Pre-flight artifact check, then create fire-and-forget GCP VM. Returns run metadata."""
    cleanup_stale(max_age_hours=2)
    project = get_project()
    print(f"Waiting for CI artifact ({target.repo}@{target.branch}, arch=x86_64)...")
    artifact_run_id = ensure_target_artifact(target, timeout_s=artifact_timeout_s)
    print(f"Artifact ready: run {artifact_run_id}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short = (target.sut_commit or target.branch)[:7]
    # Sanitize slashes from branch names (e.g. fix/v2-keyset-ids → fix-v2-)
    # Slashes in run_id create nested directory paths that break results
    # collection and gh-pages publish.
    short = short.replace("/", "-")
    run_id = f"{timestamp}-{short}"
    vm_name = _sanitize_vm_name(run_id)
    suite_ref = _suite_ref()
    token = _gh_token()
    nsec = _nsec_hex()

    startup_script = _build_startup_script(_working_tree_overlay())
    script_path = Path(f"/tmp/tollgate-startup-{vm_name}.sh")
    script_path.write_text(startup_script)

    metadata = {
        "tollgate-run-id": run_id,
        "tollgate-pr": target.pr or "",
        "tollgate-sut-commit": target.sut_commit or "",
        "tollgate-sut-branch": target.branch,
        "tollgate-artifact-run-id": artifact_run_id,
        "tollgate-artifact-repo": target.repo,
        "tollgate-pr-repo": target.pr_repo or target.repo,
        "tollgate-suite-ref": suite_ref,
        "tollgate-backend": target.backend,
        "tollgate-publish": "true" if publish else "false",
        "tollgate-project": project,
        "tollgate-zone": zone,
        "tollgate-vm-name": vm_name,
        "tollgate-gh-token": token,
        "tollgate-nsec-hex": nsec,
        "tollgate-reseller-scenarios": "true" if reseller_scenarios else "false",
        "tollgate-two-router": "true" if two_router else "false",
        "tollgate-secondary-router-host": secondary_router_host,
        "tollgate-secondary-router-port": secondary_router_port,
        "tollgate-keep-vm-on-failure": "true" if keep_vm_on_failure else "false",
        "tollgate-mint": mint,
        "tollgate-portal": portal,
        "tollgate-quick": "true" if quick else "false",
        "tollgate-smoke": "true" if smoke else "false",
        "tollgate-complete": "true" if complete else "false",
        "tollgate-hwsim": "true" if hwsim else "false",
        "tollgate-vwifi": "true" if vwifi else "false",
        "tollgate-wifi-plane": wifi_plane,
        "tollgate-lease-minutes": str(lease_minutes),
        "tollgate-benchmark": "true" if benchmark else "false",
        "tollgate-delete-at": str(int(time.time()) + lease_minutes * 60),
    }
    metadata_payload = ",".join(f"{k}={v}" for k, v in metadata.items())

    _validate_machine_type(machine_type)
    ensure_firewall_rules(project)
    print(f"Creating VM {vm_name} from snapshot {SNAPSHOT_NAME}...")
    extra_args = [
        "--tags=tollgate-runner,tollgate-run",
        "--labels=tollgate_run=true",
        "--scopes=compute-rw,storage-rw",
        f"--metadata={metadata_payload}",
        f"--metadata-from-file=startup-script={script_path}",
    ]
    r, actual_zone, actual_type = _create_vm_with_fallback(
        vm_name, project, zone, machine_type, disk_size_gb, extra_args, timeout=300,
    )
    script_path.unlink(missing_ok=True)

    if actual_zone != zone:
        r = _run_gcloud([
            "compute", "instances", "add-metadata", vm_name,
            f"--project={project}",
            f"--zone={actual_zone}",
            f"--metadata=tollgate-zone={actual_zone}",
        ], timeout=30)
        if r.returncode != 0:
            print(f"WARNING: Failed to update zone metadata for {vm_name}: {r.stderr}", file=sys.stderr)

    log_hint = (
        f"gcloud compute ssh {vm_name} --project={project} --zone={actual_zone} "
        f"--command='tail -f /var/log/tollgate-run.log'"
    )
    info = {
        "run_id": run_id,
        "vm_name": vm_name,
        "project": project,
        "zone": actual_zone,
        "machine_type": actual_type,
        "artifact_run_id": artifact_run_id,
        "suite_ref": suite_ref,
        "log_hint": log_hint,
    }
    return info


def submit_runner(
    encoded_jit_config: str,
    *,
    run_id: str,
    zone: str = DEFAULT_ZONE,
    machine_type: str = DEFAULT_MACHINE_TYPE,
    disk_size_gb: int = DEFAULT_DISK_SIZE_GB,
    keep_vm_on_failure: bool = False,
) -> dict[str, str]:
    cleanup_stale(max_age_hours=2)
    project = get_project()
    _validate_machine_type(machine_type)

    vm_name = _sanitize_vm_name(run_id)

    startup_script = _build_runner_startup_script()
    script_path = Path(f"/tmp/tollgate-runner-startup-{vm_name}.sh")
    script_path.write_text(startup_script)

    jit_config_path = Path(f"/tmp/tollgate-jit-config-{vm_name}.txt")
    jit_config_path.write_text(encoded_jit_config)

    metadata = {
        "tollgate-run-id": run_id,
        "tollgate-project": project,
        "tollgate-zone": zone,
        "tollgate-vm-name": vm_name,
        "tollgate-keep-vm-on-failure": "true" if keep_vm_on_failure else "false",
        "tollgate-lease-minutes": "60",
        "tollgate-delete-at": str(int(time.time()) + 3600),
    }
    metadata_payload = ",".join(f"{k}={v}" for k, v in metadata.items())

    ensure_firewall_rules(project)
    print(f"Creating runner VM {vm_name} from snapshot {SNAPSHOT_NAME}...")
    extra_args = [
        "--tags=tollgate-runner,tollgate-run",
        "--labels=tollgate_run=true",
        "--scopes=compute-rw,storage-rw",
        f"--metadata={metadata_payload}",
        f"--metadata-from-file=startup-script={script_path},jit-config={jit_config_path}",
    ]
    r, actual_zone, actual_type = _create_vm_with_fallback(
        vm_name, project, zone, machine_type, disk_size_gb, extra_args, timeout=300,
    )
    script_path.unlink(missing_ok=True)
    jit_config_path.unlink(missing_ok=True)

    if actual_zone != zone:
        _run_gcloud([
            "compute", "instances", "add-metadata", vm_name,
            f"--project={project}",
            f"--zone={actual_zone}",
            f"--metadata=tollgate-zone={actual_zone}",
        ], timeout=30)

    log_hint = (
        f"gcloud compute ssh {vm_name} --project={project} --zone={actual_zone} "
        f"--command='tail -f /var/log/runner-startup.log'"
    )
    info = {
        "run_id": run_id,
        "vm_name": vm_name,
        "project": project,
        "zone": actual_zone,
        "machine_type": actual_type,
        "log_hint": log_hint,
    }
    print(f"Runner VM created: {vm_name}")
    print(f"Logs: {log_hint}")
    return info


def status_run(run_id: str, zone: str = DEFAULT_ZONE) -> int:
    project = get_project()
    r = _run_gcloud([
        "compute", "instances", "list",
        f"--project={project}",
        f"--filter=metadata.tollgate-run-id={run_id}",
        "--format=json",
    ], timeout=30)
    if r.returncode != 0 or not r.stdout.strip():
        print(f"No VM found for run_id={run_id}")
        print("If the run finished, check https://tests.tollgate.me/")
        return 1
    try:
        instances = json.loads(r.stdout)
    except json.JSONDecodeError:
        print(r.stdout)
        return 1
    if not instances:
        print(f"Run {run_id}: VM deleted (likely finished). Check https://tests.tollgate.me/")
        return 0
    inst = instances[0]
    name = inst.get("name", "?")
    status = inst.get("status", "?")
    print(f"Run: {run_id}")
    print(f"VM:  {name} ({status})")
    for iface in inst.get("networkInterfaces", []):
        for ac in iface.get("accessConfigs", []):
            if ac.get("natIP"):
                print(f"IP:  {ac['natIP']}")
    print(f"Logs: gcloud compute ssh {name} --project={project} --zone={zone} --command='tail -f /var/log/tollgate-run.log'")
    return 0


def cleanup_stale(zone: str = DEFAULT_ZONE, max_age_hours: int = 1) -> int:
    return _delete_tollgate_vms(zone, max_age_hours=max_age_hours)


def cleanup_all(zone: str = DEFAULT_ZONE) -> int:
    return _delete_tollgate_vms(zone, max_age_hours=0)


def _delete_tollgate_vms(zone: str = DEFAULT_ZONE, max_age_hours: int = 1) -> int:
    """Delete tollgate VMs older than max_age_hours.

    Zone-aware: extracts each VM's actual zone from its GCP metadata so
    that fallback VMs created in alternate zones are correctly deleted.
    """
    project = get_project()
    # Include all statuses (RUNNING, TERMINATED, STOPPING, STOPPED).
    # TERMINATED VMs still cost money for persistent disks and clutter the project.
    filter_str = "labels.tollgate_run=true"
    r = _run_gcloud([
        "compute", "instances", "list",
        f"--project={project}",
        f"--filter={filter_str}",
        "--format=json",
    ], timeout=60)
    if r.returncode != 0:
        print(f"ERROR: {r.stderr}", file=sys.stderr)
        return 1
    try:
        instances = json.loads(r.stdout) if r.stdout.strip() else []
    except json.JSONDecodeError:
        instances = []
    cutoff = time.time() - max_age_hours * 3600
    deleted = 0
    failed = 0
    for inst in instances:
        name = inst.get("name")
        creation = inst.get("creationTimestamp", "")
        if not name:
            continue
        try:
            created = datetime.fromisoformat(creation.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if created > cutoff:
            continue
        vm_zone = _extract_zone_from_instance(inst, fallback_zone=zone)
        label = "stale" if max_age_hours > 0 else "tollgate"
        print(f"Deleting {label} VM {name} in {vm_zone} (created {creation})...")
        dr = _run_gcloud([
            "compute", "instances", "delete", name,
            f"--project={project}", f"--zone={vm_zone}",
            "--delete-disks=all", "--quiet",
        ], timeout=120)
        if dr.returncode == 0:
            deleted += 1
        else:
            failed += 1
            print(f"WARNING: Failed to delete VM {name}: {dr.stderr[:200]}", file=sys.stderr)
    total = len([i for i in instances if i.get("name")])
    print(f"Deleted {deleted}/{total} stale VM(s)" + (f", {failed} failed" if failed else ""))
    return 0 if deleted == total else 1


def _extract_zone_from_instance(inst, fallback_zone=DEFAULT_ZONE):
    """Extract the zone name from a GCP instance dict.

    GCP returns zone as a full URL:
        https://www.googleapis.com/compute/v1/projects/X/zones/us-east1-b
    Falls back to parsing selfLink, then to fallback_zone.
    """
    for field in ("zone",):
        val = inst.get(field, "")
        if val and "/" in val:
            return val.rsplit("/", 1)[-1]
    self_link = inst.get("selfLink", "")
    if "/zones/" in self_link:
        try:
            after_zones = self_link.split("/zones/", 1)[1]
            return after_zones.split("/", 1)[0]
        except (IndexError, ValueError):
            pass
    return fallback_zone


def _find_vm_by_run_id(run_id: str) -> tuple[str, str, str] | None:
    project = get_project()
    r = _run_gcloud([
        "compute", "instances", "list",
        f"--project={project}",
        f"--filter=metadata.tollgate-run-id={run_id}",
        "--format=json",
    ], timeout=60)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        instances = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    if not instances:
        return None
    inst = instances[0]
    name = inst.get("name", "")
    zone_raw = inst.get("zone", "")
    zone = zone_raw.rsplit("/", 1)[-1] if "/" in zone_raw else zone_raw
    return name, zone, project


def extend_lease(run_id: str, minutes: int = 60, *, zone: str = DEFAULT_ZONE) -> int:
    found = _find_vm_by_run_id(run_id)
    if not found:
        print(f"ERROR: No VM found for run-id {run_id}", file=sys.stderr)
        return 1
    vm_name, actual_zone, project = found
    new_delete_at = str(int(time.time()) + minutes * 60)
    r = _run_gcloud([
        "compute", "instances", "add-metadata", vm_name,
        f"--project={project}",
        f"--zone={actual_zone}",
        f"--metadata=tollgate-delete-at={new_delete_at}",
    ], timeout=30)
    if r.returncode != 0:
        print(f"ERROR: Failed to extend lease: {r.stderr}", file=sys.stderr)
        return 1
    print(f"Lease extended for {vm_name}: auto-delete in {minutes} minutes")
    return 0


def delete_by_run_id(run_id: str, *, zone: str = DEFAULT_ZONE) -> int:
    found = _find_vm_by_run_id(run_id)
    if not found:
        print(f"ERROR: No VM found for run-id {run_id}", file=sys.stderr)
        return 1
    vm_name, actual_zone, project = found
    status = vm_status(project, actual_zone, vm_name)
    if not status:
        print(f"VM {vm_name} does not exist (already deleted?)")
        return 0
    print(f"Deleting VM {vm_name} (status={status})...")
    r = _run_gcloud([
        "compute", "instances", "delete", vm_name,
        f"--project={project}", f"--zone={actual_zone}",
        "--delete-disks=all", "--quiet",
    ], timeout=120)
    if r.returncode != 0:
        print(f"ERROR: Failed to delete VM: {r.stderr}", file=sys.stderr)
        return 1
    print(f"VM {vm_name} deleted")
    return 0
