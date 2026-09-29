"""`lion chat`'s defaults from `~/.lion/config.toml` and `<dir>/.lion/config.toml`; `lion config`."""

from __future__ import annotations

import argparse
import json
import os
import tomllib
from pathlib import Path

__all__ = ("CHAT", "chat_config", "config_main", "instructions")

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
    "khive_config": str,
    "raw": bool,
    "verbose": bool,
}

_SAYS = {
    str: "a string",
    int: "an integer",
    bool: "true or false",
}


def _value(path: Path, key: str, v: object) -> object:
    want = CHAT.get(key)
    if want is None:  # a typo must not pass for a setting that silently does nothing
        raise SystemExit(f"{path}: [chat] has no key {key!r}; the keys are {', '.join(CHAT)}")
    if isinstance(want, tuple):
        if v in want:
            return v
    elif type(v) is want:  # exact: a TOML true is not the integer 1
        if key == "khive_config" and not Path(v).expanduser().is_absolute():
            return os.path.normpath(path.parent.parent / v)  # beside the directory the file's .lion is in
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


def instructions(directory: Path) -> list[tuple[Path, str]]:
    """The person's instruction files a chat in `directory` composes into its model's guidance, in
    this order, those that exist and hold text: `~/.lion/CLAUDE.md` for every chat, the directory's
    own `AGENTS.md` and `CLAUDE.md`, then its `.lion/CLAUDE.md`. A file reached twice (the home is the
    directory) counts once."""
    directory = Path(directory).resolve()
    paths = (
        Path.home() / ".lion" / "CLAUDE.md",
        directory / "AGENTS.md",
        directory / "CLAUDE.md",
        directory / ".lion" / "CLAUDE.md",
    )
    out: list[tuple[Path, str]] = []
    for path in dict.fromkeys(p.resolve() for p in paths):
        if not path.is_file():
            continue
        try:
            text = path.read_text(errors="replace").strip()
        except OSError as e:  # a file there the chat cannot read: said by name, not skipped
            raise SystemExit(f"{path}: {e}") from None
        if text:
            out.append((path, text))
    return out


def config_main(a: argparse.Namespace, built_in: argparse.Namespace) -> None:
    """Print what a bare `lion chat --dir <dir>` runs with, one TOML line per key, and where each
    value came from, then the instruction files it composes."""
    found = chat_config(Path(a.dir))
    print("[chat]")
    for key in CHAT:
        value, source = found.get(key, (getattr(built_in, key), "built-in"))
        if value is None:  # TOML has no null: the flag's own default decides at run time
            print(f"# {key}: unset  # {source}")
        else:
            print(f"{key} = {json.dumps(value)}  # {source}")
    composed = instructions(Path(a.dir))
    for path, text in composed:
        print(f"# instructions: {path} ({len(text)} chars)")
    if not composed:
        print("# instructions: none; ~/.lion/CLAUDE.md, AGENTS.md, CLAUDE.md, .lion/CLAUDE.md are read")
