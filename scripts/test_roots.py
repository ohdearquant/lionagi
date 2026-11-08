#!/usr/bin/env python3
"""Read the configured pytest roots without importing the project."""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


class TestRootConfigError(ValueError):
    """The project's test paths cannot safely select the intended suite."""


def configured_test_roots(project_root: Path, *, require_existing: bool = True) -> tuple[str, ...]:
    """Return checked repository-relative paths from pytest's testpaths setting."""
    root = project_root.resolve()
    try:
        with (root / "pyproject.toml").open("rb") as stream:
            config = tomllib.load(stream)
        paths = config["tool"]["pytest"]["ini_options"]["testpaths"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise TestRootConfigError("cannot read pytest testpaths from pyproject.toml") from exc
    if not isinstance(paths, list) or not paths:
        raise TestRootConfigError("pytest testpaths must be a nonempty list")
    result = []
    for value in paths:
        if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
            raise TestRootConfigError("each pytest testpath must be a nonempty relative directory")
        path = Path(value)
        if path.is_absolute() or value in (".", "..") or ".." in path.parts:
            raise TestRootConfigError(f"invalid pytest testpath {value!r}")
        try:
            target = (root / path).resolve()
        except (OSError, RuntimeError) as exc:
            raise TestRootConfigError(f"cannot resolve pytest testpath {value!r}") from exc
        if (
            not target.is_relative_to(root)
            or target == root
            or (require_existing and not target.is_dir())
        ):
            raise TestRootConfigError(
                f"pytest testpath is not a directory inside the project: {value!r}"
            )
        name = path.as_posix().rstrip("/")
        if name in result:
            raise TestRootConfigError(f"duplicate pytest testpath {name!r}")
        result.append(name)
    return tuple(result)


def main() -> int:
    try:
        roots = configured_test_roots(Path(__file__).resolve().parents[1])
    except TestRootConfigError as exc:
        print(f"test roots: {exc}", file=sys.stderr)
        return 1
    for root in roots:
        print(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
