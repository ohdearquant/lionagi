# Playbook Field Reference

## Complete Field Table

Over MCP, call `play.submit` with `args: {playbook: "<name>", ...}`, where the
remaining keys are the underscore form of the fields below (e.g. `max_ops`,
`with_synthesis`). Confirm exact argument names for your published server version
with `help=true` before relying on this table. The CLI column applies only inside
a lionagi checkout.

| Field | Type | CLI equivalent | Description |
|---|---|---|---|
| `name` | str | — | Descriptive identifier. Keep it equal to the filename stem: the CLI resolves a playbook by filename, and nothing validates the two against each other, so a mismatch is silent. |
| `description` | str | — | Free text shown by `li play <name> --help`. `li play list` prints names only. |
| `argument-hint` | str | — | CC-compatible display hint, e.g. `'[--mode MODE] [--strict]'`. Sets the `--help` usage line and, when no `args:` block exists, supplies a fallback argument schema. |
| `model` | str | positional | Model spec: `claude-code/sonnet-4-6`, `codex/gpt-5.4`. |
| `agent` | str | `-a/--agent` | Orchestrator agent profile from `~/.lionagi/agents/<name>/<name>.md`. |
| `effort` | str | `--effort` | Accepted values depend on the provider: Claude `low \| medium \| high \| xhigh \| max`; Codex `none \| minimal \| low \| medium \| high \| xhigh \| max \| ultra`, where `max` and `ultra` clamp to what the model supports. Gemini folds effort into the model spec instead. Omit to use the profile default. |
| `workers` | int | `--max-concurrent` | Max concurrent agents. Range: 1–32. |
| `max_ops` | int | `--max-ops` | Cap on total DAG operations. `0` = unlimited. Range: 0–50. |
| `with_synthesis` | bool or str | `--with-synthesis` | `true` uses the orchestrator model; a model spec string uses that model. |
| `team_mode` | str | `--team-mode` | Create a fresh team (new UUID) each invocation. Value is the team name. |
| `team_attach` | str | `--team-attach` | Upsert a team by name: attach if it exists, create if missing. |
| `bare` | bool | `--bare` | Ignore agent profiles; all workers use the CLI model. |
| `dry_run` | bool | `--dry-run` | Plan the DAG without executing it. |
| `show_graph` | bool | `--show-graph` | Write a DAG visualisation after an executing flow finishes. |
| `reactive` | str | `--reactive` | Who may request follow-up assignments: `all`, `off`, or a comma-separated role list. |
| `save` | str | `--save` | Directory to write artifact output to. |
| `prompt` | str | — | Template string. May contain `{input}` and `{arg_name}` placeholders. |
| `args` | dict | dynamic flags | Typed argument schema. Each key becomes a CLI flag, or an `args` entry in the `play.submit` MCP call. |

**Key normalization**: when a YAML or JSON playbook is loaded, top-level keys accept both
dash and underscore forms (`max-ops` and `max_ops` both work). The `args:` block is an
exception — use only underscore keys there (see Pitfalls). Over MCP, always pass underscore
keys.

**Precedence**: a playbook field fills in only when the corresponding CLI flag is still at its
default, so a flag you pass explicitly wins. Two consequences of *how* that is implemented are
worth knowing before they surprise you.

- **The boolean fields cannot be switched back off from the command line.** `bare`, `dry_run`,
  `show_graph` and `with_synthesis` are flags that only ever turn something on; there is no
  negating flag. A playbook that sets one to `true` sets it for every invocation of that
  playbook, and no command line can undo it.
- **`0` is both the default and a meaningful value for the two numeric caps.** `--max-ops 0`
  means unlimited and `--max-concurrent 0` means run the whole phase at once, but `0` is also
  what those flags hold when you do not pass them, so neither can be told apart from absence.
  A playbook that sets `max_ops` or `workers` therefore overrides an explicit `0`, and the
  uncapped run you asked for is capped without saying so. To actually run uncapped, remove the
  field from the playbook rather than passing `0`.

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
  worker_count:
    type: int
    default: 8
    help: "parallel workers (1-32)"
  strict:
    type: bool
    default: false
    help: "fail on any finding above MEDIUM severity"
