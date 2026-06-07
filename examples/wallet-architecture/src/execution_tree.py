"""
ARG Wallet Architecture — Execution Tree Simulation

Scenario: "Analyze Q2 2024 market trends in renewable energy"

A parent orchestrator issues three child agent wallets with independent
budgets, operation allowances, and endpoint graphs.  The simulation drives
each child through a sequence of API calls demonstrating four enforcement
primitives:

  1. Normal spend gating     — approved calls decrement the wallet
  2. Dependency graph block  — child-2 tries an endpoint not in its graph
  3. TTL expiry              — child-3 stalls; subsequent calls are rejected
  4. Burst reserve escalation — child-1 exhausts its base budget; human
                                approval releases the burst reserve and
                                the agent continues

Usage:
  python execution_tree.py
  python execution_tree.py --verbose
"""

from __future__ import annotations

import argparse
import sys
import os
import time
import uuid

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(
    0,
    os.path.normpath(
        os.path.join(os.path.dirname(__file__), '..', '..', 'financial-circuit-breaker', 'src')
    )
)

from wallet import (
    AllocatedWallet,
    AuthorizerType,
    OperationType,
    OperationWallet,
    RevocationReason,
    TaskCategory,
    WalletStatus,
)
from proxy_gate import GateDecision, ProxyGate


# ---------------------------------------------------------------------------
# Dependency graphs (one per child — each has its own allowed endpoints)
# ---------------------------------------------------------------------------

GRAPH_INFERENCE = {
    "allowed_endpoints": [
        {"url_pattern": "https://api.anthropic.com/*"},
    ],
    "blocked_endpoints": [],
}

GRAPH_SEARCH = {
    "allowed_endpoints": [
        {"url_pattern": "https://api.search.example.com/*"},
        {"url_pattern": "https://api.news.example.com/*"},
    ],
    "blocked_endpoints": [],
}

GRAPH_PARSER = {
    "allowed_endpoints": [
        {"url_pattern": "https://api.parser.example.com/*"},
    ],
    "blocked_endpoints": [],
}


# ---------------------------------------------------------------------------
# Wallet factory helpers
# ---------------------------------------------------------------------------

EXECUTION_TREE_ID = str(uuid.uuid4())


