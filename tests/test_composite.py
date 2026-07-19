from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.groq_client import (
    GroqAuthenticationError,
    GroqComplianceClient,
    GroqConfigurationError,
    GroqRateLimitError,
    GroqRefusalError,
    GroqSchemaValidationError,
    GroqTransportError,
    GroqUnexpectedProviderError,
    LLMAnalysis,
    LLMProviderResult,
)
from app.main import app
from app.schemas import ComplianceRequest
from app.services import (
    CompositeDecisionService,
    StaticPolicyService,
    get_decision_service,
)


PURPOSE = "Invoice for software development services"


def transaction(*, risk: float, target_country: str = "FI") -> ComplianceRequest:
    return ComplianceRequest(
        amount=Decimal("100.00"),
        risk=risk,
        source_country="US",
        target_country=target_country,
        currency="USD",
        purpose=PURPOSE,
    )


def provider_result(
    action: str,
    *,
    input_tokens: int | None = 13,
    output_tokens: int | None = 7,
) -> LLMProviderResult:
    return LLMProviderResult(
        analysis=LLMAnalysis(
            suggested_action=action,
            reason="Synthetic provider analysis result.",
        ),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def composite_with_result(action: str) -> tuple[CompositeDecisionService, AsyncMock]:
    analyze = AsyncMock(return_value=provider_result(action))
    analyzer = type("FakeAnalyzer", (), {"analyze": analyze})()
    return CompositeDecisionService(StaticPolicyService(), analyzer), analyze


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_country_skip_does_not_call_analyzer() -> None:
    service, analyze = composite_with_result("NONE")

    result = await service.decide(transaction(risk=0.5, target_country="XX"))

    assert result.policy_references == ["COUNTRY-001"]
    assert result.decision == "FLAGGED"
    analyze.assert_not_awaited()


@pytest.mark.anyio
async def test_high_risk_skip_does_not_call_analyzer() -> None:
    service, analyze = composite_with_result("NONE")

    result = await service.decide(transaction(risk=0.8))

    assert result.policy_references == ["RISK-002"]
    assert result.decision == "FLAGGED"
    analyze.assert_not_awaited()


@pytest.mark.anyio
async def test_static_review_remains_flagged_for_none() -> None:
    service, _ = composite_with_result("NONE")

    result = await service.decide(transaction(risk=0.5))

    assert result.decision == "FLAGGED"
    assert result.requires_human_review is True


@pytest.mark.anyio
async def test_static_approved_with_none_remains_approved() -> None:
    service, _ = composite_with_result("NONE")

    result = await service.decide(transaction(risk=0.1))

    assert result.decision == "APPROVED"
    assert result.requires_human_review is False


@pytest.mark.anyio
async def test_static_approved_with_flag_is_flagged_for_review() -> None:
    service, _ = composite_with_result("FLAG")

    result = await service.decide(transaction(risk=0.1))

    assert result.decision == "FLAGGED"
    assert result.requires_human_review is True


@pytest.mark.anyio
async def test_static_approved_with_review_is_flagged_for_review() -> None:
    service, _ = composite_with_result("REVIEW")

    result = await service.decide(transaction(risk=0.1))

    assert result.decision == "FLAGGED"
    assert result.requires_human_review is True


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error_type",
    [
        GroqConfigurationError,
        GroqTransportError,
        GroqRateLimitError,
        GroqAuthenticationError,
        GroqRefusalError,
        GroqSchemaValidationError,
        GroqUnexpectedProviderError,
    ],
)
async def test_each_groq_error_produces_safe_fallback(
    error_type: type[Exception],
) -> None:
    analyze = AsyncMock(side_effect=error_type("internal detail"))
    analyzer = type("FailingAnalyzer", (), {"analyze": analyze})()
    service = CompositeDecisionService(StaticPolicyService(), analyzer)

    result = await service.decide(transaction(risk=0.1))

    assert result.decision == "FLAGGED"
    assert result.requires_human_review is True
    assert result.policy_references == ["BASE-001", "LLM-UNAVAILABLE-001"]
    assert result.reason == "LLM analysis is unavailable; human review is required."
    assert result.input_tokens is None
    assert result.output_tokens is None


@pytest.mark.anyio
async def test_public_response_hides_provider_error_details() -> None:
    analyze = AsyncMock(
        side_effect=GroqTransportError("sensitive upstream timeout detail")
    )
    analyzer = type("FailingAnalyzer", (), {"analyze": analyze})()
    service = CompositeDecisionService(StaticPolicyService(), analyzer)
    app.dependency_overrides[get_decision_service] = lambda: service

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/validate",
                json=transaction(risk=0.1).model_dump(mode="json"),
            )
    finally:
        app.dependency_overrides.pop(get_decision_service, None)

    body = response.json()
    assert response.status_code == 200
    assert body["reason"] == "LLM analysis is unavailable; human review is required."
    assert body["policy_references"] == ["BASE-001", "LLM-UNAVAILABLE-001"]
    assert body["input_tokens"] is None
    assert body["output_tokens"] is None
    assert body["estimated_cost_usd"] is None
    assert "sensitive" not in response.text
    assert "GroqTransportError" not in response.text


@pytest.mark.anyio
async def test_skip_without_api_key_does_not_create_sdk_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with patch("app.groq_client.AsyncGroq") as sdk_class:
        service = CompositeDecisionService(
            StaticPolicyService(),
            GroqComplianceClient(),
        )
        result = await service.decide(
            transaction(risk=0.5, target_country="XX")
        )

    assert result.decision == "FLAGGED"
    sdk_class.assert_not_called()


@pytest.mark.anyio
async def test_llm_success_copies_token_usage() -> None:
    analyze = AsyncMock(
        return_value=provider_result(
            "NONE",
            input_tokens=21,
            output_tokens=11,
        )
    )
    analyzer = type("FakeAnalyzer", (), {"analyze": analyze})()
    service = CompositeDecisionService(StaticPolicyService(), analyzer)

    result = await service.decide(transaction(risk=0.1))

    assert result.decision_source == "LLM_ASSISTED"
    assert result.input_tokens == 21
    assert result.output_tokens == 11
    assert result.confidence is None


@pytest.mark.anyio
async def test_llm_success_exposes_tokens_but_not_cost_or_routing() -> None:
    analyze = AsyncMock(
        return_value=provider_result(
            "NONE",
            input_tokens=21,
            output_tokens=11,
        )
    )
    analyzer = type("FakeAnalyzer", (), {"analyze": analyze})()
    service = CompositeDecisionService(StaticPolicyService(), analyzer)
    app.dependency_overrides[get_decision_service] = lambda: service

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/validate",
                json=transaction(risk=0.1).model_dump(mode="json"),
            )
    finally:
        app.dependency_overrides.pop(get_decision_service, None)

    body = response.json()
    assert response.status_code == 200
    assert body["decision_source"] == "LLM_ASSISTED"
    assert body["input_tokens"] == 21
    assert body["output_tokens"] == 11
    assert body["estimated_cost_usd"] is None
    assert "llm_routing" not in body
