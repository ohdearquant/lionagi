from __future__ import annotations

import pytest

from lionagi.work import StepDef, WorkerDefinition


def test_worker_definition_yaml_load_dump():
    yaml_text = """
name: sample-work
description: Example pipeline
steps:
  - name: gather
    worker: collect
    inputs:
      instruction: gather context
  - name: write
    worker: summarize
    depends_on:
      - gather
    inputs:
      instruction: write draft
"""

    definition = WorkerDefinition.from_yaml(yaml_text)
    assert definition.name == "sample-work"
    assert len(definition.steps) == 2

    dumped = definition.to_yaml()
    reloaded = WorkerDefinition.from_yaml(dumped)
    assert reloaded.name == definition.name
    assert reloaded.description == definition.description
    assert len(reloaded.steps) == len(definition.steps)
    for step in zip(reloaded.steps, definition.steps):
        assert step[0].name == step[1].name
        assert step[0].worker == step[1].worker
        assert step[0].inputs == step[1].inputs
        assert step[0].depends_on == step[1].depends_on


def test_step_def_validation():
    definition = {
        "name": "invalid",
        "steps": [
            {"name": "one", "worker": "foo", "depends_on": ["missing"]},
            {"name": "one", "worker": "bar"},
        ],
    }
    with pytest.raises(ValueError):
        WorkerDefinition.model_validate(definition)


def test_compile_to_work_items():
    definition = WorkerDefinition(
        name="compile",
        steps=[
            StepDef(name="first", worker="collect", inputs={"instruction": "first"}),
            StepDef(
                name="second",
                worker="transform",
                depends_on=["first"],
                inputs={"instruction": "second"},
            ),
        ],
    )
    items = definition.compile_to_work_items()
    assert len(items) == 2
    assert items[0].depends_on == []
    assert items[1].depends_on == [str(items[0].id)]
    assert all(item.status.value == "pending" for item in items)
