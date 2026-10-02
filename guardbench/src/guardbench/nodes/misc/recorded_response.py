"""Expose a stored response through ordinary YAML node wiring."""

from ...core.node import Node, register


@register("misc/recorded_response")
class RecordedResponse(Node):
    async def run(self, inputs: dict) -> str:
        await self.delay()
        response = inputs["recorded_response"]
        if not isinstance(response, str):
            raise ValueError("A stored assistant response is required")
        return response
