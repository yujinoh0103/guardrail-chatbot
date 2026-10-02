from copy import deepcopy
import hashlib
import json
from pathlib import Path
import runpy

import pytest

from guardbench.core.data import load_dialogues
from guardbench.datasets import build


def write_source(tmp_path, dataset, relative, raw=None):
    path = tmp_path / build.PATHS[dataset] / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw) if raw is not None else "fixture", encoding="utf-8")
    return path


def test_esconv_merges_with_original_pointers_and_keeps_excluded_boundaries(tmp_path):
    raw = {"emotion_type": "sadness", "dialog": [
        {"speaker": speaker, "content": text, "annotation": {"tag": i}}
        for i, (speaker, text) in enumerate([
            ("supporter", "hello"), ("seeker", "one"), ("seeker", "two"),
            ("supporter", "answer"), ("seeker", "unanswered"),
        ])
    ]}
    path = write_source(tmp_path, "esconv", "ESConv.json", [raw])
    row, = build.iter_corpus("esconv", tmp_path)
    assert [m["content"] for m in row["messages"]] == ["one\ntwo", "answer"]
    assert row["messages"][0]["source"]["parts"] == [
        {"pointer": "/0/dialog/1"}, {"pointer": "/0/dialog/2"},
    ]
    assert row["messages"][0]["native"]["merged_parts"] == raw["dialog"][1:3]
    assert [m["content"] for m in row["excluded_messages"]] == ["hello", "unanswered"]
    assert Path(row["source"]["path"]) == path
    assert row["source"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert row["native"]["emotion_type"] == "sadness"


def test_generate_has_no_current_assistant_or_future_messages(tmp_path):
    write_source(tmp_path, "attunebench", "Test Samples/Sample200/conversation_1.json", {
        "conversationId": "original-id", "profile": {"score": 3},
        "turns": [{"userMessage": "first", "llmResponse": "answer", "annotations": {"B1": "yes"}},
                  {"userMessage": "second", "llmResponse": "future"}],
    })
    rows = list(build.iter_corpus("attunebench", tmp_path, mode="generate"))
    assert [m["content"] for m in rows[0]["messages"]] == ["first"]
    assert [m["content"] for m in rows[1]["messages"]] == ["first", "answer", "second"]
    assert rows[0]["source_dialogue_id"] == "original-id"
    assert rows[0]["dialogue_id"] != rows[1]["dialogue_id"]
    assert rows[0]["messages"][0]["native"]["annotations"] == {"B1": "yes"}


def test_sim_uses_target_messages_and_preserves_reasoning_outside_content(tmp_path):
    blocks = [{"type": "reasoning", "reasoning": "private"}, {"type": "text", "text": "visible"}]
    write_source(tmp_path, "sim_vail", "data/v1/model/transcript_1.json", {
        "metadata": {"transcript_id": "id"}, "messages": [{"role": "user", "content": "auditor"}],
        "target_messages": [{"role": "system", "content": "instruction"},
                            {"role": "user", "content": "question"},
                            {"role": "assistant", "content": blocks}],
    })
    row, = build.iter_corpus("sim_vail", tmp_path)
    assert [m["content"] for m in row["messages"]] == ["instruction", "question", "visible"]
    assert row["messages"][-1]["native"]["content"] == blocks
    with pytest.raises(ValueError, match="Unsupported"):
        build.text_content([{"type": "image"}])


def test_cradle_boundaries_use_row_positions_and_explicit_continuation_mapping(tmp_path, monkeypatch):
    write_source(tmp_path, "cradle_dialogue", "data/test-00000-of-00001.parquet")
    rows = [
        {"turn_id": 0, "text": "User: first", "labels": "alert_ongoing"},
        {"turn_id": 1, "text": "Listener: reply", "labels": ""},
        {"turn_id": 2, "text": "- continuation", "labels": ""},
        {"turn_id": 0, "text": "User: second", "labels": ""},
        {"turn_id": 1, "text": "Listener: another", "labels": ""},
    ]
    monkeypatch.setattr(build, "parquet_rows", lambda _: deepcopy(rows))
    sessions = list(build.iter_corpus("cradle_dialogue", tmp_path, splits=["test"]))
    assert len(sessions) == 2
    assert sessions[0]["source"]["row_start"] == 0
    assert sessions[0]["source"]["row_end_exclusive"] == 3
    assert sessions[1]["source"]["row_start"] == 3
    assert sessions[0]["messages"][0]["native"]["labels"] == "alert_ongoing"
    answer = sessions[0]["messages"][1]
    assert answer["content"] == "reply\n- continuation"
    assert answer["source"]["parts"][1]["row_index"] == 2
    assert "role_mapping" in answer["source"]["parts"][1]
    rows[1]["turn_id"] = 4
    with pytest.raises(ValueError, match="non-contiguous"):
        list(build.iter_corpus("cradle_dialogue", tmp_path, splits=["test"]))


def test_daily_annotations_follow_original_utterances(tmp_path, monkeypatch):
    write_source(tmp_path, "dailydialog", "default/test/0000.parquet")
    monkeypatch.setattr(build, "parquet_rows", lambda _: iter([
        {"dialog": ["one", "two", "three"], "act": [1, 2, 3], "emotion": [0, 4, 5]},
    ]))
    row, = build.iter_corpus("dailydialog", tmp_path, splits=["test"])
    assert row["messages"][1]["native"] == {"act": 2, "emotion": 4}
    assert row["excluded_messages"][0]["content"] == "three"
    assert row["messages"][1]["source"]["parts"] == [{"pointer": "/dialog/1", "row_index": 0}]


def test_output_is_atomic_and_can_be_edited_and_loaded(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/build_datasets.py"
    build_file = runpy.run_path(str(script))["build_file"]
    row = {"dialogue_id": "one", "messages": [{"role": "user", "content": "question"},
                                               {"role": "assistant", "content": "answer"}]}
    path = tmp_path / "edited.jsonl"

    def broken():
        yield row
        raise ValueError("invalid source")

    with pytest.raises(ValueError, match="invalid source"):
        build_file(path, broken())
    assert not path.exists()
    assert not list(tmp_path.iterdir())
    build_file(path, iter([row]))
    row["messages"][1]["content"] = "edited answer"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert load_dialogues(path)[0]["messages"][1]["content"] == "edited answer"
    with pytest.raises(ValueError, match="already exists"):
        build_file(path, iter([row]))
