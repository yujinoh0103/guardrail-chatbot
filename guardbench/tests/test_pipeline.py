import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from guardbench.core.node import Node, register
from guardbench.core.pipeline import Pipeline
from guardbench.core.runner import Runner, load_dialogues
from guardbench.core.state import TurnState
from guardbench.trace.recorder import Recorder

ROOT = Path(__file__).resolve().parents[1]


@register("chatbot/test_echo")
class TestEcho(Node):
    __test__ = False

    def __init__(self, sleep=0.0, error_keyword=None):
        super().__init__(sleep)
        self.error_keyword = error_keyword

    async def run(self, inputs):
        await self.delay()
        if self.error_keyword and self.error_keyword in inputs["user"]:
            raise RuntimeError("Test node failure")
        return f"mock: {inputs['user']} | history_messages={len(inputs['history'])} | feedback_count={int(inputs.get('fb') is not None)}"


@register("misc/test_probe")
class Probe(Node):
    async def run(self, inputs):
        await self.delay()
        return inputs


@register("misc/test_mutate")
class Mutate(Node):
    async def run(self, inputs):
        inputs["history"].append({"role": "user", "content": "mutation"})
        inputs["calls"]["invented"] = 99
        return "changed input snapshot"


@register("routers/test_target")
class Target(Node):
    def __init__(self, target="END", **kwargs):
        super().__init__(**kwargs)
        self.target = target

    async def run(self, inputs):
        return self.target


def baseline():
    return {
        "nodes": {"bot": {"use": "chatbot/test_echo", "in": ["history", "user", "fb?"], "out": "candidate"}},
        "flow": ["bot"], "final": "candidate",
    }


def execute(tmp_path, config, dialogues=None, concurrency=4):
    pipeline = Pipeline.from_dict(config) if isinstance(config, dict) else Pipeline.load(config)
    folder = tmp_path / "run"

    async def run():
        with Recorder(folder) as recorder:
            return await Runner(pipeline, recorder, concurrency).run(
                dialogues or [{"dialogue_id": "test", "turns": ["위험 테스트"]}],
            )

    results = asyncio.run(run())
    traces = [json.loads(line) for line in (folder / "trace.jsonl").read_text().splitlines()]
    turns = [json.loads(line) for line in (folder / "turns.jsonl").read_text().splitlines()]
    return results, traces, turns


def test_parallel_wall_time(tmp_path):
    config = {
        "nodes": {
            "a": {"use": "misc/fallback", "in": [], "out": "a", "params": {"sleep": 0.2}},
            "b": {"use": "misc/fallback", "in": [], "out": "b", "params": {"sleep": 0.2}},
        }, "flow": [["a", "b"]], "final": "b",
    }
    results, traces, _ = execute(tmp_path, config)
    group = next(t for t in traces if t["event"] == "parallel_group")
    assert 0.19 <= group["latency"] < 0.3
    assert results[0]["step_count"] == 1
    assert results[0]["calls"] == {"a": 1, "b": 1}


def test_goto_runs_only_target(tmp_path):
    results, traces, _ = execute(tmp_path, ROOT / "configs/pipelines/parallel_loop.yaml")
    turn = results[0]
    assert turn["status"] == "ok"
    assert turn["calls"] == {"prepare": 1, "g11": 1, "bot": 3, "g12": 3, "route": 3}
    assert turn["step_count"] == 10
    assert "feedback_count=2" in turn["final"]
    assert len([t for t in traces if t["event"] == "parallel_group"]) == 1
    assert [t["output"] for t in traces if t.get("node") == "route"] == ["bot", "bot", "END"]


def test_max_steps_records_error_and_stops_dialogue(tmp_path):
    config = baseline()
    config["nodes"]["loop"] = {"use": "routers/test_target", "in": [], "out": "next", "type": "router", "params": {"target": "bot"}}
    config["flow"].append("loop")
    config["limits"] = {"max_steps": 3}
    results, _, turns = execute(tmp_path, config, [{"dialogue_id": "test", "turns": ["first", "second"]}])
    assert len(results) == 1
    assert turns[0]["status"] == "error"
    assert turns[0]["error"]["type"] == "StepLimitError"
    assert turns[0]["step_count"] == 3
    assert turns[0]["calls"] == {"bot": 2, "loop": 1}
    assert turns[0]["final"] is None


