# SWE-bench Verified Mini — optimization ledger

Single-agent lionagi coding agent, run entirely inside a Daytona sandbox, scored
by the official deterministic `swebench` Docker oracle (no LLM judge). Each row
changes **one** variable from the row above so the delta is attributable.

Discipline: **all changes stay general** (reproduce/localize/verify recipes,
tool/loop fixes) — never per-instance hacks. Gains are validated on a held-out
slice from full Verified-500 (`load_holdout`) to prove they aren't overfit to
these 50.

Metric: RESOLVED = model_patch + held-out test_patch makes FAIL_TO_PASS pass and
keeps PASS_TO_PASS passing.

| run | model | max_ext | prompt | other | patches | RESOLVED /50 | notes |
|-----|-------|---------|--------|-------|---------|--------------|-------|
| v1  | gpt-5.4-mini | 30 | bare | concurrency 7 (buggy) | 13 | **8 (16%)** | ReActAnalysis.analysis required → discarded valid tool calls; 17 sphinx lost to CPU-cap cascade |
| v2  | gpt-5.4-mini | 30 | bare | fix(react)+create-retry | 35 | **17 (34%)** | analysis optional → engagement 39%→70%; all 50 ran |
| v3  | gpt-5.4-mini | 80 | bare | budget sweep | … | … | in flight — does more budget rescue revert-and-ran-out? |
| v4  | gpt-5.4-mini | TBD | **recipe** | reproduce→localize→minimal→verify→refine; no-revert-to-nothing; must leave non-empty edit | … | … | staged |

## Reference points (published, NOT apples-to-apples — different scaffolds/sets)
- GPT-5.4 (full) ~78% SWE-bench Verified (vals.ai); GPT-5.4-mini 54.4% SWE-bench **Pro**.
- mini×Verified×Mini-50 not published. Our number = *our scaffold's efficiency*, the gap to ~50s% is harness headroom.
- Calibration TODO: mini-SWE-agent on the same Mini-50 for an off-the-shelf-scaffold reference.

## Failure modes seen (from --keep-sandbox transcripts)
- **analysis-drop** (FIXED v2): turn omits `analysis` → validation fails → actions discarded → hallucinated success.
- **revert-and-quit** (sphinx-10323): correct fix applied, regressed PASS_TO_PASS, model reverted to nothing + honestly gave up. Target of v4 recipe.
- **over-broadening**: first fix changes a shared path → regression. Target of minimal-fix prompting.
- **non-engagement (e0)**: residual after v2; recipe's mandatory reproduce step should pull these in.
