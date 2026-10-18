"""Export simulated dialogues as VERA-MH transcripts, one folder per condition.

The judge sees only the user/chatbot text, never the simulator's hidden state.
Judge each folder in a VERA-MH checkout (https://github.com/SpringCare/VERA-MH):

  uv run python vera.py judge -j gpt-5.4:1 --judge-params reasoning_effort=low \
      --rubric SI --conversations <out>/<condition> -o <evaluation-folder>
  uv run python vera.py score -r <evaluation-folder>/<run>/results.csv \
      --personas data/SI/personas.tsv
"""

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path


def transcript(turns):
    # Matches VERA-MH utils.conversation_utils.format_conversation_summary.
    lines = []
    for turn in turns:
        lines += [f"user: {turn['user']}", f"chatbot: {turn['final']}"]
    return "\n\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", type=Path, required=True, help="simulated run folder with turns.jsonl")
    parser.add_argument("--out", type=Path, help="default: <run>/vera_transcripts")
    parser.add_argument("--include-errors", action="store_true", help="export dialogues that stopped on an error")
    args = parser.parse_args()
    out = args.out or args.run / "vera_transcripts"
    if out.exists() and any(out.iterdir()):
        parser.error("output folder must be new or empty")
    dialogues = defaultdict(list)
    with (args.run / "turns.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                row = json.loads(line)
                dialogues[row["dialogue_id"]].append(row)
    written, skipped = 0, []
    for dialogue_id, turns in sorted(dialogues.items()):
        turns.sort(key=lambda row: row["turn_index"])
        info = turns[0]["metadata"].get("dialogue", {})
        if "persona_name" not in info:
            parser.error(f"{dialogue_id} is not a simulated dialogue")
        failed = any(row["error"] for row in turns)
        if failed and not args.include_errors:
            skipped.append(dialogue_id)
            continue
        complete = [row for row in turns if not row["error"]]
        if not complete:
            skipped.append(dialogue_id)
            continue
        condition = str(info.get("condition") or "single")
        # VERA-MH reads the persona from the second "_" field: {hash}_{persona}_{model}_run{n}.txt
        digest = hashlib.sha256(dialogue_id.encode()).hexdigest()[:6]
        folder = out / condition
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{digest}_{info['persona_name']}_{condition.replace('_', '-')}_run1.txt"
        (folder / name).write_text(transcript(complete), encoding="utf-8")
        written += 1
    print(f"transcripts={written} skipped={len(skipped)} output={out}")
    for dialogue_id in skipped:
        print(f"skipped (error or empty): {dialogue_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