```

Over MCP, `playbook` and `prompt` are confirmed `play.submit` argument names. For a custom
`args:` field such as `mode` or `strict`, read the schema that
`help={"verb": "play.submit", "playbook": "audit"}` returns: naming the playbook resolves
*that* playbook's declared arguments into the reply, which a bare `help=true` catalog request
does not do. The same call returns the `schema_fingerprint` the op must carry as a sibling of
`args`; without it the op is refused with `stale_schema` and no run starts:

```json
{
  "ops": [
    {
      "op": "play.submit",
      "args": {"playbook": "audit", "prompt": "scan auth/"},
      "schema_fingerprint": "<from that help call>"
    }
  ]
}
```

Inside a lionagi checkout, the same run with the custom flags set is:
`li play audit --mode security --worker-count 4 --strict "scan auth/"`

**Field rules**, grouped by how they fail. That grouping is the useful part: only the first
group stops you, and the further down the list a mistake sits, the more it looks like it worked.

*Enforced — the spec is rejected with a message:*
- Key names must be alphanumeric and use underscores only (not dashes)
- `type`, **when present**, must be one of `str`, `int`, `float`, `bool`

*Warned and degraded — the run continues without your argument:*
- Key names must not collide with a built-in flag. An arg named `workers` becomes `--workers`,
  which already means the comma-separated worker-model pool, so the playbook's own argument is
  dropped with a warning and the built-in flag takes the value instead. The command still runs,
  which is why this is worth checking rather than waiting to be told: name it `worker_count`
  and the flag becomes `--worker-count`. Run `li play <name> --help` after declaring an arg to
  see whether it actually appears.

*Defaults applied when fields are omitted:*
- `type` defaults to `str`. Declare it whenever the value is not a string; a supplied invalid
  type is rejected during spec validation
- `default` may be omitted, in which case it is null. Supply one unless absence is meaningful
- `help` may be omitted, in which case `--help` shows a generated line naming the argument and
  its type. Write one sentence with the allowed values if they matter

---

## Template Interpolation

The `prompt` field is a template. Substitution rules:

1. `{input}` is replaced with the positional text the user passes — the `prompt` argument
   on a `play.submit` call, or the trailing quoted string on `li play <name> "..."`.
2. `{arg_name}` is replaced with the value of the corresponding arg (override or default).
3. If the template has **no** placeholders and the user passed positional text, the positional
   text is appended after a blank line (CC-skill style).
4. Missing keys are left as literal `{name}` tokens — they do not raise errors.

```yaml
prompt: |
  Run a {mode} audit with {worker_count} parallel workers. Strict: {strict}.

  Target: {input}
```

If `mode=dry`, `worker_count=8`, `strict=false`, and the caller passes `"src/auth/"`
(inside a lionagi checkout: `li play audit "src/auth/"`), the rendered prompt becomes:

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

**The rule**: every option already installed on the `li o flow` parser is reserved. The
resolver compares each generated flag against the parser's entire existing option set, so the
reserved names are whatever that parser happens to define — not a fixed list this document
owns. Declaring an `args:` key that maps to one of them logs a warning and skips your argument;
the built-in flag keeps the name and takes the value.

**The check that is always current**: run `li play <name> --help` after declaring an argument.
If your flag is not listed, it collided. This is worth doing rather than consulting any list,
because the list below can only be correct for the version it was written against.

Reserved as of this revision, all 36 of them:

`agent`, `allow_degraded_context`, `background`, `bare`, `bypass`, `cwd`, `dry_run`, `effort`,
`fast`, `file`, `help`, `invocation`, `max_agents`, `max_concurrent`, `max_ops`, `mcp_config`,
`no_mcp_config`, `notify`, `output`, `pack`, `playbook`, `project`, `reactive`, `resume`,
`resume_on_timeout`, `save`, `show_graph`, `team_attach`, `team_max_rounds`, `team_mode`,
`theme`, `timeout`, `verbose`, `with_synthesis`, `workers`, `yolo`

Note `workers` in that list. It is the collision the example above walks through, and it is the
reason to trust the rule over any list: an earlier revision of this page listed 24 names and
omitted `workers` along with 11 others, while explaining the `workers` collision two sections
up. A hand-maintained list drifts from the parser silently and in the reassuring direction.

---

## Common Pitfalls

**Dashed keys inside `args:`**
Top-level spec keys normalize dashes to underscores automatically. Keys inside
`args:` do NOT — use `my_arg`, not `my-arg`. A dashed key inside `args:` will fail
schema validation with "must be an alphanumeric identifier".

**`workers: 0` is invalid**
`workers` maps to `--max-concurrent` (range 1–32). For unlimited ops, set
`max_ops: 0` (which is the default). Do not conflate the two fields.

**Expecting `show_graph` during a dry run**
`dry_run: true` returns before the run graph is built. `show_graph: true` writes
`flow_dag.png` to the run's artifact directory only after an executing flow finishes; over
MCP, find it in the artifact list returned by `job.output`.

**Unknown top-level keys are silently ignored**
There is no schema validation error for unrecognized keys. A typo like `effrot: high`
takes no effect and produces no warning.

**`team_mode` and `team_attach` both set**
The CLI rejects this at dispatch time with an error. Only one team strategy is allowed
per playbook.

**`max_ops` range**
Valid range is 0–50 (0 = unlimited). Values above 50 are rejected at spec validation.
For a nonzero value, the initial plan and reactive follow-up assignments share the cap. A
plan that fills `max_ops` leaves no room for follow-ups. With `max_ops: 0`, the initial plan
has no caller-defined cap (the engine defensively truncates it at 200) and reactive execution
allows up to 20 follow-up assignments.

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
