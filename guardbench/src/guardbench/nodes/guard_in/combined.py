"""Qwen3Guard + expert11 as one g11 input guard: label safe/unsafe and risk categories.

Who decides what:
- Suicide & Self-Harm: both guards cover it. `si_policy` picks the rule (default `expert11`):
  expert11  expert11 decides; it applies the clinical criteria, time windows and the
            "only the writer's own experience" filter that Qwen3Guard lacks.
  either    unsafe if either guard flags it (most sensitive).
  both      unsafe only if both flag it (most specific).
  Disagreements are always recorded in `agreement`.
- Every other category (violence, illegal acts, PII, jailbreak, ...): Qwen3Guard only;
  expert11 deliberately ignores harm to others.
- Qwen3Guard "Controversial" counts as unsafe unless `controversial: safe`.
- If one guard fails, the other decides and `degraded` names the failed one; if both fail the
  label is `unresolved`.
"""

import asyncio

from ...core.node import Node, register
from .expert11 import CATEGORY, Expert11
from .qwen3guard import Qwen3Guard


@register("guard_in/combined")
class CombinedGuard(Node):
    def __init__(self, qwen_model="qwen3guard", models_config="configs/models.yaml", si_policy="expert11",
                 controversial="unsafe", expert11=None, sleep=0.0):
        super().__init__(sleep)
        if si_policy not in ("expert11", "either", "both"):
            raise ValueError("si_policy must be expert11, either or both")
        if controversial not in ("unsafe", "safe"):
            raise ValueError("controversial must be unsafe or safe")
        self.qwen = Qwen3Guard(model=qwen_model, models_config=models_config)
        self.expert11 = Expert11(**(expert11 or {}))
        self.si_policy, self.controversial = si_policy, controversial

    def _qwen_flags(self, result):
        flagged = result["label"] == "unsafe" or (result["label"] == "controversial" and self.controversial == "unsafe")
        categories = result["categories"] if flagged else []
        return CATEGORY in categories, [c for c in categories if c != CATEGORY]

    async def run(self, inputs):
        await self.delay()
        qwen, expert = await asyncio.gather(self.qwen.run(inputs), self.expert11.run(inputs), return_exceptions=True)
        failed = [name for name, r in (("qwen3guard", qwen), ("expert11", expert)) if isinstance(r, BaseException)]
        if len(failed) == 2:
            raise RuntimeError(f"both guards failed: qwen3guard: {qwen!r}; expert11: {expert!r}")
        q_si, q_other = self._qwen_flags(qwen) if "qwen3guard" not in failed else (None, [])
        e_si = expert["label"] == "unsafe" if "expert11" not in failed else None
        if q_si is None:
            si = e_si
        elif e_si is None:
            si = q_si
        else:
            si = {"expert11": e_si, "either": e_si or q_si, "both": e_si and q_si}[self.si_policy]
        if q_si is None or e_si is None:
            agreement = "single_guard"
        else:
            agreement = "agree" if q_si == e_si else "expert11_only" if e_si else "qwen3guard_only"
        categories = ([CATEGORY] if si else []) + q_other
        return {
            "label": "unsafe" if categories else "safe",
            "categories": categories,
            "subcategories": expert.get("subcategories", []) if si and "expert11" not in failed else [],
            "agreement": {"suicide_self_harm": agreement, "si_policy": self.si_policy},
            "degraded": failed,
            "expert11": expert.get("expert11") if "expert11" not in failed else None,
            "qwen3guard": ({"label": qwen["label"], "categories": qwen["categories"]}
                           if "qwen3guard" not in failed else None),
            "raw": {"qwen3guard": qwen["raw"] if "qwen3guard" not in failed else repr(qwen),
                    "expert11": expert["raw"] if "expert11" not in failed else repr(expert),
                    "controversial": self.controversial},
        }
