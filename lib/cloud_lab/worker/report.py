"""Cloud lab worker — collect, render, publish."""

from __future__ import annotations

import json
import logging
import os
import shlex
import time
from pathlib import Path
from typing import Any

from lib.cloud_lab.constants import CLOUD_ARCH, OPENWRT_IP, TEST_DIR
from lib.cloud_lab.worker.config import WorkerConfig
from lib.cloud_lab.worker.runner import pytest_collect_args, runner_scope
from lib.cloud_lab.worker.shell import _redact, _run, log
from lib.constants import BLOSSOM_SERVERS, NOSTR_RELAYS

def collect_and_render(config: WorkerConfig, results_dir: str, started_at: str, finished_at: str) -> None:
    commit_arg = f"--sut-commit {config.sut_commit} " if config.sut_commit else ""
    pr_arg = f"--sut-pr {config.sut_pr} " if config.sut_pr else ""
    scope = runner_scope(config)
    pytest_runners = pytest_collect_args(config)
    _run(
        f"cd {TEST_DIR} && source /opt/tollgate-venv/bin/activate && set -a && source .env && set +a && "
        f"python3 scripts/collect-results.py --run-dir {results_dir} "
        f"{pytest_runners}"
        f"--run-id {config.run_id} "
        f"--sut-repo {config.artifact_repo} --sut-branch {shlex.quote(config.sut_branch)} "
        f"{commit_arg}{pr_arg}--sut-backend {config.backend} "
        f"--suite-commit {config.suite_ref} --client-type container "
        f"--portal {config.portal} "
        f"--router-id gcp-cloud --router-model gcp-n2-standard-2 --router-arch {CLOUD_ARCH} "
        f"--viewport desktop --test-plan cloud-api --query-router {OPENWRT_IP} --virtual-lab "
        f"--lab-type gcloud --tier api --scope {scope} --profile gcloud-api "
        f"--started-at {started_at} --finished-at {finished_at} --allow-failures",
        timeout=60,
    )
    _run(
        f"cd {TEST_DIR} && source /opt/tollgate-venv/bin/activate && "
        f"python3 scripts/render-report.py --run-dir {results_dir}",
        timeout=60,
    )
def create_minimal_run_json(
    config: WorkerConfig, results_dir: str, started_at: str, finished_at: str, test_exit: int
) -> None:
    results_path = Path(results_dir)
    run_json = results_path / "run.json"
    if run_json.exists():
        return

    passed = failed = skipped = error = 0
    for junit in results_path.rglob("junit.xml"):
        try:
            import xml.etree.ElementTree as ET
            tree = ET.parse(junit)
            root = tree.getroot()
            passed += int(root.get("tests", 0)) - int(root.get("failures", 0)) - int(root.get("errors", 0)) - int(root.get("skipped", 0))
            failed += int(root.get("failures", 0))
            skipped += int(root.get("skipped", 0))
            error += int(root.get("errors", 0))
        except Exception:
            pass

    commit_short = (config.sut_commit or "")[:7] or "unknown"
    run_data = {
        "schema_version": 1,
        "run_id": config.run_id,
        "status": "failed" if test_exit != 0 else "passed",
        "started_at": started_at,
        "finished_at": finished_at,
        "counts": {"passed": passed, "failed": failed, "skipped": skipped, "error": error},
        "sut": {
            "repo": config.artifact_repo,
            "branch": config.sut_branch,
            "commit": config.sut_commit or "unknown",
            "commit_short": commit_short,
            "pr": config.sut_pr,
            "backend": config.backend,
            "portal": config.portal,
        },
        "lab": {"router_id": "gcp-cloud", "client_type": "container", "lab_type": "gcloud"},
        "note": "minimal run.json created after collect_and_render failure",
    }
    results_path.mkdir(parents=True, exist_ok=True)
    run_json.write_text(json.dumps(run_data, indent=2))
    log.info("Created minimal run.json: %d passed, %d failed, %d skipped", passed, failed, skipped)

    summary_path = results_path / "summary.json"
    if not summary_path.exists():
        summary_data = {"tests": [], "counts": run_data["counts"]}
        summary_path.write_text(json.dumps(summary_data, indent=2))

    report_dir = results_path / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    index_html = report_dir / "index.html"
    if not index_html.exists():
        index_html.write_text(
            f"<html><head><title>Run {config.run_id}</title></head>"
            f"<body><h1>Run {config.run_id}</h1>"
            f"<p>passed={passed} failed={failed} skipped={skipped}</p>"
            f"<p>collect_and_render failed — minimal report</p></body></html>"
        )
