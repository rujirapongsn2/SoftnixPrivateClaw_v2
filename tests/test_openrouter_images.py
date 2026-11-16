"""OpenRouter Image API (POST /images): capability from the /images/models
catalog, size mapping onto the catalog's parameters, the transport, and the
/images route's error mapping and quota release. Every HTTP call is mocked."""

import base64
import json
from types import SimpleNamespace

import httpx
import litellm  # noqa: F401  (openai subclasses httpx.AsyncClient at import; load it before the mock patches that name)
import pytest

import claw.providers.image_capability as capabilities
import claw.providers.litellm_provider as provider_module
from claw.api.llm_shared import ModelBody, create_model
from claw.db.stores import LLMConfigStore
from claw.providers.base import ProviderError
from claw.providers.image_capability import (
    ImageOptionError,
    image_capability,
    openrouter_image_options,
    resolve_capability,
)
from claw.providers.litellm_provider import LiteLLMProvider
from tests.conftest_app import build_api_app, client

SLUG = "google/gemini-3.1-flash-lite-image"
STORED = "openrouter/" + SLUG
BASE = "https://openrouter.ai/api/v1"
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
ASPECTS = [
    "1:1",
    "1:4",
    "1:8",
    "2:3",
    "3:2",
    "3:4",
    "4:1",
    "4:3",
    "4:5",
    "5:4",
    "8:1",
    "9:16",
    "16:9",
    "21:9",
]
PARAMS = {
    "resolution": {"type": "enum", "values": ["1K"]},
    "aspect_ratio": {"type": "enum", "values": ASPECTS},
    "n": {"type": "range", "min": 1, "max": 1},
    "input_references": {"type": "range", "min": 0, "max": 14},
}
# Chat API metadata as OpenRouter publishes it for this model: image output,
# but no "modalities" in supported_parameters.
CHAT_ENTRY = {
    "architecture": {"input_modalities": ["image", "text"], "output_modalities": ["image", "text"]},
    "supported_parameters": ["max_tokens", "temperature"],
}
IMAGE_ENTRY = {
    "id": SLUG,
    "architecture": {"input_modalities": ["image", "text"], "output_modalities": ["image", "text"]},
    "supported_parameters": PARAMS,
    "supports_streaming": False,
}


def _resolve(models=None, images=None, model=STORED, base=BASE):
    return resolve_capability("openrouter", base, model, {}, models, images)


# ---- capability ----


@pytest.mark.parametrize("model", [STORED, SLUG])
def test_image_api_listing_verifies_without_chat_modalities(model):
    cap = _resolve({SLUG: CHAT_ENTRY}, {SLUG: IMAGE_ENTRY}, model=model)
    assert (cap.status, cap.source, cap.reason) == ("verified", "openrouter_images", "images_api")
    assert cap.generation_mode == "openrouter_images"
    assert cap.image_parameters == PARAMS
    assert cap.request_modalities == ()
    assert cap.routed_model == STORED


def test_same_model_without_image_catalog_listing_stays_unconfirmed():
    cap = _resolve({SLUG: CHAT_ENTRY}, {})
    assert (cap.status, cap.reason) == ("unknown", "chat_transport_unconfirmed")


def test_chat_verified_model_keeps_the_chat_transport():
    chat = {**CHAT_ENTRY, "supported_parameters": ["modalities"]}
    cap = _resolve({SLUG: chat}, {SLUG: IMAGE_ENTRY})
    assert (cap.status, cap.generation_mode, cap.reason) == ("verified", "chat", "chat_image_output")


def test_vision_only_model_is_not_an_image_generator():
    vision = {
        "architecture": {"input_modalities": ["image", "text"], "output_modalities": ["text"]},
        "supported_parameters": ["max_tokens"],
    }
    assert _resolve({SLUG: vision}, {}).reason == "vision_only"
    assert _resolve({SLUG: vision}, None).reason == "vision_only"
    listed_text_only = {**IMAGE_ENTRY, "architecture": {"output_modalities": ["text"]}}
    cap = _resolve({SLUG: vision}, {SLUG: listed_text_only})
    assert (cap.status, cap.reason) == ("unsupported", "no_image_output")


