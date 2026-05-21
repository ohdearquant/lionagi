---
name: write-playbook
description: >
  Author a correct lionagi playbook — the YAML that `li play NAME` / `li o flow -p NAME`
  loads. Covers file location, every recognized spec field, args schema, argument-hint
  fallback, template interpolation, team semantics, and common pitfalls. Pull this
  before editing anything under ~/.lionagi/playbooks/ or firm/resources/playbooks/.
allowed-tools: [Read, Write, Edit, Bash]
---

# Writing a lionagi playbook

A **playbook** is a YAML file under `~/.lionagi/playbooks/<name>.playbook.yaml`
that declares a reusable, parametric flow invocation. Invoke with
`li play <name> [args] "positional prompt"` (sugar) or
`li o flow -p <name> [args] "positional prompt"` (long form).

**Source of truth**: `firm/resources/playbooks/<name>.playbook.yaml`, symlinked
into `~/.lionagi/playbooks/`. Edit in firm, no sync needed.

## Minimum viable playbook

```yaml
name: hello
description: Greet the user and answer a simple question.

model: claude-code/sonnet-4-6

prompt: |
  You are a patient teacher. Answer concisely.
```

Invoke: `li play hello "what is a monad?"`
Behavior: positional prompt is **appended** (blank line) because the template
has no `{...}` placeholders.

## Every recognized top-level field

All keys accept EITHER `dashed-form` OR `underscore_form`. Internally both
normalize to Python identifier form at load time.

| Key | Type | Maps to CLI | Purpose |
|-----|------|-------------|---------|
| `name` | str | — | Playbook identifier (should match filename) |
| `description` | str | — | Free-text; surfaced in docs/tooling |
| `argument-hint` | str | — | CC-compatible display string like `'[--tabs N] [--poll]'`. Fallback arg schema if `args:` absent. |
| `model` | str | `<positional model>` | Orchestrator model spec, e.g. `claude-code/opus-4-7` |
| `agent` | str | `-a/--agent` | Agent profile name (from `~/.lionagi/agents/<name>/<name>.md`) |
| `effort` | str | `--effort` | `low \| medium \| high \| xhigh` |
| `workers` | int | `--max-concurrent` | Max concurrent agents per phase (1-32) |
| `max_agents` | int | `--max-agents` | Cap total ops (1-50, 0 = unlimited) |
| `with_synthesis` | bool | `--with-synthesis` | Final synthesis pass after all ops |
| `team_mode` | str | `--team-mode` | FRESH team per invocation (new UUID every time) |
| `team_attach` | str | `--team-attach` | Upsert team by name — attach if exists, create if missing |
| `bare` | bool | `--bare` | Ignore agent profiles; all workers use CLI model |
| `dry_run` | bool | `--dry-run` | Plan DAG, print, do not execute |
| `show_graph` | bool | `--show-graph` | Render DAG as matplotlib PNG (requires `--save`) |
| `save` | str | `--save` | Artifact output dir |
| `prompt` | str | — | Template (supports `{input}` + named args); required for `li play` |
| `args` | dict | dynamic flags | Typed arg schema — see below |

CLI flags always override playbook defaults. Positional prompt goes through
template interpolation (see below).

## args: schema (preferred)

Typed, validated CLI args that map to template placeholders.

```yaml
args:
  mode:
    type: str           # str | int | float | bool
    default: dry        # used when CLI flag omitted
    help: "audit mode"  # shown in --help
  workers:
    type: int
    default: 8
  strict:
    type: bool          # bool defaults to False; --strict sets True
    default: false
```

CLI access: `li play audit --mode security --workers 12`.

Naming: use underscores in schema; CLI flags become `--underscore-form` →
`--my-arg` (dashed). In the prompt template, reference by underscored name:
`{my_arg}`.

**Reserved names** (clash with base flags — injection is skipped with warning):
`--file/-f`, `--playbook/-p`, `--agent/-a`, `--with-synthesis`, `--max-concurrent`,
`--output`, `--save`, `--team-mode`, `--team-attach`, `--dry-run`, `--show-graph`,
`--background`, `--bare`, `--max-agents`, `--yolo`, `-v/--verbose`, `--theme`,
`--effort`, `--cwd`, `--timeout`. Don't redeclare these in `args:`.

## argument-hint fallback

Use ONLY when you can't write a full `args:` schema (e.g. wrapping a CC skill
verbatim). Parsing rules:

- `[--flag VALUE]` or `[--flag N]` → string arg, default `null`
- `[--flag]` → bool arg, default `false`
- No type coercion; everything is a string except bool flags

```yaml
argument-hint: '[--tabs N] [--poll] [--harvest]'
```

If both `args:` and `argument-hint:` are present, **`args:` wins**. Best
practice: always migrate to `args:` — you get `--help` output, type checks,
and documented defaults.

## Template interpolation

Inside `prompt:`, three substitution rules fire in order:

