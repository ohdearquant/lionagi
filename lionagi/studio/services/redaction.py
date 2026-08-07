# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Server-side redaction for a demo-safe view of Library agent-profile content.

Enabled by ``LIONAGI_STUDIO_DEMO_MODE``, a process-wide switch read fresh on every
call -- never something a request can select. When on, every route that reads
agent-profile content projects through the single classification table below
(:data:`_SAFE_KEYS`) instead of returning frontmatter and body verbatim, so a
screen-share or recorded demo of the Library never surfaces owner-authored
prompts, guidance text, internal paths, or unrecognized frontmatter values.

Classification starts from field *name*: an unrecognized key is dropped even
when its value is a harmless-looking bool or number, because what leaks is the
key existing in the response at all, not any property of what it happens to
hold this run. A safe key's name is not enough on its own, though -- its value
must also match the scalar shape the name implies, or a mapping/list nested
under that key name would ride through the allowlist unexamined.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Fields whose values are structural/categorical -- safe to pass through verbatim
# regardless of what a profile's author put in them.
_SAFE_KEYS = frozenset(
    {
        "name",
        "provider",
        "model",
        "effort",
        "reasoning_effort",
        "permission_mode",
        "role",
        "mode",
        "yolo",
        "fast_mode",
        "lion_system",
        "protected",
        "is_default",
        "version",
        "kind",
        "has_versions",
        "history_available",
        "updated_at",
        "created_at",
        "saved_at",
    }
)

# Owner-authored free text -- replaced with a structured placeholder rather than
# passed through or silently dropped, so the field's presence still tells a
# viewer "this profile has content here" without shipping the content itself.
_TEXT_REDACT_KEYS = frozenset({"system_prompt", "guidance", "description", "content"})

# Filesystem locations -- reduced to a bare filename rather than omitted, since
# the name alone (already visible via "name") carries no more information than
# the file already reveals by existing in the roster.
_PATH_KEYS = frozenset({"path", "disk_path", "symlink_target"})

# Prefix of the placeholder text produced for redacted fields. Also used to
# detect a redacted payload bouncing back through a save request -- see
# reject_if_redacted_payload().
REDACTION_PLACEHOLDER_MARKER = "<redacted,"

# A safe key's value is vouched for by the classification table only when it
# is one of these scalar types. A mapping or sequence smuggled in under a
# safe key's name (e.g. ``role: {api_key: ...}``) is not a role -- it is
# unrecognized content wearing a safe key's name, and gets dropped like any
# other unrecognized value rather than passed through by the name match alone.
_SAFE_SCALAR_TYPES = (str, int, float, bool, type(None))


class RedactedPayloadError(Exception):
    """Raised when a write submits empty or placeholder content while demo mode is on."""


def demo_mode_enabled() -> bool:
    """True when the server-side demo-mode switch is on.

    Read fresh from the environment on every call (not cached at import time)
    so tests can toggle it with ``monkeypatch.setenv`` and a running process
    picks up a change without a restart. No request header, query parameter,
    or request body ever feeds into this decision.
    """
    raw = os.environ.get("LIONAGI_STUDIO_DEMO_MODE", "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _placeholder(value: Any) -> str:
    return f"{REDACTION_PLACEHOLDER_MARKER} {len(str(value))} chars>"


def abbreviate_path(value: Any) -> str:
    """Reduce a filesystem path to its bare filename -- shared by every route
    that carries a ``path``/``disk_path``/``symlink_target`` field, whether
    that field lives inside a profile record (:func:`project_agent_fields`)
    or in a definitions-route envelope built around one."""
    return Path(str(value)).name


def project_agent_fields(entry: Mapping[str, Any], *, redact: bool) -> dict[str, Any]:
    """Project one agent-profile record through the shared classification table.

    ``redact=False`` returns a shallow copy unchanged -- the same function backs
    both the redacted and unredacted responses, so a negative-control test that
    flips this flag is exercising the same code path the real routes use, not a
    parallel implementation that could drift from it.
    """
    if not redact:
        return dict(entry)

    out: dict[str, Any] = {}
    for key, value in entry.items():
        if key in _SAFE_KEYS:
            if isinstance(value, _SAFE_SCALAR_TYPES):
                out[key] = value
            # A mapping/sequence under a safe key is dropped, not passed
            # through -- the allowlist vouches for the key's expected scalar
            # shape, not for whatever an owner-authored profile nested there.
        elif key in _PATH_KEYS:
            if value not in (None, ""):
                out[key] = abbreviate_path(value)
        elif key in _TEXT_REDACT_KEYS:
            if value not in (None, ""):
                out[key] = _placeholder(value)
        # Any other key -- including every unrecognized frontmatter key -- is
        # dropped by name, whatever type its value is.
    return out


def redact_agent_markdown(text: str, *, redact: bool) -> str:
    """Project a raw agent-profile markdown file (frontmatter + body) the same
    way :func:`project_agent_fields` projects the parsed dict form.

    Used by the definitions routes, which serve profile content as one
    frontmatter+body string rather than as a pre-parsed record -- the two
    representations of the same file must redact identically, or a route
    reachable through ``/definitions/agent/{name}`` stays a full-fidelity
    mirror of a route already covered.
    """
    if not redact:
        return text

    from lionagi.libs.frontmatter import parse_frontmatter

    fm, body = parse_frontmatter(text)
    fm = dict(fm)
    if "reasoning_effort" in fm and "effort" not in fm:
        fm["effort"] = fm.pop("reasoning_effort")

    projected_fm = project_agent_fields(fm, redact=True)
    body_out = _placeholder(body) if body.strip() else body

    if projected_fm:
        import yaml

        fm_text = yaml.safe_dump(projected_fm, sort_keys=False, allow_unicode=True).rstrip()
        return f"---\n{fm_text}\n---\n\n{body_out}\n" if body_out else f"---\n{fm_text}\n---\n"
    return f"{body_out}\n" if body_out else ""


def is_placeholder_text(value: Any) -> bool:
    """True when a submitted value is (or contains) the redaction placeholder --
    the signature of a redacted payload bouncing back through a save request."""
    return isinstance(value, str) and REDACTION_PLACEHOLDER_MARKER in value


def reject_if_redacted_payload(*values: Any) -> None:
    """Refuse a write whose content is missing or carries the redaction
    placeholder, while demo mode is on.

    Called from every write path onto agent-profile content (the PUT
    ``/agents/{name}`` route and the generic ``/definitions/agent/{name}``
    save route) so a client that fetched a redacted view and posted it back
    unmodified can never overwrite the real file on disk -- the read-side
    switch would otherwise be a one-way door into permanent data loss.
    """
    if not demo_mode_enabled():
        return
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and is_placeholder_text(value):
            raise RedactedPayloadError(
                "Refusing to save: submitted content matches the redaction "
                "placeholder while demo mode is active"
            )
