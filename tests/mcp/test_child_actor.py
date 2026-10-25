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
import threading
from pathlib import Path
from typing import Any

import pytest

from lionhub.serve.mcp import _child_actor, config, dispatch, jobs
from lionhub.serve.mcp._child_actor import (
    ChildActorError,
    SubmittingActorError,
    child_actor_for,
    mint_child_actor_config,
)

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
id = "agent:x"
display_name = "agent:x"
visible_namespaces = ["agent:x:a", "agent:x:b"]

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
    assert minted.actor == "agent:x:worker"
    assert minted.submitting_actor == "agent:x"
    assert minted.config_path == str(target)
    assert minted.source_path == str(submitter / ".khive" / "config.toml")
    assert minted.reused is False

    source = _parsed(submitter / ".khive" / "config.toml")
    result = _parsed(target)
    assert result.pop("actor") == {
        "id": "agent:x:worker",
        "display_name": "agent:x:worker",
        "visible_namespaces": ["agent:x"],
    }
    source.pop("actor")
    assert result == source

    first = target.read_text().split("\n", 1)[0]
    assert re.fullmatch(
        r"# CHILD ACTOR CONFIG — agent:x:worker, minted by lionagi at spawn from "
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
    child = _submitting_dir(tmp_path / "child", '[actor]\nid = "agent:someone-else"\n')

    with pytest.raises(ChildActorError, match="refusing to overwrite"):
        mint_child_actor_config(str(submitter), str(child), ":worker")

    assert _parsed(child / ".khive" / "config.toml")["actor"]["id"] == "agent:someone-else"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_a_fifo_where_the_child_config_would_be_is_refused_without_blocking(trees):
    submitter, child = trees
    (child / ".khive").mkdir()
    fifo = child / ".khive" / "config.toml"
    os.mkfifo(fifo)
    outcome: list[BaseException | None] = []

    def mint() -> None:
        try:
            mint_child_actor_config(str(submitter), str(child), ":worker")
        except BaseException as exc:
            outcome.append(exc)
        else:
            outcome.append(None)

    worker = threading.Thread(target=mint, daemon=True)
    worker.start()
    worker.join(timeout=5)
    if worker.is_alive():
        # Release the reader blocked on the FIFO so the thread can end.
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        worker.join(timeout=5)
        pytest.fail("the mint blocked opening a FIFO at the child config path")
    assert isinstance(outcome[0], ChildActorError)
    assert "is not a regular file" in str(outcome[0])


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_a_submitting_config_swapped_for_a_fifo_before_it_is_read_is_refused_without_blocking(
    trees, monkeypatch
):
    submitter, child = trees
    source = submitter / ".khive" / "config.toml"
    real_isfile = os.path.isfile

    def checked_then_swapped(path):
        found = real_isfile(path)
        if os.fspath(path) == os.fspath(source):
            # another process, between the check that it is a file and the read
            source.unlink()
            os.mkfifo(source)
        return found

    monkeypatch.setattr(_child_actor.os.path, "isfile", checked_then_swapped)
    outcome: list[BaseException | None] = []

    def mint() -> None:
        try:
            mint_child_actor_config(str(submitter), str(child), ":worker")
        except BaseException as exc:
            outcome.append(exc)
        else:
            outcome.append(None)

    worker = threading.Thread(target=mint, daemon=True)
    worker.start()
    worker.join(timeout=5)
    if worker.is_alive():
        os.close(os.open(source, os.O_WRONLY | os.O_NONBLOCK))
        worker.join(timeout=5)
        pytest.fail("the mint blocked opening a FIFO at the submitting config path")
    assert isinstance(outcome[0], SubmittingActorError)
    assert "is not a regular file" in str(outcome[0])
    assert not (child / ".khive").exists()


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
        tmp_path / "submitter", SUBMITTER_CONFIG.replace('id = "agent:x"', 'id = "agent:x:child"')
    )
    child = tmp_path / "child"
    child.mkdir()

    with pytest.raises(ChildActorError, match="already a child actor"):
        mint_child_actor_config(str(submitter), str(child), ":child")


@pytest.mark.parametrize("submitter_actor", ["", "  ", "local", "agent:x:child"])
def test_a_child_actor_is_derived_only_from_a_named_submitting_actor(submitter_actor):
    with pytest.raises(ChildActorError):
        child_actor_for(submitter_actor, ":child")


def test_child_actor_for_appends_the_suffix():
    assert child_actor_for("agent:x", ":child") == "agent:x:child"


