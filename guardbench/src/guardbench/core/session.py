"""Sequential live input, retaining only completed turns in history."""

import asyncio
from copy import deepcopy

from .runner import Runner


class LiveSession:
    def __init__(self, runner: Runner, dialogue_id="live", history=None):
        if runner.pipeline.response_mode != "generate":
            raise ValueError("Live conversation requires a generate pipeline")
        self.runner, self.dialogue_id = runner, dialogue_id
        self.history = deepcopy(history or [])
        self.turn_index = sum(message["role"] == "user" for message in self.history)
        self.stopped = False
        self._lock = asyncio.Lock()

    async def send(self, user: str) -> dict:
        if not isinstance(user, str) or not user.strip():
            raise ValueError("Live user input must be nonempty text")
        async with self._lock:
            if self.stopped:
                raise RuntimeError("This dialogue stopped after a turn error")
            result = await self.runner.run_turn(
                self.dialogue_id, self.history, user, self.turn_index,
                input_metadata={"input_path": "live"},
            )
            if result["error"]:
                self.stopped = True
            else:
                self.history.extend([
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": result["final"]},
                ])
                self.turn_index += 1
            return result
