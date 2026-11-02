# SPDX-License-Identifier: Apache-2.0
"""A routed Anthropic request rejection records its class and never provider text.

Both attempts run through the real endpoint with a stub transport, as in
``test_routed_endpoint``: a test marked ``STREAMED`` runs through the non-streamed and the
streamed attempt.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing, suppress
from pathlib import Path

import anyio
import anyio.lowlevel
import pytest

import lionagi
import lionagi.service.accounts._router as router_module
from lionagi.service.accounts._types import (
    _REJECTION_CLASSES,
    AccountRoutingError,
    RouterErrorCode,
)
from lionagi.service.connections.endpoint import Endpoint
from lionhub.providers.anthropic import messages as messages_module
from tests.service.accounts.test_routed_endpoint import (
    DEADLINE,
    StubResponse,
    _answer_late,
    _attempts,
    _block_reason,
    _install_accounts,
    _model,
    _prime,
    _send,
    _warm_rows,
)

STREAMED = pytest.mark.parametrize("streamed", [False, True], ids=["call", "stream"])
# The hang guard of a test that patches the body read bound down: below the default bound, so
# only the patched bound ends the read in time, and far above the patched one, so the store
# write after the read fits on a loaded test worker.
READ_HANG_GUARD = 2.0

INVALID = "invalid_request_error"
PROMPT = "prompt is too long"
SENTINEL = "zz-sentinel-8e51c07d"
OWN_FILE = Path(__file__).resolve()
# The directories of the package's own code: the sentinel check reads the frames they own.
PACKAGE_DIRS = (
    Path(lionagi.__file__).resolve().parent,
    Path(messages_module.__file__).resolve().parents[3],
)


@pytest.fixture(autouse=True)
def _reset_router_after_test():
    yield
    router_module._reset_router_test_dependencies()


def _envelope(error) -> bytes:
    return json.dumps({"type": "error", "error": error}).encode()


def _typed(message) -> bytes:
    """A 400 body carrying the documented error type and ``message``."""
    return _envelope({"type": INVALID, "message": message})


def _body_of(size: int) -> bytes:
    """A body of exactly ``size`` bytes whose message names the first class."""
    body = _typed(PROMPT + "x" * (size - len(_typed(PROMPT))))
    assert len(body) == size
    return body


class _Rejection(StubResponse):
    """A rejection as the HTTP client returns it.

    The whole body is ``content``, as bytes, for the non-streamed attempt. For the streamed
    attempt ``aiter_bytes`` yields the body in pieces of ``piece_size`` bytes, each after a
    checkpoint, and ``yielded`` counts the pieces handed out.
    """

    def __init__(self, status: int, body: bytes, piece_size: int = 4096) -> None:
        super().__init__(status)
        self.content = body
        self.piece_size = piece_size
        self.yielded = 0

    async def aiter_bytes(self):
        for start in range(0, len(self.content), self.piece_size):
            await anyio.lowlevel.checkpoint()
            self.yielded += 1
            yield self.content[start : start + self.piece_size]


class _HangingRejection(_Rejection):
    """A rejection whose body stream never finishes: it waits for an event that is never set."""

    entered = False

    async def aiter_bytes(self):
        self.entered = True
        await anyio.Event().wait()
        yield b""


class _LateRejection(_Rejection):
    """A rejection whose whole body arrives ``delay`` seconds after the read starts."""

    delay = 0.3

    async def aiter_bytes(self):
        await anyio.sleep(self.delay)
        yield self.content


class _CancellingRejection(_Rejection):
    """A rejection whose body stream cancels ``scope`` after its first piece.

    The cancellation is pending at the checkpoint that follows, before the rest of the body is
    yielded. ``scope`` is set by the test.
    """

    scope: anyio.CancelScope | None = None

    async def aiter_bytes(self):
        yield self.content[:10]
        self.scope.cancel()
        await anyio.lowlevel.checkpoint()
        yield self.content[10:]


class _BrokenRejection(_Rejection):
    """A rejection whose body stream fails after its first piece with an error naming the body."""

    async def aiter_bytes(self):
        yield self.content[:10]
        raise RuntimeError(self.content.decode())


def _rejected(body: bytes) -> StubResponse:
    return _Rejection(400, body)


def _rejection_columns(path) -> list[tuple]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT outcome, provider_error_type, rejection_class FROM attempt ORDER BY rowid"
        ).fetchall()


def _every_ledger_value(path) -> list[str]:
    """The text of every value of every row of every table in the ledger file."""
    values = []
    with closing(sqlite3.connect(path)) as connection:
        names = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        for name in names:
            for row in connection.execute(f'SELECT * FROM "{name}"'):
                values.extend(repr(value) for value in row)
    return values


async def _reject(monkeypatch, tmp_path, body: bytes, streamed: bool):
    """Send one request answered with a 400 carrying ``body``: the ledger path, transport, error."""
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_rejected(body)])
    with pytest.raises(AccountRoutingError) as caught:
        await _send(model, streamed)
    return path, transport, caught.value


def _assert_recorded(path, error, error_type: str, rejection_class: str) -> None:
    assert error.code is RouterErrorCode.REQUEST_REJECTED
    assert error.http_status == 400
    assert (error.provider_error_type, error.rejection_class) == (error_type, rejection_class)
    assert _rejection_columns(path) == [("request_rejected", error_type, rejection_class)]


def _assert_no_sentinel(error, caplog, path) -> None:
    """The sentinel is in none of the places a provider message could be kept."""
    chain, pending = [], [error]
    while pending:
        exc = pending.pop()
        if exc is None or any(exc is seen for seen in chain):
            continue
        chain.append(exc)
        pending.extend([exc.__context__, exc.__cause__])
    for exc in chain:
        assert SENTINEL not in str(exc), type(exc)
        assert SENTINEL not in repr(exc.args), type(exc)
    for record in caplog.records:
        assert SENTINEL not in record.getMessage(), record.name
        assert SENTINEL not in repr(record.args), record.name
    for value in _every_ledger_value(path):
        assert SENTINEL not in value
    needle = SENTINEL.encode()
    for exc in chain:
        trace = exc.__traceback__
        while trace is not None:
            frame, trace = trace.tb_frame, trace.tb_next
            filename = Path(frame.f_code.co_filename).resolve()
            if filename == OWN_FILE or not any(root in filename.parents for root in PACKAGE_DIRS):
                continue
            for name, value in frame.f_locals.items():
                where = (filename.name, frame.f_code.co_name, name)
                if isinstance(value, str):
                    assert SENTINEL not in value, where
                elif isinstance(value, bytes):
                    assert needle not in value, where


# Every message wording the provider documents for each class, the classes in table order.
CLASS_MESSAGES = (
    ("prompt_too_long", (PROMPT,)),
    (
        "tool_result_missing",
        (
            "tool_use ids were found without tool_result blocks immediately after",
            "was found without a corresponding",
        ),
    ),
    ("prefill_unsupported", ("does not support assistant message prefill",)),
    (
        "thinking_config_unsupported",
        (
            '"thinking.type.enabled" is not supported',
            '"thinking.type.disabled" is not supported',
            '"thinking.type.between_tools" is not supported',
            "adaptive thinking is not supported on this model",
            "is not supported when thinking is disabled on this model",
            "effort cannot change when thinking is disabled",
        ),
    ),
    (
        "thinking_block_invalid",
        (
            "blocks in the latest assistant message cannot be modified",
            "The block is bound to a different conversation",
        ),
    ),
    (
        "tool_choice_unsupported",
        ('tool_choice: type "tool" and "any" are not supported for this model',),
    ),
    ("tool_type_unsupported", ("does not support tool types:",)),
    ("image_too_large", ("exceed the maximum image size", "many-image requests")),
)
WORDINGS = [(name, text) for name, texts in CLASS_MESSAGES for text in texts]
WORDING_IDS = [
    f"{name}_{number}" for name, texts in CLASS_MESSAGES for number, _ in enumerate(texts, 1)
]
FIRST_WORDING = {name: texts[0] for name, texts in CLASS_MESSAGES}
ORDERED_PAIRS = [
    (earlier, later)
    for index, (earlier, _) in enumerate(CLASS_MESSAGES)
    for later, _ in CLASS_MESSAGES[index + 1 :]
]
# Every wording of an earlier row with every wording of each later row, as positions in WORDINGS.
WORDING_INDEX_PAIRS = [
    (first, second)
    for first, (earlier, _) in enumerate(WORDINGS)
    for second, (later, _) in enumerate(WORDINGS)
    if first < second and earlier != later
]
# (earlier class, a wording of the earlier row, a wording of the later row) and the test ids.
WORDING_PAIRS = [
    (WORDINGS[first][0], WORDINGS[first][1], WORDINGS[second][1])
    for first, second in WORDING_INDEX_PAIRS
]
WORDING_PAIR_IDS = [
    f"{WORDING_IDS[first]}-before-{WORDING_IDS[second]}" for first, second in WORDING_INDEX_PAIRS
]

# Bodies that fall to ``other``: (id, body, provider_error_type, rejection_class).
OTHER_BODIES = [
    ("unknown_message", _typed("an unrelated rejection"), INVALID, "other"),
    ("message_in_another_case", _typed(PROMPT.upper()), INVALID, "other"),
    ("no_message", _envelope({"type": INVALID}), INVALID, "other"),
    ("message_not_text", _envelope({"type": INVALID, "message": [PROMPT]}), INVALID, "other"),
    (
        "unknown_type",
        _envelope({"type": "made_up_error", "message": PROMPT}),
        "other",
        "prompt_too_long",
    ),
    (
        "type_not_text",
        _envelope({"type": [INVALID], "message": PROMPT}),
        "other",
        "prompt_too_long",
    ),
    ("no_type", _envelope({"message": PROMPT}), "other", "prompt_too_long"),
    (
        "error_not_an_object",
        json.dumps({"type": "error", "error": PROMPT}).encode(),
        "other",
        "other",
    ),
    ("no_error", json.dumps({"message": PROMPT}).encode(), "other", "other"),
    ("not_an_object", json.dumps([PROMPT]).encode(), "other", "other"),
    ("not_json", b"<html>" + PROMPT.encode() + b"</html>", "other", "other"),
    ("not_utf8", b"\x80" + _typed(PROMPT), "other", "other"),
    ("empty", b"", "other", "other"),
    ("seventy_thousand_bytes", _body_of(70_000), "other", "other"),
]


def test_the_class_table_matches_the_production_table():
    """``CLASS_MESSAGES`` is a literal copy: the same classes and wordings, in the same order."""
    assert messages_module._REJECTION_CLASS_MESSAGES == CLASS_MESSAGES


def test_every_class_the_message_table_names_is_in_the_closed_set():
    """A row named outside ``_REJECTION_CLASSES`` would be recorded as ``other``."""
    names = [name for name, _ in messages_module._REJECTION_CLASS_MESSAGES]

    assert names
    assert [name for name in names if name not in _REJECTION_CLASSES] == []
    assert "other" not in names


def test_a_classifier_failure_logs_only_the_exception_type(caplog):
    # The package logger is held at INFO, so the level is set on the logger that emits.
    caplog.set_level(logging.DEBUG, logger=messages_module.logger.name)
    body = b"\x80" + SENTINEL.encode()
    with pytest.raises(UnicodeDecodeError):
        json.loads(body)

    assert messages_module._classify_rejection(body) == ("other", "other")

    named = [
        record
        for record in caplog.records
        if record.levelno == logging.DEBUG and "UnicodeDecodeError" in record.getMessage()
    ]
    assert len(named) == 1
    assert named[0].exc_info is None
    for record in caplog.records:
        carried = [record.getMessage(), str(record.msg), repr(record.args)]
        carried += [repr(record.exc_info), str(record.exc_text)]
        assert SENTINEL not in " ".join(carried)
    assert SENTINEL not in caplog.text


@pytest.mark.asyncio
@STREAMED
@pytest.mark.parametrize(("rejection_class", "wording"), WORDINGS, ids=WORDING_IDS)
async def test_a_documented_message_records_its_class(
    monkeypatch, tmp_path, streamed, rejection_class, wording
):
    body = _typed(f"messages.1.content.0: {wording} (see the documentation)")
    path, _, error = await _reject(monkeypatch, tmp_path, body, streamed)
    _assert_recorded(path, error, INVALID, rejection_class)


@pytest.mark.asyncio
@STREAMED
@pytest.mark.parametrize(
    ("body", "error_type", "rejection_class"),
    [case[1:] for case in OTHER_BODIES],
    ids=[case[0] for case in OTHER_BODIES],
)
async def test_an_unrecognised_body_records_other(
    monkeypatch, tmp_path, streamed, body, error_type, rejection_class
):
    path, _, error = await _reject(monkeypatch, tmp_path, body, streamed)
    _assert_recorded(path, error, error_type, rejection_class)


@pytest.mark.asyncio
@STREAMED
@pytest.mark.parametrize(
    ("size", "error_type", "rejection_class"),
    [
        (65_535, INVALID, "prompt_too_long"),
        (65_536, INVALID, "prompt_too_long"),
        (65_537, "other", "other"),
        (65_538, "other", "other"),
    ],
)
async def test_a_body_over_64_kib_is_not_decoded(
    monkeypatch, tmp_path, streamed, size, error_type, rejection_class
):
    path, _, error = await _reject(monkeypatch, tmp_path, _body_of(size), streamed)
    _assert_recorded(path, error, error_type, rejection_class)


@pytest.mark.asyncio
async def test_a_non_streamed_body_is_cut_before_the_classifier(monkeypatch, tmp_path):
    """An oversize body reaches the classifier cut to one byte past the bound."""
    seen = []
    classify = messages_module._classify_rejection

    def recording(body):
        seen.append(len(body))
        return classify(body)

    monkeypatch.setattr(messages_module, "_classify_rejection", recording)
    path, _, error = await _reject(monkeypatch, tmp_path, _body_of(200_000), False)

    assert seen == [messages_module._REJECTION_BODY_LIMIT + 1]
    _assert_recorded(path, error, "other", "other")


@pytest.mark.asyncio
async def test_a_streamed_body_is_not_read_past_the_bound(monkeypatch, tmp_path):
    seen = []
    classify = messages_module._classify_rejection

    def recording(body):
        seen.append(len(body))
        return classify(body)

    monkeypatch.setattr(messages_module, "_classify_rejection", recording)
    rejection = _Rejection(400, _body_of(200_000), piece_size=16_384)
    path = _install_accounts(monkeypatch, tmp_path)
    model, _ = _model(monkeypatch, [rejection])
    with pytest.raises(AccountRoutingError) as caught:
        await _send(model, True)

    # 65,537 bytes arrive with the fifth piece of 16,384 bytes, and no piece after it is read.
    assert seen == [messages_module._REJECTION_BODY_LIMIT + 1]
    assert rejection.yielded == 5
    _assert_recorded(path, caught.value, "other", "other")


@pytest.mark.asyncio
async def test_a_streamed_body_that_does_not_arrive_in_time_records_other(monkeypatch, tmp_path):
    assert READ_HANG_GUARD < messages_module._REJECTION_READ_SECONDS
    monkeypatch.setattr(messages_module, "_REJECTION_READ_SECONDS", 0.05)
    path = _install_accounts(monkeypatch, tmp_path)
    hanging = _HangingRejection(400, b"")
    model, transport = _model(monkeypatch, [hanging])
    with anyio.fail_after(READ_HANG_GUARD):
        with pytest.raises(AccountRoutingError) as caught:
            await _send(model, True)

    # The empty body classifies as other unread too, so the read itself must have begun.
    assert hanging.entered
    _assert_recorded(path, caught.value, "other", "other")
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_a_streamed_body_that_arrives_after_the_read_limit_records_other(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(messages_module, "_REJECTION_READ_SECONDS", 0.05)
    path = _install_accounts(monkeypatch, tmp_path)
    model, _ = _model(monkeypatch, [_LateRejection(400, _typed(PROMPT))])
    with pytest.raises(AccountRoutingError) as caught:
        await _send(model, True)

    _assert_recorded(path, caught.value, "other", "other")


@pytest.mark.asyncio
async def test_a_cancelled_caller_still_has_its_streamed_rejection_classified(
    monkeypatch, tmp_path
):
    path = _install_accounts(monkeypatch, tmp_path)
    rejection = _CancellingRejection(400, _typed(PROMPT))
    model, _ = _model(monkeypatch, [rejection])
    with anyio.CancelScope() as caller:
        rejection.scope = caller
        with suppress(AccountRoutingError):
            await _send(model, True)

    assert caller.cancel_called
    assert _rejection_columns(path) == [("request_rejected", INVALID, "prompt_too_long")]


@pytest.mark.asyncio
async def test_a_streamed_body_that_fails_midway_records_other(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    path = _install_accounts(monkeypatch, tmp_path)
    model, _ = _model(monkeypatch, [_BrokenRejection(400, _typed(f"{PROMPT}: {SENTINEL}"))])
    with pytest.raises(AccountRoutingError) as caught:
        await _send(model, True)

    _assert_recorded(path, caught.value, "other", "other")
    _assert_no_sentinel(caught.value, caplog, path)


@pytest.mark.asyncio
@STREAMED
@pytest.mark.parametrize(
    ("earlier", "later"),
    ORDERED_PAIRS,
    ids=[f"{earlier}-before-{later}" for earlier, later in ORDERED_PAIRS],
)
async def test_a_message_naming_two_classes_gets_the_earlier_row(
    monkeypatch, tmp_path, streamed, earlier, later
):
    body = _typed(f"{FIRST_WORDING[later]} and also {FIRST_WORDING[earlier]}")
    path, _, error = await _reject(monkeypatch, tmp_path, body, streamed)
    _assert_recorded(path, error, INVALID, earlier)


@pytest.mark.asyncio
@STREAMED
@pytest.mark.parametrize(
    ("earlier", "earlier_wording", "later_wording"), WORDING_PAIRS, ids=WORDING_PAIR_IDS
)
async def test_every_wording_of_an_earlier_row_wins_over_every_wording_of_a_later_row(
    monkeypatch, tmp_path, streamed, earlier, earlier_wording, later_wording
):
    body = _typed(f"{later_wording} and also {earlier_wording}")
    path, _, error = await _reject(monkeypatch, tmp_path, body, streamed)
    _assert_recorded(path, error, INVALID, earlier)


CLASS_NAMES = [name for name, _ in CLASS_MESSAGES]


async def _reject_with_sentinel(monkeypatch, tmp_path, caplog, streamed):
    """Reject a request whose message names a class and carries the sentinel, logging at DEBUG."""
    caplog.set_level(logging.DEBUG)
    return await _reject(monkeypatch, tmp_path, _typed(f"{PROMPT}: {SENTINEL}"), streamed)


def _raise_from_package_frame(held):
    """An error raised by a frame whose code file lies in the package source and holds ``held``."""
    source = "def fail():\n    kept = HELD\n    raise RuntimeError('failed')\n"
    scope = {"HELD": held}
    exec(compile(source, str(PACKAGE_DIRS[0] / "_synthetic_frame.py"), "exec"), scope)
    try:
        scope["fail"]()
    except RuntimeError as exc:
        return exc


@pytest.mark.asyncio
@STREAMED
async def test_the_provider_message_is_kept_nowhere(monkeypatch, tmp_path, caplog, streamed):
    path, _, error = await _reject_with_sentinel(monkeypatch, tmp_path, caplog, streamed)
    assert error.rejection_class == "prompt_too_long"
    _assert_no_sentinel(error, caplog, path)


@pytest.mark.asyncio
@STREAMED
async def test_the_message_check_passes_when_nothing_is_classified(
    monkeypatch, tmp_path, caplog, streamed
):
    monkeypatch.setattr(messages_module, "_classify_rejection", lambda body: ("other", "other"))
    path, _, error = await _reject_with_sentinel(monkeypatch, tmp_path, caplog, streamed)
    assert (error.provider_error_type, error.rejection_class) == ("other", "other")
    _assert_no_sentinel(error, caplog, path)


@pytest.mark.asyncio
@STREAMED
async def test_the_message_check_fails_when_the_error_carries_the_message(
    monkeypatch, tmp_path, caplog, streamed
):
    if streamed:
        # A streamed failure does not pass the public failure wrapper, so the message is
        # added where the streamed attempt's error leaves the endpoint.
        original_stream = Endpoint.stream

        async def leaky_stream(self, *args, **kwargs):
            try:
                async for chunk in original_stream(self, *args, **kwargs):
                    yield chunk
            except AccountRoutingError as error:
                error.args = (*error.args, SENTINEL)
                raise

        monkeypatch.setattr(Endpoint, "stream", leaky_stream)
    else:
        original = Endpoint._public_routed_failure

        def leaky(exc):
            error = original(exc)
            error.args = (*error.args, SENTINEL)
            return error

        monkeypatch.setattr(Endpoint, "_public_routed_failure", staticmethod(leaky))
    path, _, error = await _reject_with_sentinel(monkeypatch, tmp_path, caplog, streamed)
    assert SENTINEL in repr(error.args)
    with pytest.raises(AssertionError):
        _assert_no_sentinel(error, caplog, path)


@pytest.mark.parametrize("held", [SENTINEL, SENTINEL.encode()], ids=["text", "bytes"])
def test_the_message_check_fails_when_a_package_frame_holds_the_message(tmp_path, caplog, held):
    error = _raise_from_package_frame(held)
    with pytest.raises(AssertionError):
        _assert_no_sentinel(error, caplog, tmp_path / "empty.sqlite3")


def test_the_message_check_passes_when_a_package_frame_holds_other_text(tmp_path, caplog):
    error = _raise_from_package_frame("harmless")
    _assert_no_sentinel(error, caplog, tmp_path / "empty.sqlite3")


@pytest.mark.asyncio
@STREAMED
async def test_a_pending_cancellation_is_delivered_after_a_classified_rejection_is_recorded(
    monkeypatch, tmp_path, streamed
):
    path, model, transport, _ = await _prime(monkeypatch, tmp_path, [_rejected(_typed(PROMPT))])
    _answer_late(model, transport)

    with pytest.raises(TimeoutError):
        with anyio.fail_after(DEADLINE):
            await _send(model, streamed)

    assert _attempts(path)[-1][4] == "request_rejected"
    assert _rejection_columns(path)[-1] == ("request_rejected", INVALID, "prompt_too_long")
    assert _warm_rows(path) == []


@pytest.mark.asyncio
@STREAMED
@pytest.mark.parametrize("rejection_class", CLASS_NAMES)
async def test_a_rejection_of_each_class_is_not_retried(
    monkeypatch, tmp_path, streamed, rejection_class
):
    body = _typed(FIRST_WORDING[rejection_class])
    path, transport, error = await _reject(monkeypatch, tmp_path, body, streamed)
    assert error.code is RouterErrorCode.REQUEST_REJECTED
    assert len(transport.calls) == 1
    assert len(_attempts(path)) == 1


@pytest.mark.asyncio
@STREAMED
@pytest.mark.parametrize("rejection_class", CLASS_NAMES)
async def test_a_rejection_of_each_class_clears_the_warm_prefix_rows(
    monkeypatch, tmp_path, streamed, rejection_class
):
    body = _typed(FIRST_WORDING[rejection_class])
    path, model, transport, rows = await _prime(monkeypatch, tmp_path, [_rejected(body)])

    with pytest.raises(AccountRoutingError) as caught:
        await _send(model, streamed)

    assert caught.value.code is RouterErrorCode.REQUEST_REJECTED
    assert len(transport.calls) == 2
    assert _warm_rows(path) == []
    assert _block_reason(path, rows[0][0]) is None
