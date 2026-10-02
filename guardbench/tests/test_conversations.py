import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import runpy

import pytest

from guardbench.core.pipeline import Pipeline
from guardbench.core.runner import Runner, load_dialogues
from guardbench.trace.recorder import Recorder

ROOT = Path(__file__).resolve().parents[1]


def conversation():
    return {
        "dialogue_id": "recorded", "native": {"label": "unresolved"},
        "context_label": "unsafe", "split": "test",
        "messages": [
            {"role": role, "content": f"{role}-{turn}", "annotation": {"tag": turn}}
            for turn in range(4) for role in ("user", "assistant")
        ],
    }


def recorded_config():
    return {
        "input": {"response_mode": "recorded", "context_messages": 5},
        "nodes": {"source": {"use": "misc/recorded_response", "in": ["recorded_response"], "out": "candidate"}},
        "flow": ["source"], "final": "candidate",
    }


def run(tmp_path, config, data):
    async def execute():
        with Recorder(tmp_path / "run") as recorder:
            return await Runner(Pipeline.from_dict(config), recorder).run(data)
    results = asyncio.run(execute())
    traces = [json.loads(line) for line in (tmp_path / "run/trace.jsonl").read_text().splitlines()]
    return results, traces


def test_single_conversation_recorded_windows_and_history(tmp_path):
    data = conversation()
    original = deepcopy(data)
    results, traces = run(tmp_path, recorded_config(), data)
    assert len(results) == 4 and all(t["status"] == "ok" for t in results)
    assert [t["final"] for t in results] == [f"assistant-{i}" for i in range(4)]
    assert [len(t["context"]) for t in results] == [1, 3, 5, 5]
    assert [len(t["history"]) for t in results] == [0, 2, 4, 6]
    assert results[-1]["context"] == [
        {"role": "user", "content": "user-1"},
        {"role": "assistant", "content": "assistant-1"},
        {"role": "user", "content": "user-2"},
        {"role": "assistant", "content": "assistant-2"},
        {"role": "user", "content": "user-3"},
    ]
    assert results[0]["context"] == [{"role": "user", "content": "user-0"}]
    assert results[-1]["message_index"] == 6
    assert results[-1]["metadata"]["dialogue"]["native"] == {"label": "unresolved"}
    assert results[-1]["metadata"]["user"] == {"annotation": {"tag": 3}}
    assert results[-1]["outputs"] == {}
    assert all(t["input_keys"] == ["recorded_response"] for t in traces)
    assert data == original


def test_labels_are_separate_and_not_binarized(tmp_path):
    config = recorded_config()
    for name, label in (("context_label", "unresolved"), ("chatbot_utterance_label", "unsafe")):
        config["nodes"][name] = {"use": "misc/fallback", "in": ["context"], "out": name, "params": {"text": label}}
    config["flow"].append(["context_label", "chatbot_utterance_label"])
    config["outputs"] = ["context_label", "chatbot_utterance_label"]
    results, traces = run(tmp_path, config, conversation())
    assert all(t["outputs"] == {"context_label": "unresolved", "chatbot_utterance_label": "unsafe"} for t in results)
    assert results[0]["metadata"]["dialogue"]["context_label"] == "unsafe"
    assert results[0]["outputs"]["context_label"] == "unresolved"
    for trace in traces:
        if trace["event"] == "node" and trace["node"] == "context_label":
            assert list(trace["inputs"]) == ["context"]
            assert all(set(m) == {"role", "content"} for m in trace["inputs"]["context"])


def test_generation_uses_prior_conversation_without_replaying_it(tmp_path):
    import yaml
    config = yaml.safe_load((ROOT / "configs/pipelines/no_guard.yaml").read_text())
    data = conversation()
    data["messages"].pop()
    results, traces = run(tmp_path, config, data)
    assert len(results) == 1 and results[0]["turn_index"] == 3
    assert results[0]["calls"] == {"prepare": 1, "bot": 1}
    assert len(results[0]["history"]) == 6
    bot = next(t for t in traces if t.get("node") == "bot")
    assert len(bot["inputs"]["chat_context"]["messages"]) == 5
    assert bot["inputs"]["chat_context"]["messages"][-1]["content"] == "user-3"
    assert "history_messages=4" in results[0]["final"]
    assert results[0]["recorded_response"] is None
    assert results[0]["metadata"]["dialogue"]["native"] == {"label": "unresolved"}


def test_system_message_is_history_but_not_risk_context(tmp_path):
    data = conversation()
    data["messages"].insert(0, {"role": "system", "content": "System instruction"})
    results, _ = run(tmp_path, recorded_config(), data)
    assert results[0]["history"] == [{"role": "system", "content": "System instruction"}]
    assert all(m["role"] != "system" for t in results for m in t["context"])


def test_window_size_configurable(tmp_path):
    config = recorded_config()
    config["input"]["context_messages"] = 2
    results, _ = run(tmp_path, config, conversation())
    assert [len(t["context"]) for t in results] == [1, 2, 2, 2]


