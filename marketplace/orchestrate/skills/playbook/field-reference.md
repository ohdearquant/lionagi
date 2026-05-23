# Playbook Field Reference

## Complete Field Table

| Field | Type | CLI equivalent | Description |
|---|---|---|---|
| `name` | str | — | Playbook identifier. Must match the filename stem exactly. |
| `description` | str | — | Free-text shown by `li play list` and `li play <name> --help`. |
| `argument-hint` | str | — | CC-compatible display hint, e.g. `'[--mode MODE] [--strict]'`. Used in `--help` output only. |
| `model` | str | positional | Model spec: `claude-code/sonnet-4-6`, `codex/gpt-5.4`. |
| `agent` | str | `-a/--agent` | Orchestrator agent profile from `~/.lionagi/agents/<name>/<name>.md`. |
| `effort` | str | `--effort` | `low \| medium \| high \| xhigh`. Omit to use the profile default. |
| `workers` | int | `--max-concurrent` | Max concurrent agents. Range: 1–32. |
| `max_ops` | int | `--max-ops` | Cap on total DAG operations. `0` = unlimited. Range: 0–50. |
| `with_synthesis` | bool or str | `--with-synthesis` | `true` uses the orchestrator model; a model spec string uses that model. |
| `team_mode` | str | `--team-mode` | Create a fresh team (new UUID) each invocation. Value is the team name. |
| `team_attach` | str | `--team-attach` | Upsert a team by name: attach if it exists, create if missing. |
| `bare` | bool | `--bare` | Ignore agent profiles; all workers use the CLI model. |
| `dry_run` | bool | `--dry-run` | Plan the DAG without executing it. |
| `show_graph` | bool | `--show-graph` | Render a DAG visualisation after planning. |
| `save` | str | `--save` | Directory to write artifact output to. |
| `prompt` | str | — | Template string. May contain `{input}` and `{arg_name}` placeholders. |
| `args` | dict | dynamic flags | Typed argument schema. Each key becomes a CLI flag. |

**Key normalization**: top-level keys accept both dash and underscore forms
(`max-ops` and `max_ops` both work). The `args:` block is an exception — use
only underscore keys there (see Pitfalls).

---

## Args Schema

Declare custom arguments under `args:`. Each entry becomes a typed CLI flag
that fills a `{arg_name}` placeholder in `prompt`.

```yaml
args:
  mode:
    type: str        # str | int | float | bool
    default: dry
    help: "audit mode: dry | security | dead-code"
  workers:
    type: int
    default: 8
    help: "parallel workers (1-32)"
  strict:
    type: bool
    default: false
    help: "fail on any finding above MEDIUM severity"
```

These become CLI flags: `li play audit --mode security --workers 4 --strict "scan auth/"`

**Field rules**:
- `type` must be one of `str`, `int`, `float`, `bool`
- `default` is required; do not leave it null unless `type: str` and absence is meaningful
- `help` should be one concise sentence with allowed values if applicable
- Key names must be alphanumeric and use underscores only (not dashes)

---

## Template Interpolation

The `prompt` field is a template. Substitution rules:

1. `{input}` is replaced with the positional text the user passes after the playbook name.
2. `{arg_name}` is replaced with the value of the corresponding arg (CLI override or default).
3. If the template has **no** placeholders and the user passed positional text, the positional
   text is appended after a blank line (CC-skill style).
4. Missing keys are left as literal `{name}` tokens — they do not raise errors.

```yaml
prompt: |
  Run a {mode} audit with {workers} parallel workers. Strict: {strict}.

  Target: {input}
```

If the user runs `li play audit "src/auth/"`, and `mode=dry`, `workers=8`, `strict=false`,
the rendered prompt becomes:

```
Run a dry audit with 8 parallel workers. Strict: False.

Target: src/auth/
```

---

## Team Semantics

Use `team_mode` or `team_attach` to give agents a persistent shared message channel.
Never set both — they are mutually exclusive.

- **`team_mode: name`** — creates a fresh team with a new UUID every invocation. Prior
  messages from previous runs are not visible. Use for stateless, independent runs.
- **`team_attach: name`** — upserts by name: loads the existing team if it exists
  (preserving its message history), creates fresh if it does not. Use for stateful
  workflows where agents need continuity across runs.

```yaml
# Fresh team every run
team_mode: my-pipeline

# Persistent team — agents see each other's prior messages
team_attach: project-audit
```

---

## Reserved Arg Names

These flags are already defined by the base CLI parser. Declaring an `args:` key
that maps to one of them will be silently skipped (a warning is logged; the built-in
flag wins).

`file`, `playbook`, `agent`, `with_synthesis`, `max_concurrent`, `output`, `save`,
`team_mode`, `team_attach`, `dry_run`, `show_graph`, `background`, `bare`, `max_ops`,
`yolo`, `bypass`, `verbose`, `theme`, `fast`, `effort`, `cwd`, `timeout`,
`invocation`, `project`

---

## Common Pitfalls

**Dashed keys inside `args:`**
Top-level spec keys normalize dashes to underscores automatically. Keys inside
`args:` do NOT — use `my_arg`, not `my-arg`. A dashed key inside `args:` will fail
schema validation with "must be an alphanumeric identifier".

**`workers: 0` is invalid**
`workers` maps to `--max-concurrent` (range 1–32). For unlimited ops, set
`max_ops: 0` (which is the default). Do not conflate the two fields.

**`show_graph: true` without `--save`**
The graph renders to the screen via matplotlib. If `save` is set, it is written as
a PNG to the save directory.

**Unknown top-level keys are silently ignored**
There is no schema validation error for unrecognized keys. A typo like `effrot: high`
takes no effect and produces no warning.

**`team_mode` and `team_attach` both set**
The CLI rejects this at dispatch time with an error. Only one team strategy is allowed
per playbook.

**`max_ops` range**
Valid range is 0–50 (0 = unlimited). Values above 50 are rejected at spec validation.
If you need large plans, leave `max_ops: 0` and rely on the 200-op hard cap in the
engine.

**CLI-only flags cannot be set in YAML**
`yolo`, `bypass`, `output`, `background`, `fast`, `verbose`, and `theme` are CLI-only.
Specifying them in YAML has no effect — always pass them on the command line.

---

## Source Code Reference

| Concern | File |
|---|---|
| Playbook loading and spec validation | `lionagi/cli/orchestrate/__init__.py` |
| Playbook path resolution | `lionagi/cli/orchestrate/__init__.py` (`_resolve_playbook_path`) |
| Template interpolation | `lionagi/cli/orchestrate/__init__.py` (`_interpolate_prompt`) |
| `li play` sugar expansion | `lionagi/cli/main.py` (`_handle_play_shortcut`) |
| Flow execution engine | `lionagi/cli/orchestrate/flow.py` |
| Playbook directory | `~/.lionagi/playbooks/` |
