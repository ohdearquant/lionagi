from __future__ import annotations

import logging
from functools import partial
from typing import Annotated, Any

import anyio
import yaml
from fastapi import Body, HTTPException

from lionagi._paths import LIONAGI_HOME
from lionagi.casts.pattern import list_modes as _list_modes
from lionagi.casts.pattern import list_roles as _list_roles
from lionagi.libs.frontmatter import parse_frontmatter as _parse_frontmatter

from ..registry import studio_route
from ._path_safety import public_path, safe_path_join, validate_name_component
from .redaction import (
    RedactedPayloadError,
    demo_mode_enabled,
    project_agent_fields,
    reject_if_redacted_payload,
)

_AGENTS_ROOT = LIONAGI_HOME / "agents"
_log = logging.getLogger(__name__)

# The one agent Studio treats as the always-present fallback profile: not
# deletable, but (unlike a system agent) editable like any other agent.
DEFAULT_AGENT_NAME = "default"


def _is_protected_system(fm: dict[str, Any]) -> bool:
    """True when a file's frontmatter carries a present, truthy
    ``lion_system`` -- present and truthy in the YAML/Python sense, not just
    `True`; an *absent* key does NOT count as protected (would lock down
    every pre-feature agent file with no frontmatter at all). The single
    place both write-protection call sites (this module and
    ``definitions.py``'s agent save path) resolve the predicate, matching
    the runtime's own ``bool(frontmatter.get("lion_system", True))`` check
    in ``lionagi/cli/_providers.py``. See studio.md."""
    return "lion_system" in fm and bool(fm["lion_system"])


def _read_frontmatter(path) -> dict[str, Any]:
    try:
        text = path.read_text()
    except OSError:
        return {}
    fm, _ = _parse_frontmatter(text)
    return _normalize_frontmatter(fm)


def _normalize_frontmatter(fm: dict[str, Any]) -> dict[str, Any]:
    """Return canonical Studio agent frontmatter without mutating caller state."""
    normalized = dict(fm)
    if "reasoning_effort" in normalized:
        if "effort" not in normalized:
            normalized["effort"] = normalized["reasoning_effort"]
        normalized.pop("reasoning_effort", None)
        _log.warning("Agent frontmatter key 'reasoning_effort' is deprecated; use 'effort'")
    return normalized


_SCALAR_FRONTMATTER_TYPES = (str, int, float, bool, type(None))


def _display_scalar(value: Any) -> Any:
    """Return the display form of a scalar-shaped frontmatter value; a
    mapping or list is returned unchanged, not ``str()``-coerced.

    A ``str()`` call accepts any input, so coercing a nested mapping before
    it reaches :func:`redaction.project_agent_fields` would turn it into a
    plain string -- a shape the classification table's scalar check admits
    verbatim, defeating the very check meant to catch it. Leaving a non-scalar
    value's real shape intact lets that check see and drop it instead.
    """
    if isinstance(value, _SCALAR_FRONTMATTER_TYPES):
        return str(value) if value not in (None, "") else ""
    return value


def _canonical_model(model: Any, provider: Any) -> str:
    model_s = str(model or "").strip()
    if not model_s:
        return ""
    if "/" in model_s:
        return model_s
    provider_s = str(provider or "").strip()
    return f"{provider_s}/{model_s}" if provider_s else model_s


def list_agents() -> list[dict[str, Any]]:
    if not _AGENTS_ROOT.exists():
        return []
    out = []
    for path in sorted(_AGENTS_ROOT.glob("*.md")):
        try:
            text = path.read_text()
        except OSError:
            continue
        fm, _ = _parse_frontmatter(text)
        fm = _normalize_frontmatter(fm)
        entry: dict[str, Any] = {
            "name": path.stem,
            "path": public_path(path),
            "provider": _display_scalar(fm.get("provider")),
            "model": _display_scalar(fm.get("model")),
            "description": str(fm.get("description") or ""),
            **{k: v for k, v in fm.items() if k not in ("model", "description", "provider")},
        }
        entry["protected"] = _is_protected_system(fm)
        entry["is_default"] = path.stem == DEFAULT_AGENT_NAME
        if path.is_symlink():
            try:
                entry["symlink_target"] = public_path(path.resolve())
            except OSError:
                pass
        out.append(entry)
    return out


