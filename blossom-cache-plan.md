# BlossomFS Build Cache Plan

Status: Draft
Date: 2026-06-30

## Problem

Every cloud-lab VM (SHC or GCP) currently rebuilds from scratch:
- Rust toolchain install (~2 min)
- fips binary compile (~10 min)
- QEMU image downloads (~2 min each)
- TollGate .ipk build or download (~3 min)
- Python deps + nak CLI install (~2 min)

Total cold-start: ~20 min before tests run. On SHC (no baked snapshot),
this happens every single run.

## Solution: Blossom content-addressed cache

The pattern already exists: `lib/cloud_lab/worker/blossom_cache.py` has
`cached_build()` which checks Blossom for an artifact before building.

Extend it to cache ALL expensive build/download steps:

```python
from lib.cloud_lab.worker.blossom_cache import cached_build, compute_cache_key

# fips binary
key = compute_cache_key("fips-binary", git_commit, "x86_64", "release")
fips_path = cached_build(key, build_fips_fn, "/usr/local/bin/fips", nsec_file)

# OpenWrt image
key = compute_cache_key("openwrt-image", "24.10.0", "x86-64")
openwrt_path = cached_build(key, download_openwrt_fn, "/opt/images/openwrt.img", nsec_file)

# Debian image
key = compute_cache_key("debian-image", "bookworm", "amd64")
debian_path = cached_build(key, download_debian_fn, "/opt/images/debian.qcow2", nsec_file)
```

## Cache targets (priority order)

### 1. fips binary (~10 min → ~5 sec)

**Cache key:** `sha256("fips-binary" + git_url + commit + target_arch + profile)`

```python
def build_fips():
    _run("git clone --depth 1 --branch {ref} {url} /tmp/fips")
    _run("cd /tmp/fips && cargo build --release")
    return "/tmp/fips/target/release/fips"
```

First run: builds (10 min), uploads to Blossom, publishes NIP-94.
Subsequent runs: NIP-94 query → download from Blossom (5 sec).

**Size:** ~30 MB (single binary, well under Blossom's 1 MB free tier
limit per file — needs to be uploaded as a single blob, Cashu payment
required for >1 MB).

**Alternative:** Split into fips + fipsctl + fipstop + fips-gateway
(4 files, each under 10 MB). Cache each separately.

### 2. OpenWrt combined image (~100 MB → ~5 sec)

**Cache key:** `sha256("openwrt-combined" + version + target)`

```python
def download_openwrt():
    url = f"https://downloads.openwrt.org/releases/{ver}/targets/x86/64/openwrt-{ver}-x86-64-generic-ext4-combined.img.gz"
    _run(f"wget -q -O /tmp/openwrt.img.gz {url}")
    _run("gunzip /tmp/openwrt.img.gz")
    return "/tmp/openwrt.img"
```

**Size:** ~100 MB. Exceeds Blossom's 1 MB free tier. Options:
- Cashu payment (~100 sats per upload)
- Blossomflare (self-hosted, no limit)
- Split into chunks (complex)
- Use a CDN URL directly (no Blossom cache needed for public downloads)

**Recommendation:** For public downloads (OpenWrt, Debian images), skip
Blossom cache — just cache the URL in NIP-94 metadata. The SHC worker
downloads directly from the CDN. Blossom cache is for PRIVATE artifacts
(fips binary, TollGate .ipk) that aren't on a public CDN.

### 3. Cargo registry cache (~2 GB → ~30 sec)

**Cache key:** `sha256("cargo-registry" + Cargo.lock hash)`

The cargo registry (downloaded crates) is the biggest time sink after
the actual compilation. Caching it turns a 10-min compile into a 3-min
incremental compile.

```python
def build_fips_with_registry_cache():
    # Download cached registry from Blossom
    key = compute_cache_key("cargo-registry", cargo_lock_hash)
    cached_build(key, lambda: None, "/tmp/cargo-registry.tar.gz", nsec_file)
    # Extract to ~/.cargo/registry
    _run("tar xzf /tmp/cargo-registry.tar.gz -C ~/.cargo/")
    # Build with cached deps
    _run("cargo build --release")
```

**Size:** ~500 MB-2 GB (compressed ~200-500 MB). Too large for Blossom
free tier. Use Blossomflare or skip (rely on cargo's own incremental
compilation within a persistent VM).

**Recommendation:** Skip cargo registry caching for now. The fips binary
cache (item 1) eliminates the need for cargo entirely on cache hits.

