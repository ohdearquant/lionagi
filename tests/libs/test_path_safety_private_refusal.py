# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Path refusals preserve a reason without echoing caller-provided values."""

import pytest

from lionagi.libs.path_safety import check_path_safe, check_paths_safe, contain_paths_in_root


def _bad_path(kind):
    marker = "sensitive fixture content"
    return {
        "nul": f"{marker}\x00file",
        "traversal": f"safe/../{marker}",
        "absolute": f"/{marker}/file",
        "windows": f"C:/{marker}/file",
    }[kind], marker


@pytest.mark.parametrize(
    ("kind", "reason"),
    [("nul", "NUL"), ("traversal", "traversal"), ("absolute", "absolute"), ("windows", "Windows")],
)
@pytest.mark.parametrize("strip_at", [False, True], ids=["plain", "at-prefix"])
def test_refusal_names_field_and_reason_without_echoing_path(kind, reason, strip_at):
    value, marker = _bad_path(kind)
    if strip_at:
        value = "@" + value
    message = None
    try:
        check_path_safe(value, "path_field", strip_at=strip_at)
    except ValueError as error:
        message = str(error)
    if message is None:
        pytest.fail("unsafe path was accepted", pytrace=False)
    if marker in message or value in message or repr(value) in message:
        pytest.fail("path payload appeared in the refusal", pytrace=False)
    assert "path_field" in message
    assert reason in message


def test_batch_refusal_does_not_expose_the_offending_value():
    value, marker = _bad_path("traversal")
    message = None
    try:
        check_paths_safe(["safe.txt", value], "path_fields")
    except ValueError as error:
        message = str(error)
    if message is None:
        pytest.fail("unsafe path was accepted", pytrace=False)
    if marker in message or value in message or repr(value) in message:
        pytest.fail("path payload appeared in the batch refusal", pytrace=False)
    assert "path_fields" in message and "traversal" in message


def test_safe_values_and_allowed_absolute_paths_keep_their_identity():
    relative = "@folder/file.txt"
    absolute = "/folder/file.txt"
    assert check_path_safe(relative, "path_field", strip_at=True) is relative
    assert check_path_safe(absolute, "path_field", reject_absolute=False) is absolute
    assert check_path_safe("C:/file.txt", "path_field", reject_absolute=False) == "C:/file.txt"


@pytest.mark.parametrize("strip_at", [False, True], ids=["plain", "at-prefix"])
@pytest.mark.parametrize("surface", ["str", "repr", "chain"])
def test_symlink_escape_error_does_not_expose_input(tmp_path, strip_at, surface):
    marker = "sensitive fixture content"
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    (repo / "alias").symlink_to(outside, target_is_directory=True)
    value = ("@" if strip_at else "") + "alias/" + marker
    error = None
    try:
        contain_paths_in_root([value], repo.resolve(), "path_field", strip_at=strip_at)
    except ValueError as caught:
        error = caught
    if error is None:
        pytest.fail("symlink escape was accepted", pytrace=False)
    if surface == "str":
        leaked = marker in str(error)
    elif surface == "repr":
        leaked = marker in repr(error)
    else:
        pending = [error]
        seen = set()
        leaked = False
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            leaked = leaked or marker in str(current) or marker in repr(current)
            pending.extend(x for x in (current.__cause__, current.__context__) if x is not None)
    if leaked:
        pytest.fail("symlink error surface retains caller input", pytrace=False)


def test_unresolvable_symlink_preserves_error_category_without_input(tmp_path):
    marker = "sensitive fixture content"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / marker).symlink_to(marker, target_is_directory=True)
    error = None
    try:
        contain_paths_in_root([marker + "/file"], repo.resolve(), "path_field")
    except (OSError, RuntimeError) as caught:
        error = caught
    if error is None:
        pytest.fail("unresolvable symlink was accepted", pytrace=False)
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if marker in str(current) or marker in repr(current):
            pytest.fail("resolution error retains caller input", pytrace=False)
        pending.extend(x for x in (current.__cause__, current.__context__) if x is not None)
