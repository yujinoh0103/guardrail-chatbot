"""Node interface and a small, module-addressed registry."""

from abc import ABC, abstractmethod
import asyncio
import importlib
import math
from typing import Any

REGISTRY: dict[str, type["Node"]] = {}


class Node(ABC):
    def __init__(self, sleep: float = 0.0):
        if isinstance(sleep, bool) or not isinstance(sleep, (int, float)) or not math.isfinite(sleep) or sleep < 0:
            raise ValueError("sleep must be a finite nonnegative number")
        self.sleep = sleep

    async def delay(self) -> None:
        await asyncio.sleep(self.sleep)

    @abstractmethod
    async def run(self, inputs: dict) -> Any:
        raise NotImplementedError


def register(name: str):
    def decorator(cls: type[Node]):
        if not issubclass(cls, Node):
            raise TypeError("Registered nodes must implement Node")
        if name in REGISTRY:
            raise ValueError(f"Duplicate node registration: {name}")
        REGISTRY[name] = cls
        return cls
    return decorator


def resolve(name: str) -> type[Node]:
    if name not in REGISTRY:
        parts = name.split("/")
        if len(parts) != 2 or not all(part.isidentifier() for part in parts):
            raise ValueError(f"Unregistered node use: {name}")
        module = "guardbench.nodes." + ".".join(parts)
        try:
            importlib.import_module(module)
        except ModuleNotFoundError as exc:
            if exc.name != module and not module.startswith(exc.name + "."):
                raise
            raise ValueError(f"Unregistered node use: {name}") from exc
    if name not in REGISTRY:
        raise ValueError(f"Unregistered node use: {name}")
    return REGISTRY[name]
