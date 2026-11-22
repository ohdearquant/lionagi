# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A Claude child reads its MCP server set from a private file, never from its command line.

A server set can hold a bearer token in a header or a key in a server's ``env``. Rendered inline
after ``--mcp-config`` it sat in the child's argument list for the child's whole life, where any
local user's process listing shows it, and outside the values the spawn removes from its logs.
The provider now writes the set to a file only this user can read, names the file on the command
line, removes it when the child is done, and tells the spawn which values to keep out of messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import stat
import sys
from pathlib import Path

import pytest

from lionhub.providers import _cli_subprocess as cs
from lionhub.providers.anthropic import claude_code as cc

TOKEN = "example-mcp-bearer-0123456789"  # noqa: S105 — a test value, never a real credential
ENV_KEY = "example-server-key-0123456789"  # noqa: S105 — a test value, never a real credential
SERVERS = {
    "remote": {
        "type": "http",
        "url": "http://127.0.0.1:1/mcp",
        "headers": {"Authorization": f"Bearer {TOKEN}"},
    },
    "local": {
        "command": "some-server",
        "env": {"SERVICE_API_KEY": ENV_KEY, "LOG_LEVEL": "debug-and-verbose"},
    },
}

# A stand-in ``claude``: reports what it was handed, as the one result line the provider reads.
_REPORTS = (
    "import json, os, stat, sys\n"
    "argv = sys.argv[1:]\n"
    "path = argv[argv.index('--mcp-config') + 1]\n"
    "info = os.stat(path)\n"
    "seen = {\n"
    "    'path': path,\n"
    "    'argv': argv,\n"
    "    'content': json.load(open(path)),\n"
    "    'file_mode': stat.S_IMODE(info.st_mode),\n"
    "    'dir_mode': stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode),\n"
    "}\n"
    "print(json.dumps({'type': 'result', 'is_error': False, 'result': json.dumps(seen)}))\n"
)
# A stand-in that prints the token and the key it read from the file, in no recognisable shape,
# and then fails.
_LEAKS_THEN_FAILS = (
    "import json, pathlib, sys\n"
    "argv = sys.argv[1:]\n"
    "path = argv[argv.index('--mcp-config') + 1]\n"
    "pathlib.Path(sys.argv[0] + '.path').write_text(path)\n"
    "servers = json.load(open(path))['mcpServers']\n"
    "token = servers['remote']['headers']['Authorization'].split()[-1]\n"
    "key = servers['local']['env']['SERVICE_API_KEY']\n"
    "sys.stderr.write('upstream refused ' + token + ' and ' + key)\n"
    "sys.exit(3)\n"
)
# A stand-in that records where its file is, prints one first line and then stays alive.
_PRINTS_THEN_WAITS = (
    "import json, pathlib, sys, time\n"
    "argv = sys.argv[1:]\n"
    "path = argv[argv.index('--mcp-config') + 1]\n"
    "pathlib.Path(sys.argv[0] + '.path').write_text(path)\n"
    "print(json.dumps({'type': 'system'}), flush=True)\n"
    "time.sleep(30)\n"
)


def _stub_cli(tmp_path: Path, monkeypatch, body: str) -> Path:
    stub = tmp_path / "stub-cli"
    stub.write_text(f"#!{sys.executable} -I\n{body}")
    stub.chmod(0o755)
    monkeypatch.setattr(cc, "resolve_claude_cli", lambda: str(stub))
    return stub


