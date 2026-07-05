# Cashu mint fee causes allotment shortfall (4 sat → 3 sat) and breaks 1-sat payments

## Problem

When a user pays for internet access via Cashu token on TollGate, the Go backend (gonuts) credits the **post-fee** amount rather than the token's face value. This causes a silent allotment shortfall on every payment.

**Concrete example with testnut keyset `008e808b89acc141` (fee_ppk=10, 1%):**

| User pays | gonuts receives | Steps credited | Data at 5000 ms/step | Expected |
|-----------|-----------------|----------------|----------------------|----------|
| 4 sat     | 3 sat           | 3              | 30 MiB               | 40 MiB   |
| 2 sat     | 1 sat           | 1              | 10 MiB               | 20 MiB   |
| 1 sat     | **0 sat**       | **0**          | **NOTHING**           | 10 MiB   |

**The user sees "30.00 MB purchased" instead of "40.00 MB" on the captive portal.**

The 1-sat case is **completely broken**: the user pays but receives zero access. The minimum viable payment with this keyset is **2 sats** (yields 1 sat after fee).

## Reproduction

### Prerequisites
- TollGate router with Go backend deployed
- `testnut.cashu.exchange` configured as accepted mint
- `price_per_step=1`, `step_size=5000`, `metric=milliseconds`
- `cashu` CLI installed (see `scripts/setup-cashu.sh`)

### Step 1: Verify mint fee

```bash
# Query the mint's keyset info
curl -s https://testnut.cashu.exchange/v1/keys | jq '.keysets[] | select(.active) | {id, unit, fee_ppk}'
```

Expected output:
```json
{
  "id": "008e808b89acc141",
  "unit": "sat",
  "fee_ppk": 10
}
```

### Step 2: Mint a 4-sat token

```bash
source ~/.tollgate-test-venv/bin/activate
source /opt/cashu-venv/bin/activate

cashu send 4 --legacy
# Returns: cashuAeyJwcm...
```

### Step 3: Pay via the backend API

```bash
ssh root@<router> "wget -qO- --post-data='{\"token\":\"cashuA...\"}' \
  --header='Content-Type: application/json' \
  'http://[::1]:2121/'"
```

### Step 4: Observe the shortfall

Check the router logs:
```bash
ssh root@<router> "logread | grep -i tollgate | tail -20"
```

Expected log evidence:
```
TollWallet.Receive: Token mint: https://testnut.cashu.exchange
aiming for 3 sats with 3 sats tolerance   # ← 3, not 4!
```

Check the portal response — it will show "30.00 MB purchased" instead of "40.00 MB".

### Step 5: Verify the 1-sat case is broken

```bash
# Mint 1 sat
cashu send 1 --legacy

# Pay
ssh root@<router> "wget -qO- --post-data='{\"token\":\"cashuA...\"}' \
  --header='Content-Type: application/json' \
  'http://[::1]:2121/'"

# Result: 0 steps credited, no access granted
# User's token is consumed but they get NOTHING
```

## Root Cause

The fee deduction happens inside gonuts's `Wallet.Receive()` method. The Cashu NUT-00 protocol specifies that the mint charges a fee (fee_ppk, in parts per thousand) when receiving tokens. gonuts correctly pays this fee to the mint, but then **credits the post-fee balance** to the user's session.

The call chain:
```
POST / (token payment)
  → main.go: handlePaymentPost()
    → merchant.Pay(token)
      → tollwallet.PayToken(token, mintURL)
        → wallet.Receive()          # gonuts deducts fee here
        → returns post-fee balance   # ← THIS is the problem
      → credits balance / price_per_step = steps
```

The backend should credit the **token face value** (what the user paid), not the wallet balance after the mint takes its cut. The mint fee is a cost of doing business for the TollGate operator, not something the user should absorb.

## Impact Matrix

All amounts assume `fee_ppk=10` (testnut `008e808b89acc141`):

| Token Face Value | Fee Deducted | Received by Backend | Steps (at 1 sat/step) | Data (5s/step) | Shortfall |
|------------------|-------------|--------------------|-----------------------|----------------|-----------|
| 1 sat            | 1 sat       | **0 sat**          | **0**                 | **0 MB**       | **100%**  |
| 2 sat            | 1 sat       | 1 sat              | 1                     | 10 MB          | 50%       |
| 4 sat            | 1 sat       | 3 sat              | 3                     | 30 MB          | 25%       |
| 8 sat            | 1 sat       | 7 sat              | 7                     | 70 MB          | 12.5%     |
| 10 sat           | 1 sat       | 9 sat              | 9                     | 90 MB          | 10%       |
| 20 sat           | 1 sat       | 18 sat             | 18                    | 180 MB         | 10%       |
| 50 sat           | 1 sat       | 49 sat             | 49                    | 490 MB         | 2%        |
| 100 sat          | 1 sat       | 99 sat             | 99                    | 990 MB         | 1%        |

