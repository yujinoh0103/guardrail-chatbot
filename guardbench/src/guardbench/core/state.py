"""Isolated state for one dialogue turn."""

from copy import deepcopy
from typing import Any

META_KEYS = frozenset({"turn_index", "step_count", "calls"})
INITIAL_KEYS = frozenset({"history", "user", "context", "recorded_response"}) | META_KEYS


class TurnState:
    def __init__(self, history: list[dict], user: str, turn_index: int = 0,
                 *, context_messages: int = 5, recorded_response: str | None = None):
        conversation = [message for message in history if message["role"] in ("user", "assistant")]
        context = (conversation + [{"role": "user", "content": user}])[-context_messages:]
        self._values = {
            "history": deepcopy(history), "user": user,
            "context": deepcopy(context), "recorded_response": recorded_response,
            "turn_index": turn_index, "step_count": 0, "calls": {},
        }

    def __getitem__(self, key: str) -> Any:
        return deepcopy(self._values[key])

    def __contains__(self, key: str) -> bool:
        return key in self._values

    def inputs(self, keys: tuple[str, ...]) -> dict:
        result = {}
        for declared in keys:
            optional = declared.endswith("?")
            key = declared[:-1] if optional else declared
            if key not in self._values and not optional:
                raise KeyError(f"Missing required state input: {key}")
            result[key] = deepcopy(self._values.get(key))
        return result

    def store(self, key: str, value: Any) -> None:
        if key in INITIAL_KEYS:
            raise ValueError(f"Cannot overwrite initial or metadata key: {key}")
        self._values[key] = deepcopy(value)

    def begin_step(self, names: tuple[str, ...]) -> None:
        self._values["step_count"] += 1
        for name in names:
            calls = self._values["calls"]
            calls[name] = calls.get(name, 0) + 1
