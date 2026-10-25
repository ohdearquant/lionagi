# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Original-derived projection and authoritative recovery coordinates."""

from types import SimpleNamespace

import pytest

from lionagi.protocols.messages import _result_projection as projection


def _message(ident, output, **metadata):
    return SimpleNamespace(id=ident, content=SimpleNamespace(output=output), metadata=metadata)


def _branch(*messages):
    return SimpleNamespace(
        msgs=SimpleNamespace(
            messages={message.id: message for message in messages},
            progression=[message.id for message in messages],
        )
    )


def _session(branch, bound=4000):
    return projection._projection_session(branch, SimpleNamespace(tool_result_max_chars=bound))


@pytest.mark.parametrize("bound", [1024, 4000, 20000])
def test_projected_text_has_one_complete_original_recovery_marker(bound):
    text = ("evidence é🦁 0123456789 " * 6000)[:100045]
    original = _message("original", text)
    session = _session(_branch(original), bound)
    result = session.project(original)
    (metric,) = result.bodies
    assert len(result.output) <= bound
    assert metric.projected_length == len(result.output)
    assert metric.original_length == len(text)
    assert metric.retained == metric.k_n < len(text)
    head = 3 * metric.retained // 4
    tail = metric.retained - head
    assert head > 0 and tail > 0
    assert result.output.startswith(text[:head]) and result.output.endswith(text[-tail:])
    assert result.output.count('context(action="read_result"') == 1
    stop = len(text) - tail
    assert f"{stop - head:,} chars omitted from [{head}, {stop})" in result.output
    assert f"index=0, offset={head}, limit={min(bound, 16000, stop - head)}" in result.output
    assert "continue at next_offset" in result.output
    assert f"stopping at {stop}" in result.output
    assert original.content.output is text


def test_mapping_values_keep_types_order_and_global_json_offsets():
    output = {
        "raw": "Aé🦁 " * 20000,
        "items": [{"z": 2, "a": "雪"}] * 5000,
        "short": {"z": 2, "a": 1},
        "success": "protected " * 1000,
        "error": "whole " * 1000,
        "bytes": b"not JSON",
    }
    original = _message("original", output)
    session = _session(_branch(original))
    source = session.source(original)
    result = session.project(original)
    assert list(result.output) == list(output)
    assert result.output["short"] is output["short"]
    assert result.output["success"] is output["success"]
    assert result.output["error"] is output["error"]
    assert result.output["bytes"] is output["bytes"]
    assert result.output["items"].startswith("[JSON preview]\n")
    assert source.text.index('[{"a":"雪","z":2}') == next(
        body.start for body in source.bodies if body.key == "items"
    )
    for metric in result.bodies:
        if metric.eligible:
            value = result.output[metric.body.key]
            if metric.retained < metric.original_length:
                assert len(value) == metric.projected_length <= 4000
                start = metric.body.start + 3 * metric.retained // 4
                stop = metric.body.end - (metric.retained - 3 * metric.retained // 4)
                assert f"from [{start}, {stop})" in value
    assert output["raw"].endswith("Aé🦁 ")
    assert isinstance(output["items"], list)


def test_fitting_original_and_top_level_nonbody_keep_their_form():
    for output in ("x" * 1024, "", "é", ["x" * 10000], b"binary"):
        original = _message("original", output)
        result = _session(_branch(original), 1024).project(original)
        assert result.output is output


def test_fitting_original_with_smaller_old_copy_allowance_pays_for_marker():
    original = _message("original", "x" * 2000)
    copy = _message("copy", "obsolete preview", clipped_from="original", context_clip_kept=[1999])
    session = _session(_branch(original, copy), 2000)
    result = session.project(copy)
    (metric,) = result.bodies
    assert metric.k_n == 2000
    assert 2 <= metric.retained < 1999
    assert len(result.output) <= 2000
    assert "obsolete preview" not in result.output
    assert result.output.count('context(action="read_result"') == 1


def test_k_n_is_independent_of_allowance_minimum_and_protected_ranges():
    original = _message("original", "x" * 100045)
    session = _session(_branch(original), 4000)
    baseline = session.project(original)
    (metric,) = baseline.bodies
    reduced = session.project(
        original, allowances=[1500], minimums=[1024], protected_ranges=[(0, 2)]
    )
    (lower,) = reduced.bodies
    assert lower.k_n == metric.k_n
    assert 1024 <= lower.retained <= 1500 < metric.retained
    assert lower.projected_length <= 4000
    with pytest.raises(projection._ProjectionError, match="retained-source constraints"):
        session.project(original, allowances=[100], minimums=[1024])
    with pytest.raises(projection._ProjectionError, match="retained-source constraints"):
        session.project(original, allowances=[1500], protected_ranges=[(50000, 50020)])
    assert session.project(original).output == baseline.output


def test_largest_fit_does_not_assume_monotonic_candidate_length(monkeypatch):
    original = _message("original", "x" * 100045)
    source = projection._OriginalResult(original, 99)

    def gapped_form(source, body, retained, max_chars):
        value = "x" * (retained if retained in (1000, 10) else max_chars + 1)
        head = 3 * retained // 4
        return value, head, body.end - (retained - head)

    monkeypatch.setattr(projection, "_shortened_body", gapped_form)
    whole = projection._project_original(source, max_chars=1024)
    (metric,) = whole.bodies
    assert metric.k_n == metric.retained == 1000
    lower = projection._project_original(source, max_chars=1024, allowances=[900])
    (small,) = lower.bodies
    assert small.k_n == 1000 and small.retained == 10


def test_source_resolution_and_canonical_encoding_are_lazy(monkeypatch):
    original = _message("original", "x" * 100045)
    unused = _message("unused", {"not_needed": object()})
    session = _session(_branch(unused, original))
    real = projection._result_text
    encoded = []

    def observed(output):
        encoded.append(output)
        return real(output)

    monkeypatch.setattr(projection, "_result_text", observed)
    descriptor = session.source(original)
    assert descriptor.index == 1 and encoded == []
    session.project(original)
    session.project(original)
    assert encoded == [original.content.output]
    assert session.source(original) is descriptor
    assert projection._projection_session(object(), object()) is None


def test_candidate_authority_is_identity_scoped_nested_and_always_reset():
    original = _message("original", "x" * 100045)
    earlier = _message("earlier", "earlier")
    authority = _session(_branch(earlier, original))
    copy = _message("copy", "preview", clipped_from="original", context_clip_kept=[1500])
    candidate = _branch(copy)
    unrelated = _branch(copy)
    with pytest.raises(projection._ProjectionError, match="authoritative original index"):
        _session(candidate).project(copy)
    with projection._projection_authority(candidate, authority):
        result = _session(candidate).project(copy)
        assert "index=1" in result.output
        with pytest.raises(projection._ProjectionError, match="authoritative original index"):
            _session(unrelated).project(copy)
        with pytest.raises(RuntimeError, match="interrupt"):
            with projection._projection_authority(unrelated, authority):
                assert "index=1" in _session(unrelated).project(copy).output
                raise RuntimeError("interrupt")
        assert _session(candidate).project(copy).output == result.output
    with pytest.raises(projection._ProjectionError, match="authoritative original index"):
        _session(candidate).project(copy)
