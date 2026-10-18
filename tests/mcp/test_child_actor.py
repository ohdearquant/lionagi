# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A write-capable child gets its own khive actor, minted into its tree at spawn.

khive resolves an actor only from ``<cwd>/.khive/config.toml``. A child run in
the submitting directory signs as the submitter and reads its inbox; a child
run in a bare tree signs as the unattributed pool. Minting writes the submitting
actor plus the configured suffix into the child's tree, or refuses the submission.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

import pytest

from lionagi.mcp import _child_actor, config, dispatch, jobs
from lionagi.mcp._child_actor import ChildActorError, child_actor_for, mint_child_actor_config

try:
    import tomllib as _toml
except ModuleNotFoundError:  # Python 3.10
    import toml as _toml  # type: ignore[import-untyped,no-redef]


SUBMITTER_CONFIG = """\
# khive.toml - project-local configuration
#
# Read by `kkernel mcp` from ./.khive/config.toml relative to the server cwd.

# -- Actor identity --------------------------------------------------------
[actor]
id = "lambda:x"
display_name = "lambda:x"
visible_namespaces = ["lambda:x:a", "lambda:x:b"]

# -- Embedding engines -----------------------------------------------------
[[engines]]
name = "default"
model = "all-minilm-l6-v2"
default = true

# -- Backend split ---------------------------------------------------------
[[backends]]
name = "main"
kind = "sqlite"
path = "/srv/khive/khive.db"

[packs.session]
backend = "main"

# Anchor date-only input in this zone.
[display]
timezone = "America/New_York"
"""


def _submitting_dir(path: Path, text: str = SUBMITTER_CONFIG) -> Path:
    (path / ".khive").mkdir(parents=True, exist_ok=True)
    (path / ".khive" / "config.toml").write_text(text)
    return path


def _cut(text: str) -> tuple[str, str]:
    """The submitting config before its [actor] header, and from the next header on."""
    lines = text.split("\n")
    start = next(i for i, line in enumerate(lines) if line == "[actor]")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("["))
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
        end -= 1
    return "\n".join(lines[:start]) + "\n", "\n".join(lines[end:])


def _parsed(path: Path) -> dict[str, Any]:
    return _toml.loads(path.read_text())


@pytest.fixture
def trees(tmp_path):
    submitter = _submitting_dir(tmp_path / "submitter")
    child = tmp_path / "child"
    child.mkdir()
    return submitter, child


# --- the mint ---------------------------------------------------------------


def test_the_mint_copies_every_table_and_rewrites_only_the_actor(trees):
    submitter, child = trees

    minted = mint_child_actor_config(str(submitter), str(child), ":worker")

    target = child / ".khive" / "config.toml"
    assert minted.actor == "lambda:x:worker"
    assert minted.submitting_actor == "lambda:x"
    assert minted.config_path == str(target)
    assert minted.source_path == str(submitter / ".khive" / "config.toml")
    assert minted.reused is False

    source = _parsed(submitter / ".khive" / "config.toml")
    result = _parsed(target)
    assert result.pop("actor") == {
        "id": "lambda:x:worker",
        "display_name": "lambda:x:worker",
        "visible_namespaces": ["lambda:x"],
    }
    source.pop("actor")
    assert result == source

    first = target.read_text().split("\n", 1)[0]
    assert re.fullmatch(
        r"# CHILD ACTOR CONFIG — lambda:x:worker, minted by lionagi at spawn from "
        + re.escape(str(submitter / ".khive" / "config.toml"))
        + r" \(source sha256 prefix [0-9a-f]{8}\); only \[actor\] differs\.",
        first,
    ), first
    before, after = _cut(SUBMITTER_CONFIG)
    text = target.read_text()
    assert before in text and after in text


def test_a_second_mint_for_the_same_submitter_reuses_the_child_config(trees):
    submitter, child = trees
    first = mint_child_actor_config(str(submitter), str(child), ":worker")

    second = mint_child_actor_config(str(submitter), str(child), ":worker")

    assert second.reused is True
    assert second.actor == first.actor


def test_a_child_config_naming_another_actor_is_never_overwritten(tmp_path):
    submitter = _submitting_dir(tmp_path / "submitter")
    child = _submitting_dir(tmp_path / "child", '[actor]\nid = "lambda:someone-else"\n')

    with pytest.raises(ChildActorError, match="refusing to overwrite"):
        mint_child_actor_config(str(submitter), str(child), ":worker")

    assert _parsed(child / ".khive" / "config.toml")["actor"]["id"] == "lambda:someone-else"


