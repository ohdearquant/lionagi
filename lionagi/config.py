# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import json
import os
import re
from decimal import Decimal
from inspect import getattr_static
from pathlib import Path
from typing import Annotated, Any, ClassVar, get_args

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from lionagi._paths import LIONAGI_HOME

_ACCOUNT_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
# The secret lookup's variable-name rule, bounded.
_ACCOUNT_REFERENCE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
# Positive decimal dollars, at most nine fractional digits: a whole number of nanodollars.
_BUDGET_USD_RE = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,9})?")
_ACCOUNT_FIELDS = frozenset({"name", "key_env", "budget_usd"})
_INT64_MAX = 2**63 - 1


class AccountConfigError(Exception):
    """An invalid routed-account declaration.

    Deliberately not a ValueError: pydantic would wrap one and repeat the raw
    input, which here may be a pasted credential. The message names the field
    and the rule only.
    """

    def __init__(self, field: str, rule: str) -> None:
        self.field = field
        self.rule = rule
        super().__init__(f"{field}: {rule}")


def _looks_like_a_key(text: str) -> bool:
    return "sk-ant" in text.lower()


class ProviderAccount(BaseModel):
    """One routed account: a stable name, the name of its key's reference, a monthly budget.

    Holds no credential. ``key_env`` names the variable, dotenv key or lookup
    item the key is read from.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    key_env: str
    budget_usd: Decimal

    @classmethod
    def from_declaration(cls, item: Any) -> "ProviderAccount":
        if not isinstance(item, dict):
            raise AccountConfigError("", "must be an object")
        if set(item) != _ACCOUNT_FIELDS:
            raise AccountConfigError(
                "", "must have exactly the fields name, key_env and budget_usd"
            )
        name, key_env, budget = item["name"], item["key_env"], item["budget_usd"]
        if (
            not isinstance(name, str)
            or not _ACCOUNT_NAME_RE.fullmatch(name)
            or _looks_like_a_key(name)
        ):
            raise AccountConfigError("name", "must match [a-z][a-z0-9_-]{0,31} and not be a key")
        if (
            not isinstance(key_env, str)
            or not _ACCOUNT_REFERENCE_RE.fullmatch(key_env)
            or _looks_like_a_key(key_env)
        ):
            raise AccountConfigError(
                "key_env", "must match [A-Za-z_][A-Za-z0-9_]{0,63} and not be a key"
            )
        # A string only: a JSON number has already been through binary floating point.
        if not isinstance(budget, str) or not _BUDGET_USD_RE.fullmatch(budget):
            raise AccountConfigError(
                "budget_usd", "must be a decimal string with at most nine fractional digits"
            )
        budget_usd = Decimal(budget)
        if budget_usd <= 0 or budget_usd.scaleb(9) > _INT64_MAX:
            raise AccountConfigError("budget_usd", "must be positive and fit 64-bit nanodollars")
        return cls.model_construct(name=name, key_env=key_env, budget_usd=budget_usd)

    @property
    def budget_nusd(self) -> int:
        return int(self.budget_usd.scaleb(9))


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise AccountConfigError("ANTHROPIC_ACCOUNTS", "an object repeats a field")
    return result


def _reject_constant(_: str) -> Any:
    raise AccountConfigError("ANTHROPIC_ACCOUNTS", "must not contain non-finite numbers")


def _parse_accounts(value: Any) -> tuple[ProviderAccount, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(
                value, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant
            )
        except ValueError:
            malformed = True
        else:
            malformed = False
        if malformed:
            # Raised outside the handler, so the error carries no context: the decoder's
            # error holds the document.
            raise AccountConfigError(
                "ANTHROPIC_ACCOUNTS", "must be a JSON array of account objects"
            )
    if not isinstance(value, list | tuple):
        raise AccountConfigError("ANTHROPIC_ACCOUNTS", "must be an array of account objects")
    accounts: list[ProviderAccount] = []
    for index, item in enumerate(value):
        if isinstance(item, ProviderAccount):
            item = {"name": item.name, "key_env": item.key_env, "budget_usd": str(item.budget_usd)}
        try:
            account = ProviderAccount.from_declaration(item)
        except AccountConfigError as exc:
            field, rule = exc.field, exc.rule
        else:
            accounts.append(account)
            continue
        # Raised outside the handler, so the error chains nothing.
        path = f"ANTHROPIC_ACCOUNTS[{index}]" + (f".{field}" if field else "")
        raise AccountConfigError(path, rule)
    if len({a.name for a in accounts}) != len(accounts):
        raise AccountConfigError("ANTHROPIC_ACCOUNTS", "account names must be unique")
    if len({a.key_env for a in accounts}) != len(accounts):
        raise AccountConfigError("ANTHROPIC_ACCOUNTS", "each account needs its own key_env")
    return tuple(accounts)


class CacheConfig(BaseModel):
    ttl: int = 300
    key: str | None = None
    namespace: str | None = None
    key_builder: Any = None
    skip_cache_func: Any = lambda _: False
    serializer: dict[str, Any] | None = None
    plugins: Any = None
    alias: str | None = None
    noself: Any = lambda _: False

    def as_kwargs(self) -> dict[str, Any]:
        raw = self.model_dump(exclude_none=True)
        unserialisable_keys = (
            "key_builder",
            "skip_cache_func",
            "noself",
            "serializer",
            "plugins",
        )
        for key in unserialisable_keys:
            raw.pop(key, None)
        return raw


class AppSettings(BaseSettings, frozen=True):
    model_config = SettingsConfigDict(
        # A later file overrides an earlier one and the environment overrides every file, so the
        # user-wide file under LIONAGI_HOME supplies defaults the working directory can replace.
        env_file=(LIONAGI_HOME / ".env", ".env", ".env.local", ".secrets.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    aiocache_config: CacheConfig = Field(
        default_factory=CacheConfig, description="Cache settings for aiocache"
    )

    OPENAI_API_KEY: SecretStr | None = None
    OPENROUTER_API_KEY: SecretStr | None = None
    OLLAMA_API_KEY: SecretStr | None = None
    EXA_API_KEY: SecretStr | None = None
    TAVILY_API_KEY: SecretStr | None = None
    FIRECRAWL_API_KEY: SecretStr | None = None
    PERPLEXITY_API_KEY: SecretStr | None = None
    GROQ_API_KEY: SecretStr | None = None
    ANTHROPIC_API_KEY: SecretStr | None = None
    NVIDIA_NIM_API_KEY: SecretStr | None = None
    GEMINI_API_KEY: SecretStr | None = None
    DEEPSEEK_API_KEY: SecretStr | None = None

    # Routed Anthropic accounts with monthly budgets, as a JSON array. Empty means
    # calls use ANTHROPIC_API_KEY exactly as before.
    ANTHROPIC_ACCOUNTS: Annotated[tuple[ProviderAccount, ...], NoDecode] = ()

    OPENAI_DEFAULT_MODEL: str = "gpt-4.1-mini"

    LIONAGI_EMBEDDING_PROVIDER: str = "openai"
    LIONAGI_EMBEDDING_MODEL: str = "text-embedding-3-small"

    LIONAGI_CHAT_PROVIDER: str = "openai"
    LIONAGI_CHAT_MODEL: str = "gpt-4.1-mini"

    LIONAGI_AUTO_STORE_EVENT: bool = False
    LIONAGI_STORAGE_PROVIDER: str = "async_qdrant"

    LIONAGI_AUTO_EMBED_LOG: bool = False

    LIONAGI_QDRANT_URL: str = "http://localhost:6333"
    LIONAGI_DEFAULT_QDRANT_COLLECTION: str = "event_logs"

    LIONAGI_STATE_DB_URL: str | None = None

    # First-output liveness window for CLI-streaming run() turns; 0 disables
    # the watchdog. See docs/internals/support-libs.md#config-liveness-timeouts
    LIONAGI_WORKER_LIVENESS_TIMEOUT: float = 120.0

    # Maximum silence between chunks for early-streaming CLI run() turns;
    # 0 disables. Longer than first-output because a worker is silent for the
    # whole of any tool call it makes, so this bounds the worker's slowest
    # single tool call rather than its slowest chunk. Measured against real
    # transcripts, 96 distinct tool calls exceeded 300s (max 362s) and none
    # exceeded 600s, so a 300s window would kill live work. The asymmetry
    # favours generosity: too wide only delays noticing a genuinely hung
    # worker, while too narrow kills a healthy one mid-tool-call and reports
    # it as a liveness failure.
    LIONAGI_WORKER_IDLE_TIMEOUT: float = 600.0

    # Antigravity print-mode subprocess cap.
    # See docs/internals/support-libs.md#config-liveness-timeouts
    LIONAGI_ANTIGRAVITY_PRINT_TIMEOUT: float = 3600.0

    LOG_PERSIST_DIR: str = "./data/logs"
    LOG_SUBFOLDER: str | None = None
    LOG_CAPACITY: int = 50
    LOG_EXTENSION: str = ".json"
    LOG_USE_TIMESTAMP: bool = True
    LOG_HASH_DIGITS: int = 5
    LOG_FILE_PREFIX: str = "log"
    LOG_AUTO_SAVE_ON_EXIT: bool = True
    LOG_CLEAR_AFTER_DUMP: bool = True
    # Keep base64 image/audio payloads out of the log files; each entry keeps a
    # placeholder naming the field, media type and byte length. Set false to log
    # payloads verbatim, at the cost of log files as large as the media.
    LOG_REDACT_BINARY: bool = True
    LOG_REDACT_BINARY_THRESHOLD: int = 1024

    _instance: ClassVar[Any] = None
    # The dotenv files this instance was built from, as absolute paths, so a later
    # chdir cannot change which file a credential is read from.
    _dotenv_files: tuple[Path, ...] = PrivateAttr(default=())

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)
        files = values["_env_file"] if "_env_file" in values else self.model_config.get("env_file")
        if files is None:
            files = ()
        elif isinstance(files, str | os.PathLike):
            files = (files,)
        self._dotenv_files = tuple(Path(f).expanduser().absolute() for f in files)

    @field_validator("ANTHROPIC_ACCOUNTS", mode="before")
    @classmethod
    def _parse_anthropic_accounts(cls, value: Any) -> tuple[ProviderAccount, ...]:
        return _parse_accounts(value)

    def get_secret(self, key_name: str) -> str:
        field = type(self).model_fields.get(key_name)
        if field is None:
            try:
                getattr_static(self, key_name)
            except AttributeError:
                if "ollama" in key_name.lower():
                    return "ollama"
                raise AttributeError(f"Secret key '{key_name}' not found in settings") from None
            raise TypeError(f"Setting '{key_name}' is not a secret")
        if field.annotation is not SecretStr and SecretStr not in get_args(field.annotation):
            raise TypeError(f"Setting '{key_name}' is not a secret")

        secret = getattr(self, key_name)
        if secret is None:
            if "ollama" in key_name.lower():
                return "ollama"
            raise ValueError(f"Secret key '{key_name}' is not set")

        if isinstance(secret, SecretStr):
            return secret.get_secret_value()

        return str(secret)

    @property
    def LOG_CONFIG(self) -> dict[str, Any]:
        return {
            "persist_dir": self.LOG_PERSIST_DIR,
            "subfolder": self.LOG_SUBFOLDER,
            "capacity": self.LOG_CAPACITY,
            "extension": self.LOG_EXTENSION,
            "use_timestamp": self.LOG_USE_TIMESTAMP,
            "hash_digits": self.LOG_HASH_DIGITS,
            "file_prefix": self.LOG_FILE_PREFIX,
            "auto_save_on_exit": self.LOG_AUTO_SAVE_ON_EXIT,
            "clear_after_dump": self.LOG_CLEAR_AFTER_DUMP,
            "redact_binary": self.LOG_REDACT_BINARY,
            "redact_binary_threshold": self.LOG_REDACT_BINARY_THRESHOLD,
        }


settings = AppSettings()
AppSettings._instance = settings
