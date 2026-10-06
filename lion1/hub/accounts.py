"""The accounts agents on Claude Code and Codex run on (ADR-0024): the pool `accounts.toml` declares
beside the areas registry, a Claude Code account's token in its keychain item, the environment a
child of an account starts with, and `evidence.json` beside the pool, every account's limit
readings, last probe, mark and last use, read and written under one lock."""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .areas import DEFAULT_REGISTRY

__all__ = (
    "PROVIDERS",
    "WINDOWS",
    "SOURCES",
    "ENDINGS",
    "MARKS",
    "CREDENTIAL_VARIABLES",
    "DEFAULT_POOL",
    "LOCK_WAIT",
    "AccountsError",
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

# absolute: under launchd PATH is /usr/bin:/bin:/usr/sbin:/sbin, and a test puts a script in its place
_SECURITY = "/usr/bin/security"
_SERVICE = "lion.account.claude_code"  # the keychain item's service; its account is the pool's name
_NAME = re.compile(r"[A-Za-z0-9._-]+")
_MODEL = re.compile(r"[A-Za-z0-9._:/@-]+")
# a label the pool keeps and shows (a name, a plan group, a probe model) is never shaped as a Claude Code
# credential, which begins `sk-ant-` and runs past 64 characters, so a token pasted there is refused
_LABEL_MAX = 64
_LABEL = "letters, digits, '.', '_' and '-', at most 64 of them, not beginning 'sk-ant-'"
_ACCOUNT_HEADER = re.compile(r"\s*\[\[\s*account\s*\]\]\s*(#.*)?$")  # one `[[account]]` line
_TOKEN_MAX = 2048
_ACCOUNT_KEYS = ("name", "provider", "credential", "group")
_MINUTES = {300: "five_hour", 10080: "seven_day"}  # a Codex window is told by its length


class AccountsError(Exception):
    pass


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
    """A value the pool keeps and shows as written: `form`'s characters, at most 64, and not beginning as
    a Claude Code credential does (ADR-0024/C1)."""
    return (
        isinstance(value, str)
        and len(value) <= _LABEL_MAX
        and bool(form.fullmatch(value))
        and not value.lower().startswith("sk-ant-")
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
                    "'@' and '-', at most 64 of them, not beginning 'sk-ant-'"
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
    # `lion account` edits the file by its `[[account]]` lines (D8): an array written inline, or a table
    # named any other way, would load here and then be refused by every edit, so it is refused here
    if sum(1 for line in text.splitlines() if _ACCOUNT_HEADER.match(line)) != len(rows):
        raise AccountsError(
            f"{path}: each account is its own [[account]] table, its header on a line of its own"
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
    return isinstance(value, str) and 0 < len(value) <= _TOKEN_MAX and bool(_NAME.fullmatch(value))


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
    (`api_key` in hub/backend/openrouter.py). The one function that returns a token: its caller hands
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
    if len(value) > _TOKEN_MAX:
        raise AccountsError(
            f"account {name!r}: the token is longer than {_TOKEN_MAX} characters; nothing was written"
        )
    if not _NAME.fullmatch(value):
        raise AccountsError(
            f"account {name!r}: a token is letters, digits, '-', '_' and '.' only; nothing was written"
        )
    try:
        out = _security(["-i"], f"add-generic-password -U -a {name} -s {_SERVICE} -w {value}\n")
        failed = _exited(out) if out.returncode else None
    except AccountsError as e:  # it did not answer, or could not run: what it wrote is read below
        failed = str(e)
    # what `-i` exits with when a command it read fails is not measured, so the item is read back
    try:
        back = _security(["find-generic-password", "-a", name, "-s", _SERVICE, "-w"])
        written, unread = (
            back.returncode == 0 and back.stdout.strip() == value,
            back.returncode not in (0, 44),
        )
    except AccountsError:
        written, unread = False, True
    if failed is None and written:
        return
    why = failed or "the keychain item did not read back as written"
    if written:
        try:
            remove_token(name)
            why += "; the item it wrote was deleted"
        except AccountsError:
            why += f"; {_left(name)}"
    elif unread:
        why += f"; it could not be read back, so {_left(name)}"
    raise AccountsError(f"account {name!r}: {why}")


def remove_token(name: str) -> bool:
    """Delete a Claude Code account's keychain item; whether there was one."""
    _check_name(name)
    out = _security(["delete-generic-password", "-a", name, "-s", _SERVICE])
    if out.returncode not in (0, 44):
        raise AccountsError(f"account {name!r}: {_exited(out)}, so its keychain item was not deleted")
    return out.returncode == 0


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

    used: float  # the fraction of the window's limit used
    resets: float | None  # the instant it resets, epoch seconds; None when the source names none
    observed: float  # epoch seconds
    source: str  # a call or a probe

    def __post_init__(self) -> None:
        resets_ok = self.resets is None or _instant(self.resets)
        if not (_number(self.used) and self.used >= 0 and resets_ok and _instant(self.observed)):
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
    A holder that waits past `wait` seconds is refused."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    while True:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise AccountsError(
                            f"{path} is held by another process past {wait:g} seconds"
                        ) from None
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
        kept = book.of(account)
        for w, r in reading.items():
            if w not in kept.readings or r.observed >= kept.readings[w].observed:
                kept.readings[w] = r
        book.save()


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
    is false whatever the other says; otherwise a window with no fresh reading is UNKNOWN."""
    known = True
    for w, limit in (("seven_day", settings.seven_day), ("five_hour", settings.five_hour)):
        r = readings.get(w)
        if r is None or not fresh(r, now, settings.max_age):
            known = False
        elif r.used >= limit:
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
    of the wrong type is left out, so its window reads as unknown."""
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
    return out
