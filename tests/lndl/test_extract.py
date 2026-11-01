# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import pytest

from lionhub.lndl.extract import extract_lndl_blocks
from lionhub.lndl.prompt import get_lndl_system_prompt


def test_extract_single_lndl_block():
    text = "```lndl\n<lvar x>hello</lvar>\nOUT{x: [x]}\n```"
    blocks = extract_lndl_blocks(text)
    assert len(blocks) == 1
    assert "<lvar x>hello</lvar>" in blocks[0]


def test_extract_no_blocks():
    text = "No code blocks here."
    blocks = extract_lndl_blocks(text)
    assert blocks == []


def test_extract_ignores_non_lndl_blocks():
    text = "```python\nprint('hello')\n```\n```bash\necho hi\n```"
    blocks = extract_lndl_blocks(text)
    assert blocks == []


def test_extract_multiple_lndl_blocks():
    text = "First:\n```lndl\nblock1\n```\nSome text\n```lndl\nblock2\n```"
    blocks = extract_lndl_blocks(text)
    assert len(blocks) == 2
    assert "block1" in blocks[0]
    assert "block2" in blocks[1]


def test_extract_mixed_languages():
    text = (
        "```python\ncode\n```\n```lndl\nlndl_code\n```\n```json\n{}\n```\n```lndl\nmore_lndl\n```"
    )
    blocks = extract_lndl_blocks(text)
    assert len(blocks) == 2
    assert "lndl_code" in blocks[0]
    assert "more_lndl" in blocks[1]


def test_extract_tilde_fence():
    text = "~~~lndl\n<lvar x>val</lvar>\n~~~"
    blocks = extract_lndl_blocks(text)
    assert len(blocks) == 1
    assert "<lvar x>val</lvar>" in blocks[0]


def test_extract_empty_block():
    assert extract_lndl_blocks("```lndl\n```") == [""]


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("```lndl\r\n<lvar x>v</lvar>\r\n```", "<lvar x>v</lvar>\n"),
        ("```lndl\r\n<lvar x>v</lvar>\r\n```\r\n", "<lvar x>v</lvar>\n"),
        ("```lndl\r\n<lvar x>line 1\r\nline 2</lvar>\r\n```", "<lvar x>line 1\nline 2</lvar>\n"),
        ("  ```lndl\n  <lvar x>v</lvar>\n  ```", "  <lvar x>v</lvar>\n"),
        ("\t~~~lndl\n<lvar x>v</lvar>\n\t~~~", "<lvar x>v</lvar>\n"),
        ("````lndl\n<lvar x>v</lvar>\n````", "<lvar x>v</lvar>\n"),
    ],
    ids=[
        "crlf",
        "crlf-trailing-newline",
        "crlf-multiline-value",
        "indented",
        "indented-tildes",
        "four-backticks",
    ],
)
def test_extract_reads_crlf_indented_and_longer_fences(text, code):
    assert extract_lndl_blocks(text) == [code]


def test_a_longer_fence_holds_a_shorter_one_as_content():
    text = "````lndl\n<lvar x>```\nquoted\n```</lvar>\n````"
    assert extract_lndl_blocks(text) == ["<lvar x>```\nquoted\n```</lvar>\n"]


def test_a_block_closes_only_on_its_own_fence():
    # A shorter or different fence inside the block is content, and a block whose
    # own fence never comes back is not a block.
    assert extract_lndl_blocks("````lndl\n<lvar x>v</lvar>\n```") == []
    assert extract_lndl_blocks("```lndl\n<lvar x>v</lvar>\n~~~") == []


def test_a_block_closes_only_at_its_own_indentation():
    # A block opened at the margin keeps an indented fence inside a value as content.
    text = "```lndl\n<lvar code>\n  ```python\n  print(1)\n  ```\n</lvar>\n```"
    assert extract_lndl_blocks(text) == ["<lvar code>\n  ```python\n  print(1)\n  ```\n</lvar>\n"]
    # An indented block closes on its own indented fence, not on one at the margin.
    assert extract_lndl_blocks("  ```lndl\n  <lvar x>v</lvar>\n```") == []


def test_a_block_quoted_inside_another_fence_is_not_extracted():
    text = "```markdown\n```lndl\n<lvar x>v</lvar>\n```\n```"
    assert extract_lndl_blocks(text) == []


def test_extract_preserves_content():
    content = "<lvar Report.title t>My Title</lvar>\nOUT{report: [t]}"
    text = f"```lndl\n{content}\n```"
    blocks = extract_lndl_blocks(text)
    assert len(blocks) == 1
    assert blocks[0].strip() == content


def test_extract_lndl_case_insensitive():
    # 'LNDL' uppercase matches because lang.lower() is compared
    text = "```LNDL\nsome code\n```"
    blocks = extract_lndl_blocks(text)
    # The extractor lowercases the lang before comparing, so LNDL matches
    assert len(blocks) == 1
    assert "some code" in blocks[0]


def test_all_exports():
    from lionhub.lndl.extract import __all__

    assert "extract_lndl_blocks" in __all__


def test_prompt_instructs_fenced_output_and_extractor_matches_it():
    """The prompt must tell the model to fence its LNDL in ```lndl, and a
    response following that instruction must be extractable."""
    prompt = get_lndl_system_prompt()
    assert "```lndl" in prompt

    response = "Some reasoning here.\n\n```lndl\n<lvar a>hello</lvar>\nOUT{a}\n```\n"
    blocks = extract_lndl_blocks(response)
    assert len(blocks) == 1
    assert "<lvar a>hello</lvar>" in blocks[0]
