"""Backend boundary used by nodes, never by the runner."""

from abc import ABC, abstractmethod
from copy import deepcopy
from contextvars import ContextVar
import asyncio
import hashlib
import json
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


class Backend(ABC):
    @abstractmethod
    async def generate(self, messages: list[dict]) -> str:
        raise NotImplementedError

    async def close(self) -> None:
        return None


# Factories receive only the selected model configuration. Tests can inject fakes.
BACKENDS = {}
REQUEST_CACHE = ContextVar("guardbench_request_cache", default=None)
REQUEST_AUDIT = ContextVar("guardbench_request_audit", default=None)


def request_key(settings, messages):
    request = {"backend": settings["backend"], "base_url": settings.get("base_url"),
               "model": os.path.expandvars(settings["model_id"]),
               "sampling": settings.get("sampling", {}), "messages": messages}
    return hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class CachedBackend(Backend):
    """Reuse exact inference requests; node-specific validation still runs."""
    def __init__(self, settings, cache):
        self.settings, self.cache, self.inner = deepcopy(settings), cache, None

    async def generate(self, messages):
        key = request_key(self.settings, messages)
        saved = self.cache.get(key)
        reused = saved is not None
        if saved is None:
            async def generate():
                if self.inner is None:
                    self.inner = backend_factory(self.settings["backend"])(deepcopy(self.settings))
                content = await self.inner.generate(messages)
                return {"content": content, "usage": deepcopy(getattr(self.inner, "usage", None)), "source": "in_run"}
            saved = asyncio.create_task(generate())
            self.cache[key] = saved
        try:
            result = await asyncio.shield(saved) if isinstance(saved, asyncio.Future) else saved
        except BaseException:
            if self.cache.get(key) is saved:
                del self.cache[key]
            raise
        audit = REQUEST_AUDIT.get()
        if audit is not None:
            audit.append({"request_hash": key, "reused": reused, "source": deepcopy(result["source"]),
                          "usage": deepcopy(result.get("usage")) if not reused else None})
        return result["content"]

    async def close(self):
        if self.inner is not None:
            await self.inner.close()


def model_settings(alias: str, config_path: str | Path = "configs/models.yaml") -> dict:
    path = Path(config_path)
    if not path.is_absolute():
        path = ROOT / path
    catalog = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or alias not in catalog:
        raise ValueError(f"Unknown model alias: {alias}")
    settings = catalog[alias]
    if not isinstance(settings, dict) or not isinstance(settings.get("backend"), str):
        raise ValueError(f"Model {alias} requires a backend")
    if not isinstance(settings.get("model_id"), str) or not settings["model_id"]:
        raise ValueError(f"Model {alias} requires model_id")
    if not isinstance(settings.get("sampling", {}), dict):
        raise ValueError(f"Model {alias} sampling must be a mapping")
    if settings["backend"] == "openai_compatible" and not settings.get("base_url"):
        raise ValueError(f"Model {alias} requires base_url for openai_compatible")
    return deepcopy(settings)


def backend_factory(name: str):
    if name in ("openrouter", "openai_compatible"):
        from .openrouter import OpenRouterClient
        return OpenRouterClient
    if name not in BACKENDS:
        raise ValueError(f"Unsupported chatbot backend: {name}")
    return BACKENDS[name]


def create_backend(settings: dict) -> Backend:
    cache = REQUEST_CACHE.get()
    if cache is not None:
        return CachedBackend(settings, cache)
    return backend_factory(settings["backend"])(deepcopy(settings))
