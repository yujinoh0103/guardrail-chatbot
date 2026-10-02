"""Download the ID snapshot and group original JSONL rows by source session."""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import tempfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen


DATASETS = (
    "attunebench", "empathetic_dialogues", "esconv",
    "sim_vail_turn_scores", "cradle_dialogue",
)
BASE_URL = "https://raw.githubusercontent.com/yujinoh0103/guardrail-dataset/data/id-300-windows/id"


def read_rows(path):
    rows = []
    with Path(path).open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("row must be an object")
            except ValueError as exc:
                raise ValueError(f"{path}, line {number}: {exc}") from exc
            rows.append(row)
    if not rows:
        raise ValueError(f"{path}: no rows")
    return rows


def key_value(row, key):
    value = row
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"missing session key {key!r}")
        value = value[part]
    if isinstance(value, bool) or not isinstance(value, (str, int)) or value == "":
        raise ValueError(f"{key!r} must be a nonempty string or integer")
    return value


def group_sessions(dataset, rows, session_key):
    sessions = {}
    for number, row in enumerate(rows, 1):
        try:
            session_id = key_value(row, session_key)
        except ValueError as exc:
            raise ValueError(f"{dataset}, row {number}: {exc}") from exc
        # Keep numeric and string IDs distinct, as they were in the source.
        identity = (type(session_id), session_id)
        if identity not in sessions:
            sessions[identity] = {
                "dataset": dataset, "session_id": session_id, "records": [],
            }
        sessions[identity]["records"].append(row)
    return list(sessions.values())


def schema(value, prefix=""):
    result = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        result[path] = type(item).__name__
        if isinstance(item, dict):
            result.update(schema(item, path))
    return result


def download(dataset, directory):
    url = f"{BASE_URL}/{dataset}.jsonl"
    headers = {"User-Agent": "guardbench"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(url, headers=headers), timeout=60) as response:
            data = response.read()
    except HTTPError as exc:
        raise ValueError(
            f"{dataset}: HTTP {exc.code}; check the repository/link and "
            "GITHUB_TOKEN read access, or use --input DATASET=LOCAL_PATH"
        ) from exc
    path = directory / f"{dataset}.jsonl"
    path.write_bytes(data)
    return path


def assignments(values, option):
    result = {}
    for value in values:
        name, separator, setting = value.partition("=")
        if not separator or not name or not setting or name in result:
            raise ValueError(f"{option} requires unique DATASET=VALUE entries")
        result[name] = setting
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", default=[], metavar="DATASET=PATH",
                        help="use local JSONL; repeat per dataset, instead of downloading")
    parser.add_argument("--dataset", action="append", choices=DATASETS,
                        help="datasets to download (default: all five)")
    parser.add_argument("--session-key", action="append", default=[], metavar="DATASET=KEY",
                        help="source session ID key, with dots for nested fields")
    parser.add_argument("--inspect", action="store_true", help="show field names/types without writing output")
    parser.add_argument("--out", type=Path, default=Path("data/id_sessions.jsonl"))
    args = parser.parse_args()
    try:
        inputs = assignments(args.input, "--input")
        keys = assignments(args.session_key, "--session-key")
        if inputs and args.dataset:
            raise ValueError("use either --input or --dataset")
        datasets = list(inputs) if inputs else args.dataset or list(DATASETS)
        if len(set(datasets)) != len(datasets):
            raise ValueError("duplicate --dataset")
        if set(keys) - set(datasets):
            raise ValueError("--session-key names must match selected datasets")
        if not args.inspect:
            missing = set(datasets) - set(keys)
            if missing:
                raise ValueError(f"specify --session-key DATASET=KEY for: {', '.join(sorted(missing))}")
            if args.out.exists():
                raise ValueError(f"output already exists: {args.out}")
        output = []
        with tempfile.TemporaryDirectory(prefix="guardbench-id-") as temporary:
            for dataset in datasets:
                path = Path(inputs[dataset]) if inputs else download(dataset, Path(temporary))
                rows = read_rows(path)
                if args.inspect:
                    counts = Counter(key for row in rows for key in schema(row))
                    types = {}
                    for row in rows:
                        for key, kind in schema(row).items():
                            types.setdefault(key, set()).add(kind)
                    print(json.dumps({"dataset": dataset, "rows": len(rows), "fields": {
                        key: {"types": sorted(types[key]), "rows": count}
                        for key, count in sorted(counts.items())
                    }}, ensure_ascii=False))
                else:
                    sessions = group_sessions(dataset, rows, keys[dataset])
                    output.extend(sessions)
                    print(f"{dataset}: rows={len(rows)} sessions={len(sessions)}")
        if not args.inspect:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            # Validate all datasets before publishing one complete output file.
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.out.parent,
                                             delete=False) as stream:
                temporary_output = Path(stream.name)
                try:
                    for session in output:
                        stream.write(json.dumps(session, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.link(temporary_output, args.out)
                finally:
                    temporary_output.unlink(missing_ok=True)
            print(f"total_sessions={len(output)} output={args.out}")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    main()
