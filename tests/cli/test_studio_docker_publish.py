# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The published address in the `docker run` argument list `li studio --docker` builds.

A bare `PORT:PORT` mapping publishes the container's port on every host
interface, and with no auth token configured nothing else keeps other machines on
the network from the API. The host half of the mapping is therefore what
decides who can reach the port. These tests read the argv the
CLI hands docker, faking the subprocess the way the mount tests do, so no
docker is needed.

The daemon answers only a request whose `Host` header names an address it
expects, so the tests that care about reachability call that guard in-process
with the environment the CLI passes to the container, rather than starting a
server.
"""

from __future__ import annotations

from pathlib import Path

import pytest

TOKEN = "LIONAGI_STUDIO_AUTH_TOKEN"


def _guard_admits(host_header: str, bind_host: str) -> bool:
    """Whether the daemon's Host guard admits *host_header* with *bind_host* set.

    Calls the real middleware in-process; no server is started.
    """
    pytest.importorskip("fastapi", reason="studio extra not installed")
    import asyncio
    import os

    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from lionagi.studio.app import validate_host_header

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/plugins",
            "headers": [(b"host", host_header.encode())],
        }
    )

    async def call_next(_request):
        return JSONResponse({"ok": True})

    saved = os.environ.get("LIONAGI_STUDIO_HOST")
    os.environ["LIONAGI_STUDIO_HOST"] = bind_host
    try:
        response = asyncio.run(validate_host_header(request, call_next))
    finally:
        if saved is None:
            os.environ.pop("LIONAGI_STUDIO_HOST", None)
        else:
            os.environ["LIONAGI_STUDIO_HOST"] = saved
    return response.status_code != 400


def _bind_host_from_argv(argv: list[str]) -> str:
    """The bind host the CLI passes to the container, or the image's default."""
    for argument in argv:
        if argument.startswith("LIONAGI_STUDIO_HOST="):
            return argument.split("=", 1)[1]
    return "127.0.0.1"


def _host_header(host: str) -> str:
    """*host* as a browser sends it in a Host header: an IPv6 literal in brackets."""
    return f"[{host}]" if ":" in host else host


def _printed_host(out: str) -> str:
    """The host of the `Lion Studio:` URL the CLI prints."""
    prefix = "Lion Studio: http://"
    for line in out.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].rsplit(":", 1)[0]
    raise AssertionError(f"no printed Studio URL in {out!r}")


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
def test_a_wildcard_host_is_passed_to_the_daemon(docker_run, host):
    """The wildcard is passed too, so the container's startup warning names the same
    address the publish does. No client-reachable Host is added: `0.0.0.0` is never
    an allowed name, and `::` is not an address a client connects to, so the
    loopback names stay the ones a client uses.
    """
    invoke, _, _ = docker_run
    argv = invoke("--host", host)
    assert f"LIONAGI_STUDIO_HOST={host}" in argv
    for loopback in ("localhost", "127.0.0.1", "::1"):
        assert _guard_admits(
            f"{loopback}:8765" if ":" not in loopback else f"[{loopback}]:8765", host
        )


def test_the_wildcard_does_not_admit_a_concrete_lan_address(docker_run):
    """Passing the wildcard must not turn it into an admitted Host."""
    invoke, _, _ = docker_run
    invoke("--host", "0.0.0.0")
    assert not _guard_admits("192.168.1.5:8765", "0.0.0.0")


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


