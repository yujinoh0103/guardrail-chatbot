"""Interpret this mock feedback and return configured node names."""

from ...core.node import Node, register


@register("routers/mock")
class MockRouter(Node):
    def __init__(self, sleep=0.0, mode="feedback", retry_target="bot", fallback_target="fallback", max_retries=1):
        super().__init__(sleep)
        if mode not in ("feedback", "gate", "end"):
            raise ValueError("router mode must be feedback, gate, or end")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a nonnegative integer")
        self.mode, self.retry_target = mode, retry_target
        self.fallback_target, self.max_retries = fallback_target, max_retries

    async def run(self, inputs: dict) -> str:
        await self.delay()
        if self.mode == "end":
            return "END"
        if self.mode == "gate":
            return self.fallback_target if inputs["g11"]["label"] == "unsafe" else self.retry_target
        feedback = inputs["fb"]
        if feedback.get("fallback_requested"):
            return self.fallback_target
        calls = inputs["calls"].get(self.retry_target, 0)
        if feedback.get("retry_requested") and calls <= self.max_retries:
            return self.retry_target
        return "END"
