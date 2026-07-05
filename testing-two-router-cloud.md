# Two-Router Cloud Test Architecture

## Overview

The two-router cloud tests verify TollGate's upstream payment flow in the GCP cloud lab using two OpenWrt QEMU VMs connected via dedicated network bridges. No physical hardware is required.

**Two test files cover different aspects:**
- `tests/scenarios/test_two_router_cloud.py` — L3 connectivity, health, degraded mode lifecycle
- `tests/scenarios/test_two_router_payment.py` — Full upstream payment protocol (discovery, session, usage tracking, internet access)

## Topology

```
┌──────────────────────────────────────────────────────────────────────┐
│                       GCP VM (nested KVM)                            │
│                                                                      │
│  ┌──────────────────┐  tg-poc-br   ┌──────────────────┐             │
│  │  Alpha (OpenWrt) │◄────────────►│  Host / Debian   │             │
│  │  br-lan: 10.99.99.1             │  10.99.99.2       │             │
│  │  reseller_mode: true            │  (NAT, mints)     │             │
│  └──────────────────┘              └──────────────────┘             │
│         │ eth1 (DHCP from Beta)                                     │
│         │ 10.99.98.x                                                │
│  tg-upstream-br (L2 only, no host port)                             │
│         │ 10.99.98.1                                                │
│  ┌──────────────────┐  tg-beta-br  ┌────────────────┐              │
│  │  Beta (OpenWrt)  │◄────────────►│ Host port       │              │
│  │  br-lan: 10.99.96.11           │ 10.99.96.2       │              │
│  │  eth1: 10.99.98.1 (DHCP srv)   │ (route to mint)  │              │
│  └──────────────────┘              └────────────────┘              │
│                                                                      │
│  ┌──────────────────────────────────────────────────┐               │
│  │  mgmt-br (10.99.97.0/24) — SSH independent       │               │
│  │    host: 10.99.97.2                               │               │
│  │    alpha: 10.99.97.1    beta: 10.99.97.11         │               │
│  │    debian: 10.99.97.100                           │               │
│  └──────────────────────────────────────────────────┘               │
│                                                                      │
│  Local mints (on host):                                             │
│    CDK V2:      :8383    Nutshell V2: :8384    Nutshell V1: :8385  │
└──────────────────────────────────────────────────────────────────────┘
```

### Network Details

| Bridge | Subnet | Purpose |
|--------|--------|---------|
| `tg-poc-br` | 10.99.99.0/24 | Test LAN. Alpha + Host + Debian. **Beta is NOT on this bridge.** |
| `tg-beta-br` | 10.99.96.0/24 | Beta's isolated br-lan. Host port (10.99.96.2) for routing to mint. |
| `tg-upstream-br` | 10.99.98.0/24 | L2-only bridge for Alpha↔Beta upstream link. No host port. |
| `mgmt-br` | 10.99.97.0/24 | Management. All VMs connected for SSH independent of test bridges. |

| Host | Interface | IP | Role |
|------|-----------|-----|------|
| Alpha | br-lan | 10.99.99.1 | Primary TollGate under test (reseller) |
| Alpha | eth1 | DHCP from Beta | WAN/upstream interface |
| Beta | br-lan | 10.99.96.11 | Upstream TollGate merchant (isolated) |
| Beta | eth1 | 10.99.98.1 (static) | DHCP server for Alpha's WAN |
| Host | tg-beta-br | 10.99.96.2 | Routes Beta traffic to mint at 10.99.99.2 |

### Why isolated Beta?

Beta runs on its own bridge (`tg-beta-br`) instead of sharing `tg-poc-br` with Alpha:

1. **No DHCP conflicts** — each router serves its own subnet
2. **No broadcast cross-talk** — Alpha's clients never see Beta's beacons
3. **Unambiguous testing** — if traffic reaches 10.99.98.x, it went through Beta
4. **Matches real topology** — in production, downstream and upstream routers don't share a LAN

### Worker pipeline steps (two-router mode)

When `--two-router` is passed, the worker adds these steps between `select-mint` and `preflight`:

1. **Launch Beta VM** on `tg-beta-br` (not `tg-poc-br`)
2. **Configure mgmt NIC** on Beta (10.99.97.11)
3. **Configure Beta br-lan** to isolated 10.99.96.11/24, add static route to mint
4. **Configure Beta upstream** — DHCP server + NAT on eth1 for Alpha's WAN
5. **Deploy TollGate** to both Alpha and Beta
6. **Configure Beta as merchant** — accepted_mints, metric=milliseconds, step_size=60s, pricing
7. **Configure Alpha as reseller** — reseller_mode=true, upstream_detector watching eth1
8. **Fund Alpha wallet** — mint 100 sats from local mint, run `tollgate wallet fund`

### Upstream payment protocol

