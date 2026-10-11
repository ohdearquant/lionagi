"""The accounts agents on Claude Code and Codex run on (ADR-0024): the pool `accounts.toml` declares
beside the areas registry, a Claude Code account's token in its keychain item, the environment a
child of an account starts with, `evidence.json` beside the pool, every account's limit readings,
last probe, mark and last use, read and written under one lock, the probe that takes a reading, and
the pick of one account for a wake."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .areas import DEFAULT_REGISTRY
from .backend.codex import _TAIL, _THREAD, INSTRUCTIONS, SCRATCH_AGENTS_MD, CodexError, CodexSession

__all__ = (
    "PROVIDERS",
    "WINDOWS",
    "SOURCES",
    "ENDINGS",
    "MARKS",
    "CREDENTIAL_VARIABLES",
    "DEFAULT_POOL",
    "LOCK_WAIT",
    "PROBE_WAIT",
    "AccountsError",
    "Hold",
    "KeychainUnknown",
    "Unknown",
    "UNKNOWN",
    "Account",
    "Settings",
    "Pool",
    "parse_pool",
    "load_pool",
    "token",
    "store_token",
    "remove_token",
    "child_env",
    "Window",
    "Probe",
    "Mark",
    "Kept",
    "Book",
    "held",
    "read_evidence",
    "record",
    "readings",
    "fresh",
    "usable",
    "claude_code_reading",
    "codex_reading",
    "Probed",
    "probe",
    "pick",
)

PROVIDERS = ("claude_code", "codex")
WINDOWS = ("five_hour", "seven_day")
SOURCES = ("call", "probe")
ENDINGS = ("figures", "no figure", "failed")  # how a probe ended
MARKS = ("quota", "person")  # out of quota, or needing its person
# every variable each CLI takes a credential from (ADR-0024/C2); a child keeps none of them but the one
# its account sets. `CC_ACCOUNT` is the earlier tool's name for an account, not a credential, and goes too
CREDENTIAL_VARIABLES = {
    "claude_code": ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CC_ACCOUNT"),
    "codex": ("CODEX_ACCESS_TOKEN",),
}
DEFAULT_POOL = DEFAULT_REGISTRY.parent / "accounts.toml"
LOCK_WAIT = 120.0  # seconds a pick waits for the evidence file's lock before it holds its wake
PROBE_WAIT = 60.0  # seconds a probe runs before it and its children are killed (ADR-0024/D4)

# absolute: under launchd PATH is /usr/bin:/bin:/usr/sbin:/sbin, and a test puts a script in its place
_SECURITY = "/usr/bin/security"
_SERVICE = "lion.account.claude_code"  # the keychain item's service; its account is the pool's name
_NAME = re.compile(r"[A-Za-z0-9._-]+")
_MODEL = re.compile(r"[A-Za-z0-9._:/@-]+")
# a label the pool keeps and shows (a name, a plan group, a probe model) never holds a Claude Code
# credential's stem `sk-ant` in any case, and a token is taken only in A5's form, `sk-ant-` and past 64
# characters, so no token, nor its start anywhere in a label, is kept as one
_LABEL_MAX = 64
_LABEL = "letters, digits, '.', '_' and '-', at most 64 of them, never holding 'sk-ant'"
_TOKEN = re.compile(r"sk-ant-[A-Za-z0-9._-]{58,2041}")
_TOKEN_FORM = "'sk-ant-' then letters, digits, '-', '_' and '.', 65 to 2048 characters in all"
_ACCOUNT_HEADER = re.compile(r"\s*\[\[\s*account\s*\]\]\s*(#.*)?$")  # one `[[account]]` line
_HEADER = re.compile(r"\s*\[")  # a table's header, `[t]` or `[[t]]`, as `lion account` cuts the file
_ACCOUNT_KEYS = ("name", "provider", "credential", "group")
_MINUTES = {300: "five_hour", 10080: "seven_day"}  # a Codex window is told by its length
# a probe's prompt, as measured: the one row it is made for comes before the answer either way
_PROBE_PROMPT = b"Reply with the single word: ok"


class AccountsError(Exception):
    pass


class Hold(AccountsError):
    """A pick that chose no account, so the wake is held (ADR-0024/C5): `out` names each candidate and
    why it is out. A lock not taken within its bound is one too, naming the lock and no candidate."""

    def __init__(self, message: str, out: Mapping[str, str] | None = None) -> None:
        super().__init__(message)
        self.out = dict(out or {})


class KeychainUnknown(AccountsError):
    """A keychain item whose delete failed and could not be read back: it may or may not be there."""


class Unknown:
    """A figure no source gave: neither true nor false, and never a number."""

    __slots__ = ()

    def __bool__(self) -> bool:
        raise TypeError("unknown is neither true nor false")

    def __repr__(self) -> str:
        return "UNKNOWN"


UNKNOWN = Unknown()


@dataclass(frozen=True)
class Account:
    name: str
    provider: str
    credential: str  # `keychain`, `default`, or a Codex home's absolute path
    group: str | None = None  # the plan group

    @property
    def kept(self) -> str:
        """Where the credential is kept, in the one word any output may say."""
        return self.credential if self.credential in ("keychain", "default") else "home"


@dataclass(frozen=True)
class Settings:
    max_age: float = 600.0
    keep: float = 3600.0  # the prompt cache's life
    seven_day: float = 0.97
    five_hour: float = 0.95
    # per provider; None is the CLI's own default model
    probe_model: dict[str, str | None] = field(
        default_factory=lambda: {"claude_code": "haiku", "codex": None}
    )


@dataclass(frozen=True)
class Pool:
    accounts: tuple[Account, ...]
    settings: Settings
    path: Path  # accounts.toml, which need not exist

    @property
    def evidence(self) -> Path:
        return self.path.with_name("evidence.json")


def _number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def _label(value: Any, form: re.Pattern[str] = _NAME) -> bool:
    """A value the pool keeps and shows as written: `form`'s characters, at most 64, and nowhere holding
    the start of a Claude Code credential (ADR-0024/C1)."""
    return (
        isinstance(value, str)
        and len(value) <= _LABEL_MAX
        and bool(form.fullmatch(value))
        and "sk-ant" not in value.lower()
    )


def _key(key: str) -> str:
    """A key a refusal names: as written when it is a label, else only that it is not one of them."""
    return repr(key) if _label(key) else "outside the rule"


def _settings(path: Path, t: Any) -> Settings:
    """`[pick]` against ADR-0024/D1. A refusal names the key and its rule, never the value."""
    if not isinstance(t, dict):
        raise AccountsError(f"{path}: pick is a table, [pick]")
    known = Settings()
    for key, v in t.items():
        if key in ("max_age", "keep"):
            if not (_number(v) and v > 0):
                raise AccountsError(f"{path}: [pick] {key} is not a number of seconds above zero")
        elif key in ("seven_day", "five_hour"):
            if not (_number(v) and 0 < v <= 1):
                raise AccountsError(f"{path}: [pick] {key} is not a fraction above 0 and at most 1")
        elif key == "probe_model":
            if not isinstance(v, dict) or not all(p in PROVIDERS and _label(m, _MODEL) for p, m in v.items()):
                raise AccountsError(
                    f"{path}: [pick] probe_model is a table of a model name per provider, "
                    f"{' and '.join(PROVIDERS)}; a model name is letters, digits and '.', '_', ':', '/', "
                    "'@' and '-', at most 64 of them, never holding 'sk-ant'"
                )
        else:
            raise AccountsError(
                f"{path}: [pick] has no key {_key(key)}; the keys are max_age, keep, seven_day, "
                "five_hour and probe_model"
            )
    models = {**known.probe_model, **t.get("probe_model", {})}
    values = {k: float(t[k]) for k in ("max_age", "keep", "seven_day", "five_hour") if k in t}
    return Settings(**values, probe_model=models)


def _account(where: str, row: Any, seen: set[str]) -> Account:
    """One `[[account]]` table checked against ADR-0024/D1. A refusal names the entry by its name once
    the name is a label, and the key and its rule, never a value: a token pasted where a reference or a
    label belongs must not come back in the message."""
    if not isinstance(row, dict):
        raise AccountsError(f"{where} is not a table")
    for key in row:
        if key not in _ACCOUNT_KEYS:
            raise AccountsError(
                f"{where}: no key {_key(key)}; an account takes name, provider, credential, group"
            )
    name = row.get("name")
    if not _label(name):
        raise AccountsError(f"{where}: its name is missing or not {_LABEL}")
    where = f"{where.rsplit(' ', 1)[0]} {name!r}"
    if name in seen:
        raise AccountsError(f"{where} is named twice")
    seen.add(name)
    provider = row.get("provider")
    if provider not in PROVIDERS:
        raise AccountsError(f"{where}: its provider is not {' or '.join(PROVIDERS)}")
    credential = row.get("credential")
    if not isinstance(credential, str):
        raise AccountsError(f"{where}: credential is missing or not text")
    if provider == "claude_code" and credential not in ("keychain", "default"):
        raise AccountsError(f"{where}: a Claude Code credential is keychain or default")
    if provider == "codex" and credential != "default":
        home = os.path.expanduser(credential)
        if credential == "keychain" or "\0" in home or not os.path.isabs(home):
            raise AccountsError(f"{where}: a Codex credential is default or a home directory's absolute path")
        credential = home
    group = row.get("group")
    if group is not None and not _label(group):
        raise AccountsError(f"{where}: its group is not {_LABEL}")
    return Account(name, provider, credential, group)


def parse_pool(text: str, path: Path) -> Pool:
    """The pool a file's text declares; every rule of ADR-0024/D1 holds or the file is refused."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise AccountsError(f"{path}: {e}") from None
    for key in doc:
        if key not in ("account", "pick"):
            raise AccountsError(f"{path}: no key {_key(key)}; the file holds [[account]] tables and [pick]")
    rows = doc.get("account", [])
    if not isinstance(rows, list):
        raise AccountsError(f"{path}: account is an array of tables, [[account]]")
    seen: set[str] = set()
    accounts = tuple(_account(f"{path}: account {i}", row, seen) for i, row in enumerate(rows, 1))
    # `lion account` edits the file by its header lines, every line starting with `[` (D8): an array written
    # inline, a table named any other way, or such a line inside a value would load here and then be cut
    # wrong by an edit, so the file is read again cut at those lines; the piece before the first has no
    # account, each piece under an `[[account]]` line is its one account, in order, and every other has none
    lines = text.splitlines(keepends=True)
    heads = [i for i, line in enumerate(lines) if _HEADER.match(line)]
    left = iter(rows)
    want = [[], *([next(left, None)] if _ACCOUNT_HEADER.match(lines[i]) else [] for i in heads)]
    cuts = [0, *heads, len(lines)]
    try:
        pieces = [
            tomllib.loads("".join(lines[a:b])).get("account", [])
            for a, b in zip(cuts, cuts[1:], strict=False)
        ]
    except tomllib.TOMLDecodeError:
        pieces = None
    # `account = []` cuts nowhere and loads as no account, so it passes the comparison: refused by name
    if pieces != want or ("account" in doc and not rows):
        raise AccountsError(
            f"{path}: each account is its own [[account]] table, its header on a line of its own, and no "
            "line inside a value starts with '['"
        )
    return Pool(accounts, _settings(path, doc.get("pick", {})), path)


