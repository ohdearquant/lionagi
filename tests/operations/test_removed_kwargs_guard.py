# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Removed operation aliases must fail loudly, not get swallowed by **kwargs.

The operation helpers forward unrecognized kwargs to the provider as
``imodel_kw``. Without an explicit guard, a removed parameter name is silently
packed into the outgoing payload (or dropped) instead of raising — changing
behavior with no signal. These tests lock the loud-failure contract while
confirming genuine provider kwargs still flow through.
"""

import pytest
from pydantic import BaseModel

import lionagi as li
from lionagi.operations._guards import reject_removed_kwargs
from lionagi.operations.communicate.communicate import prepare_communicate_kw
from lionagi.operations.operate.operate import prepare_operate_kw
from lionagi.operations.operate.step import Step


class _M(BaseModel):
    x: int


def _branch():
    return li.Branch(chat_model=li.iModel(provider="openai", model="gpt-4o-mini", api_key="dummy"))


def test_reject_removed_kwargs_helper():
    reject_removed_kwargs({}, {"a": "b="}, where="x")  # no removed names → no raise
    reject_removed_kwargs({"ok": 1}, {"a": "b="}, where="x")  # unrelated key → no raise

    with pytest.raises(TypeError, match=r"x\(\) no longer accepts 'a' \(use b=\)"):
        reject_removed_kwargs({"a": 1}, {"a": "b="}, where="x")

    # empty hint → bare name, no "(use )" tail
    with pytest.raises(TypeError, match=r"no longer accepts 'a'$"):
        reject_removed_kwargs({"a": 1}, {"a": ""}, where="x")


@pytest.mark.parametrize("name", ["request_model", "operative_model", "imodel"])
def test_prepare_communicate_kw_rejects_removed_aliases(name):
    with pytest.raises(TypeError, match="communicate\\(\\) no longer accepts"):
        prepare_communicate_kw(_branch(), **{name: _M})


@pytest.mark.parametrize("name", ["request_model", "operative_model", "imodel"])
def test_prepare_operate_kw_rejects_removed_aliases(name):
    with pytest.raises(TypeError, match="operate\\(\\) no longer accepts"):
        prepare_operate_kw(_branch(), **{name: _M})


@pytest.mark.parametrize("name", ["inherit_base", "frozen"])
def test_request_operative_rejects_removed_params(name):
    with pytest.raises(TypeError, match="Step.request_operative\\(\\) no longer accepts"):
        Step.request_operative(**{name: True})


def test_legit_provider_kwargs_still_forwarded():
    kw = prepare_communicate_kw(_branch(), temperature=0.5, top_p=0.9)
    assert kw["chat_param"].imodel_kw == {"temperature": 0.5, "top_p": 0.9}
