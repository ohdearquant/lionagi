# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Every CLI flag the submit tools expose reaches argv, and the rest are refused.

A promoted flag is only real if it arrives at the CLI spelled the way the CLI
expects, so each parameter is asserted against the exact tokens it produces. The
flags a background run cannot honour are asserted to fail loudly instead of being
accepted and dropped.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastmcp", reason="requires the 'mcp' extra")

from lionagi.mcp import server  # noqa: E402 — must follow the extra guard


@pytest.fixture
def captured(monkeypatch):
    seen: dict = {}

    def fake_submit(kind, flags, **kwargs):
        seen["kind"] = kind
        seen["flags"] = list(flags)
        seen.update(kwargs)
        return {"run_id": "rid", "pid": 1, "status": "running"}

    monkeypatch.setattr(server.jobs, "submit", fake_submit)
    return seen


def _assert_tokens(flags: list[str], expected: list[str]) -> None:
    """The expected tokens appear, in order and adjacent, inside flags."""
    n = len(expected)
    windows = [flags[i : i + n] for i in range(len(flags) - n + 1)]
    assert expected in windows, f"{expected} not in {flags}"


# (tool, kwargs, tokens the CLI must receive) — one row per parameter.
AGENT_FLAGS = [
    ({"model": "claude/opus"}, ["claude/opus"]),
    ({"agent": "reviewer"}, ["-a", "reviewer"]),
    ({"preset": "coding"}, ["--preset", "coding"]),
    ({"form": "/specs/work.yaml"}, ["--form", "/specs/work.yaml"]),
    ({"image": ["/a.png"]}, ["--image", "/a.png"]),
    ({"image": ["/a.png", "/b.jpg"]}, ["--image", "/a.png", "--image", "/b.jpg"]),
    ({"effort": "high"}, ["--effort", "high"]),
    ({"cwd": "/repo"}, ["--cwd", "/repo"]),
    ({"timeout": 900}, ["--timeout", "900"]),
    ({"resume": "branch-1"}, ["-r", "branch-1"]),
    ({"continue_last": True}, ["-c"]),
    ({"context_from": ["run-1", "run-2"]}, ["--context-from", "run-1", "--context-from", "run-2"]),
    ({"context_budget": 4000}, ["--context-budget", "4000"]),
    ({"yolo": True}, ["--yolo"]),
    ({"bypass": True}, ["--bypass"]),
    ({"fast": True}, ["--fast"]),
    ({"invocation": "inv-7"}, ["--invocation", "inv-7"]),
    ({"project": "lionagi"}, ["--project", "lionagi"]),
    ({"resume_on_timeout": True}, ["--resume-on-timeout"]),
]

FLOW_FLAGS = [
    ({"model": "claude/opus"}, ["claude/opus"]),
    ({"agent": "orchestrator"}, ["-a", "orchestrator"]),
    ({"file": "/specs/flow.yaml"}, ["-f", "/specs/flow.yaml"]),
    ({"playbook": "review"}, ["-p", "review"]),
    ({"playbook_args": {"target": "docs/adr"}}, ["--target", "docs/adr"]),
    ({"playbook_args": {"max_depth": 3}}, ["--max-depth", "3"]),
    ({"playbook_args": {"deep": True}}, ["--deep"]),
    ({"effort": "high"}, ["--effort", "high"]),
    ({"cwd": "/repo"}, ["--cwd", "/repo"]),
    ({"timeout": 600}, ["--timeout", "600"]),
    ({"max_concurrent": 4}, ["--max-concurrent", "4"]),
    ({"max_ops": 12}, ["--max-ops", "12"]),
    ({"reactive": "critic,evaluator"}, ["--reactive", "critic,evaluator"]),
    ({"with_synthesis": True}, ["--with-synthesis"]),
    ({"with_synthesis": "claude/opus"}, ["--with-synthesis", "claude/opus"]),
    ({"workers": "claude/sonnet,codex"}, ["--workers", "claude/sonnet,codex"]),
    ({"pack": "/packs/cheap.yaml"}, ["--pack", "/packs/cheap.yaml"]),
    ({"bare": True}, ["--bare"]),
    ({"team_mode": True}, ["--team-mode"]),
    ({"team_mode": "reviewers"}, ["--team-mode", "reviewers"]),
    ({"team_attach": "reviewers"}, ["--team-attach", "reviewers"]),
    ({"team_max_rounds": 3}, ["--team-max-rounds", "3"]),
    ({"save": "/out"}, ["--save", "/out"]),
    ({"output": "json"}, ["--output", "json"]),
    ({"dry_run": True}, ["--dry-run"]),
    ({"show_graph": True}, ["--show-graph"]),
    ({"resume": "20260725T000000-abcdef"}, ["--resume", "20260725T000000-abcdef"]),
    (
        {"resume": "20260725T000000-abcdef", "allow_degraded_context": True},
        ["--allow-degraded-context"],
    ),
    ({"yolo": True}, ["--yolo"]),
    ({"bypass": True}, ["--bypass"]),
    ({"fast": True}, ["--fast"]),
    ({"invocation": "inv-7"}, ["--invocation", "inv-7"]),
    ({"project": "lionagi"}, ["--project", "lionagi"]),
    ({"resume_on_timeout": True}, ["--resume-on-timeout"]),
]

