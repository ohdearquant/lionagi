# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Deterministic display-name derivation for sessions/runs, shared between the
write path (transcript-mirror ingestion — cli/mirror.py) and the read path
(studio API — studio/services/sessions.py, runs.py) so both agree on what
"prompt-shaped" and "sane display width" mean. No randomness, no DB reads:
every function here is a pure transform over already-available fields, so a
row's resolved name is stable across re-reads and safe to compute per row on
a paginated list.
"""

from __future__ import annotations

import re
import time
from typing import Any

DISPLAY_NAME_MAX_LEN = 80

# A run's own prompt sometimes carries the framework's system-message banner
# verbatim (e.g. a caller that folds system + instruction into one field) —
# strip the banner token itself, then any markdown separator/heading it wraps
# (the banner is typically followed by a "---" rule and a "# Heading"), then a
# short "Label:" prefix (e.g. "Guidance:") wrapping the whole thing. These are
# tried repeatedly since they nest — a "Guidance:" wrapper around a
# "LION_SYSTEM_MESSAGE" block needs two passes to fully unwrap.
_LEADING_BANNER_RE = re.compile(
    r"^(?:LION_SYSTEM_MESSAGE|END_OF_LION_SYSTEM_MESSAGE)\b[\s:.\-]*",
    re.IGNORECASE,
)
_LEADING_MARKDOWN_RE = re.compile(r"^(?:-{2,}|#{1,6})\s*")
_LEADING_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,24}:\s*")
# A prompt routed through a YAML document keeps the block-scalar indicator that
# introduced it ("|" or "|-"), and it lands ahead of the banner, so it defeats
# every pattern above and the whole banner survives into the display name.
# Anchored to the indicator alone: a lone "|" is never the start of prose, but
# text merely containing one often is.
_LEADING_BLOCK_SCALAR_RE = re.compile(r"^\|[-+]?\s*")
_STRIP_PATTERNS = (
    _LEADING_BLOCK_SCALAR_RE,
    _LEADING_BANNER_RE,
    _LEADING_MARKDOWN_RE,
    _LEADING_LABEL_RE,
)
_MAX_STRIP_PASSES = 6


def sanitize_prompt_name(raw: str | None, *, max_len: int = DISPLAY_NAME_MAX_LEN) -> str | None:
    """Turn raw prompt/instruction text into a short, banner-free display name.

    Collapses whitespace, strips a leading system-message banner / markdown
    separator / "Label:" prefix (repeated — these stack), and caps the result
    at `max_len` with an ellipsis. A name is never left starting with a
    colon'd prefix like "Guidance:". Idempotent on text that is already
    clean and short.

    Returns `None`, never `""`, whenever there is no usable name — both for
    empty/whitespace-only `raw`, and for a banner-only `raw` (e.g. just
    `"LION_SYSTEM_MESSAGE"`, with nothing left once the banner is stripped).
    A bare `""` would be ambiguous between those two cases and easy to
    mistake for "the display name is the empty string"; `None` reads
    unambiguously as "nothing to show here" and is falsy the same way `""`
    is, so every existing `if sanitized:` caller keeps falling through to
    its own next tier without any change.
    """
    if not raw:
        return None
    text = " ".join(raw.split())
    for _ in range(_MAX_STRIP_PASSES):
        for pattern in _STRIP_PATTERNS:
            stripped = pattern.sub("", text, count=1).strip()
            if stripped != text:
                text = stripped
                break
        else:
            break
    if not text:
        return None
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def agent_role_label(agent_name: str, started_at: float | None, run_id: str | None = None) -> str:
    """Deterministic label for an agent-only session: the agent's name, a short
    slice of the row's own id, and a UTC HH:MM stamp from its start time
    ("claude-code · 1167 · 14:22"). Pure function of the row (UTC, not local
    time, so it's independent of the resolving machine), so a re-read always
    formats the same way.

    The id slice earns its place: name+minute alone reads fine until the
    common case of several long-lived sessions of one engine turns a page
    into near-identical strings ("claude-code · 21:37", "· 21:49", "· 21:50")
    a viewer has to compare digit by digit. Four characters of id, though
    meaningless to an outsider, make each row something you can point at.
    Each part is dropped when its input is missing rather than rendered
    blank, so a row with only a name still returns that bare name.
    """
    label = agent_name.strip()
    if not label:
        return label
    parts = [label]
    short = str(run_id).strip()[:4] if run_id else ""
    if short:
        parts.append(short)
    if started_at is not None:
        parts.append(time.strftime("%H:%M", time.gmtime(started_at)))
    return " · ".join(parts)


def _stripped(session_row: dict[str, Any], key: str) -> str:
    """A field's value, stripped -- '' for missing/None/whitespace-only, so a
    blank column reads as absent instead of winning its tier with nothing."""
    value = session_row.get(key)
    return str(value).strip() if value else ""


# Stored names that identify nothing, written when the writer had nothing
# better ("agent" is lionagi's default branch name, "Codex session" the codex
# mirror's create-path fallback; "session"/"flow" are weaker-grounded entries
# kept on the strength of the stored data alone). Matched by value rather than
# by reordering the priority tiers, so rows with a real stored name and a
# derivable prompt are unaffected. Compared case-folded since several call
# sites write these with inconsistent casing.
_UNINFORMATIVE_STORED_NAMES: frozenset[str] = frozenset(
    {
        "agent",
        "session",
        "flow",
        "codex session",
    }
)


def _is_uninformative(raw_name: str) -> bool:
    """Whether a stored name is a placeholder rather than a description."""
    return raw_name.casefold() in _UNINFORMATIVE_STORED_NAMES


def resolve_display_name(session_row: dict[str, Any]) -> str:
    """Priority chain for a run's displayed name:

        user_label > show/play name > playbook name > agent-role descriptor
        > sanitized prompt-derived name > short id

    `user_label` has no write path anywhere in this codebase yet — it is read
    defensively via `.get()` so a future rename feature slots into the top of
    this chain without another reorder. Every other tier reads a field that
    is already computed or stored on the row.

    The prompt-derived tier declines a stored name that is a known placeholder
    (see `_UNINFORMATIVE_STORED_NAMES`) and falls through to the short id, so
    that thousands of rows sharing one default value render as distinct cards
    rather than as one repeated title. The order itself is unchanged.
    """
    user_label = _stripped(session_row, "user_label")
    if user_label:
        return user_label

    show_play_name = _stripped(session_row, "show_play_name")
    if show_play_name:
        return show_play_name

    playbook_name = _stripped(session_row, "playbook_name")
    if playbook_name:
        return playbook_name

    agent_name = _stripped(session_row, "agent_name")
    if agent_name:
        label = agent_role_label(
            agent_name,
            session_row.get("started_at"),
            session_row.get("id") or session_row.get("run_id"),
        )
        if label:
            return label

    raw_name = _stripped(session_row, "name")
    if raw_name and not _is_uninformative(raw_name):
        sanitized = sanitize_prompt_name(raw_name)
        if sanitized:
            return sanitized

    short_id = session_row.get("id") or session_row.get("run_id") or ""
    return str(short_id)[-12:]
