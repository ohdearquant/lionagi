# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Compatibility mode for StateDB: profiles, admission and enforcement (ADR-0128).

A compatibility open writes runs into a store whose schema another application manages. It never
migrates, stamps or reconfigures that store. It admits the store only when the tables a profile
depends on have exactly the structure the profile declares, it serves only the operations the
profile names, and every connection it opens carries a SQLite authorizer that denies whatever the
profile does not list.

Three layers enforce this, because each alone has a known way around it:

- the operation layer: a write runs only inside a StateDB method marked as an operation the profile
  names, and only in a transaction the guard opened for it;
- a statement check on every statement SQLAlchemy sends, which refuses REPLACE forms, every
  conflict clause but ABORT, RETURNING and anything but INSERT and UPDATE inside an operation, and
  anything but a read outside one; with an owner rule it also refuses an INSERT into an owned
  table that does not bind the owner column to the owner, and an UPDATE of one that does not
  restrict its rows to the owner, since the authorizer sees neither values nor WHERE clauses;
- the authorizer, a ceiling on every connection that sees what reaches SQLite by any path: INSERT
  only into the profile's tables, UPDATE only of its columns, a short list of PRAGMAs, and no commit
  of a transaction in which anything was denied.

The authorizer does not see how SQLite resolves a conflict: a row a REPLACE resolution deletes
reaches it as no DELETE, and a ROLLBACK resolution ends the transaction without a callback. So
admission refuses a store whose tables declare any conflict resolution but ABORT, or whose
triggers and views name one, the statement check refuses a statement that names one, and a write
is refused once the transaction its operation opened is no longer open: SQLite ending it leaves
the connection out of a transaction, and a BEGIN the guard did not send clears its mark or,
inside an open transaction, is denied and poisons it. A statement sent to the driver directly,
past the statement check, can still name a conflict clause the authorizer cannot see: an
INSERT OR REPLACE sent that way inside an operation's transaction deletes the row it conflicts
with, and nothing refuses it.
"""

from __future__ import annotations

import functools
import sqlite3
import string
from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from sqlalchemy import event
from sqlalchemy.exc import DBAPIError

__all__ = (
    "Closure",
    "ColumnShape",
    "CompatibilityProfile",
    "ForeignKeyShape",
    "OwnerRule",
    "SchemaObject",
    "StateCompatibilityRefused",
    "TableShape",
    "TableWrites",
    "read_closure",
)


class StateCompatibilityRefused(RuntimeError):  # noqa: N818 -- the name ADR-0128 D6 gives it
    """A compatibility-mode StateDB refused an operation.

    ``committed`` names the transactions of the refused operation that had already committed when
    it was refused. Empty means the refused operation left the store's contents unchanged.
    """

    def __init__(
        self, operation: str, profile: str, reason: str, committed: tuple[str, ...] = ()
    ) -> None:
        self.operation = operation
        self.profile = profile
        self.reason = reason
        self.committed = tuple(committed)
        outcome = (
            f"already committed: {', '.join(self.committed)}"
            if self.committed
            else "nothing was committed"
        )
        super().__init__(
            f"{operation} refused in compatibility mode (profile {profile}): {reason}; {outcome}"
        )

    def __reduce__(self):
        return (type(self), (self.operation, self.profile, self.reason, self.committed))


# SQL tokens

_WORD, _IDENT, _STRING, _BLOB, _PUNCT = "word", "ident", "string", "blob", "punct"
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
# Words after which SQLite reads a double-quoted token as a name, never as a string literal. The
# rule belongs to *spelled* below, which compares a table's or index's definition; a trigger body,
# a view body and a column's DEFAULT are compared verbatim, as they are written. The column list
# after ``REFERENCES <table>``, and a single-quoted token right after ``REFERENCES``, are names too.
_NAME_POSITIONS = frozenset(
    {
        "references",
        "collate",
        "into",
        "from",
        "update",
        "on",
        "join",
        "table",
        "index",
        "trigger",
        "constraint",
    }
)
# Words SQLite never reads as a name when written bare in an expression, whether a column of that
# name is in scope or not: a quoted name spelled like one of them is that name, the bare word is a
# value in an expression, or for ``not`` the NOT operator, so ``not (x)`` negates while
# ``"not" (x)`` calls a function named not. A column may still be declared under one of them bare
# (``current_date TEXT``); its definition keeps the bare word, which the same column declared
# quoted does not equal, so a store that quotes it is refused against one that does not.
# ``true`` and ``false`` are deliberately absent: a bare ``true`` is the value in an expression
# only when no column is named ``true``, and where one is, the column wins and the two spellings
# read alike.
_VALUE_WORDS = frozenset({"null", "current_date", "current_time", "current_timestamp", "not"})


def _fold(name: str) -> str:
    """*name* as SQLite compares names: ASCII letters without regard to case, any other as it is."""
    return name.translate(_ASCII_LOWER)


def _identifier_char(ch: str) -> bool:
    # SQLite reads every character past ASCII as part of an identifier.
    return not ch.isascii() or ch.isalnum() or ch in "_$"


def _spelled_name(name: str) -> str:
    """A quoted name as *spelled* compares it: folded, and bare when a bare word spells it too.

    The folded name is compared bare when a bare word can spell it (it is not empty, does not
    start with a digit and holds only identifier characters) and it is not one of
    :data:`_VALUE_WORDS`, which a bare word reads as a value in an expression, or as an operator
    (``"null"``, ``"not"``); otherwise it is kept in backquotes, with any backquote in it doubled.
    A string literal keeps its double quotes and a bare word has none, so neither can equal the
    backquoted form.
    """
    folded = _fold(name)
    if (
        folded
        and not folded[0].isdigit()
        and all(_identifier_char(ch) for ch in folded)
        and folded not in _VALUE_WORDS
    ):
        return folded
    return "`" + folded.replace("`", "``") + "`"


def _opens_a_call(sql: str, index: int) -> bool:
    """Whether the token at *index*, past optional whitespace, opens a parenthesis.

    A quoted token immediately before one is a name to SQLite, not a string literal: a function's
    name, or in a column's type the name of a sized type (``"VARCHAR"(10)``). Only whitespace is
    skipped: a comment between the token and the parenthesis leaves the token a literal, so such
    a definition is refused even though SQLite reads it as the call.
    """
    while index < len(sql) and sql[index] in " \t\n\v\f\r":
        index += 1
    return sql[index : index + 1] == "("


def _sql_tokens(
    sql: str,
    *,
    spelled: bool = False,
    columns: frozenset[str] = frozenset(),
    verbatim: bool = False,
) -> list[tuple[str, str]]:
    """Split SQL into ``(kind, text)`` tokens, dropping comments and whitespace.

    Bare words are folded as SQLite folds a name, ASCII letters only. A quoted identifier ("x",
    `x`, [x]) is unquoted and folded but keeps its own kind, so a column named "returning" is
    never read as the keyword. A string literal (``'x'``) and a blob literal (``x'41'``, a single
    ``x`` or ``X`` immediately before the opening quote) each keep their text verbatim, quotes
    included, so a literal equals only the same literal written the same way. As in SQLite, only
    ASCII whitespace separates tokens and every character past ASCII belongs to an identifier.

    *verbatim* keeps every token as written, its quotes and the case of its letters included.
    That is how a trigger body, a view body and a column's DEFAULT are compared: whether SQLite
    reads a quoted token as a name or as a string literal depends on the scope it sits in and on
    the connection's double-quoted-string setting, so the text as written is compared rather than
    a reading of it that could differ from SQLite's.

    *spelled* keeps the case of a double-quoted token that SQLite may read as a string literal,
    for comparing a table's definition and an index's terms as ``sqlite_master`` holds them: one
    that names no column is a literal, and keeps its quotes, so ``WHERE status = "running"``
    differs from ``WHERE status = "Running"`` and neither equals the bare word ``running``. A
    double-quoted token is a name where SQLite reads a name: after a dot, after a word that takes
    a name (``REFERENCES "Runs"``, ``ON "Runs"``, ``COLLATE "NOCASE"``, ``CONSTRAINT "uq"``), in
    the column list after ``REFERENCES <table>``, naming one of *columns*, or immediately before a
    ``(``, which makes it a function's name rather than a string. Such a name is compared as
    :func:`_spelled_name` spells it, folded to the bare word where a bare word spells it the same
    and kept in backquotes otherwise (``"null"``, or a name holding a quote). A double-quoted token
    in a type position with no size, as in ``id "TEXT"``, is kept as a double-quoted literal and
    refused against the same type spelled any other way, although SQLite reads it as the type
    name: the comparison refuses that spelling rather than read it as SQLite does. A sized one, as
    in ``id "VARCHAR"(10)``, opens a parenthesis, so its definition reads it as a name and equals
    the bare ``VARCHAR(10)``; SQLite reports that column's type without the size (``VARCHAR``),
    so the column differs and the store is refused all the same.
    """
    tokens: list[tuple[str, str]] = []
    # Inside the parenthesized column list after REFERENCES <table>, where every token is a name.
    in_parent_columns = False
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in " \t\n\v\f\r":
            i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end + 1
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif ch in "xX" and sql[i + 1 : i + 2] == "'":
            # A single x or X immediately before a quote opens a blob literal, one token through
            # its closing quote, kept as written.
            j = i + 2
            while j < n and not (sql[j] == "'" and sql[j + 1 : j + 2] != "'"):
                j += 2 if sql[j] == "'" else 1
            tokens.append((_BLOB, sql[i : j + 1]))
            i = j + 1
        elif ch == "'":
            j = i + 1
            while j < n and not (sql[j] == "'" and sql[j + 1 : j + 2] != "'"):
                j += 2 if sql[j] == "'" else 1
            after_references = bool(tokens) and tokens[-1] == (_WORD, "references")
            if (in_parent_columns or after_references) and not verbatim:
                # SQLite's grammar takes a string in a REFERENCES column list, and the string right
                # after REFERENCES, as the name it spells; these are the only places this reader
                # takes a string as a name.
                name = sql[i + 1 : j].replace("''", "'")
                tokens.append((_IDENT, _spelled_name(name) if spelled else _fold(name)))
            else:
                tokens.append((_STRING, sql[i : j + 1]))
            i = j + 1
        elif ch in '"`[':
            close = "]" if ch == "[" else ch
            j = i + 1
            parts: list[str] = []
            while j < n:
                if sql[j] == close:
                    if close != "]" and sql[j + 1 : j + 2] == close:
                        parts.append(close)
                        j += 2
                        continue
                    break
                parts.append(sql[j])
                j += 1
            name = "".join(parts)
            if verbatim:
                tokens.append((_IDENT, sql[i : j + 1]))
            else:
                last = tokens[-1] if tokens else (_PUNCT, "")
                named = (
                    in_parent_columns
                    or last == (_PUNCT, ".")
                    or (last[0] == _WORD and last[1] in _NAME_POSITIONS)
                    or _opens_a_call(sql, j + 1)
                )
                literal = spelled and ch == '"' and not named and _fold(name) not in columns
                if literal:
                    tokens.append((_IDENT, sql[i : j + 1]))
                else:
                    tokens.append((_IDENT, _spelled_name(name) if spelled else _fold(name)))
            i = j + 1
        elif _identifier_char(ch):
            j = i + 1
            while j < n and _identifier_char(sql[j]):
                j += 1
            tokens.append((_WORD, sql[i:j] if verbatim else _fold(sql[i:j])))
            i = j
        else:
            if ch == "(":
                in_parent_columns = (
                    len(tokens) >= 2
                    and tokens[-2] == (_WORD, "references")
                    and tokens[-1][0] in (_WORD, _IDENT)
                )
            elif ch == ")":
                in_parent_columns = False
            tokens.append((_PUNCT, ch))
            i += 1
    return tokens


def _joined(tokens: list[tuple[str, str]]) -> str:
    """One canonical spelling of *tokens*: single spaces, none inside parentheses, after an
    opening parenthesis or a dot, or before a closing parenthesis, a comma or a dot."""
    out: list[str] = []
    for index, (kind, text) in enumerate(tokens):
        tight = (kind == _PUNCT and text in (")", ",", ".")) or (
            index and tokens[index - 1] in ((_PUNCT, "("), (_PUNCT, "."))
        )
        if out and not tight:
            out.append(" ")
        out.append(text)
    return "".join(out)


def _spelled_sql(sql: str) -> str:
    """*sql* as written: every token its own text, joined by :func:`_joined`, comments and
    whitespace aside."""
    return _joined(_sql_tokens(sql, verbatim=True))


def _top_level_words(tokens: list[tuple[str, str]]) -> list[str]:
    depth = 0
    words: list[str] = []
    for kind, text in tokens:
        if kind == _PUNCT and text == "(":
            depth += 1
        elif kind == _PUNCT and text == ")":
            depth = max(0, depth - 1)
        elif kind == _WORD and depth == 0:
            words.append(text)
    return words


# PRAGMAs a compatibility connection may run once its authorizer is installed. Each reads; the
# table forms take the table or index they describe as their argument.
_READ_PRAGMAS = frozenset(
    {"schema_version", "user_version", "data_version", "journal_mode", "foreign_keys"}
)
_TABLE_PRAGMAS = frozenset(
    {"table_info", "table_xinfo", "index_list", "index_info", "index_xinfo", "foreign_key_list"}
)
_DML = frozenset({"insert", "update", "delete", "replace"})
# What each conflict resolution but ABORT does that the guard cannot see or undo.
_CONFLICT_EFFECTS = {
    "replace": "deletes the row a write conflicts with, and no DELETE reaches the authorizer",
    "rollback": "ends the transaction the operation opened, and its earlier writes with it",
    "ignore": "skips a conflicting row without an error",
    "fail": "keeps what a statement changed before the conflict",
    # The same resolutions as a trigger's RAISE() spells them.
    "raise(rollback)": "ends the transaction the operation opened, and its earlier writes with it",
    "raise(ignore)": "drops the row being written without an error",
    "raise(fail)": "keeps what a statement changed before it",
}


def _name_of(kind: str, text: str) -> str:
    """The name a token may carry, folded as SQLite folds a name.

    A word and a quoted identifier arrive already folded; a string literal carries its quotes, so
    its text is read between them, a doubled quote standing for the character itself.
    """
    if kind == _STRING:
        return _fold(text[1:-1].replace("''", "'"))
    return _fold(text)


def _unquoted(kind: str, text: str) -> str:
    """A token's name, whatever kind of token spells it, folded as SQLite folds a name.

    A bare word is its own text; a double-quoted, backquoted, bracketed or single-quoted token is
    read between its quotes, a doubled quote standing for the character itself. SQLite reads a
    module's name, a table's name and a column's name without regard to how they are quoted.
    """
    if kind == _WORD:
        return _fold(text)
    if kind == _STRING:
        return _fold(text[1:-1].replace("''", "'"))
    if text[:1] in ('"', "`", "["):
        close = "]" if text[:1] == "[" else text[0]
        inner = text[1:-1]
        if close != "]":
            inner = inner.replace(close * 2, close)
        return _fold(inner)
    return _fold(text)


def _names(tokens: list[tuple[str, str]]) -> set[str]:
    """Every token of a trigger or view body that may name an object, folded as SQLite folds names.

    A bare word, a quoted identifier and a quoted string all count, wherever in the body they sit.
    Whether SQLite reads a quoted token as a name or as a string literal depends on the scope it
    appears in and on the connection's double-quoted-string setting, so a quoted token is taken as
    a possible name of a closure table or of another closure object wherever it appears. An object
    that may name one belongs to the closure, and a profile that does not list it refuses the
    store.
    """
    return {_name_of(kind, text) for kind, text in tokens if kind != _PUNCT}


def _resolutions(tokens: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Each conflict resolution but ABORT that a trigger or view names: ``(spelling, action)``."""
    found: list[tuple[str, str]] = []
    for index, token in enumerate(tokens):
        after = tokens[index + 1 : index + 3]
        if (
            token in ((_WORD, "insert"), (_WORD, "update"))
            and len(after) == 2
            and after[0] == (_WORD, "or")
            and after[1][1] != "abort"
        ):
            found.append((f"{token[1].upper()} OR {after[1][1].upper()}", after[1][1]))
        elif (
            token == (_WORD, "replace")
            and after[:1] == [(_WORD, "into")]
            and tokens[index - 1 : index] != [(_WORD, "or")]
        ):
            found.append(("REPLACE INTO", "replace"))
        elif (
            token == (_WORD, "raise")
            and len(after) == 2
            and after[0] == (_PUNCT, "(")
            and after[1][1] != "abort"
        ):
            found.append((f"RAISE({after[1][1].upper()})", f"raise({after[1][1]})"))
    return found


def _name(token: tuple[str, str]) -> str | None:
    """The table or column a token names, folded as SQLite folds a name; SQLite also takes a
    string literal as one."""
    kind, text = token
    if kind == _STRING:
        return _fold(text[1:-1].replace("''", "'"))
    return text if kind in (_WORD, _IDENT) else None


def _depths(tokens: list[tuple[str, str]]) -> list[int]:
    """Each token's parenthesis depth; a parenthesis is at the depth outside the list it bounds."""
    depths: list[int] = []
    depth = 0
    for token in tokens:
        if token == (_PUNCT, ")"):
            depth = max(0, depth - 1)
        depths.append(depth)
        if token == (_PUNCT, "("):
            depth += 1
    return depths


def _spans(tokens: list[tuple[str, str]], start: int) -> tuple[list[tuple[int, int]], int]:
    """The comma-separated items of the list opening at ``tokens[start]`` as ``(first, end)``
    index ranges, and the index of the parenthesis closing it (``len(tokens)`` when none does).
    """
    spans: list[tuple[int, int]] = []
    depth, first = 0, start + 1
    for index in range(start + 1, len(tokens)):
        token = tokens[index]
        if depth == 0 and token in ((_PUNCT, ","), (_PUNCT, ")")):
            spans.append((first, index))
            if token == (_PUNCT, ")"):
                return spans, index
            first = index + 1
        elif token == (_PUNCT, "("):
            depth += 1
        elif token == (_PUNCT, ")"):
            depth -= 1
    return spans, len(tokens)


def _write_target(tokens: list[tuple[str, str]]) -> tuple[str, str | None, int] | None:
    """``(verb, table, after)`` for an INSERT or UPDATE: the table it writes, lowercased, or None
    when it cannot be read, and the index of the token after the table's name. None for any other
    statement.
    """
    depths = _depths(tokens)
    start = next(
        (
            index
            for index, token in enumerate(tokens)
            if depths[index] == 0 and token in ((_WORD, "insert"), (_WORD, "update"))
        ),
        None,
    )
    if start is None:
        return None
    verb = tokens[start][1]
    index = start + 1
    if tokens[index : index + 1] == [(_WORD, "or")]:
        index += 2
    if verb == "insert":
        if tokens[index : index + 1] != [(_WORD, "into")]:
            return verb, None, index
        index += 1
    if tokens[index + 1 : index + 2] == [(_PUNCT, ".")]:
        index += 2  # a schema prefix
    table = _name(tokens[index]) if index < len(tokens) else None
    return verb, table, index + 1


def _duplicate_column(names: list[str | None]) -> str | None:
    """A column an INSERT's column list names more than once, or None when it names none twice.

    SQLite takes the first value a statement binds for a column it names twice and raises no
    error, so the columns an INSERT says it writes are not the columns it writes.
    """
    return next(
        (name for name in names if name is not None and names.count(name) > 1),
        None,
    )


def _insert_columns(tokens: list[tuple[str, str]], after: int) -> list[str | None] | None:
    """The column names an INSERT's column list names, in order, or None when it has no list."""
    if tokens[after : after + 1] != [(_PUNCT, "(")]:
        return None
    columns, _ = _spans(tokens, after)
    return [_name(tokens[first]) if end == first + 1 else None for first, end in columns]


def _insert_owner_values(
    tokens: list[tuple[str, str]], after: int, column: str
) -> list[tuple[int, int]] | str:
    """Where an INSERT writes *column* in each row it writes, as a ``(first, end)`` span of tokens
    per row, or why that cannot be read. *after* indexes the token after the table's name.
    """
    if tokens[after : after + 1] != [(_PUNCT, "(")]:
        return f"leaves out the owner column {column}"
    columns, close = _spans(tokens, after)
    names = [_name(tokens[first]) if end == first + 1 else None for first, end in columns]
    twice = _duplicate_column(names)
    if twice is not None:
        return f"names the column {twice} twice, so the value SQLite writes cannot be read"
    if column not in names:
        return f"leaves out the owner column {column}"
    at = names.index(column)
    index = close + 1
    if tokens[index : index + 1] != [(_WORD, "values")]:
        return "takes its rows from a query, so the owner it writes cannot be read"
    index += 1
    rows: list[tuple[int, int]] = []
    while tokens[index : index + 1] == [(_PUNCT, "(")]:
        items, close = _spans(tokens, index)
        if len(items) != len(names):
            return "writes a row whose values do not match its columns"
        rows.append(items[at])
        index = close + 1
        if tokens[index : index + 1] != [(_PUNCT, ",")]:
            break
        index += 1
    if not rows:
        return "writes rows the guard cannot read"
    depths = _depths(tokens)
    if any(depths[i] == 0 and tokens[i] == (_WORD, "update") for i in range(index, len(tokens))):
        return "is an upsert, whose update can rewrite a row of another owner"
    return rows


def _update_owner_values(
    tokens: list[tuple[str, str]], after: int, column: str
) -> list[tuple[int, int]] | str:
    """What each top-level ``<column> = <value>`` term of an UPDATE's WHERE clause compares the
    column with, as ``(first, end)`` spans of tokens, or why the clause cannot be read.

    Only a WHERE clause that is a conjunction is read, so each term found holds for every row the
    UPDATE writes: a top-level OR, BETWEEN (whose AND is not a conjunction) or CASE refuses.
    """
    depths = _depths(tokens)
    top = [index for index in range(after, len(tokens)) if depths[index] == 0]
    where = next((index for index in top if tokens[index] == (_WORD, "where")), None)
    if where is None:
        return "has no WHERE clause, so it writes the rows of every owner"
    if any(tokens[index] == (_WORD, "from") for index in top if index < where):
        return "joins another table, so the guard cannot read which rows it writes"
    end = next(
        (
            index
            for index in top
            if index > where
            and tokens[index] in ((_WORD, "returning"), (_WORD, "order"), (_WORD, "limit"))
        ),
        len(tokens),
    )
    clause = [index for index in top if where < index < end]
    if any(
        tokens[index] in ((_WORD, "or"), (_WORD, "between"), (_WORD, "case")) for index in clause
    ):
        return (
            "has a WHERE clause that is not a plain conjunction, so its owner term cannot be read"
        )
    cuts = [where, *(index for index in clause if tokens[index] == (_WORD, "and")), end]
    found = [
        value
        for first, last in zip(cuts[:-1], cuts[1:], strict=True)
        if (value := _owner_value(tokens, first + 1, last, column)) is not None
    ]
    return found or f"does not restrict its rows by the owner column {column}"


def _enclosing(tokens: list[tuple[str, str]], first: int, last: int) -> tuple[int, int]:
    """*first* and *last* with each parenthesis pair that encloses the whole span removed."""
    while last - first >= 2 and tokens[first] == (_PUNCT, "("):
        depth, close = 0, None
        for index in range(first, last):
            if tokens[index] == (_PUNCT, "("):
                depth += 1
            elif tokens[index] == (_PUNCT, ")"):
                depth -= 1
                if depth == 0:
                    close = index
                    break
        if close != last - 1:
            break
        first, last = first + 1, last - 1
    return first, last


def _names_column(tokens: list[tuple[str, str]], first: int, last: int, column: str) -> bool:
    """Whether the span is exactly the owner column's name, parentheses aside."""
    first, last = _enclosing(tokens, first, last)
    return last - first == 1 and tokens[first][0] in (_WORD, _IDENT) and tokens[first][1] == column


def _owner_value(
    tokens: list[tuple[str, str]], first: int, last: int, column: str
) -> tuple[int, int] | None:
    """What the term between *first* and *last* compares *column* with, as a token span.

    The comparison is read in either order (``column = value`` and ``value = column``) and through
    parentheses that enclose the term or the value. None when the term does not compare the column
    with one value: an operator other than ``=``, several, or a term that is not a comparison.
    """
    first, last = _enclosing(tokens, first, last)
    at, depth = None, 0
    for index in range(first, last):
        token = tokens[index]
        if token == (_PUNCT, "("):
            depth += 1
        elif token == (_PUNCT, ")"):
            depth -= 1
        elif token == (_PUNCT, "=") and depth == 0:
            if at is not None:
                return None
            at = index
    if at is None:
        return None
    if _names_column(tokens, first, at, column):
        return _enclosing(tokens, at + 1, last)
    if _names_column(tokens, at + 1, last, column):
        return _enclosing(tokens, first, at)
    return None


def _placeholders(tokens: list[tuple[str, str]]) -> list[int] | None:
    """The index of each ``?`` placeholder in order, or None when the statement binds a value in
    another form (``?NNN``, ``:name``, ``@name``, ``$name``, ``#name``), which a position cannot be
    read for. SQLite numbers a named parameter as it numbers ``?``, so a ``?`` after one takes a
    later argument than the position it sits at.
    """
    found: list[int] = []
    for index, (kind, text) in enumerate(tokens):
        after = tokens[index + 1 : index + 2]
        if (kind, text) == (_PUNCT, "?"):
            if after and after[0][0] == _WORD and after[0][1][:1].isdigit():
                return None
            found.append(index)
        elif kind == _PUNCT and text in ":@#" and after and after[0][0] in (_WORD, _IDENT):
            return None
        elif kind == _WORD and text.startswith("$"):
            return None
    return found


def _parameter_rows(parameters: Any, many: bool) -> list[tuple | list] | None:
    """The positional parameter rows a statement runs with, or None when they are not positional."""
    rows = list(parameters) if many else [parameters]
    return rows if all(isinstance(row, (tuple, list)) for row in rows) else None


def _copies_owner(value: list[tuple[str, str]], column: str, rows: tuple[str, ...]) -> bool:
    """Whether *value* reads *column* from a row a trigger's event makes readable (`NEW`, `OLD`)."""
    return (
        len(value) == 3
        and value[0][0] == _WORD
        and value[0][1] in rows
        and value[1] == (_PUNCT, ".")
        and value[2][0] in (_WORD, _IDENT)
        and value[2][1] == column
    )


_EVENT_WORDS = frozenset({(_WORD, "insert"), (_WORD, "update"), (_WORD, "delete")})


def _trigger_event(sql: str) -> str | None:
    """The event a trigger fires on: INSERT, UPDATE or DELETE.

    A created trigger names exactly one event, and SQLite refuses a trigger that names more than
    one (``AFTER DELETE OR INSERT ON t`` is a syntax error), so one event word is read: the first
    unquoted INSERT, UPDATE or DELETE before the ``ON`` that names the table. SQLite reserves those
    three words, so a trigger's name or a column of ``UPDATE OF`` spells one of them only quoted,
    and a quoted name is not an event word. BEFORE, AFTER and INSTEAD are not reserved: a trigger
    or an ``UPDATE OF`` column may be named after them bare, which is why the timing is not where
    the reading starts.
    """
    tokens = _sql_tokens(sql)
    depths = _depths(tokens)
    top = [index for index in range(len(tokens)) if depths[index] == 0]
    on = next((index for index in top if tokens[index] == (_WORD, "on")), None)
    if on is None:
        return None
    return next(
        (tokens[index][1] for index in top if index < on and tokens[index] in _EVENT_WORDS), None
    )


def _trigger_statements(sql: str) -> list[list[tuple[str, str]]]:
    """The statements of a trigger body, each as its tokens, or none when there is no body.

    The trigger's own name may be the keyword BEGIN, so the body starts at the first BEGIN after
    the ON that names the table the trigger fires on, and the statements it holds are separated
    by the semicolons at parenthesis depth zero.
    """
    tokens = _sql_tokens(sql)
    depths = _depths(tokens)
    top = [index for index in range(len(tokens)) if depths[index] == 0]
    on = next((index for index in top if tokens[index] == (_WORD, "on")), None)
    begin = next(
        (
            index
            for index in top
            if tokens[index] == (_WORD, "begin") and (on is None or index > on)
        ),
        None,
    )
    if begin is None:
        return []
    statements: list[list[tuple[str, str]]] = [[]]
    for index in range(begin + 1, len(tokens)):
        if depths[index] == 0 and tokens[index] == (_PUNCT, ";"):
            statements.append([])
        else:
            statements[-1].append(tokens[index])
    return statements


def _trigger_deletions(sql: str) -> list[str]:
    """What each top-level DELETE of a trigger's body deletes, as ``DELETE FROM <table>``, or
    ``DELETE`` when the table it names cannot be read.

    The authorizer refuses every DELETE on a compatibility connection, so a trigger that runs one
    is refused at admission rather than at the write that fires it.
    """
    found: list[str] = []
    for statement in _trigger_statements(sql):
        depths = _depths(statement)
        for index, token in enumerate(statement):
            if depths[index] or token != (_WORD, "delete"):
                continue
            rest = statement[index + 1 :]
            name = None
            if rest[:1] == [(_WORD, "from")] and len(rest) > 1:
                name = _name(rest[1])
            # Text for the refusal message; it is never executed.
            found.append(f"DELETE FROM {name}" if name else "DELETE")  # noqa: S608
    return found


def _trigger_owner_problems(sql: str, column: str, owned: frozenset[str]) -> list[str]:
    """How a trigger's statements write an owned table other than with the owner of the row that
    fired it: an INSERT must copy ``NEW.<column>`` (``OLD.<column>`` in a DELETE trigger) into
    every row, and an UPDATE must restrict its rows by a term comparing the column with one of
    the rows its event makes readable.
    """
    statements = _trigger_statements(sql)
    if not statements:
        return []
    # A DELETE trigger reads OLD and an INSERT trigger reads NEW; a trigger names exactly one
    # event, so the other of the two is not a column of it at all.
    rows = {"insert": ("new",), "delete": ("old",)}.get(_trigger_event(sql), ("new", "old"))
    problems: list[str] = []
    for statement in statements:
        target = _write_target(statement)
        if target is None:
            continue
        verb, table, after = target
        if table is None:
            problems.append(f"writes a table that cannot be read ({verb.upper()})")
            continue
        if table not in owned:
            continue
        what = f"{'inserts into' if verb == 'insert' else 'updates'} {table}"
        # `UPDATE t AS new` names the row being written NEW inside the statement, so a term
        # comparing the owner with NEW.<column> compares the row with itself and holds for
        # every owner's row.
        alias = (
            _name(statement[after + 1])
            if verb == "update"
            and statement[after : after + 1] == [(_WORD, "as")]
            and after + 1 < len(statement)
            else None
        )
        if alias in ("new", "old"):
            problems.append(
                f"{what} under the alias {alias.upper()}, which hides the row that fired it"
            )
            continue
        reader = _insert_owner_values if verb == "insert" else _update_owner_values
        values = reader(statement, after, column)
        if isinstance(values, str):
            problems.append(f"{what} and {values}")
            continue
        written = [_copies_owner(statement[first:end], column, rows) for first, end in values]
        if not (all(written) if verb == "insert" else any(written)):
            owner = " or ".join(f"{name.upper()}.{column}" for name in rows)
            problems.append(
                f"{what} with an owner other than {owner}, the owner of the row that fired it"
            )
    return problems


def _pragma_reads(tokens: list[tuple[str, str]]) -> bool:
    rest = tokens[1:]
    if len(rest) >= 2 and rest[1] == (_PUNCT, "."):
        rest = rest[2:]  # a schema prefix: PRAGMA main.table_info(t)
    if not rest or rest[0][0] != _WORD:
        return False
    name, args = rest[0][1], rest[1:]
    if not args:
        return name in _READ_PRAGMAS
    return name in _TABLE_PRAGMAS and args[0] == (_PUNCT, "(")


def _statement_kind(tokens: list[tuple[str, str]]) -> str:
    """``read``, ``begin``, ``commit``, ``rollback``, ``savepoint`` or ``write``.

    Anything not recognised as one of the first five is a write, so an unfamiliar statement is
    refused rather than admitted as a read.
    """
    words = [text for kind, text in tokens if kind == _WORD]
    if not words:
        return "write"
    first = words[0]
    if first in ("select", "values"):
        return "read"
    if first == "with":
        return "write" if _DML.intersection(_top_level_words(tokens)) else "read"
    if first == "pragma":
        return "read" if _pragma_reads(tokens) else "write"
    if first == "begin":
        return "begin"
    if first in ("commit", "end"):
        return "commit"
    if first == "rollback":
        return "savepoint" if "to" in words[1:3] else "rollback"
    if first in ("savepoint", "release"):
        return "savepoint"
    return "write"


def _write_refusal(tokens: list[tuple[str, str]]) -> str | None:
    """Why a write inside an operation is refused, or None when its shape is admitted."""
    top = _top_level_words(tokens)
    if top and top[0] == "with":
        verb = next((word for word in top if word in _DML), None)
    else:
        verb = top[0] if top else None
    if verb not in ("insert", "update"):
        return f"{(verb or 'this statement').upper()} is not an admitted write"
    for index, token in enumerate(tokens):
        if (
            token == (_WORD, "replace")
            and index
            and tokens[index - 1] == (_WORD, "or")
            and tokens[index + 1 : index + 2] != [(_PUNCT, "(")]
        ):
            return "the OR REPLACE conflict clause deletes the conflicting row"
    for index in range(len(tokens) - 2):
        if tokens[index] in ((_WORD, "insert"), (_WORD, "update")) and tokens[index + 1] == (
            _WORD,
            "or",
        ):
            action = tokens[index + 2][1]
            if action != "abort":
                effect = _CONFLICT_EFFECTS.get(action, "is not admitted")
                return f"the OR {action.upper()} conflict clause {effect}"
    if "returning" in top:
        return "RETURNING is not admitted"
    if verb == "insert":
        target = _write_target(tokens)
        names = None if target is None else _insert_columns(tokens, target[2])
        twice = None if names is None else _duplicate_column(names)
        if twice is not None:
            return f"the INSERT names the column {twice} twice"
    return None


# Structure


class _UnreadableTableError(RuntimeError):
    """A table's structure could not be read from the store, named for the caller's message.

    A virtual table whose module this SQLite lacks is the case: the ``table_xinfo``,
    ``index_list`` and ``foreign_key_list`` reads raise for it, and the store cannot be compared
    to the profile. ``table`` is the table's name and ``error`` the driver's error.
    """

    def __init__(self, table: str, error: Exception) -> None:
        self.table = table
        self.error = error
        super().__init__(f"the table {table} could not be read: {error}")


@dataclass(frozen=True)
class ColumnShape:
    """One column as ``PRAGMA table_xinfo`` reports it.

    ``type`` is uppercased with single spaces and ``default`` is the DEFAULT expression as it is
    written, with the tokens joined by :func:`_joined` and comments aside. ``pk`` is the column's
    position in the primary key (0 when it is not part of it); ``hidden`` is 2 for a virtual and
    3 for a stored generated column.
    """

    name: str
    type: str = ""
    notnull: bool = False
    default: str | None = None
    pk: int = 0
    hidden: int = 0


@dataclass(frozen=True)
class TableShape:
    """A table's columns in order, its unique keys, CHECK constraints, table options and conflicts.

    A unique key lists its terms in index order. A key a CREATE INDEX made spells each term as
    its SQL does, an expression or a ``COLLATE`` included; a key the table's own constraints
    made lists column names, with ``"<expression>"`` for an indexed expression. A partial
    unique index appends ``"where <predicate>"``. Options are drawn from
    ``"strict"``, ``"without rowid"`` and ``"virtual"``. ``conflicts`` holds every conflict
    resolution other than ABORT, SQLite's default, that the table's constraints declare
    (``"replace"``, ``"rollback"``, ``"ignore"``, ``"fail"``); admission refuses a table that has
    one. ``definitions`` holds each column and table-constraint definition of the table's SQL in
    one spelling (whitespace, comments, identifier quotes and the case of words aside, since
    SQLite resolves names that way), so what PRAGMA output leaves out is compared too: a
    collation, a generated column's expression, a ``DEFERRABLE`` foreign key, ``AUTOINCREMENT``.
    Two definitions SQLite reads alike but spells differently differ: ``REFERENCES runs`` and
    ``REFERENCES runs(id)``, a constraint with and without a name, a default spelled out
    (``NO ACTION``, ``COLLATE BINARY``, ``ASC``), a column's ``UNIQUE`` and the table's, ``<>``
    and ``!=``. Table-level constraints are compared as a set, so their order is not part of a
    table's definition. A column's ``default`` is compared as written, since SQLite stores the
    word as given: ``DEFAULT open`` and ``DEFAULT Open`` differ.
    A virtual table's ``definitions`` are instead its module's name, read between its quotes and
    folded, and the text of each argument in order, the spacing around an argument aside: SQLite
    looks a module up without regard to ASCII case and hands it each argument's text, which the
    module reads as it chooses, so a reordered or repeated argument is another definition, and so
    is ``content= 'events'`` against ``content='events'``, while ``USING fts4("x"(1))`` differs
    from ``USING fts4(x(1))``, ``USING "fts4"(x)`` is ``USING fts4(x)``, and ``fts3(x)`` differs
    from ``fts4(x)``, whose columns read alike.
    Values compare as :func:`read_closure` spells them, so a profile's shapes come from reading
    a store built by the managing application.
    """

    name: str
    columns: tuple[ColumnShape, ...]
    unique: frozenset[tuple[str, ...]] = frozenset()
    checks: frozenset[str] = frozenset()
    options: frozenset[str] = frozenset()
    conflicts: frozenset[str] = frozenset()
    definitions: frozenset[str] = frozenset()
    # Each column with the collation its own definition declares, read from the table's SQL while
    # a quoted name is still one token, so a column named "primary" or "collate", or one whose
    # quoted name starts with another column's, is read as the column it is. Derived from the
    # same SQL as ``definitions``, so it takes no part in comparing a store with a profile.
    collations: frozenset[tuple[str, str]] = field(default=frozenset(), compare=False)


@dataclass(frozen=True)
class ForeignKeyShape:
    """``table(columns)`` references ``parent(parent_columns)``.

    ``parent_columns`` is empty when the key names the parent's primary key implicitly.
    """

    table: str
    columns: tuple[str, ...]
    parent: str
    parent_columns: tuple[str, ...]
    on_update: str = "NO ACTION"
    on_delete: str = "NO ACTION"


@dataclass(frozen=True)
class SchemaObject:
    """A trigger or view of the closure, with its SQL as it is written.

    An object joins the closure when its SQL may name a closure table or another closure object.
    ``position`` is a trigger's place, in creation order, among the closure triggers on its own
    table that fire on the same event and timing, since SQLite fires those in an order fixed by
    when they were created; it is 0 for a view and for a trigger that is the only one of its
    event and timing on its table.
    """

    type: str
    name: str
    sql: str
    position: int = 0


@dataclass(frozen=True)
class Closure:
    """The structure of a set of tables and of everything in the store that touches them.

    A virtual table whose module arguments name one of the tables is a table of the closure, so
    its shape is compared like any other. ``foreign_keys`` holds every key into or out of the
    tables; ``objects`` every trigger and view whose SQL may name one of the tables or of the
    other objects, and, in turn, whose own SQL may bring in a view or a table of its own: the
    closure follows what its objects read, until nothing new joins.
    """

    tables: tuple[TableShape, ...]
    foreign_keys: frozenset[ForeignKeyShape] = frozenset()
    objects: frozenset[SchemaObject] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "tables", tuple(sorted(self.tables, key=lambda t: t.name)))
        object.__setattr__(self, "foreign_keys", frozenset(self.foreign_keys))
        object.__setattr__(self, "objects", frozenset(self.objects))

    @property
    def table_names(self) -> frozenset[str]:
        return frozenset(table.name for table in self.tables)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _checks(tokens: list[tuple[str, str]]) -> frozenset[str]:
    found: set[str] = set()
    for index, token in enumerate(tokens):
        if token != (_WORD, "check") or tokens[index + 1 : index + 2] != [(_PUNCT, "(")]:
            continue
        depth = 0
        for end in range(index + 1, len(tokens)):
            if tokens[end] == (_PUNCT, "("):
                depth += 1
            elif tokens[end] == (_PUNCT, ")"):
                depth -= 1
                if depth == 0:
                    found.add(_joined(tokens[index + 2 : end]))
                    break
    return frozenset(found)


