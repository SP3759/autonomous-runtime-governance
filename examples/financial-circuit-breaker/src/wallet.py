"""
ARG Wallet Module
=================
Pydantic models and enforcement logic for the ARG Allocated Wallet architecture.

Design principles:
  - Wallets are pre-allocated and decremented, never post-hoc tracked.
    An agent that cannot secure budget before a call does not make the call.
  - Purpose classification travels with every transaction.
    The audit trail answers both "how much" and "what for".
  - Burst reserve requires explicit escalation.
    An agent cannot silently exceed its normal operating envelope.
  - All state transitions emit a FinancialEvent for the telemetry pipeline.

Reference: schemas/wallet/allocated-wallet.json v2.0.0
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class WalletStatus(str, Enum):
    ACTIVE = "ACTIVE"
    EXHAUSTED = "EXHAUSTED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    BURST_RESERVE_PENDING = "BURST_RESERVE_PENDING"


class RevocationReason(str, Enum):
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"
    MANUAL = "MANUAL"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    PURPOSE_MISMATCH = "PURPOSE_MISMATCH"
    TTL_EXPIRED = "TTL_EXPIRED"


class AuthorizerType(str, Enum):
    HUMAN = "human"
    POLICY_RULE = "policy_rule"
    SERVICE_PRINCIPAL = "service_principal"
    ESCALATION_APPROVAL = "escalation_approval"


class TaskCategory(str, Enum):
    DATA_RETRIEVAL = "data_retrieval"
    CONTENT_GENERATION = "content_generation"
    CODE_EXECUTION = "code_execution"
    EXTERNAL_API_ORCHESTRATION = "external_api_orchestration"
    MULTI_AGENT_DELEGATION = "multi_agent_delegation"
    RESEARCH = "research"
    ANALYSIS = "analysis"
    CUSTOMER_INTERACTION = "customer_interaction"
    INTERNAL_TOOLING = "internal_tooling"
    OTHER = "other"


class OperationType(str, Enum):
    LLM_INFERENCE = "llm_inference"
    WEB_SEARCH = "web_search"
    CODE_EXECUTION = "code_execution"
    DATABASE_QUERY = "database_query"
    EXTERNAL_API = "external_api"
    FILE_IO = "file_io"
    SUB_AGENT_DELEGATION = "sub_agent_delegation"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class AuthorizationEntry(BaseModel):
    """Single link in the authorization chain."""
    authorizer_id: str = Field(
        description="Human user ID, service principal, or policy rule ID"
    )
    authorizer_type: AuthorizerType
    authorized_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    authorization_ttl_seconds: Optional[int] = Field(
        default=None,
        ge=1,
        description="How long this authorization is valid"
    )
    authorization_scope: Optional[str] = Field(
        default=None,
        description="Narrows the intent declaration for this step"
    )


class OperationWallet(BaseModel):
    """
    Sub-wallet for a specific operation type.
    Prevents any single tool category from consuming the full mission budget.
    """
    operation_type: OperationType
    token_limit: int = Field(ge=1)
    cost_limit_usd: Optional[float] = Field(default=None, ge=0.0)
    tokens_consumed: int = Field(default=0, ge=0)
    cost_consumed_usd: float = Field(default=0.0, ge=0.0)
    call_count: int = Field(default=0, ge=0)
    max_calls_per_minute: Optional[int] = Field(
        default=None,
        ge=1,
        description="Rate limit for fan-out detection"
    )

    @property
    def tokens_remaining(self) -> int:
        return max(0, self.token_limit - self.tokens_consumed)

    @property
    def utilization_percent(self) -> float:
        return round((self.tokens_consumed / self.token_limit) * 100, 2)

    def can_afford(self, tokens: int, cost_usd: float = 0.0) -> bool:
        if self.tokens_consumed + tokens > self.token_limit:
            return False
        if self.cost_limit_usd is not None:
            if self.cost_consumed_usd + cost_usd > self.cost_limit_usd:
                return False
        return True

    def decrement(self, tokens: int, cost_usd: float = 0.0) -> None:
        """
        Decrement the operation wallet.
        Caller must check can_afford() before calling decrement().
        """
        self.tokens_consumed += tokens
        self.cost_consumed_usd += cost_usd
        self.call_count += 1


class PurposeClassification(BaseModel):
    """
    Color-of-money record. Answers what this spend is authorized for and who signed off.

    A circuit breaker constrains magnitude -- how much, how fast.
    PurposeClassification proves intent -- what was this spend for, and who authorized it.
    Both are required for a complete financial control.
    """
    appropriation_id: str = Field(
        description="Authorized purpose bucket. Maps to cost center, project code, or compliance program."
    )
    intent_declaration: str = Field(
        description="What this agent execution is authorized to do, declared before execution begins."
    )
    task_category: TaskCategory
    authorization_chain: list[AuthorizationEntry] = Field(
        min_length=1,
        description="Ordered list of approvals. First entry is originating authorization."
    )
    operation_wallets: list[OperationWallet] = Field(
        default_factory=list,
        description="Optional subdivision of mission budget by operation type."
    )

    def get_operation_wallet(self, operation_type: OperationType) -> Optional[OperationWallet]:
        for w in self.operation_wallets:
            if w.operation_type == operation_type:
                return w
        return None

    @property
    def primary_authorizer_id(self) -> str:
        return self.authorization_chain[0].authorizer_id

    @property
    def authorization_age_seconds(self) -> int:
        origin = self.authorization_chain[0].authorized_at
        now = datetime.now(timezone.utc)
        return int((now - origin).total_seconds())


class TokenQuantum(BaseModel):
    """Mission-level budget. Hard ceiling across the entire execution tree."""
    token_limit: int = Field(ge=1)
    cost_limit_usd: float = Field(ge=0.01)
    tokens_consumed: int = Field(default=0, ge=0)
    cost_consumed_usd: float = Field(default=0.0, ge=0.0)
    burst_reserve_tokens: int = Field(
        default=0,
        ge=0,
        description="Held-back tokens requiring escalation approval to release."
    )
    burst_reserve_released: bool = Field(default=False)

    @property
    def effective_token_limit(self) -> int:
        """Available limit excluding burst reserve unless released."""
        if self.burst_reserve_released:
            return self.token_limit
        return self.token_limit - self.burst_reserve_tokens

    @property
    def tokens_remaining(self) -> int:
        return max(0, self.effective_token_limit - self.tokens_consumed)

    @property
    def utilization_percent(self) -> float:
        return round((self.tokens_consumed / self.token_limit) * 100, 2)

    def can_afford(self, tokens: int, cost_usd: float = 0.0) -> bool:
        if self.tokens_consumed + tokens > self.effective_token_limit:
            return False
        if self.cost_consumed_usd + cost_usd > self.cost_limit_usd:
            return False
        return True

    def decrement(self, tokens: int, cost_usd: float = 0.0) -> None:
        """
        Decrement the mission wallet.
        Caller must check can_afford() before calling decrement().
        """
        self.tokens_consumed += tokens
        self.cost_consumed_usd += cost_usd


# ---------------------------------------------------------------------------
# Core wallet model
# ---------------------------------------------------------------------------

class AllocatedWallet(BaseModel):
    """
    ARG Allocated Wallet v2.0.0

    A cryptographically bounded transaction passport issued to an autonomous
    agent execution tree before execution begins. Constrains both magnitude
    (how much) and intent (what for).

    Usage:
        wallet = AllocatedWallet.issue(
            execution_tree_id=str(uuid.uuid4()),
            token_limit=50_000,
            cost_limit_usd=2.50,
            ttl_seconds=300,
            dependency_graph_hash="<sha256>",
            appropriation_id="proj-2026-q2-research",
            intent_declaration="Retrieve and summarize competitor pricing pages",
            task_category=TaskCategory.RESEARCH,
            authorizer_id="user-sandy-p",
            authorizer_type=AuthorizerType.HUMAN,
        )

        result = wallet.request_spend(
            tokens=1500,
            cost_usd=0.045,
            operation_type=OperationType.WEB_SEARCH,
        )
        if result.approved:
            # make the call
    """

    wallet_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    execution_tree_id: str
    maximum_token_quantum: TokenQuantum
    purpose_classification: PurposeClassification
    ttl_seconds: int = Field(ge=1)
    dependency_graph_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: WalletStatus = Field(default=WalletStatus.ACTIVE)
    issued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime
    revoked_at: Optional[datetime] = None
    revocation_reason: Optional[RevocationReason] = None

    @model_validator(mode="after")
    def validate_expiry(self) -> AllocatedWallet:
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        return self

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def issue(
        cls,
        execution_tree_id: str,
        token_limit: int,
        cost_limit_usd: float,
        ttl_seconds: int,
        dependency_graph_hash: str,
        appropriation_id: str,
        intent_declaration: str,
        task_category: TaskCategory,
        authorizer_id: str,
        authorizer_type: AuthorizerType,
        burst_reserve_tokens: int = 0,
        operation_wallets: Optional[list[OperationWallet]] = None,
        authorization_scope: Optional[str] = None,
    ) -> AllocatedWallet:
        """
        Issue a new wallet. This is the only way to create a wallet.
        All fields are set at issuance time and the authorization chain
        is initialized with the originating authorization.
        """
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        expires_at = now + timedelta(seconds=ttl_seconds)

        return cls(
            execution_tree_id=execution_tree_id,
            maximum_token_quantum=TokenQuantum(
                token_limit=token_limit,
                cost_limit_usd=cost_limit_usd,
                burst_reserve_tokens=burst_reserve_tokens,
            ),
            purpose_classification=PurposeClassification(
                appropriation_id=appropriation_id,
                intent_declaration=intent_declaration,
                task_category=task_category,
                authorization_chain=[
                    AuthorizationEntry(
                        authorizer_id=authorizer_id,
                        authorizer_type=authorizer_type,
                        authorized_at=now,
                        authorization_ttl_seconds=ttl_seconds,
                        authorization_scope=authorization_scope,
                    )
                ],
                operation_wallets=operation_wallets or [],
            ),
            ttl_seconds=ttl_seconds,
            dependency_graph_hash=dependency_graph_hash,
            issued_at=now,
            expires_at=expires_at,
        )

    # ------------------------------------------------------------------
    # State checks
    # ------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        return (
            self.status == WalletStatus.ACTIVE
            and datetime.now(timezone.utc) < self.expires_at
        )

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at

    def _check_liveness(self) -> SpendResult:
        """Return a denial if the wallet is not usable."""
        if self.status == WalletStatus.REVOKED:
            return SpendResult(
                approved=False,
                denial_reason="WALLET_REVOKED",
                wallet_id=self.wallet_id,
            )
        if self.status == WalletStatus.EXHAUSTED:
            return SpendResult(
                approved=False,
                denial_reason="WALLET_EXHAUSTED",
                wallet_id=self.wallet_id,
            )
        if self.is_expired:
            self.status = WalletStatus.EXPIRED
            return SpendResult(
                approved=False,
                denial_reason="WALLET_EXPIRED",
                wallet_id=self.wallet_id,
            )
        if self.status == WalletStatus.BURST_RESERVE_PENDING:
            return SpendResult(
                approved=False,
                denial_reason="BURST_RESERVE_PENDING_APPROVAL",
                wallet_id=self.wallet_id,
            )
        return SpendResult(approved=True, wallet_id=self.wallet_id)

    # ------------------------------------------------------------------
    # Spend request
    # ------------------------------------------------------------------

    def request_spend(
        self,
        tokens: int,
        cost_usd: float = 0.0,
        operation_type: Optional[OperationType] = None,
    ) -> SpendResult:
        """
        Request budget for a forthcoming API call.

        Returns a SpendResult. If approved=True the caller may proceed.
        If approved=False the caller must not make the call.

        Checks in order:
          1. Wallet liveness (revoked / exhausted / expired)
          2. Mission-level budget (TokenQuantum)
          3. Operation-level budget if an operation_wallet is configured
             for this operation_type

        On approval, both the mission wallet and operation wallet are
        decremented atomically before returning.
        """
        liveness = self._check_liveness()
        if not liveness.approved:
            return liveness

        # Mission-level check
        if not self.maximum_token_quantum.can_afford(tokens, cost_usd):
            remaining = self.maximum_token_quantum.tokens_remaining
            burst_available = (
                self.maximum_token_quantum.burst_reserve_tokens > 0
                and not self.maximum_token_quantum.burst_reserve_released
            )
            if remaining == 0 and burst_available:
                # Halt execution and signal that burst reserve escalation is required.
                # The wallet stays in this state until release_burst_reserve() is called
                # or the TTL expires. All calls are blocked during this window.
                self.status = WalletStatus.BURST_RESERVE_PENDING
                return SpendResult(
                    approved=False,
                    denial_reason="MISSION_BUDGET_EXHAUSTED_BURST_RESERVE_AVAILABLE",
                    wallet_id=self.wallet_id,
                    tokens_remaining=0,
                    burst_reserve_available=self.maximum_token_quantum.burst_reserve_tokens,
                )
            self.status = WalletStatus.EXHAUSTED
            return SpendResult(
                approved=False,
                denial_reason="MISSION_BUDGET_EXHAUSTED",
                wallet_id=self.wallet_id,
                tokens_remaining=remaining,
            )

        # Operation-level check (if configured)
        op_wallet: Optional[OperationWallet] = None
        if operation_type:
            op_wallet = self.purpose_classification.get_operation_wallet(operation_type)
            if op_wallet is not None and not op_wallet.can_afford(tokens, cost_usd):
                return SpendResult(
                    approved=False,
                    denial_reason=f"OPERATION_BUDGET_EXHAUSTED:{operation_type.value}",
                    wallet_id=self.wallet_id,
                    tokens_remaining=op_wallet.tokens_remaining,
                    operation_type=operation_type,
                )

        # Both checks passed -- decrement atomically
        self.maximum_token_quantum.decrement(tokens, cost_usd)
        if op_wallet is not None:
            op_wallet.decrement(tokens, cost_usd)

        utilization = self.maximum_token_quantum.utilization_percent
        return SpendResult(
            approved=True,
            wallet_id=self.wallet_id,
            tokens_approved=tokens,
            cost_approved_usd=cost_usd,
            operation_type=operation_type,
            mission_utilization_percent=utilization,
            warn_threshold_crossed=utilization >= 75.0,
            critical_threshold_crossed=utilization >= 90.0,
        )

    # ------------------------------------------------------------------
    # Burst reserve escalation
    # ------------------------------------------------------------------

    def release_burst_reserve(
        self,
        approver_id: str,
        approver_type: AuthorizerType,
        scope: str,
    ) -> None:
        """
        Release the burst reserve after explicit escalation approval.
        Only valid when the wallet is in BURST_RESERVE_PENDING state.
        Adds an escalation entry to the authorization chain.
        """
        if self.status != WalletStatus.BURST_RESERVE_PENDING:
            raise ValueError(
                f"Cannot release burst reserve: wallet is in {self.status.value} state, "
                "expected BURST_RESERVE_PENDING"
            )
        self.maximum_token_quantum.burst_reserve_released = True
        self.status = WalletStatus.ACTIVE
        self.purpose_classification.authorization_chain.append(
            AuthorizationEntry(
                authorizer_id=approver_id,
                authorizer_type=AuthorizerType.ESCALATION_APPROVAL,
                authorization_scope=scope,
            )
        )

    # ------------------------------------------------------------------
    # Revocation
    # ------------------------------------------------------------------

    def revoke(self, reason: RevocationReason) -> None:
        self.status = WalletStatus.REVOKED
        self.revoked_at = datetime.now(timezone.utc)
        self.revocation_reason = reason

    # ------------------------------------------------------------------
    # Dependency graph verification
    # ------------------------------------------------------------------

    @staticmethod
    def compute_dependency_graph_hash(graph_definition: dict) -> str:
        """
        Compute the SHA-256 hash of a dependency graph definition.
        Use this at wallet issuance time to lock the authorized endpoint set.
        """
        import json
        canonical = json.dumps(graph_definition, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def verify_endpoint(self, endpoint_url: str, graph_definition: dict) -> bool:
        """
        Verify that a given endpoint is in the authorized dependency graph
        AND that the graph definition matches the hash locked at issuance.

        If the graph hash has been tampered with, this returns False.
        """
        computed_hash = self.compute_dependency_graph_hash(graph_definition)
        if computed_hash != self.dependency_graph_hash:
            return False
        allowed = graph_definition.get("allowed_endpoints", [])
        for entry in allowed:
            pattern = entry.get("url_pattern", "")
            if self._matches_pattern(endpoint_url, pattern):
                return True
        return False

    @staticmethod
    def _matches_pattern(url: str, pattern: str) -> bool:
        """Simple wildcard pattern matching for URL allow-list checks."""
        if pattern.endswith("*"):
            return url.startswith(pattern[:-1])
        return url == pattern


# ---------------------------------------------------------------------------
# Spend result
# ---------------------------------------------------------------------------

class SpendResult(BaseModel):
    """
    Result of a wallet.request_spend() call.

    If approved=True the caller may proceed with the API call.
    If approved=False the caller must not proceed.
    """
    approved: bool
    wallet_id: str
    denial_reason: Optional[str] = None
    tokens_approved: Optional[int] = None
    cost_approved_usd: Optional[float] = None
    operation_type: Optional[OperationType] = None
    tokens_remaining: Optional[int] = None
    burst_reserve_available: Optional[int] = None
    mission_utilization_percent: Optional[float] = None
    warn_threshold_crossed: bool = False
    critical_threshold_crossed: bool = False
