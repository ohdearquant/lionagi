# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Flow request aliases preserve their schemas, payloads and historical pickles."""

from __future__ import annotations

import base64
import json
import pickle
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import lionagi.casts as casts
import lionagi.casts.emission as emission
from lionagi.protocols import flow_requests
from lionagi.protocols.generic.element import Element

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "flow_requests"
CORE_MODULE = "lionagi.protocols.flow_requests"


@pytest.fixture(scope="module")
def request_schemas():
    return json.loads((FIXTURES / "request_schemas.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def request_pickles():
    return json.loads((FIXTURES / "request_pickles.json").read_text(encoding="utf-8"))


def _fresh(script: str, *arguments: str) -> dict:
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script, str(ROOT), *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (
        f"child stdout:\n{completed.stdout}\nchild stderr:\n{completed.stderr}"
    )
    return json.loads(completed.stdout)


@pytest.mark.parametrize("name", ["EscalationRequest", "SpawnRequest"], ids=["escalation", "spawn"])
def test_public_request_aliases_are_the_core_classes(name):
    core_class = getattr(flow_requests, name)
    assert getattr(emission, name) is core_class
    assert getattr(casts, name) is core_class
    assert core_class.__module__ == CORE_MODULE


def test_all_emissions_share_one_core_base():
    assert emission._EmissionModel is flow_requests._EmissionModel
    assert issubclass(flow_requests.EscalationRequest, emission._EmissionModel)
    assert issubclass(flow_requests.SpawnRequest, emission._EmissionModel)

    models = [
        model
        for name in emission.__all__
        if isinstance(model := getattr(emission, name), type) and issubclass(model, BaseModel)
    ]
    assert len(models) == 23
    for model in models:
        assert issubclass(model, emission._EmissionModel)
        assert getattr(casts, model.__name__) is model
        if model not in (flow_requests.EscalationRequest, flow_requests.SpawnRequest):
            assert model.__module__ == "lionagi.casts.emission"


def test_allowed_operations_are_one_frozen_vocabulary():
    allowed = flow_requests.SPAWN_ALLOWED_OPERATIONS
    assert emission.SPAWN_ALLOWED_OPERATIONS is allowed
    assert casts.SPAWN_ALLOWED_OPERATIONS is allowed
    assert isinstance(allowed, frozenset)
    assert allowed == {"operate", "chat", "communicate", "ReAct"}


@pytest.mark.parametrize("name", ["EscalationRequest", "SpawnRequest"], ids=["escalation", "spawn"])
def test_request_schema_matches_the_captured_base(name, request_schemas):
    actual = getattr(flow_requests, name).model_json_schema()
    assert json.dumps(actual, sort_keys=True) == json.dumps(request_schemas[name], sort_keys=True)


@pytest.mark.parametrize("name", ["EscalationRequest", "SpawnRequest"], ids=["escalation", "spawn"])
def test_request_defaults_and_payload_roundtrip(name):
    model = getattr(flow_requests, name)
    if name == "EscalationRequest":
        request = model(reason="needs input")
        expected = {
            "reason": "needs input",
            "context": {},
            "urgency": "blocked",
            "from_role": None,
        }
        other = model(reason="another request")
        request.context["task"] = "first"
        assert other.context == {}
        request.context.clear()
        assert request.blocking is True
    else:
        request = model(instruction="inspect sample")
        expected = {
            "instruction": "inspect sample",
            "assignee": None,
            "operation": "operate",
            "independent": False,
            "reason": None,
        }

    assert request.model_dump() == expected
    assert model.model_validate(expected).model_dump() == expected
    assert model.model_validate_json(request.model_dump_json()).model_dump() == expected


@pytest.mark.parametrize("name", ["EscalationRequest", "SpawnRequest"], ids=["escalation", "spawn"])
def test_requests_refuse_extra_fields(name):
    payload = {"reason": "needs input"} if name == "EscalationRequest" else {"instruction": "work"}
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        getattr(flow_requests, name).model_validate(payload)


@pytest.mark.parametrize(
    ("legacy", "urgency"),
    [("false", "blocked"), ([], "fyi")],
    ids=["truthy-text", "falsy-list"],
)
def test_legacy_blocking_keeps_truthiness_without_mutating_input(legacy, urgency):
    payload = {"reason": "needs input", "blocking": legacy}
    request = flow_requests.EscalationRequest.model_validate(payload)

    assert request.urgency == urgency
    assert request.blocking is (urgency == "blocked")
    assert payload == {"reason": "needs input", "blocking": legacy}
    assert "blocking" not in request.model_dump()


def test_explicit_urgency_wins_and_blocking_remains_read_only():
    request = flow_requests.EscalationRequest(reason="continuing", urgency="fyi", blocking="false")
    assert request.urgency == "fyi"
    assert request.blocking is False
    with pytest.raises(AttributeError):
        request.blocking = True


@pytest.mark.parametrize("name", ["EscalationRequest", "SpawnRequest"], ids=["escalation", "spawn"])
def test_historical_pickles_load_core_instances_and_new_pickles_name_core(name, request_pickles):
    """These Pydantic models are not Elements; Element class-path migration does not apply."""
    model = getattr(flow_requests, name)
    assert issubclass(model, BaseModel)
    assert not issubclass(model, Element)
    captured = request_pickles[name]
    assert captured["protocol"] == 0
    historical = base64.b64decode(captured["payload_base64"], validate=True)
    old_global = f"clionagi.casts.emission\n{name}\n".encode()
    new_global = f"c{CORE_MODULE}\n{name}\n".encode()
    assert old_global in historical

    request = pickle.loads(historical)
    assert type(request) is model
    assert request.model_dump() == captured["model_dump"]

    for obj in (model, request):
        fresh = pickle.dumps(obj, protocol=0)
        assert new_global in fresh
        assert old_global not in fresh
    assert pickle.loads(pickle.dumps(model, protocol=0)) is model
    restored = pickle.loads(pickle.dumps(request, protocol=0))
    assert type(restored) is model
    assert restored.model_dump() == captured["model_dump"]


@pytest.mark.parametrize("name", ["EscalationRequest", "SpawnRequest"], ids=["escalation", "spawn"])
def test_historical_pickle_loads_before_any_cast_import(name):
    result = _fresh(
        """
import base64
import importlib
import json
import pickle
import sys
from pathlib import Path

root, name = Path(sys.argv[1]), sys.argv[2]
assert not any(n == "lionagi.casts" or n.startswith("lionagi.casts.") for n in sys.modules)
assert "lionagi.protocols.flow_requests" not in sys.modules
samples = json.loads((root / "tests/fixtures/flow_requests/request_pickles.json").read_text())
sample = samples[name]
request = pickle.loads(base64.b64decode(sample["payload_base64"], validate=True))
core = importlib.import_module("lionagi.protocols.flow_requests")
legacy = sys.modules["lionagi.casts.emission"]
assert core.__spec__.name == "lionagi.protocols.flow_requests"
assert Path(core.__file__).resolve() == root / "lionagi/protocols/flow_requests.py"
assert Path(legacy.__file__).resolve() == root / "lionagi/casts/emission.py"
assert type(request) is getattr(core, name)
assert getattr(legacy, name) is getattr(core, name)
assert request.model_dump() == sample["model_dump"]
print(json.dumps({"class": type(request).__module__ + "." + type(request).__name__,
                  "payload": request.model_dump(),
                  "legacy_loaded": "lionagi.casts.emission" in sys.modules}))
""",
        name,
    )
    assert result["class"] == f"{CORE_MODULE}.{name}"
    assert result["legacy_loaded"] is True


@pytest.mark.parametrize("first", ["core", "legacy"], ids=["core-only", "legacy-first"])
def test_fresh_import_boundary_has_a_legacy_positive_control(first):
    result = _fresh(
        """
import importlib
import json
import sys
from pathlib import Path

root, first = Path(sys.argv[1]), sys.argv[2]
core_name = "lionagi.protocols.flow_requests"
assert core_name not in sys.modules
assert not any(n == "lionagi.casts" or n.startswith("lionagi.casts.") for n in sys.modules)
if first == "core":
    core = importlib.import_module(core_name)
    forbidden = [n for n in sys.modules
                 if n == "lionagi.casts" or n.startswith("lionagi.casts.")
                 or n in {"lionagi.session.session", "lionagi.operations.flow"}]
    assert not forbidden, forbidden
    print(json.dumps({"first": first, "forbidden": forbidden,
                      "core_loaded": core_name in sys.modules}))
else:
    legacy = importlib.import_module("lionagi.casts.emission")
    core = sys.modules[core_name]
    assert Path(legacy.__file__).resolve() == root / "lionagi/casts/emission.py"
    assert legacy.EscalationRequest is core.EscalationRequest
    assert legacy.SpawnRequest is core.SpawnRequest
    print(json.dumps({"first": first, "core_loaded": core_name in sys.modules}))
assert core.__spec__.name == core_name
assert Path(core.__file__).resolve() == root / "lionagi/protocols/flow_requests.py"
""",
        first,
    )
    assert result == (
        {"first": "core", "forbidden": [], "core_loaded": True}
        if first == "core"
        else {"first": "legacy", "core_loaded": True}
    )
