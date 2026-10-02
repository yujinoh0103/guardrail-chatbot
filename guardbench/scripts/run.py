"""Run a pipeline on JSONL dialogues or a single JSON conversation."""

import argparse
import asyncio
import json
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
    parser.add_argument("--split", help="select dialogues by their split field")
    parser.add_argument("--limit", type=int, help="maximum dialogues after split selection")
    parser.add_argument("--show-turns", action="store_true", help="print user, response and node outputs per turn")
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    if args.concurrency <= 0:
        parser.error("--concurrency must be positive")
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    pipeline = Pipeline.load(args.pipeline)
    dialogues = load_dialogues(args.data)
    if args.split:
        dialogues = [dialogue for dialogue in dialogues if dialogue.get("split") == args.split]
    if args.limit is not None:
        dialogues = dialogues[:args.limit]
    if not dialogues:
        parser.error("no dialogues match the selected split")
    with Recorder(args.out) as recorder:
        results = asyncio.run(Runner(pipeline, recorder, args.concurrency).run(dialogues))
    errors = sum(result["error"] is not None for result in results)
    if args.show_turns:
        for result in results:
            print(f"\n--- {result['dialogue_id']} / turn={result['turn_index']} / {result['status']} ---")
            print(f"history={len(result['history'])} context={len(result['context'])}")
            print(f"USER: {result['user']}")
            print("OUTPUTS: " + json.dumps(result["outputs"], ensure_ascii=False))
            print(f"ASSISTANT: {result['final']}")
            if result["error"]:
                print("ERROR: " + json.dumps(result["error"], ensure_ascii=False))
    print(f"dialogues={len(dialogues)} turns={len(results)} errors={errors} output={args.out}")
    return int(errors > 0)


if __name__ == "__main__":
    raise SystemExit(main())
