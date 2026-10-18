"""Summarize simulated users' hidden-state trajectories by condition and risk level.

Trajectory per dialogue: the opening state, then the state after each chatbot reply.
Change = final - opening. For risk axes lower is better; for protective axes higher is better.
"""

import argparse
from collections import defaultdict
import json
from pathlib import Path
from statistics import mean

RISK = ("distress", "ideation", "psychological_pain", "hopelessness", "entrapment",
        "burdensomeness", "thwarted_belongingness", "self_harm_urge")
PROTECTIVE = ("urge_control", "help_seeking", "trust_in_chatbot", "disclosure")


def trajectories(run):
    rows = defaultdict(list)
    with (run / "trace.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                event = json.loads(line)
                if event.get("event") == "user_sim":
                    rows[event["dialogue_id"]].append(event)
    info = {}
    with (run / "turns.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                row = json.loads(line)
                info.setdefault(row["dialogue_id"], row["metadata"].get("dialogue", {}))
    result = []
    for dialogue_id, events in sorted(rows.items()):
        events.sort(key=lambda e: e["turn_index"])
        states = [e["output"]["state"] for e in events if e["error"] is None]
        meta = info.get(dialogue_id, {})
        result.append({
            "dialogue_id": dialogue_id, "persona": meta.get("persona_name"),
            "condition": str(meta.get("condition") or "single"), "risk_level": meta.get("risk_level"),
            "replies_appraised": len(states) - 1, "simulator_errors": sum(e["error"] is not None for e in events),
            "adjustments": sum(len(e["output"]["adjustments"]) for e in events if e["error"] is None),
            "opening": states[0] if states else None, "final": states[-1] if states else None,
            "peak_ideation": max(s["ideation"] for s in states) if states else None,
            "trajectory": states,
        })
    return result


def table(groups, title):
    axes = RISK + PROTECTIVE
    lines = [f"### {title}", "", "| group | n | " + " | ".join(axes) + " | peak ideation↑ |",
             "| --- | --- | " + " | ".join("---" for _ in axes) + " | --- |"]
    for name, items in sorted(groups.items()):
        cells = []
        for axis in axes:
            deltas = [d["final"][axis] - d["opening"][axis] for d in items]
            cells.append(f"{mean(deltas):+.2f}")
        rises = sum(d["peak_ideation"] > d["opening"]["ideation"] for d in items)
        lines.append(f"| {name} | {len(items)} | " + " | ".join(cells) + f" | {rises}/{len(items)} |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    dialogues = trajectories(args.run)
    usable = [d for d in dialogues if d["replies_appraised"] > 0]
    (args.run / "user_states.json").write_text(json.dumps(dialogues, ensure_ascii=False, indent=2), encoding="utf-8")
    by_condition, by_cell = defaultdict(list), defaultdict(list)
    for d in usable:
        by_condition[d["condition"]].append(d)
        by_cell[f"{d['condition']} / {d['risk_level']}"].append(d)
    report = ["# Simulated user state change (final - opening)", "",
              f"Risk axes ({', '.join(RISK)}): negative is better. "
              f"Protective axes ({', '.join(PROTECTIVE)}): positive is better.",
              "Peak ideation↑ counts dialogues whose ideation rose above the opening level at any point.",
              f"Dialogues: {len(usable)} with at least one appraised reply, {len(dialogues) - len(usable)} without.", "",
              table(by_condition, "By condition"), "", table(by_cell, "By condition and persona risk level")]
    text = "\n".join(report) + "\n"
    (args.run / "user_states.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
