"""ADR-0119 structural equality, hashing, and substrate-cache contracts."""

from __future__ import annotations

import gc
import json
import math
import os
import subprocess
import sys
import types
import typing
import weakref
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Any, NoReturn, get_args, get_origin
from uuid import UUID

import pytest

from lionagi.ln.types import (
    Meta,
    ModelConfig,
    Params,
    Spec,
    Undefined,
    UnhashableStructuralValueError,
    Unset,
)
from lionagi.ln.types._sentinel import SingletonType, _SingletonMeta


@dataclass(slots=True, frozen=True, init=False, eq=False)
class ValueParams(Params):
    payload: Any


@dataclass(slots=True, frozen=True, init=False, eq=False)
class OtherValueParams(Params):
    payload: Any


@dataclass(slots=True, frozen=True, init=False, eq=False)
class AbsenceParams(Params):
    _config = ModelConfig(prefill_unset=False)

    payload: Any


class _HashCollision:
    __slots__ = ("value",)

    def __init__(self, value: int) -> None:
        self.value = value

    def __hash__(self) -> int:
        return 7

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _HashCollision) and self.value == other.value


class _OpaqueMutable:
    __hash__ = None


class _Level(Enum):
    ONE = 1


def test_mapping_order_is_structural_but_mutable_values_are_not_hashable():
    first = {"alpha": 1, "beta": 2}
    reordered = {"beta": 2, "alpha": 1}

    pairs = (
        (Meta("config", first), Meta("config", reordered)),
        (Spec(int, config=first), Spec(int, config=reordered)),
        (ValueParams(payload=first), ValueParams(payload=reordered)),
    )

    for left, right in pairs:
        assert left == right
        assert left._key() == right._key()
        with pytest.raises(UnhashableStructuralValueError, match="mutable structural value"):
            hash(left)
        with pytest.raises(UnhashableStructuralValueError, match="mutable structural value"):
            hash(right)


def test_immutable_values_share_one_equality_and_hash_projection():
    left = ValueParams(payload=("ordered", frozenset({3, 1, 2})))
    right = ValueParams(payload=("ordered", frozenset({2, 3, 1})))

    assert left == right
    assert left._key() == right._key()
    assert hash(left) == hash(right)
    assert Meta("value", left) == Meta("value", right)
    assert hash(Meta("value", left)) == hash(Meta("value", right))


def test_structural_atoms_are_type_sensitive():
    legacy_list = getattr(typing, "List")[int]

    assert Meta("value", True) != Meta("value", 1)
    assert Meta("value", 1) != Meta("value", 1.0)
    assert Spec(int, default=True) != Spec(int, default=1)
    assert ValueParams(payload=True) != ValueParams(payload=1)
    assert ValueParams(payload=1) != ValueParams(payload=1.0)
    assert ValueParams(payload={True: "value"}) != ValueParams(payload={1: "value"})
    assert ValueParams(payload=_Level.ONE) != ValueParams(payload=1)
    assert ValueParams(payload=list[int]) != ValueParams(payload=legacy_list)


@pytest.mark.parametrize(
    "annotation",
    (Callable[[int, str], bool], Callable[..., bool]),
)
def test_callable_typing_forms_are_immutable_structural_values(annotation):
    from lionagi.ln._structural import _try_stable_cache_key

    spec = Spec(annotation, marker="callable")

    assert _try_stable_cache_key(spec) is not None
    assert hash(spec) == hash(Spec(annotation, marker="callable"))
    assert spec.annotated() is Spec(annotation, marker="callable").annotated()


@pytest.mark.parametrize("annotation", (list[Any], Callable[..., NoReturn]))
def test_public_typing_singletons_are_cache_stable_on_supported_runtimes(annotation):
    from lionagi.ln._structural import _try_stable_cache_key

    assert _try_stable_cache_key(annotation) is not None
    assert _try_stable_cache_key(Spec(annotation, marker="any")) is not None
    assert (
        Spec(annotation, marker="any").annotated()
        is Spec(
            annotation,
            marker="any",
        ).annotated()
    )


