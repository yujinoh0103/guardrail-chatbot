# Simulated-user personas (persona_v2)

## Code map

| Step | File |
| --- | --- |
| Read each source into a neutral record | `src/guardbench/simulation/sources.py` |
| Compose layers, rate vulnerabilities, fill gaps, drop incomplete | `src/guardbench/simulation/persona_builder.py` |
| Five vulnerabilities, levels, intentions, rater instructions | `prompts/vulnerability.yaml` |
| Section templates and style definitions | `prompts/persona_context.yaml` |
| Gap-filling instructions | `prompts/persona_fill.yaml` |
| CLI | `scripts/build_personas.py` |
| Load personas for a run (VERA TSV or built JSONL) | `src/guardbench/simulation/personas.py` |
| Simulator that talks to the chatbot | `src/guardbench/simulation/user.py`, `prompts/user_simulator.yaml` |
| Conversation loop and pipeline config | `src/guardbench/core/runner.py` (`run_simulated`), `configs/pipelines/simulated_user.yaml` |

```bash
python scripts/build_personas.py --out data/built/personas/v2_small.jsonl \
    --source vera_mh_si=6 --source sim_vail=6 --source sim_vail_control=2 --source patient_psi=4 \
    --source reddit_cssrs=4 --source esconv=3 --source attunebench=3 \
    --fill --model persona_writer_nano          # or persona_writer (gpt-5.6-luna)
python scripts/run.py --pipeline configs/pipelines/simulated_user.yaml \
    --data data/built/personas/v2_small.jsonl --dialogue-id esconv:row-825
```

Build order per persona: compose from sources and rules -> rate vulnerabilities from source evidence
only -> fill missing fields with the final vulnerabilities visible -> drop personas missing any section
(`--keep-incomplete` keeps them). `--fill-from <earlier output>` reuses ratings and fills by persona id.

## Sources (bases)

| Base | Path | Count | What it provides |
| --- | --- | --- | --- |
| `vera_mh_si` | `data/external/vera_mh/SI/personas.tsv` (vendored, commit 8521169) | 100 | Clinician-written backstory, suicide risk, disclosure style, communication style, reaction to suggestions |
| `sim_vail` | `datasets/02_multiturn_contextual/sim_vail/repro/petri_overrides/src/petri/tasks/v1/auditor_instruction_labels_v1.json` | 30 | Phenotype x intention profile text |
| `sim_vail_control` | same folder, `..._control.json` | 6 | Healthy users with the same six intentions |
| `patient_psi` | `datasets/02_multiturn_contextual/mhsafeeval/config/CCD/{depression,delusion,psychosis}/` | 58 | Patient-Psi-CM CBT conceptualization: life history, core and intermediate beliefs, coping, situation-thought-emotion-behavior |
| `esconv` | `datasets/03_conversation/esconv/ESConv.json` | 1300 | Problem and emotion, situation, own messages, real before/after emotion intensity |
| `attunebench` | `datasets/02_multiturn_contextual/attunebench/Test Samples/Sample200/` | 200 | Demographics and trait scores, topic and attitude, own messages, real pre/post PANAS |
| `reddit_cssrs` | `datasets/04_reference_corpora/reddit_cssrs_2021_timevariant/500_anonymized_...csv` | 500 | C-SSRS-based label (Gaur et al. 2019) and own posts |

## Vulnerabilities and intention

SIM-VAIL's design is a full 6 x 6 factorial. Its **intentions** are independent of the phenotype (the same
six are applied to every phenotype and to healthy controls), so they are assigned independently here.
Its **phenotypes** are clinical, so they are replaced by five non-diagnostic vulnerabilities that every
persona has at some level:

| Vulnerability | Replaces | Basis |
| --- | --- | --- |
| `negative_self_belief` | DEPRESSION | Beck cognitive vulnerability-stress model |
| `abandonment_anxiety` | INSECURE_ATTACHMENT | ECR attachment anxiety (Brennan et al. 1998) |
| `intolerance_of_uncertainty` | OCD | transdiagnostic intolerance of uncertainty |
| `mistrust` | PSYCHOSIS | paranoia continuum in the general population (Bebbington, Freeman et al. 2013) |
| `emotional_urgency` | MANIA_HYPOMANIA | UPPS-P positive and negative urgency |

Level = max(rule floor, LLM rating):

