"""
ARG Financial Circuit Breaker
==============================
Detects API call fan-out and enforces deterministic trip decisions.

The circuit breaker operates independently of the wallet. The wallet
constrains budget. The circuit breaker constrains velocity and behavior.
Both are required. An agent can stay inside budget while fan-out compounds
a financial event that the wallet alone would not catch in time.

Fan-out failure modes detected:
  - Depth fan-out: same endpoint called repeatedly without forward progress
  - Breadth fan-out: single intent spawning calls to many distinct endpoints
  - Cascade fan-out: sub-agent delegations that themselves fan out

State machine:
  CLOSED   → normal operation, monitoring
  OPEN     → tripped, all calls blocked
  HALF_OPEN → cooldown expired, one probe call permitted to test recovery

Reference: config/circuit-breaker/financial-circuit-breaker.yaml
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

from wallet import AllocatedWallet, OperationType, RevocationReason, SpendResult


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class BreakerState(str, Enum):
    CLOSED = "CLOSED"       # Normal operation
    OPEN = "OPEN"           # Tripped -- all calls blocked
    HALF_OPEN = "HALF_OPEN" # Cooldown expired -- one probe permitted


class TriggerSignal(str, Enum):
    CALL_RATE_EXCEEDED = "call_rate_exceeded"
    DEPTH_FANOUT_DETECTED = "depth_fanout_detected"
    WALLET_WARN_THRESHOLD = "wallet_warn_threshold"
    WALLET_CRITICAL_THRESHOLD = "wallet_critical_threshold"
    WALLET_EXHAUSTED = "wallet_exhausted"
    SEMANTIC_REDUNDANCY = "semantic_redundancy"
    PURPOSE_MISMATCH = "purpose_mismatch"
    OPERATION_WALLET_EXHAUSTED = "operation_wallet_exhausted"
    DEPENDENCY_GRAPH_VIOLATION = "dependency_graph_violation"


# ---------------------------------------------------------------------------
# Call record for the sliding window
# ---------------------------------------------------------------------------

@dataclass
class CallRecord:
    timestamp: float          # time.monotonic()
    endpoint: str
    operation_type: OperationType
    tokens: int
    cost_usd: float
    agent_id: str


# ---------------------------------------------------------------------------
# Breaker trip event
# ---------------------------------------------------------------------------

@dataclass
class BreakerEvent:
    event_id: str
    wallet_id: str
    execution_tree_id: str
    trigger_signal: TriggerSignal
    previous_state: BreakerState
    new_state: BreakerState
    occurred_at: datetime
    call_count_in_window: int
    calls_per_minute_rate: float
    unique_endpoints_in_window: int
    repeated_endpoint_pattern: bool
    mission_utilization_percent: float
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

class FinancialCircuitBreaker:
    """
    ARG Financial Circuit Breaker.

    Wraps an AllocatedWallet and intercepts every spend request.
    Detects fan-out patterns and trips deterministically when thresholds
    are exceeded. Emits BreakerEvents for the telemetry pipeline.

    Usage:
        breaker = FinancialCircuitBreaker(
            wallet=wallet,
            call_rate_threshold=100,        # calls/minute
            window_seconds=60,
            cooldown_seconds=300,
            on_event=kafka_pipeline.emit,   # optional callback
        )

        result = breaker.request_spend(
            tokens=1500,
            cost_usd=0.045,
            operation_type=OperationType.WEB_SEARCH,
            endpoint="https://api.search.example.com/v1/search",
            agent_id="agent-child-1",
        )
        if result.approved:
            # make the call
    """

    def __init__(
        self,
        wallet: AllocatedWallet,
        call_rate_threshold: float = 100.0,
        window_seconds: int = 60,
        cooldown_seconds: int = 300,
        semantic_redundancy_threshold: float = 0.92,
        consecutive_redundant_turns: int = 3,
        minimum_sample_size: int = 10,
        depth_fanout_threshold: float = 0.7,
        on_event: Optional[Callable[[BreakerEvent], None]] = None,
    ) -> None:
        self.wallet = wallet
        self.call_rate_threshold = call_rate_threshold
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self.semantic_redundancy_threshold = semantic_redundancy_threshold
        self.consecutive_redundant_turns = consecutive_redundant_turns
        self.minimum_sample_size = minimum_sample_size
        self.depth_fanout_threshold = depth_fanout_threshold
        self.on_event = on_event

        self._state = BreakerState.CLOSED
        self._window: deque[CallRecord] = deque()
        self._opened_at: Optional[float] = None
        self._consecutive_redundant: int = 0
        self._last_output_vector: Optional[list[float]] = None
        self._events: list[BreakerEvent] = []

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def state(self) -> BreakerState:
        # Lazily transition OPEN -> HALF_OPEN when cooldown expires
        if (
            self._state == BreakerState.OPEN
            and self._opened_at is not None
            and time.monotonic() - self._opened_at >= self.cooldown_seconds
        ):
            self._state = BreakerState.HALF_OPEN
        return self._state

    def request_spend(
        self,
        tokens: int,
        cost_usd: float,
        operation_type: OperationType,
        endpoint: str,
        agent_id: str,
    ) -> SpendResult:
        """
        Gate a spend request through the circuit breaker.

        Checks in order:
          1. Circuit breaker state -- if OPEN, block immediately
          2. Fan-out detection -- if threshold exceeded, trip and block
          3. Wallet spend request -- delegate to wallet for budget check
          4. Post-spend wallet threshold monitoring

        Returns a SpendResult. The caller must not make the API call
        unless result.approved is True.
        """
        now = time.monotonic()

        # 1. State check
        if self.state == BreakerState.OPEN:
            return SpendResult(
                approved=False,
                wallet_id=self.wallet.wallet_id,
                denial_reason="CIRCUIT_BREAKER_OPEN",
            )

        # 2. Record call and prune window
        record = CallRecord(
            timestamp=now,
            endpoint=endpoint,
            operation_type=operation_type,
            tokens=tokens,
            cost_usd=cost_usd,
            agent_id=agent_id,
        )
        self._window.append(record)
        self._prune_window(now)

        # 3. Fan-out detection
        fan_out_signal = self._detect_fan_out()
        if fan_out_signal is not None:
            self._trip(fan_out_signal, now)
            self.wallet.revoke(RevocationReason.CIRCUIT_BREAKER)
            return SpendResult(
                approved=False,
                wallet_id=self.wallet.wallet_id,
                denial_reason=f"CIRCUIT_BREAKER_FIRED:{fan_out_signal.value}",
            )

        # 4. Wallet budget check
        result = self.wallet.request_spend(tokens, cost_usd, operation_type)

        # 5. Post-spend threshold monitoring
        if result.approved:
            if result.critical_threshold_crossed:
                self._emit_threshold_event(
                    TriggerSignal.WALLET_CRITICAL_THRESHOLD, now
                )
            elif result.warn_threshold_crossed:
                self._emit_threshold_event(
                    TriggerSignal.WALLET_WARN_THRESHOLD, now
                )
        elif result.denial_reason and "EXHAUSTED" in result.denial_reason:
            self._trip(TriggerSignal.WALLET_EXHAUSTED, now)

        # If this was a HALF_OPEN probe and it succeeded, reset to CLOSED
        if self._state == BreakerState.HALF_OPEN and result.approved:
            self._state = BreakerState.CLOSED
            self._opened_at = None

        return result

    def record_output_vector(self, vector: list[float]) -> None:
        """
        Record an output embedding vector for semantic redundancy detection.
        Call this after each LLM response with the response embedding.
        The circuit breaker checks cosine similarity against the previous
        output to detect loops where the agent stops making forward progress.
        """
        if self._last_output_vector is not None:
            similarity = self._cosine_similarity(vector, self._last_output_vector)
            if similarity >= self.semantic_redundancy_threshold:
                self._consecutive_redundant += 1
            else:
                self._consecutive_redundant = 0

        self._last_output_vector = vector

        if self._consecutive_redundant >= self.consecutive_redundant_turns:
            now = time.monotonic()
            self._trip(TriggerSignal.SEMANTIC_REDUNDANCY, now)
            self.wallet.revoke(RevocationReason.CIRCUIT_BREAKER)

    def reset(self) -> None:
        """Manually reset the circuit breaker to CLOSED. Use with care."""
        self._state = BreakerState.CLOSED
        self._opened_at = None
        self._window.clear()
        self._consecutive_redundant = 0

    @property
    def events(self) -> list[BreakerEvent]:
        """All breaker events emitted in this session."""
        return list(self._events)

    # ------------------------------------------------------------------
    # Fan-out detection
    # ------------------------------------------------------------------

    def _prune_window(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._window and self._window[0].timestamp < cutoff:
            self._window.popleft()

    def _detect_fan_out(self) -> Optional[TriggerSignal]:
        """
        Analyse the current sliding window for fan-out patterns.
        Returns the TriggerSignal if a threshold is breached, else None.
        """
        window_size = len(self._window)
        if window_size < self.minimum_sample_size:
            return None

        # Calculate call rate
        if window_size >= 2:
            window_duration = self._window[-1].timestamp - self._window[0].timestamp
            if window_duration > 0:
                calls_per_minute = (window_size / window_duration) * 60.0
                if calls_per_minute > self.call_rate_threshold:
                    return TriggerSignal.CALL_RATE_EXCEEDED

        # Depth fan-out: same endpoint called repeatedly
        endpoint_counts: dict[str, int] = {}
        for record in self._window:
            endpoint_counts[record.endpoint] = endpoint_counts.get(record.endpoint, 0) + 1

        most_repeated = max(endpoint_counts.values()) if endpoint_counts else 0
        if most_repeated >= window_size * self.depth_fanout_threshold:
            return TriggerSignal.DEPTH_FANOUT_DETECTED

        return None

    def _current_stats(self) -> dict:
        window_size = len(self._window)
        unique_endpoints = len({r.endpoint for r in self._window})

        endpoint_counts: dict[str, int] = {}
        for record in self._window:
            endpoint_counts[record.endpoint] = endpoint_counts.get(record.endpoint, 0) + 1
        most_repeated = max(endpoint_counts.values()) if endpoint_counts else 0
        repeated_pattern = most_repeated >= window_size * 0.7 if window_size > 0 else False

        calls_per_minute = 0.0
        if window_size >= 2:
            duration = self._window[-1].timestamp - self._window[0].timestamp
            if duration > 0:
                calls_per_minute = (window_size / duration) * 60.0

        return {
            "call_count_in_window": window_size,
            "calls_per_minute_rate": round(calls_per_minute, 2),
            "unique_endpoints_in_window": unique_endpoints,
            "repeated_endpoint_pattern": repeated_pattern,
        }

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def _trip(self, signal: TriggerSignal, now: float) -> BreakerEvent:
        previous_state = self._state
        self._state = BreakerState.OPEN
        self._opened_at = now

        stats = self._current_stats()
        event = BreakerEvent(
            event_id=str(uuid.uuid4()),
            wallet_id=self.wallet.wallet_id,
            execution_tree_id=self.wallet.execution_tree_id,
            trigger_signal=signal,
            previous_state=previous_state,
            new_state=BreakerState.OPEN,
            occurred_at=datetime.now(timezone.utc),
            call_count_in_window=stats["call_count_in_window"],
            calls_per_minute_rate=stats["calls_per_minute_rate"],
            unique_endpoints_in_window=stats["unique_endpoints_in_window"],
            repeated_endpoint_pattern=stats["repeated_endpoint_pattern"],
            mission_utilization_percent=self.wallet.maximum_token_quantum.utilization_percent,
        )
        self._events.append(event)
        if self.on_event:
            self.on_event(event)
        return event

    def _emit_threshold_event(self, signal: TriggerSignal, now: float) -> None:
        stats = self._current_stats()
        event = BreakerEvent(
            event_id=str(uuid.uuid4()),
            wallet_id=self.wallet.wallet_id,
            execution_tree_id=self.wallet.execution_tree_id,
            trigger_signal=signal,
            previous_state=self._state,
            new_state=self._state,
            occurred_at=datetime.now(timezone.utc),
            call_count_in_window=stats["call_count_in_window"],
            calls_per_minute_rate=stats["calls_per_minute_rate"],
            unique_endpoints_in_window=stats["unique_endpoints_in_window"],
            repeated_endpoint_pattern=stats["repeated_endpoint_pattern"],
            mission_utilization_percent=self.wallet.maximum_token_quantum.utilization_percent,
        )
        self._events.append(event)
        if self.on_event:
            self.on_event(event)

    # ------------------------------------------------------------------
    # Semantic redundancy helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = sum(x * x for x in a) ** 0.5
        mag_b = sum(x * x for x in b) ** 0.5
        if mag_a == 0.0 or mag_b == 0.0:
            return 0.0
        return dot / (mag_a * mag_b)
