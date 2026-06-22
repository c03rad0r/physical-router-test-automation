"""Result publishing orchestrator for physical-router-test-automation.

Deprecated: kind 30078 will be replaced by kind 6900 (DVM) in future.

Ties together three peer modules into one flow:

    scan results dir  ->  upload each file to Blossom  ->
    publish NIP-94 (kind 1063) per file  ->  publish kind 30078 summary  ->  manifest

Peer modules (siblings in ``lib/``, project-agnostic):

``blossom_publisher`` (BUD-02 upload via nak CLI)
    ``upload_to_blossom(file_path, nsec_file, server_url, content_type, cashu_token) -> dict``
        Returns ``{"url", "sha256", "size", "paid"}``.
    ``compute_sha256(file_path) -> str``
    ``get_blob_url(server_url, sha256) -> str``

``secret_scanner`` (3-layer secret detection + redaction)
    ``scan_directory(dir_path, skip_dirs=None) -> dict``
        Returns ``{"scanned": int, "blocked": [paths], "clean": [paths],
        "redacted": [{"filename", "count", "findings"}], "errors": [...]}``.
    ``scan_file(file_path) -> (sanitized_content_str | None, findings_list)``
        Content is ``None`` when the file is blocked. Otherwise a (possibly
        redacted) text string.
    ``is_blocked_file(file_path) -> bool``

``nostr_publisher`` (NIP-94 kind 1063 + kind 30078 via nak CLI)
    ``publish_nip94_event(nsec_file, filename, blossom_url, sha256, mime_type,
    metadata_tags=None, relays=None) -> dict``
        Returns ``{"success", "event_id", "event"}`` or ``{"success": False,
        "error"}``.
    ``publish_test_run_event(nsec_file, run_id, timestamp=None, file_urls=None,
    summary="", relays=None) -> dict``
        Emits a kind 30078 parameterized-replaceable event (``d`` = run_id).
        ``file_urls`` is a list of Blossom URLs; ``summary`` goes into content.

Usage
-----
    python -m lib.result_publisher results/20260620T120000Z-abc1234 \\
        --nsec-file ~/.config/prta/nsec \\
        --blossom-server https://blossom.psbt.me \\
        --relays wss://relay.damus.io,wss://nos.lol
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# Peer modules (siblings). Relative imports require running as a module:
#   python -m lib.result_publisher
from .blossom_publisher import compute_sha256, get_blob_url, upload_to_blossom
from .constants import BLOSSOM_SERVERS, NOSTR_RELAYS
from .nostr_publisher import publish_nip94_event, publish_test_run_event
from .secret_scanner import is_blocked_file, scan_directory, scan_file


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default per-file size cap (matches Blossom free tier, 1 MB).
DEFAULT_MAX_FILE_SIZE = 10_000_000

#: Default Blossom server if neither CLI nor env provides one.
DEFAULT_BLOSSOM_SERVER = BLOSSOM_SERVERS[0] if BLOSSOM_SERVERS else "https://blossom.psbt.me"

#: Default relays if neither CLI nor env provides them.
DEFAULT_RELAYS = list(NOSTR_RELAYS)

#: Defense-in-depth: files never uploaded even if the scanner marks them clean.
#: Adds coverage (sqlite/db/log) the scanner's own suffix list lacks.
HARD_BLOCKED_NAMES = {".env", "credentials", "routers.env", "routers.json", "report.html"}


def _is_artifact_for_skipped_test(file_path: Path) -> bool:
    name = file_path.name
    return "-skipped." in name
HARD_BLOCKED_SUFFIXES = {
    ".env", ".pem", ".key", ".p12", ".pfx", ".keystore",
    ".kdbx", ".sqlite", ".db",
}

logger = logging.getLogger("result_publisher")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _guess_mime_type(file_path: str | Path) -> str:
    """Return a mime type for ``file_path``, defaulting to octet-stream."""
    mime, _ = mimetypes.guess_type(str(file_path))
    return mime or "application/octet-stream"


def _is_hard_blocked(file_path: Path) -> bool:
    """Defense-in-depth block for files the scanner might miss."""
    name = file_path.name.lower()
    if name in HARD_BLOCKED_NAMES:
        return True
    return file_path.suffix.lower() in HARD_BLOCKED_SUFFIXES


def _generate_run_id() -> str:
    """Generate a sortable run id like ``20260620T120000Z``."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _is_probably_binary(file_path: Path) -> bool:
    """Heuristic: image/audio/video/zip/pdf archives cannot be text-redacted."""
    mime = _guess_mime_type(file_path)
    return any(
        mime.startswith(prefix)
        for prefix in ("image/", "audio/", "video/", "application/zip",
                        "application/gzip", "application/pdf", "application/x-")
    )


