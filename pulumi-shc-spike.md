# Pulumi SHC Spike — Decision Note

**Date:** 2026-07-02
**Status:** Spike complete. **Recommendation: CONTINUE — as a bootstrap-only tool, not a replacement.**
**Spike location:** [`infra/pulumi-shc-spike/`](../infra/pulumi-shc-spike/)
**Comparison baseline:** `lib/cloud_lab/shc.py`, `lib/cloud_lab/shc_submit.py`, `lib/cloud_lab/provider.py` (the imperative SHC path)

## TL;DR

We ran a contained Pulumi spike that provisions a single disposable SHC VPS via
the local `shc-pulumi` dynamic provider. We executed the full lifecycle for
real (two VMs created and cancelled against the live SHC API) and verified
cancellation independently.

- **`pulumi preview` / `up` / `destroy` all work** end-to-end against real SHC.
- **Pulumi is materially cleaner** for the *VM lifecycle* (create/read/destroy)
  than the imperative code in `shc_submit.py` (~150 lines of client wrapping,
  idempotency-key juggling, and provisioning polling collapse to ~20 lines).
- **Pulumi does NOT replace** the worker-bootstrap step (SSH-in, run pytest,
  publish). SHC has no cloud-init, so that step is orthogonal to IaC choice.
- **Recommended next step:** add Pulumi **Automation API** as an *optional*
  `--pulumi` flag on `cloud-lab.py submit`, replacing only the SHC create/destroy
  calls. Keep the imperative path as default until reliability is proven.

## What worked

| Capability | Evidence |
|---|---|
| Local dynamic provider loads | `import shc_pulumi` resolves to `../shc-pulumi/src/shc_pulumi/` via editable install; no `pulumi plugin install` needed. |
| `pulumi preview` (dry-run) | Resolves `size="dev-1c-4gb"` → `package_id=80, pricing_id=241`, runs SHC credit pre-check, creates **no** VM. |
| `pulumi up` (real) | Created VM **#813** then **#814**, `status: ready`, `ip: 66.92.204.238`, `os_user: debian`. Provisioned in ~152 s. |
| `pulumi destroy` (real) | Cancelled both VMs via `cancel_vm(immediate=True)`; verified `get_vm(813)` and `get_vm(814)` both return `[not_found]`. |
| Secret masking | `api_key : [secret]` in all diffs. The key is read from `SHC_API_KEY` at runtime; never written to stack config. |
| Local backend isolation | `PULUMI_BACKEND_URL=file://.../.state/` — zero writes to the Pulumi service or `~/.pulumi`. |
| Size abstraction | `dev-1c-4gb` (cheapest) just works; `dev-2c-8gb` matches the imperative path's "2C/8GB Dev VPS Standard" (pkg 81/245). |
| SSH key injection at order time | The toolkit's `order_vm` accepts `ssh_key`, so the key is injected at creation — cleaner than the imperative path's separate `POST /vm/{id}/ssh-keys/apply-live` call after provisioning. |
| Auto-cancel on destroy | `auto_cancel=True` default means `pulumi destroy` cancels the SHC service with no extra wiring. |
| Idempotent re-runs | No manual `Idempotency-Key` juggling; Pulumi's state handles it. |

## What did not work / is missing

| Gap | Detail | Whose fault |
|---|---|---|
| **No custom cloud-init user-data** | Empirically verified on BOTH Dev VPS and NVMe (2026-07-02): SHC uses cloud-init (NoCloud seed CD-ROM) for its own provisioning, but the order API does not merge any user-supplied `user_data` into the seed — the backend auto-generates a fixed cloud-config from order fields only. On NVMe/SSD/HDD cloud-init **runs** (so order-time `ssh_key` is installed automatically); on Dev VPS cloud-init is **disabled by a marker file** (so `ssh_key` reaches the seed but is never consumed — use `apply_ssh_key_live`). Custom first-boot code therefore requires SSH-after-provisioning regardless of IaC choice. Full evidence: [`../shc-toolkit/docs/cloud-init.md`](../../shc-toolkit/docs/cloud-init.md). | SHC (would affect Terraform/any IaC equally) |
| **No tags / labels / metadata** | SHC exposes none on VMs. | SHC (by design) |
| **No region selection** | SHC operates a single location (Katy, TX). | SHC (by design) |
| **`preview` is not free** | Dynamic providers call the real SHC API during preview (size resolution + credit check). Not a VM creation, but an API call. | Pulumi dynamic-provider model |
| **Lease/kill-switch asymmetry** | The imperative path enforces a VM-internal lease via `at`/polling that auto-shuts-down even if the orchestrator dies. Pulumi's `pulumi destroy` only fires if the orchestrator is alive. | Architectural — see below |
| **One bug found in my own code** | First `pulumi up` errored on the stack export because `Output.concat()` requires strings and I passed an `int` (`service_id`). The VM was still created and recorded in state, so `pulumi destroy` recovered cleanly after the fix. Fixed in `__main__.py`. | Mine (not the provider) |

