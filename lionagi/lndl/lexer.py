# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""LNDL Lexer — single-pass tokenizer for structured output tags."""

from dataclasses import dataclass
from enum import Enum, auto


class TokenType(Enum):
    LVAR_OPEN = auto()
    LVAR_CLOSE = auto()
    LACT_OPEN = auto()
    LACT_CLOSE = auto()
    OUT_OPEN = auto()
    OUT_CLOSE = auto()
    ID = auto()
    NUM = auto()
    STR = auto()
    DOT = auto()
    COMMA = auto()
    COLON = auto()
    LBRACKET = auto()
    RBRACKET = auto()
    LPAREN = auto()
    RPAREN = auto()
    GT = auto()
    NEWLINE = auto()
    EOF = auto()


@dataclass
class Token:
    type: TokenType
    value: str
    line: int
    column: int


class Lexer:
    """Single-pass tokenizer for LNDL. Not thread-safe — create per thread."""

    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.line = 1
        self.column = 1
        self.tokens: list[Token] = []

    def current_char(self) -> str | None:
        if self.pos >= len(self.text):
            return None
        return self.text[self.pos]

    def peek_char(self, offset: int = 1) -> str | None:
        peek_pos = self.pos + offset
        if peek_pos >= len(self.text):
            return None
        return self.text[peek_pos]

    def advance(self) -> None:
        if self.pos < len(self.text) and self.text[self.pos] == "\n":
            self.line += 1
            self.column = 0
        else:
            self.column += 1
        self.pos += 1

    def skip_whitespace(self) -> None:
        while (char := self.current_char()) and char in " \t\r":
            self.advance()

    def read_identifier(self) -> str:
        result = ""
        while (char := self.current_char()) and (char.isalnum() or char == "_"):
            result += char
            self.advance()
        return result

    def read_number(self) -> str:
        result = ""
        while (char := self.current_char()) and (char.isdigit() or char == "."):
            result += char
            self.advance()
        return result

    def read_string(self) -> str:
        quote_char = self.current_char()
        self.advance()
        result = ""
        while (char := self.current_char()) and char != quote_char:
            if char == "\\":
                self.advance()
                if escape_char := self.current_char():
                    if escape_char == "n":
                        result += "\n"
                    elif escape_char == "t":
                        result += "\t"
                    elif escape_char == "r":
                        result += "\r"
                    elif escape_char == "\\":
                        result += "\\"
                    elif escape_char == '"':
                        result += '"'
                    elif escape_char == "'":
                        result += "'"
                    else:
                        result += escape_char
                    self.advance()
            else:
                result += char
                self.advance()
        if self.current_char() == quote_char:
            self.advance()
        return result

    def tokenize(self) -> list[Token]:
        in_out_block = False
        while char := self.current_char():
            if char in " \t\r":
                self.skip_whitespace()
                continue

            if char == "\n":
                self.tokens.append(Token(TokenType.NEWLINE, "\n", self.line, self.column))
                self.advance()
                continue

            if char == "<":
                start_line = self.line
                start_column = self.column

                if self.text[self.pos : self.pos + 7] == "</lvar>":
                    self.tokens.append(
                        Token(TokenType.LVAR_CLOSE, "</lvar>", start_line, start_column)
                    )
                    self.pos += 7
                    self.column += 7
                    continue

                if self.text[self.pos : self.pos + 7] == "</lact>":
                    self.tokens.append(
                        Token(TokenType.LACT_CLOSE, "</lact>", start_line, start_column)
                    )
                    self.pos += 7
                    self.column += 7
                    continue

                if self.text[self.pos : self.pos + 5] == "<lvar":
                    self.tokens.append(
                        Token(TokenType.LVAR_OPEN, "<lvar", start_line, start_column)
                    )
                    self.pos += 5
                    self.column += 5
                    continue

                if self.text[self.pos : self.pos + 5] == "<lact":
                    self.tokens.append(
                        Token(TokenType.LACT_OPEN, "<lact", start_line, start_column)
                    )
                    self.pos += 5
                    self.column += 5
                    continue

                self.advance()
                continue

            if self.text[self.pos : self.pos + 4] == "OUT{":
                self.tokens.append(Token(TokenType.OUT_OPEN, "OUT{", self.line, self.column))
                self.pos += 4
                self.column += 4
                in_out_block = True
                continue

            if char.isalpha() or char == "_":
                start_line = self.line
                start_column = self.column
                identifier = self.read_identifier()
                self.tokens.append(Token(TokenType.ID, identifier, start_line, start_column))
                continue

            if char == "-" and in_out_block:
                next_char = self.peek_char()
                if next_char and next_char.isdigit():
                    start_line = self.line
                    start_column = self.column
                    self.advance()
                    number = "-" + self.read_number()
                    self.tokens.append(Token(TokenType.NUM, number, start_line, start_column))
                    continue

            if char.isdigit():
                start_line = self.line
                start_column = self.column
                number = self.read_number()
                self.tokens.append(Token(TokenType.NUM, number, start_line, start_column))
                continue

            if char in "\"'" and in_out_block:
                start_line = self.line
                start_column = self.column
                string_val = self.read_string()
                self.tokens.append(Token(TokenType.STR, string_val, start_line, start_column))
                continue

            if char == ".":
                self.tokens.append(Token(TokenType.DOT, char, self.line, self.column))
            elif char == ",":
                self.tokens.append(Token(TokenType.COMMA, char, self.line, self.column))
            elif char == ":":
                self.tokens.append(Token(TokenType.COLON, char, self.line, self.column))
            elif char == "[":
                self.tokens.append(Token(TokenType.LBRACKET, char, self.line, self.column))
            elif char == "]":
                self.tokens.append(Token(TokenType.RBRACKET, char, self.line, self.column))
            elif char == "(":
                self.tokens.append(Token(TokenType.LPAREN, char, self.line, self.column))
            elif char == ")":
                self.tokens.append(Token(TokenType.RPAREN, char, self.line, self.column))
            elif char == "}":
                self.tokens.append(Token(TokenType.OUT_CLOSE, char, self.line, self.column))
                in_out_block = False
            elif char == ">":
                self.tokens.append(Token(TokenType.GT, char, self.line, self.column))

            self.advance()

        self.tokens.append(Token(TokenType.EOF, "", self.line, self.column))
        return self.tokens


__all__ = ("Lexer", "Token", "TokenType")
