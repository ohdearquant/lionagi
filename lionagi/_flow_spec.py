"""Shared parsing and static validation for flow and playbook specs."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from lionagi._spec_limits import MAX_SPEC_PROMPT_CHARS
from lionagi.service.providers import EFFORT_LEVELS

FLOW_SPEC_FIELDS = frozenset(
    {
        "agent",
        "argument-hint",
        "args",
        "artifacts",
        "bare",
        "description",
        "dry_run",
        "effort",
        "max_agents",
        "max_ops",
        "model",
        "name",
        "pack",
        "prompt",
        "reactive",
        "save",
        "show_graph",
        "team_attach",
        "team_mode",
        "with_synthesis",
        "workers",
    }
    | {"bypass", "links", "permission_mode", "steps", "use", "yolo"}
)

_PRESERVE_DASHED = frozenset({"argument-hint"})


def normalize_flow_spec_keys(data: dict[Any, Any]) -> dict[str, Any]:
    """Normalize dashed top-level keys exactly as the CLI loader does."""
    normalized: dict[str, Any] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            raise ValueError(f"spec keys must be strings, got {type(key).__name__}")
        if key in _PRESERVE_DASHED or "-" not in key:
            normalized[key] = value
        else:
            normalized[key.replace("-", "_")] = value
    return normalized


def flow_spec_yaml_key(key: str) -> str:
    """Return the preferred YAML spelling for one canonical spec field."""
    if key in _PRESERVE_DASHED:
        return key
    return key.replace("_", "-")


def load_flow_spec(path: str | Path) -> dict[str, Any]:
    """Load and normalize one YAML or JSON flow spec."""
    spec_path = Path(path).expanduser()
    if not spec_path.is_file():
        raise ValueError(f"spec file not found: {spec_path}")
    try:
        text = spec_path.read_text()
        suffix = spec_path.suffix.lower()
        if suffix in (".yaml", ".yml"):
            import yaml

            data = yaml.safe_load(text) or {}
        elif suffix == ".json":
            import json

            data = json.loads(text)
        else:
            import yaml

            try:
                data = yaml.safe_load(text) or {}
            except Exception:
                import json

                data = json.loads(text)
    except Exception as exc:
        raise ValueError(f"failed to parse spec file {spec_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("spec file must contain a YAML/JSON object")
    return normalize_flow_spec_keys(data)


def validate_flow_args_schema(args_schema: Any) -> str | None:
    """Validate a playbook's declared argument schema."""
    if not isinstance(args_schema, dict):
        return f"spec field 'args' must be a dict, got {type(args_schema).__name__}"
    valid_types = {"str", "int", "float", "bool"}
    for name, spec in args_schema.items():
        if not isinstance(name, str) or not name.replace("_", "").isalnum():
            return f"args key {name!r} must be an alphanumeric identifier"
        if not isinstance(spec, dict):
            return f"args[{name!r}] must be a dict, got {type(spec).__name__}"
        type_str = spec.get("type", "str")
        if type_str not in valid_types:
            return f"args[{name!r}].type must be one of {sorted(valid_types)}, got {type_str!r}"
    return None


def validate_flow_spec_fields(spec: dict[str, Any]) -> str | None:
    """Validate the top-level fields shared by every flow-spec surface."""
    for key in spec:
        if key not in FLOW_SPEC_FIELDS:
            accepted = ", ".join(sorted(FLOW_SPEC_FIELDS))
            return f"unknown spec field {key!r}; accepted fields: {accepted}"

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
            return (
                f"spec field {key!r} must be in [0, 50] "
                f"(0 = no shared ceiling; reactive spawns are capped at 20), got {value}"
            )

    effort = spec.get("effort")
    if effort is not None:
        if not isinstance(effort, str):
            return f"spec field 'effort' must be a string, got {type(effort).__name__}"
        if effort not in EFFORT_LEVELS:
            allowed = sorted(EFFORT_LEVELS)
            return f"spec field 'effort' must be one of {allowed}, got {effort!r}"

    if "with_synthesis" in spec:
        value = spec["with_synthesis"]
        if not isinstance(value, bool | str):
            return (
                f"spec field 'with_synthesis' must be bool or str (model spec), "
                f"got {type(value).__name__}"
            )

    for bool_field in ("bare", "dry_run", "show_graph"):
        if bool_field in spec:
            value = spec[bool_field]
            if not isinstance(value, bool):
                return f"spec field {bool_field!r} must be a bool, got {type(value).__name__}"

    if "prompt" in spec:
        prompt = spec["prompt"]
        if not isinstance(prompt, str):
            return f"spec field 'prompt' must be a string, got {type(prompt).__name__}"
        if len(prompt) > MAX_SPEC_PROMPT_CHARS:
            return (
                f"spec field 'prompt' exceeds maximum length of {MAX_SPEC_PROMPT_CHARS} characters"
            )

    if "save" in spec:
        save = spec["save"]
        if not isinstance(save, str):
            return f"spec field 'save' must be a string, got {type(save).__name__}"

    for str_field in ("model", "agent", "team_mode", "team_attach", "reactive"):
        if str_field in spec:
            value = spec[str_field]
            if not isinstance(value, str):
                return f"spec field {str_field!r} must be a string, got {type(value).__name__}"

    if "artifacts" in spec:
        artifacts = spec["artifacts"]
        if artifacts is None:
            return "spec field 'artifacts' must be a dict, got NoneType"
        try:
            from lionagi.state.artifact_verifier import (
                validate_artifact_contract,
                warn_unknown_artifact_keys,
            )

            validate_artifact_contract(artifacts)
            warn_unknown_artifact_keys(
                artifacts,
                source="playbook",
                emit=logging.getLogger("lionagi.cli").warning,
            )
        except Exception as exc:
            return f"spec field 'artifacts' is invalid: {exc}"

    return None


def validate_flow_spec(spec: dict[str, Any]) -> str | None:
    """Run every check decidable from normalized spec content alone."""
    error = validate_flow_spec_fields(spec)
    if error is not None:
        return error
    if "args" in spec:
        return validate_flow_args_schema(spec["args"])
    return None