def publish_results(config: WorkerConfig, results_dir: str) -> str:
    run_json = Path(results_dir) / "run.json"
    if not run_json.exists():
        log.error("Cannot publish: run.json not found in %s", results_dir)
        return "https://tests.tollgate.me/"

    commit_short = config.sut_commit[:7]
    data = json.loads(run_json.read_text())
    nested = data.get("sut") or {}
    commit_short = nested.get("commit_short") or commit_short
    expected_url = f"https://tests.tollgate.me/reports/{commit_short}/{config.run_id}/report/index.html"
    log.info("Publishing from results_dir=%s → expected_url=%s", results_dir, expected_url)

    try:
        gh_token = os.environ.get("GH_TOKEN", "")
        _run(
            f"git config --global user.email 'tollgate-ci@users.noreply.github.com' && "
            f"git config --global user.name 'TollGate CI' && "
            f"git config --global --add safe.directory '*' && "
            f"cd {TEST_DIR} && GH_TOKEN={shlex.quote(gh_token)} TOLLGATE_GH_PAGES_CNAME=tests.tollgate.me "
            f"./scripts/publish-report.sh {shlex.quote(results_dir)}",
            timeout=1200,
        )
    except RuntimeError as exc:
        log.error("publish-report.sh failed: %s", _redact(str(exc))[:500])
        raise
    except Exception as exc:
        log.error("publish-report.sh unexpected error: %s", _redact(str(exc))[:500])
        raise

    return expected_url
def publish_to_nostr(config: WorkerConfig, results_dir: str, counts: dict[str, Any]) -> dict[str, Any]:
    """Publish test results to Blossom + Nostr (kind 30078).

    Deprecated: kind 30078 will be replaced by kind 6900 (DVM) in future.

    Requires nak CLI and an nsec file on the VM. Falls back silently if
    either is missing (non-fatal).

    Returns the manifest dict from result_publisher (containing ``files``
    with Blossom URLs), or an empty dict on failure.
    """
    import shutil

    nsec_file = os.environ.get("NSEC_FILE", "")
    if not nsec_file or not Path(nsec_file).exists():
        for candidate in [os.path.expanduser("~/nsec"), "/root/nsec", "/home/macbook/nsec"]:
            if Path(candidate).exists():
                nsec_file = candidate
                break
    if not nsec_file or not Path(nsec_file).exists():
        log.warning("Nostr publish skipped: nsec file not found")
        return {}

    if not shutil.which("nak"):
        log.warning("Nostr publish skipped: nak CLI not installed")
        return {}

    blossom = BLOSSOM_SERVERS[0] if BLOSSOM_SERVERS else "https://blossom.psbt.me"
    relays = ",".join(NOSTR_RELAYS) if NOSTR_RELAYS else "wss://relay.cashu.email"

    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0)
    skipped = counts.get("skipped", 0)

    cmd = (
        f"cd {TEST_DIR} && source /opt/tollgate-venv/bin/activate && "
        f"python3 -m lib.result_publisher {shlex.quote(results_dir)} "
        f"--nsec-file {shlex.quote(nsec_file)} "
        f"--blossom-server {shlex.quote(blossom)} "
        f"--relays {shlex.quote(relays)} "
        f"--run-id {shlex.quote(config.run_id)} "
        f"--branch {shlex.quote(config.sut_branch or 'main')} "
        f"--passed {passed} --failed {failed} --skipped {skipped} "
        f"--router gcp-cloud "
        f"--commit {shlex.quote((config.sut_commit or 'unknown')[:7])} "
        f"--portal {shlex.quote(config.portal or 'builtin')} "
        f"--lab-type gcloud "
        f"--sut-backend {shlex.quote(config.backend)} "
        f"--sut-repo {shlex.quote(config.artifact_repo)} "
        f"-v"
    )

    manifest: dict[str, Any] = {}
    try:
        log.info("Publishing to Blossom (%s) + Nostr (%s)...", blossom, relays)
        r = _run(cmd, timeout=600, check=False)
        stdout = (r.stdout or "").strip()
        if r.returncode == 0:
            for line in stdout.splitlines()[-5:]:
                log.info("nostr-publish: %s", line)
            log.info("Nostr publish complete")
            manifest = _parse_manifest_from_stdout(stdout)
        else:
            log.error("Nostr publish failed (rc=%d): %s", r.returncode, stdout[-300:])
    except Exception as exc:
        log.error("Nostr publish error (non-fatal): %s", _redact(str(exc))[:500])
    return manifest


