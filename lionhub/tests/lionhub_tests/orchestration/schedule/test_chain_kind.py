# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Direct schedule chain-kind validation."

from __future__ import annotations

import pytest

from lionhub.orchestration.schedule.parsing import _validate_chain_kinds

pytest.importorskip("fastapi", reason="studio extra not installed")


@pytest.mark.parametrize("parent_kind", [42, ["private-value"], {"private-value": True}])
def test_direct_kind_validation_refuses_non_string_parent(parent_kind):
    with pytest.raises(ValueError) as caught:
        _validate_chain_kinds({"kind": "agent"}, "chain", parent_kind)
    assert "action_kind" in str(caught.value)
    assert type(parent_kind).__name__ in str(caught.value)
    assert "private-value" not in str(caught.value)
