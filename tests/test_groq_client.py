from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from groq import APITimeoutError, AuthenticationError, RateLimitError

from app.groq_client import (
    GroqAuthenticationError,
    GroqComplianceClient,
    GroqConfigurationError,
    GroqRateLimitError,
    GroqRefusalError,
    GroqSchemaValidationError,
    GroqTransportError,
    LLMAnalysis,
)


PURPOSE = "Invoice for software development services"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def completion(
    content: str | None,
    *,
    input_tokens: int | None = 12,
    output_tokens: int | None = 8,
    refusal: str | None = None,
) -> SimpleNamespace:
    usage = None
    if input_tokens is not None and output_tokens is not None:
        usage = SimpleNamespace(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
        )
    message = SimpleNamespace(content=content, refusal=refusal)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=usage,
    )


@pytest.mark.anyio
async def test_uses_strict_json_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    sdk = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(
                    return_value=completion(
                        '{"suggested_action":"NONE","reason":"No concerns found."}'
                    )
                )
            )
        )
    )

    with patch("app.groq_client.AsyncGroq", return_value=sdk) as sdk_class:
        await GroqComplianceClient().analyze(PURPOSE)

    sdk_class.assert_called_once_with(
        api_key="test-key",
        timeout=3.0,
        max_retries=1,
    )
    request = sdk.chat.completions.create.await_args.kwargs
    assert request["model"] == "openai/gpt-oss-20b"
    assert request["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "compliance_analysis",
            "strict": True,
            "schema": LLMAnalysis.model_json_schema(),
        },
    }
    assert request["response_format"]["json_schema"]["schema"][
        "additionalProperties"
    ] is False
    assert PURPOSE not in request["messages"][0]["content"]
    assert PURPOSE in request["messages"][1]["content"]


@pytest.mark.anyio
async def test_returns_validated_analysis_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    create = AsyncMock(
        return_value=completion(
            '{"suggested_action":"REVIEW","reason":"Manual review is required."}',
            input_tokens=17,
            output_tokens=9,
        )
    )
    sdk = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    with patch("app.groq_client.AsyncGroq", return_value=sdk):
        result = await GroqComplianceClient().analyze(PURPOSE)

    assert result.analysis.suggested_action == "REVIEW"
    assert result.analysis.reason == "Manual review is required."
    assert result.input_tokens == 17
    assert result.output_tokens == 9


@pytest.mark.anyio
async def test_rejects_missing_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    client = GroqComplianceClient()

    with pytest.raises(GroqConfigurationError):
        await client.analyze(PURPOSE)


@pytest.mark.anyio
async def test_maps_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    error = APITimeoutError(request=httpx.Request("POST", "https://test"))

    with patch("app.groq_client.AsyncGroq") as sdk_class:
        sdk_class.return_value.chat.completions.create = AsyncMock(
            side_effect=error
        )
        with pytest.raises(GroqTransportError):
            await GroqComplianceClient().analyze(PURPOSE)


@pytest.mark.anyio
async def test_maps_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "https://test"),
    )
    error = RateLimitError("rate limited", response=response, body=None)

    with patch("app.groq_client.AsyncGroq") as sdk_class:
        sdk_class.return_value.chat.completions.create = AsyncMock(
            side_effect=error
        )
        with pytest.raises(GroqRateLimitError):
            await GroqComplianceClient().analyze(PURPOSE)


@pytest.mark.anyio
async def test_maps_authentication(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    response = httpx.Response(
        401,
        request=httpx.Request("POST", "https://test"),
    )
    error = AuthenticationError("unauthorized", response=response, body=None)

    with patch("app.groq_client.AsyncGroq") as sdk_class:
        sdk_class.return_value.chat.completions.create = AsyncMock(
            side_effect=error
        )
        with pytest.raises(GroqAuthenticationError):
            await GroqComplianceClient().analyze(PURPOSE)


@pytest.mark.anyio
async def test_rejects_invalid_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    sdk = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(
                    return_value=completion(
                        '{"suggested_action":"APPROVE","reason":"Invalid action value."}'
                    )
                )
            )
        )
    )

    with patch("app.groq_client.AsyncGroq", return_value=sdk):
        with pytest.raises(GroqSchemaValidationError):
            await GroqComplianceClient().analyze(PURPOSE)


@pytest.mark.anyio
async def test_maps_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    sdk = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(
                    return_value=completion(
                        None,
                        refusal="Unable to comply with this request.",
                    )
                )
            )
        )
    )

    with patch("app.groq_client.AsyncGroq", return_value=sdk):
        with pytest.raises(GroqRefusalError):
            await GroqComplianceClient().analyze(PURPOSE)
