#!/usr/bin/env python3
"""Convert a pytest JUnit report into compact CI flake records."""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from pathlib import Path

_EXCEPTION_LINE = re.compile(
    r"\b(?:AssertionError|ExceptionGroup|[A-Za-z_][\w.]*Error|Failed)(?::|$)"
)
_XDIST_WORKER_CRASH = re.compile(r"worker ['\"]?gw\d+['\"]? crashed while running", re.I)
_MAX_SIGNATURE_LENGTH = 500


def _nodeid(testcase: ET.Element) -> str:
    file_name = testcase.get("file")
    class_name = testcase.get("classname", "")
    test_name = testcase.get("name", "<unknown>")

    if file_name:
        file_name = file_name.replace("\\", "/")
        module_name = file_name.removesuffix(".py").replace("/", ".")
        class_suffix = class_name.removeprefix(module_name).lstrip(".")
        parts = [file_name]
        if class_suffix:
            parts.extend(class_suffix.split("."))
        parts.append(test_name)
        return "::".join(parts)

    parts = class_name.split(".") if class_name else []
    class_index = next(
        (index for index, part in enumerate(parts) if part[:1].isupper()),
        len(parts),
    )
    module_parts = parts[:class_index]
    class_parts = parts[class_index:]
    file_part = "/".join(module_parts) + ".py" if module_parts else "<unknown>"
    return "::".join([file_part, *class_parts, test_name])


def _clean_signature(value: str) -> str:
    if _XDIST_WORKER_CRASH.search(value):
        return "xdist worker crashed while running test"
    return " ".join(value.split())[:_MAX_SIGNATURE_LENGTH]


def _signature(detail: ET.Element) -> str:
    text = detail.text or ""
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("E "):
            candidate = stripped[1:].strip()
            if candidate:
                return _clean_signature(candidate)

    for line in text.splitlines():
        candidate = line.strip(" |+")
        if candidate and _EXCEPTION_LINE.search(candidate):
            return _clean_signature(candidate)

    message = detail.get("message", "").strip()
    if message:
        return _clean_signature(message)

    for line in reversed(text.splitlines()):
        if line.strip():
            return _clean_signature(line)
    return detail.tag


def records_from_junit(
    junit_path: Path,
    *,
    matrix_leg: str,
    run_id: str,
    attempt: int,
) -> Iterable[dict[str, object]]:
    root = ET.parse(junit_path).getroot()
    for testcase in root.iter("testcase"):
        detail = testcase.find("failure")
        if detail is None:
            detail = testcase.find("error")
        if detail is None:
            continue
        yield {
            "schema_version": 1,
            "nodeid": _nodeid(testcase),
            "matrix_leg": matrix_leg,
            "signature": _signature(detail),
            "run_id": run_id,
            "attempt": attempt,
        }


def write_records(records: Iterable[dict[str, object]], output: Path) -> int:
    materialized = list(records)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as stream:
        for record in materialized:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
    return len(materialized)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--matrix-leg", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", type=int, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        records = list(
            records_from_junit(
                args.junit,
                matrix_leg=args.matrix_leg,
                run_id=args.run_id,
                attempt=args.attempt,
            )
        )
    except (OSError, ET.ParseError) as exc:
        records = [
            {
                "schema_version": 1,
                "nodeid": "<pytest-session>",
                "matrix_leg": args.matrix_leg,
                "signature": _clean_signature(f"JUnit report unavailable: {exc}"),
                "run_id": args.run_id,
                "attempt": args.attempt,
            }
        ]

    if not records:
        records.append(
            {
                "schema_version": 1,
                "nodeid": "<pytest-session>",
                "matrix_leg": args.matrix_leg,
                "signature": "pytest failed without a test-level JUnit failure",
                "run_id": args.run_id,
                "attempt": args.attempt,
            }
        )

    count = write_records(records, args.output)
    print(f"wrote {count} flake failure record(s) to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
