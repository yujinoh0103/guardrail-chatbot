import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from guardbench.core.node import Node, register
from guardbench.core.pipeline import Pipeline
from guardbench.core.runner import Runner, load_dialogues
from guardbench.core.session import LiveSession
from guardbench.llm import client
from guardbench.llm.client import Backend
from guardbench.llm.openrouter import OpenRouterClient
from guardbench.trace.recorder import Recorder

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fake_backend(monkeypatch, tmp_path):
    class FakeBackend(Backend):
        calls = []
        closed = 0

        def __init__(self, settings):
            self.settings = settings

        async def generate(self, messages):
            await asyncio.sleep(0)
            self.calls.append({"settings": self.settings, "messages": deepcopy(messages)})
            if messages[-1]["content"] == "backend error":
                raise RuntimeError("Fake backend failure")
            return f"inferred reply {len(self.calls)}"

        async def close(self):
            type(self).closed += 1

    monkeypatch.setitem(client.BACKENDS, "test_fake", FakeBackend)
    models = tmp_path / "models.yaml"
    models.write_text(yaml.safe_dump({"chosen": {"backend": "test_fake", "model_id": "fake-configured-model", "sampling": {"temperature": 0}}}))
    config = yaml.safe_load((ROOT / "configs/pipelines/live_chat.yaml").read_text())
    config["nodes"]["bot"]["params"] = {"model": "chosen", "models_config": str(models)}
    return FakeBackend, config


def read_trace(folder):
    return [json.loads(line) for line in (folder / "trace.jsonl").read_text().splitlines()]


def test_live_input_reaches_real_node_and_accumulates_history(fake_backend, tmp_path):
    fake, config = fake_backend

    async def execute():
        with Recorder(tmp_path / "live") as recorder:
            session = LiveSession(Runner(Pipeline.from_dict(config), recorder), "live-test")
            first = await session.send("first input")
            assert first["final"] == "inferred reply 1"
            assert session.history == [{"role": "user", "content": "first input"}, {"role": "assistant", "content": "inferred reply 1"}]
            second = await session.send("second input")
            return first, second, session.history

    first, second, history = asyncio.run(execute())
    assert first["status"] == second["status"] == "ok"
    assert second["turn_index"] == 1 and len(history) == 4
    assert fake.calls[0]["settings"]["model_id"] == "fake-configured-model"
    assert fake.calls[1]["messages"] == history[:2] + [{"role": "user", "content": "second input"}]
    assert fake.closed == 2
    bot_traces = [t for t in read_trace(tmp_path / "live") if t.get("node") == "bot"]
    assert len(bot_traces) == 2
    assert all(t["use"] == "chatbot/default" for t in bot_traces)
    assert "history" not in bot_traces[1]["inputs"]
    assert second["metadata"] == {"input_path": "live"}


def test_live_backend_error_keeps_history_and_closes_client(fake_backend, tmp_path):
    fake, config = fake_backend

    async def execute():
        with Recorder(tmp_path / "error") as recorder:
            session = LiveSession(Runner(Pipeline.from_dict(config), recorder))
            result = await session.send("backend error")
            assert session.history == [] and session.turn_index == 0
            with pytest.raises(RuntimeError, match="stopped"):
                await session.send("another")
            return result

    result = asyncio.run(execute())
    assert result["status"] == "error" and result["error"]["type"] == "RuntimeError"
    assert fake.closed == 1


def test_live_concurrent_submissions_are_sequential(fake_backend, tmp_path):
    fake, config = fake_backend

    async def execute():
        with Recorder(tmp_path / "concurrent") as recorder:
            session = LiveSession(Runner(Pipeline.from_dict(config), recorder))
            results = await asyncio.gather(session.send("one"), session.send("two"))
            return results

    results = asyncio.run(execute())
    assert [t["turn_index"] for t in results] == [0, 1]
    assert fake.calls[1]["messages"][1]["content"] == "inferred reply 1"


