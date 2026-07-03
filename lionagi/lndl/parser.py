# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""LNDL Parser — recursive descent parser for structured output tags."""

import ast
import re
import warnings
from typing import Any

from .ast import Lact, Lvar, OutBlock, Program, RLvar
from .lexer import Token, TokenType

_warned_action_names: set[str] = set()

PYTHON_RESERVED = {
    "and",
    "as",
    "assert",
    "async",
    "await",
    "break",
    "class",
    "continue",
    "def",
    "del",
    "elif",
    "else",
    "except",
    "finally",
    "for",
    "from",
    "global",
    "if",
    "import",
    "in",
    "is",
    "lambda",
    "nonlocal",
    "not",
    "or",
    "pass",
    "raise",
    "return",
    "try",
    "while",
    "with",
    "yield",
    "print",
    "input",
    "open",
    "len",
    "range",
    "list",
    "dict",
    "set",
    "tuple",
    "str",
    "int",
    "float",
    "bool",
    "type",
}


class ParseError(Exception):
    def __init__(self, message: str, token: Token):
        self.message = message
        self.token = token
        super().__init__(f"Parse error at line {token.line}, column {token.column}: {message}")


class Parser:
    """Recursive descent parser for LNDL. Not thread-safe."""

    def __init__(self, tokens: list[Token], source_text: str | None = None):
        self.tokens = tokens
        self.pos = 0
        self.source_text = source_text

    def current_token(self) -> Token:
        if self.pos >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[self.pos]

    def peek_token(self, offset: int = 1) -> Token:
        peek_pos = self.pos + offset
        if peek_pos >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[peek_pos]

    def advance(self) -> None:
        if self.pos < len(self.tokens) - 1:
            self.pos += 1

    def expect(self, token_type: TokenType) -> Token:
        token = self.current_token()
        if token.type != token_type:
            raise ParseError(f"Expected {token_type.name}, got {token.type.name}", token)
        self.advance()
        return token

    def match(self, *token_types: TokenType) -> bool:
        return self.current_token().type in token_types

    def skip_newlines(self) -> None:
        while self.match(TokenType.NEWLINE):
            self.advance()

    def parse(self) -> Program:
        if self.source_text is None:
            raise ParseError(
                "Parser requires source_text for content extraction",
                self.current_token(),
            )

        lvars: list[Lvar] = []
        lacts: list[Lact] = []
        out_block: OutBlock | None = None
        aliases: set[str] = set()

        while not self.match(TokenType.EOF):
            self.skip_newlines()
            if self.match(TokenType.EOF):
                break

            if self.match(TokenType.LVAR_OPEN):
                lvar = self.parse_lvar()
                if lvar.alias in aliases:
                    raise ParseError(
                        f"Duplicate alias '{lvar.alias}' - aliases must be unique across lvars and lacts",
                        self.current_token(),
                    )
                aliases.add(lvar.alias)
                lvars.append(lvar)
                continue

            if self.match(TokenType.LACT_OPEN):
                lact = self.parse_lact()
                if lact.alias in aliases:
                    raise ParseError(
                        f"Duplicate alias '{lact.alias}' - aliases must be unique across lvars and lacts",
                        self.current_token(),
                    )
                aliases.add(lact.alias)
                lacts.append(lact)
                continue

            if self.match(TokenType.OUT_OPEN):
                self._lvars_so_far = lvars
                self._lacts_so_far = lacts
                out_block = self.parse_out_block()
                break

            self.advance()

        return Program(lvars=lvars, lacts=lacts, out_block=out_block)

    def parse_lvar(self) -> Lvar | RLvar:
        self.expect(TokenType.LVAR_OPEN)
        self.skip_newlines()

        first_id = self.expect(TokenType.ID).value
        extra_id: str | None = None

        if self.match(TokenType.DOT):
            self.advance()
            field = self.expect(TokenType.ID).value
            model = first_id

            if self.match(TokenType.ID):
                alias = self.current_token().value
                self.advance()
                has_explicit_alias = True
            else:
                alias = field
                has_explicit_alias = False

            is_raw = False
        elif self.match(TokenType.ID):
            # Forgiving 2-ID no-dot pattern: <lvar name alias>...</lvar>
            # First ID is treated as a redundant hint, second as the alias.
            alias = self.current_token().value
            self.advance()
            extra_id = first_id
            model = None
            field = None
            has_explicit_alias = True
            is_raw = True
        else:
            alias = first_id
            model = None
            field = None
            has_explicit_alias = False
            is_raw = True

        self.expect(TokenType.GT)
        self.skip_newlines()

        if not self.source_text:
            raise ParseError(
                "Parser requires source_text for content extraction",
                self.current_token(),
            )

        if is_raw:
            if extra_id:
                pattern = rf"<lvar\s+{re.escape(extra_id)}\s+{re.escape(alias)}\s*>(.*?)</lvar>"
            else:
                pattern = rf"<lvar\s+{re.escape(alias)}\s*>(.*?)</lvar>"
        else:
            if has_explicit_alias:
                pattern = rf"<lvar\s+{re.escape(model)}\.{re.escape(field)}\s+{re.escape(alias)}\s*>(.*?)</lvar>"
            else:
                pattern = rf"<lvar\s+{re.escape(model)}\.{re.escape(field)}\s*>(.*?)</lvar>"

        match = re.search(pattern, self.source_text, re.DOTALL)
        if not match:
            if "</lvar>" not in self.source_text:
                raise ParseError("Unclosed lvar tag - missing </lvar>", self.current_token())
            raise ParseError(
                f"Could not extract lvar content with pattern: {pattern}",
                self.current_token(),
            )

        content = match.group(1).strip()

        while not self.match(TokenType.LVAR_CLOSE):
            if self.match(TokenType.EOF):
                raise ParseError("Unclosed lvar tag - missing </lvar>", self.current_token())
            self.advance()

        self.expect(TokenType.LVAR_CLOSE)

        if is_raw:
            return RLvar(alias=alias, content=content, extra_id=extra_id)
        return Lvar(model=model, field=field, alias=alias, content=content)

    def parse_lact(self) -> Lact:
        self.expect(TokenType.LACT_OPEN)
        self.skip_newlines()

        first_id = self.expect(TokenType.ID).value
        has_explicit_alias = False
        extra_id: str | None = None

        if self.match(TokenType.DOT):
            self.advance()
            field = self.expect(TokenType.ID).value
            model = first_id

            if self.match(TokenType.ID):
                alias = self.current_token().value
                self.advance()
                has_explicit_alias = True
            else:
                alias = field
                has_explicit_alias = False
        elif self.match(TokenType.ID):
            # Forgiving 2-ID no-dot pattern: <lact name alias>...</lact>
            # First ID is a redundant hint (often a function name); second is the alias.
            alias = self.current_token().value
            self.advance()
            extra_id = first_id
            model = None
            field = None
            has_explicit_alias = True
        else:
            model = None
            field = None
            alias = first_id
            has_explicit_alias = True

        self.expect(TokenType.GT)
        self.skip_newlines()

        if not self.source_text:
            raise ParseError(
                "Parser requires source_text for call extraction", self.current_token()
            )

        if model:
            if has_explicit_alias:
                pattern = rf"<lact\s+{re.escape(model)}\.{re.escape(field)}\s+{re.escape(alias)}\s*>(.*?)</lact>"
            else:
                pattern = rf"<lact\s+{re.escape(model)}\.{re.escape(field)}\s*>(.*?)</lact>"
        elif extra_id:
            pattern = rf"<lact\s+{re.escape(extra_id)}\s+{re.escape(alias)}\s*>(.*?)</lact>"
        else:
            pattern = rf"<lact\s+{re.escape(alias)}\s*>(.*?)</lact>"

        match = re.search(pattern, self.source_text, re.DOTALL)
        if not match:
            if "</lact>" not in self.source_text:
                raise ParseError("Unclosed lact tag - missing </lact>", self.current_token())
            raise ParseError(
                f"Could not extract lact call with pattern: {pattern}",
                self.current_token(),
            )

        call = match.group(1).strip()

        while not self.match(TokenType.LACT_CLOSE):
            if self.match(TokenType.EOF):
                raise ParseError("Unclosed lact tag - missing </lact>", self.current_token())
            self.advance()

        self.expect(TokenType.LACT_CLOSE)

        if alias in PYTHON_RESERVED and alias not in _warned_action_names:
            _warned_action_names.add(alias)
            warnings.warn(
                f"Action name '{alias}' is a Python reserved keyword or builtin.",
                UserWarning,
                stacklevel=2,
            )

        return Lact(model=model, field=field, alias=alias, call=call, extra_id=extra_id)

    def _parse_out_list(self) -> list:
        """Parse one bracketed list (refs or nested groups) starting at LBRACKET.

        Returns ``list[str]`` for flat refs and ``list[list[str]]`` when the
        contents are themselves bracketed (e.g. ``[[a, b], [c, d]]``).
        """
        self.expect(TokenType.LBRACKET)
        self.skip_newlines()
        items: list = []
        while not self.match(TokenType.RBRACKET, TokenType.EOF):
            self.skip_newlines()
            if self.match(TokenType.RBRACKET, TokenType.EOF):
                break
            if self.match(TokenType.LBRACKET):
                items.append(self._parse_out_list())
            elif self.match(TokenType.ID):
                name = self.current_token().value
                self.advance()
                while self.match(TokenType.DOT):
                    self.advance()
                    if not self.match(TokenType.ID):
                        break
                    name = f"{name}.{self.current_token().value}"
                    self.advance()
                items.append(name)
            elif self.match(TokenType.STR) or self.match(TokenType.NUM):
                items.append(self.current_token().value)
                self.advance()
            else:
                self.advance()
            self.skip_newlines()
            if self.match(TokenType.COMMA):
                self.advance()
        if self.match(TokenType.RBRACKET):
            self.advance()
        return items

    def _resolve_alias_to_spec(self, alias: str) -> str | None:
        """Look up an alias in already-parsed lvars/lacts and return its implied spec name.

        Resolution priority (most-specific first):
          1. Declared field on a ``Model.field`` form   → returns the field name
          2. Declared model on a ``Model.field`` form   → returns the model name
          3. Two-token hint on a ``<l_ hint alias>`` form → returns the hint
        Returns None when the alias has no spec context (single-token raw form).
        """
        for la in getattr(self, "_lacts_so_far", []) or []:
            if la.alias == alias:
                return la.field or la.model or getattr(la, "extra_id", None)
        for lv in getattr(self, "_lvars_so_far", []) or []:
            if lv.alias == alias:
                return (
                    getattr(lv, "field", None)
                    or getattr(lv, "model", None)
                    or getattr(lv, "extra_id", None)
                )
        return None

    def parse_out_block(self) -> OutBlock:
        self.expect(TokenType.OUT_OPEN)
        self.skip_newlines()

        fields: dict[str, list[str] | str | int | float | bool] = {}

        while not self.match(TokenType.OUT_CLOSE, TokenType.EOF):
            self.skip_newlines()
            if self.match(TokenType.OUT_CLOSE, TokenType.EOF):
                break

            # Anonymous group: OUT{[a, b], [c, d]}
            if self.match(TokenType.LBRACKET):
                group = self._parse_out_list()
                # Find spec for first alias in group; bind whole group to it.
                first = group[0] if group else None
                spec = None
                if isinstance(first, str):
                    spec = self._resolve_alias_to_spec(first)
                elif isinstance(first, list) and first and isinstance(first[0], str):
                    spec = self._resolve_alias_to_spec(first[0])
                if spec:
                    existing = fields.get(spec)
                    if isinstance(existing, list):
                        existing.append(group)
                    else:
                        fields[spec] = [group]
                self.skip_newlines()
                if self.match(TokenType.COMMA):
                    self.advance()
                continue

            if not self.match(TokenType.ID):
                self.advance()
                continue

            field_name = self.current_token().value
            self.advance()
            # Consume dotted continuation (e.g. ``note.X``) so it stays one
            # logical ref through the shortcut path.
            while self.match(TokenType.DOT):
                self.advance()
                if not self.match(TokenType.ID):
                    break
                field_name = f"{field_name}.{self.current_token().value}"
                self.advance()
            self.skip_newlines()

            # Shortcut: OUT{a, b}  — bare alias, no colon. Resolve to its declared spec.
            if not self.match(TokenType.COLON):
                # ``note.X`` shortcut: routes to the schema field whose
                # declared spec name was set when the lvar was parsed
                # (``<lvar Field.name … note.X …>`` is unusual, so the typical
                # path is to look up which spec the note was declared under).
                spec = self._resolve_alias_to_spec(field_name)
                if spec is None and "." in field_name:
                    # Pure note.X with no host spec — caller must use explicit
                    # OUT{spec: [note.X]} form. We still surface it under its
                    # last segment so a downstream tool can hint the user.
                    head = field_name.split(".", 1)[0]
                    spec = head
                if spec is None:
                    spec = field_name  # fall back: treat the bare ID as a literal spec
                fields.setdefault(spec, [])
                if isinstance(fields[spec], list):
                    fields[spec].append(field_name)
                self.skip_newlines()
                if self.match(TokenType.COMMA):
                    self.advance()
                continue

            self.expect(TokenType.COLON)
            self.skip_newlines()

            if self.match(TokenType.LBRACKET):
                fields[field_name] = self._parse_out_list()

            elif self.match(TokenType.STR):
                fields[field_name] = self.current_token().value
                self.advance()

            elif self.match(TokenType.NUM):
                num_str = self.current_token().value
                self.advance()
                fields[field_name] = float(num_str) if "." in num_str else int(num_str)

            elif self.match(TokenType.ID):
                value = self.current_token().value
                self.advance()
                if value.lower() == "true":
                    fields[field_name] = True
                elif value.lower() == "false":
                    fields[field_name] = False
                else:
                    while self.match(TokenType.DOT):
                        self.advance()
                        if not self.match(TokenType.ID):
                            break
                        value = f"{value}.{self.current_token().value}"
                        self.advance()
                    fields[field_name] = [value]
            else:
                self.advance()

            self.skip_newlines()
            if self.match(TokenType.COMMA):
                self.advance()

        if self.match(TokenType.OUT_CLOSE):
            self.advance()

        return OutBlock(fields=fields)


def parse_value(value_str: Any) -> Any:
    if not isinstance(value_str, str):
        return value_str
    value_str = value_str.strip()
    if value_str.lower() == "true":
        return True
    if value_str.lower() == "false":
        return False
    if value_str.lower() == "null":
        return None
    try:
        return ast.literal_eval(value_str)
    except (ValueError, SyntaxError):
        return value_str


__all__ = ("ParseError", "Parser")
