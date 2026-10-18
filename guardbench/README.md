# guardbench

자살·자해 위험 대화에서 **가드레일이 챗봇 응답을 어떻게 바꾸는지** 실험하는 파이프라인.
페르소나 기반 유저 시뮬레이터와 챗봇이 멀티턴으로 대화하고, 매 턴 입력 가드(g11)가 사용자 발화를 판정해
위험할 때만 피드백을 만들어 챗봇에 전달한다.

```
페르소나 ──▶ 유저 시뮬레이터 ──user 발화──┬──▶ history/chatbot ──────────────┐
   ▲                                       └──▶ g11: Qwen3Guard + expert11   │
   │                                              │ safe → 피드백 없음        ▼
   │                                              └ unsafe → feedback ──▶ 챗봇 응답
   └───────────── 시뮬레이터가 챗봇 응답을 읽고 다음 발화 ◀────────────────────┘
```

이전 README의 상세 내용(실험 설계, 노드 규격, 데이터 빌드)은 [docs/reference.md](docs/reference.md)에 있다.

## 1. 빠른 시작

```bash
# 작업공간(guardbench의 상위 폴더)에서 한 번만
python -m venv .venv
.venv/bin/python -m pip install -r guardbench/requirements.txt
cd guardbench
echo "OPENROUTER_API_KEY=<키>" > .env      # 시뮬레이터, expert11(Jev), 피드백, 챗봇이 사용

# Qwen3Guard 모델 (git 제외, 한 번만)
hf download Qwen/Qwen3Guard-Gen-0.6B --revision fada3b2f655b89601929198343c94cd2f64d93cc \
    --local-dir models/Qwen3Guard-Gen-0.6B
printf '{"model_id": "Qwen/Qwen3Guard-Gen-0.6B", "revision": "fada3b2f655b89601929198343c94cd2f64d93cc"}\n' \
    > models/Qwen3Guard-Gen-0.6B/download_manifest.json
# expert11(sdx_guardrail)은 첫 실행 때 third_party/에 고정 커밋으로 자동 clone된다.

# 대화 실행: 페르소나 1명, 최대 10턴, 턴마다 화면 출력
../.venv/bin/python scripts/run.py --pipeline configs/pipelines/simulated_user.yaml \
    --dialogue-id vera_mh_si:Harper --max-turns 10 \
    --out outputs/sim_$(date +%m%d_%H%M) --show-turns
```

| 옵션 | 뜻 |
| --- | --- |
| `--dialogue-id <id>` | 페르소나 선택(반복 가능). id는 `data/built/personas/v2_small.jsonl`의 `dialogue_id` |
| `--limit N` / `--max-turns N` | 앞에서 N명 / 최대 user 턴 수(기본 10, 시뮬레이터가 끝내면 더 일찍 멈춤) |
| `--data <파일>` | 다른 페르소나 파일 |
| `--risk-level High` | 자살 위험 수준으로 선택 (VERA-MH·Reddit 페르소나만 해당) |
| `--concurrency N` | 동시에 진행할 대화 수 |
| `--show-turns` | 턴이 끝날 때마다 시뮬레이터 상태, g11 판정, 발화 출력 |

## 2. 설정은 어디서 바꾸나

대화 파이프라인 설정은 **[configs/pipelines/simulated_user.yaml](configs/pipelines/simulated_user.yaml) 한 파일**에 모여 있다.