def _issue(
    role: str,
    graph: dict,
    token_limit: int,
    cost_limit_usd: float,
    ttl_seconds: int = 300,
    burst_reserve_tokens: int = 0,
    operation_wallets: list[OperationWallet] | None = None,
) -> AllocatedWallet:
    return AllocatedWallet.issue(
        execution_tree_id=EXECUTION_TREE_ID,
        token_limit=token_limit,
        cost_limit_usd=cost_limit_usd,
        ttl_seconds=ttl_seconds,
        dependency_graph_hash=AllocatedWallet.compute_dependency_graph_hash(graph),
        appropriation_id="Q2-2024-ENERGY-RESEARCH",
        intent_declaration=f"Renewable energy market analysis — {role}",
        task_category=TaskCategory.RESEARCH,
        authorizer_id="orchestrator-agent",
        authorizer_type=AuthorizerType.SERVICE_PRINCIPAL,
        burst_reserve_tokens=burst_reserve_tokens,
        operation_wallets=operation_wallets or [],
    )


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def run(verbose: bool = False) -> None:
    print("\nARG Wallet Architecture - Execution Tree Simulation")
    print(f"Execution tree: {EXECUTION_TREE_ID}")
    print(f"Objective: Analyze Q2 2024 market trends in renewable energy")
    print("=" * 72)

    # ------------------------------------------------------------------
    # Issue wallets
    # ------------------------------------------------------------------

    wallet_child1 = _issue(
        role="LLM inference (summarizer)",
        graph=GRAPH_INFERENCE,
        token_limit=500,          # tight budget to trigger burst demo
        cost_limit_usd=2.00,
        burst_reserve_tokens=200, # human can unlock an extra 200
        operation_wallets=[
            OperationWallet(operation_type=OperationType.LLM_INFERENCE, token_limit=500),
        ],
    )

    wallet_child2 = _issue(
        role="Web search (source gathering)",
        graph=GRAPH_SEARCH,
        token_limit=800,
        cost_limit_usd=1.50,
        operation_wallets=[
            OperationWallet(operation_type=OperationType.WEB_SEARCH, token_limit=800),
        ],
    )

    wallet_child3 = _issue(
        role="PDF parser (data extraction)",
        graph=GRAPH_PARSER,
        token_limit=400,
        cost_limit_usd=0.50,
        ttl_seconds=1,            # very short TTL to demonstrate expiry
    )

    gate1 = ProxyGate(GRAPH_INFERENCE)
    gate2 = ProxyGate(GRAPH_SEARCH)
    gate3 = ProxyGate(GRAPH_PARSER)

    _section("Wallets Issued")
    _wallet_summary(wallet_child1, "child-1 (summarizer, HIGH priority)")
    _wallet_summary(wallet_child2, "child-2 (search, MEDIUM priority)")
    _wallet_summary(wallet_child3, "child-3 (parser, LOW priority, TTL=1s)")

    # ------------------------------------------------------------------
    # Phase 1: Normal spend gating
    # ------------------------------------------------------------------

    _section("Phase 1 - Normal Spend Gating")

    calls = [
        (gate1, wallet_child1, "child-1", "https://api.anthropic.com/v1/messages",
         OperationType.LLM_INFERENCE, 150, 0.02, "First inference call"),
        (gate2, wallet_child2, "child-2", "https://api.search.example.com/v1/search",
         OperationType.WEB_SEARCH, 100, 0.005, "Web search: Q2 renewable energy"),
        (gate2, wallet_child2, "child-2", "https://api.news.example.com/v1/headlines",
         OperationType.WEB_SEARCH, 80, 0.004, "News headlines: solar/wind 2024"),
        (gate3, wallet_child3, "child-3", "https://api.parser.example.com/v1/parse",
         OperationType.EXTERNAL_API, 60, 0.001, "Parse IEA report PDF"),
    ]

    for gate, wallet, agent, endpoint, op, tokens, cost, label in calls:
        decision = gate.request(
            wallet=wallet, agent_id=agent, endpoint=endpoint,
            tokens=tokens, cost_usd=cost, operation_type=op,
        )
        _print_decision(decision, label, verbose)

    # ------------------------------------------------------------------
    # Phase 2: Dependency graph violation
    # ------------------------------------------------------------------

    _section("Phase 2 - Dependency Graph Violation")

    decision = gate2.request(
        wallet=wallet_child2,
        agent_id="child-2",
        endpoint="https://api.exfil.example.com/upload",
        tokens=50,
        cost_usd=0.001,
        operation_type=OperationType.EXTERNAL_API,
    )
    _print_decision(decision, "child-2 tries unauthorized exfil endpoint", verbose)
    assert not decision.approved
    assert decision.deny_reason == "DEPENDENCY_GRAPH_VIOLATION"

    # ------------------------------------------------------------------
    # Phase 3: TTL expiry (child-3 stalls)
    # ------------------------------------------------------------------

    _section("Phase 3 - TTL Expiry (child-3 stalls)")

    print("  [Simulating child-3 stall — sleeping 2s past TTL=1s]")
    time.sleep(2)

    decision = gate3.request(
        wallet=wallet_child3,
        agent_id="child-3",
        endpoint="https://api.parser.example.com/v1/parse",
        tokens=60,
        cost_usd=0.001,
        operation_type=OperationType.EXTERNAL_API,
    )
    _print_decision(decision, "child-3 resumes after stall (should be denied)", verbose)
    assert not decision.approved
    assert decision.deny_reason == "TTL_ELAPSED"

    print("  => Orchestrator terminates child-3 (lowest priority). Budget preserved.")

    # ------------------------------------------------------------------
    # Phase 4: Budget exhaustion + burst reserve escalation
    # ------------------------------------------------------------------

    _section("Phase 4 - Budget Exhaustion + Burst Reserve Escalation")

    # child-1 base = token_limit(500) - burst_reserve(200) = 300 base tokens.
    # Already spent 150 in Phase 1 → 150 base tokens remain.
    # Spend exactly 150 to reach remaining=0 — this triggers the burst reserve signal.
    decision = gate1.request(
        wallet=wallet_child1,
        agent_id="child-1",
        endpoint="https://api.anthropic.com/v1/messages",
        tokens=150,
        cost_usd=0.02,
        operation_type=OperationType.LLM_INFERENCE,
    )
    _print_decision(decision, "child-1 spends last 150 base tokens (base now empty)", verbose)
    assert decision.approved

    # Base is now 0. Next call signals burst reserve available.
    decision = gate1.request(
        wallet=wallet_child1,
        agent_id="child-1",
        endpoint="https://api.anthropic.com/v1/messages",
        tokens=100,
        cost_usd=0.01,
        operation_type=OperationType.LLM_INFERENCE,
    )
    _print_decision(decision, "child-1 requests more tokens (base exhausted, burst available)", verbose)
    assert not decision.approved
    assert "BURST_RESERVE" in (decision.deny_reason or "")

    # Human escalation: approve burst reserve
    print()
    print("  [HUMAN ESCALATION] Orchestrator reviews: child-1 analysis is 70% complete.")
    print("  [HUMAN ESCALATION] Approves releasing 200-token burst reserve.")
    wallet_child1.release_burst_reserve(
        approver_id="human-supervisor",
        approver_type=AuthorizerType.HUMAN,
        scope="Q2-2024-ENERGY-RESEARCH:final-summary",
    )
    print(f"  Burst reserve released. Wallet status: {wallet_child1.status.value}")
    print(f"  Tokens now available: {wallet_child1.maximum_token_quantum.tokens_remaining}")

    # child-1 can now complete its work
    decision = gate1.request(
        wallet=wallet_child1,
        agent_id="child-1",
        endpoint="https://api.anthropic.com/v1/messages",
        tokens=100,
        cost_usd=0.01,
        operation_type=OperationType.LLM_INFERENCE,
    )
    _print_decision(decision, "child-1 retries after burst reserve released", verbose)
    assert decision.approved

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    _section("Final State")
    _wallet_summary(wallet_child1, "child-1 (summarizer)")
    _wallet_summary(wallet_child2, "child-2 (search)")
    _wallet_summary(wallet_child3, "child-3 (parser, stalled)")

    print()
    print("Simulation complete. All four enforcement primitives demonstrated:")
    print("  [OK] Normal spend gating")
    print("  [OK] Dependency graph violation blocked")
    print("  [OK] TTL expiry rejected stalled agent")
    print("  [OK] Burst reserve escalation approved by human supervisor")
    print()


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _section(title: str) -> None:
    print(f"\n--- {title} ---")


