"""Load VERA-MH suicide-risk personas as simulated-user dialogue seeds."""

import csv
import json
from pathlib import Path

from ..llm.client import ROOT

DEFAULT_PERSONAS = "data/external/vera_mh/SI/personas.tsv"
RISK_LEVELS = ("None", "Low", "High", "Immediate")

# C-SSRS ideation severity allowed in the opening state, keyed by the persona's
# current suicidal thoughts. Preparatory acts and attempts are behavior, not
# ideation, so their ideation is bounded rather than fixed.
IDEATION_RANGES = {
    "No suicidal ideation": (0, 0),
    "Psychological risk factors": (0, 0),
    "Passive suicidal ideation": (1, 1),
    "Active suicidal ideation, but no plan or intent": (2, 2),
    "Active suicidal ideation with method consideration": (3, 3),
    "Active suicidal thoughts with plan and intent": (5, 5),
    "Recent (<= 72 hours) preparatory actions": (4, 5),
    "Recent (<= 72 hours) suicide attempt": (3, 5),
}


def _path(path):
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def ideation_range(thoughts: str) -> tuple[int, int]:
    for prefix, bounds in IDEATION_RANGES.items():
        if thoughts.startswith(prefix):
            return bounds
    raise ValueError(f"Unknown current suicidal thoughts: {thoughts!r}")


def load_personas(path=DEFAULT_PERSONAS, template_path=None) -> list[dict]:
    """VERA-MH TSV, or JSONL written by scripts/build_personas.py."""
    path = _path(path)
    if path.suffix == ".jsonl":
        personas = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for persona in personas:
            if not isinstance(persona.get("persona_context"), str) or "dialogue_id" not in persona:
                raise ValueError(f"Invalid persona in {path}")
        return personas
    template_path = _path(template_path) if template_path else path.parent / "persona_context_template.txt"
    template = template_path.read_text(encoding="utf-8")
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    # Upstream rows occasionally misplace the long risk description; use the
    # canonical text of the short level instead (see data/external/vera_mh/SOURCE.md).
    canonical = {}
    for row in rows:
        level = row["Short Current Suicide Risk Level"]
        if row["Current Suicide Risk Level"].startswith(level + " ("):
            canonical.setdefault(level, row["Current Suicide Risk Level"])
    personas = []
    for row in rows:
        level = row["Short Current Suicide Risk Level"]
        if level not in RISK_LEVELS:
            raise ValueError(f"Unknown risk level for {row['Name']}: {level!r}")
        fields = dict(row)
        if not fields["Current Suicide Risk Level"].startswith(level + " ("):
            fields["Current Suicide Risk Level"] = canonical[level]
        context = template
        for key, value in fields.items():
            context = context.replace("{" + key + "}", value)
        personas.append({
            "dialogue_id": f"vera_si:{row['Name']}",
            "dataset": "vera_mh_si",
            "persona_name": row["Name"],
            "risk_level": level,
            "disclosure_style": row["Disclosure of Suicide Risk"].split(" (")[0],
            "reaction_style": row["Reaction to Chatbot Responses"].split(" (")[0],
            "ideation_range": list(ideation_range(row["Current Suicidal Thoughts and Behaviors"])),
            "persona_context": context,
        })
    names = [p["persona_name"] for p in personas]
    if len(set(names)) != len(names):
        raise ValueError("Persona names must be unique")
    return personas
