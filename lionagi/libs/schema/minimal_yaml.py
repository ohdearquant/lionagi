from __future__ import annotations

import re
from typing import Any

import yaml

from lionagi.ln._json_load import json_loads

# YAML Dumper with minimal, readable settings

# Characters a literal block cannot carry as written: the ones PyYAML's emitter will not print
# (tab and line feed aside, which a literal block holds as they are), the carriage return, which a
# YAML reader takes as a line break, so a lone CR would end the block early, and the next-line
# character, which a reader turns into a line feed. The line and paragraph separators stay: the
# emitter indents the text after them and the reader keeps them as they are.
_NOT_BLOCK_SAFE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff\ufeff\ufffe\uffff]")
_NON_ASCII = re.compile(r"[^\x00-\x7f]")
# Whitespace other than a line feed, followed by nothing but line feeds. Callers strip the rendered
# document, so a block that ends it would lose that whitespace.
_ENDS_IN_SPACE = re.compile(r"[^\S\n]\n*\Z")


class MinimalDumper(yaml.SafeDumper):
    # Disable anchors/aliases (&id001, *id001) for repeated objects.
    def ignore_aliases(self, data: Any) -> bool:  # type: ignore[override]
        return True

    # PyYAML refuses a literal block for text holding a tab or a space before a line break,
    # and falls back to one double-quoted line with every newline escaped; it also writes a
    # block for text holding a next-line character, which loads back as a line feed. Decide
    # from the characters instead: a block whenever every character is safe to write as is
    # and the text does not end in whitespace other than line feeds, double quotes otherwise.
    # Single quotes are never used for such text: a reader folds a line break inside them
    # into a space.
    def analyze_scalar(self, scalar: str):  # type: ignore[override]
        analysis = super().analyze_scalar(scalar)
        if analysis.multiline:
            analysis.allow_block = (
                not _NOT_BLOCK_SAFE.search(scalar)
                and not _ENDS_IN_SPACE.search(scalar)
                and (self.allow_unicode or not _NON_ASCII.search(scalar))
            )
            analysis.allow_single_quoted = False
        return analysis


def _represent_str(dumper: yaml.SafeDumper, data: str):
    # Use block scalars for multiline text; plain style otherwise.
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


MinimalDumper.add_representer(str, _represent_str)

# Optional pruning of empty values


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


# Public API


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
        value = json_loads(value)

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