def get_agent(name: str) -> dict[str, Any] | None:
    safe_path_join(_AGENTS_ROOT, name)

    stem = name.removesuffix(".md")
    path = _AGENTS_ROOT / f"{stem}.md"
    if not path.exists():
        return None
    try:
        text = path.read_text()
    except OSError:
        return None
    fm, body = _parse_frontmatter(text)
    fm = _normalize_frontmatter(fm)

    result: dict[str, Any] = {
        "name": stem,
        "path": public_path(path),
        "provider": _display_scalar(fm.get("provider")),
        "model": _display_scalar(fm.get("model")),
        "system_prompt": fm.get("system_prompt") or (body if body else None),
        "guidance": fm.get("guidance") or None,
    }

    result["yolo"] = bool(fm.get("yolo", False))
    result["fast_mode"] = bool(fm.get("fast_mode", False))
    # CLI-parity display default (absent key reads as True) -- distinct from
    # the stricter, explicit-only check _is_protected_system() uses to gate writes.
    result["lion_system"] = bool(fm.get("lion_system", True))
    result["protected"] = _is_protected_system(fm)
    result["is_default"] = stem == DEFAULT_AGENT_NAME

    for optional_key in ("permission_mode", "effort", "description", "role", "mode"):
        if optional_key in fm:
            result[optional_key] = fm[optional_key]

    if path.is_symlink():
        try:
            result["symlink_target"] = public_path(path.resolve())
        except OSError:
            pass

    return result


_KNOWN_FRONTMATTER_KEYS = (
    "provider",
    "model",
    "description",
    "guidance",
    "permission_mode",
    "effort",
    "yolo",
    "fast_mode",
    "lion_system",
    "role",
    "mode",
)


class AgentExistsError(Exception):
    """Raised by create_agent() when the target name already has a file on disk."""


class AgentProtectedError(Exception):
    """Raised when a write targets the system agent or the default agent's delete guard."""


def _canonical_role(role: Any) -> str:
    """Validate a role and return the exact value that should be stored.

    The stored value is what the runtime looks up, and that lookup is exact
    (``AgentSpec.coding()``). Validating a stripped copy while writing the
    original back would accept ``" critic "`` here and fail to launch it later,
    so the canonical form is what both the check and the write use.
    """
    role_s = str(role or "").strip()
    if role_s and role_s not in _list_roles():
        raise ValueError(f"Unknown cast role: {role_s!r}")
    return role_s


def _canonical_mode(mode: Any) -> str:
    """Validate a mode and return the exact value that should be stored. Same
    reasoning as ``_canonical_role``."""
    mode_s = str(mode or "").strip()
    if mode_s and mode_s not in _list_modes():
        raise ValueError(f"Unknown cast mode: {mode_s!r}")
    return mode_s


def _canonicalize_casts(incoming: dict[str, Any]) -> None:
    """Validate role and mode in place, so every write path stores the same
    form it validated. Absent keys stay absent -- an omitted role is not the
    same request as an empty one."""
    if "role" in incoming:
        incoming["role"] = _canonical_role(incoming.get("role"))
    if "mode" in incoming:
        incoming["mode"] = _canonical_mode(incoming.get("mode"))


def create_agent(name: str, data: dict[str, Any]) -> dict[str, Any]:
    """Create a new agent profile file. Refuses to overwrite an existing name so that
    creating a second, differently-configured version of a role always requires its own
    name. Agents created through this route are never system-owned: ``lion_system`` is
    always stamped false so the new agent is immediately editable and deletable."""
    validate_name_component(name, label="name")
    safe_path_join(_AGENTS_ROOT, name)
    stem = name.removesuffix(".md")
    path = _AGENTS_ROOT / f"{stem}.md"
    if path.exists():
        raise AgentExistsError(f"Agent '{stem}' already exists")

    incoming = _normalize_frontmatter(data)
    _canonicalize_casts(incoming)

    fm: dict[str, Any] = {}
    for key in _KNOWN_FRONTMATTER_KEYS:
        if key not in incoming:
            continue
        value = incoming[key]
        if value not in (None, ""):
            fm[key] = value

    fm["lion_system"] = False

    if "model" in fm:
        model = _canonical_model(fm.get("model"), fm.get("provider"))
        if model:
            fm["model"] = model
        else:
            fm.pop("model", None)

    body = (data.get("system_prompt") or "").strip()

    _AGENTS_ROOT.mkdir(parents=True, exist_ok=True)
    if fm:
        fm_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
        new_text = f"---\n{fm_text}\n---\n\n{body}\n" if body else f"---\n{fm_text}\n---\n"
    else:
        new_text = f"{body}\n" if body else ""

    path.write_text(new_text)
    return get_agent(stem)


