# Bake Improvement List (v10)

Items to address when baking the next GCP runner snapshot (`tollgate-runner-baked-v10`).

## High Priority

### 1. Pre-build vwifi with Alpine Docker (static musl for OpenWrt)
**Current**: Bake builds vwifi on the GCP host with glibc (`-DCMAKE_EXE_LINKER_FLAGS='-static'`), which uses glibc static. OpenWrt uses musl. The `scripts/build-vwifi.sh` script already does Alpine Docker for proper static musl, but the bake script doesn't use it.
**Fix**: Use `scripts/build-vwifi.sh --output-dir /opt/vwifi` in the bake, or replicate the Alpine Docker approach.
**Saves**: ~2min build time per cloud run if binaries are missing, and ensures correct musl ABI.

### 2. Bake Debian client deps (Playwright + Chromium)
**Current**: The Debian QEMU overlay lives on the baked snapshot with Playwright + Chromium pre-installed. This is correct. But `ensure_debian_client_deps()` in `worker.py` still runs `apt-get install` and `npx playwright install` at runtime. If the overlay is already provisioned, this should be a no-op check.
**Fix**: Add a marker file `/etc/tollgate-debian-ready` in the bake and check for it in `ensure_debian_client_deps()`.
**Saves**: ~30-60s per run.

### 3. Snapshot storage location
**Current**: Snapshots are created in `europe-west1` but VMs run in `us-central1-a` or `us-east4-a`. Cross-region snapshot access adds latency to VM creation.
**Fix**: Change `--storage-location` in bake to `us` (or remove it to use the VM's region).
**Saves**: ~10-20s on VM creation.

## Medium Priority

### 4. Management bridge in bake
**Current**: The bake only creates `tg-poc-br`. The management bridge (`mgmt-br`, 10.99.97.0/24) is created at runtime by `setup_bridge()` in `worker.py`. Since it's a static config that never changes, bake it.
**Fix**: Add `mgmt-br` bridge + tap creation to bake step 7.
**Saves**: ~2-3s per run.

### 5. Management NIC baked into OpenWrt base image
**Current**: The OpenWrt base image is provisioned via serial with `network.mgmt` UCI (eth1, 10.99.97.1). This works but takes time during serial provisioning.
**Status**: Already done in v9 bake (lines 374-378 of bake-snapshot.py).
**Verify**: Ensure the v10 bake preserves this.

### 6. Remove vhost_vsock from bake
**Current**: Bake runs `modprobe vhost_vsock` (line 456) which was needed for the old vsock-based vwifi. Since vwifi now uses TCP, this is unnecessary.
**Fix**: Remove `modprobe vhost_vsock` from bake step 8c.

### 7. Bake `iw` on Debian
**Current**: `ensure_debian_client_deps()` installs `iw` at runtime via `apt-get install -y -qq iw`.
**Fix**: Pre-install `iw` in the Debian overlay during bake.
**Saves**: ~5s per run.

### 8. Reduce bake serial provisioning time
**Current**: Serial provisioning takes ~127s (from v9 bake log). Most of this is waiting for boot + network restart.
**Fix**: The SSH-first detection in the worker already skips serial if SSH works within 15s. The bake serial is only for the initial provisioning of the base image, which happens once. Not a runtime issue.

## Low Priority / Nice to Have

### 9. Bake from ASU custom image
**Current**: Bake downloads stock OpenWrt and provisions via serial. An ASU-built custom image with SSH + password + firewall + network + WiFi packages pre-baked would eliminate serial provisioning entirely.
**Fix**: Use `scripts/build-firmware.py` to build an x86_64 image with embedded config, then use that as the base.
**Saves**: ~127s bake time (but only bake time, not runtime).

### 10. Multi-arch support
**Current**: Only x86_64 for cloud lab. Physical routers are mipsel_24kc and aarch64_cortex-a53.
**Fix**: Not needed for cloud lab. Physical testing uses real hardware.

### 11. Auto-cleanup old snapshots
**Current**: Manual `gcloud compute snapshots delete` for old snapshots.
**Fix**: Add a `cleanup-old-snapshots` command to `bake-snapshot.py` that deletes all but the latest N snapshots.
**Saves**: Operational overhead.

## Timing Reference (v9 Bake)

| Step | Duration | Notes |
|------|----------|-------|
| VM creation | 45s | GCP API |
| SSH wait | 42s | VM boot |
| Image download | 6s | Cached in base snapshot |
| CLI install | 21s | gh + gcloud |
| Python venv | 8s | Already in base |
| Cashu venv | 29s | Already in base |
| Bridge setup | 5s | |
| Serial provision | 127s | Boot + provision OpenWrt |
| WiFi packages | ~10s | opkg install |
| vwifi build | 38s | cmake + make |
| Base replace | 12s | qemu-img convert |
| Snapshot create | 123s | GCP API |
| **Total bake** | **~7min** | |

## Runtime Timing Reference (v9, estimated)

| Phase | Duration | Notes |
|-------|----------|-------|
| VM boot + startup | ~2m | GCP startup script |
| Boot OpenWrt + Debian VMs | ~30s | SSH-first, no serial |
| Start local mints | ~5s | CDK + Nutshell |
| Deploy TollGate | ~50s | Download + install |
| vwifi setup (if enabled) | ~30s | Binaries pre-baked |
| Select test mint | ~5s | V2 probe, V1 fallback |
| Run tests | ~20m | ~94 tests |
| Collect + publish | ~30s | |
| **Total run** | **~25min** | |
