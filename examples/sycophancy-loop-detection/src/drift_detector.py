"""
ARG Sycophancy Loop Detector

Implements the three detection signals from config/circuit-breaker/drift-detection.yaml:

  1. Vector drift   — cosine similarity between each turn's output embedding and
                      a ground-truth reference vector.  When similarity falls below
                      the configured threshold for `breach_window_turns` consecutive
                      turns, the circuit fires.

  2. Sycophancy     — affirmation ratio: fraction of turns where the model moved
                      its stated position toward the user's framing rather than
                      maintaining its prior position.  Fires when the ratio exceeds
                      `affirmation_ratio_threshold` over the session window (after
                      `minimum_turns` have elapsed).

  3. Reality testing (optional) — fires when factual_grounding_score drops below
                      `factual_grounding_threshold` for `breach_window_turns`
                      consecutive turns.  Disabled by default; enable for educational
                      AI deployments.

All thresholds are wired to the YAML defaults so unit tests can override them by
constructing DriftDetector with explicit kwargs rather than loading the config file.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class DriftSignal(str, Enum):
    VECTOR_DRIFT_BREACH       = "vector_drift_breach"
    SYCOPHANCY_LOOP_DETECTED  = "sycophancy_loop_detected"
    REALITY_TESTING_EROSION   = "reality_testing_erosion"


@dataclass
class TurnRecord:
    turn_index: int
    output_vector: list[float]
    """Embedding of the model's response for this turn."""

    ground_truth_vector: list[float]
    """Embedding of the 'correct' or 'calibrated' answer for this topic."""

    user_position_vector: list[float]
    """Embedding of the user's stated position / claim for this turn."""

    factual_grounding_score: float = 1.0
    """0-1 score: how well the response is grounded in structured content.
    1.0 = fully grounded; 0.0 = pure open-ended simulation."""

    cosine_similarity: float = field(init=False)
    """Computed on init: similarity between output_vector and ground_truth_vector."""

    affirmation: bool = field(init=False)
    """True when model moved its output closer to user_position than to ground truth."""

    def __post_init__(self) -> None:
        self.cosine_similarity = _cosine(self.output_vector, self.ground_truth_vector)
        gt_sim   = _cosine(self.output_vector, self.ground_truth_vector)
        user_sim = _cosine(self.output_vector, self.user_position_vector)
        self.affirmation = user_sim > gt_sim


@dataclass
class DriftEvent:
    signal: DriftSignal
    turn_index: int
    session_id: str
    value: float
    """Signal-specific value: similarity, affirmation ratio, or grounding score."""

    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

