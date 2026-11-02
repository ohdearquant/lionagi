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

import lionagi.studio.cli as studio_cli


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
    assert f"a program already holds port {held_port}" in captured.err
    assert f"http://127.0.0.1:{held_port}/health" in captured.err
    assert "li studio --port <port>" in captured.err


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
