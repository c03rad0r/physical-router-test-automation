# Serial Integration Plan

## Overview

Add USB-TTL serial connections to GL.iNet MT3000 routers, orchestrated by a dedicated mini PC. Serial serves as a parallel rescue/monitoring channel alongside existing SSH/NetBird access.

## Architecture

```
  Your Dev Machine (anywhere)
       │
       │ SSH / NetBird
       ▼
┌──────────────────────────────────────┐
│  Mini PC Orchestrator (always-on)    │
│  - Linux (Ubuntu/Debian)             │
│  - NetBird for remote access         │
│  - Python 3 + pyserial               │
│  - router-serial.py tool             │
│  - This repo cloned                  │
│                                      │
│  /dev/serial-alpha ──► Alpha UART    │
│  /dev/serial-beta  ──► Beta  UART    │
│  Ethernet          ──► LAN           │
└──────────────────────────────────────┘
```

## Phase 1: Hardware Setup

### GL.iNet MT3000 Serial Access

- UART header inside the case (4 pins: TX, RX, GND, VCC)
- Baud rate: 115200, 8N1, 3.3V logic level
- Connect only TX, RX, and GND (leave VCC disconnected)

### Parts Needed

- 2x USB-to-TTL serial adapters (FTDI FT232 or CP2102-based, ~$5-10 each)
- Jumper wires (female-to-female, typically included with adapters)

### Wiring

| USB-TTL Pin | Router UART Pin |
|-------------|-----------------|
| TX          | RX              |
| RX          | TX              |
| GND         | GND             |
| VCC         | (leave disconnected) |

### Verification

```bash
picocom /dev/ttyUSB0 -b 115200
# Press Enter — you should see a login prompt or shell prompt
```

### Persistent Port Names (udev Rules)

Create `/etc/udev/rules.d/99-serial-routers.rules` on the mini PC:

```
# Find adapter serial numbers with: udevadm info -a -n /dev/ttyUSB0 | grep serial
SUBSYSTEM=="tty", ATTRS{serial}=="ADAPTER_SERIAL_ALPHA", SYMLINK+="serial-alpha"
SUBSYSTEM=="tty", ATTRS{serial}=="ADAPTER_SERIAL_BETA",  SYMLINK+="serial-beta"
```

