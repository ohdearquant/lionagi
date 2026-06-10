# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Flow-spec validation and playbook argument injection helpers.

All public names are re-exported from the parent ``orchestrate/__init__.py``
so existing import paths remain stable.
"""

from __future__ import annotations

import argparse

from .._logging import log_error
from .._providers import add_common_cli_args  # noqa: F401  (re-exported for type stubs)


def _scan_argv_for_playbook_name(argv: list[str]) -> str | None:
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-p", "--playbook"):
            if i + 1 < len(argv):
                return argv[i + 1]
            return None
        if tok.startswith("--playbook="):
            return tok.split("=", 1)[1]
        i += 1
    return None


def _derive_args_schema_from_spec(spec: dict) -> dict:
    if isinstance(spec.get("args"), dict):
        schema: dict = {}
        for name, field in spec["args"].items():
            if not isinstance(field, dict):
                continue
            schema[name] = {
                "type": field.get("type", "str"),
                "default": field.get("default"),
                "help": field.get("help", ""),
            }
        return schema
    if spec.get("argument-hint"):
        return _parse_argument_hint(spec["argument-hint"])
    return {}


def inject_playbook_schema_into_parser(
    flow_parser: argparse.ArgumentParser, argv: list[str]
) -> dict:
    """Pre-scan argv for playbook; inject declared args as parser flags."""
    name = _scan_argv_for_playbook_name(argv)
    if not name:
        return {}
    path, err = _resolve_playbook_path(name)
    if err is not None:
        return {}  # Defer error reporting to run_orchestrate
    spec = _load_flow_spec(str(path))
    if not isinstance(spec, dict):
        return {}
    schema = _derive_args_schema_from_spec(spec)
    if not schema:
        return {}
    reserved: set[str] = set()
    for action in flow_parser._actions:
        for opt in getattr(action, "option_strings", ()):
            reserved.add(opt)
    resolved_schema: dict = {}
    for arg_name, field in schema.items():
        cli_flag = "--" + arg_name.replace("_", "-")
        if cli_flag in reserved:
            import logging as _logging

            _logging.getLogger("lionagi.cli").warning(
                "playbook arg %r (%s) collides with built-in flag; "
                "rename it in the playbook to use it",
                arg_name,
                cli_flag,
            )
            continue
        type_str = field.get("type", "str")
        help_text = field.get("help", "")
        if type_str == "bool":
            flow_parser.add_argument(
                cli_flag,
                dest=arg_name,
                action="store_true",
                default=None,
                help=help_text,
            )
        else:
            flow_parser.add_argument(
                cli_flag,
                dest=arg_name,
                default=None,
                help=help_text,
                metavar=type_str.upper(),
            )
        resolved_schema[arg_name] = field
    flow_parser.set_defaults(_playbook_args_schema=resolved_schema)
    return resolved_schema


def _resolve_playbook_path(name: str) -> tuple[object, str | None]:
    """Resolve a playbook NAME to (Path, None) or (None, error_message)."""
    from pathlib import Path

    from lionagi.libs.path_safety import validate_path_component

    if not name or not isinstance(name, str):
        return None, "playbook name must be a non-empty string"
    try:
        validate_path_component(name, label="playbook NAME")
    except ValueError:
        return (
            None,
            f"playbook NAME must be a bare identifier, got {name!r}. "
            "Use -f /abs/path.yaml for ad-hoc specs.",
        )
    root = Path("~/.lionagi/playbooks").expanduser()
    candidate = root / f"{name}.playbook.yaml"
    if not candidate.is_file():
        # Look for near-matches to suggest.
        suggestions = []
        if root.is_dir():
            for p in sorted(root.glob("*.playbook.yaml")):
                suggestions.append(p.stem.removesuffix(".playbook"))
        hint_text = (
            f" Available: {', '.join(suggestions[:10])}"
            if suggestions
            else " No playbooks found in ~/.lionagi/playbooks/"
        )
        return None, f"playbook not found: {candidate}.{hint_text}"
    try:
        resolved_root = root.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
        resolved_candidate.relative_to(resolved_root)
    except (OSError, ValueError):
        return (
            None,
            f"playbook {name!r} resolves outside playbooks root (symlink escape blocked)",
        )
    return candidate, None


def _parse_argument_hint(hint: str) -> dict:
    """Parse CC-style argument-hint string into an args schema."""
    import re

    schema: dict = {}
    pattern = re.compile(r"\[--([a-zA-Z][a-zA-Z0-9_-]*)(?:\s+([A-Z_][A-Z0-9_]*))?\]")
    for match in pattern.finditer(hint or ""):
        flag_name = match.group(1).replace("-", "_")
        value_placeholder = match.group(2)
        if value_placeholder is None:
            schema[flag_name] = {"type": "bool", "default": False}
        else:
            schema[flag_name] = {"type": "str", "default": None}
    return schema


def _validate_args_schema(args_schema) -> str | None:
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


def _coerce_arg_value(name: str, value, type_str: str):
    if value is None:
        return None, None
    if type_str == "bool":
        return bool(value), None
    if type_str == "str":
        return str(value), None
    try:
        if type_str == "int":
            return int(value), None
        if type_str == "float":
            return float(value), None
    except (TypeError, ValueError):
        return (
            None,
            f"arg --{name.replace('_', '-')} expected {type_str}, got {value!r}",
        )
    return value, None


def _load_flow_spec(path: str) -> dict | None:
    from pathlib import Path

    p = Path(path).expanduser()
    if not p.is_file():
        log_error(f"spec file not found: {p}")
        return None
    text = p.read_text()
    suffix = p.suffix.lower()
    try:
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
    except Exception as e:
        log_error(f"failed to parse spec file {p}: {e}")
        return None

    if not isinstance(data, dict):
        log_error("spec file must contain a YAML/JSON object")
        return None
    preserve_dashed = {"argument-hint"}
    normalized: dict = {}
    for key, value in data.items():
        if key in preserve_dashed or "-" not in key:
            normalized[key] = value
        else:
            normalized[key.replace("-", "_")] = value
    return normalized


def _validate_spec_fields(spec: dict) -> str | None:
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
        from .._providers import EFFORT_LEVELS

        if not isinstance(effort, str):
            return f"spec field 'effort' must be a string, got {type(effort).__name__}"
        if effort not in EFFORT_LEVELS:
            allowed = sorted(EFFORT_LEVELS)
            return f"spec field 'effort' must be one of {allowed}, got {effort!r}"

    if "with_synthesis" in spec:
        val = spec["with_synthesis"]
        if not isinstance(val, bool | str):
            return (
                f"spec field 'with_synthesis' must be bool or str (model spec), "
                f"got {type(val).__name__}"
            )

    for bool_field in ("bare", "dry_run", "show_graph"):
        if bool_field in spec:
            val = spec[bool_field]
            if not isinstance(val, bool):
                return f"spec field {bool_field!r} must be a bool, got {type(val).__name__}"

    if "prompt" in spec:
        prompt = spec["prompt"]
        if not isinstance(prompt, str):
            return f"spec field 'prompt' must be a string, got {type(prompt).__name__}"
        if len(prompt) > 8192:
            return "spec field 'prompt' exceeds maximum length of 8192 characters"

    if "save" in spec:
        save = spec["save"]
        if not isinstance(save, str):
            return f"spec field 'save' must be a string, got {type(save).__name__}"

    for str_field in ("model", "agent", "team_mode", "team_attach", "reactive"):
        if str_field in spec:
            val = spec[str_field]
            if not isinstance(val, str):
                return f"spec field {str_field!r} must be a string, got {type(val).__name__}"

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
            import logging as _logging

            _cli_log = _logging.getLogger("lionagi.cli")
            warn_unknown_artifact_keys(
                artifacts,
                source="playbook",
                emit=_cli_log.warning,
            )
        except Exception as exc:
            return f"spec field 'artifacts' is invalid: {exc}"

    return None


def _interpolate_prompt(template: str, positional: str | None, playbook_args: dict) -> str:
    """Interpolate {input} + playbook args into the prompt template."""
    if not template:
        return positional or ""

    ctx: dict = dict(playbook_args)
    if positional is not None:
        ctx["input"] = positional

    import re

    placeholders = set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", template))
    if not placeholders and positional is not None:
        return template + "\n\n" + positional

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in ctx:
            return str(ctx[key])
        return match.group(0)

    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", _sub, template)
