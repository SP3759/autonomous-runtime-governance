# Financial Circuit Breaker

> An agent can stay perfectly inside its token budget and still burn through it at 3am. The circuit breaker is what stops that.

This example demonstrates the ARG Financial Circuit Breaker catching a runaway agent fan-out loop before it exhausts its wallet allocation -- and doing so while two parallel agents continue operating normally.

## The Scenario

An autonomous research agent receives a complex objective and spawns three child processes. Each child process is issued an isolated allocated wallet.

Child process 2 enters a recursive loop at turn 8 -- passing context between two internal endpoints without making forward progress toward the objective. API call velocity spikes. The circuit breaker fires at turn 14. The wallet is revoked at 34% utilization. Child processes 1 and 3 complete normally.

This is the fan-out failure mode that token budgets alone do not catch. Each individual call looks reasonable. The pattern is the problem.

## Architecture

```
                    ┌─────────────────────────────────┐
                    │     Parent Execution Tree        │
                    │     (research objective)         │
                    └──────────┬──────────────────────┘
                               │ spawns
            ┌──────────────────┼──────────────────┐
            ▼                  ▼                  ▼
     ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
     │  child-1    │   │  child-2    │   │  child-3    │
     │  (healthy)  │   │  (fan-out)  │   │  (healthy)  │
     └──────┬──────┘   └──────┬──────┘   └──────┬──────┘
            │                 │                  │
     ┌──────▼──────┐   ┌──────▼──────┐   ┌──────▼──────┐
     │  Wallet +   │   │  Wallet +   │   │  Wallet +   │
     │  Breaker    │   │  Breaker    │   │  Breaker    │
     └──────┬──────┘   └──────┬──────┘   └──────┬──────┘
            │                 │ TRIP             │
            ▼                 ▼                  ▼
     continues          CIRCUIT OPEN         continues
                        wallet revoked
                               │
                               ▼ async
                    ┌─────────────────────┐
                    │  Kafka / Event Hubs  │
                    │  (telemetry stream)  │
                    └─────────────────────┘
```

## What This Demonstrates

**Fan-out detection.** The circuit breaker maintains a sliding window of call records. When call velocity exceeds the configured threshold -- or when the same endpoint is called repeatedly without forward progress -- the breaker trips.

**Wallet isolation.** Each child agent holds its own allocated wallet. A tripped breaker on child-2 does not affect child-1 or child-3. This is the blast radius containment the article series describes.

**Purpose classification.** Every wallet carries a `purpose_classification` block: the appropriation ID, intent declaration, and authorization chain. This is the color-of-money record. The audit trail can answer both "how much was spent" and "what was it authorized for."

**Deterministic enforcement.** The circuit breaker trip is not a probabilistic decision. It is pure arithmetic against a sliding window. No LLM reasoning. No latency.

## Quickstart

**Prerequisites:** Python 3.11+, Docker

```bash
# 1. Clone the repo
git clone https://github.com/SP3759/autonomous-runtime-governance.git
cd autonomous-runtime-governance/examples/financial-circuit-breaker

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the simulation (no Kafka required)
python src/agent_simulator.py --verbose

# 4. Run with local Kafka pipeline
docker-compose up -d
python src/agent_simulator.py --kafka --verbose
docker-compose down
```

Expected output (abbreviated):
```
ARG Financial Circuit Breaker -- Fan-Out Simulation
────────────────────────────────────────────────────
  Agents  : 3 child processes with isolated wallets
  Trigger : agent-child-2 enters recursive loop at turn 8
  Goal    : circuit breaker fires before wallet exhausted

turn 08
  !! CIRCUIT BREAKER FIRED
     trigger  : call_rate_exceeded
     state    : CLOSED → OPEN
     calls/min: 48.0
     window   : 8 calls
     ...

Simulation complete
────────────────────────────────────────────────────
  agent-child-1    HEALTHY   util=28.0%  cost=$0.2160
  agent-child-2    TRIPPED   util=34.2%  cost=$0.2632  breaker_events=1
  agent-child-3    HEALTHY   util=19.0%  cost=$0.1440

  2 agent(s) completed normally
  1 agent(s) terminated by circuit breaker
    agent-child-2: wallet at 34.2% utilization when terminated (saved 65.8% of budget)
```

## Files

```
financial-circuit-breaker/
├── README.md                    This file
├── docker-compose.yml           Local Kafka + Zookeeper + Schema Registry
├── requirements.txt
└── src/
    ├── wallet.py                Allocated wallet with purpose classification
    ├── circuit_breaker.py       Fan-out detection and state machine
    ├── agent_simulator.py       Runnable fan-out scenario
    └── kafka_pipeline.py        Governance event producer
```

## Key Design Decisions

**Pre-allocation, not post-tracking.** The wallet decrements before a call is made. An agent that cannot secure budget does not make the call. There is no reconciliation step.

**Purpose classification is not optional.** The wallet schema requires `appropriation_id`, `intent_declaration`, and at least one entry in `authorization_chain`. You cannot issue a wallet without declaring what it is for and who authorized it. This is the control the circuit breaker alone does not close.

**Telemetry is out-of-band.** The Kafka publish is fire-and-forget. It never blocks the enforcement decision. A Kafka outage does not degrade enforcement.

**Operation wallets are optional but recommended.** Subdividing the mission budget by tool call type (search, inference, code execution) prevents any single operation category from consuming the full allocation. Omit them for simple agents; configure them when tool call distribution matters.

## Extending This Example

The circuit breaker `on_event` callback accepts any callable. Swap `KafkaPipeline` for your observability stack:

```python
from circuit_breaker import FinancialCircuitBreaker, BreakerEvent
from wallet import AllocatedWallet

def my_alert_handler(event: BreakerEvent) -> None:
    # send to PagerDuty, Datadog, Azure Monitor, etc.
    pass

breaker = FinancialCircuitBreaker(
    wallet=wallet,
    on_event=my_alert_handler,
)
```

## Related

- `schemas/wallet/allocated-wallet.json` -- full wallet schema including purpose classification
- `schemas/wallet/financial-event.json` -- unified financial governance event schema
- `config/circuit-breaker/financial-circuit-breaker.yaml` -- YAML configuration reference
- `config/sidecar-proxy/eventhubs-pipeline.yaml` -- Azure Event Hubs production config
- AI Governance Blueprint Part 3: Financial Governance (article series)