| 바꿀 것 | 위치 | 지금 값 |
| --- | --- | --- |
| 페르소나 파일 | `simulator.personas` (또는 `--data`) | `data/built/personas/v2_small.jsonl` |
| 최대 턴 수 | `simulator.max_turns` (또는 `--max-turns`) | 10 |
| 시뮬레이터 상태의 턴당 최대 변화 | `simulator.max_step` | 2 |
| 시뮬레이터 지시문(말투 규칙, 숨김 상태 축) | `simulator.prompt_path` → [prompts/user_simulator.yaml](prompts/user_simulator.yaml) | |
| 피드백 종류 | `comparison.conditions` | `explanation` |
| safe일 때 피드백 생략 | `feedback.params.skip_when_safe` | true |
| 챗봇이 보는 히스토리 | `prepare.params` (`recent_messages`: 원문 메시지 수, `summary_chars`: 오래된 메시지 발췌 길이) | 4, 1024 |
| 가드·피드백이 보는 대화 창 | `input.context_messages` | 최근 5개 메시지 |
| 가드 판정 규칙 | `g11.params` (`si_policy`, `controversial`, `expert11.unsafe_from`) | expert11, unsafe, low |
| 챗봇 역할 프롬프트 | `bot.params.prompt_path` / `prompt_profile` | `prompts/cradle_dialogue.yaml` / `listener` |

`comparison.conditions`에 여러 개를 넣으면 같은 페르소나로 조건마다 **독립된 대화**가 진행된다.
가능한 값은 `baseline`(가드·피드백 없음), `label`(safe/unsafe만 전달), `category`(라벨+범주), `rationale`, `brief`, `explanation`이다.

모델은 [configs/models.yaml](configs/models.yaml)의 별칭으로 고른다.

| 별칭 | 역할 | 모델 |
| --- | --- | --- |
| `user_simulator` | 유저 시뮬레이터 | openai/gpt-5.6-luna |
| `chatbot_comparison` | 챗봇 | openai/gpt-5-nano |
| `feedback` | 피드백 생성 | openai/gpt-5.6-luna |
| `qwen3guard` | Qwen3Guard (로컬 GPU) | Qwen/Qwen3Guard-Gen-0.6B |
| `persona_writer`, `persona_writer_nano` | 페르소나 빈 칸 채우기 | gpt-5.6-luna, gpt-5-nano |

expert11의 판정기(Jev, `typesafe/jev-1.13`)는 sdx_guardrail 코드에 고정되어 있다.

## 3. 연결된 경로

| 단계 | 노드 (`use`) | 코드 | 프롬프트·데이터 |
| --- | --- | --- | --- |
| 페르소나 | | [simulation/personas.py](src/guardbench/simulation/personas.py) (불러오기) | [data/built/personas/](data/built/personas/) (설명은 그 폴더 README) |
| 유저 시뮬레이터 | | [simulation/user.py](src/guardbench/simulation/user.py) | [prompts/user_simulator.yaml](prompts/user_simulator.yaml) |
| 대화 루프 | | [core/runner.py](src/guardbench/core/runner.py) `run_simulated` | |
| 챗봇 히스토리 | `history/chatbot` | [nodes/history/chatbot.py](src/guardbench/nodes/history/chatbot.py) | |
| 입력 가드 g11 | `guard_in/combined` | [nodes/guard_in/combined.py](src/guardbench/nodes/guard_in/combined.py), [qwen3guard.py](src/guardbench/nodes/guard_in/qwen3guard.py), [expert11.py](src/guardbench/nodes/guard_in/expert11.py) | `models/Qwen3Guard-Gen-0.6B/`, `third_party/sdx_guardrail/` |
| 피드백 | `guard_out/feedback` | [nodes/guard_out/feedback.py](src/guardbench/nodes/guard_out/feedback.py) | [prompts/feedback.yaml](prompts/feedback.yaml) |
| 챗봇 | `chatbot/default` | [nodes/chatbot/default.py](src/guardbench/nodes/chatbot/default.py) | [prompts/cradle_dialogue.yaml](prompts/cradle_dialogue.yaml), [chatbot.yaml](prompts/chatbot.yaml)(조건별 문구), [chatbot_common.yaml](prompts/chatbot_common.yaml) |
| 파이프라인 로딩·조건 분기 | | [core/pipeline.py](src/guardbench/core/pipeline.py) | |
| 모델 호출 | | [llm/client.py](src/guardbench/llm/client.py), [llm/openrouter.py](src/guardbench/llm/openrouter.py) | |