@pytest.mark.parametrize(
    "text, reason",
    [
        (SUBMITTER_CONFIG.replace('[actor]\nid = "agent:x"\n', ""), "names no \\[actor\\].id"),
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

    with pytest.raises(SubmittingActorError, match="does not verify"):
        mint_child_actor_config(str(submitter), str(child), ":worker")
    assert not (child / ".khive").exists()


@pytest.mark.parametrize(
    "text",
    [
        '[actor]\nid = "agent:x"\n\n[actor.meta]\nrole = "lead"\n',
        '[actor]\nid = "agent:x"\nnotes = """\n[x]\n"""\n',
    ],
    ids=["actor-subtable", "header-inside-a-multiline-string"],
)
def test_a_submitting_config_the_rewrite_cannot_verify_is_the_submitter_s_fault(tmp_path, text):
    """The minted text comes only from the submitting config and the server's suffix."""
    submitter = _submitting_dir(tmp_path / "submitter", text)
    child = tmp_path / "child"
    child.mkdir()

    with pytest.raises(SubmittingActorError):
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
        "agent:x:worker",
        str(child / ".khive" / "config.toml"),
        "agent:x",
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
        notify_sender="agent:x:worker",
        write_capable=True,
    )

    assert asked == [os.path.abspath(child)]
    assert len(spawned) == 1
    assert jobs._read_job(handle["run_id"])["notify_sender"] == "agent:x:worker"

    with pytest.raises(jobs.NotifySenderMismatchError) as caught:
        jobs.submit(
            "agent",
            [],
            prompt="work",
            cwd=str(child),
            no_mcp_config=True,
            notify_sender="agent:x",
            write_capable=True,
        )
    assert "'agent:x:worker'" in str(caught.value)
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


def test_a_profile_name_the_resolver_refuses_is_invalid_input(profiles):
    _submitter, child, spawned = profiles
    args = {"agent": "a/b/c", "prompt": "w", "cwd": str(child), "no_mcp_config": True}

    answer = _call(ops=[_spawn_op("agent.submit", args)])["ops"][0]

    assert answer["ok"] is False, answer
    assert answer["error"]["kind"] == "invalid_input", answer
    assert "agent profile name" in answer["error"]["message"]
    assert spawned == []


def test_a_server_without_a_submitting_actor_is_unavailable_not_the_caller_s_input(
    profiles, monkeypatch
):
    """The caller cannot correct the server's own directory by changing the submission."""
    submitter, child, spawned = profiles
    (submitter / ".khive" / "config.toml").unlink()
    monkeypatch.setenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, ":worker")
    args = {"agent": "writer", "prompt": "w", "cwd": str(child), "no_mcp_config": True}

    answer = _call(ops=[_spawn_op("agent.submit", args)])["ops"][0]

    assert answer["ok"] is False, answer
    assert answer["error"]["kind"] == "unavailable", answer
    assert "holds no .khive/config.toml" in answer["error"]["message"]
    assert spawned == []
    assert not (child / ".khive").exists()


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
    assert _parsed(child / ".khive" / "config.toml")["actor"]["id"] == "agent:x:worker"
    assert jobs._read_job(answer["result"]["run_id"])["child_actor"] == "agent:x:worker"


def test_a_read_shaped_profile_is_not_minted(profiles, monkeypatch):
    _submitter, child, spawned = profiles
    monkeypatch.setenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, ":worker")

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


@pytest.mark.parametrize("flag", ["yolo", "bypass"])
def test_a_write_flag_passed_with_a_read_shaped_profile_is_minted(profiles, monkeypatch, flag):
    _submitter, child, spawned = profiles
    monkeypatch.setenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, ":worker")
    args = {
        "agent": "reviewer",
        "prompt": "r",
        "cwd": str(child),
        "no_mcp_config": True,
        flag: True,
    }

    answer = _call(ops=[_spawn_op("agent.submit", args)])["ops"][0]

    assert answer["ok"] is True, answer
    assert spawned == [{"cwd": str(child.resolve()), "minted": True}]
    assert _parsed(child / ".khive" / "config.toml")["actor"]["id"] == "agent:x:worker"
    assert jobs._read_job(answer["result"]["run_id"])["child_actor"] == "agent:x:worker"


