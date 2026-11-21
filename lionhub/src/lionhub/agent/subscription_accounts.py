# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Named subscription accounts for CLI agents.

A Claude Code account is a login token; a Codex account is a Codex home directory (its
``auth.json`` and ``config.toml``). An agent profile or ``li agent --account`` names one, and
the spawned CLI child gets it in its environment only: ``CLAUDE_CODE_OAUTH_TOKEN`` for Claude
Code, ``CODEX_HOME`` for Codex.

The accounts live in one directory, ``LIONAGI_LOGINS_DIR`` (default ``<LIONAGI_HOME>/logins``):

- ``claude-<name>``: a file holding one Claude Code token.
- ``accounts.toml``: a ``[claude_code.<name>]`` table with exactly one token source,
  ``keychain = { service = "...", account = "..." }`` (a macOS login keychain item) or
  ``dotenv = { file = "/abs/.env", var = "NAME" }``; a ``[codex.<name>]`` table with
  ``home = "/abs/path"``.

Nothing in the directory listing or ``accounts.toml`` is a secret: a token is read from its
source when a child is spawned and goes into that child's environment only, never into a
config, a log, or a run record. A name that does not resolve, or a source that cannot be read,
refuses the spawn. There is no fallback to another account or to the machine's own login.

See docs/internals/subscription-accounts.md.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from lionagi._errors import ConfigurationError

try:
    import tomllib as _tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as _tomllib  # type: ignore[no-redef]

__all__ = (
    "ACCOUNTS_FILE",
    "ACCOUNT_PROVIDERS",
    "KEYCHAIN_TIMEOUT_S",
    "LOGINS_DIR_ENV",
    "LegAccount",
    "SubscriptionAccount",
    "SubscriptionAccountError",
    "account_child_env",
    "account_provider",
    "codex_home_child_env",
    "credential_paths",
    "leg_account_reached",
    "logins_dir",
    "resolve_account",
    "resolve_leg_account",
    "validate_account_name",
    "validate_codex_home",
)

LOGINS_DIR_ENV = "LIONAGI_LOGINS_DIR"
ACCOUNTS_FILE = "accounts.toml"
KEYCHAIN_TIMEOUT_S = 10.0
#: The canonical providers an account can serve, as ``EndpointRegistry.canonical_provider``
#: names them.
ACCOUNT_PROVIDERS = ("claude_code", "codex")

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
_SECURITY = "/usr/bin/security"
#: Variables that would make Claude Code authenticate as something other than the account's
#: token: an API key or bearer token outranks the OAuth token, a credential handed on a file
#: descriptor replaces it, and a backend switch replaces the Anthropic login altogether.
_CLAUDE_OUTRANKING_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_SESSION_ACCESS_TOKEN",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_GATEWAY",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_VERTEX",
)
#: Variables by which Codex signs in from the environment instead of the home's ``auth.json``.
_CODEX_OUTRANKING_ENV = ("CODEX_ACCESS_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY")
#: Longest invalid account name an error repeats; a longer one may be a pasted credential.
_ECHO_LIMIT = 64
_CLAUDE_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"  # noqa: S105 — a variable name


class SubscriptionAccountError(ConfigurationError):
    """A named account cannot be resolved or its credential cannot be read."""


@dataclass(frozen=True)
class SubscriptionAccount:
    """One resolved account. Holds where the credential is, never the credential."""

    name: str
    provider: str
    source: str
    """``file``, ``keychain`` or ``dotenv`` for Claude Code; ``home`` for Codex."""
    location: tuple[str, ...]
    """The token file path; the keychain service and account; the .env file and variable;
    or the Codex home's real path."""

    def describe(self) -> str:
        """Where the credential comes from, for an error or a launch line. No secret."""
        if self.source == "keychain":
            return f"keychain item service={self.location[0]!r} account={self.location[1]!r}"
        if self.source == "dotenv":
            return f"variable {self.location[1]!r} in {self.location[0]!r}"
        if self.source == "home":
            return f"Codex home {self.location[0]!r}"
        return f"token file {self.location[0]!r}"


