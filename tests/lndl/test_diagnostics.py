# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for lionagi.lndl.diagnostics — pure logic, no LLM."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from lionagi.lndl.diagnostics import (
    LndlChunkHealth,
    LndlRoundRecord,
    LndlTrace,
    classify_chunk,
    classify_result,
    extract_lndl_chunks,
)

# ---------------------------------------------------------------------------
# classify_chunk
# ---------------------------------------------------------------------------


class TestClassifyChunk:
    def test_clean_lndl_with_out(self):
        text = "<lvar n a>5</lvar>\nOUT{n: [a]}"
        h = classify_chunk(text)
        assert h.has_out is True
        assert h.balanced is True
        assert h.status == "clean"
        assert h.open_tags == 1
        assert h.close_tags == 1

    def test_no_out_block(self):
        text = "<lvar n a>5</lvar>"  # missing OUT
        h = classify_chunk(text)
        assert h.has_out is False
        assert h.status == "no_out"

    def test_malformed_unbalanced_tags(self):
        # Two opens, zero closes, with OUT — flagged malformed.
        text = "<lvar n a>5<lact b>fn()<other> OUT{}"
        h = classify_chunk(text)
        assert h.has_out is True
        assert h.balanced is False
        assert h.status == "malformed"

    def test_one_tag_drift_tolerated(self):
        # Off-by-one is treated as clean (common with stray newlines).
        text = "<lvar n a>5</lvar>\nOUT{n: [a]}"  # 1 open / 1 close = balanced
        h = classify_chunk(text)
        assert h.balanced is True

    def test_empty_string(self):
        h = classify_chunk("")
        assert h.has_out is False
        assert h.status == "no_out"

    def test_non_string_input_returns_no_out(self):
        h = classify_chunk(None)  # type: ignore[arg-type]
        assert h.status == "no_out"
        assert h.text == ""

    def test_alternate_out_syntax(self):
        # OUT [...] form
        text = "<lvar a x>1</lvar>\nOUT [x]"
        h = classify_chunk(text)
        assert h.has_out is True
        assert h.status == "clean"


# ---------------------------------------------------------------------------
# classify_result
# ---------------------------------------------------------------------------


class _M(BaseModel):
    n: int


class TestClassifyResult:
    def test_basemodel_ok(self):
        assert classify_result(_M(n=1)) == "ok"

    def test_none_empty(self):
        assert classify_result(None) == "empty"

    def test_empty_string(self):
        assert classify_result("") == "empty"
        assert classify_result("   ") == "empty"

    def test_nonempty_string(self):
        assert classify_result("<lvar a x>1</lvar>OUT{}") == "str"

    def test_empty_dict(self):
        assert classify_result({}) == "empty"

    def test_all_none_dict(self):
        assert classify_result({"a": None, "b": None}) == "empty"

    def test_partial_dict(self):
        assert classify_result({"a": 1, "b": None}) == "dict"

    def test_unexpected_type_falls_through_to_dict(self):
        assert classify_result(42) == "dict"


# ---------------------------------------------------------------------------
# LndlRoundRecord
# ---------------------------------------------------------------------------


class TestLndlRoundRecord:
    def test_health_property_uses_classify_chunk(self):
        r = LndlRoundRecord(raw="<lvar a x>1</lvar>OUT{a: [x]}", outcome="success")
        assert r.health.status == "clean"

    def test_record_with_error(self):
        r = LndlRoundRecord(
            raw="<bad>",
            outcome="failed",
            error="Validation against M failed",
            actions_executed=2,
            schema="M",
        )
        assert r.outcome == "failed"
        assert r.actions_executed == 2
        assert r.schema == "M"
        assert r.error == "Validation against M failed"


# ---------------------------------------------------------------------------
# LndlTrace
# ---------------------------------------------------------------------------


