"""Cloud lab worker — optional post-test benchmarks.

Runs AFTER tollgate tests are published. Measures HTTP latency, payment
throughput, and system metrics on the warm VM. Results published separately
via Nostr — benchmark failure never blocks test reports.

Triggered by --benchmark flag on cloud-lab.py submit.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

from lib.cloud_lab.constants import OPENWRT_IP, RESULTS_ROOT, TEST_DIR
from lib.cloud_lab.worker.config import WorkerConfig
from lib.cloud_lab.worker.inner_ssh import inner_ssh
from lib.cloud_lab.worker.shell import _run, log

# Number of requests per endpoint for latency measurement.
_LATENCY_ITERATIONS = 50
# Number of concurrent requests for throughput testing.
_CONCURRENCY = [1, 5, 10, 20]


def _measure_latency_ssh(ip: str, path: str, iterations: int = _LATENCY_ITERATIONS) -> dict[str, float]:
    """Measure HTTP latency to the OpenWrt backend via SSH.

    Runs curl inside the OpenWrt VM to avoid SSH round-trip overhead.
    Returns dict with min, p50, p95, p99, mean (all in ms).
    """
    script = shlex.quote(
        f"for i in $(seq 1 {iterations}); do "
        f"curl -s -o /dev/null -w '%{{time_total}}\\n' "
        f"http://127.0.0.1:2121{path} 2>/dev/null; "
        f"done"
    )
    r = inner_ssh(ip, script, timeout=120)
    times = []
    for line in (r.stdout or "").strip().splitlines():
        try:
            times.append(float(line.strip()) * 1000)  # convert to ms
        except ValueError:
            continue

    if not times:
        return {"min": 0, "p50": 0, "p95": 0, "p99": 0, "mean": 0, "samples": 0}

    times.sort()
    n = len(times)
    return {
        "min": round(times[0], 2),
        "p50": round(statistics.median(times), 2),
        "p95": round(times[int(n * 0.95)], 2) if n >= 20 else round(times[-1], 2),
        "p99": round(times[int(n * 0.99)], 2) if n >= 100 else round(times[-1], 2),
        "mean": round(statistics.mean(times), 2),
        "samples": n,
    }


def _measure_concurrent_throughput(ip: str, path: str, concurrency: int, duration_s: int = 5) -> dict[str, Any]:
    """Measure concurrent request throughput from the Debian VM.

    Uses curl in a bash loop with background processes to simulate
    concurrent clients. Returns requests/sec and latency stats.
    """
    # Run on the Debian VM (more CPU available than OpenWrt)
    from lib.cloud_lab.constants import DEBIAN_IP
    from lib.cloud_lab.worker.inner_ssh import inner_ssh as _ssh

    script = shlex.quote(
        f"END_TIME=$(($(date +%s) + {duration_s})); "
        f"COUNTER=/tmp/tg-bench-counter-$$; "
        f"echo 0 > $COUNTER; "
        f"for i in $(seq 1 {concurrency}); do "
        f"  (while [ $(date +%s) -lt $END_TIME ]; do "
        f"    curl -s -o /dev/null http://{ip}:2121{path} 2>/dev/null && "
        f"    echo 1 >> $COUNTER; "
        f"  done) & "
        f"done; "
        f"wait; "
        f"cat $COUNTER | wc -l; "
        f"rm -f $COUNTER"
    )

    r = _ssh(DEBIAN_IP, script, timeout=duration_s + 30)
    try:
        total_requests = int((r.stdout or "0").strip())
    except ValueError:
        total_requests = 0

    return {
        "concurrency": concurrency,
        "duration_s": duration_s,
        "total_requests": total_requests,
        "requests_per_sec": round(total_requests / duration_s, 1) if duration_s > 0 else 0,
    }


def _collect_system_metrics(ip: str) -> dict[str, Any]:
    """Collect system metrics from the OpenWrt VM."""
    script = shlex.quote(
        "echo 'loadavg:' $(cut -d' ' -f1-3 /proc/loadavg); "
        "echo 'meminfo:' $(grep -E 'MemTotal|MemFree|MemAvailable' /proc/meminfo | tr '\\n' ' '); "
        "echo 'uptime:' $(cut -d' ' -f1 /proc/uptime); "
        "echo 'tollgate_proc:' $(ps | grep tollgate | grep -v grep | head -1 | awk '{print $1, $3, $4, $5}')"
    )
    r = inner_ssh(ip, script, timeout=15)

    metrics: dict[str, Any] = {"raw": (r.stdout or "").strip()}

    for line in (r.stdout or "").splitlines():
        if line.startswith("loadavg:"):
            parts = line.split()[1:]
            metrics["load_1min"] = float(parts[0]) if parts else 0
        elif line.startswith("uptime:"):
            metrics["uptime_s"] = float(line.split()[1]) if len(line.split()) > 1 else 0

    return metrics


def _collect_host_metrics() -> dict[str, Any]:
    """Collect GCP host VM metrics (the outer VM running QEMU)."""
    metrics: dict[str, Any] = {}

    # CPU info
    try:
        cpu_model = _run("grep 'model name' /proc/cpuinfo | head -1 | cut -d: -f2", timeout=5, check=False)
        metrics["cpu_model"] = cpu_model.stdout.strip()
        nproc = _run("nproc", timeout=5, check=False)
        metrics["cpu_count"] = int(nproc.stdout.strip()) if nproc.stdout.strip() else 0
    except Exception:
        pass

    # Memory
    try:
        mem = _run("grep MemAvailable /proc/meminfo", timeout=5, check=False)
        parts = mem.stdout.split()
        if len(parts) >= 2:
            metrics["mem_available_kb"] = int(parts[1])
    except Exception:
        pass

    # QEMU process CPU usage
    try:
        qemu_ps = _run("ps aux | grep qemu-system | grep -v grep | head -1", timeout=5, check=False)
        if qemu_ps.stdout.strip():
            parts = qemu_ps.stdout.split()
            metrics["qemu_cpu_percent"] = float(parts[2]) if len(parts) > 2 else 0
            metrics["qemu_mem_percent"] = float(parts[3]) if len(parts) > 3 else 0
    except Exception:
        pass

    # Machine type from metadata
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/machine-type",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            mt = resp.read().decode().strip()
            metrics["machine_type"] = mt.split("/")[-1]
    except Exception:
        pass

    return metrics


def _get_version_info(ip: str) -> dict[str, str]:
    """Get backend version info for benchmark context."""
    info: dict[str, str] = {}
    try:
        r = inner_ssh(ip, "opkg list-installed | grep tollgate-wrt || echo 'tollgate-wrt not found'", timeout=10)
        info["installed_package"] = r.stdout.strip()
    except Exception:
        pass
    return info


def run_benchmarks(config: WorkerConfig) -> dict[str, Any]:
    """Run the full benchmark suite. Called AFTER test publication.

    Returns benchmark results dict. Never raises — logs errors and continues.
    """
    results_dir = f"{RESULTS_ROOT}/{config.run_id}"
    benchmark_path = Path(results_dir) / "benchmarks.json"
    benchmark_path.parent.mkdir(parents=True, exist_ok=True)

    results: dict[str, Any] = {
        "run_id": config.run_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "backend": config.backend,
        "branch": config.sut_branch,
        "commit": (config.sut_commit or "")[:7],
        "endpoints": {},
        "throughput": [],
        "system": {},
        "host": {},
    }

    log.info("=== Benchmark suite starting ===")

    try:
        log.info("Collecting version info...")
        results["version"] = _get_version_info(OPENWRT_IP)
    except Exception as e:
        log.warning("Version collection failed: %s", e)

    try:
        log.info("Collecting host metrics...")
        results["host"] = _collect_host_metrics()
        mt = results["host"].get("machine_type", "unknown")
        log.info("Host machine_type=%s cpu_count=%s", mt, results["host"].get("cpu_count", "?"))
    except Exception as e:
        log.warning("Host metrics failed: %s", e)

    try:
        log.info("Collecting OpenWrt system metrics...")
        results["system"] = _collect_system_metrics(OPENWRT_IP)
    except Exception as e:
        log.warning("System metrics failed: %s", e)

    # HTTP endpoint latency benchmarks
    endpoints = ["/", "/usage", "/balance", "/whoami"]
    for endpoint in endpoints:
        try:
            log.info("Benchmarking latency: %s (%d iterations)...", endpoint, _LATENCY_ITERATIONS)
            latency = _measure_latency_ssh(OPENWRT_IP, endpoint)
            results["endpoints"][endpoint] = latency
            log.info(
                "  %s: p50=%.1fms p95=%.1fms p99=%.1fms mean=%.1fms (n=%d)",
                endpoint, latency["p50"], latency["p95"], latency["p99"],
                latency["mean"], latency["samples"],
            )
        except Exception as e:
            log.warning("Latency benchmark for %s failed: %s", endpoint, e)
            results["endpoints"][endpoint] = {"error": str(e)[:200]}

    # Concurrent throughput benchmarks
    for concurrency in _CONCURRENCY:
        try:
            log.info("Benchmarking throughput: concurrency=%d ...", concurrency)
            throughput = _measure_concurrent_throughput(OPENWRT_IP, "/", concurrency)
            results["throughput"].append(throughput)
            log.info(
                "  c=%d: %.1f req/s (%d requests in %ds)",
                concurrency, throughput["requests_per_sec"],
                throughput["total_requests"], throughput["duration_s"],
            )
        except Exception as e:
            log.warning("Throughput benchmark c=%d failed: %s", concurrency, e)
            results["throughput"].append({"concurrency": concurrency, "error": str(e)[:200]})

    # Summary stats
    all_latencies = []
    for ep_data in results["endpoints"].values():
        if "p50" in ep_data and isinstance(ep_data["p50"], (int, float)) and ep_data["p50"] > 0:
            all_latencies.append(ep_data["p50"])

    results["summary"] = {
        "avg_endpoint_p50_ms": round(statistics.mean(all_latencies), 2) if all_latencies else 0,
        "best_throughput_rps": max(
            (t.get("requests_per_sec", 0) for t in results["throughput"] if "requests_per_sec" in t),
            default=0,
        ),
        "machine_type": results.get("host", {}).get("machine_type", "unknown"),
        "cpu_model": results.get("host", {}).get("cpu_model", "unknown"),
        "cpu_count": results.get("host", {}).get("cpu_count", 0),
    }

    log.info(
        "=== Benchmarks complete: avg_p50=%.1fms best_rps=%.1f machine=%s ===",
        results["summary"]["avg_endpoint_p50_ms"],
        results["summary"]["best_throughput_rps"],
        results["summary"]["machine_type"],
    )

    # Write results to file
    benchmark_path.write_text(json.dumps(results, indent=2) + "\n")
    log.info("Benchmark results saved to %s", benchmark_path)

    return results


def publish_benchmark_to_nostr(config: WorkerConfig, results: dict[str, Any]) -> None:
    """Publish benchmark results as a Nostr NIP-94 event.

    Uses the same relay (relay.cashu.email) and blossom server as test
    results, but with a separate event kind (30079) and a 'benchmark' tag
    so tests.tollgate.me can render them separately.
    """
    import shutil

    nsec_file = os.environ.get("NSEC_FILE", "")
    if not nsec_file or not Path(nsec_file).exists():
        for candidate in [os.path.expanduser("~/nsec"), "/root/nsec"]:
            if Path(candidate).exists():
                nsec_file = candidate
                break
    if not nsec_file or not Path(nsec_file).exists():
        log.warning("Benchmark Nostr publish skipped: nsec not found")
        return

    if not shutil.which("nak"):
        log.warning("Benchmark Nostr publish skipped: nak not installed")
        return

    blossom = os.environ.get("BLOSSOM_SERVER", "https://blossom.psbt.me")
    relays = os.environ.get("NOSTR_RELAYS", "wss://relay.cashu.email")

    # Upload benchmark JSON to Blossom
    benchmark_file = f"{RESULTS_ROOT}/{config.run_id}/benchmarks.json"
    if not Path(benchmark_file).exists():
        log.warning("Benchmark file not found: %s", benchmark_file)
        return

    try:
        r = _run(
            f'nak blossom upload --server {shlex.quote(blossom)} '
            f'--sec {shlex.quote(Path(nsec_file).read_text().strip())} '
            f'{shlex.quote(benchmark_file)} < /dev/null',
            timeout=60, check=False,
        )
        import json as _json
        try:
            resp = _json.loads(r.stdout)
            blob_url = f"{blossom}/{resp.get('sha256', '')}"
        except Exception:
            blob_url = ""
    except Exception as e:
        log.warning("Blossom upload of benchmarks failed: %s", e)
        blob_url = ""

    # Publish NIP-94 event with benchmark tags
    summary = results.get("summary", {})
    content = _json.dumps({
        "type": "benchmark",
        "run_id": config.run_id,
        "backend": config.backend,
        "branch": config.sut_branch,
        "avg_endpoint_p50_ms": summary.get("avg_endpoint_p50_ms", 0),
        "best_throughput_rps": summary.get("best_throughput_rps", 0),
        "machine_type": summary.get("machine_type", "unknown"),
        "cpu_model": summary.get("cpu_model", "unknown"),
        "cpu_count": summary.get("cpu_count", 0),
        "blob_url": blob_url,
    })

    try:
        _run(
            f'nak event --sec {shlex.quote(Path(nsec_file).read_text().strip())} '
            f'-k 30079 '
            f'--tag d=tollgate-benchmark/{config.run_id} '
            f'--tag r={config.run_id} '
            f'--tag t=tollgate-benchmark '
            f'--tag backend={config.backend} '
            f'--tag machine={summary.get("machine_type", "unknown")} '
            f'-c {shlex.quote(content)} '
            f'{shlex.quote(relays)} < /dev/null',
            timeout=30, check=False,
        )
        log.info("Benchmark results published to Nostr (kind=30079, relay=%s)", relays)
    except Exception as e:
        log.warning("Benchmark Nostr publish failed: %s", e)
