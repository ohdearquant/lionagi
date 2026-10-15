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

_WORD, _IDENT, _STRING, _PUNCT = "word", "ident", "string", "punct"
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
# Words after which SQLite reads a double-quoted token as a name, never as a string literal.
_NAME_POSITIONS = frozenset(
    {"references", "collate", "into", "from", "update", "on", "join", "table", "index", "trigger"}
)


def _fold(name: str) -> str:
    """*name* as SQLite compares names: ASCII letters without regard to case, any other as it is."""
    return name.translate(_ASCII_LOWER)


def _identifier_char(ch: str) -> bool:
    # SQLite reads every character past ASCII as part of an identifier.
    return not ch.isascii() or ch.isalnum() or ch in "_$"


def _sql_tokens(
    sql: str, *, spelled: bool = False, columns: frozenset[str] = frozenset()
) -> list[tuple[str, str]]:
    """Split SQL into ``(kind, text)`` tokens, dropping comments and whitespace.

    Bare words are folded as SQLite folds a name, ASCII letters only. A quoted identifier ("x",
    `x`, [x]) is unquoted and folded but keeps its own kind, so a column named "returning" is
    never read as the keyword. A string literal keeps its text verbatim, quotes included. As in
    SQLite, only ASCII whitespace separates tokens and every character past ASCII belongs to an
    identifier.

    *spelled* keeps the case of a double-quoted token that SQLite may read as a string literal,
    for comparing definitions: one that names no column is one, so ``DEFAULT "Open"`` and
    ``DEFAULT "open"`` differ. A double-quoted token after a dot or a word that takes a name
    (``REFERENCES "Runs"``, ``ON "Runs"``, ``COLLATE "NOCASE"``), or one naming one of *columns*,
    is a name, and is folded.
    """
    tokens: list[tuple[str, str]] = []
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
        elif ch == "'":
            j = i + 1
            while j < n and not (sql[j] == "'" and sql[j + 1 : j + 2] != "'"):
                j += 2 if sql[j] == "'" else 1
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
            last = tokens[-1] if tokens else (_PUNCT, "")
            named = last == (_PUNCT, ".") or (last[0] == _WORD and last[1] in _NAME_POSITIONS)
            literal = spelled and ch == '"' and not named and _fold(name) not in columns
            tokens.append((_IDENT, name if literal else _fold(name)))
            i = j + 1
        elif _identifier_char(ch):
            j = i + 1
            while j < n and _identifier_char(sql[j]):
                j += 1
            tokens.append((_WORD, _fold(sql[i:j])))
            i = j
        else:
            tokens.append((_PUNCT, ch))
            i += 1
    return tokens


def _joined(tokens: list[tuple[str, str]]) -> str:
    """One canonical spelling of *tokens*: single spaces, none inside parentheses or around dots."""
    out: list[str] = []
    for index, (kind, text) in enumerate(tokens):
        tight = (kind == _PUNCT and text in (")", ",", ".")) or (
            index and tokens[index - 1] in ((_PUNCT, "("), (_PUNCT, "."))
        )
        if out and not tight:
            out.append(" ")
        out.append(text)
    return "".join(out)


def _normalize_sql(sql: str, columns: frozenset[str] = frozenset()) -> str:
    return _joined(_sql_tokens(sql, spelled=True, columns=columns))


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


# Words after which SQLite reads a string literal as a table's name, and the conflict actions an
# ``UPDATE OR <action>`` names before one.
_TABLE_BEFORE = frozenset({"into", "update", "from", "join", "on", "table", "in"})
_ACTIONS = frozenset({"replace", "rollback", "abort", "fail", "ignore"})
# Words that end a FROM list, after which a comma no longer separates table names.
_FROM_ENDS = frozenset(
    {
        "where",
        "group",
        "order",
        "limit",
        "having",
        "window",
        "union",
        "intersect",
        "except",
        "set",
        "select",
        "values",
        "returning",
    }
)


