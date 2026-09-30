import asyncio
import typing
from typing import Generic, Literal, TypeVar

import pytest
from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, Field, create_model

from lionagi import Actor, Profile, RunRequest, Success
from lionagi.spec import Operable, field_key, render_guidance, spec_name


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
    first = _said_path(key, "leaf").removesuffix('["leaf"]')
    rest = _said_path("leaf", key).removeprefix("leaf[").removesuffix("]")
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
