"""
Unit tests for ARG Allocated Wallet.

Covers:
  - Normal spend approval and decrement
  - Mission budget exhaustion → EXHAUSTED
  - Burst reserve escalation flow (BURST_RESERVE_PENDING → approved → spend)
  - Burst reserve stuck-state fix: re-entering PENDING not possible after release
  - Operation wallet per-type budget enforcement
  - TTL expiry
  - Revocation
  - Dependency graph hash verification
"""

import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from wallet import (
    AllocatedWallet,
    AuthorizerType,
    OperationType,
    OperationWallet,
    RevocationReason,
    SpendResult,
    TaskCategory,
    WalletStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GRAPH = {
    "allowed_endpoints": [
        {"url_pattern": "https://api.example.com/*"},
    ],
    "blocked_endpoints": [],
}
GRAPH_HASH = AllocatedWallet.compute_dependency_graph_hash(GRAPH)


def make_wallet(
    token_limit: int = 10_000,
    cost_limit_usd: float = 1.00,
    ttl_seconds: int = 300,
    burst_reserve_tokens: int = 0,
    operation_wallets: list[OperationWallet] | None = None,
) -> AllocatedWallet:
    return AllocatedWallet.issue(
        execution_tree_id=str(uuid.uuid4()),
        token_limit=token_limit,
        cost_limit_usd=cost_limit_usd,
        ttl_seconds=ttl_seconds,
        dependency_graph_hash=GRAPH_HASH,
        appropriation_id="test-appropriation",
        intent_declaration="Unit test wallet",
        task_category=TaskCategory.RESEARCH,
        authorizer_id="test-user",
        authorizer_type=AuthorizerType.HUMAN,
        burst_reserve_tokens=burst_reserve_tokens,
        operation_wallets=operation_wallets,
    )


# ---------------------------------------------------------------------------
# Basic spend approval
# ---------------------------------------------------------------------------

class TestBasicSpend:
    def test_approved_within_budget(self):
        wallet = make_wallet(token_limit=1000)
        result = wallet.request_spend(tokens=500, cost_usd=0.01)
        assert result.approved
        assert result.tokens_approved == 500
        assert wallet.maximum_token_quantum.tokens_consumed == 500

    def test_utilization_percent_updates(self):
        wallet = make_wallet(token_limit=1000)
        wallet.request_spend(tokens=250, cost_usd=0.01)
        assert wallet.maximum_token_quantum.utilization_percent == 25.0

    def test_cost_decrements(self):
        wallet = make_wallet(token_limit=1000, cost_limit_usd=1.00)
        wallet.request_spend(tokens=100, cost_usd=0.50)
        assert wallet.maximum_token_quantum.cost_consumed_usd == 0.50

    def test_warn_threshold_flagged(self):
        wallet = make_wallet(token_limit=1000)
        wallet.request_spend(tokens=749, cost_usd=0.01)
        result = wallet.request_spend(tokens=1, cost_usd=0.001)
        assert result.approved
        assert result.warn_threshold_crossed

    def test_critical_threshold_flagged(self):
        wallet = make_wallet(token_limit=1000)
        wallet.request_spend(tokens=899, cost_usd=0.01)
        result = wallet.request_spend(tokens=1, cost_usd=0.001)
        assert result.approved
        assert result.critical_threshold_crossed

    def test_multiple_calls_accumulate(self):
        wallet = make_wallet(token_limit=1000)
        for _ in range(5):
            wallet.request_spend(tokens=100, cost_usd=0.01)
        assert wallet.maximum_token_quantum.tokens_consumed == 500


# ---------------------------------------------------------------------------
# Mission budget exhaustion
# ---------------------------------------------------------------------------

class TestMissionExhaustion:
    def test_over_token_limit_denied(self):
        wallet = make_wallet(token_limit=1000)
        result = wallet.request_spend(tokens=1001, cost_usd=0.01)
        assert not result.approved
        assert "EXHAUSTED" in result.denial_reason

    def test_over_cost_limit_denied(self):
        wallet = make_wallet(token_limit=10_000, cost_limit_usd=0.50)
        result = wallet.request_spend(tokens=100, cost_usd=0.51)
        assert not result.approved

    def test_status_set_to_exhausted(self):
        wallet = make_wallet(token_limit=100)
        wallet.request_spend(tokens=100, cost_usd=0.01)
        result = wallet.request_spend(tokens=1, cost_usd=0.001)
        assert not result.approved
        assert wallet.status == WalletStatus.EXHAUSTED

    def test_exhausted_wallet_denies_subsequent_calls(self):
        wallet = make_wallet(token_limit=100)
        wallet.request_spend(tokens=100, cost_usd=0.01)
        result = wallet.request_spend(tokens=1, cost_usd=0.001)
        assert not result.approved
        assert wallet.status == WalletStatus.EXHAUSTED


# ---------------------------------------------------------------------------
# Burst reserve
# ---------------------------------------------------------------------------

class TestBurstReserve:
    def test_entering_burst_pending_state(self):
        wallet = make_wallet(token_limit=1000, burst_reserve_tokens=100)
        # Effective limit is 900; exhaust it
        wallet.request_spend(tokens=900, cost_usd=0.01)
        result = wallet.request_spend(tokens=1, cost_usd=0.001)
        assert not result.approved
        assert result.denial_reason == "MISSION_BUDGET_EXHAUSTED_BURST_RESERVE_AVAILABLE"
        assert wallet.status == WalletStatus.BURST_RESERVE_PENDING
        assert result.burst_reserve_available == 100

    def test_all_calls_blocked_during_pending(self):
        wallet = make_wallet(token_limit=1000, burst_reserve_tokens=100)
        wallet.request_spend(tokens=900, cost_usd=0.01)
        wallet.request_spend(tokens=1, cost_usd=0.001)  # triggers PENDING
        result = wallet.request_spend(tokens=1, cost_usd=0.001)
        assert not result.approved
        assert result.denial_reason == "BURST_RESERVE_PENDING_APPROVAL"

    def test_release_enables_burst_spend(self):
        wallet = make_wallet(token_limit=1000, burst_reserve_tokens=100)
        wallet.request_spend(tokens=900, cost_usd=0.01)
        wallet.request_spend(tokens=1, cost_usd=0.001)  # triggers PENDING
        wallet.release_burst_reserve(
            approver_id="manager-1",
            approver_type=AuthorizerType.HUMAN,
            scope="emergency overage approved",
        )
        assert wallet.status == WalletStatus.ACTIVE
        result = wallet.request_spend(tokens=50, cost_usd=0.005)
        assert result.approved

    def test_release_on_non_pending_wallet_raises(self):
        wallet = make_wallet(token_limit=1000, burst_reserve_tokens=100)
        with pytest.raises(ValueError, match="BURST_RESERVE_PENDING"):
            wallet.release_burst_reserve(
                approver_id="manager-1",
                approver_type=AuthorizerType.HUMAN,
                scope="should fail",
            )

    def test_no_repeat_pending_after_burst_released_and_exhausted(self):
        """
        Regression: wallet must not re-enter BURST_RESERVE_PENDING after
        the burst reserve has been released and fully consumed.
        """
        wallet = make_wallet(token_limit=1000, burst_reserve_tokens=100)
        # Exhaust non-reserve tokens (effective limit = 900)
        wallet.request_spend(tokens=900, cost_usd=0.01)
        wallet.request_spend(tokens=1, cost_usd=0.001)  # → PENDING
        wallet.release_burst_reserve(
            approver_id="manager-1",
            approver_type=AuthorizerType.HUMAN,
            scope="approved",
        )
        # Exhaust the burst reserve
        wallet.request_spend(tokens=100, cost_usd=0.01)
        # Now fully exhausted — must be EXHAUSTED, not BURST_RESERVE_PENDING
        result = wallet.request_spend(tokens=1, cost_usd=0.001)
        assert not result.approved
        assert wallet.status == WalletStatus.EXHAUSTED
        assert result.denial_reason != "MISSION_BUDGET_EXHAUSTED_BURST_RESERVE_AVAILABLE"

    def test_authorization_chain_extended_on_release(self):
        wallet = make_wallet(token_limit=1000, burst_reserve_tokens=100)
        wallet.request_spend(tokens=900, cost_usd=0.01)
        wallet.request_spend(tokens=1, cost_usd=0.001)
        wallet.release_burst_reserve(
            approver_id="manager-1",
            approver_type=AuthorizerType.HUMAN,
            scope="approved for emergency",
        )
        chain = wallet.purpose_classification.authorization_chain
        assert len(chain) == 2
        assert chain[1].authorizer_id == "manager-1"


# ---------------------------------------------------------------------------
# Operation wallets
# ---------------------------------------------------------------------------

class TestOperationWallets:
    def make_wallet_with_ops(self) -> AllocatedWallet:
        return make_wallet(
            token_limit=10_000,
            operation_wallets=[
                OperationWallet(
                    operation_type=OperationType.WEB_SEARCH,
                    token_limit=3_000,
                ),
                OperationWallet(
                    operation_type=OperationType.LLM_INFERENCE,
                    token_limit=6_000,
                ),
            ],
        )

    def test_operation_wallet_decrements(self):
        wallet = self.make_wallet_with_ops()
        wallet.request_spend(tokens=500, cost_usd=0.01, operation_type=OperationType.WEB_SEARCH)
        op = wallet.purpose_classification.get_operation_wallet(OperationType.WEB_SEARCH)
        assert op.tokens_consumed == 500

    def test_operation_wallet_exhaustion_blocks_type(self):
        wallet = self.make_wallet_with_ops()
        wallet.request_spend(tokens=3_000, cost_usd=0.01, operation_type=OperationType.WEB_SEARCH)
        result = wallet.request_spend(tokens=1, cost_usd=0.001, operation_type=OperationType.WEB_SEARCH)
        assert not result.approved
        assert "OPERATION_BUDGET_EXHAUSTED" in result.denial_reason

    def test_other_operation_type_unaffected(self):
        wallet = self.make_wallet_with_ops()
        wallet.request_spend(tokens=3_000, cost_usd=0.01, operation_type=OperationType.WEB_SEARCH)
        result = wallet.request_spend(tokens=1_000, cost_usd=0.01, operation_type=OperationType.LLM_INFERENCE)
        assert result.approved

    def test_unknown_operation_type_falls_back_to_mission_limit(self):
        wallet = self.make_wallet_with_ops()
        result = wallet.request_spend(tokens=500, cost_usd=0.01, operation_type=OperationType.CODE_EXECUTION)
        assert result.approved

    def test_both_mission_and_op_wallet_decrement(self):
        wallet = self.make_wallet_with_ops()
        wallet.request_spend(tokens=1_000, cost_usd=0.01, operation_type=OperationType.LLM_INFERENCE)
        assert wallet.maximum_token_quantum.tokens_consumed == 1_000
        op = wallet.purpose_classification.get_operation_wallet(OperationType.LLM_INFERENCE)
        assert op.tokens_consumed == 1_000


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------

class TestTTL:
    def test_expired_wallet_denied(self):
        wallet = make_wallet(ttl_seconds=1)
        # Manually push expiry to the past
        wallet.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        result = wallet.request_spend(tokens=100, cost_usd=0.01)
        assert not result.approved
        assert result.denial_reason == "WALLET_EXPIRED"
        assert wallet.status == WalletStatus.EXPIRED

    def test_active_wallet_within_ttl(self):
        wallet = make_wallet(ttl_seconds=300)
        assert wallet.is_active


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------

class TestRevocation:
    def test_revoked_wallet_denied(self):
        wallet = make_wallet()
        wallet.revoke(RevocationReason.CIRCUIT_BREAKER)
        result = wallet.request_spend(tokens=100, cost_usd=0.01)
        assert not result.approved
        assert result.denial_reason == "WALLET_REVOKED"

    def test_revocation_sets_reason(self):
        wallet = make_wallet()
        wallet.revoke(RevocationReason.POLICY_VIOLATION)
        assert wallet.status == WalletStatus.REVOKED
        assert wallet.revocation_reason == RevocationReason.POLICY_VIOLATION
        assert wallet.revoked_at is not None


# ---------------------------------------------------------------------------
# Dependency graph verification
# ---------------------------------------------------------------------------

class TestDependencyGraph:
    def test_allowed_endpoint_passes(self):
        wallet = make_wallet()
        assert wallet.verify_endpoint("https://api.example.com/v1/search", GRAPH)

    def test_blocked_endpoint_fails(self):
        wallet = make_wallet()
        assert not wallet.verify_endpoint("https://evil.example.com/exfil", GRAPH)

    def test_tampered_graph_fails(self):
        wallet = make_wallet()
        tampered = dict(GRAPH)
        tampered["allowed_endpoints"] = [{"url_pattern": "https://evil.example.com/*"}]
        assert not wallet.verify_endpoint("https://evil.example.com/exfil", tampered)

    def test_hash_computation_is_deterministic(self):
        h1 = AllocatedWallet.compute_dependency_graph_hash(GRAPH)
        h2 = AllocatedWallet.compute_dependency_graph_hash(GRAPH)
        assert h1 == h2

    def test_hash_is_64_char_hex(self):
        h = AllocatedWallet.compute_dependency_graph_hash(GRAPH)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)
