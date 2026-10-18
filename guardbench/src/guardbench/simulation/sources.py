"""Read persona building blocks from each source into one neutral record shape.

Every loader returns dicts with: source, source_id, provenance, and only the
fields that source actually provides. Composition happens in persona_builder.
"""

import ast
import csv
import json
import re
import sys
from pathlib import Path

from .personas import DEFAULT_PERSONAS, load_personas
from ..llm.client import ROOT

DATASETS_ROOT = ROOT.parent / "datasets"
PATHS = {
    "sim_vail": "02_multiturn_contextual/sim_vail/repro/petri_overrides/src/petri/tasks/v1",
    "patient_psi": "02_multiturn_contextual/mhsafeeval/config",
    "esconv": "03_conversation/esconv/ESConv.json",
    "attunebench": "02_multiturn_contextual/attunebench/Test Samples/Sample200",
    "reddit_cssrs": "04_reference_corpora/reddit_cssrs_2021_timevariant/"
                    "500_anonymized_Reddit_users_posts_labels - 500_anonymized_Reddit_users_posts_labels.csv",
}
# C-SSRS-based labels of Gaur et al. (2019); "Supportive" users post support for others.
CSSRS_IDEATION = {"Supportive": (0, 0), "Indicator": (0, 1), "Ideation": (1, 5), "Behavior": (3, 5), "Attempt": (3, 5)}
CSSRS_RISK = {"Supportive": "None", "Indicator": "Low", "Ideation": "High", "Behavior": "High", "Attempt": "Immediate"}


def _root(datasets_root):
    return Path(datasets_root) if datasets_root else DATASETS_ROOT


def _words(text, limit):
    words = text.split()
    return " ".join(words[:limit]) + (" ..." if len(words) > limit else "")


def _post_list(text):
    """Posts are stored as a Python list literal; some rows are not valid literals."""
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return [part.strip(" '\"") for part in re.split(r"""['"], ['"]""", text.strip().strip("[]"))]


def vera(path=DEFAULT_PERSONAS, datasets_root=None):
    rows = []
    for persona in load_personas(path):
        rows.append({
            "source": "vera_mh_si", "source_id": persona["persona_name"],
            "provenance": {"path": str(path), "commit": "8521169cb5a9032250bb37e7789fa3e9d33c62f9"},
            "name": persona["persona_name"], "backstory": persona["persona_context"],
            "risk_level": persona["risk_level"], "ideation_range": persona["ideation_range"],
            "disclosure_style": persona["disclosure_style"], "reaction_style": persona["reaction_style"],
            # VERA backstories already carry a communication style.
            "communication_style": None,
        })
    return rows


def sim_vail(datasets_root=None, control=False):
    folder = _root(datasets_root) / PATHS["sim_vail"]
    name = "auditor_instruction_labels_v1_control.json" if control else "auditor_instruction_labels_v1.json"
    rows = []
    for item in json.loads((folder / name).read_text(encoding="utf-8")):
        # Keep only the profile paragraph; the shared preamble is evaluator framing.
        profile = item["instruction_text"].rsplit("evaluation framework.", 1)[-1].strip()
        rows.append({
            "source": "sim_vail_control" if control else "sim_vail", "source_id": item["id"],
            "provenance": {"path": str(folder / name), "index": item["index"]},
            "vulnerability": {"phenotype": item["phenotype"], "intention": item["intention_label"],
                              "judge_dimension": item.get("primary_judge_dimension"), "profile": profile},
        })
    return rows


def patient_psi(datasets_root=None):
    folder = _root(datasets_root) / PATHS["patient_psi"] / "CCD"
    rows = []
    for path in sorted(folder.glob("*/patient*.json"), key=lambda p: (p.parent.name, int(p.stem[7:]))):
        data = json.loads(path.read_text(encoding="utf-8"))
        disorder = path.parent.name
        during = data.get(f"intermediate_beliefs_during_{disorder}", [])
        rows.append({
            "source": "patient_psi", "source_id": f"{disorder}:{path.stem}",
            "provenance": {"path": str(path)},
            "disorder": disorder, "backstory": data["life_history"],
            "cognition": {
                "core_beliefs": data["core_belief_description"],
                "intermediate_beliefs": data["intermediate_beliefs"] + during,
                "coping_strategies": data["coping_strategies"],
                "cognitive_models": data["cognitive_models"],
            },
        })
    return rows


def esconv(datasets_root=None):
    path = _root(datasets_root) / PATHS["esconv"]
    rows = []
    for index, item in enumerate(json.loads(path.read_text(encoding="utf-8"))):
        seeker = item["survey_score"]["seeker"]
        try:
            outcome = {"measure": "ESConv emotion intensity (1-5, seeker self-report)",
                       "before": int(seeker["initial_emotion_intensity"]),
                       "after": int(seeker["final_emotion_intensity"])}
        except (KeyError, ValueError):
            outcome = None
        utterances = [turn["content"].strip() for turn in item["dialog"] if turn["speaker"] == "seeker"]
        rows.append({
            "source": "esconv", "source_id": f"row-{index}", "provenance": {"path": str(path), "row_index": index},
            "situation": {"problem": item["problem_type"], "emotion": item["emotion_type"],
                          "description": item["situation"], "experience": item.get("experience_type")},
            "reference_outcome": outcome,
            "own_text": [u for u in utterances if len(u.split()) >= 5],
        })
    return rows


def attunebench(datasets_root=None):
    folder = _root(datasets_root) / PATHS["attunebench"]
    rows = []
    for path in sorted(folder.glob("conversation_*.json"), key=lambda p: int(p.stem.split("_")[1])):
        data = json.loads(path.read_text(encoding="utf-8"))
        profile = data["participant_profile"]
        rows.append({
            "source": "attunebench", "source_id": path.stem, "provenance": {"path": str(path)},
            "demographics": profile["demographics"], "trait_scores": profile["computed_scores"],
            "situation": {"problem": data["metadata"]["text"],
                          "attitude": profile.get("conversation_topic_attitude")},
            "reference_outcome": {"measure": "PANAS totals (10-50), participant self-report",
                                  "before": {"positive": data["prePanas"]["totalPositiveAffect"],
                                             "negative": data["prePanas"]["totalNegativeAffect"]},
                                  "after": {"positive": data["postPanas"]["totalPositiveAffect"],
                                            "negative": data["postPanas"]["totalNegativeAffect"]}},
            "own_text": [turn["userMessage"].strip() for turn in data["turns"]],
        })
    return rows


def reddit_cssrs(datasets_root=None):
    path = _root(datasets_root) / PATHS["reddit_cssrs"]
    csv.field_size_limit(sys.maxsize)
    rows = []
    with path.open(encoding="utf-8", errors="replace", newline="") as stream:
        for row in csv.DictReader(stream):
            posts = _post_list(row["Post"])
            label = row["Label"].strip()
            rows.append({
                "source": "reddit_cssrs", "source_id": row["User"], "provenance": {"path": str(path)},
                "cssrs_label": label, "risk_level": CSSRS_RISK[label], "ideation_range": list(CSSRS_IDEATION[label]),
                "own_text": [p.strip() for p in posts if len(p.split()) >= 5],
            })
    return rows


LOADERS = {"vera_mh_si": vera, "sim_vail": sim_vail, "patient_psi": patient_psi,
           "esconv": esconv, "attunebench": attunebench, "reddit_cssrs": reddit_cssrs}
