# ADR-0083 — Flow-in-Sandbox with Unified Local Observability

- **Status:** Proposed
- **Date:** 2026-06-03
- **Supersedes:** —
- **Related:** ADR-0023b (persistence rides the hook bus), ADR-0019 (activity
  staleness), ADR-0022 (per-branch provenance), ADR-0076 (observer as hook
  transport), the SWE-bench Daytona harness
  (`benchmarks/orchestration/suites/swebench/`)

## Context

We can already run a lionagi agent **inside a Daytona sandbox**: `lionagi/tools/daytona.py`
wraps create → clone → `exec_stream`, and `benchmarks/orchestration/suites/swebench/_sandbox_entry.py`
runs a full ReAct agent in the container, emitting one `@@SIG@@ {json}` line per
reactive-bus event back to the host. The host parses those signals into memory +
local JSON files.

What we do **not** yet have: a sandboxed run that appears in `li monitor` and
Studio **identically to a local run**. Today's signal stream is a *lossy summary*
(`_summarize` in `_sandbox_entry.py:35` keeps only `{t, fn, action, arg, ok}`),
and it is never written to the local `state.db`. So a Daytona flow is invisible to
the unified observability + control surface that every local `li o flow` / `li agent`
run enjoys.

The local path is precise and worth mirroring exactly. `start_live_persist`
(`cli/orchestrate/_orchestration.py:719`) creates the session row; the per-branch
hook `_on_message` (`:908`) is a fixed 4-call sequence:

```python
await db.insert_message(msg_dict)                       # full message row
await db.append_to_progression(branch_prog_id, msg_id)  # branch timeline
await db.append_to_progression(session_prog_id, msg_id) # session timeline
await db.touch_session_activity(session_id, at=...)      # ADR-0019 heartbeat
```

and `stop_live_persist` (`:943`) writes the terminal status via `update_status`.
`li monitor` reads `sessions WHERE status='running'`, the `current_phase` column,
and the per-branch progression. That is the entire contract observability depends on.

The design intent was always for this to cross the container boundary.
`daytona.py:12` states it outright: *"exchange with it over the reactive bus —
emission flows out as a signal stream, control flows in as a polled signal file.
The container is the isolation boundary; the bus is the protocol across it."*

## Decision

Run the flow inside the sandbox and **replay its message events into the *same*
`StateDB` calls `_on_message` makes**, on the host. Concretely:

1. **The host owns the DB and the session row.** It already knows the run config
   (model, provider, effort, project), so it creates the `sessions` row up-front
   (`status='running'`) and `li monitor` shows the run the instant it launches —
   before the sandbox emits anything.

2. **The sandbox is stateless re: persistence.** It does *not* run
   `start_live_persist` (that would write to a throwaway in-container `state.db`
   nobody reads). Instead, the in-sandbox entry attaches an emit-only hook on every
   branch — the **same** `route_message_persistence` seam the local path uses
   (ADR-0023b) — whose handler serializes the full `msg.to_dict(mode="db")` to a
   dedicated wire line and emits a `branch` event on first sight of each branch.

3. **A new, full-fidelity wire channel** carries persistence events:
   `@@LIONDB@@ {json}`, distinct from the lossy `@@SIG@@` human-watch stream so the
   two coexist (a human can still tail `@@SIG@@`; the DB consumes `@@LIONDB@@`).
   Event kinds: `branch` (branch row + optional system message), `message` (full
   `msg.to_dict(mode="db")`), `phase` (sets `sessions.current_phase`).

4. **`SandboxBridge` is the host-side mirror of `start_live_persist` /
   `_on_message` / `stop_live_persist`**, driven by the wire instead of an
   in-process hook bus. It owns the `StateDB`, creates the session, and on each
   `@@LIONDB@@` event performs the *identical* StateDB write sequence. Result:
   sessions, branches, messages, and progressions are byte-for-byte what a local
   run produces — `li monitor` / Studio / `li kill` work with zero special-casing.

```text
  IN SANDBOX (stateless)                  WIRE                 HOST (owns state.db)
  ─────────────────────                  ──────               ────────────────────
  branch.operate / session.flow                               SandboxBridge.start()
    └ route_message_persistence hook  ──@@LIONDB@@ branch──▶   create_progression
                                                               create_branch
    └ every new message  ────────────  ──@@LIONDB@@ message─▶   insert_message
                                                               append_to_progression ×2
                                                               touch_session_activity
  RunEnd                              ───(stream closes)────▶   SandboxBridge.finish()
                                                               update_session + update_status
```

