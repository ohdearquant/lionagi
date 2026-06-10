# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from .nested import (
    deep_merge,
    deep_update,
    flatten,
    get_target_container,
    nget,
    npop,
    nset,
    unflatten,
)

__all__ = [
    "get_target_container",
    "nget",
    "nset",
    "npop",
    "flatten",
    "unflatten",
    "deep_update",
    "deep_merge",
]
