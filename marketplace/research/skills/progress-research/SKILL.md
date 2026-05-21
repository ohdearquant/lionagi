---
name: progress-research
description: >
  Multi-round ChatGPT research pipeline with drill, evaluate, and progression tracking.
  Suggest when: "next round", "R4/R5/R6", "research prompts", "drill and send",
  "evaluate responses", "progress research", "chatgpt research", "deep research",
  "research pipeline", exploring new asset classes, strategies, or topics needing
  broad literature coverage.
allowed-tools: [Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch]
---

# /progress-research

Multi-round ChatGPT Deep Research pipeline with quality gates. Each round: drill -> refine -> fire -> evaluate -> record.

Use ChatGPT Deep Research as a parallel breadth-exploration engine, with Claude (Opus) handling synthesis, validation, and decision-making. ChatGPT drills deeper on literature and theory (50+ min per query); Claude cross-references, catches hallucinations, and decides what's tradable/buildable.

## When to Use

- Starting a new ChatGPT research round (R1, R5, R6, ...)
- Evaluating incoming ChatGPT responses
- Updating the research progression after a round completes
- Ocean says "next round", "send prompts", "evaluate responses", "drill into X"
- New asset class or strategy exploration
- Literature review on a specific quant topic
- Competitive analysis across a broad space
- Any research where breadth matters more than speed
- When you need 8-10 perspectives on the same question

## When NOT to Use

- Quick factual lookups (use WebSearch)
- Implementation tasks (just build it)
- Anything where the answer is in our existing codebase (use Read/Grep)
- Time-sensitive decisions (ChatGPT takes 15-50 min per prompt)

---

## Bootstrap: Starting a New Research Topic (R1-R3)

Use these phases when beginning a fresh topic with no prior rounds. Skip to Phase 0 if rounds already exist.

### R1: Broad Exploration — Context Load + Mega-Prompt

**Goal**: Map the landscape. Get 8-10 concrete research directions.

**Context auto-population** before generating the mega-prompt:

```python
# From memory
memory.recall("current capital positions portfolio", limit=3)
memory.recall("{topic} prior research findings", limit=5)


# From task queue
work.tasks(limit=5)

# From project state — read if they exist
# Read RESEARCH_LOG.md, RESEARCH_CATALOG.md
```

**Generate the R1 mega-prompt**:

```markdown
## Context (auto-populated)
- Available capital: [from task queue / memory]
- Platforms: [Kalshi, KuCoin, Coinbase — from project state]
- Data assets: [what data we already have]
- Constraints: [fees, regulatory, technical]
- Academic framework: [relevant theory we already know]

## Request
Given the above context, generate 8-10 specific, CONCRETE research directions for: {TOPIC}

For each direction:
1. One-sentence thesis (falsifiable)
2. Required data (do we have it or need to acquire?)
3. Expected edge mechanism (WHY would this work?)
4. Fatal flaw check (what kills this idea?)
5. Tractability × Impact × Novelty score (1-10 each)

Rank by total score. Flag any direction where fatal flaw is confirmed.

DO NOT give me theoretical curiosities. Every direction must terminate in either:
(a) a tradeable strategy, (b) a buildable product, or (c) an explicit "this cannot work because X"
```

**Fire**: Open ChatGPT Pro in browser, send mega-prompt.
**Save**: `.khive/workspaces/{date}/chatgpt-research/{topic}/round1_mega_prompt.md` and `round1_output.md`

### R2: Parallel Deep Dives (8-10 prompts → ChatGPT parallel)

**Goal**: Deep-drill into each surviving direction from R1.

**Claude curates R1 output**:
- Kill directions with confirmed fatal flaws
- Add our specific context (fees, API limits, capital constraints)
- Refine each surviving direction into a specific deep-dive prompt

**Prompt template per direction**:

```markdown
## Context
[Same context block as R1, plus R1 findings for this direction]

## Deep Dive: {Direction Name}

Research this specific direction in depth:
1. Literature: What academic papers cover this? (names, years, key results)
2. Prior art: Has anyone implemented this? (companies, funds, open source)
3. Mathematics: What is the pricing/valuation framework? (formulas, not just concepts)
4. Data requirements: Exactly what data, what frequency, what history depth?
5. Implementation: Pseudocode for the core algorithm
6. Risk model: What are the loss scenarios? Max drawdown estimate?
7. Fee impact: At [specific fee structure], what minimum edge is needed to be profitable?
8. Scale limits: At what capital level does this strategy break down?

Be SPECIFIC. I need numbers, not narratives.
```

**Fire**: Open ChatGPT Pro in browser, send all prompts in parallel tabs.
**Save**: `.khive/workspaces/{date}/chatgpt-research/{topic}/round2_prompts/` and `round2_outputs/`

### R3: Synthesis + Scope (Claude — NOT ChatGPT)

**Goal**: Cross-reference all R2 outputs. Decide which 2-3 directions to pursue.