def _names(tokens: list[tuple[str, str]]) -> set[str]:
    """Every token that can name a table, folded as SQLite folds a name.

    A word or quoted identifier always counts. A string literal counts only where SQLite's grammar
    reads one as a table's name: after INTO, UPDATE, FROM, JOIN, ON, TABLE or IN, after a dot,
    after ``OR <action>``, or after a comma in a FROM list. A literal value counts nowhere.
    """
    names: set[str] = set()
    in_from = [False]
    for index, (kind, text) in enumerate(tokens):
        if kind == _PUNCT:
            if text == "(":
                in_from.append(False)
            elif text == ")" and len(in_from) > 1:
                in_from.pop()
            elif text == ";":
                in_from = [False]
            continue
        if kind == _WORD and text in ("from", "join"):
            in_from[-1] = True
        elif kind == _WORD and text in _FROM_ENDS:
            in_from[-1] = False
        if kind != _STRING:
            names.add(text)
            continue
        last = tokens[index - 1] if index else (_PUNCT, "")
        before = tokens[index - 2] if index > 1 else (_PUNCT, "")
        if (
            last == (_PUNCT, ".")
            or (last[0] == _WORD and last[1] in _TABLE_BEFORE)
            or (last[0] == _WORD and last[1] in _ACTIONS and before == (_WORD, "or"))
            or (last == (_PUNCT, ",") and in_from[-1])
        ):
            names.add(_fold(text[1:-1].replace("''", "'")))
    return names


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


def _trigger_event(tokens: list[tuple[str, str]], top: list[int], on: int | None) -> str | None:
    """The event a trigger fires on: the last of INSERT, UPDATE and DELETE before its ``ON``.

    The event word sits just before the ON that names the table, so the last one wins and a
    trigger whose name is one of the three is not mistaken for it.
    """
    if on is None:
        return None
    return next(
        (
            tokens[index][1]
            for index in reversed(top)
            if index < on and tokens[index][1] in ("insert", "update", "delete")
        ),
        None,
    )


def _trigger_owner_problems(sql: str, column: str, owned: frozenset[str]) -> list[str]:
    """How a trigger's statements write an owned table other than with the owner of the row that
    fired it: an INSERT must copy ``NEW.<column>`` (``OLD.<column>`` in a DELETE trigger) into
    every row, and an UPDATE must restrict its rows by a term comparing the column with one of
    the rows its event makes readable.
    """
    tokens = _sql_tokens(sql)
    depths = _depths(tokens)
    top = [index for index in range(len(tokens)) if depths[index] == 0]
    # The trigger's own name may be the keyword BEGIN, so the body starts at the first BEGIN
    # after the ON that names the table the trigger fires on.
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
    # An INSERT trigger's OLD and a DELETE trigger's NEW are NULL, so neither is an owner.
    event = _trigger_event(tokens, top, on)
    rows = {"insert": ("new",), "delete": ("old",)}.get(event, ("new", "old"))
    statements: list[list[tuple[str, str]]] = [[]]
    for index in range(begin + 1, len(tokens)):
        if depths[index] == 0 and tokens[index] == (_PUNCT, ";"):
            statements.append([])
        else:
            statements[-1].append(tokens[index])
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
        reader = _insert_owner_values if verb == "insert" else _update_owner_values
        values = reader(statement, after, column)
        what = f"{'inserts into' if verb == 'insert' else 'updates'} {table}"
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
    return None


# Structure


@dataclass(frozen=True)
class ColumnShape:
    """One column as ``PRAGMA table_xinfo`` reports it.

    ``type`` is uppercased with single spaces and ``default`` is normalized SQL text. ``pk`` is the
    column's position in the primary key (0 when it is not part of it); ``hidden`` is 2 for a
    virtual and 3 for a stored generated column.
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
    one spelling (whitespace, comments, identifier quotes and the case of words aside; a
    double-quoted token keeps its case, since SQLite can read it as a string literal), so what
    PRAGMA output leaves out is compared too: a collation, a generated column's expression, a
    ``DEFERRABLE`` foreign key, ``AUTOINCREMENT``. Two definitions SQLite reads alike but spells
    differently differ: ``REFERENCES runs`` and ``REFERENCES runs(id)``, a constraint with and
    without a name, a default spelled out (``NO ACTION``, ``COLLATE BINARY``, ``ASC``),
    constraints in another order, a column's ``UNIQUE`` and the table's, ``<>`` and ``!=``.
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
    """A trigger or view whose SQL names a closure table, with that SQL normalized."""

    type: str
    name: str
    sql: str


@dataclass(frozen=True)
class Closure:
    """The structure of a set of tables and of everything in the store that touches them.

    ``foreign_keys`` holds every key into or out of the tables; ``objects`` every trigger and view
    whose SQL names one of them.
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


def _column_collation(definition: str) -> str:
    """The collation a column's definition declares, ``BINARY`` when it declares none."""
    tokens = _sql_tokens(definition)
    for index, token in enumerate(tokens):
        if token == (_WORD, "collate") and tokens[index + 1 : index + 2]:
            return tokens[index + 1][1].upper()
    return "BINARY"


_TABLE_CONSTRAINTS = frozenset({"constraint", "primary", "unique", "check", "foreign"})


