# Data Allotment Testing

These tests validate that TollGate cuts client connectivity after a paid data allotment is consumed.

The automated Playwright coverage lives in `tests/protocol/data-allotment.spec.mjs`. It is opt-in because it intentionally consumes bandwidth and expects the host to be connected through a paid TollGate session.

```bash
TOLLGATE_ENABLE_DATA_ALLOTMENT_TESTS=true \
TOLLGATE_DATA_TEST_URL=https://nbg1-speed.hetzner.com/100MB.bin \
TOLLGATE_DATA_TEST_TIMEOUT=300 \
npx playwright test tests/data-allotment.spec.mjs --config=tests/playwright.config.mjs
```

Alternative traffic generators for manual investigation:

```bash
# TCP stream between two hosts
yes | pv | nc <server-ip> 9000

# Fixed-size transfer
dd if=/dev/zero bs=1M count=256 | nc <server-ip> 9000

# iperf3 if both endpoints have it installed
iperf3 -c <server-ip> -t 300
```

Do not commit generated reports, screenshots, packet captures, or bandwidth logs.
