"""lionbench sandbox image — mirrors ``lionagi.tools.daytona.lionagi_image`` but
also provisions the external-harness CLIs (Claude Code, codex) the harness
adapters need, plus Node (required for the npm-installed CLIs).
"""

from __future__ import annotations

SNAPSHOT_NAME = "lionbench-v0"


def lionbench_image(
    *,
    python: str = "3.12",
    pip_spec: str = "lionagi==0.26.14",
    extra_pip: tuple[str, ...] = ("pytest", "uv"),
    apt: tuple[str, ...] = ("git", "curl"),
    node_major: str = "20",
    npm_packages: tuple[str, ...] = ("@anthropic-ai/claude-code", "@openai/codex"),
):
    """Declarative ``daytona.Image``: git + lionagi's dependency tree + Node +
    the external CLI harnesses, so ``ClaudeCodeAdapter``/``CodexAdapter`` have
    something to exec once the sandbox is up.

    ``uv`` is in ``extra_pip`` (not just ``pytest``) because harvested oracle
    commands are ``uv run --all-extras pytest ...`` (see harvest.py's
    ``default_oracle_command`` — extras like studio's fastapi aren't in the
    base dependency tree, confirmed live against PR #1643); the swebench
    suite's own snapshot never needed `uv` since its runner drives tests via
    bare `pip install -e .`, but lionbench's harvested oracle commands do."""
    from lionagi.tools.daytona import _require_daytona

    _require_daytona()
    from daytona import Image

    img = Image.debian_slim(python)
    if apt:
        img = img.run_commands(f"apt-get update && apt-get install -y {' '.join(apt)}")
    img = img.run_commands(
        f"curl -fsSL https://deb.nodesource.com/setup_{node_major}.x | bash - "
        "&& apt-get install -y nodejs"
    )
    img = img.pip_install(pip_spec, *extra_pip)
    if npm_packages:
        img = img.run_commands(f"npm install -g {' '.join(npm_packages)}")
    return img