def delete_agent(name: str) -> bool:
    """Delete an agent profile file. Refuses the default agent (name match) and any
    system agent (``lion_system`` true, the pre-existing default). Returns False if the
    agent does not exist so the route can 404 instead of raising."""
    safe_path_join(_AGENTS_ROOT, name)
    stem = name.removesuffix(".md")
    path = _AGENTS_ROOT / f"{stem}.md"
    if not path.exists():
        return False

    if stem == DEFAULT_AGENT_NAME:
        raise AgentProtectedError(f"Agent '{stem}' is the default agent and cannot be deleted")

    fm = _read_frontmatter(path)
    if _is_protected_system(fm):
        raise AgentProtectedError(f"Agent '{stem}' is a system agent and cannot be deleted")

    path.unlink()
    return True


def update_agent(name: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """Write an agent profile back to disk; preserves unknown frontmatter keys; follows symlinks."""
    safe_path_join(_AGENTS_ROOT, name)
    stem = name.removesuffix(".md")
    path = _AGENTS_ROOT / f"{stem}.md"
    if not path.exists():
        return None

    try:
        existing_text = path.read_text()
    except OSError:
        existing_text = ""
    existing_fm, existing_body = _parse_frontmatter(existing_text)
    existing_fm = _normalize_frontmatter(existing_fm)

    if _is_protected_system(existing_fm):
        raise AgentProtectedError(f"Agent '{stem}' is a system agent and cannot be edited")

    # While demo mode is on, a client that fetched the redacted view and posted
    # it back unmodified must not be able to overwrite the real file with the
    # placeholder text -- the other write path onto agent files (the generic
    # definitions save route) carries the same guard; see redaction.py.
    reject_if_redacted_payload(
        data.get("system_prompt"), data.get("guidance"), data.get("description")
    )

    fm: dict[str, Any] = dict(existing_fm)
    incoming = _normalize_frontmatter(data)
    _canonicalize_casts(incoming)
    for key in _KNOWN_FRONTMATTER_KEYS:
        if key not in incoming:
            continue
        value = incoming[key]
        if value in (None, ""):
            fm.pop(key, None)
        else:
            fm[key] = value

    if "model" in fm:
        model = _canonical_model(fm.get("model"), fm.get("provider"))
        if model:
            fm["model"] = model
        else:
            fm.pop("model", None)

    new_body = data.get("system_prompt")
    if new_body is None:
        new_body = existing_body
    new_body = (new_body or "").strip()

    if fm:
        fm_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
        new_text = f"---\n{fm_text}\n---\n\n{new_body}\n" if new_body else f"---\n{fm_text}\n---\n"
    else:
        new_text = f"{new_body}\n" if new_body else ""

    path.write_text(new_text)

    return get_agent(name)


@studio_route("/agents/", method="GET", area="agents", name="list_agents")
async def list_agents_route() -> dict[str, Any]:
    agents = await anyio.to_thread.run_sync(list_agents)
    redact = demo_mode_enabled()
    agents = [project_agent_fields(a, redact=redact) for a in agents]
    return {"agents": agents}


@studio_route("/agents/{name}", method="GET", area="agents", name="get_agent")
async def get_agent_route(name: str) -> dict[str, Any]:
    agent = await anyio.to_thread.run_sync(partial(get_agent, name))
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return project_agent_fields(agent, redact=demo_mode_enabled())


@studio_route("/agents/{name}", method="POST", area="agents", name="create_agent")
async def create_agent_route(
    name: str, body: Annotated[dict[str, Any], Body(default_factory=dict)]
) -> dict[str, Any]:
    try:
        created = await anyio.to_thread.run_sync(partial(create_agent, name, body))
    except AgentExistsError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return project_agent_fields(created, redact=demo_mode_enabled())


@studio_route("/agents/{name}", method="PUT", area="agents", name="update_agent")
async def update_agent_route(
    name: str, body: Annotated[dict[str, Any], Body(...)]
) -> dict[str, Any]:
    try:
        updated = await anyio.to_thread.run_sync(partial(update_agent, name, body))
    except (AgentProtectedError, RedactedPayloadError) as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return project_agent_fields(updated, redact=demo_mode_enabled())


@studio_route("/agents/{name}", method="DELETE", area="agents", name="delete_agent")
async def delete_agent_route(name: str) -> dict[str, Any]:
    try:
        deleted = await anyio.to_thread.run_sync(partial(delete_agent, name))
    except AgentProtectedError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return {"deleted": True, "name": name}


@studio_route("/agents/{name}/validate", method="POST", area="agents")
async def validate_agent(name: str, body: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
    errors: list[str] = []
    if not (body.get("name") or "").strip():
        errors.append("name is required")
    if not (body.get("provider") or "").strip():
        errors.append("provider is required")
    if not (body.get("model") or "").strip():
        errors.append("model is required")
    return {"ok": not errors, "errors": errors or None}
