# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for lionagi.lndl.lexer — pure tokenizer, no parser involved."""

from __future__ import annotations

from lionagi.lndl.lexer import Lexer, Token, TokenType


def _types(text: str) -> list[TokenType]:
    return [t.type for t in Lexer(text).tokenize()]


class TestLexerTags:
    def test_lvar_open_close(self):
        types = _types("<lvar x>v</lvar>")
        assert types[0] is TokenType.LVAR_OPEN
        assert TokenType.LVAR_CLOSE in types

    def test_lact_open_close(self):
        types = _types("<lact x>fn()</lact>")
        assert types[0] is TokenType.LACT_OPEN
        assert TokenType.LACT_CLOSE in types

    def test_out_open_close(self):
        types = _types("OUT{a}")
        assert TokenType.OUT_OPEN in types
        assert TokenType.OUT_CLOSE in types

    def test_unrecognized_angle_bracket_advances(self):
        """A '<' that isn't lvar/lact/close just advances past — no token emitted."""
        tokens = Lexer("<other>").tokenize()
        # No LVAR_OPEN/LACT_OPEN emitted for '<other>'; GT emitted for '>'
        assert TokenType.LVAR_OPEN not in [t.type for t in tokens]
        assert TokenType.GT in [t.type for t in tokens]


class TestLexerLiterals:
    def test_identifier(self):
        tokens = Lexer("hello").tokenize()
        assert tokens[0].type is TokenType.ID
        assert tokens[0].value == "hello"

    def test_identifier_with_underscore(self):
        tokens = Lexer("my_field_1").tokenize()
        assert tokens[0].value == "my_field_1"

    def test_number_outside_out_block(self):
        tokens = Lexer("42").tokenize()
        assert tokens[0].type is TokenType.NUM
        assert tokens[0].value == "42"

    def test_float_number(self):
        tokens = Lexer("3.14").tokenize()
        assert tokens[0].type is TokenType.NUM
        assert tokens[0].value == "3.14"

    def test_negative_number_in_out_block(self):
        tokens = Lexer("OUT{score: -1}").tokenize()
        types_values = [(t.type, t.value) for t in tokens]
        assert (TokenType.NUM, "-1") in types_values

    def test_negative_sign_outside_out_block_not_number(self):
        """A bare '-' outside an OUT{} block is not folded into a negative number."""
        tokens = Lexer("-1").tokenize()
        # Since in_out_block is False, '-' falls through the char-dispatch and
        # is simply advanced past (no dedicated MINUS token type exists).
        assert TokenType.NUM in [t.type for t in tokens]
        num_tok = next(t for t in tokens if t.type is TokenType.NUM)
        assert num_tok.value == "1"

    def test_string_in_out_block(self):
        tokens = Lexer('OUT{name: "hello"}').tokenize()
        str_tok = next(t for t in tokens if t.type is TokenType.STR)
        assert str_tok.value == "hello"

    def test_string_single_quote(self):
        tokens = Lexer("OUT{name: 'hi'}").tokenize()
        str_tok = next(t for t in tokens if t.type is TokenType.STR)
        assert str_tok.value == "hi"

    def test_string_escape_newline(self):
        src = 'OUT{x: "a' + chr(92) + 'nb"}'
        str_tok = next(t for t in Lexer(src).tokenize() if t.type is TokenType.STR)
        assert str_tok.value == "a\nb"

    def test_string_escape_tab(self):
        src = 'OUT{x: "a' + chr(92) + 'tb"}'
        str_tok = next(t for t in Lexer(src).tokenize() if t.type is TokenType.STR)
        assert str_tok.value == "a\tb"

    def test_string_escape_carriage_return(self):
        src = 'OUT{x: "a' + chr(92) + 'rb"}'
        str_tok = next(t for t in Lexer(src).tokenize() if t.type is TokenType.STR)
        assert str_tok.value == "a\rb"

    def test_string_escape_backslash(self):
        src = 'OUT{x: "a' + chr(92) + chr(92) + 'b"}'
        str_tok = next(t for t in Lexer(src).tokenize() if t.type is TokenType.STR)
        assert str_tok.value == "a" + chr(92) + "b"

    def test_string_escape_double_quote(self):
        src = 'OUT{x: "a' + chr(92) + '"b"}'
        str_tok = next(t for t in Lexer(src).tokenize() if t.type is TokenType.STR)
        assert str_tok.value == 'a"b'

    def test_string_escape_unknown_passthrough(self):
        """An escape char with no special case falls through as itself, backslash dropped."""
        src = 'OUT{x: "a' + chr(92) + 'qb"}'
        str_tok = next(t for t in Lexer(src).tokenize() if t.type is TokenType.STR)
        assert str_tok.value == "aqb"

    def test_string_not_tokenized_outside_out_block(self):
        """Quote chars outside an OUT{} block are not lexed as STR tokens."""
        tokens = Lexer('"hello"').tokenize()
        assert TokenType.STR not in [t.type for t in tokens]


class TestLexerPunctuation:
    def test_dot(self):
        assert TokenType.DOT in _types("a.b")

    def test_comma(self):
        assert TokenType.COMMA in _types("OUT{a, b}")

    def test_colon(self):
        assert TokenType.COLON in _types("OUT{a: 1}")

    def test_brackets(self):
        types = _types("OUT{a: [1, 2]}")
        assert TokenType.LBRACKET in types
        assert TokenType.RBRACKET in types

    def test_parens(self):
        types = _types("<lact a>fn(x=1)</lact>")
        assert TokenType.LPAREN in types
        assert TokenType.RPAREN in types

    def test_gt(self):
        assert TokenType.GT in _types("<lvar a>")

    def test_newline(self):
        assert TokenType.NEWLINE in _types("a\nb")

    def test_eof_always_last(self):
        tokens = Lexer("a").tokenize()
        assert tokens[-1].type is TokenType.EOF


class TestLexerPositions:
    def test_line_and_column_tracked(self):
        tokens = Lexer("a\nb").tokenize()
        id_tokens = [t for t in tokens if t.type is TokenType.ID]
        assert id_tokens[0].line == 1
        assert id_tokens[1].line == 2

    def test_whitespace_skipped(self):
        tokens = Lexer("  a   b  ").tokenize()
        ids = [t.value for t in tokens if t.type is TokenType.ID]
        assert ids == ["a", "b"]


class TestToken:
    def test_token_is_dataclass_with_fields(self):
        tok = Token(TokenType.ID, "x", 1, 2)
        assert tok.type is TokenType.ID
        assert tok.value == "x"
        assert tok.line == 1
        assert tok.column == 2