def load_pool(path: Path | str = DEFAULT_POOL) -> Pool:
    """The pool; with no file, one account per provider named `default`, on the CLI's own sign-in."""
    path = Path(path).expanduser()
    try:
        text = path.read_bytes().decode()
    except FileNotFoundError:
        return Pool(tuple(Account("default", p, "default") for p in PROVIDERS), Settings(), path)
    except UnicodeDecodeError as e:
        raise AccountsError(f"{path} is not UTF-8: {e}") from None
    return parse_pool(text, path)


def _check_name(name: str) -> None:
    # the name rides a line the keychain tool splits on spaces: only the pool's labels pass
    if not _label(name):
        raise AccountsError(f"an account's name is {_LABEL}")


def _shaped(value: Any) -> bool:
    return isinstance(value, str) and bool(_TOKEN.fullmatch(value))


def _exited(out: subprocess.CompletedProcess) -> str:
    # the tool's own words are never relayed: what it prints may hold the token or a part of it (C1)
    return f"the keychain tool exited {out.returncode}"


def _deletes(name: str) -> str:
    return f"`security delete-generic-password -a {name} -s {_SERVICE}` deletes it"


def _left(name: str) -> str:
    return f"its keychain item may still hold the token; {_deletes(name)}"


def _security(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([_SECURITY, *args], input=stdin, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        raise AccountsError("the keychain tool did not answer in 10 seconds") from None
    except OSError as e:
        raise AccountsError(f"the keychain tool could not run: {e}") from None


def token(name: str) -> str:
    """The token in a Claude Code account's keychain item, read as the router's key is read
    (`api_key` in lion1/hub/backend/openrouter.py). The one function that returns a token: its caller hands
    it to `child_env` and nowhere else (ADR-0024/C1)."""
    _check_name(name)
    out = _security(["find-generic-password", "-a", name, "-s", _SERVICE, "-w"])
    value = out.stdout.strip()
    if out.returncode == 44:
        raise AccountsError(f"account {name!r} has no keychain item; `lion account add` writes it")
    if out.returncode:
        raise AccountsError(f"account {name!r}: {_exited(out)}")
    if not _shaped(value):
        raise AccountsError(f"account {name!r}: its keychain item does not hold a token of the expected form")
    return value


def store_token(name: str, value: str) -> None:
    """Write a Claude Code account's token into its keychain item, replacing one already there, and read
    it back. The token goes to the tool on its standard input, never in an argument; a refusal never
    repeats it, any part of it or its length. A write the tool failed is read back as well, since the
    tool may fail after writing: an item that holds the token then is deleted, or the refusal names the
    command that deletes it, so no refused write leaves the token behind."""
    _check_name(name)
    if not isinstance(value, str) or not value:
        raise AccountsError(f"account {name!r}: no token was given; nothing was written")
    if not _shaped(value):  # A5's form only: no label can be one, and nothing else rides the tool's line
        raise AccountsError(f"account {name!r}: a token is {_TOKEN_FORM}; nothing was written")
    try:
        out = _security(["-i"], f"add-generic-password -U -a {name} -s {_SERVICE} -w {value}\n")
        failed = _exited(out) if out.returncode else None
    except AccountsError as e:  # it did not answer, or could not run: what it wrote is read below
        failed = str(e)
    # what `-i` exits with when a command it read fails is not measured, so the item is read back
    try:
        back = _security(["find-generic-password", "-a", name, "-s", _SERVICE, "-w"])
        held = None if back.returncode not in (0, 44) else back.returncode == 0
        written = held is True and back.stdout.strip() == value
    except AccountsError:
        held, written = None, False
    if failed is None and written:
        return
    why = failed or "the keychain item did not read back as written"
    if written:
        try:
            remove_token(name)
            why += "; the item it wrote was deleted"
        except AccountsError:
            why += f"; {_left(name)}"
    elif held is None:
        why += f"; it could not be read back, so {_left(name)}"
    elif held:  # a part of this token, or an earlier value: which is not known, so it is not deleted
        why += f"; the item holds another value, which may be a part of the token; {_deletes(name)}"
    raise AccountsError(f"account {name!r}: {why}")


def remove_token(name: str) -> bool:
    """Delete a Claude Code account's keychain item; whether there was one. A delete the tool failed, or
    that did not answer, is read back, without the secret, since the tool may fail after deleting: an
    item still there was not deleted, one gone was, and one that cannot be read is `KeychainUnknown`."""
    _check_name(name)
    try:
        out = _security(["delete-generic-password", "-a", name, "-s", _SERVICE])
        if out.returncode in (0, 44):
            return out.returncode == 0
        failed = _exited(out)
    except AccountsError as e:
        failed = str(e)
    try:
        back = _security(["find-generic-password", "-a", name, "-s", _SERVICE])  # no -w: no secret
    except AccountsError:
        back = None
    if back is not None and back.returncode == 44:
        return True
    if back is not None and back.returncode == 0:
        raise AccountsError(f"account {name!r}: {failed}, so its keychain item was not deleted")
    raise KeychainUnknown(f"account {name!r}: {failed}, and whether its keychain item is gone is unknown")


def child_env(
    account: Account, environ: Mapping[str, str], token: str | None = None, home: str | None = None
) -> dict[str, str]:
    """The environment a child on `account` starts with (ADR-0024/C2): `environ` less every variable
    the account's CLI takes a credential from, then the chosen one. A Claude Code account with a token
    sets `CLAUDE_CODE_OAUTH_TOKEN`; a Codex child runs from the agent's private home `home`, whose login
    link must resolve to the account's (`default`'s is `environ`'s `CODEX_HOME`, else `~/.codex`),
    resolved without opening either file; `default` with no private home sets no credential. Nothing is
    started here."""
    env = {k: v for k, v in environ.items() if k not in CREDENTIAL_VARIABLES[account.provider]}
    if account.provider == "claude_code":
        if account.credential == "keychain":
            if not token:
                raise AccountsError(f"account {account.name!r} keeps a token in the keychain; none was given")
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        elif token is not None:
            raise AccountsError(f"account {account.name!r} keeps no token; one was given and is not used")
        return env
    if token is not None:
        raise AccountsError(f"account {account.name!r} is a Codex account and takes no token")
    if home is not None:
        if not os.path.isabs(home):  # a relative one names another home once the child's cwd moves
            raise AccountsError(f"account {account.name!r}: the private home is not an absolute path")
        own = account.credential
        if own == "default":
            own = environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
        if os.path.realpath(os.path.join(home, "auth.json")) != os.path.realpath(
            os.path.join(own, "auth.json")
        ):
            raise AccountsError(
                f"account {account.name!r}: the private home's login is not this account's; the pick points "
                "it at the account's before a child starts"
            )
        env["CODEX_HOME"] = home
    elif account.credential != "default":
        raise AccountsError(
            f"account {account.name!r} needs the agent's private home; without one only default runs"
        )
    return env


def _instant(v: Any) -> bool:
    return _number(v) and v > 0


@dataclass(frozen=True)
class Window:
    """One window's limit reading (ADR-0024/C3)."""

    # the fraction of the window's limit used; None: the window is absent, its plan has none (C3), which
    # is never a figure, and names no reset
    used: float | None
    resets: float | None  # the instant it resets, epoch seconds; None when the source names none
    observed: float  # epoch seconds
    source: str  # a call or a probe

    def __post_init__(self) -> None:
        if self.used is None:
            ok = self.resets is None
        else:
            ok = _number(self.used) and self.used >= 0 and (self.resets is None or _instant(self.resets))
        if not (ok and _instant(self.observed)):
            raise ValueError(f"not a limit reading: {self}")
        if self.source not in SOURCES:
            raise ValueError(f"a reading's source is {' or '.join(SOURCES)}, not {self.source!r}")


@dataclass(frozen=True)
class Probe:
    at: float
    ended: str  # figures, no figure, or failed

    def __post_init__(self) -> None:
        if not (_instant(self.at) and self.ended in ENDINGS):
            raise ValueError(f"not a probe: {self}")


@dataclass(frozen=True)
class Mark:
    kind: str  # quota or person
    at: float
    until: float | None = None  # a quota mark's end; None: until a later probe succeeds

    def __post_init__(self) -> None:
        until_ok = self.until is None or (self.kind == "quota" and _instant(self.until))
        if not (self.kind in MARKS and _instant(self.at) and until_ok):
            raise ValueError(f"not a mark: {self}")


@dataclass
class Kept:
    """What the evidence file holds for one account."""

    readings: dict[str, Window] = field(default_factory=dict)  # by window; a window absent is unknown
    probe: Probe | None = None
    mark: Mark | None = None
    used: float | None = None  # its last use, epoch seconds

    def __post_init__(self) -> None:
        if not (set(self.readings) <= set(WINDOWS) and (self.used is None or _instant(self.used))):
            raise ValueError(f"not an account's evidence: {self}")


def _load(data: bytes, path: Path) -> dict[tuple[str, str], Kept]:
    if not data.strip():
        return {}
    try:
        doc = json.loads(data)
        out = {}
        for provider, rows in doc.items():
            if provider not in PROVIDERS:
                raise ValueError(f"no provider {provider!r}")
            for name, row in rows.items():
                out[(provider, name)] = Kept(
                    readings={w: Window(**r) for w, r in row["readings"].items()},
                    probe=None if row["probe"] is None else Probe(**row["probe"]),
                    mark=None if row["mark"] is None else Mark(**row["mark"]),
                    used=row["used"],
                )
        return out
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        raise AccountsError(f"{path} is not an evidence file, so it was not read: {e}") from None


class Book:
    """The evidence file as one holder of its lock reads it: `kept` by (provider, name), and `save`."""

    def __init__(self, path: Path, fd: int) -> None:
        self.path, self._fd = path, fd
        with open(fd, "rb", closefd=False) as f:
            self.kept = _load(f.read(), path)

    def of(self, account: Account) -> Kept:
        return self.kept.setdefault((account.provider, account.name), Kept())

    def save(self) -> None:
        """Write `kept` whole: a temporary file in the same directory, locked before the path names it,
        then put in place. The holder keeps the lock on the new file, so a waiter that took the old
        one sees the path has moved and waits again."""
        doc: dict[str, dict[str, Any]] = {}
        for (provider, name), k in sorted(self.kept.items()):
            doc.setdefault(provider, {})[name] = asdict(k)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with open(fd, "wb", closefd=False) as f:
                f.write(json.dumps(doc, indent=1, sort_keys=True).encode())
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        os.close(self._fd)
        self._fd = fd


@contextlib.contextmanager
def held(path: Path | str, wait: float = LOCK_WAIT) -> Iterator[Book]:
    """The evidence file under an exclusive `flock` on the file itself, as the agent's pid file is held,
    from its read to the holder's last write: a read-decide-write inside one `with` meets no other.
    A holder that waits past `wait` seconds is refused with a `Hold`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    first = True  # once per holder: a file opened anew after a replace gets no fresh pass on its bound
    while True:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    if first or time.monotonic() <= deadline:
                        break
                finally:
                    first = False
                # taken late counts as not taken: the holder reads nothing past its bound
                if time.monotonic() > deadline:  # a pick that waits past its bound holds its wake
                    raise Hold(f"{path} is held by another process past {wait:g} seconds") from None
                time.sleep(0.01)
            # locked on a file a writer has since replaced or removed: the path names another, locked anew
            try:
                same = os.path.samestat(os.stat(path), os.fstat(fd))
            except FileNotFoundError:
                same = False
        except BaseException:
            os.close(fd)
            raise
        if same:
            break
        os.close(fd)
    book = None
    try:
        book = Book(path, fd)
        yield book
    finally:
        os.close(fd if book is None else book._fd)


def read_evidence(path: Path | str) -> dict[tuple[str, str], Kept]:
    """What the evidence file holds, read under its lock; no file yet is no evidence."""
    path = Path(path)
    if not path.exists():
        return {}
    with held(path) as book:
        return book.kept


def record(path: Path | str, account: Account, reading: Mapping[str, Window]) -> None:
    """A call's or a probe's reading into the evidence file under its lock: per window, the later
    observed of what is kept and what is given."""
    with held(path) as book:
        _take(book.of(account), reading)
        book.save()


def _take(kept: Kept, reading: Mapping[str, Window]) -> None:
    for w, r in reading.items():
        if w not in kept.readings or r.observed >= kept.readings[w].observed:
            kept.readings[w] = r


def readings(kept: Mapping[tuple[str, str], Kept], pool: Pool, account: Account) -> dict[str, Window]:
    """The account's reading per window: the latest any account of its plan group took, since the group
    draws on one limit (ADR-0024/C3). A group spans one provider."""
    members = [account] + [
        a
        for a in pool.accounts
        if a != account and account.group and a.group == account.group and a.provider == account.provider
    ]
    out: dict[str, Window] = {}
    for a in members:
        for w, r in kept.get((a.provider, a.name), Kept()).readings.items():
            if w not in out or r.observed > out[w].observed:
                out[w] = r
    return out


def fresh(reading: Window, now: float, max_age: float) -> bool:
    """False from its reset instant on, and once it is older than `max_age` seconds."""
    if reading.resets is not None and now >= reading.resets:
        return False
    return now - reading.observed <= max_age


def usable(readings: Mapping[str, Window], settings: Settings, now: float) -> bool | Unknown:
    """Under `seven_day` of seven days and under `five_hour` of five hours. A window over its threshold
    is false whatever the other says; otherwise a window with no fresh reading is UNKNOWN. A fresh absent
    window stands in nobody's way; a stale one is no reading, as a stale figure is."""
    known = True
    for w, limit in (("seven_day", settings.seven_day), ("five_hour", settings.five_hour)):
        r = readings.get(w)
        if r is None or not fresh(r, now, settings.max_age):
            known = False
        elif r.used is not None and r.used >= limit:
            return False
    return True if known else UNKNOWN


def claude_code_reading(row: Any, observed: float, source: str) -> dict[str, Window]:
    """The readings in a Claude Code stream's `rate_limit_event` row: each window's `utilization` and
    `resetsAt` under `unifiedWindows`, observed when the row is read. A window with a field absent or of
    the wrong type is left out, so it reads as unknown."""
    info = (
        row.get("rate_limit_info")
        if isinstance(row, dict) and row.get("type") == "rate_limit_event"
        else None
    )
    windows = info.get("unifiedWindows") if isinstance(info, dict) else None
    out = {}
    for w in WINDOWS:
        got = windows.get(w) if isinstance(windows, dict) else None
        used = got.get("utilization") if isinstance(got, dict) else None
        resets = got.get("resetsAt") if isinstance(got, dict) else None
        if _number(used) and used >= 0 and _instant(resets):
            out[w] = Window(float(used), float(resets), observed, source)
    return out


def _when(text: Any) -> float | None:
    """An ISO 8601 instant with its zone, as epoch seconds; anything else is None."""
    if not isinstance(text, str):
        return None
    try:
        at = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at.timestamp() if at.tzinfo is not None else None


def codex_reading(row: Any, source: str) -> dict[str, Window]:
    """The readings in a Codex rollout's `token_count` row: `rate_limits`, observed at the row's own
    `timestamp`, each entry's `used_percent` read as a fraction and its window told by
    `window_minutes`. Its reset is `resets_at` when that is an instant, else none, and then only
    `max_age` ends the reading. A row with no instant gives no reading; an entry with a field absent or
    of the wrong type is left out, so its window reads as unknown. When one of `primary` and
    `secondary` is an entry naming a known window and the other is there and empty, the plan has no
    other window: that one is absent, observed with the row (C3). Both empty, a slot missing or of
    another type, or an entry of an unknown length leave the other unknown."""
    payload = row.get("payload") if isinstance(row, dict) else None
    limits = (
        payload.get("rate_limits")
        if isinstance(payload, dict) and payload.get("type") == "token_count"
        else None
    )
    observed = _when(row.get("timestamp")) if isinstance(row, dict) else None
    if not isinstance(limits, dict) or observed is None:
        return {}
    out = {}
    for entry in limits.values():
        if not isinstance(entry, dict):
            continue
        minutes, used = entry.get("window_minutes"), entry.get("used_percent")
        w = _MINUTES.get(minutes) if _number(minutes) else None
        if w is None or not (_number(used) and used >= 0):
            continue
        resets = entry.get("resets_at")
        out[w] = Window(used / 100, float(resets) if _instant(resets) else None, observed, source)
    filled = [e for e in (limits.get("primary"), limits.get("secondary")) if isinstance(e, dict)]
    empty = [slot for slot in ("primary", "secondary") if slot in limits and limits[slot] is None]
    if len(filled) == 1 and len(empty) == 1:
        minutes = filled[0].get("window_minutes")
        named = _MINUTES.get(minutes) if _number(minutes) else None
        if named is not None:
            other = next(w for w in WINDOWS if w != named)
            out.setdefault(other, Window(None, None, observed, source))
    return out


def _time(instant: float) -> str:
    """An instant as a person reads it, in the local zone."""
    return datetime.fromtimestamp(instant).astimezone().strftime("%Y-%m-%d %H:%M %Z")


@dataclass(frozen=True)
class Probed:
    """What one probe took (ADR-0024/D4): when it started, how it ended, its readings, and for a failure
    its exit code or cause, never the CLI's own words, which may hold a credential (C1)."""

    at: float
    ended: str  # figures, no figure, or failed
    readings: dict[str, Window] = field(default_factory=dict)
    why: str | None = None


@dataclass
class _Probing(CodexSession):
    """The Codex backend as a probe runs it: its argv and its catalogue as every call's, and every child
    it starts on the probe's environment, where the backend's own `_env` takes this process's (C2)."""

    environment: dict[str, str] = field(default_factory=dict)

    def _env(self) -> dict[str, str]:
        return dict(self.environment)


def _call(
    name: str, argv: list[str], env: dict[str, str], deadline: float, bound: float, cwd: str | None = None
) -> tuple[bytes, str | None]:
    """A probe's child, the prompt on its stdin: (what it printed, None), or (nothing, why it failed).
    Its stderr is never read and its stdout never relayed, since either may hold a credential (C1). Its
    whole process group is killed when it ends or at `deadline`, as the backends kill theirs on a
    timeout, so nothing left in its group outlives it when the host permits the kill; a kill the host
    refuses makes the probe a failure and leaves the leader to the host. A descendant that leaves the
    group for a session of its own is beyond that kill, and a pipe it holds open does not hold the
    probe past `deadline`. Every refusal of the host on the way (a start, a read, a kill, a close, a
    reap) is a failure value, never raised (C6)."""
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
    except OSError as e:
        return b"", f"{name} could not start: {e.strerror or type(e).__name__}"
    why = None
    try:
        out, _ = proc.communicate(_PROBE_PROMPT, timeout=deadline - time.monotonic())
    except subprocess.TimeoutExpired:
        out, why = None, f"{name} did not end within {bound:g} seconds"
    except OSError as e:  # a pipe the host refused mid-read: a failure is a value, never raised (C6)
        out, why = None, f"{name} could not be read: {e.strerror or type(e).__name__}"
    finally:
        # the group may be gone already (ESRCH); refused for any other reason, its members may be alive
        # past the probe, which D4 promises never happens, so the probe is a failure, whatever it printed
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as e:
            out, why = None, f"{name}'s process group could not be killed: {e.strerror or type(e).__name__}"
        for pipe in (proc.stdin, proc.stdout):  # nothing more is read from it (C1)
            if pipe is not None:
                with contextlib.suppress(OSError):  # a close the host refuses leaks a handle, not a reading
                    pipe.close()
        # reaped within the bound; a leader a refused kill left running is left to the host, not waited on
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()) + 0.05)
        except subprocess.TimeoutExpired:
            pass
        except OSError as e:  # reaped by another hand, or refused: how it ended is unknown, so it failed
            out, why = None, f"{name}'s exit could not be read: {e.strerror or type(e).__name__}"
    if why is not None:
        return b"", why
    if proc.returncode:
        return b"", f"{name} exited {proc.returncode}"
    return out, None


