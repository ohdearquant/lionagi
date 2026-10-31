# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The router hands the ledger the warm-prefix evidence of each routed request.

Covers the cache point a request is certified by, whether a one-hour mark follows it, the
settlements that carry that evidence to the ledger, and the cancellation arms of ``begin``.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import anyio
import pytest
from pydantic import SecretStr

import lionagi.service.accounts._router as router_module
from lionagi.service.accounts._ledger import CredentialLedger
from lionagi.service.accounts._pricing import CURRENT_REVISION
from lionagi.service.accounts._router import AccountRouter, RouterAttempt
from lionagi.service.accounts._types import (
    AccountRoutingError,
    AccountSpec,
    CompleteUsage,
    ProvenNotSent,
    Quote,
    Reservation,
    RouterErrorCode,
    Scope,
    UnknownOutcome,
)
from lionhub.providers.anthropic._prompt_cache import place, points

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
MODEL = "claude-sonnet-5-5"
BUDGET = 10**15
ALPHA = AccountSpec("alpha", "ALPHA_KEY", BUDGET)
BETA = AccountSpec("beta", "BETA_KEY", BUDGET)
HOUR_MARK = {"type": "ephemeral", "ttl": "1h"}
FIVE_MARK = {"type": "ephemeral"}


def scope() -> Scope:
    return Scope("branch", uuid4(), "anthropic")


def text(value: str, mark: dict | None = None) -> dict:
    block = {"type": "text", "text": value}
    if mark is not None:
        block["cache_control"] = dict(mark)
    return block


def tool(name: str, mark: dict | None = None) -> dict:
    block = {"name": name, "description": "d", "input_schema": {"type": "object", "properties": {}}}
    if mark is not None:
        block["cache_control"] = dict(mark)
    return block


def user(content) -> dict:
    return {"role": "user", "content": content}


def request(*, system=None, tools=None, messages=None, **extra) -> dict:
    payload = {
        "model": MODEL,
        "max_tokens": 32,
        "messages": messages if messages is not None else [user("A short request")],
    }
    if system is not None:
        payload["system"] = system
    if tools is not None:
        payload["tools"] = tools
    payload.update(extra)
    return payload


def cert_at(payload: dict, index: int | None) -> str | None:
    if index is None:
        return None
    return points(payload)[0][index].cert


def response_body(read: int, write: int) -> dict:
    return {
        "id": "msg_test",
        "model": MODEL,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 0,
                "ephemeral_1h_input_tokens": write,
            },
        },
    }


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "ledger.sqlite3"
        self.ledger = CredentialLedger(self.path, clock=lambda: NOW)
        self.router = AccountRouter(
            sources=None,
            ledger=self.ledger,
            accounts=(ALPHA, BETA),
            revision=CURRENT_REVISION,
        )


async def make_env(monkeypatch, tmp_path: Path, resolve=None) -> Env:
    """A router over a ledger that holds both accounts with nothing spent or open.

    Key lookup and request validation are the router's own boundaries to the environment and the
    endpoint; both are replaced so the tests reach neither.
    """
    env = Env(tmp_path)
    quote = Quote("model-a", 1, "price-a", "a" * 64)
    first = await env.ledger.reserve(scope(), uuid4(), quote, (ALPHA, BETA))
    await env.ledger.settle(first.attempt_id, ProvenNotSent())

    async def fake_resolve(key_env, sources):
        if resolve is not None:
            return await resolve()
        return SecretStr("test-key-0123456789")

    monkeypatch.setattr(router_module, "resolve_account_key", fake_resolve)
    monkeypatch.setattr(router_module, "validate_routed_request", lambda *a, **k: None)
    return env


async def begin(router: AccountRouter, payload: dict) -> RouterAttempt:
    return await router.begin(
        scope(), payload, [], endpoint=None, request_kwargs={}, body_kwargs={}
    )


def put_warm(path: Path, account: str, cert: str, stable: int = 6) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO warm_prefix(provider, account, cert, stable_tokens, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("anthropic", account, cert, stable, (NOW + HOUR).isoformat()),
        )
        connection.commit()


def warm_rows(path: Path) -> dict[tuple[str, str], tuple[object, object]]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            (row[0], row[1]): (row[2], row[3])
            for row in connection.execute(
                "SELECT account, cert, stable_tokens, expires_at FROM warm_prefix"
            )
        }