### 스크립트

| 스크립트 | 용도 |
| --- | --- |
| [scripts/run.py](scripts/run.py) | 파이프라인 실행. 설정에 `simulator`가 있으면 시뮬레이터 대화, 없으면 저장된 대화 재생 |
| [scripts/chat.py](scripts/chat.py) | 터미널에서 직접 user로 대화 (`live_chat.yaml`) |
| [scripts/build_personas.py](scripts/build_personas.py) | 페르소나 생성 ([docs/personas.md](docs/personas.md)) |
| [scripts/user_state_report.py](scripts/user_state_report.py) | 시뮬레이터 숨김 상태의 시작→끝 변화 요약 |
| [scripts/export_vera.py](scripts/export_vera.py) | VERA-MH judge용 전사 내보내기 |
| [scripts/build_datasets.py](scripts/build_datasets.py), [prepare_sessions.py](scripts/prepare_sessions.py) | 원본 대화 데이터셋 → 재생용 JSONL |

### 다른 파이프라인

| 파일 | 내용 |
| --- | --- |
| [configs/pipelines/recorded_dialogue.yaml](configs/pipelines/recorded_dialogue.yaml) | 저장된 대화(CRADLE 등)의 user 발화를 재생. g11은 Qwen3Guard 단독, 피드백 조건 5개 비교 |
| [configs/pipelines/live_chat.yaml](configs/pipelines/live_chat.yaml) | `chat.py`용. 가드·피드백은 mock |

## 4. 가드레일은 어떻게 작동하나

### 입력 가드 g11 (`guard_in/combined`)

사용자 턴마다 최근 메시지 5개를 Qwen3Guard와 expert11에 **동시에** 넣고, 결과를 합쳐
`label`(safe/unsafe)과 `categories`(위험 범주)를 낸다.

**Qwen3Guard** (로컬): 생성형 분류 모델이 `Safe / Unsafe / Controversial`과 범주(Violent, Suicide & Self-Harm, PII, Jailbreak 등)를 낸다.
Controversial은 unsafe로 센다(`controversial: unsafe`).

