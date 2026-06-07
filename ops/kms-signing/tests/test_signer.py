"""
Unit tests for ARG KMS Signer.

All tests use LocalECSigner — no cloud credentials required.

Covers:
  - LocalECSigner produces a non-empty base64 signature
  - Signature verifies against the same payload
  - Different payloads produce different signatures
  - key_id is the string passed at construction
  - algorithm is "ES256"
  - public_key_pem returns a PEM string
  - build_signature_block returns all schema-required fields
  - build_signature_block payload_hash is correct SHA-256 hex
  - build_signature_block signature verifies
  - Two calls with different payloads produce different hashes and signatures
  - make_signer("local") returns a LocalECSigner
  - make_signer("unknown") raises ValueError
  - KMSSigner Protocol: LocalECSigner satisfies it (runtime_checkable)
"""

import base64
import hashlib
import json
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from signer import KMSSigner, LocalECSigner, build_signature_block, make_signer


# ---------------------------------------------------------------------------
# LocalECSigner
# ---------------------------------------------------------------------------

class TestLocalECSigner:
    def test_sign_returns_nonempty_base64(self):
        s = LocalECSigner()
        sig = s.sign(b"hello world")
        assert sig
        # Should be valid base64
        decoded = base64.b64decode(sig)
        assert len(decoded) > 0

    def test_signature_verifies(self):
        s = LocalECSigner()
        payload = b"canonical telemetry payload"
        sig = s.sign(payload)
        assert s.verify(payload, sig)

    def test_wrong_payload_fails_verification(self):
        s = LocalECSigner()
        sig = s.sign(b"correct payload")
        assert not s.verify(b"tampered payload", sig)

    def test_different_payloads_produce_different_signatures(self):
        s = LocalECSigner()
        sig1 = s.sign(b"payload one")
        sig2 = s.sign(b"payload two")
        assert sig1 != sig2

    def test_key_id_default(self):
        s = LocalECSigner()
        assert s.key_id == "local-ec-dev"

    def test_key_id_custom(self):
        s = LocalECSigner(key_id="test-key-42")
        assert s.key_id == "test-key-42"

    def test_algorithm_is_es256(self):
        assert LocalECSigner().algorithm == "ES256"

    def test_public_key_pem(self):
        pem = LocalECSigner().public_key_pem()
        assert pem.startswith("-----BEGIN PUBLIC KEY-----")
        assert "-----END PUBLIC KEY-----" in pem

    def test_two_instances_have_different_keys(self):
        """Each LocalECSigner generates a fresh key — they cannot verify each other's sigs."""
        s1, s2 = LocalECSigner(), LocalECSigner()
        sig = s1.sign(b"data")
        assert not s2.verify(b"data", sig)


# ---------------------------------------------------------------------------
# build_signature_block
# ---------------------------------------------------------------------------

CONTEXT    = {"session_id": "abc", "turn_index": 0}
LINEAGE    = {"model_id": "claude-sonnet-4-6", "temperature": 0.7}
EVALUATION = {"cosine_similarity": 0.95, "policy_decision": "CONTINUE"}


class TestBuildSignatureBlock:
    def _block(self, signer=None):
        return build_signature_block(CONTEXT, LINEAGE, EVALUATION, signer or LocalECSigner())

    def test_required_fields_present(self):
        block = self._block()
        for field in ("key_id", "algorithm", "payload_hash", "signature", "signed_at"):
            assert field in block, f"Missing field: {field}"

    def test_payload_hash_is_sha256_hex(self):
        block = self._block()
        assert len(block["payload_hash"]) == 64
        assert all(c in "0123456789abcdef" for c in block["payload_hash"])

    def test_payload_hash_matches_canonical_json(self):
        canonical = json.dumps(
            {"context": CONTEXT, "lineage": LINEAGE, "evaluation": EVALUATION},
            sort_keys=True, separators=(",", ":"),
        ).encode()
        expected = hashlib.sha256(canonical).hexdigest()
        block = self._block()
        assert block["payload_hash"] == expected

    def test_signature_is_nonempty_base64(self):
        block = self._block()
        decoded = base64.b64decode(block["signature"])
        assert len(decoded) > 0

    def test_signature_verifies(self):
        signer = LocalECSigner()
        block  = build_signature_block(CONTEXT, LINEAGE, EVALUATION, signer)
        # Re-derive the signed bytes
        canonical = json.dumps(
            {"context": CONTEXT, "lineage": LINEAGE, "evaluation": EVALUATION},
            sort_keys=True, separators=(",", ":"),
        ).encode()
        assert signer.verify(canonical, block["signature"])

    def test_algorithm_matches_signer(self):
        signer = LocalECSigner()
        block  = build_signature_block(CONTEXT, LINEAGE, EVALUATION, signer)
        assert block["algorithm"] == signer.algorithm

    def test_key_id_matches_signer(self):
        signer = LocalECSigner(key_id="my-key")
        block  = build_signature_block(CONTEXT, LINEAGE, EVALUATION, signer)
        assert block["key_id"] == "my-key"

    def test_different_evaluations_produce_different_hashes(self):
        s = LocalECSigner()
        eval1 = {"policy_decision": "CONTINUE"}
        eval2 = {"policy_decision": "TERMINATE"}
        b1 = build_signature_block(CONTEXT, LINEAGE, eval1, s)
        b2 = build_signature_block(CONTEXT, LINEAGE, eval2, s)
        assert b1["payload_hash"] != b2["payload_hash"]
        assert b1["signature"]    != b2["signature"]

    def test_signed_at_is_iso_timestamp(self):
        from datetime import datetime
        block = self._block()
        dt = datetime.fromisoformat(block["signed_at"])
        assert dt.tzinfo is not None  # must be timezone-aware


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class TestFactory:
    def test_local_backend_returns_local_ec_signer(self):
        s = make_signer("local")
        assert isinstance(s, LocalECSigner)

    def test_local_backend_with_key_id(self):
        s = make_signer("local", key_id="custom-id")
        assert s.key_id == "custom-id"

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown signing backend"):
            make_signer("gcp")


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestProtocol:
    def test_local_ec_signer_satisfies_protocol(self):
        assert isinstance(LocalECSigner(), KMSSigner)
