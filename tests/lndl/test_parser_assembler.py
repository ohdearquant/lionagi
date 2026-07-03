# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the LNDL parser, assembler, and normalize pipeline.

Package-level only — no Branch/operate integration. Covers:
- Two-token shortcut resolution via extra_id
- Dict field placeholder rendering and assembly
- Note namespace collect and OUT resolution
- Nested groups for list[Model]
- normalize._fix_missing_gt repair
- _coerce_str_to_list conservative prose handling
- parse_function_call / qualified_name
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from lionagi.lndl import Lexer, Parser, assemble, collect_notes, normalize_lndl_text
from lionagi.lndl.assembler import NOTE_NAMESPACE, _coerce_str_to_list
from lionagi.lndl.ast import Lact, RLvar
from lionagi.lndl.normalize import _fix_missing_gt

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(text: str):
    """Convenience: lex + parse and return the Program."""
    lexer = Lexer(text)
    tokens = lexer.tokenize()
    parser = Parser(tokens, source_text=text)
    return parser.parse()


# ---------------------------------------------------------------------------
# 1. Two-token shortcut resolution
# ---------------------------------------------------------------------------


class TestTwoTokenShortcut:
    """OUT{alias} with a two-token tag resolves alias → extra_id (the spec)."""

    def test_lact_two_token_extra_id(self):
        """<lact q1 a>call()</lact> sets extra_id='q1', alias='a'."""
        text = "<lact q1 a>multiply(number1=3, number2=4)</lact>\nOUT{a}"
        prog = _parse(text)

        assert len(prog.lacts) == 1
        lact = prog.lacts[0]
        assert isinstance(lact, Lact)
        assert lact.alias == "a"
        assert lact.extra_id == "q1"
        assert lact.model is None
        assert lact.field is None

    def test_lact_two_token_out_resolves_to_spec(self):
        """OUT{a} where 'a' is a two-token lact alias resolves spec to 'q1'."""
        text = "<lact q1 a>multiply(number1=3, number2=4)</lact>\nOUT{a}"
        prog = _parse(text)

        # The OUT block must have mapped the alias 'a' to spec 'q1'
        assert prog.out_block is not None
        assert "q1" in prog.out_block.fields
        assert "a" in prog.out_block.fields["q1"]

    def test_lvar_two_token_extra_id(self):
        """<lvar quality a>0.92</lvar> sets extra_id='quality', alias='a'."""
        text = "<lvar quality a>0.92</lvar>\nOUT{a}"
        prog = _parse(text)

        assert len(prog.lvars) == 1
        lvar = prog.lvars[0]
        assert isinstance(lvar, RLvar)
        assert lvar.alias == "a"
        assert lvar.extra_id == "quality"
        assert lvar.content == "0.92"

    def test_lvar_two_token_out_resolves_to_spec(self):
        """OUT{a} where 'a' is a two-token lvar alias resolves spec to 'quality'."""
        text = "<lvar quality a>0.92</lvar>\nOUT{a}"
        prog = _parse(text)

        assert prog.out_block is not None
        assert "quality" in prog.out_block.fields
        assert "a" in prog.out_block.fields["quality"]

    def test_single_token_lvar_no_extra_id(self):
        """<lvar alias>...</lvar> (single token) has extra_id=None."""
        text = "<lvar myfield>value</lvar>\nOUT{myfield}"
        prog = _parse(text)

        assert len(prog.lvars) == 1
        lvar = prog.lvars[0]
        assert isinstance(lvar, RLvar)
        assert lvar.extra_id is None

    def test_two_token_lact_content_extracted_correctly(self):
        """Body content of a two-token lact is extracted without the tags."""
        text = "<lact compute result>add(x=1, y=2)</lact>\nOUT{compute}"
        prog = _parse(text)

        lact = prog.lacts[0]
        assert lact.call == "add(x=1, y=2)"
        assert lact.alias == "result"
        assert lact.extra_id == "compute"


# ---------------------------------------------------------------------------
# 2. Dict field placeholder
# ---------------------------------------------------------------------------