def test_exact_step_limit_can_finish(tmp_path):
    config = baseline()
    config["limits"] = {"max_steps": 1}
    results, _, _ = execute(tmp_path, config)
    assert results[0]["status"] == "ok"


def test_optional_missing_input_is_none(tmp_path):
    results, traces, _ = execute(tmp_path, baseline())
    assert traces[0]["inputs"] == {"history": [], "user": "위험 테스트", "fb": None}
    assert traces[0]["input_keys"] == ["history", "user", "fb"]
    assert "feedback_count=0" in results[0]["final"]


@pytest.mark.parametrize("case", ["missing_node", "unknown_use", "missing_input", "parallel_output", "nonrouter_next", "missing_final", "parallel_input"])
def test_required_load_validation(case):
    config = baseline()
    if case == "missing_node":
        config["flow"] = ["missing"]
    elif case == "unknown_use":
        config["nodes"]["bot"]["use"] = "chatbot/unknown"
    elif case == "missing_input":
        config["nodes"]["bot"]["in"] = ["history", "user", "fb"]
    elif case == "nonrouter_next":
        config["nodes"]["bot"]["out"] = "next"
        config["final"] = "next"
    elif case == "missing_final":
        config["final"] = "missing"
    else:
        config["nodes"]["other"] = {"use": "misc/fallback", "in": [], "out": "candidate" if case == "parallel_output" else "other"}
        config["flow"] = [["bot", "other"]]
        if case == "parallel_input":
            config["nodes"]["other"]["in"] = ["candidate"]
    with pytest.raises(ValueError):
        Pipeline.from_dict(config)


@pytest.mark.parametrize("use", ["unknown/mock", "invalid", "chatbot/mock_typo"])
def test_unregistered_use_is_config_error(use):
    config = baseline()
    config["nodes"]["bot"]["use"] = use
    with pytest.raises(ValueError, match="Unregistered"):
        Pipeline.from_dict(config)


@pytest.mark.parametrize("target", ["missing", None, ["bot"]])
def test_invalid_router_target_records_error(tmp_path, target):
    config = baseline()
    config["nodes"]["route"] = {"use": "routers/test_target", "in": [], "out": "next", "type": "router", "params": {"target": target}}
    config["flow"].append("route")
    results, traces, _ = execute(tmp_path, config)
    assert results[0]["error"]["type"] == "ValueError"
    assert traces[-1]["output"] == target
    assert traces[-1]["error"] is not None


def test_multi_turn_history_and_state_reset(tmp_path):
    results, traces, _ = execute(tmp_path, ROOT / "configs/pipelines/parallel_loop.yaml", [{"dialogue_id": "test", "turns": ["위험", "hello"]}])
    second = next(t for t in traces if t.get("node") == "bot" and t["turn_index"] == 1)
    assert second["inputs"]["chat_context"]["messages"][:-1] == [
        {"role": "user", "content": "위험"}, {"role": "assistant", "content": results[0]["final"]},
    ]
    assert second["inputs"]["fb"] is None
    assert "feedback_count=0" in results[1]["final"]
    assert results[1]["calls"]["bot"] == 1


def test_dialogue_exception_isolation(tmp_path):
    config = baseline()
    config["nodes"]["bot"]["params"] = {"error_keyword": "fail", "sleep": 0.01}
    dialogues = [
        {"dialogue_id": "bad", "turns": ["hello", "fail", "never reached"]},
        {"dialogue_id": "good", "turns": ["hello", "world"]},
    ]
    results, traces, _ = execute(tmp_path, config, dialogues, concurrency=2)
    assert [(t["dialogue_id"], t["turn_index"], t["status"]) for t in results] == [
        ("bad", 0, "ok"), ("bad", 1, "error"), ("good", 0, "ok"), ("good", 1, "ok"),
    ]
    error = next(t for t in traces if t["error"])
    assert error["node"] == "bot" and error["error"]["type"] == "RuntimeError"


