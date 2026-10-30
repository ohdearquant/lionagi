# Copyright (c) 2023-2026, LionAGI contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

import pytest

from lionagi.agent import _email_in_headers as predicate
from lionagi.agent.hooks import guard_email_in_headers
from lionagi.agent.spec import AgentSpec

ADDRESS = "sample-person@project.example"
OTHER = "another-person@project.example"

LEAKS = [
    'curl -s -A "release-check (sample-person@project.example)" https://packages.example/',
    'UA="release-check (sample-person@project.example)"; curl -s -H "User-Agent: $UA" https://packages.example/',
    "curl -s -A 'Service sample-person@project.example' https://data.example/ -o /tmp/result.json",
    "python3 - <<'EOF'\nimport requests\n"
    'r = requests.get(url, headers={"User-Agent": "research/0.1 (sample-person@project.example)"})\nEOF',
    "cat > /tmp/example.py <<'PY'\nimport urllib.request\n"
    "req = urllib.request.Request(url, headers={'User-Agent':'lookup (sample-person@project.example)'})\nPY",
    'curl -s -H "X-Contact: sample-person@project.example" https://data.example/',
    "curl --user-agent 'jobs/1.0 (sample-person@project.example)' https://tiles.example/1/1/1.png",
]

CONTROLS = [
    'curl -s -A "release-check/1.0 (+https://project.example)" https://packages.example/',
    'curl -s -H "User-Agent: product/0.9 (+https://project.example)" https://data.example/',
    "curl -s -A 'release-check (sample@example.com)' https://packages.example/",
    'UA="release-check (sample@example.com)"; echo "$UA"',
    "git -c user.email=sample-person@project.example commit -m x",
    "mail-tool send sample-person@project.example",
    "python3 - <<'EOF'\nmsg['From'] = 'Sample <sample-person@project.example>'\n"
    "msg['To'] = 'another-person@project.example'\nEOF",
    "rg -n 'sample-person@project.example' docs | head",
    "ls -H /tmp; echo sample-person@project.example is on the resume",
    'git commit -a --author="Sample <sample-person@project.example>" -m x',
    "ssh -h build.example.test echo sample-person@project.example",
]


@pytest.mark.parametrize("command", LEAKS)
async def test_each_header_leak_is_refused_with_a_passing_control(command):
    with pytest.raises(PermissionError) as error:
        await guard_email_in_headers("bash", "", {"command": command})
    assert "outbound HTTP header" in str(error.value)
    assert "sam…@project.example" in str(error.value)
    assert ADDRESS not in str(error.value)
    assert await guard_email_in_headers("bash", "", {"command": CONTROLS[0]}) is None


@pytest.mark.parametrize("command", CONTROLS)
async def test_each_control_passes_with_a_refused_probe(command):
    assert await guard_email_in_headers("bash", "", {"command": command}) is None
    with pytest.raises(PermissionError):
        await guard_email_in_headers("bash", "", {"command": LEAKS[0]})


@pytest.mark.parametrize("action,field", [("write", "content"), ("edit", "new_string")])
async def test_editor_written_text_is_checked(action, field):
    body = 'HEADERS = {"User-Agent": "product (sample-person@project.example)"}'
    with pytest.raises(PermissionError):
        await guard_email_in_headers("editor", action, {field: body})
    assert (
        await guard_email_in_headers("editor", action, {field: 'headers={"User-Agent": "product"}'})
        is None
    )


def test_find_leak_unit_shapes():
    assert predicate.find_leak('headers={"User-Agent": "x (sample-person@project.example)"}') == (
        ADDRESS,
        "headers=",
    )
    assert predicate.find_leak('-A "x (sample@example.com)"') is None
    assert predicate.find_leak("no header here sample-person@project.example") is None
    assert predicate.find_leak("") is None


async def test_allowlist_and_note_apply_only_to_the_configured_address():
    allowed = frozenset({ADDRESS})
    assert await guard_email_in_headers("bash", "", {"command": LEAKS[0]}, allow=allowed) is None
    with pytest.raises(PermissionError, match="Deployment policy") as error:
        await guard_email_in_headers(
            "bash",
            "",
            {"command": f'curl -H "X-Contact: {OTHER}"'},
            allow=allowed,
            note="Deployment policy",
        )
    assert OTHER not in str(error.value)


async def test_allowed_first_address_does_not_hide_later_disallowed_address():
    text = f'headers={{"X-Contact": "{ADDRESS}", "X-Other": "{OTHER}"}}'
    with pytest.raises(PermissionError) as error:
        await guard_email_in_headers("bash", "", {"command": text}, allow=frozenset({ADDRESS}))
    assert "ano…@project.example" in str(error.value)


async def test_agent_allowlists_are_independent():
    async def call(allow):
        await asyncio.sleep(0)
        try:
            await guard_email_in_headers("bash", "", {"command": LEAKS[0]}, allow=allow)
        except PermissionError:
            return "blocked"
        return "allowed"

    assert await asyncio.gather(call(frozenset({ADDRESS})), call(frozenset())) == [
        "allowed",
        "blocked",
    ]
    assert predicate.find_leak(LEAKS[0]) is not None


def test_secure_coding_preset_wires_bash_and_editor_guards():
    spec = AgentSpec.coding()
    assert guard_email_in_headers in spec.hook_handlers["security_pre:bash"]
    assert guard_email_in_headers in spec.hook_handlers["security_pre:editor"]
    unsafe = AgentSpec.coding(secure=False)
    assert guard_email_in_headers not in unsafe.hook_handlers.get("security_pre:bash", [])


@pytest.mark.parametrize("action,field", [("write", "content"), ("edit", "new_string")])
async def test_secure_agent_editor_preprocessor_refuses_written_headers(tmp_path, action, field):
    from lionagi.agent.factory import create_agent
    from lionagi.agent.gate import GateDeniedError

    branch = await create_agent(AgentSpec.coding(cwd=str(tmp_path)), load_settings=False)
    args = {"action": action, "file_path": str(tmp_path / "output.txt"), field: LEAKS[0]}
    with pytest.raises(GateDeniedError) as error:
        await branch.acts.registry["editor"].preprocessor(args)
    assert error.value.result.control == "guard_email_in_headers"


async def test_security_recheck_catches_a_user_hook_rewrite(tmp_path):
    from lionagi.agent.factory import create_agent
    from lionagi.agent.gate import GateDeniedError

    spec = AgentSpec.coding(cwd=str(tmp_path))

    async def rewrite(_name, _action, args):
        return {**args, "command": LEAKS[0]}

    spec.pre("bash", rewrite)
    branch = await create_agent(spec, load_settings=False)
    with pytest.raises(GateDeniedError) as error:
        await branch.acts.registry["bash"].preprocessor({"action": "run", "command": "echo safe"})
    assert error.value.result.control == "guard_email_in_headers"
