# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from .._structural import UnhashableStructuralValueError
from ._sentinel import (
    MaybeSentinel,
    MaybeUndefined,
    MaybeUnset,
    SingletonType,
    T,
    Undefined,
    UndefinedType,
    Unset,
    UnsetType,
    is_sentinel,
    not_sentinel,
)
from .base import DataClass, Enum, KeysDict, KeysLike, Meta, ModelConfig, Params
from .filters import (
    FieldRef,
    Filter,
    RoleFilter,
    SpecFilter,
    TypeFilter,
    all_of,
    as_filter,
    resolve_path,
)
from .operable import Operable
from .registry import (
    DuplicateRegistryKeyError,
    DuplicateRegistryOwnerError,
    Registry,
    RegistryCompositionError,
    RegistryEntry,
    RegistryFragment,
    RegistryOverride,
    RegistryOverrideRule,
    RegistryRecord,
    RegistrySource,
    RegistryValueError,
)
from .spec import CommonMeta, Spec

__all__ = (
    # Sentinel types
    "Undefined",
    "Unset",
    "MaybeUndefined",
    "MaybeUnset",
    "MaybeSentinel",
    "SingletonType",
    "UndefinedType",
    "UnsetType",
    "is_sentinel",
    "not_sentinel",
    "UnhashableStructuralValueError",
    # Base classes
    "ModelConfig",
    "Enum",
    "Params",
    "DataClass",
    "Meta",
    "KeysDict",
    "KeysLike",
    "T",
    "Registry",
    "RegistryEntry",
    "RegistryFragment",
    "RegistryOverrideRule",
    "RegistrySource",
    "RegistryRecord",
    "RegistryOverride",
    "RegistryCompositionError",
    "DuplicateRegistryOwnerError",
    "DuplicateRegistryKeyError",
    "RegistryValueError",
    # Spec system
    "Spec",
    "CommonMeta",
    "Operable",
    # Filter DSL
    "Filter",
    "TypeFilter",
    "SpecFilter",
    "FieldRef",
    "RoleFilter",
    "as_filter",
    "all_of",
    "resolve_path",
)
