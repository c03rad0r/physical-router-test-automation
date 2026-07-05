# Virtual Lab on Ubuntu host `218`

Local QEMU-based TollGate test lab for running API tests, Linux client captive
portal tests, and multihop router-to-router tests without physical hardware.

## Current topology

```text
Ubuntu host 218 (192.168.13.218, internet via wlp4s0)
├── tg-poc-br (Linux bridge, 192.168.1.2/24)
│   ├── tg-poc-tap → OpenWrt VM eth0 (br-lan, 192.168.1.1)
│   └── tg-poc-dc0 → tg-poc-client container (192.168.1.100)
├── OpenWrt VM (KVM, x86_64, 256MB RAM)
│   ├── Serial console: $workdir/run/serial.sock
│   ├── QEMU monitor: $workdir/run/monitor.sock
│   ├── SSH: root@192.168.1.1 (password: tollgate)
│   ├── Internet via host NAT (gateway 192.168.1.2)
│   └── nodogsplash + curl + socat + jq + luci installed
└── tg-poc-client (Docker: debian:bookworm-slim)
    ├── curl, iputils-ping, iproute2
    ├── veth wired into tg-poc-br
    └── default gateway: 192.168.1.1 (OpenWrt VM)
```

The client sits behind the OpenWrt VM's LAN so nodogsplash firewall behavior is
real. The host NATs traffic from the 192.168.1.0/24 subnet to the internet so the
VM can `opkg install` packages.

## Quick start

From this Mac:

```bash
# 1. Check host readiness
python3 scripts/virtual-lab.py doctor --host 218

# 2. Install QEMU/KVM tools on 218 (first time only)
python3 scripts/virtual-lab.py install-deps --host 218

# 3. Download OpenWrt x86_64 image (first time only)
python3 scripts/virtual-lab.py prepare-image --host 218

# 4. Start the full POC environment (VM + provisioning + container)
python3 scripts/virtual-lab.py start-poc --host 218

# 5. Verify connectivity
python3 scripts/virtual-lab.py smoke-poc --host 218

# 6. Check status
python3 scripts/virtual-lab.py status-poc --host 218

# 6b. Run virtualizable reseller-mode scenarios once a seller router is exposed
python3 scripts/virtual-lab.py run-reseller-scenarios --host 218 --secondary-router-host <seller-ip>

# 7. Clean up
python3 scripts/virtual-lab.py stop-poc --host 218
```

The `start-poc` command does everything:
1. Creates bridge + tap + assigns host bridge IP
2. Handles route conflicts (moves 192.168.1.0/24 from physical iface to bridge)
3. Sets up iptables NAT/FORWARD for VM internet access
4. Boots the OpenWrt VM with serial console Unix socket
5. Provisions the VM via serial console (root password, SSH, firewall rules, gateway/DNS)
6. Verifies SSH login
7. Starts Debian container, installs packages, wires into bridge

## How it works

### Serial console provisioning

The VM boots with `-serial unix:$workdir/run/serial.sock,server,nowait`. A Python
script connects to this Unix socket, waits for "Please press Enter to activate this
console", then sends provisioning commands:

- Set root password via `printf '%s\n%s\n' 'pw' 'pw' | passwd root` (BusyBox has no chpasswd)
- Enable dropbear password auth
- Add WAN SSH firewall rule
- Configure gateway (192.168.1.2) and DNS (8.8.8.8) for internet access

This all happens over the serial socket, no pre-baked images or custom firmware needed.

### Debian client container

The container starts on Docker's default bridge (has internet for `apt install`),
installs curl/ping/iproute2, then disconnects from Docker's network and gets wired
into `tg-poc-br` via a veth pair. The container's default gateway points to the
OpenWrt VM, so nodogsplash can intercept its HTTP traffic.

### Route conflict handling

Host 218's `enp5s0` has `192.168.1.0/24` from a previous physical setup. The
`start-poc` script detects this conflict and moves the route to `tg-poc-br`.

### VM internet access

The host MASQUERADEs traffic from `192.168.1.0/24` going out any interface except
`tg-poc-br`. FORWARD rules allow traffic between the bridge and the internet-facing
interface. The VM uses the host bridge IP (192.168.1.2) as its gateway.

