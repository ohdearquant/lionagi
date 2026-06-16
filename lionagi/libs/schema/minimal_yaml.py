from __future__ import annotations

from typing import Any

import orjson
import yaml

# --- YAML Dumper with minimal, readable settings --------------------------------


class MinimalDumper(yaml.SafeDumper):
    # Disable anchors/aliases (&id001, *id001) for repeated objects.
    def ignore_aliases(self, data: Any) -> bool:  # type: ignore[override]
        return True


def _represent_str(dumper: yaml.SafeDumper, data: str):
    # Use block scalars for multiline text; plain style otherwise.
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


MinimalDumper.add_representer(str, _represent_str)

# --- Optional pruning of empty values -------------------------------------------


def _is_empty(x: Any) -> bool:
    """True for None, blank strings, and empty containers; keeps 0 and False."""
    if x is None:
        return True
    if isinstance(x, str):
        return x.strip() == ""
    if isinstance(x, dict):
        return len(x) == 0
    if isinstance(x, (list, tuple, set)):
        return len(x) == 0
    # Keep numbers (including 0) and booleans (including False)
    return False


def _prune(x: Any) -> Any:
    """Recursively remove empty leaves and empty containers produced thereby."""
    if isinstance(x, dict):
        pruned = {k: _prune(v) for k, v in x.items() if not _is_empty(v)}
        # Remove keys that became empty after recursion
        return {k: v for k, v in pruned.items() if not _is_empty(v)}
    if isinstance(x, list):
        pruned_list = [_prune(v) for v in x if not _is_empty(v)]
        return [v for v in pruned_list if not _is_empty(v)]
    if isinstance(x, tuple):
        pruned_list = [_prune(v) for v in x if not _is_empty(v)]
        return tuple(v for v in pruned_list if not _is_empty(v))
    if isinstance(x, set):
        pruned_set = {_prune(v) for v in x if not _is_empty(v)}
        return {v for v in pruned_set if not _is_empty(v)}
    return x


# --- Public API ------------------------------------------------------------------


def minimal_yaml(
    value: Any,
    *,
    drop_empties: bool = True,
    indent: int = 2,
    line_width: int = 2**31 - 1,  # avoid PyYAML inserting line-wraps
    sort_keys: bool = False,
) -> str:
    """Serialize Python value to minimal YAML (block style, no aliases, optional empty-pruning)."""
    if isinstance(value, str):
        value = orjson.loads(value)

    data = _prune(value) if drop_empties else value
    return yaml.dump(
        data,
        Dumper=MinimalDumper,
        default_flow_style=False,  # block style
        sort_keys=sort_keys,  # preserve insertion order
        allow_unicode=True,
        indent=indent,
        width=line_width,
    )
