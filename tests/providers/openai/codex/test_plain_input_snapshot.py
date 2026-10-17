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

import asyncio

import pytest

from lionagi.providers.openai import codex as codex_mod
from lionagi.providers.openai.codex import CodexCodeRequest, toml_override_value

SECRET = "sk-PLAIN-SNAPSHOT-SECRET"
# The answer every hostile value gives when read by its own methods instead of its text.
HOSTILE = "HOSTILE-ANSWER"
GENERATED_PROFILE = "lionagi-mcp-" + "0" * 32
RAW_PAYLOAD = 'x", apps = true, y = "'


class _TextThatReadsAsSomethingElse(str):
    """A ``str`` subclass whose text is one value and whose own readers answer another.

    ``__str__``, ``__format__`` and ``encode`` answer ``HOSTILE``; ``__eq__`` answers no
    for every other value, so a check that compares with ``==`` reads one answer and the
    command line another, and only the base reader ``str.__str__`` gives the text.
    """

    def __str__(self):
        return HOSTILE

    def __format__(self, spec=""):
        return HOSTILE

    def encode(self, *args, **kwargs):  # noqa: ARG002
        return HOSTILE.encode()

    def __eq__(self, other):
        return False

    def __hash__(self):
        return 0


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
        [_TextThatReadsAsSomethingElse("github")],
        ["github"],
        None,
    ),
}

# A bool cannot be subclassed, so the hostile for a bool field is a non-bool value: the
# snapshot refuses it by the field's name and its type, never by any text.
BOOLEAN_FIELDS: tuple[str, ...] = (
    "bypass_approvals",
    "full_auto",
    "search",
    "fast_mode",
    "contain_account_integrations",
)


def _contained(tmp_path, **kwargs) -> CodexCodeRequest:
    kwargs.setdefault("prompt", "hi")
    return CodexCodeRequest(repo=tmp_path, contain_account_integrations=True, **kwargs)


# The snapshot names the reduced `config_overrides` field `overrides`; every other field
# keeps its own name. The invariant test reads each field off the snapshot through this.
_SNAPSHOT_NAME = {"config_overrides": "overrides"}


def _spawn(monkeypatch, request) -> dict:
    """The command the spawn would run, or the error raised before a process exists.

    No process starts: ``ndjson_from_cli`` is replaced by a recorder that raises before a
    child exists.
    """
    seen: dict = {}

    def spawned(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen["stdin_data"] = kwargs.get("stdin_data")
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


def test_every_reduced_field_has_a_hostile_case():
    """A field added to the reduced set without a case fails here."""
    from lionagi.providers.openai.codex import PLAIN_INPUT_FIELDS

    assert set(FIELD_CASES) | set(BOOLEAN_FIELDS) == set(PLAIN_INPUT_FIELDS)


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


def test_the_prompt_reaches_the_child_as_the_text_the_check_read(tmp_path, no_git):
    """The turn prompt travels over the child's stdin; the bytes written are the plain text
    the check measured, not the hostile answer of the caller's object."""
    request = _contained(tmp_path).model_copy(
        update={"prompt": _TextThatReadsAsSomethingElse("hi")}
    )

    seen = _spawn(no_git, request)

    assert seen["stdin_data"] == codex_mod._plain_request(request).prompt == "hi"


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
    from lionagi.providers import _cli_subprocess

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
