# FIPS v0.4 Testing Plan

Systematic testing plan covering: protocol validation (cloud), BLE transport
(hardware), and integration with the TollGate testing ecosystem.

## Architecture: integrate, don't merge

```
                    ┌──────────────────────────────────────────┐
                    │        GCP Shared Infrastructure          │
                    │  ┌─────────────────────────────────────┐ │
                    │  │ Baked image: Docker + Rust + fips   │ │
                    │  │ + TollGate deps + nak CLI           │ │
                    │  └─────────────────────────────────────┘ │
                    │                                          │
                    │  ┌──────────────┐  ┌──────────────────┐ │
                    │  │ OpenWrt QEMU │  │ Debian QEMU      │ │
                    │  │ TollGate.ipk │  │ TollGate + fips  │ │
                    │  │ fips.apk     │  │ daemon running   │ │
                    │  └──────────────┘  └──────────────────┘ │
                    │         ↕ fips mesh peering ↕            │
                    └──────────────────────────────────────────┘
                                     │
                    ┌────────────────┼────────────────┐
                    │                │                │
              ┌─────┴─────┐  ┌──────┴──────┐  ┌──────┴──────┐
              │ fips-cloud │  │ phys-router │  │  fips-lab   │
              │ -lab       │  │ -test-auto  │  │ (hardware)  │
              │            │  │             │  │             │
              │ Chaos mesh │  │ TollGate    │  │ BLE echo    │
              │ Interop    │  │ Captive     │  │ Throughput  │
              │ Throughput │  │ portal      │  │ ESP32 peering│
              │            │  │ LuCI UI     │  │             │
              └──────┬─────┘  └──────┬─────┘  └──────┬─────┘
                     │               │               │
                     └───────┬───────┘               │
                             │                       │
                    ┌────────┴────────┐              │
                    │ Blossom + Nostr │              │
                    │ Publishing      │              │
                    │ (shared npub)   │              │
                    └────────┬────────┘              │
                             │                       │
                    ┌────────┴────────┐              │
                    │ Dashboard SPA   │◀─────────────┘
                    │ (#t tag filter) │
                    │ fips-* | test-* │
                    └─────────────────┘
```

### Repos stay separate, integration points are explicit

| Repo | Role | Changes needed |
|------|------|----------------|
| **physical-router-test-automation** | TollGate testing | Add fips .apk install to cloud-lab worker; add fips mesh connectivity tests between OpenWrt↔Debian VMs |
| **fips-cloud-lab** | fips protocol testing (chaos, interop, throughput) | Already working; share GCP image base with tollgate-runner-baked |
| **fips-lab** | fips BLE hardware testing (labgrid, ESP32) | Already working; publish results to same Nostr npub |
| **fips-comparison** | Source tree comparison | Retire — replaced by fips-cloud-lab interop mode |
| **hackathon-tooling** | Shared code (publishers, DVM, patterns) | Already shared |

### Shared infrastructure

1. **GCP baked image**: Merge `fips-cloud-lab-baked` and `tollgate-runner-baked-v16` into one image with both fips + TollGate deps. Saves cost (one snapshot vs two).

2. **Blossom + Nostr**: Both projects publish to npub `9a515b0f...` with different `#t` tags:
   - `fips-test`, `fips-interop`, `fips-rekey`, `fips-throughput` → fips-cloud-lab
   - `fips-ble`, `fips-benchmark` → fips-lab
   - `test-run`, `benchmark` → physical-router-test-automation
   - Dashboard filters by tag

3. **Cloud-lab worker**: physical-router-test-automation's cloud-lab already boots OpenWrt QEMU + Debian QEMU. Add:
   - Install fips .apk on OpenWrt VM
   - Install fips binary on Debian VM
   - Start fips daemon on both
   - Verify mesh peering: `fipsctl show peers` on both VMs
   - Ping test: Debian VM pings OpenWrt VM over fips0 interface

---

## Testing phases

### Phase 0: Build verification (v0.4.0 rebase)

**Goal:** Confirm the rebased BLE code compiles on the v0.4.0 codebase.

**Platform:** Local macOS + GCP Linux VM

**Steps:**
1. `cargo build --release` — Linux (BlueZ BLE)
2. `cargo build --release --features ble-macos` — macOS (CoreBluetooth)
3. Verify fips binary starts with BLE transport configured
4. Verify all transports compile (UDP, TCP, Ethernet, Tor, Nym, BLE)

**Pass criteria:** Zero compile errors. Binary starts and initializes BLE adapter.

