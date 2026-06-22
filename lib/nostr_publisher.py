#!/usr/bin/env python3
"""
Nostr Publisher -- NIP-94 (kind 1063) + kind 30078 summary events via nak CLI.

Publishes Nostr events for BlossomFS compatibility and test-run indexing.

Event kinds:
  - 1063: NIP-94 File Metadata. BlossomFS uses these to populate the /nip94/
          directory. Each event advertises a single file (URL, sha256, MIME,
          architecture, version). BlossomFS serves files at:
              /nip94/<pubkey>/<filename>
          and metadata at:
              /metadata/<pubkey>/<filename>
  - 30078: Application-specific data (parameterized replaceable). Used here as
           a per-run "index" event: the d-tag is the run_id, and the tags list
           all file URLs for that run. Reader pages fetch kind 30078 events to
           discover all artifacts belonging to a test run.

Uses the nak CLI (https://github.com/fiatjaf/nak) for signing + publishing.
The nsec is read from a file (mode 0600) and passed via NOSTR_SECRET_KEY env
var -- never visible in the process list.

Project-agnostic -- no imports from other lib modules. Designed to be lifted
into hackathon-tooling or any CI pipeline.
"""

import json
import os
import re
import subprocess
import sys
import time
from typing import Any

# --- Constants ---

from lib.constants import NOSTR_RELAYS as DEFAULT_RELAYS

KIND_NIP94_FILE_METADATA = 1063   # NIP-94: file header for BlossomFS
KIND_APP_DATA = 30078             # Parameterized replaceable: run index


# --- Internal helpers ---


def _nostr_now() -> int:
    """Current Unix timestamp (Nostr convention)."""
    return int(time.time())


def _nak_available() -> bool:
    """Check if nak CLI is installed and on PATH."""
    result = subprocess.run(["which", "nak"], capture_output=True, text=True)
    return result.returncode == 0


def _parse_nak_publish_output(stderr: str) -> dict[str, Any]:
    relay_results: dict[str, dict[str, Any]] = {}
    pattern = re.compile(r"^publishing to (.+?)\.\.\. (success\.|failed:)\s*(.*)$")
    for line in stderr.splitlines():
        line = line.strip()
        m = pattern.match(line)
        if not m:
            continue
        relay, status_raw, message = m.group(1), m.group(2), m.group(3).strip()
        accepted = status_raw.startswith("success")
        relay_results[relay] = {
            "accepted": accepted,
            "message": message if message else ("" if accepted else "unknown"),
        }
    any_accepted = any(r["accepted"] for r in relay_results.values())
    all_rejected_reasons = [
        f"{relay}: {r['message']}" for relay, r in relay_results.items() if not r["accepted"]
    ]
    return {
        "relay_results": relay_results,
        "any_accepted": any_accepted,
        "all_rejected_reasons": all_rejected_reasons,
    }


def _publish_event(
    nsec_file: str,
    kind: int,
    content: str,
    tags: list,
    relays: list = None,
) -> dict:
    """Sign and publish a Nostr event via nak CLI.

    nak exits 0 even when relays reject events (e.g. whitelist blocks).
    The relay status lines (``publishing to <relay>... success|failed``)
    are printed to stderr — stdout contains only the signed event JSON.
    stderr is parsed to detect silent rejections.
    """
    if relays is None:
        relays = DEFAULT_RELAYS

    if not _nak_available():
        return {
            "success": False,
            "error": "nak CLI not found. Install: https://github.com/fiatjaf/nak",
        }

    with open(nsec_file) as f:
        nsec_hex = f.read().strip()

    cmd = [
        "nak", "event",
        "-k", str(kind),
        "-c", content,
    ]

    for tag in tags:
        tag_key = tag[0]
        tag_vals = ";".join(str(t) for t in tag[1:])
        cmd.extend(["-t", f"{tag_key}={tag_vals}"])

    cmd.extend(relays)

    env = os.environ.copy()
    env["NOSTR_SECRET_KEY"] = nsec_hex

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)

    if result.returncode != 0:
        return {
            "success": False,
            "error": f"nak event failed: {result.stderr.strip()[:300]}",
        }

    nak_status = _parse_nak_publish_output(result.stderr)

    event: dict = {}
    event_id = ""
    try:
        event = json.loads(result.stdout.strip().split("\n")[-1])
        event_id = event.get("id", "")
    except (json.JSONDecodeError, IndexError):
        pass

    if not nak_status["any_accepted"]:
        reasons = "; ".join(nak_status["all_rejected_reasons"]) or "all relays rejected (no detail)"
        return {
            "success": False,
            "error": f"Event signed but rejected by all relays: {reasons}",
            "event_id": event_id,
            "event": event,
            "relay_status": nak_status,
        }

    return {
        "success": True,
        "event_id": event_id,
        "event": event,
        "relay_status": nak_status,
    }