def _rows(out: bytes) -> Iterator[Any]:
    for line in out.decode(errors="replace").split("\n"):
        try:
            yield json.loads(line)
        except ValueError:
            continue


def _claude_code_probe(cli: str, model: str | None, env: dict[str, str], bound: float) -> Probed:
    at = time.time()
    # `_exec`'s headless flags (hub/backend/claude_code), in the stream form, which carries the limit row
    # its one-envelope form lacks; `_exec` writes its argv inline, so the flags are named again here
    argv = [cli, "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence"]
    argv += ["--setting-sources", "", "--strict-mcp-config", *(["--model", model] if model else [])]
    argv += ["--tools", "", "--max-turns", "1"]
    out, failed = _call("claude", argv, env, time.monotonic() + bound, bound)
    if failed:
        return Probed(at, "failed", why=failed)
    got: dict[str, Window] = {}
    for row in _rows(out):  # each `rate_limit_event` row as it is read, the last of each window kept
        got.update(claude_code_reading(row, time.time(), "probe"))
    return Probed(at, "figures" if got else "no figure", got)


def _rollout(env: Mapping[str, str], thread: Any) -> dict[str, Window]:
    """The readings in the last `token_count` row of the thread's rollout, found as the Codex backend's
    `_meter` finds it, under the home the child ran with. `_meter` keeps the row's `rate_limits` alone,
    without the instant a reading is observed at, so the row is read here. Nothing when none is found."""
    if not isinstance(thread, str) or not _THREAD.fullmatch(thread):
        return {}
    home = Path(env.get("CODEX_HOME") or Path(env.get("HOME") or Path.home()) / ".codex")
    try:
        (path,) = (home / "sessions").glob(f"*/*/*/rollout-*-{thread}.jsonl")
        with path.open("rb") as f:
            f.seek(max(0, path.stat().st_size - _TAIL))
            tail = f.read()
    except (OSError, ValueError):
        return {}
    for row in reversed(list(_rows(tail))):
        payload = row.get("payload") if isinstance(row, dict) else None
        if isinstance(payload, dict) and payload.get("type") == "token_count":
            return codex_reading(row, "probe")
    return {}