**Estimated time:** 10 minutes per platform.

---

### Phase 1: Unit test pass (59 tests)

**Goal:** All existing BLE unit tests pass on v0.4.0.

**Platform:** Local (no hardware needed)

**Steps:**
```bash
cargo test --features ble transport::ble::     # BLE transport unit tests
cargo test --features ble node::tests::ble     # Node integration tests
```

**Test inventory (from codebase survey):**
| Module | Tests | What they cover |
|--------|-------|-----------------|
| io.rs | 11 | MockBleIo streams, MTU, listen, connect, scan, accept |
| discovery.rs | 6 | Peer dedup, add/take, transport_addr format |
| psm.rs | 6 | PSM codec, learn/lookup, resolve, clear |
| pool.rs | 7 | Eviction, static/non-static, MTU, replace |
| rate_limit.rs | 9 | Token bucket, BBR adaptation, clamps |
| addr.rs | 8 | Address parsing, formatting, roundtrip |
| android_io.rs | 4 | Inbound channel, outbound send, PSM scan |
| mod.rs | 8 | Transport state, scan, dedup, auto_connect |
| node/tests/ble.rs | 4 | Two-node handshake, three-node chain, mixed transport |

**Pass criteria:** 59/59 tests pass.

**Risk areas:** The rebase may have changed function signatures in node/mod.rs or dispatch.rs that the BLE tests depend on. If tests fail, the failure messages will show what needs updating.

---

### Phase 2: Two-platform BLE smoke test

**Goal:** Verify real BLE peering works between macOS and Linux on v0.4.0.

**Platform:** MacBook (macOS, CoreBluetooth) + Linux box (BlueZ)

**Steps:**
1. Build fips on both platforms with BLE enabled
2. Configure both with BLE transport: `adapter: "hci0"`, `advertise: true`, `scan: true`, `auto_connect: true`
3. Start fips daemon on both
4. Wait for discovery (scan → probe → connect → handshake)
5. Verify: `fipsctl show peers` shows the other node
6. Verify: `fipsctl show status` shows mesh converged
7. Ping test: `fipsctl ping <peer-npub>` or `ping6 <peer>.fips`

**Metrics to capture:**
- Time to first peer discovered (scan → result)
- Time to connection established (probe → connected)
- Time to handshake complete (connected → peered)
- First packet RTT

**Pass criteria:** Both nodes peer within 60 seconds. Ping succeeds.

**Known risk:** macOS CFRunLoop issue — bluest L2CAP reads hang without NSRunLoop pumping. The io_macos.rs code uses notification-based event handling to work around this, but it needs verification on v0.4.0.

---

### Phase 3: Echo RTT benchmark

**Goal:** Measure BLE L2CAP CoC round-trip latency across payload sizes.

**Platform:** Via fips-lab (labgrid) or manual `fipsctl benchmark echo`

**Configuration:**
```python
ECHO_PAYLOAD_SIZES = [0, 32, 64, 128, 256]
ECHO_COUNT = 20
ECHO_MAX_MEDIAN_MS = 500
```

**Test matrix:**
| Pair | Direction | Expected |
|------|-----------|----------|
| Linux → macOS | Outbound | median < 500ms, loss ≤ 2% at ≤64B |
| macOS → Linux | Inbound | median < 500ms, loss ≤ 2% at ≤64B |
| Linux → ESP32 | Outbound | median < 500ms, loss ≤ 2% at ≤64B |
| macOS → ESP32 | Outbound | (new — not tested before) |
| ESP32 → Linux | Inbound | (xfail — ESP32 cannot initiate) |

**Known issues:**
- Issue #133: payloads ≥128B may have loss (L2CAP segmentation). Document loss rate, don't assert zero.
- macOS central role is 4x slower than peripheral (40kbps vs 110kbps).