- Rule floors come only from what a source states about this person: SIM-VAIL phenotype -> high;
  Patient-Psi delusion/psychosis -> `mistrust` high; Patient-Psi own core beliefs -> mid;
  VERA-MH diagnoses (unipolar depression, bipolar, obsessive/intrusive, psychosis) -> mid.
  Borrowed donor beliefs set no floor.
- The LLM rater sees source evidence only (no LLM-filled text, no donor beliefs, no other people's
  example messages) and must quote it. A non-low level whose quote is not found in that evidence is
  dropped to low and kept as `unsupported_level`. In the first small build 32 of 38 rejected quotes
  had been copied from the dimension descriptions rather than the profile.
- `primary` is the highest level; ties go to source-stated levels over LLM ratings; all low -> none.

Intention is one of seven: the six SIM-VAIL intentions (phenotype-free wording adapted from the
SIM-VAIL healthy-control instructions) or `none`, a plain help-seeker. SIM-VAIL bases keep their own
intention and profile text; all others get a balanced rotation.

## Fields

| Field | Meaning | Where it comes from |
| --- | --- | --- |
| `dialogue_id`, `persona_name`, `dataset` | id (`source:original id`), filename-safe name, schema version | code |
| `base` | base source, original id and file location | source |
| `layers` | every borrowed or rule-assigned layer and its origin | code |
| `backstory` | life context | VERA-MH, Patient-Psi original; LLM elsewhere (Reddit from own posts) |
| `basics` | age range, gender | rule (Patient-Psi gender from he/she); "not stated" for ESConv and Reddit, whose own text may imply them |
| `demographics`, `trait_scores` | real participant profile and questionnaires | AttuneBench original |
| `risk_level`, `ideation_range` | suicide risk level and allowed opening C-SSRS ideation range | VERA-MH original; Reddit label by rule |
| `cssrs_label`, `disorder` | Reddit C-SSRS label; Patient-Psi disorder | original |
| `situation` | topic, main feeling, what is going on | ESConv original; AttuneBench topic original; LLM elsewhere (VERA-MH has it in its backstory) |
| `sim_vail` | original SIM-VAIL phenotype, intention, judge dimension, profile text | SIM-VAIL original |
| `vulnerability` | `levels` (five dimensions, each level + origin + quote), `primary`, `intention`, `rating` (raw LLM rating) | rules + LLM rating, see above |
| `cognition` | core beliefs, rules and assumptions, coping, typical moment | Patient-Psi original; core beliefs from a donor (at-risk VERA-MH, Reddit Indicator+, SIM-VAIL depression/attachment/psychosis) or LLM; the rest LLM |
| `help_profile` | hopes, accepted and rejected help, responses that help or hurt | LLM (CAMI / DynPatient style) |
| `reveal_timing` | when the most painful part comes out (early, mid, late) | VERA-MH disclosure style; CRADLE-style rotation elsewhere |
| `communication_style`, `reaction_style` | how the person talks; reaction to suggestions | VERA-MH original; Patient-Psi types and VERA-MH styles by rotation elsewhere |
| `style_examples` | three real messages to copy the voice from | own text, else real ESConv seeker messages |
| `reference_outcome` | the real person's before/after rating | ESConv, AttuneBench |
| `applicable_axes` | conversation-observable evaluation axes for this persona | rule: base axes + suicide axes if at risk + vulnerability (mid/high) and intention axes |
| `filled_by_llm` | model, filled fields and their values | LLM fill record |
| `persona_context` | the rendered backstory; the only persona text the simulator reads | template |

Every persona written without `--keep-incomplete` has all nine sections: backstory, situation,
vulnerability, intention, cognition, help profile, reveal timing, communication style, style examples.

Notes:

- Age and gender are never LLM-written: a gpt-5-nano trial produced 31 of 34 personas under 35 and
  malformed genders.
- Reddit C-SSRS posts contain upstream entity-normalization artifacts (for example "Tired", "Pain"
  substituted into sentences). They pass into own-post style examples unchanged.
- Rotations (communication style, reaction style, reveal timing, intention) follow persona order.
  Every other random layer has its own seed per persona id and layer, so it stays stable.
- `data/built/personas/mixed_v1.jsonl` is the earlier persona_v1 build (phenotype-only vulnerability,
  incomplete sections). It still runs, but its `vulnerability` field means the SIM-VAIL profile.
