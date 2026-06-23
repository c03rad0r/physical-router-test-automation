"""Cloud lab worker — suite checkout and outer deps."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import time
from pathlib import Path

from lib.cloud_lab.constants import (
    CDK_MINT_URL,
    CLOUD_ARCH,
    DEBIAN_IP,
    DEBIAN_MAC,
    MGMT_BETA_IP,
    NUTSHELL_V1_MINT_URL,
    NUTSHELL_V2_MINT_URL,
    OPENWRT_IP,
    SELLER_OPENWRT_IP,
    SUITE_REPO_URL,
    TEST_DIR,
    VIRT_LAB_PASSWORD,
)
from lib.cloud_lab.worker.config import WorkerConfig
from lib.cloud_lab.worker.inner_ssh import inner_ssh
from lib.cloud_lab.worker.mints import ensure_cdk_binary
from lib.cloud_lab.worker.shell import _run, log

def ensure_suite_checkout(config: WorkerConfig) -> None:
    test_dir = Path(TEST_DIR)
    if test_dir.exists() and (test_dir / ".git").exists():
        log.info("Suite checkout: re-fetching %s at %s", SUITE_REPO_URL, config.suite_ref[:7])
        _run(
            f"cd {TEST_DIR} && git fetch --depth 1 origin {shlex.quote(config.suite_ref)}",
            timeout=120, check=False,
        )
        _run(f"cd {TEST_DIR} && git checkout {shlex.quote(config.suite_ref)}", timeout=60)
    else:
        log.info("Suite checkout: cloning %s at %s", SUITE_REPO_URL, config.suite_ref[:7])
        _run(f"rm -rf {TEST_DIR} && git clone --depth 50 {SUITE_REPO_URL} {TEST_DIR}", timeout=180)
        # Fetch the specific commit directly — it may be on a feature branch
        # not included in the default-branch shallow clone.
        _run(
            f"cd {TEST_DIR} && git fetch --depth 1 origin {shlex.quote(config.suite_ref)}",
            timeout=60, check=False,
        )
        _run(f"cd {TEST_DIR} && git checkout {shlex.quote(config.suite_ref)}", timeout=60)
def write_env_file(config: WorkerConfig) -> None:
    reseller_scenarios = "1" if config.reseller_scenarios else ""
    backend = config.backend
    secondary_host = config.secondary_router_host
    if config.reseller_scenarios and not secondary_host:
        secondary_host = SELLER_OPENWRT_IP
    if config.two_router and not secondary_host:
        secondary_host = MGMT_BETA_IP
    env_content = (
        f"TOLLGATE_LUCI_PASSWORD={VIRT_LAB_PASSWORD}\n"
        f"TOLLGATE_SSH_PASSWORD={VIRT_LAB_PASSWORD}\n"
        f"TOLLGATE_SSH_HOST={OPENWRT_IP}\n"
        f"TOLLGATE_LUCI_URL=http://{OPENWRT_IP}\n"
        f"TOLLGATE_ROUTER_ARCH={CLOUD_ARCH}\n"
        f"TOLLGATE_CLIENT_TYPE=container\n"
        f"TOLLGATE_VIRTUAL_LAB=1\n"
        f"TOLLGATE_LAB_TYPE=gcloud\n"
        f"TOLLGATE_VIRTUAL_GATEWAY={OPENWRT_IP}\n"
        f"TOLLGATE_NDS_PORTAL_PORT=2050\n"
        f"TOLLGATE_TEST_MINT_URL={CDK_MINT_URL}\n"
        f"TOLLGATE_CDK_MINT_URL={CDK_MINT_URL}\n"
        f"TOLLGATE_NUTSHELL_V2_MINT_URL={NUTSHELL_V2_MINT_URL}\n"
        f"TOLLGATE_NUTSHELL_V1_MINT_URL={NUTSHELL_V1_MINT_URL}\n"
        f"TOLLGATE_V2_MINT_URL={CDK_MINT_URL}\n"
        f"TOLLGATE_CLIENT_IP={DEBIAN_IP}\n"
        f"TOLLGATE_CLIENT_MAC={DEBIAN_MAC}\n"
        f"TOLLGATE_CONTAINER_HOST={DEBIAN_IP}\n"
        f"TOLLGATE_ROUTER_ID=gcp-cloud\n"
        f"TOLLGATE_ROUTER_MODEL=gcp-n2-standard-2\n"
        f"TOLLGATE_BACKEND={backend}\n"
        f"TOLLGATE_VIEWPORT=desktop\n"
        f"TOLLGATE_DISABLE_ARTIFACT_RERUN=1\n"
        f"TOLLGATE_CASHU_VENV=/opt/cashu-venv\n"
        f"TOLLGATE_RECORD_ALL=1\n"
        f"TOLLGATE_ENABLE_RESELLER_SCENARIOS={reseller_scenarios}\n"
        f"TOLLGATE_SECONDARY_ROUTER_HOST={secondary_host}\n"
        f"TOLLGATE_SECONDARY_ROUTER_PORT={config.secondary_router_port}\n"
        f"TOLLGATE_SECONDARY_ROUTER_PASSWORD={VIRT_LAB_PASSWORD}\n"
        f"TOLLGATE_PORTAL={config.portal}\n"
        f"TOLLGATE_ENABLE_HWSIM={'1' if config.hwsim_enabled else ''}\n"
        f"TOLLGATE_ENABLE_VWIFI={'1' if config.vwifi_enabled else ''}\n"
        f"TOLLGATE_WIFI_PLANE={config.wifi_plane}\n"
    )
    gh_token = os.environ.get("GH_TOKEN", "")
    if gh_token:
        env_content += f"GH_TOKEN={gh_token}\n"
    Path(TEST_DIR, ".env").write_text(env_content)
def ensure_outer_deps() -> None:
    r = _run(
        "test -d /opt/tollgate-venv && /opt/tollgate-venv/bin/python3 -c "
        "'import pytest, pytest_html, pytest_timeout; print(\"VENV_OK\")' 2>/dev/null",
        timeout=15,
        check=False,
    )
    if "VENV_OK" not in r.stdout:
        log.info("Setting up Python venv on outer VM...")
        _run(
            f"apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv git >/dev/null && "
            f"rm -rf /opt/tollgate-venv && python3 -m venv /opt/tollgate-venv && "
            f"/opt/tollgate-venv/bin/pip install -q -r {TEST_DIR}/requirements.txt",
            timeout=180,
        )

    r = _run("test -x /opt/cashu-venv/bin/cashu && echo CASHU_OK", timeout=10, check=False)
    if "CASHU_OK" not in r.stdout:
        log.info("Setting up cashu CLI venv...")
        r = _run(
            "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv >/dev/null && "
            "rm -rf /opt/cashu-venv && python3 -m venv /opt/cashu-venv && "
            "/opt/cashu-venv/bin/pip install -q --upgrade pip && "
            "/opt/cashu-venv/bin/pip install -q cashu 'marshmallow<4' && "
            "/opt/cashu-venv/bin/python - <<'PY'\n"
            "from pathlib import Path\n"
            "import cashu.core.models\n"
            "models=Path(cashu.core.models.__file__)\n"
            "text=models.read_text()\n"
            "text=text.replace('    active: bool\\n','    active: bool = True\\n')\n"
            "models.write_text(text)\n"
            "PY\n"
            "test -x /opt/cashu-venv/bin/cashu && echo CASHU_OK",
            timeout=240,
            check=False,
        )
        if "CASHU_OK" not in (r.stdout or ""):
            log.warning("Cashu CLI install failed (non-fatal, some tests will skip)")

    ensure_cdk_binary()

    ensure_blossomfs()
def ensure_blossomfs() -> None:
    try:
        mountpoint = "/mnt/blossomfs"
        r = _run(f"mountpoint -q {mountpoint} && echo MOUNTED", timeout=10, check=False)
        if "MOUNTED" in r.stdout:
            log.info("BlossomFS already mounted at %s", mountpoint)
            os.environ["BLOSSOMFS_MOUNT"] = mountpoint
            return

        _run(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
            "fuse3 libfuse3-dev pkg-config build-essential git curl >/dev/null",
            timeout=120, check=False,
        )

        r = _run('command -v cargo >/dev/null 2>&1 && echo RUST_OK', timeout=10, check=False)
        if "RUST_OK" not in r.stdout:
            log.info("Installing Rust toolchain for BlossomFS...")
            _run(
                'curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y',
                timeout=120,
            )

        clone_dir = "/opt/blossomfs"
        repo_url = "https://github.com/Amperstrand/blossomfs"
        r = _run(f"test -d {clone_dir}/.git && echo CLONE_OK", timeout=10, check=False)
        if "CLONE_OK" not in r.stdout:
            log.info("Cloning BlossomFS...")
            _run(f"rm -rf {clone_dir} && git clone --depth 1 {shlex.quote(repo_url)} {clone_dir}", timeout=120)

        r = _run(f"test -x {clone_dir}/target/release/blossomfs && echo BUILD_OK", timeout=10, check=False)
        if "BUILD_OK" not in r.stdout:
            log.info("Building BlossomFS (cargo build --release)...")
            _run(
                f'. "$HOME/.cargo/env" && cd {clone_dir} && cargo build --release 2>&1',
                timeout=300,
            )

        nsec_file = os.environ.get("NSEC_FILE", "")
        if not nsec_file or not Path(nsec_file).exists():
            for candidate in [os.path.expanduser("~/nsec"), "/root/nsec"]:
                if Path(candidate).exists():
                    nsec_file = candidate
                    break
        if not nsec_file or not Path(nsec_file).exists():
            nsec_hex = os.environ.get("BOT_NSEC_HEX", "").strip()
            if nsec_hex:
                nsec_file = os.path.expanduser("~/nsec")
                Path(nsec_file).write_text(nsec_hex)

        if not nsec_file or not Path(nsec_file).exists():
            log.warning("BlossomFS skipped: no nsec file available")
            return

        if not shutil.which("nak"):
            log.warning("BlossomFS skipped: nak CLI not available for npub derivation")
            return

        r = _run(
            f"NOSTR_SECRET_KEY=$(cat {shlex.quote(nsec_file)}) nak key public 2>/dev/null",
            timeout=15, check=False,
        )
        npub = r.stdout.strip()
        if not npub.startswith("npub1"):
            log.warning("BlossomFS skipped: could not derive npub from nsec")
            return

        server_url = "https://blossom.psbt.me"
        _run(f"mkdir -p {mountpoint}", timeout=10, check=False)
        _run(
            f'. "$HOME/.cargo/env" && {clone_dir}/target/release/blossomfs mount '
            f"--mountpoint {mountpoint} "
            f"--npub {shlex.quote(npub)} "
            f"--server {shlex.quote(server_url)} "
            f"--nsec-file {shlex.quote(nsec_file)} "
            f"--read-only false "
            f"--cache-dir /tmp/blossomfs-cache "
            f"--daemon",
            timeout=30,
        )
        log.info("BlossomFS mounted at %s (RW, npub=%s...)", mountpoint, npub[:16])
        os.environ["BLOSSOMFS_MOUNT"] = mountpoint
    except Exception as exc:
        log.warning("BlossomFS setup failed (non-fatal): %s", str(exc)[:200])
def wait_for_dpkg_lock(timeout: int = 300) -> None:
    """Wait for unattended-upgrades to release the dpkg lock at boot."""
    for attempt in range(timeout // 5):
        r = _run("fuser /var/lib/dpkg/lock-frontend 2>/dev/null", timeout=5, check=False)
        if r.returncode != 0:
            log.info("dpkg lock is free")
            return
        if attempt == 0:
            log.info("Waiting for unattended-upgrades to release dpkg lock...")
        time.sleep(5)
    log.warning("dpkg lock still held after %ds, proceeding anyway", timeout)
def ensure_github_cli(token: str) -> None:
    os.environ["GH_TOKEN"] = token
    _run("git config --global --add safe.directory '*'", timeout=10, check=False)

    r = _run("command -v gh >/dev/null 2>&1 && echo GH_BIN_OK", timeout=10, check=False)
    if "GH_BIN_OK" not in r.stdout:
        wait_for_dpkg_lock()
        log.info("Installing GitHub CLI...")
        _run(
            "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq wget >/dev/null && "
            "mkdir -p -m 755 /etc/apt/keyrings && "
            "wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg > /etc/apt/keyrings/githubcli-archive-keyring.gpg && "
            "chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg && "
            'echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] '
            'https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list && '
            "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gh >/dev/null",
            timeout=180,
        )

    _run("gh auth setup-git", timeout=15, check=False)
    _run("git config --global user.email 'test@localhost'", timeout=10, check=False)
    _run("git config --global user.name 'CI'", timeout=10, check=False)

    r = _run("GH_TOKEN=$GH_TOKEN gh auth status >/dev/null 2>&1 && echo GH_OK", timeout=15, check=False)
    if "GH_OK" in r.stdout:
        return
    last_err = ""
    for attempt in range(1, 4):
        r = _run(
            f"env -u GH_TOKEN -u GITHUB_TOKEN printf '%s\\n' {shlex.quote(token)} | env -u GH_TOKEN -u GITHUB_TOKEN gh auth login --with-token 2>&1",
            timeout=30,
            check=False,
        )
        if r.returncode == 0:
            break
        last_err = (r.stderr or r.stdout or "").strip()[-500:]
        log.warning("gh auth attempt %d failed: %s", attempt, last_err[:200])
        if attempt < 3:
            time.sleep(5 * attempt)
    else:
        raise RuntimeError(f"gh auth failed after 3 attempts: {last_err}")
def ensure_debian_client_deps() -> bool:
    r = inner_ssh(DEBIAN_IP, 'python3 -c "import playwright; print(\\"PLAYWRIGHT_OK\\")" 2>/dev/null')
    if "PLAYWRIGHT_OK" in r.stdout:
        log.info("Debian Playwright cache hit")
        return True
    log.info("Installing Debian Playwright deps (one-time)...")
    install = (
        "apt-get -o Acquire::ForceIPv4=true update -qq && "
        "DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::ForceIPv4=true install -y -qq --no-install-recommends "
        "python3-pip libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 libcairo2 libcups2 libdbus-1-3 "
        "libdrm2 libgbm1 libglib2.0-0 libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 "
        "libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 xvfb ffmpeg fonts-liberation fonts-freefont-ttf >/dev/null && "
        "python3 -m pip install -q --break-system-packages playwright && "
        "python3 -m playwright install chromium >/dev/null && "
        'python3 -c "import playwright; print(\\"PLAYWRIGHT_OK\\")"'
    )
    r = inner_ssh(DEBIAN_IP, install, timeout=600)
    return "PLAYWRIGHT_OK" in r.stdout
