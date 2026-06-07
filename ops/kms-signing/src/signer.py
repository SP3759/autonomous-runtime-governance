"""
ARG KMS Signer — Protocol and local implementation.

Defines the KMSSigner protocol that every signer in this package implements,
and provides LocalECSigner: a dev/test signer that generates an ephemeral
EC P-256 key pair in memory and produces real ES256 signatures.

LocalECSigner is NOT for production use:
  - The private key exists only in process memory.
  - There is no key rotation, audit trail, or custody policy.
  - Use AzureKeyVaultSigner or AWSKMSSigner for production deployments.

For production requirements see community/key-management.md.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class KMSSigner(Protocol):
    """
    Minimal interface for all ARG signing backends.

    Implementations:
      LocalECSigner       — ephemeral EC P-256 key (dev/test only)
      AzureKeyVaultSigner — azure-keyvault-keys (see azure_signer.py)
      AWSKMSSigner        — boto3 (see aws_signer.py)
    """

    def sign(self, payload_bytes: bytes) -> str:
        """
        Sign `payload_bytes` with the active private key.
        Returns a base64-encoded signature string (standard, not URL-safe).
        """
        ...

    @property
    def key_id(self) -> str:
        """Stable identifier for the current signing key (e.g. key name + version)."""
        ...

    @property
    def algorithm(self) -> str:
        """
        Signing algorithm as used in the crypto-signature schema enum.
        One of: 'ES256', 'RS256', 'PS256'.
        """
        ...

    def public_key_pem(self) -> str:
        """PEM-encoded public key for offline verification."""
        ...


# ---------------------------------------------------------------------------
# LocalECSigner — ephemeral EC P-256, ES256
# ---------------------------------------------------------------------------

class LocalECSigner:
    """
    Generates an ephemeral EC P-256 key pair using the `cryptography` library.
    Produces real ECDSA-P256 / SHA-256 (ES256) signatures compatible with the
    crypto-signature.json schema.

    Intended for:
      - Local development and testing
      - CI environments with no KMS access

    NOT intended for:
      - Production deployments (key is not persisted or rotated)
      - Multi-process environments (each instance has a different key)
    """

    def __init__(self, key_id: str = "local-ec-dev") -> None:
        try:
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.backends import default_backend
        except ImportError as e:
            raise ImportError(
                "LocalECSigner requires the 'cryptography' package. "
                "Install with: pip install cryptography"
            ) from e

        self._ec = ec
        self._backend = default_backend()
        self._key_id = key_id
        self._private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())

    def sign(self, payload_bytes: bytes) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        signature_der = self._private_key.sign(payload_bytes, ec.ECDSA(hashes.SHA256()))
        return base64.b64encode(signature_der).decode()

    @property
    def key_id(self) -> str:
        return self._key_id

    @property
    def algorithm(self) -> str:
        return "ES256"

    def public_key_pem(self) -> str:
        from cryptography.hazmat.primitives import serialization
        return self._private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()

    def verify(self, payload_bytes: bytes, signature_b64: str) -> bool:
        """Verify a previously produced signature. Used in tests."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.exceptions import InvalidSignature
        try:
            sig_bytes = base64.b64decode(signature_b64)
            self._private_key.public_key().verify(
                sig_bytes, payload_bytes, ec.ECDSA(hashes.SHA256())
            )
            return True
        except InvalidSignature:
            return False


# ---------------------------------------------------------------------------
# Signature block builder (shared by all emitters)
# ---------------------------------------------------------------------------

def build_signature_block(
    context: dict,
    lineage: dict,
    evaluation: dict,
    signer: KMSSigner,
) -> dict:
    """
    Build a schema-compliant crypto-signature block.

    Canonical form: JSON-serialised {context, lineage, evaluation} with
    sorted keys and no whitespace — matches the contract in telemetry-payload.json.

    Returns a dict matching schemas/telemetry/crypto-signature.json.
    """
    import json

    canonical = json.dumps(
        {"context": context, "lineage": lineage, "evaluation": evaluation},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    payload_hash = hashlib.sha256(canonical).hexdigest()
    signature    = signer.sign(canonical)

    return {
        "key_id":       signer.key_id,
        "algorithm":    signer.algorithm,
        "payload_hash": payload_hash,
        "signature":    signature,
        "signed_at":    datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_signer(backend: str = "local", **kwargs) -> KMSSigner:
    """
    Factory for obtaining a KMSSigner by backend name.

    backend="local"   → LocalECSigner (no cloud credentials required)
    backend="azure"   → AzureKeyVaultSigner (requires azure-keyvault-keys)
    backend="aws"     → AWSKMSSigner (requires boto3)

    Additional kwargs are forwarded to the chosen backend's constructor.
    """
    if backend == "local":
        return LocalECSigner(**kwargs)
    if backend == "azure":
        from .azure_signer import AzureKeyVaultSigner
        return AzureKeyVaultSigner(**kwargs)
    if backend == "aws":
        from .aws_signer import AWSKMSSigner
        return AWSKMSSigner(**kwargs)
    raise ValueError(f"Unknown signing backend: '{backend}'. Choose: local, azure, aws")
