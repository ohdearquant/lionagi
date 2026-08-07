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


def read_json_file_checked(path: Path) -> JsonRead:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return JsonRead(value=None, available=True)
    except OSError as exc:
        _log.warning("could not read %s: %s", path, exc)
        return JsonRead(value=None, available=False, error=str(exc))
    try:
        return JsonRead(value=json.loads(text), available=True)
    except json.JSONDecodeError as exc:
        _log.warning("invalid JSON in %s: %s", path, exc)
        return JsonRead(value=None, available=False, error=str(exc))


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
