# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The frontend's dead-health set mirrors a severity boundary defined here.

`apps/studio/frontend/src/lib/health.ts` decides whether an invocation's health
verdict is settled enough to present. A verdict read from a capped sample of an
invocation's children is a worst-of over a subset, so it is a LOWER BOUND on
severity: reading the rest can raise it and can never lower it.

That is what lets a partial verdict already naming a dead process be presented
as settled. It holds only because the frontend's `DEAD_HEALTH` is closed upward
over this module's severity order — every value at or above `unresponsive` is in
it — so raising the verdict keeps it dead, and the boards render one collapsed
state for every dead value.

Nothing in either language enforces that. A new level added between `idle` and
`unresponsive`, or a value dropped from the set, would leave the frontend
presenting a partial verdict as settled when reading the remaining children
could still move it out of the dead set. This asserts the mirror instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lionagi.state.health import HEALTH_SEVERITY, SessionHealth

_HEALTH_TS = (
    Path(__file__).resolve().parents[2]
    / "apps"
    / "studio"
    / "frontend"
    / "src"
    / "lib"
    / "health.ts"
)

_DEAD_HEALTH_DECLARATION = re.compile(
    r"export const DEAD_HEALTH = new Set\(\[(?P<members>[^\]]*)\]\)"
)


def _declared_dead_health() -> set[str]:
    """The set literal as the frontend actually spells it."""
    source = _HEALTH_TS.read_text()
    match = _DEAD_HEALTH_DECLARATION.search(source)
    assert match is not None, (
        f"DEAD_HEALTH is no longer declared the way this test reads it in {_HEALTH_TS}. "
        f"A parse that finds nothing must fail rather than report an empty set, because "
        f"an empty set compares unequal and would look like a mirror break instead of a "
        f"broken reader."
    )
    members = set(re.findall(r'"([^"]+)"', match.group("members")))
    assert members, "the declaration parsed but named no members"
    return members


def test_the_severity_order_covers_every_health_level():
    """Control for the expectation below, which is computed from this map.

    A level missing from the map would silently shrink the expected set and let
    a real mirror break pass.
    """
    assert set(HEALTH_SEVERITY) == set(SessionHealth)


def test_the_frontends_dead_health_set_is_closed_upward_over_severity():
    dead_floor = HEALTH_SEVERITY[SessionHealth.UNRESPONSIVE]
    expected = {
        level.value for level, severity in HEALTH_SEVERITY.items() if severity >= dead_floor
    }

    assert _declared_dead_health() == expected, (
        "DEAD_HEALTH in health.ts no longer matches every level at or above "
        f"{SessionHealth.UNRESPONSIVE.value}. isUnsettledHealth presents a verdict read "
        "from a partial sample as settled whenever it is dead, and that is only sound "
        "while raising the verdict keeps it in this set."
    )


@pytest.mark.parametrize(
    "level",
    [SessionHealth.HEALTHY, SessionHealth.IDLE],
)
def test_the_levels_below_the_floor_stay_out_of_the_dead_set(level: SessionHealth):
    """The other half of the boundary.

    A partial verdict at one of these can be raised into the dead set by an
    unread child, which is exactly the case `isUnsettledHealth` has to keep
    reporting as unsettled. Naming them here means widening the set to swallow
    one fails a test rather than quietly changing what the boards present.
    """
    assert HEALTH_SEVERITY[level] < HEALTH_SEVERITY[SessionHealth.UNRESPONSIVE]
    assert level.value not in _declared_dead_health()
