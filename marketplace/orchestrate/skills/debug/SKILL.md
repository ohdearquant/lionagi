---
name: debug
description: >
  Systematic debug workflow: research → orchestrate agents → escalate. Suggest when:
  stuck after 2-3 attempts, unfamiliar tooling, tempted to "try random things",
  or errors don't match documentation.
allowed-tools: [Bash, Read, Grep, Glob, WebSearch, WebFetch]
---

# Debug

Systematic approach to difficult debugging. Research → Orchestrate → Escalate.

## Activation Triggers

- Stuck on error after 2-3 failed fix attempts
- Unfamiliar library or dependency version
- Tempted to "try things randomly"
- Complex multi-system interactions
- Error messages that don't match documentation

## Anti-Patterns

- **Random flailing**: Trying fixes without understanding the problem
- **Editing generated files**: Modifying auto-generated output instead of fixing the source
- **Shallow fixes**: Adding workarounds without understanding root cause
- **Silent struggling**: Not asking for help when clearly stuck
- **Cheating through**: Disabling features or tests instead of fixing them

## Workflow Summary

| Phase | Action | Gate |
|-------|--------|------|
| **1. Research** | Check prior runs, search codebase, spawn researcher agent | 2-3 focused queries before any fix attempt |
| **2. Orchestrate** | Parallel diagnostic agents via `li o fanout` or focused analyst | Agent must produce actionable insight |
| **3. Escalate** | Generate consultation request with full evidence | Must demonstrate exhaustive research first |
| **4. Document** | Write fix to `./notes/debug-log.md` | Only after resolution |

See [research-protocol.md](research-protocol.md) for detailed methodology, agent selection
table, phase-by-phase commands, and escalation template.

## Key Principles

- **Research before action**: Never try fixes without understanding the problem
- **Be specific**: Vague queries yield vague answers
- **Document attempts**: Track what was tried and what happened
- **Ask early**: Better to ask for help than waste time flailing
- **Store the fix**: Future sessions will thank present you

## Relevant Source Files

- `lionagi/cli/agent.py` — `li agent` one-shot and resumed turn entry point
- `lionagi/cli/orchestrate/fanout.py` — `li o fanout` parallel workers
- `lionagi/cli/_runs.py` — run persistence at `~/.lionagi/runs/`
