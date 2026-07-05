# Firmware Build + Flash Helper

Design document for `scripts/build-firmware.py`, a tool that builds custom OpenWrt images with test credentials baked in and optionally flashes them to lab routers.

## Background

The test framework has tools for flashing routers (`scripts/flash-routers.mjs`) and deploying packages (`scripts/deploy.sh`), but no way to produce a clean firmware image with SSH keys and test settings preconfigured. Right now, getting a router into a testable state requires manual OpenWrt installation followed by hand-configured SSH access.

This script closes that gap. It calls the OpenWrt ASU (Attended Sysupgrade) API to build a custom image server-side, embeds our credentials via `uci-defaults`, and downloads the result.

## The ASU API

The OpenWrt ASU endpoint at `https://sysupgrade.openwrt.org/api/v1/build` builds custom images on demand.

**Request:**

```json
POST /api/v1/build
{
  "version": "24.10.1",
  "target": "mediatek/filogic",
  "profile": "glinet_gl-mt3000",
  "packages": [],
  "diff_packages": false,
  "defaults": "#!/bin/sh\nset -eu\n... exit 0"
}
```

**Poll for completion:**

```
GET /api/v1/build/{request_hash}
```

Returns `status: 200` when the image is ready. Download from:

```
https://sysupgrade.openwrt.org/store/{bin_dir}/{filename}
```

The `defaults` field becomes `/etc/uci-defaults/99-asu-defaults` on the image. This script runs once on first boot and self-deletes. That's where we inject SSH keys, passwords, and firewall rules.

## Design Decisions

### Dual Auth: SSH Key + Password

The test framework relies on `sshpass` (password auth) everywhere. SSH key auth is better for interactive use and CI. We need both.

**SSH key:** Written to `/etc/dropbear/authorized_keys` via `uci-defaults`.

**Password:** Set via `chpasswd` in the same `uci-defaults` script. Keeps `sshpass`-based tooling working.

**Key detection order:**

1. `--key` CLI flag (explicit path)
2. `TOLLGATE_SSH_KEY` environment variable
3. Auto-scan `~/.ssh/` for `id_ed25519.pub`, `id_rsa.pub`, `id_ecdsa.pub` (in that order)

The script shows the detected key type and last 8 characters of the fingerprint, then asks for confirmation before building.

**Privacy:** The key comment (typically `user@hostname`) is stripped before embedding. Only the key type and base64 data go into the firmware. No hostnames in images.

### Random Password Per Build

A fixed default password baked into firmware images with WAN SSH open is a real risk. Anyone on the upstream network could log in. Predictable passwords like `c03...` are even worse.

**Decision:** Each build gets a random password generated with `openssl rand -base64 18` (20+ characters, high entropy). The password is:

- Shown to the user during the build
- Logged to `credentials/<router-id>.txt` on the local machine (mode 600)
- Never committed to git (`credentials/` is in `.gitignore`)

For CI pipelines, the password can be overridden with `TOLLGATE_FIRMWARE_PASSWORD`. If that env var is set, its value is used instead of generating a new one.

WAN SSH is still enabled (tests need it), but the random password makes brute-force attacks impractical against lab routers.

### The uci-defaults Script

The first-boot script injected into the image:

```sh
#!/bin/sh

# SSH key
mkdir -p /etc/dropbear
echo "<key-type> <base64-data>" > /etc/dropbear/authorized_keys
chmod 600 /etc/dropbear/authorized_keys

# Password (chpasswd does not exist on BusyBox — use passwd instead)
printf '%s\n%s\n' '<password>' '<password>' | passwd root

# WAN SSH firewall rule
uci add firewall rule
uci set firewall.@rule[-1].name='Allow-SSH-WAN'
uci set firewall.@rule[-1].src='wan'
uci set firewall.@rule[-1].dest_port='22'
uci set firewall.@rule[-1].proto='tcp'
uci set firewall.@rule[-1].target='ACCEPT'
uci commit firewall

exit 0
```

The `exit 0` ensures OpenWrt's `uci-defaults` mechanism deletes the script after a successful run. The password and key are only in the script on first boot.

**Lessons learned:**