@pytest.mark.parametrize("flag", ["yolo", "bypass"])
def test_a_write_flag_passed_without_a_cwd_is_invalid_input(profiles, monkeypatch, flag):
    _submitter, _child, spawned = profiles
    monkeypatch.setenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, ":worker")
    args = {"query": ["claude_code/sonnet"], "prompt": "w", "no_mcp_config": True, flag: True}

    answer = _call(ops=[_spawn_op("agent.submit", args)])["ops"][0]

    assert answer["ok"] is False, answer
    assert answer["error"]["kind"] == "invalid_input", answer
    assert "pass cwd" in answer["error"]["message"]
    assert spawned == []


@pytest.mark.parametrize("resumes", [{"resume": "branch-1"}, {"continue_last": True}])
def test_a_resumed_run_is_minted_since_it_reloads_its_branch_s_write_flags(
    profiles, monkeypatch, resumes
):
    _submitter, child, spawned = profiles
    monkeypatch.setenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, ":worker")
    args = {"prompt": "go", "cwd": str(child), "no_mcp_config": True, **resumes}

    answer = _call(ops=[_spawn_op("agent.submit", args)])["ops"][0]

    assert answer["ok"] is True, answer
    assert spawned == [{"cwd": str(child.resolve()), "minted": True}]
    assert _parsed(child / ".khive" / "config.toml")["actor"]["id"] == "agent:x:worker"


def test_a_submitting_config_the_rewrite_cannot_verify_is_unavailable(profiles, monkeypatch):
    submitter, child, spawned = profiles
    (submitter / ".khive" / "config.toml").write_text(
        '[actor]\nid = "agent:x"\n\n[actor.meta]\nrole = "lead"\n'
    )
    monkeypatch.setenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, ":worker")
    args = {"agent": "writer", "prompt": "w", "cwd": str(child), "no_mcp_config": True}

    answer = _call(ops=[_spawn_op("agent.submit", args)])["ops"][0]

    assert answer["ok"] is False, answer
    assert answer["error"]["kind"] == "unavailable", answer
    assert spawned == []
    assert not (child / ".khive").exists()


@pytest.mark.parametrize("op", ["flow.submit", "fanout.submit"])
def test_an_orchestration_run_is_minted_whatever_its_profile(profiles, monkeypatch, op):
    """Its workers are cast onto profiles while it runs, so the submit cannot bound their writes."""
    _submitter, child, spawned = profiles
    monkeypatch.setenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, ":worker")
    args = {"agent": "reviewer", "prompt": "r", "cwd": str(child), "no_mcp_config": True}

    answer = _call(ops=[_spawn_op(op, args)])["ops"][0]

    assert answer["ok"] is True, answer
    assert spawned == [{"cwd": str(child.resolve()), "minted": True}]
    assert _parsed(child / ".khive" / "config.toml")["actor"]["id"] == "agent:x:worker"


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
    assert _parsed(child / ".khive" / "config.toml")["actor"]["id"] == "agent:x:worker"
    assert handle["child_actor"] == "agent:x:worker"


# --- reuse, suffix, refusal ---------------------------------------------------


@pytest.mark.parametrize("change", ["actor-table", "other-tables"])
def test_a_child_config_with_the_child_id_that_is_not_the_minted_one_is_refused_and_kept(
    trees, change
):
    submitter, child = trees
    mint_child_actor_config(str(submitter), str(child), ":worker")
    target = child / ".khive" / "config.toml"
    if change == "actor-table":
        changed = target.read_text().replace(
            'visible_namespaces = ["agent:x"]', 'visible_namespaces = ["agent:x", "agent:other"]'
        )
    else:
        changed = target.read_text() + '\n[extra]\nkey = "value"\n'
    assert changed != target.read_text()
    target.write_text(changed)
    before = target.read_bytes()

    with pytest.raises(ChildActorError, match="refusing to overwrite it, remove it"):
        mint_child_actor_config(str(submitter), str(child), ":worker")

    assert target.read_bytes() == before


@pytest.mark.parametrize("suffix", [":child\n# line", "   ", ":a b", ":a\tb", ":a​b", ":a\x00b"])
def test_a_suffix_holding_whitespace_or_a_control_character_is_refused(suffix):
    with pytest.raises(ChildActorError, match="whitespace or a control character"):
        child_actor_for("agent:x", suffix)


def test_a_submitting_actor_holding_whitespace_is_refused():
    with pytest.raises(ChildActorError, match="whitespace or a control character"):
        child_actor_for("agent:x y", ":worker")