## Where Pulumi was cleaner

1. **Boilerplate collapse.** The imperative path in `shc_submit.py` + `shc.py`
   manually implements: Bearer auth, idempotency-key generation, the
   `X-User-Api-Confirm` confirmation-retry flow, provisioning-state polling,
   and the separate SSH-key-injection call. The Pulumi program expresses all of
   this as:
   ```python
   vm = SHCVMResource("runner", hostname=..., size="dev-2c-8gb",
                      api_key=..., ssh_key=..., auto_cancel=True)
   ```
2. **State management is free.** No hand-rolled run-dir JSON, no
   `service_id`-to-runner-dir bookkeeping. Pulumi state is the source of truth.
3. **Diff/preview UX.** `pulumi preview --diff` shows exactly what will change
   (size upgrade, hostname replacement, etc.) before touching SHC. The
   imperative path has no equivalent.
4. **Secret handling.** `pulumi.Output.secret()` masks the API key everywhere
   automatically; the imperative path relies on convention.
5. **Size upgrades are in-place.** Changing `size` triggers the SHC upgrade API
   (queued, prorated) — no destroy/recreate. The imperative path would need to
   implement this manually.
6. **Local backend isolation** is a one-liner (`PULUMI_BACKEND_URL=file://...`),
   which makes the whole spike trivially deletable.

## Where Pulumi was more awkward

1. **Worker bootstrap doesn't fit the resource model — and SHC gives us no alternative.** We empirically confirmed (2026-07-02, see [`../shc-toolkit/docs/cloud-init.md`](../../shc-toolkit/docs/cloud-init.md)) that SHC does **not** support custom cloud-init user-data on any tier. The real `cloud-lab-worker.sh` flow — SSH in, upload script, `nohup` it, poll for a done-marker — is therefore **mandatory** procedural work, not a choice. It can be shoehorned into a Pulumi `Command` resource, but that's strictly more complex than the current `subprocess.run(ssh_cmd(...))`.
2. **Dynamic providers can't be unit-tested in isolation.** Because
   `preview`/`up` call the real SHC API, there's no "mock the provider" mode at
   the Pulumi layer. (`shc-pulumi` itself has good mocked unit tests; the gap is
   at the *consumer* program level.)
3. **The lease/kill-switch model is genuinely different.** The imperative path
   is "fire-and-forget safe": the VM self-destructs at lease expiry even if the
   submitter process is killed. Pulumi's model is "orchestrator must survive to
   destroy". For fire-and-forget cloud-lab runs, this is a real regression
   unless we add a cron-based reaper that calls `pulumi destroy` on stale
   stacks.
4. **Provisioning latency is opaque.** `pulumi up` shows `creating (0s)` →
   `creating (150s)` with no intermediate feedback. The imperative path logs
   each provisioning-state poll. (Fixable with `pulumi.log.info` in the
   provider, not a fundamental issue.)

## Would Pulumi Automation API be useful?

**Yes — this is the most promising integration path.**

Pulumi Automation API lets you drive `preview`/`up`/`destroy` from inside a
Python program (no shell-out, no `.pulumi/` dir required). This means
`cloud-lab.py submit` could:

