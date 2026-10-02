import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest

from guardbench.core.pipeline import Pipeline
from guardbench.core.runner import Runner
from guardbench.nodes.misc.chat_context import ChatContext
from guardbench.trace.recorder import Recorder

ROOT = Path(__file__).resolve().parents[1]


def test_history_compaction_is_bounded_and_preserves_recent_messages():
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"message-{i}: " + "x" * 1000} for i in range(100)]
    original = deepcopy(history)
    node = ChatContext(max_chars=2000, recent_messages=2, summary_chars=300, excerpt_chars=80)
    result = asyncio.run(node.run({"history": history, "user": "current user"}))
    assert node.size(result["summary"], result["messages"]) <= 2000
    assert len(result["messages"]) <= 3
    assert result["messages"][-1] == {"role": "user", "content": "current user"}
    assert result["summary"] and len(result["summary"]) <= 300
    assert "message-" in result["summary"]
    assert history == original


def test_system_and_current_user_are_not_silently_clipped():
    node = ChatContext(max_chars=500)
    result = asyncio.run(node.run({"history": [{"role": "system", "content": "instruction"}], "user": "original user"}))
    assert result["messages"] == [{"role": "system", "content": "instruction"}, {"role": "user", "content": "original user"}]
    with pytest.raises(ValueError, match="exceed"):
        asyncio.run(node.run({"history": [], "user": "x" * 501}))
    with pytest.raises(ValueError, match="exceed"):
        asyncio.run(node.run({"history": [{"role": "system", "content": "x" * 501}], "user": "hello"}))


def test_zero_recent_history_can_use_excerpts():
    node = ChatContext(recent_messages=0)
    result = asyncio.run(node.run({"history": [{"role": "user", "content": "old preference"}], "user": "current"}))
    assert result["messages"] == [{"role": "user", "content": "current"}]
    assert result["summary"] == "user: old preference"


@pytest.mark.parametrize("filename", ["no_guard", "parallel_loop", "parallel_fallback", "gate_first"])
def test_yaml_chatbot_only_receives_prepared_context(tmp_path, filename):
    pipeline = Pipeline.load(ROOT / f"configs/pipelines/{filename}.yaml")
    assert pipeline.nodes["bot"].inputs == ("chat_context", "fb?")
    messages = [
        {"role": role, "content": f"{role}-{turn}: " + "x" * 2000}
        for turn in range(30) for role in ("user", "assistant")
    ] + [{"role": "user", "content": "current safe user"}]

    async def execute():
        with Recorder(tmp_path) as recorder:
            return await Runner(pipeline, recorder).run({"dialogue_id": "long", "messages": messages})

    results = asyncio.run(execute())
    assert results[0]["status"] == "ok"
    assert len(results[0]["history"]) == 60
    assert results[0]["context"] == messages[-5:]
    traces = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    bot = next(t for t in traces if t.get("node") == "bot")
    prepared = bot["inputs"]["chat_context"]
    assert "history" not in bot["inputs"] and "user" not in bot["inputs"]
    assert ChatContext.size(prepared["summary"], prepared["messages"]) <= 8192
    assert len(prepared["messages"]) <= 5
    if filename != "no_guard":
        guard = next(t for t in traces if t.get("node") == "g11")
        assert guard["inputs"]["context"] == messages[-5:]
        assert "x" * 2000 in guard["inputs"]["context"][0]["content"]


def test_gate_unsafe_skips_chatbot_preparation(tmp_path):
    pipeline = Pipeline.load(ROOT / "configs/pipelines/gate_first.yaml")

    async def execute():
        with Recorder(tmp_path) as recorder:
            return await Runner(pipeline, recorder).run({"dialogue_id": "gate", "turns": ["위험"]})

    result = asyncio.run(execute())[0]
    assert result["status"] == "ok"
    assert "prepare" not in result["calls"] and "bot" not in result["calls"]
