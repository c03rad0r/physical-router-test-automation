# Physical Router Test Setup

## Hardware

Routers defined in `config/routers.json` (the router inventory). Each router entry specifies model, SSH host, architecture, OpenWrt version, and network topology.

Current inventory:

| ID | Model | WAN IP | LAN IP | Access |
|----|-------|--------|--------|--------|
| upstream | GL.iNet GL-MT3000 | 192.168.13.112 | 192.168.1.1 | Direct SSH via switch |
| alpha | D-Link COVR-X1860 A1 | 192.168.13.211 | 192.168.1.1 | Direct SSH via switch |
| beta | D-Link COVR-X1860 A1 | (none) | 192.168.1.1 | SSH via en6 ethernet |

## Network Topology

```
Internet → Main Router (192.168.13.1) → Switch → Test Router WAN (DHCP)
                                                      ↕
                                                 Test Machine (192.168.13.244)
```

For two-router tests:

```
[Dev Machine] ──ethernet── [Router A LAN: 192.168.1.x]
        │
        └──Router A connects to upstream WiFi──
                │
                Router A broadcasts APs:
                  TollGate-XXXX (open)
                  c08r4d0r-XXXX (PSK)

[Dev Machine] ──ethernet── [Router B LAN: different subnet]
```

### Connectivity paths

| From | To | Method | Notes |
|------|----|--------|-------|
| Dev machine | Router | SSH to WAN IP | Always works when WAN has DHCP |
| Dev machine | Router | SSH to LAN IP | Works via direct ethernet or when on same subnet |
| Dev machine | Router | SSH via jump host | For remote/offline routers |

### Key constraint

If a router's upstream WiFi is disconnected and it has no WAN DHCP, it becomes unreachable except via:
1. Direct ethernet to LAN port
2. Serial console (see `docs/serial-integration-plan.md`)
3. Jump host relay through another router

## Configuration

Router inventory is in `config/routers.json`. Select a router via:

```bash
export TOLLGATE_ROUTER_ID=upstream
```

Or via `--router` CLI option on test scripts.

## Test Automation

### pytest (primary)

Set the router via environment or pymake (loads `mint-health/routers.env`):

```bash
export TOLLGATE_ROUTER_ID=upstream
# or: ./scripts/pymake.py smoke-degraded --router alpha

# API tests
pytest tests/api/ -m api

# Phone tests (requires ADB device)
pytest tests/phone/ -m phone

# Hardware scenario tests (mint health, upstream WiFi, recovery)
make lock PHASE='scenario run'
TOLLGATE_USE_HARDWARE_LOCK=1 pytest tests/scenarios/ -m hardware

# Familiar Make aliases (forward to pymake → pytest)
make smoke-degraded ROUTER=alpha
```

See [`config/make-pytest-map.yaml`](../config/make-pytest-map.yaml) for the full Make → pytest mapping.

### Test categories

| Category | Location | What it tests | Duration |
|----------|----------|---------------|----------|
| API | `tests/api/` | Backend API endpoints, CLI commands, wallet ops | ~5 min |
| Phone | `tests/phone/` | End-to-end captive portal flow via ADB | ~10 min |
| Scenario | `tests/scenarios/` | Mint health degradation, upstream WiFi, recovery | ~20 min |
| Unit | `tests/unit/` | Local unit tests for framework utilities | <1 min |

### Firmware deployment

```bash
# Build custom firmware with embedded SSH key + password
scripts/build-firmware.py --router upstream

# Flash via SSH
scripts/build-firmware.py --router upstream --flash

# Flash via U-Boot (recovery)
scripts/uboot-recover.py --image <firmware.bin>
```

## Test Scenarios

### 1. Mint health degradation

Block mint via `/etc/hosts` → restart → verify degraded mode (offline wallet, cached balance) → unblock → verify recovery. Ported from `feature/router-to-router-interaction` Makefile tests.

### 2. Startup connectivity hygiene

After power cycle, OpenWrt brings up whatever STAs have `disabled=0`. If a non-internet STA is enabled, `startupConnectivityCheck()` detects no internet and triggers emergency scan+switch.

### 3. Dead-only boot recovery

Boot with ONLY a dead STA enabled, verify emergency scan finds a disabled candidate and switches.

### 4. Upstream WiFi management

Scan, connect, list, remove upstream WiFi networks via `tollgate upstream` CLI.

### 5. Captive portal browser tests

Playwright-based tests for portal UI: cashu token input, lightning payment, degraded mode display.

## Emergency Procedures

| Problem | Fix |
|---------|-----|
| Router stranded (no internet) | SSH to LAN IP, `tollgate upstream connect <ssid> <password>` |
| DNS broken after wifi reload | `ssh root@<ip> "rm -f /etc/resolv.conf && echo nameserver 8.8.8.8 > /etc/resolv.conf"` |
| Both routers offline | Physical ethernet to LAN port, configure static IP on dev machine |
| Stale DHCP on phy0-sta0 | `ssh root@<ip> "ifconfig phy0-sta0 0.0.0.0"` then `ifup wwan` |
| Router bricked | U-Boot recovery via `scripts/uboot-recover.py` |

## Serial Console Access

See `docs/serial-integration-plan.md` for USB-TTL serial setup and `scripts/router-serial.py` for serial console automation.

Serial provides a parallel rescue/monitoring channel that works even when the router has no network connectivity.