def _conflicts(tokens: list[tuple[str, str]]) -> frozenset[str]:
    found: set[str] = set()
    for index in range(len(tokens) - 2):
        if tokens[index : index + 2] == [(_WORD, "on"), (_WORD, "conflict")]:
            kind, action = tokens[index + 2]
            if kind == _WORD and action != "abort":
                found.add(action)
    return frozenset(found)


def _options(tokens: list[tuple[str, str]]) -> frozenset[str]:
    words = [text for kind, text in tokens if kind == _WORD]
    options: set[str] = set()
    if words[:3] == ["create", "virtual", "table"]:
        options.add("virtual")
    depth, tail = 0, len(tokens)
    for index, token in enumerate(tokens):
        if token == (_PUNCT, "("):
            depth += 1
        elif token == (_PUNCT, ")"):
            depth -= 1
            if depth == 0:
                tail = index + 1
    tail_words = [text for kind, text in tokens[tail:] if kind == _WORD]
    if "strict" in tail_words:
        options.add("strict")
    if "without" in tail_words and "rowid" in tail_words:
        options.add("without rowid")
    return frozenset(options)


def _groups(tokens: list[tuple[str, str]], start: int) -> list[list[tuple[str, str]]]:
    """The comma-separated items of the parenthesized list opening at ``tokens[start]``."""
    groups: list[list[tuple[str, str]]] = [[]]
    depth = 0
    for token in tokens[start + 1 :]:
        if token == (_PUNCT, ")") and depth == 0:
            break
        if token == (_PUNCT, ",") and depth == 0:
            groups.append([])
            continue
        if token == (_PUNCT, "("):
            depth += 1
        elif token == (_PUNCT, ")"):
            depth -= 1
        groups[-1].append(token)
    return [group for group in groups if group]