- **No `chpasswd` on BusyBox.** OpenWrt's BusyBox does not include `chpasswd`. Use `printf '%s\n%s\n' '<pw>' '<pw>' | passwd root` instead.
- **No `set -eu` in uci-defaults.** If the script aborts partway through, later commands (like the firewall rule) never execute, leaving the router inaccessible from WAN. Without `set -eu`, each command runs independently and a single failure doesn't block the rest.

### Python, Not Node.js

The script is standalone Python with no external dependencies (stdlib `urllib` for HTTP).

Why not Node.js, which the rest of the test framework uses? Embedding a multi-line shell script as a JSON string value is error-prone in bash. Python handles the quoting cleanly. The script is a build-time tool, not a runtime dependency, so the language choice doesn't affect the test suite.

### Router Config Schema

New fields added to `config/routers.json`:

```json
{
  "routers": {
    "lab-router-a": {
      "model": "glinet_gl-mt3000",
      "openwrtVersion": "24.10.1",
      "openwrtTarget": "mediatek/filogic",
      "openwrtProfile": "glinet_gl-mt3000",
      "sshHost": "192.168.13.112",
      "sshUser": "root",
      "arch": "aarch64_cortex-a53"
    }
  }
}
```

The existing fields (`sshHost`, `arch`, `wifiInterface`) are unchanged. `openwrtVersion`, `openwrtTarget`, and `openwrtProfile` are new.

## CLI Interface

```
scripts/build-firmware.py --router <router-id> [--key <path>] [--yes] [--flash] [--output <dir>]
```

| Flag | Purpose |
|---|---|
| `--router` | Router ID from `config/routers.json` (required) |
| `--key` | Path to SSH public key (overrides auto-detection) |
| `--yes` | Skip confirmations (non-interactive, for CI) |
| `--flash` | Flash the image after downloading (uses existing SSH helpers) |
| `--output` | Directory for the downloaded image (default: temp dir) |

**Interactive mode** (default): detects key, shows config summary, asks confirmation, builds, downloads, optionally flashes.

**Non-interactive mode** (`--yes`): skips all prompts. Expects `TOLLGATE_SSH_KEY` or `--key` to be set. Uses `TOLLGATE_FIRMWARE_PASSWORD` if set, otherwise generates a random one.

## Workflow

### Building and Flashing

```
$ scripts/build-firmware.py --router lab-router-a

Detected SSH key: ED25519 (...3f:a4:k8)
Router: GL.iNet MT3000 (glinet_gl-mt3000)
OpenWrt: 24.10.1 (mediatek/filogic)

Build firmware with this key? [y/N] y
Generated password: aB3xK9mQ2pR7vN5tW1cY
Submitting build to ASU...
Polling... done.
Downloaded: /tmp/openwrt-24.10.1-glinet_gl-mt3000-sysupgrade.bin

Credentials saved to credentials/lab-router-a.txt

Flash to 192.168.13.112 now? [y/N] y
Uploading image... initiating sysupgrade...
```

### Test Run Integration

```
$ scripts/run-tests.sh <commit> lab-router-a
  → Checks if router is reachable
  → If not: suggests running build-firmware.py first
  → Reads credentials from credentials/lab-router-a.txt
  → Runs test suite via sshpass
```

The script does not replace `flash-routers.mjs`. That tool handles bulk flashing of multiple routers. This one handles single-router firmware builds with credential injection.

## Security Considerations

1. **Key comment stripped.** Only key type + base64 data in the firmware. No `user@hostname` leakage.
2. **Random password.** Not a fixed default. Logged locally only, never in git.
3. **WAN SSH is open.** Acceptable for lab/test routers. Would be disabled on production hardware.
4. **Image contains credentials.** The `uci-defaults` script has the password in plaintext inside the image. Treat built images as sensitive. Don't share or upload them.
5. **ASU server sees the build request.** Sent over HTTPS to the OpenWrt project's infrastructure. Acceptable for test firmware, not for production secrets.
6. **Credentials file permissions.** `credentials/` is gitignored. Files are written with mode 600.

## Open Questions

| Question | Tentative Answer |
|---|---|
| Password settable via env var for CI? | Yes, `TOLLGATE_FIRMWARE_PASSWORD` |
| Multiple keys per build? | Not initially. One key per build. |
| Use ASU build cache? | Yes. Same config produces the same request hash, so repeated builds are fast. |
| How long are images on the ASU server? | Unknown. Download immediately, don't rely on server-side storage. |
