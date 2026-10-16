# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The published address in the `docker run` argument list `li studio --docker` builds.

A bare `PORT:PORT` mapping publishes the container's port on every host
interface, and with no auth token configured nothing else keeps other machines on
the network from the API. The host half of the mapping is therefore what
decides who can reach the port. These tests read the argv the
CLI hands docker, faking the subprocess the way the mount tests do, so no
docker is needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def docker_run(tmp_path, monkeypatch):
    """Run the real `--docker` path with docker faked; yields (invoke, home)."""
    import lionagi.studio.cli as studio_module

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(studio_module, "_has_docker", lambda: True)

    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))

        class Result:
            returncode = 0
            stderr = b""

        return Result()

    monkeypatch.setattr(studio_module.subprocess, "run", fake_run)

    def invoke(*argv: str) -> list[str]:
        from lionagi.cli.main import main

        assert main(["studio", "--docker", *argv]) == 0
        assert len(captured) == 2, f"expected a pull and a run, saw {captured}"
        return captured[1]

    return invoke, tmp_path


def test_the_default_publish_names_loopback(docker_run):
    """No host named: the port is published on 127.0.0.1, not on every interface."""
    invoke, _ = docker_run
    argv = invoke()
    assert argv[argv.index("-p") + 1] == "127.0.0.1:8765:8765"


def test_the_default_run_argv_is_exactly_this(docker_run):
    """The whole argument list, so the mapping is pinned where it is built."""
    invoke, home = docker_run
    assert invoke() == [
        "docker",
        "run",
        "--rm",
        "-p",
        "127.0.0.1:8765:8765",
        "-v",
        f"{home}/.lionagi:/root/.lionagi",
        "--name",
        "lion-studio",
        "ghcr.io/ohdearquant/lion-studio:latest",
    ]


def test_a_port_flag_reaches_the_publish(docker_run):
    invoke, _ = docker_run
    argv = invoke("--port", "9001")
    assert argv[argv.index("-p") + 1] == "127.0.0.1:9001:8765"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "studio.example.com"])
def test_an_explicit_host_is_the_published_address(docker_run, host):
    """`--host` already names the address to bind, so the publish follows it."""
    invoke, _ = docker_run
    argv = invoke("--host", host)
    assert argv[argv.index("-p") + 1] == f"{host}:8765:8765"


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "127.0.0.1:8765:8765"),
        ("localhost", "localhost:8765:8765"),
        ("0.0.0.0", "0.0.0.0:8765:8765"),
        ("::1", "[::1]:8765:8765"),
        ("::", "[::]:8765:8765"),
        ("fe80::1", "[fe80::1]:8765:8765"),
        ("[::1]", "[::1]:8765:8765"),
        # A blank value is the default, not a request to publish everywhere:
        # docker reads an empty host half as every interface.
        ("", "127.0.0.1:8765:8765"),
        ("  ", "127.0.0.1:8765:8765"),
    ],
)
def test_the_publish_spec_brackets_ipv6_literals_only(host, expected):
    """A literal IPv6 address needs brackets, or its colons read as separators."""
    from lionagi.studio.cli import _docker_publish_spec

    assert _docker_publish_spec(host, 8765) == expected


def test_the_publish_spec_keeps_the_container_port():
    from lionagi.studio.cli import _docker_publish_spec

    assert _docker_publish_spec("127.0.0.1", 4321) == "127.0.0.1:4321:8765"
