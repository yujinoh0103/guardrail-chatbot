"""Deterministic candidate with visible history and feedback counts."""

from ...core.node import Node, register


@register("chatbot/mock")
class MockChatbot(Node):
    def __init__(self, sleep=0.0, error_keyword=None):
        super().__init__(sleep)
        self.error_keyword = error_keyword
        self.feedback_count = 0

    async def run(self, inputs: dict) -> str:
        await self.delay()
        prepared = inputs["chat_context"]
        messages = prepared["messages"]
        user = messages[-1]["content"]
        history_count = len(messages) - 1
        summary_chars = len(prepared["summary"])
        if self.error_keyword and self.error_keyword in user:
            raise RuntimeError(f"Mock chatbot failure for keyword: {self.error_keyword}")
        feedback = inputs.get("fb")
        if feedback is not None:
            self.feedback_count += 1
        return f"mock: {user} | history_messages={history_count} | feedback_count={self.feedback_count} | summary_chars={summary_chars}"
