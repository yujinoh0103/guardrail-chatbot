"""Run a pipeline on JSONL dialogues or a single JSON conversation."""

import argparse
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from guardbench.core.pipeline import Pipeline
from guardbench.core.runner import Runner, load_dialogues
from guardbench.trace.recorder import Recorder
from dotenv import load_dotenv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True, help="JSONL dialogues or one JSON dialogue object")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    if args.concurrency <= 0:
        parser.error("--concurrency must be positive")
    pipeline = Pipeline.load(args.pipeline)
    dialogues = load_dialogues(args.data)
    with Recorder(args.out) as recorder:
        results = asyncio.run(Runner(pipeline, recorder, args.concurrency).run(dialogues))
    errors = sum(result["error"] is not None for result in results)
    print(f"dialogues={len(dialogues)} turns={len(results)} errors={errors} output={args.out}")
    return int(errors > 0)


if __name__ == "__main__":
    raise SystemExit(main())