def test_path_values_are_immutable_structural_atoms():
    left = ValueParams(payload=Path("/tmp/lionagi"))
    right = ValueParams(payload=Path("/tmp/lionagi"))

    assert left == right
    assert hash(left) == hash(right)
    assert ValueParams(payload=PureWindowsPath("C:/LionAGI")) == ValueParams(
        payload=PureWindowsPath("c:/lionagi")
    )
    assert ValueParams(payload=PureWindowsPath("C:/ß")) != ValueParams(
        payload=PureWindowsPath("C:/ss")
    )


def test_path_subclasses_are_opaque_and_cache_ineligible():
    from lionagi.ln._structural import _try_stable_cache_key

    class EqualPathType(type(type(Path()))):
        def __eq__(cls, other):
            return other is type(Path())

        def __hash__(cls):
            return hash(type(Path()))

    class MutablePath(type(Path()), metaclass=EqualPathType):
        pass

    path = MutablePath("/tmp/lionagi")
    path.extra = []

    assert _try_stable_cache_key(path) is None
    assert ValueParams(payload=path) != ValueParams(payload=MutablePath("/tmp/lionagi"))


def test_uuid_values_are_immutable_structural_atoms():
    value = UUID("12345678-1234-5678-1234-567812345678")

    assert ValueParams(payload=value) == ValueParams(payload=UUID(str(value)))
    assert hash(ValueParams(payload=value)) == hash(ValueParams(payload=UUID(str(value))))


def test_lone_surrogate_strings_remain_valid_structural_values():
    left = ValueParams(payload="\ud800")
    right = ValueParams(payload="\ud800")

    assert left == right
    assert hash(left) == hash(right)


def test_hash_collisions_never_define_equality(monkeypatch):
    left = ValueParams(payload=_HashCollision(1))
    right = ValueParams(payload=_HashCollision(2))

    key_type = type(left._key())
    monkeypatch.setattr(key_type, "__hash__", lambda self: 7)
    assert hash(left) == hash(right)
    assert left != right


def test_sequence_order_and_concrete_owner_type_are_semantic():
    assert ValueParams(payload=(1, 2)) != ValueParams(payload=(2, 1))
    assert ValueParams(payload=(1, 2)) != ValueParams(payload=[1, 2])
    assert ValueParams(payload={1, 2}) != ValueParams(payload=frozenset({1, 2}))
    assert ValueParams(payload="same") != OtherValueParams(payload="same")


def test_float_payload_bits_are_semantic():
    assert Meta("value", 0.0) != Meta("value", -0.0)
    assert Meta("value", math.nan) == Meta("value", math.nan)


def test_bit_equal_nan_mapping_keys_do_not_restore_insertion_order_semantics():
    first_nan = float("nan")
    second_nan = float("nan")
    first = {first_nan: "first", second_nan: "second"}
    reordered = {second_nan: "second", first_nan: "first"}

    assert Meta("value", first) == Meta("value", reordered)


def test_callable_metadata_uses_identity():
    def validator(value: Any) -> Any:
        return value

    same = Meta("validator", validator)
    repeated = Meta("validator", validator)
    distinct = Meta("validator", lambda value: value)

    assert same == repeated
    assert hash(same) == hash(repeated)
    assert same != distinct


def test_callable_presentation_mutation_cannot_change_identity_ordering():
    def alpha(value):
        return value

    def omega(value):
        return value

    payload = frozenset({alpha, omega})
    owner = ValueParams(payload=payload)
    before = owner._key()
    alpha.__qualname__, omega.__qualname__ = omega.__qualname__, alpha.__qualname__
    fresh = ValueParams(payload=payload)

    assert owner._key() == before
    assert owner == fresh
    assert hash(owner) == hash(fresh)


def test_callable_params_use_declared_fields_before_callable_identity():
    class CallableParams(ValueParams):
        def __call__(self):
            return self.payload

    assert CallableParams(payload="same") == CallableParams(payload="same")
    assert CallableParams(payload="left") != CallableParams(payload="right")


