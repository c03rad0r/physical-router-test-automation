# Testing Rust v1 on Physical Routers

The test framework supports testing the Rust v1 TollGate backend (`tollgate-rs`)
alongside the Go v1 backend. Both produce the same `.ipk` package name
(`tollgate-wrt`), so the Rust build will overwrite any existing Go installation
via `opkg --force-overwrite`.

## Prerequisites

- Router flashed with OpenWrt (use `scripts/build-firmware.py` to build images)
- `gh` CLI authenticated with access to `Amperstrand/tollgate-rs-ai-research-and-experiments`
- Python venv set up (`scripts/setup-python.sh`)
- Router reachable over SSH from your machine

## Quick Start

### Step 1: Download the Rust .ipk

```bash
./scripts/download-rust-ci-artifact.sh experimental
```

This downloads the latest CI-built `.ipk` for `aarch64_cortex-a53` from the
`experimental` branch of the Rust repo. Override arch with `TOLLGATE_ROUTER_ARCH`.

### Step 2: Deploy to router

```bash
TOLLGATE_SSH_PASSWORD=xxx TOLLGATE_ROUTER_ARCH=aarch64_cortex-a53 \
  ./scripts/deploy-rust-ci.sh experimental 192.168.x.x
```

Or use the unified `deploy-ci.sh` with backend flag:

```bash
TOLLGATE_SSH_PASSWORD=xxx ./scripts/deploy-ci.sh --backend rust experimental '' 192.168.x.x
```

### Step 3: Run smoke tests

```bash
source ~/.tollgate-test-venv/bin/activate
TOLLGATE_BACKEND=rust TOLLGATE_SSH_HOST=192.168.x.x make smoke-rust
```

### Step 4: Run full API tests

```bash
TOLLGATE_BACKEND=rust TOLLGATE_SSH_HOST=192.168.x.x make api-rust
```

### Step 5: Using test-pr.sh (unified workflow)

```bash
TOLLGATE_LUCI_PASSWORD=xxx ./scripts/test-pr.sh --backend rust --branch experimental --test api
```

This handles the full cycle: resolve branch, download artifact, deploy to router,
run tests, generate HTML report.

## Backend Selection

All scripts accept `--backend go|rust` or read `TOLLGATE_BACKEND` from the
environment. Default is `go`.

| Mechanism | Example |
|---|---|
| Env var | `TOLLGATE_BACKEND=rust make api` |
| CLI flag | `./scripts/test-pr.sh --backend rust --branch experimental` |
| Pytest flag | `pytest -m api --backend=rust` |
| Makefile target | `make smoke-rust` |

## Makefile Targets

| Target | What it runs |
|---|---|
| `make smoke-rust` | Rust smoke tests (~15s, API-only) |
| `make api-rust` | Rust API tests |
| `make critical-rust` | Rust critical tests (~2min) |
| `make test-rust` | All Rust pytest tests |

## Switching Between Go and Rust

The Rust `.ipk` overwrites the Go `.ipk` (same package name `tollgate-wrt`).
To switch back to Go:

```bash
./scripts/deploy-ci.sh main '' 192.168.x.x
```

To switch to Rust:

```bash
./scripts/deploy-ci.sh --backend rust experimental '' 192.168.x.x
```

## Multi-Router Lab

Set up `config/routers.json` to target different backends on different routers:

```json
{
  "default": "rust-router-a",
  "routers": {
    "rust-router-a": {
      "model": "D-Link COVR-X1860",
      "sshHost": "192.168.1.1",
      "arch": "aarch64_cortex-a53",
      "wifiInterface": "wlan0",
      "tollgateSsidPrefix": "TollGate-",
      "openwrtVersion": "24.10.1",
      "openwrtTarget": "mediatek/filogic",
      "openwrtProfile": "dlink_covr-x1860-a1"
    },
    "go-router-b": {
      "model": "D-Link COVR-X1860",
      "sshHost": "192.168.1.2",
      "arch": "aarch64_cortex-a53",
      "wifiInterface": "wlan0",
      "tollgateSsidPrefix": "TollGate-",
      "openwrtVersion": "24.10.1",
      "openwrtTarget": "mediatek/filogic",
      "openwrtProfile": "dlink_covr-x1860-a1"
    }
  }
}
```

Test Rust on one router, Go on another:

```bash
TOLLGATE_ROUTER_ID=rust-router-a TOLLGATE_BACKEND=rust make smoke-rust
TOLLGATE_ROUTER_ID=go-router-b TOLLGATE_BACKEND=go make smoke
```

## Feature Compatibility

| Works on Rust v1 | Go v1 only |
|---|---|
| All 7 API endpoints (GET /, POST /, GET /usage, GET /balance, GET /whoami, POST /ln-invoice, GET /ln-invoice) | LuCI admin UI |
| Cashu token payments | CLI socket (wallet, status, version) |
| Lightning invoice creation | Session persistence across restarts |
| NDS valve (ndsctl auth/deauth) | |
| Captive portal | |

Tests marked `@pytest.mark.go_only` are automatically skipped when
`--backend=rust`. Tests marked `@pytest.mark.rust_only` are skipped when
`--backend=go`.

## Architecture Reference

```
Rust .ipk:  Amperstrand/tollgate-rs-ai-research-and-experiments  ->  branch: experimental  ->  CI: "Build and Package"
Go .ipk:    OpenTollGate/tollgate-module-basic-go                ->  branch: main           ->  CI: "Build and Publish"
```

Both produce `tollgate-wrt` packages with identical init.d service names and
configuration paths (`/etc/tollgate/config.json`). The `BackendConfig` class in
`lib/backend.py` maps backend type to repo/workflow/feature flags.
