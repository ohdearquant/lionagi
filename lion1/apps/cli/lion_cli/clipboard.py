"""The system clipboard from a terminal: the platform's own tool when one is there, else OSC 52."""

from __future__ import annotations

import base64
import shutil
import subprocess
import sys

__all__ = ("to_clipboard",)

# each takes the new clipboard text on stdin; the first present one that succeeds wins
_TOOLS = (("pbcopy",), ("wl-copy",), ("xclip", "-selection", "clipboard"))


def to_clipboard(text: str) -> str:
    """Put `text` on the clipboard; returns the route it took: the tool's name, or `osc52`."""
    for argv in _TOOLS:
        if shutil.which(argv[0]) is None:
            continue
        try:
            # xclip and wl-copy fork a child that keeps serving the selection: a captured stdout would
            # wait on that child forever, so nothing is captured
            done = subprocess.run(
                argv,
                input=text.encode(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0:  # xclip with no display, wl-copy outside Wayland: the next route
            return argv[0]
    # no tool (a remote shell, a container): the terminal sets its clipboard on this escape
    # (iTerm2, Ghostty, kitty, and tmux with `set-clipboard on`)
    sys.stdout.write(f"\x1b]52;c;{base64.b64encode(text.encode()).decode()}\x07")
    sys.stdout.flush()
    return "osc52"