# ---------------------------------------------------------------------------
# Core upload + publish
# ---------------------------------------------------------------------------

def _upload_one(
    file_path: Path,
    nsec_file: str,
    blossom_server: str,
    mime_type: str,
    max_file_size: int,
) -> dict:
    """Upload one file to Blossom. Returns a per-file manifest entry.

    Raises ``ValueError`` if the file exceeds ``max_file_size``.
    """
    size = file_path.stat().st_size
    if size > max_file_size:
        raise ValueError(
            f"{file_path} is {size} bytes, exceeds max {max_file_size} bytes"
        )

    result = upload_to_blossom(
        str(file_path),
        nsec_file,
        server_url=blossom_server,
        content_type=mime_type,
    )
    url = result.get("url") or get_blob_url(blossom_server, result["sha256"])
    return {
        "original_path": str(file_path),
        "relative_path": None,
        "blossom_url": url,
        "sha256": result["sha256"],
        "size": result.get("size", size),
        "mime_type": mime_type,
        "redacted": False,
    }


def _publish_file_event(
    entry: dict,
    nsec_file: str,
    relays: list[str],
    metadata: dict,
) -> str | None:
    """Publish a NIP-94 (kind 1063) file-header event. Returns event id."""
    filename = Path(entry["original_path"]).name
    try:
        result = publish_nip94_event(
            nsec_file=nsec_file,
            filename=filename,
            blossom_url=entry["blossom_url"],
            sha256=entry["sha256"],
            mime_type=entry["mime_type"],
            metadata_tags={
                "size": entry["size"],
                "summary": metadata.get("run_id", ""),
            },
            relays=relays,
        )
        if result.get("success"):
            event_id = result.get("event_id", "")
            logger.info("NIP-94 published for %s -> %s", filename, event_id)
            return event_id or None
        logger.error(
            "NIP-94 publish failed for %s: %s",
            filename, result.get("error", "unknown"),
        )
        return None
    except Exception:
        logger.exception(
            "Failed to publish NIP-94 event for %s", entry["original_path"]
        )
        return None


def publish_single_file(
    file_path: str | Path,
    nsec_file: str,
    blossom_server: str = DEFAULT_BLOSSOM_SERVER,
    relays: list[str] | None = None,
    metadata: dict | None = None,
    max_file_size: int = DEFAULT_MAX_FILE_SIZE,
) -> dict:
    """Scan, upload, and publish a single file.

    Returns ``{"url", "sha256", "size", "mime_type", "event_id"}``.
    """
    relays = relays or list(DEFAULT_RELAYS)
    metadata = metadata or {}
    p = Path(file_path)
    if not p.is_file():
        raise FileNotFoundError(p)

    if is_blocked_file(str(p)):
        raise ValueError(f"{p} is blocked by the secret scanner")

    mime = _guess_mime_type(p)
    entry = _upload_one(p, nsec_file, blossom_server, mime, max_file_size)
    entry["relative_path"] = p.name
    event_id = _publish_file_event(entry, nsec_file, relays, metadata)
    return {
        "url": entry["blossom_url"],
        "sha256": entry["sha256"],
        "size": entry["size"],
        "mime_type": entry["mime_type"],
        "event_id": event_id,
    }


