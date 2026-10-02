"""Conversation input validation, without dataset-specific label interpretation."""

from copy import deepcopy
import json
from pathlib import Path


def validate_dialogue(item: dict) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Dialogue must be a mapping")
    dialogue_id = item.get("dialogue_id")
    if isinstance(dialogue_id, bool) or not isinstance(dialogue_id, (str, int)) or dialogue_id == "":
        raise ValueError("dialogue_id must be a nonempty string or integer")
    if ("turns" in item) == ("messages" in item):
        raise ValueError("Provide exactly one of turns or messages")
    if "turns" in item:
        turns = item["turns"]
        if not isinstance(turns, list) or not turns or not all(isinstance(t, str) for t in turns):
            raise ValueError("turns must be a nonempty list of strings")
    else:
        messages = item["messages"]
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a nonempty list")
        expected, user_count = "user", 0
        for index, message in enumerate(messages):
            if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                raise ValueError(f"Message {index} requires string content")
            role = message.get("role")
            if role == "system" and index == 0:
                continue
            if role != expected:
                raise ValueError(f"Message {index} must have role {expected}")
            user_count += int(role == "user")
            expected = "assistant" if role == "user" else "user"
        if not user_count:
            raise ValueError("messages requires at least one user message")
    return deepcopy(item)


def validate_dialogues(items: list[dict]) -> list[dict]:
    if not isinstance(items, list) or not items:
        raise ValueError("Provide at least one dialogue")
    result, seen = [], set()
    for item in items:
        dialogue = validate_dialogue(item)
        if dialogue["dialogue_id"] in seen:
            raise ValueError(f"Duplicate dialogue_id: {dialogue['dialogue_id']}")
        seen.add(dialogue["dialogue_id"])
        result.append(dialogue)
    return result


def load_dialogues(path: str | Path) -> list[dict]:
    """Read JSONL dialogues or one JSON dialogue object."""
    path = Path(path)
    if path.suffix.lower() == ".json":
        return validate_dialogues([json.loads(path.read_text(encoding="utf-8"))])
    dialogues = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                dialogues.append(validate_dialogue(json.loads(line)))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Invalid dialogue on line {line_number}: {exc}") from exc
    return validate_dialogues(dialogues)


def metadata(item: dict, excluded: tuple[str, ...]) -> dict:
    return deepcopy({key: value for key, value in item.items() if key not in excluded})