def test_a_failed_run_is_the_launch_return_code(tmp_path, monkeypatch):
    """`docker run` refusing the mapping (a non-zero exit) must not read as success."""
    import lionagi.studio.cli as studio_module

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(studio_module, "_has_docker", lambda: True)

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))

        class Result:
            returncode = 125 if cmd[1] == "run" else 0
            stderr = b""

        return Result()

    monkeypatch.setattr(studio_module.subprocess, "run", fake_run)
    from lionagi.cli.main import main

    assert main(["studio", "--docker"]) == 125
    assert calls[1][1] == "run", calls


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


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_the_wildcard_printed_url_is_localhost(docker_run, capsys, host):
    """A wildcard names no address a browser can open, so the URL says localhost."""
    invoke, _, _ = docker_run
    invoke("--host", host)
    assert "Lion Studio: http://localhost:8765" in capsys.readouterr().out


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "0.0.0.0",
        "::1",
        "::",
        "fe80::1",
        "[::1]",
        "192.168.1.5",
        "::ffff:192.168.1.5",
        "",
        "  ",
        "[]",
        "[ ]",
    ],
)
def test_the_printed_host_is_admitted_by_the_daemon(docker_run, capsys, host):
    """Every `--host` the CLI accepts prints a URL the daemon's Host guard admits
    with the environment the CLI passes to the container.

    The guard reads the Host header, never the source address, so a URL whose host
    it refuses is a link that answers 400 even from this machine.
    """
    invoke, _, _ = docker_run
    argv = invoke("--host", host)
    printed = _printed_host(capsys.readouterr().out)
    bind_host = _bind_host_from_argv(argv)
    assert _guard_admits(f"{printed}:8765", bind_host), (
        f"the daemon refuses Host {printed}:8765 with {bind_host!r} set"
    )
    if bind_host in ("0.0.0.0", "::"):
        # The check above only fails for a wildcard if the wildcard itself is
        # refused: a URL that regressed to `[::]` would otherwise still be
        # admitted and turn no assertion red.
        assert not _guard_admits(f"{_host_header(host)}:8765", bind_host), (
            f"the daemon admits Host {_host_header(host)}:8765 with {bind_host!r} set"
        )


def test_the_publish_spec_normalizes_an_ipv4_mapped_address():
    """`::ffff:192.168.1.5` is the IPv4 address 192.168.1.5 in another spelling.

    Docker publishes it as an IPv6 literal, and a browser reaching it sends the
    hexadecimal form, which the daemon's IPv6 Host grammar (no dots) refuses. The
    IPv4 form is what gets published, printed and passed on.
    """
    from lionagi.studio.cli import _docker_publish_spec, _published_host

    assert _published_host("::ffff:192.168.1.5") == "192.168.1.5"
    assert _docker_publish_spec("::ffff:192.168.1.5", 8765) == "192.168.1.5:8765:8765"


def test_a_mapped_address_reaches_the_daemon_as_its_ipv4_form(docker_run, capsys):
    invoke, _, _ = docker_run
    argv = invoke("--host", "::ffff:192.168.1.5")
    assert argv[argv.index("-p") + 1] == "192.168.1.5:8765:8765"
    assert "LIONAGI_STUDIO_HOST=192.168.1.5" in argv
    assert "Lion Studio: http://192.168.1.5:8765" in capsys.readouterr().out
    assert _guard_admits("192.168.1.5:8765", _bind_host_from_argv(argv))


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_a_wildcard_bind_host_is_not_admitted_as_a_host(docker_run, host):
    """The address the CLI publishes on may be a wildcard, but the wildcard is not
    a name a page is opened at, so the daemon must never accept it back as a Host.
    Admitting one would let any page reach the API by naming the wildcard, and it
    would leave the printed-URL checks unable to fail for it."""
    invoke, _, _ = docker_run
    invoke("--host", host)

    assert not _guard_admits(f"{_host_header(host)}:8765", host)
    assert not _guard_admits(_host_header(host), host)


@pytest.mark.parametrize("host", ["[::1]:8765", "127.0.0.1:8765"])
def test_an_address_carrying_a_port_names_the_port_flag(docker_run, host, capsys):
    """`--host` takes an address, not an address and port, so a value carrying a
    port is refused with a message that says where the port goes."""
    from lionagi.cli.main import main

    _, captured, _ = docker_run
    assert main(["studio", "--docker", "--host", host]) == 2
    assert captured == [], f"docker ran with a publish docker rejects: {captured}"
    err = capsys.readouterr().err
    assert "--port" in err, err


def test_an_interrupt_during_the_pull_is_not_a_failed_launch(tmp_path, monkeypatch, capsys):
    """Ctrl-C while docker pulls stops the launch the way one during the run does:
    `li studio --docker` returns 0 instead of letting the interrupt escape."""
    import lionagi.studio.cli as studio_module

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(studio_module, "_has_docker", lambda: True)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["docker", "pull"]:
            raise KeyboardInterrupt

        class Result:
            returncode = 0
            stderr = b""

        return Result()

    monkeypatch.setattr(studio_module.subprocess, "run", fake_run)
    from lionagi.cli.main import main

    escaped: BaseException | None = None
    rc = None
    try:
        rc = main(["studio", "--docker"])
    except KeyboardInterrupt as exc:
        escaped = exc
    assert escaped is None, "a KeyboardInterrupt during docker pull escaped the launch"
    assert rc == 0