def _ordered_collation(table: TableShape, column: str) -> str:
    """The collation *column* compares under in *table*, read from its column definition."""
    for definition in sorted(table.definitions):
        tokens = _sql_tokens(definition)
        if (
            len(tokens) > 1
            and tokens[0][0] in (_WORD, _IDENT)
            and tokens[0][1] == column
            and tokens[1][1] not in _TABLE_CONSTRAINTS
        ):
            return _column_collation(definition)
    return "BINARY"


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
    columns = tuple(
        ColumnShape(
            name=_fold(str(row[1])),
            type=" ".join(str(row[2] or "").upper().split()),
            notnull=bool(row[3]),
            default=None if row[4] is None else _normalize_sql(str(row[4])),
            pk=int(row[5]),
            hidden=int(row[6]),
        )
        for row in fetch(f"PRAGMA table_xinfo({quoted})")
    )
    names = frozenset(column.name for column in columns)
    unique: set[tuple[str, ...]] = set()
    for row in fetch(f"PRAGMA index_list({quoted})"):
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
    return TableShape(
        _fold(name),
        columns,
        frozenset(unique),
        _checks(tokens),
        _options(tokens),
        _conflicts(tokens),
        _definitions(tokens),
    )


def _foreign_keys(fetch: _Fetch, table: str) -> list[ForeignKeyShape]:
    groups: dict[Any, list] = {}
    for row in fetch(f"PRAGMA foreign_key_list({_quote(table)})"):
        groups.setdefault(row[0], []).append(row)
    keys = []
    for rows in groups.values():
        rows.sort(key=lambda row: row[1])
        parent_columns = tuple(_fold(str(row[4])) for row in rows if row[4] is not None)
        keys.append(
            ForeignKeyShape(
                table=_fold(table),
                columns=tuple(_fold(str(row[3])) for row in rows),
                parent=_fold(str(rows[0][2])),
                parent_columns=parent_columns if len(parent_columns) == len(rows) else (),
                on_update=str(rows[0][5]).upper(),
                on_delete=str(rows[0][6]).upper(),
            )
        )
    return keys


def _read_closure(fetch: _Fetch, tables: Iterable[str]) -> Closure:
    wanted = frozenset(_fold(name) for name in tables)
    entries = {
        (str(row[0]), _fold(str(row[1]))): row
        for row in fetch("SELECT type, name, tbl_name, sql FROM sqlite_master")
    }
    shapes = [
        _table_shape(fetch, str(row[1]), row[3] or "", entries)
        for name in sorted(wanted)
        if (row := entries.get(("table", name))) is not None
    ]
    columns = frozenset(column.name for shape in shapes for column in shape.columns)
    keys: set[ForeignKeyShape] = set()
    objects: set[SchemaObject] = set()
    for (kind, name), row in entries.items():
        if kind == "table" and not name.startswith("sqlite_"):
            keys.update(
                key
                for key in _foreign_keys(fetch, str(row[1]))
                if key.table in wanted or key.parent in wanted
            )
        elif kind in ("trigger", "view") and row[3]:
            # A table is named with its ASCII letters in either case, as SQLite matches it,
            # and can be named by a string literal where a name is read; the SQL is kept as a
            # definition is, with the case of a double-quoted token SQLite may read as a literal.
            if wanted.intersection(_names(_sql_tokens(row[3]))):
                objects.add(SchemaObject(kind, name, _normalize_sql(row[3], columns)))
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
    for key in sorted(observed.foreign_keys - expected.foreign_keys, key=repr):
        found.append(f"foreign key not in the profile: {key}")
    for key in sorted(expected.foreign_keys - observed.foreign_keys, key=repr):
        found.append(f"foreign key missing: {key}")
    for item in sorted(observed.objects - expected.objects, key=repr):
        found.append(f"{item.type} {item.name} is not in the profile")
    for item in sorted(expected.objects - observed.objects, key=repr):
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
        return _closure_differences(expected, _read_closure(fetch, expected.table_names))

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
        rule = self.profile.owner
        if rule is not None:
            # `owner = ?` compares under the column's collation, so an owner that is not BINARY
            # takes a variant of the owner's name as the owner, and writes another owner's row.
            variants = [
                f"{table.name}.{rule.column} compares under {_ordered_collation(table, rule.column)}"
                for table in self.profile.closure.tables
                if rule.column in {column.name for column in table.columns}
                and _ordered_collation(table, rule.column) != "BINARY"
            ]
            if variants:
                raise self.refusal(
                    f"{when}: the owner column compares under a collation, not BINARY, so its "
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
        found = connection.exec_driver_sql(
            f"SELECT 1 FROM {_quote(rule.principals)} WHERE {_quote(rule.key)} = ?",  # noqa: S608
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