def test_missing_recorded_response_isolated_turn_error(tmp_path):
    bad = conversation()
    bad["messages"].pop()
    good = conversation()
    good["dialogue_id"] = "good"
    results, _ = run(tmp_path, recorded_config(), [bad, good])
    bad_turns = [t for t in results if t["dialogue_id"] == "recorded"]
    assert len(bad_turns) == 4 and bad_turns[-1]["status"] == "error"
    assert bad_turns[-1]["calls"] == {}
    assert all(t["status"] == "ok" for t in results if t["dialogue_id"] == "good")


def test_recorded_mode_prevents_response_replacement(tmp_path):
    config = recorded_config()
    config["nodes"]["source"] = {"use": "misc/fallback", "in": [], "out": "candidate", "params": {"text": "replacement"}}
    results, _ = run(tmp_path, config, conversation())
    assert len(results) == 1 and results[0]["status"] == "error"
    assert results[0]["final"] is None


@pytest.mark.parametrize("messages", [[], [{"role": "assistant", "content": "a"}], [{"role": "user", "content": 1}], [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]])
def test_invalid_message_format(tmp_path, messages):
    path = tmp_path / "conversation.json"
    path.write_text(json.dumps({"dialogue_id": "test", "messages": messages}))
    with pytest.raises(ValueError):
        load_dialogues(path)


def test_json_and_jsonl_preserve_same_conversation(tmp_path):
    data = conversation()
    for suffix in ("json", "jsonl"):
        path = tmp_path / f"conversation.{suffix}"
        path.write_text(json.dumps(data) + "\n")
        assert load_dialogues(path) == [data]


@pytest.mark.parametrize("option,value", [("response_mode", "unknown"), ("context_messages", 0), ("context_messages", True)])
def test_invalid_input_options(option, value):
    config = recorded_config()
    config["input"][option] = value
    with pytest.raises(ValueError):
        Pipeline.from_dict(config)


def test_unknown_output_key_rejected():
    config = recorded_config()
    config["outputs"] = ["context_label"]
    with pytest.raises(ValueError):
        Pipeline.from_dict(config)


def test_cli_one_conversation(tmp_path):
    command = [sys.executable, str(ROOT / "scripts/run.py"), "--pipeline", str(ROOT / "configs/pipelines/recorded_dialogue.yaml"), "--data", str(ROOT / "data/conversation.json"), "--out", str(tmp_path / "cli")]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "dialogues=1 turns=4 errors=0" in result.stdout


def test_generate_requires_pending_user(tmp_path):
    config = recorded_config()
    config["input"]["response_mode"] = "generate"
    with pytest.raises(ValueError, match="ending in user"):
        run(tmp_path, config, conversation())


def test_prepare_sessions_preserves_source_rows_and_id_types():
    group = runpy.run_path(str(ROOT / "scripts/prepare_sessions.py"))["group_sessions"]
    rows = [
        {"native": {"session": "a"}, "window": 4, "label": "unresolved"},
        {"native": {"session": 1}, "window": 2},
        {"native": {"session": "a"}, "window": 1},
        {"native": {"session": "1"}, "window": 3},
    ]
    original = deepcopy(rows)
    sessions = group("source", rows, "native.session")
    assert [s["session_id"] for s in sessions] == ["a", 1, "1"]
    assert sessions[0]["records"] == [rows[0], rows[2]]
    assert sum(len(s["records"]) for s in sessions) == len(rows)
    assert all(s["dataset"] == "source" for s in sessions)
    assert rows == original


def test_prepare_sessions_cli_local_input_and_no_overwrite(tmp_path):
    source = tmp_path / "input.jsonl"
    source.write_text('\n'.join(json.dumps(row) for row in [
        {"session_id": "s1", "text": "안녕하세요"},
        {"session_id": "s2", "text": "second"},
        {"session_id": "s1", "text": "third"},
    ]) + '\n', encoding="utf-8")
    output = tmp_path / "sessions.jsonl"
    command = [sys.executable, str(ROOT / "scripts/prepare_sessions.py"),
               "--input", f"example={source}", "--session-key", "example=session_id",
               "--out", str(output)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "rows=3 sessions=2" in result.stdout
    data = output.read_text(encoding="utf-8")
    assert json.loads(data.splitlines()[0])["records"][0]["text"] == "안녕하세요"
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0 and "already exists" in result.stderr
    assert output.read_text(encoding="utf-8") == data


def test_prepare_sessions_rejects_missing_key_without_partial_output(tmp_path):
    good, bad = tmp_path / "good.jsonl", tmp_path / "bad.jsonl"
    good.write_text('{"id": "a"}\n')
    bad.write_text('{"window_id": "b"}\n')
    output = tmp_path / "sessions.jsonl"
    command = [sys.executable, str(ROOT / "scripts/prepare_sessions.py"),
               "--input", f"good={good}", "--input", f"bad={bad}",
               "--session-key", "good=id", "--session-key", "bad=id", "--out", str(output)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0 and "missing session key" in result.stderr
    assert not output.exists()