def _codex_probe(cli: str, model: str | None, env: dict[str, str], bound: float) -> Probed:
    at = time.time()
    deadline = time.monotonic() + bound
    backend = _Probing(model=model, cli=cli, environment=env)
    try:
        # the catalogue every call hands the CLI, asked of it as the backend asks, which refuses a release
        # its tools-off claim was not measured on
        catalog = asyncio.run(asyncio.wait_for(backend._catalog(), bound))
    except TimeoutError:
        return Probed(at, "failed", why=f"codex did not end within {bound:g} seconds")
    except OSError as e:
        return Probed(at, "failed", why=f"codex could not start: {e.strerror or type(e).__name__}")
    except CodexError:  # its message may carry what the CLI printed (C1)
        return Probed(at, "failed", why="codex gave no release or model catalogue the backend calls with")
    try:
        scratch = tempfile.mkdtemp(prefix="lion-probe-")
    except OSError as e:
        why = f"codex's scratch could not be made: {e.strerror or type(e).__name__}"
        return Probed(at, "failed", why=why)
    try:
        try:
            for file, text in (("AGENTS.md", SCRATCH_AGENTS_MD), ("instructions.md", INSTRUCTIONS)):
                Path(scratch, file).write_text(text)
            Path(scratch, "models.json").write_text(catalog)
        except OSError as e:  # a full or refused disk: a failure is a value (C6)
            why = f"codex's scratch could not be written: {e.strerror or type(e).__name__}"
            return Probed(at, "failed", why=why)
        # the backend's persisted `exec`: its one-shot form is ephemeral and leaves no rollout to read
        argv = backend._thread_argv(Path(scratch), None)
        out, failed = _call("codex", argv, env, deadline, bound, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if failed:
        return Probed(at, "failed", why=failed)
    thread = None
    for event in _rows(out):
        if isinstance(event, dict) and event.get("type") == "thread.started":
            thread = event.get("thread_id")
    got = _rollout(env, thread)
    return Probed(at, "figures" if got else "no figure", got)


def probe(
    account: Account,
    settings: Settings,
    environ: Mapping[str, str] | None = None,
    claude: str | None = None,
    codex: str | None = None,
    timeout: float = PROBE_WAIT,
) -> Probed:
    """One small call on the provider's probe model, made only to take a limit reading (ADR-0024/D4),
    within `timeout` seconds, on the environment `child_env` builds for the account from `environ` (C2):
    a Claude Code token read from its keychain item, a Codex home as the private home. It ends `figures`,
    `no figure` (the call ran and gave none) or `failed` (it could not start, exited non-zero, timed out,
    or its token could not be read), a failure being a value, never raised. `claude` and `codex` are the
    CLIs; absent, the ones the backends find on PATH. No Codex login file is opened."""
    environ = os.environ if environ is None else environ
    model = settings.probe_model.get(account.provider)
    if account.provider == "codex":
        home = None if account.credential == "default" else account.credential
        env = child_env(account, environ, home=home)
        cli = codex or shutil.which("codex")
        if not cli:
            return Probed(time.time(), "failed", why="no codex on PATH")
        return _codex_probe(cli, model, env, timeout)
    try:
        secret = token(account.name) if account.credential == "keychain" else None
    except AccountsError as e:
        return Probed(time.time(), "failed", why=f"its token could not be read: {e}")
    env = child_env(account, environ, token=secret)
    cli = claude or shutil.which("claude")
    if not cli:
        return Probed(time.time(), "failed", why="no claude on PATH")
    return _claude_code_probe(cli, model, env, timeout)


def _together(
    accounts: Sequence[Account], settings: Settings, probe: Callable[[Account, Settings], Probed]
) -> list[Probed]:
    """One probe per account, all at once, each on its own thread. A probe that raises, which `probe`
    never does, raises here once every thread has ended, so nothing is written for a probe that never
    returned."""
    got: list[Probed | BaseException | None] = [None] * len(accounts)

    def one(i: int, account: Account) -> None:
        try:
            got[i] = probe(account, settings)
        except BaseException as e:
            got[i] = e

    threads = [threading.Thread(target=one, args=(i, a)) for i, a in enumerate(accounts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for g in got:
        if isinstance(g, BaseException):
            raise g
    return [g for g in got if isinstance(g, Probed)]


@contextlib.contextmanager
def _ctrl_c_held() -> Iterator[None]:
    """A Ctrl-C inside the block is held and delivered as the block ends, to whatever handler was there, so
    every probe started inside is written before it acts: one made and not written would be made again
    within `max_age` (C6). Only the main thread receives a signal, so only there is one held."""
    if threading.current_thread() is not threading.main_thread() or signal.getsignal(signal.SIGINT) is None:
        yield
        return
    got: list[int] = []
    before = signal.signal(signal.SIGINT, lambda signum, frame: got.append(signum))
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, before)
        if got:
            signal.raise_signal(signal.SIGINT)


def _took(kept: Kept, got: Probed) -> None:
    _take(kept, got.readings)
    kept.probe = Probe(got.at, got.ended)


def _marked(mark: Mark) -> str:
    if mark.kind == "person":
        return "marked: needs its person"
    return "marked: out of quota until " + ("a probe succeeds" if mark.until is None else _time(mark.until))


def _place(r: Window) -> tuple[int, float]:
    return (0, 0.0) if r.used is None else (1, r.used)  # an absent window before every figure of it


def pick(
    pool: Pool,
    provider: str,
    accounts: Sequence[str] | None,
    last: tuple[str, float] | None,
    now: float,
    probe: Callable[[Account, Settings], Probed],
    wait: float = LOCK_WAIT,
) -> Account:
    """The account a wake on `provider`'s model runs on (ADR-0024/D5, C4, C5, C6), its last use written.
    The candidates are the pool's accounts of that provider, narrowed by the agent's `accounts`, less
    those whose mark has not ended. The agent's `last` use, (name, instant), stays within `keep` while its
    readings say usable. Every other candidate whose usable is unknown is probed, all at once, unless its
    last probe is within `max_age`. The usable rank by seven days, five hours and name, an absent window
    before a figure; the unknown follow, least recently used first. It all runs under the evidence file's
    lock, so a second pick waits and then reads what this one learned. No account raises `Hold`, naming
    each candidate and why it is out; a name in `accounts` that is not the pool's on `provider` is a
    configuration error, `AccountsError`."""
    s = pool.settings
    own = [a for a in pool.accounts if a.provider == provider]
    for name in accounts or ():
        if not any(a.name == name for a in own):
            other = next((a.provider for a in pool.accounts if a.name == name), None)
            where = f"a {other} account" if other else f"not in {pool.path}"
            raise AccountsError(
                f"the agent's accounts name {_key(name)}, which is {where}, not one on {provider}"
            )
    if accounts is not None:
        own = [a for a in own if a.name in accounts]
    if not own:
        raise Hold(f"no {provider} account to pick from in {pool.path}")
    with held(pool.evidence, wait) as book:
        out: dict[str, str] = {}

        def verdict(a: Account) -> bool | Unknown:
            return usable(readings(book.kept, pool, a), s, now)

        candidates = []
        for a in own:
            mark = book.kept.get((a.provider, a.name), Kept()).mark
            if mark is not None and (mark.until is None or mark.until > now):
                out[a.name] = _marked(mark)
            else:
                candidates.append(a)
        stay = None
        if last is not None and now - last[1] <= s.keep:
            stay = next((a for a in candidates if a.name == last[0]), None)
        choice = stay if stay is not None and verdict(stay) is True else None
        if choice is None:
            due = []
            for a in candidates:
                if verdict(a) is not UNKNOWN:
                    continue
                before = book.kept.get((a.provider, a.name), Kept()).probe
                if before is None or now - before.at > s.max_age:
                    if a is not stay:  # keep decides on the readings it has: no probe for it
                        due.append(a)
                elif before.ended == "failed":  # not probed again within max_age (C6), and still unknown
                    out[a.name] = (
                        f"its last probe failed at {_time(before.at)}; the next may run at "
                        f"{_time(before.at + s.max_age)}"
                    )
            if due:
                with _ctrl_c_held():
                    for a, took in zip(due, _together(due, s, probe), strict=True):
                        _took(book.of(a), took)
                        if took.ended == "failed":
                            out[a.name] = f"its probe failed: {took.why}"
                    book.save()
            # the account used within keep stays while a fresh reading says it is usable (C4), its plan
            # group's included, which a probe of another member may just have taken
            choice = stay if stay is not None and verdict(stay) is True else None
        if choice is None:
            ranked, unknown = [], []
            for a in candidates:
                if a.name in out:
                    continue
                got = readings(book.kept, pool, a)
                v = usable(got, s, now)
                if v is UNKNOWN:
                    used = book.kept.get((a.provider, a.name), Kept()).used
                    unknown.append(((used is not None, used or 0.0, a.name), a))
                elif v:
                    ranked.append(((_place(got["seven_day"]), _place(got["five_hour"]), a.name), a))
                else:
                    out[a.name] = "; ".join(  # the windows that made it false: fresh figures at a threshold
                        f"{w} {r.used:g} is at or over {limit:g}"
                        for w, limit in (("seven_day", s.seven_day), ("five_hour", s.five_hour))
                        if (r := got.get(w)) is not None
                        and r.used is not None
                        and fresh(r, now, s.max_age)
                        and r.used >= limit
                    )
            best = min(ranked or unknown, key=lambda item: item[0], default=None)
            choice = None if best is None else best[1]
        if choice is not None:
            book.of(choice).used = now
        book.save()
    if choice is None:
        out = {a.name: out[a.name] for a in own}
        said = "; ".join(f"{name}: {why}" for name, why in out.items())
        raise Hold(f"no {provider} account can run the wake: {said}", out)
    return choice
