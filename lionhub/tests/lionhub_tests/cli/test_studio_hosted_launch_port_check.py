# SPDX-License-Identifier: Apache-2.0
"""``li studio`` stops before it issues a link when it cannot serve its port.

When nothing answers as a lionagi daemon, the hosted launch serves the page from this
process. A port held by another program would make that start fail only after the
link was printed or opened, leaving a page with a pairing code no daemon redeems.
These tests hold a real loopback port with a plain listener and check that the launch
refuses before anything is minted, printed or opened, and that a free port starts
as before.
"""

from __future__ import annotations

import errno
import socket

import pytest

import lionhub.cli.studio as studio_cli


@pytest.fixture
def launch(monkeypatch, capsys):
    """Record what the hosted launch mints, opens and starts, with no daemon answering."""
    calls: dict[str, list] = {"mint": [], "open": [], "serve": []}

    def mint(**kwargs):
        calls["mint"].append(kwargs)
        return "minted-code"

    def open_browser(url):
        calls["open"].append(url)
        return True

    def serve(host, port):
        calls["serve"].append((host, port))
        return 0

    import webbrowser

    import lionagi.studio.launch_access as launch_access

    monkeypatch.setattr(studio_cli, "_code_from_running_daemon", lambda port: None)
    monkeypatch.setattr(launch_access, "mint_pairing_code", mint)
    monkeypatch.setattr(webbrowser, "open", open_browser)
    monkeypatch.setattr(studio_cli, "_start_backend_only", serve)
    return calls


def _interactive(monkeypatch):
    """Mark the terminal interactive, so a launch that gets this far opens the browser.

    Called in the test body: the streams it patches are the ones the test runs with.
    """
    monkeypatch.setattr(studio_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(studio_cli.sys.stdout, "isatty", lambda: True)


@pytest.fixture
def held_port():
    """A loopback port a plain TCP listener holds, which never answers HTTP."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()


def _free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def test_bind_refusal_names_a_held_port(held_port):
    refusal = studio_cli._bind_refusal("127.0.0.1", held_port)

    assert isinstance(refusal, OSError)
    assert refusal.errno == errno.EADDRINUSE


def test_bind_refusal_accepts_a_free_port_and_leaves_it_free():
    port = _free_port()

    assert studio_cli._bind_refusal("127.0.0.1", port) is None
    # The probe closed its listener: the daemon can bind the port right after.
    assert studio_cli._bind_refusal("127.0.0.1", port) is None


def test_a_held_port_stops_the_launch_before_a_code_or_link(launch, held_port, capsys, monkeypatch):
    _interactive(monkeypatch)
    from lionagi.cli.main import main

    assert main(["studio", "--port", str(held_port)]) == 1

    assert launch == {"mint": [], "open": [], "serve": []}
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "#pair=" not in captured.err
    assert "Lion Studio did not start: " in captured.err
    assert (
        f"port {held_port} is in use, and nothing answered "
        f"http://127.0.0.1:{held_port}/health as a lionagi daemon in time"
    ) in captured.err
    assert (
        "Stop that program, or choose another port with `li studio --port <port>`." in captured.err
    )


def test_a_free_port_mints_opens_and_starts_the_daemon(launch, capsys, monkeypatch):
    _interactive(monkeypatch)
    port = _free_port()

    from lionagi.cli.main import main

    assert main(["studio", "--port", str(port)]) == 0

    assert launch["mint"] == [{"for_launch": True}]
    assert len(launch["open"]) == 1
    assert launch["open"][0].endswith("#pair=minted-code")
    assert launch["serve"] == [("127.0.0.1", port)]
    assert "did not start" not in capsys.readouterr().err


def test_a_daemon_already_answering_skips_the_bind_check(launch, held_port, capsys, monkeypatch):
    _interactive(monkeypatch)
    # A daemon answered, so the held port is that daemon's: no bind is tried for it.
    monkeypatch.setattr(studio_cli, "_code_from_running_daemon", lambda port: "code-from-daemon")
    from lionagi.cli.main import main

    assert main(["studio", "--port", str(held_port)]) == 0

    assert launch["mint"] == []
    assert launch["serve"] == []
    assert launch["open"] == [studio_cli._hosted_launch_url(held_port, "code-from-daemon")]
    captured = capsys.readouterr()
    assert "A daemon was already running on this port" in captured.out
    assert "did not start" not in captured.err


def test_a_daemon_already_answering_is_never_probed_for_a_bind(launch, capsys, monkeypatch):
    """The same property as above with the bind check spied on, so no socket is needed."""
    _interactive(monkeypatch)
    probed: list[tuple[str, int]] = []

    def refuse(host, port):
        probed.append((host, port))
        return OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(studio_cli, "_code_from_running_daemon", lambda port: "code-from-daemon")
    monkeypatch.setattr(studio_cli, "_bind_refusal", refuse)
    from lionagi.cli.main import main

    assert main(["studio", "--port", "8791"]) == 0

    assert probed == []
    assert launch["mint"] == []
    assert launch["serve"] == []
    assert launch["open"] == [studio_cli._hosted_launch_url(8791, "code-from-daemon")]
    assert "A daemon was already running on this port" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("refusal", "advice", "not_advice"),
    [
        pytest.param(
            OSError(errno.EADDRINUSE, "Address already in use"),
            [
                "port {port} is in use, and nothing answered http://127.0.0.1:{port}/health "
                "as a lionagi daemon in time",
                "Stop that program, or choose another port with `li studio --port <port>`.",
            ],
            ["--host", "already holds"],
            id="port-in-use",
        ),
        pytest.param(
            OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address"),
            ["127.0.0.1 is not an address this machine can listen on", "Check `--host`."],
            ["Stop that program", "--port <port>"],
            id="address-not-available",
        ),
        pytest.param(
            socket.gaierror(socket.EAI_NONAME, "Name or service not known"),
            ["127.0.0.1 is not an address this machine can listen on", "Check `--host`."],
            ["Stop that program", "--port <port>"],
            id="host-does-not-resolve",
        ),
        pytest.param(
            OSError(errno.EACCES, "Permission denied"),
            [
                "port {port} needs privileges",
                "Choose another port with `li studio --port <port>`.",
            ],
            ["Stop that program", "--host"],
            id="port-needs-privileges",
        ),
        pytest.param(
            OSError(errno.EMFILE, "Too many open files"),
            [
                "cannot listen on 127.0.0.1:{port}",
                "Too many open files",
                "Choose another port with `li studio --port <port>`.",
            ],
            ["Stop that program", "--host"],
            id="any-other-error",
        ),
    ],
)
def test_each_bind_failure_gives_advice_that_fits_its_cause(
    launch, capsys, monkeypatch, refusal, advice, not_advice
):
    _interactive(monkeypatch)
    # The refusal is stubbed, so no socket is opened and any port number will do.
    port = 8791
    monkeypatch.setattr(studio_cli, "_bind_refusal", lambda _host, _port: refusal)
    from lionagi.cli.main import main

    assert main(["studio", "--port", str(port)]) == 1

    assert launch == {"mint": [], "open": [], "serve": []}
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "#pair=" not in captured.err
    assert "Lion Studio did not start: " in captured.err
    for text in advice:
        assert text.format(port=port) in captured.err
    for text in not_advice:
        assert text not in captured.err