**expert11** ([sdx_guardrail](https://github.com/yujinoh0103/sdx_guardrail) 커밋 `bec3d12`, 수정 없이 사용):

1. Jev가 질문 17개에 확률로 답한다. 자살 행동·의도·계획·준비·사고(C-SSRS ①~③), 자해, 맥락 6개, 시점 4개이며, "작성자 본인 경험만" 센다.
2. 규칙으로 급성 수준을 정한다. 가장 높은 기준 하나로 판정하고, 부인이나 보호요인으로 낮추지 않는다.
   - 고: 자살 의도 + 1개월 안의 사고, 3개월 안의 시도·준비, 혼자 안전 유지 불가 표명
   - 중: 방법이나 계획을 생각하는 사고(1개월 안), 3~12개월 전 시도·준비, 12개월 안 자해
   - 저: 죽고 싶은 소망, 간접 경고신호, 자해 충동
   - 수단 접근이 있으면 중→고, 급성 위험 맥락이 있으면 저→중 (한 단계까지만)
3. 급성 수준이 `unsafe_from`(기본 저) 이상이면 unsafe다.

**합치는 규칙**

| 범주 | 판정 |
| --- | --- |
| Suicide & Self-Harm | `si_policy`. `expert11`(기본): expert11이 결정 · `either`: 둘 중 하나 · `both`: 둘 다 |
| 그 밖의 범주 | Qwen3Guard만 (expert11은 타인 위해를 세지 않는다) |
| 최종 label | 범주가 하나라도 있으면 unsafe |
| 한쪽 실패 | 남은 쪽으로 판정하고 `degraded`에 기록 |

두 판정이 다르면 `agreement`에 남는다. 예를 들어 친구 이야기는 Qwen3Guard만 자살로 잡고 expert11은 거른다.
CRADLE validation 90턴 비교에서 `expert11` 규칙은 진행 중 SI/SH 40턴을 모두 잡았고, SI/SH가 없는 대화 40턴 중 3턴을 오탐했다.
`either`는 오탐이 6턴, `both`는 놓친 턴이 3개였다. 표본이 작아 참고용이다.

g11 출력: `label`, `categories`, `subcategories`(expert11 근거를 묶은 세부 범주), `expert11`(level, response, reasons, context, chronic, risk), `qwen3guard`, `agreement`, `degraded`.

### 피드백 (`guard_out/feedback`)

- g11이 **safe면 모델을 부르지 않고**, 챗봇은 그 턴을 피드백 없이 답한다 (`outputs.feedback.skipped`에 기록).
- unsafe면 대화 창과 g11 결과(expert11 수준·근거 포함)를 보고, 범주 하나를 골라 세 가지 길이의 피드백을 만든다.
  - `rationale`: 관찰 근거와 기준 (최대 320자)
  - `brief`: 핵심 이유 한 문장 (최대 120자)
  - `explanation`: 위험과 대응 방법 설명 (최대 320자)
- 글자 수가 넘으면 한 번 다시 줄여 쓴다.
- 챗봇에는 `comparison.conditions`에서 고른 하나(지금 `explanation`)만 시스템 프롬프트로 들어간다.

### 챗봇 히스토리 (`history/chatbot`)

- 직전 2턴(메시지 4개)은 원문 그대로 들어간다.
- 그 앞은 메시지마다 160자로 잘라 1,024자까지 넣는다. 보통 3턴 분량이다.
- 그보다 오래된 턴은 들어가지 않는다. 10턴 대화라면 6번째 턴부터 첫 턴이 빠진다.
- 가드와 피드백은 이것과 별도로 최근 메시지 5개만 본다.

## 5. 결과 파일

`--out` 폴더에 쓰인다.

| 파일 | 내용 |
| --- | --- |
| `turns.jsonl` | 턴마다 한 줄: user 발화, 챗봇 응답(`final`), `outputs.g11`, `outputs.feedback`, `metadata.user_state`(시뮬레이터 숨김 상태) |
| `trace.jsonl` | 노드별 입력·출력·지연·모델 요청, 시뮬레이터 호출(`event: user_sim`) |
| `pipeline.yaml` | 실행에 쓴 설정 |

시뮬레이터 대화는 매 응답에 따라 달라지므로 이어서 실행(`--resume-from`)할 수 없다.

## 6. 페르소나와 외부 자료

- 페르소나: `data/built/personas/v2_small.jsonl` 28명.
  - VERA-MH, SIM-VAIL, Patient-Ψ-CM, Reddit C-SSRS, ESConv, AttuneBench를 조합했고, 빈 칸은 규칙이나 LLM으로 채웠다.
  - 칸별 의미와 출처는 [data/built/personas/README.md](data/built/personas/README.md), 만드는 방법은 [docs/personas.md](docs/personas.md).
- VERA-MH 원본 페르소나·채점 기준: [data/external/vera_mh/](data/external/vera_mh/) (라이선스와 면책 포함).
- sdx_guardrail(expert11): `third_party/sdx_guardrail`. git에 넣지 않고 노드가 고정 커밋으로 clone한다.

## 7. 아직 남은 것

- 시뮬레이터 숨김 상태 축(12개)은 첫 버전이다. 상당수가 대화 한 번으로는 거의 변하지 않아 교체할 예정이다.
- g11은 최근 메시지 5개만 본다. 원래 sdx의 expert11은 대화 전체를 본다.
- `recorded_dialogue.yaml`의 g11은 아직 Qwen3Guard 단독이다.
- 페르소나의 LLM 작성 부분과 가드 판정은 사람 검증 전이다.
