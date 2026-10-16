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

TOKEN = "LIONAGI_STUDIO_AUTH_TOKEN"


@pytest.fixture(autouse=True)
def _no_ambient_token(monkeypatch):
    """The token decides whether an `-e` is added, so no test inherits one."""
    monkeypatch.delenv(TOKEN, raising=False)


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

    return invoke, captured, tmp_path


def test_the_default_publish_names_loopback(docker_run):
    """No host named: the port is published on 127.0.0.1, not on every interface."""
    invoke, _, _ = docker_run
    argv = invoke()
    assert argv[argv.index("-p") + 1] == "127.0.0.1:8765:8765"


def test_the_default_run_argv_is_exactly_this(docker_run):
    """The whole argument list, so the mapping is pinned where it is built."""
    invoke, _, home = docker_run
    assert invoke() == [
        "docker",
        "run",
        "--rm",
        "-p",
        "127.0.0.1:8765:8765",
        "-e",
        "LIONAGI_STUDIO_HOST=127.0.0.1",
        "-v",
        f"{home}/.lionagi:/root/.lionagi",
        "--name",
        "lion-studio",
        "ghcr.io/ohdearquant/lion-studio:latest",
    ]


def test_a_port_flag_reaches_the_publish(docker_run):
    invoke, _, _ = docker_run
    argv = invoke("--port", "9001")
    assert argv[argv.index("-p") + 1] == "127.0.0.1:9001:8765"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::1"])
def test_an_explicit_address_is_the_published_address(docker_run, host):
    """`--host` already names the address to bind, so the publish follows it."""
    invoke, _, _ = docker_run
    argv = invoke("--host", host)
    expected = f"[{host}]" if ":" in host else host
    assert argv[argv.index("-p") + 1] == f"{expected}:8765:8765"


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "127.0.0.1:8765:8765"),
        ("0.0.0.0", "0.0.0.0:8765:8765"),
        ("::1", "[::1]:8765:8765"),
        ("::", "[::]:8765:8765"),
        ("fe80::1", "[fe80::1]:8765:8765"),
        ("[::1]", "[::1]:8765:8765"),
        ("192.168.1.5", "192.168.1.5:8765:8765"),
        # A blank value is the default, not a request to publish everywhere:
        # docker reads an empty host half as every interface. The brackets come
        # off before the blank is judged, so a bracketed blank is blank too.
        ("", "127.0.0.1:8765:8765"),
        ("  ", "127.0.0.1:8765:8765"),
        ("[]", "127.0.0.1:8765:8765"),
        ("[ ]", "127.0.0.1:8765:8765"),
    ],
)
def test_the_publish_spec_brackets_ipv6_literals_only(host, expected):
    """A literal IPv6 address needs brackets, or its colons read as separators."""
    from lionagi.studio.cli import _docker_publish_spec

    assert _docker_publish_spec(host, 8765) == expected


@pytest.mark.parametrize("host", ["[]", "[ ]", "", "  "])
def test_a_bracketed_blank_host_still_publishes_on_loopback(docker_run, host):
    """`--host "[]"` names no address and must not become an every-interface publish."""
    invoke, _, _ = docker_run
    argv = invoke("--host", host)
    assert argv[argv.index("-p") + 1] == "127.0.0.1:8765:8765"


def test_the_publish_spec_keeps_the_container_port():
    from lionagi.studio.cli import _docker_publish_spec

    assert _docker_publish_spec("127.0.0.1", 4321) == "127.0.0.1:4321:8765"


@pytest.mark.parametrize(
    "host",
    [
        "studio.example.com",  # docker does not resolve a name in the host half
        "localhost",
        "127.0.0.1:8765",  # an address carrying a port is not an address
        "fe80::1%en0",  # a zone suffix is not part of a publishable literal
        "1.2.3.4.5",
    ],
)
def test_a_host_that_is_not_an_address_is_refused(docker_run, host, capsys):
    """Docker would reject the mapping, so the CLI refuses it before docker runs."""
    import lionagi.studio.cli as studio_module

    _, captured, home = docker_run
    studio_module._start_docker(host=host, api_port=8765, frontend_port=3000)

    assert captured == [], f"docker ran with a publish docker rejects: {captured}"
    err = capsys.readouterr().err
    assert err.startswith("Error: --host "), err
    assert "cannot publish" in err or "not an IP address" in err


def test_a_refused_host_is_a_nonzero_launch(docker_run):
    """The refusal is the launch failing, reported by `main` as a nonzero result."""
    from lionagi.cli.main import main

    assert main(["studio", "--docker", "--host", "studio.example.com"]) == 2


def test_the_published_host_is_passed_to_the_daemon(docker_run):
    """The daemon admits a Host equal to the address it is reached by, so it is told."""
    invoke, _, _ = docker_run
    argv = invoke("--host", "192.168.1.5")
    assert "LIONAGI_STUDIO_HOST=192.168.1.5" in argv


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_a_wildcard_host_is_not_passed_to_the_daemon(docker_run, host):
    """0.0.0.0 and :: name no single address; the daemon keeps its loopback Hosts."""
    invoke, _, _ = docker_run
    argv = invoke("--host", host)
    assert not [a for a in argv if a.startswith("LIONAGI_STUDIO_HOST=")]


def test_the_token_name_reaches_the_container_without_its_value(docker_run, monkeypatch):
    """`-e NAME` lets docker read the value from the environment, so argv holds no secret."""
    invoke, _, _ = docker_run
    monkeypatch.setenv(TOKEN, "s3cret-token-value")
    argv = invoke("--host", "192.168.1.5")
    assert "-e" in argv
    assert TOKEN in argv
    assert "s3cret-token-value" not in " ".join(argv)


def test_no_token_in_the_environment_sends_no_token_flag(docker_run):
    invoke, _, _ = docker_run
    argv = invoke()
    assert TOKEN not in argv


def test_the_printed_url_names_the_published_host(docker_run, capsys):
    """Loopback by default, the given address otherwise, IPv6 in brackets."""
    invoke, _, _ = docker_run
    invoke("--host", "192.168.1.5")
    assert "Lion Studio: http://192.168.1.5:8765" in capsys.readouterr().out


def test_the_printed_url_brackets_an_ipv6_publish(docker_run, capsys):
    invoke, _, _ = docker_run
    invoke("--host", "::1")
    assert "Lion Studio: http://[::1]:8765" in capsys.readouterr().out


def test_the_default_printed_url_is_loopback(docker_run, capsys):
    invoke, _, _ = docker_run
    invoke()
    assert "Lion Studio: http://127.0.0.1:8765" in capsys.readouterr().out
