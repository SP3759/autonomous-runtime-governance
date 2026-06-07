"""
Schema validation tests.

Verifies that:
  1. All JSON schemas in schemas/ parse as valid JSON
  2. breaker-event.json and spend-result.json match the actual Python objects
     emitted by circuit_breaker.py and wallet.py
  3. telemetry-payload.json required fields are present
  4. The TriggerSignal enum values in circuit_breaker.py are a subset of the
     trigger_signal enum in breaker-event.json (schema stays in sync with code)
"""

import json
import pathlib
import sys
import os
import uuid
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from wallet import (
    AllocatedWallet,
    AuthorizerType,
    OperationType,
    OperationWallet,
    TaskCategory,
    SpendResult,
    WalletStatus,
)
from circuit_breaker import (
    BreakerEvent,
    BreakerState,
    FinancialCircuitBreaker,
    TriggerSignal,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = pathlib.Path(__file__).parents[3]
SCHEMAS_DIR = REPO_ROOT / "schemas"

TELEMETRY_SCHEMAS = list((SCHEMAS_DIR / "telemetry").glob("*.json"))
WALLET_SCHEMAS = list((SCHEMAS_DIR / "wallet").glob("*.json"))
ALL_SCHEMAS = TELEMETRY_SCHEMAS + WALLET_SCHEMAS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GRAPH = {
    "allowed_endpoints": [{"url_pattern": "https://api.example.com/*"}],
    "blocked_endpoints": [],
}
GRAPH_HASH = AllocatedWallet.compute_dependency_graph_hash(GRAPH)


def make_wallet(**kwargs) -> AllocatedWallet:
    defaults = dict(
        execution_tree_id=str(uuid.uuid4()),
        token_limit=10_000,
        cost_limit_usd=1.00,
        ttl_seconds=300,
        dependency_graph_hash=GRAPH_HASH,
        appropriation_id="test",
        intent_declaration="test",
        task_category=TaskCategory.RESEARCH,
        authorizer_id="test-user",
        authorizer_type=AuthorizerType.HUMAN,
    )
    defaults.update(kwargs)
    return AllocatedWallet.issue(**defaults)


# ---------------------------------------------------------------------------
# 1. All schemas parse as valid JSON
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("schema_path", ALL_SCHEMAS, ids=lambda p: p.name)
def test_schema_is_valid_json(schema_path):
    data = json.loads(schema_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert "$schema" in data
    assert "title" in data


# ---------------------------------------------------------------------------
# 2. All schemas have a description
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("schema_path", ALL_SCHEMAS, ids=lambda p: p.name)
def test_schema_has_description(schema_path):
    data = json.loads(schema_path.read_text(encoding="utf-8"))
    assert "description" in data, f"{schema_path.name} is missing a top-level description"


# ---------------------------------------------------------------------------
# 3. breaker-event.json required fields match BreakerEvent dataclass
# ---------------------------------------------------------------------------

class TestBreakerEventSchema:
    def _load(self):
        return json.loads((SCHEMAS_DIR / "wallet" / "breaker-event.json").read_text())

    def test_required_fields_present(self):
        schema = self._load()
        required = set(schema["required"])
        expected = {
            "event_id", "wallet_id", "execution_tree_id", "trigger_signal",
            "previous_state", "new_state", "occurred_at", "call_count_in_window",
            "calls_per_minute_rate", "unique_endpoints_in_window",
            "repeated_endpoint_pattern", "mission_utilization_percent",
        }
        assert required == expected

    def test_trigger_signal_enum_matches_code(self):
        schema = self._load()
        schema_signals = set(
            schema["properties"]["trigger_signal"]["enum"]
        )
        code_signals = {signal.value for signal in TriggerSignal}
        missing_from_schema = code_signals - schema_signals
        assert not missing_from_schema, (
            f"TriggerSignal values in code not in schema: {missing_from_schema}\n"
            "Update schemas/wallet/breaker-event.json trigger_signal enum."
        )

    def test_state_enum_matches_code(self):
        schema = self._load()
        schema_states = set(schema["properties"]["previous_state"]["enum"])
        code_states = {state.value for state in BreakerState}
        assert schema_states == code_states

    def test_real_breaker_event_matches_schema_shape(self):
        """Construct a real BreakerEvent and verify its fields against schema required fields."""
        schema = self._load()
        required_fields = set(schema["required"])

        event = BreakerEvent(
            event_id=str(uuid.uuid4()),
            wallet_id=str(uuid.uuid4()),
            execution_tree_id=str(uuid.uuid4()),
            trigger_signal=TriggerSignal.CALL_RATE_EXCEEDED,
            previous_state=BreakerState.CLOSED,
            new_state=BreakerState.OPEN,
            occurred_at=datetime.now(timezone.utc),
            call_count_in_window=15,
            calls_per_minute_rate=120.0,
            unique_endpoints_in_window=1,
            repeated_endpoint_pattern=True,
            mission_utilization_percent=42.5,
        )
        event_dict = {
            "event_id": event.event_id,
            "wallet_id": event.wallet_id,
            "execution_tree_id": event.execution_tree_id,
            "trigger_signal": event.trigger_signal.value,
            "previous_state": event.previous_state.value,
            "new_state": event.new_state.value,
            "occurred_at": event.occurred_at.isoformat(),
            "call_count_in_window": event.call_count_in_window,
            "calls_per_minute_rate": event.calls_per_minute_rate,
            "unique_endpoints_in_window": event.unique_endpoints_in_window,
            "repeated_endpoint_pattern": event.repeated_endpoint_pattern,
            "mission_utilization_percent": event.mission_utilization_percent,
        }
        missing = required_fields - set(event_dict.keys())
        assert not missing, f"BreakerEvent is missing required schema fields: {missing}"


# ---------------------------------------------------------------------------
# 4. spend-result.json required fields match SpendResult Pydantic model
# ---------------------------------------------------------------------------

class TestSpendResultSchema:
    def _load(self):
        return json.loads((SCHEMAS_DIR / "wallet" / "spend-result.json").read_text())

    def test_required_fields_present(self):
        schema = self._load()
        assert set(schema["required"]) == {"approved", "wallet_id"}

    def test_denial_reason_enum_covers_known_codes(self):
        schema = self._load()
        enum_values = set(schema["properties"]["denial_reason"]["enum"])
        known_codes = {
            "WALLET_REVOKED",
            "WALLET_EXHAUSTED",
            "WALLET_EXPIRED",
            "BURST_RESERVE_PENDING_APPROVAL",
            "MISSION_BUDGET_EXHAUSTED",
            "MISSION_BUDGET_EXHAUSTED_BURST_RESERVE_AVAILABLE",
            "CIRCUIT_BREAKER_OPEN",
            None,
        }
        missing = known_codes - enum_values
        assert not missing, f"denial_reason enum missing codes: {missing}"

    def test_approved_result_shape(self):
        """A real approved SpendResult has the fields the schema documents."""
        schema = self._load()
        wallet = make_wallet()
        result = wallet.request_spend(tokens=100, cost_usd=0.01)
        assert result.approved
        result_dict = result.model_dump()
        # All schema properties should be representable in the result
        for field in schema["properties"]:
            assert field in result_dict, f"SpendResult missing field: {field}"

    def test_denied_result_has_denial_reason(self):
        wallet = make_wallet(token_limit=50)
        result = wallet.request_spend(tokens=100, cost_usd=0.01)
        assert not result.approved
        assert result.denial_reason is not None
        assert result.tokens_approved is None

    def test_operation_type_enum_matches_code(self):
        schema = self._load()
        schema_types = set(
            v for v in schema["properties"]["operation_type"]["enum"] if v is not None
        )
        code_types = {op.value for op in OperationType}
        missing = code_types - schema_types
        assert not missing, (
            f"OperationType values in code not in spend-result.json: {missing}"
        )


# ---------------------------------------------------------------------------
# 5. telemetry-payload.json structure
# ---------------------------------------------------------------------------

class TestTelemetryPayloadSchema:
    def _load(self):
        return json.loads((SCHEMAS_DIR / "telemetry" / "telemetry-payload.json").read_text())

    def test_required_blocks_present(self):
        schema = self._load()
        required = set(schema["required"])
        assert {"payload_id", "schema_version", "context", "lineage", "evaluation", "signature"} <= required

    def test_block_refs_resolve_to_existing_files(self):
        """Each $ref in the envelope points to a real file in schemas/telemetry/."""
        schema = self._load()
        telemetry_dir = SCHEMAS_DIR / "telemetry"
        for block in ("context", "lineage", "evaluation", "signature"):
            prop = schema["properties"].get(block, {})
            ref = prop.get("$ref")
            assert ref is not None, f"Block '{block}' missing $ref"
            target = telemetry_dir / ref
            assert target.exists(), f"$ref '{ref}' in block '{block}' points to non-existent file"

    def test_schema_version_enum_contains_current(self):
        schema = self._load()
        assert "1.0.0" in schema["properties"]["schema_version"]["enum"]

    def test_additional_properties_disallowed(self):
        schema = self._load()
        assert schema.get("additionalProperties") is False


# ---------------------------------------------------------------------------
# 6. Cross-schema consistency: trigger_signal values consistent across schemas
# ---------------------------------------------------------------------------

class TestCrossSchemaConsistency:
    def test_trigger_signal_consistent_between_breaker_event_and_financial_event(self):
        breaker_schema = json.loads(
            (SCHEMAS_DIR / "wallet" / "breaker-event.json").read_text()
        )
        financial_schema = json.loads(
            (SCHEMAS_DIR / "wallet" / "financial-event.json").read_text()
        )

        breaker_signals = set(
            breaker_schema["properties"]["trigger_signal"]["enum"]
        )
        financial_signals = set(
            financial_schema["properties"]["circuit_breaker_detail"]
            ["properties"]["trigger_signal"]["enum"]
        )

        # Every signal in breaker-event must be representable in financial-event
        missing = breaker_signals - financial_signals
        assert not missing, (
            f"Signals in breaker-event.json not in financial-event.json circuit_breaker_detail: {missing}\n"
            "Update schemas/wallet/financial-event.json circuit_breaker_detail.trigger_signal enum."
        )

    def test_wallet_status_consistent_across_schemas(self):
        allocated = json.loads(
            (SCHEMAS_DIR / "wallet" / "allocated-wallet.json").read_text()
        )
        status_values = set(allocated["properties"]["status"]["enum"])
        code_statuses = {s.value for s in WalletStatus}
        assert status_values == code_statuses, (
            f"WalletStatus mismatch.\n"
            f"  Schema: {status_values}\n"
            f"  Code:   {code_statuses}"
        )