def test_a_child_tree_that_is_the_submitter_is_refused(tmp_path):
    submitter = _submitting_dir(tmp_path / "submitter")

    with pytest.raises(ChildActorError, match="submitting directory"):
        mint_child_actor_config(str(submitter), str(submitter), ":worker")


def test_a_submitter_without_a_config_is_refused(tmp_path):
    submitter = tmp_path / "submitter"
    submitter.mkdir()
    child = tmp_path / "child"
    child.mkdir()

    with pytest.raises(ChildActorError, match="holds no .khive/config.toml"):
        mint_child_actor_config(str(submitter), str(child), ":worker")
    assert not (child / ".khive").exists()


def test_a_submitter_that_is_already_a_child_actor_is_refused(tmp_path):
    submitter = _submitting_dir(
        tmp_path / "submitter", SUBMITTER_CONFIG.replace('id = "lambda:x"', 'id = "lambda:x:child"')
    )
    child = tmp_path / "child"
    child.mkdir()

    with pytest.raises(ChildActorError, match="already a child actor"):
        mint_child_actor_config(str(submitter), str(child), ":child")


@pytest.mark.parametrize("submitter_actor", ["", "  ", "local", "lambda:x:child"])
def test_a_child_actor_is_derived_only_from_a_named_submitting_actor(submitter_actor):
    with pytest.raises(ChildActorError):
        child_actor_for(submitter_actor, ":child")


def test_child_actor_for_appends_the_suffix():
    assert child_actor_for("lambda:x", ":child") == "lambda:x:child"


@pytest.mark.parametrize(
    "text, reason",
    [
        (SUBMITTER_CONFIG.replace('[actor]\nid = "lambda:x"\n', ""), "names no \\[actor\\].id"),
        ("[actor\nid = ", "cannot be read or parsed"),
        (
            SUBMITTER_CONFIG.replace("[actor]\n", "[actor]  # inline\n"),
            "'\\[actor\\]' header lines",
        ),
    ],
    ids=["no-actor", "unparsable", "no-bare-actor-header"],
)
def test_a_submitting_config_that_cannot_be_rewritten_is_refused(tmp_path, text, reason):
    submitter = _submitting_dir(tmp_path / "submitter", text)
    child = tmp_path / "child"
    child.mkdir()

    with pytest.raises(ChildActorError, match=reason):
        mint_child_actor_config(str(submitter), str(child), ":worker")
    assert not (child / ".khive").exists()


def test_a_rewrite_that_drops_a_table_fails_verification_and_writes_nothing(trees, monkeypatch):
    submitter, child = trees
    monkeypatch.setattr(_child_actor, "_ANY_HEADER", re.compile(r"(?!)"))

    with pytest.raises(ChildActorError, match="does not verify"):
        mint_child_actor_config(str(submitter), str(child), ":worker")
    assert not (child / ".khive").exists()


# --- submit -----------------------------------------------------------------


class _FakeProc:
    pid = 4242


@pytest.fixture
def submit_sandbox(monkeypatch, tmp_path):
    """A submitting directory holding a config is this process's cwd; job state lives under tmp."""
    submitter = _submitting_dir(tmp_path / "submitter")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "li_command", lambda: ["echo"])
    monkeypatch.setattr(jobs, "_read_lifecycle", lambda run_id: None)
    monkeypatch.chdir(submitter)
    spawned: list[dict[str, Any]] = []
    real_popen = jobs.subprocess.Popen

    def fake_popen(argv, **kw):
        # jobs.subprocess is the subprocess module, so this double sees every
        # spawn in the process; only the run's own argv is the child here.
        if argv[0] != "echo":
            return real_popen(argv, **kw)
        spawned.append(
            {"cwd": kw.get("cwd"), "minted": (child / ".khive" / "config.toml").is_file()}
        )
        return _FakeProc()

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    return submitter, child, spawned


def _run_dirs(tmp_path: Path) -> list[Path]:
    jobs_dir = tmp_path / "jobs"
    return sorted(jobs_dir.iterdir()) if jobs_dir.exists() else []


