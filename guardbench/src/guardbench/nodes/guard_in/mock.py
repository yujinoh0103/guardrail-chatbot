"""Keyword-only moderation of the current user or the evaluation context."""

from ...core.node import Node, register


@register("guard_in/mock")
class MockGuardIn(Node):
    def __init__(self, sleep=0.0, keywords=None, category="keyword", scope="user"):
        super().__init__(sleep)
        if keywords is not None and (not isinstance(keywords, list) or not all(isinstance(k, str) and k for k in keywords)):
            raise ValueError("keywords must be a list of nonempty strings")
        if scope not in ("user", "context"):
            raise ValueError("scope must be user or context")
        self.keywords, self.category, self.scope = keywords or [], category, scope

    async def run(self, inputs: dict) -> dict:
        await self.delay()
        messages = inputs["context"] if self.scope == "context" else [{"role": "user", "content": inputs["user"]}]
        hits = [
            {"message_index": i, "role": message["role"], "matched": words}
            for i, message in enumerate(messages)
            if (words := [word for word in self.keywords if word.casefold() in message["content"].casefold()])
        ]
        matched = [word for word in self.keywords if any(word in hit["matched"] for hit in hits)]
        raw = {"matched": matched}
        if self.scope == "context":
            raw.update(scope=self.scope, matched_messages=hits)
        return {
            "label": "unsafe" if matched else "safe",
            "categories": [self.category] if matched else [], "raw": raw,
        }
