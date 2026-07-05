# Virtual Wi-Fi Architecture for TollGate Tests

This document separates three different kinds of coverage that are easy to
confuse:

1. **Current cloud lab captive-portal coverage over TAP/bridged Ethernet**
2. **New simulated Wi-Fi coverage over one shared `mac80211_hwsim` kernel**
3. **Physical Wi-Fi coverage on real routers and phones**

The short version: the existing GCP cloud lab is the right default for stable
TollGate package/captive-portal CI. It cannot make the Debian QEMU client scan
SSIDs created by `mac80211_hwsim` inside the OpenWrt QEMU guest. Real virtual
Wi-Fi scan/association tests need all simulated radios to live in the same Linux
kernel, either directly on the host or inside a dedicated radio-plane VM.

## Current cloud lab topology

The cloud worker launches separate QEMU guests:

- `alpha`: OpenWrt VM under test at `10.99.99.1`
- `beta` / seller: optional second OpenWrt VM at `10.99.99.11`
- `debian`: Debian client VM at `10.99.99.100`

The guests communicate through host-created TAP devices and Linux bridges:

```text
GCP host kernel

  tg-poc-br (10.99.99.2/24)
    ├── tg-poc-tap  ── virtio-net ── alpha OpenWrt br-lan (10.99.99.1)
    ├── tg-poc-tap2 ── virtio-net ── Debian client eth0 (10.99.99.100)
    └── tg-poc-tap3 ── virtio-net ── beta/seller OpenWrt br-lan (10.99.99.11)

  tg-upstream-br
    ├── tg-upst-tap-a ── alpha WAN (two-router mode)
    └── tg-upst-tap-b ── beta WAN  (two-router mode)
```

Relevant code:

- `lib/cloud_lab/worker.py:_launch_qemu()` creates QEMU guests with
  `virtio-net-pci` devices backed by host TAPs.
- `lib/cloud_lab/worker.py:setup_bridge()` creates `tg-poc-br`, TAP devices,
  NAT, and the optional upstream bridge.
- `lib/cloud_lab/worker.py:start_inner_vms()` launches alpha, beta/seller, and
  Debian.
- `tests/api/test_visual_happy_path.py` and
  `lib/clients/container.py` exercise captive portal and payment from the
  Debian client over this bridged LAN path.

This is good coverage for:

- TollGate package install and service startup on OpenWrt
- backend API and local mint behavior
- nodogsplash interception on `br-lan`
- captive portal browser/payment flow after a client is already on the LAN
- report publishing and CI orchestration

It is **not** Wi-Fi association coverage. The Debian guest sees an Ethernet
device, not an 802.11 radio.

## Why alpha guest hwsim cannot be scanned by Debian guest

`mac80211_hwsim` is a Linux-kernel-local radio simulator. Radios communicate
only when they exist in the same kernel/radio simulation domain.

A QEMU VM is not a Linux network namespace. Alpha OpenWrt has its own guest
kernel. Debian has a different guest kernel. The GCP host has a third kernel.
An hwsim PHY created inside alpha exists only inside alpha's kernel:

```text
alpha guest kernel
  mac80211_hwsim phy0/phy1  ← visible only to alpha

debian guest kernel
  eth0 virtio-net           ← cannot scan alpha's guest hwsim SSIDs

host kernel
  tg-poc-br/TAPs            ← forwards Ethernet frames, not hwsim RF
```

Host TAP/bridge networking transports Ethernet frames between virtio-net
devices. It does not transport simulated 802.11 RF or expose alpha's guest PHYs
to Debian. A host-owned hwsim PHY also cannot be assumed to pass through to a
QEMU guest as if it were USB/PCI Wi-Fi hardware. Real USB/PCI passthrough is a
separate physical-hardware technique and is not the default cloud goal.

Therefore:

- Do **not** claim Debian QEMU can scan alpha guest hwsim SSIDs in the current
  topology.
- Do **not** fake Wi-Fi by renaming Ethernet interfaces `wlan0`.
- Do **not** treat the current VM/TAP portal flow as Wi-Fi association coverage.

## Feasible architectures

### A. Keep current VM/TAP topology for portal/payment tests

**Status:** current default and recommended CI baseline.

Pros:

- Stable and already implemented.
- Tests real OpenWrt package install, UCI paths, nodogsplash, backend, local
  mints, browser/payment flow, reports, and two-router/reseller plumbing.
