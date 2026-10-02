"""Backend boundary used by nodes, never by the runner."""

from abc import ABC, abstractmethod
from copy import deepcopy
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
    return backend_factory(settings["backend"])(deepcopy(settings))
