from typing import Literal, Protocol

from fastapi import Depends
from pydantic import BaseModel

from app.groq_client import (
    GroqClientError,
    GroqComplianceClient,
    LLMProviderResult,
)
from app.schemas import ComplianceRequest, Decision, DecisionSource


DecisionRouting = Literal["SKIP", "ASSIST"]
LLM_UNAVAILABLE_POLICY = "LLM-UNAVAILABLE-001"
LLM_UNAVAILABLE_REASON = (
    "LLM analysis is unavailable; human review is required."
)


class PolicyDecision(BaseModel):
    """Internal result produced by the complete decision pipeline."""

    decision: Decision
    reason: str
    policy_references: list[str]
    decision_source: DecisionSource
    confidence: float | None
    requires_human_review: bool
    llm_routing: DecisionRouting
    input_tokens: int | None = None
    output_tokens: int | None = None


class DecisionService(Protocol):
    async def decide(self, transaction: ComplianceRequest) -> PolicyDecision:
        """Evaluate a transaction without adding API transport metadata."""
        ...


class LLMAnalyzer(Protocol):
    async def analyze(self, purpose: str) -> LLMProviderResult:
        """Return structured provider analysis for untrusted purpose data."""
        ...


class StaticPolicyService:
    """Deterministic, ordered policy evaluation for the MVP."""

    async def decide(self, transaction: ComplianceRequest) -> PolicyDecision:
        if transaction.target_country == "XX":
            return PolicyDecision(
                decision="FLAGGED",
                reason="Target country XX is prohibited by policy.",
                policy_references=["COUNTRY-001"],
                decision_source="STATIC_RULE",
                confidence=None,
                requires_human_review=False,
                llm_routing="SKIP",
            )

        if transaction.risk >= 0.8:
            return PolicyDecision(
                decision="FLAGGED",
                reason="Risk score meets or exceeds the high-risk threshold.",
                policy_references=["RISK-002"],
                decision_source="STATIC_RULE",
                confidence=None,
                requires_human_review=False,
                llm_routing="SKIP",
            )

        if transaction.risk >= 0.5:
            return PolicyDecision(
                decision="FLAGGED",
                reason="Risk score requires human review.",
                policy_references=["RISK-REVIEW-001"],
                decision_source="STATIC_RULE",
                confidence=None,
                requires_human_review=True,
                llm_routing="ASSIST",
            )

        return PolicyDecision(
            decision="APPROVED",
            reason="Transaction satisfies the baseline static policy.",
            policy_references=["BASE-001"],
            decision_source="STATIC_RULE",
            confidence=None,
            requires_human_review=False,
            llm_routing="ASSIST",
        )


class CompositeDecisionService:
    """Apply static policy first, then safely merge optional LLM analysis."""

    def __init__(
        self,
        static_service: StaticPolicyService,
        llm_analyzer: LLMAnalyzer,
    ) -> None:
        self._static_service = static_service
        self._llm_analyzer = llm_analyzer

    async def decide(self, transaction: ComplianceRequest) -> PolicyDecision:
        static_result = await self._static_service.decide(transaction)
        if static_result.llm_routing == "SKIP":
            return static_result

        try:
            provider_result = await self._llm_analyzer.analyze(
                transaction.purpose
            )
        except GroqClientError:
            return static_result.model_copy(
                update={
                    "decision": "FLAGGED",
                    "reason": LLM_UNAVAILABLE_REASON,
                    "policy_references": [
                        *static_result.policy_references,
                        LLM_UNAVAILABLE_POLICY,
                    ],
                    "decision_source": "STATIC_RULE",
                    "confidence": None,
                    "requires_human_review": True,
                    "input_tokens": None,
                    "output_tokens": None,
                }
            )

        suggested_action = provider_result.analysis.suggested_action
        if static_result.decision == "FLAGGED":
            decision = "FLAGGED"
            requires_human_review = True
        elif suggested_action == "NONE":
            decision = "APPROVED"
            requires_human_review = False
        else:
            decision = "FLAGGED"
            requires_human_review = True

        return static_result.model_copy(
            update={
                "decision": decision,
                "reason": (
                    f"{static_result.reason} "
                    f"LLM analysis: {provider_result.analysis.reason}"
                ),
                "decision_source": "LLM_ASSISTED",
                "confidence": None,
                "requires_human_review": requires_human_review,
                "input_tokens": provider_result.input_tokens,
                "output_tokens": provider_result.output_tokens,
            }
        )


def get_groq_client() -> GroqComplianceClient:
    return GroqComplianceClient()


def get_decision_service(
    groq_client: GroqComplianceClient = Depends(get_groq_client),
) -> DecisionService:
    return CompositeDecisionService(StaticPolicyService(), groq_client)