def test_a_relative_sender_root_refuses_only_a_submit_that_declares_a_sender(
    submit_sandbox, tmp_path, monkeypatch
):
    _submitter, child, spawned = submit_sandbox
    monkeypatch.setenv(config.SENDER_ROOTS_ENV_VAR, "signers")

    jobs.submit("agent", [], prompt="work", cwd=str(child), no_mcp_config=True)
    assert len(spawned) == 1

    with pytest.raises(jobs.NotifySenderResolutionError, match="'signers'"):
        jobs.submit(
            "agent", [], prompt="work", cwd=str(child), no_mcp_config=True, notify_sender="agent:x"
        )
    assert len(spawned) == 1
    assert len(_run_dirs(tmp_path)) == 1


def test_a_relative_sender_root_is_reported_unavailable_not_internal(profiles, monkeypatch):
    _submitter, child, spawned = profiles
    monkeypatch.setenv(config.SENDER_ROOTS_ENV_VAR, "signers")
    args = {"agent": "reviewer", "prompt": "r", "cwd": str(child), "no_mcp_config": True}

    without = _call(ops=[_spawn_op("agent.submit", args)])["ops"][0]
    declared = _call(ops=[_spawn_op("agent.submit", {**args, "notify_sender": "agent:x"})])["ops"][
        0
    ]

    assert without["ok"] is True, without
    assert declared["ok"] is False, declared
    assert declared["error"]["kind"] == "unavailable", declared
    assert len(spawned) == 1


def test_a_refused_submit_takes_back_the_config_it_minted(submit_sandbox, tmp_path, monkeypatch):
    _submitter, child, spawned = submit_sandbox
    monkeypatch.setenv(config.SENDER_ROOTS_ENV_VAR, str(tmp_path / "allowed"))
    monkeypatch.setenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, ":worker")

    with pytest.raises(jobs.NotifySenderResolutionError, match="outside the roots"):
        jobs.submit(
            "agent",
            [],
            prompt="work",
            cwd=str(child),
            no_mcp_config=True,
            notify_sender="agent:x:worker",
            write_capable=True,
        )

    assert spawned == []
    assert _run_dirs(tmp_path) == []
    assert not (child / ".khive").exists()


def test_a_refused_submit_leaves_a_config_it_did_not_create(submit_sandbox, tmp_path, monkeypatch):
    submitter, child, spawned = submit_sandbox
    mint_child_actor_config(str(submitter), str(child), ":worker")
    target = child / ".khive" / "config.toml"
    before = target.read_bytes()
    monkeypatch.setenv(config.SENDER_ROOTS_ENV_VAR, str(tmp_path / "allowed"))
    monkeypatch.setenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, ":worker")

    with pytest.raises(jobs.NotifySenderResolutionError, match="outside the roots"):
        jobs.submit(
            "agent",
            [],
            prompt="work",
            cwd=str(child),
            no_mcp_config=True,
            notify_sender="agent:x:worker",
            write_capable=True,
        )

    assert spawned == []
    assert target.read_bytes() == before


def test_a_submit_into_a_child_config_it_would_not_mint_is_refused_and_leaves_it(
    submit_sandbox, tmp_path, monkeypatch
):
    submitter, child, spawned = submit_sandbox
    mint_child_actor_config(str(submitter), str(child), ":worker")
    target = child / ".khive" / "config.toml"
    target.write_text(target.read_text() + '\n[extra]\nkey = "value"\n')
    before = target.read_bytes()
    monkeypatch.setenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, ":worker")
    monkeypatch.setattr("lionagi.cli._mcp_resolve.discover_mcp_config", lambda start: None)

    with pytest.raises(ChildActorError, match="refusing to overwrite"):
        jobs.submit(
            "agent",
            [],
            prompt="work",
            cwd=str(child),
            exclude_mcp_servers=["ghost"],
            write_capable=True,
        )

    assert spawned == []
    assert _run_dirs(tmp_path) == []
    assert target.read_bytes() == before


def test_a_submit_refused_after_its_reservation_takes_back_the_config_it_minted(
    submit_sandbox, tmp_path, monkeypatch
):
    _submitter, child, spawned = submit_sandbox
    monkeypatch.setenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, ":worker")
    monkeypatch.setattr("lionagi.cli._mcp_resolve.discover_mcp_config", lambda start: None)

    with pytest.raises(jobs.SubmitInputError, match="not declared"):
        jobs.submit(
            "agent",
            [],
            prompt="work",
            cwd=str(child),
            exclude_mcp_servers=["ghost"],
            write_capable=True,
        )

    assert spawned == []
    assert _run_dirs(tmp_path) == []
    assert not (child / ".khive").exists()
