# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.lndl.normalize.normalize_lndl_text."""

from lionagi.lndl.normalize import normalize_lndl_text


class TestNormalizeLndlText:
    def test_curly_brace_lvar(self):
        text = "{lvar x}value{/lvar}"
        normalized = normalize_lndl_text(text)
        assert "<lvar" in normalized
        assert "{lvar" not in normalized

    def test_curly_brace_lact(self):
        text = "{lact x}fn(a='b'){/lact}"
        normalized = normalize_lndl_text(text)
        assert "<lact" in normalized

    def test_xml_attributes_cleaned(self):
        text = '<lvar name="title" type="str" t>'
        normalized = normalize_lndl_text(text)
        # XML attrs should be stripped; 't' alias should remain
        assert 'name="title"' not in normalized
        assert "t" in normalized

    def test_note_namespace_lowercased(self):
        text = "<lvar Note.draft d>some text</lvar>"
        normalized = normalize_lndl_text(text)
        assert "note.draft" in normalized or "<lvar note." in normalized

    def test_passthrough_normal_text(self):
        text = "<lvar Report.title t>Title</lvar>"
        normalized = normalize_lndl_text(text)
        assert "Report.title" in normalized
        assert "<lvar" in normalized

    def test_empty_string(self):
        result = normalize_lndl_text("")
        assert result == ""

    def test_fenced_block_preferred(self):
        """When the model wraps LNDL in ```lndl fences, prefer the fenced content."""
        text = "some preamble\n```lndl\n<lvar x>1</lvar>\nOUT{x: [x]}\n```\ntrailer"
        normalized = normalize_lndl_text(text)
        assert "<lvar x>1</lvar>" in normalized
        assert "preamble" not in normalized

    def test_missing_gt_repaired(self):
        """<lact alias fn(args)</lact> (missing closing >) gets repaired."""
        text = "<lact myalias multiply(x=1, y=2)</lact>\nOUT{myalias}"
        result = normalize_lndl_text(text)
        assert "<lact myalias>multiply(x=1, y=2)</lact>" in result
