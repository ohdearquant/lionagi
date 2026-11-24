# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Read one JSON file a caller names, once, under the rules of ADR-0048 Amendment 1 H2.

The file is named by an absolute path, opened once with ``O_NOFOLLOW`` and checked with
``fstat`` on that descriptor, so the bytes judged are the bytes kept. It must be a regular
file no larger than the caller's limit (a larger file is refused, never truncated), and
UTF-8 JSON whose top level is an object, with ``NaN`` and the infinities refused and a name
repeated inside any object refused.

A refusal names the rule it broke and the path as given, and carries no bytes from the
file. Callers supply the label their messages use and the exception type they raise.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ("JsonFile", "read_json_file")


class _RefusedError(ValueError):
    """Raised from inside the JSON parser; carries a reason with no file content."""


@dataclass(frozen=True)
class JsonFile:
    """The accepted bytes of a JSON file and what was read from them."""

    path: str
    resolved_path: str
    data: bytes
    sha256: str
    document: dict[str, Any]


def _refuse_constant(name: str) -> Any:
    raise _RefusedError(f"uses {name}, which JSON does not define")


def _refuse_repeated_names(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise _RefusedError("repeats a name inside an object")
    return obj


def read_json_file(
    path: Any,
    *,
    label: str,
    max_bytes: int,
    error: Callable[[str, str], Exception],
) -> JsonFile:
    """Read the JSON object at *path* once.

    *label* names the file in every message (``hooks_from``, ``graph``). *error* builds the
    exception raised for a refusal from the rule's name and the message: ``absolute-path``,
    ``no-symlink``, ``open``, ``regular-file``, ``size`` or ``json``.
    """
    if not isinstance(path, str) or not path:
        raise error("absolute-path", f"{label} must be an absolute path")
    if path == "-":
        raise error("absolute-path", f"{label} cannot be '-': the server reads the file itself")
    given = Path(path)
    if not given.is_absolute():
        raise error(
            "absolute-path",
            f"{label} must be an absolute path, got {path!r}: the server reads it, so a "
            "relative path would resolve against the server's directory",
        )
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise error("no-symlink", f"{label} needs O_NOFOLLOW, which this platform does not provide")
    flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise error(
                "no-symlink",
                f"{label} {path!r} is a symbolic link; name the file it points to",
            ) from None
        raise error("open", f"could not open {label} {path!r}: {exc.strerror}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise error("regular-file", f"{label} {path!r} is not a regular file")
        chunks: list[bytes] = []
        total = 0
        while total <= max_bytes:
            chunk = os.read(fd, max_bytes + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > max_bytes:
        raise error("size", f"{label} {path!r} is larger than {max_bytes} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise error("json", f"{label} {path!r} is not UTF-8 text") from None
    try:
        document = json.loads(
            text, parse_constant=_refuse_constant, object_pairs_hook=_refuse_repeated_names
        )
    except _RefusedError as exc:
        raise error("json", f"{label} {path!r} {exc}") from None
    except json.JSONDecodeError as exc:
        raise error(
            "json",
            f"{label} {path!r} is not valid JSON (line {exc.lineno}, column {exc.colno})",
        ) from None
    except RecursionError:
        raise error("json", f"{label} {path!r} nests too deeply") from None
    if not isinstance(document, dict):
        raise error("json", f"{label} {path!r} must hold a JSON object at the top")
    return JsonFile(
        path=path,
        resolved_path=str(given.parent.resolve() / given.name),
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        document=document,
    )