def _definitions(tokens: list[tuple[str, str]]) -> frozenset[str]:
    """Each column and table-constraint definition of a CREATE TABLE, in one spelling.

    An explicit ``ON CONFLICT ABORT`` is dropped: it spells SQLite's default.
    """
    start = next((i for i, token in enumerate(tokens) if token == (_PUNCT, "(")), None)
    if start is None:
        return frozenset()
    found: set[str] = set()
    for group in _groups(tokens, start):
        kept: list[tuple[str, str]] = []
        index = 0
        while index < len(group):
            if group[index : index + 3] == [(_WORD, "on"), (_WORD, "conflict"), (_WORD, "abort")]:
                index += 3
                continue
            kept.append(group[index])
            index += 1
        found.add(_joined(kept))
    return frozenset(found)


def _argument_texts(sql: str) -> list[str]:
    """Each module argument of a ``CREATE VIRTUAL TABLE`` as SQLite hands it to the module.

    SQLite gives the module the text of each argument from its first token to its last, exactly as
    written, so the spacing and comments around an argument are not part of it while those inside
    it are. The argument list is the statement's first parenthesis outside a quoted token or a
    comment; an argument with no token is dropped, as SQLite drops it.
    """
    texts: list[str] = []
    first = last = None
    depth = -1
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in " \t\n\v\f\r":
            i += 1
            continue
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end + 1
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if ch in "'\"`[":
            close = "]" if ch == "[" else ch
            j = i + 1
            while j < n and not (sql[j] == close and (close == "]" or sql[j + 1 : j + 2] != close)):
                j += 1 if close == "]" or sql[j] != close else 2
            end = min(j + 1, n)
        else:
            end = i + 1
            if depth == -1:
                if ch == "(":
                    depth = 0
                i = end
                continue
            if ch == "(":
                depth += 1
            elif ch == ")" and depth == 0:
                break
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                if first is not None:
                    texts.append(sql[first:last])
                first = last = None
                i = end
                continue
        if depth >= 0:
            first = i if first is None else first
            last = end
        i = end
    if first is not None:
        texts.append(sql[first:last])
    return texts


