# Coding Harness Research - 2026-06-03

Issue: [#1246](https://github.com/ohdearquant/lionagi/issues/1246) (created 2026-06-02, updated 2026-06-03, C:1.0). Related scope: [#1247](https://github.com/ohdearquant/lionagi/issues/1247) and [#1248](https://github.com/ohdearquant/lionagi/issues/1248) (both 2026-06-02, C:1.0).

## Scope

This note studies the requested OSS coding harnesses, OpenCode (`sst/opencode`) and `mini-swe-agent`, with concrete source anchors for agent loop, tool set, permission model, context management, and persistence/engagement mechanics. It also drafts the remaining AST/static-analysis tool surface for follow-up work beyond the first #1247 slice.

## Source Inventory

| Source | Type | Date | Confidence | Use |
| --- | --- | ---: | ---: | --- |
| `sst/opencode` commit [`a0e4db3`](https://github.com/sst/opencode/tree/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2) | Official source | 2026-06-03 | 0.95 | OpenCode loop, tools, permissions, compaction |
| `SWE-agent/mini-swe-agent` commit [`3df30a4`](https://github.com/SWE-agent/mini-swe-agent/tree/3df30a4bb564add41470c768559481e135607761) | Official source | 2026-06-02 | 0.95 | mini loop, bash-only interface, trajectory save |
| [OpenCode README](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/README.md#L100-L113) | Official docs | 2026-06-03 | 0.9 | Built-in agent modes |
| [OpenCode `session/prompt.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/session/prompt.ts#L1261-L1499) | Official source | 2026-06-03 | 0.95 | Stop/continue loop |
| [OpenCode `session/processor.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/session/processor.ts#L107-L849) | Official source | 2026-06-03 | 0.95 | Tool state, snapshots, doom-loop guard |
| [OpenCode `tool/edit.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/tool/edit.ts#L58-L207) | Official source | 2026-06-03 | 0.95 | Edit permission and LSP diagnostics |
| [OpenCode `session/compaction.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/session/compaction.ts#L37-L558) | Official source | 2026-06-03 | 0.95 | Context compaction and auto-continue |
| [OpenCode `permission/index.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/permission/index.ts#L41-L180) | Official source | 2026-06-03 | 0.95 | ask/allow/deny permission model |
| [mini-swe-agent README](https://github.com/SWE-agent/mini-swe-agent/blob/3df30a4bb564add41470c768559481e135607761/README.md#L23-L55) | Official docs | 2026-06-02 | 0.85 | Minimal/bash-only/linear-history framing and performance claim |
| [mini control-flow docs](https://github.com/SWE-agent/mini-swe-agent/blob/3df30a4bb564add41470c768559481e135607761/docs/advanced/control_flow.md#L59-L123) | Official docs | 2026-06-02 | 0.9 | Default loop explanation |
| [mini `agents/default.py`](https://github.com/SWE-agent/mini-swe-agent/blob/3df30a4bb564add41470c768559481e135607761/src/minisweagent/agents/default.py#L85-L170) | Official source | 2026-06-02 | 0.95 | Agent loop and save path |
| [mini `environments/local.py`](https://github.com/SWE-agent/mini-swe-agent/blob/3df30a4bb564add41470c768559481e135607761/src/minisweagent/environments/local.py#L23-L66) | Official source | 2026-06-02 | 0.95 | Bash execution and sentinel finish |
| [mini `config/mini.yaml`](https://github.com/SWE-agent/mini-swe-agent/blob/3df30a4bb564add41470c768559481e135607761/src/minisweagent/config/mini.yaml#L9-L42) | Official source | 2026-06-02 | 0.95 | Persistence prompt and workflow |
| [`lionagi/tools/coding.py`](https://github.com/ohdearquant/lionagi/blob/acc50703a63d076d207df5151bc5d775301e8b32/lionagi/tools/coding.py#L572-L587) | Internal source | 2026-06-02 | 0.9 | Current lionagi tool surface |
| [SWE-agent ACI paper](https://arxiv.org/abs/2405.15793) | Academic | 2024-05-06 | 0.7 | Background: agent-computer interface matters; recency risk >2 years soon |
| khive KG recall for `PermissionPolicy`, `AgentConfig`, `FlowAgent` | Internal memory/KG | 2026-06-03 | 0.7 | Internal comparison context |

Tools used: `gh issue view 1246`, `gh issue view 1246 --comments`, `gh issue view 1247`, `gh issue view 1248`, `li team receive`, khive `search`/`memory.recall`/`knowledge.topic`, WebSearch for `sst opencode` and `mini-swe-agent`, `git clone --depth 1`, `rg`, `nl -ba`.

## Findings

### 1. OpenCode keeps the loop running until the model has no actionable tool continuation

[Finding] `SessionPrompt.runLoop` checks the latest user/assistant pair, treats assistant finishes with unresolved tool calls as non-final, and breaks only when the assistant has a real finish reason without pending tool work. Source: [`session/prompt.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/session/prompt.ts#L1261-L1294) (2026-06-03, C:0.95). Context: this directly addresses early-quit after tool calls.

[Finding] Each OpenCode loop step creates an assistant message, resolves the current agent/tools, streams the LLM, and returns `"continue"` unless the processor reports `"stop"`; `"compact"` creates a compaction task and also continues. Source: [`session/prompt.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/session/prompt.ts#L1334-L1499) (2026-06-03, C:0.95). Context: loop policy is explicit code, not only prompt framing.

[Finding] OpenCode injects a final max-step assistant message when the agent reaches `agent.steps`, using `prompt/max-steps.txt` to force a text-only work summary instead of silent tool retries. Source: [`session/prompt.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/session/prompt.ts#L1342-L1455), [`max-steps.txt`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/session/prompt/max-steps.txt#L1-L15) (2026-06-03, C:0.9). Context: this is a bounded persistence guard.

### 2. OpenCode treats tool execution as a stateful event stream with snapshots and repair hooks

[Finding] `SessionProcessor` records tool parts as pending/running/completed/error, tracks snapshots before and after a step, emits patch parts when files changed, and summarizes each user turn asynchronously. Source: [`session/processor.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/session/processor.ts#L107-L193), [`session/processor.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/session/processor.ts#L557-L617) (2026-06-03, C:0.95). Context: tool outputs become inspectable session parts, not transient strings.

[Finding] OpenCode has a concrete doom-loop guard: if the last three parts are the same tool with identical input, the processor asks for `doom_loop` permission before allowing another identical call. Source: [`session/processor.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/session/processor.ts#L424-L451) (2026-06-03, C:0.95). Context: this is a persistence safety valve, not a generic "be persistent" prompt.

[Finding] Provider tool-call repair lowercases tool names when possible and otherwise routes malformed calls through an `invalid` tool with the original error. Source: [`session/llm.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/session/llm.ts#L290-L309) (2026-06-03, C:0.9). Context: malformed tool calls are converted into model-visible feedback instead of terminating the run.

### 3. OpenCode's tool set is broad, but the important pattern is tool-specialization plus immediate feedback

[Finding] The built-in registry initializes `shell`, `read`, `glob`, `grep`, `edit`, `write`, `task`, `webfetch`, `todo`, `websearch`, `skill`, `apply_patch`, and optional `lsp`. Source: [`tool/registry.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/tool/registry.ts#L224-L260) (2026-06-03, C:0.95). Context: OpenCode separates file IO/search/editing from shell instead of making shell do everything.

[Finding] `EditTool` and `WriteTool` both call `lsp.touchFile()` after mutation, pull diagnostics, and append file-specific LSP errors to the tool output. Source: [`tool/edit.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/tool/edit.ts#L192-L207), [`tool/write.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/tool/write.ts#L74-L100) (2026-06-03, C:0.95). Context: this is the strongest #1247 pattern for lionagi.

[Finding] `LSP.Diagnostic.report()` limits to errors, formats `ERROR [line:col] message`, caps per-file output at 20 diagnostics, and wraps them in a `<diagnostics file="...">` block. Source: [`lsp/diagnostic.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/lsp/diagnostic.ts#L3-L27) (2026-06-03, C:0.95). Context: diagnostics are compact enough for model consumption.

[Finding] The shell tool parses bash/PowerShell with tree-sitter, scans path-touching commands, asks for external-directory permission when command args escape the worktree, and asks shell permission for command patterns. Source: [`tool/shell.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/tool/shell.ts#L320-L345), [`tool/shell.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/tool/shell.ts#L387-L422), [`tool/shell.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/tool/shell.ts#L620-L655) (2026-06-03, C:0.95). Context: richer than lionagi's current regex control-operator gate.

[Finding] Shell output is streamed into bounded metadata, spills oversized full output to a truncation file, and tells the model where to inspect the full output. Source: [`tool/shell.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/tool/shell.ts#L448-L608), [`tool/truncate.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/tool/truncate.ts#L86-L142) (2026-06-03, C:0.95). Context: large outputs remain accessible without flooding context.

### 4. OpenCode's permission model is ask-by-default and supports corrected feedback

[Finding] `Permission.evaluate()` returns the last matching allow/deny/ask rule, defaulting to `ask` for unmatched permission+pattern pairs. Source: [`permission/index.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/permission/index.ts#L41-L51) (2026-06-03, C:0.95). Context: this is compatible with lionagi's existing PermissionPolicy KG concept.

[Finding] `Permission.ask()` emits pending permission requests and `reply()` supports reject-with-message via `CorrectedError`, once approvals, and durable `always` approvals by appending allow rules. Source: [`permission/index.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/permission/index.ts#L80-L180) (2026-06-03, C:0.95). Context: a rejected action can become model-visible correction, not just a block.

[Finding] OpenCode exposes built-in `build` and `plan` agents; `plan` is read-only by default, denies file edits, and asks before bash. Source: [README](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/README.md#L100-L108) (2026-06-03, C:0.9). Context: this maps cleanly to lionagi "research/read-only" vs "coding/build" presets.

### 5. OpenCode compaction preserves actionable state and auto-continues

[Finding] Compaction uses a fixed summary template with sections for goal, constraints, done/in-progress/blocked, decisions, next steps, critical context, and relevant files. Source: [`session/compaction.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/session/compaction.ts#L37-L79) (2026-06-03, C:0.95). Context: this is more reusable than freeform summarization.

[Finding] `select()` preserves recent turns under a model-dependent token budget and summarizes older head context. Source: [`session/compaction.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/session/compaction.ts#L138-L295) (2026-06-03, C:0.95). Context: keeps the immediate edit/test loop live.

[Finding] After successful auto-compaction, OpenCode can synthesize a user message telling the agent to continue if it has next steps or stop and ask for clarification if unsure. Source: [`session/compaction.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/session/compaction.ts#L478-L558) (2026-06-03, C:0.95). Context: this is a concrete engagement/persistence pattern.

### 6. mini-swe-agent is a deliberately minimal bash-only harness with strong persistence pressure

[Finding] `DefaultAgent.run()` initializes system/user messages, loops `step()` until the latest message has `role == "exit"`, and saves the trajectory in `finally` after every step. Source: [`agents/default.py`](https://github.com/SWE-agent/mini-swe-agent/blob/3df30a4bb564add41470c768559481e135607761/src/minisweagent/agents/default.py#L85-L105), [`agents/default.py`](https://github.com/SWE-agent/mini-swe-agent/blob/3df30a4bb564add41470c768559481e135607761/src/minisweagent/agents/default.py#L163-L170) (2026-06-02, C:0.95). Context: persistence is mechanical and cheap.

[Finding] `step()` is only `execute_actions(query())`; `query()` enforces step/cost/wall-clock limits, appends the model response, and `execute_actions()` runs every parsed action through the environment. Source: [`agents/default.py`](https://github.com/SWE-agent/mini-swe-agent/blob/3df30a4bb564add41470c768559481e135607761/src/minisweagent/agents/default.py#L107-L138) (2026-06-02, C:0.95). Context: the loop is inspectable enough for direct A/B harness experiments.

[Finding] The local environment executes only shell commands with `subprocess.run(shell=True)`, captures combined stdout/stderr, applies a timeout, and treats a first-line `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` sentinel as submission. Source: [`environments/local.py`](https://github.com/SWE-agent/mini-swe-agent/blob/3df30a4bb564add41470c768559481e135607761/src/minisweagent/environments/local.py#L23-L66) (2026-06-02, C:0.95). Context: mini trades tool specificity for a universal shell action.

[Finding] `mini.yaml` requires reasoning text plus at least one bash tool call in every response, gives a six-step workflow from codebase analysis through verification and final sentinel, and warns that cwd/env changes are not persistent because each action runs in a new subshell. Source: [`config/mini.yaml`](https://github.com/SWE-agent/mini-swe-agent/blob/3df30a4bb564add41470c768559481e135607761/src/minisweagent/config/mini.yaml#L9-L42) (2026-06-02, C:0.95). Context: this is direct prompt pressure against no-op replies.

[Finding] The mini observation template truncates long output to head/tail and tells the model to narrow the command, view fewer file lines, use more selective grep/find, or redirect/search full output. Source: [`config/default.yaml`](https://github.com/SWE-agent/mini-swe-agent/blob/3df30a4bb564add41470c768559481e135607761/src/minisweagent/config/default.yaml#L114-L141) (2026-06-02, C:0.9). Context: recovery guidance is inside the observation path.

[Finding] `InteractiveAgent` supports `confirm`, `yolo`, and `human` modes; rejected commands become user interruption messages with feedback, and `confirm_exit` lets the human add a new task rather than accept completion. Source: [`agents/interactive.py`](https://github.com/SWE-agent/mini-swe-agent/blob/3df30a4bb564add41470c768559481e135607761/src/minisweagent/agents/interactive.py#L1-L7), [`agents/interactive.py`](https://github.com/SWE-agent/mini-swe-agent/blob/3df30a4bb564add41470c768559481e135607761/src/minisweagent/agents/interactive.py#L98-L156) (2026-06-02, C:0.95). Context: permission UX is small but effective.

### 7. The main design conflict: bash-only minimalism vs specialized tool feedback

[Conflict] mini-swe-agent's README argues the agent needs no tools other than bash and benefits from a completely linear history; OpenCode's source demonstrates specialized read/edit/search/LSP tools with immediate diagnostics. Sources: mini README [`README.md`](https://github.com/SWE-agent/mini-swe-agent/blob/3df30a4bb564add41470c768559481e135607761/README.md#L38-L55), OpenCode registry/diagnostics [`tool/registry.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/tool/registry.ts#L224-L260), [`tool/edit.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/tool/edit.ts#L192-L207) (2026-06-02/03, C:0.9). Context: this should be tested as a harness variable, not settled by source inspection.

[Conflict] OpenCode's shell prompt explicitly says not to use shell for reading/writing/editing/searching files; mini's prompt gives shell examples for creating, editing, and reading files. Sources: OpenCode [`tool/shell/shell.txt`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/tool/shell/shell.txt#L7-L10), mini [`config/default.yaml`](https://github.com/SWE-agent/mini-swe-agent/blob/3df30a4bb564add41470c768559481e135607761/src/minisweagent/config/default.yaml#L58-L103) (2026-06-02/03, C:0.9). Context: lionagi can keep both modes: structured-tool default plus a bash-only benchmark baseline.

## Prioritized Fold Into Lionagi

1. **P0 - Add OpenCode as a harness provider, not just a source of ideas.** Issue #1246's comment explicitly reframes OpenCode as a first-class provider candidate; lionagi should orchestrate OpenCode/Codex/Claude-Code/naked harnesses and A/B them. Source: [#1246 comment](https://github.com/ohdearquant/lionagi/issues/1246) (2026-06-03, C:1.0). Artifact: provider adapter with session start/continue, prompt injection, transcript capture, and run result normalization.

2. **P0 - Post-edit diagnostics hook.** Mirror OpenCode's `edit`/`write` pattern: every successful lionagi editor mutation should run a small diagnostic pass and append structured file:line feedback to the tool result. Source: OpenCode [`tool/edit.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/tool/edit.ts#L192-L207), #1247 priority 1 (2026-06-02/03, C:0.95).

3. **P0 - Mechanical persistence contract.** Add a loop policy that continues while tool results, diagnostics, or explicit next steps exist; require explicit finalization after verification or max-step summary. Source: OpenCode [`session/prompt.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/session/prompt.ts#L1261-L1499), mini [`config/mini.yaml`](https://github.com/SWE-agent/mini-swe-agent/blob/3df30a4bb564add41470c768559481e135607761/src/minisweagent/config/mini.yaml#L35-L42) (2026-06-02/03, C:0.9).

4. **P1 - Structured compaction with auto-continue.** Port OpenCode's sectioned compaction schema and synthetic "continue if next steps" prompt into lionagi context tooling. Source: OpenCode [`session/compaction.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/session/compaction.ts#L37-L79), [`session/compaction.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/session/compaction.ts#L478-L558) (2026-06-03, C:0.95).

5. **P1 - Persist full trajectories every step.** mini saves full `messages`, config, environment, cost, exit status, and submission; lionagi should keep a normalized trajectory artifact per coding run even when no commit is produced. Source: mini [`agents/default.py`](https://github.com/SWE-agent/mini-swe-agent/blob/3df30a4bb564add41470c768559481e135607761/src/minisweagent/agents/default.py#L140-L170), [`docs/usage/output_files.md`](https://github.com/SWE-agent/mini-swe-agent/blob/3df30a4bb564add41470c768559481e135607761/docs/usage/output_files.md#L17-L54) (2026-06-02, C:0.9).

6. **P1 - Permission profiles: build/plan/read-only plus ask/once/always.** OpenCode's built-in build/plan split and ask-by-default ruleset are concrete and small enough to map to lionagi agent presets. Source: OpenCode README [`README.md`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/README.md#L100-L113), [`permission/index.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/permission/index.ts#L80-L180) (2026-06-03, C:0.9).

7. **P1 - Output truncation with durable full-output paths.** Lionagi currently caps bash output, but should preserve full output to an artifact path and tell the model to search/read by offset. Source: OpenCode [`tool/truncate.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/tool/truncate.ts#L86-L142), mini [`config/default.yaml`](https://github.com/SWE-agent/mini-swe-agent/blob/3df30a4bb564add41470c768559481e135607761/src/minisweagent/config/default.yaml#L123-L141) (2026-06-02/03, C:0.9).

8. **P2 - Doom-loop gate and malformed-tool feedback.** Add cheap detection for repeated identical tool calls and turn malformed calls into model-visible correction messages. Source: OpenCode [`session/processor.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/session/processor.ts#L424-L451), [`session/llm.ts`](https://github.com/sst/opencode/blob/a0e4db3714ca78655245c3a7d1dc06f7e7e3a6f2/packages/opencode/src/session/llm.ts#L290-L309) (2026-06-03, C:0.9).

9. **P2 - Keep a bash-only mini baseline.** mini's bash-only linear harness is the cleanest benchmark control. Lionagi should A/B structured tools vs a mini-like mode rather than assume more tools always help. Source: mini README [`README.md`](https://github.com/SWE-agent/mini-swe-agent/blob/3df30a4bb564add41470c768559481e135607761/README.md#L38-L55) (2026-06-02, C:0.85).

## Remaining AST / Static-Analysis Tool Surface For #1247 Follow-Ups

Issue #1247 ranks post-edit diagnostics first, structural search/rewrite second, outline/navigation third, and parse validation fourth. The first slice can ship one diagnostic tool; the remaining surface should be staged as follows.

### Shared Result Schema

All static tools should return a structured result:

- `tool`: `ruff`, `pyright`, `mypy`, `ast-grep`, `python_ast`, `tree_sitter`, `eslint`, `tsc`, etc.
- `command`: exact command or library operation used.
- `cwd`: working directory used for config discovery.
- `status`: `ok`, `diagnostics`, `unavailable`, `error`.
- `diagnostics`: list of `{path, line, col, end_line, end_col, severity, code, message, source, rule_url, fixable, snippet}`.
- `summary`: counts by severity/source and `introduced_count` when a before/after baseline exists.
- `next_action`: short recovery hint, for example "edit this file and rerun code_check".

### Tool Surface

1. `code_check(paths=None, scope="changed", tools=None, baseline_id=None)`: runs configured linters/type-checkers and reports structured diagnostics. Default Python order: `ruff check` first, then optional `pyright`/`mypy` only if present/configured. Must degrade to `unavailable` if a binary is missing.

2. `post_edit_check(file_path, before_baseline=None)`: hook target, not just user-facing tool. Runs after `editor.write`/`editor.edit`, filters diagnostics to current file plus new project-wide errors, and emits `[System: ...]` or equivalent Branch system notification.

3. `syntax_check(file_path, language=None)`: cheap parser validation. Python uses stdlib `ast.parse`; non-Python can use tree-sitter if installed. This should run before or immediately after writes to catch syntax-breaking edits.

4. `ast_search(pattern, language, paths=None, selector=None, context=3)`: wraps `ast-grep` (`sg`) for structural search. Example targets: bare `except: pass`, missing `await`, functions without return annotations, unsafe subprocess calls.

5. `ast_rewrite_preview(pattern, rewrite, language, paths=None)`: returns a unified diff and match inventory; no mutation. This is the safe default for refactors.

6. `ast_rewrite_apply(pattern, rewrite, language, paths=None, require_confirmation=True)`: applies the previewed rewrite through the same permission/edit path as `editor`, then runs `post_edit_check`.

7. `outline(path, include_imports=True)`: returns imports, classes, functions, methods, signatures, decorators, and line spans without reading full file contents. Python stdlib `ast` is enough for MVP.

8. `find_definition(symbol, path=None, language=None)` and `find_references(symbol, paths=None, language=None)`: start with Python AST and text fallback, later route to LSP when an LSP client exists.

9. `diagnostic_baseline(paths=None)`: snapshots current diagnostics so post-edit tools can report introduced errors rather than all historical debt.

10. `import_graph(paths=None, depth=1)` and `callers_callees(symbol, path)`: lower-priority navigation aids for localization once diagnostics and structural search are stable.

### Sequencing

- **P0**: `code_check` + `post_edit_check` + `diagnostic_baseline` for Python, with optional `ruff` dependency guard.
- **P1**: `syntax_check` and `ast_search`; both have high signal and low mutation risk.
- **P1**: `ast_rewrite_preview`; useful for refactor planning without write risk.
- **P2**: `outline`, `find_definition`, `find_references`; improves localization and context economy.
- **P3**: `ast_rewrite_apply`, `import_graph`, call graph. These need stronger permissions and tests.

## Gaps

- Not found: a reliable OpenCode public docs page in the cloned `packages/docs` tree; it appears to contain Mintlify boilerplate in this snapshot. Impact: OpenCode public-product claims are limited to README and source code. Tools searched: `rg --files packages/docs`, `nl -ba packages/docs/*.mdx`, `rg provider|permission|agent`.
- Not verified: mini-swe-agent's `>74%` SWE-bench claim beyond its README and linked leaderboard context. Impact: performance claims should be treated as source-reported, not independently validated. Source: mini README [`README.md`](https://github.com/SWE-agent/mini-swe-agent/blob/3df30a4bb564add41470c768559481e135607761/README.md#L23-L31) (2026-06-02, C:0.65).
- Not run: neither OpenCode nor mini-swe-agent was executed locally. This report is source-code research, not runtime validation. Impact: provider integration complexity and runtime UX need a tester/implementer pass.
- Not visible yet: #1247's shipped implementation slice was not present as a dirty local change during this research op. The AST surface above assumes only one first slice ships and scopes follow-ups accordingly. Tools searched: `git status --short`, `rg ast|ruff|pyright|diagnostic`.
- Conflict unresolved: bash-only minimalism vs specialized diagnostics should be A/B tested on SWE-bench Verified Mini; source inspection alone cannot decide the better default.

## Domain Utility

Domain utility: SKIPPED - this was external/current OSS source research plus local code inspection; WebSearch, GitHub, grep, and khive recall were higher-signal than composing internal domains.