def validate_account_name(name: object, *, where: str = "account") -> str:
    """Return *name* when it is a valid account name; refuse anything else."""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        shown = (
            repr(name)
            if isinstance(name, str) and len(name) <= _ECHO_LIMIT
            else f"a {type(name).__name__} value"
            + (f" of {len(name)} characters" if isinstance(name, str) else "")
        )
        raise SubscriptionAccountError(
            f"{where}: {shown} is not an account name (lowercase letters, digits, '_' and '-', "
            "starting with a letter or digit)."
        )
    return name


def validate_codex_home(raw: object, *, where: str = "codex_home") -> str:
    """The real path of a Codex home named as an absolute path to an existing directory.

    The path must be absolute as written: a relative path or a ``~`` would name a different
    directory depending on who reads it. It is used as its real path, so the parent and the
    child read the same directory whatever symlink it was named through.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise SubscriptionAccountError(
            f"{where}: expected an absolute directory path, got {raw!r}."
        )
    if raw.startswith("~") or not os.path.isabs(raw):
        raise SubscriptionAccountError(
            f"{where}: {raw!r} is not an absolute path; name the Codex home as an absolute path."
        )
    real = os.path.realpath(raw)
    if not os.path.isdir(real):
        raise SubscriptionAccountError(f"{where}: {raw!r} is not an existing directory.")
    return real


def account_provider(provider: str) -> str | None:
    """The account provider a model provider name uses, or None when it takes no account."""
    from lionagi.service.connections.registry import EndpointRegistry
    from lionagi.service.providers import _CLAUDE_PROVIDER_NAMES

    canonical = EndpointRegistry.canonical_provider(provider)
    if canonical == "codex":
        return "codex"
    if canonical in _CLAUDE_PROVIDER_NAMES:
        return "claude_code"
    return None


def logins_dir(environ: Mapping[str, str] | None = None) -> Path:
    """The accounts directory: ``LIONAGI_LOGINS_DIR`` from the process environment, else from
    ``<LIONAGI_HOME>/.env``, else ``<LIONAGI_HOME>/logins``.

    A working directory's own ``.env`` is not read for this setting, so a checkout under review
    cannot point a leg at accounts of its choosing.
    """
    from lionagi._paths import LIONAGI_HOME

    source = os.environ if environ is None else environ
    raw = source.get(LOGINS_DIR_ENV)
    if not raw:
        home_env = LIONAGI_HOME / ".env"
        if home_env.is_file():
            from dotenv import dotenv_values

            try:
                raw = dotenv_values(home_env, interpolate=False).get(LOGINS_DIR_ENV)
            except (OSError, UnicodeError) as exc:
                raise SubscriptionAccountError(
                    f"{LOGINS_DIR_ENV}: cannot read {str(home_env)!r} ({type(exc).__name__})."
                ) from None
    if not raw:
        return LIONAGI_HOME / "logins"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise SubscriptionAccountError(f"{LOGINS_DIR_ENV}: {raw!r} is not an absolute path.")
    return path


def _read_accounts_file(directory: Path) -> dict:
    path = directory / ACCOUNTS_FILE
    try:
        if not path.is_file():
            return {}
        data = _tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, RecursionError, _tomllib.TOMLDecodeError) as exc:
        raise SubscriptionAccountError(
            f"cannot read the accounts file {str(path)!r} ({type(exc).__name__})."
        ) from None
    unknown = sorted(set(data) - set(ACCOUNT_PROVIDERS))
    if unknown:
        raise SubscriptionAccountError(
            f"{str(path)!r}: unknown top-level table(s) {unknown}; expected "
            f"{list(ACCOUNT_PROVIDERS)}."
        )
    for provider in ACCOUNT_PROVIDERS:
        if provider in data and not isinstance(data[provider], dict):
            raise SubscriptionAccountError(f"{str(path)!r}: [{provider}] must be a table.")
    return data


def _table_entry(path: Path, provider: str, name: str, row: object) -> SubscriptionAccount:
    where = f"{str(path)!r} [{provider}.{name}]"
    if not isinstance(row, dict):
        raise SubscriptionAccountError(f"{where} must be a table.")
    if provider == "codex":
        if set(row) != {"home"}:
            raise SubscriptionAccountError(f"{where} takes exactly one key, `home`.")
        home = validate_codex_home(row["home"], where=f"{where} home")
        return SubscriptionAccount(name, provider, "home", (home,))
    sources = sorted(set(row) & {"keychain", "dotenv"})
    if len(sources) != 1 or set(row) != set(sources):
        raise SubscriptionAccountError(
            f"{where} takes exactly one token source: `keychain` or `dotenv`."
        )
    source = sources[0]
    spec = row[source]
    keys = ("service", "account") if source == "keychain" else ("file", "var")
    if (
        not isinstance(spec, dict)
        or set(spec) != set(keys)
        or not all(isinstance(spec[k], str) and spec[k] for k in keys)
    ):
        raise SubscriptionAccountError(
            f"{where} {source} takes exactly the non-empty strings {list(keys)}."
        )
    if source == "dotenv" and (spec["file"].startswith("~") or not os.path.isabs(spec["file"])):
        raise SubscriptionAccountError(f"{where} dotenv file {spec['file']!r} is not absolute.")
    return SubscriptionAccount(name, provider, source, tuple(spec[k] for k in keys))


def credential_paths(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Every file or directory the accounts directory keeps or names a sign-in in.

    That is the directory itself, which holds the token files and ``accounts.toml``, each
    ``.env`` file a Claude Code row reads, and each Codex home as a whole: besides its
    ``auth.json``, a home's ``config.toml`` can hold provider keys and MCP server ``env`` values.
    A Codex home that no longer exists is still named. A keychain item is not a file and is not
    listed. No secret is read. The child sandbox denies a child reading any of these.

    Raises:
        SubscriptionAccountError: the directory setting, the accounts file or one of its rows
            cannot be read.
    """
    directory = logins_dir(environ)
    accounts_file = directory / ACCOUNTS_FILE
    data = _read_accounts_file(directory)
    paths = [str(directory)]
    # The token files are named one by one too, so a file that links out of the directory is
    # denied at its target as well.
    if directory.is_dir():
        paths += sorted(str(path) for path in directory.glob("claude-*"))
    for name, row in sorted(data.get("claude_code", {}).items()):
        account = _table_entry(accounts_file, "claude_code", name, row)
        if account.source == "dotenv":
            paths.append(account.location[0])
    for name, row in sorted(data.get("codex", {}).items()):
        home = row.get("home") if isinstance(row, dict) else None
        if not isinstance(home, str) or home.startswith("~") or not os.path.isabs(home):
            raise SubscriptionAccountError(
                f"{str(accounts_file)!r} [codex.{name}] home must be an absolute path."
            )
        paths.append(home)
    # A deny entry may not carry a `..` segment, and `..` after a link would name another
    # directory than the one a reader reaches, so each path is listed in both spellings: the
    # directory setting and the `.env` files are written by hand as freely as a home.
    spelled = [form for path in paths for form in (os.path.normpath(path), os.path.realpath(path))]
    return tuple(dict.fromkeys(spelled))