def test_trace_required_fields(tmp_path):
    _, traces, turns = execute(tmp_path, ROOT / "configs/pipelines/parallel_loop.yaml")
    common = {"dialogue_id", "turn_index", "step_count", "started_at", "ended_at", "latency", "error"}
    for event in traces:
        assert common <= event.keys()
        assert event["latency"] >= 0
        if event["event"] == "node":
            assert {"node", "use", "input_keys", "inputs", "output"} <= event.keys()
        else:
            assert event["event"] == "parallel_group" and "nodes" in event
    assert common | {"final", "calls", "status"} <= turns[0].keys()


def test_parallel_failure_waits_for_sibling_and_records_group(tmp_path):
    config = baseline()
    config["nodes"]["bot"]["params"] = {"error_keyword": "위험"}
    config["nodes"]["sibling"] = {"use": "misc/fallback", "in": [], "out": "sibling", "params": {"sleep": 0.04}}
    config["flow"] = [["bot", "sibling"]]
    results, traces, _ = execute(tmp_path, config)
    assert results[0]["status"] == "error"
    assert {t["node"] for t in traces if t["event"] == "node"} == {"bot", "sibling"}
    assert traces[-1]["event"] == "parallel_group" and traces[-1]["error"]
    assert traces[-1]["latency"] >= 0.035


def test_inputs_are_isolated_from_mutation(tmp_path):
    config = baseline()
    config["nodes"]["mutator"] = {"use": "misc/test_mutate", "in": ["history", "calls"], "out": "mutation"}
    config["nodes"]["probe"] = {"use": "misc/test_probe", "in": ["history", "calls"], "out": "snapshot"}
    config["flow"] = [["mutator", "probe"], "bot"]
    results, traces, _ = execute(tmp_path, config)
    snapshot = next(t["output"] for t in traces if t.get("node") == "probe")
    assert snapshot["history"] == []
    assert "invented" not in snapshot["calls"]
    assert "invented" not in results[0]["calls"]
    assert "history_messages=0" in results[0]["final"]
    assert next(t for t in traces if t.get("node") == "mutator")["inputs"]["history"] == []


def test_singleton_parallel_group_has_event(tmp_path):
    config = baseline()
    config["flow"] = [["bot"]]
    _, traces, _ = execute(tmp_path, config)
    assert [t["event"] for t in traces] == ["node", "parallel_group"]


def test_dialogue_concurrency_bound(tmp_path):
    config = baseline()
    config["nodes"]["bot"]["params"] = {"sleep": 0.05}
    dialogues = [{"dialogue_id": str(i), "turns": ["hello"]} for i in range(4)]
    start = time.perf_counter()
    _, traces, _ = execute(tmp_path, config, dialogues, concurrency=2)
    assert time.perf_counter() - start >= 0.09
    events = []
    for record in traces:
        events.extend([(record["started_at"], 1), (record["ended_at"], -1)])
    active, peak = 0, 0
    for _, delta in sorted(events):
        active += delta
        peak = max(peak, active)
    assert peak == 2


@pytest.mark.parametrize("filename", ["no_guard", "parallel_loop", "parallel_fallback", "gate_first"])
def test_example_pipelines(tmp_path, filename):
    dialogues = load_dialogues(ROOT / "data/sample.jsonl")
    results, _, _ = execute(tmp_path, ROOT / f"configs/pipelines/{filename}.yaml", dialogues)
    assert len(results) == 8 and all(t["status"] == "ok" for t in results)
    for turn in results:
        unsafe = "위험" in turn["user"]
        if filename == "parallel_loop":
            assert turn["calls"]["g11"] == 1
            assert turn["calls"]["bot"] == (3 if unsafe else 1)
        if filename in ("parallel_fallback", "gate_first"):
            assert turn["calls"].get("fallback", 0) == int(unsafe)
        if filename == "gate_first":
            assert turn["calls"].get("bot", 0) == int(not unsafe)


