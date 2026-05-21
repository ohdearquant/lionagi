from __future__ import annotations

from typing import Any

import yaml

from lionagi.cli._runs import LIONAGI_HOME

from ._path_safety import public_path, safe_path_join

_PLAYBOOKS_ROOT = LIONAGI_HOME / "playbooks"


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
    """Write a playbook YAML back to disk.

    Conservative merge:

    - ``description`` always overwrites if present in the payload.
    - Graph-format keys (``use``, ``steps``, ``links``) overwrite only when
      they carry content, so a declarative playbook opened in the graph
      editor doesn't get stamped with empty ``steps: {}``.
    - Declarative keys (``agent``, ``effort``, ``max-ops``, ``prompt``,
      ``args``, ``yolo``, ``show-graph``, ``argument-hint``) overwrite when
      present in the payload; ``None`` / empty-string removes the key.
    - Every other key already on disk is preserved untouched.

    Symlinks: ``~/.lionagi/playbooks/*`` may be symlinks; ``write_text`` on
    a symlinked path writes through to the target.
    """
    stem = name.removesuffix(".playbook.yaml").removesuffix(".yaml")
    safe_path_join(_PLAYBOOKS_ROOT, f"{stem}.playbook.yaml")
    path = _PLAYBOOKS_ROOT / f"{stem}.playbook.yaml"
    if not path.exists():
        return None

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

    # Graph-format keys: only write when non-empty.
    use = data.get("use")
    if isinstance(use, dict) and use.get("models"):
        merged["use"] = use

    steps = data.get("steps")
    if isinstance(steps, dict) and len(steps) > 0:
        merged["steps"] = steps

    links = data.get("links")
    if isinstance(links, list) and len(links) > 0:
        merged["links"] = links

    # Declarative-format keys: drop on explicit None/"" so the editor can
    # clear an optional field; otherwise overwrite.
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
    """Lightweight pre-save validation. Returns ``{ok, errors?}``.

    Currently checks:
    - links don't reference non-existent steps
    - duplicate step ids would be a JSON-impossible state but we guard anyway
    """
    errors: list[str] = []

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