RULES = [text("rules", HOUR_MARK)]
NESTED_RESULT = [
    {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": [text("result", HOUR_MARK)],
    }
]
DOCUMENT = [
    {
        "type": "document",
        "source": {"type": "content", "content": [text("body", HOUR_MARK)]},
    }
]


# --- the cache point a request is certified by -----------------------------------------------

KEY_CASES = [
    pytest.param(request(system=[text("a", HOUR_MARK), text("b", HOUR_MARK)]), 1, id="last-of-two"),
    pytest.param(
        request(system=[text("a", HOUR_MARK), text("b", FIVE_MARK)]), 0, id="hour-below-five-minute"
    ),
    pytest.param(request(system=[text(" ", HOUR_MARK)]), None, id="blank-system-text"),
    pytest.param(
        request(tools=[tool("t1", HOUR_MARK)], system="rules"), 0, id="string-system-tool-key"
    ),
    pytest.param(request(system=[text("a", FIVE_MARK)]), None, id="no-hour-mark"),
    pytest.param(
        request(messages=[user([text("q", HOUR_MARK)])]), None, id="hour-mark-only-in-messages"
    ),
]


@pytest.mark.parametrize(("payload", "index"), KEY_CASES)
async def test_begin_certifies_the_last_hour_mark_on_tools_or_system(
    monkeypatch, tmp_path, payload, index
):
    env = await make_env(monkeypatch, tmp_path)

    attempt = await begin(env.router, payload)

    assert attempt.cert == cert_at(payload, index)


async def test_a_different_system_text_has_a_different_cert(monkeypatch, tmp_path):
    env = await make_env(monkeypatch, tmp_path)

    first = await begin(env.router, request(system=[text("a", HOUR_MARK)]))
    second = await begin(env.router, request(system=[text("b", HOUR_MARK)]))

    assert first.cert is not None and second.cert is not None
    assert first.cert != second.cert


async def test_begin_certifies_the_block_the_placement_marks_for_an_hour(monkeypatch, tmp_path):
    env = await make_env(monkeypatch, tmp_path)
    payload = request(tools=[tool("t1")], system=[text("rules")])
    place(payload, None)
    keys = [
        p
        for p in points(payload)[0]
        if p.tier in ("tools", "system") and p.markable and p.mark == "1h"
    ]
    assert keys

    attempt = await begin(env.router, payload)

    assert attempt.cert == keys[-1].cert


# --- a one-hour mark after the key ------------------------------------------------------------

AFTER_CASES = [
    pytest.param(
        request(system=RULES, messages=[user([text("q", HOUR_MARK)])]), True, id="message"
    ),
    pytest.param(request(system=RULES, messages=[user(NESTED_RESULT)]), True, id="tool-result"),
    pytest.param(request(system=RULES), False, id="none"),
    pytest.param(
        request(system=RULES, messages=[user([text("q", FIVE_MARK)])]), False, id="five-minute"
    ),
    pytest.param(
        request(system=RULES, cache_control=dict(HOUR_MARK)), True, id="top-level-with-later-block"
    ),
    pytest.param(
        request(system=RULES, messages=[user(" ")], cache_control=dict(HOUR_MARK)),
        False,
        id="top-level-key-is-last",
    ),
    pytest.param(
        request(system=RULES, cache_control=dict(FIVE_MARK)), False, id="top-level-five-minute"
    ),
    pytest.param(
        request(tools=[tool("a", HOUR_MARK), tool("b", FIVE_MARK)]), False, id="later-tool"
    ),
]


@pytest.mark.parametrize(("payload", "expected"), AFTER_CASES)
async def test_begin_reports_a_later_hour_mark(monkeypatch, tmp_path, payload, expected):
    env = await make_env(monkeypatch, tmp_path)

    attempt = await begin(env.router, payload)

    assert attempt.hour_after_key is expected


async def test_a_one_hour_mark_inside_a_document_source_counts_as_later():
    parsed = request(system=RULES, messages=[user(DOCUMENT)])

    cert, later = router_module._warm_evidence(parsed)

    assert cert == cert_at(parsed, 0)
    assert later is True


async def test_a_priced_request_has_no_server_tools(monkeypatch, tmp_path):
    env = await make_env(monkeypatch, tmp_path)

    attempt = await begin(env.router, request(system=RULES))

    assert attempt.server_tools is False


