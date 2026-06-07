"""
Unit tests for ARG Financial Circuit Breaker.

Covers:
  - CLOSED state: normal call flow, fan-out not yet triggered
  - OPEN state: trip on call rate velocity
  - OPEN state: trip on depth fan-out (repeated endpoint)
  - OPEN state: trip on semantic redundancy (cosine similarity)
  - OPEN state: trip on wallet exhaustion
  - HALF_OPEN state: cooldown expiry, probe allowed, reset to CLOSED
  - BreakerEvent emitted to on_event callback
  - Warn and critical threshold events (non-trip)
  - depth_fanout_threshold configurable
"""

import time
import uuid
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from wallet import (
    AllocatedWallet,
    AuthorizerType,
    OperationType,
    OperationWallet,
    RevocationReason,
    TaskCategory,
)
from circuit_breaker import (
    BreakerEvent,
    BreakerState,
    FinancialCircuitBreaker,
    TriggerSignal,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GRAPH = {
    "allowed_endpoints": [{"url_pattern": "https://api.example.com/*"}],
    "blocked_endpoints": [],
}
GRAPH_HASH = AllocatedWallet.compute_dependency_graph_hash(GRAPH)


def make_wallet(token_limit: int = 50_000, cost_limit_usd: float = 5.00) -> AllocatedWallet:
    return AllocatedWallet.issue(
        execution_tree_id=str(uuid.uuid4()),
        token_limit=token_limit,
        cost_limit_usd=cost_limit_usd,
        ttl_seconds=300,
        dependency_graph_hash=GRAPH_HASH,
        appropriation_id="test-appropriation",
        intent_declaration="Circuit breaker unit test",
        task_category=TaskCategory.RESEARCH,
        authorizer_id="test-user",
        authorizer_type=AuthorizerType.HUMAN,
        operation_wallets=[
            OperationWallet(
                operation_type=OperationType.WEB_SEARCH,
                token_limit=25_000,
            ),
            OperationWallet(
                operation_type=OperationType.LLM_INFERENCE,
                token_limit=20_000,
            ),
        ],
    )


def make_breaker(
    wallet: AllocatedWallet | None = None,
    call_rate_threshold: float = 60.0,
    window_seconds: int = 30,
    minimum_sample_size: int = 3,
    depth_fanout_threshold: float = 0.7,
    cooldown_seconds: int = 1,
    on_event=None,
) -> FinancialCircuitBreaker:
    return FinancialCircuitBreaker(
        wallet=wallet or make_wallet(),
        call_rate_threshold=call_rate_threshold,
        window_seconds=window_seconds,
        minimum_sample_size=minimum_sample_size,
        depth_fanout_threshold=depth_fanout_threshold,
        cooldown_seconds=cooldown_seconds,
        on_event=on_event,
    )


def spend(breaker: FinancialCircuitBreaker, endpoint: str = "https://api.example.com/v1/search",
          op: OperationType = OperationType.WEB_SEARCH, tokens: int = 100, cost: float = 0.003):
    return breaker.request_spend(
        tokens=tokens,
        cost_usd=cost,
        operation_type=op,
        endpoint=endpoint,
        agent_id="test-agent",
    )


# ---------------------------------------------------------------------------
# Normal operation (CLOSED state)
# ---------------------------------------------------------------------------

class TestNormalOperation:
    def test_single_call_approved(self):
        breaker = make_breaker()
        result = spend(breaker)
        assert result.approved
        assert breaker.state == BreakerState.CLOSED

    def test_multiple_calls_approved_below_threshold(self):
        breaker = make_breaker(call_rate_threshold=1000.0, minimum_sample_size=100)
        for _ in range(10):
            result = spend(breaker)
            assert result.approved

    def test_approved_call_decrements_wallet(self):
        wallet = make_wallet()
        breaker = make_breaker(wallet=wallet)
        spend(breaker, tokens=500)
        assert wallet.maximum_token_quantum.tokens_consumed == 500


# ---------------------------------------------------------------------------
# Velocity fan-out (CALL_RATE_EXCEEDED)
# ---------------------------------------------------------------------------

class TestVelocityTrip:
    def test_trips_on_high_call_rate(self):
        # threshold=20/min, window=1s, sample=3
        # Firing 10 calls in <0.1s → ~6000/min
        breaker = make_breaker(
            call_rate_threshold=20.0,
            window_seconds=1,
            minimum_sample_size=3,
        )
        results = [spend(breaker) for _ in range(10)]
        blocked = [r for r in results if not r.approved]
        assert len(blocked) > 0
        assert any("CIRCUIT_BREAKER_FIRED" in (r.denial_reason or "") for r in blocked)

    def test_state_becomes_open_after_trip(self):
        breaker = make_breaker(
            call_rate_threshold=20.0,
            window_seconds=1,
            minimum_sample_size=3,
        )
        for _ in range(10):
            spend(breaker)
        assert breaker.state == BreakerState.OPEN

    def test_wallet_revoked_after_trip(self):
        from wallet import WalletStatus
        wallet = make_wallet()
        breaker = make_breaker(
            wallet=wallet,
            call_rate_threshold=20.0,
            window_seconds=1,
            minimum_sample_size=3,
        )
        for _ in range(10):
            spend(breaker)
        assert wallet.status == WalletStatus.REVOKED

    def test_event_emitted_on_trip(self):
        events: list[BreakerEvent] = []
        breaker = make_breaker(
            call_rate_threshold=20.0,
            window_seconds=1,
            minimum_sample_size=3,
            on_event=events.append,
        )
        for _ in range(10):
            spend(breaker)
        trip_events = [e for e in events if e.new_state == BreakerState.OPEN]
        assert len(trip_events) >= 1
        assert trip_events[0].trigger_signal == TriggerSignal.CALL_RATE_EXCEEDED

    def test_open_breaker_blocks_all_subsequent_calls(self):
        breaker = make_breaker(
            call_rate_threshold=20.0,
            window_seconds=1,
            minimum_sample_size=3,
        )
        for _ in range(10):
            spend(breaker)
        result = spend(breaker)
        assert not result.approved
        assert result.denial_reason == "CIRCUIT_BREAKER_OPEN"


# ---------------------------------------------------------------------------
# Depth fan-out (DEPTH_FANOUT_DETECTED)
# ---------------------------------------------------------------------------

class TestDepthFanout:
    def test_trips_on_repeated_endpoint(self):
        # Use float('inf') velocity threshold to isolate depth fan-out detection.
        # threshold=0.7: trip when >=70% of calls in window go to same endpoint.
        breaker = make_breaker(
            call_rate_threshold=float("inf"),
            window_seconds=60,
            minimum_sample_size=3,
            depth_fanout_threshold=0.7,
        )
        same_endpoint = "https://api.example.com/v1/search"
        other_endpoint = "https://api.example.com/v1/classify"

        # 2 diverse calls (33%), then flood same endpoint — pushes past 70%
        spend(breaker, endpoint=other_endpoint)
        spend(breaker, endpoint=other_endpoint)
        results = []
        for _ in range(8):
            results.append(spend(breaker, endpoint=same_endpoint))

        blocked = [r for r in results if not r.approved]
        assert len(blocked) > 0
        assert any("depth_fanout_detected" in (r.denial_reason or "") for r in blocked)

    def test_depth_fanout_threshold_configurable(self):
        # With threshold=0.99, 70% same-endpoint calls should NOT trip
        breaker = make_breaker(
            call_rate_threshold=float("inf"),
            window_seconds=60,
            minimum_sample_size=3,
            depth_fanout_threshold=0.99,
        )
        same = "https://api.example.com/v1/search"
        other = "https://api.example.com/v1/classify"
        # 2 diverse, 4 same → 67% same, below 0.99 threshold
        spend(breaker, endpoint=other)
        spend(breaker, endpoint=other)
        results = [spend(breaker, endpoint=same) for _ in range(4)]
        assert all(r.approved for r in results)
        assert breaker.state == BreakerState.CLOSED


# ---------------------------------------------------------------------------
# Semantic redundancy
# ---------------------------------------------------------------------------

class TestSemanticRedundancy:
    def _identical_vector(self, dim: int = 10) -> list[float]:
        return [1.0] + [0.0] * (dim - 1)

    def test_trips_on_consecutive_identical_vectors(self):
        events: list[BreakerEvent] = []
        breaker = make_breaker(on_event=events.append)
        breaker.consecutive_redundant_turns = 3

        v = self._identical_vector()
        for _ in range(4):
            breaker.record_output_vector(v)

        trip_events = [e for e in events if e.new_state == BreakerState.OPEN]
        assert len(trip_events) >= 1
        assert trip_events[0].trigger_signal == TriggerSignal.SEMANTIC_REDUNDANCY

    def test_diverse_vectors_do_not_trip(self):
        breaker = make_breaker()
        vectors = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
            [1.0, 1.0, 0.0],
        ]
        for v in vectors:
            breaker.record_output_vector(v)
        assert breaker.state == BreakerState.CLOSED

    def test_wallet_revoked_on_semantic_redundancy_trip(self):
        from wallet import WalletStatus
        wallet = make_wallet()
        breaker = make_breaker(wallet=wallet)
        breaker.consecutive_redundant_turns = 3
        v = self._identical_vector()
        for _ in range(4):
            breaker.record_output_vector(v)
        assert wallet.status == WalletStatus.REVOKED