def _module_arguments(sql: str) -> frozenset[str]:
    """A virtual table's module and the text of each of its arguments, for ``definitions``.

    The module's name is read between its quotes and folded, since SQLite looks a module up without
    regard to ASCII case, so ``USING "fts4"(x)`` equals ``USING fts4(x)``. Each argument is compared
    as the text SQLite hands the module (:func:`_argument_texts`), which the module reads as it
    chooses: fts4 reads ``content='events'`` as the table ``events`` and ``content= 'events'`` as a
    table whose name starts with a space and a quote.
    """
    tokens = _sql_tokens(sql, verbatim=True)
    # The module's argument list is the statement's first parenthesis: a table name holding one is
    # quoted, and a quoted name is a single token.
    start = next((i for i, token in enumerate(tokens) if token == (_PUNCT, "(")), len(tokens))
    if start < 2:
        return frozenset()
    kind, text = tokens[start - 1]
    found = "using " + _unquoted(kind, text)
    # The arguments are one ordered tuple: a reordered or repeated argument is another definition,
    # and an empty argument list is no argument list.
    arguments = _argument_texts(sql)
    if arguments:
        found += " (" + ", ".join(arguments) + ")"
    return frozenset({found})


# The full-text modules whose only argument naming a table is ``content=``; every other argument
# of theirs is a column, an option word or an option's value.
_FTS_MODULES = frozenset({"fts3", "fts4", "fts5"})