class DictModel(BaseModel):
    data: dict[str, str]


class TestDictFieldPlaceholder:
    """<lvar data.key1 a>...</lvar> style → dict assembler."""

    def test_assembler_produces_dict(self):
        text = "<lvar data.key1 a>hello</lvar>\n<lvar data.key2 b>world</lvar>\nOUT{data: [a, b]}"
        prog = _parse(text)
        result = assemble(prog, DictModel)

        assert result == {"data": {"key1": "hello", "key2": "world"}}

    def test_model_validate_succeeds(self):
        text = "<lvar data.key1 a>hello</lvar>\n<lvar data.key2 b>world</lvar>\nOUT{data: [a, b]}"
        prog = _parse(text)
        result = assemble(prog, DictModel)
        validated = DictModel.model_validate(result)

        assert validated.data == {"key1": "hello", "key2": "world"}

    def test_lvar_field_attribute_is_dict_key(self):
        """Each lvar's field attribute becomes the dict key."""
        text = "<lvar data.alpha a>v1</lvar>\n<lvar data.beta b>v2</lvar>\nOUT{data: [a, b]}"
        prog = _parse(text)

        lvars = {lv.alias: lv for lv in prog.lvars}
        assert lvars["a"].field == "alpha"
        assert lvars["b"].field == "beta"

    def test_dict_assembly_with_three_keys(self):
        text = (
            "<lvar info.x a>1</lvar>\n"
            "<lvar info.y b>2</lvar>\n"
            "<lvar info.z c>3</lvar>\n"
            "OUT{info: [a, b, c]}"
        )

        class InfoModel(BaseModel):
            info: dict[str, str]

        prog = _parse(text)
        result = assemble(prog, InfoModel)
        assert result == {"info": {"x": "1", "y": "2", "z": "3"}}


# ---------------------------------------------------------------------------
# 3. Note namespace
# ---------------------------------------------------------------------------


class TestNoteNamespace:
    """note.X lvar declarations and cross-round OUT resolution."""

    def test_collect_notes_returns_note_lvars(self):
        text = (
            "<lvar note.outline a>My outline</lvar>\n"
            "<lvar note.draft b>Draft text</lvar>\n"
            "OUT{plan: [a]}"
        )
        prog = _parse(text)
        notes = collect_notes(prog)

        assert notes == {"outline": "My outline", "draft": "Draft text"}

    def test_collect_notes_ignores_non_note_lvars(self):
        text = (
            "<lvar note.outline a>My outline</lvar>\n"
            "<lvar result b>Final result</lvar>\n"
            "OUT{plan: [a]}"
        )
        prog = _parse(text)
        notes = collect_notes(prog)

        # Only note.* entries returned
        assert set(notes.keys()) == {"outline"}

    def test_out_note_ref_resolves_from_same_program(self):
        """OUT{plan: [note.outline]} resolves from notes declared in the same program."""

        class PlanModel(BaseModel):
            plan: str

        text = "<lvar note.outline a>My outline content</lvar>\nOUT{plan: [note.outline]}"
        prog = _parse(text)
        result = assemble(prog, PlanModel)

        assert result == {"plan": "My outline content"}

    def test_out_note_ref_resolves_from_scratchpad(self):
        """OUT{plan: [note.outline]} resolves when the note was declared in a prior round."""

        class PlanModel(BaseModel):
            plan: str

        text = "<lvar result b>Final result</lvar>\nOUT{plan: [note.outline]}"
        prog = _parse(text)
        result = assemble(prog, PlanModel, scratchpad={"outline": "Prior round outline"})

        assert result == {"plan": "Prior round outline"}

    def test_note_namespace_constant(self):
        """NOTE_NAMESPACE should be 'note'."""
        assert NOTE_NAMESPACE == "note"


# ---------------------------------------------------------------------------
# 4. Nested groups for list[Model]
# ---------------------------------------------------------------------------


class Item(BaseModel):
    name: str
    score: float


class Container(BaseModel):
    items: list[Item]