def test_live_history_uses_final_fallback_response(fake_backend, tmp_path):
    fake, config = fake_backend

    async def execute():
        with Recorder(tmp_path / "fallback") as recorder:
            session = LiveSession(Runner(Pipeline.from_dict(config), recorder))
            result = await session.send("위험")
            return result, session.history

    result, history = asyncio.run(execute())
    assert len(fake.calls) == 1 and result["calls"]["fallback"] == 1
    assert result["final"] == "고정된 대체 응답입니다."
    assert history[-1]["content"] == result["final"]


def test_real_chatbot_retries_receive_feedback(fake_backend, tmp_path):
    fake, live_config = fake_backend
    config = yaml.safe_load((ROOT / "configs/pipelines/parallel_loop.yaml").read_text())
    config["nodes"]["bot"] = live_config["nodes"]["bot"]

    async def execute():
        with Recorder(tmp_path / "loop") as recorder:
            return await LiveSession(Runner(Pipeline.from_dict(config), recorder)).send("위험")

    result = asyncio.run(execute())
    assert result["status"] == "ok" and result["calls"]["bot"] == 3
    assert result["calls"]["g11"] == 1 and result["calls"]["prepare"] == 1
    assert fake.closed == 3
    assert fake.calls[0]["messages"] == [{"role": "user", "content": "위험"}]
    assert fake.calls[1]["messages"][-1] == {"role": "user", "content": "위험"}
    assert '"retry_requested": true' in fake.calls[1]["messages"][0]["content"]


@register("guard_in/test_window")
class WindowGuard(Node):
    async def run(self, inputs):
        return {"context_label": "unresolved", "messages_seen": inputs["context"]}


@register("guard_out/test_evaluate")
class ResponseGuard(Node):
    async def run(self, inputs):
        return {"chatbot_utterance_label": "safe", "candidate_seen": inputs["candidate"], "user_seen": inputs["user"]}


def test_two_turn_recorded_jsonl_evaluates_without_generation(monkeypatch, tmp_path):
    def unexpected_generation(settings):
        raise AssertionError("Recorded evaluation invoked a chatbot backend")

    monkeypatch.setattr(client, "create_backend", unexpected_generation)
    import guardbench.nodes.chatbot.default as chatbot_module
    monkeypatch.setattr(chatbot_module, "create_backend", unexpected_generation)
    config = yaml.safe_load((ROOT / "configs/pipelines/recorded_dialogue.yaml").read_text())
    config["nodes"]["g11"] = {"use": "guard_in/test_window", "in": ["context", "user"], "out": "g11"}
    config["nodes"]["g12"] = {"use": "guard_out/test_evaluate", "in": ["context", "user", "candidate", "g11"], "out": "fb"}
    data = {
        "dialogue_id": "stored", "native": {"label": "unresolved"},
        "messages": [
            {"role": "user", "content": "user one", "clinical_tag": "a"},
            {"role": "assistant", "content": "original response one"},
            {"role": "user", "content": "user two"},
            {"role": "assistant", "content": "original response two"},
        ],
    }
    path = tmp_path / "stored.jsonl"
    path.write_text(json.dumps(data) + "\n")

    async def execute():
        with Recorder(tmp_path / "recorded") as recorder:
            return await Runner(Pipeline.from_dict(config), recorder).run(load_dialogues(path))

    results = asyncio.run(execute())
    assert [t["final"] for t in results] == ["original response one", "original response two"]
    assert all(t["status"] == "ok" for t in results)
    assert results[1]["history"] == [{"role": "user", "content": "user one"}, {"role": "assistant", "content": "original response one"}]
    assert results[1]["context"][-1] == {"role": "user", "content": "user two"}
    assert results[0]["metadata"]["user"]["clinical_tag"] == "a"
    assert results[1]["metadata"]["dialogue"]["native"] == {"label": "unresolved"}
    assert [t["outputs"]["fb"]["candidate_seen"] for t in results] == ["original response one", "original response two"]
    assert all(t["outputs"]["g11"]["context_label"] == "unresolved" for t in results)
    traces = read_trace(tmp_path / "recorded")
    assert not any(t.get("use", "").startswith("chatbot/") for t in traces)
    second_guard = next(t for t in traces if t.get("node") == "g12" and t["turn_index"] == 1)
    assert second_guard["inputs"]["candidate"] == "original response two"


