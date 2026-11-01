# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The core fence extractor preserves the captured LNDL rules."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lionagi.libs.schema.extract_code_block import extract_fenced_blocks
from lionagi.operations.parse.parse import _extract_lndl
from lionhub.lndl.extract import extract_lndl_blocks

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "fenced_blocks.json").read_text(encoding="utf-8")
)["cases"]
BY_ID = {case["id"]: case for case in CORPUS}


@pytest.mark.parametrize("case", CORPUS, ids=[case["id"] for case in CORPUS])
def test_base_corpus(case):
    assert extract_lndl_blocks(case["text"]) == case["blocks"]
    assert extract_fenced_blocks(case["text"], "lndl") == case["blocks"]


def test_argument_language_is_case_insensitive():
    case = BY_ID["fence-argument-language-case-control"]
    assert case["blocks"]
    assert extract_fenced_blocks(case["text"], "LNDL") == case["blocks"]
    assert extract_fenced_blocks(case["text"], "LnDl") == case["blocks"]


def test_another_language_uses_the_same_fences():
    case = BY_ID["fence-generic-python-language-control"]
    assert case["blocks"] == []
    assert extract_fenced_blocks(case["text"], "pYtHoN") == ['print("hello")\n']
    assert extract_fenced_blocks(case["text"], "lndl") == []


@pytest.mark.parametrize(
    "case_id",
    [
        "fence-multiple-block-source-order",
        "fence-plain-reply-no-block",
        "fence-empty-block",
        "fence-whitespace-input",
    ],
    ids=["source-order", "no-block", "empty-block", "unchanged-whitespace"],
)
def test_parse_joins_blocks_or_returns_the_unchanged_reply(case_id):
    case = BY_ID[case_id]
    expected = "\n\n".join(case["blocks"]) if case["blocks"] else case["text"]
    assert _extract_lndl(case["text"], None) == expected


def test_all_three_legacy_exports_are_one_function():
    import lionagi
    import lionhub.lndl

    assert lionhub.lndl.extract_lndl_blocks is extract_lndl_blocks
    assert lionagi.extract_lndl_blocks is extract_lndl_blocks


@pytest.mark.parametrize("first", ["parse", "legacy"], ids=["parse-no-lndl", "legacy-import"])
def test_fresh_parse_and_legacy_import_boundary(first):
    script = r"""
import importlib
import json
import sys
from pathlib import Path

root, first = Path(sys.argv[1]), sys.argv[2]
core_name = "lionagi.libs.schema.extract_code_block"
samples = json.loads((root / "tests/fixtures/fenced_blocks.json").read_text())["cases"]
sample = next(case for case in samples if case["id"] == "existing-extract-multiple-lndl-blocks")
assert len(sample["blocks"]) > 1
assert not any(n == "lionhub.lndl" or n.startswith("lionhub.lndl.") for n in sys.modules)
if first == "parse":
    parser = importlib.import_module("lionagi.operations.parse.parse")
    result = parser._extract_lndl(sample["text"], None)
    assert result == "\n\n".join(sample["blocks"])
    loaded = sorted(n for n in sys.modules if n == "lionhub.lndl" or n.startswith("lionhub.lndl."))
    assert not loaded, loaded
    assert Path(parser.__file__).resolve() == root / "lionagi/operations/parse/parse.py"
else:
    legacy = importlib.import_module("lionhub.lndl.extract")
    assert legacy.extract_lndl_blocks(sample["text"]) == sample["blocks"]
    assert Path(legacy.__file__).resolve() == root / "lionhub/src/lionhub/lndl/extract.py"
    loaded = sorted(n for n in sys.modules if n == "lionhub.lndl" or n.startswith("lionhub.lndl."))
    assert "lionhub.lndl.extract" in loaded
core = sys.modules[core_name]
assert core.__spec__.name == core_name
assert Path(core.__file__).resolve() == root / "lionagi/libs/schema/extract_code_block.py"
print(json.dumps({"first": first, "lndl_loaded": bool(loaded),
                  "core_loaded": core_name in sys.modules}))
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script, str(ROOT), first],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (
        f"child stdout:\n{completed.stdout}\nchild stderr:\n{completed.stderr}"
    )
    assert json.loads(completed.stdout) == {
        "first": first,
        "lndl_loaded": first == "legacy",
        "core_loaded": True,
    }