def publish_results(
    results_dir: str | Path,
    nsec_file: str,
    run_id: str | None = None,
    blossom_server: str = DEFAULT_BLOSSOM_SERVER,
    relays: list[str] | None = None,
    metadata: dict | None = None,
    max_file_size: int = DEFAULT_MAX_FILE_SIZE,
    dry_run: bool = False,
    publish_nip94: bool = False,
) -> dict:
    """Publish every file under ``results_dir`` to Blossom + Nostr.

    Flow::

        1. scan_directory(results_dir)
        2. for each clean file    -> upload -> NIP-94 (kind 1063)
        3. for each redacted file -> scan_file -> write sanitized temp -> upload
        4. publish kind 30078 summary (file URLs + JSON summary in content)
        5. return manifest

    Parameters
    ----------
    results_dir
        Directory to scan and publish.
    nsec_file
        Path to the nsec hex file (Blossom auth + Nostr signing).
    run_id
        Run identifier. Generated if omitted.
    blossom_server
        Blossom server base URL.
    relays
        Nostr relays to publish to.
    metadata
        Extra run metadata (branch, pr, commit, router, passed, failed, ...).
        Merged into the kind 30078 summary content.
    max_file_size
        Skip files larger than this many bytes. Defaults to 1 MB.
    dry_run
        If True, scan and report only; do not upload or publish.

    Returns
    -------
    dict
        Manifest with ``run_id``, ``timestamp``, ``files``, ``summary_event_id``,
        ``blossom_server``, and ``scan_summary``.
    """
    relays = relays or list(DEFAULT_RELAYS)
    metadata = metadata or {}
    run_id = run_id or _generate_run_id()
    results_dir = Path(results_dir)
    if not results_dir.is_dir():
        raise FileNotFoundError(f"results dir not found: {results_dir}")

    timestamp_str = datetime.now(timezone.utc).isoformat(timespec="seconds")
    timestamp_unix = int(time.time())
    logger.info("Publishing run %s from %s", run_id, results_dir)
    logger.info("Blossom server: %s", blossom_server)
    logger.info("Relays: %s", ", ".join(relays))

    # ------------------------------------------------------------------
    # 1. Scan
    # ------------------------------------------------------------------
    scan_result = scan_directory(str(results_dir))
    clean_paths: list[str] = list(scan_result.get("clean", []))
    redacted_entries: list[dict] = list(scan_result.get("redacted", []))
    blocked_paths: list[str] = list(scan_result.get("blocked", []))
    scan_errors: list[dict] = list(scan_result.get("errors", []))

    # Defense-in-depth: move hard-blocked files out of clean/redacted buckets.
    extra_blocked = []
    clean_paths = [
        p for p in clean_paths
        if not _hard_filter(Path(p), extra_blocked)
    ]
    redacted_entries = [
        r for r in redacted_entries
        if not _hard_filter(Path(r["filename"]), extra_blocked)
    ]
    blocked_paths = list({*blocked_paths, *extra_blocked})

    n_blocked = len(blocked_paths)
    n_redacted = len(redacted_entries)
    n_clean = len(clean_paths)
    logger.info(
        "Scan complete: %d blocked, %d redacted, %d clean (of %d scanned)",
        n_blocked, n_redacted, n_clean, scan_result.get("scanned", 0),
    )
    for bp in blocked_paths:
        logger.warning("Blocked (skipped): %s", bp)
    for err in scan_errors:
        logger.warning("Scan error: %s: %s", err.get("filename"), err.get("error"))

    scan_summary = {
        "blocked": n_blocked,
        "redacted": n_redacted,
        "clean": n_clean,
        "scanned": scan_result.get("scanned", 0),
    }

    if dry_run:
        logger.info("Dry run: no uploads or events will be published")
        return {
            "run_id": run_id,
            "timestamp": timestamp_str,
            "files": [],
            "summary_event_id": None,
            "blossom_server": blossom_server,
            "scan_summary": scan_summary,
            "dry_run": True,
        }

    # ------------------------------------------------------------------
    # 2. Upload clean files
    # ------------------------------------------------------------------
    uploaded: list[dict] = []
    for fpath_str in clean_paths:
        fpath = Path(fpath_str)
        if not fpath.is_file():
            logger.warning("Clean-list file missing on disk: %s", fpath)
            continue
        mime = _guess_mime_type(fpath)
        try:
            entry = _upload_one(fpath, nsec_file, blossom_server, mime, max_file_size)
        except ValueError as exc:
            logger.warning("Skipping (size): %s", exc)
            continue
        except Exception:
            logger.exception("Upload failed for %s", fpath)
            continue
        entry["relative_path"] = str(fpath.relative_to(results_dir))
        uploaded.append(entry)
        logger.info(
            "Uploaded clean %s -> %s (%d bytes)",
            entry["relative_path"], entry["blossom_url"], entry["size"],
        )

    # ------------------------------------------------------------------
    # 3. Upload redacted files (sanitized text via temp file)
    # ------------------------------------------------------------------
    redacted_uploaded: list[dict] = []
    for r_info in redacted_entries:
        fpath = Path(r_info["filename"])
        if not fpath.is_file():
            continue
        # Binary files cannot be text-redacted; upload as-is if scanner
        # flagged warnings only, otherwise skip.
        if _is_probably_binary(fpath):
            logger.info(
                "Redacted file is binary, uploading as-is: %s", fpath
            )
            mime = _guess_mime_type(fpath)
            try:
                entry = _upload_one(fpath, nsec_file, blossom_server, mime, max_file_size)
            except Exception:
                logger.exception("Upload failed for redacted binary %s", fpath)
                continue
            entry["relative_path"] = str(fpath.relative_to(results_dir))
            entry["redacted"] = False
            redacted_uploaded.append(entry)
            continue

        sanitized, findings = scan_file(str(fpath))
        if sanitized is None:
            logger.warning("Redacted file became blocked on rescan: %s", fpath)
            continue

        suffix = fpath.suffix or ".txt"
        with tempfile.NamedTemporaryFile(
            prefix="prta_redacted_", suffix=suffix, mode="w",
            encoding="utf-8", delete=False,
        ) as tmp:
            tmp.write(sanitized)
            tmp_path = Path(tmp.name)
        try:
            mime = _guess_mime_type(fpath)
            entry = _upload_one(tmp_path, nsec_file, blossom_server, mime, max_file_size)
            entry["original_path"] = str(fpath)
            entry["relative_path"] = str(fpath.relative_to(results_dir))
            entry["redacted"] = True
            entry["redactions"] = r_info.get("count", len(findings))
            redacted_uploaded.append(entry)
            logger.info(
                "Uploaded redacted %s -> %s (%d bytes, %d redactions)",
                entry["relative_path"], entry["blossom_url"],
                entry["size"], entry["redactions"],
            )
        except Exception:
            logger.exception("Redacted upload failed for %s", fpath)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    all_files = uploaded + redacted_uploaded

    # ------------------------------------------------------------------
    # 4. NIP-94 (kind 1063) per file — opt-in to avoid relay spam
    # ------------------------------------------------------------------
    if publish_nip94:
        for entry in all_files:
            entry["nip94_event_id"] = _publish_file_event(
                entry, nsec_file, relays, {**metadata, "run_id": run_id}
            )
    else:
        logger.info("NIP-94 per-file events skipped (use --publish-nip94 to enable)")

    # ------------------------------------------------------------------
    # 5. Kind 30078 summary event
    # ------------------------------------------------------------------
    file_urls = [e["blossom_url"] for e in all_files]
    summary_payload = {
        "run_id": run_id,
        "timestamp": timestamp_str,
        "blossom_server": blossom_server,
        "scan_summary": scan_summary,
        "files": [
            {
                "path": e["relative_path"],
                "url": e["blossom_url"],
                "sha256": e["sha256"],
                "mime": e["mime_type"],
                "size": e["size"],
                "redacted": e.get("redacted", False),
            }
            for e in all_files
        ],
        "metadata": metadata,
    }
    summary_content = json.dumps(summary_payload, separators=(",", ":"))

    extra_tags: list = []
    if metadata.get("backend"):
        extra_tags.append(["backend", metadata["backend"]])
    if metadata.get("sut_repo"):
        extra_tags.append(["sut_repo", metadata["sut_repo"]])

    summary_event_id: str | None = None
    try:
        result = publish_test_run_event(
            nsec_file=nsec_file,
            run_id=run_id,
            timestamp=timestamp_unix,
            file_urls=file_urls,
            summary=summary_content,
            relays=relays,
            extra_tags=extra_tags if extra_tags else None,
        )
        if result.get("success"):
            summary_event_id = result.get("event_id") or None
            logger.info(
                "Summary event %s published for run %s",
                summary_event_id, run_id,
            )
        else:
            logger.error(
                "Summary event publish failed: %s",
                result.get("error", "unknown"),
            )
    except Exception:
        logger.exception("Failed to publish summary event for run %s", run_id)

    manifest = {
        "run_id": run_id,
        "timestamp": timestamp_str,
        "files": [
            {
                "path": e["relative_path"],
                "url": e["blossom_url"],
                "sha256": e["sha256"],
                "size": e["size"],
                "mime": e["mime_type"],
                "redacted": e.get("redacted", False),
                "nip94_event_id": e.get("nip94_event_id"),
            }
            for e in all_files
        ],
        "summary_event_id": summary_event_id,
        "blossom_server": blossom_server,
        "scan_summary": scan_summary,
    }

    logger.info(
        "Run %s complete: %d files published, %d blocked, %d redacted",
        run_id, len(all_files), n_blocked, n_redacted,
    )
    return manifest


