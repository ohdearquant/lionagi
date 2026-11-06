# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Mirror offsets reject non-finite JSON values."

import json

import pytest


def test_mirror_offsets_refuse_non_finite(tmp_path, monkeypatch):
    """The transcript cursor file is not the closed shape its fields suggest.

    The offset is this module's own arithmetic, but the tool-name map comes out
    of a transcript another program wrote and is never coerced to ``str``.
    """
    from lionhub.serve import mirror as mirror

    target = tmp_path / "offsets.json"
    monkeypatch.setattr(mirror, "_OFFSETS_PATH", target)

    bad = mirror._FileState(session_uid="s", offset=12, tool_names={"t1": float("nan")})
    with pytest.raises(ValueError, match=r"non-finite float at \$\.f\.pending_tool_names\.t1"):
        mirror._save_states({"f": bad})
    assert not target.exists()

    good = mirror._FileState(session_uid="s", offset=12, tool_names={"t1": "Read"})
    mirror._save_states({"f": good})
    assert json.loads(target.read_text())["f"] == {
        "offset": 12,
        "session_uid": "s",
        "pending_tool_names": {"t1": "Read"},
        "tool_names": {"t1": "Read"},
        "leaf_uuid": None,
        # Carried so a message mirrored after a restart keeps the attribution of
        # the turn that produced it; it reaches this file out of a codex rollout,
        # so it is on the same foreign-data footing as tool_names above.
        "turn": {},
    }
