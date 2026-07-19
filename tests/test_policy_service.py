from decimal import Decimal

import pytest

from app.schemas import ComplianceRequest
from app.services import StaticPolicyService


def transaction(*, risk: float, target_country: str = "FI") -> ComplianceRequest:
    return ComplianceRequest(
        amount=Decimal("100.00"),
        risk=risk,
        source_country="US",
        target_country=target_country,
        currency="USD",
        purpose="Invoice for software development services",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize(
    (
        "risk",
        "decision",
        "requires_human_review",
        "policy_reference",
        "llm_routing",
    ),
    [
        (0.4999, "APPROVED", False, "BASE-001", "ASSIST"),
        (0.5, "FLAGGED", True, "RISK-REVIEW-001", "ASSIST"),
        (0.7999, "FLAGGED", True, "RISK-REVIEW-001", "ASSIST"),
        (0.8, "FLAGGED", False, "RISK-002", "SKIP"),
    ],
)
async def test_risk_boundaries(
    risk: float,
    decision: str,
    requires_human_review: bool,
    policy_reference: str,
    llm_routing: str,
) -> None:
    result = await StaticPolicyService().decide(transaction(risk=risk))

    assert result.decision == decision
    assert result.requires_human_review is requires_human_review
    assert result.policy_references == [policy_reference]
    assert result.decision_source == "STATIC_RULE"
    assert result.confidence is None
    assert result.llm_routing == llm_routing


@pytest.mark.anyio
async def test_country_rule_has_priority_over_review_risk() -> None:
    result = await StaticPolicyService().decide(
        transaction(risk=0.5, target_country="XX")
    )

    assert result.decision == "FLAGGED"
    assert result.requires_human_review is False
    assert result.policy_references == ["COUNTRY-001"]
    assert result.decision_source == "STATIC_RULE"
    assert result.confidence is None
    assert result.llm_routing == "SKIP"
    assert "prohibited" in result.reason.lower()