**Claude's checklist**:
- [ ] Cross-reference claims across directions (contradictions?)
- [ ] Fee reality check (plug in actual Kalshi/KuCoin/Coinbase fees)
- [ ] Data availability check (do we actually have the data, or is ChatGPT assuming?)
- [ ] Scale check (does this work at our capital level, $100-10K?)
- [ ] Math verification (re-derive key formulas — ChatGPT hallucinates math)
- [ ] Novelty check (is someone already doing this better?)

**Output**: `round3_synthesis.md` — 2-3 surviving directions with go/no-go decision and rationale. Then proceed to the Phase workflow below for R4+.

---

## Ongoing Rounds Workflow (R4+)

### Phase 0: Context Load (lambda does directly)

```bash
# 1. Read the handoff from last round
Read(".khive/workspaces/YYYYMMDD/HANDOFF_R{N+1}_PLANNING.md")

# 2. Check research progression state
Read(".khive/workspaces/research_progression/README.md")
wc -l .khive/workspaces/research_progression/*.md

# 3. Check what responses/evaluations exist
ls .khive/workspaces/YYYYMMDD/chatgpt-responses/
ls .khive/workspaces/YYYYMMDD/evaluations/
```

Present status table to Ocean. Ask what to do or proceed if standing orders say "keep the steam going."

### Phase 1: Drill Agents (parallel, max 5)

For each prompt topic, launch an **analyst (Opus)** agent that:

1. **Reads** the prior round's ChatGPT response (R{N-1}) for follow-ups
2. **Reads** our empirical data (adverse_selection.jsonl, category_spreads.jsonl, etc.)
3. **Pulls live Kalshi/KuCoin data** via existing CLI or API calls
4. **Reads** our proven computation results (fee proof, Greeks, etc.)
5. **Identifies gaps** in the prior response (wrong fee model, missing data, hallucinated claims)
6. **Produces a refined prompt** with our REAL data embedded

**Agent output**: `.khive/workspaces/YYYYMMDD/r{N}_drill/R{N}_XX_topic.md`

Each drill output MUST contain:
```markdown
# R{N}-{XX}: {Topic}

## Gaps Identified in Prior Round
[numbered list of corrections/gaps]

## Our Empirical Data
[real numbers from our data files, not agent guesses]

## Refined Prompt
[the actual prompt to copy-paste to ChatGPT Pro]
```

**Mandatory checks per drill agent**:
- Fee model: use PROVEN formula (maker = 1c always for 1-lot; multi-lot = ceil(0.0175 * C * P * (1-P) * 100)c)
- Data identity: verify Kalshi category labels match actual content
- Prior corrections: check 08_corrections.md for known errors in this topic area

### Phase 2: Batch and Present

Group prompts into batches of 2-3 for ChatGPT Pro parallel processing:

| Batch | Priority | Criteria |
|-------|----------|----------|
| Batch 1 | P0 (operational) | Directly improves current trading or resolves blockers |
| Batch 2 | P1 (research) | New topics with live data embedded |
| Batch 3 | P2 (academic) | Capstone/paper material, lower urgency |

For each prompt, specify:
- **Follow-up** (send in same ChatGPT conversation) vs **New session** (self-contained)
- Which prior response to paste as context (if follow-up)

Present to Ocean: "Ready for Batch 1. [N] prompts. Fire when ready."

### Phase 3: Evaluate Responses (parallel, max 5)

When ChatGPT responses arrive, launch **analyst (Opus)** evaluators. One per response.

Each evaluator applies **three checks**:

#### Rigor Check
- Math formulas correct?
- Fee model matches our proven numbers?
- Statistical methods appropriate?
- Logical consistency (no internal contradictions)?

#### Daydream Check
- Feasible at our current capital (check current balances from task input)?
- Integrates with our actual codebase (trader.py, market_maker.py, cli.py)?
- Not over-engineered for our scale?
- Addresses known blockers (liquidity, execution, data availability)?

#### Numbers Check
- Specific numbers traceable to sources?
- Revenue projections match known constraints?
- Empirical claims consistent with our data?
- No hallucinated statistics?

#### Citation Audit (for academic responses)
- Every paper: full citation extracted
- Verification status: EXISTS / PLAUSIBLE / SUSPECT / FABRICATED
- Check arXiv IDs point to correct papers
- Flag unknown co-authors or future-dated publications

**Evaluator output**: `.khive/workspaces/YYYYMMDD/evaluations/eval_R{N}_XX_topic.md`

Each evaluation MUST contain:
```markdown
# Evaluation: R{N}-{XX} {Topic}

## Summary Verdict: [APPROVE / APPROVE-WITH-FIXES / REJECT]

## Rigor Check
## Daydream Check
## Numbers Check
## Citations Check (if academic)
## What's Usable
## What Needs Fixing
```

### Phase 4: Critic Pass (sequential, AFTER all evaluators)

