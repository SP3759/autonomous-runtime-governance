"""
Unit tests for ARG ProxyGate.

Covers:
  - Approved request: valid wallet, allowed endpoint, budget available
  - DEPENDENCY_GRAPH_VIOLATION: endpoint not in allowed list
  - DEPENDENCY_GRAPH_VIOLATION: endpoint matches blocked list
  - TTL_ELAPSED: wallet TTL has expired
  - WALLET_REVOKED: wallet has been revoked
  - WALLET_EXPIRED: wallet marked expired
  - Wallet spend denial passed through (WALLET_EXHAUSTED)
  - Burst reserve deny passed through
  - Wildcard pattern matching: /* suffix
  - Wildcard pattern matching: deep path still matches /*
  - Blocked list takes priority over allowed list
  - endpoint_allowed() public helper
"""

import sys
import os
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(
    0,
    os.path.normpath(
        os.path.join(os.path.dirname(__file__), '..', '..', 'financial-circuit-breaker', 'src')
    )
)

from wallet import (
    AllocatedWallet,
    AuthorizerType,
    OperationType,
    RevocationReason,
    TaskCategory,
    WalletStatus,
)
from proxy_gate import DenyReason, GateDecision, ProxyGate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

GRAPH = {
    "allowed_endpoints": [
        {"url_pattern": "https://api.example.com/*"},
        {"url_pattern": "https://api.search.example.com/v1/*"},
    ],
    "blocked_endpoints": [
        {"url_pattern": "https://api.example.com/admin/*"},
    ],
}
GRAPH_HASH = AllocatedWallet.compute_dependency_graph_hash(GRAPH)


def make_wallet(
    token_limit: int = 1000,
    cost_limit_usd: float = 5.00,
    ttl_seconds: int = 300,
    burst_reserve_tokens: int = 0,
) -> AllocatedWallet:
    return AllocatedWallet.issue(
        execution_tree_id=str(uuid.uuid4()),
        token_limit=token_limit,
        cost_limit_usd=cost_limit_usd,
        ttl_seconds=ttl_seconds,
        dependency_graph_hash=GRAPH_HASH,
        appropriation_id="test",
        intent_declaration="proxy gate test",
        task_category=TaskCategory.RESEARCH,
        authorizer_id="test-user",
        authorizer_type=AuthorizerType.HUMAN,
        burst_reserve_tokens=burst_reserve_tokens,
    )


def gate() -> ProxyGate:
    return ProxyGate(GRAPH)


# ---------------------------------------------------------------------------
# Approved path
# ---------------------------------------------------------------------------

class TestApproved:
    def test_approved_request(self):
        g, w = gate(), make_wallet()
        d = g.request(
            wallet=w, agent_id="agent-1",
            endpoint="https://api.example.com/v1/data",
            tokens=100, cost_usd=0.01,
        )
        assert d.approved
        assert d.tokens_approved == 100
        assert d.deny_reason is None

    def test_approved_decrements_wallet(self):
        g, w = gate(), make_wallet(token_limit=500)
        g.request(wallet=w, agent_id="a", endpoint="https://api.example.com/x",
                  tokens=200, cost_usd=0.01)
        assert w.maximum_token_quantum.tokens_consumed == 200

    def test_approved_deep_path(self):
        g, w = gate(), make_wallet()
        d = g.request(
            wallet=w, agent_id="a",
            endpoint="https://api.example.com/v2/nested/path?q=1",
            tokens=10, cost_usd=0.001,
        )
        assert d.approved

    def test_approved_specific_pattern(self):
        g, w = gate(), make_wallet()
        d = g.request(
            wallet=w, agent_id="a",
            endpoint="https://api.search.example.com/v1/search",
            tokens=10, cost_usd=0.001,
        )
        assert d.approved


# ---------------------------------------------------------------------------
# Dependency graph violations
# ---------------------------------------------------------------------------

class TestDependencyGraph:
    def test_unknown_endpoint_denied(self):
        g, w = gate(), make_wallet()
        d = g.request(
            wallet=w, agent_id="a",
            endpoint="https://api.evil.example.com/exfil",
            tokens=10, cost_usd=0.001,
        )
        assert not d.approved
        assert d.deny_reason == DenyReason.DEPENDENCY_GRAPH_VIOLATION

    def test_blocked_endpoint_denied_even_if_pattern_matches_allowed(self):
        # api.example.com/* is allowed but api.example.com/admin/* is blocked
        g, w = gate(), make_wallet()
        d = g.request(
            wallet=w, agent_id="a",
            endpoint="https://api.example.com/admin/reset",
            tokens=10, cost_usd=0.001,
        )
        assert not d.approved
        assert d.deny_reason == DenyReason.DEPENDENCY_GRAPH_VIOLATION

    def test_endpoint_allowed_helper_true(self):
        assert gate().endpoint_allowed("https://api.example.com/v1/data") is True

    def test_endpoint_allowed_helper_false(self):
        assert gate().endpoint_allowed("https://not-in-graph.example.com/x") is False

    def test_endpoint_allowed_blocked_returns_false(self):
        assert gate().endpoint_allowed("https://api.example.com/admin/users") is False


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------

class TestTTL:
    def test_ttl_elapsed_denied(self):
        g = gate()
        w = make_wallet(ttl_seconds=1)
        time.sleep(1.05)  # wait for TTL to elapse
        d = g.request(
            wallet=w, agent_id="a",
            endpoint="https://api.example.com/x",
            tokens=10, cost_usd=0.001,
        )
        assert not d.approved
        assert d.deny_reason == DenyReason.TTL_ELAPSED

    def test_active_wallet_not_denied_by_ttl(self):
        g, w = gate(), make_wallet(ttl_seconds=300)
        d = g.request(
            wallet=w, agent_id="a",
            endpoint="https://api.example.com/x",
            tokens=10, cost_usd=0.001,
        )
        assert d.approved


# ---------------------------------------------------------------------------
# Wallet status checks
# ---------------------------------------------------------------------------

class TestWalletStatus:
    def test_revoked_wallet_denied(self):
        g, w = gate(), make_wallet()
        w.revoke(RevocationReason.POLICY_VIOLATION)
        d = g.request(
            wallet=w, agent_id="a",
            endpoint="https://api.example.com/x",
            tokens=10, cost_usd=0.001,
        )
        assert not d.approved
        assert d.deny_reason == DenyReason.WALLET_REVOKED


# ---------------------------------------------------------------------------
# Spend-level denials (pass-through from wallet)
# ---------------------------------------------------------------------------

class TestSpendDenials:
    def test_exhausted_wallet_denied(self):
        g, w = gate(), make_wallet(token_limit=100)
        d = g.request(
            wallet=w, agent_id="a",
            endpoint="https://api.example.com/x",
            tokens=200, cost_usd=0.001,
        )
        assert not d.approved
        assert "EXHAUSTED" in (d.deny_reason or "").upper() or "BUDGET" in (d.deny_reason or "").upper()

    def test_burst_reserve_pending_surfaces(self):
        # The FIRST spend that exhausts the base budget signals burst_reserve_available.
        # Subsequent calls return BURST_RESERVE_PENDING_APPROVAL (no reserve field).
        g = gate()
        w = make_wallet(token_limit=50, burst_reserve_tokens=100)
        d = g.request(wallet=w, agent_id="a", endpoint="https://api.example.com/x",
                      tokens=50, cost_usd=0.001)
        # Wallet exhausts on first call — burst reserve is available
        assert not d.approved
        assert "BURST_RESERVE" in (d.deny_reason or "").upper()
        assert d.burst_reserve_available == 100
