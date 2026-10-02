"""Build editable, source-linked JSONL for the five local conversation corpora."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from guardbench.core.data import load_dialogues
from guardbench.datasets.build import PATHS, iter_corpus


def build_file(path, dialogues, limit=None):
    """Validate and publish a complete file without replacing existing output."""
    if path.exists():
        raise ValueError(f"Output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    seen, turns = set(), 0
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            for row in dialogues:
                if row["dialogue_id"] in seen:
                    raise ValueError(f"Duplicate dialogue_id: {row['dialogue_id']}")
                seen.add(row["dialogue_id"])
                turns += sum(m["role"] == "user" for m in row["messages"])
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                if limit is not None and len(seen) >= limit:
                    break
            if not seen:
                raise ValueError("No dialogues produced")
            stream.flush()
            os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return {"dialogues": len(seen), "user_messages": turns, "output": str(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasets-root", type=Path, default=Path(__file__).resolve().parents[2] / "datasets")
    parser.add_argument("--dataset", choices=list(PATHS), action="append", help="repeat; default: all five")
    parser.add_argument("--split", choices=("train", "validation", "test"), action="append",
                        help="CRADLE/DailyDialog splits; default: all three")
    parser.add_argument("--mode", choices=("recorded", "generate"), default="recorded")
    parser.add_argument("--out-dir", type=Path, help="default: data/built/<mode>")
    parser.add_argument("--limit", type=int, help="maximum output rows per dataset for a smoke run")
    parser.add_argument("--source-url", action="append", default=[], metavar="DATASET=URL",
                        help="set verified upstream repository URL; repeat per dataset")
    parser.add_argument("--validate-only", type=Path, help="validate an edited Runner JSONL without building")
    args = parser.parse_args()
    try:
        if args.limit is not None and args.limit <= 0:
            raise ValueError("--limit must be positive")
        if args.validate_only:
            rows = load_dialogues(args.validate_only)
            if any(row.get("messages", [{}])[-1].get("role") != ("assistant" if args.mode == "recorded" else "user") for row in rows):
                raise ValueError(f"messages must end with {'assistant' if args.mode == 'recorded' else 'user'} for {args.mode}")
            print(f"valid dialogues={len(rows)} mode={args.mode}")
            return
        datasets = args.dataset or list(PATHS)
        out_dir = args.out_dir or Path("data/built") / args.mode
        if len(set(datasets)) != len(datasets):
            raise ValueError("Duplicate --dataset")
        urls = {}
        for entry in args.source_url:
            name, sep, url = entry.partition("=")
            if not sep or name not in datasets or name in urls or not url.startswith(("https://", "http://")):
                raise ValueError("--source-url requires unique selected DATASET=https://... entries")
            urls[name] = url
        for dataset in datasets:
            path = out_dir / f"{dataset}.jsonl"
            if path.exists():
                raise ValueError(f"Output already exists: {path}")
        for dataset in datasets:
            rows = iter_corpus(dataset, args.datasets_root, args.split or ("train", "validation", "test"), args.mode, urls.get(dataset))
            report = build_file(out_dir / f"{dataset}.jsonl", rows, args.limit)
            print(json.dumps({"dataset": dataset, "mode": args.mode, **report}, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    main()
