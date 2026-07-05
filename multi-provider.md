# Multi-Provider VM Architecture

## Overview

physical-router-test-automation supports running test VMs on **GCP** (default) or **SHC** (Sovereign Hybrid Compute). A provider abstraction layer handles VM lifecycle while the test pipeline remains identical.

## Quick Start

### GCP (default — unchanged)
```bash
python3 scripts/cloud-lab.py up --vm-name my-test
python3 scripts/cloud-lab.py submit --pr 42
python3 scripts/cloud-lab.py down --vm-name my-test
```

### SHC
```bash
export SHC_API_KEY="shc_live_..."
python3 scripts/cloud-lab.py up --cloud shc --vm-name my-test
# → Creates SHC Dev VPS Standard (2C/8GB/16GB), provisions in ~64s
# → Applies your ~/.ssh/id_rsa.pub to the VM
# → Prints: ssh debian@<ip>

# SSH in and run tests manually:
ssh debian@<ip>
cd /opt/tollgate-test && pytest tests/api -m api

python3 scripts/cloud-lab.py down --cloud shc --vm-name my-test
```

## Provider Architecture

```
TOLLGATE_VM_PROVIDER=gcloud (default)     TOLLGATE_VM_PROVIDER=shc
         │                                        │
         ▼                                        ▼
   GCPProvider                              SHCProvider
  (wraps gcp.py)                         (wraps shc-toolkit)
         │                                        │
         ▼                                        ▼
   gcloud CLI                             SHC API v2 + SSH
         │                                        │
         └─────────────┬────────────────────────┘
                       ▼
              VMInfo dataclass
              {name, service_id, ip, ...}
                       │
                       ▼
              worker/ pipeline
         (provider-agnostic — QEMU,
          test execution, result publishing)
```

## Supported Commands per Provider

| Command | GCP | SHC |
|---------|-----|-----|
| `up` | ✅ Creates from snapshot | ✅ Orders Dev VPS, provisions, applies SSH key |
| `down` | ✅ Deletes VM | ✅ Cancels VM (immediate, with refund) |
| `status` | ✅ Shows VM status | ✅ Lists all SHC VMs with IPs |
| `ssh` | ✅ Connects via gcloud compute ssh | ✅ Connects via standard SSH |
| `cleanup-stale` | ✅ Deletes old VMs | ✅ Cancels old SHC VMs |
| `cleanup-all` | ✅ Deletes all | ✅ Cancels all |
| `submit` | ✅ Full CI (JIT runner + snapshot) | ✅ Full CI (SSH bootstrap + cloud-init) |
| `status-run` | ✅ Via run-id | ❌ N/A (no run-id system on SHC) |

## Files

```
lib/cloud_lab/
  provider.py     ← VMProvider interface + GCPProvider + SHCProvider
  shc.py          ← SHC API v2 client (confirmation flow, idempotency, ssh_key fix)
  gcp.py          ← GCP VM lifecycle (unchanged, wrapped by GCPProvider)
  worker/         ← Test pipeline (provider-agnostic, runs inside VM)
  constants.py    ← Shared constants
```

## SHC-Specific Notes

- **Machine types:** SHC Dev VPS Standard (pkg 81) ≈ GCP n2-standard-2 (2C/8GB)
- **Cost:** ~$0.46/day, prorata refund on cancel (~$0.005 per 15-min test run)
- **Provisioning:** 22-90 seconds (vs GCP snapshot boot ~10s)
- **SSH user:** `debian` (not `root` — use `sudo -i` for root)
- **Nested KVM:** ✅ Available on all Dev VPS plans
- **SSH key propagation:** 2-5 min after `apply_ssh_key_live`. Password fallback via `get_vm_credentials()` if key auth not ready.
- **Password rotation:** SHC rotates VM passwords every ~5 min. `wait_for_shc_run` fetches fresh credentials each poll cycle.
- **Cloud-init:** SHC images support cloud-init. Inner Debian VM uses `genericcloud` image with cloud-init seed ISO for root SSH configuration.
- **Kernel modules:** Fresh SHC Debian 13 needs `modprobe bridge tun kvm kvm_intel vhost_net` (GCP snapshot has these pre-loaded).
- **QEMU images:** Bootstrap downloads OpenWrt 24.10.1 + Debian 12 genericcloud on first run (~60s). BlossomFS and vwifi binaries cached via Blossom/Nostr.

## SHC Submit Hardening

The `submit --cloud shc` pipeline includes these reliability features:

1. **Idempotency key**: Each VM order gets `idempotency_key=f"tollgate-{run_id}"`. Retrying the same run won't create duplicate VMs.
2. **Unique hostname**: `tollgate-{branch}-{timestamp}` prevents collisions on concurrent submits.
3. **VM TTL self-cancel**: Bootstrap schedules `shutdown` via `at` command after `--lease` minutes. Prevents cost leaks if CI runner is killed.
4. **API retry logic**: SHC API client retries 3x with exponential backoff on 5xx errors and network failures.
5. **Secret cleanup**: `BOT_NSEC_HEX` and `GH_TOKEN` are `unset` after the worker pipeline starts.
6. **15-step bootstrap with per-step error checking**: Each step calls `fail N "msg"` on failure, which writes `BOOTSTRAP_FAILED` to `/tmp/tollgate-status`.
7. **Completion marker**: `touch /tmp/tollgate-done` at the end allows reliable completion detection.

## Adding a New Provider

1. Implement `VMProvider` in a new file (e.g., `lib/cloud_lab/aws.py`)
2. Register in `provider.py`:
   ```python
   _PROVIDERS["aws"] = AWSProvider
   ```
3. Add to `--cloud` choices in `scripts/cloud-lab.py`
4. Test: `python3 scripts/cloud-lab.py up --cloud aws --vm-name test`