Then reload:

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
# Verify:
ls -la /dev/serial-alpha /dev/serial-beta
```

## Phase 2: Software — `scripts/router-serial.py`

Python CLI tool for reliable serial automation. Uses `pyserial` for serial communication and marker-based output capture.

### Subcommands

```
router-serial exec    --port PORT "command"                          # Run command, capture output
router-serial wait    --port PORT --pattern "PATTERN" --timeout N    # Wait for pattern in output
router-serial bootlog --port PORT --timeout N                        # Capture full boot until login prompt
router-serial watch   --port PORT                                    # Interactive tail (Ctrl+C to stop)
router-serial login   --port PORT                                    # Send login credentials if at login prompt
```

### How Command Execution Works

1. Opens serial port at 115200 baud
2. Sends newline to detect current state (login prompt vs shell prompt)
3. If at login prompt, sends username (root, no password on OpenWrt)
4. Wraps commands with markers: `echo '___SERIAL_START___'; <cmd>; echo "___SERIAL_END___:$?"`
5. Reads output between markers, parses exit code
6. Returns stdout and exit code via process exit code

### Dependencies

```
pyserial>=3.5
```

## Phase 3: Makefile Integration

### Target Prefix Convention

| Prefix | Meaning | Transport |
|--------|---------|-----------|
| `r-`   | Remote  | SSH (existing, unchanged) |
| `s-`   | Serial  | Serial console only |
| `h-`   | Hybrid  | SSH first, serial fallback |

### Serial Targets (`s-` prefix)

| Target | Purpose |
|--------|---------|
| `s-shell` | Interactive serial console (picocom) |
| `s-boot-log` | Capture full boot output |
| `s-wait-boot` | Wait for router to fully boot |
| `s-status` | Check tollgate status via serial |
| `s-recovery CMD="..."` | Run any recovery command via serial |
| `s-wait-pattern PATTERN=... TIMEOUT=120` | Wait for a log pattern |
| `s-cleanup` | Cleanup mint blocks and restore config via serial |
| `s-cold-boot-test` | Full cold boot test with serial monitoring |

### Hybrid Targets (`h-` prefix)

| Target | Purpose |
|--------|---------|
| `h-status` | Status check — SSH first, serial fallback |
| `h-check-merchant` | Merchant check — SSH first, serial fallback |
| `h-restart-and-watch` | Restart via SSH, monitor via serial |
| `h-cleanup` | Cleanup — SSH first, serial rescue if stranded |

### `routers.env` Additions

```bash
# Serial console ports (persistent symlinks via udev)
ROUTER_ALPHA_SERIAL=/dev/serial-alpha
ROUTER_BETA_SERIAL=/dev/serial-beta
SERIAL_BAUD=115200
```

## Phase 4: Enhanced Test Capabilities

### Cold Boot Test (Part B) — Before and After

Before (blind reboot):
```bash
ssh root@alpha "reboot"
sleep 60  # hope it comes back
ssh root@alpha "logread -e tollgate | head -30"
```

After (serial monitoring):
```bash
make -f Makefile s-cold-boot-test ROUTER=alpha
# Captures every line from kernel boot through procd startup
# Automatically checks tollgate status when boot completes
# Full log saved to /tmp/cold-boot-alpha.log
```

### Stranded Router Recovery — Before and After

Before (complex rescue via another router):
```bash
make -f Makefile r-rescue-router ROUTER=beta VIA=alpha  # 50+ lines of shell
```

After (direct serial recovery):
```bash
make -f Makefile s-recovery ROUTER=beta CMD="sed -i '/nofee.testnut/d' /etc/hosts && /etc/init.d/tollgate-wrt restart"
```

### True Connectivity-Loss Testing

Before (simulated with iptables, NetBird still works):
```bash
ssh root@alpha "iptables -A OUTPUT -d <mint-ip> -j DROP"
```

After (actual disconnect, serial still works):
```bash
# Disconnect WAN physically or via serial
make -f Makefile s-recovery ROUTER=alpha CMD="ifconfig eth0 down"
# Monitor via serial — no network dependency
make -f Makefile s-wait-pattern ROUTER=alpha PATTERN="degraded mode" TIMEOUT=60
```

## What Gets Eliminated

| Before | After |
|--------|-------|
| `r-rescue-router` (50+ lines) | `s-recovery` (single command) |
| LAN_HOST dual-path per router | Serial as universal fallback |
| Blind reboot-and-wait in Part B | `s-cold-boot-test` with full log capture |
| "WARNING: may strand router" warnings | Serial recovery makes any test safe |

## What Stays Unchanged

- All existing `r-` targets (SSH-based) work exactly as before
- File transfer still uses `scp` (serial too slow for binaries)
- Router mutex system works as-is
- Playwright tests are unaffected
- The `r-` targets remain the default for everyday use

## Implementation Priority

| Priority | Step | What | Effort |
|----------|------|------|--------|
| P0 | 1 | Hardware: USB-TTL to routers, verify serial console | 1-2 hrs |
| P0 | 2 | `scripts/router-serial.py` | 2-3 hrs |
| P1 | 3 | `routers.env` serial config + udev rules | 15 min |
| P1 | 4 | `s-cleanup` and `s-recovery` Makefile targets | 1 hr |
| P1 | 5 | `s-cold-boot-test` Makefile target | 1 hr |
| P2 | 6 | `h-` hybrid Makefile targets | 1-2 hrs |
| P2 | 7 | `s-boot-log`, `s-wait-pattern` targets | 1 hr |

Total estimated effort: ~1 day.

## Mini PC Setup Checklist

```bash
# 1. Install OS (Ubuntu Server or Debian minimal)
# 2. Install dependencies
sudo apt install python3 python3-pip picocom
pip3 install pyserial

# 3. Clone this repo
git clone <repo-url> /opt/physical-router-test-automation
cd /opt/physical-router-test-automation

# 4. Copy and edit routers.env with serial ports + network IPs
cp mint-health/routers.env.example mint-health/routers.env
# Edit with real values

# 5. Setup udev rules for persistent serial port names
# (see Phase 1 above)

# 6. Install NetBird for remote access
curl -fsSL https://pkgs.netbird.io/install.sh | sh
netbird up --setup-key <your-key>

# 7. Test serial connection
picocom /dev/serial-alpha -b 115200
# Press Enter — should see OpenWrt prompt

# 8. Test router-serial.py
python3 scripts/router-serial.py exec --port /dev/serial-alpha "echo hello"
# Should print: hello

# 9. Run a test
make -f mint-health/Makefile s-status ROUTER=alpha
```
