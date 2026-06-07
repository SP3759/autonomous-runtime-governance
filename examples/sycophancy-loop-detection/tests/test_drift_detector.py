"""
Unit tests for ARG DriftDetector.

Covers:
  - Cosine similarity computation
  - Vector drift breach: fires after breach_window_turns consecutive below-threshold turns
  - Vector drift: does NOT fire if breach window is not yet full
  - Sycophancy loop: fires when affirmation ratio exceeds threshold (after minimum_turns)
  - Sycophancy: does NOT fire before minimum_turns
  - Sycophancy: does NOT fire when affirmation ratio is below threshold
  - Reality testing erosion (enabled explicitly)
  - Circuit open: no events after first fire
  - Multiple signals on same turn (vector + sycophancy simultaneously)
  - Diagnostic helpers: affirmation_ratio(), average_similarity()
"""

import math
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from drift_detector import DriftDetector, DriftEvent, DriftSignal, TurnRecord, _cosine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(v: list[float]) -> list[float]:
    mag = math.sqrt(sum(x * x for x in v))
    if mag == 0:
        return v
    return [x / mag for x in v]


GT    = _norm([1.0, 0.0, 0.0, 0.0])
USER  = _norm([0.0, 1.0, 0.0, 0.0])
MIXED = _norm([0.5, 0.5, 0.0, 0.0])  # equidistant from GT and USER

# A vector very close to GT (similarity ~1.0)
NEAR_GT   = _norm([0.98, 0.02, 0.0, 0.0])
# A vector far from GT — deliberately below 0.85 threshold
FAR_GT    = _norm([0.2, 0.8, 0.0, 0.0])


def record(
    i: int,
    output: list[float] = None,
    gt: list[float] = None,
    user: list[float] = None,
    grounding: float = 1.0,
) -> TurnRecord:
    return TurnRecord(
        turn_index=i,
        output_vector=output or NEAR_GT,
        ground_truth_vector=gt or GT,
        user_position_vector=user or USER,
        factual_grounding_score=grounding,
    )


def make_detector(**kwargs) -> DriftDetector:
    defaults = dict(
        session_id="test-session",
        vector_drift_threshold=0.85,
        breach_window_turns=3,
        affirmation_ratio_threshold=0.80,
        minimum_turns=5,
        reality_testing_enabled=False,
    )
    defaults.update(kwargs)
    return DriftDetector(**defaults)


# ---------------------------------------------------------------------------
# Cosine similarity
# ---------------------------------------------------------------------------