def _hard_filter(p: Path, blocked: list[str]) -> bool:
    """Return True if ``p`` is hard-blocked; append to ``blocked`` if so."""
    if _is_hard_blocked(p):
        blocked.append(str(p))
        return True
    if _is_artifact_for_skipped_test(p):
        blocked.append(str(p))
        return True
    return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m lib.result_publisher",
        description=(
            "Publish test results to Blossom + Nostr. "
            "Scans for secrets, uploads clean/redacted files, publishes "
            "NIP-94 (kind 1063) file events and a kind 30078 run summary."
        ),
    )
    p.add_argument(
        "results_dir",
        help="Directory of test results to publish (e.g. results/<run_id>)",
    )
    p.add_argument(
        "--nsec-file",
        default=os.environ.get("NSEC_FILE", "~/.config/prta/nsec"),
        help="Path to nsec file for Blossom auth + Nostr signing (env: NSEC_FILE)",
    )
    p.add_argument(
        "--blossom-server",
        default=DEFAULT_BLOSSOM_SERVER,
        help="Blossom server base URL (env: BLOSSOM_SERVER)",
    )
    p.add_argument(
        "--relays",
        default=",".join(DEFAULT_RELAYS),
        help="Comma-separated Nostr relays (env: NOSTR_RELAYS)",
    )
    p.add_argument(
        "--run-id", default=None,
        help="Run identifier. Auto-generated if omitted.",
    )
    p.add_argument(
        "--branch",
        default=os.environ.get("TOLLGATE_BRANCH", ""),
        help="Git branch metadata to embed in the summary event",
    )
    p.add_argument(
        "--pr",
        default=os.environ.get("TOLLGATE_PR", ""),
        help="PR number metadata to embed in the summary event",
    )
    p.add_argument(
        "--passed", type=int, default=None,
        help="Number of passing tests (metadata)",
    )
    p.add_argument(
        "--failed", type=int, default=None,
        help="Number of failing tests (metadata)",
    )
    p.add_argument(
        "--skipped", type=int, default=None,
        help="Number of skipped tests (metadata)",
    )
    p.add_argument(
        "--router", default=None,
        help="Router model metadata",
    )
    p.add_argument(
        "--commit", default=None,
        help="SUT commit hash (short) metadata",
    )
    p.add_argument(
        "--portal", default=None,
        help="Portal/addon name (e.g., net4sats, builtin)",
    )
    p.add_argument(
        "--sut-backend", default=None,
        help="Backend type metadata (go or rust) — added as Nostr tag for filtering",
    )
    p.add_argument(
        "--sut-repo", default=None,
        help="SUT repository (e.g., Amperstrand/tollgate-rs-ai-research-and-experiments) — added as Nostr tag",
    )
    p.add_argument(
        "--max-file-size", type=int, default=DEFAULT_MAX_FILE_SIZE,
        help=f"Skip files larger than this many bytes (default: {DEFAULT_MAX_FILE_SIZE})",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Scan and report only; do not upload or publish",
    )
    p.add_argument(
        "--publish-nip94", action="store_true", default=False,
        help="Publish NIP-94 (kind 1063) per-file events for BlossomFS /nip94/ discovery. "
             "Default off to avoid relay spam. Only the kind 30078 summary is published by default.",
    )
    p.add_argument(
        "--manifest-out", default=None,
        help="Write the manifest JSON to this path (default: stdout only)",
    )
    p.add_argument(
        "--lab-type", default=os.environ.get("TOLLGATE_LAB_TYPE", "physical"),
        choices=["gcloud", "virtual-lab", "physical", "shc"],
        help="Lab environment type. gcloud/virtual-lab/shc = publish without IP/MAC redaction. "
             "physical = redact all network identifiers. (env: TOLLGATE_LAB_TYPE)",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    relays = [r.strip() for r in args.relays.split(",") if r.strip()]
    metadata: dict = {}
    if args.branch:
        metadata["branch"] = args.branch
    if args.pr:
        metadata["pr"] = args.pr
    if args.passed is not None:
        metadata["passed"] = args.passed
    if args.failed is not None:
        metadata["failed"] = args.failed
    if args.skipped is not None:
        metadata["skipped"] = args.skipped
    if args.router:
        metadata["router"] = args.router
    if args.commit:
        metadata["commit"] = args.commit
    if args.portal:
        metadata["portal"] = args.portal
    if args.sut_backend:
        metadata["backend"] = args.sut_backend
    if args.sut_repo:
        metadata["sut_repo"] = args.sut_repo

    start = time.monotonic()
    try:
        manifest = publish_results(
            results_dir=args.results_dir,
            nsec_file=os.path.expanduser(args.nsec_file),
            run_id=args.run_id,
            blossom_server=args.blossom_server,
            relays=relays,
            metadata=metadata,
            max_file_size=args.max_file_size,
            dry_run=args.dry_run,
            publish_nip94=args.publish_nip94,
        )
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 2
    except Exception:
        logger.exception("Publishing failed")
        return 1

    elapsed = time.monotonic() - start
    logger.info("Done in %.1fs", elapsed)

    out = json.dumps(manifest, indent=2)
    if args.manifest_out:
        Path(args.manifest_out).write_text(out)
        logger.info("Manifest written to %s", args.manifest_out)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
