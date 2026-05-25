"""Regression: every `env.<attr>` reference in flow.py must match an actual
``OrchestrationEnv`` field. Catches the `env.agent_profile` AttributeError that
shipped in v0.26.x (ADR-0029 commit 2f08b8c5f) where the dataclass field is
``orc_profile`` but two call sites referenced ``agent_profile``.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from lionagi.cli.orchestrate._orchestration import OrchestrationEnv

_FLOW_PY = Path(__file__).resolve().parents[3] / "lionagi" / "cli" / "orchestrate" / "flow.py"


def test_orchestration_env_has_no_agent_profile_field() -> None:
    """If we ever rename ``orc_profile`` to ``agent_profile`` this test should
    be deleted and the other test updated. Until then, ``agent_profile`` MUST
    NOT be a field — its presence elsewhere is a typo.
    """
    fields = {f.name for f in dataclasses.fields(OrchestrationEnv)}
    assert "agent_profile" not in fields, (
        f"OrchestrationEnv has no agent_profile field; flow.py uses must reference "
        f"orc_profile. Got fields: {sorted(fields)}"
    )
    assert "orc_profile" in fields


def test_flow_py_env_public_attrs_exist_on_orchestration_env() -> None:
    """Every PUBLIC ``env.<attr>`` access in flow.py must match a real field
    or method on ``OrchestrationEnv``. Prevents typo regressions like
    ``env.agent_profile`` when the field is actually ``orc_profile``.

    Private attributes (starting with ``_``) are excluded — they are commonly
    attached dynamically (e.g. ``env._finalize_extras``) and Python allows
    setattr on dataclass instances without a declared field.
    """
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