### 4. TollGate .ipk (~5 MB → ~2 sec)

**Already partially cached:** The CI uploads .ipk to Blossom.
The cloud-lab worker should query NIP-94 for the commit hash instead
of downloading from GitHub Actions artifacts.

**Cache key:** `sha256("tollgate-ipk" + repo + commit + arch)`

### 5. Cloud-init seed ISOs (~1 KB each → instant)

Generated per-run (SSH keys, network config). Too small to cache but
the generation logic should be reusable across SHC and GCP.

## Implementation plan

### Phase 1: Cache fips binary on Blossom

Add to `lib/cloud_lab/worker/blossom_cache.py`:

```python
def get_fips_binary(ref: str, dest: str, nsec_file: str, blossom_server: str) -> str:
    """Get fips binary with Blossom cache. Builds if not cached."""
    # Determine commit hash
    commit = subprocess.run(
        ["git", "ls-remote", "--heads", "https://github.com/Amperstrand/fips.git", ref],
        capture_output=True, text=True
    ).stdout.split()[0]

    key = compute_cache_key("fips-binary", "amperstrand/fips", commit, "x86_64", "release")

    def build():
        _run(f"git clone --depth 1 --branch {ref} https://github.com/Amperstrand/fips.git /tmp/fips-build")
        _run("cd /tmp/fips-build && cargo build --release")
        return "/tmp/fips-build/target/release/fips"

    return cached_build(key, build, dest, nsec_file, blossom_server)
```

Integrate into `scripts/test-fips.sh` and `lib/cloud_lab/worker/provision.py`.

### Phase 2: Cache OpenWrt + Debian images via NIP-94 metadata

Instead of caching the large images on Blossom, publish their CDN URLs +
SHA256 as NIP-94 events. The worker checks NIP-94 first, uses the CDN URL
if found (fast), falls back to discovering the URL if not.

```python
def get_openwrt_image(version: str, dest: str, nsec_file: str) -> str:
    key = compute_cache_key("openwrt-url", version, "x86-64")
    # Check if we already know the URL
    cached = _check_blossom_cache(key, nsec_file)
    if cached:
        # NIP-94 event has the URL in a tag — download directly
        _download_blob(cached, dest)
        return dest
    # Discover URL, cache it
    url = f"https://downloads.openwrt.org/releases/{version}/targets/x86/64/..."
    _download_blob(url, dest)
    _upload_and_publish_metadata(key, url, nsec_file)  # publish URL, not file
    return dest
```

### Phase 3: Cache Rust toolchain (optional)

The Rust toolchain is ~500 MB. Instead of caching it on Blossom, use
rustup's own caching: if `~/.cargo/env` exists and `rustc --version`
matches, skip the install.

```python
def ensure_rust(toolchain: str = "stable"):
    if _run("rustc --version", check=False).returncode == 0:
        return  # Already installed
    _run("curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y")
```

### Phase 4: Unified provision script

Create `lib/cloud_lab/worker/provision_fips.py` that uses all caches:

```python
def provision_fips_on_shc(nsec_file: str, blossom_server: str):
    """Provision a SHC VM for fips testing. Uses Blossom cache."""
    ensure_rust()
    fips_path = get_fips_binary("ai-experiments", "/usr/local/bin/fips", nsec_file, blossom_server)
    openwrt_path = get_openwrt_image("24.10.0", "/opt/images/openwrt.img", nsec_file)
    debian_path = get_debian_image("bookworm", "/opt/images/debian.qcow2", nsec_file)
    # ... boot VMs, install fips, verify mesh
```

## Cost analysis

| Step | Cold (no cache) | Warm (cached) | Savings |
|------|----------------|---------------|---------|
| Rust install | 2 min | 0 (skip) | 2 min |
| fips build | 10 min | 5 sec (download) | 9:55 |
| OpenWrt download | 2 min | 5 sec (CDN) | 1:55 |
| Debian download | 2 min | 5 sec (CDN) | 1:55 |
| QEMU install | 1 min | 0 (pre-installed) | 1 min |
| **Total cold-start** | **~20 min** | **~30 sec** | **19:30** |

With caching, a SHC VM goes from 20-min cold start to 30-second warm start.
At $0.46/day for 2C/8GB, this means a 30-min test run costs ~$0.01 instead
of ~$0.02 (halved by eliminating the build time).
