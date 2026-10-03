import asyncio
import typing
from typing import Generic, Literal, TypeVar

import pytest
from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, Field, create_model

import lion1.spec as spec_module
from lion1 import Actor, Profile, RunRequest, Success
from lion1.actor import _fields
from lion1.spec import Operable, field_key, render_guidance, spec_name


class ReadFile(BaseModel):
    """Read a file from the workspace.

    The rest of the docstring is for people, not the model.
    """

    path: str = Field(description="relative to the workspace root")
    limit: int | None = Field(None, description="lines to read")
    flags: list[str] = []
    mode: Literal["text", "bytes"] = "text"


class Grep(BaseModel):
    pattern: str


class Echo(BaseModel):
    """Say it back."""

    __spec_name__ = "say"
    text: str


class OtherGrep(BaseModel):
    __spec_name__ = "grep"
    pattern: str
    flags: str = ""


class Aliased(BaseModel):
    path: str = Field(alias="file_path")


class ValidationAliased(BaseModel):
    path: str = Field(validation_alias="file_path")


class BothAliases(BaseModel):
    path: str = Field(alias="path_out", validation_alias="file_path")


class GeneratedAlias(BaseModel):
    model_config = ConfigDict(alias_generator=lambda name: f"file_{name}")
    path: str


class PathOrName(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    summary: str = Field(validation_alias=AliasPath("report", "summary"))


class PathOrNameByValidate(BaseModel):
    model_config = ConfigDict(validate_by_name=True)
    summary: str = Field(validation_alias=AliasPath("report", "summary"))


# `validate_by_name` arrived in pydantic 2.11; earlier releases ignore the key and read the alias
by_name_setting = pytest.mark.skipif(
    "validate_by_name" not in ConfigDict.__annotations__,
    reason="pydantic before 2.11 has no validate_by_name",
)


class PathThenKey(BaseModel):
    summary: str = Field(validation_alias=AliasChoices(AliasPath("report", "summary"), "alt_summary"))


class KeyTaken(BaseModel):
    a: int = Field(validation_alias=AliasChoices(AliasPath("nested", "a"), "b"))
    b: int


class KeyTakenEarlier(BaseModel):
    b: int
    a: int = Field(validation_alias=AliasChoices(AliasPath("nested", "a"), "b"))


class KeyShared(BaseModel):
    a: int = Field(validation_alias=AliasChoices(AliasPath("x", "a"), "k"))
    c: int = Field(validation_alias=AliasChoices(AliasPath("y", "c"), "k"))


class NameTaken(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    a: int = Field(validation_alias=AliasPath("x", "y"))
    c: int = Field(validation_alias="a")


class FirstPathTaken(BaseModel):
    b: tuple[str, ...]
    a: int = Field(validation_alias=AliasChoices(AliasPath("b", 0), "safe"))


class ReaderCollision(BaseModel):
    a: int = Field(validation_alias=AliasPath("x", "a"))
    b: dict[str, int] = Field(validation_alias=AliasChoices("x", AliasPath("x", "b")))


class EarlierPathTaken(BaseModel):
    b: tuple[str, ...]
    a: int = Field(validation_alias=AliasChoices(AliasPath("n", "a"), AliasPath("b", 0), "safe"))


class SelfShadow(BaseModel):
    a: dict = Field(validation_alias=AliasChoices(AliasPath("x", "y"), "x"))


class LaterPathInside(BaseModel):
    a: dict[str, str] = Field(validation_alias="x")
    b: str = Field(validation_alias=AliasChoices(AliasPath("x", "a"), "x"))


class TwoPathsInside(BaseModel):
    a: dict[str, int] = Field(validation_alias="x")
    b: int = Field(validation_alias=AliasChoices(AliasPath("x", "a"), "x"))
    c: int = Field(validation_alias=AliasPath("x", "c"))


T = TypeVar("T")


class Box(BaseModel, Generic[T]):
    item: T


def rendered_type(annotation) -> str:
    spec = create_model("Probe", value=(annotation, ...))
    head, line = render_guidance(spec).splitlines()
    assert head == "probe"
    return line.removeprefix("  value: ")


# the name


@pytest.mark.parametrize(
    "class_name, name",
    [
        ("Grep", "grep"),
        ("ReadFile", "read_file"),
        ("ReadFileV2", "read_file_v2"),
        ("Grep2Text", "grep2_text"),
        ("HTTPRequest", "http_request"),
        ("getURLPath", "get_url_path"),
        ("ABC", "abc"),
        ("already_snake", "already_snake"),
    ],
)
def test_the_name_is_the_snake_case_of_the_class_name(class_name, name):
    assert spec_name(create_model(class_name)) == name


def test_a_name_the_class_sets_wins():
    assert spec_name(Echo) == "say"
    assert spec_name(OtherGrep) == "grep"


@pytest.mark.parametrize("value", ["", None, 5, b"mail"])
def test_a_blank_or_non_string_spec_name_falls_back_to_the_class_name(value):
    class SendMail(BaseModel):
        __spec_name__ = value

    assert spec_name(SendMail) == "send_mail"


# the guidance


def test_guidance_is_the_name_the_first_doc_line_and_each_field_with_type_default_and_description():
    assert render_guidance(ReadFile) == "\n".join(
        [
            "read_file: Read a file from the workspace.",
            "  path: str  # relative to the workspace root",
            "  limit: int | NoneType = None  # lines to read",
            "  flags: list[str] = []",
            "  mode: 'text' | 'bytes' = 'text'",
        ]
    )


def test_a_spec_with_no_docstring_is_introduced_by_its_name_alone():
    assert render_guidance(Grep) == "grep\n  pattern: str"


def test_only_the_first_line_of_the_docstring_is_shown_trimmed():
    class Padded(BaseModel):
        __doc__ = "\n\n   Count the lines.   \n   More for people.\n"
        path: str

    assert render_guidance(Padded) == "padded: Count the lines.\n  path: str"


def test_the_guidance_uses_the_name_the_class_sets():
    assert render_guidance(Echo) == "say: Say it back.\n  text: str"


def test_a_spec_with_no_fields_is_its_head_line():
    class Stop(BaseModel):
        """End the run."""

    assert render_guidance(Stop) == "stop: End the run."


def test_a_factory_default_is_shown_as_the_value_the_factory_makes():
    class Search(BaseModel):
        roots: list[str] = Field(default_factory=lambda: ["src", "tests"], description="where to look")

    assert render_guidance(Search) == "search\n  roots: list[str] = ['src', 'tests']  # where to look"


def test_a_default_computed_from_the_other_fields_is_shown_as_computed():
    class Window(BaseModel):
        start: int = 0
        end: int = Field(default_factory=lambda data: data["start"] + 10, description="exclusive")

    assert render_guidance(Window) == "window\n  start: int = 0\n  end: int = (computed)  # exclusive"


@pytest.mark.parametrize(
    "annotation, text",
    [
        (int, "int"),
        (str, "str"),
        (int | None, "int | NoneType"),
        (typing.Union[int, str], "int | str"),  # noqa: UP007
        (Literal["x", 2], "'x' | 2"),
        (list[int], "list[int]"),
        (dict[str, list[int]], "dict[str, list[int]]"),
        (tuple[()], "tuple"),
        (list[Literal["a"]] | None, "list['a'] | NoneType"),
        (typing.NewType("UserId", int), "UserId"),
    ],
)
def test_a_field_type_is_rendered_the_way_it_is_written(annotation, text):
    assert rendered_type(annotation) == text


def test_an_unbound_type_variable_is_rendered_by_its_name():
    assert render_guidance(Box) == "box\n  item: T"


@pytest.mark.parametrize("spec", [Aliased, ValidationAliased, BothAliases, GeneratedAlias])
def test_guidance_names_each_field_by_the_key_validation_accepts(spec):
    assert spec.model_validate({"file_path": "a.md"}).path == "a.md"
    key = render_guidance(spec).splitlines()[1].split(":")[0].strip()
    assert spec.model_validate({key: "a.md"}).path == "a.md"


@pytest.mark.parametrize(
    "spec, key",
    [
        (PathOrName, "summary"),
        pytest.param(PathOrNameByValidate, "summary", marks=by_name_setting),
        (PathThenKey, "alt_summary"),
    ],
)
def test_a_field_behind_an_alias_path_is_named_by_one_key_validation_accepts_when_there_is_one(spec, key):
    assert render_guidance(spec).splitlines()[1] == f"  {key}: str"
    assert spec.model_validate({key: "done"}).summary == "done"


COLLIDING = [
    (KeyTaken, {"a": ["nested", "a"], "b": ["b"]}, {"nested": {"a": 1}, "b": 2}),
    (KeyTakenEarlier, {"b": ["b"], "a": ["nested", "a"]}, {"nested": {"a": 1}, "b": 2}),
    (KeyShared, {"a": ["x", "a"], "c": ["y", "c"]}, {"x": {"a": 1}, "y": {"c": 2}}),
    (NameTaken, {"a": ["x", "y"], "c": ["a"]}, {"x": {"y": 1}, "a": 2}),
]
COLLIDING_IDS = [
    "key of a later field",
    "key of an earlier field",
    "key two fields offer",
    "name another alias takes",
]


@pytest.mark.parametrize("spec, keys, data", COLLIDING, ids=COLLIDING_IDS)
def test_a_key_another_field_is_read_from_gives_way_to_a_path_no_other_field_reads(spec, keys, data):
    assert {name: field_key(spec, name) for name in spec.model_fields} == keys


def test_a_later_path_an_earlier_one_is_read_before_never_names_the_field():
    # `b` is read from x first, and x holds a's value too, so x.b would never be read
    assert {name: field_key(ReaderCollision, name) for name in ReaderCollision.model_fields} == {
        "a": ["x", "a"],
        "b": ["x"],
    }
    original = ReaderCollision.model_validate({"x": {"a": 1, "z": 2}})
    assert ReaderCollision.model_validate(_fields(original)) == original


def test_a_key_behind_a_path_inside_another_field_gives_way_to_the_first_path():
    # `safe` is read only when b[0] is missing, and b holds a value: a is named by n.a, read first
    assert field_key(EarlierPathTaken, "a") == ["n", "a"]
    original = EarlierPathTaken.model_validate({"n": {"a": 5}, "b": ["7"]})
    assert original.a == 5
    assert EarlierPathTaken.model_validate(_fields(original)) == original


def test_a_key_a_path_read_before_it_lies_inside_gives_way_to_that_path():
    # x.y is read before x, so a value written at x that holds y would be read from x.y
    assert field_key(SelfShadow, "a") == ["x", "y"]
    original = SelfShadow.model_validate({"x": {"y": {"y": {"k": 2}}}})
    assert original.a == {"y": {"k": 2}}
    assert SelfShadow.model_validate(_fields(original)) == original


@pytest.mark.parametrize("b, a", [([], 9), (["7"], 7)], ids=["b empty", "b holding an item"])
def test_a_field_whose_first_path_lies_inside_another_field_keeps_its_one_key(b, a):
    # b[0] is read before `safe` and lies inside b's value, which a rebuilt spec holds as it was: a
    # read from b[0] is read from there again, and one read from `safe` finds b[0] missing again
    assert field_key(FirstPathTaken, "a") == ["safe"]
    original = FirstPathTaken.model_validate({"b": b, "safe": 9})
    assert original.a == a
    assert FirstPathTaken.model_validate(_fields(original)) == original


@pytest.mark.parametrize(
    "spec, keys, data",
    [
        (LaterPathInside, {"a": ["x"], "b": ["x", "a"]}, {"x": {"a": "B", "z": "Z"}}),
        (TwoPathsInside, {"a": ["x"], "b": ["x", "a"], "c": ["x", "c"]}, {"x": {"a": 1, "c": 2, "z": 3}}),
    ],
    ids=["one path inside", "two paths inside"],
)
def test_a_field_no_key_keeps_apart_is_named_by_the_first_path_validation_tries(spec, keys, data):
    # every key of b and c lies in or around a's x: no choice reads every field back, so each field is
    # named by its first path, and b, read from inside a's value, is written there again
    assert {name: field_key(spec, name) for name in spec.model_fields} == keys
    original = spec.model_validate(data)
    assert spec.model_validate(_fields(original)) == original


class Child(BaseModel):
    child: str


def many_choices(n: int) -> type[BaseModel]:
    # first is read from inside blocker's x or from safe, which decoy reads around; each other field
    # chooses between two keys no other field reads
    fields = {
        "first": (str, Field(validation_alias=AliasChoices(AliasPath("x", "child"), "safe"))),
        "blocker": (Child, Field(validation_alias="x")),
        "decoy": (str, Field(validation_alias=AliasChoices("d", AliasPath("safe", "child")))),
    }
    for i in range(n):
        fields[f"f{i}"] = (str, Field(validation_alias=AliasChoices(f"p{i}", f"q{i}")))
    return create_model("ManyChoices", **fields)


def test_fields_that_share_no_path_with_others_do_not_crowd_out_the_choices_of_those_that_do():
    # across the whole spec, decoy and ten fields of two choices each put 2048 choices ahead of the first
    # that offers first safe
    spec = many_choices(10)
    assert field_key(spec, "first") == ["safe"]
    original = spec.model_validate({"x": {"child": "C"}, "d": "D", **{f"p{i}": str(i) for i in range(10)}})
    assert spec.model_validate(_fields(original)) == original


def test_guidance_and_a_rebuilt_record_each_choose_the_keys_once(monkeypatch):
    calls = 0
    reads_back = spec_module._reads_back

    def counted(keys, paths):
        nonlocal calls
        calls += 1
        return reads_back(keys, paths)

    monkeypatch.setattr(spec_module, "_reads_back", counted)
    spec = many_choices(200)
    render_guidance(spec)
    assert 0 < calls < 2 * len(spec.model_fields)
    calls = 0
    spec = many_choices(200)
    record = spec.model_validate({"x": {"child": "C"}, "d": "D", **{f"p{i}": str(i) for i in range(200)}})
    _fields(record)
    assert 0 < calls < 2 * len(spec.model_fields)


@pytest.mark.parametrize(
    "config",
    [
        pytest.param({"populate_by_name": True}, id="names read too"),
        pytest.param(
            {"validate_by_alias": False, "validate_by_name": True}, id="names only", marks=by_name_setting
        ),
    ],
)
def test_a_key_follows_a_config_changed_after_the_key_was_first_named(config):
    spec = create_model("Rebuilt", value=(str, Field(validation_alias=AliasPath("old", "v"))))
    assert field_key(spec, "value") == ["old", "v"]
    spec.model_config.update(config)
    spec.model_rebuild(force=True)
    assert field_key(spec, "value") == ["value"]
    original = spec.model_validate({"value": "ok"})
    assert spec.model_validate(_fields(original)) == original


def test_a_key_handed_out_is_the_callers_own():
    spec = many_choices(1)
    key = field_key(spec, "first")
    held = list(key)
    key.append("y")
    assert field_key(spec, "first") == held


@pytest.mark.parametrize("spec, keys, data", COLLIDING, ids=COLLIDING_IDS)
def test_a_spec_rebuilt_from_its_fields_keeps_every_value(spec, keys, data):
    original = spec.model_validate(data)
    assert original.model_dump() == {"a": 1, **({"b": 2} if "b" in spec.model_fields else {"c": 2})}
    assert spec.model_validate(_fields(original)) == original


@pytest.mark.parametrize(
    "path, said, data",
    [
        (("report", "summary"), 'report["summary"]', {"report": {"summary": "done"}}),
        (("wire", "texts", 0), 'wire["texts"][0]', {"wire": {"texts": ["done"]}}),
    ],
)
def test_a_field_read_only_through_an_alias_path_is_said_as_the_nesting_it_reads(path, said, data):
    spec = create_model("PathOnly", summary=(str, Field(validation_alias=AliasPath(*path))))
    assert render_guidance(spec).splitlines()[1] == f"  {said}: str"
    assert spec.model_validate(data).summary == "done"


@by_name_setting
def test_a_spec_that_turns_reading_by_name_off_names_a_path_field_by_its_path():
    # validate_by_name=False wins over populate_by_name=True, as validation reads them
    spec = create_model(
        "NamesOff",
        __config__=ConfigDict(populate_by_name=True, validate_by_name=False),
        summary=(str, Field(validation_alias=AliasPath("report", "summary"))),
    )
    assert render_guidance(spec).splitlines()[1] == '  report["summary"]: str'
    assert spec.model_validate({"report": {"summary": "done"}}).summary == "done"


@pytest.mark.parametrize(
    "config",
    [
        ConfigDict(validate_by_name=True),
        ConfigDict(populate_by_name=True, validate_by_name=False),
        ConfigDict(validate_by_alias=False, validate_by_name=True),
    ],
)
def test_a_field_is_named_by_a_key_the_installed_pydantic_reads_whatever_the_config_carries(config):
    # before 2.11 a config may carry validate_by_name and validate_by_alias, and validation ignores them
    spec = create_model(
        "Settings", __config__=config, summary=(str, Field(validation_alias=AliasPath("report", "summary")))
    )
    *outer, last = field_key(spec, "summary")
    data = {last: "done"}
    for k in reversed(outer):
        data = {k: data}
    assert spec.model_validate(data).summary == "done"


@pytest.mark.parametrize(
    "alias, said, data",
    [
        (AliasPath("report", "leaf"), 'report["leaf"]', {"report": {"leaf": "done"}}),
        (AliasPath("wire-texts", "leaf"), '"wire-texts"["leaf"]', {"wire-texts": {"leaf": "done"}}),
        (AliasPath("résumé", "leaf"), 'résumé["leaf"]', {"résumé": {"leaf": "done"}}),
        (AliasPath("标题", "leaf"), '"标题"["leaf"]', {"标题": {"leaf": "done"}}),
        (AliasPath("first\nsecond", "leaf"), '"first\\nsecond"["leaf"]', {"first\nsecond": {"leaf": "done"}}),
        (AliasPath('a"b', "leaf"), '"a\\"b"["leaf"]', {'a"b': {"leaf": "done"}}),
        (AliasPath("a[0]", "leaf"), '"a[0]"["leaf"]', {"a[0]": {"leaf": "done"}}),
        (
            AliasPath("zero\u200bwidth", "leaf"),
            '"zero\\u200bwidth"["leaf"]',
            {"zero\u200bwidth": {"leaf": "done"}},
        ),
        (
            AliasPath("line\u2028break", "leaf"),
            '"line\\u2028break"["leaf"]',
            {"line\u2028break": {"leaf": "done"}},
        ),
        ("two words", '"two words"', {"two words": "done"}),
        ("", '""', {"": "done"}),
    ],
)
def test_a_key_is_said_bare_only_when_it_reads_as_itself(alias, said, data):
    spec = create_model("Keyed", summary=(str, Field(validation_alias=alias)))
    assert render_guidance(spec).split("\n")[1] == f"  {said}: str"
    assert spec.model_validate(data).summary == "done"


def _said_path(first: str | int, rest: str | int) -> str:
    spec = create_model("PathSpec", value=(str, Field(validation_alias=AliasPath(first, rest))))
    return render_guidance(spec).split("\n")[1].removeprefix("  ").rsplit(": ", 1)[0]


@pytest.mark.parametrize("form", ["tag", "program"])
@pytest.mark.parametrize(
    "key",
    [
        "report",
        "résumé",
        "wire-texts",
        "标题",
        "123",
        "class",
        "None",
        "__x",
        "e\u0301",
        "\ufb01eld",
        "🦁",
        "a#b",
        "a:b",
        "a'b",
        "{}",
        "a b",
        "a\u00a0b",
        "a\tb",
        "a\u0085b",
        "a\ufeffb",
        "first\nsecond",
        'a"b',
        "a\\b",
        "a[0]",
        "zero\u200bwidth",
        "line\u2028break",
        "tag\U000e0001",
        "",
    ],
)
def test_the_actor_fills_a_path_field_through_the_keys_its_guidance_says(key, form):
    """What the guidance says a path is, written back as the dict it names, fills the field: in an OUT{}
    tag and in a program, whichever of the key's positions it stands in."""
    leaf = _said_path("leaf", "leaf").removesuffix('["leaf"]')  # however the guidance says `leaf` first
    first = _said_path(key, "leaf").removesuffix('["leaf"]')
    rest = _said_path("leaf", key).removeprefix(leaf + "[").removesuffix("]")
    spec = create_model("PathSpec", value=(str, Field(validation_alias=AliasPath(key, key))))
    out = f'OUT{{path_spec: {{{first}: {{{rest}: "done"}}}}}}'
    turn = out if form == "tag" else f"Working on it.\n```\n{out}\n```\n"
    profile = Profile("p") if form == "tag" else Profile("p", language="program")

    async def backend(messages):
        return turn

    run = asyncio.run(
        Actor("t").run(profile, RunRequest(inputs=("go",), emits=(spec,), max_rounds=1), backend)
    )
    assert isinstance(run.outcome, Success), run.refused
    assert run.output["path_spec"].value == "done"


# the Operable


def test_specs_are_kept_by_name_in_registration_order():
    op = Operable([Grep, ReadFile, Echo])
    assert list(op) == ["grep", "read_file", "say"]
    assert len(op) == 3
    assert op.names() == frozenset({"grep", "read_file", "say"})
    assert op.get("say") is Echo
    assert op.get("read_file") is ReadFile
    assert op.get("echo") is None
    assert "say" in op
    assert "echo" not in op
    assert Echo not in op


def test_an_empty_operable_offers_nothing():
    op = Operable()
    assert (list(op), len(op), op.names(), op.guidance()) == ([], 0, frozenset(), "")


def test_add_returns_the_name_the_spec_is_said_under():
    op = Operable()
    assert op.add(ReadFile) == "read_file"
    assert op.add(Echo) == "say"
    assert list(op) == ["read_file", "say"]


def test_registering_the_same_class_twice_is_one_registration():
    op = Operable([Grep])
    assert op.add(Grep) == "grep"
    assert len(op) == 1
    assert op.get("grep") is Grep


def test_a_second_class_under_a_taken_name_is_refused_and_the_first_stays():
    op = Operable([Grep])
    with pytest.raises(ValueError, match="spec 'grep' is already registered by Grep"):
        op.add(OtherGrep)
    assert op.get("grep") is Grep
    assert len(op) == 1
    with pytest.raises(ValueError, match="spec 'grep' is already registered by OtherGrep"):
        Operable([OtherGrep, ReadFile, Grep])


def test_a_subset_holds_the_named_specs_in_the_order_asked():
    op = Operable([Grep, ReadFile, Echo])
    sub = op.subset(["say", "grep"])
    assert isinstance(sub, Operable) and sub is not op
    assert list(sub) == ["say", "grep"]
    assert sub.get("say") is Echo
    assert list(op) == ["grep", "read_file", "say"]


def test_a_subset_reads_a_generator_once():
    op = Operable([Grep, ReadFile, Echo])
    sub = op.subset(n for n in ["read_file", "grep"])
    assert list(sub) == ["read_file", "grep"]


def test_a_subset_may_repeat_a_name_and_may_be_empty():
    op = Operable([Grep, ReadFile])
    assert list(op.subset(["grep", "grep"])) == ["grep"]
    assert len(op.subset([])) == 0


def test_a_subset_naming_a_spec_not_registered_is_refused_with_every_missing_name():
    op = Operable([Grep, ReadFile])
    # eight missing names, so an unsorted listing would match the sorted one only by rare chance
    with pytest.raises(KeyError) as caught:
        op.subset(["zed", "grep", "nope", "mail", "fetch", "act", "bash", "look", "yank"])
    missing = "['act', 'bash', 'fetch', 'look', 'mail', 'nope', 'yank', 'zed']"
    assert caught.value.args == (f"specs not registered here: {missing}",)


def test_guidance_lists_every_spec_in_registration_order_separated_by_a_blank_line():
    op = Operable([Echo, Grep, ReadFile])
    assert op.guidance() == "\n\n".join(
        [render_guidance(Echo), render_guidance(Grep), render_guidance(ReadFile)]
    )
    assert op.guidance().startswith("say: Say it back.\n  text: str\n\ngrep\n  pattern: str\n\nread_file: ")
