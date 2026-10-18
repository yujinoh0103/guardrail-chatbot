"""Run a pipeline on JSONL dialogues or a single JSON conversation."""

import argparse
import asyncio
import json
import hashlib
from copy import deepcopy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml

from guardbench.core.pipeline import comparison_variants
from guardbench.core.runner import Runner, load_dialogues
from guardbench.core.data import source_labels, source_risk_labels
from guardbench.trace.recorder import Recorder
from dotenv import load_dotenv
from guardbench.llm.client import REQUEST_CACHE, model_settings, request_key
from guardbench.simulation.personas import DEFAULT_PERSONAS, RISK_LEVELS, load_personas
from guardbench.simulation.user import UserSimulator


def merge_config(base, changes):
    """Recursively override a pipeline without duplicating its YAML."""
    result = deepcopy(base)
    for key, value in changes.items():
        result[key] = merge_config(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else deepcopy(value)
    return result


def load_request_cache(folders):
    """Load raw responses only; exact model/messages/sampling keys control reuse."""
    cache = {}
    for folder in folders:
        for path in sorted(folder.rglob("trace.jsonl")):
            for row in Runner._saved_records(path):
                if row.get("event") != "node" or row.get("error") or row.get("use") == "guard_in/qwen3guard":
                    continue
                queries = row.get("model_requests", [row["model_request"]] if "model_request" in row else [])
                output = row.get("output")
                if isinstance(output, str):
                    responses = [output]
                elif isinstance(output, dict):
                    raw = output.get("raw", {})
                    responses = raw.get("attempts") or [raw.get("outputs", {}).get(q.get("axis")) or raw.get("output") for q in queries]
                else:
                    continue
                if len(queries) != len(responses):
                    continue
                settings = model_settings(row.get("params", {}).get("model", "selfharm_guard"), row.get("params", {}).get("models_config", "configs/models.yaml"))
                for query, response in zip(queries, responses):
                    if not isinstance(response, str):
                        continue
                    snapshot = {**settings, "model_id": query["model"], "sampling": query.get("sampling", {})}
                    key = request_key(snapshot, query["messages"])
                    cache[key] = {"content": response, "usage": None,
                                  "source": {"trace": str(path.resolve()), "node": row["node"],
                                             "dialogue_id": row["dialogue_id"], "turn_index": row["turn_index"]}}
    return cache


def run_experiment(args, parser):
    """Run paired single-turn cases with source history held fixed across methods."""
    root = Path(__file__).resolve().parents[1]
    suite = yaml.safe_load(args.experiment.read_text(encoding="utf-8"))
    base = yaml.safe_load((root / suite["pipeline"]).read_text(encoding="utf-8"))
    base = merge_config(base, suite.get("defaults", {}))
    if "cases_from" in suite:
        suite["cases"] = yaml.safe_load((root / suite["cases_from"]).read_text(encoding="utf-8"))["cases"]
    methods = suite["methods"]
    if args.method:
        if set(args.method) - set(methods):
            parser.error("unknown experiment method")
        methods = {name: value for name, value in methods.items() if name in args.method}
    cases = suite["cases"]
    if args.case:
        if set(args.case) - {case["id"] for case in cases}:
            parser.error("unknown experiment case")
        cases = [case for case in cases if case["id"] in args.case]
    cases = cases[:args.limit] if args.limit else cases
    if not methods or not cases:
        parser.error("experiment needs methods and cases")
    sources, selected = {}, []
    for case in cases:
        path = root / case["data"]
        if path not in sources:
            sources[path] = {str(d["dialogue_id"]): d for d in load_dialogues(path)}
        dialogue = deepcopy(sources[path][case["dialogue_id"]])
        messages = dialogue["messages"]
        users = [i for i, m in enumerate(messages) if m["role"] == "user"]
        index = case["turn_index"]
        if not isinstance(index, int) or index < 0 or index >= len(users):
            parser.error("invalid case turn_index")
        dialogue["messages"] = [m for m in messages[:users[index] + 1] if m["role"] != "system"]
        dialogue["source_dialogue_id"] = case["dialogue_id"]
        dialogue["dialogue_id"] = case["id"]
        dialogue["case"] = deepcopy(case)
        selected.append(dialogue)
    configs = {}
    for name, changes in methods.items():
        if not name or Path(name).name != name or name in (".", ".."):
            parser.error("method name must be a directory name")
        config = merge_config(base, changes)
        config["input"]["replay_users"] = False
        configs[name] = (config, comparison_variants(config))
    if args.out.exists() and any(args.out.iterdir()):
        parser.error("experiment output must be new; use --resume-from with a new --out")
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "experiment.yaml").write_text(yaml.safe_dump(suite, allow_unicode=True, sort_keys=False), encoding="utf-8")
    artifacts = [*sorted((root / "prompts").glob("*.yaml")), *sorted((root / "src").rglob("*.py")),
                 root / "configs/models.yaml", args.experiment.resolve(), root / suite["pipeline"], Path(__file__).resolve()]
    manifest = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in artifacts}
    if args.resume_from and (args.resume_from / "manifest.json").exists():
        previous = json.loads((args.resume_from / "manifest.json").read_text())
        if previous["files"] != manifest:
            parser.error("resume configuration/code changed; use --reuse-requests-from to match actual model requests instead")
    (args.out / "manifest.json").write_text(json.dumps({"files": manifest, "selected_methods": list(methods),
        "selected_cases": [case["id"] for case in cases], "argv": sys.argv,
        "reuse_requests_from": [str(p.resolve()) for p in args.reuse_requests_from or []]}, ensure_ascii=False, indent=2))

    async def execute():
        semaphore, cache, results = asyncio.Semaphore(args.concurrency), {}, []
        request_cache = load_request_cache(args.reuse_requests_from or [])
        request_token = REQUEST_CACHE.set(request_cache)
        if args.resume_from:
            for path in args.resume_from.glob("*/trace.jsonl"):
                for record in Runner._saved_records(path):
                    if record.get("event") == "node" and record.get("node") == "g11" and record.get("error") is None:
                        key = json.dumps([record["use"], record["params"], record["inputs"]], sort_keys=True, ensure_ascii=False)
                        future = asyncio.get_running_loop().create_future()
                        future.set_result((record["output"], {field: record[field] for field in
                                           ("model_request", "model_requests") if field in record}))
                        cache[key] = future
        # Methods run sequentially so a failed provider probe cannot fan out into the whole suite.
        for name, (config, variants) in configs.items():
            destination = args.out / name
            destination.mkdir()
            (destination / "pipeline.yaml").write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
            with Recorder(destination) as recorder:
                jobs = []
                for condition, pipeline in variants:
                    dialogues = deepcopy(selected)
                    for dialogue in dialogues:
                        dialogue["original_dialogue_id"] = dialogue["dialogue_id"]
                        dialogue["dialogue_id"] += ":" + str(condition)
                        dialogue["condition"] = condition
                        dialogue["method"] = name
                    resume = args.resume_from / name if args.resume_from else None
                    if resume is not None and not (resume / "turns.jsonl").exists():
                        resume = None
                    runner = Runner(pipeline, recorder, args.concurrency, resume_from=resume,
                                    node_cache=cache, cache_nodes=("g11",))
                    runner.semaphore = semaphore
                    jobs.append(runner.run(dialogues))
                rows = [row for group in await asyncio.gather(*jobs) for row in group]
                results.extend(rows)
                errors = sum(row["error"] is not None for row in rows)
                print(f"method={name} turns={len(rows)} errors={errors} output={destination}", flush=True)
                if errors:
                    print("Stopped after method errors; successful calls are saved for resume.", flush=True)
                    break
        REQUEST_CACHE.reset(request_token)
        return results

    results = asyncio.run(execute())
    return int(any(row["error"] is not None for row in results))


