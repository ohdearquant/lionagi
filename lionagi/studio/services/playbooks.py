from __future__ import annotations

import hashlib
import shutil
from functools import partial
from pathlib import Path
from typing import Annotated, Any

import anyio
import yaml
from fastapi import Body, HTTPException

from lionagi._flow_spec import (
    FLOW_SPEC_FIELDS,
    flow_spec_yaml_key,
)
from lionagi._flow_spec import (
    normalize_flow_spec_keys as _normalize_spec_keys,
)
from lionagi._flow_spec import (
    validate_flow_spec_fields as _check_spec_fields,
)
from lionagi._paths import LIONAGI_HOME, ensure_lionagi_dir

from ..registry import studio_route
from ._path_safety import public_path, safe_path_join

_PLAYBOOKS_ROOT = LIONAGI_HOME / "playbooks"

# Bundled read-only templates, shipped inside the installed package (see
# builtin_playbooks/README.md) so they're available on a real deployment.
_BUILTIN_PLAYBOOKS_ROOT = Path(__file__).resolve().parent.parent / "builtin_playbooks"


class _PlaybookDumper(yaml.SafeDumper):
    """SafeDumper with two ergonomic overrides for hand-edited playbook YAML."""


def _str_representer(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    # Multi-line strings (prompt, long descriptions) → literal block scalar so
    # diffs stay readable and round-trips don't reformat them into quoted scalars.
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_PlaybookDumper.add_representer(str, _str_representer)


def list_playbooks() -> list[dict[str, Any]]:
    if not _PLAYBOOKS_ROOT.exists():
        return []
    out = []
    for path in sorted(_PLAYBOOKS_ROOT.glob("*.playbook.yaml")):
        name = path.name.removesuffix(".playbook.yaml")
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            raw = {}
        entry: dict[str, Any] = {
            "name": name,
            "path": public_path(path),
            "description": raw.get("description", "") if isinstance(raw, dict) else "",
        }
        if path.is_symlink():
            try:
                entry["symlink_target"] = public_path(path.resolve())
            except OSError:
                pass
        out.append(entry)
    return out


def get_playbook(name: str) -> dict[str, Any] | None:
    stem = name.removesuffix(".playbook.yaml").removesuffix(".yaml")
    safe_path_join(_PLAYBOOKS_ROOT, f"{stem}.playbook.yaml")
    path = _PLAYBOOKS_ROOT / f"{stem}.playbook.yaml"
    if not path.exists():
        return None
    try:
        text = path.read_text()
        raw = yaml.safe_load(text) or {}
    except (OSError, yaml.YAMLError):
        return None
    result: dict[str, Any] = {
        "name": stem,
        "path": public_path(path),
        "data": raw if isinstance(raw, dict) else {},
        "raw": text,
    }
    if path.is_symlink():
        try:
            result["symlink_target"] = public_path(path.resolve())
        except OSError:
            pass
    return result


def fingerprint_playbook(name: str) -> str:
    """Return the exact content version used to gate a playbook launch."""
    stem = name.removesuffix(".playbook.yaml").removesuffix(".yaml")
    safe_path_join(_PLAYBOOKS_ROOT, f"{stem}.playbook.yaml")
    path = _PLAYBOOKS_ROOT / f"{stem}.playbook.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"Playbook {stem!r} not found")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def list_builtin_playbooks() -> list[dict[str, Any]]:
    """List the bundled built-in playbook templates (read-only, package data)."""
    if not _BUILTIN_PLAYBOOKS_ROOT.exists():
        return []
    out = []
    for path in sorted(_BUILTIN_PLAYBOOKS_ROOT.glob("*.playbook.yaml")):
        name = path.name.removesuffix(".playbook.yaml")
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        out.append(
            {
                "name": name,
                "description": raw.get("description", ""),
                "args": raw.get("args") or {},
                "argument_hint": raw.get("argument-hint", ""),
                "installed": (_PLAYBOOKS_ROOT / f"{name}.playbook.yaml").exists(),
            }
        )
    return out


def get_builtin_playbook(name: str) -> dict[str, Any] | None:
    """Full detail (data + raw text) for one bundled built-in template."""
    stem = name.removesuffix(".playbook.yaml").removesuffix(".yaml")
    safe_path_join(_BUILTIN_PLAYBOOKS_ROOT, f"{stem}.playbook.yaml")
    path = _BUILTIN_PLAYBOOKS_ROOT / f"{stem}.playbook.yaml"
    if not path.exists():
        return None
    try:
        text = path.read_text()
        raw = yaml.safe_load(text) or {}
    except (OSError, yaml.YAMLError):
        return None
    return {
        "name": stem,
        "data": raw if isinstance(raw, dict) else {},
        "raw": text,
        "installed": (_PLAYBOOKS_ROOT / f"{stem}.playbook.yaml").exists(),
    }


def install_builtin_playbook(name: str) -> dict[str, Any]:
    """Idempotently copy a built-in template into ``~/.lionagi/playbooks`` as a normal, user-editable playbook. No-op when the destination already exists."""
    stem = name.removesuffix(".playbook.yaml").removesuffix(".yaml")
    safe_path_join(_BUILTIN_PLAYBOOKS_ROOT, f"{stem}.playbook.yaml")
    src = _BUILTIN_PLAYBOOKS_ROOT / f"{stem}.playbook.yaml"
    if not src.exists():
        raise FileNotFoundError(f"Built-in playbook template {stem!r} not found")

    safe_path_join(_PLAYBOOKS_ROOT, f"{stem}.playbook.yaml")
    dest = _PLAYBOOKS_ROOT / f"{stem}.playbook.yaml"
    installed_now = False
    if not dest.exists():
        ensure_lionagi_dir(dest.parent)
        shutil.copyfile(src, dest)
        installed_now = True

    return {"installed": installed_now, "playbook": get_playbook(stem)}


_SPECIAL_PLAYBOOK_KEYS = frozenset({"description", "links", "name", "steps", "use"})
_DECLARATIVE_KEYS: dict[str, str] = {
    field: flow_spec_yaml_key(field) for field in sorted(FLOW_SPEC_FIELDS - _SPECIAL_PLAYBOOK_KEYS)
}


def create_playbook(name: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write a brand-new playbook YAML to disk. Raises FileExistsError if one already exists for *name*, ValueError if the spec fields or step/link references are invalid."""
    stem = name.removesuffix(".playbook.yaml").removesuffix(".yaml")
    safe_path_join(_PLAYBOOKS_ROOT, f"{stem}.playbook.yaml")
    path = _PLAYBOOKS_ROOT / f"{stem}.playbook.yaml"
    if path.exists():
        raise FileExistsError(f"Playbook '{stem}' already exists")

    data = data or {}
    normalized_data = _normalize_spec_keys(data)
    spec_err = _check_spec_fields(normalized_data)
    if spec_err:
        raise ValueError(spec_err)

    content: dict[str, Any] = {"description": data.get("description") or ""}

    for field, key in _DECLARATIVE_KEYS.items():
        value = normalized_data.get(field)
        if value not in (None, ""):
            content[key] = value

    use = data.get("use")
    if isinstance(use, dict) and use.get("models"):
        content["use"] = use

    steps = data.get("steps")
    if isinstance(steps, dict) and len(steps) > 0:
        content["steps"] = steps

    links = data.get("links")
    if isinstance(links, list) and len(links) > 0:
        content["links"] = links

    validation = validate_playbook(stem, content)
    if not validation["ok"]:
        raise ValueError("; ".join(validation["errors"] or ["invalid playbook"]))

    ensure_lionagi_dir(_PLAYBOOKS_ROOT)
    new_text = yaml.dump(
        content,
        Dumper=_PlaybookDumper,
        sort_keys=False,
        allow_unicode=True,
        width=120,
    )
    # Exclusive create closes the check-then-write TOCTOU: two concurrent
    # create requests (FastAPI runs sync routes in a threadpool) could both
    # pass the path.exists() guard above, and a plain write_text would let the
    # second silently clobber the first. "x" mode makes the write itself the
    # exclusion; re-raise with the same clean message the guard uses so the
    # route's 409 detail never leaks the absolute path.
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(new_text)
    except FileExistsError:
        raise FileExistsError(f"Playbook '{stem}' already exists") from None

    return get_playbook(stem)


def update_playbook(name: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """Write a playbook YAML back to disk with a conservative merge: description overwrites when present, graph keys (use/steps/links) only when non-empty, declarative keys overwrite or clear on None/"", all other disk keys preserved."""
    stem = name.removesuffix(".playbook.yaml").removesuffix(".yaml")
    safe_path_join(_PLAYBOOKS_ROOT, f"{stem}.playbook.yaml")
    path = _PLAYBOOKS_ROOT / f"{stem}.playbook.yaml"
    if not path.exists():
        return None

    # Validate before the merge so invalid values never reach the on-disk spec.
    normalized_data = _normalize_spec_keys(data)
    spec_err = _check_spec_fields(normalized_data)
    if spec_err:
        raise ValueError(spec_err)

    try:
        existing_text = path.read_text()
        existing_raw = yaml.safe_load(existing_text) or {}
    except (OSError, yaml.YAMLError):
        existing_raw = {}
    if not isinstance(existing_raw, dict):
        existing_raw = {}

    merged: dict[str, Any] = dict(existing_raw)

    if "description" in data:
        merged["description"] = data["description"] or ""

    use = data.get("use")
    if isinstance(use, dict) and use.get("models"):
        merged["use"] = use

    steps = data.get("steps")
    if isinstance(steps, dict) and len(steps) > 0:
        merged["steps"] = steps

    links = data.get("links")
    if isinstance(links, list) and len(links) > 0:
        merged["links"] = links

    for field, key in _DECLARATIVE_KEYS.items():
        if field not in normalized_data:
            continue
        value = normalized_data[field]
        if key != field:
            merged.pop(field, None)
        if value is None or value == "":
            merged.pop(key, None)
        else:
            merged[key] = value

    validation = validate_playbook(stem, merged)
    if not validation["ok"]:
        raise ValueError("; ".join(validation["errors"] or ["invalid playbook"]))

    new_text = yaml.dump(
        merged,
        Dumper=_PlaybookDumper,
        sort_keys=False,
        allow_unicode=True,
        width=120,
    )
    path.write_text(new_text)

    return get_playbook(stem)


def validate_playbook(name: str, data: dict[str, Any]) -> dict[str, Any]:
    """Pre-save validation of spec fields and step-link references. Returns ``{ok, errors?}``."""
    errors: list[str] = []

    # Spec-field validation: normalize hyphenated keys first (max-ops → max_ops)
    # so YAML-authored playbooks get the same constraints as CLI invocations.
    normalized = _normalize_spec_keys(data)
    spec_err = _check_spec_fields(normalized)
    if spec_err:
        errors.append(spec_err)

    steps = data.get("steps") if isinstance(data.get("steps"), dict) else {}
    links = data.get("links") if isinstance(data.get("links"), list) else []
    step_ids = set(steps.keys())

    for i, link in enumerate(links):
        if not isinstance(link, dict):
            errors.append(f"link {i}: not an object")
            continue
        frm = link.get("from")
        to = link.get("to")
        if frm and frm not in step_ids:
            errors.append(f"link {i}: 'from' references unknown step '{frm}'")
        if to and to not in step_ids:
            errors.append(f"link {i}: 'to' references unknown step '{to}'")

    return {"ok": len(errors) == 0, "errors": errors or None}


@studio_route("/playbooks/", method="GET", area="playbooks", name="list_playbooks")
async def list_playbooks_route() -> dict[str, Any]:
    playbooks = await anyio.to_thread.run_sync(list_playbooks)
    return {"playbooks": playbooks}


@studio_route("/playbooks/{name}", method="GET", area="playbooks", name="get_playbook")
async def get_playbook_route(name: str) -> dict[str, Any]:
    pb = await anyio.to_thread.run_sync(partial(get_playbook, name))
    if pb is None:
        raise HTTPException(status_code=404, detail=f"Playbook '{name}' not found")
    return pb


@studio_route("/playbooks/{name}", method="POST", area="playbooks", name="create_playbook")
async def create_playbook_route(
    name: str, body: Annotated[dict[str, Any], Body(default_factory=dict)]
) -> dict[str, Any]:
    try:
        created = await anyio.to_thread.run_sync(partial(create_playbook, name, body))
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return created


@studio_route("/playbooks/{name}", method="PUT", area="playbooks", name="update_playbook")
async def update_playbook_route(
    name: str, body: Annotated[dict[str, Any], Body(...)]
) -> dict[str, Any]:
    try:
        updated = await anyio.to_thread.run_sync(partial(update_playbook, name, body))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Playbook '{name}' not found")
    return updated


@studio_route("/playbooks/{name}", method="DELETE", area="playbooks")
async def delete_playbook(name: str) -> dict[str, Any]:
    # TODO(lift-backend-writes)
    raise HTTPException(status_code=501, detail="Not implemented")


@studio_route(
    "/playbooks/{name}/validate", method="POST", area="playbooks", name="validate_playbook"
)
async def validate_playbook_route(
    name: str, body: Annotated[dict[str, Any], Body(...)]
) -> dict[str, Any]:
    return validate_playbook(name, body)


@studio_route("/playbooks/{name}/run", method="POST", area="playbooks")
async def run_playbook(name: str) -> dict[str, Any]:
    # TODO(lift-backend-writes)
    raise HTTPException(status_code=501, detail="Not implemented")


# ── Built-in templates (read-only package data + idempotent install) ──────────
# Distinct path prefix (not nested under /playbooks/{name}) so there is no
# ambiguity with the single-segment {name} routes above.


@studio_route("/playbook-templates/", method="GET", area="playbooks", name="list_builtin_playbooks")
async def list_builtin_playbooks_route() -> dict[str, Any]:
    playbooks = await anyio.to_thread.run_sync(list_builtin_playbooks)
    return {"playbooks": playbooks}


@studio_route(
    "/playbook-templates/{name}",
    method="GET",
    area="playbooks",
    name="get_builtin_playbook",
)
async def get_builtin_playbook_route(name: str) -> dict[str, Any]:
    pb = await anyio.to_thread.run_sync(partial(get_builtin_playbook, name))
    if pb is None:
        raise HTTPException(status_code=404, detail=f"Built-in playbook '{name}' not found")
    return pb


@studio_route(
    "/playbook-templates/{name}/install",
    method="POST",
    area="playbooks",
    name="install_builtin_playbook",
)
async def install_builtin_playbook_route(name: str) -> dict[str, Any]:
    try:
        result = await anyio.to_thread.run_sync(partial(install_builtin_playbook, name))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return result
