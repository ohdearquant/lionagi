# Get Started

Choose the path that matches how you want to run models. Both begin with the
same package and take you to a verified first result.

## CLI track

Use this track when you want to orchestrate coding-agent CLIs such as Codex or
Claude Code from the terminal.

1. [Install LionAGI and authenticate a CLI provider](install.md).
2. Run `li doctor`.
3. [Run one agent, continue it, then preview orchestration](first-flow.md).

Your first success is a non-empty agent response plus the `[to resume]` hint
printed by `li agent`.

## Python track

Use this track when LionAGI is a dependency in your application and you want
API-backed models, typed output, or programmatic tool orchestration.

1. [Install LionAGI in a project](install.md#python-project-installation).
2. Export one provider API key.
3. [Record a chat turn and request typed output](python.md).

Your first success is a recorded `Branch.communicate()` response followed by a
validated Pydantic model from `Branch.operate()`.

## Not sure which path you need?

Use the [surface chooser](../choosing-a-surface.md). It selects the lightest
surface from the shape of the task, without requiring you to learn the full
architecture first.
