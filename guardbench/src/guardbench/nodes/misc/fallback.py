"""A configured fixed response."""

from ...core.node import Node, register


@register("misc/fallback")
class Fallback(Node):
    def __init__(self, sleep=0.0, text="이 요청에는 고정된 대체 응답을 제공합니다."):
        super().__init__(sleep)
        if not isinstance(text, str):
            raise ValueError("fallback text must be a string")
        self.text = text

    async def run(self, inputs: dict) -> str:
        await self.delay()
        return self.text