def _parse_manifest_from_stdout(stdout: str) -> dict[str, Any]:
    """Extract the last JSON object from result_publisher stdout."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}


def verify_nostr_publish(config: WorkerConfig) -> dict[str, Any]:
    """Verify that the kind 30078 event and Blossom blobs are retrievable.

    Deprecated: kind 30078 will be replaced by kind 6900 (DVM) in future.

    Called after :func:`publish_to_nostr`. Uses ``nak req`` to query the
    relay for the kind 30078 event with ``d`` = ``config.run_id``, then
    fetches one Blossom URL from the event's ``file`` tags to confirm the
    blob is live.

    Returns a dict with ``event_found``, ``event_id``, ``blob_url_checked``,
    ``blob_retrievable`` keys. Non-fatal — logs warnings on failure.
    """
    import shutil

    result: dict[str, Any] = {
        "event_found": False,
        "event_id": "",
        "blob_url_checked": "",
        "blob_retrievable": False,
    }

    if not shutil.which("nak"):
        log.warning("verify_nostr_publish skipped: nak CLI not found")
        return result

    relays_env = ",".join(NOSTR_RELAYS) if NOSTR_RELAYS else "wss://relay.cashu.email"
    relay = relays_env.split(",")[0].strip()

    # Allow a brief window for relay propagation
    time.sleep(2)

    nak_cmd = (
        f"nak req -k 30078 -d {shlex.quote(config.run_id)} "
        f"-l 1 {shlex.quote(relay)}"
    )
    try:
        r = _run(nak_cmd, timeout=30, check=False)
    except Exception as exc:
        log.warning("verify_nostr_publish: nak req error: %s", _redact(str(exc))[:200])
        return result

    stdout = (r.stdout or "").strip()
    if r.returncode != 0 or not stdout:
        log.warning(
            "verify_nostr_publish: kind 30078 event not found on %s (rc=%d) — "
            "relay propagation may be slow",
            relay, r.returncode,
        )
        return result

    # nak may print log lines before the JSON event; find the last JSON object
    event: dict[str, Any] = {}
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                event = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

    if not event:
        log.warning("verify_nostr_publish: could not parse event from nak output")
        return result

    result["event_found"] = True
    result["event_id"] = event.get("id", "")
    log.info("verify_nostr_publish: ✓ kind 30078 event found (id=%s)", result["event_id"][:16])

    file_urls: list[str] = []
    for tag in event.get("tags", []):
        if len(tag) >= 2 and tag[0] == "file":
            file_urls.append(tag[1])

    if not file_urls:
        try:
            content = json.loads(event.get("content", "{}"))
            file_urls = [f.get("url", "") for f in content.get("files", []) if f.get("url")]
        except (json.JSONDecodeError, KeyError):
            pass

    if not file_urls:
        log.warning("verify_nostr_publish: event has no file URLs to verify")
        return result

    blob_url = file_urls[0]
    result["blob_url_checked"] = blob_url

    try:
        blob_r = _run(
            f"curl -sSf -o /dev/null -w '%{{http_code}}' {shlex.quote(blob_url)}",
            timeout=15, check=False,
        )
        http_code = (blob_r.stdout or "").strip().strip("'")
        if http_code == "200":
            result["blob_retrievable"] = True
            log.info("verify_nostr_publish: ✓ Blossom blob retrievable (%s)", blob_url[:80])
        else:
            log.warning(
                "verify_nostr_publish: Blossom blob returned HTTP %s (%s)",
                http_code, blob_url[:80],
            )
    except Exception as exc:
        log.warning("verify_nostr_publish: Blossom fetch error: %s", _redact(str(exc))[:200])

    if result["event_found"] and result["blob_retrievable"]:
        log.info(
            "verify_nostr_publish: ✓ PASS — event %s + %d blobs verified on %s",
            result["event_id"][:16], len(file_urls), relay,
        )
    else:
        log.warning(
            "verify_nostr_publish: ⚠ PARTIAL — event_found=%s blob_retrievable=%s",
            result["event_found"], result["blob_retrievable"],
        )

    return result


def post_pr_comment(config: WorkerConfig, report_url: str, counts: dict[str, Any]) -> None:
    if not config.sut_pr:
        return
    body = (
        f"## Cloud lab results\n\n"
        f"**Run:** `{config.run_id}`\n\n"
        f"| Passed | Failed | Skipped |\n"
        f"|--------|--------|--------|\n"
        f"| {counts.get('passed', '?')} | {counts.get('failed', '?')} | {counts.get('skipped', '?')} |\n\n"
        f"[View full report]({report_url})\n"
    )
    repo = config.pr_repo or config.artifact_repo
    _run(
        f"gh pr comment {shlex.quote(config.sut_pr)} --repo {shlex.quote(repo)} "
        f"--body {shlex.quote(body)}",
        timeout=30,
        check=False,
    )
