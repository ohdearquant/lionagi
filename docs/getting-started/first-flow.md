# CLI Quickstart

Get one agent to a verified result before adding orchestration. This guide uses
the `codex` alias; substitute `claude` if that is the CLI you authenticated.

## 1. Run one agent

From a directory you want the agent to inspect:

```bash
li agent codex "Describe this directory in one concise paragraph." --cwd .
```

Success has two observable signals: a non-empty response prints to stdout, and
LionAGI prints a `[to resume]` command containing the saved branch ID. `--cwd`
sets the working directory for the CLI-backed provider.

If the provider fails before responding, run `codex --version` and
`codex login` directly, then rerun `li doctor`.

## 2. Continue the conversation

Continue the most recently used branch:

```bash
li agent -c "Turn that into three bullets."
```

Or use the explicit branch ID from the resume hint:

```bash
li agent -r <branch-id> "Name one file I should read first and explain why."
```

For bounded work, set a hard wall-clock deadline:

```bash
li agent -c "Finish with the most important caveat." --timeout 120
```

LionAGI adds the deadline to the agent's prompt and terminates the run when the
limit is reached. Add `--resume-on-timeout` when you want one automatic resume
attempt after an agent timeout:

```bash
li agent -c "Complete the review." --timeout 120 --resume-on-timeout
```

## 3. Fan out independent work

Use fan-out when several workers can answer independently:

```bash
li o fanout codex \
  "Review this repository from correctness and maintainability perspectives." \
  --cwd . -n 2 --with-synthesis --save ./lion-results/fanout
```

The saved directory contains `worker_1.md`, `worker_2.md`, and, because
`--with-synthesis` is set, `synthesis.md`. The command also records run state
under `~/.lionagi/runs/`.

## 4. Preview a dependency-aware flow

A flow asks an orchestrator to plan work with dependency edges. Preview the
plan before spending worker turns:

```bash
li o flow codex \
  "Inspect this repository, identify one documentation gap, then propose a fix." \
  --cwd . --max-ops 4 --dry-run
```

`--dry-run` prints the planned agents, operations, dependencies, and model
resolution without executing the worker graph. The exact plan is model-driven,
so its node names and count may vary within the `--max-ops` cap.

Execute the same task after the preview looks appropriate:

```bash
li o flow codex \
  "Inspect this repository, identify one documentation gap, then propose a fix." \
  --cwd . --max-ops 4 --save ./lion-results/flow
```

Flow artifacts are written below the `--save` directory. The durable manifest,
branch snapshots, stream buffers, and checkpoint remain under the run's
`~/.lionagi/runs/<run-id>/` directory.

## What you have now

- A resumable single-agent branch.
- Parallel worker artifacts from fan-out.
- A previewed and executed dependency-aware flow.
- Durable local run state for inspection and recovery.

Next, learn the full
[agent → fan-out → flow → playbook progression](../guides/orchestration.md),
or see how to [monitor and recover durable work](../guides/durable-operations.md).