@pytest.mark.parametrize("change", ["duplicate", "reserved", "router_parallel", "unused", "bad_params", "zero_limit"])
def test_additional_load_validation(change):
    config = baseline()
    if change == "duplicate":
        config["flow"] = ["bot", "bot"]
    elif change == "reserved":
        config["nodes"]["bot"]["out"] = "calls"
    elif change == "router_parallel":
        config["nodes"]["bot"]["type"] = "router"
        config["flow"] = [["bot"]]
    elif change == "unused":
        config["nodes"]["unused"] = deepcopy(config["nodes"]["bot"])
    elif change == "bad_params":
        config["nodes"]["bot"]["params"] = {"sleep": -1}
    else:
        config["limits"] = {"max_steps": 0}
    with pytest.raises(ValueError):
        Pipeline.from_dict(config)


def test_neutral_guard_and_arbitrary_feedback(tmp_path):
    config = baseline()
    config["nodes"]["g11"] = {"use": "guard_in/none", "in": ["user"], "out": "g11", "params": {"sleep": 0.001}}
    config["nodes"]["g12"] = {"use": "guard_out/mock", "in": ["history", "candidate", "g11"], "out": "fb", "params": {"mode": "constant", "feedback": {"custom": {"nested": [1, 2]}}}}
    config["flow"] = ["g11", "bot", "g12"]
    _, traces, _ = execute(tmp_path, config)
    assert traces[0]["output"] == {"label": "safe", "categories": [], "raw": None}
    assert traces[-1]["output"] == {"custom": {"nested": [1, 2]}}


def test_goto_missing_runtime_input_is_turn_error(tmp_path):
    config = baseline()
    config["nodes"]["route"] = {"use": "routers/test_target", "in": [], "out": "next", "type": "router", "params": {"target": "reader"}}
    config["nodes"]["reader"] = {"use": "misc/test_probe", "in": ["candidate"], "out": "read"}
    config["flow"] = ["route", "bot", "reader"]
    results, traces, _ = execute(tmp_path, config)
    assert results[0]["error"]["type"] == "KeyError"
    assert traces[-1]["node"] == "reader" and traces[-1]["error"]


def test_final_missing_on_route_end_is_turn_error(tmp_path):
    config = baseline()
    config["nodes"]["route"] = {"use": "routers/test_target", "in": [], "out": "next", "type": "router"}
    config["flow"] = ["route", "bot"]
    results, _, _ = execute(tmp_path, config)
    assert results[0]["error"]["type"] == "KeyError"


def test_state_protects_metadata_and_required_keys():
    state = TurnState([], "hello")
    with pytest.raises(ValueError):
        state.store("step_count", 99)
    with pytest.raises(KeyError):
        state.inputs(("missing",))
    assert state.inputs(("missing?",)) == {"missing": None}


def test_recorder_protects_existing_results(tmp_path):
    with Recorder(tmp_path) as recorder:
        recorder.write({"event": "original"})
    with pytest.raises(FileExistsError):
        Recorder(tmp_path)
    assert json.loads((tmp_path / "trace.jsonl").read_text()) == {"event": "original"}


@pytest.mark.parametrize("line", ['{"dialogue_id":"x","turns":[1]}', '{"turns":["hello"]}', 'invalid json'])
def test_invalid_data_reports_line(tmp_path, line):
    path = tmp_path / "data.jsonl"
    path.write_text(line + "\n")
    with pytest.raises(ValueError, match="line 1"):
        load_dialogues(path)


def test_cli_uses_mock_pipeline(tmp_path):
    result = subprocess.run([
        sys.executable, str(ROOT / "scripts/run.py"), "--pipeline", str(ROOT / "configs/pipelines/no_guard.yaml"),
        "--data", str(ROOT / "data/sample.jsonl"), "--out", str(tmp_path / "cli"),
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "turns=8 errors=0" in result.stdout


def test_source_and_config_terminology():
    expression = r"\b(" + "pa" + "ss|" + "re" + "vise)\b"
    result = subprocess.run(["rg", "-i", expression, "src", "configs"], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 1, result.stdout + result.stderr
