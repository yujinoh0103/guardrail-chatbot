"""Example feedback producer; its shape is not a core contract."""

from copy import deepcopy

from ...core.node import Node, register


@register("guard_out/mock")
class MockGuardOut(Node):
    def __init__(self, sleep=0.0, mode="retry", feedback=None):
        super().__init__(sleep)
        if mode not in ("retry", "fallback", "constant"):
            raise ValueError("mode must be retry, fallback, or constant")
        if feedback is not None and not isinstance(feedback, dict):
            raise ValueError("feedback must be a mapping")
        self.mode, self.feedback = mode, deepcopy(feedback or {})

    async def run(self, inputs: dict) -> dict:
        await self.delay()
        if self.mode == "constant":
            return deepcopy(self.feedback)
        guard = inputs["g11"]
        return {
            "retry_requested": self.mode == "retry" and guard["label"] == "unsafe",
            "fallback_requested": self.mode == "fallback" and guard["label"] == "unsafe",
            "notes": deepcopy(guard["categories"]), "candidate_length": len(inputs["candidate"]),
        }
