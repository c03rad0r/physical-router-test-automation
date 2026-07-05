# Make-to-Pytest Migration Analysis

**Date**: 2026-05-21 (aligned with live registry)
**Scope**: Which Makefile test targets have no pytest equivalent, and what's needed to migrate them.

## Live registry and runners

| Artifact | Purpose |
|----------|---------|
| [`config/make-pytest-map.yaml`](../config/make-pytest-map.yaml) | Source of truth: Make target → pytest node / runner |
| [`scripts/pymake.py`](../scripts/pymake.py) | CLI: `./scripts/pymake.py smoke-degraded --router alpha` |
| [`make/migration.mk`](../make/migration.mk) | Root Makefile stubs forward to pymake |

Migrated root Makefile targets print a deprecation banner and invoke pymake (no dual-run of old shell tests).

```bash
make lock PHASE='smoke-degraded'
make smoke-degraded ROUTER=alpha          # → pymake → pytest
./scripts/pymake.py smoke-degraded --router alpha
make pytest-scenarios ROUTER=alpha        # all hardware scenarios
```

---

## Executive Summary

| Metric | Count |
|---|---|
| Total registered migrated/ops targets | 44 |
| Covered by pytest / Playwright runner | 40 |
| Ops-only / delegated targets | 4 |
| Functional make-test gaps in live registry | 0 |
| Main remaining work | ESP32/relay/arch normalization |

The live registry now maps the router hardware test targets to pytest or Playwright runners. The remaining non-pytest areas are operational/delegated flows: serial shell/status/recovery and ESP32/relay/arch targets that still delegate to the ESP32 Makefile.

---

## Coverage Matrix

### SSL Tests (PR #123)

| Make Target | What It Does | Pytest Equivalent | Status |
|---|---|---|---|
| `r-test-ssl-self-signed` | Apply self-signed cert, verify files/uci/listener | `tests/api/test_ssl_go_cli.py::test_ssl_self_signed_apply` | ✅ Covered |
| `r-test-ssl-self-signed-yes` | Apply with `--yes` flag | `test_ssl_go_cli.py::test_ssl_apply_with_yes_flag` | ✅ Covered |
| `r-test-ssl-remove` | Remove SSL, verify cleanup | `test_ssl_go_cli.py::test_ssl_remove_reverts_config` | ✅ Covered |
| `r-test-ssl-remove-no-backup` | Remove when no backup exists | `test_ssl_go_cli.py::test_ssl_remove_without_backup_errors` | ✅ Covered |
| `r-test-ssl-reapply` | Re-apply with existing backup | `test_ssl_go_cli.py::test_ssl_reapply_with_existing_backup` | ✅ Covered |
| `r-test-ssl-setup-verify` | Verify clean SSL state | `test_ssl_go_cli.py::test_ssl_status_not_configured` | ✅ Covered |
| `r-test-ssl-status` | Check `tollgate ssl status` output | `test_ssl_go_cli.py::test_ssl_status_shows_details` | ✅ Covered |
| `r-test-ssl-verify-cert` | Deep cert validation (SAN, CN, expiry) | `test_ssl_go_cli.py::test_ssl_cert_cn_san_valid` | ✅ Covered |
| `r-test-ssl-verify-nds` | Verify NDS allows port 443 | `test_ssl_go_cli.py::test_ssl_nds_allows_443` | ✅ Covered |
| `r-test-ssl-verify-no-dns` | No dnsmasq entry for self-signed | `test_ssl_go_cli.py::test_ssl_no_dnsmasq_for_self_signed` | ✅ Covered |
| `r-test-ssl-idempotent` | Apply twice, verify consistent | `test_ssl_go_cli.py::test_ssl_idempotent_apply` | ✅ Covered |
| `r-test-ssl-full` | Full lifecycle (apply→verify→remove→verify) | Covered by individual tests above | ✅ Covered |
| `r-test-ssl-comprehensive` | All self-signed tests in sequence (12 phases) | Covered by individual tests above | ✅ Covered |
| `r-test-ssl-real-cert` | Real cert via LE staging + Cloudflare DNS-01 | `tests/api/test_ssl_real_cert.py::test_ssl_real_cert_apply_via_acme` | ✅ Covered, env-gated |
| `r-test-ssl-real-cert-remove` | Remove real cert (dnsmasq + NDS revert) | `tests/api/test_ssl_real_cert.py::test_ssl_real_cert_remove_cleans_state` | ✅ Covered |
| `r-test-ssl-real-cert-full` | Full real cert lifecycle | `tests/api/test_ssl_real_cert.py` | ✅ Covered, env-gated |
| `r-test-ssl-all` | Comprehensive + real cert | `tests/api/test_ssl_go_cli.py tests/api/test_ssl_real_cert.py` | ✅ Covered, real cert env-gated |

