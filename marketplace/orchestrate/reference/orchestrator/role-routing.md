# Role-to-Model Guidance and Effort Tiers

## Role Tiers

Model selection should match the cognitive demands of the role. Do not prescribe specific
model names in the plan — use the role's profile default, and override only when the task
genuinely demands different capabilities.

| Role tier | Capability focus | Roles |
|---|---|---|
| Analysis-capable, medium-high effort | Breadth reading, evidence grounding, deep scanning | researcher, explorer, analyst, auditor, architect, strategist, innovator |
| Code-capable, high effort | Reliable code execution, targeted edits, test writing | implementer, tester, coordinator |
| Highest reasoning, high effort | Adversarial quality gate, formal verdict | critic |

**Why implementers use a code-capable model**: For code that must be correct, execution
reliability matters more than scanning breadth. Analysis phases do the heavy reading;
implementers need precise write execution. Give implementers detailed specs — not exploration
tasks.

**Per-agent override**: Set `model` on a FlowAgent to override the profile default. Use
sparingly — profile defaults are calibrated for the role.

---

## When to Use Which Role

| Role | Use when | Produces |
|---|---|---|
| explorer | Wide codebase scan, inventory gathering | Structured inventory with file:line refs |
| researcher | External knowledge, docs, prior art | Evidence synthesis, citations |
| analyst | Cross-referencing multiple inputs, gap finding | Gap analysis, prioritized findings table |
| auditor | Security, compliance, system-wide invariant checks | Audit report with severity |
| architect | Structural design, interface decisions | Design doc, component diagram |
| strategist | High-stakes tradeoffs, multi-option evaluation | Decision brief with tradeoffs |
| innovator | Novel approaches, blue-sky exploration | Options paper |
| implementer | Applying a spec to write or modify code | Changed files, passing tests |
| tester | Writing and running tests for new code | Test suite, coverage report |
| coordinator | Multi-lane orchestration, branch management, merges | Coordination notes, merge result |
| reviewer | Code review against stated requirements | Review report with findings |
| critic | Adversarial quality gate, integrated verdict | verdict.md with APPROVE/REJECT |
| synthesizer | Final consolidation of multi-agent research | Consolidated report |
| commentator | Draft PR or issue comments | Comment text |
| suggester | Generate options without committing | Options list |

Do not add a role you cannot name a concrete artifact for. A researcher and an explorer doing
the same scan is duplication — pick one.

---

## Effort Tiers

Set effort per agent (via profile default or `guidance` override) to match the cognitive
depth the task requires. Effort affects cost and latency.

```text
low:    Skim structure, produce inventory. Read file headers, not every line.
        Use for: explorers scanning large codebases quickly.

medium: Read carefully, produce analysis. Balance depth and speed.
        Use for: reviewers, testers, suggesters, commentators.

high:   Think deeply, produce thorough output.
        Use for: analysts, researchers, implementers, architects.

xhigh:  Maximum reasoning. Complex multi-step or high-stakes problems.
        Use for: auditors, innovators, theorists, strategists.
```

---

## guidance vs instruction

`instruction` = what to do (the task).
`guidance` = how to do it (behavioral framing).

Use `guidance` for:
- "Be concise"
- "Focus on P0 only"
- "Write no more than 200 lines"
- "Skim structure — do not read every line"
- Effort overrides: "Be thorough — this is the most critical phase"

Do not put file paths, artifact names, or task logic into `guidance`. That belongs in
`instruction`.

---

## Skills

Two different things get called skills around here, and they do not share a lookup path.

**`li skill <name>` reads `~/.lionagi/skills/<name>/SKILL.md` and nowhere else.** That
directory is yours, and installing this plugin puts nothing in it. Workers spawned through
lionagi reach skills this way, so a `li skill` directive resolves only for a skill already
sitting in that directory.

**The twelve skills this plugin ships** become available to Claude through Claude Code once the
plugin is enabled. They are listed with descriptions in the plugin README. They are *not*
reachable with `li skill` unless you separately place a copy under `~/.lionagi/skills/`.

Load a skill before acting on a task that matches a known procedure. The rows below are
examples of the habit rather than a supported list, because what resolves depends entirely on
what your own directory holds:

| Situation | Skill |
|---|---|
| About to run `git commit` | `commit` |
| About to open a PR | `pr` |
| About to post a PR comment | `pr-review` |
| About to run local CI | `ci` |
| About to bump a version or tag a release | `release-prep` |
| Editing a `.playbook.yaml` file | `playbook` |

Replace them with whatever you actually keep. A name that is not there fails at load, naming
the path it looked in and listing what is available, so a wrong row is loud rather than
silent.

Distribute skills to workers by embedding a load directive in the op instruction:

```text
Before analyzing, run `li skill <your-threat-model-skill>` and follow its
procedure. Produce findings.md with severity × file:line × suggestion for
each finding.
```

Do not copy-paste the skill body into the instruction — use the load directive. Do not
load a skill you do not intend to use.