@pytest.mark.parametrize(
    "models, reason",
    [
        ({SLUG: CHAT_ENTRY}, "images_metadata_unavailable"),
        ({}, "images_metadata_unavailable"),
        (None, "metadata_unavailable"),
    ],
)
def test_image_catalog_unavailable_is_unknown_with_reason(models, reason):
    cap = _resolve(models, None)
    assert (cap.status, cap.reason) == ("unknown", reason)


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"architecture": None, "supported_parameters": PARAMS},
        {"architecture": {"output_modalities": "image"}, "supported_parameters": PARAMS},
        {"architecture": {"output_modalities": ["image"]}, "supported_parameters": ["n"]},
        {"architecture": {"output_modalities": ["image"]}},
    ],
)
def test_malformed_image_catalog_entry_is_unknown(entry):
    cap = _resolve({SLUG: CHAT_ENTRY}, {SLUG: entry})
    assert (cap.status, cap.reason) == ("unknown", "images_metadata_invalid")


@pytest.mark.parametrize("base", ["https://gateway.invalid/v1", "https://openrouter.ai/api/v2"])
def test_custom_endpoint_never_uses_image_catalog(base):
    cap = _resolve({SLUG: CHAT_ENTRY}, {SLUG: IMAGE_ENTRY}, base=base)
    assert (cap.status, cap.reason) == ("unknown", "custom_endpoint")


async def test_image_capability_reads_both_catalogs(monkeypatch):
    calls = []

    async def models():
        calls.append("models")
        return {SLUG: CHAT_ENTRY}

    async def images():
        calls.append("images")
        return {SLUG: IMAGE_ENTRY}

    monkeypatch.setattr(capabilities, "openrouter_catalog", models)
    monkeypatch.setattr(capabilities, "openrouter_image_catalog", images)
    cap = await image_capability("openrouter", BASE, STORED)
    assert cap.status == "verified" and cap.generation_mode == "openrouter_images"
    assert sorted(calls) == ["images", "models"]


@pytest.mark.parametrize("owner", [None, "u1"])
async def test_model_can_be_enabled_as_image(db_factory, monkeypatch, owner):
    async def models():
        return {SLUG: CHAT_ENTRY}

    async def images():
        return {SLUG: IMAGE_ENTRY}

    monkeypatch.setattr(capabilities, "openrouter_catalog", models)
    monkeypatch.setattr(capabilities, "openrouter_image_catalog", images)
    store = LLMConfigStore(db_factory)
    provider = await store.create_provider("or", "sk-or", BASE, model_prefix="openrouter", owner_id=owner)
    row = await create_model(
        SimpleNamespace(llm_config=store), provider.id, ModelBody(model_id=STORED, kind="image"), owner
    )
    assert row["enabled"] and row["kind"] == "image"


# ---- size -> Image API parameters ----


@pytest.mark.parametrize(
    "size, expected",
    [
        (None, {"n": 1}),
        ("", {"n": 1}),
        ("1024x1024", {"n": 1, "aspect_ratio": "1:1", "resolution": "1K"}),
        ("1024x576", {"n": 1, "aspect_ratio": "16:9", "resolution": "1K"}),
        ("768x1024", {"n": 1, "aspect_ratio": "3:4", "resolution": "1K"}),
    ],
)
def test_size_maps_only_to_declared_parameters(size, expected):
    assert openrouter_image_options(PARAMS, size) == expected


@pytest.mark.parametrize(
    "size, message",
    [
        ("1792x1024", "aspect ratio 7:4 is not supported"),
        ("2048x2048", "resolution for 2048x2048 is not supported (supported: 1K)"),
        ("512x512", "resolution for 512x512 is not supported (supported: 1K)"),
        ("big", "unrecognized size"),
        ("0x10", "unrecognized size"),
    ],
)
def test_unsupported_size_is_refused_with_choices(size, message):
    with pytest.raises(ImageOptionError, match=message.replace("(", r"\(").replace(")", r"\)")):
        openrouter_image_options(PARAMS, size)


def test_model_without_size_parameters():
    assert openrouter_image_options({}, None) == {}
    with pytest.raises(ImageOptionError, match="does not accept a size"):
        openrouter_image_options({}, "1024x1024")
    assert openrouter_image_options({"size": {"type": "enum", "values": ["1024x1024"]}}, "1024x1024") == {
        "size": "1024x1024"
    }
    square_only = {"resolution": {"type": "enum", "values": ["1K"]}}
    assert openrouter_image_options(square_only, "1024x1024") == {"resolution": "1K"}
    with pytest.raises(ImageOptionError, match="16:9"):
        openrouter_image_options(square_only, "1024x576")


# ---- transport ----


def _mock_http(monkeypatch, respond):
    requests = []
    original = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        return respond(request)

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    return requests


def _ok(*items):
    return lambda request: httpx.Response(
        200, json={"created": 1, "data": list(items), "usage": {"cost": 0.04}}
    )