def _virtual_table_names(tokens: list[tuple[str, str]]) -> set[str]:
    """The names a virtual table's module arguments may carry, for the closure scan.

    A virtual table touches the closure when its ``CREATE VIRTUAL TABLE`` statement names a table
    of the closure among its module arguments, as ``content='events'`` names ``events``. Only the
    argument list is read, so the statement's keywords, the table's own name and the module's name
    name nothing in the store. For fts3, fts4 and fts5 only the value of a ``content=`` argument
    names a table, so a column or ``content_rowid='seq'`` names none. Any other module reads its
    arguments as it chooses, so every token of every argument counts, a bare word, a single-quoted
    string and a double-quoted name alike, since a quoted token may be read either way.
    """
    start = next((i for i, token in enumerate(tokens) if token == (_PUNCT, "(")), None)
    if start is None or start < 1:
        return set()
    if _unquoted(*tokens[start - 1]) not in _FTS_MODULES:
        return _names(tokens[start:])
    found: set[str] = set()
    for group in _groups(tokens, start):
        if len(group) > 2 and _unquoted(*group[0]) == "content" and group[1] == (_PUNCT, "="):
            found |= _names(group[2:])
    return found


def _column_collation(definition: str) -> str:
    """The collation a column's definition declares, its name folded as SQLite folds a name.

    ``binary`` is the answer for a definition that declares none. Only a clause at parenthesis
    depth zero of the definition counts: a ``COLLATE`` inside a CHECK, a DEFAULT or a GENERATED
    expression collates one comparison of that expression, not the column. When the definition
    carries several such clauses SQLite compares the column under the last of them, so the last
    one is read. Folding is by ASCII letters only, so a name spelled with another character in
    the place of one, ``bınary``, is not the folded ``binary``.
    """
    return _declared_collation(_sql_tokens(definition))


def _declared_collation(tokens: list[tuple[str, str]]) -> str:
    """The collation the last depth-zero ``COLLATE`` of *tokens* names, or ``binary``."""
    depths = _depths(tokens)
    collation = "binary"
    for index, token in enumerate(tokens):
        if depths[index] or token != (_WORD, "collate"):
            continue
        name = _name(tokens[index + 1]) if index + 1 < len(tokens) else None
        if name is not None:
            collation = name
    return collation


def _column_collations(tokens: list[tuple[str, str]]) -> frozenset[tuple[str, str]]:
    """Each column of a CREATE TABLE with the collation its own definition declares.

    A definition is a column's unless it is a table constraint, and a table constraint starts
    with a bare word (``CHECK``, ``UNIQUE``, ``PRIMARY KEY``, ``FOREIGN KEY``, ``CONSTRAINT``), so
    a definition whose first token is a quoted name or a string is a column's whatever that name
    spells. The name is the first token alone, and the collation is read from what follows it,
    so a column named ``collate`` is not read as its own clause.
    """
    start = next((i for i, token in enumerate(tokens) if token == (_PUNCT, "(")), None)
    if start is None:
        return frozenset()
    found: set[tuple[str, str]] = set()
    for group in _groups(tokens, start):
        if group[0][0] == _WORD and _is_table_constraint([text for _, text in group]):
            continue
        name = _name(group[0])
        if name is not None:
            found.add((name, _declared_collation(group[1:])))
    return frozenset(found)


def _is_table_constraint(words: list[str]) -> bool:
    """Whether a definition that starts with a bare word, as its words, is a table constraint.

    A table constraint is ``CHECK (``, ``UNIQUE (``, ``PRIMARY KEY (``, ``FOREIGN KEY (``, or one
    of those after ``CONSTRAINT <name>``; a column's type is a word, never a parenthesis.
    """
    if words[:2] in (["check", "("], ["unique", "("]):
        return True
    if words[:3] in (["primary", "key", "("], ["foreign", "key", "("]):
        return True
    return words[:1] == ["constraint"] and _is_table_constraint(words[2:])


def _ordered_collation(table: TableShape, column: str) -> str | None:
    """The collation *column* compares under in *table*, read from its own definition, or None
    when the table's SQL does not hold exactly one definition for it: the owner check then
    refuses rather than assume binary.
    """
    found = [collation for name, collation in table.collations if name == column]
    return found[0] if len(found) == 1 else None


def _indexed_terms(index_sql: str, columns: frozenset[str]) -> list[str]:
    """The terms of a CREATE INDEX, each in one spelling: a column, an expression, a COLLATE."""
    tokens = _sql_tokens(index_sql, spelled=True, columns=columns)
    on = next((i for i, token in enumerate(tokens) if token == (_WORD, "on")), None)
    if on is None:
        return []
    start = next((i for i in range(on, len(tokens)) if tokens[i] == (_PUNCT, "(")), None)
    if start is None:
        return []
    return [_joined(group) for group in _groups(tokens, start)]


def _predicate(index_sql: str, columns: frozenset[str]) -> str:
    tokens = _sql_tokens(index_sql, spelled=True, columns=columns)
    depth = 0
    for index, (kind, text) in enumerate(tokens):
        if kind == _PUNCT and text == "(":
            depth += 1
        elif kind == _PUNCT and text == ")":
            depth -= 1
        elif kind == _WORD and text == "where" and depth == 0:
            return _joined(tokens[index + 1 :])
    return ""


_Fetch = Callable[[str], list]


def _table_shape(fetch: _Fetch, name: str, sql: str, entries: Mapping) -> TableShape:
    quoted = _quote(name)
    try:
        columns = tuple(
            ColumnShape(
                name=_fold(str(row[1])),
                type=" ".join(str(row[2] or "").upper().split()),
                notnull=bool(row[3]),
                default=None if row[4] is None else _spelled_sql(str(row[4])),
                pk=int(row[5]),
                hidden=int(row[6]),
            )
            for row in fetch(f"PRAGMA table_xinfo({quoted})")
        )
        index_rows = fetch(f"PRAGMA index_list({quoted})")
    except (sqlite3.Error, DBAPIError) as exc:
        # A virtual table whose module this SQLite lacks raises here, as the module is only looked
        # up when the table is read: the store cannot be compared to the profile, so the read is
        # refused by the table's name rather than by the driver's message alone.
        raise _UnreadableTableError(name, exc) from exc
    names = frozenset(column.name for column in columns)
    unique: set[tuple[str, ...]] = set()
    for row in index_rows:
        index_name, is_unique, partial = str(row[1]), row[2], row[4]
        if not is_unique:
            continue
        entry = entries.get(("index", _fold(index_name)))
        index_sql = (entry[3] if entry else None) or ""
        # An index a CREATE INDEX made is read from its SQL, which keeps what PRAGMA output
        # does not: an indexed expression's text and a term's collation.
        key = _indexed_terms(index_sql, names) or [
            "<expression>" if column is None else _fold(str(column))
            for _, _, column in sorted(fetch(f"PRAGMA index_info({_quote(index_name)})"))
        ]
        if partial:
            key.append("where " + _predicate(index_sql, names))
        unique.add(tuple(key))
    tokens = _sql_tokens(sql, spelled=True, columns=names)
    options = _options(tokens)
    return TableShape(
        _fold(name),
        columns,
        frozenset(unique),
        _checks(tokens),
        options,
        _conflicts(tokens),
        _module_arguments(sql) if "virtual" in options else _definitions(tokens),
        _column_collations(tokens),
    )


def _foreign_keys(fetch: _Fetch, table: str) -> list[ForeignKeyShape]:
    groups: dict[Any, list] = {}
    try:
        rows = fetch(f"PRAGMA foreign_key_list({_quote(table)})")
    except (sqlite3.Error, DBAPIError) as exc:
        # As in _table_shape: a table whose module this SQLite lacks raises when it is read, so the
        # read is refused by the table's name rather than by the driver's message alone.
        raise _UnreadableTableError(table, exc) from exc
    for row in rows:
        groups.setdefault(row[0], []).append(row)
    keys = []
    for key_rows in groups.values():
        key_rows.sort(key=lambda row: row[1])
        parent_columns = tuple(_fold(str(row[4])) for row in key_rows if row[4] is not None)
        keys.append(
            ForeignKeyShape(
                table=_fold(table),
                columns=tuple(_fold(str(row[3])) for row in key_rows),
                parent=_fold(str(key_rows[0][2])),
                parent_columns=parent_columns if len(parent_columns) == len(key_rows) else (),
                on_update=str(key_rows[0][5]).upper(),
                on_delete=str(key_rows[0][6]).upper(),
            )
        )
    return keys


def _trigger_slot(sql: str) -> tuple[str, str]:
    """The timing and the event of a CREATE TRIGGER, BEFORE when it names no timing."""
    tokens = _sql_tokens(sql)
    index = next((i for i, token in enumerate(tokens) if token == (_WORD, "trigger")), len(tokens))
    index += 1
    if tokens[index : index + 3] == [(_WORD, "if"), (_WORD, "not"), (_WORD, "exists")]:
        index += 3
    # The trigger's own name, schema-qualified or not, can be spelled like any word below.
    index += 3 if tokens[index + 1 : index + 2] == [(_PUNCT, ".")] else 1
    timing = "before"
    for kind, text in tokens[index:]:
        if kind != _WORD:
            continue
        if text in ("before", "after"):
            timing = text
        elif text == "instead":
            timing = "instead of"
        elif text in ("delete", "insert", "update"):
            return timing, text
    return timing, ""


