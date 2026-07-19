import httpx
import pytest

from app.main import app
from app.services import StaticPolicyService, get_decision_service


VALID_PAYLOAD = {
    "amount": 100.00,
    "risk": 0.1,
    "source_country": "US",
    "target_country": "FI",
    "currency": "USD",
    "purpose": "Invoice for software development services",
}


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
@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("amount", 0),
        ("amount", -100.00),
        ("amount", 100.555),
        ("target_country", "USA"),
        ("target_country", "X"),
        ("currency", "US"),
        ("currency", "USDT"),
        ("risk", -0.01),
        ("risk", 1.01),
        ("purpose", "1234"),
        ("purpose", "x" * 501),
    ],
)
async def test_validate_rejects_each_invalid_field(
    field: str,
    invalid_value: object,
) -> None:
    payload = {**VALID_PAYLOAD, field: invalid_value}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/api/v1/validate", json=payload)

    assert response.status_code == 422
