# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0101 D4: capability-class matching.

A small declarative token -> class map, not a policy engine. Every
capability token used in ``required_capabilities``/``advertised_capabilities``
falls into exactly one class:

- ``eligibility`` (default for any token not listed below): a plain
  subset-match routing filter -- the worker must advertise the token for a
  task carrying it to be claimable at all.
- ``serialization`` (e.g. an exclusive-GPU token): folds into the task's
  host-scoped ``concurrency_key`` at submit time, so ADR-0061 concurrency
  admission queues at most one such task per host. The queue orders
  admission ADVISORILY only -- a worker-side host lock (an OS flock the
  worker takes before touching the resource) stays authoritative; this
  module never arbitrates the machine lock itself.
- ``affinity`` (e.g. a warmed-cache token): a soft ordering preference among
  otherwise-eligible candidates. It never filters -- a worker that does not
  advertise the affinity token still claims a matching task when it is the
  only eligible worker running.

Both the submit path (``task_applications._derive_concurrency_key``) and the
claim path (``worker.claim_and_execute``) import this module rather than
duplicating the token->class policy.
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = (
    "CAPABILITY_CLASSES",
    "DEFAULT_CAPABILITY_CLASS",
    "affinity_score",
    "affinity_tokens",
    "capability_class",
    "host_scoped_concurrency_key",
    "matching_tokens",
    "serialization_tokens",
    "worker_can_serve",
)

DEFAULT_CAPABILITY_CLASS = "eligibility"

# Declarative token -> class map. Extend this map to classify a new
# capability token; do not branch on token names anywhere else.
CAPABILITY_CLASSES: dict[str, str] = {
    "gpu-exclusive": "serialization",
    "warmed-cache": "affinity",
}


def capability_class(token: str) -> str:
    """The class of *token*; unknown tokens default to ``eligibility``."""
    return CAPABILITY_CLASSES.get(token, DEFAULT_CAPABILITY_CLASS)


def _tokens_of_class(tokens: Iterable[str], cls: str) -> list[str]:
    return [t for t in tokens if capability_class(t) == cls]


def serialization_tokens(tokens: Iterable[str]) -> list[str]:
    return _tokens_of_class(tokens, "serialization")


def affinity_tokens(tokens: Iterable[str]) -> list[str]:
    return _tokens_of_class(tokens, "affinity")


def matching_tokens(tokens: Iterable[str]) -> list[str]:
    """The eligibility ∪ serialization tokens -- the subset a worker must
    advertise for a task carrying *tokens* to be claimable. Affinity tokens
    are excluded: they order candidates but never gate claimability."""
    return [t for t in tokens if capability_class(t) != "affinity"]


def worker_can_serve(
    required_capabilities: Iterable[str] | None,
    advertised_capabilities: Iterable[str] | None,
) -> bool:
    """D4's match rule (capability half): R's eligibility∪serialization
    tokens ⊆ W.advertised_capabilities. Execution-target matching is a
    separate check the caller applies alongside this one."""
    required = set(matching_tokens(required_capabilities or ()))
    advertised = set(advertised_capabilities or ())
    return required.issubset(advertised)


def affinity_score(
    required_capabilities: Iterable[str] | None,
    advertised_capabilities: Iterable[str] | None,
) -> int:
    """Count of *required_capabilities*' affinity tokens also advertised by
    the worker -- higher is a stronger soft preference, never a filter."""
    advertised = set(advertised_capabilities or ())
    return sum(1 for t in affinity_tokens(required_capabilities or ()) if t in advertised)


def host_scoped_concurrency_key(
    host: str, required_capabilities: Iterable[str] | None
) -> str | None:
    """D4: only serialization-class tokens fold into the host-scoped
    concurrency_key; eligibility/affinity tokens never gate concurrency
    admission. Returns None when no serialization token is present."""
    tokens = serialization_tokens(required_capabilities or ())
    if not tokens:
        return None
    return f"{host}:{'+'.join(sorted(tokens))}"
