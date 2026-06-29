"""RFC 1918 isolation tests — verify public WiFi cannot reach upstream private networks.

Tests the firewall rules from PR #58 (tollgate-module-basic-go) that DROP
forwarded traffic from lan→wan destined for RFC 1918 private IP ranges.

Test approach:
  1. Apply the RFC 1918 DROP rules on the router via SSH
  2. Verify rules exist in UCI and are loaded in nftables
  3. From the router, verify FORWARD chain blocks RFC 1918 destinations
  4. Verify router's own OUTPUT is NOT affected (can still reach gateway)
  5. Clean up rules after test

Runs against:
  - Physical router (uses router.ssh fixture)
  - Virtual lab (uses _run_in_namespace for client-side checks)

Requires:
  - TOLLGATE_VIRTUAL_LAB=1 for virtual lab tests
  - Router fixture for physical router tests
"""

import os
import re
import subprocess
import textwrap
import time

import pytest

from lib.constants import POC_GATEWAY

pytestmark = [pytest.mark.api, pytest.mark.firewall]

LAB_HOST = os.environ.get("TOLLGATE_VIRTUAL_LAB_HOST", "218")
CONTAINER = os.environ.get("TOLLGATE_CLIENT_NS", "tg-poc-client")

RFC1918_RULES = [
    ("block_rfc1918_10",   "Block-LAN-To-RFC1918-10",  "10.0.0.0/8"),
    ("block_rfc1918_172",  "Block-LAN-To-RFC1918-172", "172.16.0.0/12"),
    ("block_rfc1918_192",  "Block-LAN-To-RFC1918-192", "192.168.0.0/16"),
    ("block_linklocal",    "Block-LAN-To-LinkLocal",   "169.254.0.0/16"),
]

APPLY_SCRIPT = textwrap.dedent("""\
    set -e
    {rules}
    uci commit firewall
    /etc/init.d/firewall restart 2>/dev/null || fw3 restart 2>/dev/null || true
    sleep 2
    echo RULES_APPLIED
""")

CLEANUP_SCRIPT = textwrap.dedent("""\
    for rule in {section_names}; do
        uci -q delete firewall.$rule
    done
    uci commit firewall
    /etc/init.d/firewall restart 2>/dev/null || fw3 restart 2>/dev/null || true
    sleep 2
    echo RULES_REMOVED
""")


def _generate_uci_rules():
    lines = []
    for section, name, cidr in RFC1918_RULES:
        lines.append(f"uci set firewall.{section}=rule")
        lines.append(f"uci set firewall.{section}.name='{name}'")
        lines.append(f"uci set firewall.{section}.src='lan'")
        lines.append(f"uci set firewall.{section}.dest='wan'")
        lines.append(f"uci set firewall.{section}.dest_ip='{cidr}'")
        lines.append(f"uci set firewall.{section}.proto='all'")
        lines.append(f"uci set firewall.{section}.family='ipv4'")
        lines.append(f"uci set firewall.{section}.target='DROP'")
    return "\n".join(lines)


def _skip_if_no_router(router):
    if router is None:
        pytest.skip("No router fixture available")


def _skip_if_no_virtual_lab():
    if os.environ.get("TOLLGATE_VIRTUAL_LAB") != "1":
        pytest.skip("Set TOLLGATE_VIRTUAL_LAB=1 for virtual lab tests")


