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
_STRIP_PATTERNS = (_LEADING_BANNER_RE, _LEADING_MARKDOWN_RE, _LEADING_LABEL_RE)
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


def agent_role_label(agent_name: str, started_at: float | None) -> str:
    """Deterministic label for an agent-only session: the agent's name plus a
    UTC HH:MM disambiguator from its own start time, so concurrent runs of the
    same agent read as distinct cards ("implementer · 14:22") without a
    lookup against sibling rows. Stable across re-reads (same started_at
    always formats the same way) and computed from UTC so it does not depend
    on the resolving machine's local timezone.

    Two same-agent runs started in the same minute still collide on this
    label — that is accepted, by design, not a bug: the row's id remains the
    real identity everywhere it matters (links, keys, API lookups), and this
    label exists only to make a list of cards readable at a glance. Making
    the label itself collision-proof (seconds, a counter, a suffix of the
    id) would make the common case noisier to read to avoid an edge case
    nothing actually depends on for correctness.
    """
    label = agent_name.strip()
    if not label:
        return label
    if started_at is None:
        return label
    stamp = time.strftime("%H:%M", time.gmtime(started_at))
    return f"{label} · {stamp}"


def _stripped(session_row: dict[str, Any], key: str) -> str:
    """A field's value, stripped -- '' for missing/None/whitespace-only, so a
    blank column reads as absent instead of winning its tier with nothing."""
    value = session_row.get(key)
    return str(value).strip() if value else ""


def resolve_display_name(session_row: dict[str, Any]) -> str:
    """Priority chain for a run's displayed name:

        user_label > show/play name > playbook name > agent-role descriptor
        > sanitized prompt-derived name > short id

    `user_label` has no write path anywhere in this codebase yet — it is read
    defensively via `.get()` so a future rename feature slots into the top of
    this chain without another reorder. Every other tier reads a field that
    is already computed or stored on the row.
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
        label = agent_role_label(agent_name, session_row.get("started_at"))
        if label:
            return label

    raw_name = _stripped(session_row, "name")
    if raw_name:
        sanitized = sanitize_prompt_name(raw_name)
        if sanitized:
            return sanitized

    short_id = session_row.get("id") or session_row.get("run_id") or ""
    return str(short_id)[-12:]
