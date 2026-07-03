# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for lionagi.lndl.assembler — ActionCall placeholders, list/dict
assembly paths, and grouped-list salvage, beyond what test_parser_assembler.py
already covers for the note-namespace and nested-group happy paths."""

from __future__ import annotations

from pydantic import BaseModel

from lionagi.lndl.assembler import (
    assemble,
    assemble_spec_value,
    collect_actions,
    replace_actions,
)
from lionagi.lndl.lexer import Lexer
from lionagi.lndl.parser import Parser
from lionagi.lndl.types import ActionCall


def _parse(text: str):
    tokens = Lexer(text).tokenize()
    return Parser(tokens, source_text=text).parse()


class AnswerModel(BaseModel):
    answer: str


class FindingsModel(BaseModel):
    findings: list[str]


class Item(BaseModel):
    name: str
    score: float


class Container(BaseModel):
    items: list[Item]


class TestActionCallPlaceholders:
    """A lact not yet executed becomes an ActionCall placeholder in the assembled dict."""

    def test_unresolved_lact_becomes_action_call(self):
        prog = _parse('<lact q a>lookup(query="x")</lact>\nOUT{answer: [a]}')
        result = assemble(prog, AnswerModel)
        assert isinstance(result["answer"], ActionCall)
        assert result["answer"].name == "a"
        assert result["answer"].function == "lookup"
        assert result["answer"].arguments == {"query": "x"}

    def test_resolved_lact_uses_action_results(self):
        prog = _parse('<lact q a>lookup(query="x")</lact>\nOUT{answer: [a]}')
        result = assemble(prog, AnswerModel, action_results={"a": "found it"})
        assert result["answer"] == "found it"

    def test_malformed_lact_call_dropped_not_raised(self):
        """A lact whose body isn't a parseable function call is silently skipped."""
        prog = _parse("<lact a>not_a_valid_call!!!</lact>\nOUT{answer: [a]}")
        result = assemble(prog, AnswerModel)
        assert result["answer"] is None


class TestCollectAndReplaceActions:
    def test_collect_actions_finds_nested_placeholders(self):
        prog = _parse('<lact q a>lookup(query="x")</lact>\nOUT{answer: [a]}')
        result = assemble(prog, AnswerModel)
        actions = collect_actions(result)
        assert len(actions) == 1
        assert actions[0].name == "a"

    def test_collect_actions_empty_for_no_placeholders(self):
        assert collect_actions({"answer": "plain value"}) == []

    def test_collect_actions_walks_lists(self):
        ac = ActionCall(name="x", function="f", arguments={}, raw_call="f()")
        assert collect_actions([{"a": ac}, {"b": "clean"}]) == [ac]

    def test_replace_actions_substitutes_by_name(self):
        prog = _parse('<lact q a>lookup(query="x")</lact>\nOUT{answer: [a]}')
        result = assemble(prog, AnswerModel)
        resolved = replace_actions(result, {"a": "the answer"})
        assert resolved == {"answer": "the answer"}

    def test_replace_actions_leaves_unmatched_placeholder(self):
        ac = ActionCall(name="missing", function="f", arguments={}, raw_call="f()")
        resolved = replace_actions({"x": ac}, {})
        assert resolved["x"] is ac

    def test_replace_actions_passthrough_scalars(self):
        assert replace_actions("plain", {}) == "plain"
        assert replace_actions(42, {}) == 42


class TestListScalarAssembly:
    def test_multiple_raw_aliases_become_list(self):
        prog = _parse("<lvar a>one</lvar>\n<lvar b>two</lvar>\nOUT{findings: [a, b]}")
        result = assemble(prog, FindingsModel)
        assert result == {"findings": ["one", "two"]}

    def test_single_string_encoded_list_coerced(self):
        prog = _parse('<lvar a>["x", "y"]</lvar>\nOUT{findings: [a]}')
        result = assemble(prog, FindingsModel)
        assert result == {"findings": ["x", "y"]}


class TestGroupedListSalvage:
    """When a nested group's entries are string literals (not declared aliases),
    the assembler salvages by piping joined text into the model's first str field."""

    def test_string_literal_groups_salvaged_into_first_str_field(self):
        prog = _parse('OUT{items: [["raw text one"], ["raw text two"]]}')
        result = assemble(prog, Container)
        assert result["items"] == [
            {"name": "raw text one"},
            {"name": "raw text two"},
        ]