## What this validates

- API tests via SSH + HTTP (all 31 `tests/api/` tests)
- Linux captive portal detection and interaction
- nodogsplash firewall behavior
- TollGate session management
- Token payment flow via curl
- Router-to-router / multihop scenarios (future)
- Virtualizable reseller-mode behavior when run with
  `TOLLGATE_ENABLE_RESELLER_SCENARIOS=1` or
  `scripts/virtual-lab.py run-reseller-scenarios`

## What still requires physical hardware

- Android captive portal notification behavior
- ADB UI automation
- Mobile-data fallback behavior
- Real WiFi association/deauth quirks (RSSI, signal strength, interference)
- Device-specific OpenWrt target/kernel/package issues (mipsel, aarch64)

## What works in virtual environments

- **WiFi site scan** via `mac80211_hwsim` kernel module (`tests/api/test_mac80211_hwsim.py`)
  - Virtual radios with `iw list`, `iw scan`, AP bringup
  - x86_64 only (cloud lab, local QEMU)
- API tests, captive portal via container client, payment flow, DNS
- Router-to-router / multihop scenarios (two-router cloud)

## Framework integration

The virtual lab integrates with the existing pytest framework:

- `Router` class now accepts `port` parameter for non-standard SSH ports
- `deploy.py` SCP commands support `-P` port flag
- `conftest.py` reads `sshPort`/`TOLLGATE_SSH_PORT` from inventory
- Container client mode (`--client=container`) available for virtual lab tests
- Virtual lab marker: `@pytest.mark.virtual_lab`

## Target topology (full lab)

```text
Ubuntu host 218
├── OpenWrt VM: seller
│   ├── SSH: 127.0.0.1:2201
│   ├── TollGate backend on 2121
│   └── LAN bridge to reseller WAN
├── OpenWrt VM: reseller
│   ├── SSH: 127.0.0.1:2202
│   ├── TollGate backend on 2121
│   ├── nodogsplash on client-facing LAN
│   └── WAN bridge to seller LAN
└── Debian client container
    ├── attached to reseller LAN
    ├── curl/ping for connectivity checks
    └── Chromium for captive portal browser flow
```

The reseller-mode tests are intentionally split into two groups:

- `tests/scenarios/test_reseller_mode.py` uses only SSH/UCI/CLI/DNS-blocking
  operations and is designed to run on physical routers, the on-prem QEMU lab,
  and the GCP cloud lab.
- **Virtual WiFi scanning** works via `mac80211_hwsim` — see
  `tests/api/test_mac80211_hwsim.py`. The kernel module creates virtual radios
  that support `iw list`, `iw scan`, and AP bringup. Available on x86_64 targets
  (cloud lab, local QEMU). Real RSSI, association quirks, and emergency scan still
  require physical radio hardware.

GCP runs can opt into the virtualizable scenario tier with:

```bash
./scripts/cloud-lab.py submit --pr 122 --publish --reseller-scenarios \
  --secondary-router-host <seller-ip-reachable-from-reseller>
```

`--reseller-scenarios` intentionally fails fast unless a secondary/seller router
is configured. This prevents a green cloud/on-prem report that skipped the core
router-to-router assertions.

## Files

| File | Purpose |
|---|---|
| `scripts/virtual-lab.py` | VM orchestration: doctor, install-deps, prepare-image, start-poc, stop-poc, status-poc, smoke-poc, poc |
| `config/routers.virtual.example.json` | Inventory template for virtual seller/reseller |
| `tests/api/test_virtual_lab_poc.py` | POC test: container reaches gateway |
| `tests/api/test_virtual_lab_integration.py` | Integration tests: captive portal, curl, DNS |
| `lib/clients/container.py` | Container client adapter for pytest |
| `docs/virtual-lab.md` | This file |

## Constants

| Constant | Value |
|---|---|
| Bridge | `tg-poc-br` |
| TAP | `tg-poc-tap` |
| Container | `tg-poc-client` |
| Gateway (VM) | `192.168.1.1` |
| Host bridge IP | `192.168.1.2/24` |
| Container IP | `192.168.1.100/24` |
| VM password | `tollgate` |
| Subnet | `192.168.1.0/24` |
