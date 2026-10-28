import asyncio
from dataclasses import dataclass
from functools import lru_cache
from importlib.util import find_spec
import json
from pathlib import Path
from typing import Literal
import time

import httpx


@dataclass(frozen=True)
class ImageCapability:
    status: Literal["verified", "unsupported", "unknown"]
    provider: str
    api_base: str
    model: str
    generation_mode: Literal["images_endpoint", "chat"]
    source: str
    reason: str
    request_modalities: tuple[str, ...] = ()

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
    prefix: str, api_base: str, model: str, metadata: dict, openrouter_models: dict | None = None
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
        if openrouter_models is None:
            return result("unknown", "openrouter", "metadata_unavailable")
        entry = openrouter_models.get(bare)
        if not isinstance(entry, dict):
            return result("unknown", "openrouter", "model_not_found")
        architecture = entry.get("architecture")
        if not isinstance(architecture, dict):
            return result("unknown", "openrouter", "metadata_unavailable")
        outputs = architecture.get("output_modalities")
        inputs = architecture.get("input_modalities")
        parameters = entry.get("supported_parameters")
        if not all(
            isinstance(value, list) and all(isinstance(item, str) for item in value)
            for value in (outputs, inputs, parameters)
        ):
            return result("unknown", "openrouter", "metadata_unavailable")
        if "image" not in outputs:
            return result(
                "unsupported", "openrouter", "vision_only" if "image" in inputs else "no_image_output"
            )
        if "text" in inputs and "modalities" in parameters:
            return result("verified", "openrouter", "chat_image_output", outputs)
        return result("unknown", "openrouter", "chat_transport_unconfirmed")
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


_catalog: tuple[float, dict | None] = (0, None)


async def openrouter_catalog() -> dict | None:
    global _catalog
    now = time.monotonic()
    if now < _catalog[0]:
        return _catalog[1]
    catalog = None
    try:
        async with asyncio.timeout(5):
            async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                async with client.stream("GET", "https://openrouter.ai/api/v1/models") as response:
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 4_000_000:
                            raise ValueError("metadata_limit")
        entries = json.loads(data).get("data")
        if isinstance(entries, list) and len(entries) <= 20000:
            catalog = {
                row["id"]: row for row in entries if isinstance(row, dict) and isinstance(row.get("id"), str)
            }
    except (httpx.HTTPError, ValueError, TypeError, AttributeError, TimeoutError):
        pass
    _catalog = (now + (300 if catalog is not None else 30), catalog)
    return catalog


async def image_capability(prefix: str, api_base: str, model: str) -> ImageCapability:
    metadata = bundled_metadata()
    preliminary = resolve_capability(prefix, api_base, model, metadata)
    if preliminary.provider == "openrouter" and preliminary.reason == "metadata_unavailable":
        return resolve_capability(prefix, api_base, model, metadata, await openrouter_catalog())
    return preliminary