async def test_an_attempt_built_with_four_arguments_has_no_evidence():
    quote = Quote("model-a", 1, "price-a", "a" * 64)
    reservation = Reservation(uuid4(), scope(), "alpha", "2026-01", quote)

    attempt = RouterAttempt(reservation, b"{}", frozenset(), SecretStr("test-key-0123456789"))

    assert (attempt.cert, attempt.hour_after_key, attempt.server_tools) == (None, False, False)


# --- the evidence reaches the ledger ----------------------------------------------------------

WRITE_CASES = [
    pytest.param(request(system=RULES), 120, id="no-later-mark"),
    pytest.param(request(system=RULES, messages=[user(NESTED_RESULT)]), None, id="later-mark"),
]


@pytest.mark.parametrize(("payload", "stable"), WRITE_CASES)
async def test_a_response_with_an_hour_write_records_the_stable_prefix(
    monkeypatch, tmp_path, payload, stable
):
    env = await make_env(monkeypatch, tmp_path)
    attempt = await begin(env.router, payload)

    await env.router.settle_response(attempt, response_body(read=20, write=100))

    assert warm_rows(env.path)[(attempt.account, attempt.cert)][0] == stable


@pytest.mark.parametrize(("payload", "stable"), WRITE_CASES)
async def test_a_stream_end_records_the_same_row_as_a_response(
    monkeypatch, tmp_path, payload, stable
):
    env = await make_env(monkeypatch, tmp_path)
    attempt = await begin(env.router, payload)

    await env.router.settle_stream_end(attempt, CompleteUsage(MODEL, 10, 20, 0, 100, 5, 1, None))

    assert warm_rows(env.path)[(attempt.account, attempt.cert)][0] == stable


async def test_begin_binds_the_account_warm_for_the_cert(monkeypatch, tmp_path):
    env = await make_env(monkeypatch, tmp_path)
    payload = request(system=RULES)
    put_warm(env.path, "beta", cert_at(payload, 0))

    attempt = await begin(env.router, payload)

    assert attempt.account == "beta"


async def test_a_request_without_an_hour_mark_keeps_the_pace_order(monkeypatch, tmp_path):
    env = await make_env(monkeypatch, tmp_path)
    payload = request(system=[text("rules", FIVE_MARK)])
    put_warm(env.path, "beta", "unrelated-cert")

    attempt = await begin(env.router, payload)

    assert attempt.account == "alpha"


# --- what the settlements pass on -------------------------------------------------------------


class SpyLedger:
    """Records ledger calls; each one takes a moment so a cancelled caller shows."""

    def __init__(self) -> None:
        self.settled: list[tuple[object, dict]] = []
        self.blocked: list[tuple[str, str, str]] = []
        self.finished = 0

    async def settle(self, attempt_id, evidence, **kwargs):
        await anyio.sleep(0.02)
        self.settled.append((evidence, kwargs))
        self.finished += 1

    async def block(self, provider, account, reason):
        await anyio.sleep(0.02)
        self.blocked.append((provider, account, reason))
        self.finished += 1


def spy_router() -> tuple[AccountRouter, SpyLedger, RouterAttempt]:
    spy = SpyLedger()
    router = AccountRouter(
        sources=None, ledger=spy, accounts=(ALPHA, BETA), revision=CURRENT_REVISION
    )
    quote = Quote(MODEL, 1, "price-a", "a" * 64)
    reservation = Reservation(uuid4(), scope(), "alpha", "2026-01", quote)
    attempt = RouterAttempt(
        reservation,
        b"{}",
        frozenset({"1h"}),
        SecretStr("test-key-0123456789"),
        cert="c1",
        hour_after_key=True,
        server_tools=True,
    )
    return router, spy, attempt


EVIDENCE = {"cert": "c1", "hour_after_key": True, "server_tools": True}


async def test_settle_response_passes_the_attempt_evidence():
    router, spy, attempt = spy_router()

    await router.settle_response(attempt, response_body(read=0, write=0))

    ((evidence, kwargs),) = spy.settled
    assert isinstance(evidence, CompleteUsage)
    assert kwargs == EVIDENCE


async def test_settle_stream_end_passes_the_attempt_evidence():
    router, spy, attempt = spy_router()

    await router.settle_stream_end(attempt, CompleteUsage(MODEL, 1, 0, 0, 0, 1, 1, None))

    ((_, kwargs),) = spy.settled
    assert kwargs == EVIDENCE