**Real cert tests need**: Cloudflare DNS-01 credentials and a domain pointing to the router. They are implemented in `tests/api/test_ssl_real_cert.py` and remain opt-in through the `SSL_CF_TOKEN`/domain environment requirements tracked in `config/make-pytest-map.yaml`.

### Hostname (PR #117)

| Make Target | What It Does | Pytest Equivalent | Status |
|---|---|---|---|
| `r-test-hostname` | Verify UCI, kernel, uhttpd hostname | `tests/api/test_hostname.py` (PR #117 gated) | ✅ Covered |

### Degraded Mode / Mint Health

| Make Target | What It Does | Pytest Equivalent | Status |
|---|---|---|---|
| `r-smoke-offline` | Block mint + restart + verify degraded | `test_mint_health.py::test_restart_into_degraded_mode` | ✅ Covered |
| `r-smoke-degraded` | Full degraded lifecycle (~3 min) | `test_mint_health.py::test_full_degraded_lifecycle` | ✅ Covered |
| `r-smoke-recovery` | Unblock + wait for recovery | `test_mint_health.py::test_recovery_to_full_merchant` | ✅ Covered |
| `r-smoke-degraded-recovery` | Degraded→recovery WITHOUT restart (BoltDB lock) | `test_boot_hygiene.py::test_degraded_recovery_no_restart` | ✅ Covered |
| `r-smoke-dynamic-rebuild` | Full→degraded→full rebuild (~10 min) | `test_boot_hygiene.py::test_dynamic_merchant_rebuild` | ✅ Covered |
| `r-smoke-degraded-connect` | Connect upstream while already degraded (RISKY) | `test_two_router.py::TestDegradedConnectWhileDegraded::test_connect_while_degraded` | ✅ Covered, risky-gated |
| `r-test-offline-ops` | Verify wallet balance + status offline | `test_mint_health.py::test_offline_wallet_operations` | ✅ Covered |
| `r-test-first-boot-offline` | First boot with unreachable mint | `test_mint_health.py::test_first_boot_offline` | ✅ Covered |
| `r-test-no-mints` | No configured mints scenario | `test_mint_health.py::test_no_configured_mints` | ✅ Covered |
| `r-test-default-mints` | Verify default mint config | `test_mint_health.py` (partial) | ⚠️ Partial |
| `r-test-edge-cases` | Edge case testing (phase 8) | `test_mint_health.py` (partial) | ⚠️ Partial |
| `r-check-degraded` | Verify degraded merchant state | — | ❌ **Make-only** (helper) |
| `r-check-merchant` | Verify full merchant state | — | ❌ **Make-only** (helper) |

### Two-Router / Upstream

| Make Target | What It Does | Pytest Equivalent | Status |
|---|---|---|---|
| `r-smoke-upstream` | Two-router degraded upstream payment | `test_two_router.py::test_offline_renewal_via_lan` | ✅ Covered |
| `r-smoke-pin-upstream` | Upstream pin prevents scan-away | `test_two_router.py::test_pin_prevents_scan_away` | ✅ Covered |
| `r-smoke-degraded-upstream` | Connect online, degrade, verify offline renewal | `test_two_router.py::test_offline_renewal_via_lan` | ✅ Covered |
| `r-check-sta-health` | No stale/duplicate STA sections | `test_upstream_wifi.py::test_sta_health` | ✅ Covered |

### Startup Hygiene

| Make Target | What It Does | Pytest Equivalent | Status |
|---|---|---|---|
| `r-test-startup-hygiene` | Full power cycle: dead STA + remove ecash + reboot | `test_boot_hygiene.py::test_startup_hygiene_auto_switch` | ✅ Covered |
| `r-test-startup-hygiene-dead-only` | Boot with ONLY dead STA | `test_boot_hygiene.py::test_startup_hygiene_dead_only` | ✅ Covered |
| `r-test-startup-hygiene-setup` | Setup step (enable dead STA, remove ecash) | Inline in pytest test | ✅ Covered |
| `r-test-startup-hygiene-verify` | Verify auto-switch + restore | Inline in pytest test | ✅ Covered |

### Recovery

| Make Target | What It Does | Pytest Equivalent | Status |
|---|---|---|---|
| `r-test-reseller-guard` | Reseller mode guard | `test_reseller_mode.py::test_reseller_mode_toggle_persists` | ✅ Covered |
| `r-test-cleanup` | Cleanup after tests | Inline in pytest fixtures | ✅ Covered |

### Captive Portal / Payment (Playwright)

| Make Target | What It Does | Pytest Equivalent | Status |
|---|---|---|---|
| `r-test-captive-portal` | Playwright captive portal tests (.spec.mjs) | `tests/protocol/captive-portal.spec.mjs` | ⚠️ Same tests, different runner |
| `r-test-captive-portal-happy` | Happy-path only | `tests/protocol/captive-portal.spec.mjs` (subset) | ⚠️ Same tests, different runner |
| `r-test-cashu-payment` | E2E cashu payment via Playwright | `tests/protocol/payment-lifecycle.spec.mjs` | ⚠️ Similar, different runner |

These are JavaScript Playwright tests called from make. Not candidates for Python migration — they're already automated. Could be unified under pytest via `pytest-playwright` but marginal benefit.

---

## Remaining Non-Pytest / Delegated Targets

These are not functional router Make-test gaps in the live registry; they are operational commands or component-specific suites that still intentionally delegate outside pytest.

| Target / Area | Status | Notes |
|---|---|
| `serial-recovery` | ops | Emergency command path; tracked in registry as `runner: ops` |
| `serial-shell` / `serial-status` | ops | Interactive/read-only serial operations, not tests |
| `arch-test-full` | delegated | Delegates to `esp32/Makefile` Node/firmware E2E suite |
| ESP32 multi-mint/CVM/relay targets | delegated/manual | Root Makefile forwards to `esp32/Makefile`; good next normalization target if a unified pytest surface is desired |

---

## Utility Cleanup Still Worth Doing

The target migration is effectively complete for the router test registry, but there is still cleanup value in promoting repeated shell snippets to `router.py` helpers:

- UCI helpers: `uci_get`, `uci_set`, `uci_commit`.
- Hosts/mint blocking helpers used by mint-health and boot-hygiene scenarios.
- Upstream WiFi helpers for status/connect/remove flows.
- Parsed SSL status helper for clearer assertions in SSL tests.

---

## Recommended Migration Order

1. **Normalize ESP32/relay/arch targets** if they should appear in unified pytest/reporting instead of Make delegation.
2. **Promote common router helpers** (`uci_*`, mint block/unblock, upstream WiFi helpers) to reduce duplicated SSH snippets.
3. **Keep the registry authoritative**: update `config/make-pytest-map.yaml` first, then reflect any changes here.

Router hardware pytest/Playwright parity is complete in the live registry; remaining work is consistency and reporting ergonomics.

After migration, the Makefile targets remain as convenience aliases (`make smoke-degraded` still works) but the source of truth is pytest. Implementation status is tracked in `config/make-pytest-map.yaml` (`status: migrated`).

### Newly migrated (pytest)

- `r-smoke-degraded-connect` → `tests/scenarios/test_two_router.py::TestDegradedConnectWhileDegraded` (requires `TOLLGATE_ALLOW_RISKY=1`)
- `r-test-default-mints` → `test_default_mints_configured`
- `r-test-edge-cases` → `test_connect_to_unknown_ssid_fails` (upstream WiFi)
- `r-test-ssl-real-cert*` → `tests/api/test_ssl_real_cert.py` (requires Cloudflare token + domain)
- Serial ops → `lib/serial_console.py` + `pymake serial-recovery|serial-shell|serial-status`
