# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Test-data loaders.

Reads JSON/JSONL fixtures bundled at ``lionagi/testing/data/`` — OpenAI-shaped
sample responses, error scenarios, and conversation traces. Used by both the
legacy ``LionAGIMockFactory`` path and the new ``ScriptedEndpoint`` workflow.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Default lives inside the installed package so users get fixtures even when
# they don't have the lionagi source tree.
_DATA_DIR = Path(__file__).resolve().parent / "data"


class TestDataLoader:
    """Centralized loader for bundled test data files."""

    def __init__(self, data_dir: Path | None = None):
        self.data_dir = Path(data_dir) if data_dir is not None else _DATA_DIR

    def load_json(self, filename: str) -> dict[str, Any]:
        """Load a JSON fixture from within ``self.data_dir``.

        *filename* must be a plain name (no path separators, no ``..``).
        Absolute paths and traversal sequences are rejected fail-closed before
        any filesystem access (LIONAGI-AUDIT-002 path-boundary fix).
        """
        import os

        # Reject absolute paths and names containing any path separator.
        if os.path.isabs(filename) or os.sep in filename or (os.altsep and os.altsep in filename):
            raise ValueError(f"Filename must be a plain name, not a path: {filename!r}")
        # Reject forward slashes explicitly (covers posix and windows altsep gaps).
        if "/" in filename or "\\" in filename:
            raise ValueError(f"Filename must be a plain name, not a path: {filename!r}")

        if not filename.endswith(".json"):
            filename += ".json"

        data_dir_resolved = self.data_dir.resolve()
        file_path = (data_dir_resolved / filename).resolve()

        # Containment check — resolved path must stay inside data_dir.
        if (
            not str(file_path).startswith(str(data_dir_resolved) + os.sep)
            and file_path != data_dir_resolved
        ):
            raise PermissionError(f"Fixture path escapes data directory: {filename!r}")

        if not file_path.exists():
            raise FileNotFoundError(f"Test data file not found: {file_path}")
        with open(file_path, encoding="utf-8") as f:
            return json.load(f)

    def get_conversation_data(self, scenario: str) -> dict[str, Any]:
        conversations = self.load_json("sample_conversations")
        if scenario not in conversations:
            available = list(conversations.keys())
            raise ValueError(f"Scenario '{scenario}' not found. Available: {available}")
        return conversations[scenario]

    def get_api_response(self, response_type: str) -> dict[str, Any]:
        responses = self.load_json("api_responses")
        if response_type not in responses:
            available = list(responses.keys())
            raise ValueError(f"Response type '{response_type}' not found. Available: {available}")
        return responses[response_type]

    def get_error_scenario(self, error_type: str) -> dict[str, Any]:
        errors = self.load_json("error_scenarios")
        if error_type not in errors:
            available = list(errors.keys())
            raise ValueError(f"Error type '{error_type}' not found. Available: {available}")
        return errors[error_type]

    def list_conversations(self) -> list[str]:
        return list(self.load_json("sample_conversations").keys())

    def list_api_responses(self) -> list[str]:
        return list(self.load_json("api_responses").keys())

    def list_error_scenarios(self) -> list[str]:
        return list(self.load_json("error_scenarios").keys())


_default_loader = TestDataLoader()


def load_test_data(filename: str) -> dict[str, Any]:
    return _default_loader.load_json(filename)


def get_conversation(scenario: str) -> dict[str, Any]:
    return _default_loader.get_conversation_data(scenario)


def get_api_response(response_type: str) -> dict[str, Any]:
    return _default_loader.get_api_response(response_type)


def get_error_scenario(error_type: str) -> dict[str, Any]:
    return _default_loader.get_error_scenario(error_type)


__all__ = (
    "TestDataLoader",
    "get_api_response",
    "get_conversation",
    "get_error_scenario",
    "load_test_data",
)