**Pass criteria:**
- Payloads 0-64B: loss ≤ 2%, median < 500ms
- Payloads 128-256B: document loss rate (issue #133 regression check)

---

### Phase 4: Throughput + rate limiter

**Goal:** Measure sustained BLE throughput and verify rate limiter behavior.

**Platform:** Linux ↔ macOS (or Linux ↔ ESP32)

**Configuration:**
```python
THROUGHPUT_FRAME_SIZES = [20, 50, 100]
THROUGHPUT_DURATION = 5  # seconds
THROUGHPUT_RATE = 30000  # 30kbps target
```

**Test scenarios:**
| Scenario | Target Rate | Duration | Expected |
|----------|-------------|----------|----------|
| Conservative | 25 kbps | 5 min | Stable, no drops |
| Comfortable | 50 kbps | 5 min | Stable (MAX_RATE_BPS) |
| Aggressive | 75 kbps | 3 min | Rate limiter kicks in |
| Stress | 100 kbps | 5 min | Connection may drop (~5min ceiling) |

**Metrics to capture:**
- Actual achieved throughput (bps)
- Packets sent vs received (loss %)
- Rate limiter interventions (token bucket depletion count)
- Queue depth over time (macOS: BLE_PERIPHERAL_QUEUE_BYTE_CAP = 65536)
- Connection survival time at each rate

**Pass criteria:**
- 25kbps: 100% delivery, no connection drops for 5 minutes
- 50kbps: ≥95% delivery, no connection drops for 5 minutes
- Rate limiter engages at ≤50kbps ceiling

---

### Phase 5: Connection stability + reconnection

**Goal:** Verify long-duration stability and recovery from disruption.

**Platform:** Linux ↔ macOS

**Test scenarios:**

**5a: Long-duration stability**
- 30-minute continuous connection at 30kbps
- Measure: connection uptime, total bytes transferred, any silent stalls
- Pass: connection survives 30 minutes without manual intervention

**5b: Service restart reconnection**
- Kill fips daemon on one peer
- Restart after 5 seconds
- Measure: time to re-scan, re-probe, re-connect, re-handshake
- Pass: reconnection within 30 seconds (improvement target: <10s)
- Known xfail: fips-lab marks this as >5s (not yet fixed)

**5c: BLE adapter power cycle**
- `sudo hciconfig hci0 down` on Linux
- Wait 10 seconds
- `sudo hciconfig hci0 up`
- Measure: time to re-detect adapter, re-advertise, re-scan, re-connect
- Pass: auto-recovery within 60 seconds

**5d: Out-of-range recovery**
- Walk one device out of BLE range (>10m or behind wall)
- Wait 30 seconds (connection drops)
- Walk back into range
- Measure: time to re-discover, re-connect
- Pass: auto-reconnection within 60 seconds of being back in range

---

### Phase 6: Multi-peer BLE mesh

**Goal:** Verify BLE connection pool, multi-peer peering, and eviction.

**Platform:** 3+ BLE devices (Linux + macOS + ESP32)

**Test scenarios:**

**6a: Three-peer mesh**
- 3 devices all advertising + scanning + auto-connecting
- Verify: each device has 2 BLE peers
- Verify: ping works between all pairs (via multi-hop if needed)
- Measure: per-peer throughput when sharing radio time

**6b: Connection pool eviction**
- Fill pool to max (7 connections on Linux)
- Introduce 8th peer
- Verify: pool evicts oldest non-static connection
- Verify: evicted peer's session cleaned up properly
- Measure: eviction → new connection handoff time

**6c: Mixed transport**
- Device A: BLE + UDP
- Device B: BLE only
- Device C: UDP only
- Verify: A peers with B (BLE) and C (UDP)
- Verify: B can reach C via A (multi-hop over mixed transport)
- This is already tested in node/tests/ble.rs with MockBleIo — verify on real hardware

---

### Phase 7: WiFi coexistence / interference

**Goal:** Measure BLE performance degradation under WiFi interference.

**Platform:** Linux (BLE + WiFi) ↔ macOS (BLE only)

**Test setup:**
1. Start BLE echo benchmark (payload 64B, continuous)
2. Start iperf3 UDP flood on 2.4GHz WiFi channel overlapping BLE
3. Measure: BLE throughput degradation, packet loss increase, RTT increase

**Test matrix:**
| WiFi State | BLE Expected Throughput | BLE Expected Loss |
|------------|------------------------|-------------------|
| WiFi off | 25-50 kbps | < 2% |
| WiFi idle | 20-45 kbps | < 5% |
| WiFi active (iperf3 10Mbps UDP) | 10-30 kbps | 5-15% |
| WiFi + microwave (real interference) | Variable | Variable |

**Metrics:**
- BLE throughput before/after WiFi activation
- BLE packet loss rate under WiFi load
- BLE RTT increase under WiFi load
- BLE connection stability (any drops?)

**Research baseline:** Nordic nRF7002 testing shows BLE drops from 478kbps → 145kbps without coexistence PTA. With PTA, BLE stays at 478kbps. Desktop OS BLE stacks (BlueZ, CoreBluetooth) handle coexistence automatically.

---

### Phase 8: Fips mesh integration (with TollGate)

**Goal:** Verify fips mesh carries real application traffic between TollGate VMs.

**Platform:** physical-router-test-automation cloud-lab (GCP nested-virt)

**Test setup:**
1. Cloud-lab boots: OpenWrt QEMU + Debian QEMU (existing)
2. Install fips .apk on OpenWrt VM
3. Install fips binary on Debian VM
4. Start fips daemon on both with auto-connect
5. Verify mesh peering: `fipsctl show peers` on both
6. Ping: Debian VM → `ping6 <openwrt-npub>.fips`
7. HTTP: Debian VM → `curl http://<openwrt-npub>.fips/` (TollGate LuCI)
8. Payment: TollGate payment flow over fips mesh

**This is the integration point** where physical-router-test-automation and fips testing converge. The cloud-lab VMs run both TollGate and fips. Tests verify that the fips mesh carries TollGate traffic end-to-end.

---

## Tools and infrastructure

### BLE-specific tools

| Tool | Source | Purpose |
|------|--------|---------|
| fipsctl benchmark echo | fips daemon | RTT measurement per payload size |
| fipsctl benchmark throughput | fips daemon | Sustained throughput measurement |
| fipsctl show peers/tree/stats | fips daemon | Mesh state inspection |
| ble_spike.rs | fips testing/ble/ | Manual BLE API validation (listen/connect/sink/throughput) |
| fips-decrypt | microfips | Decrypt Noise traffic from pcap captures |
| fips_dissector.lua | microfips tools/ | Wireshark/tshark FMP frame dissector |
| btsnoop_decrypt.py | fips-lab | BLE L2CAP payload decryption |
| btmon capture | fips-lab | BlueZ monitor capture |
| tshark + RSSI | fips-lab | BLE statistics + signal strength |

### Cloud testing tools

| Tool | Source | Purpose |
|------|--------|---------|
| fips-cloud-lab submit.py | fips-cloud-lab | GCP VM lifecycle |
| fips-cloud-lab worker.sh | fips-cloud-lab | Docker chaos scenario runner |
| fips-cloud-lab worker-interop.sh | fips-cloud-lab | Mixed-version interop testing |
| fips-cloud-lab visualize.py | fips-cloud-lab | Topology GIF, charts, HTML report |
| fips-cloud-lab publish.py | fips-cloud-lab | Blossom upload + Nostr events |
| fips-cloud-lab dashboard.html | fips-cloud-lab | Reader SPA (Cloudflare Pages) |

### Shared infrastructure

| Resource | Current | Proposed |
|----------|---------|----------|
| GCP baked image | fips-cloud-lab-baked (Docker+Rust) | Merge with tollgate-runner-baked (add TollGate deps) |
| Blossom server | blossom.psbt.me | Keep (or switch to blossomflare) |
| Nostr relays | relay.cashu.email + nos.lol | Keep |
| Nostr npub | 9a515b0f... (shared tollgate+fips) | Keep |
| Dashboard | fips-cloud-lab.pages.dev | Expand to show both fips + tollgate runs |
| Cloud-lab | physical-router-test-automation/lib/cloud_lab | Add fips .apk install to provision step |

---

## fips-comparison: retire

The `fips-comparison` directory (two side-by-side source trees: amperstrand-fips + jmcorgan-fips) is no longer needed. Its purpose — comparing our fork against upstream — is now served by:
- `git log upstream/master..ai-experiments` — shows exactly what we changed
- `fips-cloud-lab --mode interop` — builds both versions and tests interop
- The rebased ai-experiments branch IS the comparison

**Action:** Archive or delete `fips-comparison/`.

---

## Schedule

| Phase | Duration | Prerequisites | Priority |
|-------|----------|---------------|----------|
| 0. Build verification | 10 min | Rebase complete (done) | Immediate |
| 1. Unit tests | 5 min | Phase 0 passes | Immediate |
| 2. Two-platform smoke | 30 min | Phase 1 passes, both devices available | High |
| 3. Echo RTT | 1 hour | Phase 2 passes | High |
| 4. Throughput + rate limiter | 2 hours | Phase 3 passes | High |
| 5. Stability + reconnection | 4 hours | Phase 4 passes | Medium |
| 6. Multi-peer mesh | 2 hours | 3+ BLE devices | Medium |
| 7. WiFi coexistence | 1 hour | Phase 4 passes | Low |
| 8. TollGate integration | 2 hours | Cloud-lab + fips .apk ready | When TollGate integration planned |
