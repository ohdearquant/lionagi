# Artifact Handoff Protocol and Instruction Templates

## Artifact Handoff Protocol

Each agent owns one directory: `{artifact_root}/{agent_id}/`. All ops on the same agent
share that directory — the second op can read files the first op wrote without re-injection.
Cross-agent reads use relative paths: `../{dep_agent_id}/{filename}.md`.

Every op instruction must specify:

1. **What to read**: "Read the explorer inventory at `../e1/inventory.md`"
2. **What to produce**: "Write your gap analysis as `gap_analysis.md`"
3. **File naming**: Use descriptive names — `gap_analysis.md`, not `output.md`. Unique names
   across agents matter — the critic reads everything and needs to distinguish sources.
4. **Who consumes it**: "The implementer in the next phase will use this to write fixes"

Cross-agent memory does not carry between agents. If agent B needs agent A's data, agent A
must write it to a file and agent B's instruction must name the path explicitly.

---

## Preflight Context Caching

Orchestrator-gathered context belongs in `{artifact_root}/_context/`. Workers reference
these files by path rather than each re-fetching. Fetching the same diff or source file five
times in parallel wastes cost without adding value.

Common context artifacts to pre-fetch:
- `_context/diff.md` — PR diff or `git diff` output
- `_context/pr_meta.json` — PR title, description, labels, linked issues
- `_context/README.md` — project README (for scope context)
- `_context/relevant_source.{ext}` — source files touched by the task

Reference in instructions: "Read `../_context/diff.md` for the PR changes."

---

## Instruction Templates

### Root producer (no upstream artifacts)

```
Scan all files under {scope}. Write inventory.md to your current directory.
For each item: name, file path with line number, one-line description.
No prose — structured data only. The analyst in the next phase will
cross-reference your output with the other explorer's.
```

### Mid-pipeline agent (reading upstream artifacts)

```
Read ../e1/inventory.md (backend explorer) and ../e2/inventory.md (frontend
explorer). Identify: gaps (what is missing), overlaps (what is duplicated),
quality issues (what needs improvement). Write gap_analysis.md with a
prioritized table. The implementer will use this to write fixes.
```

### Critic (reads everything)

```
Read ALL prior artifacts: ../e1/inventory.md, ../e2/inventory.md,
../a1/gap_analysis.md, ../i1/fix_brief.md. For each proposed fix:
(1) Does it address a real gap from the analysis?
(2) Is the fix correct and complete?
(3) Any regressions?
Write verdict.md with APPROVE / APPROVE-WITH-FIXES / REJECT per item.
```

---

## Briefing the Implementer

When analysis phases produce precise fix specs, implementers do not need to explore the
codebase. The analyst should produce for each fix target:

- 2-3 change options with tradeoffs
- Exact file:line locations for each option
- Before/after snippets showing the expected transformation
- Verification criteria (how to confirm the fix worked)
- Risk flags (what could break, what to check after)

The implementer's instruction then says:

```
Read `../a1/fix_brief.md`. It contains options per target with file:line
locations and before/after snippets. Apply the recommended option unless
you see a reason not to. Run the verification command listed.
```

---

## Sandbox and Tool Access

Orchestrator and workers both have access to the operator's full environment: `git`, `gh`,
`uv`, network, file system.

The orchestrator retains exclusive responsibility for:

1. **Planning** — only the orchestrator produces the FlowPlan. Workers execute single ops;
   they cannot spawn child DAGs.
2. **Cross-op synthesis** — reading all artifacts and producing the final consolidated result.
3. **Terminal side effects** — creating or merging PRs, posting a single consolidated comment,
   finalizing a release. These can be delegated to a coordinator worker, but by convention
   the orchestrator handles them after synthesis.

---

## Post-Execution: Resume and Iterate

After flow completion, output includes branch IDs for every agent:

```
[orchestrator] li agent -r adf15442 "..."
[explorer]     li agent -r 1d63e2bd "..."
[analyst]      li agent -r 95d31076 "..."
```

Resume an individual agent for follow-up rather than re-running the full flow:

- Critic returned APPROVE-WITH-FIXES: resume the implementer to apply specific fixes
- Need deeper research on one finding: resume the researcher with a targeted question
- Iterative refinement: resume the architect with new constraints from the operator

Re-create the full flow (new `li o flow` invocation) when:
- Scope changed significantly
- Prior context is stale (files changed since the last run)
- Different team composition is needed for the new work
