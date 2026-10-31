# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""One plain-value snapshot serves every check and the command line.

A ``str`` subclass, a mapping subclass, a value nested in a table or a key whose
``__class__`` lies can answer a containment check one way and the command line another.
Every field a check or the renderer reads is reduced to plain built-in data in one
function (``_plain_request``) before either sees it, and the snapshot is taken again where
the process would start, so a ``model_copy(update=...)`` — which skips validation — cannot
carry an unchecked object to the command line. The invariant test below walks the field
list that function reduces, so a field added to it without a reduction fails it.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import os
from pathlib import Path

import pydantic
import pytest

from lionagi._errors import CLIInputRefusedError
from lionhub.providers.openai import codex as codex_mod
from lionhub.providers.openai.codex import CodexCodeRequest, toml_override_value

SECRET = "sk-PLAIN-SNAPSHOT-SECRET"
# The answer every hostile value gives when read by its own methods instead of its text.
HOSTILE = "HOSTILE-ANSWER"
GENERATED_PROFILE = "lionagi-mcp-" + "0" * 32
RAW_PAYLOAD = 'x", apps = true, y = "'


class _TextThatReadsAsSomethingElse(str):
    """A ``str`` subclass whose text is one value and whose own readers answer another.

    ``__str__``, ``__format__``, ``encode`` and ``replace`` answer ``HOSTILE``; ``__eq__``
    answers no for every other value, so a check that compares with ``==`` — or a renderer
    that escapes through ``replace`` — reads one answer and the command line another, and
    only the base reader ``str.__str__`` gives the text.
    """

    def __str__(self):
        return HOSTILE

    def __format__(self, spec=""):
        return HOSTILE

    def encode(self, *args, **kwargs):  # noqa: ARG002
        return HOSTILE.encode()

    def replace(self, *args, **kwargs):  # noqa: ARG002
        return HOSTILE

    def __eq__(self, other):
        return False

    def __hash__(self):
        return 0


class _TextWhoseStrLies(str):
    """A ``str`` subclass whose own ``__str__`` and ``__format__`` answer ``HOSTILE`` while its
    buffer holds the text it was built with, read only by the base reader."""

    def __str__(self):
        return HOSTILE

    def __format__(self, spec=""):
        return HOSTILE


class _EnvValueWhoseEncoderAnswersElsewhere(str):
    """A path-like environment value whose own ``encode`` answers another directory: the spawn
    encodes this value, so the directory codex reads is the answer, not the buffer."""

    answer = "/elsewhere/.codex"

    def encode(self, *args, **kwargs):  # noqa: ARG002
        return type(self).answer.encode()


class _PathWhoseStrAnswersSafeFirst(str):
    """A path whose own ``__str__`` answers a path inside the repo first and an absolute path
    after: a check that reads it before the renderer passes it and the renderer escapes."""

    def __new__(cls, safe: str, then: str):
        obj = super().__new__(cls, safe)
        obj._then = then
        obj._reads = 0
        return obj

    def __str__(self):
        self._reads += 1
        return str.__str__(self) if self._reads <= 1 else self._then


class _PathLikeWhoseStrAnswersSafeFirst(type(Path())):
    """A path-like object whose ``__str__`` answers a safe path for its first two reads and an
    absolute path after, so a check and a renderer that each read it disagree."""

    def __new__(cls, safe: str, *args, then: str = "/etc/passwd", **kwargs):
        obj = super().__new__(cls, safe, *args, **kwargs)
        obj._then = then
        obj._reads = 0
        return obj

    def __str__(self):
        self._reads += 1
        return super().__str__() if self._reads <= 2 else self._then


class _TextHoldingANulBehindACleanEncoder(str):
    """A path whose text holds a NUL while its own ``encode`` answers clean bytes: a check
    that measured the encoder's answer would pass a NUL argument the spawn must refuse."""

    def encode(self, *args, **kwargs):  # noqa: ARG002
        return str.__str__(self).replace("\x00", "").encode()


class _CannotBeAPath:
    """A value with no ``__fspath__`` and no text, which no path field may accept."""


class _ListWhoseTruthinessDiffers(list):
    """A non-empty list whose own truthiness answers ``False``: a check that asks whether a
    grant was named would judge containment one way and the command line another."""

    def __bool__(self):
        return False


class _TextWhoseReplaceEscapesNothing(str):
    """A ``str`` subclass whose ``replace`` answers an unescaped inline table."""

    def replace(self, *args, **kwargs):  # noqa: ARG002
        return _AnswersTheRawPayload("x")


class _AnswersTheRawPayload(str):
    def replace(self, *args, **kwargs):  # noqa: ARG002
        return RAW_PAYLOAD


class _TextWhoseEncoderRaises(str):
    """A ``str`` subclass whose own ``encode`` raises an error carrying its text."""

    def encode(self, *args, **kwargs):  # noqa: ARG002
        raise ValueError(str.__str__(self))


class _IntThatIsNotABoolean(int):
    """An ``int`` subclass, standing in for a value a bool field must refuse."""


class _TableToldByItems(dict):
    """A table whose ``items()`` names an account switch its storage does not hold."""

    def items(self):
        return [("apps", True), ("web_search", True)]


class _MappingWhoseItemsDifferFromItsStorage(dict):
    """A mapping subclass whose ``items()`` answers a pair its own storage does not hold, so a
    check that reads ``items()`` once and a renderer that reads the storage would disagree."""

    def items(self):
        return [("model_x", _TextThatReadsAsSomethingElse("plain"))]


class _KeyWhoseClassAnswersStr:
    """Not text, but its ``__class__`` answers ``str``, so an ``isinstance`` test passes it
    while ``type()`` names the real class."""

    @property
    def __class__(self):
        return str

    def __hash__(self):
        return 1

    def __eq__(self, other):
        return self is other

    def __format__(self, spec=""):
        return "profile"

    def __str__(self):
        return "ok"


class _Stopped(Exception):
    pass