```python
# sketch — not implemented in the spike
from pulumi import automation as auto

stack = auto.create_or_select_stack(
    stack_name=run_id, project_name="tollgate-runner",
    program=lambda: runner_program(run_id, branch, ...),
    opts=auto.LocalWorkspaceOptions(workdir=spike_dir, secrets_provider="passphrase"),
)
stack.up()                          # creates the VM
outputs = stack.outputs()           # {ip, service_id, os_user, ssh_command}
bootstrap_worker(outputs["ip"], ...)  # existing SSH + cloud-lab-worker.sh
stack.destroy()                     # cancels the VM
```

This replaces the SHC-specific code in `shc_submit.py` (lines ~390–500:
balance check, `submit_order`, poll-for-ready, apply-ssh-key, idempotency key)
with ~20 lines of Automation API. The worker bootstrap and publish steps stay
exactly as they are.

**Bonus:** Automation API gives us programmatic access to `stack.outputs()` and
structured error handling, which is cleaner than scraping `pulumi up` stdout.

## Recommendation

**CONTINUE — but as a bootstrap-only tool, not a wholesale replacement.**

| Layer | Recommendation |
|---|---|
| SHC VM create/destroy | **Adopt Pulumi** (via Automation API, behind a `--pulumi` flag). ~150 lines of imperative SHC code → ~20 lines. |
| Worker bootstrap (SSH + `cloud-lab-worker.sh`) | **Keep imperative.** Orthogonal to IaC; SHC has no cloud-init. |
| Lease/kill-switch | **Keep imperative** for fire-and-forget safety, OR add a cron reaper that `pulumi destroy`s stacks older than N hours. |
| GCP cloud-lab | **Do not migrate.** GCP flow is mature, heavily customized (snapshots, hwsim, vwifi), and Pulumi's GCP provider would not simplify it. The spike scope is SHC only. |

### Why not "reject"

The SHC create/destroy code is the most boilerplate-heavy, least-differentiated
part of `shc_submit.py`. Pulumi collapses it dramatically, adds diff/preview
for free, and handles idempotency/state correctly. The cost is one new
dependency (pulumi + the local provider) and the lease-model difference (which
is solvable).

### Why not "replace everything"

The worker bootstrap, publish pipeline, and GCP flow are not Pulumi-shaped
problems. Migrating them would add ceremony without value, and the GCP flow has
too much bespoke logic (nested-KVM topology, hwsim, vwifi relay) to benefit from
declarative modeling.

## Concrete next steps (gradual adoption)

Rather than a side-by-side bake-off, adopt Pulumi incrementally behind a flag so
the imperative path remains the default and each step is independently
revertible:

1. **Automation API behind `--pulumi` (SHC create/destroy only).** Add an
   optional `--pulumi` flag to `scripts/cloud-lab.py submit` that uses Pulumi
   Automation API for the SHC VM create + destroy, inlining the program from
   this spike's `__main__.py` into a Python function. Keep
   `bootstrap_worker()` and `publish_report()` exactly as they are — they
   cannot move to Pulumi because SHC has no custom user-data (empirically
   verified). Default behavior is unchanged.
2. **Add a stale-stack reaper** to `scripts/cloud-lab.py cleanup-stale` that
   finds local Pulumi stacks older than 3 h and runs `destroy` + `stack rm`.
   This restores the fire-and-forget safety property the imperative lease
   provides.
3. **Promote `--pulumi` to default** once it has run cleanly in CI for a few
   real PRs, then deprecate the imperative SHC client (`lib/cloud_lab/shc.py`
   is already gitignored as "experimental").
4. **Do NOT** attempt to Pulumi-ize the GCP flow, the worker bootstrap, or the
   publish pipeline — none of them benefit from declarative modeling and SHC's
   lack of custom user-data makes the bootstrap permanently imperative.

## Reproducing the spike results

```bash
cd infra/pulumi-shc-spike
export SHC_API_KEY="shc_live_..."
./run-preview.sh   # dry-run, no VM
./run-up.sh        # creates VM (~2.5 min), prints ip/service_id
./run-destroy.sh   # cancels VM immediately
```

Two real VMs were created and cancelled during this spike (#813, #814). Both
were verified `not_found` via `SHCClient.get_vm()` after destroy. Total cost:
$0.00 (the `dev-1c-4gb` tier is currently billing $0.00/day per the provider's
cost audit).