def resolve_account(
    name: str, provider: str, *, environ: Mapping[str, str] | None = None
) -> SubscriptionAccount:
    """Resolve *name* for an agent whose model provider is *provider*.

    Refused: an invalid name; a provider that takes no account; a name with no entry for the
    provider; a name defined twice for one provider (a token file and a table).
    """
    validate_account_name(name)
    kind = account_provider(provider)
    if kind is None:
        raise SubscriptionAccountError(
            f"account {name!r}: the {provider!r} provider takes no subscription account "
            f"(accounts serve {list(ACCOUNT_PROVIDERS)})."
        )
    directory = logins_dir(environ)
    table = _read_accounts_file(directory).get(kind, {})
    token_file = directory / f"claude-{name}"
    # A dangling link counts as an entry, so it refuses when read rather than letting a
    # table entry of the same name answer for it.
    has_file = kind == "claude_code" and os.path.lexists(token_file)
    if has_file and name in table:
        raise SubscriptionAccountError(
            f"account {name!r} is defined twice for claude_code: {str(token_file)!r} and "
            f"[claude_code.{name}] in {str(directory / ACCOUNTS_FILE)!r}."
        )
    if has_file:
        return SubscriptionAccount(name, kind, "file", (str(token_file),))
    if name in table:
        return _table_entry(directory / ACCOUNTS_FILE, kind, name, table[name])
    raise SubscriptionAccountError(
        f"account {name!r} has no {kind} entry in {str(directory)!r}"
        + (f" (no file claude-{name}, " if kind == "claude_code" else " (")
        + f"no [{kind}.{name}] in {ACCOUNTS_FILE}). The spawn does not fall back to another "
        "account or to this machine's own login."
    )