class TestAssembleNoOutBlock:
    def test_program_without_out_block_returns_empty_dict(self):
        prog = _parse("<lvar a>1</lvar>")
        assert assemble(prog, AnswerModel) == {}


class TestAssembleSpecValueDirect:
    def test_dict_origin_no_field_bound_refs_filtered(self):
        """dict[K, V] target: refs with no bound field (raw aliases) contribute nothing."""
        result = assemble_spec_value(["a", "b"], dict[str, str], {}, {}, None)
        assert result is None

    def test_empty_refs_returns_none(self):
        result = assemble_spec_value([], str, {}, {}, None)
        assert result is None

    def test_non_string_alias_in_refs_skipped(self):
        """Defensive: a non-str item in refs (e.g. a stray int) is skipped, not raised."""
        result = assemble_spec_value([1, "missing_alias"], str, {}, {}, None)
        assert result is None

    def test_scalar_target_multiple_aliases_returns_list(self):
        """A scalar-typed spec with more than one bound alias falls back to a list."""
        prog = _parse("<lvar a>1</lvar>\n<lvar b>2</lvar>\nOUT{answer: [a, b]}")
        result = assemble(prog, AnswerModel)
        assert result["answer"] == ["1", "2"]


class TestFlatFieldRepeatListModel:
    """list[Model] via flat (non-nested-bracket) alias repetition: OUT{items: [n1, s1, n2, s2]}."""

    def test_field_repeat_splits_into_two_items(self):
        text = (
            "<lvar Item.name n1>Apple</lvar>\n"
            "<lvar Item.score s1>0.9</lvar>\n"
            "<lvar Item.name n2>Banana</lvar>\n"
            "<lvar Item.score s2>0.7</lvar>\n"
            "OUT{items: [n1, s1, n2, s2]}"
        )
        prog = _parse(text)
        result = assemble(prog, Container)
        assert len(result["items"]) == 2
        assert result["items"][0]["name"] == "Apple"
        assert result["items"][1]["name"] == "Banana"


class PlanModel(BaseModel):
    plan: str


class TestBareNoteLiteralInOut:
    """OUT{plan: note.outline} (bare literal, not a [ref] list) resolves via scratchpad."""

    def test_bare_note_ref_resolves_from_same_program(self):
        prog = _parse("<lvar note.outline a>My outline</lvar>\nOUT{plan: note.outline}")
        result = assemble(prog, PlanModel)
        assert result == {"plan": "My outline"}

    def test_bare_note_ref_resolves_from_scratchpad(self):
        prog = _parse("<lvar x>1</lvar>\nOUT{plan: note.outline}")
        result = assemble(prog, PlanModel, scratchpad={"outline": "from scratchpad"})
        assert result == {"plan": "from scratchpad"}


class TestNestedModelFieldListCoercion:
    """A str-typed alias feeding a nested model's list[str] field gets coerced."""

    def test_json_string_coerced_to_list_in_nested_model(self):
        class Report(BaseModel):
            tags: list[str]

        class Wrap(BaseModel):
            report: Report

        prog = _parse('<lvar Report.tags t>["x", "y"]</lvar>\nOUT{report: [t]}')
        result = assemble(prog, Wrap)
        assert result == {"report": {"tags": ["x", "y"]}}


class TestOptionalFieldUnwrap:
    """Model | None fields must unwrap the Union before the model-coercion
    branch runs, else the assembler falls back to a bare list of values."""

    def test_optional_model_field_coerces_via_unwrapped_type(self):
        class Report(BaseModel):
            title: str
            summary: str

        class Wrapper(BaseModel):
            report: Report | None = None

        prog = _parse(
            "<lvar Report.title t>Analysis</lvar>\n"
            '<lact Report.summary s>summarize(text="x")</lact>\n'
            "OUT{report: [t, s]}"
        )
        result = assemble(prog, Wrapper, action_results={"s": "done"})
        assert result["report"] == {"title": "Analysis", "summary": "done"}
