# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `li agent --context-from <ref>` (spec v0.2).

Covers: ref resolution order (session/branch/run/file id, unique prefix,
miss), ambiguous-prefix hard error, total-not-per-ref budget with argv-order
allocation and loud tail-ref truncation, the distillation ladder (artifact /
final-message fallback / truncation marker), rejection when combined with
-r/-c, manifest recording of context_from, and injection above the user
prompt in the new branch's first instruction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lionagi.cli._context_from import (
    _CHARS_PER_TOKEN,
    DEFAULT_CONTEXT_BUDGET_TOKENS,
    AmbiguousContextRefError,
    ContextCandidate,
    ContextFromError,
    _distill,
    _resolve_file_ref,
    build_context_block,
    resolve_context_refs,
)
from lionagi.cli._runs import RunDir
from lionagi.state.db import StateDB

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test temp DB; mirrors tests/cli/test_status.py's fixture."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(
        "lionagi.state.db.settings",
        SimpleNamespace(LIONAGI_STATE_DB_URL=None),
    )
    return db_path


@pytest.fixture
def runs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the filesystem branch/run scan to an isolated tmp dir."""
    root = tmp_path / "runs"
    root.mkdir()
    monkeypatch.setattr("lionagi.cli._context_from.RUNS_ROOT", root)
    return root


async def _make_session(db: StateDB, **fields) -> str:
    sid = fields.pop("id", None) or uuid.uuid4().hex
    pid = uuid.uuid4().hex
    await db.create_progression(pid)
    await db.create_session(
        {
            "id": sid,
            "progression_id": pid,
            "status": fields.pop("status", "completed"),
            "invocation_kind": "agent",
            "started_at": time.time(),
            **fields,
        }
    )
    return sid


async def _make_branch(
    db: StateDB, session_id: str, *, branch_id: str | None = None, **fields
) -> tuple[str, str]:
    bid = branch_id or str(uuid.uuid4())
    pid = uuid.uuid4().hex
    await db.create_progression(pid)
    await db.create_branch(
        {
            "id": bid,
            "session_id": session_id,
            "progression_id": pid,
            "model": fields.pop("model", "sonnet"),
            "provider": fields.pop("provider", "anthropic"),
        }
    )
    return bid, pid


async def _add_message(db: StateDB, progression_id: str, *, role: str, content: dict) -> str:
    mid = uuid.uuid4().hex
    await db.insert_message(
        {"id": mid, "created_at": time.time(), "content": content, "role": role}
    )
    await db.append_to_progression(progression_id, mid)
    return mid


def _write_branch_file(runs_root: Path, run_id: str, branch_id: str) -> Path:
    branches_dir = runs_root / run_id / "branches"
    branches_dir.mkdir(parents=True, exist_ok=True)
    path = branches_dir / f"{branch_id}.json"
    path.write_text("{}")
    return path


# ── Ladder: artifact present / final-message fallback / truncation marker ──


def test_ladder_uses_verbatim_artifact_when_it_fits():
    cand = ContextCandidate(
        kind="branch",
        ref="r1",
        model="anthropic/sonnet",
        step1_text="the saved artifact",
        step2_text="x" * 500,
    )
    text, truncated = _distill(cand, budget_chars=1000)
    assert text == "the saved artifact"
    assert truncated is False


def test_ladder_falls_back_to_final_message_when_no_artifact():
    # spec ladder rung 2: final assistant message FIRST, then the initial instruction.
    cand = ContextCandidate(
        kind="branch",
        ref="r1",
        model=None,
        step1_text=None,
        step2_text="final\n\ninitial",
        step3_head="initial",
        step3_tail="final",
    )
    text, truncated = _distill(cand, budget_chars=1000)
    assert text == "final\n\ninitial"
    assert truncated is False


def test_ladder_truncates_loudly_when_both_oversized():
    cand = ContextCandidate(
        kind="branch",
        ref="r1",
        model=None,
        step1_text="a" * 5000,
        step2_text="b" * 5000,
        step3_head="HEAD" * 500,
        step3_tail="TAIL" * 500,
    )
    text, truncated = _distill(cand, budget_chars=100)
    assert truncated is True
    assert "[...truncated...]" in text
    assert len(text) <= 100


# ── Budget: total-not-per-ref, argv order, tail-ref loud truncation ────────


def test_build_context_block_allocates_total_budget_in_argv_order(caplog):
    first = ContextCandidate(kind="branch", ref="first", model="m", step2_text="F" * 60)
    second = ContextCandidate(kind="branch", ref="second", model="m", step2_text="S" * 60)

    logger = logging.getLogger("lionagi.cli.warn")
    logger.handlers.clear()
    logger.propagate = True

    budget_tokens = 60  # 240 chars total, enough for the first ref's wrapper + payload
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        block = build_context_block([first, second], budget_tokens=budget_tokens)

    # first ref gets its natural fit (60 chars verbatim final-message text)
    assert "F" * 60 in block
    # second ref (tail) only had budget left for wrapper + a loud marker
    assert 'ref="second"' in block
    assert "[...truncated...]" in block
    assert any("second" in rec.message for rec in caplog.records)
    # the COMBINED block (delimiters + separators included) respects the total budget,
    # not just the distilled payload text
    assert len(block) <= budget_tokens * _CHARS_PER_TOKEN


def test_build_context_block_total_budget_bounds_combined_wrapped_block(caplog):
    """Regression: budget must bound the wrapped block (tags + separators), not payload only.

    Two refs whose raw payloads alone (120 chars) fit comfortably under a naive
    payload-only accounting of the budget, but the XML wrapper + separator overhead
    means the true combined block must still respect the total budget.
    """
    first = ContextCandidate(kind="branch", ref="first", model="m", step2_text="F" * 60)
    second = ContextCandidate(kind="branch", ref="second", model="m", step2_text="S" * 60)

    logger = logging.getLogger("lionagi.cli.warn")
    logger.handlers.clear()
    logger.propagate = True

    budget_tokens = 20  # 80 chars total -- tight enough that wrapper overhead dominates
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        block = build_context_block([first, second], budget_tokens=budget_tokens)

    # neither payload leaks through unbounded -- both refs are loudly truncated
    assert "F" * 60 not in block
    assert "S" * 60 not in block
    assert block.count("[...truncated...]") == 2
    # the combined block is far smaller than the un-bounded 236-char blowup the
    # payload-only accounting produced (two 60-char payloads + wrapper tags)
    assert len(block) < 200


def test_file_ref_over_budget_truncates_loudly_no_verbatim_blowup(tmp_path):
    big_file = tmp_path / "notes.md"
    big_file.write_text("X" * 10_000)
    cand = _resolve_file_ref(str(big_file))
    assert cand is not None
    assert cand.kind == "file"

    block = build_context_block([cand], budget_tokens=10)  # 40 chars
    assert len(block) < 10_000
    assert "[...truncated...]" in block


# ── Ref resolution order + unique prefix + miss ─────────────────────────────


@pytest.mark.asyncio
async def test_resolve_session_id_ref(temp_db_path):
    async with StateDB() as db:
        sid = await _make_session(db)
        bid, pid = await _make_branch(db, sid)
        await _add_message(db, pid, role="user", content={"instruction": "do the task"})
        await _add_message(
            db, pid, role="assistant", content={"assistant_response": "done, verdict: ok"}
        )

        [cand] = await resolve_context_refs([sid])
    assert cand.kind == "session"
    assert cand.step3_tail == "done, verdict: ok"
    assert cand.model == "anthropic/sonnet"
    # ladder rung 2: final assistant message FIRST, then the initial instruction
    assert cand.step2_text == "done, verdict: ok\n\ndo the task"


@pytest.mark.asyncio
async def test_resolve_session_id_unique_prefix(temp_db_path):
    async with StateDB() as db:
        sid = await _make_session(db, id=uuid.uuid4().hex)
        bid, pid = await _make_branch(db, sid)
        await _add_message(db, pid, role="assistant", content={"assistant_response": "result"})

        [cand] = await resolve_context_refs([sid[:8]])
    assert cand.kind == "session"


@pytest.mark.asyncio
async def test_resolve_branch_id_ref(temp_db_path, runs_root):
    async with StateDB() as db:
        sid = await _make_session(db)
        bid, pid = await _make_branch(db, sid)
        await _add_message(
            db, pid, role="assistant", content={"assistant_response": "final answer"}
        )
        _write_branch_file(runs_root, "run-1", bid)

        [cand] = await resolve_context_refs([bid])
    assert cand.kind == "branch"
    assert cand.step3_tail == "final answer"


@pytest.mark.asyncio
async def test_resolve_run_id_ref_via_manifest(temp_db_path, runs_root):
    async with StateDB() as db:
        sid = await _make_session(db)
        bid, pid = await _make_branch(db, sid)
        await _add_message(
            db, pid, role="assistant", content={"assistant_response": "run-level answer"}
        )
        _write_branch_file(runs_root, "20260705T000000-abc123", bid)
        (runs_root / "20260705T000000-abc123" / "run.json").write_text(
            json.dumps({"branch_id": bid, "run_id": "20260705T000000-abc123"})
        )

        [cand] = await resolve_context_refs(["20260705T000000-abc123"])
    assert cand.kind == "run"
    assert cand.step3_tail == "run-level answer"


@pytest.mark.asyncio
async def test_resolve_file_path_ref(temp_db_path, runs_root, tmp_path):
    f = tmp_path / "prior.md"
    f.write_text("prior findings verbatim")

    [cand] = await resolve_context_refs([str(f)])
    assert cand.kind == "file"
    assert cand.step1_text == "prior findings verbatim"


@pytest.mark.asyncio
async def test_resolve_unresolvable_ref_raises(temp_db_path, runs_root):
    with pytest.raises(ContextFromError, match="could not resolve"):
        await resolve_context_refs(["totally-unknown-ref-xyz"])


@pytest.mark.asyncio
async def test_resolve_empty_source_branch_errors(temp_db_path, runs_root):
    async with StateDB() as db:
        sid = await _make_session(db)
        bid, pid = await _make_branch(db, sid)
        _write_branch_file(runs_root, "run-x", bid)

        with pytest.raises(ContextFromError, match="no assistant message"):
            await resolve_context_refs([bid])


# ── Ambiguous prefix (2+ matches) → hard error listing candidates ─────────


@pytest.mark.asyncio
async def test_resolve_ambiguous_session_prefix_raises(temp_db_path):
    async with StateDB() as db:
        sid1 = await _make_session(db, id="abc11111" + uuid.uuid4().hex[:10])
        sid2 = await _make_session(db, id="abc22222" + uuid.uuid4().hex[:10])

    with pytest.raises(AmbiguousContextRefError) as excinfo:
        async with StateDB() as db:
            await resolve_context_refs(["abc"])
    assert sid1 in excinfo.value.candidates
    assert sid2 in excinfo.value.candidates
    assert "abc" in str(excinfo.value)


@pytest.mark.asyncio
async def test_resolve_ambiguous_branch_prefix_raises(temp_db_path, runs_root):
    async with StateDB() as db:
        sid = await _make_session(db)
        bid1, _ = await _make_branch(db, sid, branch_id="dup1111-aaaa-bbbb-cccc-000000000001")
        bid2, _ = await _make_branch(db, sid, branch_id="dup1111-aaaa-bbbb-cccc-000000000002")
    _write_branch_file(runs_root, "run-a", bid1)
    _write_branch_file(runs_root, "run-b", bid2)

    with pytest.raises(AmbiguousContextRefError) as excinfo:
        async with StateDB() as db:
            await resolve_context_refs(["dup1111"])
    assert set(excinfo.value.candidates) == {bid1, bid2}


@pytest.mark.asyncio
async def test_resolve_ambiguous_run_prefix_raises(temp_db_path, runs_root):
    (runs_root / "run-dup-1").mkdir()
    (runs_root / "run-dup-2").mkdir()

    with pytest.raises(AmbiguousContextRefError):
        await resolve_context_refs(["run-dup"])


# ── CLI wiring: mutual exclusion, manifest, first-instruction injection ────


def _wire_agent_stubs(
    monkeypatch, tmp_path: Path, operate_return=None, capture: dict | None = None
):
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.service.manager import iModelManager

    async def fake_operate(self, instruction=None, **kw):
        if capture is not None:
            capture["instruction"] = instruction
        return operate_return

    monkeypatch.setattr(Branch, "operate", fake_operate)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)

    async def fake_setup(*a, **kw):
        return None

    async def fake_teardown(ctx, *, status="completed", exception=None, cwd=None):
        return status

    monkeypatch.setattr(agent_mod, "setup_agent_persist", fake_setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", fake_teardown)
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)
    monkeypatch.setattr(
        agent_mod,
        "_provenance",
        SimpleNamespace(
            resolve_model_spec=lambda p, m: f"{p}/{m}",
            agent_definition_hash=lambda n: "abc",
        ),
    )
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: None)


@pytest.mark.asyncio
async def test_context_from_rejected_with_resume(monkeypatch, tmp_path):
    from lionagi.cli.agent import _run_agent

    with pytest.raises(ContextFromError, match="cannot be combined"):
        await _run_agent(
            "claude/sonnet",
            "follow up",
            resume="some-branch-id",
            context_from=["some-ref"],
        )


@pytest.mark.asyncio
async def test_context_from_rejected_with_continue_last(monkeypatch, tmp_path):
    from lionagi.cli.agent import _run_agent

    with pytest.raises(ContextFromError, match="cannot be combined"):
        await _run_agent(
            "claude/sonnet",
            "follow up",
            continue_last=True,
            context_from=["some-ref"],
        )


def test_run_agent_exits_2_on_context_from_plus_resume(monkeypatch, tmp_path):
    import lionagi.cli.agent as agent_mod
    from lionagi.ln.concurrency import run_async as _real_run_async

    _wire_agent_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(agent_mod, "run_async", _real_run_async)

    args = SimpleNamespace(
        query=["claude/sonnet", "follow up"],
        prompt_flag=None,
        prompt_file=None,
        yolo=False,
        verbose=False,
        theme=None,
        resume="some-branch-id",
        continue_last=False,
        effort=None,
        agent=None,
        cwd=None,
        timeout=None,
        fast=False,
        invocation=None,
        project=None,
        bypass=False,
        preset=None,
        resume_on_timeout=False,
        form=None,
        context_from=["some-ref"],
        context_budget=None,
    )

    from lionagi.cli.agent import run_agent

    rc = run_agent(args)
    assert rc == 2


@pytest.mark.asyncio
async def test_manifest_records_context_from(monkeypatch, tmp_path):
    import lionagi.cli.agent as agent_mod

    capture: dict = {}
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="the result", capture=capture)

    run = RunDir(
        run_id="run-ctx",
        state_root=tmp_path / "state",
        artifact_root=tmp_path / "artifacts",
    )
    run.ensure_state_dirs()
    monkeypatch.setattr(agent_mod, "allocate_run", lambda: run)

    async def fake_build(refs, budget_tokens):
        return '<prior-run-context ref="x" kind="file" model="unknown">\nprior text\n</prior-run-context>'

    monkeypatch.setattr(agent_mod, "resolve_and_build_context_block", fake_build)

    from lionagi.cli.agent import _run_agent

    result, provider, branch_id, terminal_status, _sid = await _run_agent(
        "claude/sonnet",
        "the actual prompt",
        context_from=["some.md"],
    )

    assert terminal_status == "completed"
    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["context_from"] == ["some.md"]
    assert manifest["branch_id"] == branch_id


@pytest.mark.asyncio
async def test_injected_block_present_above_prompt_in_first_instruction(monkeypatch, tmp_path):
    import lionagi.cli.agent as agent_mod

    capture: dict = {}
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok", capture=capture)
    monkeypatch.setattr(
        agent_mod,
        "allocate_run",
        lambda: SimpleNamespace(
            run_id="r",
            artifact_root=tmp_path / "artifacts",
            stream_dir=tmp_path / "stream",
            branches_dir=tmp_path / "branches",
            write_manifest=lambda data: None,
        ),
    )

    marker = '<prior-run-context ref="prior" kind="branch" model="anthropic/sonnet">\ndistilled\n</prior-run-context>'

    async def fake_build(refs, budget_tokens):
        return marker

    monkeypatch.setattr(agent_mod, "resolve_and_build_context_block", fake_build)

    from lionagi.cli.agent import _run_agent

    await _run_agent(
        "claude/sonnet",
        "the user prompt",
        context_from=["prior"],
    )

    instruction = capture["instruction"]
    assert marker in instruction
    assert instruction.index(marker) < instruction.index("the user prompt")


# ── Explicit `--context-budget 0` must be preserved, not defaulted ─────────


@pytest.mark.asyncio
async def test_context_budget_zero_passed_through_not_defaulted(monkeypatch, tmp_path):
    """`context_budget=0` must reach `resolve_and_build_context_block` as `0`,
    never silently upgraded to `DEFAULT_CONTEXT_BUDGET_TOKENS` by a truthiness
    fallback (`context_budget or DEFAULT...` treats 0 as falsy)."""
    import lionagi.cli.agent as agent_mod

    capture: dict = {}
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok", capture=capture)
    monkeypatch.setattr(
        agent_mod,
        "allocate_run",
        lambda: SimpleNamespace(
            run_id="r",
            artifact_root=tmp_path / "artifacts",
            stream_dir=tmp_path / "stream",
            branches_dir=tmp_path / "branches",
            write_manifest=lambda data: None,
        ),
    )

    captured_budget: dict = {}

    async def fake_build(refs, budget_tokens):
        captured_budget["value"] = budget_tokens
        return '<prior-run-context ref="x" kind="file" model="unknown">\n[...truncated...]\n</prior-run-context>'

    monkeypatch.setattr(agent_mod, "resolve_and_build_context_block", fake_build)

    from lionagi.cli.agent import _run_agent

    await _run_agent(
        "claude/sonnet",
        "the user prompt",
        context_from=["some-ref"],
        context_budget=0,
    )

    assert captured_budget["value"] == 0


@pytest.mark.asyncio
async def test_context_budget_zero_reaches_build_context_block_only_truncation_marker(
    monkeypatch, temp_db_path, runs_root, tmp_path
):
    """End-to-end: `--context-budget 0` flows through the REAL resolve+build
    pipeline (no mocking of `resolve_and_build_context_block`) and the
    injected block contains only the loud truncation marker -- never the
    verbatim source content."""
    import lionagi.cli.agent as agent_mod

    capture: dict = {}
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok", capture=capture)
    monkeypatch.setattr(
        agent_mod,
        "allocate_run",
        lambda: SimpleNamespace(
            run_id="r",
            artifact_root=tmp_path / "artifacts",
            stream_dir=tmp_path / "stream",
            branches_dir=tmp_path / "branches",
            write_manifest=lambda data: None,
        ),
    )

    source = tmp_path / "prior.md"
    source.write_text("verbatim prior findings that must not leak through at budget 0")

    from lionagi.cli.agent import _run_agent

    await _run_agent(
        "claude/sonnet",
        "the user prompt",
        context_from=[str(source)],
        context_budget=0,
    )

    instruction = capture["instruction"]
    assert "[...truncated...]" in instruction
    assert "verbatim prior findings" not in instruction


def test_build_context_block_budget_zero_yields_only_truncation_marker():
    cand = ContextCandidate(
        kind="file",
        ref="r1",
        model=None,
        step1_text="a saved artifact that would otherwise be used verbatim",
        step2_text="initial\n\nfinal",
        step3_head="initial",
        step3_tail="final",
    )
    block = build_context_block([cand], budget_tokens=0)
    assert "[...truncated...]" in block
    assert "saved artifact" not in block
    assert "initial" not in block
    assert "final" not in block


# ── CLI surface: exit codes, stderr content, precedence, argv ordering ─────


def _base_cli_args(**overrides) -> SimpleNamespace:
    defaults = dict(
        query=["claude/sonnet", "hello"],
        prompt_flag=None,
        prompt_file=None,
        yolo=False,
        verbose=False,
        theme=None,
        resume=None,
        continue_last=False,
        effort=None,
        agent=None,
        cwd=None,
        timeout=None,
        fast=False,
        invocation=None,
        project=None,
        bypass=False,
        preset=None,
        resume_on_timeout=False,
        form=None,
        context_from=None,
        context_budget=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_cli_unresolvable_ref_exits_2_with_stderr(monkeypatch, temp_db_path, runs_root, caplog):
    import lionagi.cli.agent as agent_mod
    from lionagi.ln.concurrency import run_async as _real_run_async

    monkeypatch.setattr(agent_mod, "run_async", _real_run_async)

    logger = logging.getLogger("lionagi.cli.error")
    logger.handlers.clear()
    logger.propagate = True

    args = _base_cli_args(context_from=["totally-unknown-ref-xyz"])

    from lionagi.cli.agent import run_agent

    with caplog.at_level(logging.ERROR, logger="lionagi.cli.error"):
        rc = run_agent(args)

    assert rc == 2
    assert any("could not resolve" in rec.message for rec in caplog.records)


def test_cli_ambiguous_prefix_exits_2_lists_candidates(monkeypatch, temp_db_path, caplog):
    import lionagi.cli.agent as agent_mod
    from lionagi.ln.concurrency import run_async as _real_run_async

    monkeypatch.setattr(agent_mod, "run_async", _real_run_async)

    sid1 = "abc11111" + uuid.uuid4().hex[:10]
    sid2 = "abc22222" + uuid.uuid4().hex[:10]

    async def _seed():
        async with StateDB() as db:
            await _make_session(db, id=sid1)
            await _make_session(db, id=sid2)

    asyncio.run(_seed())

    logger = logging.getLogger("lionagi.cli.error")
    logger.handlers.clear()
    logger.propagate = True

    args = _base_cli_args(context_from=["abc"])

    from lionagi.cli.agent import run_agent

    with caplog.at_level(logging.ERROR, logger="lionagi.cli.error"):
        rc = run_agent(args)

    assert rc == 2
    messages = "\n".join(rec.message for rec in caplog.records)
    assert "ambiguous" in messages
    assert sid1 in messages
    assert sid2 in messages


@pytest.mark.asyncio
async def test_ref_precedence_session_over_file_path(temp_db_path, runs_root, tmp_path):
    """A ref that is simultaneously a valid file path AND a resolvable id must
    resolve via the id ladder (session -> branch -> run) and never fall
    through to the file-path escape hatch."""
    ref = str(tmp_path / "collision")
    Path(ref).write_text("file contents must lose to the session match")

    async with StateDB() as db:
        sid = await _make_session(db, id=ref)
        bid, pid = await _make_branch(db, sid)
        await _add_message(
            db, pid, role="assistant", content={"assistant_response": "session wins"}
        )

        [cand] = await resolve_context_refs([ref])

    assert cand.kind == "session"
    assert cand.step3_tail == "session wins"


@pytest.mark.asyncio
async def test_repeated_identical_refs_preserve_argv_order(temp_db_path, runs_root, tmp_path):
    f = tmp_path / "prior.md"
    f.write_text("same content")

    candidates = await resolve_context_refs([str(f), str(f)])
    assert len(candidates) == 2
    assert candidates[0].ref == candidates[1].ref == str(f)

    block = build_context_block(candidates, budget_tokens=DEFAULT_CONTEXT_BUDGET_TOKENS)
    assert block.count("same content") == 2
    first_idx = block.index("same content")
    second_idx = block.index("same content", first_idx + 1)
    assert first_idx < second_idx
