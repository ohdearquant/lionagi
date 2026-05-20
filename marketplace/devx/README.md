# devx

Developer-experience skill bundle for lionagi — conventional commits, CI, formatting, PRs, mid-session summarize, and project health. Note: `session-start` and `session-summarize` are not shipped in this bundle; see `skills/TODO.md`.

## Skills

| Skill | Description |
|-------|-------------|
| `/ci` | Run local CI pipeline across formatting, linting, tests, and build before pushing. |
| `/fmt` | Format multi-stack projects by auto-detecting Rust, Python, Markdown, and TypeScript tooling. |
| `/commit` | Run a conventional commit workflow with pre-commit checks, staging review, and optional push. |
| `/pr` | Create GitHub PRs with branch push, conventional titles, and PR metadata setup. |
| `/summarize` | Capture mid-session context, decisions, patterns, and next steps without ending the session. |
| `/wake-up` | Run the lambda wake-up heartbeat: check inbox, forum, tasks, health gates, and progress work. |
| `/init` | Bootstrap a development environment by detecting stacks, installing dependencies, and setting up hooks. |
| `/status` | Show sub-lambda dashboard state: task queues, build health, blockers, idle state, and delegation readiness. |

## Agents

| Agent | Description |
|-------|-------------|
| `reviewer` | Review artifacts against standards, verify completeness, and produce professional quality-gate verdicts. |

## Install

Add this plugin via the Claude Code plugin marketplace or copy the `.claude-plugin/plugin.json` into your project's `.claude-plugin/` directory.

```bash
# From a lionagi checkout
cp -r marketplace/devx/.claude-plugin /your-project/.claude-plugin
cp -r marketplace/devx/skills /your-project/.claude/skills
cp -r marketplace/devx/agents /your-project/.claude/agents
```

## Quick Start

Format all code in the current project:

```
/fmt
```

Stage changes, run pre-commit checks, and commit with a conventional message:

```
/commit
```

Run full CI before pushing:

```
/ci
```