- Fast enough for smoke/full cloud runs.

Cons:

- Debian is connected by virtio-net Ethernet, not Wi-Fi.
- Cannot validate client scan, association, roaming, or RF behavior.

Use this for CI-stable captive portal and payment correctness.

### B. Add host-kernel hwsim with network namespaces or containers

**Status:** recommended incremental virtual Wi-Fi proof-of-concept.

All radios and clients live in one Linux kernel:

```text
shared Linux kernel (host or one radio-plane VM)

  mac80211_hwsim radios=3
    ├── phy0 → netns alpha        → hostapd SSID TollGate-ALPHA
    ├── phy1 → netns bravo        → hostapd SSID TollGate-BRAVO
    └── phy2 → netns debian-client → wpa_supplicant + DHCP + curl/browser
```

Pros:

- Real `iw scan` from the client namespace can see alpha/bravo SSIDs.
- Real association and DHCP can be tested.
- Does not disturb the stable OpenWrt VM cloud lab.

Cons:

- Initially tests representative AP/client behavior, not full OpenWrt guest
  package behavior.
- Running full OpenWrt userspace in namespaces is possible to investigate, but
  UCI/init/firewall/nodogsplash semantics may be brittle.

Use this for opt-in radio-plane validation.

### C. Dedicated radio-plane VM

**Status:** recommended if host-kernel privileges are too risky for CI workers.

Boot one Linux VM and run all hwsim radios/namespaces inside that VM. This keeps
radio-plane experiments isolated from the GCP host while preserving the
single-kernel requirement.

Pros:

- Cleaner isolation than running hwsim directly on the GCP host.
- Still supports real scan/association because all namespaces share one kernel.

Cons:

- More orchestration and snapshot work.
- Still separate from the authoritative OpenWrt package VMs unless full OpenWrt
  userspace is later containerized inside the radio-plane VM.

### D. Real Wi-Fi hardware passthrough

**Status:** physical-lab option only.

Use USB/PCI Wi-Fi adapters with passthrough or real routers/phones. This is the
right place for real RF quirks, channel behavior, Android captive portal UX, and
deauth/reconnect behavior.

### E. Cross-VM virtual 802.11 passthrough

**Status:** research/prototype only.

Do not plan on this unless a supported QEMU Wi-Fi device/model or a proven
mac80211 forwarding mechanism is identified. TAP/virtio-net is not that
mechanism.

Research directions that may be worth a future spike, but are not the default
implementation path:

- `wmediumd`: user-space medium simulator commonly used with hwsim testbeds.
- `welled`: VMCI/vsock-style cross-VM frame relay research.
- `vwifi`: separate virtual Wi-Fi driver work aimed at VM-to-VM simulation.

These are intentionally out of the first POC because they add another network
emulation layer and still need proof before they can be trusted in CI.

## Recommended implementation path

1. Keep the existing VM/TAP cloud lab unchanged as the default.
2. Add explicit flags/env for a separate virtual Wi-Fi plane:
   - `TOLLGATE_WIFI_PLANE=hwsim-netns`
   - future CLI flag: `--wifi-plane=hwsim-netns`
3. Add a small POC orchestrator that runs only when explicitly enabled.
4. First prove RF basics:
   - load `mac80211_hwsim radios=3`
   - create namespaces `tg-vwifi-alpha`, `tg-vwifi-bravo`, `tg-vwifi-client`
   - move one PHY into each namespace
   - run hostapd in alpha/bravo with SSIDs `TollGate-ALPHA` and
     `TollGate-BRAVO`
   - run `iw scan` from the client namespace and assert both SSIDs are visible
   - associate client to alpha, obtain DHCP, curl alpha captive endpoint
   - associate client to bravo, obtain DHCP, curl bravo captive endpoint
5. Only after that works, investigate whether full OpenWrt userspace can run in
   containers/namespaces. If it is brittle, keep:
   - OpenWrt VM/TAP lab as package/captive portal authority
   - hwsim-netns lab as Wi-Fi scan/association authority
   - physical lab as final real RF authority

## vwifi cross-VM WiFi frame relay

### Overview