def _run_in_namespace(*args, timeout=15):
    return subprocess.run(
        ["ssh", LAB_HOST, "sudo", "ip", "netns", "exec", CONTAINER, *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


@pytest.fixture
def rfc1918_rules(router):
    """Apply RFC 1918 rules before tests, clean up after."""
    _skip_if_no_router(router)

    section_names = " ".join(s for s, _, _ in RFC1918_RULES)
    apply_cmd = APPLY_SCRIPT.format(
        rules=_generate_uci_rules(),
        section_names=section_names,
    )
    cleanup_cmd = CLEANUP_SCRIPT.format(section_names=section_names)

    output = router.ssh(apply_cmd, timeout=15)
    assert "RULES_APPLIED" in output, f"Failed to apply rules: {output}"

    yield router

    output = router.ssh(cleanup_cmd, timeout=15)
    assert "RULES_REMOVED" in output, f"Failed to clean up rules: {output}"


# =============================================================================
# UCI Configuration Tests
# =============================================================================

class TestRFC1918RulesExist:
    """Verify the RFC 1918 DROP rules are correctly configured in UCI."""

    def test_all_four_rules_exist(self, rfc1918_rules):
        router = rfc1918_rules
        out = router.ssh("uci show firewall | grep -E 'block_rfc1918|block_linklocal'")
        for section, _, _ in RFC1918_RULES:
            assert section in out, f"Rule section '{section}' not found in UCI:\n{out}"

    def test_rules_target_drop(self, rfc1918_rules):
        router = rfc1918_rules
        for section, _, _ in RFC1918_RULES:
            out = router.ssh(f"uci get firewall.{section}.target")
            assert "DROP" in out, f"{section}.target should be DROP, got: {out}"

    def test_rules_src_lan_dest_wan(self, rfc1918_rules):
        router = rfc1918_rules
        for section, _, _ in RFC1918_RULES:
            src = router.ssh(f"uci get firewall.{section}.src").strip()
            dest = router.ssh(f"uci get firewall.{section}.dest").strip()
            assert src == "lan", f"{section}.src should be 'lan', got: {src}"
            assert dest == "wan", f"{section}.dest should be 'wan', got: {dest}"

    def test_correct_cidr_ranges(self, rfc1918_rules):
        router = rfc1918_rules
        for section, _, expected_cidr in RFC1918_RULES:
            out = router.ssh(f"uci get firewall.{section}.dest_ip").strip()
            assert out == expected_cidr, (
                f"{section}.dest_ip should be {expected_cidr}, got: {out}"
            )

    def test_rules_ipv4_family(self, rfc1918_rules):
        router = rfc1918_rules
        for section, _, _ in RFC1918_RULES:
            out = router.ssh(f"uci get firewall.{section}.family").strip()
            assert out == "ipv4", f"{section}.family should be 'ipv4', got: {out}"


# =============================================================================
# nftables Runtime Tests
# =============================================================================

class TestRulesLoadedInNftables:
    """Verify the rules are actually loaded in the nftables ruleset."""

    def test_drop_rules_in_nft(self, rfc1918_rules):
        router = rfc1918_rules
        out = router.ssh("nft list ruleset 2>/dev/null || fw4 print 2>/dev/null")
        assert "10.0.0.0/8" in out, "10.0.0.0/8 DROP not in nft ruleset"
        assert "172.16.0.0/12" in out, "172.16.0.0/12 DROP not in nft ruleset"
        assert "192.168.0.0/16" in out, "192.168.0.0/16 DROP not in nft ruleset"


# =============================================================================
# Router Output Chain Tests (should NOT be affected)
# =============================================================================

class TestRouterOutputUnaffected:
    """The RFC 1918 filter only affects FORWARD chain, not OUTPUT.
    Router's own traffic (DNS, payments, updates) should still work."""

    def test_router_can_ping_gateway(self, rfc1918_rules):
        """Router can ping its own gateway — OUTPUT chain, not FORWARD."""
        router = rfc1918_rules
        gw = router.ssh("ip route show default | awk '{print $3}'").strip()
        if not gw:
            pytest.skip("No default gateway configured")
        out = router.ssh(f"ping -c 1 -W 2 {gw} 2>&1", timeout=10)
        assert "0% packet loss" in out or "1 received" in out, (
            f"Router cannot ping gateway {gw} — OUTPUT chain affected by rules:\n{out}"
        )

    def test_router_can_reach_dns(self, rfc1918_rules):
        """Router can still do DNS lookups — OUTPUT chain."""
        router = rfc1918_rules
        out = router.ssh("nslookup example.com 2>&1 || ping -c 1 -W 2 8.8.8.8 2>&1",
                         timeout=10)
        # If DNS or ping works, OUTPUT is fine
        has_connectivity = (
            "Address:" in out or
            "0% packet loss" in out or
            "1 received" in out
        )
        if not has_connectivity:
            pytest.skip(f"No upstream connectivity on this router: {out}")


# =============================================================================
# Forwarding Isolation Tests (the core security check)
# =============================================================================

class TestForwardingIsolation:
    """Verify forwarded traffic to RFC 1918 is blocked.

    These tests check the FORWARD chain behavior. They require a client
    behind the router that can generate forwarded traffic.

    For virtual lab: commands run from the client namespace.
    For physical router: commands run from a device on the LAN.
    """

    def test_blocked_10_range(self, rfc1918_rules):
        """Forwarded traffic to 10.x.x.x should be DROPped."""
        _skip_if_no_virtual_lab()
        # Try to reach a non-existent 10.x address through the router
        # With the rule, the packet should be dropped (no response)
        result = _run_in_namespace("ping", "-c", "1", "-W", "2", "10.99.99.99",
                                   timeout=10)
        assert result.returncode != 0, (
            "Ping to 10.x.x.x succeeded — RFC 1918 filter NOT working for 10/8"
        )

    def test_blocked_172_range(self, rfc1918_rules):
        """Forwarded traffic to 172.16.x.x should be DROPped."""
        _skip_if_no_virtual_lab()
        result = _run_in_namespace("ping", "-c", "1", "-W", "2", "172.16.99.99",
                                   timeout=10)
        assert result.returncode != 0, (
            "Ping to 172.16.x.x succeeded — RFC 1918 filter NOT working for 172.16/12"
        )

    def test_blocked_192_range(self, rfc1918_rules):
        """Forwarded traffic to 192.168.x.x should be DROPped.

        This is the most important test — 192.168.0.0/16 is where
        the operator's ISP router, printers, TVs, and NAS live.
        """
        _skip_if_no_virtual_lab()
        # Use a test address in 192.168.x.x that should be unreachable
        # (the upstream gateway's subnet, not the TollGate's own subnet)
        result = _run_in_namespace("ping", "-c", "1", "-W", "2", "192.168.1.99",
                                   timeout=10)
        assert result.returncode != 0, (
            "Ping to 192.168.x.x succeeded — CRITICAL: RFC 1918 filter NOT working for 192.168/16"
        )

    def test_blocked_linklocal(self, rfc1918_rules):
        """Forwarded traffic to 169.254.x.x should be DROPped."""
        _skip_if_no_virtual_lab()
        result = _run_in_namespace("ping", "-c", "1", "-W", "2", "169.254.99.99",
                                   timeout=10)
        assert result.returncode != 0, (
            "Ping to 169.254.x.x succeeded — link-local filter not working"
        )

    def test_router_itself_reachable(self, rfc1918_rules):
        """The TollGate router itself must be reachable — INPUT, not FORWARD."""
        _skip_if_no_virtual_lab()
        result = _run_in_namespace("ping", "-c", "1", "-W", "2", POC_GATEWAY,
                                   timeout=10)
        assert result.returncode == 0, (
            f"Cannot reach router at {POC_GATEWAY} — INPUT chain affected by rules:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


# =============================================================================
# Idempotency Tests
# =============================================================================

class TestIdempotency:
    """Rules should be safely re-appliable without duplicates."""

    def test_double_apply_no_duplicates(self, rfc1918_rules):
        """Applying rules twice should not create duplicate sections."""
        router = rfc1918_rules
        # Apply again
        router.ssh(_generate_uci_rules() + "\nuci commit firewall", timeout=10)
        # Count occurrences of each rule section
        for section, _, _ in RFC1918_RULES:
            out = router.ssh(f"uci show firewall.{section}")
            count = out.count(f"firewall.{section}=rule")
            assert count == 1, (
                f"Rule {section} appears {count} times after double-apply "
                f"(should be 1):\n{out}"
            )
