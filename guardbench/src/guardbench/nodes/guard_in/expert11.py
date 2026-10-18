"""expert11 (sdx_guardrail) suicide/self-harm assessment as a g11 input guard.

The question set, level rules and Jev client are used unchanged from a pinned checkout of
github.com/yujinoh0103/sdx_guardrail in third_party/ (cloned on first use). This wrapper only
formats the conversation the way sdx does and maps expert11's levels to the g11 contract:
label safe/unsafe plus risk categories, with expert11's own codes kept alongside.
"""

import asyncio
import os
import subprocess
import sys
import threading

from ...core.node import Node, register
from ...llm.client import ROOT

SDX_DIR = ROOT / "third_party" / "sdx_guardrail"
SDX_REPO = "https://github.com/yujinoh0103/sdx_guardrail.git"
SDX_COMMIT = "bec3d12571790afad98b544fd3a34573b30832ed"
LEVELS = ("none", "low", "moderate", "high")
CATEGORY = "Suicide & Self-Harm"  # Qwen3Guard's name for the same risk, so both guards share one taxonomy
# expert11 reason codes -> finer subcategories reported with the category.
SUBCATEGORIES = (
    ("suicidal_behavior", "suicide_attempt_or_preparation"), ("preparation", "suicide_attempt_or_preparation"),
    ("suicidal_ideation_active_intent", "suicide_intent_or_plan"), ("suicidal_ideation_plan", "suicide_intent_or_plan"),
    ("suicidal_ideation", "suicidal_ideation"), ("self_harm", "self_harm"),
    ("unable_to_stay_safe", "unable_to_stay_safe"), ("indirect_warning", "indirect_warning_signs"),
)
_SDX, _LOCK = {}, threading.Lock()


def _sdx():
    """Import sdx modules once from the pinned checkout (cloning it if missing)."""
    with _LOCK:
        if _SDX:
            return _SDX
        if not (SDX_DIR / ".git").exists():
            SDX_DIR.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "--quiet", SDX_REPO, str(SDX_DIR)], check=True)
            subprocess.run(["git", "-C", str(SDX_DIR), "checkout", "--quiet", SDX_COMMIT], check=True)
        head = subprocess.run(["git", "-C", str(SDX_DIR), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        if head != SDX_COMMIT:
            raise RuntimeError(f"third_party/sdx_guardrail is at {head}, expected {SDX_COMMIT}")
        common = str(SDX_DIR / "common")
        if common not in sys.path:
            sys.path.append(common)  # sdx modules import each other by bare name
        import combine, jev_client, risk_score, rules  # noqa: E401
        _SDX.update(combine=combine, jev=jev_client, risk=risk_score, rules=rules,
                    questions=rules.load_design("expert11", "questions"), levels=rules.load_design("expert11", "levels"))
        return _SDX


def sdx_state(context, context_chars):
    """sdx format: earlier turns as 'User: ...' / 'Assistant: ...' lines, current as 'User: ...'."""
    lines = [f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}" for m in context[:-1]
             if m["role"] in ("user", "assistant")]
    return {"context": "\n".join(lines)[-context_chars:], "current": f"User: {context[-1]['content']}"}


def subcategories(reasons):
    found = []
    for prefix, name in SUBCATEGORIES:
        if any(code.startswith(prefix) for code in reasons) and name not in found:
            found.append(name)
    return found


@register("guard_in/expert11")
class Expert11(Node):
    def __init__(self, judge="jev", unsafe_from="low", context_chars=6000, workers=8, sleep=0.0):
        super().__init__(sleep)
        if judge not in ("jev", "kev", "clm"):
            raise ValueError("expert11 judge must be jev, kev or clm")
        if unsafe_from not in LEVELS[1:]:
            raise ValueError("unsafe_from must be low, moderate or high")
        if isinstance(context_chars, bool) or not isinstance(context_chars, int) or context_chars <= 0:
            raise ValueError("context_chars must be a positive integer")
        self.judge, self.unsafe_from, self.context_chars, self.workers = judge, unsafe_from, context_chars, workers

    def _assess(self, state):
        sdx = _sdx()
        session = sdx["jev"].make_session(self.workers, self.judge)
        try:
            raw = sdx["jev"].call_jev(session, state, sdx["questions"].JUDGES["all"])
        finally:
            session.close()
        answers = sdx["jev"].flatten(raw.get("answers", {}))
        codes = sdx["combine"].codes(answers, sdx["levels"])
        risk, components = sdx["risk"].acute_risk(answers)
        return codes, risk, components, raw

    async def run(self, inputs):
        await self.delay()
        context = inputs["context"]
        if not context or context[-1] != {"role": "user", "content": inputs["user"]}:
            raise ValueError("expert11 context must end with the current user")
        if self.judge == "jev" and not os.environ.get("OPENROUTER_API_KEY"):
            raise ValueError("expert11 with Jev needs OPENROUTER_API_KEY")
        state = sdx_state(context, self.context_chars)
        codes, risk, components, raw = await asyncio.to_thread(self._assess, state)
        unsafe = codes["level"] in LEVELS[LEVELS.index(self.unsafe_from):]
        return {
            "label": "unsafe" if unsafe else "safe",
            "categories": [CATEGORY] if unsafe else [],
            "subcategories": subcategories(codes["reasons"]) if unsafe else [],
            "expert11": {**codes, "risk": round(risk, 4)},
            "raw": {"judge": self.judge, "sdx_commit": SDX_COMMIT, "question_version": _sdx()["questions"].VERSION,
                    "unsafe_from": self.unsafe_from, "state": state,
                    "risk_components": {k: round(v, 4) for k, v in components.items()},
                    "cost": (raw.get("usage") or {}).get("cost"), "answers": raw.get("answers")},
        }
