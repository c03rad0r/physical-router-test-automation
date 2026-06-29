# SHC CI Integration Plan — TollGate Testing on Sovereign Hybrid Compute

**Status**: Plan approved, execute next session
**Date**: 2026-06-29

## Executive Summary

Migrate TollGate testing from Hetzner/GCloud to **SHC (Sovereign Hybrid Compute)** as the
default cloud testing platform. Use `physical-router-test-automation` as the test framework
(not conwrt). Enable GitHub CI for automated test runs.

## Decisions (from interview)

| Decision | Choice |
|----------|--------|
| Cloud platform | SHC (proven: $0.01/test, nested KVM, 10s provisioning) |
| Test framework | `physical-router-test-automation` (NOT conwrt — conwrt uses this, doesn't duplicate) |
| CI trigger | Manual (workflow_dispatch) for now, every-commit later |
| Environment tag | `context-vm` (new standard, replacing `dvm`) |
| Dashboard default | tests.tollgate.me defaults to "TollGate" filter |
| conwrt e2e | No — conwrt uses physical-router-test-automation, doesn't build its own |

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  GitHub Actions (OpenTollGate/tollgate-module-basic-go)         │
│                                                                  │
│  ┌──────────────────────┐   ┌────────────────────────────────┐ │
│  │ Existing CI (no SHC) │   │ New: SHC Test Job              │ │
│  │                      │   │                                │ │
│  │ • Go unit tests ✅   │   │ 1. shc order --hostname ci-test │ │
│  │ • Docker integ ✅    │   │ 2. SSH in, install deps         │ │
│  │ • Cross-compile ⚠️   │   │ 3. Clone repos                  │ │
│  │                      │   │ 4. Boot QEMU OpenWrt + KVM     │ │
│  │ No secrets needed    │   │ 5. Build + deploy TollGate      │ │
│  │ for unit/integ       │   │ 6. Run physical-router-tests    │ │
│  └──────────────────────┘   │ 7. Publish to Nostr             │ │
│                              │ 8. shc cancel (cleanup)         │ │
│                              │                                 │ │
│                              │ Triggered via workflow_dispatch │ │
│                              │ Secrets: SHC_API_KEY            │ │
│                              └────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
          │                                    │
          ▼                                    ▼
┌──────────────────┐              ┌───────────────────────────┐
│ GitHub Artifacts │              │ tests.tollgate.me         │
│ (coverage, etc)  │              │                           │
│                  │              │ • Default: TollGate filter │
│                  │              │ • New tag: context-vm      │
│                  │              │ • Shows SHC runs live      │
└──────────────────┘              └───────────────────────────┘
```

## Phase 1: Fix Existing CI (Quick Win)

The tollgate-module-basic-go already has a `test.yml` with unit tests and Docker integration
tests that don't need secrets. But it only triggers on `main`/`develop` branches.

**Action**: Add `workflow_dispatch` trigger to `test.yml` so it can run manually on any branch.
Also add our feature branches to the push trigger.

**Files to change**: `.github/workflows/test.yml` in tollgate-module-basic-go

**Result**: Go unit tests + Docker integration tests run in CI without any cloud VM.

## Phase 2: SHC Test Runner Script

Create a script in `physical-router-test-automation` that orchestrates the full SHC test run:

```bash
# scripts/run-shc-test.sh
# Usage: ./scripts/run-shc-test.sh --commit <git-hash> [--plan api]

1. Order SHC VM (Dev VPS Standard, $0.46/day)
2. Wait for provisioning
3. SSH in, install: qemu, go, python3, docker
4. Clone tollgate-module-basic-go at <commit>
5. Clone physical-router-test-automation
6. Boot QEMU OpenWrt x86_64 with KVM
7. Build TollGate binary from source
8. Deploy to OpenWrt VM (SCP + install)
9. Configure test .env (router IP, SSH, etc.)
10. Run: pytest tests/api/ -m "api and not phone" --timeout=300
11. Publish results to Nostr (tag: context-vm, category: tollgate)
12. Cancel SHC VM
```

**Key**: This script lives in `physical-router-test-automation` and uses its existing test
infrastructure (conftest.py fixtures, router library, payment protocol, etc.).

## Phase 3: GitHub Actions Integration

Add an SHC test job to the tollgate-module-basic-go CI:

```yaml
# .github/workflows/test.yml — new job
shc-router-tests:
  name: SHC Router Tests
  runs-on: ubuntu-latest
  if: github.event_name == 'workflow_dispatch'
  steps:
    - uses: actions/checkout@v4
    - name: Install SHC CLI
      run: pip install shc-toolkit
    - name: Run SHC test
      env:
        SHC_API_KEY: ${{ secrets.SHC_API_KEY }}
        TOLLGATE_COMMIT: ${{ github.sha }}
      run: |
        git clone https://github.com/OpenTollGate/physical-router-test-automation.git
        cd physical-router-test-automation
        ./scripts/run-shc-test.sh --commit $TOLLGATE_COMMIT --plan api
    - name: Upload results
      uses: actions/upload-artifact@v4
      if: always()
      with:
        name: shc-test-results
        path: results/
```

**Secrets needed**: `SHC_API_KEY` (stored in GitHub repo secrets)

## Phase 4: Dashboard Improvements

### tests.tollgate.me Changes

1. **Default to "TollGate" filter** — page loads with TollGate category selected
2. **Add "context-vm" category** — for SHC/cloud-VM-based test runs (replacing "dvm")
3. **Keep "Other" for misc runs** — Nostr event floods (price feeds, etc.)

### How runs get tagged

The Nostr event published by `publish-report.sh` needs a category tag:
```json
{
  "kind": 30078,
  "tags": [
    ["category", "tollgate"],
    ["environment", "context-vm"],
    ["commit", "<git-hash>"],
    ["plan", "api"]
  ]
}
```

**Action**: Update `publish-report.sh` in physical-router-test-automation to include
`category` and `environment` tags in the Nostr event.

### Dashboard source

The dashboard is a static site (likely in physical-router-test-automation or a separate repo).
Need to find and modify the default filter.

## Phase 5: conwrt Integration

conwrt should NOT build its own e2e test suite. Instead:

1. conwrt's serial/firewall/zycast tools are tested via unit tests (Python)
2. For router-level e2e testing, conwrt uses `physical-router-test-automation`
3. conwrt-specific tests (RFC 1918 isolation, serial boot capture) live in
   `physical-router-test-automation/tests/api/` alongside existing TollGate tests

**Action**: Move `test_rfc1918_isolation.py` from conwrt to physical-router-test-automation
if not already there (it IS already there as PR #55).

## Cost Analysis

| Scenario | Runs/day | Cost/run | Daily cost |
|----------|---------|----------|------------|
| Manual trigger | 0-2 | $0.01 | $0-0.02 |
| PR testing | 1-5 | $0.01 | $0.01-0.05 |
| Every commit | 5-20 | $0.01 | $0.05-0.20 |
| Nightly | 1 | $0.01 | $0.01 |

**Annual cost estimate** (every commit + nightly): ~$50-70/year

## Execution Plan (Next Session)

1. **Fix test.yml trigger** — add workflow_dispatch to tollgate CI (5 min)
2. **Write run-shc-test.sh** — SHC orchestration script (30 min)
3. **Order SHC VM, run full test** — deploy TollGate, run API suite (45 min)
4. **Publish results to Nostr** — tagged as context-vm, category=tollgate (15 min)
5. **Update tests.tollgate.me** — default to TollGate filter (30 min)
6. **Verify on dashboard** — Playwright screenshot showing our run (10 min)

Total: ~2.5 hours next session

## What This Session Proved

- ✅ SHC provisions in 10s with nested KVM
- ✅ QEMU + OpenWrt boots in 15s with KVM
- ✅ RFC 1918 rules load correctly on real OpenWrt
- ✅ physical-router-test-automation tests collect on SHC (477 tests)
- ✅ Docker available for cloud-lab integration tests
- ✅ Cost: $0.01 net per test run (with cancel credit)
- ✅ TollGate CI pipeline exists (unit tests, Docker tests work without secrets)
- ❌ TollGate cross-compilation CI currently fails (pre-existing, not our issue)
- ❌ test.yml doesn't trigger on feature branches (needs fix)
