"""
ARG Agent Simulator
====================
Simulates a realistic API fan-out event and demonstrates the financial
circuit breaker tripping before wallet allocation is exhausted.

Scenario:
  An agent receives a research objective and spawns three child processes.
  Child process 2 enters a recursive loop -- passing context between two
  internal endpoints without making forward progress toward the objective.
  API call velocity spikes. The circuit breaker fires at turn 14 of 20.
  The other two child processes continue unaffected (isolated wallets).

Run:
    python agent_simulator.py

    # With Kafka pipeline:
    python agent_simulator.py --kafka

    # Verbose output with spend detail:
    python agent_simulator.py --verbose
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from wallet import (
    AllocatedWallet,
    AuthorizerType,
    OperationType,
    OperationWallet,
    TaskCategory,
)
from circuit_breaker import BreakerEvent, BreakerState, FinancialCircuitBreaker


# ---------------------------------------------------------------------------
# ANSI colors for terminal output
# ---------------------------------------------------------------------------

class C:
    RESET  = "\033[0m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    CYAN   = "\033[96m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def _print_event(event: BreakerEvent) -> None:
    print(
        f"\n{C.RED}{C.BOLD}  !! CIRCUIT BREAKER FIRED{C.RESET}"
        f"\n     trigger  : {event.trigger_signal.value}"
        f"\n     state    : {event.previous_state.value} → {event.new_state.value}"
        f"\n     calls/min: {event.calls_per_minute_rate}"
        f"\n     window   : {event.call_count_in_window} calls"
        f"\n     endpoints: {event.unique_endpoints_in_window} unique"
        f"\n     repeated : {event.repeated_endpoint_pattern}"
        f"\n     util     : {event.mission_utilization_percent:.1f}%"
        f"\n     wallet   : {event.wallet_id[:8]}..."
    )


# ---------------------------------------------------------------------------
# Dependency graph (locked at wallet issuance)
# ---------------------------------------------------------------------------

RESEARCH_DEPENDENCY_GRAPH = {
    "graph_id": str(uuid.uuid4()),
    "version": "1.0.0",
    "allowed_models": [
        {"model_id": "gpt-4o", "max_tokens_per_call": 4096},
        {"model_id": "text-embedding-3-small", "max_tokens_per_call": 8192},
    ],
    "allowed_endpoints": [
        {"url_pattern": "https://api.search.example.com/*", "methods": ["GET"], "rate_limit_per_minute": 60},
        {"url_pattern": "https://api.summarize.example.com/*", "methods": ["POST"], "rate_limit_per_minute": 30},
        {"url_pattern": "https://api.classify.example.com/*", "methods": ["POST"], "rate_limit_per_minute": 30},
    ],
    "blocked_endpoints": [],
}

DEPENDENCY_GRAPH_HASH = AllocatedWallet.compute_dependency_graph_hash(RESEARCH_DEPENDENCY_GRAPH)


# ---------------------------------------------------------------------------
# Wallet factory
# ---------------------------------------------------------------------------

def issue_child_wallet(
    agent_name: str,
    token_limit: int,
    cost_limit_usd: float,
    parent_execution_tree_id: str,
) -> AllocatedWallet:
    """Issue an isolated wallet for each child agent."""
    return AllocatedWallet.issue(
        execution_tree_id=str(uuid.uuid4()),
        token_limit=token_limit,
        cost_limit_usd=cost_limit_usd,
        ttl_seconds=300,
        dependency_graph_hash=DEPENDENCY_GRAPH_HASH,
        appropriation_id="proj-2026-q2-competitive-research",
        intent_declaration=(
            f"{agent_name}: retrieve and summarize competitor pricing pages "
            "within the authorized endpoint set"
        ),
        task_category=TaskCategory.RESEARCH,
        authorizer_id="user-sandy-p",
        authorizer_type=AuthorizerType.HUMAN,
        burst_reserve_tokens=int(token_limit * 0.10),
        operation_wallets=[
            OperationWallet(
                operation_type=OperationType.WEB_SEARCH,
                token_limit=int(token_limit * 0.50),
                max_calls_per_minute=30,
            ),
            OperationWallet(
                operation_type=OperationType.LLM_INFERENCE,
                token_limit=int(token_limit * 0.40),
                max_calls_per_minute=20,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

class AgentSimulation:
    def __init__(self, use_kafka: bool = False, verbose: bool = False) -> None:
        self.use_kafka = use_kafka
        self.verbose = verbose
        self.kafka_pipeline = None

        if use_kafka:
            from kafka_pipeline import KafkaPipeline
            self.kafka_pipeline = KafkaPipeline()

        # Issue isolated wallets for three child agents
        parent_tree_id = str(uuid.uuid4())
        self.wallets = {
            "agent-child-1": issue_child_wallet("agent-child-1", 20_000, 1.00, parent_tree_id),
            "agent-child-2": issue_child_wallet("agent-child-2", 20_000, 1.00, parent_tree_id),
            "agent-child-3": issue_child_wallet("agent-child-3", 20_000, 1.00, parent_tree_id),
        }

        # Wrap each wallet in a circuit breaker
        self.breakers = {
            agent_id: FinancialCircuitBreaker(
                wallet=wallet,
                call_rate_threshold=40.0,   # 40 calls/minute threshold
                window_seconds=30,
                cooldown_seconds=60,
                minimum_sample_size=5,
                on_event=self._handle_breaker_event,
            )
            for agent_id, wallet in self.wallets.items()
        }

        self.tripped_agents: set[str] = set()

    def _handle_breaker_event(self, event: BreakerEvent) -> None:
        _print_event(event)
        if self.kafka_pipeline:
            self.kafka_pipeline.emit_breaker_event(event)

    def _simulate_normal_call(
        self,
        agent_id: str,
        turn: int,
        endpoint: str,
        operation_type: OperationType,
        tokens: int,
        cost_usd: float,
    ) -> bool:
        """Simulate a normal agent API call. Returns True if approved."""
        if agent_id in self.tripped_agents:
            return False

        breaker = self.breakers[agent_id]
        result = breaker.request_spend(
            tokens=tokens,
            cost_usd=cost_usd,
            operation_type=operation_type,
            endpoint=endpoint,
            agent_id=agent_id,
        )

        status = f"{C.GREEN}ALLOW{C.RESET}" if result.approved else f"{C.RED}BLOCK{C.RESET}"
        util = f"{result.mission_utilization_percent:.1f}%" if result.mission_utilization_percent else "n/a"

        if self.verbose or not result.approved:
            print(
                f"  [{_ts()}] {agent_id:16s} turn={turn:02d}  {status}"
                f"  tokens={tokens:5d}  util={util:6s}"
                + (f"  reason={result.denial_reason}" if result.denial_reason else "")
            )

        if breaker.state == BreakerState.OPEN and agent_id not in self.tripped_agents:
            self.tripped_agents.add(agent_id)

        if self.kafka_pipeline and result.approved:
            self.kafka_pipeline.emit_spend_event(
                wallet_id=self.wallets[agent_id].wallet_id,
                agent_id=agent_id,
                tokens=tokens,
                cost_usd=cost_usd,
                operation_type=operation_type,
                endpoint=endpoint,
            )

        return result.approved

    def run(self) -> None:
        print(f"\n{C.BOLD}{C.CYAN}ARG Financial Circuit Breaker -- Fan-Out Simulation{C.RESET}")
        print(f"{C.DIM}{'─' * 60}{C.RESET}")
        print(f"  Agents  : 3 child processes with isolated wallets")
        print(f"  Trigger : agent-child-2 enters recursive loop at turn 8")
        print(f"  Goal    : circuit breaker fires before wallet exhausted")
        print(f"{C.DIM}{'─' * 60}{C.RESET}\n")

        for turn in range(1, 21):
            print(f"{C.DIM}turn {turn:02d}{C.RESET}")

            # agent-child-1: normal research pattern
            self._simulate_normal_call(
                agent_id="agent-child-1",
                turn=turn,
                endpoint="https://api.search.example.com/v1/search",
                operation_type=OperationType.WEB_SEARCH,
                tokens=800,
                cost_usd=0.024,
            )
            time.sleep(0.05)

            # agent-child-2: enters fan-out loop at turn 8
            # Before turn 8: normal calls
            # From turn 8: recursive loop between two endpoints, velocity spikes
            if turn < 8:
                self._simulate_normal_call(
                    agent_id="agent-child-2",
                    turn=turn,
                    endpoint="https://api.summarize.example.com/v1/summarize",
                    operation_type=OperationType.LLM_INFERENCE,
                    tokens=1200,
                    cost_usd=0.036,
                )
            else:
                # Fan-out: rapid alternating calls between two endpoints
                # Each call individually looks reasonable -- the pattern is the problem
                for _ in range(4):
                    self._simulate_normal_call(
                        agent_id="agent-child-2",
                        turn=turn,
                        endpoint="https://api.summarize.example.com/v1/summarize",
                        operation_type=OperationType.LLM_INFERENCE,
                        tokens=950,
                        cost_usd=0.028,
                    )
                    self._simulate_normal_call(
                        agent_id="agent-child-2",
                        turn=turn,
                        endpoint="https://api.classify.example.com/v1/classify",
                        operation_type=OperationType.LLM_INFERENCE,
                        tokens=950,
                        cost_usd=0.028,
                    )
                    time.sleep(0.01)

            # agent-child-3: normal research pattern, different endpoints
            self._simulate_normal_call(
                agent_id="agent-child-3",
                turn=turn,
                endpoint="https://api.classify.example.com/v1/classify",
                operation_type=OperationType.LLM_INFERENCE,
                tokens=600,
                cost_usd=0.018,
            )

            time.sleep(0.1)

        # Final state report
        print(f"\n{C.BOLD}Simulation complete{C.RESET}")
        print(f"{C.DIM}{'─' * 60}{C.RESET}")

        for agent_id, wallet in self.wallets.items():
            breaker = self.breakers[agent_id]
            status_color = C.RED if agent_id in self.tripped_agents else C.GREEN
            status_label = "TRIPPED" if agent_id in self.tripped_agents else "HEALTHY"
            print(
                f"  {agent_id:16s}  {status_color}{status_label:8s}{C.RESET}"
                f"  util={wallet.maximum_token_quantum.utilization_percent:5.1f}%"
                f"  cost=${wallet.maximum_token_quantum.cost_consumed_usd:.4f}"
                f"  status={wallet.status.value}"
                f"  breaker_events={len(breaker.events)}"
            )

        tripped = len(self.tripped_agents)
        healthy = 3 - tripped
        print(f"\n  {C.GREEN}{healthy} agent(s) completed normally{C.RESET}")
        if tripped:
            print(f"  {C.RED}{tripped} agent(s) terminated by circuit breaker{C.RESET}")
            for agent_id in self.tripped_agents:
                wallet = self.wallets[agent_id]
                util = wallet.maximum_token_quantum.utilization_percent
                print(
                    f"    {agent_id}: wallet at {util:.1f}% utilization when terminated"
                    f" (saved {100 - util:.1f}% of budget)"
                )

        if self.kafka_pipeline:
            self.kafka_pipeline.flush()
            print(f"\n  {C.CYAN}Events emitted to Kafka{C.RESET}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="ARG Financial Circuit Breaker simulation")
    parser.add_argument("--kafka", action="store_true", help="Emit events to local Kafka")
    parser.add_argument("--verbose", action="store_true", help="Show all call results")
    args = parser.parse_args()

    sim = AgentSimulation(use_kafka=args.kafka, verbose=args.verbose)
    sim.run()


if __name__ == "__main__":
    main()