def _read_closure(fetch: _Fetch, tables: Iterable[str]) -> Closure:
    wanted = frozenset(_fold(name) for name in tables)
    entries = {
        (str(row[0]), _fold(str(row[1]))): row
        for row in fetch("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY rowid")
    }
    # A trigger's or a view's body may name closure objects, a virtual table's module arguments may
    # name a closure table: each is read as the tokens it carries a name in.
    reads = {
        key: _names(_sql_tokens(row[3]))
        for key, row in entries.items()
        if key[0] in ("trigger", "view") and row[3]
    }
    virtual = {
        key[1]: _virtual_table_names(tokens)
        for key, row in entries.items()
        if key[0] == "table"
        and row[3]
        and _top_level_words(tokens := _sql_tokens(row[3]))[:3] == ["create", "virtual", "table"]
    }
    # A module keeps a virtual table's data in shadow tables named after it (``docs_data`` for
    # ``docs``), which it writes whenever the virtual table is written, so a trigger on one runs on
    # that write. Every ordinary table named ``<virtual table>_...`` is taken as one: the module
    # alone knows its suffixes, and taking more tables only refuses more stores.
    shadows = {
        name: {
            other
            for kind, other in entries
            if kind == "table" and other.startswith(name + "_") and other not in virtual
        }
        for name in virtual
    }
    # A table is named with its ASCII letters in either case, as SQLite matches it, and a quoted
    # token may be read as a name or as a string literal depending on its scope and on the
    # connection's setting, so every spelling a body carries counts: an object that may name a
    # closure table or another closure object belongs to the closure, and what that object reads
    # joins in turn, until nothing new joins. Only a table or a view is read through a body: an
    # index or a trigger is never read by name, so a word that names one pulls nothing in. The
    # body itself is kept as written.
    known = {
        name
        for kind, name in entries
        if kind in ("table", "view") and not name.startswith("sqlite_")
    }
    inside = set(wanted)
    joined: set[tuple[str, str]] = set()
    joined_virtual: set[str] = set()
    while True:
        found = {
            key for key, names in reads.items() if key not in joined and inside.intersection(names)
        }
        # An object whose own name the closure already holds joins too: what a closure object
        # reads is part of the closure, and that includes an object it reads by name.
        found |= {key for key in reads if key not in joined and key[1] in inside}
        # A virtual table joins when its arguments name a closure table, and when the closure
        # already holds it however it got there (named in *tables*, read by a view or a trigger).
        # Its name being a table of the closure, it is compared as one.
        found_virtual = {
            name
            for name, names in virtual.items()
            if name not in joined_virtual and (name in inside or inside & names)
        }
        if not found and not found_virtual:
            break
        joined |= found
        joined_virtual |= found_virtual
        for name in found_virtual:
            # What the virtual table's arguments name joins with it, as what a view's body names
            # does, and so do its shadow tables: the closure follows every object it reads.
            inside.add(name)
            inside |= (virtual[name] & known) | shadows[name]
        for key in found:
            inside |= reads[key] & known
    shapes = [
        _table_shape(fetch, str(row[1]), row[3] or "", entries)
        for name in sorted(inside)
        if (row := entries.get(("table", name))) is not None
    ]
    table_names = frozenset(shape.name for shape in shapes)
    keys: set[ForeignKeyShape] = set()
    for (kind, name), row in entries.items():
        if kind == "table" and not name.startswith("sqlite_"):
            keys.update(
                key
                for key in _foreign_keys(fetch, str(row[1]))
                if key.table in table_names or key.parent in table_names
            )
    objects: set[SchemaObject] = set()
    positions: dict[tuple[str, str, str], int] = {}
    for (kind, name), row in entries.items():
        if (kind, name) not in joined:
            continue
        position = 0
        if kind == "trigger":
            # SQLite fires the triggers on one table for one event and timing in an order fixed by
            # when they were created, so a trigger's place among those is part of the closure.
            slot = (_fold(str(row[2])), *_trigger_slot(row[3] or ""))
            position = positions.get(slot, 0)
            positions[slot] = position + 1
        objects.add(SchemaObject(kind, name, _spelled_sql(row[3]), position))
    return Closure(tuple(shapes), frozenset(keys), frozenset(objects))


def read_closure(connection: sqlite3.Connection, tables: Iterable[str]) -> Closure:
    """Read the structure of *tables*, and every key, trigger and view touching them, from a store.

    This is how a profile's closure is made: build a scratch store with the managing application's
    own initializer and migrations, then read the tables the profile's operations depend on.
    """
    return _read_closure(lambda sql: connection.execute(sql).fetchall(), tables)


def _closure_differences(expected: Closure, observed: Closure) -> list[str]:
    found: list[str] = []
    actual_tables = {table.name: table for table in observed.tables}
    for table in expected.tables:
        actual = actual_tables.get(table.name)
        if actual is None:
            found.append(f"table {table.name} is missing")
            continue
        want = {column.name: column for column in table.columns}
        have = {column.name: column for column in actual.columns}
        for name in sorted(want.keys() - have.keys()):
            found.append(f"column {table.name}.{name} is missing")
        for name in sorted(have.keys() - want.keys()):
            found.append(f"column {table.name}.{name} is not in the profile")
        for name in sorted(want.keys() & have.keys()):
            if want[name] != have[name]:
                found.append(
                    f"column {table.name}.{name} is {have[name]}, the profile has {want[name]}"
                )
        if want.keys() == have.keys() and table.columns != actual.columns and not found:
            found.append(f"the columns of {table.name} are in a different order")
        for label in ("unique", "checks", "options", "conflicts"):
            if getattr(table, label) != getattr(actual, label):
                found.append(
                    f"{label} of {table.name}: store has {sorted(getattr(actual, label))}, "
                    f"the profile has {sorted(getattr(table, label))}"
                )
        if table.definitions != actual.definitions:
            found.append(
                f"definitions of {table.name}: store has "
                f"{sorted(actual.definitions - table.definitions)}, the profile has "
                f"{sorted(table.definitions - actual.definitions)}"
            )
    # A table only the store's closure holds is one a closure object reads or writes there,
    # where the profile's store has no such table.
    for name in sorted(actual_tables.keys() - {table.name for table in expected.tables}):
        found.append(f"table {name} is not in the profile")
    for key in sorted(observed.foreign_keys - expected.foreign_keys, key=repr):
        found.append(f"foreign key not in the profile: {key}")
    for key in sorted(expected.foreign_keys - observed.foreign_keys, key=repr):
        found.append(f"foreign key missing: {key}")
    expected_objects = {(item.type, item.name): item for item in expected.objects}
    observed_objects = {(item.type, item.name): item for item in observed.objects}
    for item in sorted(observed.objects - expected.objects, key=repr):
        twin = expected_objects.get((item.type, item.name))
        if item.type == "trigger" and twin is not None and twin.sql == item.sql:
            found.append(
                f"trigger {item.name} is at position {item.position} in creation order among "
                "the closure triggers on its table for its event and timing, the profile's at "
                f"{twin.position}, and SQLite fires those in an order fixed by when they were "
                "created"
            )
        else:
            found.append(f"{item.type} {item.name} is not in the profile")
    for item in sorted(expected.objects - observed.objects, key=repr):
        twin = observed_objects.get((item.type, item.name))
        if item.type == "trigger" and twin is not None and twin.sql == item.sql:
            continue  # the same trigger in another position, reported above
        found.append(f"{item.type} {item.name} is missing or changed")
    return found


# Profiles


@dataclass(frozen=True, eq=False)
class TableWrites:
    """What compatibility mode writes to one table.

    ``insert`` names the columns an INSERT supplies (empty: the table takes no inserts) and
    ``update`` the columns an UPDATE may set. ``unwritten`` says, for every other column of a table
    that takes inserts, why leaving it to its default or to NULL is correct.
    """

    insert: frozenset[str] = frozenset()
    update: frozenset[str] = frozenset()
    unwritten: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "insert", frozenset(_fold(c) for c in self.insert))
        object.__setattr__(self, "update", frozenset(_fold(c) for c in self.update))
        object.__setattr__(
            self,
            "unwritten",
            MappingProxyType({_fold(c): why for c, why in dict(self.unwritten).items()}),
        )


@dataclass(frozen=True)
class OwnerRule:
    """Where a store records who owns a row (ADR-0128 D5).

    Every table in the profile's closure that has a column named ``column`` is owned. Compatibility
    mode writes the configured owner into that column on every insert, never leaving it to the
    column's default, and restricts every update of an owned table to the owner's rows. The
    statement check holds every statement to that: an INSERT into an owned table must name the
    column and bind it, in every row, to a ``?`` parameter whose value is the owner, and must not
    be an upsert that updates; an UPDATE of one must have a WHERE clause that is a conjunction
    with a term ``column = ?`` bound to the owner. A table it cannot read refuses. Admission
    refuses a store whose triggers write an owned table without copying the owner of the row that
    fired them (``NEW.column``, or ``OLD.column`` in a DELETE trigger). The owner must exist as a
    ``key`` value of the ``principals`` table, read at admission and again in every write
    transaction.
    """

    column: str
    principals: str
    key: str = "id"

    def __post_init__(self) -> None:
        for name in ("column", "principals", "key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"OwnerRule.{name} names a column or table: {value!r}")
            object.__setattr__(self, name, _fold(value))


def _rowid_alias(column: ColumnShape, table: TableShape) -> bool:
    return (
        column.pk == 1
        and column.type == "INTEGER"
        and sum(1 for c in table.columns if c.pk) == 1
        and "without rowid" not in table.options
    )


@dataclass(frozen=True, eq=False)
class CompatibilityProfile:
    """A named, versioned description of what compatibility mode may do to a store.

    A store is admitted only when the tables in ``closure`` match it exactly. ``writes`` lists, per
    table, the columns inserted and updated; ``operations`` the StateDB operations served.
    ``store_versions``, when set, narrows the stores a profile is tried on by their
    ``schema_meta`` version; a version never admits a store on its own. ``invariants`` states the
    reader invariants the operations keep. ``owner``, when set, names where the store records who
    owns a row; every owned table the profile inserts into must insert the owner column, and none may
    update it. Profiles compare by identity.
    """

    name: str
    version: int
    closure: Closure
    writes: Mapping[str, TableWrites]
    operations: frozenset[str]
    store_versions: frozenset[str] | None = None
    invariants: tuple[str, ...] = ()
    owner: OwnerRule | None = None

    def __post_init__(self) -> None:
        if not self.name or "@" in self.name:
            raise ValueError(f"a profile name is a non-empty name without '@': {self.name!r}")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError(f"a profile version is a positive integer: {self.version!r}")
        operations = frozenset(self.operations)
        if not operations:
            raise ValueError(f"profile {self.name} names no operation")
        writes = {_fold(name): spec for name, spec in dict(self.writes).items()}
        tables = {table.name: table for table in self.closure.tables}
        for name, spec in writes.items():
            self._check_writes(name, spec, tables.get(name))
        if self.owner is not None:
            self._check_owner(self.owner, writes, tables)
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "writes", MappingProxyType(writes))
        if self.store_versions is not None:
            object.__setattr__(self, "store_versions", frozenset(map(str, self.store_versions)))
        object.__setattr__(self, "invariants", tuple(self.invariants))

    def _check_writes(self, name: str, spec: TableWrites, table: TableShape | None) -> None:
        where = f"profile {self.name}, table {name}"
        if table is None:
            raise ValueError(f"{where}: the table is not in the closure")
        columns = {column.name: column for column in table.columns}
        unknown = (spec.insert | spec.update | spec.unwritten.keys()) - columns.keys()
        if unknown:
            raise ValueError(f"{where}: no such columns {sorted(unknown)}")
        generated = {c.name for c in table.columns if c.hidden in (2, 3)}
        if (spec.insert | spec.update) & generated:
            raise ValueError(f"{where}: generated columns cannot be written")
        for column, why in spec.unwritten.items():
            if not isinstance(why, str) or not why.strip():
                raise ValueError(f"{where}: unwritten column {column} needs its reason")
        if not spec.insert:
            return
        both = spec.insert & spec.unwritten.keys()
        if both:
            raise ValueError(f"{where}: columns both inserted and unwritten {sorted(both)}")
        left = columns.keys() - spec.insert - spec.unwritten.keys()
        if left:
            raise ValueError(f"{where}: inserts must account for every column; {sorted(left)}")
        for column in spec.unwritten:
            shape = columns[column]
            if (
                shape.notnull
                and shape.default is None
                and shape.hidden == 0
                and not _rowid_alias(shape, table)
            ):
                raise ValueError(
                    f"{where}: {column} is NOT NULL with no default, so an insert leaving it "
                    "out fails"
                )

    def _check_owner(
        self, owner: OwnerRule, writes: Mapping[str, TableWrites], tables: Mapping[str, TableShape]
    ) -> None:
        if not isinstance(owner, OwnerRule):
            raise TypeError(f"profile {self.name}: owner takes an OwnerRule, got {type(owner)!r}")
        principals = tables.get(owner.principals)
        if principals is None:
            raise ValueError(
                f"profile {self.name}: the principals table {owner.principals} is not in the closure"
            )
        if owner.key not in {column.name for column in principals.columns}:
            raise ValueError(
                f"profile {self.name}: {owner.principals} has no column {owner.key} to find an owner by"
            )
        for name, spec in writes.items():
            if owner.column not in {column.name for column in tables[name].columns}:
                continue
            where = f"profile {self.name}, table {name}"
            if owner.column in spec.update:
                raise ValueError(f"{where}: the owner column {owner.column} is never updated")
            if spec.insert and owner.column not in spec.insert:
                raise ValueError(
                    f"{where}: inserts write the owner column {owner.column}; a default is never "
                    "the owner"
                )

    @property
    def owned_tables(self) -> frozenset[str]:
        """The closure's tables that carry the owner column; empty without an owner rule."""
        if self.owner is None:
            return frozenset()
        return frozenset(
            table.name
            for table in self.closure.tables
            if any(column.name == self.owner.column for column in table.columns)
        )

    @property
    def label(self) -> str:
        return f"{self.name}@{self.version}"


