# Example: Allocated Wallet Architecture

Demonstrates the four enforcement primitives of the ARG Allocated Wallet using a
multi-agent research execution tree.

## Scenario

An orchestrator receives the objective: **"Analyze Q2 2024 market trends in renewable
energy."**  It issues three child agent wallets — each with an independent token budget,
an allowed-endpoint graph, and a TTL — then drives them through four enforcement events:

| Phase | Agent | Outcome |
|---|---|---|
| 1 — Normal spend gating | All three | Approved calls decrement wallets |
| 2 — Dependency graph violation | child-2 (search) | Unauthorized exfil endpoint blocked |
| 3 — TTL expiry | child-3 (parser) | Stalled agent rejected after TTL elapses |
| 4 — Burst reserve escalation | child-1 (summarizer) | Base budget exhausted → human approves burst reserve → agent continues |

## Structure

```
examples/wallet-architecture/
├── src/
│   ├── proxy_gate.py       # Enforcement layer: status, TTL, dependency graph, spend
│   └── execution_tree.py   # Runnable: parent + 3 child agents, full 4-phase scenario
└── tests/
    └── test_proxy_gate.py  # 14 unit tests for all gate enforcement paths
```

## Quick Start

```bash
cd examples/wallet-architecture

# Run the simulation
python src/execution_tree.py

# Run with full gate decision detail
python src/execution_tree.py --verbose

# Run the test suite
python -m pytest tests/ -v
```

## Proxy Gate Enforcement Order

`ProxyGate.request()` evaluates every outbound call in this sequence.  The first
`DENY` short-circuits — no tokens are spent and the downstream API is never called.

```
1. Wallet status   REVOKED → WALLET_REVOKED
                   EXPIRED → WALLET_EXPIRED

2. TTL             wallet.is_expired → TTL_ELAPSED

3. Dependency      endpoint not in allowed_endpoints → DEPENDENCY_GRAPH_VIOLATION
   graph           endpoint in blocked_endpoints     → DEPENDENCY_GRAPH_VIOLATION

4. Wallet spend    delegates to AllocatedWallet.request_spend()
                   → WALLET_EXHAUSTED / BURST_RESERVE_PENDING_APPROVAL /
                     MISSION_BUDGET_EXHAUSTED / OPERATION_BUDGET_EXHAUSTED / APPROVED
```

The gate is stateless.  All state lives in `AllocatedWallet`.  One `ProxyGate` instance
can be shared across all agents in an execution tree, provided each agent holds its own
wallet.

## Wallet Primitives Demonstrated

**Purpose classification** — Each wallet is issued with `appropriation_id`,
`intent_declaration`, and `task_category`, proving the "color of money" before any
tokens are allocated.  The authorizer chain is immutable after issuance.

**Dependency graph hash** — The allowed-endpoint graph is hashed at issuance time.
The proxy gate validates each endpoint against the graph at call time.  If the graph
definition were tampered with after issuance, `verify_endpoint()` would return False
because the hash would no longer match.

**TTL enforcement** — `ttl_seconds` is embedded in the wallet at issuance.  The proxy
gate checks `wallet.is_expired` before every call.  Stalled agents cannot consume budget
indefinitely; their wallets expire automatically.

**Burst reserve** — A fraction of the token budget can be held in reserve at issuance.
When the base budget reaches zero the wallet enters `BURST_RESERVE_PENDING` state and
all calls are blocked.  A human supervisor (or escalation policy) must call
`release_burst_reserve()` to unlock the reserve.  This is an explicit human-in-the-loop
checkpoint, not an automatic overflow.

## Relationship to Financial Circuit Breaker

The wallet enforces **budget limits and endpoint authorization** — it answers
"is this agent allowed to spend here, and does it have the budget?"

The financial circuit breaker (`examples/financial-circuit-breaker/`) wraps the wallet
and adds **behavioral pattern detection** — it answers "is this agent exhibiting
runaway call velocity, fan-out loops, or semantic redundancy?"

In production both run together: the circuit breaker calls `ProxyGate.request()` on
every outbound call, and also monitors call patterns to decide whether to trip the
breaker and revoke the wallet entirely.