def _plain_leaves(value) -> bool:
    """Whether ``value`` is plain built-in data at every depth."""
    if value is None or type(value) in (bool, int, float, str):
        return True
    if type(value) is tuple:
        return all(_plain_leaves(item) for item in value)
    if type(value) is list:
        return all(_plain_leaves(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _plain_leaves(item) for key, item in value.items())
    return False


def _values_after(args: list[str], flag: str, stop: str = "--") -> list[str]:
    end = args.index(stop) if stop in args else len(args)
    return [
        args[index + 1]
        for index, token in enumerate(args[:end])
        if token == flag and index + 1 < end
    ]


def _override_argument(snapshot) -> str | None:
    """The `-c` argument the snapshot's own override pairs render, or None."""
    if not snapshot.overrides:
        return None
    key, value = snapshot.overrides[0]
    return f"{key}={toml_override_value(value)}"


# The snapshot names the reduced `config_overrides` field `overrides`; every other field
# keeps its own name. The invariant test reads each field off the snapshot through this.
_SNAPSHOT_NAME = {"config_overrides": "overrides"}


# Every field `_plain_request` reduces, with a hostile value, the plain data the snapshot
# must hold for it, and the one argument the command line must show for it (None when the
# field is not rendered on the command line). The expectation is derived from the snapshot,
# so the check's read and the rendered argument are compared through one normalized object.
FIELD_CASES: dict[str, tuple[object, object, object]] = {
    "prompt": (_TextThatReadsAsSomethingElse("hi"), "hi", None),
    "system_prompt": (
        _TextThatReadsAsSomethingElse("be brief"),
        "be brief",
        lambda s: f"developer_instructions={toml_override_value(s.system_prompt)}",
    ),
    "model": (_TextThatReadsAsSomethingElse("gpt-5.3-codex"), "gpt-5.3-codex", lambda s: s.model),
    "profile": (
        _TextThatReadsAsSomethingElse(GENERATED_PROFILE),
        GENERATED_PROFILE,
        lambda s: s.profile,
    ),
    "sandbox": (_TextThatReadsAsSomethingElse("read-only"), "read-only", lambda s: s.sandbox),
    "ask_for_approval": (
        _TextThatReadsAsSomethingElse("on-request"),
        "on-request",
        lambda s: f"approval_policy={toml_override_value(s.ask_for_approval)}",
    ),
    "color": (_TextThatReadsAsSomethingElse("auto"), "auto", lambda s: s.color),
    "reasoning_effort": (
        _TextThatReadsAsSomethingElse("high"),
        "high",
        lambda s: f"reasoning_effort={toml_override_value(s.reasoning_effort)}",
    ),
    "plan_mode_reasoning_effort": (
        _TextThatReadsAsSomethingElse("low"),
        "low",
        lambda s: f"plan_mode_reasoning_effort={toml_override_value(s.plan_mode_reasoning_effort)}",
    ),
    "enable_features": (
        [_TextThatReadsAsSomethingElse("web_search")],
        ["web_search"],
        lambda s: s.enable_features[0],
    ),
    "disable_features": (
        [_TextThatReadsAsSomethingElse("web_search")],
        ["web_search"],
        lambda s: s.disable_features[0],
    ),
    "config_overrides": (
        {"features": {"web_search": _TextThatReadsAsSomethingElse("plain")}},
        [("features", {"web_search": "plain"})],
        _override_argument,
    ),
    "account_connectors": (
        _ListWhoseTruthinessDiffers([_TextThatReadsAsSomethingElse("github")]),
        ["github"],
        None,
    ),
    "output_schema": (
        _TextWhoseStrLies("schemas/out.json"),
        "schemas/out.json",
        lambda s: s.output_schema,
    ),
    "output_last_message": (
        _TextWhoseStrLies("schemas/last.json"),
        "schemas/last.json",
        lambda s: s.output_last_message,
    ),
    "images": (
        [_TextWhoseStrLies("assets/shot.png")],
        ["assets/shot.png"],
        lambda s: s.images[0],
    ),
    "add_dir": (
        [_TextWhoseStrLies("vendor/lib")],
        ["vendor/lib"],
        lambda s: s.add_dir[0],
    ),
    # The class is not an argument of its own; the light sandbox's pinned override shows it.
    "workload": (
        _TextThatReadsAsSomethingElse("light"),
        "light",
        lambda s: "sandbox_workspace_write.network_access=false",
    ),
}

# The reduced fields whose plain value cannot be compared the same way for a request built
# by construction and one carried by a copy: pydantic coerces an ``env`` mapping (and its
# keys and values) to plain text at construction, so a hostile value only reaches the
# reduction through ``model_copy``. Each has its own test below.
EXTRA_REDUCED_FIELDS: tuple[str, ...] = ("env", "exclusive_mcp_names")


def test_exclusive_names_reduce_hostile_copy_values(tmp_path):
    request = _contained(tmp_path).model_copy(
        update={"exclusive_mcp_names": [_TextWhoseStrLies("kept")]}
    )
    assert codex_mod._plain_request(request).exclusive_mcp_names == ["kept"]
    request = request.model_copy(update={"exclusive_mcp_names": [1]})
    with pytest.raises(TypeError, match="exclusive_mcp_names entry 0"):
        codex_mod._plain_request(request)


# A bool cannot be subclassed, so the hostile for a bool field is a non-bool value: the
# snapshot refuses it by the field's name and its type, never by any text.
BOOLEAN_FIELDS: tuple[str, ...] = (
    "bypass_approvals",
    "full_auto",
    "search",
    "fast_mode",
    "contain_account_integrations",
    "ignore_user_config",
)


def _contained(tmp_path, **kwargs) -> CodexCodeRequest:
    kwargs.setdefault("prompt", "hi")
    return CodexCodeRequest(repo=tmp_path, contain_account_integrations=True, **kwargs)


def _spawn(monkeypatch, request) -> dict:
    """The command the spawn would run, or the error raised before a process exists.

    No process starts: ``ndjson_from_cli`` is replaced by a recorder that raises before a
    child exists. The recorder also keeps the environment the spawn was handed, so a test
    can assert the child's environment is the snapshot's own copy.
    """
    seen: dict = {}

    def spawned(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen["stdin_data"] = kwargs.get("stdin_data")
        seen["env"] = dict(kwargs["env"]) if kwargs.get("env") is not None else None
        raise _Stopped

    async def drain():
        async for _ in codex_mod._ndjson_from_cli(request):
            pass

    monkeypatch.setattr(codex_mod, "CODEX_CLI", "codex")
    monkeypatch.setattr(codex_mod, "_linked_worktree_git_writable_roots", lambda _cwd: ())
    monkeypatch.setattr(codex_mod, "ndjson_from_cli", spawned)
    try:
        asyncio.run(drain())
    except _Stopped:
        pass
    except Exception as exc:  # noqa: BLE001
        seen["raised"] = exc
    return seen


def _by_construction(tmp_path, field, hostile):
    """The request pydantic builds from the hostile, or the refusal it raises.

    A field whose validator names a closed set reads the hostile's ``__eq__``, which
    answers no, so construction refuses it: the value never reaches a check or the
    command line. A field pydantic keeps is coerced to plain data. Either outcome leaves
    no hostile object on a built request.
    """
    try:
        return _contained(tmp_path, **{field: hostile}), None
    except Exception as refused:  # noqa: BLE001
        return None, refused


@pytest.fixture
def no_git(monkeypatch):
    monkeypatch.setattr(codex_mod, "_linked_worktree_git_writable_roots", lambda _cwd: ())
    return monkeypatch


def _snapshot_field_names() -> set[str]:
    """Every field name `_PlainRequest` carries, read off the class itself.

    The field list is derived rather than kept by hand, so a field added to the snapshot
    without a case (or without a reduction) fails the invariant test below.
    """
    return {field.name for field in dataclasses.fields(codex_mod._PlainRequest)}


def test_every_reduced_field_has_a_hostile_case():
    """A field added to the reduced set without a case fails here."""
    covered = set(FIELD_CASES) | set(BOOLEAN_FIELDS) | set(EXTRA_REDUCED_FIELDS)
    covered = {_SNAPSHOT_NAME.get(name, name) for name in covered}
    assert covered == _snapshot_field_names()


@pytest.mark.parametrize("field", sorted(FIELD_CASES))
def test_the_text_the_check_reads_is_the_text_on_the_command_line(field, tmp_path, no_git):
    """For each reduced field, a hostile value built by construction and one carried by a
    ``model_copy`` are both read as plain data by the snapshot, and the command line at
    ``as_cmd_args`` and where a process would start carries exactly that plain value."""
    hostile, expected, show = FIELD_CASES[field]

    built, refused = _by_construction(tmp_path, field, hostile)
    if refused is not None:
        # pydantic read the hostile's `__eq__`, which answers no, so construction refused
        # it: nothing this field carries reaches a check or the command line.
        assert type(refused).__name__ == "ValidationError"
    for request in (built, _contained(tmp_path).model_copy(update={field: hostile})):
        if request is None:
            continue
        snapshot = codex_mod._plain_request(request)
        value = getattr(snapshot, _SNAPSHOT_NAME.get(field, field))
        assert _plain_leaves(value), (field, type(value))
        assert value == expected

        args = request.as_cmd_args()
        assert HOSTILE not in args
        if show is not None:
            assert show(snapshot) in args

        seen = _spawn(no_git, request)
        assert "raised" not in seen, seen.get("raised")
        assert HOSTILE not in seen["cmd"]
        if show is not None:
            assert show(codex_mod._plain_request(request)) in seen["cmd"]
        # The spawn's argv is the command the snapshot renders, so a spawn that read the
        # raw request - and rendered this field's hostile answer - would differ.
        assert seen["cmd"][1:] == request.as_cmd_args()
        # The environment the child is handed is the snapshot's own copy, so the check
        # and the spawn read the same mapping.
        assert seen["env"] == snapshot.env


def test_the_prompt_reaches_the_child_as_the_text_the_check_read(tmp_path, no_git):
    """The turn prompt travels over the child's stdin; the bytes written are the plain text
    the check measured, not the hostile answer of the caller's object."""
    request = _contained(tmp_path).model_copy(
        update={"prompt": _TextThatReadsAsSomethingElse("hi")}
    )
    expected = request.as_cmd_args()

    seen = _spawn(no_git, request)

    assert seen["stdin_data"] == codex_mod._plain_request(request).prompt == "hi"
    # The prompt never becomes an argument, so the spawn's whole argv is the snapshot's own
    # command: a spawn that read the raw request would still render it from the same fields.
    assert seen["cmd"] == [codex_mod.CODEX_CLI, *expected]


def test_the_connectors_reach_the_spawn_as_the_text_the_check_read(tmp_path, no_git):
    """A connector name whose own readers answer ``HOSTILE`` is judged by its text, so the
    grant the containment check reads is the grant the spawn reads: the spawn's argv is
    exactly the command the snapshot renders, not the answer of the caller's object."""
    request = _contained(tmp_path).model_copy(
        update={"account_connectors": [_TextThatReadsAsSomethingElse("github")]}
    )
    expected = request.as_cmd_args()

    seen = _spawn(no_git, request)

    assert "raised" not in seen, seen.get("raised")
    assert HOSTILE not in seen["cmd"]
    # Containment is lifted by the named connector, so the account features are not forced
    # off; the spawn's argv is the snapshot's own, so a spawn that read the raw request (a
    # connector list the check would not read as a grant) would carry `--disable apps`.
    assert "apps" not in seen["cmd"]
    assert "plugins" not in seen["cmd"]
    assert seen["cmd"][1:] == expected


@pytest.mark.parametrize("field", sorted(BOOLEAN_FIELDS))
def test_a_bool_field_refuses_a_non_bool_by_name_and_type(field, tmp_path, no_git):
    """A copied request can carry a non-bool where a bool belongs; the snapshot refuses it
    by the field's name and its type, and nothing reaches the command line."""
    request = _contained(tmp_path).model_copy(update={field: _IntThatIsNotABoolean(1)})

    with pytest.raises(TypeError, match=field):
        request.as_cmd_args()
    seen = _spawn(no_git, request)
    assert isinstance(seen.get("raised"), TypeError)
    assert field in str(seen["raised"])
    assert "cmd" not in seen


# ── the fields a check judges before the command line is built ───────────────


def test_an_enable_feature_is_rendered_as_the_text_the_check_read(tmp_path, no_git):
    """An element whose own ``__str__`` answers ``apps`` is judged by its text and rendered
    as that text, so it cannot pass containment as an ordinary switch and reach the CLI as
    an account switch."""
    request = _contained(tmp_path).model_copy(
        update={"enable_features": [_TextThatReadsAsSomethingElse("x")]}
    )
    args = request.as_cmd_args()

    assert _values_after(args, "--enable") == ["x"]
    # Containment adds `--disable apps --disable plugins`; the point is that no `--enable`
    # value and no rendered token is the hostile answer.
    assert "apps" not in _values_after(args, "--enable")
    assert HOSTILE not in args


def test_a_profile_is_judged_and_rendered_by_the_same_text(tmp_path, no_git):
    """The profile containment compares is the profile the command line names."""
    request = _contained(tmp_path).model_copy(
        update={"profile": _TextThatReadsAsSomethingElse(GENERATED_PROFILE)}
    )
    args = request.as_cmd_args()

    assert _values_after(args, "-p") == [GENERATED_PROFILE]
    assert HOSTILE not in args


def test_an_ambiguous_approval_policy_is_refused_on_a_copied_request(tmp_path, no_git):
    """A copy carries ``ask_for_approval`` beside an ``approval_policy`` override with no
    validation; the refusal runs where the process would start, so both entries never reach
    the command line."""
    request = CodexCodeRequest(
        prompt="hi", repo=tmp_path, ask_for_approval="on-request"
    ).model_copy(update={"config_overrides": {"approval_policy": "never"}})

    with pytest.raises(ValueError, match="ask_for_approval cannot be combined"):
        request.as_cmd_args()
    seen = _spawn(no_git, request)
    assert isinstance(seen.get("raised"), ValueError)
    assert "cannot be combined" in str(seen["raised"])
    assert "cmd" not in seen


def test_a_nested_features_value_is_rendered_as_its_plain_text(tmp_path, no_git):
    """A nested value whose own ``replace`` answers an unescaped inline table is read as its
    text, so the table renders only the switch the request holds."""
    request = _contained(
        tmp_path,
        config_overrides={"features": {"web_search": _TextWhoseReplaceEscapesNothing("x")}},
    )
    argument = next(a for a in request.as_cmd_args() if a.startswith("features="))

    assert argument == 'features={ web_search = "x" }'
    assert "apps" not in argument


def test_a_nested_table_is_read_by_its_storage_not_its_items(tmp_path, no_git):
    """A nested table whose ``items()`` names an account switch its storage does not hold is
    read by its storage, so the switch it names never reaches the command line."""
    request = _contained(tmp_path, config_overrides={"features": _TableToldByItems({"x": 1})})
    args = request.as_cmd_args()
    features = next(a for a in args if a.startswith("features="))

    assert "apps" not in features
    assert features == "features={ x = 1 }"


def test_a_key_whose_class_answers_str_is_refused_by_position_and_type(tmp_path, no_git):
    """``isinstance`` passes a key whose ``__class__`` answers ``str``; the snapshot reads
    text only when ``issubclass(type(key), str)`` holds, so such a key is refused by its
    position and its real type, never rendered as a profile."""
    request = _contained(tmp_path).model_copy(
        update={"config_overrides": {_KeyWhoseClassAnswersStr(): "work"}}
    )

    with pytest.raises(TypeError) as refused:
        request.as_cmd_args()
    assert "entry 0" in str(refused.value)
    assert "_KeyWhoseClassAnswersStr" in str(refused.value)
    assert "profile=" not in str(refused.value)

    seen = _spawn(no_git, request)
    assert isinstance(seen.get("raised"), TypeError)
    assert "_KeyWhoseClassAnswersStr" in str(seen["raised"])
    assert "cmd" not in seen


def test_a_mapping_whose_items_differ_from_its_storage_is_read_once(tmp_path, no_git):
    """A mapping subclass whose ``items()`` answers a pair its storage does not hold is read
    through ``items()`` once, so the check and the command line read that same pair."""
    overrides = _MappingWhoseItemsDifferFromItsStorage({"ignored": "storage"})
    request = _contained(tmp_path).model_copy(update={"config_overrides": overrides})

    snapshot = codex_mod._plain_request(request)
    assert snapshot.overrides == [("model_x", "plain")]

    argument = codex_mod._plain_request(request).overrides[0]
    rendered = f"{argument[0]}={toml_override_value(argument[1])}"
    assert rendered == 'model_x="plain"'
    assert rendered in request.as_cmd_args()
    assert rendered in _spawn(no_git, request)["cmd"]


# ── an encoder that raises ───────────────────────────────────────────────────


def test_an_env_value_whose_encoder_raises_is_refused_by_position(tmp_path):
    """An env value whose own ``encode`` raises an error carrying its text is refused by
    position and type at construction, and no rendering of the refusal holds the text."""
    with pytest.raises(TypeError) as refused:
        CodexCodeRequest(
            prompt="hi", repo=tmp_path, env={"HOME": "/h", "K": _TextWhoseEncoderRaises(SECRET)}
        )

    assert "entry 1" in str(refused.value)
    assert SECRET not in " ".join(
        [str(refused.value), repr(refused.value), *map(repr, refused.value.args)]
    )


def test_an_env_value_whose_encoder_raises_is_refused_by_position_at_the_spawn_copy():
    """The spawn's own copy refuses the same entry by position and type, with no error held
    as its context and no part of the value on it."""
    from lionhub.providers import _cli_subprocess

    with pytest.raises(ValueError) as refused:
        _cli_subprocess.env_as_the_spawn_encodes_it(
            {"HOME": "/h", "K": _TextWhoseEncoderRaises(SECRET)}
        )

    assert "entry 1" in str(refused.value)
    assert refused.value.__context__ is None
    assert refused.value.__cause__ is None
    assert SECRET not in " ".join(
        [str(refused.value), repr(refused.value), *map(repr, refused.value.args)]
    )


def test_a_nested_override_value_whose_encoder_raises_is_read_as_its_text(tmp_path, no_git):
    """A nested override value whose own ``encode`` raises is read as its plain text, so the
    request builds and the command line carries that text with no error raised: the raise is
    never reached, and no error holds the value."""
    request = _contained(tmp_path, config_overrides={"model_x": [_TextWhoseEncoderRaises(SECRET)]})
    seen = _spawn(no_git, request)

    assert "raised" not in seen
    assert any(argument.startswith("model_x=") for argument in seen["cmd"])
    assert f'model_x=["{SECRET}"]' in seen["cmd"]


def test_a_copied_nested_override_value_whose_encoder_raises_carries_no_text(tmp_path, no_git):
    """The same nested value carried by a copy is read as its plain text where the process
    would start, so no error reaches the caller and the command line carries the text."""
    request = _contained(tmp_path).model_copy(
        update={"config_overrides": {"model_x": [_TextWhoseEncoderRaises(SECRET)]}}
    )
    seen = _spawn(no_git, request)

    assert "raised" not in seen
    assert any(argument.startswith("model_x=") for argument in seen["cmd"])


# ── a construction validator reads text by type, not by `isinstance` ──────────


class _KeyWhoseClassAnswersStrAndHoldsAText:
    """Not text, but its ``__class__`` answers ``str``, and its ``repr`` carries ``SECRET``.

    ``isinstance`` passes it; the field validator must judge it by ``issubclass(type(x),
    str)`` and refuse it by position and type, so pydantic never renders its ``repr`` into
    a validation error.
    """

    @property
    def __class__(self):
        return str

    def __hash__(self):
        return 1

    def __eq__(self, other):
        return self is other

    def __repr__(self):
        return f"_KeyWhoseClassAnswersStrAndHoldsAText({SECRET})"

    def __str__(self):
        return "ok"

    def __fspath__(self):
        return "ok"


class _ValueWhoseClassAnswersStr:
    """A non-text value whose ``__class__`` answers ``str`` and whose ``repr`` carries
    ``SECRET``; an ``isinstance`` test lets pydantic quote it."""

    @property
    def __class__(self):
        return str

    def __repr__(self):
        return f"_ValueWhoseClassAnswersStr({SECRET})"

    def __str__(self):
        return "ok"

    def __fspath__(self):
        return "ok"


def _carries_no_secret(error: BaseException) -> None:
    parts = [str(error), repr(error), *(repr(part) for part in error.args)]
    pending = [error.__context__, error.__cause__]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        parts.extend([str(current), repr(current), *(repr(part) for part in current.args)])
        object_value = getattr(current, "object", None)
        if object_value is not None:
            parts.append(repr(object_value))
        pending.extend([current.__context__, current.__cause__])
    assert SECRET not in " ".join(parts)


def _everything_a_validation_error_shows(error: BaseException) -> str:
    parts = [str(error), repr(error), *(repr(part) for part in error.args)]
    for attribute in ("json", "errors"):
        reader = getattr(error, attribute, None)
        if callable(reader):
            parts.append(str(reader()))
    return " ".join(parts)


def test_a_config_override_key_whose_class_answers_str_is_refused_at_construction(tmp_path):
    """A key whose ``__class__`` answers ``str`` passes ``isinstance``; the field validator
    reads text only when ``issubclass(type(key), str)`` holds, so it is refused by position
    and type, with none of its text in the refusal."""
    with pytest.raises(TypeError) as refused:
        CodexCodeRequest(
            prompt="hi",
            repo=tmp_path,
            config_overrides={_KeyWhoseClassAnswersStrAndHoldsAText(): "v"},
        )

    assert "entry 0" in str(refused.value)
    assert "_KeyWhoseClassAnswersStrAndHoldsAText" in str(refused.value)
    _carries_no_secret(refused.value)


def test_an_env_key_whose_class_answers_str_is_refused_at_construction(tmp_path):
    """An env key whose ``__class__`` answers ``str`` is refused by position and type, not
    handed to pydantic, which would quote it into the validation error."""
    with pytest.raises(TypeError) as refused:
        CodexCodeRequest(prompt="hi", repo=tmp_path, env={_ValueWhoseClassAnswersStr(): "v"})

    assert "entry 0 (key is _ValueWhoseClassAnswersStr)" in str(refused.value)
    _carries_no_secret(refused.value)


def test_an_env_value_whose_class_answers_str_is_refused_at_construction(tmp_path):
    """An env value whose ``__class__`` answers ``str`` is refused by position and type."""
    with pytest.raises(TypeError) as refused:
        CodexCodeRequest(
            prompt="hi", repo=tmp_path, env={"HOME": "/h", "K": _ValueWhoseClassAnswersStr()}
        )

    assert "entry 1 (value is _ValueWhoseClassAnswersStr)" in str(refused.value)
    _carries_no_secret(refused.value)


def test_the_claude_code_env_refuses_a_value_whose_class_answers_str():
    """The shared env check refuses the same value at construction for the Claude Code
    request, so neither provider hands pydantic an object it would quote."""
    from lionhub.providers.anthropic.claude_code import ClaudeCodeRequest

    with pytest.raises(TypeError) as refused:
        ClaudeCodeRequest(prompt="hi", env={"HOME": "/h", "K": _ValueWhoseClassAnswersStr()})

    assert "entry 1 (value is _ValueWhoseClassAnswersStr)" in str(refused.value)
    _carries_no_secret(refused.value)


# ── construction reduces env and override keys to plain text ──────────────────


class _SubText(str):
    """A ``str`` subclass with no readers of its own, standing in for any str subclass a
    caller passes in the runtime fields."""


class _TextWhoseBufferHoldsALoneSurrogate(_SubText):
    """A ``str`` subclass whose buffer holds a lone surrogate (U+D800) that no encoder the
    spawn uses can carry, refused by position and type, never quoted."""


class _TextWhoseEncoderAnswersTheSpawnBytes(_SubText):
    """A ``str`` subclass whose own ``encode`` answers other bytes than its buffer holds. The
    spawn encodes this value, so the text construction stores must be the text of the bytes the
    encoder answers, never the buffer a pydantic ``dict[str, str]`` check would reject the
    subclass for."""

    def __new__(cls, buffer: str, sent: bytes):
        obj = super().__new__(cls, buffer)
        obj._sent = sent
        return obj

    def encode(self, *args, **kwargs):  # noqa: ARG002
        return self._sent


def _every_rendering_of(error: BaseException) -> str:
    """``str``, ``repr``, ``args`` and ``errors()``/``json()`` of one error, or of the parts it
    can give: ``json()`` on an error holding a lone surrogate itself raises, so each reader is
    tried and skipped when it fails rather than failing the test with its own error."""
    parts = [str(error), repr(error), *(repr(part) for part in getattr(error, "args", ()))]
    for attribute in ("json", "errors"):
        reader = getattr(error, attribute, None)
        if callable(reader):
            try:
                parts.append(str(reader()))
            except Exception:  # noqa: BLE001
                pass
    return " ".join(parts)


def _the_error_and_its_chain_show_no_secret(error: BaseException) -> None:
    """Every rendering of ``error``, its ``errors()``/``json()``, and its whole chain."""
    text = _every_rendering_of(error)
    pending = [error.__context__, error.__cause__]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        text += " " + _every_rendering_of(current)
        object_value = getattr(current, "object", None)
        if object_value is not None:
            text += " " + repr(object_value)
        pending.extend([current.__context__, current.__cause__])
    assert SECRET not in text


def test_a_codex_env_value_that_is_a_plain_str_subclass_is_stored_as_plain_text(tmp_path):
    """A ``str`` subclass carrying a POSIX surrogate escape passes the encodability check, so
    ``dict(value)`` would keep the subclass and pydantic, running strict UTF-8 on it, would
    reject it with a ``ValidationError`` quoting the value. The validator returns plain text,
    so the request builds and the stored env is what the spawn encodes."""
    escaped = SECRET + os.fsdecode(b"\xff")
    request = CodexCodeRequest(prompt="hi", repo=tmp_path, env={"K": _SubText(escaped)})

    assert type(request.env["K"]) is str
    assert request.env["K"] == escaped


def test_a_claude_code_env_value_that_is_a_plain_str_subclass_is_stored_as_plain_text():
    """The shared env reduction returns plain text for the Claude Code request too."""
    from lionhub.providers.anthropic.claude_code import ClaudeCodeRequest

    escaped = SECRET + os.fsdecode(b"\xff")
    request = ClaudeCodeRequest(prompt="hi", env={"K": _SubText(escaped)})

    assert type(request.env["K"]) is str
    assert request.env["K"] == escaped


def test_an_env_value_whose_own_encode_answers_other_bytes_is_stored_as_the_spawn_reads_it(
    tmp_path,
):
    """A value whose own ``encode`` answers different bytes than its buffer holds is stored as
    the text of the bytes the spawn would send, so construction and the spawn read one env. Its
    buffer holds a lone surrogate, so a check that read the buffer instead would refuse it."""
    value = _TextWhoseEncoderAnswersTheSpawnBytes(SECRET + "\ud800", b"ok")

    request = CodexCodeRequest(prompt="hi", repo=tmp_path, env={"K": value})

    assert type(request.env["K"]) is str
    assert request.env["K"] == "ok"
    assert request.env == codex_mod._plain_request(request).env


def test_an_env_value_the_spawn_cannot_encode_is_refused_by_position_and_type(tmp_path):
    """A value holding a lone surrogate is refused by position and type, with none of its text
    on the refusal, its ``args`` or its chain."""
    value = _TextWhoseBufferHoldsALoneSurrogate(SECRET + "\ud800")

    with pytest.raises(TypeError) as refused:
        CodexCodeRequest(prompt="hi", repo=tmp_path, env={"HOME": "/h", "K": value})

    assert "entry 1" in str(refused.value)
    assert refused.value.__context__ is None
    assert refused.value.__cause__ is None
    _the_error_and_its_chain_show_no_secret(refused.value)


def test_a_config_override_key_that_is_a_plain_str_subclass_is_refused_by_size_or_text(
    tmp_path,
):
    """A ``str`` subclass key carrying a lone surrogate is reduced with ``str.__str__`` before
    pydantic validates the mapping, so it reaches the input check and is refused as an input
    the CLI cannot take, rather than quoted in a ``ValidationError``."""
    key = _SubText(SECRET + "\ud800")

    with pytest.raises(CLIInputRefusedError) as refused:
        CodexCodeRequest(prompt="hi", repo=tmp_path, config_overrides={key: "v"})

    assert not isinstance(refused.value, pydantic.ValidationError)
    assert str(refused.value) == (
        "The key of the config override entry 0 holds a character strict UTF-8 cannot "
        "encode: a lone surrogate cannot be passed to the Codex CLI."
    )
    _the_error_and_its_chain_show_no_secret(refused.value)


def test_an_over_limit_override_is_refused_by_size_not_as_a_validation_error(tmp_path):
    """A reduced override reaches the turn-input check, so an entry past the argument limit is
    refused as an input the CLI cannot take, by size, rather than quoted in a validation error
    or handed to exec."""
    with pytest.raises(CLIInputRefusedError, match="config override entry 0") as refused:
        CodexCodeRequest(
            prompt="hi",
            repo=tmp_path,
            config_overrides={_SubText("k"): "v" * (1_048_576 + 1)},
        )

    assert not isinstance(refused.value, pydantic.ValidationError)
    assert "argument of 1,048," in str(refused.value) or "or more" in str(refused.value)


# ── the environment and the four path fields are in the snapshot ──────────────


def _evil_codex_home(tmp_path) -> Path:
    """A codex home whose config names a profile that turns an account feature on."""
    codex = tmp_path / "evil_home" / ".codex"
    codex.mkdir(parents=True)
    (codex / "config.toml").write_text('profile = "work"\n[profiles.work.features]\napps = true\n')
    return codex


def _clean_home(tmp_path) -> Path:
    home = tmp_path / "clean_home"
    (home / ".codex").mkdir(parents=True)
    return home


def test_a_bytes_env_key_is_judged_by_the_name_it_becomes_at_spawn(tmp_path, no_git):
    """``b"CODEX_HOME"`` decodes to ``CODEX_HOME`` as the spawn encodes it; the containment
    check reads the snapshot's own copy, so the codex home codex will read is the one the
    check judged, and the request is refused before a process starts."""
    evil = _evil_codex_home(tmp_path)
    request = _contained(tmp_path).model_copy(
        update={"env": {b"CODEX_HOME": str(evil), "HOME": str(_clean_home(tmp_path))}}
    )

    seen = _spawn(no_git, request)

    assert codex_mod.ContainmentRefusedError is not None
    assert isinstance(seen.get("raised"), ValueError)
    assert "cannot be combined with profile = 'work'" in str(seen["raised"])
    assert "cmd" not in seen


def test_an_env_value_whose_encoder_answers_another_codex_home_is_refused(tmp_path, no_git):
    """A value whose own ``encode`` answers a different codex home than its buffer holds is
    read the way the spawn encodes it, so the home the child would read is the home the
    check judged."""
    evil = _evil_codex_home(tmp_path)
    _EnvValueWhoseEncoderAnswersElsewhere.answer = str(evil)
    request = _contained(tmp_path).model_copy(
        update={
            "env": {
                "CODEX_HOME": _EnvValueWhoseEncoderAnswersElsewhere("/safe/.codex"),
                "HOME": str(_clean_home(tmp_path)),
            }
        }
    )

    seen = _spawn(no_git, request)

    assert isinstance(seen.get("raised"), ValueError)
    assert "cmd" not in seen


def test_the_environment_the_check_read_is_the_environment_the_child_gets(tmp_path, no_git):
    """A clean codex home passes containment, and the environment handed to the spawn is the
    snapshot's own copy: a key the caller spelled as bytes reaches the child as its name."""
    home = _clean_home(tmp_path)
    request = _contained(tmp_path).model_copy(
        update={"env": {b"CODEX_HOME": str(home / ".codex"), "HOME": str(home)}}
    )

    seen = _spawn(no_git, request)

    assert "raised" not in seen
    assert seen["env"] == codex_mod._plain_request(request).env
    assert seen["env"]["CODEX_HOME"] == str(home / ".codex")


def test_a_text_output_schema_whose_own_str_hides_the_text_it_carries(tmp_path):
    """A ``str`` subclass whose buffer is an absolute path while its own ``__str__`` answers a
    safe relative name is judged by the text it carries, so the absolute path is refused
    instead of passing a check as the safe answer and rendering the absolute one."""
    absolute = str(tmp_path / "out.json")

    with pytest.raises(Exception, match="absolute path"):
        _contained(tmp_path, output_schema=_TextWhoseStrLies(absolute))


def test_a_path_like_output_schema_whose_read_escapes_after_the_check(tmp_path, no_git):
    """A path-like value whose ``__str__`` answers a safe name for its first reads and an
    absolute path after is read once while the request is built, so the text containment
    judged is the text the command line renders: plain text is stored, the safe text reaches
    argv, and the later absolute answer never does."""
    request = _contained(tmp_path, output_schema=_PathLikeWhoseStrAnswersSafeFirst("safe.json"))

    assert request.output_schema == "safe.json"
    args = request.as_cmd_args()
    assert _values_after(args, "--output-schema") == ["safe.json"]
    assert "/etc/passwd" not in args

    seen = _spawn(no_git, request)
    assert "raised" not in seen, seen.get("raised")
    assert _values_after(seen["cmd"], "--output-schema") == ["safe.json"]
    assert "/etc/passwd" not in seen["cmd"]


def test_an_output_schema_whose_own_str_hides_an_absolute_path_is_refused(tmp_path):
    """A value whose own ``__str__`` answers a safe name while its buffer is an absolute path
    is judged by the text the command line would render, so containment refuses it rather
    than passing the safe answer and rendering the absolute one."""
    absolute = str(tmp_path / "out.json")

    with pytest.raises(Exception, match="absolute path"):
        _contained(
            tmp_path,
            output_schema=_PathWhoseStrAnswersSafeFirst(absolute, then="/etc/passwd"),
        )


def test_a_path_like_output_schema_that_escapes_later_is_transparent(tmp_path, no_git):
    """A path-like value whose ``__str__`` answers a safe path first and an absolute path
    after is read once into the snapshot, so the text the containment check judged is the
    text the command line renders: the safe text is what reaches argv."""
    request = _contained(tmp_path).model_copy(
        update={"output_schema": _PathLikeWhoseStrAnswersSafeFirst("safe.json")}
    )

    seen = _spawn(no_git, request)

    assert "raised" not in seen, seen.get("raised")
    assert _values_after(seen["cmd"], "--output-schema") == ["safe.json"]
    assert "/etc/passwd" not in seen["cmd"]


def test_an_image_whose_text_hides_a_nul_behind_a_clean_encoder_is_refused(tmp_path, no_git):
    """An image whose text holds a NUL while its own ``encode`` answers clean bytes is
    reduced to its plain text before ``os.fsencode`` reads it, so the NUL reaches the
    command-line check and the image never becomes an argument."""
    request = _contained(tmp_path).model_copy(
        update={"images": [_TextHoldingANulBehindACleanEncoder("bad\x00image.png")]}
    )

    seen = _spawn(no_git, request)

    assert isinstance(seen.get("raised"), ValueError)
    assert "NUL" in str(seen["raised"])
    assert "cmd" not in seen


def test_an_image_whose_encoder_raises_is_read_as_its_plain_text(tmp_path, no_git):
    """An image whose own ``encode`` raises is read as the plain text of its path, so the
    request builds and the argument is that text with no raised error carrying it."""
    request = _contained(tmp_path).model_copy(
        update={"images": [_TextWhoseEncoderRaises("assets/shot.png")]}
    )

    seen = _spawn(no_git, request)

    assert "raised" not in seen
    assert _values_after(seen["cmd"], "-i") == ["assets/shot.png"]


def test_a_path_field_value_that_is_not_a_path_is_refused_by_name(tmp_path, no_git):
    """A value with no ``__fspath__`` and no text is refused by the field's name and its type,
    never rendered as a path."""
    request = _contained(tmp_path).model_copy(
        update={"images": [_CannotBeAPath()], "add_dir": [_CannotBeAPath()]}
    )

    with pytest.raises(TypeError) as refused:
        request.as_cmd_args()
    assert "images entry 0" in str(refused.value)
    assert "_CannotBeAPath" in str(refused.value)
    seen = _spawn(no_git, request)
    assert isinstance(seen.get("raised"), TypeError)
    assert "cmd" not in seen


def test_a_copied_add_dir_entry_reaches_the_command_line_as_its_plain_text(tmp_path, no_git):
    """An add_dir entry whose own ``__str__`` answers another path is read once as its plain
    text, so the grant the child is given is the grant the request holds."""
    request = _contained(tmp_path).model_copy(
        update={"add_dir": [_TextWhoseStrLies(str(tmp_path))]}
    )
    expected = str(tmp_path)

    seen = _spawn(no_git, request)

    assert "raised" not in seen
    assert expected in seen["cmd"]
    assert HOSTILE not in seen["cmd"]


def test_the_connector_grant_the_check_reads_is_the_grant_the_spawn_reads(tmp_path, no_git):
    """A connector list whose own truthiness answers ``False`` is read as its plain entries,
    so naming a connector lifts containment on the spawn path as it does on the check."""
    request = _contained(tmp_path).model_copy(
        update={
            "account_connectors": _ListWhoseTruthinessDiffers(
                [_TextThatReadsAsSomethingElse("github")]
            ),
            "enable_features": ["web_search"],
        }
    )

    seen = _spawn(no_git, request)

    assert "raised" not in seen
    assert _values_after(seen["cmd"], "--enable") == ["web_search"]
    assert "apps" not in seen["cmd"]
    assert "plugins" not in seen["cmd"]
    assert seen["cmd"][1:] == request.as_cmd_args()


# ── a mechanical guard: the snapshot is the only reader ──────────────────────

# Every function whose read of a request field the snapshot must serve. A raw read of one of
# those fields here is a decision made on an object the command line will not render.
_CHECK_AND_SPAWN_PATH: dict[str, set[str]] = {
    "codex.py": {
        "_contain_path_fields_in_repo",
        "_refuse_a_profile_under_containment",
        "_refuse_ambiguous_approval",
        "_refuse_oversized_cli_inputs",
        "_refuse_oversized_command",
        "as_cmd_args",
        "_build_declarative_args",
        "_settled",
        "_contains_account_integrations",
        "_argument_text_fields",
        "_ndjson_from_cli",
        "_refuse_an_unusable_working_directory",
    },
    "_cli_subprocess.py": {
        "raise_if_env_is_not_a_string_map",
        "unencodable_env_entry",
        "env_as_the_spawn_encodes_it",
        "ndjson_from_cli",
        "build_declarative_cli_args",
    },
}

# The one function that may read the request's own fields: it is what builds the snapshot.
# It is also the control below, so a detector that finds nothing fails rather than passes.
_ALLOWED_READERS = {"_plain_request", "_plain_path_fields"}


def _snapshot_reads(path: Path, targets: set[str]) -> dict[str, list[str]]:
    """Every ``self.<f>``/``request.<f>``/``getattr(request, "<f>")`` read of a snapshot field
    inside the named functions of ``path``, keyed by function name."""
    fields = _snapshot_field_names() | {"config_overrides"}
    tree = ast.parse(path.read_text())
    found: dict[str, list[str]] = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or node.name not in targets:
            continue
        reads: list[str] = []
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Attribute)
                and isinstance(inner.value, ast.Name)
                and inner.value.id in ("self", "request", "model_instance")
                and inner.attr in fields
            ):
                reads.append(f"{inner.value.id}.{inner.attr}")
            elif (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "getattr"
                and len(inner.args) >= 2
                and isinstance(inner.args[0], ast.Name)
                and inner.args[0].id in ("self", "request", "model_instance")
                and isinstance(inner.args[1], ast.Constant)
                and inner.args[1].value in fields
            ):
                reads.append(f"getattr({inner.args[0].id}, {inner.args[1].value!r})")
        found[node.name] = reads
    return found


def test_the_check_and_spawn_path_read_the_snapshot_and_no_raw_field():
    """No function on the check or spawn path reads a request field the snapshot carries.

    The field list is read off ``_PlainRequest``, so a field added to the snapshot without
    moving its readers moves nothing here and this fails. ``_plain_request`` is the control
    in the same pass: it reads raw fields, so a detector that finds nothing anywhere fails
    rather than passing quietly.
    """
    import lionhub.providers._cli_subprocess as subproc_mod

    files = {"codex.py": Path(codex_mod.__file__), "_cli_subprocess.py": Path(subproc_mod.__file__)}
    checked = 0
    for filename, targets in _CHECK_AND_SPAWN_PATH.items():
        found = _snapshot_reads(files[filename], targets)
        assert found, f"no function of {filename} was scanned, so the check is vacuous"
        checked += len(found)
        raw = {
            name: reads for name, reads in found.items() if reads and name not in _ALLOWED_READERS
        }
        assert not raw, f"{filename} reads a raw request field on the check or spawn path: {raw}"

    control = _snapshot_reads(files["codex.py"], {"_plain_request"})
    assert control["_plain_request"], "the control read found nothing, so the detector is blind"
    assert checked >= 10, "too few functions were scanned for the enumeration to mean anything"