# Enforcement

_STATE_KEY = "lionagi.state.compat"
_SQLITE_AUTH = 23  # the SQLITE_AUTH result code; sqlite3 names it only from Python 3.11

_ALLOWED_ACTIONS = frozenset(
    {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, getattr(sqlite3, "SQLITE_RECURSIVE", 33)}
)
_ACTION_NAMES = {
    getattr(sqlite3, f"SQLITE_{name}"): name
    for name in (
        "CREATE_INDEX",
        "CREATE_TABLE",
        "CREATE_TEMP_INDEX",
        "CREATE_TEMP_TABLE",
        "CREATE_TEMP_TRIGGER",
        "CREATE_TEMP_VIEW",
        "CREATE_TRIGGER",
        "CREATE_VIEW",
        "DELETE",
        "DROP_INDEX",
        "DROP_TABLE",
        "DROP_TEMP_INDEX",
        "DROP_TEMP_TABLE",
        "DROP_TEMP_TRIGGER",
        "DROP_TEMP_VIEW",
        "DROP_TRIGGER",
        "DROP_VIEW",
        "ATTACH",
        "DETACH",
        "ALTER_TABLE",
        "REINDEX",
        "ANALYZE",
        "CREATE_VTABLE",
        "DROP_VTABLE",
        "SAVEPOINT",
    )
    if hasattr(sqlite3, f"SQLITE_{name}")
}


class _ConnectionState:
    """Enforcement state of one connection, shared by its authorizer and the engine listeners.

    ``write_txn`` is set only by the guard's begin listener, once it has reserved the writer and
    checked the schema and the owner. It is cleared when a COMMIT, a ROLLBACK or a BEGIN outside
    a transaction is prepared, and when a statement finds the connection out of the transaction
    it marked.
    ``denied`` poisons the current write transaction: its COMMIT is denied. A BEGIN prepared
    inside an open transaction is denied, so it poisons that transaction.
    """

    __slots__ = ("denied", "last_denial", "seen", "write_txn")

    def __init__(self) -> None:
        self.denied = False
        self.last_denial: str | None = None
        self.seen = False
        self.write_txn = False


@dataclass
class _Scope:
    name: str
    committed: list[str]
    active: bool = True


# The operation running in this task. A StateDB operation sets it for its own duration; a write
# outside it is refused. Code an operation calls inherits it, so an operation never calls code
# it does not own while it runs.
_SCOPE: ContextVar[_Scope | None] = ContextVar("lionagi_state_compat_operation", default=None)


def _current_scope() -> _Scope | None:
    """The running operation's scope; one whose operation has returned is no scope.

    A task an operation starts copies the context, scope included, and can outlive it.
    """
    scope = _SCOPE.get()
    return scope if scope is not None and scope.active else None


def _is_auth_error(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.DatabaseError) and (
        getattr(exc, "sqlite_errorcode", None) == _SQLITE_AUTH or str(exc) == "not authorized"
    )


