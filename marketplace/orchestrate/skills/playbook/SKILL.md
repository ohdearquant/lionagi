---
name: playbook
description: >
  Author lionagi playbooks — reusable YAML workflow templates that define parametric
  agent tasks. Playbooks live at ~/.lionagi/playbooks/ and run via the `play.submit`
  MCP verb (or `li play <name>` from a lionagi checkout).
  Use when: creating reusable workflows, parameterizing agent tasks, or setting up
  repeatable pipelines.
allowed-tools: [Bash, Read, Write, Glob, Grep]
---

# Authoring Lionagi Playbooks

A playbook is a `.playbook.yaml` file that defines a reusable, parametric agent
workflow. Install it in `~/.lionagi/playbooks/` on the machine the MCP server runs
on — that filesystem step is the same whichever interface runs the playbook.

To run it, call the `mcp__plugin_orchestrate_lion__request` tool with `play.submit`. Ask for
the fingerprint first, naming the playbook — `play.submit`'s schema depends on which playbook
you name, because a playbook's own declared arguments are resolved into it:

```json
{"help": {"verb": "play.submit", "playbook": "hello"}}
```

```json
{"ops": [{"op": "play.submit", "args": {"playbook": "hello", "prompt": "what is a monad?"}, "schema_fingerprint": "<from the help call above>"}]}
```

The fingerprint is a **sibling of `args`**, never a key inside it. Omit it and the op is
refused with `stale_schema` and no run starts; nest it inside `args` and it is not read, so
the same refusal repeats and the failure reads as idempotent.

If you're working inside a lionagi checkout, the CLI equivalent is
`li play <name> "<prompt>"`, which expands to `li o flow -p <name> "<prompt>"`.

---

## Minimum Viable Playbook

```yaml
name: hello
description: Greet and answer a question.
model: claude-code/sonnet-4-6

prompt: |
  You are a patient teacher. Explain the following topic in plain language,
  with one concrete example.
```

Run it with `play.submit`:

```json
{"ops": [{"op": "play.submit", "args": {"playbook": "hello", "prompt": "what is a monad?"}, "schema_fingerprint": "<from help={\"verb\": \"play.submit\", \"playbook\": \"hello\"}>"}]}
```

Inside a lionagi checkout: `li play hello "what is a monad?"`

The positional text is appended to the prompt with a blank line because the
template contains no `{input}` placeholder. That is the only behaviour difference
from a template that declares `{input}` explicitly.

---

## Authoring Checklist

- [ ] Filename: `<name>.playbook.yaml`
- [ ] Location: `~/.lionagi/playbooks/`
- [ ] `name:` equals the filename stem — nothing validates the two against each other, and the
      CLI resolves by filename, so a mismatch is silent rather than an error
- [ ] `description:` is one clear sentence
- [ ] Choose routing deliberately: `model:` and `agent:` are optional; when both are omitted,
      flow uses its default orchestrator configuration. If `agent:` is set, verify that profile
      exists on the machine running the MCP server
- [ ] `prompt:` references only declared `args` keys and optional `{input}`
- [ ] Every `args` entry declares `type`, `default`, and `help`. A supplied `type` is validated;
      omitting these fields yields `str`, null, and a generated help line respectively
- [ ] No dashed keys inside `args:`
- [ ] `team_mode` and `team_attach` are not both set
- [ ] `workers` is 1–32 if set; `max_ops` is 0–50 if set
- [ ] If reactive follow-ups are enabled with a nonzero `max_ops`, leave room below the cap;
      the initial plan and spawned assignments share the same budget
- [ ] Dry-run check: `play.submit` with `dry_run: true` plans without executing (call
      `help=true` first to confirm the argument name for your published server version)
- [ ] Help check (lionagi checkout only): `li play <name> --help` always lists fields from
      `args:`; `argument-hint` controls the usage line and can define fallback flags when no
      `args:` block exists. Over MCP, qualified playbook help resolves the custom arguments

---

## Companion Files

| File | Contents |
|---|---|
| `field-reference.md` | Complete field table, args schema, template interpolation rules, team semantics, reserved names, pitfalls, source code reference |
| `examples.md` | Three annotated example playbooks (minimal, audit, pr-review) plus the examples directory index |
| `patterns.md` | Orchestration patterns and advanced composition strategies |
