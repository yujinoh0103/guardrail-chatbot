"""Append node/group events and turn results as JSONL."""

from datetime import datetime, timezone
import json
from pathlib import Path


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def error_info(exc: Exception) -> dict:
    return {"type": type(exc).__name__, "message": str(exc)}


class Recorder:
    def __init__(self, output_dir: str | Path):
        folder = Path(output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        paths = [folder / "trace.jsonl", folder / "turns.jsonl"]
        if any(path.exists() for path in paths):
            raise FileExistsError("Choose a new output directory; trace files already exist")
        self.trace = paths[0].open("x", encoding="utf-8")
        self.turns = paths[1].open("x", encoding="utf-8")

    def write(self, record: dict, *, turn: bool = False) -> None:
        # Synchronous single-line writes cannot interleave between asyncio tasks.
        stream = self.turns if turn else self.trace
        stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        stream.flush()

    def close(self) -> None:
        self.trace.close()
        self.turns.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