def run_simulated(args, parser, config, variants):
    """Each condition talks to its own simulated user, so states can diverge by condition."""
    spec = config["simulator"]
    if not isinstance(spec, dict):
        parser.error("simulator must be a mapping")
    if args.resume_from or args.reuse_requests_from:
        parser.error("simulated dialogues cannot be resumed or replayed from earlier requests")
    if args.split or args.source_label_prefix or args.source_risk_family or args.drop_source_system or args.min_user_turns or args.max_user_turns:
        parser.error("simulated runs select personas with --dialogue-id, --risk-level and --limit")
    if args.max_turns is not None and args.max_turns <= 0:
        parser.error("--max-turns must be positive")
    params = {key: value for key, value in spec.items() if key not in ("max_turns", "personas")}
    try:
        simulator = UserSimulator(**params)
    except (TypeError, ValueError) as exc:
        parser.error(f"invalid simulator: {exc}")
    max_turns = args.max_turns or spec.get("max_turns", 10)
    personas = load_personas(args.data or spec.get("personas", DEFAULT_PERSONAS))
    if args.dialogue_id:
        personas = [p for p in personas if p["dialogue_id"] in args.dialogue_id or p["persona_name"] in args.dialogue_id]
    if args.risk_level:
        personas = [p for p in personas if p.get("risk_level") in args.risk_level]
    if args.limit is not None:
        personas = personas[:args.limit]
    if not personas:
        parser.error("no personas match the selection")
    def show(record):
        print(f"\n--- {record['dialogue_id']} / turn={record['turn_index']} / {record['status']} ---")
        print("USER_STATE: " + json.dumps(record["metadata"].get("user_state"), ensure_ascii=False))
        guard = (record["outputs"] or {}).get("g11") or {}
        if guard:
            print(f"G11: {guard.get('label')} {guard.get('categories')}")
        print(f"USER: {record['user']}")
        print(f"ASSISTANT: {record['final']}", flush=True)
        if record["error"]:
            print("ERROR: " + json.dumps(record["error"], ensure_ascii=False), flush=True)

    class LiveRecorder(Recorder):
        """Print each turn as it is recorded, so long simulated runs show progress."""
        def write(self, record, *, turn=False):
            super().write(record, turn=turn)
            if turn and args.show_turns:
                show(record)

    with LiveRecorder(args.out) as recorder:
        (args.out / "pipeline.yaml").write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
        async def execute():
            semaphore = asyncio.Semaphore(args.concurrency)
            jobs = []
            for condition, pipeline in variants:
                selected = deepcopy(personas)
                for persona in selected:
                    persona["original_dialogue_id"] = persona["dialogue_id"]
                    persona["condition"] = condition
                    if condition is not None:
                        persona["dialogue_id"] = f"{persona['dialogue_id']}:{condition}"
                runner = Runner(pipeline, recorder, args.concurrency)
                runner.semaphore = semaphore
                jobs.append(runner.run_simulated(selected, simulator, max_turns))
            groups = await asyncio.gather(*jobs)
            return [turn for group in groups for turn in group]
        results = asyncio.run(execute())
    errors = sum(result["error"] is not None for result in results)
    print(f"personas={len(personas)} dialogues={len(personas) * len(variants)} turns={len(results)} errors={errors} output={args.out}")
    print("Simulator failures are user_sim events with an error in trace.jsonl.")
    return int(errors > 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline", type=Path)
    parser.add_argument("--data", type=Path, help="JSONL dialogues or one JSON dialogue object")
    parser.add_argument("--experiment", type=Path, help="YAML suite of pipeline overrides and paired source turns")
    parser.add_argument("--method", action="append", help="select a suite method; repeat for multiple")
    parser.add_argument("--case", action="append", help="select a suite case ID; repeat for multiple")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--resume-from", type=Path, help="reuse completed turns and successful node calls from a previous output directory")
    parser.add_argument("--reuse-requests-from", type=Path, action="append", help="reuse exact raw model requests from prior trace folders while running current node validation; repeat for multiple")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--split", help="select dialogues by their split field")
    parser.add_argument("--dialogue-id", action="append", help="select an exact source dialogue ID; repeat to select multiple")
    parser.add_argument("--source-label-prefix", action="append", help="select dialogues containing a user source label prefix; repeat for OR matching")
    parser.add_argument("--source-risk-family", choices=("SI", "SH"), action="append", help="select exact parsed CRADLE SI/SH labels; repeat for OR matching")
    parser.add_argument("--max-user-turns", type=int, help="exclude dialogues with more user turns; do not truncate")
    parser.add_argument("--min-user-turns", type=int, help="exclude dialogues with fewer user turns")
    parser.add_argument("--drop-source-system", action="store_true", help="keep the original system message as metadata instead of chatbot input")
    parser.add_argument("--limit", type=int, help="maximum dialogues after split selection")
    parser.add_argument("--show-turns", action="store_true", help="print user, response and node outputs per turn")
    parser.add_argument("--risk-level", choices=RISK_LEVELS, action="append", help="simulator only: select personas by current suicide risk level; repeat for OR matching")
    parser.add_argument("--max-turns", type=int, help="simulator only: override simulator.max_turns")
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    if args.concurrency <= 0:
        parser.error("--concurrency must be positive")
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if args.max_user_turns is not None and args.max_user_turns <= 0:
        parser.error("--max-user-turns must be positive")
    if args.min_user_turns is not None and args.min_user_turns <= 0:
        parser.error("--min-user-turns must be positive")
    if args.min_user_turns is not None and args.max_user_turns is not None and args.min_user_turns > args.max_user_turns:
        parser.error("--min-user-turns cannot exceed --max-user-turns")
    if args.source_label_prefix and any(not prefix.strip() for prefix in args.source_label_prefix):
        parser.error("--source-label-prefix must be nonempty")
    if args.experiment:
        if args.pipeline or args.data:
            parser.error("--experiment replaces --pipeline/--data")
        if args.show_turns or args.split or args.dialogue_id or args.source_label_prefix or args.source_risk_family or args.drop_source_system or args.min_user_turns or args.max_user_turns:
            parser.error("experiment cases define selection; use --limit and --method")
        return run_experiment(args, parser)
    if not args.pipeline:
        parser.error("provide --pipeline and --data, or --experiment")
    if args.method or args.case:
        parser.error("--method/--case require --experiment")
    config = yaml.safe_load(args.pipeline.read_text(encoding="utf-8"))
    variants = comparison_variants(config)
    if "simulator" in config:
        return run_simulated(args, parser, config, variants)
    if args.risk_level or args.max_turns:
        parser.error("--risk-level/--max-turns require a pipeline with a simulator section")
    if not args.data:
        parser.error("provide --pipeline and --data, or --experiment")
    dialogues = load_dialogues(args.data)
    if args.split:
        dialogues = [dialogue for dialogue in dialogues if dialogue.get("split") == args.split]
    if args.dialogue_id:
        dialogues = [dialogue for dialogue in dialogues if str(dialogue["dialogue_id"]) in args.dialogue_id]
    if args.source_label_prefix:
        prefixes = tuple(prefix.strip() for prefix in args.source_label_prefix)
        dialogues = [dialogue for dialogue in dialogues if any(
            label.startswith(prefixes)
            for message in dialogue.get("messages", []) if message["role"] == "user"
            for label in source_labels(message))]
    if args.source_risk_family:
        dialogues = [dialogue for dialogue in dialogues if any(
            label["family"] in args.source_risk_family
            for message in dialogue.get("messages", []) if message["role"] == "user"
            for label in source_risk_labels(message))]
    if args.min_user_turns is not None or args.max_user_turns is not None:
        def user_turn_count(dialogue):
            return len(dialogue["turns"]) if "turns" in dialogue else sum(
                m["role"] == "user" for m in dialogue["messages"])
        dialogues = [dialogue for dialogue in dialogues if
                     (args.min_user_turns is None or user_turn_count(dialogue) >= args.min_user_turns) and
                     (args.max_user_turns is None or user_turn_count(dialogue) <= args.max_user_turns)]
    if args.limit is not None:
        dialogues = dialogues[:args.limit]
    if not dialogues:
        parser.error("no dialogues match the selection")
    for dialogue in dialogues:
        families = set()
        for message in dialogue.get("messages", []):
            if message["role"] == "user":
                message["source_labels"] = source_labels(message)
                message["source_risk_labels"] = source_risk_labels(message)
                families.update(label["family"] for label in message["source_risk_labels"])
        dialogue["source_risk_families"] = sorted(families)
    if args.drop_source_system:
        for dialogue in dialogues:
            messages = dialogue.get("messages", [])
            if messages and messages[0]["role"] == "system":
                dialogue["reference_system"] = messages.pop(0)
    with Recorder(args.out) as recorder:
        async def execute():
            semaphore = asyncio.Semaphore(args.concurrency)
            jobs = []
            for condition, pipeline in variants:
                selected = deepcopy(dialogues)
                if condition is not None:
                    for dialogue in selected:
                        dialogue["original_dialogue_id"] = dialogue["dialogue_id"]
                        dialogue["dialogue_id"] = f"{dialogue['dialogue_id']}:{condition}"
                        dialogue["condition"] = condition
                        dialogue["condition_name"] = "binary_guard_label" if condition == "label" else condition
                        identity = str(dialogue.get("source_dialogue_id", dialogue["original_dialogue_id"]))
                        if dialogue.get("dataset") == "cradle_dialogue":
                            identity = identity.rsplit(":", 1)[-1]
                        dialogue["display_name"] = "__".join([
                            str(dialogue.get("dataset", "dialogue")), str(dialogue.get("split", "unsplit")),
                            identity, "+".join(dialogue["source_risk_families"]) or "unlabelled",
                            dialogue["condition_name"],
                        ])
                runner = Runner(pipeline, recorder, args.concurrency, resume_from=args.resume_from)
                runner.semaphore = semaphore
                jobs.append(runner.run(selected))
            groups = await asyncio.gather(*jobs)
            return [turn for group in groups for turn in group]
        results = asyncio.run(execute())
    errors = sum(result["error"] is not None for result in results)
    reused = sum("reused_from" in result for result in results)
    if args.show_turns:
        for result in results:
            print(f"\n--- {result['dialogue_id']} / turn={result['turn_index']} / {result['status']} ---")
            print(f"history={len(result['history'])} context={len(result['context'])}")
            print(f"USER: {result['user']}")
            print("OUTPUTS: " + json.dumps(result["outputs"], ensure_ascii=False))
            print(f"ASSISTANT: {result['final']}")
            if result["error"]:
                print("ERROR: " + json.dumps(result["error"], ensure_ascii=False))
    print(f"dialogues={len(dialogues) * len(variants)} turns={len(results)} reused_turns={reused} errors={errors} output={args.out}")
    return int(errors > 0)


if __name__ == "__main__":
    raise SystemExit(main())
