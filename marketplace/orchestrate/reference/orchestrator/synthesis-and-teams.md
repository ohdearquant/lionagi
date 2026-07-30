# Synthesis, Team Coordination, Re-Plan Budget, and Metrics

## Synthesis

When synthesis is enabled (`--with-synthesis` or `synthesis=true` in the plan), the
orchestrator runs a final pass after all other ops complete. The synthesis op receives
all agent artifacts and produces a cohesive deliverable.

Synthesis responsibilities:
- **Reconcile conflicts**: When agents disagree, present both views with evidence.
- **Fill gaps**: Name what no agent covered.
- **Trace the chain**: Show how work flowed through the DAG — who did what and how outputs
  changed across the pipeline.
- **Honor the critic**: If a control op was in the pipeline, its verdict is authoritative.
  The synthesis does not override it.
- **Resume commands**: Include the branch IDs printed at flow completion so the operator
  can follow up with any individual agent.

If team mode was active, check inter-agent messages for coordination context that did not
make it into the artifact files.

---

## Team Coordination

By default, agents in a flow are isolated — they exchange information only through artifact
files. `--team-mode` adds a persistent messaging layer for real-time coordination.

**Use team mode when:**
- Parallel agents work on overlapping scope and need to avoid duplication
- One agent needs to ask another to clarify a finding mid-execution
- A reviewer needs to send targeted fix requests to a specific implementer

**Skip team mode when:**
- The DAG is purely sequential (artifact files handle all handoff)
- Agents are fully independent (pure fanout with no overlap)
- Speed matters more than coordination (team mode adds overhead)

Team messages are for short coordination signals. Artifacts are for deliverables.
Do not put large outputs in team messages. Do not put coordination signals in artifact files.

---

## Re-Plan Budget

Control ops may request re-planning by returning `should_continue=true`. The engine
supports at most 3 rounds (initial plan + 2 re-plans). After round 3, the flow stops
regardless of the verdict.

**When re-planning:**
- List only new agents in `agents` (reuse existing agent ids where possible — they retain
  their memory).
- List only the new ops to run. Do not re-emit ops that already succeeded.
- Target the specific gaps named in `next_steps`. Do not re-do the full DAG.
- Re-plan ops share the same `--max-ops` budget as the initial plan. The cumulative total
  across all rounds must stay within the cap.

**Re-plan op pattern:**

When the critic returns APPROVE-WITH-FIXES on specific items:
1. Resume the implementer agent (`li agent -r {branch_id}`) — cheaper than a new flow.
2. Only re-run the full flow if the scope of fixes is broad enough to need multiple new agents.

When the critic returns REJECT:
1. Re-plan targets the root causes named in `next_steps`.
2. New analysis ops (not new explorers) if the problem was in analysis quality.
3. New implementation ops if the analysis was correct but execution failed.

---

## Metrics

Track per-flow to maintain quality:

- **phase_efficiency**: Actual op count / minimum ops needed for the dependency structure.
  Target 1.0-1.2. Higher means unnecessary ops were added.
- **artifact_loss**: Artifacts produced but not consumed by any downstream op. Target 0.
  Every artifact should be named in at least one downstream instruction.
- **critic_sequencing**: Was the critic correctly placed as the terminal op? Target 100%.
- **instruction_specificity**: Fraction of op instructions that name a specific upstream
  read path, output filename, and downstream consumer. Target 100%.

A flow where artifact_loss > 0 indicates planning errors: either an op produced work nobody
needed, or a downstream op was missing a `depends_on` edge and didn't get the data it needed.

Both failures are planning errors. Audit the dependency graph when artifact_loss is nonzero.
