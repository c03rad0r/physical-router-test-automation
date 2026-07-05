# Portal Test Findings — GL-MT3000 Physical Router

Date: 2025-06-02
Router: GL.iNet GL-MT3000, OpenWrt 24.10.2, aarch64_cortex-a53, IP `10.171.103.1`
Backend: Go v1 (tollgate-module-basic-go)
Phone: Samsung SM-G991B (Galaxy S21 5G), Android 14, ADB serial `R5CR508MD9R`
SSID: `TollGate-101B` (open, no password)

---

## 1. Cashu Token Version Support Matrix

### Token format versions

| Version | Prefix | Encoding | Example Length | Go Backend | Rust Backend |
|---------|--------|----------|----------------|------------|--------------|
| V1 | `cashuA` | Base64 JSON | ~120 chars | **Works** | Unknown |
| V3 | `cashuAeyJ` | Base64 JSON | ~378 chars | **Works** | Unknown |
| V4 | `cashuB` | Binary CBOR | ~350 chars | **Rejected** ("invalid V3 token") | Unknown |

### What was tested

| Token | Mint | Version | Amount | Result | Notes |
|-------|------|---------|--------|--------|-------|
| 8-sat token | `testnut.cashu.exchange` | V3 | 8 sats | **Accepted** via portal UI + `pay_direct` | 378 chars, typed via ADB in 80-char chunks |
| 4-sat token | `mint.chorus.community` | V4 | 4 sats | **Rejected** — `"Invalid cashu token: invalid token: invalid V3 token"` | Go backend tries to parse all `cashuA`/`cashuB` as V3 |

### Go backend (gonuts) token parsing

The Go backend uses `gonuts` which only implements V1/V3 token parsing. When it receives a V4 token (`cashuB` prefix):

1. Base64-decodes the payload
2. Attempts JSON deserialization (V3 format)
3. Fails because V4 uses CBOR, not JSON
4. Returns `kind:21023` error with `code: payment-error-invalid-token`

The error message says "invalid V3 token" because the parser assumes all tokens are V3 — it doesn't distinguish between versions.

### Keyset ID compatibility (separate from token version)

| Format | Prefix | Length | Example | Go Backend | Notes |
|--------|--------|--------|---------|------------|-------|
| V1 keyset ID | `00` | 8 bytes (16 hex chars) | `0016f5fb5e5278f2` | **Required** | gonuts only supports V1 |
| V2 keyset ID | `01` | 33 bytes (66 hex chars) | `01df97b6...` | **Crash** | Fatal on startup |

V2 keyset IDs are a **separate issue** from V4 tokens. The Go backend crashes if configured with a mint that returns V2 keyset IDs (e.g., CDK mints). Only `testnut.cashu.exchange` (V1 keysets) works with Go.

### Action items

