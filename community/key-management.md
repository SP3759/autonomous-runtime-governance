# Key Management Requirements

The Cryptographic Signature block in the ARG telemetry schema requires a private key held by the enforcement gate. This document defines the minimum key governance requirements before production deployment.

## Why This Matters

A signed audit ledger with no key governance policy is not security. It is a prop. If the signing key is compromised, unrotated, or held without clear custody, the tamper-proof guarantee of the audit ledger collapses entirely.

## Minimum Requirements

### Key Custody
- The enforcement gate private key must be stored in a Hardware Security Module (HSM) or managed KMS (AWS KMS, Azure Key Vault, GCP Cloud KMS)
- The private key must never exist in plaintext on disk or in environment variables
- Key custody must be documented and reviewed annually

### Key Rotation
- Signing keys must be rotated on a defined schedule (recommended: 90 days)
- All audit records must reference the `key_id` used at signing time
- Historical records signed with rotated keys remain valid and verifiable

### Access Control
- Key access must be restricted to the enforcement gate service identity
- All key access events must be logged
- No human operator should have direct access to the signing key in production

### Incident Response
- Define and document the response procedure for a suspected key compromise before going live
- A compromised key requires: immediate revocation, rotation, and notification to compliance stakeholders
- All audit records signed with a compromised key must be flagged for manual review

### Audit
- Key rotation history must be retained for the full audit retention period
- KMS access logs must be included in compliance reporting

## Recommended KMS Integrations

- AWS KMS with CloudTrail logging
- Azure Key Vault with diagnostic logging enabled
- GCP Cloud KMS with Cloud Audit Logs

See the relevant provider documentation for service identity configuration.