1. **Control parity reuses the existing reverse channel.** The sandbox already
   polls a `control` file → `branch.control(LoopDirective)` (`_sandbox_entry.py:191`).
   `SandboxBridge.cancel()` / `.signal(directive)` writes that file into the running
   sandbox; wiring `li kill <session_id>` / Studio-cancel to the bridge closes the
   control loop so a remote run is as cancellable as a local one.

2. **Worker harness = `pi`, model = OpenRouter/DeepSeek.** `pi` is a registered
   AGENTIC CLI provider (`lionagi/providers/pi/`, the `@mariozechner/pi-coding-agent`
   binary). `providers/pi/cli/models.py:63` maps `openrouter/...` → `--provider
   openrouter` and `:281` → `OPENROUTER_API_KEY`, so
   `iModel(provider="pi", model="openrouter/deepseek/deepseek-v4-flash", api_key=…)`
   drives pi against OpenRouter. Its `tool_use`/`tool_result` StreamChunks flow
   through lionagi's run path into messages → bus → the emit hook → the wire → the
   bridge. The Daytona snapshot must carry node + the pi npm package.

## Strategic context (why this is the spine, not a side feature)

The endgame is a hosted "flow as a service" and a GitHub App
(`@lionagi resolve this issue` → flow in a sandbox → PR draft). The honest
worry is quality: autonomy is commoditized; harness engineering is the moat, and
we have exactly one real number (SWE-bench 8/50, engagement-gated). **This feature
is also the eval harness.** Each `issue → flow → PR draft → (CI green? merged?)`
run is a labeled task with a real reward signal. Building the
observability/control spine makes every run a fully-traced, monitorable,
scoreable data point — the substrate the orchestration optimizer (DOE-driven
config search) consumes. Product, measurement, and optimization are one system.

Phasing:

- **Phase 1 (this ADR's core):** `SandboxBridge` + wire protocol — the parity
  engine, unit-verifiable against a temp `state.db` with **no Daytona needed**.
- **Phase 2:** generic in-sandbox `entry.py` (emit-only persistence hook, runs a
  flow/agent with pi/OpenRouter); `run_flow_in_sandbox` glue (DaytonaSandbox +
  bridge over an asyncio queue); `li o flow --sandbox daytona` CLI flag; the
  pi-enabled snapshot. Verified by a real Daytona run.
- **Phase 3:** control wiring (`li kill` → bridge → control file); GitHub App
  trigger (`@lionagi` → enqueue flow → PR draft); the optimizer loop.

## Consequences

**Positive.** Unified observability/control for remote runs with no new monitor
code. The bridge reuses the exact StateDB API, so parity can't silently drift —
a regression test pins it. The sandbox stays stateless (no in-container DB,
no merge logic). The wire protocol is additive (coexists with `@@SIG@@`).

**Negative / risks.** (a) More bytes on the wire than the lossy summary — full
message dicts per event; acceptable for fidelity, and stdout streaming is already
the channel. (b) The sync `exec_stream` `on_stdout` callback must hand events to
the async bridge via a queue + drain task (Phase 2). (c) Ordering: a `message`
event for an unseen branch is tolerated by lazily creating a minimal branch row
(mirrors the local lazy `_ensure_branch_row`). (d) Provider-key handling: keys
travel in the in-sandbox spec file, never argv/env (session commands don't inherit
creation-time env_vars — `_sandbox_entry.py:219`).

## Alternatives considered

- **Sandbox writes its own `state.db`, host syncs it.** Rejected: needs DB merge,
  polling, and conflict handling; worse "live" latency; two writers.
- **Network-mounted `state.db` (sandbox writes host DB directly).** Rejected:
  requires NFS/SMB or an HTTP DB shim; SQLite over a network FS is a known
  foot-gun; couples the container to host filesystem topology.
- **Keep the lossy `@@SIG@@` summary, reconstruct rows on the host.** Rejected:
  not "exactly the same as local" — loses message content/role fidelity that
  Studio renders.

```text
