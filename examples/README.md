# lionagi CLI examples

Three reference primitives for the `li` CLI:

| Primitive | Location | Invocation | Purpose |
|-----------|----------|------------|---------|
| **Agent** | `~/.lionagi/agents/<name>/<name>.md` | `li agent -a <name> "..."` | Reusable agent profile — system prompt + default model/effort/yolo |
| **Skill** | `~/.lionagi/skills/<name>/SKILL.md` | `li skill <name>` | Static reference content — agent shells out to fetch on demand |
| **Playbook** | `~/.lionagi/playbooks/<name>.playbook.yaml` | `li play <name> "..."` | Parametric, runnable flow spec — typed args + prompt template |

**Rule of thumb:**

- **Agent** = *who* is running (persistent profile).
- **Skill** = *what they know* (reference docs, fetched by the agent mid-run).
- **Playbook** = *what to run* (declarative flow spec invoked by the user).

---

## Quick shape reference

### Agent profile — directory layout (preferred)

```
~/.lionagi/agents/orchestrator/
    orchestrator.md          # main profile (frontmatter + system prompt)
    patterns/                # optional: supplementary refs the agent reads on demand
        empaco.md
    refs/
        commit-conventions.md
```

The flat form `~/.lionagi/agents/orchestrator.md` still resolves for backward
compatibility. Directory layout wins when both exist.

See [`agents/`](./agents/) for minimal and extended examples.

### Skill — CC-compatible

```
~/.lionagi/skills/hello/SKILL.md
```

```markdown
---
name: hello
description: Greet the user
---

# Greeting skill

Always start with a warm greeting...
```

`li skill hello` prints the body (content after frontmatter) to stdout. An
orchestrator agent can shell out to `li skill <name>` and inject the result
into its context — no extra protocol needed.

See [`skills/`](./skills/) for minimal and structured examples.

### Playbook — args schema + template interpolation

```yaml
# ~/.lionagi/playbooks/audit.playbook.yaml
name: audit
description: Run parallel audit across a target
argument-hint: '[--mode MODE] [--worker-count N]'

model: claude-code/opus-4-7
agent: orchestrator

args:
  mode:
    type: str
    default: dry
    help: "audit mode: dry | security | dead-code"
  worker_count:
    type: int
    default: 8

prompt: |
  Run a {mode} audit with {worker_count} parallel workers.

  Target: {input}
```

```bash
li play audit --mode security --worker-count 12 "the auth service"
# → model=claude-code/opus-4-7, agent=orchestrator
# → prompt = "Run a security audit with 12 parallel workers.\n\nTarget: the auth service"
```

Template rules:

1. `{input}` resolves to the positional prompt text.
2. `{any_arg}` resolves to a declared arg (CLI override > playbook default).
3. If the template has **no** `{...}` placeholders, the positional text is **appended** with a blank line — mirroring CC slash commands.

See [`playbooks/`](./playbooks/) for minimal, parametric, and template examples.

---

## When to reach for which

| You want to... | Use |
|----------------|-----|
| Reuse a system prompt across many invocations | **Agent** |
| Give an agent mid-run access to a procedure/recipe/convention | **Skill** |
| Let a user (or another agent) trigger a structured, parameterized flow | **Playbook** |
| Write a big orchestration pattern that the orchestrator decides to apply | **Skill**, referenced from the orchestrator agent's system prompt |
| Expose a command the user types directly | **Playbook** |
