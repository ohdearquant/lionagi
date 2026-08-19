# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.operations.operate.step — Step factory methods."""

from pydantic import BaseModel

from lionagi.ln.types import Spec
from lionagi.models import FieldModel
from lionagi.operations.operate.step import Step


def test_step_request_operative_converts_field_models_and_excludes_action_responses():
    """field_models list is converted to Spec; actions=True excludes action_responses from request."""
    fm = FieldModel(name="score", base_type=int)
    op = Step.request_operative(
        name="Check",
        field_models=[fm],
        actions=True,
        reason=True,
    )

    request_cls = op.create_request_model()
    request_fields = set(request_cls.model_fields.keys())

    # reason and action_required / action_requests should be present
    assert "reason" in request_fields
    assert "action_required" in request_fields
    assert "action_requests" in request_fields
    assert "score" in request_fields

    # action_responses is excluded from request
    assert "action_responses" not in request_fields

    # response model includes action_responses
    response_cls = op.create_response_model()
    response_fields = set(response_cls.model_fields.keys())
    assert "action_responses" in response_fields


def test_step_respond_operative_additional_fields_returns_new_operative():
    """respond_operative with additional_fields returns a new Operative including new fields."""
    base_op = Step.request_operative(name="Check")
    confidence_spec = Spec(float, name="confidence")

    new_op = Step.respond_operative(base_op, additional_fields={"confidence": confidence_spec})

    # new_op is a distinct object
    assert new_op is not base_op

    response_cls = new_op.create_response_model()
    assert "confidence" in response_cls.model_fields

    # original operative's response model is not affected
    original_response_cls = base_op.create_response_model()
    assert "confidence" not in original_response_cls.model_fields


def test_operative_model_type_cache_reuses_same_schema():
    class Payload(BaseModel):
        value: int

    first = Step.respond_operative(Step.request_operative(base_type=Payload, reason=True))
    second = Step.respond_operative(Step.request_operative(base_type=Payload, reason=True))

    assert first is not second
    assert first.request_type is second.request_type
    assert first.response_type is second.response_type


def test_operative_model_type_cache_distinguishes_same_shaped_base_classes():
    def make_first_payload_class():
        class Payload(BaseModel):
            value: int

        return Payload

    def make_second_payload_class():
        class Payload(BaseModel):
            value: int

        return Payload

    Payload1 = make_first_payload_class()
    Payload2 = make_second_payload_class()
    assert Payload1 is not Payload2

    model1 = Step.request_operative(base_type=Payload1).request_type
    model2 = Step.request_operative(base_type=Payload2).request_type

    assert model1 is not model2
    assert issubclass(model1, Payload1)
    assert issubclass(model2, Payload2)
    assert isinstance(model2.model_validate({"value": 1}), Payload2)
    assert not isinstance(model2.model_validate({"value": 1}), Payload1)


def test_operative_model_type_cache_is_sensitive_to_reason_and_actions():
    class Payload(BaseModel):
        value: int

    plain = Step.request_operative(base_type=Payload).request_type
    reason = Step.request_operative(base_type=Payload, reason=True).request_type
    actions = Step.request_operative(base_type=Payload, actions=True).request_type

    assert plain is not reason
    assert plain is not actions
    assert reason is not actions
    assert "reason" in reason.model_fields
    assert "action_requests" in actions.model_fields
    assert "action_responses" not in actions.model_fields


def test_operative_model_type_cache_preserves_cold_and_warm_validation_behavior():
    class Payload(BaseModel):
        value: int
        label: str

    cold = Step.respond_operative(Step.request_operative(base_type=Payload, reason=True))
    warm = Step.respond_operative(Step.request_operative(base_type=Payload, reason=True))
    payload = {
        "value": 7,
        "label": "complete",
        "reason": {"title": "check", "content": "validated", "confidence_score": 0.9},
    }

    assert (
        cold.request_type.model_validate(payload).model_dump()
        == warm.request_type.model_validate(payload).model_dump()
    )
    assert (
        cold.response_type.model_validate(payload).model_dump()
        == warm.response_type.model_validate(payload).model_dump()
    )


def test_operative_model_type_cache_size_zero_restores_per_call_classes(
    monkeypatch,
):
    from lionagi.adapters.spec_adapters import pydantic_field

    monkeypatch.setattr(pydantic_field._model_type_cache, "_max_size", 0)

    class Payload(BaseModel):
        value: int = 0

    first = Step.request_operative(base_type=Payload)
    second = Step.request_operative(base_type=Payload)

    assert first.request_type is not second.request_type


# Bound at module scope, so both resolve from the module they name. The tests below
# turn on that and nothing else about them.
MODULE_LEVEL_TYPE = type("MODULE_LEVEL_TYPE", (), {})


def module_level_fn(v):
    return v


def test_structural_cache_admission_classifies_mutable_and_immutable_metadata():
    """Lists, dicts, bound methods, partials and arbitrary objects are never cache-safe;
    scalars, tuples, frozensets, types and plain functions are.

    Admission asks two things: whether the value can be keyed soundly, and whether an entry
    keyed on it would outlive the name it was declared under. A locally built function or
    type fails the second even though it passes the first, because the caches behind this
    gate hold their built value strongly and that value holds the callable.
    """
    from functools import partial

    from lionagi.ln._structural import _try_stable_cache_key

    class Validators:
        def check(self, v):
            return v

    def local_fn(v):
        return v

    class CallableValidator:
        def __call__(self, value):
            return value

    unsafe_values = [
        [1, 2],
        {"a": 1},
        Validators().check,
        [].append,
        partial(module_level_fn),
        CallableValidator(),
        Validators(),
        # Keyable, and refused anyway: neither resolves from the module it names.
        local_fn,
        type("LocalType", (), {}),
    ]
    for value in unsafe_values:
        assert _try_stable_cache_key(value) is None

    safe_values = [
        1,
        "s",
        True,
        1.5,
        None,
        int,
        len,
        (1, 2),
        frozenset({1, 2}),
        module_level_fn,
        MODULE_LEVEL_TYPE,
    ]
    for value in safe_values:
        assert _try_stable_cache_key(value) is not None


