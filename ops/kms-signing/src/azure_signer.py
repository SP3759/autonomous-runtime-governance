"""
ARG KMS Signer — Azure Key Vault implementation.

Uses the azure-keyvault-keys SDK to sign with a key stored in Azure Key Vault.
The private key never leaves the HSM; only the hash is transmitted to the vault.

Supported algorithms: ES256 (recommended), PS256, RS256.

Prerequisites:
  pip install azure-keyvault-keys azure-identity

Authentication:
  In production, use workload identity (DefaultAzureCredential picks it up
  automatically from the pod's managed identity).  In development, log in via
  `az login` or set AZURE_CLIENT_ID / AZURE_CLIENT_SECRET / AZURE_TENANT_ID.

Example:
  from ops.kms_signing.src.azure_signer import AzureKeyVaultSigner

  signer = AzureKeyVaultSigner(
      vault_url="https://arg-keyvault.vault.azure.net/",
      key_name="arg-signing-key",
      algorithm="ES256",
  )
  block = build_signature_block(context, lineage, evaluation, signer)
"""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class AzureKeyVaultSigner:
    """
    Signs telemetry payloads using an EC or RSA key in Azure Key Vault.

    The signing operation calls KeyClient.sign(), which sends only the
    payload hash (32 bytes for SHA-256) to the vault — the canonical payload
    never leaves the local process.

    Parameters:
      vault_url   — e.g. "https://arg-keyvault.vault.azure.net/"
      key_name    — name of the key in the vault
      algorithm   — "ES256" (default), "PS256", or "RS256"
      key_version — pin to a specific version; omit to use the latest
    """

    _ALGORITHM_MAP = {
        "ES256": "ES256",
        "PS256": "PS256",
        "RS256": "RS256",
    }

    def __init__(
        self,
        vault_url: str,
        key_name: str,
        algorithm: str = "ES256",
        key_version: str | None = None,
    ) -> None:
        try:
            from azure.keyvault.keys.crypto import CryptographyClient, SignatureAlgorithm
            from azure.keyvault.keys import KeyClient
            from azure.identity import DefaultAzureCredential
        except ImportError as e:
            raise ImportError(
                "AzureKeyVaultSigner requires: pip install azure-keyvault-keys azure-identity"
            ) from e

        if algorithm not in self._ALGORITHM_MAP:
            raise ValueError(f"algorithm must be one of {list(self._ALGORITHM_MAP)}")

        credential = DefaultAzureCredential()
        key_client = KeyClient(vault_url=vault_url, credential=credential)

        key = key_client.get_key(key_name, version=key_version)
        self._crypto_client = CryptographyClient(key, credential=credential)
        self._algorithm_str = algorithm
        self._sign_algo = SignatureAlgorithm[algorithm]
        self._key_id_str = f"{key_name}/{key.properties.version or 'latest'}"
        self._public_key = key.key

    def sign(self, payload_bytes: bytes) -> str:
        digest = hashlib.sha256(payload_bytes).digest()
        result = self._crypto_client.sign(self._sign_algo, digest)
        return base64.b64encode(result.signature).decode()

    @property
    def key_id(self) -> str:
        return self._key_id_str

    @property
    def algorithm(self) -> str:
        return self._algorithm_str

    def public_key_pem(self) -> str:
        """
        Returns the PEM-encoded public key from Key Vault.
        Requires azure-keyvault-keys >= 4.8.
        """
        try:
            from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
            from azure.keyvault.keys.crypto._key_validity import _get_public_key

            pub = _get_public_key(self._public_key)
            return pub.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
        except Exception:
            return "(public key export unavailable — see Key Vault portal)"