class TestCosine:
    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert _cosine(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        assert _cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_zero_vector(self):
        assert _cosine([0.0, 0.0], [1.0, 0.0]) == 0.0

    def test_dimension_mismatch_raises(self):
        with pytest.raises(ValueError):
            _cosine([1.0, 0.0], [1.0, 0.0, 0.0])


# ---------------------------------------------------------------------------
# TurnRecord auto-fields
# ---------------------------------------------------------------------------

class TestTurnRecord:
    def test_affirmation_true_when_closer_to_user(self):
        r = TurnRecord(
            turn_index=0,
            output_vector=_norm([0.1, 0.9, 0.0, 0.0]),  # near USER
            ground_truth_vector=GT,
            user_position_vector=USER,
        )
        assert r.affirmation is True

    def test_affirmation_false_when_closer_to_gt(self):
        r = TurnRecord(
            turn_index=0,
            output_vector=_norm([0.9, 0.1, 0.0, 0.0]),  # near GT
            ground_truth_vector=GT,
            user_position_vector=USER,
        )
        assert r.affirmation is False

    def test_cosine_similarity_computed(self):
        r = record(0, output=GT)
        assert r.cosine_similarity == pytest.approx(1.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Vector drift
# ---------------------------------------------------------------------------

class TestVectorDrift:
    def test_no_event_below_window_size(self):
        det = make_detector(breach_window_turns=3)
        # Only 2 below-threshold turns — should not fire
        det.record_turn(record(0, output=FAR_GT))
        events = det.record_turn(record(1, output=FAR_GT))
        assert not events
        assert not det.circuit_open

    def test_fires_after_breach_window(self):
        det = make_detector(breach_window_turns=3)
        det.record_turn(record(0, output=FAR_GT))
        det.record_turn(record(1, output=FAR_GT))
        events = det.record_turn(record(2, output=FAR_GT))
        assert len(events) == 1
        assert events[0].signal == DriftSignal.VECTOR_DRIFT_BREACH
        assert det.circuit_open

    def test_no_fire_when_above_threshold(self):
        det = make_detector(breach_window_turns=3)
        for i in range(5):
            events = det.record_turn(record(i, output=NEAR_GT))
            assert not events
        assert not det.circuit_open

    def test_reset_by_single_above_threshold_turn(self):
        """One good turn in the middle breaks the consecutive window."""
        det = make_detector(breach_window_turns=3)
        det.record_turn(record(0, output=FAR_GT))
        det.record_turn(record(1, output=NEAR_GT))  # resets window
        det.record_turn(record(2, output=FAR_GT))
        events = det.record_turn(record(3, output=FAR_GT))
        # Only 2 consecutive far turns after the reset — should not fire
        assert not events

    def test_event_detail_contains_window_info(self):
        det = make_detector(breach_window_turns=3)
        for i in range(3):
            events = det.record_turn(record(i, output=FAR_GT))
        assert det.events[0].detail["window_size"] == 3
        assert len(det.events[0].detail["per_turn_similarities"]) == 3


# ---------------------------------------------------------------------------
# Sycophancy loop
# ---------------------------------------------------------------------------

class TestSycophancy:
    def _sycophantic_record(self, i: int) -> TurnRecord:
        """Always affirms the user (output near USER, far from GT)."""
        return record(i, output=_norm([0.1, 0.9, 0.0, 0.0]))

    def _factual_record(self, i: int) -> TurnRecord:
        """Always stays near GT."""
        return record(i, output=NEAR_GT)

    def test_does_not_fire_before_minimum_turns(self):
        det = make_detector(minimum_turns=5, affirmation_ratio_threshold=0.80)
        for i in range(4):
            events = det.record_turn(self._sycophantic_record(i))
            assert not any(e.signal == DriftSignal.SYCOPHANCY_LOOP_DETECTED for e in events)

    def test_fires_after_minimum_turns_high_ratio(self):
        det = make_detector(
            minimum_turns=5,
            affirmation_ratio_threshold=0.80,
            breach_window_turns=99,  # disable vector drift
            vector_drift_threshold=0.0,  # nothing can breach this
        )
        events_all = []
        for i in range(6):
            events_all.extend(det.record_turn(self._sycophantic_record(i)))
        syco_events = [e for e in events_all if e.signal == DriftSignal.SYCOPHANCY_LOOP_DETECTED]
        assert len(syco_events) >= 1

    def test_does_not_fire_low_ratio(self):
        det = make_detector(
            minimum_turns=5,
            affirmation_ratio_threshold=0.80,
            breach_window_turns=99,
            vector_drift_threshold=0.0,
        )
        # Alternate: 50% affirmation ratio — well below 0.80
        for i in range(10):
            r = self._sycophantic_record(i) if i % 2 == 0 else self._factual_record(i)
            det.record_turn(r)
        syco = [e for e in det.events if e.signal == DriftSignal.SYCOPHANCY_LOOP_DETECTED]
        assert not syco

    def test_event_value_is_affirmation_ratio(self):
        det = make_detector(
            minimum_turns=5,
            affirmation_ratio_threshold=0.80,
            breach_window_turns=99,
            vector_drift_threshold=0.0,
        )
        for i in range(10):
            det.record_turn(self._sycophantic_record(i))
        syco = [e for e in det.events if e.signal == DriftSignal.SYCOPHANCY_LOOP_DETECTED]
        assert syco[0].value == pytest.approx(1.0, abs=0.01)


# ---------------------------------------------------------------------------
# Reality testing erosion
# ---------------------------------------------------------------------------

class TestRealityTesting:
    def test_fires_after_breach_window(self):
        det = make_detector(
            reality_testing_enabled=True,
            factual_grounding_threshold=0.70,
            breach_window_turns=3,
            vector_drift_threshold=0.0,  # disable vector drift
        )
        for i in range(3):
            events = det.record_turn(record(i, output=NEAR_GT, grounding=0.50))
        rt_events = [e for e in det.events if e.signal == DriftSignal.REALITY_TESTING_EROSION]
        assert len(rt_events) >= 1

    def test_does_not_fire_when_disabled(self):
        det = make_detector(
            reality_testing_enabled=False,
            factual_grounding_threshold=0.70,
            breach_window_turns=3,
            vector_drift_threshold=0.0,
        )
        for i in range(5):
            det.record_turn(record(i, output=NEAR_GT, grounding=0.10))
        rt_events = [e for e in det.events if e.signal == DriftSignal.REALITY_TESTING_EROSION]
        assert not rt_events


# ---------------------------------------------------------------------------
# Circuit open behaviour
# ---------------------------------------------------------------------------

class TestCircuitOpen:
    def test_no_events_after_circuit_open(self):
        det = make_detector(breach_window_turns=3)
        for i in range(3):
            det.record_turn(record(i, output=FAR_GT))
        assert det.circuit_open
        # Attempt more turns — should be no-ops
        result = det.record_turn(record(3, output=FAR_GT))
        assert result == []
        assert len(det.events) == 1  # still only one event

    def test_on_event_callback_called(self):
        fired: list[DriftEvent] = []
        det = make_detector(breach_window_turns=3, on_event=fired.append)
        for i in range(3):
            det.record_turn(record(i, output=FAR_GT))
        assert len(fired) == 1
        assert fired[0].signal == DriftSignal.VECTOR_DRIFT_BREACH


# ---------------------------------------------------------------------------
# Diagnostic helpers
# ---------------------------------------------------------------------------

class TestDiagnostics:
    def test_affirmation_ratio_empty(self):
        det = make_detector()
        assert det.affirmation_ratio() == 0.0

    def test_affirmation_ratio_all_affirming(self):
        det = make_detector(breach_window_turns=99, vector_drift_threshold=0.0)
        for i in range(6):
            det.record_turn(record(i, output=_norm([0.1, 0.9, 0.0, 0.0])))
        assert det.affirmation_ratio() == pytest.approx(1.0)

    def test_average_similarity_empty(self):
        det = make_detector()
        assert det.average_similarity() == pytest.approx(1.0)

    def test_average_similarity_near_one_for_gt_outputs(self):
        det = make_detector(breach_window_turns=99, vector_drift_threshold=0.0)
        for i in range(5):
            det.record_turn(record(i, output=GT))
        assert det.average_similarity() == pytest.approx(1.0, abs=1e-6)

    def test_turns_returns_copy(self):
        det = make_detector()
        det.record_turn(record(0))
        turns1 = det.turns
        turns2 = det.turns
        assert turns1 is not turns2

    def test_events_returns_copy(self):
        det = make_detector(breach_window_turns=3)
        for i in range(3):
            det.record_turn(record(i, output=FAR_GT))
        ev1 = det.events
        ev2 = det.events
        assert ev1 is not ev2
