"""Regression: every public env.<attr> in flow.py must match an OrchestrationEnv field."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from lionagi.cli.orchestrate._orchestration import OrchestrationEnv

_FLOW_PY = Path(__file__).resolve().parents[3] / "lionagi" / "cli" / "orchestrate" / "flow.py"


def test_orchestration_env_has_no_agent_profile_field() -> None:
    """agent_profile must not be a field; the correct name is orc_profile."""
    fields = {f.name for f in dataclasses.fields(OrchestrationEnv)}
    assert "agent_profile" not in fields, (
        f"OrchestrationEnv has no agent_profile field; flow.py uses must reference "
        f"orc_profile. Got fields: {sorted(fields)}"
    )
    assert "orc_profile" in fields


def test_flow_py_env_public_attrs_exist_on_orchestration_env() -> None:
    """All public env.<attr> accesses in flow.py must resolve to real OrchestrationEnv fields or methods."""
    fields = {f.name for f in dataclasses.fields(OrchestrationEnv)}
    methods = {n for n in dir(OrchestrationEnv) if not n.startswith("_")}
    valid = fields | methods

    text = _FLOW_PY.read_text()
    refs = {
        name
        for name in re.findall(r"\benv\.([a-zA-Z_][a-zA-Z0-9_]*)", text)
        if not name.startswith("_")
    }
    missing = refs - valid
    assert not missing, (
        f"flow.py references public attributes that do not exist on "
        f"OrchestrationEnv: {sorted(missing)}. Likely a typo or stale rename."
    )
