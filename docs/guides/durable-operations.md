# Durable Operations

LionAGI records CLI work locally so a terminal closing does not erase the run's
identity, branch state, or recovery data.

## Start a background flow

`--background` requires an explicit artifact directory:

```bash
li o flow codex \
  "Audit this repository and produce a prioritized report." \
  --cwd . --max-ops 6 --background --save ./lion-results/background-audit
```

The launcher prints the child PID, a session ID suitable for `li monitor`, and
the path to `flow.log`. Save the session ID for the commands below.

```bash
SESSION_ID=<session-id-from-launch>
```

## Observe current work

List active and recent entities:

```bash
li monitor --watch
```

Drill into one session or query the control-plane status:

```bash
li monitor "$SESSION_ID"
li o ctl status "$SESSION_ID"
```

The monitor reads LionAGI's state database; it does not infer health from a
log file. Use `flow.log` for detailed process output, not as the source of
lifecycle truth.

## Pause, resume, and steer a live flow

Control commands apply to running flow and playbook sessions:

```bash
li o ctl pause "$SESSION_ID"
li o ctl status "$SESSION_ID"
li o ctl resume "$SESSION_ID"
li o ctl msg "$SESSION_ID" "Prioritize evidence from the failing tests."
```

These commands enqueue controls; the live flow applies them at its next poll
or operation boundary. `msg` adds context for operations that have not yet
rendered their instructions. It does not rewrite an operation already running.

## Understand local state and artifacts

Each CLI run owns a state directory:

```text
~/.lionagi/runs/<run-id>/
├── run.json
├── checkpoint.json
├── branches/
└── stream/
```

`run.json` records the state and artifact roots. `branches/` contains resumable
branch snapshots. `checkpoint.json` records flow progress. When `--save DIR`
is present, worker and synthesis artifacts go to that directory; otherwise
they live below the run's own `artifacts/` directory.

## Resume after a process stops

Resume a single agent conversation with the branch ID printed by `li agent`:

```bash
li agent -r <branch-id> "Continue from the saved context and finish."
```

Resume a checkpointed flow with a run, session, invocation, or play ID:

```bash
li o flow --resume <run-or-session-id>
```

Flow resume replays the persisted plan and ignores new model, prompt, and
playbook flags. It is different from `li o ctl resume`, which only releases a
pause gate in a process that is still running.

Version 1 recovery cannot reconstruct a predecessor's full conversation for a
pending operation that requested inherited context. LionAGI refuses that case
by default. `--allow-degraded-context` opts into running such an operation with
empty predecessor conversation state; use it only when that loss is acceptable.

## Diagnose a stuck or failed run

1. Run `li o ctl status ID` to read the recorded lifecycle and reason.
2. Inspect the `flow.log` path printed by a background launch.
3. Open `run.json` to locate the exact state and artifact roots.
4. Run `li doctor` to check imports, core dependencies, Studio reachability,
   and `~/.lionagi` writability.
5. Resume from `checkpoint.json` only after the original process is no longer
   active.

Expected durable evidence is a state-database row plus a run directory. A flow
adds a checkpoint; `--save` adds reviewable output at the chosen path.

Next, use [Studio and schedules](studio.md) when durable work needs a visual
operating surface or a trigger.
