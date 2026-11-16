import asyncio
from dataclasses import dataclass, field, replace
from functools import lru_cache
from math import gcd
from importlib.util import find_spec
import json
from pathlib import Path
from typing import Any, Literal
import time

import httpx


@dataclass(frozen=True)
class ImageCapability:
    status: Literal["verified", "unsupported", "unknown"]
    provider: str
    api_base: str
    model: str
    generation_mode: Literal["images_endpoint", "chat", "openrouter_images"]
    source: str
    reason: str
    request_modalities: tuple[str, ...] = ()
    # supported_parameters of the OpenRouter Image API catalog entry
    # (name -> {"type": "enum"|"range"|"boolean", ...}); only set for openrouter_images.
    image_parameters: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def routed_model(self) -> str:
        if self.model.startswith(self.provider + "/") or self.provider == "openai":
            return self.model
        return self.provider + "/" + self.model


@lru_cache(maxsize=1)
def bundled_metadata() -> dict:
    spec = find_spec("litellm")
    if spec is None or spec.origin is None:
        return {}
    try:
        return json.loads(
            (Path(spec.origin).parent / "model_prices_and_context_window_backup.json").read_text()
        )
    except (OSError, ValueError):
        return {}


def generation_mode(provider: str) -> Literal["images_endpoint", "chat"]:
    return "images_endpoint" if provider in {"openai", "azure"} else "chat"


def resolve_capability(
    prefix: str,
    api_base: str,
    model: str,
    metadata: dict,
    openrouter_models: dict | None = None,
    openrouter_images: dict | None = None,
) -> ImageCapability:
    route, separator, bare = model.partition("/")
    known = {str(item.get("litellm_provider", "")) for item in metadata.values() if isinstance(item, dict)}
    known |= {"openrouter", "cloudflare", "azure", "openai", "gemini"}
    provider = prefix or (route if separator and route in known else "openai")
    mode = generation_mode(provider)

    def result(status, source, reason, outputs=()):
        modalities = tuple(item for item in ("image", "text") if item in outputs)
        return ImageCapability(status, provider, api_base, model, mode, source, reason, modalities)

    if prefix and separator and route in known and route != prefix:
        return result("unsupported", "route", "provider_mismatch")
    if provider == "cloudflare":
        return result("unsupported", "route", "transport_unsupported")
    native_base = {"openai": "https://api.openai.com/v1", "openrouter": "https://openrouter.ai/api/v1"}.get(
        provider
    )
    if api_base and api_base.removesuffix("/") != native_base:
        return result("unknown", "route", "custom_endpoint")
    if provider == "azure":
        return result("unknown", "route", "deployment_mapping_required")
    bare = bare if separator and route == provider else model
    if provider == "openrouter":
        chat = _openrouter_chat(result, bare, openrouter_models)
        if chat.status == "verified":
            return chat
        return _openrouter_images(result, bare, openrouter_images, chat)
    entry = metadata.get(model) or metadata.get(f"{provider}/{bare}") or metadata.get(bare)
    if not isinstance(entry, dict) or entry.get("litellm_provider") != provider:
        return result("unknown", "bundled", "model_not_found")
    entry_mode = entry.get("mode")
    if not isinstance(entry_mode, str) or not entry_mode:
        return result("unknown", "bundled", "metadata_unavailable")
    if entry_mode != "image_generation":
        return result(
            "unsupported", "bundled", "vision_only" if entry.get("supports_vision") else "no_image_output"
        )
    if mode == "images_endpoint":
        return result("verified", "bundled", "images_endpoint")
    endpoints = entry.get("supported_endpoints")
    inputs = entry.get("supported_input_modalities")
    outputs = entry.get("supported_output_modalities")
    if not all(
        isinstance(value, list) and all(isinstance(item, str) for item in value)
        for value in (endpoints, inputs, outputs)
    ):
        return result("unknown", "bundled", "metadata_unavailable")
    if "/v1/chat/completions" in endpoints and "text" in inputs and "image" in outputs:
        return result("verified", "bundled", "chat_image_output", outputs)
    return result("unknown", "bundled", "chat_transport_unconfirmed")


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _openrouter_chat(result, bare: str, catalog: dict | None) -> ImageCapability:
    """Chat Completions route: image output is requested with ``modalities``."""
    if catalog is None:
        return result("unknown", "openrouter", "metadata_unavailable")
    entry = catalog.get(bare)
    if not isinstance(entry, dict):
        return result("unknown", "openrouter", "model_not_found")
    architecture = entry.get("architecture")
    if not isinstance(architecture, dict):
        return result("unknown", "openrouter", "metadata_unavailable")
    outputs = architecture.get("output_modalities")
    inputs = architecture.get("input_modalities")
    parameters = entry.get("supported_parameters")
    if not all(_string_list(value) for value in (outputs, inputs, parameters)):
        return result("unknown", "openrouter", "metadata_unavailable")
    if "image" not in outputs:
        return result("unsupported", "openrouter", "vision_only" if "image" in inputs else "no_image_output")
    if "text" in inputs and "modalities" in parameters:
        return result("verified", "openrouter", "chat_image_output", outputs)
    return result("unknown", "openrouter", "chat_transport_unconfirmed")


