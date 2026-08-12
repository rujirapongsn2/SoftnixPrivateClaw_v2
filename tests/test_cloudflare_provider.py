"""Cloudflare AI dispatch in LiteLLMProvider: request shape, gateway-id query
param, tool-call mapping, and error-envelope handling. Uses a fake
httpx.AsyncClient (no live Cloudflare API) — mirrors the MockTransport pattern
used by test_hubspot_mcp_server.py, since httpx.AsyncClient itself is
monkeypatched rather than a single call."""

import httpx
import pytest

from claw.providers.base import ChatResult, ProviderError, TextDelta
from claw.providers.litellm_provider import LiteLLMProvider, _parse_cloudflare_base


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, captured: dict, response: _FakeResponse):
        self._captured = captured
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self._captured["url"] = url
        self._captured["headers"] = headers
        self._captured["json"] = json
        return self._response


def _patch_client(monkeypatch, response: _FakeResponse) -> dict:
    captured: dict = {}
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient(captured, response))
    return captured


async def _collect(provider: LiteLLMProvider, **kwargs) -> list:
    return [event async for event in provider.stream_chat(**kwargs)]


def test_parse_cloudflare_base_defaults_gateway_to_default():
    url, gateway_id = _parse_cloudflare_base(
        "https://api.cloudflare.com/client/v4/accounts/acct123/ai/run"
    )
    assert url == "https://api.cloudflare.com/client/v4/accounts/acct123/ai/run"
    assert gateway_id == "default"


def test_parse_cloudflare_base_reads_gateway_query_param():
    url, gateway_id = _parse_cloudflare_base(
        "https://api.cloudflare.com/client/v4/accounts/acct123/ai/run?gateway=my-gw"
    )
    assert url == "https://api.cloudflare.com/client/v4/accounts/acct123/ai/run"
    assert gateway_id == "my-gw"


