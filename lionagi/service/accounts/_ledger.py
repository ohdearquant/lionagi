# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Private SQLite accounting for routed provider attempts (ADR-0126 D4)."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import cmp_to_key
from pathlib import Path
from typing import TypeVar
from uuid import UUID

from lionagi.ln import now_utc
from lionagi.ln.concurrency import run_sync
from lionagi.service.accounts._types import (
    INT64_MAX,
    AccountRoutingError,
    AccountSpec,
    CompleteUsage,
    Evidence,
    ProvenNotSent,
    Quote,
    Reservation,
    RouterErrorCode,
    Scope,
    SettleResult,
    UnknownOutcome,
)

__all__ = ("CredentialLedger", "AccountStatus", "default_ledger_path")

_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])\Z")
_INT64_MIN = -(INT64_MAX + 1)
_TABLES = {"account", "month", "binding", "attempt", "adjustment"}
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class AccountStatus:
    """Accounting totals for one provider account in one UTC month."""

    provider: str
    name: str
    month_utc: str
    budget_nusd: int | None
    settled_nusd: int
    adjustments_nusd: int
    open_ceiling_nusd: int
    open_attempts: int
    block_reason: str | None


def default_ledger_path() -> Path:
    """Return the private ledger path beside the global settings file."""
    return Path.home() / ".lionagi" / "credential-ledger.sqlite3"


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise


def _routing_error(
    code: RouterErrorCode,
    *,
    account: str | None = None,
    attempt_id: UUID | None = None,
) -> AccountRoutingError:
    return AccountRoutingError(code, account=account, attempt_id=attempt_id)


