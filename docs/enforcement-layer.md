# Enforcement Layer

The Enforcement Layer is the programmatic muscle of the ARG architecture. It executes deterministic binary decisions at sub-millisecond speed. No LLM reasoning. No probabilistic evaluation. Pure code.

## What It Does

- Runs fast regex checks on inputs and outputs
- Validates structural JSON schema conformance
- Executes vector distance math against policy boundaries
- Issues PASS or FAIL decisions instantly
- Streams audit events to the telemetry pipeline asynchronously

## What It Does Not Do

- Write to a database synchronously
- Wait for disk I/O
- Call an LLM to evaluate a compliance decision
- Block the execution path with heavy computation

## The Sidecar Gate

The Enforcement Layer is deployed as a platform proxy or sidecar, not baked into agent application code. This means:

- Security teams own and verify the enforcement boundary independently
- Product teams iterate on prompts and models without touching governance code
- The agent runtime is completely unaware of the compliance layer

## Cryptographic Signing

Every enforcement decision is signed with the gate private key before being committed to the auditability ledger. This makes the audit trail tamper-proof and verifiable.

See `community/key-management.md` for key governance requirements before production deployment.