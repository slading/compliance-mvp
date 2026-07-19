import os

import pytest

from app.groq_client import GroqComplianceClient


SYNTHETIC_PURPOSE = "Invoice for software development services"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.integration
@pytest.mark.anyio
async def test_groq_returns_structured_analysis() -> None:
    if not os.getenv("GROQ_API_KEY"):
        pytest.skip("GROQ_API_KEY is required for the opt-in integration test")

    result = await GroqComplianceClient().analyze(SYNTHETIC_PURPOSE)

    assert result.analysis.suggested_action in {"FLAG", "REVIEW", "NONE"}
    assert 10 <= len(result.analysis.reason) <= 300
