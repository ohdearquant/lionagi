# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The attached application is handed no secret material (ADR-0138 F1).

The application receives the root application and the daemon's services when it attaches, and
each handler receives its request. A handler that walks everything those objects hold, while the
daemon holds a live admission bearer, pairing codes, a decision capability and a reconnect key,
finds none of them, and no response or log line carries them.

The walk follows data: attributes, slots, mappings and sequences. It does not follow a function's
globals or closure, and it never imports a module; code the application imports or introspects is
outside what it is handed, and F1's Consequences names that exposure of the in-process form.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import BuiltinFunctionType, FunctionType, MethodType, ModuleType, SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request

import lionhub.serve.daemon.launch_access as launch_access
import lionhub.serve.daemon.reconnect as reconnect

_HOSTED = "https://lion-studio.khive.ai"
_PROBE = "/trust-boundary-probe"
# A walk that reaches more objects than this has left the application's own graph.
_MAX_OBJECTS = 200_000


@pytest.fixture()
def short_dir() -> Iterator[Path]:
    """A directory whose path fits a unix socket's ~100-byte limit."""
    with tempfile.TemporaryDirectory(prefix="studio-tb-", dir="/tmp") as path:
        yield Path(path)


@pytest.fixture()
def fresh() -> Iterator[None]:
    launch_access.reset_launch_access()
    yield
    launch_access.reset_launch_access()


class _Secrets:
    """The daemon's secret values, the texts and the bytes."""

    def __init__(self) -> None:
        self.texts: dict[str, str] = {}
        self.blobs: dict[str, bytes] = {}

    def text(self, name: str, value: str | None) -> None:
        assert value, f"{name} was not issued, so the walk could not miss it"
        self.texts[name] = value

    def blob(self, name: str, value: bytes | None) -> None:
        assert value, f"{name} was not issued, so the walk could not miss it"
        self.blobs[name] = value

    def in_text(self, text: str) -> list[str]:
        return [name for name, value in self.texts.items() if value in text] + [
            name for name, value in self.blobs.items() if value.hex() in text
        ]