def _capture_spawn(monkeypatch) -> dict:
    """Replace the provider's spawn with one that records what it was called with."""
    captured: dict = {}

    async def fake(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured.update(kwargs)
        path = cmd[cmd.index("--mcp-config") + 1] if "--mcp-config" in cmd else None
        captured["file_existed"] = bool(path) and Path(path).is_file()
        return
        yield

    monkeypatch.setattr(cc, "ndjson_from_cli", fake)
    monkeypatch.setattr(cc, "resolve_claude_cli", lambda: "claude")
    return captured


async def _drain(stream) -> list:
    async with contextlib.aclosing(stream):
        return [obj async for obj in stream]


@pytest.mark.asyncio
async def test_the_child_is_handed_a_private_file_and_no_secret_on_its_command_line(
    tmp_path, monkeypatch
):
    _stub_cli(tmp_path, monkeypatch, _REPORTS)
    request = cc.ClaudeCodeRequest(prompt="hi", repo=tmp_path, mcp_servers=SERVERS)

    results = [o for o in await _drain(cc._ndjson_from_cli(request)) if o.get("type") == "result"]
    seen = json.loads(results[-1]["result"])

    assert not any(TOKEN in arg or ENV_KEY in arg for arg in seen["argv"])
    assert seen["content"] == {"mcpServers": SERVERS}
    assert seen["file_mode"] == stat.S_IRUSR | stat.S_IWUSR
    # The file lived for the child and is gone with it, directory included.
    assert not Path(seen["path"]).exists()
    assert not Path(seen["path"]).parent.exists()


@pytest.mark.asyncio
async def test_the_child_finds_the_directory_of_its_file_open_to_its_owner_alone(
    tmp_path, monkeypatch
):
    _stub_cli(tmp_path, monkeypatch, _REPORTS)
    request = cc.ClaudeCodeRequest(prompt="hi", repo=tmp_path, mcp_servers=SERVERS)

    results = [o for o in await _drain(cc._ndjson_from_cli(request)) if o.get("type") == "result"]
    seen = json.loads(results[-1]["result"])

    assert seen["dir_mode"] == stat.S_IRWXU


@pytest.mark.asyncio
async def test_a_failing_child_leaves_no_file_and_its_message_carries_no_secret(
    tmp_path, monkeypatch
):
    stub = _stub_cli(tmp_path, monkeypatch, _LEAKS_THEN_FAILS)
    request = cc.ClaudeCodeRequest(prompt="hi", repo=tmp_path, mcp_servers=SERVERS)

    with pytest.raises(RuntimeError) as raised:
        await _drain(cc._ndjson_from_cli(request))

    assert "upstream refused" in str(raised.value)
    assert TOKEN not in str(raised.value)
    assert ENV_KEY not in str(raised.value)
    handed = Path(Path(f"{stub}.path").read_text())
    assert not handed.exists()
    assert not handed.parent.exists()


@pytest.mark.asyncio
async def test_the_spawn_is_told_which_values_of_the_server_set_are_secret(tmp_path, monkeypatch):
    captured = _capture_spawn(monkeypatch)
    request = cc.ClaudeCodeRequest(prompt="hi", repo=tmp_path, mcp_servers=SERVERS)

    await _drain(cc._ndjson_from_cli(request))

    assert captured["file_existed"] is True
    assert not any(TOKEN in arg or ENV_KEY in arg for arg in captured["cmd"])
    declared = set(captured["secret_values"])
    # A header is declared whole and by its last word; an env value only under a credential name.
    assert {f"Bearer {TOKEN}", TOKEN, ENV_KEY} <= declared
    assert "debug-and-verbose" not in declared


@pytest.mark.asyncio
async def test_an_empty_server_set_stays_inline_and_writes_no_file(tmp_path, monkeypatch):
    captured = _capture_spawn(monkeypatch)
    request = cc.ClaudeCodeRequest(prompt="hi", repo=tmp_path, mcp_servers={})

    await _drain(cc._ndjson_from_cli(request))

    cmd = captured["cmd"]
    assert cmd[cmd.index("--mcp-config") + 1] == json.dumps({"mcpServers": {}})
    assert captured["file_existed"] is False
    assert list(captured["secret_values"]) == []


@pytest.mark.asyncio
async def test_a_named_config_wins_and_no_file_is_written(tmp_path, monkeypatch):
    captured = _capture_spawn(monkeypatch)
    named = json.dumps({"mcpServers": {}})
    request = cc.ClaudeCodeRequest(
        prompt="hi", repo=tmp_path, mcp_config=named, mcp_servers=SERVERS
    )

    await _drain(cc._ndjson_from_cli(request))

    cmd = captured["cmd"]
    assert cmd.count("--mcp-config") == 1
    assert cmd[cmd.index("--mcp-config") + 1] == named


def test_the_argument_list_names_the_file_only_when_one_was_written(tmp_path):
    request = cc.ClaudeCodeRequest(prompt="hi", repo=tmp_path, mcp_servers=SERVERS)

    inline = request.as_cmd_args()
    assert inline[inline.index("--mcp-config") + 1] == json.dumps({"mcpServers": SERVERS})

    by_file = request.as_cmd_args(mcp_servers_file=tmp_path / "mcp.json")
    assert by_file[by_file.index("--mcp-config") + 1] == str(tmp_path / "mcp.json")
    assert not any(TOKEN in arg for arg in by_file)


def test_the_private_file_holds_the_set_and_only_its_owner_can_read_it():
    directory, path = cc._write_private_mcp_servers(SERVERS)
    try:
        assert json.loads(path.read_text()) == {"mcpServers": SERVERS}
        assert stat.S_IMODE(path.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR
        assert path.parent == Path(directory)
    finally:
        path.unlink()
        Path(directory).rmdir()


def test_the_private_directory_is_open_to_its_owner_alone():
    directory, path = cc._write_private_mcp_servers(SERVERS)
    try:
        assert stat.S_IMODE(Path(directory).stat().st_mode) == stat.S_IRWXU
    finally:
        path.unlink()
        Path(directory).rmdir()


def test_a_server_that_is_not_a_mapping_declares_nothing():
    assert cc._mcp_server_secret_values(None) == []
    assert cc._mcp_server_secret_values({"odd": "not-a-mapping", "bare": {"command": "x"}}) == []
    assert cc._mcp_server_secret_values({"s": {"headers": {"X-Key": "one-word-value"}}}) == [
        "one-word-value"
    ]


@pytest.mark.asyncio
async def test_a_declared_value_is_kept_out_of_a_failing_childs_message():
    leak = f"import sys; sys.stderr.write('child saw ' + {TOKEN!r}); sys.exit(3)"

    with pytest.raises(RuntimeError) as raised:
        await _drain(
            cs.ndjson_from_cli([sys.executable, "-c", leak], engine="claude", secret_values=[TOKEN])
        )

    assert "child saw" in str(raised.value)
    assert TOKEN not in str(raised.value)


@pytest.mark.asyncio
async def test_an_undeclared_value_is_quoted_as_the_child_wrote_it():
    """The control for the test above: without the declaration the same child is quoted whole."""
    leak = f"import sys; sys.stderr.write('child saw ' + {TOKEN!r}); sys.exit(3)"

    with pytest.raises(RuntimeError) as raised:
        await _drain(cs.ndjson_from_cli([sys.executable, "-c", leak], engine="claude"))

    assert TOKEN in str(raised.value)


def test_a_declared_value_under_the_length_floor_is_not_redacted():
    assert cs._declared_secret_values(["short", "long-enough-value"]) == {
        "declared value 2": "long-enough-value"
    }
    assert cs._declared_secret_values(None) == {}


@pytest.mark.asyncio
async def test_closing_the_stream_after_its_first_item_removes_the_directory(tmp_path, monkeypatch):
    stub = _stub_cli(tmp_path, monkeypatch, _PRINTS_THEN_WAITS)
    request = cc.ClaudeCodeRequest(prompt="hi", repo=tmp_path, mcp_servers=SERVERS)
    stream = cc._ndjson_from_cli(request)
    try:
        first = await stream.__anext__()
        handed = Path(Path(f"{stub}.path").read_text())
        # While the child is alive the file is there for it to read.
        assert first == {"type": "system"}
        assert handed.is_file()
    finally:
        await stream.aclose()

    assert not handed.exists()
    assert not handed.parent.exists()


@pytest.mark.asyncio
async def test_cancelling_the_reader_while_the_child_runs_removes_the_directory(
    tmp_path, monkeypatch
):
    stub = _stub_cli(tmp_path, monkeypatch, _PRINTS_THEN_WAITS)
    request = cc.ClaudeCodeRequest(prompt="hi", repo=tmp_path, mcp_servers=SERVERS)
    first_seen = asyncio.Event()

    async def read() -> None:
        async for _ in cc._ndjson_from_cli(request):
            first_seen.set()

    reader = asyncio.create_task(read())
    await asyncio.wait_for(first_seen.wait(), timeout=20)
    handed = Path(Path(f"{stub}.path").read_text())
    assert handed.is_file()

    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader

    assert not handed.exists()
    assert not handed.parent.exists()


MIDDLE = "middle-word-0123456789"
LAST = "last-word-0123456789"
# A value with a double quote, a backslash and a non-ASCII letter: JSON writes each differently.
TRICKY = 'pa"ss\\wörd-0123456789'


def _child_says(text: str) -> list[str]:
    script = (
        "import sys; sys.stderr.buffer.write(('child saw ' + "
        f"{text!r}).encode('utf-8')); sys.exit(3)"
    )
    return [sys.executable, "-c", script]


def test_every_word_after_the_first_of_a_header_is_declared_once():
    header = f"Scheme {MIDDLE} {LAST}"
    assert cc._mcp_server_secret_values({"s": {"headers": {"X-Auth": header}}}) == [
        header,
        MIDDLE,
        LAST,
    ]

    digest = 'Digest username="bob", response="abc"'
    declared = cc._mcp_server_secret_values({"s": {"headers": {"Authorization": digest}}})
    assert declared[0] == digest
    assert {'username="bob",', 'response="abc"'} <= set(declared)
    assert len(declared) == len(set(declared))

    same = {"headers": {"A": "plain-value-0123", "B": "plain-value-0123"}}
    assert cc._mcp_server_secret_values({"s": same}) == ["plain-value-0123"]


def test_a_value_is_also_declared_in_the_form_it_takes_inside_a_json_string():
    declared = cc._mcp_server_secret_values({"s": {"env": {"SERVICE_API_KEY": TRICKY}}})
    assert declared == [
        TRICKY,
        json.dumps(TRICKY)[1:-1],
        json.dumps(TRICKY, ensure_ascii=False)[1:-1],
    ]


@pytest.mark.asyncio
async def test_a_middle_word_of_a_header_is_kept_out_of_a_failing_childs_message():
    declared = cc._mcp_server_secret_values(
        {"s": {"headers": {"X-Auth": f"Scheme {MIDDLE} {LAST}"}}}
    )

    with pytest.raises(RuntimeError) as raised:
        await _drain(
            cs.ndjson_from_cli(_child_says(MIDDLE), engine="claude", secret_values=declared)
        )

    assert "child saw" in str(raised.value)
    assert MIDDLE not in str(raised.value)


@pytest.mark.parametrize("ensure_ascii", [True, False])
@pytest.mark.asyncio
async def test_a_value_a_child_prints_json_escaped_is_kept_out_of_its_message(ensure_ascii):
    printed = json.dumps(TRICKY, ensure_ascii=ensure_ascii)[1:-1]
    assert printed != TRICKY
    declared = cc._mcp_server_secret_values({"s": {"env": {"SERVICE_API_KEY": TRICKY}}})

    with pytest.raises(RuntimeError) as raised:
        await _drain(
            cs.ndjson_from_cli(_child_says(printed), engine="claude", secret_values=declared)
        )

    assert "child saw" in str(raised.value)
    assert printed not in str(raised.value)


@pytest.mark.asyncio
async def test_a_json_escaped_form_that_is_not_declared_is_quoted_as_the_child_wrote_it():
    """The control for the test above: declare only the plain value and the escaped form shows."""
    printed = json.dumps(TRICKY)[1:-1]

    with pytest.raises(RuntimeError) as raised:
        await _drain(
            cs.ndjson_from_cli(_child_says(printed), engine="claude", secret_values=[TRICKY])
        )

    assert printed in str(raised.value)
