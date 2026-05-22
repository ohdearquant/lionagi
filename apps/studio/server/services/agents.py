from __future__ import annotations

import logging
from typing import Any

import yaml

from lionagi.cli._runs import LIONAGI_HOME

from ._path_safety import public_path, safe_path_join

_AGENTS_ROOT = LIONAGI_HOME / "agents"
_log = logging.getLogger(__name__)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter + markdown body. Returns (frontmatter_dict, body_text)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        fm = {}
    return fm if isinstance(fm, dict) else {}, parts[2].strip()


def _normalize_frontmatter(fm: dict[str, Any]) -> dict[str, Any]:
    """Return canonical Studio agent frontmatter without mutating caller state."""
    normalized = dict(fm)
    if "reasoning_effort" in normalized:
        if "effort" not in normalized:
            normalized["effort"] = normalized["reasoning_effort"]
        normalized.pop("reasoning_effort", None)
        _log.warning("Agent frontmatter key 'reasoning_effort' is deprecated; use 'effort'")
    return normalized


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
            "provider": str(fm.get("provider") or ""),
            "model": str(fm.get("model") or ""),
            "description": str(fm.get("description") or ""),
            **{k: v for k, v in fm.items() if k not in ("model", "description", "provider")},
        }
        if path.is_symlink():
            try:
                entry["symlink_target"] = public_path(path.resolve())
            except OSError:
                pass
        out.append(entry)
    return out


def get_agent(name: str) -> dict[str, Any] | None:
    # Validate path component — raises HTTPException(404) if unsafe
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

    # Flatten into AgentProfile shape expected by the frontend
    result: dict[str, Any] = {
        "name": stem,
        "path": public_path(path),
        "provider": str(fm.get("provider") or ""),
        "model": str(fm.get("model") or ""),
        "system_prompt": fm.get("system_prompt") or (body if body else None),
        "guidance": fm.get("guidance") or None,
    }

    # Preserve optional fields present in frontmatter. `effort` is canonical;
    # `reasoning_effort` is accepted only as a legacy read fallback.
    for optional_key in (
        "permission_mode",
        "effort",
        "description",
        "yolo",
        "fast_mode",
        "lion_system",
    ):
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
)


def update_agent(name: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """Write an agent profile back to disk.

    Unknown frontmatter keys (e.g. ``yolo``, ``max-ops``) are preserved so
    hand-authored agent files don't lose configuration on save.
    If the file is a symlink (the common case — ``~/.lionagi/agents/*`` points
    at ``firm/agents/*``), the write follows the link so the real source file
    is updated.
    """
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

    # Merge: start with everything that was there, overlay known keys from
    # the request. A key explicitly set to "" or None is treated as "clear".
    fm: dict[str, Any] = dict(existing_fm)
    incoming = _normalize_frontmatter(data)
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

    # system_prompt convention: body of the markdown file. Frontmatter
    # ``system_prompt`` is supported on read but we always write to the body
    # so existing agent .md files (which use the body) stay readable.
    new_body = data.get("system_prompt")
    if new_body is None:
        new_body = existing_body
    new_body = (new_body or "").strip()

    if fm:
        fm_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
        new_text = f"---\n{fm_text}\n---\n\n{new_body}\n" if new_body else f"---\n{fm_text}\n---\n"
    else:
        new_text = f"{new_body}\n" if new_body else ""

    # open(..., "w") on a symlink truncates the target, not the link — exactly
    # what we want.
    path.write_text(new_text)

    return get_agent(name)
