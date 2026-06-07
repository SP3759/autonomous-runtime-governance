"""
ARG KMS Signer — AWS KMS implementation.

Uses boto3 to sign with an asymmetric key in AWS KMS.  The private key
never leaves the KMS HSM; only the SHA-256 digest is transmitted.

Supported key specs: ECC_NIST_P256 (ES256), RSA_2048 with RSASSA_PKCS1_V1_5_SHA_256
(RS256) or RSASSA_PSS_SHA_256 (PS256).

Prerequisites:
  pip install boto3

Authentication:
  In production, use an IAM role attached to your EC2/ECS/Lambda workload.
  Locally, configure credentials via `aws configure` or environment variables.

Example:
  from ops.kms_signing.src.aws_signer import AWSKMSSigner

  signer = AWSKMSSigner(
      key_id="arn:aws:kms:us-east-1:123456789012:key/mrk-xxxxxxxx",
      algorithm="ES256",
      region_name="us-east-1",
  )
  block = build_signature_block(context, lineage, evaluation, signer)
"""

from __future__ import annotations

import base64
import hashlib


# AWS KMS signing algorithm identifiers mapped from ARG schema enum values
_ALGORITHM_MAP = {
    "ES256": "ECDSA_SHA_256",
    "RS256": "RSASSA_PKCS1_V1_5_SHA_256",
    "PS256": "RSASSA_PSS_SHA_256",
}


class AWSKMSSigner:
    """
    Signs telemetry payloads using an asymmetric key in AWS KMS.

    AWS KMS requires MessageType="DIGEST" + a pre-computed SHA-256 digest
    for asymmetric signing.  Only 32 bytes of the digest are transmitted to KMS;
    the canonical payload remains local.

    Parameters:
      key_id       — KMS key ARN, key ID, or alias (e.g. "alias/arg-signing")
      algorithm    — "ES256" (default), "RS256", or "PS256"
      region_name  — AWS region where the key resides
    """

    def __init__(
        self,
        key_id: str,
        algorithm: str = "ES256",
        region_name: str = "us-east-1",
    ) -> None:
        try:
            import boto3
        except ImportError as e:
            raise ImportError(
                "AWSKMSSigner requires: pip install boto3"
            ) from e

        if algorithm not in _ALGORITHM_MAP:
            raise ValueError(f"algorithm must be one of {list(_ALGORITHM_MAP)}")

        self._client = boto3.client("kms", region_name=region_name)
        self._key_id_str = key_id
        self._algorithm_str = algorithm
        self._kms_algorithm = _ALGORITHM_MAP[algorithm]

        # Resolve alias to key ARN so key_id is stable across rotations
        response = self._client.describe_key(KeyId=key_id)
        self._key_arn = response["KeyMetadata"]["Arn"]

    def sign(self, payload_bytes: bytes) -> str:
        digest = hashlib.sha256(payload_bytes).digest()
        response = self._client.sign(
            KeyId=self._key_arn,
            Message=digest,
            MessageType="DIGEST",
            SigningAlgorithm=self._kms_algorithm,
        )
        return base64.b64encode(response["Signature"]).decode()

    @property
    def key_id(self) -> str:
        return self._key_arn  # ARN is version-stable; alias may point elsewhere after rotation

    @property
    def algorithm(self) -> str:
        return self._algorithm_str

    def public_key_pem(self) -> str:
        """Download the public key from KMS for offline verification."""
        response = self._client.get_public_key(KeyId=self._key_arn)
        raw = response["PublicKey"]  # DER-encoded SubjectPublicKeyInfo
        from cryptography.hazmat.primitives.serialization import (
            load_der_public_key, Encoding, PublicFormat
        )
        pub = load_der_public_key(raw)
        return pub.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