FANOUT_FLAGS = [
    ({"model": "claude/opus"}, ["claude/opus"]),
    ({"agent": "orchestrator"}, ["-a", "orchestrator"]),
    ({"num_workers": 5}, ["--num-workers", "5"]),
    ({"workers": "claude/sonnet,codex"}, ["--workers", "claude/sonnet,codex"]),
    ({"pack": "/packs/cheap.yaml"}, ["--pack", "/packs/cheap.yaml"]),
    ({"max_concurrent": 2}, ["--max-concurrent", "2"]),
    ({"with_synthesis": "claude/opus"}, ["--with-synthesis", "claude/opus"]),
    ({"synthesis_prompt": "rank them"}, ["--synthesis-prompt", "rank them"]),
    ({"team_mode": "sweep"}, ["--team-mode", "sweep"]),
    ({"save": "/out"}, ["--save", "/out"]),
    ({"output": "json"}, ["--output", "json"]),
    ({"effort": "high"}, ["--effort", "high"]),
    ({"cwd": "/repo"}, ["--cwd", "/repo"]),
    ({"timeout": 300}, ["--timeout", "300"]),
    ({"yolo": True}, ["--yolo"]),
    ({"bypass": True}, ["--bypass"]),
    ({"fast": True}, ["--fast"]),
    ({"invocation": "inv-7"}, ["--invocation", "inv-7"]),
    ({"project": "lionagi"}, ["--project", "lionagi"]),
    ({"resume_on_timeout": True}, ["--resume-on-timeout"]),
]

PLAY_FLAGS = [
    ({"model": "claude/opus"}, ["claude/opus"]),
    ({"agent": "orchestrator"}, ["-a", "orchestrator"]),
    ({"playbook_args": {"target": "docs/adr"}}, ["--target", "docs/adr"]),
    ({"playbook_args": {"strict": True}}, ["--strict"]),
    ({"team_mode": "reviewers"}, ["--team-mode", "reviewers"]),
    ({"team_attach": "reviewers"}, ["--team-attach", "reviewers"]),
    ({"team_max_rounds": 3}, ["--team-max-rounds", "3"]),
    ({"max_concurrent": 4}, ["--max-concurrent", "4"]),
    ({"max_ops": 12}, ["--max-ops", "12"]),
    ({"reactive": "off"}, ["--reactive", "off"]),
    ({"workers": "claude/sonnet,codex"}, ["--workers", "claude/sonnet,codex"]),
    ({"pack": "/packs/cheap.yaml"}, ["--pack", "/packs/cheap.yaml"]),
    ({"bare": True}, ["--bare"]),
    ({"with_synthesis": True}, ["--with-synthesis"]),
    ({"save": "/out"}, ["--save", "/out"]),
    ({"output": "json"}, ["--output", "json"]),
    ({"dry_run": True}, ["--dry-run"]),
    ({"show_graph": True}, ["--show-graph"]),
    ({"cwd": "/repo"}, ["--cwd", "/repo"]),
    ({"timeout": 900}, ["--timeout", "900"]),
    ({"effort": "high"}, ["--effort", "high"]),
    ({"yolo": True}, ["--yolo"]),
    ({"bypass": True}, ["--bypass"]),
    ({"fast": True}, ["--fast"]),
    ({"invocation": "inv-7"}, ["--invocation", "inv-7"]),
    ({"project": "lionagi"}, ["--project", "lionagi"]),
    ({"resume_on_timeout": True}, ["--resume-on-timeout"]),
]


