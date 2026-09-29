"""`lion chat`'s defaults from `~/.lion/config.toml` and `<dir>/.lion/config.toml`; `lion config`."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

__all__ = ("CHAT", "chat_config", "config_main")

# The `[chat]` keys: `lion chat`'s long flags with dashes as underscores, and what each takes. The
# directory, the first message, the log and the chat to continue name one run, so no file sets them.
CHAT: dict[str, type | tuple[str, ...]] = {
    "model": str,
    "rounds": int,
    "shell": bool,
    "offline": bool,
    "box": ("vm", "daytona"),
    "image": str,
    "apply": bool,
    "code": bool,
    "tools": list,
    "max_turns": int,
    "raw": bool,
    "verbose": bool,
}

_SAYS = {
    str: "a string",
    int: "an integer",
    bool: "true or false",
    list: "a list of names or one comma-separated string",
}


def _value(path: Path, key: str, v: object) -> object:
    want = CHAT.get(key)
    if want is None:  # a typo must not pass for a setting that silently does nothing
        raise SystemExit(f"{path}: [chat] has no key {key!r}; the keys are {', '.join(CHAT)}")
    if want is list:  # the flag's own form is one comma-separated string, and that is what chat reads
        if isinstance(v, str):
            return v
        if isinstance(v, list) and all(isinstance(t, str) for t in v):
            return ",".join(v)
    elif isinstance(want, tuple):
        if v in want:
            return v
    elif type(v) is want:  # exact: a TOML true is not the integer 1
        return v
    says = f"one of {', '.join(want)}" if isinstance(want, tuple) else _SAYS[want]
    raise SystemExit(f"{path}: [chat] {key} = {v!r} is not {says}")


def _read(path: Path) -> dict[str, object]:
    try:
        doc = tomllib.loads(path.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise SystemExit(f"{path}: {e}") from None
    if extra := sorted(set(doc) - {"chat"}):
        raise SystemExit(f"{path}: no table {extra[0]!r}; the file holds [chat]")
    table = doc.get("chat", {})
    if not isinstance(table, dict):
        raise SystemExit(f"{path}: chat is a table, [chat]")
    return {k: _value(path, k, v) for k, v in table.items()}


def chat_config(directory: Path) -> dict[str, tuple[object, Path]]:
    """Each `[chat]` key the files set, with the file it came from: the home file, then the
    directory's over it."""
    found: dict[str, tuple[object, Path]] = {}
    here = Path(directory).resolve() / ".lion" / "config.toml"
    for path in dict.fromkeys(((Path.home() / ".lion" / "config.toml").resolve(), here)):
        if path.exists():
            found.update({k: (v, path) for k, v in _read(path).items()})
    return found


def config_main(a: argparse.Namespace, built_in: argparse.Namespace) -> None:
    """Print what a bare `lion chat --dir <dir>` runs with, one TOML line per key, and where each
    value came from."""
    found = chat_config(Path(a.dir))
    print("[chat]")
    for key in CHAT:
        value, source = found.get(key, (getattr(built_in, key), "built-in"))
        if value is None:  # TOML has no null: the flag's own default decides at run time
            print(f"# {key}: unset  # {source}")
        else:
            print(f"{key} = {json.dumps(value)}  # {source}")
