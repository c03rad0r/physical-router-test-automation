# Publishing Pipeline

[Test results dashboard](https://tests.tollgate.me/) is a client-side SPA that reads DVM lifecycle events from Nostr relays and fetches artifacts from Blossom.

## Architecture

```
pytest → collect_and_render → publish_to_nostr (Blossom upload + NIP-94) → DVM lifecycle (kind 5900/7000/6900)
```

### DVM Event Lifecycle (NIP-90)

The cloud lab pipeline publishes a full DVM job lifecycle:

1. **Kind 5900 (job request)** — Published at pipeline start with branch, backend, scope params
2. **Kind 7000 (processing)** — Feedback event: "Cloud lab pipeline starting"
3. **Kind 7000 (success/error)** — Feedback event at pipeline end
4. **Kind 6900 (result)** — Job result with pass/fail counts + Blossom artifact URLs

All events link back to the kind 5900 request via `e` tag.

### Blossom Upload (BUD-02 + BUD-11)

Test artifacts are uploaded to [Blossom](https://github.com/hzrd149/blossom):

1. `lib/result_publisher.py` scans the results directory
2. Files are uploaded to Blossom via `lib/blossom_publisher.py` (BUD-02 PUT /upload)
3. Auth is signed via nak CLI (BUD-11, kind 24242 events)
4. NIP-94 (kind 1063) file metadata events published per file (optional, off by default)

The result_publisher captures the Blossom URLs in a JSON manifest, which the pipeline passes to the kind 6900 DVM result event.

### Kind 30078 (deprecated)

Previously published kind 30078 (NIP-78 app-specific data) summary events. Now gated behind `SKIP_30078_SUMMARY` env var (set to `true` by the cloud lab pipeline). The dashboard no longer fetches kind 30078 — only DVM kinds (5900/6900/7000/1063).

## Dashboard (tests.tollgate.me)

Static SPA served from `docs/` on the `main` branch via GitHub Pages. It:

- Connects to Nostr relay (`wss://relay.cashu.email`)
- Fetches kind 5900/6900/7000/1063 events from ALL runner pubkeys
- Renders a sidebar of test runs with pass/fail/skip stats
- Shows screenshots and file artifacts from Blossom URLs
- Supports search, filter, mobile push/pop navigation

## Configuration

All relay and Blossom server URLs are centralized in `lib/constants.py`:

| Setting | Default | Override via env |
|---------|---------|------------------|
| Nostr relay | `wss://relay.cashu.email` | `NOSTR_RELAYS` (comma-separated list) |
| Blossom server | `https://blossom.psbt.me` | `BLOSSOM_SERVERS` (comma-separated list) |

## Verification

After publishing, `verify_nostr_publish()` queries the relay with `nak req -k 6900` for the result event matching `run_id` in the content JSON, then fetches one Blossom URL to confirm the blob is live.

## Related

- [hackathon-tooling/nostr-compute-kinds.md](https://github.com/Amperstrand/hackathon-tooling/blob/main/docs/nostr-compute-kinds.md) — NIP-78 vs NIP-90 analysis, kind 5900 decision
- [hackathon-tooling/dvm-runner-pattern.md](https://github.com/Amperstrand/hackathon-tooling/blob/main/docs/dvm-runner-pattern.md) — DVM runner reference architecture
- [hackathon-tooling/blossomfs-build-cache.md](https://github.com/Amperstrand/hackathon-tooling/blob/main/docs/blossomfs-build-cache.md) — Content-addressable build cache design