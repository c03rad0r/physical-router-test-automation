"""Cloud lab worker — TollGate deploy."""

from __future__ import annotations

import logging
import shlex
import time

from lib.cloud_lab.constants import CLOUD_ARCH, OPENWRT_IP, TEST_DIR
from lib.cloud_lab.worker.config import WorkerConfig
from lib.cloud_lab.worker.shell import _run, log

def deploy_tollgate(config: WorkerConfig) -> None:
    repo_arg = repr(config.artifact_repo)
    branch_arg = repr(config.sut_branch)
    run_id_arg = repr(config.artifact_run_id)
    backend_arg = repr(config.backend)
    py = f"""
import logging
import os
import sys

from lib.backend import BackendConfig
from lib.deploy import deploy_branch
from lib.router import Router

os.environ["TOLLGATE_DISABLE_ARTIFACT_RERUN"] = "1"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")

backend = BackendConfig({backend_arg})
hosts = [{OPENWRT_IP!r}]
secondary = {config.secondary_router_host!r}
if secondary:
    hosts.append(secondary)

ok = True
for host in hosts:
    router = Router(host=host, phone_ip="", phone_mac="", domain="", backend=backend)
    result = deploy_branch(
        router,
        {branch_arg},
        arch={CLOUD_ARCH!r},
        force=True,
        reboot=False,
        repo={repo_arg},
        backend=backend,
        run_id={run_id_arg},
        commit={repr(config.sut_commit)} if config.sut_commit else None,
    )
    print(
        f"host={{host}} version={{result['installed_version']}} "
        f"health={{result['health_code']}} success={{result['success']}}"
    )
    ok = ok and bool(result["success"])

sys.exit(0 if ok else 1)
"""
    _run(
        f"cd {TEST_DIR} && source /opt/tollgate-venv/bin/activate && set -a && source .env && set +a && "
        f"python3 -c {shlex.quote(py)}",
        timeout=300,
    )
    log.info("Deploy complete for %d host(s)", 1 + bool(config.secondary_router_host))
def deploy_portal_overlay(config: WorkerConfig) -> None:
    """Download and install an alternative portal .ipk on the OpenWrt VM."""
    from lib.portal import PortalConfig

    portal = PortalConfig(config.portal)
    if not portal.needs_separate_deploy:
        return

    repo_arg = repr(portal.repo)
    workflow_arg = repr(portal.workflow)
    portal_type_arg = repr(portal.type)
    py = f"""
import logging
import os
import sys

from lib.portal import PortalConfig
from lib.deploy import deploy_portal
from lib.router import Router
from lib.backend import BackendConfig

os.environ["TOLLGATE_DISABLE_ARTIFACT_RERUN"] = "1"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")

backend = BackendConfig({repr(config.backend)})
router = Router(host={OPENWRT_IP!r}, phone_ip='', phone_mac='', domain='', backend=backend)
portal = PortalConfig({portal_type_arg})

result = deploy_portal(router, portal, arch={CLOUD_ARCH!r})
print(f"portal={{portal.type}} success={{result.get('success')}} skipped={{result.get('skipped', False)}}")
sys.exit(0 if result.get('success') or result.get('skipped') else 1)
"""
    _run(
        f"cd {TEST_DIR} && source /opt/tollgate-venv/bin/activate && set -a && source .env && set +a && "
        f"python3 -c {shlex.quote(py)}",
        timeout=300,
    )
    log.info("Portal overlay deploy complete: %s", config.portal)
def wait_for_backend() -> None:
    from lib.cloud_lab.constants import VIRT_LAB_PASSWORD
    ssh_prefix = (
        f"sshpass -p {shlex.quote(VIRT_LAB_PASSWORD)} "
        f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-o ControlPath=none root@{OPENWRT_IP} "
    )
    for attempt in range(30):
        r = _run(
            f"{ssh_prefix}"
            f"\"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:2121/ 2>/dev/null || echo 000\"",
            timeout=10, check=False,
        )
        code = r.stdout.strip().split("'")[-2] if "'" in r.stdout else r.stdout.strip()
        if "200" in code:
            log.info("TollGate backend healthy (attempt %d, http=%s)", attempt + 1, code)
            return
        if attempt % 5 == 0:
            log.info("Waiting for backend... attempt %d, http=%s", attempt + 1, code)
        time.sleep(2)
    raise RuntimeError("TollGate backend did not become healthy after 60s")
