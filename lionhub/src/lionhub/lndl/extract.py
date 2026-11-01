# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Extract ```lndl code blocks from LLM responses."""

from lionagi.libs.schema.extract_code_block import extract_fenced_blocks


def extract_lndl_blocks(text: str) -> list[str]:
    """Extract all ```lndl fenced code blocks from text, in order, with CRLF line ends read as LF."""
    return extract_fenced_blocks(text, "lndl")


__all__ = ("extract_lndl_blocks",)
