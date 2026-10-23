# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Pin schema projection and partial capability selection before adapter cutover."""

import json

from pydantic import BaseModel

from lionagi.ln.types import Operable, Spec
from lionagi.operations._observe import attempt_extract
from lionagi.session.capabilities import render_capabilities_prompt


def test_prompt_keeps_nested_definitions_and_property_order_without_root_required():
    class Payload(BaseModel):
        value: int

    grant = Operable((Spec(Payload, name="zeta"), Spec(Payload, name="alpha")), name="OrderedGrant")

    prompt = render_capabilities_prompt(grant)
    contract = json.loads(prompt.rsplit("```json\n", 1)[1].split("```", 1)[0])

    assert set(contract) == {"properties", "$defs"}
    assert tuple(contract["properties"]) == ("zeta", "alpha")
    assert contract["$defs"]["Payload"]["required"] == ["value"]
    assert contract["$defs"]["Payload"]["properties"]["value"]["type"] == "integer"
    assert "Allowed capability keys (emitting any other key is rejected): alpha, zeta." in prompt


def test_emission_subset_uses_declaration_order_and_omits_unemitted_required_fields():
    grant = Operable(
        (Spec(int, name="zeta"), Spec(str, name="unemitted"), Spec(int, name="alpha")),
        name="OrderedGrant",
    )

    bundles, violations, rejects = attempt_extract('{"alpha": "2", "zeta": 1}', grant)

    assert violations == []
    assert rejects == []
    assert len(bundles) == 1
    bundle = bundles[0]
    assert type(bundle).__name__ == "OrderedGrant"
    assert tuple(type(bundle).model_fields) == ("zeta", "alpha")
    assert bundle.model_dump() == {"zeta": 1, "alpha": 2}
