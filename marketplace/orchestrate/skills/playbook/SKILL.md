---
name: playbook
description: >
  Author lionagi playbooks — reusable YAML workflow templates that define parametric
  agent tasks. Playbooks live at ~/.lionagi/playbooks/ and run via li play <name>.
  Use when: creating reusable workflows, parameterizing agent tasks, or setting up
  repeatable pipelines.
allowed-tools: [Bash, Read, Write, Glob, Grep]
---

# Authoring Lionagi Playbooks

A playbook is a `.playbook.yaml` file that defines a reusable, parametric agent
workflow. Install in `~/.lionagi/playbooks/` and invoke with
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

Run: `li play hello "what is a monad?"`

The positional text is appended to the prompt with a blank line because the
template contains no `{input}` placeholder. That is the only behaviour difference
from a template that declares `{input}` explicitly.

---

## Authoring Checklist

- [ ] Filename: `<name>.playbook.yaml`
- [ ] Location: `~/.lionagi/playbooks/`
- [ ] `name:` matches the filename stem exactly
- [ ] `description:` is one clear sentence
- [ ] Either `model:` or `agent:` is set (both is allowed; `agent` provides the profile, `model` overrides the model)
- [ ] `prompt:` references only declared `args` keys and optional `{input}`
- [ ] Every `args` entry has `type`, `default`, and `help`
- [ ] No dashed keys inside `args:`
- [ ] `team_mode` and `team_attach` are not both set
- [ ] `workers` is 1–32 if set; `max_ops` is 0–50 if set
- [ ] Dry-run check: `li o flow -p <name> --dry-run "test prompt"` plans without executing
- [ ] Help check: `li play <name> --help` lists your custom flags (if `argument-hint` is set)

---

## Companion Files

| File | Contents |
|---|---|
| `field-reference.md` | Complete field table, args schema, template interpolation rules, team semantics, reserved names, pitfalls, source code reference |
| `examples.md` | Three annotated example playbooks (minimal, audit, pr-review) plus the examples directory index |
| `patterns.md` | Orchestration patterns and advanced composition strategies |