**Note**: The fee calculation is `floor(face_value * (1000 - fee_ppk) / 1000)`, so for fee_ppk=10:
- 4 sat → floor(4 × 990/1000) = floor(3.96) = **3 sat** — confirmed by log evidence
- 1 sat → floor(1 × 990/1000) = floor(0.99) = **0 sat** — zero credit

**Minimum viable payment formula**: `ceil(1000 / (1000 - fee_ppk))`
- fee_ppk=0: min = **1 sat**
- fee_ppk=10: min = ceil(1000/990) = **2 sats**
- fee_ppk=100: min = ceil(1000/900) = **2 sats**
- fee_ppk=500: min = ceil(1000/500) = **2 sats**
- fee_ppk=999: min = ceil(1000/1) = **1000 sats**
- fee_ppk≥1000: **no viable payment** (fee exceeds amount)

## Workaround

### Option A: Increase token amount (user-side)

Mint more sats than needed. To get 4 sats credited with fee_ppk=10, mint 5 sats:
- 5 sat token → floor(5 × 990/1000) = floor(4.95) = **4 sat credited** ✓

This is not practical for end users who don't know the mint's fee.

### Option B: Increase price_per_step (router-side)

Set `price_per_step` to at least the minimum viable payment:

```bash
# Run the fee detection hotpatch in patch mode
TOLLGATE_SSH_HOST=192.168.13.112 ./scripts/fee-hotpatch.sh --patch
```

This sets `price_per_step=2` (for fee_ppk=10), so:
- 2 sat token → 1 sat after fee → 1 step = 10 MiB (at 5s/step) ✓
- The minimum price becomes 2 sats instead of 1
- **Limitation**: users still get less data per sat than expected (1 step per 2 sats instead of 2)

### Option C: Disable the fee at the mint (operator-side)

If you control the mint, set `fee_ppk=0` in the mint configuration.

## Proposed Fix

The fix should be in the Go backend's payment processing. Two approaches:

### Approach 1: Credit face value, absorb fee (recommended)

The backend controls the wallet. The mint fee is an operational cost. Credit the user based on the **token face value** (sum of proof amounts), not the wallet balance after receive.

```go
// In merchant/payment.go or equivalent
func (m *Merchant) PayToken(token string, mintURL string) (int, error) {
    // Parse token to get face value BEFORE calling Receive()
    faceValue := parseTokenFaceValue(token)  // sum of proof amounts
    
    // Receive (fee is paid to mint internally)
    _, err := m.wallet.Receive(token, mintURL)
    if err != nil {
        return 0, err
    }
    
    // Credit FACE VALUE, not post-fee balance
    steps := faceValue / m.pricePerStep
    return steps, nil
}
```

### Approach 2: Calculate from proof amounts directly

Extract proof amounts from the decoded token and use those directly:

```go
// The token is base64-encoded JSON with proofs
// Each proof has an "amount" field
// Sum of amounts = face value = what the user paid
tokenAmounts := extractProofAmounts(token)
totalFaceValue := sum(tokenAmounts)
steps := totalFaceValue / config.PricePerStep
```

This avoids depending on gonuts's internal balance tracking entirely.

### Why the backend should absorb the fee

1. **User expectation**: Paying 4 sats should give 4 steps. The user has no way to know about mint fees.
2. **Operator controls the wallet**: The fee goes to the mint the operator configured. It's a cost of running the service.
3. **Fee varies by keyset**: Different mints have different fees. The user doesn't choose the keyset.
4. **Current behavior is deceptive**: The portal says "X MB purchased" but gives less. This breaks trust.

## Hotpatch

See [`scripts/fee-hotpatch.sh`](../scripts/fee-hotpatch.sh) for a router-side mitigation script that:

1. Queries each configured mint's `/v1/keys` for `fee_ppk`
2. Calculates the minimum viable payment per mint
3. Prints a detailed fee impact table per mint
4. Warns if `price_per_step` is below the minimum
5. Optionally patches `price_per_step` with `--patch` flag
6. Optionally sets a specific `step_size` with `--set-step-size N`

This is a **workaround**, not a fix. The proper fix must be in the backend code.

## Environment

- **TollGate backend**: Go v1 (`tollgate-module-basic-go`)
- **Wallet**: `gonuts-tollgate` (fork of `gonuts`)
- **Mint**: `testnut.cashu.exchange` (FakeWallet, `fee_ppk: 10`)
- **Keyset**: `008e808b89acc141` (V1, active, `unit: sat`)
- **Router**: GL.iNet GL-MT3000, OpenWrt

## Related

- testnut keyset: `008e808b89acc141`, unit: sat, active: True, fee_ppk: 10
- gonuts `Wallet.Receive()` performs fee deduction per NUT-00
- Portal showed "30.00 MB purchased" instead of "40.00 MB" during lab testing
- Router log: `aiming for 3 sats with 3 sats tolerance` (from a 4-sat token)
- Cashu token version compatibility: Go backend accepts V1 and V3 tokens only (see `docs/portal-test-findings.md`)
