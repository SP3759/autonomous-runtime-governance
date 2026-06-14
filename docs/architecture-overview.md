# ARG Architecture Overview

## The Core Problem with Static Governance

You cannot govern probabilistic AI systems with static rules. Language is fluid. A policy document on a shared drive cannot intercept a live data payload. A system prompt cannot stop a runaway agent from making API calls at 3am.

ARG solves this by moving governance from documentation into continuously operational infrastructure.

## The Four-Layer Model

### Layer 1: Policy Layer (Compliance)
The definitional tier. Legal boundaries, corporate risk thresholds, and localized privacy mandates live here. This layer defines what is allowed, not how it is enforced.

### Layer 2: Orchestration Layer (Governance Kernel)
The connective tissue. The Orchestration Layer takes abstract rules from the Policy Layer and automatically translates them into hard, automated gates in the Enforcement Layer based on real-time application context.

### Layer 3: Enforcement Layer (Security)
The programmatic muscle. Token rate-limiters, sandboxed runtime environments, cryptographic ledger logging, and binary input/output filters. Sub-millisecond execution speed. No LLM reasoning involved.

### Layer 4: Identity Layer (Access Governance)
The accountability boundary. The Identity Proxy sits between the agent orchestrator and every downstream service. It validates cryptographic parent-child lineage, matches semantic context against initialization scope, and mints ephemeral attestation tokens that self-destruct after each sub-task. Prevents non-delegated privilege escalation that standard IAM stacks cannot detect.

## The Sidecar Architecture

Governance lives in network middleware, not inside the agent application code.

```
[Agent Runtime] → API call → [Platform Proxy / Sidecar Gate]
                                        ↓ (synchronous, sub-ms)
                               [PASS / FAIL decision]
                                   ↓              ↓
                          [Identity Proxy]   [Asynchronous Stream]
                          Lineage validation      ↓
                          Semantic matching  [Kafka / Event Hubs]
                          Ephemeral tokens        ↓
                                           [Auditability Ledger]
```

## Two Operational Tracks

**Synchronous Enforcement**: Fast regex checks, JSON validation, vector distance math, identity lineage verification. Binary pass/fail at the gate. No database writes, no disk I/O.

**Asynchronous Auditability**: All computationally expensive processing happens downstream in a replayable streaming pipeline. The evidence ledger captures the full execution lineage, identity chain, and intent classification without touching the critical path.

## Key Design Principles

- Policy as code, not documentation
- Deterministic enforcement, never LLM-based
- Governance decoupled from agent application code
- Every execution event is signed, replayable, and tamper-proof
- Financial blast radius is constrained at the infrastructure level
- Identity lineage is tracked cryptographically across the full execution tree
- Agents can only execute what they were scoped to do at the exact moment of initialization