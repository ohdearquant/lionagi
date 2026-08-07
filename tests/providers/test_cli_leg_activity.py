# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The bounded per-leg activity record: what a CLI leg did, without quoting it."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from lionagi.service.types.cli_session import CLISession

CLI_PROVIDERS = [
    "lionagi/providers/anthropic/claude_code.py",
    "lionagi/providers/openai/codex.py",
    "lionagi/providers/google/gemini_code.py",
    "lionagi/providers/pi/cli.py",
]

REPO_ROOT = Path(__file__).resolve().parents[2]


def _session_with_tools(tool_uses):
    session = CLISession(session_id="s1", model="m")
    session.tool_uses = tool_uses
    return session


def test_activity_counts_tools_and_file_operations():
    session = _session_with_tools(
        [
            {"name": "Read", "input": {"file_path": "/a.py"}},
            {"name": "Read", "input": {"file_path": "/b.py"}},
            {"name": "Write", "input": {"file_path": "/c.py"}},
            {"name": "MultiEdit", "input": {"file_path": "/d.py"}},
            {"name": "Bash", "input": {"command": "ls"}},
        ]
    )
    session.populate_activity()

    assert session.activity["total_tool_calls"] == 5
    assert session.activity["tool_counts"] == {
        "Read": 2,
        "Write": 1,
        "MultiEdit": 1,
        "Bash": 1,
    }
    # MultiEdit is in the edit family, so it must count as an edit rather
    # than falling through to uncategorised.
    assert session.activity["file_operations"] == {"reads": 2, "writes": 1, "edits": 1}


def test_activity_carries_no_tool_inputs():
    """The property that lets this record default to on.

    If any tool argument can reach the activity block, the block needs a
    redactor in front of it and can no longer be written unconditionally.
    """
    secret_path = "/Users/someone/.ssh/id_rsa_UNIQUEMARKER"
    secret_cmd = "curl -H 'Authorization: Bearer TOKENMARKER' https://example.invalid"

    session = _session_with_tools(
        [
            {"name": "Read", "input": {"file_path": secret_path}, "id": "IDMARKER"},
            {"name": "Bash", "input": {"command": secret_cmd}},
        ]
    )
    session.populate_activity()

    blob = json.dumps(session.activity)
    assert "UNIQUEMARKER" not in blob
    assert "TOKENMARKER" not in blob
    assert "IDMARKER" not in blob
    assert secret_path not in blob
    assert secret_cmd not in blob
    # Positive control: the tool NAMES are the thing it is supposed to keep,
    # so an empty/broken extractor cannot pass the assertions above.
    assert "Read" in blob and "Bash" in blob


def test_activity_size_is_bounded_by_distinct_tool_names_not_call_count():
    """Key count tracks distinct tool names; only the integers grow.

    This is what makes the record safe to write on every leg: a run with
    three thousand calls stores the same handful of keys as one with three.
    """
    names = ["Read", "Write", "Bash"]
    few = _session_with_tools([{"name": n, "input": {"file_path": "/a"}} for n in names])
    many = _session_with_tools(
        [{"name": n, "input": {"file_path": "/a"}} for n in names for _ in range(1000)]
    )
    few.populate_activity()
    many.populate_activity()

    assert many.activity["total_tool_calls"] == 3000
    assert few.activity["total_tool_calls"] == 3
    # The structure is identical; only the counts differ.
    assert many.activity.keys() == few.activity.keys()
    assert many.activity["tool_counts"].keys() == few.activity["tool_counts"].keys()
    assert len(many.activity["tool_counts"]) == len(names)
    assert many.activity["file_operations"].keys() == few.activity["file_operations"].keys()


def test_full_summary_still_carries_detail_when_opted_in():
    """The opt-in path keeps what the default path drops, or the detail is gone."""
    session = _session_with_tools([{"name": "Read", "input": {"file_path": "/secret.py"}}])
    session.populate_summary()

    blob = json.dumps(session.summary)
    assert "/secret.py" in blob
    assert session.summary["tool_details"]


@pytest.mark.parametrize("rel_path", CLI_PROVIDERS)
def test_provider_populates_activity_outside_the_opt_in_gate(rel_path):
    """Every CLI provider must record activity unconditionally.

    A provider that calls populate_activity() inside the
    ``if request.cli_include_summary:`` block would record nothing on the
    default path while still passing any test that only checks the call
    exists. Asserted structurally rather than by grep so an indentation
    change cannot silently move the call under the gate.
    """
    tree = ast.parse((REPO_ROOT / rel_path).read_text())

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "populate_activity"
    ]
    # Non-empty input assert: an unparsed or renamed file must fail here
    # rather than pass the gated-call check vacuously.
    assert len(calls) >= 1, f"{rel_path} never calls populate_activity()"

    gated = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if "cli_include_summary" not in ast.dump(node.test):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "populate_activity"
            ):
                gated.append(inner)

    assert not gated, f"{rel_path} calls populate_activity() inside the opt-in gate"

    # Positive control for the gate detector itself: the summary call IS
    # gated, so a detector that finds nothing anywhere is broken, not clean.
    gated_summary = [
        inner
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and "cli_include_summary" in ast.dump(node.test)
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Attribute)
        and inner.func.attr == "populate_summary"
    ]
    assert gated_summary, f"{rel_path}: gate detector found no gated populate_summary"


@pytest.mark.parametrize(
    ("tool_name", "bucket"),
    [
        ("Read", "reads"),
        ("read_file", "reads"),
        ("Write", "writes"),
        ("create_file", "writes"),
        ("Edit", "edits"),
        ("MultiEdit", "edits"),
        ("patch", "edits"),
    ],
)
def test_both_extractors_agree_on_file_operation_family(tool_name, bucket):
    """The two views classify against one shared set of tool names.

    They report differently (counts here, paths there) but must never
    disagree about which family a tool belongs to.
    """
    session = _session_with_tools([{"name": tool_name, "input": {"file_path": "/x.py"}}])
    session.populate_activity()
    session.populate_summary()

    assert session.activity["file_operations"][bucket] == 1
    assert session.summary["file_operations"][bucket] == ["/x.py"]