def _print_decision(decision: GateDecision, label: str, verbose: bool) -> None:
    status = "APPROVED" if decision.approved else f"DENIED ({decision.deny_reason})"
    tokens = f"{decision.tokens_approved}t" if decision.approved else ""
    print(f"  {'[OK]' if decision.approved else '[--]'} {label}")
    print(f"       {decision.agent_id} -> {decision.endpoint}")
    print(f"       {status} {tokens}")
    if verbose and not decision.approved and decision.detail:
        print(f"       detail: {decision.detail}")


def _wallet_summary(wallet: AllocatedWallet, label: str) -> None:
    q = wallet.maximum_token_quantum
    consumed  = q.tokens_consumed
    limit     = q.token_limit
    remaining = q.tokens_remaining
    burst     = q.burst_reserve_tokens
    released  = q.burst_reserve_released
    pct       = (consumed / limit * 100) if limit else 0
    print(
        f"  {label}\n"
        f"    status={wallet.status.value}  "
        f"consumed={consumed}/{limit} ({pct:.0f}%)  "
        f"remaining={remaining}  "
        f"burst={burst}{'(released)' if released else ''}"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ARG wallet architecture simulation")
    parser.add_argument("--verbose", action="store_true", help="Print gate decision details")
    args = parser.parse_args()
    run(verbose=args.verbose)
