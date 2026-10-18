# VERA-MH (vendored subset)

- Repository: https://github.com/SpringCare/VERA-MH
- Commit: 8521169cb5a9032250bb37e7789fa3e9d33c62f9 (2026-10-13)
- License: `LICENSE` (MIT-style with conditions and disclaimer; keep it with these files)
- Concept paper: https://arxiv.org/abs/2510.15297

Copied unchanged:

| File | Used by |
| --- | --- |
| `SI/personas.tsv` | `guardbench.simulation.personas` (100 clinician-written suicide-risk personas) |
| `SI/persona_context_template.txt` | persona backstory rendering |
| `persona_prompt_template.txt`, `persona_prompt_reminder.txt` | reference for `prompts/user_simulator.yaml` (style rules adapted there) |
| `SI/rubric.tsv` | reference only; judging runs in the VERA-MH repo on exported transcripts |

Known upstream data issue: persona `Skylar` has a seed phrase in the
`Current Suicide Risk Level` column. The loader uses `Short Current Suicide Risk Level`
(`Low`) and the canonical long description for that level instead.

A VERA-MH score does not certify safety; see the disclaimer in `LICENSE`.
