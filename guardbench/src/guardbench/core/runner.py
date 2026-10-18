"""Backend-independent asyncio execution of pipelines and dialogues."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import time

from .data import load_dialogues, metadata, validate_dialogues
from .pipeline import Pipeline
from .state import TurnState
from ..trace.recorder import Recorder, error_info, timestamp
from ..llm.client import REQUEST_AUDIT


class StepLimitError(RuntimeError):
    """The configured turn step budget is exhausted."""


class Runner:
    def __init__(self, pipeline: Pipeline, recorder: Recorder, concurrency: int = 4,
                 *, resume_from: str | Path | None = None, node_cache=None, cache_nodes=()):
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency <= 0:
            raise ValueError("concurrency must be a positive integer")
        self.pipeline, self.recorder = pipeline, recorder
        self.semaphore = asyncio.Semaphore(concurrency)
        self.resume_from = str(Path(resume_from).resolve()) if resume_from is not None else None
        self.completed, self.saved_nodes = {}, {}
        self.node_cache, self.cache_nodes = node_cache, frozenset(cache_nodes)
        if resume_from is not None:
            for record in self._saved_records(Path(resume_from) / "turns.jsonl"):
                key = (record["dialogue_id"], record["turn_index"])
                self.completed[key] = record
            for record in self._saved_records(Path(resume_from) / "trace.jsonl"):
                if record.get("event") == "node":
                    key = (record["dialogue_id"], record["turn_index"], record["step_count"], record["node"])
                    self.saved_nodes[key] = record

    @staticmethod
    def _saved_records(path):
        # An interrupted write can leave an incomplete last line; earlier events remain usable.
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    if not line.endswith("\n"):
                        break
                    raise

    async def _invoke(self, name, instance, state, dialogue_id):
        spec = self.pipeline.nodes[name]
        record = {
            "event": "node", "dialogue_id": dialogue_id,
            "turn_index": state["turn_index"], "step_count": state["step_count"],
            "node": name, "use": spec.use,
            "params": deepcopy(spec.params),
            "input_keys": [key.removesuffix("?") for key in spec.inputs], "inputs": {},
            "output": None, "started_at": timestamp(), "ended_at": None,
            "latency": None, "error": None,
        }
        start = time.perf_counter()
        audit = []
        audit_token = REQUEST_AUDIT.set(audit)
        try:
            inputs = state.inputs(spec.inputs)
            record["input_keys"] = list(inputs)
            # Preserve the snapshot in the trace even if a node mutates its inputs.
            record["inputs"] = state.inputs(spec.inputs)
            saved = self.saved_nodes.get((dialogue_id, state["turn_index"], state["step_count"], name))
            if (saved is not None and saved.get("error") is None
                    and saved.get("output") is not None
                    and saved.get("use") == spec.use and saved.get("inputs") == inputs
                    and saved.get("params", spec.params) == spec.params):
                result = deepcopy(saved["output"])
                record["reused_from"] = self.resume_from
                for field in ("model_request", "model_requests"):
                    if field in saved:
                        record[field] = deepcopy(saved[field])
            else:
                if self.node_cache is not None and name in self.cache_nodes:
                    cache_key = json.dumps([spec.use, spec.params, inputs], sort_keys=True, ensure_ascii=False)
                    cached = self.node_cache.get(cache_key)
                    if cached is None:
                        async def generate():
                            output = await instance.run(inputs)
                            requests = {field: deepcopy(getattr(instance, field)) for field in
                                        ("model_request", "model_requests") if hasattr(instance, field)}
                            return output, requests
                        cached = asyncio.create_task(generate())
                        self.node_cache[cache_key] = cached
                    else:
                        record["reused_in_run"] = True
                    try:
                        output, requests = await cached
                    except BaseException:
                        if self.node_cache.get(cache_key) is cached:
                            del self.node_cache[cache_key]
                        raise
                    result = deepcopy(output)
                    for field, value in requests.items():
                        setattr(instance, field, deepcopy(value))
                else:
                    result = await instance.run(inputs)
            record["output"] = result
            if spec.router and (not isinstance(result, str) or (result != "END" and result not in self.pipeline.positions)):
                raise ValueError(f"Router {name} returned an unknown target: {result!r}")
            return result
        except asyncio.CancelledError as exc:
            record["error"] = error_info(exc)
            raise
        except Exception as exc:
            record["error"] = error_info(exc)
            raise
        finally:
            REQUEST_AUDIT.reset(audit_token)
            if audit:
                record["request_audit"] = deepcopy(audit)
            for field in ("model_request", "model_requests"):
                if hasattr(instance, field):
                    record[field] = deepcopy(getattr(instance, field))
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
                if self.pipeline.response_mode == "generate" and not self.pipeline.replay_users:
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
                        message_metadata = {"user": metadata(message, ("role", "content"))}
                        if self.pipeline.replay_users:
                            message_metadata["reference_assistant"] = response
                        else:
                            message_metadata["assistant"] = metadata(response, ("role", "content")) if response else {}
                        items.append((len(items), message_index, message["content"],
                                      response["content"] if response and not self.pipeline.replay_users else None,
                                      message_metadata))
            for index, message_index, user, response, message_metadata in items:
                saved = self.completed.get((dialogue["dialogue_id"], index))
                expected_context = TurnState(history, user, index,
                                             context_messages=self.pipeline.context_messages)["context"]
                if (saved is not None and saved.get("error") is None and saved.get("status") == "ok"
                        and isinstance(saved.get("final"), str) and saved.get("history") == history
                        and saved.get("user") == user and saved.get("context") == expected_context
                        and saved.get("response_mode") == self.pipeline.response_mode
                        and saved.get("recorded_response") == response
                        and saved.get("message_index") == message_index):
                    result = deepcopy(saved)
                    result["reused_from"] = self.resume_from
                    # Carry successful node events forward so another resume remains possible.
                    for (saved_id, saved_index, _, _), node_record in self.saved_nodes.items():
                        if saved_id == dialogue["dialogue_id"] and saved_index == index:
                            copied = deepcopy(node_record)
                            copied["reused_from"] = self.resume_from
                            self.recorder.write(copied)
                    self.recorder.write(result, turn=True)
                else:
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

    async def _simulate(self, simulator, persona, dialogue_id, turn_index, history, state):
        """One simulator call, recorded as a trace event; returns None on failure."""
        phase = "open" if state is None else "after_reply"
        record = {"event": "user_sim", "dialogue_id": dialogue_id, "turn_index": turn_index,
                  "phase": phase, "previous_state": deepcopy(state), "output": None,
                  "started_at": timestamp(), "ended_at": None, "latency": None, "error": None}
        start = time.perf_counter()
        try:
            result = await simulator.step(persona, history, state, trace=record)
            record["output"] = {key: deepcopy(value) for key, value in result.items() if key != "raw"}
            record["raw"] = result["raw"]
            return result
        except Exception as exc:
            record["error"] = error_info(exc)
            return None
        finally:
            record["ended_at"] = timestamp()
            record["latency"] = time.perf_counter() - start
            self.recorder.write(record)

    async def _simulated_dialogue(self, persona, simulator, max_turns):
        async with self.semaphore:
            dialogue_id, history, turns = persona["dialogue_id"], [], []
            info = metadata(persona, ("dialogue_id", "persona_context", "backstory", "style_examples"))
            step = await self._simulate(simulator, persona, dialogue_id, 0, history, None)
            for index in range(max_turns):
                if step is None:
                    break
                sim = {key: step[key] for key in ("appraisal", "inner_thought", "action", "end_conversation", "adjustments")}
                result = await self.run_turn(dialogue_id, history, step["utterance"], index,
                                             input_metadata={"dialogue": info, "user_state": step["state"],
                                                             "user_sim": sim})
                turns.append(result)
                if result["error"]:
                    break
                history.extend([
                    {"role": "user", "content": step["utterance"]},
                    {"role": "assistant", "content": result["final"]},
                ])
                # Always appraise the last reply so the final state reflects the whole dialogue.
                following = await self._simulate(simulator, persona, dialogue_id, index + 1, history, step["state"])
                if step["end_conversation"]:
                    break
                step = following
            return turns

    async def run_simulated(self, personas: list[dict], simulator, max_turns: int) -> list[dict]:
        """Converse with a user simulator instead of replaying stored user messages."""
        if self.pipeline.response_mode != "generate" or self.pipeline.replay_users:
            raise ValueError("Simulated users require response_mode generate without replay_users")
        if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns <= 0:
            raise ValueError("max_turns must be a positive integer")
        if self.resume_from is not None:
            raise ValueError("Simulated dialogues cannot be resumed; the user depends on each reply")
        ids = [persona["dialogue_id"] for persona in personas]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("Provide personas with unique dialogue_id values")
        results = await asyncio.gather(*(self._simulated_dialogue(p, simulator, max_turns) for p in personas))
        return [turn for dialogue_turns in results for turn in dialogue_turns]

    async def run(self, dialogues: list[dict] | dict) -> list[dict]:
        if isinstance(dialogues, dict):
            dialogues = [dialogues]
        dialogues = validate_dialogues(dialogues)
        if self.pipeline.response_mode == "generate" and not self.pipeline.replay_users:
            for dialogue in dialogues:
                if "messages" in dialogue and dialogue["messages"][-1]["role"] != "user":
                    raise ValueError("generate mode requires messages ending in user")
        results = await asyncio.gather(*(self._dialogue(dialogue) for dialogue in dialogues))
        return [turn for dialogue_turns in results for turn in dialogue_turns]