def test_a_write_capable_submit_mints_before_the_spawn_and_records_it(submit_sandbox):
    _submitter, child, spawned = submit_sandbox

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, ":worker")
        handle = jobs.submit(
            "agent", [], prompt="work", cwd=str(child), no_mcp_config=True, write_capable=True
        )

    assert spawned == [{"cwd": str(child), "minted": True}]
    expected = (
        "lambda:x:worker",
        str(child / ".khive" / "config.toml"),
        "lambda:x",
    )
    assert (
        handle["child_actor"],
        handle["child_actor_config"],
        handle["submitting_actor"],
    ) == expected
    record = jobs._read_job(handle["run_id"])
    assert (
        record["child_actor"],
        record["child_actor_config"],
        record["submitting_actor"],
    ) == expected
    status = jobs.status(handle["run_id"])
    assert (
        status["child_actor"],
        status["child_actor_config"],
        status["submitting_actor"],
    ) == expected


def test_a_write_capable_submit_without_a_cwd_is_refused_before_any_record(
    submit_sandbox, tmp_path
):
    _submitter, _child, spawned = submit_sandbox

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, ":worker")
        with pytest.raises(jobs.ChildActorError, match="needs its own tree: pass cwd"):
            jobs.submit("agent", [], prompt="work", no_mcp_config=True, write_capable=True)

    assert spawned == []
    assert _run_dirs(tmp_path) == []


def test_a_write_capable_submit_from_a_directory_without_a_submitting_actor_is_refused(
    submit_sandbox, tmp_path, monkeypatch
):
    _submitter, child, spawned = submit_sandbox
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)

    monkeypatch.setenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, ":worker")
    with pytest.raises(jobs.ChildActorError, match="no submitting actor exists"):
        jobs.submit(
            "agent", [], prompt="work", cwd=str(child), no_mcp_config=True, write_capable=True
        )

    assert spawned == []
    assert _run_dirs(tmp_path) == []
    assert not (child / ".khive").exists()


def test_an_ordinary_submit_mints_nothing(submit_sandbox):
    _submitter, child, spawned = submit_sandbox

    handle = jobs.submit("agent", [], prompt="work", cwd=str(child), no_mcp_config=True)

    assert spawned == [{"cwd": str(child), "minted": False}]
    assert not (child / ".khive").exists()
    assert (handle["child_actor"], handle["child_actor_config"], handle["submitting_actor"]) == (
        None,
        None,
        None,
    )
    record = jobs._read_job(handle["run_id"])
    assert (record["child_actor"], record["child_actor_config"], record["submitting_actor"]) == (
        None,
        None,
        None,
    )
    status = jobs.status(handle["run_id"])
    assert (status["child_actor"], status["child_actor_config"], status["submitting_actor"]) == (
        None,
        None,
        None,
    )


def test_a_declared_sender_is_judged_against_the_minted_child_actor(
    submit_sandbox, tmp_path, monkeypatch
):
    _submitter, child, spawned = submit_sandbox
    monkeypatch.setenv(config.SENDER_ROOTS_ENV_VAR, str(tmp_path))
    monkeypatch.setenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, ":worker")
    asked: list[str] = []

    def resolve(cwd: str) -> str:
        asked.append(cwd)
        return _toml.loads(Path(cwd, ".khive", "config.toml").read_text())["actor"]["id"]

    monkeypatch.setattr(jobs, "_resolve_submitter_identity", resolve)

    handle = jobs.submit(
        "agent",
        [],
        prompt="work",
        cwd=str(child),
        no_mcp_config=True,
        notify_sender="lambda:x:worker",
        write_capable=True,
    )

    assert asked == [os.path.abspath(child)]
    assert len(spawned) == 1
    assert jobs._read_job(handle["run_id"])["notify_sender"] == "lambda:x:worker"

    with pytest.raises(jobs.NotifySenderMismatchError) as caught:
        jobs.submit(
            "agent",
            [],
            prompt="work",
            cwd=str(child),
            no_mcp_config=True,
            notify_sender="lambda:x",
            write_capable=True,
        )
    assert "'lambda:x:worker'" in str(caught.value)
    assert len(spawned) == 1


# --- dispatch ---------------------------------------------------------------


def _call(**kwargs):
    return asyncio.run(dispatch.request(**kwargs))


