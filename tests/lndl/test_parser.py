# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for lionagi.lndl.parser — error paths, OUT block forms, parse_value."""

from __future__ import annotations

import warnings

import pytest

from lionagi.lndl.errors import LNDLError
from lionagi.lndl.lexer import Lexer
from lionagi.lndl.parser import ParseError, Parser, parse_value


def _parse(text: str):
    tokens = Lexer(text).tokenize()
    return Parser(tokens, source_text=text).parse()


class TestParseErrors:
    def test_missing_source_text_raises(self):
        tokens = Lexer("<lvar a>1</lvar>").tokenize()
        with pytest.raises(ParseError, match="requires source_text"):
            Parser(tokens).parse()

    def test_unclosed_lvar_raises(self):
        with pytest.raises(ParseError, match="Unclosed lvar tag"):
            _parse("<lvar a>1")

    def test_unclosed_lact_raises(self):
        with pytest.raises(ParseError, match="Unclosed lact tag"):
            _parse("<lact a>fn()")

    def test_duplicate_alias_lvar_lvar_raises(self):
        with pytest.raises(ParseError, match="Duplicate alias"):
            _parse("<lvar a>1</lvar>\n<lvar a>2</lvar>\nOUT{a}")

    def test_duplicate_alias_lvar_lact_raises(self):
        with pytest.raises(ParseError, match="Duplicate alias"):
            _parse("<lvar a>1</lvar>\n<lact a>fn()</lact>\nOUT{a}")

    def test_parse_error_message_has_line_and_column(self):
        try:
            _parse("<lvar a>1")
        except ParseError as e:
            assert e.token is not None
            assert "line" in str(e)
            assert "column" in str(e)
        else:
            pytest.fail("expected ParseError")

    def test_non_reserved_alias_does_not_warn(self):
        """A lact whose ALIAS is not a Python reserved word/builtin never warns."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _parse("<lact q myalias>fn()</lact>\nOUT{myalias}")
        assert caught == []

    def test_reserved_keyword_alias_warns(self):
        """A lact whose ALIAS (not hint) is a Python reserved word/builtin warns."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _parse("<lact hintx len>fn()</lact>\nOUT{len}")
        messages = [str(w.message) for w in caught]
        assert any("reserved keyword or builtin" in m for m in messages)

    def test_malformed_number_double_dot_raises_parse_error(self):
        with pytest.raises(ParseError, match="Invalid number literal"):
            _parse("OUT{x: 1.2.3}")

    def test_malformed_number_repeated_dots_raises_parse_error(self):
        with pytest.raises(ParseError, match="Invalid number literal"):
            _parse("OUT{x: 1..2}")

    def test_malformed_negative_number_raises_parse_error(self):
        with pytest.raises(ParseError, match="Invalid number literal"):
            _parse("OUT{x: -1.2.3}")

    def test_oversized_int_literal_raises_parse_error(self):
        huge = "9" * 100_000
        with pytest.raises(ParseError, match="Invalid number literal"):
            _parse(f"OUT{{x: {huge}}}")

    def test_malformed_number_error_is_lndl_error(self):
        try:
            _parse("OUT{x: 1.2.3}")
        except ParseError as e:
            assert isinstance(e, LNDLError)
        else:
            pytest.fail("expected ParseError")

    def test_parse_error_is_lndl_error(self):
        try:
            _parse("<lvar a>1")
        except ParseError as e:
            assert isinstance(e, LNDLError)
        else:
            pytest.fail("expected ParseError")

    def test_deeply_nested_out_list_raises_parse_error_not_recursion_error(self):
        depth = 40
        text = "OUT{y: " + "[" * depth + "x" + "]" * depth + "}"
        with pytest.raises(ParseError, match="nesting too deep"):
            _parse(text)

    def test_out_list_nesting_within_cap_parses(self):
        depth = 10
        text = "OUT{y: " + "[" * depth + "x" + "]" * depth + "}"
        prog = _parse(text)
        nested = prog.out_block.fields["y"]
        for _ in range(depth - 1):
            assert isinstance(nested, list)
            assert len(nested) == 1
            nested = nested[0]
        assert nested == ["x"]


class TestDottedLact:
    def test_dotted_lact_parses_model_field_alias(self):
        prog = _parse("<lact model.field alias>fn(x=1)</lact>\nOUT{alias}")
        assert len(prog.lacts) == 1
        lact = prog.lacts[0]
        assert lact.model == "model"
        assert lact.field == "field"
        assert lact.alias == "alias"
        assert lact.call == "fn(x=1)"

    def test_dotted_lact_without_explicit_alias_uses_field_as_alias(self):
        prog = _parse("<lact model.field>fn(x=1)</lact>\nOUT{field}")
        assert len(prog.lacts) == 1
        lact = prog.lacts[0]
        assert lact.model == "model"
        assert lact.field == "field"
        assert lact.alias == "field"


class TestOutBlockForms:
    def test_string_value(self):
        prog = _parse('OUT{a: "hello"}')
        assert prog.out_block.fields["a"] == "hello"

    def test_number_value_int(self):
        prog = _parse("OUT{a: 5}")
        assert prog.out_block.fields["a"] == 5
        assert isinstance(prog.out_block.fields["a"], int)

    def test_number_value_float(self):
        prog = _parse("OUT{a: 3.5}")
        assert prog.out_block.fields["a"] == 3.5
        assert isinstance(prog.out_block.fields["a"], float)

    def test_bool_true(self):
        prog = _parse("OUT{a: true}")
        assert prog.out_block.fields["a"] is True

    def test_bool_false(self):
        prog = _parse("OUT{a: false}")
        assert prog.out_block.fields["a"] is False

    def test_dotted_id_value_becomes_list(self):
        prog = _parse("OUT{f: some.dotted}")
        assert prog.out_block.fields["f"] == ["some.dotted"]

    def test_anonymous_group_binds_to_first_alias_spec(self):
        text = "<lvar Item.name n1>Apple</lvar>\n<lvar Item.score s1>0.9</lvar>\nOUT{[n1, s1]}"
        prog = _parse(text)
        assert prog.out_block.fields == {"name": [["n1", "s1"]]}

    def test_bare_shortcut_falls_back_to_alias_as_spec(self):
        """OUT{x} where 'x' has no model/field/hint context falls back to itself."""
        prog = _parse("<lvar x>5</lvar>\nOUT{x}")
        assert prog.out_block.fields == {"x": ["x"]}

    def test_note_shortcut_with_no_host_spec_uses_head_segment(self):
        prog = _parse("<lvar note.draft d>hi</lvar>\nOUT{note.draft}")
        assert prog.out_block.fields == {"note": ["note.draft"]}

    def test_empty_out_block(self):
        prog = _parse("OUT{}")
        assert prog.out_block.fields == {}

    def test_no_out_block_present(self):
        prog = _parse("<lvar a>1</lvar>")
        assert prog.out_block is None


class TestParseValue:
    def test_true(self):
        assert parse_value("true") is True
        assert parse_value("True") is True

    def test_false(self):
        assert parse_value("false") is False

    def test_null(self):
        assert parse_value("null") is None

    def test_int_literal(self):
        assert parse_value("42") == 42

    def test_float_literal(self):
        assert parse_value("3.14") == pytest.approx(3.14)

    def test_non_literal_string_passthrough(self):
        assert parse_value("hello world") == "hello world"

    def test_non_string_passthrough(self):
        assert parse_value(5) == 5

    def test_whitespace_stripped(self):
        assert parse_value("  42  ") == 42