def test_full_field_state_distinguishes_all_absence_values():
    assert AbsenceParams(payload=Undefined) != AbsenceParams(payload=Unset)
    assert AbsenceParams(payload=Unset) != AbsenceParams(payload=None)
    assert AbsenceParams(payload=None) != AbsenceParams(payload=False)


def test_nominal_sentinel_spoof_is_opaque_and_unhashable():
    from lionagi.ln._structural import _try_stable_cache_key

    class UndefinedType:
        __module__ = "lionagi.ln.types._sentinel"
        __hash__ = None

    fake = UndefinedType()
    value = ValueParams(payload=fake)

    assert _try_stable_cache_key(fake) is None
    assert value != ValueParams(payload=Undefined)
    with pytest.raises(UnhashableStructuralValueError, match=r"\$\.payload"):
        hash(value)


@pytest.mark.parametrize("base", (tuple, frozenset))
def test_immutable_builtin_subclasses_are_opaque_and_cache_ineligible(base):
    from lionagi.ln._structural import _try_stable_cache_key

    class Stateful(base):
        def __new__(cls, values):
            instance = super().__new__(cls, values)
            instance.visible = list(values)
            return instance

        def __iter__(self):
            return iter(self.visible)

    child = Stateful((1, 2))
    owner = ValueParams(payload=child)
    before = owner._key()
    child.visible.append(3)

    assert _try_stable_cache_key(child) is None
    assert owner._key() == before
    assert owner == ValueParams(payload=child)


