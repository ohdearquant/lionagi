# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Test fixtures and scaffolding for hook system tests."""

import pytest

from lionagi.protocols.types import Event


class MyCancelled(Exception):
    """Cancellation exception type for testing."""

    pass


class FakeEvent(Event):
    """Minimal Event double for registry/hook tests."""

    def __init__(self, eid="E1", created_at=123.0):
        super().__init__()
        # Need to set attributes via object.__setattr__ since they're frozen
        object.__setattr__(self, "id", eid)
        object.__setattr__(self, "created_at", created_at)

    @classmethod
    def class_name(cls, full: bool = False):
        return f"{cls.__module__}.{cls.__name__}" if full else cls.__name__


class FakeEventType(Event):
    """Minimal Event double for pre_event_create tests."""

    @classmethod
    def class_name(cls, full: bool = False):
        return f"{cls.__module__}.{cls.__name__}" if full else cls.__name__


@pytest.fixture
def patch_cancellation(monkeypatch):
    from lionagi.service.hooks import hook_event, hook_registry

    monkeypatch.setattr(hook_event, "get_cancelled_exc_class", lambda: MyCancelled)
    monkeypatch.setattr(hook_registry, "get_cancelled_exc_class", lambda: MyCancelled)
    return MyCancelled


@pytest.fixture
def patch_logger(monkeypatch):
    calls = []

    async def mock_alog(msg):
        calls.append(msg)

    from lionagi.service.hooks.hooked_event import global_hook_logger

    monkeypatch.setattr(global_hook_logger, "alog", mock_alog)
    return calls


class FakeFailAfter:
    """Mock fail_after context manager that immediately raises cancellation."""

    def __init__(self, timeout):
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            # Simulate timeout by raising cancellation
            raise MyCancelled("Timeout")


@pytest.fixture
def patch_timeout(monkeypatch):
    from lionagi.service.hooks import hook_event

    monkeypatch.setattr(hook_event, "fail_after", FakeFailAfter)
