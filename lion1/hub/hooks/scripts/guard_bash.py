"""A before hook for `bash`: refuse the lines a console should never run on a person's tree, and let
the rest through. stdin: {"event": "before", "command": "bash", "args": {"command": ...}, ...};
exit 2 refuses, stdout is why. Run as `python -m hub.hooks.scripts.guard_bash` from a `hooks.toml`."""

import json
import re
import sys

__all__ = ("REFUSED", "main")

_FLAGS = r"(?:-[A-Za-z]+\s+)*"  # options before or after the recursive one
_ROOTS = r"(?:\.{1,2}/?|\./\*|\*|/|~/?|\$\{?PWD\}?/?)(?=\s|$|;|&|\|)"  # the tree, its parent, or above
_WORD = r"""(?:[^\s;&|'"]|"[^"]*"|'[^']*')+"""  # one shell word, quoted parts included
# git's global options this guard reads before the subcommand
_GLOBAL = rf"-[Cc]\s+{_WORD}|--(?:git-dir|work-tree)(?:=|\s+){_WORD}|--(?=\s)"
_GIT = rf"\bgit(?:\s+(?:{_GLOBAL}))*\s+"
_DISCARD = (
    r"(reset\s+--hard|checkout\s+--|checkout\s+\.(?=\s|$)|restore\s+(--\s+)?\.(?=\s|$)|clean\s+-[a-z]*f)"
)
REFUSED = (
    (rf"{_GIT}push\b", "pushing is the person's act"),
    (rf"{_GIT}{_DISCARD}", "that discards the person's work"),
    (  # best effort, not a shell parser: any other option leaves the subcommand unknown
        rf"{_GIT}(?!{_GLOBAL}|--(?:git-dir|work-tree)\b)-[^\s;&|]*[^;&|\n]*?\s(push\b|{_DISCARD})",
        "a git option this guard does not read, before a subcommand it refuses",
    ),
    (
        rf"\brm\s+{_FLAGS}-[A-Za-z]*[rR][A-Za-z]*\s+{_FLAGS}{_ROOTS}",
        "a recursive delete of the tree or above it",
    ),
    (r"\bsudo\b", "no privilege in the sandbox"),
    (r"\b(curl|wget)\b.*\|\s*(sh|bash|zsh|python[0-9.]*)\b", "piping the network into a shell"),
    (r"\b(sh|bash|zsh|source)\s+<\(\s*(curl|wget)\b", "piping the network into a shell"),
)


def main() -> int:
    p = json.load(sys.stdin)
    line = str(p.get("args", {}).get("command", ""))
    for pattern, why in REFUSED:
        if re.search(pattern, line):
            print(f"refused by guard_bash: {why} ({pattern})")
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
