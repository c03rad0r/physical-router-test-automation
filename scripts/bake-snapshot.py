#!/usr/bin/env python3
"""Bake a snapshot with all cloud lab dependencies pre-installed.

Supports GCP and SHC (Sovereign Hybrid Compute). Creates a temporary VM,
installs gh CLI, Python venv, cashu venv, and pre-provisions the OpenWrt
base image. Then snapshots the disk and cleans up.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import cast

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.cloud_lab.constants import (
    DEFAULT_DISK_SIZE_GB,
    DEFAULT_MACHINE_TYPE,
    DEFAULT_ZONE,
    OPENWRT_IP,
    SNAPSHOT_NAME,
    SUITE_REPO_URL,
    VIRT_LAB_PASSWORD,
    VIRT_LAB_WORKDIR,
)

_OPENWRT_VERSION = "24.10.1"
_DEBIAN_IMAGE = "debian-12-nocloud-amd64.qcow2"
_DEBIAN_IMAGE_URL = f"https://cloud.debian.org/images/cloud/bookworm/latest/{_DEBIAN_IMAGE}"
from lib.cloud_lab.gcp import (
    ensure_firewall_rules,
    get_project,
)


# ── Provider Abstraction ─────────────────────────────────────────

class CloudProvider(ABC):
    """Abstract VM lifecycle + SSH for bake operations."""
    name: str

    @abstractmethod
    def create_vm(self, name: str, machine_type: str, disk_size_gb: int) -> dict:
        """Create a VM. Returns dict with at least {'ip': str, 'id': str}."""
        ...

    @abstractmethod
    def ssh(self, ip: str, command: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
        """Run a command on the VM via SSH."""
        ...

    @abstractmethod
    def wait_ssh(self, ip: str, timeout: int = 180) -> bool:
        """Wait until SSH is available."""
        ...

    @abstractmethod
    def create_snapshot(self, vm_id: str, name: str) -> bool:
        """Take a snapshot of the VM. Returns True on success."""
        ...

    @abstractmethod
    def delete_vm(self, vm_id: str) -> None:
        """Delete the VM."""
        ...

    @abstractmethod
    def get_ssh_user(self) -> str:
        """Return the SSH username for this provider."""
        ...


class GCPProvider(CloudProvider):
    """GCP VM lifecycle via gcloud CLI."""

    name = "gcp"

    def __init__(self, zone: str, project: str, base_snapshot: str = ""):
        self.zone = zone
        self.project = project
        self.base_snapshot = base_snapshot

    def _run_gcloud(self, args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
        markers = (
            "NameResolutionError", "Failed to resolve", "ConnectionError",
            "Max retries exceeded", "Network is unreachable", "timed out",
        )
        last = subprocess.CompletedProcess(args=["gcloud"], returncode=1, stdout="", stderr="")
        for attempt in range(1, 4):
            last = subprocess.run(
                ["gcloud", *args],
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

    def create_vm(self, name: str, machine_type: str, disk_size_gb: int) -> dict:
        ensure_firewall_rules(self.project)
        r = self._run_gcloud([
            "compute", "instances", "create", name,
            f"--project={self.project}", f"--zone={self.zone}",
            f"--machine-type={machine_type}",
            f"--source-snapshot={self.base_snapshot}",
            f"--boot-disk-size={disk_size_gb}GB",
            "--enable-nested-virtualization",
            "--min-cpu-platform=Intel Cascade Lake",
            "--tags=tollgate-runner",
        ], timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"Failed to create GCP VM: {r.stderr}")
        # GCP uses VM name as connection target (gcloud resolves IP internally)
        return {"ip": name, "id": name}

    def ssh(self, ip: str, command: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
        vm_name = ip  # For GCP, ip is actually the VM name
        wrapped = f"sudo HOME=/root bash -c {shlex.quote(command)}"
        cmd = [
            "gcloud", "compute", "ssh", vm_name,
            f"--project={self.project}", f"--zone={self.zone}",
            "--command", wrapped,
            "--ssh-flag=-o StrictHostKeyChecking=no",
            "--ssh-flag=-o UserKnownHostsFile=/dev/null",
            "--ssh-flag=-o ConnectTimeout=10",
            "--quiet",
        ]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)

    def wait_ssh(self, ip: str, timeout: int = 180) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.ssh(ip, "echo SSH_OK", timeout=15)
            if r.returncode == 0 and "SSH_OK" in r.stdout:
                return True
            elapsed = int(time.time() - deadline + timeout)
            print(f"  Waiting for SSH... ({elapsed}s elapsed)")
            time.sleep(10)
        return False

    def create_snapshot(self, vm_id: str, name: str) -> bool:
        self._run_gcloud([
            "compute", "instances", "stop", vm_id,
            f"--project={self.project}", f"--zone={self.zone}", "--quiet",
        ], timeout=120)
        r = self._run_gcloud([
            "compute", "disks", "snapshot", vm_id,
            f"--project={self.project}", f"--zone={self.zone}",
            f"--snapshot-names={name}",
            "--storage-location=europe-west1",
        ], timeout=120)
        if r.returncode != 0:
            print(f"ERROR: Snapshot creation failed: {r.stderr}", file=sys.stderr)
            return False
        return True

    def delete_vm(self, vm_id: str) -> None:
        self._run_gcloud([
            "compute", "instances", "delete", vm_id,
            f"--project={self.project}", f"--zone={self.zone}",
            "--delete-disks=all", "--quiet",
        ], timeout=120)

    def get_ssh_user(self) -> str:
        return "root"


class SHCProvider(CloudProvider):
    """SHC (Sovereign Hybrid Compute) VM lifecycle via REST API + SSH."""

    name = "shc"
    _API_BASE = "https://blesta.sovereignhybridcompute.com/user-api/v2"
    _PACKAGE_ID = 81
    _PRICING_ID = 245

    def __init__(self):
        self.api_key = os.environ.get("SHC_API_KEY", "")
        if not self.api_key:
            raise ValueError("SHC_API_KEY not set. Required for --cloud shc.")

    def _ssh_key_path(self) -> str:
        return os.environ.get("SHC_SSH_KEY", os.path.expanduser("~/.ssh/id_ed25519_gitlab"))

    def _ssh_pubkey(self) -> str:
        pub_path = self._ssh_key_path() + ".pub"
        with open(pub_path) as f:
            return f.read().strip()

    def _api(self, method: str, path: str, data: dict | None = None,
             confirm_id: str | None = None) -> dict:
        url = f"{self._API_BASE}{path}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if confirm_id:
            headers["X-User-Api-Confirm"] = confirm_id
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()
            print(f"SHC API error {e.code}: {err_body[:500]}", file=sys.stderr)
            raise

    def create_vm(self, name: str, machine_type: str, disk_size_gb: int) -> dict:
        pubkey = self._ssh_pubkey()
        order = {
            "package_id": self._PACKAGE_ID,
            "pricing_id": self._PRICING_ID,
            "hostname": name,
            "ssh_key": pubkey,
        }
        resp = self._api("POST", "/ordering/submit", order)
        if resp.get("confirmation_required"):
            cnf_id = resp.get("confirmation_id", "")
            print(f"  Confirming order ({cnf_id})...")
            resp = self._api("POST", "/ordering/submit", order, confirm_id=cnf_id)
        vm_id = str(resp.get("service_id") or resp.get("id") or "")
        if not vm_id:
            raise RuntimeError(f"SHC order did not return a VM ID: {resp}")
        print(f"  VM ordered: service_id={vm_id}, waiting for provisioning...")
        summary = self._wait_provisioning(int(vm_id))
        ips = summary.get("ips", [])
        if not ips:
            raise RuntimeError(f"SHC VM {vm_id} has no IP addresses")
        ip = ips[0].get("ip", "") if isinstance(ips[0], dict) else str(ips[0])
        print(f"  VM ready: id={vm_id}, ip={ip}")
        return {"ip": ip, "id": vm_id}

    def _wait_provisioning(self, vm_id: int, timeout: int = 300) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            summary = self._api("GET", f"/vm/{vm_id}/summary")
            state = summary.get("provisioning_state", "unknown")
            print(f"  provisioning_state={state}")
            if state == "ready":
                return summary
            if state in ("failed", "error"):
                raise RuntimeError(f"SHC VM {vm_id} provisioning failed: {summary}")
            time.sleep(10)
        raise RuntimeError(f"SHC VM {vm_id} not ready after {timeout}s")

    def ssh(self, ip: str, command: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
        user = self.get_ssh_user()
        wrapped = f"sudo HOME=/home/{user} bash -c {shlex.quote(command)}"
        cmd = [
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "-i", self._ssh_key_path(),
            f"{user}@{ip}", wrapped,
        ]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)

    def wait_ssh(self, ip: str, timeout: int = 180) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.ssh(ip, "echo SSH_OK", timeout=15)
            if r.returncode == 0 and "SSH_OK" in r.stdout:
                return True
            elapsed = int(time.time() - deadline + timeout)
            print(f"  Waiting for SSH... ({elapsed}s elapsed)")
            time.sleep(5)
        return False

    def create_snapshot(self, vm_id: str, name: str) -> bool:
        print("  Snapshot skipped (SHC limit: 1 per VM)")
        return False

    def delete_vm(self, vm_id: str) -> None:
        print(f"  VM deletion: use 'shc cancel {vm_id}' or let billing period expire")

    def get_ssh_user(self) -> str:
        return "debian"


def _step(step_num: int, total: int, name: str) -> None:
    print(f"\n[{step_num}/{total}] {name}")
    print("-" * 60)


def _auto_snapshot_name() -> str:
    base = SNAPSHOT_NAME
    if base.startswith("tollgate-runner-baked-v"):
        version_str = base[len("tollgate-runner-baked-v"):]
        try:
            version = int(version_str)
            return f"tollgate-runner-baked-v{version + 1}"
        except ValueError:
            pass
    return f"{base}-new"


def cmd_bake(args: argparse.Namespace) -> int:
    cloud = cast(str, args.cloud)
    machine_type = cast(str, args.machine_type)
    base_snapshot = cast(str, args.base_snapshot)
    snapshot_name = cast(str, args.snapshot_name) or _auto_snapshot_name()
    disk_size_gb = cast(int, args.disk_size)

    if cloud == "gcp":
        zone = cast(str, args.zone)
        project = get_project()
        provider: CloudProvider = GCPProvider(zone, project, base_snapshot)
    else:
        zone = ""
        project = ""
        provider = SHCProvider()

    total_steps = 12
    vm_name = f"tollgate-bake-{int(time.time())}"

    print(f"Bake configuration:")
    print(f"  Cloud:          {cloud}")
    print(f"  Base snapshot:  {base_snapshot}")
    print(f"  New snapshot:   {snapshot_name}")
    if cloud == "gcp":
        print(f"  Project:        {project}")
        print(f"  Zone:           {zone}")
    print(f"  Machine type:   {machine_type}")
    print(f"  Temp VM name:   {vm_name}")

    # Step 1: Create temp VM
    _step(1, total_steps, "Creating temporary VM")
    t0 = time.monotonic()
    try:
        vm_info = provider.create_vm(vm_name, machine_type, disk_size_gb)
    except Exception as e:
        print(f"ERROR: Failed to create VM: {e}", file=sys.stderr)
        return 1
    ip = vm_info["ip"]
    vm_id = vm_info["id"]
    print(f"  VM created in {time.monotonic() - t0:.1f}s (ip={ip})")

    try:
        # Step 2: Wait for SSH
        _step(2, total_steps, "Waiting for SSH access")
        t0 = time.monotonic()
        if not provider.wait_ssh(ip):
            print("ERROR: SSH not available after 180s", file=sys.stderr)
            return 1
        print(f"  SSH ready in {time.monotonic() - t0:.1f}s")

        # Step 3: Download base images (OpenWrt + Debian)
        _step(3, total_steps, "Downloading OpenWrt and Debian base images")
        t0 = time.monotonic()
        workdir = VIRT_LAB_WORKDIR
        owrt_img = f"openwrt-{_OPENWRT_VERSION}-x86-64-generic-ext4-combined.img"
        owrt_gz = f"{owrt_img}.gz"
        owrt_url = f"https://downloads.openwrt.org/releases/{_OPENWRT_VERSION}/targets/x86/64/{owrt_gz}"
        images_cmd = (
            f"mkdir -p {workdir}/images && cd {workdir}/images && "
            f"rm -f openwrt-base.qcow2 && "
            f"[ -f {owrt_gz} ] || curl -fL -o {owrt_gz} {owrt_url} && "
            f"[ -f {owrt_img} ] || (gzip -d < {owrt_gz} > {owrt_img} || [ -f {owrt_img} ]) && "
            f"qemu-img convert -f raw -O qcow2 {owrt_img} openwrt-base.qcow2 && "
            f"qemu-img resize openwrt-base.qcow2 2G && "
            f"if [ ! -f {_DEBIAN_IMAGE} ]; then "
            f"  curl -fL -o {_DEBIAN_IMAGE} {_DEBIAN_IMAGE_URL}; "
            f"fi && "
            "echo IMAGES_OK"
        )
        r = provider.ssh(ip, images_cmd, timeout=600)
        if r.returncode != 0 or "IMAGES_OK" not in (r.stdout or ""):
            print(f"ERROR: Image download failed: {r.stderr[:500]}", file=sys.stderr)
            return 1
        print(f"  Images ready in {time.monotonic() - t0:.1f}s")

        # Step 4: Install GitHub and Google Cloud CLIs
        _step(4, total_steps, "Installing GitHub and Google Cloud CLIs")
        t0 = time.monotonic()
        gh_install_cmd = (
            "apt-get update -qq && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq wget curl apt-transport-https ca-certificates gnupg >/dev/null && "
            "mkdir -p -m 755 /etc/apt/keyrings && "
            "if ! command -v gh >/dev/null 2>&1; then "
            "  wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg > /etc/apt/keyrings/githubcli-archive-keyring.gpg && "
            "  chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg && "
            '  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] '
            'https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list; '
            "fi && "
            "if ! command -v gcloud >/dev/null 2>&1; then "
            "  curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor --yes -o /usr/share/keyrings/cloud.google.gpg && "
            '  echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" '
            "> /etc/apt/sources.list.d/google-cloud-sdk.list; "
            "fi && "
            "apt-get update -qq && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gh google-cloud-cli socat >/dev/null && "
            "command -v gh >/dev/null && command -v gcloud >/dev/null && echo CLI_INSTALLED_OK"
        )
        r = provider.ssh(ip, gh_install_cmd, timeout=180)
        if r.returncode != 0 or "CLI_INSTALLED_OK" not in (r.stdout or ""):
            print(f"WARNING: CLI install may have failed: {r.stderr[:300]}", file=sys.stderr)
        print(f"  Done in {time.monotonic() - t0:.1f}s")

        # Step 5: Clone repo and create Python venv
        _step(5, total_steps, "Creating Python venv with test dependencies")
        t0 = time.monotonic()
        venv_cmd = (
            "apt-get update -qq && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv git >/dev/null 2>&1 || true; "
            f"if [ -d /opt/tollgate-venv ] && /opt/tollgate-venv/bin/python3 -c 'import pytest; print(\"VENV_OK\")' 2>/dev/null; then "
            "echo 'Python venv already present'; exit 0; fi; "
            f"rm -rf /opt/tollgate-test && "
            f"git clone --depth 50 {SUITE_REPO_URL} /opt/tollgate-test && "
            "rm -rf /opt/tollgate-venv && "
            "python3 -m venv /opt/tollgate-venv && "
            "/opt/tollgate-venv/bin/pip install -q -r /opt/tollgate-test/requirements.txt && "
            "/opt/tollgate-venv/bin/python3 -c 'import pytest; print(\"VENV_OK\")'"
        )
        r = provider.ssh(ip, venv_cmd, timeout=300)
        if r.returncode != 0 or "VENV_OK" not in (r.stdout or ""):
            print(f"WARNING: Python venv creation may have failed: {r.stderr[:300]}", file=sys.stderr)
        print(f"  Done in {time.monotonic() - t0:.1f}s")

        # Step 6: Create cashu venv
        _step(6, total_steps, "Creating cashu CLI venv")
        t0 = time.monotonic()
        cashu_cmd = (
            "if [ -x /opt/cashu-venv/bin/cashu ] && /opt/cashu-venv/bin/cashu --version >/dev/null 2>&1; then "
            "echo 'Cashu already present'; exit 0; fi; "
            "rm -rf /opt/cashu-venv && "
            "python3 -m venv /opt/cashu-venv && "
            "/opt/cashu-venv/bin/pip install -q --upgrade pip && "
            "/opt/cashu-venv/bin/pip install -q cashu 'marshmallow<4' && "
            "sed -i 's/    active: bool$/    active: bool = True/' "
            "$(/opt/cashu-venv/bin/python3 -c 'import cashu.core.models; print(cashu.core.models.__file__)') && "
            "test -x /opt/cashu-venv/bin/cashu && echo CASHU_OK"
        )
        r = provider.ssh(ip, cashu_cmd, timeout=300)
        if r.returncode != 0 or "CASHU_OK" not in (r.stdout or ""):
            print(f"WARNING: Cashu CLI install may have failed: {r.stderr[:300]}", file=sys.stderr)
        print(f"  Done in {time.monotonic() - t0:.1f}s")

        # Step 6b: Download CDK mintd and cdk-cli binaries
        _step(7, total_steps, "Downloading CDK mint binaries")
        t0 = time.monotonic()
        cdk_cmd = (
            "mkdir -p /opt/cdk-mintd && "
            "CDK_VER=0.16.0 && "
            "if [ -x /opt/cdk-mintd/cdk-mintd ]; then echo 'cdk-mintd cached'; else "
            "  wget -q -O /opt/cdk-mintd/cdk-mintd "
            "    https://github.com/cashubtc/cdk/releases/download/v${CDK_VER}/cdk-mintd-${CDK_VER}-x86_64 && "
            "  chmod +x /opt/cdk-mintd/cdk-mintd; fi && "
            "if [ -x /opt/cdk-mintd/cdk-cli ]; then echo 'cdk-cli cached'; else "
            "  wget -q -O /opt/cdk-mintd/cdk-cli "
            "    https://github.com/cashubtc/cdk/releases/download/v${CDK_VER}/cdk-cli-${CDK_VER}-x86_64 && "
            "  chmod +x /opt/cdk-mintd/cdk-cli && "
            "  ln -sf /opt/cdk-mintd/cdk-cli /usr/local/bin/cdk-cli; fi && "
            "/opt/cdk-mintd/cdk-mintd --version 2>&1 | head -1 && "
            "/opt/cdk-mintd/cdk-cli --version 2>&1 | head -1 && "
            "echo CDK_OK"
        )
        r = provider.ssh(ip, cdk_cmd, timeout=180)
        if r.returncode != 0 or "CDK_OK" not in (r.stdout or ""):
            print(f"WARNING: CDK install may have failed: {r.stderr[:300]}", file=sys.stderr)
        print(f"  Done in {time.monotonic() - t0:.1f}s")

        # Step 7: Setup bridge and prepare OpenWrt overlay
        _step(8, total_steps, "Setting up network bridge and OpenWrt overlay")
        t0 = time.monotonic()
        workdir = VIRT_LAB_WORKDIR
        bridge_cmd = (
            "sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1; "
            "ip link add name tg-poc-br type bridge 2>/dev/null || true; "
            "ip addr add 10.99.99.2/24 dev tg-poc-br 2>/dev/null || true; "
            "ip link set tg-poc-br up; "
            "ip tuntap add dev tg-poc-tap mode tap user root 2>/dev/null || true; "
            "ip link set tg-poc-tap master tg-poc-br 2>/dev/null || true; "
            "ip link set tg-poc-tap up; "
            "iptables -t nat -C POSTROUTING -s 10.99.99.0/24 ! -o tg-poc-br -j MASQUERADE 2>/dev/null || "
            "iptables -t nat -A POSTROUTING -s 10.99.99.0/24 ! -o tg-poc-br -j MASQUERADE; "
            f"mkdir -p {workdir}/run {workdir}/overlays {workdir}/images && "
            f"cd {workdir} && "
            "OWRT_BASE=images/openwrt-base.qcow2; "
            "[ -f \"$OWRT_BASE\" ] || OWRT_BASE=../images/openwrt-base.qcow2; "
            "OWRT_BASE=$(readlink -f \"$OWRT_BASE\"); "
            "rm -f overlays/tollgate-poc.qcow2 && "
            "qemu-img create -f qcow2 -F qcow2 -b \"$OWRT_BASE\" overlays/tollgate-poc.qcow2 >/dev/null && "
            "echo BRIDGE_OK"
        )
        r = provider.ssh(ip, bridge_cmd, timeout=60)
        if r.returncode != 0 or "BRIDGE_OK" not in (r.stdout or ""):
            print(f"ERROR: Bridge setup failed: {r.stderr[:300]}", file=sys.stderr)
            return 1
        print(f"  Done in {time.monotonic() - t0:.1f}s")

        # Step 8: Boot QEMU with OpenWrt overlay and provision via serial
        _step(9, total_steps, "Booting OpenWrt QEMU and provisioning via serial")
        t0 = time.monotonic()
        qemu_boot_cmd = (
            f"cd {workdir} && "
            "rm -f run/openwrt.serial.sock run/openwrt.monitor.sock run/openwrt.pid 2>/dev/null; "
            "nohup qemu-system-x86_64 "
            "-enable-kvm -m 128 -smp 1 -display none "
            "-serial unix:run/openwrt.serial.sock,server=on,wait=off "
            "-monitor unix:run/openwrt.monitor.sock,server=on,wait=off "
            "-drive file=overlays/tollgate-poc.qcow2,format=qcow2,if=virtio "
            "-netdev tap,id=net0,ifname=tg-poc-tap,script=no,downscript=no "
            "-device virtio-net-pci,netdev=net0,mac=52:54:00:12:34:56 "
            "-pidfile run/openwrt.pid "
            ">/tmp/openwrt-qemu.log 2>&1 &"
        )
        r = provider.ssh(ip, qemu_boot_cmd, timeout=30)
        time.sleep(3)

        # Now run the serial provisioning script inline
        password = shlex.quote(VIRT_LAB_PASSWORD)
        serial_provision_cmd = (
            f"cd {workdir} && "
            "/opt/tollgate-venv/bin/python3 - <<'PYEOF'\n"
            "import socket, time, sys, os\n"
            f"SOCK_PATH = os.path.expandvars('{workdir}') + '/run/openwrt.serial.sock'\n"
            f"PASSWORD = '{VIRT_LAB_PASSWORD}'\n"
            "OPENWRT_IP = '10.99.99.1'\n"
            "TIMEOUT = 90\n"
            "\n"
            "def recv_all(s, timeout=2):\n"
            "    s.settimeout(timeout)\n"
            "    chunks = []\n"
            "    try:\n"
            "        while True:\n"
            "            chunk = s.recv(4096)\n"
            "            if not chunk:\n"
            "                break\n"
            "            chunks.append(chunk)\n"
            "    except (TimeoutError, socket.timeout):\n"
            "        pass\n"
            "    return b''.join(chunks).decode('utf-8', errors='replace')\n"
            "\n"
            "def send_and_wait(s, cmd, wait=2):\n"
            "    s.sendall((cmd + '\\n').encode())\n"
            "    time.sleep(wait)\n"
            "    return recv_all(s, timeout=2)\n"
            "\n"
            "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "deadline = time.time() + 30\n"
            "connected = False\n"
            "while time.time() < deadline:\n"
            "    try:\n"
            "        s.connect(SOCK_PATH)\n"
            "        connected = True\n"
            "        break\n"
            "    except (ConnectionRefusedError, FileNotFoundError):\n"
            "        time.sleep(1)\n"
            "if not connected:\n"
            "    print('TIMEOUT: serial socket not ready')\n"
            "    sys.exit(1)\n"
            "\n"
            "print('Connected to serial, waiting for boot...')\n"
            "s.sendall(b'\\n')\n"
            "deadline = time.time() + TIMEOUT\n"
            "booted = False\n"
            "while time.time() < deadline:\n"
            "    s.sendall(b'\\n')\n"
            "    data = recv_all(s, timeout=2)\n"
            "    if 'Please press Enter' in data or 'root@OpenWrt' in data or ':/#' in data or 'OpenWrt' in data:\n"
            "        booted = True\n"
            "        break\n"
            "    time.sleep(1)\n"
            "if not booted:\n"
            "    print('TIMEOUT: OpenWrt did not boot')\n"
            "    s.close()\n"
            "    sys.exit(1)\n"
            "\n"
            "send_and_wait(s, '', wait=2)\n"
            "commands = [\n"
            f"    \"printf '%s\\\\\\n%s\\\\\\n' {password} {password} | passwd root\",\n"
            "    \"uci set dropbear.@dropbear[0].PasswordAuth='on'\",\n"
            "    'uci commit dropbear',\n"
            "    '/etc/init.d/dropbear restart',\n"
            "    'uci add firewall rule',\n"
            "    \"uci set firewall.@rule[-1].name='Allow-SSH-WAN'\",\n"
            "    \"uci set firewall.@rule[-1].src='wan'\",\n"
            "    \"uci set firewall.@rule[-1].dest_port='22'\",\n"
            "    \"uci set firewall.@rule[-1].proto='tcp'\",\n"
            "    \"uci set firewall.@rule[-1].target='ACCEPT'\",\n"
            "    'uci commit firewall',\n"
            "    'fw4 restart',\n"
            "    \"uci set network.lan.ipaddr='\" + OPENWRT_IP + \"'\",\n"
            "    \"uci set network.lan.netmask='255.255.255.0'\",\n"
            "    \"uci set network.lan.gateway='10.99.99.2'\",\n"
            "    \"uci set network.lan.dns='8.8.8.8'\",\n"
            "    'uci set network.mgmt=interface',\n"
            "    \"uci set network.mgmt.proto='static'\",\n"
            "    \"uci set network.mgmt.device='eth1'\",\n"
            "    \"uci set network.mgmt.ipaddr='10.99.97.1'\",\n"
            "    \"uci set network.mgmt.netmask='255.255.255.0'\",\n"
            "    'uci commit network',\n"
            "    '/etc/init.d/network restart',\n"
            "    'sync',\n"
            "]\n"
            "for cmd in commands:\n"
            "    send_and_wait(s, cmd, wait=2)\n"
            "\n"
            "s.close()\n"
            "print('SERIAL_PROVISION_OK')\n"
            "PYEOF\n"
        )
        r = provider.ssh(ip, serial_provision_cmd, timeout=180)
        if r.returncode != 0 or "SERIAL_PROVISION_OK" not in (r.stdout or ""):
            print(f"ERROR: Serial provisioning failed: {r.stderr[:500]}", file=sys.stderr)
            print(f"  stdout: {(r.stdout or '')[:500]}", file=sys.stderr)
            return 1

        # Wait for SSH to the OpenWrt VM
        print("  Waiting for OpenWrt SSH...")
        ssh_wait_cmd = (
            "for i in $(seq 1 30); do "
            f"sshpass -p {shlex.quote(VIRT_LAB_PASSWORD)} ssh "
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"-o ConnectTimeout=3 root@{OPENWRT_IP} 'echo SSH_OK' 2>/dev/null && break; "
            "sleep 2; "
            "done"
        )
        r = provider.ssh(ip, ssh_wait_cmd, timeout=120)
        if r.returncode != 0:
            print(f"WARNING: OpenWrt SSH wait returned {r.returncode}", file=sys.stderr)
        print(f"  Serial provisioning done in {time.monotonic() - t0:.1f}s")

        # Step 8b: Install WiFi packages for hwsim-based virtual WiFi testing
        _step(10, total_steps, "Installing WiFi packages for hwsim testing")
        wifi_pkgs = "kmod-mac80211-hwsim wpad-basic iw-full iwinfo socat"
        wifi_install_cmd = (
            f"sshpass -p {shlex.quote(VIRT_LAB_PASSWORD)} ssh "
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"-o ConnectTimeout=3 root@{OPENWRT_IP} "
            f"'opkg update >/dev/null 2>&1 && opkg install {wifi_pkgs} 2>&1 && echo WIFI_PKGS_OK || echo WIFI_PKGS_SKIP' "
            "2>/dev/null || true"
        )
        r = provider.ssh(ip, wifi_install_cmd, timeout=120)
        if "WIFI_PKGS_OK" in (r.stdout or ""):
            print("  WiFi packages installed: kmod-mac80211-hwsim wpad-basic iw-full iwinfo")
        else:
            print(f"  WARNING: WiFi package install skipped (non-fatal): {(r.stdout or '')[:200]}", file=sys.stderr)

        shutdown_cmd = (
            f"sshpass -p {shlex.quote(VIRT_LAB_PASSWORD)} ssh "
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"-o ConnectTimeout=3 root@{OPENWRT_IP} 'sync; poweroff' 2>/dev/null || true; "
            "sleep 8"
        )
        provider.ssh(ip, shutdown_cmd, timeout=30)

        # Step 8c: Build and install vwifi binaries for cross-VM WiFi relay
        _step(10, total_steps, "Building vwifi binaries for cross-VM WiFi relay")
        t0_vwifi = time.monotonic()
        vwifi_build_cmd = (
            "apt-get update -qq && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
            "cmake make g++ pkg-config libnl-3-dev libnl-genl-3-dev git >/dev/null 2>&1 || true && "
            "rm -rf /tmp/vwifi-build && "
            "git clone --depth 1 https://github.com/Raizo62/vwifi.git /tmp/vwifi-build && "
            "mkdir -p /opt/vwifi/bin/host /opt/vwifi/bin/debian /opt/vwifi/bin/openwrt && "
            "cd /tmp/vwifi-build && "
            "mkdir -p build-host && cd build-host && "
            "cmake .. -DCMAKE_BUILD_TYPE=Release && make -j$(nproc) && "
            "cp vwifi-server vwifi-ctrl /opt/vwifi/bin/host/ && "
            "cd /tmp/vwifi-build && "
            "mkdir -p build-guest && cd build-guest && "
            "cmake .. -DCMAKE_BUILD_TYPE=Release -DCMAKE_EXE_LINKER_FLAGS='-static' && "
            "make -j$(nproc) && "
            "cp vwifi-client vwifi-add-interfaces /opt/vwifi/bin/debian/ && "
            "cp vwifi-client vwifi-add-interfaces /opt/vwifi/bin/openwrt/ && "
            "ls -la /opt/vwifi/bin/host/ /opt/vwifi/bin/debian/ /opt/vwifi/bin/openwrt/ && "
            "modprobe vhost_vsock 2>/dev/null || true && "
            "echo VWIFI_BUILD_OK"
        )
        r = provider.ssh(ip, vwifi_build_cmd, timeout=600)
        if "VWIFI_BUILD_OK" in (r.stdout or ""):
            print("  vwifi binaries built and installed to /opt/vwifi/bin/")
            print("  Done in {:.1f}s".format(time.monotonic() - t0_vwifi))
        else:
            print(f"  WARNING: vwifi build failed (non-fatal): {(r.stdout or '')[:200]}", file=sys.stderr)
            print(f"  stderr: {(r.stderr or '')[:300]}", file=sys.stderr)

        # Step 8d: Pre-bake Playwright into Debian QEMU overlay
        _step(10, total_steps, "Pre-baking Playwright + Chromium into Debian overlay")
        t0_deb = time.monotonic()
        debian_pw_cmd = (
            f"cd {workdir} && "
            "if [ -f overlays/debian-client.qcow2 ]; then "
            "echo 'Reusing cached Debian overlay'; "
            "else "
            "DEB_BASE=images/debian-12-nocloud-amd64.qcow2; "
            "[ -f \"$DEB_BASE\" ] || DEB_BASE=../images/debian-12-nocloud-amd64.qcow2; "
            "DEB_BASE=$(readlink -f \"$DEB_BASE\"); "
            "qemu-img create -f qcow2 -F qcow2 -b \"$DEB_BASE\" overlays/debian-client.qcow2 >/dev/null && "
            "qemu-img resize --shrink overlays/debian-client.qcow2 10G >/dev/null 2>&1 || true; "
            "fi && "
            "ip tuntap add dev tg-poc-tap2 mode tap user root 2>/dev/null || true && "
            "ip link set tg-poc-tap2 master tg-poc-br 2>/dev/null || true && "
            "ip link set tg-poc-tap2 up 2>/dev/null || true && "
            "ip link add name mgmt-br type bridge 2>/dev/null || true; "
            "ip addr add 10.99.97.2/24 dev mgmt-br 2>/dev/null || true; "
            "ip link set mgmt-br up 2>/dev/null || true; "
            "ip tuntap add dev mgmt-tap2 mode tap user root 2>/dev/null || true; "
            "ip link set mgmt-tap2 master mgmt-br 2>/dev/null || true; "
            "ip link set mgmt-tap2 up 2>/dev/null || true; "
            "nohup qemu-system-x86_64 "
            "-enable-kvm -m 1536 -smp 2 -display none "
            f"-drive file=overlays/debian-client.qcow2,format=qcow2,if=virtio "
            "-netdev tap,id=net0,ifname=tg-poc-tap2,script=no,downscript=no "
            f"-device virtio-net-pci,netdev=net0,mac=de:54:4e:91:49:da "
            "-netdev tap,id=mgmt0,ifname=mgmt-tap2,script=no,downscript=no "
            f"-device virtio-net-pci,netdev=mgmt0,mac=52:54:00:c0:02:64 "
            ">/tmp/debian-qemu.log 2>&1 &"
        )
        r = provider.ssh(ip, debian_pw_cmd, timeout=60)

        print("  Waiting for Debian VM SSH...")
        deb_ssh_wait = (
            f"for i in $(seq 1 30); do "
            f"sshpass -p {shlex.quote(VIRT_LAB_PASSWORD)} ssh "
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"-o ConnectTimeout=3 root@10.99.99.100 'echo DEB_SSH_OK' 2>/dev/null && break; "
            "sleep 2; done"
        )
        r = provider.ssh(ip, deb_ssh_wait, timeout=120)

        if "DEB_SSH_OK" in (r.stdout or ""):
            print("  Debian VM SSH ready, installing Playwright...")
            pw_install = (
                f"sshpass -p {shlex.quote(VIRT_LAB_PASSWORD)} ssh "
                "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                "root@10.99.99.100 "
                "'apt-get -o Acquire::ForceIPv4=true update -qq && "
                "DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::ForceIPv4=true install -y -qq --no-install-recommends "
                "python3-pip libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 libcairo2 libcups2 libdbus-1-3 "
                "libdrm2 libgbm1 libglib2.0-0 libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 "
                "libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 xvfb fonts-liberation fonts-freefont-ttf >/dev/null && "
                "python3 -m pip install -q --break-system-packages playwright && "
                "python3 -m playwright install chromium >/dev/null && "
                "python3 -c \"import playwright; print('PLAYWRIGHT_OK')\"' 2>&1"
            )
            r = provider.ssh(ip, pw_install, timeout=600)
            if "PLAYWRIGHT_OK" in (r.stdout or ""):
                print("  Playwright + Chromium installed in Debian overlay")
            else:
                print(f"  WARNING: Playwright install may have failed: {(r.stdout or '')[:300]}", file=sys.stderr)

            print("  Shutting down Debian VM...")
            deb_shutdown = (
                f"sshpass -p {shlex.quote(VIRT_LAB_PASSWORD)} ssh "
                "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                "root@10.99.99.100 'sync; poweroff' 2>/dev/null || true; "
                "sleep 5; killall -9 qemu-system-x86_64 2>/dev/null; sleep 2; "
                f"cd {workdir} && "
                "echo 'Flattening Debian overlay onto base...'; "
                "DEB_BASE=images/debian-12-nocloud-amd64.qcow2; "
                "[ -f \"$DEB_BASE\" ] || DEB_BASE=../images/debian-12-nocloud-amd64.qcow2; "
                "DEB_BASE=$(readlink -f \"$DEB_BASE\"); "
                "qemu-img convert -f qcow2 -O qcow2 overlays/debian-client.qcow2 /tmp/debian-base-flat.qcow2 && "
                "mv /tmp/debian-base-flat.qcow2 \"$DEB_BASE\" && "
                "rm -f overlays/debian-client.qcow2 && "
                "echo DEBIAN_FLATTEN_OK"
            )
            r = provider.ssh(ip, deb_shutdown, timeout=120)
            if "DEBIAN_FLATTEN_OK" in (r.stdout or ""):
                print(f"  Debian base image updated with Playwright ({time.monotonic() - t0_deb:.1f}s)")
            else:
                print(f"  WARNING: Debian flatten may have failed: {(r.stdout or '')[:300]}", file=sys.stderr)
        else:
            print(f"  WARNING: Debian VM SSH not ready, skipping Playwright pre-bake ({time.monotonic() - t0_deb:.1f}s)", file=sys.stderr)

        # Step 11: Install GitHub Actions runner binary (GCP only)
        if cloud == "gcp":
            _step(11, total_steps, "Installing GitHub Actions runner binary")
            t0_runner = time.monotonic()
            runner_version = "2.334.0"
            runner_install_cmd = (
                "id runner >/dev/null 2>&1 || useradd -m -s /bin/bash runner && "
                f"RUNNER_VERSION={runner_version} && "
                'RUNNER_DIR=/home/runner/actions-runner && '
                f'[ -f "$RUNNER_DIR/run.sh" ] && grep -q "{runner_version}" "$RUNNER_DIR/run.sh" && echo "Runner v{runner_version} already present" && echo RUNNER_INSTALL_OK && exit 0; '
                'echo "Removing old runner..." && rm -rf "$RUNNER_DIR" && '
                'rm -rf "$RUNNER_DIR" && mkdir -p "$RUNNER_DIR" && '
                'cd "$RUNNER_DIR" && '
                "curl -fL -o actions-runner-linux-x64.tar.gz "
                '"https://github.com/actions/runner/releases/download/v$RUNNER_VERSION/actions-runner-linux-x64-$RUNNER_VERSION.tar.gz" && '
                "tar xzf actions-runner-linux-x64.tar.gz && "
                "rm -f actions-runner-linux-x64.tar.gz && "
                "chown -R runner:runner /home/runner/actions-runner && "
                'ls "$RUNNER_DIR/run.sh" && '
                "echo RUNNER_INSTALL_OK"
            )
            r = provider.ssh(ip, runner_install_cmd, timeout=300)
            if "RUNNER_INSTALL_OK" in (r.stdout or ""):
                print(f"  GitHub Actions runner v{runner_version} installed to /home/runner/actions-runner/")
                print("  Done in {:.1f}s".format(time.monotonic() - t0_runner))
            else:
                print(f"  WARNING: Runner install failed (non-fatal): {(r.stdout or '')[:200]}", file=sys.stderr)
                print(f"  stderr: {(r.stderr or '')[:300]}", file=sys.stderr)
        else:
            _step(11, total_steps, "Installing GitHub Actions runner binary")
            print("  Skipped (GCP-specific)")

        # Step 12: Stop QEMU and copy overlay as new base
        _step(12, total_steps, "Stopping QEMU and replacing base image with provisioned overlay")
        t0 = time.monotonic()
        replace_cmd = (
            "killall -TERM qemu-system-x86_64 2>/dev/null || true; sleep 5; "
            "killall -9 qemu-system-x86_64 2>/dev/null || true; sleep 2; "
            f"cd {workdir} && "
            "OWRT_BASE=$(readlink -f images/openwrt-base.qcow2 2>/dev/null || echo images/openwrt-base.qcow2); "
            "echo \"Replacing base at $OWRT_BASE\"; "
            "ls -la \"$OWRT_BASE\"; "
            "qemu-img convert -f qcow2 -O qcow2 overlays/tollgate-poc.qcow2 /tmp/openwrt-base-flat.qcow2 && "
            "mv /tmp/openwrt-base-flat.qcow2 \"$OWRT_BASE\" && "
            "ls -la \"$OWRT_BASE\" && "
            "rm -f overlays/tollgate-poc.qcow2 overlays/tollgate-seller.qcow2 && "
            "echo BASE_REPLACED_OK"
        )
        r = provider.ssh(ip, replace_cmd, timeout=120)
        if r.returncode != 0 or "BASE_REPLACED_OK" not in (r.stdout or ""):
            print(f"ERROR: Base image replacement failed: {r.stderr[:300]}", file=sys.stderr)
            return 1
        print(f"  Base image replaced in {time.monotonic() - t0:.1f}s")

        # Snapshot
        _step(12, total_steps, "Creating snapshot")
        t0 = time.monotonic()
        provider.create_snapshot(vm_id, snapshot_name)
        print(f"  Snapshot step done in {time.monotonic() - t0:.1f}s")

    finally:
        print("\nCleaning up temporary VM...")
        provider.delete_vm(vm_id)

    print(f"\n{'=' * 60}")
    print(f"Bake complete!")
    print(f"  Cloud:          {cloud}")
    print(f"  New snapshot:   {snapshot_name}")
    if cloud == "gcp":
        print(f"  To use: update SNAPSHOT_NAME in lib/cloud_lab/constants.py")
        print(f"  Verify: ./scripts/cloud-lab.py up --vm-name test-bake-vm")
    else:
        print(f"  VM {vm_id} is ready for use (no snapshot on SHC)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    bake = sub.add_parser("bake", help="Bake a new snapshot from the current base snapshot")
    bake.add_argument("--cloud", default="gcp", choices=["gcp", "shc"], help="Cloud provider")
    bake.add_argument("--snapshot-name", default="", help=f"Name for the new snapshot (default: auto-increment from {SNAPSHOT_NAME})")
    bake.add_argument("--base-snapshot", default=SNAPSHOT_NAME, help=f"Base snapshot to create VM from (default: {SNAPSHOT_NAME})")
    bake.add_argument("--zone", default=DEFAULT_ZONE)
    bake.add_argument("--machine-type", default=DEFAULT_MACHINE_TYPE)
    bake.add_argument("--disk-size", type=int, default=DEFAULT_DISK_SIZE_GB)
    bake.set_defaults(func=cmd_bake)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    from typing import Callable
    func = cast(Callable[[argparse.Namespace], int], args.func)
    return func(args)


if __name__ == "__main__":
    raise SystemExit(main())