class TestNestedGroupsListModel:
    """OUT{items: [[n1, s1], [n2, s2]]} → list of 2 validated Item instances."""

    def test_assembler_produces_two_items(self):
        text = (
            "<lvar Item.name n1>Apple</lvar>\n"
            "<lvar Item.score s1>0.9</lvar>\n"
            "<lvar Item.name n2>Banana</lvar>\n"
            "<lvar Item.score s2>0.7</lvar>\n"
            "OUT{items: [[n1, s1], [n2, s2]]}"
        )
        prog = _parse(text)
        result = assemble(prog, Container)

        assert isinstance(result["items"], list)
        assert len(result["items"]) == 2

    def test_model_validate_produces_items(self):
        text = (
            "<lvar Item.name n1>Apple</lvar>\n"
            "<lvar Item.score s1>0.9</lvar>\n"
            "<lvar Item.name n2>Banana</lvar>\n"
            "<lvar Item.score s2>0.7</lvar>\n"
            "OUT{items: [[n1, s1], [n2, s2]]}"
        )
        prog = _parse(text)
        result = assemble(prog, Container)
        validated = Container.model_validate(result)

        assert validated.items[0].name == "Apple"
        assert validated.items[0].score == pytest.approx(0.9)
        assert validated.items[1].name == "Banana"
        assert validated.items[1].score == pytest.approx(0.7)

    def test_out_block_contains_nested_lists(self):
        """Parser must parse [[n1, s1], [n2, s2]] as list-of-lists."""
        text = (
            "<lvar Item.name n1>Apple</lvar>\n"
            "<lvar Item.score s1>0.9</lvar>\n"
            "OUT{items: [[n1, s1]]}"
        )
        prog = _parse(text)

        fields = prog.out_block.fields
        assert "items" in fields
        assert isinstance(fields["items"], list)
        assert isinstance(fields["items"][0], list)

    def test_three_items_nested_groups(self):
        text = (
            "<lvar Item.name n1>One</lvar>\n"
            "<lvar Item.score s1>1.0</lvar>\n"
            "<lvar Item.name n2>Two</lvar>\n"
            "<lvar Item.score s2>2.0</lvar>\n"
            "<lvar Item.name n3>Three</lvar>\n"
            "<lvar Item.score s3>3.0</lvar>\n"
            "OUT{items: [[n1, s1], [n2, s2], [n3, s3]]}"
        )
        prog = _parse(text)
        result = assemble(prog, Container)
        validated = Container.model_validate(result)

        assert len(validated.items) == 3
        assert validated.items[2].name == "Three"


# ---------------------------------------------------------------------------
# 5. normalize._fix_missing_gt
# ---------------------------------------------------------------------------


class TestFixMissingGt:
    """_fix_missing_gt repairs <lact alias fn(args)</lact> missing the closing >."""

    def test_single_alias_lact_repair(self):
        bad = "<lact myalias fn(args)</lact>"
        fixed = _fix_missing_gt(bad)
        assert fixed == "<lact myalias>fn(args)</lact>"

    def test_two_token_lact_repair(self):
        """<lact spec alias fn(a=1)</lact> → <lact spec alias>fn(a=1)</lact>."""
        bad = "<lact spec alias fn(a=1)</lact>"
        fixed = _fix_missing_gt(bad)
        assert fixed == "<lact spec alias>fn(a=1)</lact>"

    def test_lvar_missing_gt_repair(self):
        bad = "<lvar q result(1, 2)</lvar>"
        fixed = _fix_missing_gt(bad)
        assert fixed == "<lvar q>result(1, 2)</lvar>"

    def test_well_formed_tag_unchanged(self):
        good = "<lact spec alias>fn(args)</lact>"
        assert _fix_missing_gt(good) == good

    def test_no_parens_tag_unchanged(self):
        """Tags without parentheses in the opening should not be touched."""
        good = "<lact alias>body text here</lact>"
        assert _fix_missing_gt(good) == good

    def test_normalize_lndl_text_calls_fix(self):
        """normalize_lndl_text applies _fix_missing_gt before other passes."""
        bad = "<lact myalias multiply(x=1, y=2)</lact>\nOUT{myalias}"
        result = normalize_lndl_text(bad)
        # After repair the tag should be well-formed
        assert "<lact myalias>multiply(x=1, y=2)</lact>" in result