async def _generate(**overrides):
    kwargs = {
        "api_key": "sk-or-saved",
        "api_base": BASE,
        "mode": "openrouter_images",
        "explicit_credentials": True,
        "image_options": {"n": 1, "aspect_ratio": "1:1", "resolution": "1K"},
        "timeout": 30,
    } | overrides
    return await LiteLLMProvider(api_key="operator-key").generate_image("a tree", STORED, **kwargs)


async def test_request_url_slug_auth_and_body(monkeypatch):
    requests = _mock_http(
        monkeypatch, _ok({"b64_json": base64.b64encode(_PNG).decode(), "media_type": "image/png"})
    )
    assert await _generate() == [(_PNG, "png")]
    (request,) = requests
    assert request.method == "POST" and str(request.url) == BASE + "/images"
    assert request.headers["authorization"] == "Bearer sk-or-saved"
    assert json.loads(request.content) == {
        "model": SLUG,
        "prompt": "a tree",
        "n": 1,
        "aspect_ratio": "1:1",
        "resolution": "1K",
    }


async def test_options_cannot_override_model_or_prompt(monkeypatch):
    requests = _mock_http(monkeypatch, _ok({"b64_json": base64.b64encode(_PNG).decode()}))
    await _generate(image_options={"model": "other/model", "prompt": "other"})
    body = json.loads(requests[0].content)
    assert (body["model"], body["prompt"]) == (SLUG, "a tree")


async def test_format_comes_from_bytes_not_label(monkeypatch):
    _mock_http(monkeypatch, _ok({"b64_json": base64.b64encode(_JPEG).decode(), "media_type": "image/png"}))
    assert await _generate() == [(_JPEG, "jpg")]


async def test_missing_saved_key_never_reaches_network(monkeypatch):
    requests = _mock_http(monkeypatch, _ok())
    with pytest.raises(ProviderError, match="saved provider API key"):
        await _generate(api_key="")
    assert requests == []


@pytest.mark.parametrize(
    "respond, error_type",
    [
        (
            lambda r: httpx.Response(401, json={"error": {"code": 401, "message": "bad key sk-or-saved"}}),
            "auth",
        ),
        (lambda r: httpx.Response(403, json={"error": {"code": 403, "message": "x"}}), "auth"),
        (lambda r: httpx.Response(402, json={"error": {"code": 402, "message": "x"}}), "credits"),
        (lambda r: httpx.Response(429, json={"error": {"code": 429, "message": "x"}}), "rate_limit"),
        (lambda r: httpx.Response(400, json={"error": {"code": 400, "message": "x"}}), "bad_request"),
        (lambda r: httpx.Response(502, json={"error": {"code": 502, "message": "x"}}), "upstream"),
        (_ok(), "no_image"),
        (_ok({"url": "https://example.invalid/x.png"}), "no_image"),
        (_ok({"b64_json": "@@not base64@@"}), "invalid_response"),
        (
            _ok(
                {
                    "b64_json": base64.b64encode(b"<svg onload=alert(1)>").decode(),
                    "media_type": "image/svg+xml",
                }
            ),
            "unsupported_format",
        ),
        (lambda r: httpx.Response(200, content=b"not json"), "invalid_response"),
        (lambda r: httpx.Response(200, json=["list"]), "invalid_response"),
    ],
)
async def test_failures_have_distinct_error_types(monkeypatch, respond, error_type):
    requests = _mock_http(monkeypatch, respond)
    with pytest.raises(ProviderError) as error:
        await _generate()
    assert error.value.error_type == error_type
    assert "sk-or-saved" not in str(error.value)
    assert len(requests) == 1


async def test_timeout_is_not_retried(monkeypatch):
    def respond(request):
        raise httpx.ReadTimeout("slow", request=request)

    requests = _mock_http(monkeypatch, respond)
    with pytest.raises(ProviderError) as error:
        await _generate()
    assert error.value.error_type == "timeout"
    assert len(requests) == 1


async def test_slow_response_hits_the_overall_deadline(monkeypatch):
    import asyncio

    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.02)
                yield b" "

    requests = _mock_http(monkeypatch, lambda r: httpx.Response(200, stream=Drip()))
    with pytest.raises(ProviderError) as error:
        await _generate(timeout=0.2)
    assert error.value.error_type == "timeout"
    assert len(requests) == 1


async def test_oversized_response_is_refused(monkeypatch):
    monkeypatch.setattr(provider_module, "_MAX_IMAGE_RESPONSE_BYTES", 1000)
    _mock_http(monkeypatch, lambda r: httpx.Response(200, content=b"x" * 5000))
    with pytest.raises(ProviderError) as error:
        await _generate()
    assert error.value.error_type == "too_large"


