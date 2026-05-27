# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""lionagi.work — structured forms, validation rules, worker definitions, and engine.

Public surface:

    Forms & field specs
    -------------------
    FieldSpec       — typed field declaration (name, type, required, default)
    WorkForm        — structured input/output container with lifecycle status
    fill_form()     — populate a form with values (auto-validates)
    validate_form() — validate form values against FieldSpec declarations

    Validation rules
    ----------------
    Rule            — single declarative validation rule (required/type/range/pattern/custom)
    RuleSet         — ordered collection of rules applied to a WorkForm

    Worker definitions
    ------------------
    WorkerDefinition — static descriptor for a worker type
    load_definition() — load from a YAML/JSON path or plain dict

    Engine
    ------
    WorkEngine   — orchestrator: register workers, submit forms, query results
    WorkTask     — runtime record for a single submitted task
    WorkResult   — outcome of a completed or failed task
"""

from __future__ import annotations

from .definition import WorkerDefinition, load_definition
from .engine import WorkEngine, WorkResult, WorkTask
from .form import FieldSpec, WorkForm, fill_form, validate_form
from .rules import Rule, RuleSet

__all__ = (
    # Forms
    "FieldSpec",
    "WorkForm",
    "fill_form",
    "validate_form",
    # Rules
    "Rule",
    "RuleSet",
    # Worker definitions
    "WorkerDefinition",
    "load_definition",
    # Engine
    "WorkEngine",
    "WorkResult",
    "WorkTask",
)