def _spawn_op(op: str, args: dict) -> dict:
    return {"op": op, "args": args, "schema_fingerprint": _call(help=op)["schema_fingerprint"]}


@pytest.fixture
def profiles(monkeypatch, submit_sandbox, tmp_path):
    """One write-capable and one read-shaped profile, declared globally under a redirected HOME."""
    home = tmp_path / "home"
    agents = home / ".lionagi" / "agents"
    agents.mkdir(parents=True)
    (agents / "writer.md").write_text("---\nmodel: claude_code/sonnet\nyolo: true\n---\nWrite.\n")
    (agents / "reviewer.md").write_text("---\nmodel: claude_code/sonnet\n---\nReview.\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return submit_sandbox


def test_a_write_capable_profile_without_a_cwd_is_invalid_input(profiles, monkeypatch):
    _submitter, _child, spawned = profiles
    monkeypatch.setenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, ":worker")

    answer = _call(
        ops=[_spawn_op("agent.submit", {"agent": "writer", "prompt": "w", "no_mcp_config": True})]
    )["ops"][0]

    assert answer["ok"] is False, answer
    assert answer["error"]["kind"] == "invalid_input", answer
    assert "pass cwd" in answer["error"]["message"]
    assert spawned == []


def test_a_write_capable_profile_without_a_cwd_spawns_when_minting_is_off(profiles, monkeypatch):
    """The unchanged default accepts the write-capable submit main accepted."""
    _submitter, _child, spawned = profiles
    monkeypatch.delenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, raising=False)

    answer = _call(
        ops=[_spawn_op("agent.submit", {"agent": "writer", "prompt": "w", "no_mcp_config": True})]
    )["ops"][0]

    assert answer["ok"] is True, answer
    assert spawned == [{"cwd": None, "minted": False}]
    assert answer["result"]["child_actor"] is None


def test_a_write_capable_profile_with_a_cwd_is_minted(profiles):
    _submitter, child, spawned = profiles

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, ":worker")
        answer = _call(
            ops=[
                _spawn_op(
                    "agent.submit",
                    {"agent": "writer", "prompt": "w", "cwd": str(child), "no_mcp_config": True},
                )
            ]
        )["ops"][0]

    assert answer["ok"] is True, answer
    assert spawned == [{"cwd": str(child.resolve()), "minted": True}]
    assert _parsed(child / ".khive" / "config.toml")["actor"]["id"] == "lambda:x:worker"
    assert jobs._read_job(answer["result"]["run_id"])["child_actor"] == "lambda:x:worker"


def test_a_read_shaped_profile_is_not_minted(profiles):
    _submitter, child, spawned = profiles

    answer = _call(
        ops=[
            _spawn_op(
                "agent.submit",
                {"agent": "reviewer", "prompt": "r", "cwd": str(child), "no_mcp_config": True},
            )
        ]
    )["ops"][0]

    assert answer["ok"] is True, answer
    assert spawned == [{"cwd": str(child.resolve()), "minted": False}]
    assert not (child / ".khive").exists()
    assert jobs._read_job(answer["result"]["run_id"])["child_actor"] is None


def test_minting_is_off_unless_the_server_names_a_child_suffix(
    submit_sandbox, tmp_path, monkeypatch
):
    """An unset setting leaves a write-capable submit exactly as it was."""
    _submitter, child, spawned = submit_sandbox
    monkeypatch.delenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, raising=False)
    monkeypatch.setattr(jobs, "_submit_cwd", lambda: None)

    handle = jobs.submit("agent", [], prompt="work", no_mcp_config=True, write_capable=True)

    assert spawned == [{"cwd": None, "minted": False}]
    assert not (child / ".khive").exists()
    assert (handle["child_actor"], handle["child_actor_config"], handle["submitting_actor"]) == (
        None,
        None,
        None,
    )


def test_the_configured_suffix_is_the_one_minted(submit_sandbox, monkeypatch):
    _submitter, child, spawned = submit_sandbox
    monkeypatch.setenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, ":worker")

    handle = jobs.submit(
        "agent", [], prompt="work", cwd=str(child), no_mcp_config=True, write_capable=True
    )

    assert spawned == [{"cwd": str(child), "minted": True}]
    assert _parsed(child / ".khive" / "config.toml")["actor"]["id"] == "lambda:x:worker"
    assert handle["child_actor"] == "lambda:x:worker"