def test_the_refusal_tracks_reachability_by_name_not_the_kind_of_callable():
    """A function and a `type()` result are refused for being unreachable, not for being built.

    The module-scope pair is the arm that makes this a statement about reachability: an
    admission rule that simply refused every function and every dynamic type would satisfy
    the first half of this test and be a different rule.
    """
    from lionagi.ln._structural import _try_stable_cache_key

    def local_fn(v):
        return v

    assert _try_stable_cache_key(local_fn) is None
    assert _try_stable_cache_key(type("LocalType", (), {})) is None

    assert _try_stable_cache_key(module_level_fn) is not None
    assert _try_stable_cache_key(MODULE_LEVEL_TYPE) is not None


def test_model_type_cache_key_opts_out_for_bound_method_validator():
    """Hashable, so the hash fallback alone misses it: the opt-out is _try_stable_cache_key rejecting it."""
    from lionagi.adapters.spec_adapters import pydantic_field

    class Validators:
        def check(self, v):
            return v

    spec = Spec(int, name="value", validator=Validators().check)

    def build_key():
        return pydantic_field._model_type_cache_key(
            adapter_type=pydantic_field.PydanticSpecAdapter,
            base_type=BaseModel,
            model_name="Payload",
            declaration=(spec,),
            doc=None,
        )

    assert build_key() is None

    # Bound methods are hashable, but retaining one would pin the receiver and
    # cache mutable method state. Admission therefore must not use hashability.


def test_model_type_cache_key_opts_out_for_list_and_dict_metadata():
    """List-valued and dict-valued metadata (e.g. a multi-validator list or a mutable default) opt out of caching too."""
    from lionagi.adapters.spec_adapters import pydantic_field

    def fn_a(v):
        return v

    def fn_b(v):
        return v

    list_validator_spec = Spec(int, name="value", validator=[fn_a, fn_b])
    dict_metadata_spec = Spec(int, name="value", json_schema_extra={"x": 1})

    for spec in (list_validator_spec, dict_metadata_spec):
        key = pydantic_field._model_type_cache_key(
            adapter_type=pydantic_field.PydanticSpecAdapter,
            base_type=BaseModel,
            model_name="Payload",
            declaration=(spec,),
            doc=None,
        )
        assert key is None


def test_step_request_operative_with_mutable_default_metadata_bypasses_cache():
    """Distinct mutable-default Specs never raise and never get cross-wired to the wrong shared model type (regression class: type-identity cache collision)."""

    class Payload(BaseModel):
        value: int

    spec1 = Spec(list, name="tags", default=[1])
    spec2 = Spec(list, name="tags", default=[2])

    op1 = Step.request_operative(base_type=Payload, fields={"tags": spec1})
    op2 = Step.request_operative(base_type=Payload, fields={"tags": spec2})

    assert op1.request_type is not op2.request_type

    inst1 = op1.request_type.model_validate({"value": 1})
    inst2 = op2.request_type.model_validate({"value": 2})

    assert inst1.tags == [1]
    assert inst2.tags == [2]


def test_the_cache_is_not_what_keeps_a_declared_callable_alive_past_its_name():
    """Building the model takes the reference; storing the result adds none.

    Admission asks whether a name resolves to the callable now, so a name withdrawn
    afterwards is outside what it can see. This measures whether that gap matters:
    the same declaration is built with the cache holding the result and with it
    holding nothing, and the callable's fate is the same either way, so refusing
    admission would not change it. Asserted as an equality rather than a fixed
    outcome, so it still states the claim if the model layer stops retaining.
    """
    import gc
    import sys
    import types
    import weakref

    from lionagi.adapters.spec_adapters import pydantic_field
    from lionagi.ln.types.operable import Operable

    class Payload(BaseModel):
        value: int

    def survives_withdrawal(*, build_model: bool) -> bool:
        mod = types.ModuleType("_probe_validator_module")
        exec("def probe(cls, v):\n    return v\n", mod.__dict__)
        sys.modules["_probe_validator_module"] = mod
        fn = mod.probe
        fn.__module__ = "_probe_validator_module"
        ref = weakref.ref(fn)
        try:
            spec = Spec(int, name="checked", validator=fn)
            if build_model:
                pydantic_field.PydanticSpecAdapter.create_model(
                    Operable((spec,), name="Probe"), "Probe", base_type=Payload
                )
            del mod.probe, fn, spec
        finally:
            sys.modules.pop("_probe_validator_module", None)
        del mod
        for _ in range(3):
            gc.collect()
        return ref() is not None

    cache = pydantic_field._model_type_cache
    original_max = cache._max_size
    try:
        cache._max_size = 512
        with_cache = survives_withdrawal(build_model=True)
        held = len(cache._cache)
        cache._cache.clear()
        cache._max_size = 0
        without_cache = survives_withdrawal(build_model=True)
        held_disabled = len(cache._cache)
    finally:
        cache._max_size = original_max
        cache._cache.clear()

    assert held == 1 and held_disabled == 0, "the two arms did not differ in what the cache stored"
    assert with_cache == without_cache

    # Control: with no model built nothing outlives the name, so the arms above
    # report the model layer rather than this probe.
    assert survives_withdrawal(build_model=False) is False