def _openrouter_images(result, bare: str, catalog: dict | None, chat: ImageCapability) -> ImageCapability:
    """Image API route (POST /images), confirmed only by the /images/models catalog.

    Listing there is what proves the transport; the Chat API's ``modalities``
    parameter says nothing about it. A model the catalog does not list keeps
    the Chat API verdict, so a vision-only model stays unsupported.
    """
    if catalog is None:
        # Only the Image API catalog could have settled these two Chat API verdicts.
        if chat.status == "unknown" and chat.reason in {"model_not_found", "chat_transport_unconfirmed"}:
            return result("unknown", "openrouter_images", "images_metadata_unavailable")
        return chat
    entry = catalog.get(bare)
    if not isinstance(entry, dict):
        return chat
    architecture = entry.get("architecture")
    outputs = architecture.get("output_modalities") if isinstance(architecture, dict) else None
    parameters = entry.get("supported_parameters")
    if not _string_list(outputs) or not isinstance(parameters, dict):
        return result("unknown", "openrouter_images", "images_metadata_invalid")
    if "image" not in outputs:
        return result("unsupported", "openrouter_images", "no_image_output")
    return replace(
        result("verified", "openrouter_images", "images_api"),
        generation_mode="openrouter_images",
        request_modalities=(),
        image_parameters=parameters,
    )


class ImageOptionError(ValueError):
    """A requested size the model's Image API metadata does not support."""


_RESOLUTION_TIERS = ((512, "512"), (768, "768"), (1024, "1K"), (1536, "1.5K"), (2048, "2K"), (4096, "4K"))


def _allows(descriptor: Any, value: Any) -> bool:
    if not isinstance(descriptor, dict):
        return False
    if descriptor.get("type") == "enum":
        values = descriptor.get("values")
        return isinstance(values, list) and value in values
    if descriptor.get("type") == "range":
        low, high = descriptor.get("min"), descriptor.get("max")
        return (
            isinstance(value, int)
            and isinstance(low, int | float)
            and isinstance(high, int | float)
            and low <= value <= high
        )
    return False


def _choices(descriptor: Any) -> str:
    values = descriptor.get("values") if isinstance(descriptor, dict) else None
    return ", ".join(str(v) for v in values) if isinstance(values, list) and values else "none"


