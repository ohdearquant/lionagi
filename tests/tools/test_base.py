# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for tools/base.py: LionTool, ResourceCategory, Resource, Prompt."""

import pytest
from pydantic import ValidationError

from lionagi.tools.base import Prompt, Resource, ResourceCategory

# ---------------------------------------------------------------------------
# ResourceCategory enum
# ---------------------------------------------------------------------------


def test_resource_category_values():
    assert ResourceCategory.FRAMEWORK.value == "framework"
    assert ResourceCategory.TEMPLATE.value == "template"
    assert ResourceCategory.UTILITY.value == "utility"
    assert ResourceCategory.PROMPT.value == "prompt"
    assert ResourceCategory.OTHER.value == "other"


# ---------------------------------------------------------------------------
# Resource serializes category and rejects invalid category
# ---------------------------------------------------------------------------


def test_resource_category_serializes_and_rejects_invalid_category():
    r = Resource(category="prompt", metadata={"title": "T"})
    data = r.model_dump()
    assert data["category"] == "prompt"

    with pytest.raises((ValueError, ValidationError)):
        Resource(category="missing")


def test_resource_default_category_is_other():
    r = Resource()
    assert r.category == ResourceCategory.OTHER


def test_resource_category_frozen():
    r = Resource(category="utility")
    with pytest.raises((TypeError, ValidationError)):
        r.category = "prompt"


def test_prompt_category_is_prompt():
    p = Prompt()
    assert p.category == ResourceCategory.PROMPT


@pytest.mark.parametrize("cat", ["framework", "template", "utility", "prompt", "other"])
def test_resource_accepts_all_valid_categories(cat):
    r = Resource(category=cat)
    assert r.category == cat


def test_resource_meta_obj_reflects_metadata():
    r = Resource(metadata={"title": "MyTitle", "domain": "code", "overview": "x"})
    meta = r.meta_obj
    assert meta.title == "MyTitle"
    assert meta.domain == "code"
