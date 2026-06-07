# ARG KMS Signing

Production-grade cryptographic signing for the ARG telemetry audit ledger.

The `crypto-signature.json` schema requires an asymmetric signature over each
telemetry payload — not just a hash.  This package provides:

- **`KMSSigner` protocol** — the interface every signing backend implements
- **`LocalECSigner`** — ephemeral EC P-256 key for development and testing
- **`AzureKeyVaultSigner`** — Azure Key Vault (ES256 / RS256 / PS256)
- **`AWSKMSSigner`** — AWS KMS (ES256 / RS256 / PS256)
- **`build_signature_block()`** — constructs the canonical payload and returns a
  schema-compliant signature block

---

## Why Asymmetric Signing

The `payload_hash` field (SHA-256) detects accidental corruption.  The `signature`
field (ECDSA/RSA) detects intentional tampering — it proves that the enforcement gate
held the private key at the time of signing, and that no field in the three content
blocks (context, lineage, evaluation) was modified after signing.

A hash with no signature is not tamper-proof.  It is tamper-evident only if the hash
is stored separately from the payload — which defeats the purpose of an inline audit
record.  See `community/key-management.md` for the full key governance requirements.

---

## Quick Start

```bash
cd ops/kms-signing
pip install -r requirements.txt
python -m pytest tests/ -v
```

### Inject into TelemetryEmitter

```python
from ops.kms_signing.src.signer import LocalECSigner
from examples.sycophancy_loop_detection.src.telemetry_emitter import TelemetryEmitter

# Development: ephemeral key, real signatures
emitter = TelemetryEmitter(
    session_id=session_id,
    user_id=user_id,
    model_deployment_id="claude-sonnet-4-6",
    signer=LocalECSigner(key_id="dev-key"),
)

# Production (Azure): key never leaves Key Vault HSM
from ops.kms_signing.src.azure_signer import AzureKeyVaultSigner
emitter = TelemetryEmitter(
    ...,
    signer=AzureKeyVaultSigner(
        vault_url="https://arg-keyvault.vault.azure.net/",
        key_name="arg-signing-key",
        algorithm="ES256",
    ),
)

# Production (AWS): key never leaves KMS HSM
from ops.kms_signing.src.aws_signer import AWSKMSSigner
emitter = TelemetryEmitter(
    ...,
    signer=AWSKMSSigner(
        key_id="arn:aws:kms:us-east-1:123456789012:key/mrk-xxx",
        algorithm="ES256",
    ),
)
```

---

## Signature Block Output

```json
{
  "key_id":       "arg-signing-key/abc123def456",
  "algorithm":    "ES256",
  "payload_hash": "e827178f402676988f41...",
  "signature":    "MEUCIQCJxxx...base64...",
  "signed_at":    "2026-06-06T12:34:56.789Z"
}
```

The `payload_hash` is `SHA-256(canonical_json(context, lineage, evaluation))`.
The `signature` is the ECDSA-P256 signature over the same canonical bytes.
Both are computed from the identical input — the signature is NOT over the hash.

---

## Algorithm Selection

| Algorithm | Key type | Recommended for |
|---|---|---|
| ES256 | EC P-256 | Default. Smaller keys, faster signing, modern standard. |
| PS256 | RSA-2048+ | FIPS 140-2 environments requiring RSA. |
| RS256 | RSA-2048+ | Legacy PKCS#1 RSA. Use PS256 in preference. |

---

## Key Rotation

The `key_id` field in every signed payload records which key version was used.
Historical records signed with rotated keys remain valid and verifiable — you keep
the old public key and verify against the `key_id`.

**Azure Key Vault rotation:**

```bash
# Create a new version of the key (old version remains available for verification)
az keyvault key create \
  --vault-name arg-keyvault \
  --name arg-signing-key \
  --kty EC --curve P-256 --ops sign verify

# AzureKeyVaultSigner without key_version= always uses the latest version
# Records signed with the prior version still verify via:
#   az keyvault key get-key-rotation-policy --vault-name ... --name ...
```

**AWS KMS rotation:**

```bash
# Enable automatic annual rotation
aws kms enable-key-rotation --key-id <key-id>

# On rotation, the ARN stays the same — AWSKMSSigner.key_id returns the ARN,
# so all records signed pre/post rotation are traceable to the same ARN.
```

---

## Offline Verification

Given a payload and its signature block, any party with the public key can verify:

```python
import base64, hashlib, json
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes, serialization

# Load the public key (download from Key Vault / KMS, or LocalECSigner.public_key_pem())
pub_key = serialization.load_pem_public_key(pem_bytes)

# Recompute canonical payload
canonical = json.dumps(
    {"context": payload["context"],
     "lineage": payload["lineage"],
     "evaluation": payload["evaluation"]},
    sort_keys=True, separators=(",", ":"),
).encode()

# Verify
sig_bytes = base64.b64decode(payload["signature"]["signature"])
pub_key.verify(sig_bytes, canonical, ec.ECDSA(hashes.SHA256()))
# Raises InvalidSignature if tampered; passes silently if valid
```

---

## Files

| File | Purpose |
|---|---|
| `src/signer.py` | `KMSSigner` protocol, `LocalECSigner`, `build_signature_block`, `make_signer` |
| `src/azure_signer.py` | Azure Key Vault implementation (requires `azure-keyvault-keys`) |
| `src/aws_signer.py` | AWS KMS implementation (requires `boto3`) |
| `tests/test_signer.py` | 22 unit tests — no cloud credentials required |
