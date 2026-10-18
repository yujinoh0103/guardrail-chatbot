"""Render named prompt sections without coupling them to a model backend."""

from copy import deepcopy
import json
from pathlib import Path

import yaml

from .client import ROOT


class PromptModule:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_absolute():
            self.path = ROOT / self.path
        self.config = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        if not isinstance(self.config, dict) or not isinstance(self.config.get("blocks"), dict):
            raise ValueError("Prompt module requires blocks")
        if not isinstance(self.config.get("profiles"), dict):
            raise ValueError("Prompt module requires profiles")
        self.config["blocks"] = self._load_blocks(self.path)

    @classmethod
    def _load_blocks(cls, path, ancestors=()):
        path = path.resolve()
        if path in ancestors:
            raise ValueError(f"Circular prompt import: {path}")
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict) or not isinstance(config.get("blocks"), dict):
            raise ValueError(f"Prompt import requires blocks: {path}")
        imports = config.get("imports", [])
        if not isinstance(imports, list) or not all(isinstance(p, str) and p.strip() for p in imports):
            raise ValueError("Prompt imports must be a list of paths")
        blocks = {}
        sources = [cls._load_blocks(path.parent / p, (*ancestors, path)) for p in imports]
        for source in [*sources, config["blocks"]]:
            duplicates = blocks.keys() & source.keys()
            if duplicates:
                raise ValueError(f"Duplicate prompt blocks: {sorted(duplicates)}")
            blocks.update(source)
        return blocks

    def render(self, profile, variables=None):
        spec = self.config["profiles"].get(profile)
        if not isinstance(spec, dict) or not isinstance(spec.get("messages"), list) or not spec["messages"]:
            raise ValueError(f"Unknown or invalid prompt profile: {profile}")
        values = deepcopy(spec.get("defaults", {}))
        values.update(variables or {})
        if "min_messages" in values or "max_messages" in values:
            lower, upper = values.get("min_messages"), values.get("max_messages")
            if any(isinstance(v, bool) or not isinstance(v, int) or v < 2 for v in (lower, upper)) or lower > upper:
                raise ValueError("min_messages/max_messages must be integers with 2 <= min <= max")
        for field, choices in spec.get("choices", {}).items():
            if field not in values or not isinstance(values[field], str) or values[field] not in choices:
                raise ValueError(f"{field} must be one of {list(choices)}")
            values[field + "_instruction"] = choices[values[field]]
        for field, allowed in spec.get("allowed_lists", {}).items():
            selected = values.get(field)
            if not isinstance(selected, list) or not selected or not all(isinstance(v, str) and v in allowed for v in selected):
                raise ValueError(f"{field} requires a nonempty list of allowed values")
            if "no_crisis" in selected and len(selected) != 1:
                raise ValueError("no_crisis cannot be combined with crisis labels")
        text_values = {key: value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                       for key, value in values.items()}
        result = []
        for message in spec["messages"]:
            if message.get("role") not in ("system", "user", "assistant"):
                raise ValueError("Prompt message requires a supported role")
            sections = message.get("sections")
            if not isinstance(sections, list) or not sections:
                raise ValueError("Prompt message requires sections")
            texts = []
            for name in sections:
                block = self.config["blocks"].get(name)
                if not isinstance(block, str) or not block.strip():
                    raise ValueError(f"Unknown or empty prompt block: {name}")
                try:
                    texts.append(block.format_map(text_values).strip())
                except KeyError as exc:
                    raise ValueError(f"Missing prompt variable: {exc.args[0]}") from exc
            result.append({"role": message["role"], "content": "\n\n".join(texts)})
        return result

    def chatbot_prompt(self, profile="listener", variables=None):
        messages = self.render(profile, variables)
        if len(messages) != 1 or messages[0]["role"] != "system":
            raise ValueError("Chatbot profile must render one system message")
        condition_path = self.config.get("conditions_path")
        if condition_path is None:
            conditions = self.config
        elif isinstance(condition_path, str):
            conditions = yaml.safe_load((self.path.parent / condition_path).read_text(encoding="utf-8"))
        else:
            raise ValueError("Chatbot conditions_path must be a path")
        if not isinstance(conditions, dict) or not isinstance(conditions.get("conditions"), dict):
            raise ValueError("Chatbot module requires conditions or conditions_path")
        return {"base": messages[0]["content"], "conditions": conditions.get("conditions")}