def test_recorded_pipeline_rejects_real_chatbot(fake_backend):
    _, config = fake_backend
    config["input"]["response_mode"] = "recorded"
    with pytest.raises(ValueError, match="cannot contain chatbot"):
        Pipeline.from_dict(config)


def test_live_rejects_recorded_pipeline(tmp_path):
    with Recorder(tmp_path) as recorder:
        runner = Runner(Pipeline.load(ROOT / "configs/pipelines/recorded_dialogue.yaml"), recorder)
        with pytest.raises(ValueError, match="generate"):
            LiveSession(runner)


def test_sdk_receives_configured_model_url_sampling_and_messages(monkeypatch):
    received = {}

    class FakeSDK:
        def __init__(self, **kwargs):
            received["client"] = kwargs
            self.chat = SimpleNamespace(completions=self)

        async def create(self, **kwargs):
            received["request"] = kwargs
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="SDK response"))])

        async def close(self):
            received["closed"] = True

    import guardbench.llm.openrouter as module
    monkeypatch.setattr(module, "AsyncOpenAI", FakeSDK)
    monkeypatch.setenv("TEST_CHAT_KEY", "test-only-key")
    monkeypatch.setenv("TEST_CHAT_MODEL", "test/configured-model")
    messages = [{"role": "user", "content": "hello"}]

    async def execute():
        backend = OpenRouterClient({"model_id": "${TEST_CHAT_MODEL}", "base_url": "https://example.invalid/v1", "api_key_env": "TEST_CHAT_KEY", "sampling": {"temperature": 0.2, "max_tokens": 7}})
        try:
            return await backend.generate(messages)
        finally:
            await backend.close()

    assert asyncio.run(execute()) == "SDK response"
    assert received["client"]["base_url"] == "https://example.invalid/v1"
    assert received["request"] == {"model": "test/configured-model", "messages": messages, "temperature": 0.2, "max_tokens": 7}
    assert received["closed"]


def test_empty_model_environment_is_rejected(monkeypatch):
    monkeypatch.setenv("TEST_CHAT_MODEL", "")
    with pytest.raises(ValueError, match="model_id"):
        OpenRouterClient({"model_id": "${TEST_CHAT_MODEL}"})


def test_live_cli_piped_input_with_mock_pipeline(tmp_path):
    result = subprocess.run([
        sys.executable, str(ROOT / "scripts/chat.py"), "--pipeline", str(ROOT / "configs/pipelines/no_guard.yaml"),
        "--out", str(tmp_path / "cli"),
    ], input="hello\nsecond turn\n/quit\n", capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    turns = [json.loads(line) for line in (tmp_path / "cli/turns.jsonl").read_text().splitlines()]
    assert len(turns) == 2
    assert turns[1]["history"][1]["content"] == turns[0]["final"]
    assert "assistant> mock:" in result.stdout


def test_recorded_cli_writes_evaluation_outputs(tmp_path):
    result = subprocess.run([
        sys.executable, str(ROOT / "scripts/run.py"), "--pipeline", str(ROOT / "configs/pipelines/recorded_dialogue.yaml"),
        "--data", str(ROOT / "data/conversation.json"), "--out", str(tmp_path / "cli"),
    ], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    turns = [json.loads(line) for line in (tmp_path / "cli/turns.jsonl").read_text().splitlines()]
    assert len(turns) == 4
    assert all(set(t["outputs"]) == {"g11", "fb"} for t in turns)
    assert all(t["final"] == t["recorded_response"] for t in turns)