def _children(obj: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(obj, dict):
        for key, value in list(obj.items()):
            yield f"[{key!r}]", value
            yield f".keys({key!r})", key
        return
    if isinstance(obj, (list, tuple, set, frozenset)):
        for index, value in enumerate(list(obj)):
            yield f"[{index}]", value
        return
    if isinstance(obj, MethodType):
        yield ".__self__", obj.__self__
        return
    try:
        namespace = getattr(obj, "__dict__", None)
    except Exception:  # noqa: BLE001 - an object that raises on reading hands a handler nothing
        return
    if isinstance(namespace, dict):
        for key, value in list(namespace.items()):
            yield f".{key}", value
    for klass in type(obj).__mro__:
        for slot in getattr(klass, "__slots__", ()):
            if slot in ("__dict__", "__weakref__"):
                continue
            try:
                yield f".{slot}", getattr(obj, slot)
            except Exception:  # noqa: BLE001 - an unset or raising slot holds nothing to reach
                continue


def _reach(root: Any, secrets: _Secrets) -> list[str]:
    """Every path from ``root`` that ends at a secret, or at a module whose globals hold them."""
    found: list[str] = []
    seen: set[int] = set()
    stack: list[tuple[Any, str]] = [(root, "root")]
    while stack:
        obj, path = stack.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        assert len(seen) <= _MAX_OBJECTS, f"the walk left the application's graph at {path}"
        if isinstance(obj, str):
            found += [f"{path} -> {name}" for name, v in secrets.texts.items() if v in obj]
            continue
        if isinstance(obj, (bytes, bytearray)):
            found += [f"{path} -> {name}" for name, v in secrets.blobs.items() if v in obj]
            continue
        if isinstance(obj, ModuleType):
            # A module hands its globals: the daemon's are where its secrets live.
            found.append(f"{path} -> module {obj.__name__}")
            continue
        if isinstance(obj, (FunctionType, BuiltinFunctionType, type, int, float, complex)):
            continue
        stack.extend((child, f"{path}{step}") for step, child in _children(obj))
    return found


def test_the_walk_finds_a_secret_and_a_module_it_is_handed() -> None:
    """The control: the same walk reports a secret in data and a module that holds one."""
    secrets = _Secrets()
    secrets.blob("reconnect key", b"\x01" * 32)
    secrets.text("bearer", "planted-bearer-value")

    planted = {
        "a": [SimpleNamespace(inner=(b"\x00" + b"\x01" * 32,))],
        "b": "x planted-bearer-value",
    }

    assert sorted(_reach(planted, secrets)) == [
        "root['a'][0].inner[0] -> reconnect key",
        "root['b'] -> bearer",
    ]
    # The shape the services had before this record's F1 clause: daemon modules, not functions.
    assert _reach(SimpleNamespace(launch_access=launch_access), secrets) == [
        "root.launch_access -> module lionhub.serve.daemon.launch_access"
    ]


@pytest.mark.skipif(not reconnect.unix_sockets_supported(), reason="no unix sockets")
def test_a_handler_reaches_no_secret_through_what_the_application_is_handed(
    short_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from fastapi.testclient import TestClient

    import lionagi.studio.app as app_mod
    import lionagi.studio.services.operator as operator_mod
    import lionhub.orchestration.scheduler.lifecycle as lifecycle_mod
    import lionhub.serve.daemon.app as daemon_mod
    import lionhub.serve.daemon.services.launches as launches_mod
    import lionhub.serve.diagnostics as code_identity
    from lionhub.orchestration.scheduler.engine import scheduler

    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    # The lifespan's credential wiring is under test; its other startup work is stubbed so
    # nothing reaches a real store, the scheduler or the mirror.
    monkeypatch.setattr(code_identity, "snapshot_git_position", lambda: None)
    monkeypatch.setattr(scheduler, "start", _noop)
    monkeypatch.setattr(scheduler, "stop", _noop)
    monkeypatch.setattr(lifecycle_mod, "run_startup_reconciliation", _noop)
    monkeypatch.setattr(operator_mod, "operator_startup", _noop)
    monkeypatch.setattr(operator_mod, "operator_shutdown", _noop)
    monkeypatch.setattr(launches_mod, "shutdown_launches", _noop)
    monkeypatch.setattr(daemon_mod, "_start_claude_mirror", lambda: (None, None))
    monkeypatch.setattr(daemon_mod, "_stop_claude_mirror", _noop)
    monkeypatch.setattr(daemon_mod, "_startup_warmup", _noop)
    monkeypatch.setattr(daemon_mod, "_finalize_warmup", _noop)
    socket_path = short_dir / "reconnect.sock"
    monkeypatch.setattr(daemon_mod, "default_socket_path", lambda *args: socket_path)

    handed: dict[str, Any] = {}
    secrets = _Secrets()

    async def probe(request: Request) -> dict[str, list[str]]:
        roots = {"request": request, "scope": request.scope, "services": handed["services"]}
        return {"reached": _reach(roots, secrets)}

    def attach(application: FastAPI, services: daemon_mod.DaemonServices):
        handed["application"] = application
        handed["services"] = services
        application.add_api_route(_PROBE, probe, methods=["GET"])
        return app_mod.attach_studio(application, services)

    attach.__module__ = app_mod.__name__  # read Studio's own compatibility manifest
    # A launcher in this process mints the launch's code before the daemon starts, so serving
    # carries it and mints the admission bearer.
    secrets.text("launch pairing code", launch_access.mint_pairing_code(for_launch=True))
    application = daemon_mod.create_daemon_app(attach)

    with caplog.at_level(logging.DEBUG):
        with TestClient(application, base_url="http://127.0.0.1:8765") as client:
            reply = reconnect.read_reconnect_reply(socket_path)
            assert reply is not None
            secrets.text("reconnect pairing code", reply[0])
            secrets.blob("reconnect key", reply[1])
            secrets.text("admission bearer", launch_access.launch_bearer())
            secrets.text("pairing code", launch_access.mint_pairing_code(origins=[_HOSTED]))
            secrets.text("decision capability", launch_access.mint_decision_capability(_HOSTED))
            for index, digest in enumerate(list(launch_access._capabilities)):
                secrets.blob(f"capability digest {index}", digest)
            assert launch_access.resolve_decision_capability(
                secrets.texts["decision capability"], origin=_HOSTED
            ), "the capability is live while the handler looks for it"

            response = client.get(_PROBE)
            health = client.get("/health", params={"challenge": "ab" * 16})
            reached_from_application = _reach(handed["application"], secrets)

    assert response.status_code == 200
    assert response.json() == {"reached": []}
    assert reached_from_application == []
    assert (
        handed["services"].resolve_decision_capability is launch_access.resolve_decision_capability
    )
    # /health answers with a proof derived from the key, never the key.
    assert "proof" in health.json()
    assert secrets.in_text(response.text + health.text) == []
    logged = "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)
    assert secrets.in_text(logged) == []
