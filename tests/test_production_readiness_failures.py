import asyncio
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import patch

import httpx
import pytest

from app.groq_client import (
    GroqClientError,
    GroqRateLimitError,
    GroqSchemaValidationError,
    GroqTransportError,
    GroqUnexpectedProviderError,
    LLMAnalysis,
    LLMProviderResult,
)
from app.main import app
from app.services import (
    CompositeDecisionService,
    StaticPolicyService,
    get_decision_service,
)


BASE_PAYLOAD = {
    "amount": 100.00,
    "risk": 0.1,
    "source_country": "US",
    "target_country": "FI",
    "currency": "USD",
    "purpose": "Invoice for software development services",
}


class FailingAnalyzer:
    def __init__(self, error: GroqClientError) -> None:
        self._error = error

    async def analyze(self, purpose: str) -> LLMProviderResult:
        raise self._error


class IsolatedConcurrentAnalyzer:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.active_calls = 0
        self.max_active_calls = 0

    async def analyze(self, purpose: str) -> LLMProviderResult:
        request_number = int(purpose.rsplit(" ", maxsplit=1)[1])
        self.calls.append(purpose)
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            await asyncio.sleep(0.005 + (request_number % 5) * 0.001)
            return LLMProviderResult(
                analysis=LLMAnalysis(
                    suggested_action="NONE",
                    reason=(
                        f"Synthetic isolated analysis for request "
                        f"{request_number:02d}."
                    ),
                ),
                input_tokens=1_000 + request_number,
                output_tokens=2_000 + request_number,
            )
        finally:
            self.active_calls -= 1


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def forbid_real_groq_sdk() -> Iterator[None]:
    with patch(
        "app.groq_client.AsyncGroq",
        side_effect=AssertionError("AsyncGroq must not be created in offline tests"),
    ):
        yield


@contextmanager
def use_analyzer(analyzer: object) -> Iterator[None]:
    service = CompositeDecisionService(StaticPolicyService(), analyzer)
    app.dependency_overrides[get_decision_service] = lambda: service
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_decision_service, None)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure_mode", "error_type"),
    [
        pytest.param("timeout", GroqTransportError, id="provider-timeout"),
        pytest.param("rate-limit", GroqRateLimitError, id="http-429"),
        pytest.param(
            "invalid-output",
            GroqSchemaValidationError,
            id="invalid-structured-output",
        ),
        pytest.param(
            "connection-error",
            GroqTransportError,
            id="connection-error",
        ),
        pytest.param(
            "api-error",
            GroqUnexpectedProviderError,
            id="api-error",
        ),
    ],
)
async def test_known_provider_failure_returns_safe_api_fallback(
    failure_mode: str,
    error_type: type[GroqClientError],
) -> None:
    private_detail = f"private {failure_mode} provider detail"
    analyzer = FailingAnalyzer(error_type(private_detail))

    with use_analyzer(analyzer):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post("/api/v1/validate", json=BASE_PAYLOAD)

    body = response.json()
    assert response.status_code == 200
    assert response.status_code != 500
    assert body["decision"] == "FLAGGED"
    assert body["requires_human_review"] is True
    assert body["reason"] == (
        "LLM analysis is unavailable; human review is required."
    )
    assert body["policy_references"] == ["BASE-001", "LLM-UNAVAILABLE-001"]
    assert body["input_tokens"] is None
    assert body["output_tokens"] is None
    assert body["estimated_cost_usd"] is None
    assert private_detail not in response.text
    assert error_type.__name__ not in response.text
    assert "llm_routing" not in body


@pytest.mark.anyio
async def test_concurrent_requests_keep_request_ids_and_state_isolated() -> None:
    analyzer = IsolatedConcurrentAnalyzer()
    request_count = 20
    purposes = [f"Synthetic concurrent purpose {index}" for index in range(request_count)]

    with use_analyzer(analyzer):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/api/v1/validate",
                        json={**BASE_PAYLOAD, "purpose": purpose},
                    )
                    for purpose in purposes
                )
            )

    bodies = [response.json() for response in responses]
    assert all(response.status_code == 200 for response in responses)
    assert analyzer.max_active_calls > 1
    assert sorted(analyzer.calls) == sorted(purposes)
    assert len({body["request_id"] for body in bodies}) == request_count

    for index, body in enumerate(bodies):
        expected_reason = f"Synthetic isolated analysis for request {index:02d}."
        assert body["decision"] == "APPROVED"
        assert body["requires_human_review"] is False
        assert body["decision_source"] == "LLM_ASSISTED"
        assert expected_reason in body["reason"]
        assert body["input_tokens"] == 1_000 + index
        assert body["output_tokens"] == 2_000 + index
        assert body["estimated_cost_usd"] is None
        assert "llm_routing" not in body