- [x] File issue on `tollgate-module-basic-go`: Go backend rejects V4 Cashu tokens (`cashuB` prefix) → [#21](https://github.com/Amperstrand/tollgate-module-basic-go/issues/21)
- [x] File issue on `tollgate-module-basic-go`: Mint fee causes allotment shortfall → [#20](https://github.com/Amperstrand/tollgate-module-basic-go/issues/20)
- [x] File issue on `tollgate-module-basic-go`: Profit-share zero-amount payout crash → [#22](https://github.com/Amperstrand/tollgate-module-basic-go/issues/22)
- [x] Test V3 token from `mint.coinos.io` (after V4→V3 conversion) — **works via pay_direct**
- [ ] Test coinos token through captive portal UI end-to-end
- [ ] Test V4 token against Rust backend for comparison
- [ ] Consider whether accepted_mints should be restricted to mints that return V1 keysets when running Go backend

---

## 2. Captive Portal Flow

### Happy path (confirmed working)

1. Phone connects to TollGate-101B (open WiFi)
2. Nodogsplash intercepts HTTP traffic (port 2050)
3. Portal HTML renders in browser with token input field
4. User types/pastes Cashu token
5. Portal validates token client-side ("Valid Cashu token, 8 sats")
6. "Pay 8 sat to get 80.00 MB" button becomes active
7. User taps Purchase
8. Backend accepts token, returns kind:1022 event
9. Nodogsplash authenticates client MAC
10. Phone has internet access (ping 1.1.1.1 = 40ms, loads example.com)
11. Data metering tracks usage: `566.0 KB / 70.0 MB (0.8%)`

### Native captive portal popup (Android "Sign in to network")

**Status: Unreliable, not used by the framework.**

The framework (`lib/clients/wifi.py`) does NOT rely on Android's native captive portal detection. Instead it:

1. `reset_state()` — deauth client, restart NDS + backend
2. `wifi._connect_to_wifi()` — disconnect WiFi, reconnect to TollGate SSID
3. `_open_portal_on_phone()` — **manually opens portal URL in browser** via `am start -a android.intent.action.VIEW`

This is the correct approach because:

- Android caches network validation results. Once a network is validated as "has internet", Android may not re-check for minutes even after the client is deauthenticated
- The native popup timing varies by OEM (Samsung, Pixel, etc.)
- Some Android versions don't show the popup at all for open networks
- The framework's approach is deterministic and fast

**Attempted but not completed**: Testing the native popup by toggling WiFi off/on via `svc wifi disable`/`svc wifi enable`. The phone reconnected to the home network ("2") instead of TollGate-101B, then ADB connection was lost. The framework avoids this by explicitly connecting to the TollGate SSID via `cmd wifi connect-network` (requires shell-level permissions) or the Android WiFi settings API.

### Token input methods

| Method | Works? | Notes |
|--------|--------|-------|
| ADB `input text` (80-char chunks, 0.3s delay) | **Yes** | Framework approach. Silent truncation at ~200 chars without chunking |
| ADB `input text` (full token, no chunking) | **No** | Android silently truncates at ~200 chars |
| Portal `?token=` URL parameter | **No** | NDS redirect strips query parameters — URL rewritten to `/splash.html?redir=...` |
| Browser clipboard paste (Paste button) | **No** | CB002: Portal paste button broken on Android 14 Firefox |
| Android native long-press → Paste | **Untested** | Should work in theory but not tested |
| QR code scan | **Untested** | Portal has no QR input mechanism |
| User manual typing | **Impractical** | 378 chars is too long for manual entry |

### ADB token typing details

- Tokens up to ~378 chars work with 80-char chunks and 0.3s inter-chunk delay
- The portal UI only shows the last ~30 characters in the input field, but the full token IS present
- The keyboard must be dismissed (tap outside the input field) before the Purchase button is visible
- Samsung keyboard may auto-correct or modify token characters — use `adb shell input text` which bypasses the IME

### Portal UX issues

- **CB002**: Paste button on portal doesn't work on Android 14 Firefox. The button exists but doesn't paste from clipboard
- **Token display**: Input field only shows ~30 chars of a 378-char token. Users can't verify they entered the right token
- **No QR input**: Portal has no mechanism to scan a QR code containing a token
- **Keyboard occlusion**: Soft keyboard covers the Purchase button — must dismiss keyboard first

---

## 3. Backend Limitations

### Go backend (tollgate-module-basic-go)

| Limitation | Impact | Workaround | Issue |
|-----------|--------|------------|-------|
| V4 tokens rejected | Users with modern Cashu wallets can't pay | Send V3 tokens only | File needed |
| V2 keyset IDs crash on startup | Can't use CDK mints | Use `testnut.cashu.exchange` (V1 keysets) | #18 |
| CLI socket only on Go | Feature detection skips on Rust | N/A | By design |
| Sessions in JSON file | Go persists, Rust doesn't | N/A | By design |
| LuCI admin UI | Go only, Rust has no UI | N/A | By design |
| gonuts bolt11 decode | Dummy invoices from testnut cause fatal error | Patched in `feature/v2-keyset-ids` branch | #156 |

### Lightning payments (testnut.cashu.exchange)

**Status: Broken for portal display, but backend flow works.**

- `testnut.cashu.exchange` returns a dummy string (`dummy-mint-4-46876...`) instead of a bolt11 invoice
- The backend's tolerant fix (PR #156) allows the quote to be created despite the invalid invoice
- Monitoring goroutine works — FakeWallet auto-pays, tokens get minted, access is granted
- The portal cannot decode the dummy string as bolt11 → Lightning tab shows error
- `testnut.cashu.space` returns proper bolt11 — would work for Lightning testing
- This is ONLY an issue with `testnut.cashu.exchange`. Production mints return valid bolt11

### `pay_direct` vs portal UI

| Aspect | `pay_direct` (SSH curl) | Portal UI |
|--------|------------------------|-----------|
| Token delivery | POST to `http://[::1]:2121/` with `X-Forwarded-For` | Browser form submission |
| Client IP | Spoofed via header | Real client IP from NDS |
| Auth detection | Check `ndsctl status` | Portal shows "remaining" data |
| Real user behavior | **No** — framework shortcut | **Yes** — actual user flow |
| Use case | Fast API testing, CI | E2E testing, demo |

---

## 4. Firewall Fix (Critical Finding)

### Problem

`/etc/config/firewall-tollgate` was a UCI-format file loaded as an fw4 include. fw4 rejected it because the syntax wasn't valid for an include file. This meant the masquerade/NAT rules never loaded. Authenticated phones could reach the router but not the internet.

### Symptoms

- Phone connects to WiFi, pays for access, NDS authenticates
- `ndsctl status` shows "Authenticated"
- Phone cannot ping 1.1.1.1 or load any external site
- `nft list ruleset` shows no masquerade rule on the WAN interface

### Fix

```bash
# Rename the broken file
mv /etc/config/firewall-tollgate /etc/config/firewall-tollgate.disabled

# Remove UCI include reference
uci delete firewall.@include[-1]  # or the specific index
uci commit firewall

# Restart firewall
fw4 restart

# Verify masquerade is active
nft list ruleset | grep masquerade
```

### Root cause

The TollGate `.ipk` creates `/etc/config/firewall-tollgate` and adds it as a UCI include. The file contains iptables-style rules that fw4 can't parse as an include. fw4 silently ignores the file and the masquerade rule never loads.

### Impact on framework

The framework's `pay_direct()` worked because it SSH'd to the router and hit the backend directly — it never tested actual internet access from the phone. Phone tests that check internet connectivity after payment would have caught this.

---

## 5. Router Configuration

### Accepted mints (current)

```json
{
  "accepted_mints": [
    {"url": "https://testnut.cashu.exchange"},
    {"url": "https://mint.coinos.io"}
  ]
}
```

### Mint compatibility with Go backend

| Mint | Keyset Version | Token Version | Works with Go? | Tested |
|------|---------------|---------------|----------------|--------|
| `testnut.cashu.exchange` | V1 (`008e808b89acc141`) | V3 (`cashuA`) | **Yes** | pay_direct + portal |
| `mint.coinos.io` | V1 (`007311aa2fa58cc8`) | V4→V3 conversion | **Yes** | pay_direct (SSH curl) |
| `mint.chorus.community` | V1 (`008ec635ab34aeda`) | V4→V3 conversion | **Yes** (removed from config) | pay_direct (SSH curl) |

### Data pricing

- 1 sat = 10 MiB step
- 8 sats = ~80 MiB allotment
- Pricing configured in `/etc/tollgate/config.json`

---

## 6. Test Matrix — Known Unknowns

Things to test when a router is available again. Ordered by priority.

### High Priority (core functionality)

| # | Test | Prerequisites | Expected Result | Notes |
|---|------|---------------|-----------------|-------|
| T1 | V3 token from `mint.coinos.io` | Coinos returns V1 keysets | Accepted | Third configured mint, never tested |
| T2 | V1 token from any mint | Wallet that outputs V1 tokens | Accepted | V1 is the baseline format |
| T3 | Native captive portal popup flow | Phone on TollGate WiFi, deauthenticated | Android shows "Sign in to network" notification | Toggle WiFi off/on, force connectivity recheck |
| T4 | Data cutoff after allotment consumed | Paid session, generate traffic to exhaust allotment | Internet access blocked, portal re-appears | 80 MiB at 8 sats |
| T5 | Deauth + reconnect cycle | Paid session, deauth, reconnect WiFi | Portal re-appears, can pay again | Session persistence check |
| T6 | Multiple simultaneous clients | 2+ devices on TollGate WiFi | Each gets own session, data metered independently | Needs second device |
| T7 | Token reuse (double-spend) | Same token submitted twice | Second submission rejected | Cashu protocol prevents this |

### Medium Priority (edge cases, UX)

| # | Test | Prerequisites | Expected Result | Notes |
|---|------|---------------|-----------------|-------|
| T8 | V4 token against Rust backend | Rust backend deployed | Unknown — may work | CDK supports V4 |
| T9 | `testnut.cashu.space` Lightning invoice | Configured as mint | Portal shows QR code, auto-pays | Returns real bolt11 |
| T10 | Android long-press paste in portal | Token in clipboard | Token pastes into field | Alternative to Paste button |
| T11 | Portal on Chrome (not Firefox) | Chrome as default browser | Paste button may work | CB002 only tested on Firefox |
| T12 | Session expiry (time-based) | Short time allotment (e.g., 1 minute) | Access cuts off after time expires | Pricing metric = milliseconds |
| T13 | Extend session with second token | Active session, second token submitted | Session extended, data/time added | |
| T14 | Portal from different client OS | iPhone, Windows, Chromebook | Portal renders, payment works | Cross-platform portal test |
| T15 | Large token amount (100+ sats) | Mint 100-sat token | Accepted, large data allotment | Token string will be very long |

### Lower Priority (infrastructure, resilience)

| # | Test | Prerequisites | Expected Result | Notes |
|---|------|---------------|-----------------|-------|
| T16 | Backend restart during active session | Paid session, restart `tollgate-wrt` | Session survives (Go persists to JSON) | Rust loses session (in-memory) |
| T17 | Router reboot during active session | Paid session, `reboot` | Session state after reboot depends on persistence | |
| T18 | Mint unreachable during payment | Block mint IP via iptables | Graceful error in portal, no crash | Degraded mode test |
| T19 | Nodogsplash restart during session | Paid session, `/etc/init.d/nodogsplash restart` | Client deauthenticated, must re-auth | |
| T20 | IPv6 still disabled after reboot | Reboot router, check `ip -6 addr show br-lan` | No global IPv6 on LAN | Bypass prevention |
| T21 | DHCP bypass fix persists after reboot | Reboot router, phone reconnects | Phone gets IP via DHCP | `ndsRTR` chain still patched |
| T22 | Config hot-reload (change pricing mid-session) | Active session, change step_size in config.json | New pricing for next payment, current session unchanged | |
| T23 | Profit share payout with real mint | Configured LN address, active payments | Payout triggered, LN payment sent | Needs real mint, not testnut |
| T24 | Portal with HTTPS captive portal | TLS-enabled portal | Portal loads, no certificate errors | Requires custom cert setup |

---

## 7. Framework Integration Notes

### What the framework does well

- `connected_wifi` fixture: `reset_state()` + `wifi.reconnect()` + `_open_portal_on_phone()` — solid deterministic flow
- `pay_direct()` for fast API testing without phone interaction
- ADB token typing with 80-char chunks — works reliably for long tokens
- Data metering verification via `/usage` endpoint

### What needs framework work

- **Native captive portal popup**: Framework currently bypasses it. Could add an optional mode that waits for the Android notification instead of opening the browser directly. Would require:
  - `adb shell dumpsys notification | grep captive` polling
  - Timeout + fallback to direct URL opening
  - Only works on Android (not container clients)

- **WiFi reconnect reliability**: `svc wifi disable/enable` causes phone to reconnect to strongest saved network, not necessarily TollGate. Framework should force-connect to TollGate SSID specifically.

- **Phone disconnect handling**: Phone dropped off ADB during WiFi toggle testing. Framework should handle ADB reconnection gracefully.

- **V4 token testing**: No test currently validates that the backend rejects V4 tokens gracefully (returns error, doesn't crash). Should add an API test.

### Phone connection details

- Phone MAC: `46:02:f9:03:c4:e6`
- Phone IP on TollGate WiFi: `10.171.103.152`
- Phone IP on home WiFi ("2"): `192.168.13.110`
- ADB over TCP: `192.168.13.110:5555` (home WiFi only, not available on TollGate WiFi)
- ADB over USB: Works when physically connected
- Phone PIN: `5555`

---

## 8. Demo Readiness Checklist

For a live demo of the TollGate captive portal:

- [x] Router boots with TollGate service running
- [x] Phone connects to open WiFi
- [x] Captive portal intercepts HTTP
- [x] Portal renders with token input
- [x] V3 Cashu token accepted via portal UI
- [x] Internet access granted after payment
- [x] Data metering visible and tracking
- [ ] **Native "Sign in to network" popup** — not reliable, use direct browser opening
- [ ] **V4 token support** — Go backend rejects V4, only V1/V3 work
- [ ] **Clipboard paste** — broken on Android 14 Firefox (CB002)
- [ ] **Lightning QR code** — testnut returns dummy invoice, not displayable
- [x] **Multiple mint tokens** — testnut works via portal + pay_direct; coinos works via pay_direct (V4→V3 conversion needed)
- [ ] **Coinos via portal UI** — only tested pay_direct, not end-to-end through captive portal
- [ ] **Data cutoff** — not tested (need to exhaust 80 MiB allotment)

### Demo approach

1. Pre-mint V3 tokens from `testnut.cashu.exchange` using the cashu CLI
2. Print tokens on paper or display on a second screen
3. Connect phone to TollGate WiFi
4. Open browser to `http://10.171.103.1:2050/` (or wait for captive portal)
5. Type/paste token
6. Tap Purchase
7. Show internet access + data metering

For users bringing their own tokens:
- Must be V1 or V3 format (`cashuA` prefix)
- Must be from an accepted mint (testnut, coinos, chorus)
- Go backend rejects V4 tokens — users with modern wallets (eNuts, etc.) may produce V4 tokens
- Consider adding V4 support before public demo
