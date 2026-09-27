"""A before hook: refuse a read of anything that looks like a secret; let the rest through.
stdin: {"event": "before", "command": "read", "args": {"path": ...}, ...}; exit 2 refuses, stdout is why.
Run as `python -m hub.hooks.scripts.no_secrets` from a `hooks.toml`."""

import json
import subprocess
import sys

__all__ = ("WORDS", "tracked", "main")

WORDS = (".env", "secret", "credential", "id_rsa", ".pem")


def tracked(path: str) -> bool:
    """A file git tracks from the current directory, named exactly: source, not a secret (a secret in
    git is a leak already, not something a read gate can close). `hub/hooks/scripts/no_secrets.py`
    reads. A glob or a directory is not one tracked file: a tracked `public.pem` says nothing of the
    untracked `private.pem` that `*.pem` also reaches, so either is refused."""
    done = subprocess.run(["git", "ls-files", "-z", "--", f":(literal){path}"], capture_output=True)
    return done.returncode == 0 and done.stdout == path.encode() + b"\0"


def main() -> int:
    p = json.load(sys.stdin)
    args = p.get("args", {})
    for key in ("path", "include"):  # a read's path, a search's path or file glob
        name = str(args.get(key) or "")
        if any(w in name.lower() for w in WORDS) and not tracked(name):
            print(f"{name} looks like a secret; the console does not read those")
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