# ---------------------------------------------------------------------------
# Wallet exhaustion trip
# ---------------------------------------------------------------------------

class TestWalletExhaustionTrip:
    def test_trips_when_wallet_exhausted(self):
        wallet = make_wallet(token_limit=500, cost_limit_usd=1.00)
        breaker = make_breaker(
            wallet=wallet,
            call_rate_threshold=10_000.0,
            minimum_sample_size=1,
        )
        # Exhaust the wallet
        result = spend(breaker, tokens=501, cost=0.01)
        assert not result.approved

    def test_exhaustion_event_emitted(self):
        events: list[BreakerEvent] = []
        wallet = make_wallet(token_limit=300, cost_limit_usd=10.00)
        # minimum_sample_size=100 keeps fan-out detection inactive for 2 calls
        breaker = make_breaker(
            wallet=wallet,
            call_rate_threshold=float("inf"),
            minimum_sample_size=100,
            on_event=events.append,
        )
        spend(breaker, tokens=300, cost=0.01)  # exact limit
        spend(breaker, tokens=1, cost=0.001)   # triggers exhaustion trip
        exhaustion_events = [
            e for e in events
            if e.trigger_signal == TriggerSignal.WALLET_EXHAUSTED
        ]
        assert len(exhaustion_events) >= 1


