# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for the free create_message() deprecation warning."""

import warnings

from lionagi.protocols.messages import create_message
from lionagi.protocols.messages.instruction import Instruction
from lionagi.protocols.messages.manager import MessageManager


class TestCreateMessageDeprecation:
    def test_warns_deprecation_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            create_message(instruction="hello")

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecations) == 1
        assert "create_message" in str(deprecations[0].message)
        assert "MessageManager.create_message" in str(deprecations[0].message)

    def test_stacklevel_identifies_caller(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            create_message(instruction="hello")  # this exact line

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecations) == 1
        assert deprecations[0].filename == __file__

    def test_returns_same_value_as_manager_create_message(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            free_result = create_message(instruction="hello")

        manager_result = MessageManager.create_message(instruction="hello")

        assert isinstance(free_result, Instruction)
        assert isinstance(manager_result, Instruction)
        assert free_result.content.to_dict() == manager_result.content.to_dict()

    def test_manager_create_message_does_not_warn(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            MessageManager.create_message(instruction="hello")

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecations) == 0
