"""
ARG Telemetry Emitter for Sycophancy Loop Detection

Builds schema-compliant telemetry payloads (schemas/telemetry/telemetry-payload.json)
for each inference turn in a sycophancy loop detection session.

Each payload contains the four canonical blocks:
  context    — session identifiers, cohort, privacy jurisdiction
  lineage    — model config at inference time (prompt hash, model_id, temperature)
  evaluation — drift scores and policy decision
  signature  — asymmetric signature over (context + lineage + evaluation)

The signature block conforms to schemas/telemetry/crypto-signature.json:
  - payload_hash: SHA-256 hex of canonical JSON
  - signature:    base64-encoded ECDSA/RSA signature from the KMS signer
  - key_id:       stable key identifier (e.g. key name + version)
  - algorithm:    ES256 / RS256 / PS256
  - signed_at:    ISO 8601 UTC timestamp

By default, TelemetryEmitter uses LocalECSigner (an ephemeral EC P-256 key,
suitable for development and testing).  For production, inject an
AzureKeyVaultSigner or AWSKMSSigner from ops/kms-signing/.
"""

from __future__ import annotations

import hashlib
import sys
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from drift_detector import DriftEvent, DriftSignal, TurnRecord

# Resolve ops/kms-signing/src from the sycophancy example's src directory
sys.path.insert(
    0,
    os.path.normpath(
        os.path.join(os.path.dirname(__file__), '..', '..', '..', 'ops', 'kms-signing', 'src')
    )
)
from signer import KMSSigner, LocalECSigner, build_signature_block


# ---------------------------------------------------------------------------
# Block builders
# ---------------------------------------------------------------------------

def _context_block(
    session_id: str,
    turn_index: int,
    user_id: str,
    model_deployment_id: str,
    privacy_jurisdiction: str = "US",
) -> dict[str, Any]:
    return {
        "session_id":           session_id,
        "turn_index":           turn_index,
        "user_id":              user_id,
        "model_deployment_id":  model_deployment_id,
        "request_timestamp":    datetime.now(timezone.utc).isoformat(),
        "privacy_jurisdiction": privacy_jurisdiction,
    }


def _lineage_block(
    prompt_hash: str,
    model_id: str,
    temperature: float,
    max_tokens: int,
    system_prompt_hash: str | None = None,
) -> dict[str, Any]:
    block: dict[str, Any] = {
        "prompt_hash": prompt_hash,
        "model_id":    model_id,
        "temperature": temperature,
        "max_tokens":  max_tokens,
    }
    if system_prompt_hash:
        block["system_prompt_hash"] = system_prompt_hash
    return block


def _evaluation_block(
    record: TurnRecord,
    drift_events: list[DriftEvent],
    policy_decision: str,
) -> dict[str, Any]:
    return {
        "cosine_similarity":       record.cosine_similarity,
        "affirmation":             record.affirmation,
        "factual_grounding_score": record.factual_grounding_score,
        "drift_signals_fired":     [e.signal.value for e in drift_events],
        "policy_decision":         policy_decision,
        "evaluated_at":            datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Public emitter
# ---------------------------------------------------------------------------

class TelemetryEmitter:
    """
    Stateless builder — no internal buffers.  Call emit_turn() once per model
    turn; it returns the complete payload dict suitable for JSON serialisation
    and Kafka emission.

    Parameters:
      session_id          — shared across all turns in a session
      user_id             — anonymised user identifier
      model_deployment_id — deployment label (e.g. "claude-sonnet-4-6-tutoring")
      model_id            — model identifier sent to lineage block
      temperature         — inference temperature
      max_tokens          — max_tokens setting used at inference time
      wallet_id           — if the session is gated by an AllocatedWallet
      execution_tree_id   — if the session is part of a multi-agent workflow
      signer              — KMSSigner implementation. Defaults to LocalECSigner
                            (ephemeral EC key — use AzureKeyVaultSigner or
                            AWSKMSSigner from ops/kms-signing/ for production)
      privacy_jurisdiction — ISO 3166 country code for the privacy jurisdiction
    """

    def __init__(
        self,
        session_id: str,
        user_id: str,
        model_deployment_id: str,
        model_id: str                 = "claude-sonnet-4-6",
        temperature: float            = 0.7,
        max_tokens: int               = 2048,
        wallet_id: str | None         = None,
        execution_tree_id: str | None = None,
        signer: KMSSigner | None      = None,
        privacy_jurisdiction: str     = "US",
    ) -> None:
        self.session_id           = session_id
        self.user_id              = user_id
        self.model_deployment_id  = model_deployment_id
        self.model_id             = model_id
        self.temperature          = temperature
        self.max_tokens           = max_tokens
        self.wallet_id            = wallet_id
        self.execution_tree_id    = execution_tree_id
        self.signer               = signer or LocalECSigner()
        self.privacy_jurisdiction = privacy_jurisdiction

    def emit_turn(
        self,
        record: TurnRecord,
        drift_events: list[DriftEvent],
        prompt_text: str,
        system_prompt: str | None = None,
    ) -> dict[str, Any]:
        """
        Build and return a complete telemetry-payload dict for one turn.
        The caller is responsible for writing it to Kafka / the audit ledger.
        """
        policy_decision = "TERMINATE" if any(
            e.signal in (
                DriftSignal.VECTOR_DRIFT_BREACH,
                DriftSignal.SYCOPHANCY_LOOP_DETECTED,
                DriftSignal.REALITY_TESTING_EROSION,
            )
            for e in drift_events
        ) else "CONTINUE"

        prompt_hash = hashlib.sha256(prompt_text.encode()).hexdigest()
        system_hash = (
            hashlib.sha256(system_prompt.encode()).hexdigest()
            if system_prompt else None
        )

        context    = _context_block(
            self.session_id, record.turn_index, self.user_id,
            self.model_deployment_id, self.privacy_jurisdiction,
        )
        lineage    = _lineage_block(
            prompt_hash, self.model_id, self.temperature,
            self.max_tokens, system_hash,
        )
        evaluation = _evaluation_block(record, drift_events, policy_decision)
        signature  = build_signature_block(context, lineage, evaluation, self.signer)

        payload: dict[str, Any] = {
            "payload_id":     str(uuid.uuid4()),
            "schema_version": "1.0.0",
            "context":        context,
            "lineage":        lineage,
            "evaluation":     evaluation,
            "signature":      signature,
        }
        if self.wallet_id:
            payload["wallet_id"] = self.wallet_id
        if self.execution_tree_id:
            payload["execution_tree_id"] = self.execution_tree_id

        return payload
