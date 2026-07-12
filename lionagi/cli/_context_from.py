# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li agent --context-from <ref>`: resolve prior-run refs into a bounded context block.

Ref resolution order: session id (state.db, prefix match) -> branch id
(~/.lionagi/runs/*/branches/*.json, prefix match) -> run id (run.json manifest
prefix match) -> file path. Distillation is mechanical (no LLM): a saved
artifact/summary verbatim, else the final assistant message + initial
instruction, else a loudly-marked head/tail truncation to fit budget. Budget
is shared across the combined injected block (including XML wrapper and
inter-block separators), allocated in argv order.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from lionagi._paths import RUNS_ROOT

DEFAULT_CONTEXT_BUDGET_TOKENS = 8000
_CHARS_PER_TOKEN = 4
_TRUNCATION_MARKER = "\n[...truncated...]\n"


class ContextFromError(ValueError):
    """A `--context-from` ref could not be resolved or composed."""


class AmbiguousContextRefError(ContextFromError):
    """A `--context-from` ref prefix matched 2+ candidates."""

    def __init__(self, ref: str, candidates: list[str]):
        self.ref = ref
        self.candidates = candidates
        listed = ", ".join(candidates)
        super().__init__(f"--context-from {ref!r} is ambiguous: matches {listed}")


@dataclass
class ContextCandidate:
    """One resolved `--context-from` source, ready for distillation."""

    kind: str
    ref: str
    model: str | None
    step1_text: str | None = None  # saved artifact/summary, verbatim if it fits
    step2_text: str | None = None  # final assistant message + initial instruction
    step3_head: str | None = None  # head half for loud truncation
    step3_tail: str | None = None  # tail half for loud truncation


# ── Filesystem resolution (branch id / run id) ──────────────────────────────


def _find_branch_candidates(ref: str) -> list[tuple[str, Path]]:
    """All distinct (branch_id, path) matches for *ref* as an exact id or prefix.

    Mirrors _runs.find_branch's glob scan but collects every distinct branch_id
    across all run dirs (instead of returning the first) so ambiguity can be
    detected; the same branch_id snapshotted into multiple run dirs (resume)
    is one candidate, not many.
    """
    seen: dict[str, Path] = {}
    if RUNS_ROOT.exists():
        for run_dir in sorted(RUNS_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not run_dir.is_dir():
                continue
            branches = run_dir / "branches"
            if not branches.exists():
                continue
            for match in branches.glob(f"{ref}*.json"):
                seen.setdefault(match.stem, match)
    return sorted(seen.items())


def _resolve_branch_ref(ref: str) -> str | None:
    candidates = _find_branch_candidates(ref)
    if not candidates:
        return None
    if len(candidates) > 1:
        raise AmbiguousContextRefError(ref, [bid for bid, _ in candidates])
    return candidates[0][0]


def _find_run_candidates(ref: str) -> list[str]:
    if not RUNS_ROOT.exists():
        return []
    return sorted(p.name for p in RUNS_ROOT.iterdir() if p.is_dir() and p.name.startswith(ref))


def _resolve_run_ref(ref: str) -> str | None:
    candidates = _find_run_candidates(ref)
    if not candidates:
        return None
    if len(candidates) > 1:
        raise AmbiguousContextRefError(ref, candidates)
    return candidates[0]


def _primary_branch_for_run(run_id: str) -> str | None:
    run_dir = RUNS_ROOT / run_id
    manifest_path = run_dir / "run.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError):
            manifest = {}
        branch_id = manifest.get("branch_id")
        if branch_id:
            return branch_id
    branches_dir = run_dir / "branches"
    if not branches_dir.exists():
        return None
    files = sorted(branches_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0].stem if files else None


def _resolve_file_ref(ref: str) -> ContextCandidate | None:
    path = Path(ref).expanduser()
    if not path.is_file():
        return None
    text = path.read_text()
    return ContextCandidate(
        kind="file", ref=ref, model=None, step1_text=text, step3_head=text, step3_tail=text
    )


# ── State-db resolution (session id, and message content for any branch) ────


def _extract_message_text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (ValueError, TypeError):
            return content
    if isinstance(content, dict):
        return str(
            content.get("assistant_response")
            or content.get("instruction")
            or content.get("content")
            or ""
        )
    return str(content or "")


def _load_saved_artifact_text(session_row: dict) -> str | None:
    """ADR-0035 completion contract step 1: a produced artifact/summary, verbatim."""
    contract = session_row.get("artifact_contract_json")
    artifacts_root = session_row.get("artifacts_path")
    if not contract or not artifacts_root:
        return None
    if isinstance(contract, str):
        try:
            contract = json.loads(contract)
        except (ValueError, TypeError):
            return None

    from lionagi.state.artifact_verifier import verify_artifact_contract

    verification = verify_artifact_contract(contract, artifacts_root=artifacts_root)
    if not verification or not verification["produced"]:
        return None
    entry = verification["produced"][0]
    try:
        return (Path(artifacts_root) / entry["path"]).read_text()
    except OSError:
        return None


async def _resolve_session_ref(db, ref: str) -> dict | None:
    if len(ref) >= 36:
        return await db.get_session(ref)
    rows = await db.fetch_all("SELECT * FROM sessions WHERE id LIKE ?", (ref + "%",))
    if not rows:
        return None
    if len(rows) > 1:
        raise AmbiguousContextRefError(ref, [r["id"] for r in rows])
    return rows[0]


async def _primary_branch_for_session(db, session_id: str) -> str | None:
    rows = await db.fetch_all(
        "SELECT id FROM branches WHERE session_id = ? ORDER BY created_at DESC", (session_id,)
    )
    return rows[0]["id"] if rows else None


async def _candidate_from_branch(db, branch_id: str, ref: str, kind: str) -> ContextCandidate:
    branch_row = await db.get_branch(branch_id)
    if branch_row is None:
        raise ContextFromError(
            f"--context-from {ref!r}: branch {branch_id} has no persisted record"
        )
    session_row = await db.get_session(branch_row["session_id"]) or {}

    progression_id = branch_row.get("progression_id")
    msg_ids = await db.get_progression(progression_id) if progression_id else []
    initial_text: str | None = None
    final_text: str | None = None
    for msg_id in msg_ids:
        msg = await db.get_message(msg_id)
        if not msg:
            continue
        text_val = _extract_message_text(msg)
        if not text_val.strip():
            continue
        role = msg.get("role")
        if role == "user" and initial_text is None:
            initial_text = text_val
        elif role == "assistant":
            final_text = text_val

    if final_text is None:
        raise ContextFromError(f"--context-from {ref!r}: source has no assistant message")

    model = None
    if branch_row.get("provider") and branch_row.get("model"):
        model = f"{branch_row['provider']}/{branch_row['model']}"

    step2 = f"{final_text}\n\n{initial_text}" if initial_text else final_text
    return ContextCandidate(
        kind=kind,
        ref=ref,
        model=model,
        step1_text=_load_saved_artifact_text(session_row),
        step2_text=step2,
        step3_head=initial_text or "",
        step3_tail=final_text,
    )


async def _resolve_one(db, ref: str) -> ContextCandidate:
    session_row = await _resolve_session_ref(db, ref)
    if session_row is not None:
        branch_id = await _primary_branch_for_session(db, session_row["id"])
        if branch_id is None:
            raise ContextFromError(f"--context-from {ref!r}: session has no branch")
        return await _candidate_from_branch(db, branch_id, ref, "session")

    branch_id = _resolve_branch_ref(ref)
    if branch_id is not None:
        return await _candidate_from_branch(db, branch_id, ref, "branch")

    run_id = _resolve_run_ref(ref)
    if run_id is not None:
        primary_branch_id = _primary_branch_for_run(run_id)
        if primary_branch_id is None:
            raise ContextFromError(f"--context-from {ref!r}: run {run_id} has no resolvable branch")
        return await _candidate_from_branch(db, primary_branch_id, ref, "run")

    file_candidate = _resolve_file_ref(ref)
    if file_candidate is not None:
        return file_candidate

    raise ContextFromError(
        f"--context-from {ref!r}: could not resolve as a session id, branch id, run id, or file path"
    )


async def resolve_context_refs(refs: Sequence[str]) -> list[ContextCandidate]:
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        return [await _resolve_one(db, ref) for ref in refs]


# ── Distillation ladder + budget allocation ─────────────────────────────────


def _head_tail_truncate(head: str, tail: str, budget_chars: int) -> str:
    if budget_chars <= len(_TRUNCATION_MARKER):
        return _TRUNCATION_MARKER.strip()
    avail = budget_chars - len(_TRUNCATION_MARKER)
    head_budget = avail // 2
    tail_budget = avail - head_budget
    return f"{head[:head_budget]}{_TRUNCATION_MARKER}{tail[-tail_budget:] if tail_budget else ''}"


def _distill(candidate: ContextCandidate, budget_chars: int) -> tuple[str, bool]:
    """First-fit-within-budget ladder; returns (text, truncated)."""
    if candidate.step1_text is not None and len(candidate.step1_text) <= budget_chars:
        return candidate.step1_text, False
    if candidate.step2_text is not None and len(candidate.step2_text) <= budget_chars:
        return candidate.step2_text, False
    text = _head_tail_truncate(candidate.step3_head or "", candidate.step3_tail or "", budget_chars)
    return text, True


def _wrap_block(candidate: ContextCandidate, text: str) -> str:
    model_attr = candidate.model or "unknown"
    return (
        f'<prior-run-context ref="{candidate.ref}" kind="{candidate.kind}" model="{model_attr}">\n'
        f"{text}\n"
        "</prior-run-context>"
    )


_BLOCK_SEPARATOR = "\n\n"


def build_context_block(candidates: Sequence[ContextCandidate], budget_tokens: int) -> str:
    """Assemble the XML-delimited context block(s), argv order, total-not-per-ref budget.

    `budget_tokens` bounds the COMBINED injected block (XML wrapper +
    separators included, not just distilled payload text); wrapper/marker
    overhead is reserved first, in argv order, before payload text.

    A single ref always yields at least one loud-marker-only block, even at
    `budget_tokens == 0` — a slightly over-budget marker beats silently
    dropping the one ref the caller asked for. With multiple refs, the total
    budget is a hard ceiling: any ref that can't fit even its minimum
    marker overhead is dropped (and everything after it, since the reserved
    budget only shrinks). If even the first ref can't fit, injection is
    skipped entirely.
    """
    from lionagi.cli._logging import warn

    if not candidates:
        return ""

    total_budget = budget_tokens * _CHARS_PER_TOKEN

    if len(candidates) == 1:
        candidate = candidates[0]
        overhead = len(_wrap_block(candidate, ""))
        text, truncated = _distill(candidate, max(total_budget - overhead, 0))
        if truncated:
            warn(
                f"--context-from {candidate.ref!r}: content exceeded the "
                f"{budget_tokens}-token budget, truncated (loud, not fatal)"
            )
        return _wrap_block(candidate, text)

    marker_min = len(_TRUNCATION_MARKER.strip())

    # Reserve wrapper + separator + minimum loud-marker overhead for each
    # candidate, in argv order, dropping any candidate (and all after it,
    # since the reserved budget only shrinks) that cannot fit even that
    # minimum within what remains of the total budget.
    fitted: list[tuple[ContextCandidate, int]] = []
    reserved = 0
    for candidate in candidates:
        overhead = len(_wrap_block(candidate, "")) + (len(_BLOCK_SEPARATOR) if fitted else 0)
        if reserved + overhead + marker_min > total_budget:
            break
        fitted.append((candidate, overhead))
        reserved += overhead

    if not fitted:
        warn(
            f"--context-from: total budget ({budget_tokens} tokens) is too small to "
            "fit even the minimum wrapped context block; skipping context "
            "injection entirely (no context was added)"
        )
        return ""

    if len(fitted) < len(candidates):
        dropped = [c.ref for c in candidates[len(fitted) :]]
        warn(
            f"--context-from: total budget ({budget_tokens} tokens) has no room left "
            f"for {dropped!r}; dropped entirely rather than exceeding the budget"
        )

    remaining = total_budget
    blocks: list[str] = []
    for candidate, overhead in fitted:
        text_budget = max(remaining - overhead, 0)
        text, truncated = _distill(candidate, text_budget)
        remaining -= overhead + len(text)
        if truncated:
            warn(
                f"--context-from {candidate.ref!r}: content exceeded the "
                f"{budget_tokens}-token budget, truncated (loud, not fatal)"
            )
        blocks.append(_wrap_block(candidate, text))
    return _BLOCK_SEPARATOR.join(blocks)


async def resolve_and_build_context_block(refs: Sequence[str], budget_tokens: int) -> str:
    candidates = await resolve_context_refs(refs)
    return build_context_block(candidates, budget_tokens)