async def test_non_streaming_request_shape_and_gateway_header(monkeypatch):
    payload = {
        "result": {"response": "pong", "tool_calls": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        "success": True,
        "errors": [],
        "messages": [],
    }
    provider = LiteLLMProvider()
    captured = _patch_client(monkeypatch, _FakeResponse(200, payload))

    events = await _collect(
        provider,
        messages=[{"role": "user", "content": "hi"}],
        model="cloudflare/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        api_key="cf-token",
        api_base="https://api.cloudflare.com/client/v4/accounts/acct123/ai/run",
    )

    assert captured["url"] == "https://api.cloudflare.com/client/v4/accounts/acct123/ai/run"
    assert captured["headers"]["Authorization"] == "Bearer cf-token"
    assert captured["headers"]["cf-aig-gateway-id"] == "default"
    assert captured["json"]["model"] == "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
    assert captured["json"]["input"]["messages"][0]["content"] == "hi"
    assert captured["json"]["input"]["temperature"] == 0.1
    assert any(isinstance(e, TextDelta) and e.text == "pong" for e in events)
    result = next(e for e in events if isinstance(e, ChatResult))
    assert result.content == "pong"
    assert result.usage == {"prompt_tokens": 1, "completion_tokens": 1}


async def test_gateway_id_sent_from_base_url_query_param(monkeypatch):
    payload = {"result": {"response": "ok"}, "success": True, "errors": [], "messages": []}
    provider = LiteLLMProvider()
    captured = _patch_client(monkeypatch, _FakeResponse(200, payload))

    await _collect(
        provider,
        messages=[{"role": "user", "content": "hi"}],
        model="cloudflare/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        api_key="tok",
        api_base="https://api.cloudflare.com/client/v4/accounts/acct123/ai/run?gateway=my-gw",
    )

    assert captured["headers"]["cf-aig-gateway-id"] == "my-gw"
    assert captured["url"] == "https://api.cloudflare.com/client/v4/accounts/acct123/ai/run"


async def test_tool_call_response_maps_to_tool_call_with_no_content(monkeypatch):
    payload = {
        "result": {
            "response": None,
            "tool_calls": [{"name": "get_weather", "arguments": {"city": "Bangkok"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        },
        "success": True,
        "errors": [],
        "messages": [],
    }
    provider = LiteLLMProvider()
    _patch_client(monkeypatch, _FakeResponse(200, payload))

    events = await _collect(
        provider,
        messages=[{"role": "user", "content": "weather?"}],
        tools=[{"type": "function", "function": {"name": "get_weather", "parameters": {}}}],
        model="cloudflare/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        api_key="tok",
        api_base="https://api.cloudflare.com/client/v4/accounts/acct123/ai/run",
    )

    assert not any(isinstance(e, TextDelta) for e in events)
    result = next(e for e in events if isinstance(e, ChatResult))
    assert result.content is None
    assert result.finish_reason == "tool_calls"
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.name == "get_weather"
    assert tc.arguments == {"city": "Bangkok"}
    assert tc.id  # a local id is fabricated since Cloudflare doesn't return one


async def test_error_envelope_raises_provider_error_with_cloudflare_message(monkeypatch):
    payload = {
        "errors": [{"message": "Insufficient balance; add money to your gateway or use BYOK", "code": 2021}],
        "success": False,
        "result": {},
        "messages": [],
    }
    provider = LiteLLMProvider()
    _patch_client(monkeypatch, _FakeResponse(402, payload))

    with pytest.raises(ProviderError, match="Insufficient balance"):
        await _collect(
            provider,
            messages=[{"role": "user", "content": "hi"}],
            model="cloudflare/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
            api_key="tok",
            api_base="https://api.cloudflare.com/client/v4/accounts/acct123/ai/run",
        )


async def test_missing_base_url_raises_clear_error():
    provider = LiteLLMProvider()
    with pytest.raises(ProviderError, match="base URL"):
        await _collect(
            provider,
            messages=[{"role": "user", "content": "hi"}],
            model="cloudflare/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
            api_key="tok",
            api_base=None,
        )


async def test_missing_api_key_raises_clear_error_instead_of_placeholder(monkeypatch):
    provider = LiteLLMProvider()
    captured = _patch_client(monkeypatch, _FakeResponse(200, {"result": {}, "success": True, "errors": [], "messages": []}))

    with pytest.raises(ProviderError, match="API key"):
        await _collect(
            provider,
            messages=[{"role": "user", "content": "hi"}],
            model="cloudflare/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
            api_key=None,
            api_base="https://api.cloudflare.com/client/v4/accounts/acct123/ai/run",
        )

    assert captured == {}  # never sent "sk-local" to the real API


async def test_custom_temperature_forwarded_to_request_body(monkeypatch):
    payload = {"result": {"response": "ok"}, "success": True, "errors": [], "messages": []}
    provider = LiteLLMProvider()
    captured = _patch_client(monkeypatch, _FakeResponse(200, payload))

    await _collect(
        provider,
        messages=[{"role": "user", "content": "hi"}],
        model="cloudflare/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        temperature=0.0,
        api_key="tok",
        api_base="https://api.cloudflare.com/client/v4/accounts/acct123/ai/run",
    )

    assert captured["json"]["input"]["temperature"] == 0.0


async def test_env_default_credentials_used_when_no_per_call_key(monkeypatch):
    payload = {"result": {"response": "ok"}, "success": True, "errors": [], "messages": []}
    provider = LiteLLMProvider(
        api_key="env-token",
        api_base="https://api.cloudflare.com/client/v4/accounts/acct123/ai/run",
        default_model="cloudflare/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    )
    captured = _patch_client(monkeypatch, _FakeResponse(200, payload))

    events = await _collect(
        provider,
        messages=[{"role": "user", "content": "hi"}],
        model=None,
        api_key=None,
        api_base=None,
    )

    assert captured["headers"]["Authorization"] == "Bearer env-token"
    assert any(isinstance(e, ChatResult) for e in events)


async def test_image_generation_rejected_for_cloudflare_models():
    provider = LiteLLMProvider()
    with pytest.raises(ProviderError, match="image generation is not supported"):
        await provider.generate_image(
            prompt="a cat",
            model="cloudflare/@cf/black-forest-labs/flux-1-schnell",
            api_key="tok",
            api_base="https://api.cloudflare.com/client/v4/accounts/acct123/ai/run",
        )
