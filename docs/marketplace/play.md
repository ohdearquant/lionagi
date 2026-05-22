# play Plugin

Playbook authoring for lionagi orchestration flows — define reusable, parametric workflow templates that `li play` and `li o flow` can load and execute.

**Source**: `marketplace/play/`  
**Install**: `claude /plugin install play@lionagi`  
**Version**: 0.1.0 (Apache-2.0)

## Skills

| Skill | Description |
|---|---|
| [`/write-playbook`](#write-playbook) | Author a correct lionagi playbook YAML: file location, all recognized spec fields, args schema, template interpolation, team semantics, and common pitfalls |

### `/write-playbook`

> **Source**: `marketplace/play/skills/write-playbook/SKILL.md`

A **playbook** is a YAML file under `~/.lionagi/playbooks/<name>.playbook.yaml` that declares a reusable, parametric flow invocation.

```bash
li play <name> [args] "positional prompt"     # sugar
li o flow -p <name> [args] "positional prompt" # long form
```

**Source of truth**: `firm/resources/playbooks/<name>.playbook.yaml`, symlinked into `~/.lionagi/playbooks/`. Edit in firm, no sync needed.

---

#### Minimum viable playbook

```yaml
name: hello
description: Greet the user and answer a simple question.

model: claude-code/sonnet-4-6

prompt: |
  You are a patient teacher. Answer concisely.
```

Invoke with `li play hello "what is a monad?"`. The positional prompt is **appended** (blank line) because the template has no `{...}` placeholders.

---

#### All recognized top-level fields

Both `dashed-form` and `underscore_form` are accepted for all keys — the spec loader normalizes to Python identifier form at load time.

| Key | Type | Maps to CLI | Purpose |
|---|---|---|---|
| `name` | str | — | Playbook identifier (should match filename) |
| `description` | str | — | Free-text; surfaced in docs/tooling |
| `argument-hint` | str | — | CC-compatible display string like `'[--tabs N] [--poll]'`. Fallback if `args:` absent. |
| `model` | str | positional model | Orchestrator model spec, e.g. `claude-code/opus-4-7` |
| `agent` | str | `-a/--agent` | Agent profile name (from `~/.lionagi/agents/<name>.md`) |
| `effort` | str | `--effort` | `low \| medium \| high \| xhigh` |
| `workers` | int | `--max-concurrent` | Max concurrent agents per phase (1–32) |
| `max_agents` | int | `--max-agents` | Cap total ops (1–50, 0 = unlimited) |
| `with_synthesis` | bool | `--with-synthesis` | Final synthesis pass after all ops |
| `team_mode` | str | `--team-mode` | Fresh team per invocation (new UUID every time) |
| `team_attach` | str | `--team-attach` | Upsert team by name — attach if exists, create if missing |
| `bare` | bool | `--bare` | Ignore agent profiles; all workers use CLI model |
| `dry_run` | bool | `--dry-run` | Plan DAG, print, do not execute |
| `show_graph` | bool | `--show-graph` | Render DAG as matplotlib PNG (requires `--save`) |
| `save` | str | `--save` | Artifact output directory |
| `prompt` | str | — | Template (supports `{input}` + named args); required for `li play` |
| `args` | dict | dynamic flags | Typed arg schema — see below |

CLI flags always override playbook defaults.

---

#### `args:` schema (preferred)

Typed, validated CLI args that map to template placeholders.

```yaml
args:
  mode:
    type: str           # str | int | float | bool
    default: dry
    help: "audit mode"
  workers:
    type: int
    default: 8
  strict:
    type: bool          # bool defaults to False; --strict sets True
    default: false
```

CLI access: `li play audit --mode security --workers 12`.

Naming rules: use underscores in schema; CLI flags become dashed form (`my_arg` → `--my-arg`); reference by underscored name in template (`{my_arg}`).

**Reserved names** (clash with base flags — injection skipped with warning): `--file/-f`, `--playbook/-p`, `--agent/-a`, `--with-synthesis`, `--max-concurrent`, `--output`, `--save`, `--team-mode`, `--team-attach`, `--dry-run`, `--show-graph`, `--background`, `--bare`, `--max-agents`, `--yolo`, `-v/--verbose`, `--theme`, `--effort`, `--cwd`, `--timeout`. Do not redeclare these in `args:`.

---

#### `argument-hint` fallback

Use only when you cannot write a full `args:` schema (e.g. wrapping a CC skill verbatim):

```yaml
argument-hint: '[--tabs N] [--poll] [--harvest]'
```

Parsing rules: `[--flag VALUE]` → string arg, default `null`; `[--flag]` → bool arg, default `false`. No type coercion.

If both `args:` and `argument-hint:` are present, `args:` wins. Best practice: always migrate to `args:` — you get `--help` output, type checks, and documented defaults.

---

#### Template interpolation

Inside `prompt:`, three substitution rules fire in order:

```yaml
# 1. {input} only — positional prompt replaces placeholder
prompt: "Summarize this: {input}"
# li play x "the RFC"  →  "Summarize this: the RFC"

# 2. Named args + {input}
prompt: "Run {workers} workers in {mode} mode. Target: {input}"
# li play x --workers 5 --mode deep "auth crate"
# →  "Run 5 workers in deep mode. Target: auth crate"

# 3. No placeholders — positional appended with blank line
prompt: "You are a code reviewer. Be terse."
# li play x "review PR #42"
# →  "You are a code reviewer. Be terse.\n\nreview PR #42"
```

Undeclared placeholders remain literal `{xxx}` tokens — they are not errors.

---

#### Team semantics

Pick ONE — `team_mode` and `team_attach` are mutually exclusive (error at dispatch):

```yaml
# Always fresh — new team, new UUID every invocation.
# Results not accessible to future runs.
team_mode: one-off-audit

# Upsert — first run creates team called "ongoing-review".
# Subsequent runs attach and see accumulated message history.
team_attach: ongoing-review
```

Neither requires a pre-existing team — both create-on-miss.

---

#### Full-featured example

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

---

#### Common pitfalls

!!! warning "Silent no-ops from unknown keys"
    Top-level keys not in the recognized list are silently ignored. `max-agents: 10` works (normalized); `maxAgents: 10` or `max_iterations: 10` just vanish. Check the field table before inventing keys.

!!! warning "Invalid `effort` value"
    Must be one of `low | medium | high | xhigh`. Anything else causes a spec validation error. `quick` is not a valid value and is rejected.

!!! warning "`show_graph: true` without `--save`"
    Graph rendering needs an output directory. Either add `save: ./graphs/` to the playbook or pass `--save DIR` on the CLI.

| Pitfall | Effect | Fix |
|---|---|---|
| Dashed keys inside `args:` | Unpredictable behavior | Use `my_arg:`, not `my-arg:` |
| `args.save` or `args.verbose` declared | Shadowed by base CLI flags | Rename (`output-dir`, `chatty`) |
| `workers: 0` | Validation error — range is [1, 32] | Use `max_agents: 0` for unlimited ops |
| `team_mode` + `team_attach` both set | Dispatch error | Pick one |
| `prompt` over 8192 chars | Cap exceeded | Delegate to skills read at runtime |

---

#### Installing a playbook

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

---

#### Authoring checklist

- [ ] Filename is `<name>.playbook.yaml` (exact suffix matters)
- [ ] Location: `firm/resources/playbooks/` (source), symlinked into `~/.lionagi/playbooks/`
- [ ] `name:` matches the filename stem
- [ ] `description:` is one clear sentence
- [ ] Either `model:` or `agent:` is set (both fine; CLI can override)
- [ ] `prompt:` references exactly the placeholders declared in `args:` plus optional `{input}`
- [ ] Every arg in `args:` has `type`, sensible `default`, and `help`
- [ ] No dashed keys inside `args:` — use `my_arg:` not `my-arg:`
- [ ] `team_mode` and `team_attach` are not both set
- [ ] Smoke test: `li o flow -p <name> --help` shows custom flags with types
- [ ] Smoke test: `li o flow -p <name> --dry-run "test input"` plans without executing
