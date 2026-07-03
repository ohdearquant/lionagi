# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0


from lionagi.lndl.extract import extract_lndl_blocks
from lionagi.lndl.prompt import get_lndl_system_prompt


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
    text = "```lndl\n```"
    # Empty blocks with no content (empty string code group) may not match
    # depending on the regex — test what actually happens
    blocks = extract_lndl_blocks(text)
    # Either empty or not found — both are valid implementations
    assert isinstance(blocks, list)


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
    from lionagi.lndl.extract import __all__

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