After ALL evaluators complete, compile a **cross-response analysis**:

1. **Verdict table**: all responses, verdicts, key findings
2. **Recurring errors**: fee model mistakes, citation issues, missing data
3. **Contradictions**: where responses disagree with each other
4. **Action items**: ranked by leverage (what to do NOW vs later)
5. **What's dead**: strategies/approaches killed by this round's findings

Present to Ocean as a concise summary.

### Phase 5: Update Research Progression

Launch ONE agent to update the progression folder:

1. **Create** `{NN}_round_R{N}.md` — structured analysis of all responses
2. **Update** `07_sources.md` — add new citations with verification status
3. **Update** `08_corrections.md` — add new corrections with severity and source
4. **Update** `README.md` — update file table if needed

### Phase 6: Handoff

Write `.khive/workspaces/YYYYMMDD/HANDOFF_R{N+1}_PLANNING.md`:

1. What's done (this round's findings)
2. R{N+1} prompt candidates (ranked by value)
3. Execution plan for next session
4. Data collection status
5. Standing orders from Ocean

---

## Anti-Hallucination Checklist (use at every synthesis step)

```
□ Did ChatGPT cite a specific paper? → Verify it exists (WebSearch)
□ Did ChatGPT give a formula? → Re-derive from first principles
□ Did ChatGPT claim "this strategy returns X%"? → Backtest yourself
□ Did ChatGPT say "no competitors"? → WebSearch for prior art
□ Did ChatGPT give parameter values? → Sanity check against market data
□ Did all directions agree? → Suspicious. At least one should conflict.
```

## Workspace Structure

```
.khive/workspaces/{date}/chatgpt-research/{topic}/
├── round1_mega_prompt.md
├── round1_output.md
├── round2_prompts/
│   ├── direction_01.md
│   └── ...
├── round2_outputs/
│   └── direction_01_output.md
├── round3_synthesis.md          # Claude's synthesis (the critical step)
├── r{N}_drill/
│   └── R{N}_XX_topic.md
├── chatgpt-responses/
├── evaluations/
│   └── eval_R{N}_XX_topic.md
└── HANDOFF_R{N+1}_PLANNING.md

.khive/workspaces/research_progression/
├── README.md              ← Index + validation guide
├── 01_round_R1.md         ← Per-round analysis
├── ...
├── {NN}_round_R{N}.md
├── {NN+1}_own_computation.md  ← Lambda's direct math (highest confidence)
├── {NN+2}_frameworks.md       ← Theoretical + empirical
├── 07_sources.md          ← Master citation list with verification status
└── 08_corrections.md      ← All corrections, severity, source, impact
```

## Typical R1-R2 Flow

1. `/progress-research "topic"` → generates mega-prompt, saves to workspace
2. Open ChatGPT Pro, send mega-prompt in one tab
3. (wait 15-50 min, do other work)
4. Save R1 output to `round1_output.md`
5. `/progress-research --round 2` → generates 8 deep-dive prompts from output
6. Open ChatGPT Pro, send all prompts in parallel tabs
7. (wait, do other work)
8. Save R2 outputs to `round2_outputs/`
9. `/progress-research --synthesize` → Claude does R3 synthesis
10. Repeat for R4+

## Confidence Hierarchy

From highest to lowest:
1. **Own computation** — mathematical proofs, live API data
2. **Evaluator corrections** — errors caught in ChatGPT output
3. **Drill corrections** — Opus agents checking prior rounds against real data
4. **ChatGPT responses** — useful frameworks, but hallucinate and make fee errors
5. **Agent-synthesized frameworks** — good structure, verify specifics

## Important Rules

- **NEVER use Haiku model** for drill or evaluation agents — garbage output
- **Drill agents = analyst (Opus)**. Evaluator agents = analyst (Opus). Progression update = analyst (Sonnet is OK).
- **Max 5 agents per batch** (OOM prevention)
- **Critics run AFTER evaluators, never in parallel**
- **Fee model is PROVEN**: maker = 1c for 1-lot (max(1.75*P*(1-P)) = 0.4375 < 1). Multi-lot: ceil(0.0175 * C * P * (1-P) * 100)c. Include this in EVERY drill prompt.
- **Check 08_corrections.md** before each round — don't repeat known errors
- **"Financials" != Economics** on Kalshi — always verify category labels match content
- **Fabricated citation rate**: ~3-5% per ChatGPT response. Always audit academic outputs.
- **The most valuable findings are the ones that KILL strategies** — prioritize honest negative results over optimistic projections
- **NEVER use `python` or `pip`** — always `uv run`

## Quality Metrics to Track

- Corrections per round (should decrease over time — if not, pipeline hasn't converged)
- Fee error rate (currently: 3/6 responses per round — target: 0)
- Citation fabrication rate (currently: ~3-5% — target: flag all before use)
- Actionable-to-theoretical ratio (target: >50% actionable per round)