# ---------------------------------------------------------------------------
# HALF_OPEN recovery
# ---------------------------------------------------------------------------

class TestHalfOpen:
    def test_transitions_to_half_open_after_cooldown(self):
        # cooldown_seconds=0 means the lazy state property immediately
        # transitions OPEN → HALF_OPEN on the first state read after tripping.
        breaker = make_breaker(
            call_rate_threshold=20.0,
            window_seconds=1,
            minimum_sample_size=3,
            cooldown_seconds=0,
        )
        for _ in range(10):
            spend(breaker)
        # State is lazily computed; with cooldown=0 it is already HALF_OPEN
        assert breaker.state in (BreakerState.OPEN, BreakerState.HALF_OPEN)
        time.sleep(0.05)
        assert breaker.state == BreakerState.HALF_OPEN

    def test_reset_clears_state(self):
        breaker = make_breaker(
            call_rate_threshold=20.0,
            window_seconds=1,
            minimum_sample_size=3,
        )
        for _ in range(10):
            spend(breaker)
        breaker.reset()
        assert breaker.state == BreakerState.CLOSED


# ---------------------------------------------------------------------------
# Threshold events (non-trip)
# ---------------------------------------------------------------------------

class TestThresholdEvents:
    def test_warn_event_emitted_at_75_percent(self):
        events: list[BreakerEvent] = []
        wallet = make_wallet(token_limit=1000, cost_limit_usd=100.0)
        breaker = make_breaker(
            wallet=wallet,
            call_rate_threshold=10_000.0,
            minimum_sample_size=100,
            on_event=events.append,
        )
        # Spend 749 then 1 more to cross 75%
        spend(breaker, tokens=749, cost=0.001)
        spend(breaker, tokens=1, cost=0.001)

        warn_events = [
            e for e in events
            if e.trigger_signal == TriggerSignal.WALLET_WARN_THRESHOLD
        ]
        assert len(warn_events) >= 1
        assert breaker.state == BreakerState.CLOSED  # should NOT have tripped

    def test_critical_event_emitted_at_90_percent(self):
        events: list[BreakerEvent] = []
        wallet = make_wallet(token_limit=1000, cost_limit_usd=100.0)
        breaker = make_breaker(
            wallet=wallet,
            call_rate_threshold=10_000.0,
            minimum_sample_size=100,
            on_event=events.append,
        )
        spend(breaker, tokens=899, cost=0.001)
        spend(breaker, tokens=1, cost=0.001)

        critical_events = [
            e for e in events
            if e.trigger_signal == TriggerSignal.WALLET_CRITICAL_THRESHOLD
        ]
        assert len(critical_events) >= 1
        assert breaker.state == BreakerState.CLOSED

    def test_warn_and_critical_are_distinct_signals(self):
        assert TriggerSignal.WALLET_WARN_THRESHOLD != TriggerSignal.WALLET_CRITICAL_THRESHOLD


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------

class TestEventLog:
    def test_events_list_populated_on_trip(self):
        breaker = make_breaker(
            call_rate_threshold=20.0,
            window_seconds=1,
            minimum_sample_size=3,
        )
        for _ in range(10):
            spend(breaker)
        assert len(breaker.events) >= 1

    def test_events_returns_copy(self):
        breaker = make_breaker()
        events1 = breaker.events
        events2 = breaker.events
        assert events1 is not events2
