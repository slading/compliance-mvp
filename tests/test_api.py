import json
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from app.main import app
from app.schemas import ComplianceResponse
from app.services import StaticPolicyService, get_decision_service


EVAL_CASES = json.loads(
    (Path(__file__).parent / "eval_cases.json").read_text(encoding="utf-8")
)
VALID_CASES = [case for case in EVAL_CASES if "expected" in case]
INVALID_CASE = next(case for case in EVAL_CASES if case.get("expected_status") == 422)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def use_static_decision_service() -> None:
    app.dependency_overrides[get_decision_service] = StaticPolicyService
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_decision_service, None)


@pytest.mark.anyio
@pytest.mark.parametrize("case", VALID_CASES, ids=lambda case: case["id"])
async def test_validate_success_cases(case: dict) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/api/v1/validate", json=case["payload"])

    assert response.status_code == 200

    result = ComplianceResponse.model_validate(response.json())
    assert result.decision == case["expected"]["decision"]
    assert (
        result.requires_human_review
        is case["expected"]["requires_human_review"]
    )
    assert result.decision_source == "STATIC_RULE"
    assert result.confidence is None
    assert UUID(str(result.request_id)) == result.request_id
    assert result.latency_ms >= 0
    assert result.estimated_cost_usd == Decimal("0")
    assert result.input_tokens is None
    assert result.output_tokens is None
    assert "llm_routing" not in response.json()


@pytest.mark.anyio
async def test_validate_rejects_invalid_input() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/validate",
            json=INVALID_CASE["payload"],
        )

    assert response.status_code == INVALID_CASE["expected_status"]
