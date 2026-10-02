"""Keyword-only input moderation."""

from ...core.node import Node, register


@register("guard_in/mock")
class MockGuardIn(Node):
    def __init__(self, sleep=0.0, keywords=None, category="keyword"):
        super().__init__(sleep)
        if keywords is not None and (not isinstance(keywords, list) or not all(isinstance(k, str) and k for k in keywords)):
            raise ValueError("keywords must be a list of nonempty strings")
        self.keywords, self.category = keywords or [], category

    async def run(self, inputs: dict) -> dict:
        await self.delay()
        matched = [word for word in self.keywords if word.casefold() in inputs["user"].casefold()]
        return {
            "label": "unsafe" if matched else "safe",
            "categories": [self.category] if matched else [], "raw": {"matched": matched},
        }