def _checked_int(value: object, *, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise ValueError(f"invalid {label}")
    return value


def _validate_reason(reason: object) -> str:
    if not isinstance(reason, str) or _REASON_RE.fullmatch(reason) is None:
        raise ValueError("invalid reason code")
    return reason


def _validate_source(source_ref: object) -> str:
    if not isinstance(source_ref, str) or not source_ref or len(source_ref) > 512:
        raise ValueError("invalid source reference")
    return source_ref


def _validate_month(month_utc: object) -> str:
    if not isinstance(month_utc, str) or _MONTH_RE.fullmatch(month_utc) is None:
        raise ValueError("invalid UTC month")
    return month_utc


def _month_and_iso(clock: Callable[[], datetime]) -> tuple[str, str]:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return an aware datetime")
    value = value.astimezone(timezone.utc)
    # A month is a credit period, which starts when the provider refreshes the credit at 00:00 UTC
    # on a calendar month's last day (ADR-0126 Amendment 3): the one keyed 2026-11 runs from
    # 2026-10-31T00:00Z to 2026-11-30T00:00Z.
    return (value + timedelta(days=1)).strftime("%Y-%m"), value.isoformat()


def _aware_instant(value: object) -> datetime | None:
    """Parse stored text as an instant; None unless it is ISO text carrying an offset."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _clear_warm_prefix(connection: sqlite3.Connection, provider: str, account: str) -> None:
    """Forget every warm-prefix record of one account."""
    connection.execute(
        "DELETE FROM warm_prefix WHERE provider = ? AND account = ?",
        (provider, account),
    )


def _warm_accounts(
    connection: sqlite3.Connection, provider: str, cert: str, now: datetime
) -> set[str]:
    """Return the accounts holding an unexpired warm-prefix record for ``cert``.

    A record whose expiry is not offset-carrying ISO text counts as absent.
    """
    rows = connection.execute(
        "SELECT account, expires_at FROM warm_prefix WHERE provider = ? AND cert = ?",
        (provider, cert),
    ).fetchall()
    names: set[str] = set()
    for name, expires_at in rows:
        expiry = _aware_instant(expires_at)
        if expiry is not None and expiry > now:
            names.add(name)
    return names


def _record_warm_prefix(
    connection: sqlite3.Connection,
    provider: str,
    account: str,
    cert: str,
    usage: CompleteUsage,
    created_at: object,
    hour_after_key: bool,
    server_tools: bool,
) -> None:
    """Record or refresh the warm prefix a completed attempt shows for its account.

    A one-hour cache write stores the stable prefix length (unknown when the request
    carried a later cache point or server tools); a request with no write whose cache
    read equals the stored length refreshes the expiry. Nothing is recorded for a
    blocked account or an attempt time that is not an aware instant.
    """
    state = connection.execute(
        "SELECT block_reason FROM account WHERE provider = ? AND name = ?",
        (provider, account),
    ).fetchone()
    if state is None or state[0] is not None:
        return
    started = _aware_instant(created_at)
    if started is None:
        return
    expires = started + timedelta(hours=1)
    stored = connection.execute(
        "SELECT stable_tokens, expires_at FROM warm_prefix "
        "WHERE provider = ? AND account = ? AND cert = ?",
        (provider, account, cert),
    ).fetchone()
    stored_expiry = _aware_instant(stored[1]) if stored is not None else None
    if stored_expiry is None:
        stored = None
    if usage.cache_write_1h_tokens > 0:
        new_stable: int | None = None
        if not (hour_after_key or server_tools):
            new_stable = usage.cache_read_input_tokens + usage.cache_write_1h_tokens
        if stored is None:
            stable, expiry = new_stable, expires
        else:
            stable = new_stable if new_stable is not None else stored[0]
            expiry = max(stored_expiry, expires)
    else:
        if server_tools or stored is None or stored[0] is None:
            return
        if usage.cache_read_input_tokens != stored[0]:
            return
        stable, expiry = stored[0], max(stored_expiry, expires)
    connection.execute(
        "INSERT OR REPLACE INTO warm_prefix(provider, account, cert, stable_tokens, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (provider, account, cert, stable, expiry.isoformat()),
    )


def _validate_quote(quote: Quote) -> None:
    if not isinstance(quote, Quote):
        raise AccountRoutingError(RouterErrorCode.UNSUPPORTED_PRICING)
    if (
        type(quote.ceiling_nusd) is not int
        or not 1 <= quote.ceiling_nusd <= INT64_MAX
        or not isinstance(quote.price_revision, str)
        or not quote.price_revision
        or not isinstance(quote.requested_model, str)
        or not quote.requested_model
        or not isinstance(quote.request_digest, str)
        or _DIGEST_RE.fullmatch(quote.request_digest) is None
    ):
        raise AccountRoutingError(RouterErrorCode.UNSUPPORTED_PRICING)


def _usage_json(evidence: CompleteUsage) -> tuple[str, str | None]:
    for name in (
        "input_tokens",
        "cache_read_input_tokens",
        "cache_write_5m_tokens",
        "cache_write_1h_tokens",
        "output_tokens",
    ):
        value = getattr(evidence, name)
        if type(value) is not int or value < 0:
            raise ValueError(f"invalid {name}")
    _checked_int(evidence.cost_nusd, minimum=0, maximum=INT64_MAX, label="cost")
    if not isinstance(evidence.served_model, str) or not evidence.served_model:
        raise ValueError("invalid served model")
    usage = {
        "served_model": evidence.served_model,
        "input_tokens": evidence.input_tokens,
        "cache_read_input_tokens": evidence.cache_read_input_tokens,
        "cache_write_5m_tokens": evidence.cache_write_5m_tokens,
        "cache_write_1h_tokens": evidence.cache_write_1h_tokens,
        "output_tokens": evidence.output_tokens,
    }
    request_id = evidence.request_id
    if not isinstance(request_id, str) or _REQUEST_ID_RE.fullmatch(request_id) is None:
        request_id = None
    return json.dumps(usage, sort_keys=True, separators=(",", ":")), request_id


def _sum_rows(rows: list[tuple[int]]) -> int:
    return sum(row[0] for row in rows)


class CredentialLedger:
    """Persist account bindings and attempt costs in a local SQLite file."""

    def __init__(
        self,
        path: Path,
        *,
        busy_timeout_s: float = 5.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.busy_timeout_s = busy_timeout_s
        self._clock = clock or (lambda: now_utc())

    async def _run(self, operation: Callable[[], _T]) -> _T:
        try:
            return await run_sync(operation)
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                raise _routing_error(RouterErrorCode.LEDGER_BUSY) from None
            raise _routing_error(RouterErrorCode.LEDGER_UNAVAILABLE) from None
        except (sqlite3.DatabaseError, OSError):
            raise _routing_error(RouterErrorCode.LEDGER_UNAVAILABLE) from None

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=self.busy_timeout_s,
        )
        try:
            connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_s * 1000)}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            self._ensure_schema(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    @staticmethod
    def _table_names(connection: sqlite3.Connection) -> set[str]:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        # Unlocked, these two reads can straddle another process's schema commit, so
        # they only let a complete ledger through; every other answer is read again,
        # together, under the write lock before anything is refused or created.
        if connection.execute("PRAGMA user_version").fetchone()[0] == 1 and (
            _TABLES | {"warm_prefix"}
        ).issubset(self._table_names(connection)):
            return

        # A ledger written before warm-prefix tracking is still version 1 and has none
        # of this table, so the table is added whenever it is missing and a reader that
        # predates it never needs it.
        warm_prefix_ddl = """
            CREATE TABLE IF NOT EXISTS warm_prefix (
                provider TEXT NOT NULL,
                account TEXT NOT NULL,
                cert TEXT NOT NULL,
                stable_tokens INTEGER,
                expires_at TEXT NOT NULL,
                PRIMARY KEY (provider, account, cert),
                FOREIGN KEY (provider, account) REFERENCES account(provider, name)
            )
            """

        with _transaction(connection):
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = self._table_names(connection)
            if version == 1:
                if not _TABLES.issubset(tables):
                    raise _routing_error(RouterErrorCode.LEDGER_UNAVAILABLE)
                connection.execute(warm_prefix_ddl)
                return
            if version != 0 or tables:
                raise _routing_error(RouterErrorCode.LEDGER_UNAVAILABLE)
            connection.execute(
                """
                CREATE TABLE account (
                    provider TEXT NOT NULL,
                    name TEXT NOT NULL,
                    key_env TEXT NOT NULL,
                    block_reason TEXT,
                    PRIMARY KEY (provider, name)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE month (
                    provider TEXT NOT NULL,
                    name TEXT NOT NULL,
                    month_utc TEXT NOT NULL,
                    budget_nusd INTEGER NOT NULL
                        CHECK (typeof(budget_nusd) = 'integer' AND budget_nusd > 0),
                    PRIMARY KEY (provider, name, month_utc),
                    FOREIGN KEY (provider, name) REFERENCES account(provider, name)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE binding (
                    scope_kind TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    account TEXT NOT NULL,
                    PRIMARY KEY (scope_kind, scope_id, provider),
                    FOREIGN KEY (provider, account) REFERENCES account(provider, name)
                )
                """
            )
            connection.execute(
                f"""
                CREATE TABLE attempt (
                    id TEXT PRIMARY KEY,
                    scope_kind TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    account TEXT NOT NULL,
                    month_utc TEXT NOT NULL,
                    ceiling_nusd INTEGER NOT NULL
                        CHECK (typeof(ceiling_nusd) = 'integer'
                               AND ceiling_nusd BETWEEN 1 AND {INT64_MAX}),
                    price_revision TEXT NOT NULL,
                    requested_model TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('open', 'settled')),
                    cost_nusd INTEGER,
                    usage_json TEXT,
                    request_id TEXT,
                    outcome TEXT,
                    created_at TEXT NOT NULL,
                    settled_at TEXT,
                    CHECK (
                        (state = 'settled' AND typeof(cost_nusd) = 'integer'
                         AND cost_nusd >= 0)
                        OR (state = 'open' AND cost_nusd IS NULL)
                    ),
                    FOREIGN KEY (provider, account) REFERENCES account(provider, name),
                    FOREIGN KEY (provider, account, month_utc)
                        REFERENCES month(provider, name, month_utc)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE adjustment (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (
                        kind IN ('correction', 'reconciliation', 'unblock')
                    ),
                    attempt_id TEXT,
                    provider TEXT NOT NULL,
                    account TEXT NOT NULL,
                    month_utc TEXT,
                    delta_nusd INTEGER NOT NULL CHECK (typeof(delta_nusd) = 'integer'),
                    source_ref TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            # Every admission sums one account's month; without this each one scans
            # every attempt the file has ever held.
            connection.execute(
                "CREATE INDEX attempt_by_account_month "
                "ON attempt(provider, account, month_utc, state)"
            )
            connection.execute(
                "CREATE INDEX adjustment_by_account_month "
                "ON adjustment(provider, account, month_utc)"
            )
            connection.execute(warm_prefix_ddl)
            connection.execute("PRAGMA user_version = 1")

    @staticmethod
    def _spend_parts(
        connection: sqlite3.Connection,
        provider: str,
        account: str,
        month_utc: str,
    ) -> tuple[int, int, int, int]:
        settled = _sum_rows(
            connection.execute(
                """
                SELECT cost_nusd FROM attempt
                WHERE provider = ? AND account = ? AND month_utc = ? AND state = 'settled'
                """,
                (provider, account, month_utc),
            ).fetchall()
        )
        adjustments = _sum_rows(
            connection.execute(
                """
                SELECT delta_nusd FROM adjustment
                WHERE provider = ? AND account = ? AND month_utc = ? AND kind = 'correction'
                """,
                (provider, account, month_utc),
            ).fetchall()
        )
        open_rows = connection.execute(
            """
            SELECT ceiling_nusd FROM attempt
            WHERE provider = ? AND account = ? AND month_utc = ? AND state = 'open'
            """,
            (provider, account, month_utc),
        ).fetchall()
        opened = sum(row[0] for row in open_rows)
        scopes = connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT DISTINCT scope_kind, scope_id FROM attempt
                WHERE provider = ? AND account = ? AND month_utc = ? AND state = 'open'
            )
            """,
            (provider, account, month_utc),
        ).fetchone()[0]
        spent = settled + adjustments
        if settled > INT64_MAX or not _INT64_MIN <= adjustments <= INT64_MAX or opened > INT64_MAX:
            raise _routing_error(RouterErrorCode.LEDGER_UNAVAILABLE, account=account)
        if spent < 0:
            raise _routing_error(RouterErrorCode.LEDGER_UNAVAILABLE, account=account)
        return settled, adjustments, opened, scopes

    @staticmethod
    def _fits(spent: int, opened: int, ceiling: int, budget: int) -> bool:
        total = spent + opened + ceiling
        return total <= INT64_MAX and total <= budget

    @staticmethod
    def _ratio_order(
        left: tuple[AccountSpec, int, int, int],
        right: tuple[AccountSpec, int, int, int],
    ) -> int:
        left_spec, left_spent, left_open, left_scopes = left
        right_spec, right_spent, right_open, right_scopes = right
        left_total = left_spent + left_open
        right_total = right_spent + right_open
        difference = left_total * right_spec.budget_nusd - right_total * left_spec.budget_nusd
        if difference:
            return -1 if difference < 0 else 1
        if left_scopes != right_scopes:
            return -1 if left_scopes < right_scopes else 1
        if left_spec.name != right_spec.name:
            return -1 if left_spec.name < right_spec.name else 1
        return 0

    def _reserve_sync(
        self,
        scope: Scope,
        attempt_id: UUID,
        quote: Quote,
        accounts: Sequence[AccountSpec],
        cert: str | None = None,
    ) -> Reservation:
        with closing(self._connect()) as connection:
            with _transaction(connection):
                _validate_quote(quote)
                if not accounts:
                    raise ValueError("at least one account is required")
                specs: dict[str, AccountSpec] = {}
                for spec in accounts:
                    if type(spec.budget_nusd) is not int or not 1 <= spec.budget_nusd <= INT64_MAX:
                        raise ValueError("invalid account budget")
                    previous = specs.get(spec.name)
                    if previous is not None and previous != spec:
                        raise _routing_error(
                            RouterErrorCode.ACCOUNT_CONFIG_CONFLICT,
                            account=spec.name,
                        )
                    specs[spec.name] = spec

                month_utc, created_at = _month_and_iso(self._clock)
                later = connection.execute(
                    "SELECT 1 FROM attempt WHERE month_utc > ? LIMIT 1", (month_utc,)
                ).fetchone()
                if later is not None:
                    raise _routing_error(RouterErrorCode.LEDGER_UNAVAILABLE)
                if connection.execute(
                    "SELECT 1 FROM attempt WHERE id = ?", (str(attempt_id),)
                ).fetchone():
                    raise _routing_error(
                        RouterErrorCode.DUPLICATE_ATTEMPT,
                        attempt_id=attempt_id,
                    )

                provider = scope.provider
                for spec in specs.values():
                    existing = connection.execute(
                        "SELECT key_env FROM account WHERE provider = ? AND name = ?",
                        (provider, spec.name),
                    ).fetchone()
                    if existing is not None and existing[0] != spec.key_env:
                        raise _routing_error(
                            RouterErrorCode.ACCOUNT_CONFIG_CONFLICT,
                            account=spec.name,
                        )
                    month_row = connection.execute(
                        """
                        SELECT budget_nusd FROM month
                        WHERE provider = ? AND name = ? AND month_utc = ?
                        """,
                        (provider, spec.name, month_utc),
                    ).fetchone()
                    if month_row is not None and month_row[0] != spec.budget_nusd:
                        raise _routing_error(
                            RouterErrorCode.ACCOUNT_CONFIG_CONFLICT,
                            account=spec.name,
                        )
                    connection.execute(
                        "INSERT OR IGNORE INTO account(provider, name, key_env) VALUES (?, ?, ?)",
                        (provider, spec.name, spec.key_env),
                    )

                binding = connection.execute(
                    """
                    SELECT account FROM binding
                    WHERE scope_kind = ? AND scope_id = ? AND provider = ?
                    """,
                    (scope.kind, str(scope.id), provider),
                ).fetchone()
                selected: AccountSpec | None = None
                if binding is not None:
                    bound_name = binding[0]
                    selected = specs.get(bound_name)
                    account_state = connection.execute(
                        "SELECT block_reason FROM account WHERE provider = ? AND name = ?",
                        (provider, bound_name),
                    ).fetchone()
                    if selected is None or account_state is None or account_state[0] is not None:
                        raise _routing_error(
                            RouterErrorCode.ACCOUNT_BLOCKED,
                            account=bound_name,
                            attempt_id=attempt_id,
                        )
                    settled, adjustments, opened, _ = self._spend_parts(
                        connection, provider, bound_name, month_utc
                    )
                    budget_row = connection.execute(
                        """
                        SELECT budget_nusd FROM month
                        WHERE provider = ? AND name = ? AND month_utc = ?
                        """,
                        (provider, bound_name, month_utc),
                    ).fetchone()
                    budget = selected.budget_nusd if budget_row is None else budget_row[0]
                    if not self._fits(settled + adjustments, opened, quote.ceiling_nusd, budget):
                        raise _routing_error(
                            RouterErrorCode.BUDGET_EXHAUSTED,
                            account=bound_name,
                            attempt_id=attempt_id,
                        )
                else:
                    candidates: list[tuple[AccountSpec, int, int, int]] = []
                    blocked_count = 0
                    for spec in specs.values():
                        account_state = connection.execute(
                            "SELECT block_reason FROM account WHERE provider = ? AND name = ?",
                            (provider, spec.name),
                        ).fetchone()
                        if account_state is None or account_state[0] is not None:
                            blocked_count += 1
                            continue
                        settled, adjustments, opened, scope_count = self._spend_parts(
                            connection, provider, spec.name, month_utc
                        )
                        spent = settled + adjustments
                        if self._fits(spent, opened, quote.ceiling_nusd, spec.budget_nusd):
                            candidates.append((spec, spent, opened, scope_count))
                    if not candidates:
                        if blocked_count == len(specs):
                            raise _routing_error(
                                RouterErrorCode.ACCOUNT_BLOCKED,
                                attempt_id=attempt_id,
                            )
                        raise _routing_error(
                            RouterErrorCode.BUDGET_EXHAUSTED,
                            attempt_id=attempt_id,
                        )
                    ordered = sorted(candidates, key=cmp_to_key(self._ratio_order))
                    pool = ordered
                    if cert is not None:
                        # Prefer an account that holds a live warm prefix for this credential
                        # and stays within nine tenths of its budget after this reservation.
                        warm_names = _warm_accounts(
                            connection, provider, cert, datetime.fromisoformat(created_at)
                        )
                        warm = [
                            entry
                            for entry in ordered
                            if entry[0].name in warm_names
                            and 10 * (entry[1] + entry[2] + quote.ceiling_nusd)
                            <= 9 * entry[0].budget_nusd
                        ]
                        if warm:
                            pool = warm
                    selected = pool[0][0]
                    connection.execute(
                        """
                        INSERT INTO binding(scope_kind, scope_id, provider, account)
                        VALUES (?, ?, ?, ?)
                        """,
                        (scope.kind, str(scope.id), provider, selected.name),
                    )

                connection.execute(
                    """
                    INSERT OR IGNORE INTO month(provider, name, month_utc, budget_nusd)
                    VALUES (?, ?, ?, ?)
                    """,
                    (provider, selected.name, month_utc, selected.budget_nusd),
                )
                connection.execute(
                    """
                    INSERT INTO attempt(
                        id, scope_kind, scope_id, provider, account, month_utc,
                        ceiling_nusd, price_revision, requested_model, request_digest,
                        state, cost_nusd, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, ?)
                    """,
                    (
                        str(attempt_id),
                        scope.kind,
                        str(scope.id),
                        provider,
                        selected.name,
                        month_utc,
                        quote.ceiling_nusd,
                        quote.price_revision,
                        quote.requested_model,
                        quote.request_digest,
                        created_at,
                    ),
                )
        return Reservation(attempt_id, scope, selected.name, month_utc, quote)

    async def reserve(
        self,
        scope: Scope,
        attempt_id: UUID,
        quote: Quote,
        accounts: Sequence[AccountSpec],
        *,
        cert: str | None = None,
    ) -> Reservation:
        """Reserve a quote and bind its scope before a provider request is sent.

        An unbound scope prefers an eligible account that holds a live warm prefix for
        ``cert``; a bound scope keeps its account and never reads warm-prefix state.
        """
        return await self._run(lambda: self._reserve_sync(scope, attempt_id, quote, accounts, cert))

    def _settle_sync(
        self,
        attempt_id: UUID,
        evidence: Evidence,
        cert: str | None = None,
        hour_after_key: bool = False,
        server_tools: bool = False,
    ) -> SettleResult:
        usage_json: str | None = None
        request_id: str | None = None
        cost_nusd: int
        outcome: str
        if isinstance(evidence, CompleteUsage):
            usage_json, request_id = _usage_json(evidence)
            cost_nusd = evidence.cost_nusd
            outcome = "complete"
        elif isinstance(evidence, ProvenNotSent):
            cost_nusd = 0
            outcome = "not_sent"
        elif isinstance(evidence, UnknownOutcome):
            outcome = _validate_reason(evidence.reason_code)
            cost_nusd = 0
        else:
            raise ValueError("invalid settlement evidence")

        with closing(self._connect()) as connection:
            conflict: tuple[str, str] | None = None
            result: SettleResult
            with _transaction(connection):
                row = connection.execute(
                    """
                    SELECT provider, account, month_utc, ceiling_nusd, state, cost_nusd,
                           usage_json, request_id, outcome, created_at
                    FROM attempt WHERE id = ?
                    """,
                    (str(attempt_id),),
                ).fetchone()
                if row is None:
                    raise LookupError(str(attempt_id))
                (
                    provider,
                    account,
                    month_utc,
                    ceiling,
                    state,
                    old_cost,
                    old_usage,
                    old_request,
                    old_outcome,
                    created_at,
                ) = row
                if isinstance(evidence, UnknownOutcome):
                    if state == "settled":
                        return "already_settled"
                    # The first reason recorded on an open attempt stands: a later write (a
                    # cancellation arriving after a rate limit was recorded) keeps it.
                    connection.execute(
                        "UPDATE attempt SET outcome = ? "
                        "WHERE id = ? AND state = 'open' AND outcome IS NULL",
                        (outcome, str(attempt_id)),
                    )
                    if outcome not in ("cancelled", "stream_closed"):
                        _clear_warm_prefix(connection, provider, account)
                    result = "still_open"
                elif state == "settled":
                    identical = (
                        old_outcome == outcome
                        and old_cost == cost_nusd
                        and old_usage == usage_json
                        and old_request == request_id
                    )
                    if identical:
                        return "already_settled"
                    connection.execute(
                        "UPDATE account SET block_reason = 'settlement_conflict' "
                        "WHERE provider = ? AND name = ?",
                        (provider, account),
                    )
                    _clear_warm_prefix(connection, provider, account)
                    conflict = (account, str(attempt_id))
                    result = "settled"
                else:
                    settled, adjustments, opened, _ = self._spend_parts(
                        connection, provider, account, month_utc
                    )
                    remaining_open = opened - ceiling
                    updated_settled = settled + cost_nusd
                    updated_spend = updated_settled + adjustments
                    if (
                        remaining_open < 0
                        or updated_settled > INT64_MAX
                        or updated_spend + remaining_open > INT64_MAX
                    ):
                        raise _routing_error(
                            RouterErrorCode.BUDGET_EXHAUSTED,
                            account=account,
                            attempt_id=attempt_id,
                        )
                    _, settled_at = _month_and_iso(self._clock)
                    connection.execute(
                        """
                        UPDATE attempt
                        SET state = 'settled', cost_nusd = ?, usage_json = ?, request_id = ?,
                            outcome = ?, settled_at = ?
                        WHERE id = ? AND state = 'open'
                        """,
                        (cost_nusd, usage_json, request_id, outcome, settled_at, str(attempt_id)),
                    )
                    if isinstance(evidence, CompleteUsage) and cost_nusd > ceiling:
                        connection.execute(
                            "UPDATE account SET block_reason = 'ceiling_exceeded' "
                            "WHERE provider = ? AND name = ?",
                            (provider, account),
                        )
                        _clear_warm_prefix(connection, provider, account)
                    elif isinstance(evidence, CompleteUsage):
                        if cert is not None:
                            _record_warm_prefix(
                                connection,
                                provider,
                                account,
                                cert,
                                evidence,
                                created_at,
                                hour_after_key,
                                server_tools,
                            )
                    elif isinstance(evidence, ProvenNotSent) and not evidence.caller_cancelled:
                        _clear_warm_prefix(connection, provider, account)
                    result = "settled"
            if conflict is not None:
                raise _routing_error(
                    RouterErrorCode.SETTLEMENT_CONFLICT,
                    account=conflict[0],
                    attempt_id=attempt_id,
                ) from None
        return result

    async def settle(
        self,
        attempt_id: UUID,
        evidence: Evidence,
        *,
        cert: str | None = None,
        hour_after_key: bool = False,
        server_tools: bool = False,
    ) -> SettleResult:
        """Apply evidence to an attempt using its stored account and month.

        With ``cert``, complete usage within the ceiling records or refreshes the
        account's warm prefix; ``hour_after_key`` and ``server_tools`` mark a request
        whose cached prefix length cannot be inferred from its token counts.
        """
        return await self._run(
            lambda: self._settle_sync(attempt_id, evidence, cert, hour_after_key, server_tools)
        )

    def _block_sync(self, provider: str, account: str, reason: str) -> None:
        reason = _validate_reason(reason)
        with closing(self._connect()) as connection:
            with _transaction(connection):
                cursor = connection.execute(
                    "UPDATE account SET block_reason = ? WHERE provider = ? AND name = ?",
                    (reason, provider, account),
                )
                if cursor.rowcount == 0:
                    raise LookupError(account)
                _clear_warm_prefix(connection, provider, account)

    async def block(self, provider: str, account: str, reason: str) -> None:
        """Mark an existing account unavailable for new reservations."""
        await self._run(lambda: self._block_sync(provider, account, reason))

    @staticmethod
    def _adjustment_row(connection: sqlite3.Connection, adjustment_id: UUID) -> tuple | None:
        return connection.execute(
            """
            SELECT kind, attempt_id, provider, account, month_utc, delta_nusd, source_ref
            FROM adjustment WHERE id = ?
            """,
            (str(adjustment_id),),
        ).fetchone()

    @staticmethod
    def _insert_adjustment(
        connection: sqlite3.Connection,
        adjustment_id: UUID,
        kind: str,
        attempt_id: UUID | None,
        provider: str,
        account: str,
        month_utc: str | None,
        delta_nusd: int,
        source_ref: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO adjustment(
                id, kind, attempt_id, provider, account, month_utc,
                delta_nusd, source_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(adjustment_id),
                kind,
                None if attempt_id is None else str(attempt_id),
                provider,
                account,
                month_utc,
                delta_nusd,
                source_ref,
                created_at,
            ),
        )

    def _reconcile_sync(
        self,
        adjustment_id: UUID,
        attempt_id: UUID,
        cost_nusd: int,
        source_ref: str,
    ) -> None:
        cost_nusd = _checked_int(cost_nusd, minimum=0, maximum=INT64_MAX, label="cost")
        source_ref = _validate_source(source_ref)
        with closing(self._connect()) as connection:
            with _transaction(connection):
                attempt = connection.execute(
                    """
                    SELECT provider, account, month_utc, state, cost_nusd, outcome
                    FROM attempt WHERE id = ?
                    """,
                    (str(attempt_id),),
                ).fetchone()
                if attempt is None:
                    raise LookupError(str(attempt_id))
                provider, account, month_utc, state, old_cost, old_outcome = attempt
                expected = (
                    "reconciliation",
                    str(attempt_id),
                    provider,
                    account,
                    month_utc,
                    0,
                    source_ref,
                )
                existing = self._adjustment_row(connection, adjustment_id)
                if existing is not None:
                    if (
                        existing == expected
                        and old_outcome == "reconciled"
                        and old_cost == cost_nusd
                    ):
                        return
                    raise _routing_error(
                        RouterErrorCode.SETTLEMENT_CONFLICT,
                        account=account,
                        attempt_id=attempt_id,
                    )
                if state != "open":
                    raise _routing_error(
                        RouterErrorCode.SETTLEMENT_CONFLICT,
                        account=account,
                        attempt_id=attempt_id,
                    )
                settled, adjustments, opened, _ = self._spend_parts(
                    connection, provider, account, month_utc
                )
                attempt_ceiling = connection.execute(
                    "SELECT ceiling_nusd FROM attempt WHERE id = ?", (str(attempt_id),)
                ).fetchone()[0]
                remaining_open = opened - attempt_ceiling
                updated_settled = settled + cost_nusd
                updated_spend = updated_settled + adjustments
                if (
                    remaining_open < 0
                    or updated_settled > INT64_MAX
                    or updated_spend + remaining_open > INT64_MAX
                ):
                    raise _routing_error(
                        RouterErrorCode.BUDGET_EXHAUSTED,
                        account=account,
                        attempt_id=attempt_id,
                    )
                _, settled_at = _month_and_iso(self._clock)
                connection.execute(
                    """
                    UPDATE attempt SET state = 'settled', cost_nusd = ?, usage_json = NULL,
                        request_id = NULL, outcome = 'reconciled', settled_at = ?
                    WHERE id = ? AND state = 'open'
                    """,
                    (cost_nusd, settled_at, str(attempt_id)),
                )
                self._insert_adjustment(
                    connection,
                    adjustment_id,
                    "reconciliation",
                    attempt_id,
                    provider,
                    account,
                    month_utc,
                    0,
                    source_ref,
                    settled_at,
                )

    async def reconcile(
        self,
        adjustment_id: UUID,
        attempt_id: UUID,
        cost_nusd: int,
        source_ref: str,
    ) -> None:
        """Settle an open attempt and record its source reference atomically."""
        await self._run(
            lambda: self._reconcile_sync(adjustment_id, attempt_id, cost_nusd, source_ref)
        )

    def _account_exists(self, connection: sqlite3.Connection, provider: str, account: str) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM account WHERE provider = ? AND name = ?", (provider, account)
            ).fetchone()
            is None
        ):
            raise LookupError(account)

    def _adjust_sync(
        self,
        adjustment_id: UUID,
        provider: str,
        account: str,
        month_utc: str,
        delta_nusd: int,
        source_ref: str,
        attempt_id: UUID | None,
    ) -> None:
        month_utc = _validate_month(month_utc)
        delta_nusd = _checked_int(
            delta_nusd, minimum=_INT64_MIN, maximum=INT64_MAX, label="adjustment"
        )
        source_ref = _validate_source(source_ref)
        with closing(self._connect()) as connection:
            with _transaction(connection):
                expected = (
                    "correction",
                    None if attempt_id is None else str(attempt_id),
                    provider,
                    account,
                    month_utc,
                    delta_nusd,
                    source_ref,
                )
                existing = self._adjustment_row(connection, adjustment_id)
                if existing is not None:
                    if existing == expected:
                        return
                    raise _routing_error(
                        RouterErrorCode.SETTLEMENT_CONFLICT,
                        account=account,
                        attempt_id=attempt_id,
                    )
                self._account_exists(connection, provider, account)
                settled, adjustments, opened, _ = self._spend_parts(
                    connection, provider, account, month_utc
                )
                updated_adjustments = adjustments + delta_nusd
                updated_spend = settled + updated_adjustments
                if updated_spend < 0:
                    raise ValueError("adjustment would make settled spend negative")
                if (
                    not _INT64_MIN <= updated_adjustments <= INT64_MAX
                    or updated_spend + opened > INT64_MAX
                ):
                    raise ValueError("adjustment exceeds signed 64-bit range")
                _, created_at = _month_and_iso(self._clock)
                self._insert_adjustment(
                    connection,
                    adjustment_id,
                    "correction",
                    attempt_id,
                    provider,
                    account,
                    month_utc,
                    delta_nusd,
                    source_ref,
                    created_at,
                )

    async def adjust(
        self,
        adjustment_id: UUID,
        provider: str,
        account: str,
        month_utc: str,
        delta_nusd: int,
        source_ref: str,
        attempt_id: UUID | None = None,
    ) -> None:
        """Record a sourced correction for one account and UTC month."""
        await self._run(
            lambda: self._adjust_sync(
                adjustment_id,
                provider,
                account,
                month_utc,
                delta_nusd,
                source_ref,
                attempt_id,
            )
        )

    def _clear_block_sync(
        self,
        adjustment_id: UUID,
        provider: str,
        account: str,
        source_ref: str,
    ) -> None:
        source_ref = _validate_source(source_ref)
        with closing(self._connect()) as connection:
            with _transaction(connection):
                self._account_exists(connection, provider, account)
                expected = ("unblock", None, provider, account, None, 0, source_ref)
                existing = self._adjustment_row(connection, adjustment_id)
                if existing is not None:
                    if existing == expected:
                        return
                    raise _routing_error(RouterErrorCode.SETTLEMENT_CONFLICT, account=account)
                _, created_at = _month_and_iso(self._clock)
                self._insert_adjustment(
                    connection,
                    adjustment_id,
                    "unblock",
                    None,
                    provider,
                    account,
                    None,
                    0,
                    source_ref,
                    created_at,
                )
                blocked = connection.execute(
                    "SELECT block_reason FROM account WHERE provider = ? AND name = ?",
                    (provider, account),
                ).fetchone()
                connection.execute(
                    "UPDATE account SET block_reason = NULL WHERE provider = ? AND name = ?",
                    (provider, account),
                )
                if blocked is not None and blocked[0] is not None:
                    _clear_warm_prefix(connection, provider, account)

    async def clear_block(
        self,
        adjustment_id: UUID,
        provider: str,
        account: str,
        source_ref: str,
    ) -> None:
        """Clear an account mark and persist the source reference."""
        await self._run(
            lambda: self._clear_block_sync(adjustment_id, provider, account, source_ref)
        )

    def _status_sync(self, provider: str, month_utc: str | None) -> tuple[AccountStatus, ...]:
        with closing(self._connect()) as connection:
            if month_utc is None:
                month_utc, _ = _month_and_iso(self._clock)
            else:
                month_utc = _validate_month(month_utc)
            accounts = connection.execute(
                "SELECT name, block_reason FROM account WHERE provider = ? ORDER BY name",
                (provider,),
            ).fetchall()
            statuses: list[AccountStatus] = []
            for name, block_reason in accounts:
                budget_row = connection.execute(
                    """
                    SELECT budget_nusd FROM month
                    WHERE provider = ? AND name = ? AND month_utc = ?
                    """,
                    (provider, name, month_utc),
                ).fetchone()
                settled, adjustments, opened, _ = self._spend_parts(
                    connection, provider, name, month_utc
                )
                if (
                    not _INT64_MIN <= settled <= INT64_MAX
                    or not _INT64_MIN <= adjustments <= INT64_MAX
                    or not 0 <= opened <= INT64_MAX
                    or settled + adjustments > INT64_MAX
                    or settled + adjustments + opened > INT64_MAX
                ):
                    raise _routing_error(RouterErrorCode.LEDGER_UNAVAILABLE, account=name)
                statuses.append(
                    AccountStatus(
                        provider,
                        name,
                        month_utc,
                        None if budget_row is None else budget_row[0],
                        settled,
                        adjustments,
                        opened,
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM attempt
                            WHERE provider = ? AND account = ? AND month_utc = ?
                              AND state = 'open'
                            """,
                            (provider, name, month_utc),
                        ).fetchone()[0],
                        block_reason,
                    )
                )
            return tuple(statuses)

    async def status(
        self,
        provider: str,
        month_utc: str | None = None,
    ) -> tuple[AccountStatus, ...]:
        """Return accounting totals for every account of a provider."""
        return await self._run(lambda: self._status_sync(provider, month_utc))