def _read_token(account: SubscriptionAccount) -> str:
    if account.source == "file":
        path = account.location[0]
        try:
            token = Path(path).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise SubscriptionAccountError(
                f"account {account.name!r}: cannot read {account.describe()} "
                f"({type(exc).__name__})."
            ) from None
    elif account.source == "dotenv":
        file, var = account.location
        if not os.path.isfile(file):
            raise SubscriptionAccountError(
                f"account {account.name!r}: {account.describe()}: the file does not exist."
            )
        from dotenv import dotenv_values

        try:
            values = dotenv_values(file, interpolate=False)
        except (OSError, UnicodeError) as exc:
            raise SubscriptionAccountError(
                f"account {account.name!r}: cannot read {account.describe()} "
                f"({type(exc).__name__})."
            ) from None
        if var not in values:
            raise SubscriptionAccountError(
                f"account {account.name!r}: {account.describe()}: the file does not set it."
            )
        token = (values[var] or "").strip()
    elif account.source == "keychain":
        token = _read_keychain(account)
    else:
        raise SubscriptionAccountError(f"account {account.name!r} holds no token.")
    if not token:
        raise SubscriptionAccountError(f"account {account.name!r}: {account.describe()} is empty.")
    return token


def _read_keychain(account: SubscriptionAccount) -> str:
    """One named generic-password item from the login keychain, read with a bound wait.

    A missing item, a locked keychain, or an access prompt nobody answers each refuse the spawn
    naming the item. The value is kept in memory only; nothing the tool prints is logged.
    """
    service, item_account = account.location
    if sys.platform != "darwin":
        raise SubscriptionAccountError(
            f"account {account.name!r}: {account.describe()} needs the macOS keychain, and this "
            f"platform is {sys.platform!r}."
        )
    try:
        done = subprocess.run(  # noqa: S603 — fixed absolute tool, arguments from the config
            [_SECURITY, "find-generic-password", "-s", service, "-a", item_account, "-w"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise SubscriptionAccountError(
            f"account {account.name!r}: {account.describe()} was not read within "
            f"{KEYCHAIN_TIMEOUT_S:g} s (the keychain is locked or waiting on an access prompt)."
        ) from None
    except OSError as exc:
        raise SubscriptionAccountError(
            f"account {account.name!r}: cannot run {_SECURITY} for {account.describe()} "
            f"({type(exc).__name__})."
        ) from None
    except UnicodeError as exc:
        # The undecodable bytes are the item's value: the error carries them, so it is dropped.
        raise SubscriptionAccountError(
            f"account {account.name!r}: {account.describe()} is not text ({type(exc).__name__})."
        ) from None
    if done.returncode == 44:
        raise SubscriptionAccountError(
            f"account {account.name!r}: {account.describe()} is not in the keychain."
        )
    if done.returncode != 0:
        raise SubscriptionAccountError(
            f"account {account.name!r}: {account.describe()} could not be read "
            f"(security exit {done.returncode}; the keychain may be locked)."
        )
    return done.stdout.strip()


def codex_home_child_env(home: str, parent_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The child environment for a Codex agent run under *home* (a validated real path).

    The variables by which Codex would sign in from the environment are removed, so the
    home's own sign-in decides the account."""
    source = os.environ if parent_env is None else parent_env
    env = {k: v for k, v in source.items() if k not in _CODEX_OUTRANKING_ENV}
    env["CODEX_HOME"] = home
    return env


def account_child_env(
    account: SubscriptionAccount, parent_env: Mapping[str, str] | None = None
) -> tuple[dict[str, str], frozenset[str]]:
    """The whole child environment for *account*, and the names the secret lookup must skip.

    Claude Code: the parent environment without the variables that outrank an OAuth token,
    with ``CLAUDE_CODE_OAUTH_TOKEN`` set to the account's token; the secret lookup skips those
    variables, so it does not put one back. Codex: the parent environment with ``CODEX_HOME``.
    Reads the credential now, so a source that cannot be read refuses here.
    """
    source = os.environ if parent_env is None else parent_env
    if account.provider == "codex":
        home = validate_codex_home(account.location[0], where=f"account {account.name!r} home")
        return codex_home_child_env(home, source), frozenset(_CODEX_OUTRANKING_ENV)
    token = _read_token(account)
    outranking = {name.upper() for name in _CLAUDE_OUTRANKING_ENV}
    env = {k: v for k, v in source.items() if k.upper() not in outranking}
    env[_CLAUDE_TOKEN_ENV] = token
    return env, frozenset(_CLAUDE_OUTRANKING_ENV)


@dataclass(frozen=True)
class LegAccount:
    """What a leg runs on: the child environment, the names the secret lookup skips, and the
    record a run manifest and a saved branch keep (names and paths only, never a token)."""

    env: dict[str, str] = field(repr=False)
    """Holds a Claude Code token, so it is kept out of the dataclass repr."""
    secret_fill_skip: frozenset[str]
    record: dict[str, str]


def leg_account_reached(leg: LegAccount, runtime_state: object) -> bool:
    """Whether an endpoint's runtime state holds *leg*'s child environment and every name its
    secret lookup must skip. An endpoint that dropped either would run the child on this
    machine's own login, or let the lookup put a removed credential back."""
    if not isinstance(runtime_state, Mapping) or "env" not in runtime_state:
        return False
    if not leg.secret_fill_skip:
        return True
    kept = runtime_state.get("secret_fill_skip")
    return isinstance(kept, (set, frozenset)) and leg.secret_fill_skip <= kept


def resolve_leg_account(
    provider: str,
    *,
    account: str | None = None,
    codex_home: str | None = None,
    parent_env: Mapping[str, str] | None = None,
) -> LegAccount | None:
    """Resolve a leg's ``account`` or ``codex_home`` for a model of *provider*.

    None when neither is set: the child inherits this process's environment, as before.
    Refused: both set; ``codex_home`` on a provider other than codex; anything
    ``resolve_account`` or the credential read refuses.
    """
    if account is not None and codex_home is not None:
        raise SubscriptionAccountError("account and codex_home name the same thing; set one.")
    if account is not None:
        resolved = resolve_account(account, provider)
        env, skip = account_child_env(resolved, parent_env)
        record = {"account": resolved.name, "account_provider": resolved.provider}
        if resolved.provider == "codex":
            # The home the leg runs in, so a resume can hold the branch to it even after the
            # name is pointed at another home.
            record["codex_home"] = env["CODEX_HOME"]
        return LegAccount(env, skip, record)
    if codex_home is not None:
        if account_provider(provider) != "codex":
            raise SubscriptionAccountError(
                f"codex_home applies to a codex agent; this agent's provider is {provider!r}."
            )
        home = validate_codex_home(codex_home)
        return LegAccount(
            codex_home_child_env(home, parent_env),
            frozenset(_CODEX_OUTRANKING_ENV),
            {"codex_home": home, "account_provider": "codex"},
        )
    return None