# ---- /images route ----


async def _route_setup(app, monkeypatch, generate):
    async def models():
        return {SLUG: CHAT_ENTRY}

    async def images():
        return {SLUG: IMAGE_ENTRY}

    monkeypatch.setattr(capabilities, "openrouter_catalog", models)
    monkeypatch.setattr(capabilities, "openrouter_image_catalog", images)
    state = app.state.claw
    reg = await app.client.post("/api/auth/register", json={"email": "orimg@x.io", "password": "password123"})
    body = reg.json()
    provider = await state.llm_config.create_provider("or", "sk-or-saved", BASE, model_prefix="openrouter")
    await state.llm_config.create_model(provider.id, STORED, "Nano Banana 2 Lite", kind="image")
    session = await state.sessions.create(body["user"]["id"], "or images")
    state.runtime = SimpleNamespace(provider=SimpleNamespace(generate_image=generate))
    return body["user"]["id"], {"Authorization": f"Bearer {body['access_token']}"}, session.id


async def test_route_generates_through_image_api(db_factory, monkeypatch):
    captured = {}

    async def generate(prompt, model, **kwargs):
        captured.update(kwargs, model=model)
        return [(_PNG, "png")]

    app = build_api_app(db_factory)
    async with client(app) as c:
        app.client = c
        uid, headers, sid = await _route_setup(app, monkeypatch, generate)
        r = await c.post(
            f"/api/sessions/{sid}/images", headers=headers, json={"model": STORED, "prompt": "a tree"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["path"].endswith(".png")
        assert captured["model"] == STORED
        assert captured["mode"] == "openrouter_images"
        assert captured["image_options"] == {"n": 1}
        assert captured["api_key"] == "sk-or-saved" and captured["explicit_credentials"] is True
        assert (await app.state.claw.usage.usage_today(uid))["images"] == 1


async def test_route_refuses_unsupported_size_before_quota(db_factory, monkeypatch):
    async def generate(*args, **kwargs):
        pytest.fail("unsupported size reached the provider")

    app = build_api_app(db_factory)
    async with client(app) as c:
        app.client = c
        uid, headers, sid = await _route_setup(app, monkeypatch, generate)
        r = await c.post(
            f"/api/sessions/{sid}/images",
            headers=headers,
            json={"model": STORED, "prompt": "a tree", "size": "1792x1024"},
        )
        assert r.status_code == 422
        assert "7:4" in r.json()["detail"] and "16:9" in r.json()["detail"]
        assert (await app.state.claw.usage.usage_today(uid))["images"] == 0


@pytest.mark.parametrize(
    "error_type, status, needle",
    [
        ("timeout", 504, "ไม่ส่งคำขอซ้ำอัตโนมัติ"),
        ("auth", 502, "API key"),
        ("credits", 502, "เครดิต"),
        ("rate_limit", 429, "จำกัดอัตรา"),
        ("no_image", 502, "ไม่มีภาพ"),
        ("unsupported_format", 502, "รูปแบบภาพ"),
        (None, 502, "สร้างภาพไม่สำเร็จ"),
    ],
)
async def test_route_maps_errors_and_releases_quota(db_factory, monkeypatch, error_type, status, needle):
    calls = []

    async def generate(*args, **kwargs):
        calls.append(1)
        raise ProviderError("upstream said sk-or-saved", error_type=error_type)

    app = build_api_app(db_factory)
    async with client(app) as c:
        app.client = c
        uid, headers, sid = await _route_setup(app, monkeypatch, generate)
        r = await c.post(
            f"/api/sessions/{sid}/images", headers=headers, json={"model": STORED, "prompt": "a tree"}
        )
        assert r.status_code == status
        assert needle in r.json()["detail"]
        assert "sk-or-saved" not in r.text
        assert calls == [1]
        assert (await app.state.claw.usage.usage_today(uid))["images"] == 0


async def test_route_oversized_image_releases_quota(db_factory, monkeypatch):
    async def generate(*args, **kwargs):
        return [(b"\x89PNG" + b"x" * 64, "png")]

    app = build_api_app(db_factory)
    app.state.claw.settings.image.max_bytes = 10
    async with client(app) as c:
        app.client = c
        uid, headers, sid = await _route_setup(app, monkeypatch, generate)
        r = await c.post(
            f"/api/sessions/{sid}/images", headers=headers, json={"model": STORED, "prompt": "a tree"}
        )
        assert r.status_code == 502
        assert (await app.state.claw.usage.usage_today(uid))["images"] == 0
