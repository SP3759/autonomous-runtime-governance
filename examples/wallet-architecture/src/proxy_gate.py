"""
ARG Proxy Gate

The proxy gate is the enforcement layer that sits between an agent and every
outbound API call.  It is the only path through which tokens can be spent.
Agents never call external endpoints directly; they call the proxy gate and
only proceed if the gate approves.

Enforcement sequence (in order — any DENY short-circuits):
  1. Wallet status       — REVOKED / EXPIRED stop all traffic immediately
  2. TTL                 — reject if the wallet's TTL has elapsed
  3. Dependency graph    — reject if the requested endpoint is not in the
                           wallet's allowed_endpoints list
  4. Wallet spend        — delegate to AllocatedWallet.request_spend()
                           (handles token/cost limits, burst reserve, etc.)

GateDecision is the unit returned to the caller.  The caller MUST check
.approved before making the downstream API call.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import sys
import os

# Resolve wallet.py from the financial-circuit-breaker example
sys.path.insert(
    0,
    os.path.normpath(
        os.path.join(os.path.dirname(__file__), '..', '..', 'financial-circuit-breaker', 'src')
    )
)
from wallet import AllocatedWallet, OperationType, SpendResult, WalletStatus


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class DenyReason(str, Enum):
    WALLET_REVOKED            = "WALLET_REVOKED"
    WALLET_EXPIRED            = "WALLET_EXPIRED"
    TTL_ELAPSED               = "TTL_ELAPSED"
    DEPENDENCY_GRAPH_VIOLATION = "DEPENDENCY_GRAPH_VIOLATION"
    # Spend-level denials are passed through from wallet.SpendResult.denial_reason
    WALLET_EXHAUSTED          = "WALLET_EXHAUSTED"
    BURST_RESERVE_PENDING     = "BURST_RESERVE_PENDING_APPROVAL"
    OPERATION_BUDGET_EXHAUSTED = "OPERATION_BUDGET_EXHAUSTED"


@dataclass
class GateDecision:
    approved: bool
    wallet_id: str
    agent_id: str
    endpoint: str
    deny_reason: str | None = None
    tokens_approved: int | None = None
    cost_approved_usd: float | None = None
    tokens_remaining: int | None = None
    burst_reserve_available: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def _deny(cls, wallet_id: str, agent_id: str, endpoint: str,
               reason: str, **extra) -> "GateDecision":
        return cls(
            approved=False,
            wallet_id=wallet_id,
            agent_id=agent_id,
            endpoint=endpoint,
            deny_reason=reason,
            **extra,
        )

    @classmethod
    def _approve(cls, wallet_id: str, agent_id: str, endpoint: str,
                 result: SpendResult) -> "GateDecision":
        return cls(
            approved=True,
            wallet_id=wallet_id,
            agent_id=agent_id,
            endpoint=endpoint,
            tokens_approved=result.tokens_approved,
            cost_approved_usd=result.cost_approved_usd,
        )


# ---------------------------------------------------------------------------
# ProxyGate
# ---------------------------------------------------------------------------

class ProxyGate:
    """
    Stateless enforcement layer.  One ProxyGate instance can be shared across
    multiple agents; all agent state lives in their individual AllocatedWallet.

    Parameters:
      dependency_graph   — the actual graph dict against which endpoint URLs
                           are validated.  Must match the hash stored in the
                           wallet (validated at wallet issuance time, not here).
                           Shape: {"allowed_endpoints": [{"url_pattern": "..."}],
                                   "blocked_endpoints": [...]}
    """

    def __init__(self, dependency_graph: dict[str, Any]) -> None:
        self._graph = dependency_graph

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def request(
        self,
        *,
        wallet: AllocatedWallet,
        agent_id: str,
        endpoint: str,
        tokens: int,
        cost_usd: float,
        operation_type: OperationType = OperationType.EXTERNAL_API,
    ) -> GateDecision:
        """
        Gate a single outbound API call.  Returns a GateDecision; the caller
        must check .approved before proceeding.
        """
        wid = str(wallet.wallet_id)

        # 1. Wallet status
        if wallet.status == WalletStatus.REVOKED:
            return GateDecision._deny(wid, agent_id, endpoint, DenyReason.WALLET_REVOKED)
        if wallet.status == WalletStatus.EXPIRED:
            return GateDecision._deny(wid, agent_id, endpoint, DenyReason.WALLET_EXPIRED)

        # 2. TTL
        if wallet.is_expired:
            return GateDecision._deny(
                wid, agent_id, endpoint, DenyReason.TTL_ELAPSED,
                detail={"elapsed_seconds": time.time() - wallet.issued_at.timestamp()},
            )

        # 3. Dependency graph — check blocked list first, then allowed
        if not self._endpoint_allowed(endpoint):
            return GateDecision._deny(
                wid, agent_id, endpoint, DenyReason.DEPENDENCY_GRAPH_VIOLATION,
                detail={"endpoint": endpoint, "allowed_patterns": self._allowed_patterns()},
            )

        # 4. Wallet spend
        result = wallet.request_spend(
            tokens=tokens,
            cost_usd=cost_usd,
            operation_type=operation_type,
        )
        if not result.approved:
            return GateDecision._deny(
                wid, agent_id, endpoint,
                reason=result.denial_reason or "DENIED",
                tokens_remaining=result.tokens_remaining,
                burst_reserve_available=result.burst_reserve_available,
            )

        return GateDecision._approve(wid, agent_id, endpoint, result)

    def endpoint_allowed(self, endpoint: str) -> bool:
        """Public check — returns True if endpoint matches an allowed pattern."""
        return self._endpoint_allowed(endpoint)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _endpoint_allowed(self, endpoint: str) -> bool:
        # Check blocked list first
        for entry in self._graph.get("blocked_endpoints", []):
            if self._matches(endpoint, entry.get("url_pattern", "")):
                return False
        # Must match at least one allowed pattern
        for entry in self._graph.get("allowed_endpoints", []):
            if self._matches(endpoint, entry.get("url_pattern", "")):
                return True
        return False

    @staticmethod
    def _matches(url: str, pattern: str) -> bool:
        if pattern.endswith("*"):
            return url.startswith(pattern[:-1])
        return url == pattern

    def _allowed_patterns(self) -> list[str]:
        return [e["url_pattern"] for e in self._graph.get("allowed_endpoints", [])]
