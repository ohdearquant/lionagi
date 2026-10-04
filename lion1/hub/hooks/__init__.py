"""Hooks: what runs before a command, after it, and each turn, beside the actor's own loop.

`shell` reads `hooks.toml` and runs shell lines with JSON on stdin (exit code = verdict); `gates`
are the same rules as Python callables for an app that composes its actor in code; `scripts/` holds
the shell hooks kept here, run as `python -m hub.hooks.scripts.<name>`; `hooks.toml` here is
the reference file to copy into a directory's `.lion/`.
"""

from .gates import SECRET_WORDS, no_secrets
from .shell import EVENTS, REFUSE, Hook, install, load, run_hook

__all__ = (
    "SECRET_WORDS",
    "no_secrets",
    "EVENTS",
    "REFUSE",
    "Hook",
    "install",
    "load",
    "run_hook",
)
