# Orchestration Layer

The Orchestration Layer is the governance kernel. It is the connective tissue between abstract policy definitions and deterministic runtime enforcement.

## What It Does

The Orchestration Layer:
1. Reads policy definitions from the Policy Layer
2. Evaluates real-time application context (user geography, session state, model version, request type)
3. Compiles context-aware enforcement rules and pushes them to the Enforcement Layer cache
4. Monitors behavioral drift signals from the telemetry pipeline
5. Triggers circuit breakers when drift thresholds are exceeded

## Behavioral Drift Monitoring

The Orchestration Layer monitors the Evaluation Blocks streaming through the telemetry pipeline in real time. Key signals:

- **Vector drift scores**: Distance between model outputs and the ground-truth knowledge boundary
- **Token velocity**: Unusual spikes in API call frequency
- **Semantic redundancy**: Outputs that stop making forward progress toward the session objective
- **Session lineage**: Multi-turn drift patterns that are invisible at the individual request level

## Circuit Breaker Triggers

When drift signals exceed configured thresholds, the Orchestration Layer fires a circuit breaker:

```yaml
circuit_breaker:
  name: sycophancy-drift-breaker
  signal: vector_drift_score
  threshold: 0.85
  window_turns: 3
  action: terminate_session_and_log
```

## Wallet Issuance

For agentic workflows, the Orchestration Layer issues Allocated Wallets at task initialization. See `schemas/wallet/` for the wallet schema specification.