"""Generate three pre-response feedback styles from context and g11 risk types."""

import json
from copy import deepcopy
import os
from pathlib import Path

import yaml

from ...core.node import Node, register
from ...llm.client import ROOT, backend_factory, create_backend, model_settings


@register("guard_out/feedback")
class Feedback(Node):
    def __init__(self, model="feedback", models_config="configs/models.yaml",
                 prompt_path="prompts/feedback.yaml", sleep=0.0, max_chars=None,
                 length_retries=1, include_guard_details=True, skip_when_safe=False):
        super().__init__(sleep)
        self.settings = model_settings(model, models_config)
        backend_factory(self.settings["backend"])
        path = Path(prompt_path)
        self.prompt_path = str(path if path.is_absolute() else ROOT / path)
        prompt = yaml.safe_load(Path(self.prompt_path).read_text(encoding="utf-8"))
        if not isinstance(prompt, dict) or not isinstance(prompt.get("system"), str) or not prompt["system"].strip():
            raise ValueError("Feedback prompt requires a nonempty system string")
        self.system = prompt["system"]
        self.max_chars = max_chars or {"rationale": 320, "brief": 120, "explanation": 320}
        if set(self.max_chars) != {"rationale", "brief", "explanation"} or any(
            isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in self.max_chars.values()
        ):
            raise ValueError("Feedback max_chars requires three positive character limits")
        if isinstance(length_retries, bool) or not isinstance(length_retries, int) or length_retries < 0:
            raise ValueError("Feedback length_retries must be a nonnegative integer")
        self.length_retries = length_retries
        if not isinstance(include_guard_details, bool):
            raise ValueError("include_guard_details must be a boolean")
        self.include_guard_details = include_guard_details
        if not isinstance(skip_when_safe, bool):
            raise ValueError("skip_when_safe must be a boolean")
        self.skip_when_safe = skip_when_safe

    def _parse(self, raw, risk_types):
        result = json.loads(raw)
        if not isinstance(result, dict) or set(result) != {"risk_type", "rationale", "brief", "explanation"}:
            raise ValueError("Feedback must contain risk_type/rationale/brief/explanation")
        if (risk_types and result["risk_type"] not in risk_types) or (not risk_types and result["risk_type"] is not None):
            raise ValueError("Feedback risk_type must be chosen from g11 categories, or null when empty")
        for style in self.max_chars:
            if not isinstance(result[style], str) or not result[style].strip():
                raise ValueError(f"Feedback {style} must be a nonempty string")
        return result

    async def run(self, inputs):
        await self.delay()
        context, guard = inputs["context"], inputs["g11"]
        if not context or context[-1]["role"] != "user":
            raise ValueError("Feedback context must end in user")
        if self.skip_when_safe and guard["label"] == "safe":
            # No model call; the chatbot answers this turn without guard feedback.
            self.model_requests = []
            return {"risk_type": None, "rationale": None, "brief": None, "explanation": None,
                    "skipped": "g11 label safe", "raw": {"model_id": self.settings["model_id"]}}
        risk_types = guard["categories"]
        if not isinstance(risk_types, list) or not all(isinstance(r, str) for r in risk_types):
            raise ValueError("g11 categories must be a list of strings")
        payload = {"context": context, "label": guard["label"], "risk_types": risk_types,
                   "max_characters_per_style": self.max_chars}
        if self.include_guard_details:
            raw = guard.get("raw", {})
            payload["guard_details"] = {key: raw[key] for key in
                ("judge", "cell", "tnt", "goalex", "assignment") if key in raw}
            # guard_in/combined and guard_in/expert11: clinical level and reasons, not the raw Jev answers.
            if guard.get("subcategories"):
                payload["guard_details"]["subcategories"] = guard["subcategories"]
            if guard.get("expert11"):
                payload["guard_details"]["expert11"] = {key: guard["expert11"][key] for key in
                    ("level", "response", "reasons", "context", "chronic") if key in guard["expert11"]}
        settings = deepcopy(self.settings)
        settings.setdefault("sampling", {})["response_format"] = {
            "type": "json_schema", "json_schema": {
                "name": "guard_feedback", "strict": True,
                "schema": {"type": "object", "additionalProperties": False,
                           "properties": {"risk_type": {"type": ["string", "null"],
                                                         "enum": risk_types if risk_types else [None]},
                                          **{style: {"type": "string"} for style in self.max_chars}},
                           "required": ["risk_type", *self.max_chars]},
            },
        }
        backend = create_backend(settings)
        messages = [
            {"role": "system", "content": self.system + "\nKeep well below the supplied character limits, including spaces."},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        attempts = []
        self.model_requests = []
        try:
            for attempt in range(self.length_retries + 1):
                self.model_requests.append({
                    "model": os.path.expandvars(self.settings["model_id"]),
                    "messages": deepcopy(messages),
                    "sampling": deepcopy(settings.get("sampling", {})),
                })
                raw = await backend.generate(messages)
                attempts.append(raw)
                result = self._parse(raw, risk_types)
                over_limits = {style: {"characters": len(result[style]), "target": limit}
                               for style, limit in self.max_chars.items() if len(result[style]) > limit}
                if not over_limits or attempt == self.length_retries:
                    break
                messages.extend([
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": "Rewrite only the overlong fields much more concisely. "
                     "Preserve risk_type, meaning, language and the other fields. "
                     "Return the complete four-key JSON object again. Aim below 80% of each target. "
                     + json.dumps(over_limits)},
                ])
        finally:
            await backend.close()
        return {**result, "raw": {"output": raw, "model_id": self.settings["model_id"],
                                   "prompt_path": self.prompt_path, "attempts": attempts,
                                   "length_overruns": over_limits}}