class DriftDetector:
    """
    Stateful per-session detector.  Feed one TurnRecord per model turn;
    the detector calls `on_event` (if provided) whenever a threshold fires.

    Parameters map directly to drift-detection.yaml:
      vector_drift_threshold      → vector_drift.threshold            (0.85)
      breach_window_turns         → vector_drift.breach_window_turns  (3)
      evaluation_window_turns     → vector_drift.evaluation_window_turns (10)
      affirmation_ratio_threshold → sycophancy.affirmation_ratio_threshold (0.80)
      minimum_turns               → sycophancy.minimum_turns          (5)
      reality_testing_enabled     → reality_testing.enabled           (False)
      factual_grounding_threshold → reality_testing.factual_grounding_threshold (0.70)
    """

    def __init__(
        self,
        session_id: str,
        vector_drift_threshold: float       = 0.85,
        breach_window_turns: int            = 3,
        evaluation_window_turns: int        = 10,
        affirmation_ratio_threshold: float  = 0.80,
        minimum_turns: int                  = 5,
        reality_testing_enabled: bool       = False,
        factual_grounding_threshold: float  = 0.70,
        on_event: Callable[[DriftEvent], None] | None = None,
    ) -> None:
        self.session_id                  = session_id
        self.vector_drift_threshold      = vector_drift_threshold
        self.breach_window_turns         = breach_window_turns
        self.evaluation_window_turns     = evaluation_window_turns
        self.affirmation_ratio_threshold = affirmation_ratio_threshold
        self.minimum_turns               = minimum_turns
        self.reality_testing_enabled     = reality_testing_enabled
        self.factual_grounding_threshold = factual_grounding_threshold
        self.on_event                    = on_event

        self._turns: list[TurnRecord]   = []
        self._events: list[DriftEvent]  = []
        self._breached: bool            = False  # circuit has fired

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def turns(self) -> list[TurnRecord]:
        return list(self._turns)

    @property
    def events(self) -> list[DriftEvent]:
        return list(self._events)

    @property
    def circuit_open(self) -> bool:
        return self._breached

    def record_turn(self, record: TurnRecord) -> list[DriftEvent]:
        """
        Process one model turn.  Returns any DriftEvents fired on this turn.
        Once the circuit is open, all calls are no-ops (caller must check
        circuit_open and terminate the session).
        """
        if self._breached:
            return []

        self._turns.append(record)
        fired: list[DriftEvent] = []

        fired.extend(self._check_vector_drift(record))
        fired.extend(self._check_sycophancy())
        if self.reality_testing_enabled:
            fired.extend(self._check_reality_testing(record))

        return fired

    # ------------------------------------------------------------------
    # Detection logic
    # ------------------------------------------------------------------

    def _check_vector_drift(self, record: TurnRecord) -> list[DriftEvent]:
        """
        Fire VECTOR_DRIFT_BREACH when the last `breach_window_turns` turns
        all have cosine_similarity < vector_drift_threshold.
        """
        window = self._turns[-self.breach_window_turns:]
        if len(window) < self.breach_window_turns:
            return []

        if all(t.cosine_similarity < self.vector_drift_threshold for t in window):
            avg_sim = sum(t.cosine_similarity for t in window) / len(window)
            event = DriftEvent(
                signal=DriftSignal.VECTOR_DRIFT_BREACH,
                turn_index=record.turn_index,
                session_id=self.session_id,
                value=avg_sim,
                detail={
                    "window_size": self.breach_window_turns,
                    "threshold": self.vector_drift_threshold,
                    "per_turn_similarities": [t.cosine_similarity for t in window],
                },
            )
            self._fire(event)
            return [event]
        return []

    def _check_sycophancy(self) -> list[DriftEvent]:
        """
        Fire SYCOPHANCY_LOOP_DETECTED when affirmation_ratio exceeds the
        threshold over the evaluation window (after minimum_turns).
        """
        if len(self._turns) < self.minimum_turns:
            return []

        window = self._turns[-self.evaluation_window_turns:]
        ratio = sum(1 for t in window if t.affirmation) / len(window)
        if ratio >= self.affirmation_ratio_threshold:
            event = DriftEvent(
                signal=DriftSignal.SYCOPHANCY_LOOP_DETECTED,
                turn_index=self._turns[-1].turn_index,
                session_id=self.session_id,
                value=ratio,
                detail={
                    "window_size": len(window),
                    "affirmation_count": sum(1 for t in window if t.affirmation),
                    "threshold": self.affirmation_ratio_threshold,
                },
            )
            self._fire(event)
            return [event]
        return []

    def _check_reality_testing(self, record: TurnRecord) -> list[DriftEvent]:
        """
        Fire REALITY_TESTING_EROSION when factual_grounding_score falls below
        the threshold for `breach_window_turns` consecutive turns.
        """
        window = self._turns[-self.breach_window_turns:]
        if len(window) < self.breach_window_turns:
            return []

        if all(t.factual_grounding_score < self.factual_grounding_threshold for t in window):
            avg_score = sum(t.factual_grounding_score for t in window) / len(window)
            event = DriftEvent(
                signal=DriftSignal.REALITY_TESTING_EROSION,
                turn_index=record.turn_index,
                session_id=self.session_id,
                value=avg_score,
                detail={
                    "window_size": self.breach_window_turns,
                    "threshold": self.factual_grounding_threshold,
                    "per_turn_scores": [t.factual_grounding_score for t in window],
                },
            )
            self._fire(event)
            return [event]
        return []

    def _fire(self, event: DriftEvent) -> None:
        self._events.append(event)
        self._breached = True
        if self.on_event:
            self.on_event(event)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def affirmation_ratio(self, window: int | None = None) -> float:
        """Current affirmation ratio over the last `window` turns (default: all turns)."""
        turns = self._turns[-window:] if window else self._turns
        if not turns:
            return 0.0
        return sum(1 for t in turns if t.affirmation) / len(turns)

    def average_similarity(self, window: int | None = None) -> float:
        """Average cosine similarity over the last `window` turns (default: all turns)."""
        turns = self._turns[-window:] if window else self._turns
        if not turns:
            return 1.0
        return sum(t.cosine_similarity for t in turns) / len(turns)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors.  Returns 0.0 for zero vectors."""
    if len(a) != len(b):
        raise ValueError(f"Vector dimension mismatch: {len(a)} vs {len(b)}")
    dot   = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)