def _ids(rows):
    return ["+".join(sorted(kwargs)) + "=" + "|".join(tokens) for kwargs, tokens in rows]


@pytest.mark.parametrize(("kwargs", "tokens"), AGENT_FLAGS, ids=_ids(AGENT_FLAGS))
def test_agent_parameters_reach_the_cli(captured, kwargs, tokens):
    server.submit_agent(prompt="go", **kwargs)
    assert captured["kind"] == "agent"
    _assert_tokens(captured["flags"], tokens)


@pytest.mark.parametrize(("kwargs", "tokens"), FLOW_FLAGS, ids=_ids(FLOW_FLAGS))
def test_flow_parameters_reach_the_cli(captured, kwargs, tokens):
    server.submit_flow(prompt="go", **kwargs)
    assert captured["kind"] == "flow"
    _assert_tokens(captured["flags"], tokens)


@pytest.mark.parametrize(("kwargs", "tokens"), FANOUT_FLAGS, ids=_ids(FANOUT_FLAGS))
def test_fanout_parameters_reach_the_cli(captured, kwargs, tokens):
    server.submit_fanout(prompt="go", **kwargs)
    assert captured["kind"] == "fanout"
    _assert_tokens(captured["flags"], tokens)


@pytest.mark.parametrize(("kwargs", "tokens"), PLAY_FLAGS, ids=_ids(PLAY_FLAGS))
def test_play_parameters_reach_the_cli(captured, kwargs, tokens):
    server.submit_play(name="review", prompt="go", **kwargs)
    assert captured["kind"] == "play"
    _assert_tokens(captured["flags"], tokens)


def test_a_playbook_bool_left_off_sends_no_flag(captured):
    # A declared bool is the flag's presence, so False must not send "--deep false".
    server.submit_play(name="review", playbook_args={"deep": False, "target": "x"})
    assert "--deep" not in captured["flags"]
    _assert_tokens(captured["flags"], ["--target", "x"])


@pytest.mark.parametrize(
    ("submit", "kwargs"),
    [
        (server.submit_agent, {}),
        (server.submit_flow, {}),
        (server.submit_fanout, {}),
        (server.submit_play, {"name": "review"}),
    ],
    ids=["agent", "flow", "fanout", "play"],
)
def test_the_escape_hatch_is_gone(captured, submit, kwargs):
    # Nothing may pass a raw flag list: a flag that is not a documented parameter
    # is a capability no caller can see, which is what this surface exists to end.
    with pytest.raises(TypeError):
        submit(prompt="go", extra_args=["--form", "/specs/work.yaml"], **kwargs)
    assert captured == {}


@pytest.mark.parametrize(
    ("flag", "match"),
    [
        ("verbose", "job_output"),
        ("theme", "log file"),
        ("list_profiles", "without running"),
        ("background", "orphans"),
        ("help", "schema"),
        ("notify", "notify_seat"),
        ("prompt", "prompt parameter"),
    ],
)
def test_flags_a_background_run_cannot_honour_are_refused(captured, flag, match):
    # Refusing with the reason is the feature: accepting one of these and doing
    # nothing with it would tell the caller it took effect.
    with pytest.raises(ValueError, match=match):
        server.submit_play(name="review", playbook_args={flag: True})
    assert captured == {}


def test_a_playbook_argument_that_is_not_a_name_is_refused(captured):
    with pytest.raises(ValueError, match="not an argument name"):
        server.submit_flow(prompt="go", playbook="review", playbook_args={"target dir": "x"})
    assert captured == {}


@pytest.mark.parametrize(
    ("submit", "kwargs"),
    [
        (server.submit_flow, {}),
        (server.submit_fanout, {}),
        (server.submit_play, {"name": "review"}),
    ],
    ids=["flow", "fanout", "play"],
)
def test_an_unknown_output_format_is_refused_before_submitting(captured, submit, kwargs):
    with pytest.raises(ValueError, match="must be 'text' or 'json'"):
        submit(prompt="go", output="yaml", **kwargs)
    assert captured == {}


def test_every_submit_parameter_carries_a_description():
    # The schema is what a caller reads to find a capability; an undescribed
    # parameter is only marginally more visible than a hidden one.
    import asyncio

    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in ("submit_agent", "submit_flow", "submit_fanout", "submit_play"):
        props = tools[name].parameters["properties"]
        undescribed = [k for k, v in props.items() if not v.get("description")]
        assert undescribed == [], f"{name} has undescribed parameters: {undescribed}"