class _Guard:
    """Admission and enforcement for one compatibility-mode StateDB."""

    def __init__(self, profile: CompatibilityProfile, owner: str | None = None) -> None:
        if not isinstance(profile, CompatibilityProfile):
            raise TypeError(f"compatibility takes a CompatibilityProfile, got {type(profile)!r}")
        if profile.owner is not None and (not isinstance(owner, str) or not owner):
            raise ValueError(f"profile {profile.label} records owners, so an owner is required")
        if profile.owner is None and owner is not None:
            raise ValueError(f"profile {profile.label} records no owner, so none can be given")
        self.profile = profile
        self.owner = owner
        self.owned_tables = profile.owned_tables
        # PRAGMA schema_version as admitted; a write transaction that finds another value
        # re-reads the closure before any statement runs.
        self.schema_version: int | None = None
        self._inserts = frozenset(name for name, spec in profile.writes.items() if spec.insert)
        self._updates = frozenset(
            (name, column) for name, spec in profile.writes.items() for column in spec.update
        )

    def refusal(self, reason: str, operation: str | None = None) -> StateCompatibilityRefused:
        scope = _current_scope()
        if operation is None:
            operation = scope.name if scope is not None else "statement"
        committed = tuple(scope.committed) if scope is not None else ()
        return StateCompatibilityRefused(operation, self.profile.label, reason, committed)

    # operations

    def enter(self, name: str) -> tuple[Token, _Scope]:
        if name not in self.profile.operations:
            raise self.refusal("the profile does not name this operation", operation=name)
        outer = _current_scope()
        scope = _Scope(name, outer.committed if outer is not None else [])
        return _SCOPE.set(scope), scope

    def require_scope(self, operation: str) -> _Scope:
        scope = _current_scope()
        if scope is None:
            raise self.refusal(
                "compatibility mode writes only inside an operation the profile names",
                operation=operation,
            )
        return scope

    # admission

    def _differences(self, fetch: _Fetch) -> list[str]:
        expected = self.profile.closure
        try:
            observed = _read_closure(fetch, expected.table_names)
        except _UnreadableTableError as exc:
            # A table the store holds but this SQLite cannot read - a virtual table whose module
            # is absent - leaves the closure unknown, so the store is refused by the table's name
            # rather than by the driver's error alone.
            raise self.refusal(
                f"the closure could not be read: the table {exc.table} could not be read "
                f"({exc.error})"
            ) from exc
        return _closure_differences(expected, observed)

    def _check_store(self, fetch: _Fetch, when: str, operation: str | None) -> None:
        declared = [
            f"{table} declares ON CONFLICT {action.upper()}, which "
            f"{_CONFLICT_EFFECTS.get(action, 'the guard does not admit')}"
            for table, action in sorted(
                (table.name, action)
                for table in self.profile.closure.tables
                for action in table.conflicts
            )
        ]
        # A trigger runs inside the write that fires it, so a resolution it names acts on the
        # operation's transaction as one the table declares would.
        declared += [
            f"{kind} {name} names {spelling}, which "
            f"{_CONFLICT_EFFECTS.get(action, 'the guard does not admit')}"
            for kind, name, spelling, action in sorted(
                (item.type, item.name, spelling, action)
                for item in self.profile.closure.objects
                for spelling, action in _resolutions(_sql_tokens(item.sql))
            )
        ]
        if declared:
            shown = "; ".join(declared)
            raise self.refusal(
                f"{when}: compatibility mode admits only ABORT, SQLite's default conflict "
                f"resolution: {shown}",
                operation,
            )
        # A trigger runs inside the write that fires it, and every DELETE is refused there by
        # the authorizer, so a trigger that runs one is refused when the store is admitted.
        deletions = [
            f"trigger {item.name} {problem}"
            for item in sorted(self.profile.closure.objects, key=lambda o: o.name)
            if item.type == "trigger"
            for problem in _trigger_deletions(item.sql)
        ]
        if deletions:
            raise self.refusal(
                f"{when}: compatibility mode admits no DELETE, and a trigger's DELETE runs "
                f"inside the write that fires it: " + "; ".join(deletions),
                operation,
            )
        rule = self.profile.owner
        if rule is not None:
            # `owner = ?` compares under the column's collation, so an owner that is not binary
            # takes a variant of the owner's name as the owner, and writes another owner's row.
            collations = {
                table.name: _ordered_collation(table, rule.column)
                for table in self.profile.closure.tables
                if rule.column in {column.name for column in table.columns}
            }
            unread = [name for name, collation in collations.items() if collation is None]
            if unread:
                raise self.refusal(
                    f"{when}: the collation of the owner column could not be read from the "
                    f"table's SQL, so it cannot be shown to be binary: "
                    + ", ".join(f"{name}.{rule.column}" for name in sorted(unread)),
                    operation,
                )
            variants = [
                f"{name}.{rule.column} compares under {collation}"
                for name, collation in sorted(collations.items())
                if collation != "binary"
            ]
            if variants:
                raise self.refusal(
                    f"{when}: the owner column compares under a collation, not binary, so its "
                    f"rows cannot be told from a variant's: {'; '.join(variants)}",
                    operation,
                )
            # A trigger's statements pass no statement check, so what they write is read here.
            unowned = [
                f"trigger {item.name} {problem}"
                for item in sorted(self.profile.closure.objects, key=lambda o: o.name)
                if item.type == "trigger"
                for problem in _trigger_owner_problems(item.sql, rule.column, self.owned_tables)
            ]
            if unowned:
                raise self.refusal(
                    f"{when}: a trigger writes an owned table without the owner: "
                    + "; ".join(unowned),
                    operation,
                )
        versions = self.profile.store_versions
        if versions is not None:
            present = fetch(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
            )
            row = fetch("SELECT value FROM schema_meta WHERE key = 'version'") if present else []
            version = str(row[0][0]) if row else None
            if version not in versions:
                raise self.refusal(
                    f"{when}: store version {version!r} is not one this profile is tried on "
                    f"({', '.join(sorted(versions))})",
                    operation,
                )
        differences = self._differences(fetch)
        if differences:
            shown = "; ".join(differences[:8])
            more = f"; {len(differences) - 8} more" if len(differences) > 8 else ""
            raise self.refusal(
                f"{when}: the store does not match the profile: {shown}{more}", operation
            )

    def admit(self, connection) -> None:
        """Admit the store or raise; *connection* is a sync connection inside a read snapshot."""

        def fetch(sql: str) -> list:
            return connection.exec_driver_sql(sql).fetchall()

        try:
            self._check_store(fetch, "admission", "open")
            self._check_owner(connection, "admission", "open")
            self.schema_version = int(fetch("PRAGMA schema_version")[0][0])
        except StateCompatibilityRefused:
            raise
        except (sqlite3.Error, DBAPIError) as exc:
            raise self.refusal(f"the store could not be read: {exc}", operation="open") from exc

    def _check_owner(self, connection, when: str, operation: str | None) -> None:
        rule = self.profile.owner
        if rule is None:
            return
        # BINARY, whatever the key column's collation is: a key that names an owner only through
        # case folding names one whose rows the statement check, which compares the owner column
        # under that column's own collation, would then treat as another owner's.
        found = connection.exec_driver_sql(
            f"SELECT 1 FROM {_quote(rule.principals)} "  # noqa: S608 -- the names are the rule's
            f"WHERE {_quote(rule.key)} = ? COLLATE BINARY",
            (self.owner,),
        ).fetchall()
        if not found:
            raise self.refusal(
                f"{when}: the owner {self.owner!r} is not in {rule.principals}", operation
            )

    def _recheck(self, connection) -> None:
        def fetch(sql: str) -> list:
            return connection.exec_driver_sql(sql).fetchall()

        current = int(fetch("PRAGMA schema_version")[0][0])
        if current == self.schema_version:
            return
        self._check_store(fetch, "the store's schema changed after admission", None)
        self.schema_version = current

    # connection setup

    def configure(self, dbapi_connection, connection_record) -> None:
        """Set connection-local settings and install the authorizer, or raise.

        The caller closes the connection when this raises. ``journal_mode`` is read and never set:
        it is a property of the store, and the store belongs to the managing application.
        """
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA synchronous = NORMAL")
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA journal_mode")
            mode = str(cursor.fetchone()[0]).lower()
        finally:
            cursor.close()
        if mode != "wal":
            raise self.refusal(
                f"the store's journal_mode is {mode}, and compatibility mode never changes it",
                operation="open",
            )
        driver = connection_record.driver_connection
        if not callable(getattr(driver, "set_authorizer", None)):
            raise self.refusal(
                "the SQLite driver cannot install an authorizer; aiosqlite 0.22.0 or later is "
                "required",
                operation="open",
            )
        state = _ConnectionState()
        connection_record.info[_STATE_KEY] = state
        authorizer = self._authorizer(state, driver)
        dbapi_connection.run_async(lambda conn: conn.set_authorizer(authorizer))
        # Positive control: a statement prepared after installation must reach the authorizer.
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SELECT 1")
            cursor.fetchall()
        finally:
            cursor.close()
        if not state.seen:
            raise self.refusal("the authorizer did not run once installed", operation="open")

    def _authorizer(self, state: _ConnectionState, driver: Any) -> Callable[..., int]:
        inserts, updates = self._inserts, self._updates
        ok, deny = sqlite3.SQLITE_OK, sqlite3.SQLITE_DENY

        def denied(what: str) -> int:
            state.denied = True
            state.last_denial = what
            return deny

        # Runs on the driver's thread while SQLite prepares a statement. Every decision is read
        # again on each prepare: the engine turns the driver's statement cache off.
        def authorize(action, arg1, arg2, db_name, _trigger) -> int:
            state.seen = True
            if action in _ALLOWED_ACTIONS:
                return ok
            if action == sqlite3.SQLITE_FUNCTION:
                if (arg2 or "").lower() == "load_extension":
                    return denied("function load_extension")
                return ok
            if action == sqlite3.SQLITE_TRANSACTION:
                verb = (arg1 or "").upper()
                if verb == "BEGIN":
                    if driver.in_transaction:
                        # SQLite would refuse it and leave the open transaction under the
                        # mark it had; denying it poisons that transaction instead.
                        return denied("BEGIN inside an open transaction")
                    # The begin listener sets the mark after its own BEGIN, so a transaction
                    # anything else opens, including after SQLite ended an operation's
                    # transaction by itself, never carries it.
                    state.write_txn = False
                    return ok
                if verb == "COMMIT" and state.denied:
                    return denied("COMMIT of a transaction in which a statement was refused")
                state.write_txn = False
                return ok
            if action == sqlite3.SQLITE_PRAGMA:
                name = _fold(arg1 or "")
                if name in _TABLE_PRAGMAS or (name in _READ_PRAGMAS and arg2 is None):
                    return ok
                # SQLAlchemy sets this connection-local flag when it resets an isolation level.
                if name == "read_uncommitted" and arg2 in (None, "0", "1"):
                    return ok
                return denied(f"PRAGMA {name}" + ("" if arg2 is None else f" = {arg2}"))
            table = _fold(arg1 or "")
            if state.write_txn and not driver.in_transaction:
                # SQLite ended the operation's transaction by itself (a ROLLBACK conflict
                # resolution) without a callback, so the flag is stale. A write prepared now
                # would run in a transaction the driver opens for it, which the operation's
                # COMMIT would then commit.
                state.write_txn = False
            if action == sqlite3.SQLITE_INSERT:
                if db_name == "main" and state.write_txn and table in inserts:
                    return ok
                return denied(f"INSERT INTO {db_name}.{table}")
            if action == sqlite3.SQLITE_UPDATE:
                if db_name == "main" and state.write_txn and (table, _fold(arg2 or "")) in updates:
                    return ok
                return denied(f"UPDATE of {db_name}.{table}.{arg2}")
            name = _ACTION_NAMES.get(action, f"action {action}")
            return denied(" ".join(str(part) for part in (name, arg1, arg2) if part))

        return authorize

    # engine listeners

    def install(self, sync_engine) -> None:
        event.listen(sync_engine, "begin", self._on_begin)
        event.listen(sync_engine, "before_cursor_execute", self._on_statement)
        event.listen(sync_engine, "handle_error", self._on_error)

    def _on_begin(self, connection) -> None:
        if connection.get_execution_options().get("isolation_level") == "AUTOCOMMIT":
            return
        self.require_scope("transaction")
        state = connection.info.get(_STATE_KEY)
        if state is None:
            raise self.refusal("this connection was not configured for compatibility mode")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        # Cleared only now: a BEGIN that succeeded proves no earlier transaction is still open on
        # this connection, so nothing poisoned can be committed under a clean flag.
        state.denied = False
        state.last_denial = None
        try:
            self._recheck(connection)
            # Under the writer's lock, so the principal cannot go between this read and the
            # transaction's writes.
            self._check_owner(connection, "the write transaction", None)
        except BaseException:
            try:
                connection.exec_driver_sql("ROLLBACK")
            except Exception:  # noqa: S110 -- the pool's reset rolls back too; the refusal wins
                pass
            raise
        state.write_txn = True

    def _on_statement(self, connection, _cursor, statement, parameters, _context, many) -> None:
        state = connection.info.get(_STATE_KEY)
        if state is None:
            raise self.refusal("this connection was not configured for compatibility mode")
        state.last_denial = None
        tokens = _sql_tokens(statement)
        kind = _statement_kind(tokens)
        if kind in ("read", "begin", "commit", "rollback"):
            return
        if kind == "savepoint":
            reason = "savepoints are not admitted"
        elif _current_scope() is None:
            reason = "compatibility mode runs no write outside an operation the profile names"
        elif not state.write_txn:
            reason = "a write runs only inside the transaction its operation opened"
        elif not connection.connection.driver_connection.in_transaction:
            # SQLite ended the transaction by itself (a ROLLBACK conflict resolution, a full disk)
            # without a callback the authorizer sees, so the flag is stale.
            state.write_txn = False
            state.denied = True
            reason = (
                "SQLite rolled back the transaction this operation opened, and its earlier writes "
                "with it; no later write runs outside a transaction"
            )
        else:
            reason = _write_refusal(tokens) or self._owner_refusal(tokens, parameters, many)
            if reason is None:
                return
        if state.write_txn:
            state.denied = True
        raise self.refusal(reason)

    def _owner_refusal(
        self, tokens: list[tuple[str, str]], parameters: Any, many: bool
    ) -> str | None:
        # The authorizer sees the table and columns a write touches, never the values it writes or
        # the rows its WHERE clause picks, so the owner of an owned row is checked here.
        rule = self.profile.owner
        target = None if rule is None else _write_target(tokens)
        if rule is None or target is None:
            return None
        verb, table, after = target
        if table is None:
            return f"the table this {verb.upper()} writes cannot be read"
        if table not in self.owned_tables:
            return None
        if verb == "insert":
            what = f"an INSERT into {table}"
            values = _insert_owner_values(tokens, after, rule.column)
            if isinstance(values, str):
                return f"{what} {values}; a default is never the owner"
        else:
            what = f"an UPDATE of {table}"
            values = _update_owner_values(tokens, after, rule.column)
            if isinstance(values, str):
                return f"{what} {values}"
        slots = _placeholders(tokens)
        rows = _parameter_rows(parameters, many)
        if slots is None or rows is None:
            return f"{what} binds its values in a form whose owner cannot be read"

        def bound(first: int, end: int) -> bool:
            if end != first + 1 or first not in slots:
                return False
            at = slots.index(first)
            return all(
                len(row) > at and type(row[at]) is str and row[at] == self.owner for row in rows
            )

        found = [bound(first, end) for first, end in values]
        if all(found) if verb == "insert" else any(found):
            return None
        if verb == "insert":
            return f"{what} writes the owner column {rule.column} with a value other than the owner"
        return f"{what} does not restrict its rows to the owner by a bound {rule.column} = ?"

    def _on_error(self, context) -> None:
        if isinstance(context.original_exception, StateCompatibilityRefused):
            return
        connection = context.connection
        if connection is None or not _is_auth_error(context.original_exception):
            return
        try:
            state = connection.info.get(_STATE_KEY)
        except Exception:  # an invalidated connection has no info; the driver error stands
            return
        if state is None or state.last_denial is None:
            return
        raise self.refusal(f"the authorizer denied {state.last_denial}") from (
            context.original_exception
        )


def _operation(name: str):
    """Mark a StateDB method as operation *name*.

    In compatibility mode the method runs only when the profile names *name*, and its writes run
    inside the operation's scope; natively the mark does nothing.
    """

    def decorate(method):
        @functools.wraps(method)
        async def wrapper(self, *args, **kwargs):
            guard = self._compat
            if guard is None:
                return await method(self, *args, **kwargs)
            token, scope = guard.enter(name)
            try:
                return await method(self, *args, **kwargs)
            finally:
                # A task the operation started holds this scope in its copied context.
                scope.active = False
                _SCOPE.reset(token)

        return wrapper

    return decorate