# --- Public API ---


def publish_nip94_event(
    nsec_file: str,
    filename: str,
    blossom_url: str,
    sha256: str,
    mime_type: str,
    metadata_tags: dict = None,
    relays: list = None,
) -> dict:
    """Publish a NIP-94 file metadata event (kind 1063) for BlossomFS.

    This makes the file discoverable by BlossomFS under:
        /nip94/<pubkey>/<filename>
        /metadata/<pubkey>/<filename>

    Required NIP-94 tags:
        - url:   Blossom blob URL
        - x:     SHA-256 (also "ox" for the "original" sha256)
        - m:     MIME type
        - filename: display name

    Optional metadata_tags keys (added if present):
        - architecture (goes to "A" tag)
        - version      (goes to "v" tag)
        - package_name (goes to "n" tag)
        - compression  (goes to "compression" tag)
        - format       (goes to "format" tag)
        - size         (goes to "size" tag, in bytes)

    Args:
        nsec_file: Path to file containing hex Nostr private key.
        filename: Display name / basename of the file.
        blossom_url: Full Blossom blob URL (from get_blob_url or upload result).
        sha256: SHA-256 hex of the file content.
        mime_type: MIME type (e.g. "image/png", "text/html").
        metadata_tags: Optional dict with architecture, version, etc.
        relays: Relay list override.

    Returns:
        Result dict from _publish_event.
    """
    if metadata_tags is None:
        metadata_tags = {}

    tags = [
        ["url", blossom_url],
        ["x", sha256],
        ["ox", sha256],
        ["m", mime_type],
        ["filename", filename],
    ]

    # Optional BlossomFS / package metadata tags
    if "architecture" in metadata_tags:
        tags.append(["A", metadata_tags["architecture"]])
    if "version" in metadata_tags:
        tags.append(["v", metadata_tags["version"]])
    if "package_name" in metadata_tags:
        tags.append(["n", metadata_tags["package_name"]])
    if "compression" in metadata_tags:
        tags.append(["compression", metadata_tags["compression"]])
    if "format" in metadata_tags:
        tags.append(["format", metadata_tags["format"]])
    if "size" in metadata_tags:
        tags.append(["size", str(metadata_tags["size"])])
    if "summary" in metadata_tags:
        tags.append(["summary", metadata_tags["summary"]])

    # Content: human-readable file description
    content = json.dumps({
        "filename": filename,
        "url": blossom_url,
        "sha256": sha256,
        "mime_type": mime_type,
    })

    return _publish_event(nsec_file, KIND_NIP94_FILE_METADATA, content, tags, relays)


def publish_test_run_event(
    nsec_file: str,
    run_id: str,
    timestamp: int = None,
    file_urls: list = None,
    summary: str = "",
    relays: list = None,
    extra_tags: list = None,
) -> dict:
    """Publish a kind 30078 parameterized replaceable test-run index event.

    Deprecated: kind 30078 will be replaced by kind 6900 (DVM) in future.

    This is the "index" event that reader pages fetch to discover all
    artifacts belonging to a test run. The d-tag is set to run_id, making it
    replaceable (publishing again with the same run_id replaces the old event).

    Each file URL is added as a separate "file" tag so consumers can enumerate
    them. The summary goes into the event content.

    Args:
        nsec_file: Path to file containing hex Nostr private key.
        run_id: Unique run identifier (becomes the d-tag).
        timestamp: Unix timestamp (default: now).
        file_urls: List of file URLs published in this run.
        summary: Human-readable run summary (goes into content).
        relays: Relay list override.

    Returns:
        Result dict from _publish_event.
    """
    if timestamp is None:
        timestamp = _nostr_now()
    if file_urls is None:
        file_urls = []

    tags = [
        ["d", run_id],
        ["t", "test-run"],
        ["timestamp", str(timestamp)],
    ]

    if extra_tags:
        tags.extend(extra_tags)

    # Each file URL as a separate tag so consumers can enumerate them
    for url in file_urls:
        tags.append(["file", url])

    content = summary if summary else f"Test run {run_id} at {timestamp}"

    return _publish_event(nsec_file, KIND_APP_DATA, content, tags, relays)


# --- CLI entry point ---

if __name__ == "__main__":
    nsec_file = os.environ.get("NSEC_FILE", "")
    if not nsec_file:
        print("Set NSEC_FILE env var pointing to your hex private key file")
        sys.exit(1)

    # Quick test: publish a minimal kind 30078 event
    result = publish_test_run_event(
        nsec_file=nsec_file,
        run_id="test-run-001",
        summary="Nostr publisher smoke test",
        file_urls=[],
    )
    print(json.dumps(result, indent=2))