class _StatefulDict(dict):
    """A dict subclass whose extra state items() cannot see."""

    def __init__(self, *args, tag=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tag = tag


class _StatefulMapping(Mapping):
    """The same hazard on a Mapping that is deliberately not a dict subclass."""

    def __init__(self, data, tag=None):
        self._data = dict(data)
        self.tag = tag

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __repr__(self) -> str:
        return f"_StatefulMapping({self._data!r})"


@pytest.mark.parametrize("factory", (_StatefulDict, _StatefulMapping))
def test_mapping_subclasses_are_opaque_rather_than_projected_by_items(factory):
    left = ValueParams(payload=factory({"k": 1}, tag="a"))
    right = ValueParams(payload=factory({"k": 1}, tag="b"))

    assert left != right


def test_a_mapping_subclass_stays_opaque_even_when_all_of_its_state_matches():
    """Opacity is a property of the type, so a matching instance is unequal too."""
    assert ValueParams(payload=_StatefulDict({"k": 1}, tag="a")) != ValueParams(
        payload=_StatefulDict({"k": 1}, tag="a")
    )


def test_an_opaque_mapping_still_compares_equal_to_itself():
    shared = _StatefulMapping({"k": 1}, tag="a")
    assert ValueParams(payload=shared) == ValueParams(payload=shared)


def test_exact_dicts_are_still_projected_structurally():
    """The control: opacity must not be reached by breaking plain mappings."""
    assert ValueParams(payload={"k": 1}) == ValueParams(payload={"k": 1})
    assert ValueParams(payload={"k": 1}) != ValueParams(payload={"k": 2})


@dataclass(frozen=True, slots=True)
class _Payload:
    body: Any


def test_a_large_payload_is_not_retained_by_the_substrate_cache():
    """One cached key strongly retains its whole target, so size has to gate retention."""
    from lionagi.ln._structural import _MAX_CACHED_WEIGHT, _try_stable_cache_key

    assert _try_stable_cache_key(_Payload("x" * (_MAX_CACHED_WEIGHT + 1))) is None


def test_a_small_payload_is_still_retained():
    """The control: the ceiling must not be reached by disabling the cache."""
    from lionagi.ln._structural import _try_stable_cache_key

    assert _try_stable_cache_key(_Payload("x")) is not None


def test_the_ceiling_accumulates_rather_than_measuring_one_scalar():
    """Many small values retain as much as one large one, so the cost is summed."""
    from lionagi.ln._structural import _MAX_CACHED_WEIGHT, _try_stable_cache_key

    many = tuple("x" * 64 for _ in range((_MAX_CACHED_WEIGHT // 64) + 2))
    assert all(len(part) < _MAX_CACHED_WEIGHT for part in many)
    assert _try_stable_cache_key(_Payload(many)) is None


def test_an_unretained_value_is_still_comparable_and_hashable():
    """Withholding a cache entry bounds retention; it must not change what compares equal."""
    from lionagi.ln._structural import _MAX_CACHED_WEIGHT

    body = "x" * (_MAX_CACHED_WEIGHT + 1)
    left = ValueParams(payload=_Payload(body))
    right = ValueParams(payload=_Payload(body))

    assert left == right
    assert hash(left) == hash(right)
    assert left != ValueParams(payload=_Payload(body + "y"))


def test_a_large_payload_never_enters_the_dataclass_cache():
    from lionagi.ln._structural import (
        _MAX_CACHED_WEIGHT,
        _stable_dataclass_keys,
        _structural_key,
    )

    before = len(_stable_dataclass_keys._cache)
    _structural_key(_Payload("x" * (_MAX_CACHED_WEIGHT + 1)))
    assert len(_stable_dataclass_keys._cache) == before

    _structural_key(_Payload("x"))
    assert len(_stable_dataclass_keys._cache) == before + 1


def _oversized(limit: int) -> str:
    return "x" * (limit + 1)


_UNBOUNDED_SHAPES = {
    "str": lambda limit: _oversized(limit),
    "bytes": lambda limit: _oversized(limit).encode(),
    "int": lambda limit: 1 << (limit * 8),
    "pure_posix_path": lambda limit: PurePosixPath(*(_oversized(limit),)),
    "pure_windows_path": lambda limit: PureWindowsPath(*(_oversized(limit),)),
    "concrete_path": lambda limit: Path(*(_oversized(limit),)),
    "enum_value": lambda limit: Enum("_SizedEnum", {"BODY": _oversized(limit)}).BODY,
    "tuple_of_small_parts": lambda limit: tuple("x" * 64 for _ in range((limit // 64) + 2)),
    "frozenset_of_small_parts": lambda limit: frozenset(
        str(index) * 64 for index in range((limit // 64) + 2)
    ),
    "nested_dataclass": lambda limit: _Payload(_Payload(_oversized(limit))),
}


@pytest.mark.parametrize("shape", sorted(_UNBOUNDED_SHAPES))
def test_no_shape_that_carries_unbounded_content_is_retained(shape):
    """Every branch that materializes its content has to be counted, not the ones remembered."""
    from lionagi.ln._structural import _MAX_CACHED_WEIGHT, _try_stable_cache_key

    value = _UNBOUNDED_SHAPES[shape](_MAX_CACHED_WEIGHT)
    assert _try_stable_cache_key(_Payload(value)) is None


def test_the_unbounded_shape_enumeration_is_not_empty():
    """The parametrization above asserts nothing if its source is."""
    assert len(_UNBOUNDED_SHAPES) >= 8


def test_a_small_value_of_every_unbounded_shape_is_still_retained():
    """The control: the ceiling must not be reached by refusing everything."""
    from lionagi.ln._structural import _try_stable_cache_key

    for factory in _UNBOUNDED_SHAPES.values():
        assert _try_stable_cache_key(_Payload(factory(1))) is not None


def _module_level_marker() -> None:
    return None


def _dynamic_function(payload: bytes) -> Callable[[], None]:
    def made() -> None:
        return None

    made.payload = payload
    return made


_IDENTITY_ONLY_SHAPES = {
    "closure": _dynamic_function,
    "lambda": lambda payload: lambda: payload,
    "type_call": lambda payload: type("_Made", (), {"payload": payload}),
}


@pytest.mark.parametrize("shape", sorted(_IDENTITY_ONLY_SHAPES))
def test_no_dynamically_created_callable_is_retained(shape):
    """A callable's token is its identity, so its length cannot price what it carries."""
    from lionagi.ln._structural import _try_stable_cache_key

    made = _IDENTITY_ONLY_SHAPES[shape](b"x" * 4096)
    assert _try_stable_cache_key(_Payload(made)) is None


def test_the_identity_only_enumeration_is_not_empty():
    """The parametrization above asserts nothing if its source is."""
    assert len(_IDENTITY_ONLY_SHAPES) >= 3


def test_a_module_level_callable_is_still_retained():
    """The control: bounding retention by refusing every callable would disable the cache."""
    from lionagi.ln._structural import _try_stable_cache_key

    for held in (json.dumps, dict, len, _module_level_marker, _Payload):
        assert _try_stable_cache_key(_Payload(held)) is not None


def test_a_callable_wearing_a_module_level_name_is_not_retained():
    """__module__ and __qualname__ are writable, so the name has to resolve back to this object."""
    from lionagi.ln._structural import _try_stable_cache_key

    impostor = types.FunctionType(_module_level_marker.__code__, {}, "_module_level_marker")
    impostor.__module__ = _module_level_marker.__module__
    impostor.__qualname__ = _module_level_marker.__qualname__

    assert _try_stable_cache_key(_Payload(impostor)) is None


def test_a_dynamically_created_callable_is_released_after_projection():
    """Non-admission is the mechanism; releasing the payload is the property it buys."""
    from lionagi.ln._structural import _structural_key

    made = _dynamic_function(b"x" * 4096)
    _structural_key(_Payload(made))
    released = weakref.ref(made)
    del made
    gc.collect()

    assert released() is None


def test_an_int_wider_than_the_decimal_conversion_limit_still_projects():
    """A decimal token would raise here, and a declaration must compare rather than explode."""
    wide = 1 << 70000

    assert ValueParams(payload=wide) == ValueParams(payload=wide)
    assert ValueParams(payload=wide) != ValueParams(payload=wide + 1)
    assert hash(ValueParams(payload=wide)) == hash(ValueParams(payload=wide))


@pytest.mark.parametrize("value", (0, 1, -1, 255, -255, 256, -256, 1 << 63, -(1 << 63)))
def test_int_tokens_stay_distinct_across_widths_and_signs(value):
    from lionagi.ln._structural import _structural_key

    others = (0, 1, -1, 255, -255, 256, -256, 1 << 63, -(1 << 63))
    same = _structural_key(value)._sort_token
    for other in others:
        if other == value:
            continue
        assert _structural_key(other)._sort_token != same


def test_cycles_fail_with_a_typed_path():
    cyclic: list[Any] = []
    cyclic.append(cyclic)

    value = ValueParams(payload=cyclic)
    assert value == value
    with pytest.raises(UnhashableStructuralValueError, match=r"\$\.payload\[0\]"):
        hash(value)


def test_opaque_mutable_values_fail_only_when_hashing_is_requested():
    value = _OpaqueMutable()
    meta = Meta("opaque", value)

    assert meta == Meta("opaque", value)
    assert meta != Meta("opaque", _OpaqueMutable())
    with pytest.raises(UnhashableStructuralValueError, match=r"\$\.value"):
        hash(meta)


def test_unordered_projection_is_seed_independent():
    script = (
        "from lionagi.ln._structural import _structural_key; "
        "print(repr(_structural_key({'z', 3, (True, None), frozenset({2, 1})})))"
    )
    outputs = []
    for seed in ("1", "2", "3", "4"):
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        outputs.append(
            subprocess.check_output(
                [sys.executable, "-c", script],
                env=env,
                text=True,
            ).strip()
        )

    assert len(set(outputs)) == 1


class _EqualType(type):
    def __eq__(cls, other: object) -> bool:
        return isinstance(other, _EqualType)

    def __hash__(cls) -> int:
        return 1


@dataclass(frozen=True, init=False, eq=False)
class _LayoutAlpha(Params, metaclass=_EqualType):
    alpha: int


@dataclass(frozen=True, init=False, eq=False)
class _LayoutBeta(Params, metaclass=_EqualType):
    beta: int


def test_field_layout_cache_keys_owner_types_by_identity():
    assert _LayoutAlpha(alpha=1).to_dict() == {"alpha": 1}
    assert _LayoutBeta(beta=2).to_dict() == {"beta": 2}


def test_sentinel_policy_cache_cannot_cross_equal_metaclasses():
    shared_config = ModelConfig(none_as_sentinel=True)

    class Allowed(Params, metaclass=_EqualType):
        _config = shared_config

    class Denied(Params, metaclass=_EqualType):
        _config = shared_config

    Allowed.__module__ = "lionagi.operations.types"
    Allowed.__qualname__ = "MorphParam"

    assert Allowed._is_sentinel(None)
    with pytest.raises(ValueError, match="not allowlisted"):
        Denied._is_sentinel(None)


def test_singleton_cache_keys_subclasses_by_identity():
    class EqualSingletonMeta(_SingletonMeta):
        def __eq__(cls, other: object) -> bool:
            return isinstance(other, EqualSingletonMeta)

        def __hash__(cls) -> int:
            return 1

    class AlphaSingleton(SingletonType, metaclass=EqualSingletonMeta):
        pass

    class BetaSingleton(SingletonType, metaclass=EqualSingletonMeta):
        pass

    alpha = AlphaSingleton()
    beta = BetaSingleton()

    assert alpha is AlphaSingleton()
    assert beta is BetaSingleton()
    assert alpha is not beta
    assert type(alpha) is AlphaSingleton
    assert type(beta) is BetaSingleton


def test_spec_annotation_cache_is_type_sensitive_and_bypasses_typing_cache():
    class Alpha(metaclass=_EqualType):
        pass

    class Beta(metaclass=_EqualType):
        pass

    alpha = Spec(Alpha, marker="same").annotated()
    beta = Spec(Beta, marker="same").annotated()

    assert alpha is not beta
    assert alpha.__origin__ is Alpha
    assert beta.__origin__ is Beta


def test_nested_type_identity_survives_generic_nullable_materialization():
    class Alpha(metaclass=_EqualType):
        pass

    class Beta(metaclass=_EqualType):
        pass

    alpha = Spec(list[Alpha], marker="generic").annotated()
    beta = Spec(list[Beta], marker="generic").annotated()
    nullable_alpha = Spec(Alpha, nullable=True).annotated()
    nullable_beta = Spec(Beta, nullable=True).annotated()

    assert alpha.__origin__.__args__[0] is Alpha
    assert beta.__origin__.__args__[0] is Beta
    assert nullable_alpha.__origin__.__args__[0] is Alpha
    assert nullable_beta.__origin__.__args__[0] is Beta


def test_stable_annotation_is_constructed_once_under_concurrency():
    spec = Spec(int, marker="concurrent")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = tuple(pool.map(lambda _: spec.annotated(), range(64)))

    assert all(result is results[0] for result in results)


def test_uncached_annotated_adapter_preserves_public_typing_reflection():
    alias = Spec(int, marker="reflection").annotated()

    assert get_origin(alias) is Annotated
    assert get_args(alias)[0] is int
    assert get_args(alias)[1].key == "marker"


def test_spec_annotation_cache_distinguishes_bool_and_int_metadata():
    boolean = Spec(int, cache_probe=True).annotated()
    integer = Spec(int, cache_probe=1).annotated()

    assert boolean is not integer
    assert boolean.__metadata__[0].value is True
    assert integer.__metadata__[0].value == 1
    assert type(integer.__metadata__[0].value) is int


def test_spec_annotation_cache_distinguishes_signed_zero_metadata():
    positive = Spec(int, cache_probe=0.0).annotated()
    negative = Spec(int, cache_probe=-0.0).annotated()

    assert positive is not negative
    assert math.copysign(1.0, positive.__metadata__[0].value) == 1.0
    assert math.copysign(1.0, negative.__metadata__[0].value) == -1.0


def test_mutable_spec_metadata_opts_out_of_annotation_cache():
    spec = Spec(int, payload={"value": 1})

    assert spec.annotated() is not spec.annotated()