[vwifi](https://github.com/Raizo62/vwifi) relays 802.11 frames between QEMU
VMs via vsock/TCP. This enables real `iw scan` from a Debian QEMU guest to see
SSIDs hosted on the OpenWrt QEMU guest — solving the cross-kernel hwsim
limitation described above.

### Architecture

```text
GCP Host VM
  ├── vwifi-server (background process)
  │     Listens for vsock connections from guests
  │     Relays 802.11 frames between connected guests
  │
  ├── Alpha OpenWrt QEMU (guest-cid=10)
  │     ├── vhost-vsock-pci device
  │     ├── mac80211_hwsim radios=0 (empty)
  │     ├── vwifi-add-interfaces 1 → creates wlan0 PHY
  │     ├── vwifi-client → connects to host vwifi-server via vsock
  │     └── hostapd on wlan0 → SSID TollGate-ALPHA
  │
  └── Debian QEMU (guest-cid=20)
        ├── vhost-vsock-pci device
        ├── mac80211_hwsim radios=0 (empty)
        ├── vwifi-add-interfaces 1 → creates wlan0 PHY
        ├── vwifi-client → connects to host vwifi-server via vsock
        └── iw wlan0 scan → sees TollGate-ALPHA ✓
```

### Usage

```bash
# Enable vwifi cross-VM WiFi relay
./scripts/cloud-lab.py submit --pr 42 --vwifi --publish

# Combined with hwsim (vwifi implies hwsim-like behavior)
./scripts/cloud-lab.py submit --pr 42 --vwifi --publish
```

### What works with vwifi

- **STA scan**: Debian guest `iw scan` sees `TollGate-ALPHA` SSID from OpenWrt
- **STA association**: Client can associate with the AP (depends on hostapd)
- **Real 802.11 frame relay**: Actual management and data frames cross VMs

### What still needs verification

- DHCP after association (requires hostapd + dnsmasq on the vwifi interface)
- Full captive portal flow over vwifi (requires nodogsplash on vwifi interface)
- Performance under load

### Build requirements

Host needs: `cmake make g++ pkg-config libnl-3-dev libnl-genl-3-dev`

Guest binaries are built statically for x86_64 Linux, working on both Debian
(glibc) and OpenWrt (musl). The build script is `scripts/build-vwifi.sh`.

### Snapshot baking

`scripts/bake-snapshot.py` builds vwifi from source and installs binaries to
`/opt/vwifi/bin/` (host), `/opt/vwifi/bin/debian/` (Debian guest), and
`/opt/vwifi/bin/openwrt/` (OpenWrt guest) during snapshot creation. It also
loads the `vhost_vsock` kernel module.

### Code paths

- `scripts/cloud-lab.py --vwifi` → sets metadata `tollgate-vwifi=true`
- `lib/cloud_lab/gcp.py:submit_run()` → passes vwifi metadata
- `lib/cloud_lab/worker.py:load_config_from_metadata()` → sets `vwifi_enabled`
- `lib/cloud_lab/worker.py:_setup_vwifi_host()` → starts vwifi-server on host
- `lib/cloud_lab/worker.py:start_inner_vms()` → adds vsock devices to QEMU VMs
- `lib/cloud_lab/worker.py:_setup_hwsim_wifi()` → loads hwsim radios=0 (empty)
- `lib/cloud_lab/worker.py:_setup_vwifi_guests()` → installs vwifi-client, starts relay
- `tests/api/test_mac80211_hwsim.py` → STA tests run when VWIFI_ENABLED=1

## Local POC command

Run only on a disposable Linux machine or VM with root privileges:

```bash
sudo TOLLGATE_WIFI_PLANE=hwsim-netns \
  python3 scripts/hwsim-netns-poc.py run --json
```

The pytest wrapper is skipped unless explicitly enabled:

```bash
sudo TOLLGATE_WIFI_PLANE=hwsim-netns \
  python3 -m pytest tests/api/test_virtual_wifi_hwsim_netns.py -v
```

## Acceptance criteria

The hwsim-netns profile may claim virtual Wi-Fi coverage only when tests prove:

- client namespace `iw scan` sees `TollGate-ALPHA`
- client namespace `iw scan` sees `TollGate-BRAVO`
- client associates with alpha
- client obtains DHCP on alpha's WLAN
- client curls alpha's captive endpoint
- client associates with bravo
- client obtains DHCP on bravo's WLAN
- client curls bravo's captive endpoint

Payment/auth coverage over this radio plane is a later phase. Until TollGate or
OpenWrt-equivalent userspace runs in the radio plane, the existing VM/TAP cloud
lab remains the authoritative payment/captive-portal package test.