class TestLndlTrace:
    def test_empty_trace(self):
        t = LndlTrace()
        assert len(t) == 0
        assert t.chunks == []
        assert t.outcomes() == {}
        assert t.health() == {"clean": 0, "malformed": 0, "no_out": 0}

    def test_append_records(self):
        t = LndlTrace()
        t.append(LndlRoundRecord(raw="<lvar a x>1</lvar>OUT{}", outcome="success"))
        t.append(LndlRoundRecord(raw="bad", outcome="failed", error="parse error"))
        assert len(t) == 2
        assert t.outcomes() == {"success": 1, "failed": 1}

    def test_health_aggregation(self):
        t = LndlTrace()
        t.append(LndlRoundRecord(raw="<lvar a x>1</lvar>OUT{a: [x]}", outcome="success"))
        t.append(LndlRoundRecord(raw="<lvar a x>1</lvar>", outcome="failed"))  # no_out
        t.append(LndlRoundRecord(raw="", outcome="continue"))  # empty: no health
        h = t.health()
        assert h["clean"] == 1
        assert h["no_out"] == 1
        assert h["malformed"] == 0

    def test_chunks_filters_empty(self):
        t = LndlTrace()
        t.append(LndlRoundRecord(raw="x", outcome="success"))
        t.append(LndlRoundRecord(raw="", outcome="failed"))
        assert t.chunks == ["x"]

    def test_errors_indexed(self):
        t = LndlTrace()
        t.append(LndlRoundRecord(raw="x", outcome="success"))
        t.append(LndlRoundRecord(raw="x", outcome="retry", error="bad alias"))
        t.append(LndlRoundRecord(raw="x", outcome="success"))
        errs = t.errors()
        assert errs == [(1, "bad alias")]

    def test_summary_includes_health_and_outcomes(self):
        t = LndlTrace()
        t.append(LndlRoundRecord(raw="<lvar a x>1</lvar>OUT{}", outcome="success"))
        s = t.summary()
        assert "1 rounds" in s
        assert "success" in s
        assert "clean" in s


# ---------------------------------------------------------------------------
# extract_lndl_chunks
# ---------------------------------------------------------------------------


def _msg(assistant_response: str | None) -> SimpleNamespace:
    """Mimic a Branch message with .content.assistant_response."""
    content = SimpleNamespace(assistant_response=assistant_response)
    return SimpleNamespace(content=content)


class TestExtractLndlChunks:
    def test_extracts_lndl_assistant_messages(self):
        msgs = [
            _msg("<lvar a x>1</lvar>OUT{a: [x]}"),
            _msg("just plain text"),  # no LNDL markers — skipped
            _msg("<lact b c>fn()</lact>OUT{}"),
        ]
        chunks = extract_lndl_chunks(msgs)
        assert len(chunks) == 2
        assert "<lvar" in chunks[0]
        assert "<lact" in chunks[1]

    def test_since_offset(self):
        msgs = [
            _msg("OLD<lvar a x>1</lvar>OUT{}"),
            _msg("NEW<lact b c>fn()</lact>OUT{}"),
        ]
        chunks = extract_lndl_chunks(msgs, since=1)
        assert len(chunks) == 1
        assert "NEW" in chunks[0]

    def test_skips_non_assistant_messages(self):
        msgs = [
            SimpleNamespace(content=SimpleNamespace(assistant_response=None)),
            _msg("<lvar a x>1</lvar>OUT{}"),
        ]
        chunks = extract_lndl_chunks(msgs)
        assert len(chunks) == 1

    def test_handles_messages_without_content_attr(self):
        msgs = [SimpleNamespace()]  # no .content at all
        chunks = extract_lndl_chunks(msgs)
        assert chunks == []

    def test_recognizes_all_marker_forms(self):
        markers = ["<lact", "<lvar", "OUT{", "OUT ["]
        for m in markers:
            chunks = extract_lndl_chunks([_msg(f"some text {m} more")])
            assert len(chunks) == 1, f"failed to recognize marker: {m}"
