# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Phase 1 tests for lionagi.lndl.fuzzy (normalize_lndl_text only).

# TODO(lndl-phase-2): add TestParseLndlFuzzy once Phase 2 lands (see issue #966).
"""

import pytest

from lionagi.lndl.fuzzy import normalize_lndl_text, parse_lndl_fuzzy


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


class TestParseLndlFuzzyPhase2Guard:
    """parse_lndl_fuzzy must raise NotImplementedError in Phase 1."""

    def test_raises_not_implemented(self):
        with pytest.raises(NotImplementedError, match="Phase 2"):
            parse_lndl_fuzzy("some text", object())

    def test_error_references_issue(self):
        with pytest.raises(NotImplementedError) as exc_info:
            parse_lndl_fuzzy("x", None)
        assert "966" in str(exc_info.value)
