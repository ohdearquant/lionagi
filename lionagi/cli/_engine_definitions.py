# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Engine kind registry and class loader shared by the engine command and Studio.

This module imports no command module, so both the `li engine` command and the
Studio workflow compiler can use it without importing one another.
"""

from __future__ import annotations

from typing import Any

from lionagi.ln import import_module

# ── Engine kind registry ───────────────────────────────────────────────────
# Maps the CLI kind name to the class import path and the main positional
# argument name and help text.  Adding a new kind means adding one entry here.

KIND_META: dict[str, dict[str, Any]] = {
    "research": {
        "cls_path": ("lionhub.orchestration.engines", "ResearchEngine"),
        "pos_arg": "topic",
        "pos_help": "Research topic or question.",
    },
    "review": {
        "cls_path": ("lionhub.orchestration.engines", "ReviewEngine"),
        "pos_arg": "artifact",
        "pos_help": "Artifact text or path to review.",
    },
    "coding": {
        "cls_path": ("lionhub.orchestration.engines", "CodingEngine"),
        "pos_arg": "spec",
        "pos_help": "Coding specification (natural-language or JSON string).",
    },
    "hypothesis": {
        "cls_path": ("lionhub.orchestration.engines", "HypothesisEngine"),
        "pos_arg": "findings",
        "pos_help": "Research findings or background text for hypothesis generation.",
    },
    "planning": {
        "cls_path": ("lionhub.orchestration.engines", "PlanningEngine"),
        "pos_arg": "prompt",
        "pos_help": "Goal or task description to plan and execute.",
    },
}


def import_engine_class(module: str, name: str) -> type:
    mod = import_module(module)
    return getattr(mod, name)
