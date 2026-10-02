"""Backend-independent asyncio execution of pipelines and dialogues."""

import asyncio
import time

from .data import load_dialogues, metadata, validate_dialogues
from .pipeline import Pipeline
from .state import TurnState
from ..trace.recorder import Recorder, error_info, timestamp


class StepLimitError(RuntimeError):
    """The configured turn step budget is exhausted."""


class Runner:
    def __init__(self, pipeline: Pipeline, recorder: Recorder, concurrency: int = 4):
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency <= 0:
            raise ValueError("concurrency must be a positive integer")
        self.pipeline, self.recorder = pipeline, recorder
        self.semaphore = asyncio.Semaphore(concurrency)

    async def _invoke(self, name, instance, state, dialogue_id):
        spec = self.pipeline.nodes[name]
        record = {
            "event": "node", "dialogue_id": dialogue_id,
            "turn_index": state["turn_index"], "step_count": state["step_count"],
            "node": name, "use": spec.use,
            "input_keys": [key.removesuffix("?") for key in spec.inputs], "inputs": {},
            "output": None, "started_at": timestamp(), "ended_at": None,
            "latency": None, "error": None,
        }
        start = time.perf_counter()
        try:
            inputs = state.inputs(spec.inputs)
            record["input_keys"] = list(inputs)
            # Preserve the snapshot in the trace even if a node mutates its inputs.
            record["inputs"] = state.inputs(spec.inputs)
            result = await instance.run(inputs)
            record["output"] = result
            if spec.router and (not isinstance(result, str) or (result != "END" and result not in self.pipeline.positions)):
                raise ValueError(f"Router {name} returned an unknown target: {result!r}")
            return result
        except Exception as exc:
            record["error"] = error_info(exc)
            raise
        finally:
            record["ended_at"] = timestamp()
            record["latency"] = time.perf_counter() - start
            self.recorder.write(record)

    async def run_turn(self, dialogue_id, history, user, turn_index=0,
                       *, recorded_response=None, input_metadata=None, message_index=None) -> dict:
        state = TurnState(history, user, turn_index,
                          context_messages=self.pipeline.context_messages,
                          recorded_response=recorded_response)
        started_at, start = timestamp(), time.perf_counter()
        final, error = None, None
        try:
            if self.pipeline.response_mode == "recorded" and recorded_response is None:
                raise ValueError("recorded mode requires an assistant response for each user")
            instances = {name: spec.create() for name, spec in self.pipeline.nodes.items()}
            position, target = 0, None
            while position < len(self.pipeline.flow):
                if state["step_count"] >= self.pipeline.max_steps:
                    raise StepLimitError(f"max_steps={self.pipeline.max_steps} exhausted")
                names = (target,) if target is not None else self.pipeline.flow[position]
                state.begin_step(names)
                if target is None and self.pipeline.parallel[position]:
                    group_start, group_clock = timestamp(), time.perf_counter()
                    results = await asyncio.gather(
                        *(self._invoke(name, instances[name], state, dialogue_id) for name in names),
                        return_exceptions=True,
                    )
                    failures = [result for result in results if isinstance(result, BaseException)]
                    self.recorder.write({
                        "event": "parallel_group", "dialogue_id": dialogue_id,
                        "turn_index": turn_index, "step_count": state["step_count"],
                        "nodes": list(names), "started_at": group_start, "ended_at": timestamp(),
                        "latency": time.perf_counter() - group_clock,
                        "error": error_info(failures[0]) if failures else None,
                    })
                    if failures:
                        raise failures[0]
                else:
                    results = [await self._invoke(names[0], instances[names[0]], state, dialogue_id)]
                # Commit only when every node in the step has finished successfully.
                for name, result in zip(names, results):
                    state.store(self.pipeline.nodes[name].output, result)
                if len(names) == 1 and self.pipeline.nodes[names[0]].router:
                    destination = results[0]
                    if destination == "END":
                        break
                    position = self.pipeline.positions[destination]
                    target = destination
                else:
                    position += 1
                    target = None
            final = state[self.pipeline.final]
            if not isinstance(final, str):
                raise TypeError("final response must be a string for chat history")
            if self.pipeline.response_mode == "recorded" and final != recorded_response:
                raise ValueError("recorded mode must retain the original assistant response as final")
        except Exception as exc:
            final, error = None, error_info(exc)
        result = {
            "event": "turn", "dialogue_id": dialogue_id, "turn_index": turn_index,
            "user": user, "final": final, "step_count": state["step_count"],
            "calls": state["calls"], "started_at": started_at, "ended_at": timestamp(),
            "latency": time.perf_counter() - start, "status": "error" if error else "ok",
            "error": error,
            "response_mode": self.pipeline.response_mode,
            "context": state["context"], "history": state["history"],
            "recorded_response": recorded_response, "message_index": message_index,
            "metadata": input_metadata or {},
            "outputs": state.inputs(tuple(key + "?" for key in self.pipeline.outputs)),
        }
        self.recorder.write(result, turn=True)
        return result

    async def _dialogue(self, dialogue: dict) -> list[dict]:
        async with self.semaphore:
            history, turns = [], []
            info = metadata(dialogue, ("dialogue_id", "messages", "turns"))
            if "turns" in dialogue:
                items = [(index, None, user, None, {}) for index, user in enumerate(dialogue["turns"])]
            else:
                messages = dialogue["messages"]
                if self.pipeline.response_mode == "generate":
                    if messages[-1]["role"] != "user":
                        raise ValueError("generate mode requires messages ending in user")
                    history = [{"role": m["role"], "content": m["content"]} for m in messages[:-1]]
                    index = sum(m["role"] == "user" for m in messages) - 1
                    items = [(index, len(messages) - 1, messages[-1]["content"], None,
                              {"user": metadata(messages[-1], ("role", "content"))})]
                else:
                    if messages[0]["role"] == "system":
                        history.append({"role": "system", "content": messages[0]["content"]})
                    items = []
                    for message_index, message in enumerate(messages):
                        if message["role"] != "user":
                            continue
                        response = messages[message_index + 1] if message_index + 1 < len(messages) else None
                        items.append((len(items), message_index, message["content"],
                                      response["content"] if response else None,
                                      {"user": metadata(message, ("role", "content")),
                                       "assistant": metadata(response, ("role", "content")) if response else {}}))
            for index, message_index, user, response, message_metadata in items:
                result = await self.run_turn(dialogue["dialogue_id"], history, user, index,
                                             recorded_response=response,
                                             input_metadata={"dialogue": info, **message_metadata},
                                             message_index=message_index)
                turns.append(result)
                if result["error"]:
                    break
                history.extend([
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": result["final"]},
                ])
            return turns

    async def run(self, dialogues: list[dict] | dict) -> list[dict]:
        if isinstance(dialogues, dict):
            dialogues = [dialogues]
        dialogues = validate_dialogues(dialogues)
        if self.pipeline.response_mode == "generate":
            for dialogue in dialogues:
                if "messages" in dialogue and dialogue["messages"][-1]["role"] != "user":
                    raise ValueError("generate mode requires messages ending in user")
        results = await asyncio.gather(*(self._dialogue(dialogue) for dialogue in dialogues))
        return [turn for dialogue_turns in results for turn in dialogue_turns]