1. **`{input}`** → the positional prompt text passed on the CLI.
2. **`{arg_name}`** → a declared arg (CLI value > `default`). Undeclared
   placeholders remain literal `{xxx}` tokens.
3. **No placeholders present** → positional text is **appended** with a blank
   line, CC slash-command style.

```yaml
# 1. {input} only
prompt: "Summarize this: {input}"
# li play x "the RFC"  →  "Summarize this: the RFC"

# 2. Named args + {input}
prompt: "Run {workers} workers in {mode} mode. Target: {input}"
# li play x --workers 5 --mode deep "auth crate"
# →  "Run 5 workers in deep mode. Target: auth crate"

# 3. No placeholders — positional appended
prompt: "You are a code reviewer. Be terse."
# li play x "review PR #42"
# →  "You are a code reviewer. Be terse.\n\nreview PR #42"
```

## Team semantics (pick ONE, not both)

```yaml
# Always fresh — new team, new UUID every invocation. Results not
# accessible to future runs.
team_mode: one-off-audit

# Upsert — first run creates team called "ongoing-review", subsequent runs
# attach to the same team and see accumulated message history.
team_attach: ongoing-review
```

`team_mode` and `team_attach` are mutually exclusive (error at dispatch).
Neither requires a pre-existing team — both create-on-miss.

## Common pitfalls

**Silent no-ops from unknown keys.** Top-level keys not in the recognized list
are ignored silently. `max-agents: 10` works (normalized to `max_agents`), but
`maxAgents: 10` or `max_iterations: 10` just vanish. Check the table above
before inventing fields.

**Dash vs. underscore.** Spec loader auto-normalizes `max-agents` → `max_agents`
for top-level keys. It does NOT touch `argument-hint` (CC convention) and does
NOT touch names inside `args:` (you own them). Use whatever's consistent with
the surrounding code.

**`args:` arg name collisions with built-ins.** If you declare `args.save` or
`args.verbose`, your flag gets shadowed by the base CLI. Rename (`output-dir`,
`chatty`).

**Invalid `effort` value.** Must be one of `low | medium | high | xhigh`.
Anything else → spec validation error.

**`prompt` length.** Cap is 8192 chars. Longer templates should delegate to
skills the orchestrator reads at runtime (`li skill <name>`).

**`show_graph: true` without `--save`.** Graph rendering needs an output dir.
Either add `save: ./graphs/` to the playbook or pass `--save DIR` on the CLI.

**`workers: 0`.** Range is [1, 32]. Use `max_agents: 0` for "unlimited ops";
`workers` means per-phase concurrency and must be a positive int if set.

## Full-featured example

```yaml
name: audit
description: >
  Parallel codebase audit. Orchestrator plans N specialists; one critic
  synthesis pass. Optional persistent team for cross-invocation notes.

argument-hint: '[--mode MODE] [--strict] [--notes-team NAME]'

model: claude-code/opus-4-7
agent: orchestrator
effort: high
max_agents: 10
with_synthesis: true

args:
  mode:
    type: str
    default: dry
    help: "audit mode: dry | security | dead-code | api-surface"
  strict:
    type: bool
    default: false
    help: "treat any finding above MEDIUM as blocking"
  notes_team:
    type: str
    default: ""
    help: "if set, attach to this team for persistent notes"

prompt: |
  Run a {mode} audit on the target below. Strict mode: {strict}.
  Persistent notes team (empty string = none): "{notes_team}".

  Target: {input}

  Deploy specialists in parallel, one per module. Collect findings.
  Critic synthesizes at the end — output MUST-FIX / SHOULD-FIX / CONSIDER
  with file:line citations.
```

## Authoring checklist

- [ ] Filename is `<name>.playbook.yaml` (exact suffix matters)
- [ ] Location: `firm/resources/playbooks/` (source), symlinked into `~/.lionagi/playbooks/`
- [ ] `name:` matches the filename stem
- [ ] `description:` is one clear sentence — shown in tooling
- [ ] Either `model:` or `agent:` is set (both fine; CLI can override)
- [ ] `prompt:` references exactly the placeholders declared in `args:` plus optional `{input}`
- [ ] Every declared arg in `args:` has a `type`, a sensible `default`, and a `help`
- [ ] No dashed keys inside `args:` (use `my_arg:` not `my-arg:` for the schema)
- [ ] `team_mode` and `team_attach` are not both set
- [ ] Smoke test: `li o flow -p <name> --help` shows your custom flags with types
- [ ] Smoke test: `li o flow -p <name> --dry-run "test input"` plans without executing

## Installing a new playbook

```bash
# 1. Write it in firm (source of truth)
vim firm/resources/playbooks/my-thing.playbook.yaml

# 2. Symlink into ~/.lionagi/playbooks/ so `li play` sees it
ln -s "$(pwd)/my-thing.playbook.yaml" \
      ~/.lionagi/playbooks/my-thing.playbook.yaml

# 3. Verify
li play list                                  # should list 'my-thing'
li o flow -p my-thing --help                  # should show your args
li play my-thing --dry-run "sample input"     # plans without executing
```
