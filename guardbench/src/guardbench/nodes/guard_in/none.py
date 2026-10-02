"""Neutral input moderation for wiring experiments."""

from ...core.node import Node, register


@register("guard_in/none")
class NoGuardIn(Node):
    async def run(self, inputs: dict) -> dict:
        await self.delay()
        return {"label": "safe", "categories": [], "raw": None}
