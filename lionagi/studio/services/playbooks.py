from __future__ import annotations

from typing import Any

import yaml

from lionagi._paths import LIONAGI_HOME
from lionagi.service.providers import EFFORT_LEVELS as _VALID_EFFORT_LEVELS

from ._path_safety import public_path, safe_path_join

_PLAYBOOKS_ROOT = LIONAGI_HOME / "playbooks"

# Keys that stay dashed per CLI convention (all others: hyphens → underscores).
_PRESERVE_DASHED: frozenset[str] = frozenset({"argument-hint"})


def _normalize_spec_keys(data: dict[str, Any]) -> dict[str, Any]:
    """Convert hyphenated YAML keys to underscored, mirroring CLI normalization."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in _PRESERVE_DASHED or "-" not in key:
            out[key] = value
        else:
            out[key.replace("-", "_")] = value
    return out


def _check_spec_fields(spec: dict[str, Any]) -> str | None:
    """Validate playbook spec fields; return an error string or None.

    Mirrors lionagi/cli/orchestrate/__init__.py::_validate_spec_fields() exactly.
    Implemented inline to avoid loading the full orchestrate module at import time.
    """
    if "workers" in spec:
        workers = spec["workers"]
        if not isinstance(workers, int) or isinstance(workers, bool):
            return f"spec field 'workers' must be an integer, got {type(workers).__name__}"
        if not (1 <= workers <= 32):
            return f"spec field 'workers' must be in [1, 32], got {workers}"

    for key in ("max_ops", "max_agents"):
        if key not in spec:
            continue
        value = spec[key]
        if not isinstance(value, int) or isinstance(value, bool):
            return f"spec field {key!r} must be an integer, got {type(value).__name__}"
        if not (0 <= value <= 50):
            return f"spec field {key!r} must be in [0, 50] (0 = unlimited), got {value}"

    effort = spec.get("effort")
    if effort is not None:
        if not isinstance(effort, str):
            return f"spec field 'effort' must be a string, got {type(effort).__name__}"
        if effort not in _VALID_EFFORT_LEVELS:
            return (
                f"spec field 'effort' must be one of {sorted(_VALID_EFFORT_LEVELS)}, got {effort!r}"
            )

    if "with_synthesis" in spec:
        val = spec["with_synthesis"]
        if not isinstance(val, bool | str):
            return (
                f"spec field 'with_synthesis' must be bool or str (model spec), "
                f"got {type(val).__name__}"
            )

    return None


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


# Declarative-format keys we will write through from the editor. Anything not
# in this list is preserved as-is from the existing YAML so handcrafted keys
# (or future additions) don't get clobbered.
_DECLARATIVE_KEYS: tuple[str, ...] = (
    "agent",
    "effort",
    "max-ops",
    "prompt",
    "args",
    "yolo",
    "show-graph",
    "argument-hint",
)


def update_playbook(name: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """Write a playbook YAML back to disk with conservative merge.

    description overwrites when present; graph keys (use/steps/links) only when
    non-empty; declarative keys overwrite or are cleared on None/""; all other
    disk keys preserved.  Writes through symlinks to the real source file.
    """
    stem = name.removesuffix(".playbook.yaml").removesuffix(".yaml")
    safe_path_join(_PLAYBOOKS_ROOT, f"{stem}.playbook.yaml")
    path = _PLAYBOOKS_ROOT / f"{stem}.playbook.yaml"
    if not path.exists():
        return None

    # Validate spec fields before the merge: the merge silently drops unknown
    # keys (e.g. 'workers'), so validating the raw payload catches bad values
    # that would otherwise pass through to validate_playbook() undetected.
    spec_err = _check_spec_fields(_normalize_spec_keys(data))
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

    for key in _DECLARATIVE_KEYS:
        if key not in data:
            continue
        value = data[key]
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
    """Pre-save validation. Returns ``{ok, errors?}``.

    Checks:
    - spec fields (workers, max_ops, effort, with_synthesis) have valid types/ranges
    - links don't reference non-existent steps
    """
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
