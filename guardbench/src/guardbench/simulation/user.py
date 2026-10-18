"""LLM user simulator that reports a hidden suicide/self-harm state each turn."""

from copy import deepcopy
import json
import os
from pathlib import Path

import yaml

from ..llm.client import ROOT, backend_factory, create_backend, model_settings

SCALE_MAX = {"ideation": 5, "disclosure": 3}
SLOW_AXES = ("ideation",)


class UserSimulator:
    def __init__(self, model="user_simulator", models_config="configs/models.yaml",
                 prompt_path="prompts/user_simulator.yaml", max_step=2, transcript_messages=40):
        if type(max_step) is not int or not 1 <= max_step <= 10:
            raise ValueError("max_step must be an integer from 1 to 10")
        if type(transcript_messages) is not int or transcript_messages < 2:
            raise ValueError("transcript_messages must be an integer of at least 2")
        self.settings = model_settings(model, models_config)
        backend_factory(self.settings["backend"])
        path = Path(prompt_path)
        self.prompt_path = path if path.is_absolute() else ROOT / path
        self.prompt = yaml.safe_load(self.prompt_path.read_text(encoding="utf-8"))
        if not all(isinstance(self.prompt.get(k), str) for k in ("system", "open", "respond")) or not isinstance(self.prompt.get("schema"), dict):
            raise ValueError("Simulator prompt requires system, open, respond and schema")
        self.axes = tuple(self.prompt["schema"]["properties"]["state"]["required"])
        self.max_step, self.transcript_messages = max_step, transcript_messages

    def build_messages(self, persona, history, state):
        system = self.prompt["system"].replace("{persona_context}", persona["persona_context"])
        if state is None:
            payload = {"instruction": self.prompt["open"]}
        else:
            # Roles are flipped in wording only: the simulator reads the chatbot as the other party.
            transcript = [{"speaker": "you" if m["role"] == "user" else "chatbot", "content": m["content"]}
                          for m in history if m["role"] in ("user", "assistant")]
            if not transcript or transcript[-1]["speaker"] != "chatbot":
                raise ValueError("Simulator responds only after a chatbot message")
            payload = {"instruction": self.prompt["respond"], "previous_state": state,
                       "transcript": transcript[-self.transcript_messages:]}
        return [{"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

    def _constrain(self, proposed, previous, persona):
        """Bound per-turn change so a single message cannot reset a clinical state."""
        state, adjustments = dict(proposed), []
        if previous is None:
            if persona.get("ideation_range") is None:
                return state, adjustments
            low, high = persona["ideation_range"]
            value = min(max(state["ideation"], low), high)
            if value != state["ideation"]:
                adjustments.append({"axis": "ideation", "proposed": state["ideation"], "applied": value,
                                    "rule": "opening ideation must match persona"})
                state["ideation"] = value
            return state, adjustments
        for axis in self.axes:
            old, new = previous[axis], state[axis]
            if axis == "disclosure":
                applied = max(old, new)
                rule = "disclosure cannot decrease"
            else:
                step = 1 if axis in SLOW_AXES else self.max_step
                applied = min(max(new, old - step), old + step)
                rule = f"change limited to {step} per turn"
            if applied != new:
                adjustments.append({"axis": axis, "proposed": new, "applied": applied, "rule": rule})
                state[axis] = applied
        return state, adjustments

    def _validate(self, result):
        schema = self.prompt["schema"]
        if not isinstance(result, dict) or set(result) != set(schema["required"]):
            raise ValueError("Simulator response must match the schema keys")
        state = result["state"]
        if not isinstance(state, dict) or set(state) != set(self.axes):
            raise ValueError("Simulator state must contain every axis")
        for axis, value in state.items():
            if type(value) is not int or not 0 <= value <= SCALE_MAX.get(axis, 10):
                raise ValueError(f"Invalid simulator state value: {axis}={value!r}")
        appraisal = result["appraisal"]
        if (not isinstance(appraisal, dict) or type(appraisal.get("felt_heard")) is not int
                or not -2 <= appraisal["felt_heard"] <= 2
                or type(appraisal.get("felt_judged")) is not bool or type(appraisal.get("felt_pressured")) is not bool):
            raise ValueError("Invalid simulator appraisal")
        if result["action"] not in schema["properties"]["action"]["enum"]:
            raise ValueError("Invalid simulator action")
        if not isinstance(result["utterance"], str) or not result["utterance"].strip():
            raise ValueError("Simulator utterance must be nonempty")
        if type(result["end_conversation"]) is not bool:
            raise ValueError("end_conversation must be a boolean")

    async def step(self, persona, history, state=None, trace=None) -> dict:
        """Open the conversation (state=None) or react to the latest chatbot message.

        The instance is shared by concurrent dialogues, so the request snapshot goes
        into the caller's trace mapping rather than onto the simulator.
        """
        messages = self.build_messages(persona, history, state)
        settings = deepcopy(self.settings)
        settings.setdefault("sampling", {})["response_format"] = {
            "type": "json_schema", "json_schema": {"name": "simulated_user_turn", "strict": True,
                                                    "schema": self.prompt["schema"]}}
        if trace is not None:
            trace["model_request"] = {"model": os.path.expandvars(settings["model_id"]),
                                      "messages": deepcopy(messages), "sampling": deepcopy(settings["sampling"])}
        backend = create_backend(settings)
        try:
            raw = await backend.generate(messages)
        finally:
            await backend.close()
        result = json.loads(raw)
        self._validate(result)
        result["proposed_state"] = result["state"]
        result["state"], result["adjustments"] = self._constrain(result["state"], state, persona)
        result["raw"] = raw
        return result