def openrouter_image_options(parameters: dict[str, Any], size: str | None) -> dict[str, Any]:
    """Map a ``WxH`` size onto the Image API parameters the catalog lists.

    Only parameters the model declares are sent. A size it cannot honor raises
    ImageOptionError instead of being dropped, so the user is told before any
    paid call. No size means the model's own default.
    """
    options: dict[str, Any] = {}
    if _allows(parameters.get("n"), 1):
        options["n"] = 1
    if not size:
        return options
    if _allows(parameters.get("size"), size):
        return options | {"size": size}
    width, x, height = size.lower().partition("x")
    if not (x and width.isdigit() and height.isdigit() and int(width) > 0 and int(height) > 0):
        raise ImageOptionError(f"unrecognized size {size!r}; expected WIDTHxHEIGHT")
    w, h = int(width), int(height)
    divisor = gcd(w, h)
    ratio = f"{w // divisor}:{h // divisor}"
    tier = next((name for edge, name in _RESOLUTION_TIERS if max(w, h) <= edge), None)
    aspect, resolution = parameters.get("aspect_ratio"), parameters.get("resolution")
    if aspect is None and resolution is None:
        if "size" in parameters:
            raise ImageOptionError(
                f"size {size} is not supported (supported: {_choices(parameters['size'])})"
            )
        raise ImageOptionError("this model does not accept a size")
    if aspect is not None:
        if not _allows(aspect, ratio):
            raise ImageOptionError(f"aspect ratio {ratio} is not supported (supported: {_choices(aspect)})")
        options["aspect_ratio"] = ratio
    elif ratio != "1:1":
        raise ImageOptionError(f"aspect ratio {ratio} is not supported (supported: 1:1)")
    if resolution is not None:
        if tier is None or not _allows(resolution, tier):
            raise ImageOptionError(
                f"resolution for {size} is not supported (supported: {_choices(resolution)})"
            )
        options["resolution"] = tier
    return options


OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_IMAGE_MODELS_URL = "https://openrouter.ai/api/v1/images/models"
_catalogs: dict[str, tuple[float, dict | None]] = {}


_catalog_locks: dict[str, asyncio.Lock] = {}


async def _cached_catalog(url: str) -> dict | None:
    """Public, unauthenticated catalog read: bounded, cached, never retried.
    Concurrent callers on an expired entry share one download."""
    expires, cached = _catalogs.get(url, (0, None))
    if time.monotonic() < expires:
        return cached
    async with _catalog_locks.setdefault(url, asyncio.Lock()):
        expires, cached = _catalogs.get(url, (0, None))
        if time.monotonic() < expires:
            return cached
        catalog = await _fetch_catalog(url)
        _catalogs[url] = (time.monotonic() + (300 if catalog is not None else 30), catalog)
        return catalog


async def _fetch_catalog(url: str) -> dict | None:
    try:
        async with asyncio.timeout(5):
            async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 4_000_000:
                            raise ValueError("metadata_limit")
        entries = json.loads(data).get("data")
    except (httpx.HTTPError, ValueError, TypeError, AttributeError, TimeoutError):
        return None
    if not isinstance(entries, list) or len(entries) > 20000:
        return None
    return {row["id"]: row for row in entries if isinstance(row, dict) and isinstance(row.get("id"), str)}


async def openrouter_catalog() -> dict | None:
    return await _cached_catalog(OPENROUTER_MODELS_URL)


async def openrouter_image_catalog() -> dict | None:
    return await _cached_catalog(OPENROUTER_IMAGE_MODELS_URL)


async def image_capability(prefix: str, api_base: str, model: str) -> ImageCapability:
    metadata = bundled_metadata()
    preliminary = resolve_capability(prefix, api_base, model, metadata)
    if preliminary.provider != "openrouter" or preliminary.source == "route":
        return preliminary
    models = await openrouter_catalog()
    # An empty image catalog leaves the Chat API verdict as is.
    chat = resolve_capability(prefix, api_base, model, metadata, models, {})
    if chat.status == "verified":
        return chat
    return resolve_capability(prefix, api_base, model, metadata, models, await openrouter_image_catalog())
