"""Interactive text conversation; batch JSON/JSONL remains in run.py."""

import argparse
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
from guardbench.core.pipeline import Pipeline
from guardbench.core.runner import Runner
from guardbench.core.session import LiveSession
from guardbench.trace.recorder import Recorder

ROOT = Path(__file__).resolve().parents[1]


async def chat(pipeline, recorder, dialogue_id):
    session = LiveSession(Runner(pipeline, recorder, concurrency=1), dialogue_id)
    while True:
        try:
            # The single-session CLI has no inference in flight while awaiting text.
            text = input("user> ")
        except EOFError:
            return 0
        if text.strip() == "/quit":
            return 0
        if not text.strip():
            continue
        result = await session.send(text)
        if result["error"]:
            print(f"error: {result['error']['type']}: {result['error']['message']}", file=sys.stderr)
            return 1
        print(f"assistant> {result['final']}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline", type=Path, default=ROOT / "configs/pipelines/live_chat.yaml")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dialogue-id", default="live")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    pipeline = Pipeline.load(args.pipeline)
    if pipeline.response_mode != "generate":
        parser.error("Live input requires input.response_mode: generate")
    with Recorder(args.out) as recorder:
        return asyncio.run(chat(pipeline, recorder, args.dialogue_id))


if __name__ == "__main__":
    raise SystemExit(main())
