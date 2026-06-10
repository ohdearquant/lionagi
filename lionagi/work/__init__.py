# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""lionagi.work — WorkForm + Rule/RuleSet (R2-1 slim re-cut).

Public surface::

    from lionagi.work import (
        FieldSpec,
        FieldType,
        FormStatus,
        VALID_TRANSITIONS,
        WorkForm,
        fill_form,
        validate_form,
        Rule,
        RuleSet,
        CheckKind,
        REGEX_MAX_INPUT_LENGTH,
        REGEX_MATCH_TIMEOUT,
    )
"""

from .form import (
    VALID_TRANSITIONS,
    FieldSpec,
    FieldType,
    FormStatus,
    WorkForm,
    fill_form,
    validate_form,
)
from .rules import (
    REGEX_MATCH_TIMEOUT,
    REGEX_MAX_INPUT_LENGTH,
    CheckKind,
    Rule,
    RuleSet,
)

__all__ = (
    # form
    "FieldSpec",
    "FieldType",
    "FormStatus",
    "VALID_TRANSITIONS",
    "WorkForm",
    "fill_form",
    "validate_form",
    # rules
    "Rule",
    "RuleSet",
    "CheckKind",
    "REGEX_MAX_INPUT_LENGTH",
    "REGEX_MATCH_TIMEOUT",
)
