"""Compose simulated-user personas from VERA-MH, SIM-VAIL, Patient-Psi-CM, ESConv, AttuneBench and Reddit C-SSRS.

Examples:
  python scripts/build_personas.py --out data/built/personas/mixed.jsonl \
      --source vera_mh_si=20 --source sim_vail --source patient_psi=10 --source reddit_cssrs=20
  python scripts/build_personas.py --out data/built/personas/calibration.jsonl \
      --source attunebench=40 --source esconv=40 --fill --model persona_writer_nano

`--source NAME` takes every persona of a source; `NAME=N` takes a stratified sample of N.
Run the simulator on the output with `scripts/run.py --data <out>`.
"""

import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv

from guardbench.simulation.persona_builder import SECTIONS, PersonaBuilder, fill, missing_fields, sections_present
from guardbench.simulation.sources import LOADERS

SOURCES = (*LOADERS, "sim_vail_control")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True, help="output JSONL")
    parser.add_argument("--source", action="append", required=True, metavar="NAME[=N]",
                        help=f"repeatable; one of {', '.join(SOURCES)}")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--datasets-root", type=Path, default=Path(__file__).resolve().parents[2] / "datasets")
    parser.add_argument("--examples", type=int, default=3, help="style examples per persona")
    parser.add_argument("--fill", action="store_true", help="fill missing fields with an LLM (OpenRouter key from .env)")
    parser.add_argument("--fill-from", type=Path, help="reuse filled values from an earlier output instead of new calls")
    parser.add_argument("--model", default="persona_writer", choices=("persona_writer", "persona_writer_nano"),
                        help="persona_writer = gpt-5.6-luna, persona_writer_nano = gpt-5-nano")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--keep-incomplete", action="store_true", help="also write personas missing a section")
    args = parser.parse_args()
    limits = {}
    for item in args.source:
        name, _, count = item.partition("=")
        if name not in SOURCES or name in limits:
            parser.error(f"unknown or repeated source: {name}")
        if count and (not count.isdigit() or int(count) <= 0):
            parser.error(f"invalid count for {name}")
        limits[name] = int(count) if count else None
    if args.out.exists():
        parser.error("output exists; choose a new path")
    builder = PersonaBuilder(seed=args.seed, datasets_root=args.datasets_root, example_count=args.examples)
    personas = builder.build(limits)
    failures = {}
    if args.fill or args.fill_from:
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
        previous = {}
        if args.fill_from:
            for line in args.fill_from.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                previous[row["dialogue_id"]] = row
        targets = personas if args.fill else [p for p in personas if p["dialogue_id"] in previous]
        failures = asyncio.run(fill(targets, builder, args.model, concurrency=args.concurrency, previous=previous))
    incomplete = {p["dialogue_id"]: sorted(set(SECTIONS) - set(sections_present(p))) for p in personas}
    incomplete = {k: v for k, v in incomplete.items() if v}
    if not args.keep_incomplete:
        personas = [p for p in personas if p["dialogue_id"] not in incomplete]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as stream:
        for persona in personas:
            stream.write(json.dumps(persona, ensure_ascii=False) + "\n")
    print(f"personas={len(personas)} output={args.out}")
    print("by base: " + json.dumps(Counter(p["base"]["source"] for p in personas)))
    print("layers: " + json.dumps(Counter(layer for p in personas for layer in p["layers"])))
    print("filled by LLM: " + json.dumps(Counter(f for p in personas for f in p.get("filled_by_llm", {}).get("fields", []))))
    print("still missing: " + json.dumps(Counter(f for p in personas for f in missing_fields(p))))
    print(f"incomplete personas: {len(incomplete)} ({'kept' if args.keep_incomplete else 'dropped'}): "
          + json.dumps(Counter(section for missing in incomplete.values() for section in missing)))
    print("primary vulnerability: " + json.dumps(Counter(str(p["vulnerability"]["primary"]) for p in personas)))
    print("intention: " + json.dumps(Counter(p["vulnerability"]["intention"] for p in personas)))
    for dialogue_id, error in failures.items():
        print(f"help profile failed: {dialogue_id}: {error}")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
