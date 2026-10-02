"""Real chatbot inference through a configured backend/model alias."""

from copy import deepcopy
import json

from ...core.node import Node, register
from ...llm.client import backend_factory, create_backend, model_settings


@register("chatbot/default")
class Chatbot(Node):
    def __init__(self, model="chatbot", models_config="configs/models.yaml", sleep=0.0):
        super().__init__(sleep)
        self.settings = model_settings(model, models_config)
        backend_factory(self.settings["backend"])

    async def run(self, inputs: dict) -> str:
        await self.delay()
        prepared = inputs["chat_context"]
        messages = deepcopy(prepared["messages"])
        if not messages or messages[-1].get("role") != "user":
            raise ValueError("chat_context must end with the current user message")
        summary = prepared["summary"]
        if summary:
            messages.insert(0, {"role": "system", "content": "Earlier conversation excerpts:\n" + summary})
        feedback = inputs.get("fb")
        if feedback is not None:
            messages.insert(len(messages) - 1, {
                "role": "system", "content": "Feedback for this candidate:\n" + json.dumps(feedback, ensure_ascii=False),
            })
        backend = create_backend(self.settings)
        try:
            response = await backend.generate(messages)
            if not isinstance(response, str):
                raise TypeError("Chat backend response must be a string")
            return response
        finally:
            await backend.close()