async def test_settle_unknown_passes_no_evidence():
    router, spy, attempt = spy_router()

    await router.settle_unknown(attempt, "rate_limited")

    assert spy.settled == [(UnknownOutcome("rate_limited"), {})]


@pytest.mark.parametrize(("given", "expected"), [({}, False), ({"caller_cancelled": True}, True)])
async def test_settle_not_sent_forwards_who_cancelled(given, expected):
    router, spy, attempt = spy_router()

    await router.settle_not_sent(attempt, **given)
    await router.settle_reservation_not_sent(attempt.reservation, **given)

    assert spy.settled == [(ProvenNotSent(expected), {})] * 2


SHIELDED = {
    "response": lambda router, attempt: router.settle_response(attempt, response_body(0, 0)),
    "unknown": lambda router, attempt: router.settle_unknown(attempt, "rate_limited"),
    "block": lambda router, attempt: router.block(attempt, "auth_required"),
    "stream_end": lambda router, attempt: router.settle_stream_end(
        attempt, CompleteUsage(MODEL, 1, 0, 0, 0, 1, 1, None)
    ),
}


@pytest.mark.parametrize("name", sorted(SHIELDED))
async def test_a_cancelled_caller_still_commits_the_ledger_write(name):
    router, spy, attempt = spy_router()

    with anyio.CancelScope() as outer:
        outer.cancel()
        await SHIELDED[name](router, attempt)
        await anyio.sleep(0)

    assert spy.finished == 1
    assert outer.cancelled_caught


# --- two routers on one ledger file -----------------------------------------------------------


async def test_two_routers_on_one_ledger_file_share_the_warm_rows(monkeypatch, tmp_path):
    env = await make_env(monkeypatch, tmp_path)
    other = AccountRouter(
        sources=None,
        ledger=CredentialLedger(env.path, clock=lambda: NOW),
        accounts=(ALPHA, BETA),
        revision=CURRENT_REVISION,
    )
    payload = request(system=RULES)
    cert = cert_at(payload, 0)
    put_warm(env.path, "beta", cert)

    first = await begin(env.router, payload)
    second = await begin(other, payload)
    await other.settle_unknown(first, "rate_limited")
    cleared = ("beta", cert) not in warm_rows(env.path)
    third = await begin(env.router, payload)

    assert (first.account, second.account) == ("beta", "beta")
    assert cleared
    assert third.account == "alpha"


# --- begin when the key cannot be resolved ----------------------------------------------------


async def seeded_env(monkeypatch, tmp_path, resolve):
    env = await make_env(monkeypatch, tmp_path, resolve)
    payload = request(system=RULES)
    cert = cert_at(payload, 0)
    put_warm(env.path, "alpha", cert)
    return env, payload, cert


async def test_a_generator_exit_while_resolving_keeps_the_warm_rows(monkeypatch, tmp_path):
    async def resolve():
        raise GeneratorExit

    env, payload, cert = await seeded_env(monkeypatch, tmp_path, resolve)

    with pytest.raises(GeneratorExit):
        await begin(env.router, payload)

    assert ("alpha", cert) in warm_rows(env.path)


async def test_a_cancellation_while_resolving_keeps_the_warm_rows(monkeypatch, tmp_path):
    started = asyncio.Event()

    async def resolve():
        started.set()
        await asyncio.sleep(3600)

    env, payload, cert = await seeded_env(monkeypatch, tmp_path, resolve)
    task = asyncio.ensure_future(begin(env.router, payload))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert ("alpha", cert) in warm_rows(env.path)


async def test_a_failure_while_resolving_clears_the_warm_rows(monkeypatch, tmp_path):
    async def resolve():
        raise RuntimeError("lookup failed")

    env, payload, cert = await seeded_env(monkeypatch, tmp_path, resolve)

    with pytest.raises(RuntimeError):
        await begin(env.router, payload)

    assert ("alpha", cert) not in warm_rows(env.path)


async def test_a_refused_key_clears_the_warm_rows(monkeypatch, tmp_path):
    async def resolve():
        raise AccountRoutingError(RouterErrorCode.CREDENTIAL_CONFLICT)

    env, payload, cert = await seeded_env(monkeypatch, tmp_path, resolve)

    with pytest.raises(AccountRoutingError):
        await begin(env.router, payload)

    assert ("alpha", cert) not in warm_rows(env.path)
