# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""LNDL AST Nodes."""

from dataclasses import dataclass


class ASTNode:
    __slots__ = ()


class Expr(ASTNode):
    __slots__ = ()


@dataclass(slots=True)
class Literal(Expr):
    value: str | int | float | bool


@dataclass(slots=True)
class Identifier(Expr):
    name: str


class Stmt(ASTNode):
    __slots__ = ()


@dataclass(slots=True)
class Lvar(Stmt):
    model: str
    field: str
    alias: str
    content: str


@dataclass(slots=True)
class RLvar(Stmt):
    alias: str
    content: str
    # Two-token raw form ``<lvar hint alias>...</lvar>`` records the leading
    # token here so the OUT-shortcut path can resolve ``alias`` back to
    # ``hint`` (the implied spec name).  ``None`` for the single-token
    # ``<lvar alias>`` form.
    extra_id: str | None = None


@dataclass(slots=True)
class Lact(Stmt):
    model: str | None
    field: str | None
    alias: str
    call: str
    # Two-token form ``<lact hint alias>fn(...)</lact>`` — same role as
    # ``RLvar.extra_id``: a hint that ``alias`` is meant to fill the spec
    # named ``hint``. Used by OUT-shortcut resolution.
    extra_id: str | None = None


@dataclass(slots=True)
class OutBlock(Stmt):
    fields: dict[str, list[str] | str | int | float | bool]


@dataclass(slots=True)
class Program:
    lvars: list[Lvar | RLvar]
    lacts: list[Lact]
    out_block: OutBlock | None


__all__ = (
    "ASTNode",
    "Expr",
    "Identifier",
    "Lact",
    "Literal",
    "Lvar",
    "OutBlock",
    "Program",
    "RLvar",
    "Stmt",
)
