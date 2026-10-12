from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JsonRead:
    """Result of reading one live JSON file, distinguishing "the file is not
    there" from "the file is there but could not be parsed".

    ``value`` is the parsed object, or ``None`` if there was nothing to
    parse. ``available`` is ``False`` only when the file exists but reading
    or parsing it failed - a missing file is a normal, available empty
    read, not a failure. ``error`` carries the diagnostic when
    ``available`` is ``False``.
    """

    value: dict[str, Any] | None
    available: bool
    error: str | None = None


class _RepeatedKeyError(ValueError):
    pass


def _refuse_repeated_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len({key for key, _ in pairs}) != len(pairs):
        # the key is not named: in a file that holds secrets it may be one
        raise _RepeatedKeyError("the file writes a key twice in one object")
    return dict(pairs)


def read_json_file_checked(path: Path, *, unique_keys: bool = False) -> JsonRead:
    """Read *path* as JSON. With *unique_keys*, a file that writes a key twice in
    one object is unavailable, where a parse would keep the last value alone."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return JsonRead(value=None, available=True)
    except UnicodeDecodeError:
        # the codec's message quotes the byte it could not decode, which may be part
        # of a secret the file holds
        _log.warning("could not read %s: the file is not UTF-8", path)
        return JsonRead(value=None, available=False, error="the file is not UTF-8")
    except OSError as exc:
        _log.warning("could not read %s: %s", path, exc)
        return JsonRead(value=None, available=False, error=str(exc))
    try:
        value = json.loads(text, object_pairs_hook=_refuse_repeated_keys if unique_keys else None)
    except _RepeatedKeyError as exc:
        _log.warning("could not read %s: %s", path, exc)
        return JsonRead(value=None, available=False, error=str(exc))
    except (ValueError, RecursionError) as exc:
        # JSONDecodeError is a ValueError, as is an integer too long to convert;
        # a document nested deeper than the interpreter's recursion limit raises
        # RecursionError
        _log.warning("invalid JSON in %s: %s", path, exc)
        return JsonRead(value=None, available=False, error=str(exc))
    return JsonRead(value=value, available=True)


def read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def parse_json_col(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value
