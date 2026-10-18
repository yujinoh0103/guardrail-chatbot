"""Real chatbot inference through a configured backend/model alias."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

import yaml

from ...core.node import Node, register
from ...llm.client import ROOT, backend_factory, create_backend, model_settings
from ...llm.prompts import PromptModule


@register("chatbot/default")
class Chatbot(Node):
    def __init__(self, model="chatbot", models_config="configs/models.yaml", sleep=0.0,
                 prompt_path=None, condition="baseline", prompt_profile="listener", prompt_vars=None,
                 response_feedback_key=None, response_feedback_prompt="prompts/chatbot_common.yaml"):
        super().__init__(sleep)
        self.settings = model_settings(model, models_config)
        backend_factory(self.settings["backend"])
        if condition not in ("baseline", "label", "category", "rationale", "brief", "explanation"):
            raise ValueError("Unknown chatbot comparison condition")
        if prompt_path is None and condition != "baseline":
            raise ValueError("A comparison condition requires prompt_path")
        self.condition, self.prompt = condition, None
        if response_feedback_key is not None and (not isinstance(response_feedback_key, str) or not response_feedback_key.isidentifier()):
            raise ValueError("response_feedback_key must be a state key")
        self.response_feedback_key = response_feedback_key
        self.response_feedback_template = None
        if response_feedback_key is not None:
            path = Path(response_feedback_prompt)
            blocks = PromptModule._load_blocks(path if path.is_absolute() else ROOT / path)
            template = blocks.get("response_feedback")
            if not isinstance(template, str) or not template.strip():
                raise ValueError("Response feedback prompt requires a response_feedback block")
            template.format(feedback="")
            self.response_feedback_template = template
        if prompt_path is not None:
            path = Path(prompt_path)
            prompt = yaml.safe_load((path if path.is_absolute() else ROOT / path).read_text(encoding="utf-8"))
            if isinstance(prompt, dict) and "profiles" in prompt:
                prompt = PromptModule(path).chatbot_prompt(prompt_profile, prompt_vars)
            if not isinstance(prompt, dict) or not isinstance(prompt.get("base"), str):
                raise ValueError("Chatbot prompt requires base and conditions")
            if not isinstance(prompt.get("conditions"), dict) or not isinstance(prompt["conditions"].get(condition), str):
                raise ValueError("Chatbot prompt must define the selected condition")
            prompt["conditions"][condition].format(feedback="")
            self.prompt = prompt

    def build_messages(self, inputs: dict) -> list[dict]:
        """Build the exact ordered messages used for inference and prompt inspection."""
        prepared = inputs["chat_context"]
        messages = deepcopy(prepared["messages"])
        if not messages or messages[-1].get("role") != "user":
            raise ValueError("chat_context must end with the current user message")
        summary = prepared["summary"]
        if summary:
            messages.insert(0, {"role": "system", "content": "Earlier conversation excerpts:\n" + summary})
        if self.prompt is not None:
            guidance = ""
            if self.condition == "label":
                label = inputs["g11"]["label"]
                if label not in ("safe", "unsafe", "controversial", "unresolved"):
                    raise ValueError("Unsupported g11 label for binary comparison")
                guidance = "safe" if label == "safe" else "unsafe"
            elif self.condition == "category":
                guard = inputs["g11"]
                guidance = json.dumps({"label": guard["label"], "categories": guard["categories"]}, ensure_ascii=False)
            elif self.condition != "baseline" and not inputs["feedback"].get("skipped"):
                guidance = inputs["feedback"][self.condition]
                if not isinstance(guidance, str) or not guidance.strip():
                    raise ValueError("Selected feedback must be a nonempty string")
            system = self.prompt["base"]
            # Skipped feedback (guard_out/feedback skip_when_safe) leaves the base prompt only, as in baseline.
            skipped = self.condition not in ("baseline", "label", "category") and inputs["feedback"].get("skipped")
            condition_prompt = "" if skipped else self.prompt["conditions"][self.condition].format(feedback=guidance)
            if condition_prompt.strip():
                system += "\n" + condition_prompt
            messages.insert(0, {"role": "system", "content": system})
        if self.response_feedback_key is not None:
            feedback = inputs.get(self.response_feedback_key)
        else:
            feedback = inputs.get("fb") if self.prompt is None else None
        if feedback is not None:
            if self.response_feedback_key is not None:
                if not isinstance(feedback, dict):
                    raise ValueError("Response evaluation must be a mapping")
                evaluation = {key: feedback[key] for key in ("label", "categories", "rationale", "scores") if key in feedback}
                content = self.response_feedback_template.format(feedback=json.dumps(evaluation, ensure_ascii=False))
            else:
                content = "Feedback for this candidate:\n" + json.dumps(feedback, ensure_ascii=False)
            messages.insert(len(messages) - 1, {
                "role": "system", "content": content,
            })
        return messages

    async def run(self, inputs: dict) -> str:
        await self.delay()
        messages = self.build_messages(inputs)
        self.model_request = {
            "model": os.path.expandvars(self.settings["model_id"]),
            "messages": deepcopy(messages),
            "sampling": deepcopy(self.settings.get("sampling", {})),
            "base_policy_hash": hashlib.sha256(self.prompt["base"].encode()).hexdigest() if self.prompt else None,
        }
        backend = create_backend(self.settings)
        try:
            response = await backend.generate(messages)
            if not isinstance(response, str):
                raise TypeError("Chat backend response must be a string")
            return response
        finally:
            await backend.close()