# ---------------------------------------------------------------------------
# 6. _coerce_str_to_list conservative behavior
# ---------------------------------------------------------------------------


class TestCoerceStrToList:
    """_coerce_str_to_list must preserve prose as single-item and split JSON correctly."""

    def test_prose_with_commas_is_single_item(self):
        prose = "Step 1, then step 2, then step 3"
        result = _coerce_str_to_list(prose)
        assert result == [prose]

    def test_json_array_splits(self):
        result = _coerce_str_to_list('["a", "b", "c"]')
        assert result == ["a", "b", "c"]

    def test_bracketed_comma_list_splits(self):
        result = _coerce_str_to_list("[alpha, beta, gamma]")
        assert result == ["alpha", "beta", "gamma"]

    def test_newline_separated_splits(self):
        result = _coerce_str_to_list("item1\nitem2\nitem3")
        assert result == ["item1", "item2", "item3"]

    def test_empty_string_returns_empty(self):
        result = _coerce_str_to_list("")
        assert result == []

    def test_single_word_returns_single_item(self):
        result = _coerce_str_to_list("hello")
        assert result == ["hello"]

    def test_python_list_literal_splits(self):
        result = _coerce_str_to_list("['x', 'y']")
        assert result == ["x", "y"]

    def test_prose_with_many_commas_not_fragmented(self):
        """Comma-heavy prose must not be shredded into noise items."""
        prose = "First, second, third, fourth, and fifth"
        result = _coerce_str_to_list(prose)
        # Must be preserved as one item, not split at every comma
        assert len(result) == 1
        assert result[0] == prose


# ===========================================================================
# 7. parse_function_call and qualified_name
# ===========================================================================


class TestParseFunctionCall:
    def test_simple_call(self):
        from lionagi.lndl._parse_function_call import parse_function_call

        result = parse_function_call('search(query="AI")')
        assert result["action"] == "search"
        assert result["arguments"] == {"query": "AI"}
        assert "service" not in result

    def test_namespaced_call(self):
        from lionagi.lndl._parse_function_call import parse_function_call

        result = parse_function_call('memory.recall(query="context")')
        assert result["service"] == "memory"
        assert result["action"] == "recall"
        assert result["arguments"] == {"query": "context"}

    def test_qualified_name_simple(self):
        from lionagi.lndl._parse_function_call import (
            parse_function_call,
            qualified_name,
        )

        parsed = parse_function_call('search(query="x")')
        assert qualified_name(parsed) == "search"

    def test_qualified_name_namespaced(self):
        from lionagi.lndl._parse_function_call import (
            parse_function_call,
            qualified_name,
        )

        parsed = parse_function_call("svc.tool(x=1)")
        assert qualified_name(parsed) == "svc.tool"

    def test_reserved_keyword_escaping(self):
        from lionagi.lndl._parse_function_call import parse_function_call

        result = parse_function_call('search(from="2024-01-01")')
        assert result["arguments"]["from_"] == "2024-01-01"

    def test_invalid_call_raises(self):
        from lionagi.lndl._parse_function_call import parse_function_call

        with pytest.raises(ValueError, match="Invalid function call"):
            parse_function_call("not a call at all")

    def test_nested_dict_arg(self):
        from lionagi.lndl._parse_function_call import parse_function_call

        result = parse_function_call('fn(config={"key": "val"})')
        assert result["arguments"]["config"] == {"key": "val"}

    def test_batch_parse(self):
        from lionagi.lndl._parse_function_call import parse_batch_function_calls

        results = parse_batch_function_calls('[search(q="a"), search(q="b")]')
        assert len(results) == 2
        assert results[0]["action"] == "search"
        assert results[1]["arguments"]["q"] == "b"