```
1. Alpha upstream_detector probes eth1 gateway → GET http://10.99.98.1:2121/
2. Beta returns kind 10021 advertisement with price_per_step tags
3. Alpha selects compatible mint/pricing, mints a Cashu token
4. Alpha POSTs token as text/plain to POST http://10.99.98.1:2121/
5. Beta validates token, credits wallet, returns kind 1022 session event
6. Alpha tracks usage via GET /usage on Beta, auto-renews before exhaustion
7. Alpha's traffic routes: eth1 → Beta eth1 → Beta NAT → internet
```

## Test Suites

### Infrastructure tests: `tests/scenarios/test_two_router_cloud.py`

| Test | What it verifies |
|------|-----------------|
| `test_alpha_wan_link_to_beta` | Alpha has DHCP lease from Beta, can ping it, SSH works |
| `test_block_mint_enters_degraded_mode` | Mint block → Alpha enters degraded (kind 21023) |
| `test_unblock_mint_recovers_from_degraded` | Mint unblock → Alpha recovers to full merchant (kind 10021) |
| `test_both_routers_healthy` | Both TollGate instances respond on :2121 |

### Payment protocol tests: `tests/scenarios/test_two_router_payment.py`

| Test | What it verifies |
|------|-----------------|
| `test_beta_advertisement_visible_from_alpha` | Alpha fetches Beta's kind 10021 ad with pricing tags |
| `test_alpha_upstream_detector_sees_beta` | Log evidence upstream_detector found Beta |
| `test_alpha_wallet_funded` | Alpha wallet balance > 0 (worker funding worked) |
| `test_alpha_pays_beta_and_gets_session` | Kind 1022 session evidence in logs/CLI |
| `test_alpha_usage_tracking_on_beta` | Beta tracks session for Alpha's WAN IP |
| `test_internet_through_beta` | Alpha can ping 1.1.1.1 through Beta |
| `test_beta_session_on_alpha_disconnect` | Beta's NDS state is valid for Alpha |
| `test_both_routers_healthy_after_payment` | Both return valid kinds; Beta is full merchant |

All tests use feature detection (`pytest.skip()`) and run independently — no ordering dependencies.

## Running

```bash
# Submit two-router cloud run (full test suite including payment tests)
./scripts/cloud-lab.py submit --branch main --two-router --publish

# Two-router with reseller scenarios
./scripts/cloud-lab.py submit --branch main --two-router --reseller-scenarios --publish

# Check status
./scripts/cloud-lab.py status-run --run-id <run-id>
```

The `--two-router` flag triggers:
1. A second OpenWrt VM (Beta) is launched on `tg-beta-br`
2. `tg-upstream-br` bridge is created for Alpha↔Beta link
3. Beta is configured as upstream DHCP server + NAT gateway + TollGate merchant
4. Alpha is configured as reseller with funded wallet
5. `TOLLGATE_SECONDARY_ROUTER_HOST` is set to Beta's mgmt IP (10.99.97.11)
6. Both `test_two_router_cloud.py` and `test_two_router_payment.py` are included in the pytest run

## Design Decisions

### Isolated Beta bridge

Beta runs on `tg-beta-br` (10.99.96.0/24) instead of sharing `tg-poc-br`. The host has a port on `tg-beta-br` (10.99.96.2) and a static route is added on Beta so it can reach the local mint at 10.99.99.2 via the host.

### Mint reachability

The local mints listen on `10.99.99.2` (the host's `tg-poc-br` IP). Beta reaches the mint via:
- Static route: `ip route add 10.99.99.0/24 via 10.99.96.2`
- /etc/hosts entries for mint DNS names pointing to 10.99.99.2

### HTTP API over CLI Socket

The degraded mode tests use HTTP API responses (`GET /`, kind 10021/21023) instead of the CLI socket. This is because:
- The CLI socket may not exist on all backend versions
- The HTTP API is always available when TollGate is running
- The tests can run against both Go and Rust backends

### Wallet funding via CLI

Alpha's wallet is funded by minting tokens from the local mint using the `cashu` CLI on the host, then running `tollgate wallet fund '<token>'` on Alpha. This loads tokens into the internal gonuts wallet that the upstream_session_manager uses for payments.

### Millisecond metric for faster tests

Beta is configured with `metric: "milliseconds"` and `step_size: 60000` (1 minute) for faster test cycles. In production, routers typically use `bytes` with much larger step sizes.

## Related Files

| File | Purpose |
|------|---------|
| `tests/scenarios/test_two_router_cloud.py` | Infrastructure health + degraded mode tests |
| `tests/scenarios/test_two_router_payment.py` | Upstream payment protocol tests |
| `lib/cloud_lab/worker.py` | Cloud worker (VM setup, Beta config, payment config, wallet funding) |
| `lib/cloud_lab/constants.py` | Bridge names, IPs, mint URLs |
| `lib/helpers.py` | `is_degraded()`, `wait_for_degraded()`, `is_full_merchant()` |
| `lib/router.py` | `block_mint()`, `unblock_mint()`, `api_body()`, `cli_command()` |
| `scripts/cloud-lab.py` | CLI for submitting runs |
