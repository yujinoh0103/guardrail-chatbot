"""Bounded, deterministic history excerpts for chatbot input."""

import json

from ...core.node import Node, register


@register("misc/chat_context")
class ChatContext(Node):
    def __init__(self, sleep=0.0, max_chars=8192, recent_messages=4,
                 summary_chars=1024, excerpt_chars=160):
        super().__init__(sleep)
        for name, value, minimum in (
            ("max_chars", max_chars, 128), ("recent_messages", recent_messages, 0),
            ("summary_chars", summary_chars, 0), ("excerpt_chars", excerpt_chars, 1),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        self.max_chars, self.recent_messages = max_chars, recent_messages
        self.summary_chars, self.excerpt_chars = summary_chars, excerpt_chars

    @staticmethod
    def size(summary, messages):
        return len(json.dumps({"summary": summary, "messages": messages},
                              ensure_ascii=False, separators=(",", ":")))

    async def run(self, inputs: dict) -> dict:
        await self.delay()
        history = inputs["history"]
        system = [{"role": m["role"], "content": m["content"]}
                  for m in history if m["role"] == "system"]
        conversation = [{"role": m["role"], "content": m["content"]}
                        for m in history if m["role"] in ("user", "assistant")]
        current = {"role": "user", "content": inputs["user"]}
        if self.size("", system + [current]) > self.max_chars:
            raise ValueError("Current user and system messages exceed chatbot max_chars")
        recent = conversation[-self.recent_messages:] if self.recent_messages else []
        while recent and self.size("", system + recent + [current]) > self.max_chars:
            recent.pop(0)
        older = conversation[:len(conversation) - len(recent)]
        messages = system + recent + [current]
        snippets = []
        # Preserve speaker and chronological order; excerpts do not infer meaning.
        for message in reversed(older):
            content = message["content"]
            if len(content) > self.excerpt_chars:
                content = content[:self.excerpt_chars - 1] + "…"
            snippet = f"{message['role']}: {content}"
            candidate = "\n".join([snippet] + snippets)
            if len(candidate) > self.summary_chars or self.size(candidate, messages) > self.max_chars:
                break
            snippets.insert(0, snippet)
        return {"summary": "\n".join(snippets), "messages": messages}
