"""`lion account {add,list,remove}` (ADR-0024/D8): the pool in `accounts.toml`, a Claude Code account's
token in its keychain item, and what `evidence.json` says of each account. The file is rewritten in
place, every other byte kept, and read back before it is written. A refusal exits non-zero with the
reason on stderr; no output names a credential, or where one is kept beyond the word `keychain`,
`default` or `home`. `add` and `remove` hold a lock file beside the pool from their read to their
write, so two of them on one pool run one after the other."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import getpass
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from hub.accounts import (
    _ACCOUNT_HEADER,
    _LABEL,
    LOCK_WAIT,
    WINDOWS,
    Account,
    AccountsError,
    Kept,
    KeychainUnknown,
    _deletes,
    _label,
    fresh,
    load_pool,
    parse_pool,
    read_evidence,
    readings,
    remove_token,
    store_token,
)

__all__ = ("add", "remove", "rows", "account_main")

_HEADER = re.compile(r"\s*\[")  # a table's header, `[t]` or `[[t]]`


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode()  # bytes, so a CRLF file keeps its line endings
    except FileNotFoundError:
        return ""
    except UnicodeDecodeError as e:
        raise AccountsError(f"{path} is not UTF-8, so nothing was written: {e}") from None


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(text.encode())
    try:
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def _locked(path: Path, wait: float = LOCK_WAIT) -> Iterator[None]:
    """An exclusive `flock` on `.accounts.toml.lock` beside the pool, held from a command's read to its
    write. The lock file is never replaced, so a waiter that takes it holds the one every writer takes;
    one that waits past `wait` seconds is refused before it reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_name(f".{path.name}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait
        first = True
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                if first or time.monotonic() <= deadline:
                    break
            first = False
            # taken late counts as not taken: the command reads nothing past its bound
            if time.monotonic() > deadline:
                raise AccountsError(
                    f"{path} is being changed by another command past {wait:g} seconds; nothing was written"
                ) from None
            time.sleep(0.01)
        yield
    finally:
        os.close(fd)


def add(
    path: Path, name: str, provider: str, credential: str, group: str | None, secret: Callable[[], str]
) -> Account:
    """Declare an account at the end of the pool's file and, for `keychain`, write the token `secret`
    returns into its item, under the pool's lock. Every refusal but the token's own comes before the token
    is asked for; a file write that fails takes the item back."""
    with _locked(path):
        return _add(path, name, provider, credential, group, secret)


def _add(
    path: Path, name: str, provider: str, credential: str, group: str | None, secret: Callable[[], str]
) -> Account:
    text = _read(path)
    before = parse_pool(text, path)
    if any(a.name == name for a in before.accounts):
        raise AccountsError(f"account {name!r} is already in {path}; nothing was written")
    fields = {"name": name, "provider": provider, "credential": credential, "group": group}
    entry = "[[account]]\n" + "".join(
        f"{k} = {json.dumps(v, ensure_ascii=False)}\n" for k, v in fields.items() if v is not None
    )
    (got,) = parse_pool(entry, path).accounts  # the entry alone, by every rule of the pool
    new_text = text + ("\n" if text and not text.endswith("\n") else "") + ("\n" if text else "") + entry
    # the write is textual: read it back and require that the one account, and nothing else, was added,
    # so a file that declares its accounts some other way (an inline array) is refused, never broken
    try:
        after = parse_pool(new_text, path)
    except AccountsError:
        after = None
    if after is None or after.accounts != (*before.accounts, got) or after.settings != before.settings:
        raise AccountsError(f"{path}: the account could not be added in place, so nothing was written")
    if got.credential == "keychain":
        store_token(got.name, secret())
    try:
        _write(path, new_text)
    except BaseException as e:
        if got.credential == "keychain":
            try:
                remove_token(got.name)  # the item goes with the entry it was written for
            except AccountsError as left:
                e.add_note(f"{left}; {_deletes(got.name)}")
        raise
    return got


def remove(path: Path, name: str) -> tuple[Account, bool]:
    """Delete an account's `[[account]]` table and, for `keychain`, its item, under the pool's lock; a
    Codex home is its person's and stays. Comments right above a table are its own and go with it; those
    below its last key are the next table's and stay. The file is written first and the item deleted
    after, and a delete that fails puts the table back. (the account, whether a keychain item was
    deleted)."""
    if not _label(name):  # never shown: a token pasted as the name must not come back
        raise AccountsError(f"an account's name is {_LABEL}; nothing was changed")
    with _locked(path):
        return _remove(path, name)


def _remove(path: Path, name: str) -> tuple[Account, bool]:
    text = _read(path)
    before = parse_pool(text, path)
    target = next((a for a in before.accounts if a.name == name), None)
    if target is None:
        raise AccountsError(f"no account {name!r} in {path}")
    lines = text.splitlines(keepends=True)
    heads = [i for i, line in enumerate(lines) if _HEADER.match(line)] + [len(lines)]
    new_text = None
    for start, stop in zip(heads, heads[1:], strict=False):
        if (
            _ACCOUNT_HEADER.match(lines[start])
            and parse_pool("".join(lines[start:stop]), path).accounts[0] == target
        ):
            first, end = start, stop
            while first > 0 and lines[first - 1].lstrip().startswith("#"):
                first -= 1
            while not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#"):
                end -= 1
            while end < stop and not lines[end].strip():
                end += 1
            new_text = "".join(lines[:first] + lines[end:])
            break
    after = None if new_text is None else parse_pool(new_text, path)
    rest = tuple(a for a in before.accounts if a != target)
    if after is None or after.accounts != rest or after.settings != before.settings:
        raise AccountsError(f"{path}: account {name!r} could not be removed in place, so nothing was changed")
    _write(path, new_text)
    if target.credential != "keychain":
        return target, False
    try:
        return target, remove_token(name)
    except KeychainUnknown as e:  # it may be gone: putting the table back could leave an entry with no item
        raise AccountsError(
            f"{e}; its table is removed, and if the item is still there {_deletes(name)}"
        ) from None
    except AccountsError as e:
        try:
            _write(path, text)
        except BaseException as back:
            raise AccountsError(
                f"{e}, and {path} could not be put back ({type(back).__name__}), so the item is left with no "
                f"entry; {_deletes(name)}"
            ) from None
        raise AccountsError(f"{e}; its table was put back, so nothing was changed") from None


def rows(path: Path, now: float | None = None) -> list[dict]:
    """Each account as `list` shows it: name, provider, the word for where its credential is kept, plan
    group, each window's reading as its group holds it with its age and source, and its mark. A window
    with no reading, or one past its reset, is None: unknown, never a number (ADR-0024/C3)."""
    now = time.time() if now is None else now
    pool = load_pool(path)
    kept = read_evidence(pool.evidence)
    out = []
    for a in pool.accounts:
        got = readings(kept, pool, a)
        windows = {}
        for w in WINDOWS:
            r = got.get(w)
            past = r is not None and r.resets is not None and now >= r.resets
            windows[w] = (
                None
                if r is None or past
                else {
                    "used": r.used,
                    "age": round(now - r.observed),
                    "source": r.source,
                    "resets": r.resets,
                    "fresh": fresh(r, now, pool.settings.max_age),
                }
            )
        mark = kept.get((a.provider, a.name), Kept()).mark
        out.append(
            {
                "name": a.name,
                "provider": a.provider,
                "credential": a.kept,
                "group": a.group,
                "windows": windows,
                "mark": None if mark is None else asdict(mark),
            }
        )
    return out


def _age(seconds: float) -> str:
    s = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{s // size}{unit}"
    return f"{s}s"


def _line(row: dict) -> str:
    parts = [f"{row['name']}: {row['provider']}, {row['credential']}"]
    if row["group"]:
        parts[0] += f", group {row['group']}"
    for w, r in row["windows"].items():
        if r is None:
            parts.append(f"{w} unknown")
        else:
            stale = "" if r["fresh"] else ", older than max_age"
            parts.append(f"{w} {r['used']:.2f} ({_age(r['age'])} old, from a {r['source']}{stale})")
    mark = row["mark"]
    if mark is None:
        parts.append("no mark")
    elif mark["kind"] == "person":
        parts.append("marked: needs its person")
    elif mark["until"] is None:
        parts.append("marked: out of quota until a probe succeeds")
    else:
        until = datetime.fromtimestamp(mark["until"]).astimezone().strftime("%Y-%m-%d %H:%M %Z")
        parts.append(f"marked: out of quota until {until}")
    return "; ".join(parts)


def _secret() -> str:
    """The token: piped in on standard input, else typed at the terminal without echo."""
    if sys.stdin.isatty():
        return getpass.getpass("the account's token (not shown): ").strip()
    return sys.stdin.read().strip()


def account_main(a: argparse.Namespace) -> None:
    path = Path(a.file).expanduser()
    try:
        if a.verb == "add":
            got = add(path, a.name, a.provider, a.credential, a.group, _secret)
            group = f", group {got.group}" if got.group else ""
            print(f"added {got.name} to {path}: {got.provider}, {got.kept}{group}")
        elif a.verb == "list":
            listed = rows(path)
            if a.json:
                print(json.dumps(listed, indent=1))
            else:
                print("\n".join(_line(r) for r in listed) or f"no accounts: {path} declares none")
        else:
            gone, had = remove(path, a.name)
            if gone.kept == "keychain":
                said = "and its keychain item" if had else "which had no keychain item"
            elif gone.kept == "home":
                said = "its home is left to its person"
            else:
                said = "on the CLI's own sign-in"
            print(f"removed {gone.name} from {path}, {said}")
    except AccountsError as e:
        print(f"lion account {a.verb}: {e}", file=sys.stderr)
        raise SystemExit(1) from None
