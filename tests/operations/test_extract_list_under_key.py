# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A list of objects under one emission key is read as one emission per element.

The reply shape comes from a model that put several findings in one fenced block,
``{"issue_found": [{...}, {...}]}``, instead of one object per key. Each element is
validated as the key's own model, so ``extra="forbid"`` still applies per element.
"""

from __future__ import annotations

import json

from lionagi.casts.emission import build_emission_operable
from lionagi.operations._observe import attempt_extract
from lionhub.orchestration.engines.engine import emission_keys
from lionhub.orchestration.engines.review import DimensionClean, IssueFound

EMITS = (IssueFound, DimensionClean)


def _issue(n: int, **extra) -> dict:
    return {
        "description": f"issue {n}: a doc row still points at a removed module",
        "dimension": "maintainability",
        "severity": "minor",
        "location": f"docs/page-{n}.md",
        "confidence": 0.6,
        "evidence": f"the diff removes the module at hunk {n}",
        "source": "diff excerpt",
        **extra,
    }


def _reply(payload: dict) -> str:
    return "Notes on the diff.\n\n```json\n" + json.dumps(payload, indent=2) + "\n```\n"


def _grant():
    return build_emission_operable(EMITS, name="review_emissions")


def test_list_under_issue_found_yields_one_bundle_per_element():
    text = _reply({"issue_found": [_issue(1), _issue(2), _issue(3)]})
    bundles, violations, rejects = attempt_extract(text, _grant())
    assert not violations and not rejects
    assert [type(b.issue_found) for b in bundles] == [IssueFound] * 3
    assert [b.issue_found.location for b in bundles] == [
        "docs/page-1.md",
        "docs/page-2.md",
        "docs/page-3.md",
    ]


def test_single_object_under_issue_found_is_unchanged():
    bundles, violations, rejects = attempt_extract(_reply({"issue_found": _issue(1)}), _grant())
    assert not violations and not rejects
    assert len(bundles) == 1 and bundles[0].issue_found.location == "docs/page-1.md"


def test_a_bad_element_is_rejected_alone_and_the_others_survive():
    bad = _issue(2, not_a_field="x")  # extra="forbid" still applies per element
    bundles, _violations, rejects = attempt_extract(
        _reply({"issue_found": [_issue(1), bad, _issue(3)]}), _grant()
    )
    assert [b.issue_found.location for b in bundles] == ["docs/page-1.md", "docs/page-3.md"]
    assert len(rejects) == 1 and rejects[0].block == {"issue_found": bad}


def test_other_keys_in_the_block_stay_together_and_the_list_is_split():
    clean = {"dimension": "security", "rationale": "no auth surface touched"}
    bundles, violations, rejects = attempt_extract(
        _reply({"dimension_clean": clean, "issue_found": [_issue(1), _issue(2)]}), _grant()
    )
    assert not violations and not rejects
    kinds = sorted(k for b in bundles for k in b.model_fields_set)
    assert kinds == ["dimension_clean", "issue_found", "issue_found"]


def test_an_empty_list_and_a_list_of_non_objects_still_reject():
    for value in ([], ["not an object"], [_issue(1), "not an object"]):
        bundles, _violations, rejects = attempt_extract(_reply({"issue_found": value}), _grant())
        assert bundles == [] and len(rejects) == 1, value


def test_a_list_under_a_key_outside_the_grant_is_not_an_emission():
    assert attempt_extract(_reply({"verdict": [_issue(1)]}), _grant()) == ([], [], [])


def test_a_list_next_to_a_key_outside_the_grant_is_a_violation_not_split():
    bundles, violations, _rejects = attempt_extract(
        _reply({"issue_found": [_issue(1)], "verdict": "x"}), _grant()
    )
    assert bundles == [] and [v.offending for v in violations] == [["verdict"]]


def test_the_repair_hint_names_the_list_shape():
    hint = emission_keys(EMITS)
    assert "'issue_found'" in hint and "list of objects" in hint
