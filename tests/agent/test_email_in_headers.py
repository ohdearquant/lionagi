# Copyright (c) 2023-2026, LionAGI contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import inspect
import signal
import time

import pytest

from lionagi.agent import _email_in_headers as predicate
from lionagi.agent.hooks import guard_email_in_headers
from lionagi.agent.spec import AgentSpec


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="requires a real-time signal timer")
def test_repeated_header_prefixes_are_scanned_promptly():
    # Include the address delimiter so find_leak takes its scanning path.
    text = "x-" * 100_000 + chr(64)

    def timed_out(_signum, _frame):
        raise AssertionError("header scan exceeded five seconds")

    previous_handler = signal.signal(signal.SIGALRM, timed_out)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 5)
    try:
        started = time.perf_counter()
        assert predicate.find_leak(text) is None
        assert time.perf_counter() - started < 5
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def test_assignment_matching_handles_long_blank_lines_promptly():
    text = " \n" * 200_000 + "x" + chr(64)
    started = time.perf_counter()
    assert predicate.find_leak(text) is None
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    "name", ["X-Contact", "X-" + "a" * 128, "X-" + "a" * 129, "X-" + "a" * 5000, "Foo-X-Contact"]
)
def test_x_header_names_still_refuse_addresses(name):
    address = "sample.person" + chr(64) + "corp.co"
    result = predicate.find_leak(f"{name}: {address}")
    assert result is not None
    assert result[0] == address


def test_long_name_after_user_agent_token_is_checked():
    address = "sample.person" + chr(64) + "corp.co"
    name = "user-agent" + "-a" * 300 + "-X-Foo"
    result = predicate.find_leak(f"{name}: {address}")
    assert result is not None
    assert result[0] == address


def test_x_header_with_allowed_domain_remains_allowed():
    address = "sample.person" + chr(64) + "example.com"
    assert predicate.find_leak(f"X-Contact: {address}") is None


ADDRESS = "sample-person@project.example"
OTHER = "another-person@project.example"


def test_public_guard_signature_contains_no_private_parameters():
    assert not any(
        name.startswith("_") for name in inspect.signature(guard_email_in_headers).parameters
    )


def test_direct_predicate_uses_only_builtin_allowed_domains():
    text = f'headers={{"X-Contact": "{ADDRESS}"}}'
    assert predicate.find_leak(text) is not None
    assert predicate.find_leak_with_allowed(text, frozenset({ADDRESS})) is None
    assert predicate.find_leak(text) is not None


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


@pytest.mark.parametrize(
    "template",
    [
        'curl -sH "From: {address}"',
        'curl -sSA "{address}"',
        "curl -Akfin({address}) u",
        "curl -Atool/0.1({address})",
        'curl -H"From: {address}" u',
        "curl -A'tool/0.1 ({address})' u",
        'curl -sH"From: {address}" u',
        "wget -U'x {address}' u",
        "wget -Ux({address}) u",
        "curl -HFrom:{address}",
        'wget -U "{address}"',
        "req.headers['From'] = \"{address}\"",
        'headers["From"] = "{address}"',
        'h["User-Agent"] = "x ({address})"',
    ],
)
def test_widened_header_forms_are_refused(template):
    address = "sample.person" + chr(64) + "corp.co"
    assert predicate.find_leak(template.format(address=address)) is not None


@pytest.mark.parametrize(
    "template",
    [
        "grep -A3 {address} file",
        "ls -lA file; echo {address}",
        "openssl dgst -SHA256 {address}",
        "diff -U3 {address} file",
        "msg['From'] = \"{address}\"",
        'git commit -a --author="Name <{address}>"',
        "ssh -h host; echo {address}",
        "psql -Upostgres; echo {address}",
    ],
)
def test_widened_header_forms_leave_controls_unchanged(template):
    address = "sample.person" + chr(64) + "corp.co"
    assert predicate.find_leak(template.format(address=address)) is None


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


async def test_editor_written_text_is_checked():
    body = 'HEADERS = {"User-Agent": "product (sample-person@project.example)"}'
    with pytest.raises(PermissionError):
        await guard_email_in_headers("editor", "write", {"content": body})
    assert (
        await guard_email_in_headers(
            "editor", "write", {"content": 'headers={"User-Agent": "product"}'}
        )
        is None
    )


async def test_unbound_edit_refuses_without_echoing_content():
    with pytest.raises(PermissionError, match="unbound editor") as error:
        await guard_email_in_headers("editor", "edit", {"new_string": ADDRESS})
    assert ADDRESS not in str(error.value)


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
    if action == "edit":
        (tmp_path / "output.txt").write_text("placeholder")
        args["old_string"] = "placeholder"
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


async def test_global_settings_allow_one_address_and_append_note(tmp_path, monkeypatch):
    from lionagi.agent.factory import create_agent
    from lionagi.agent.gate import GateDeniedError

    home = tmp_path / "home"
    (home / ".lionagi").mkdir(parents=True)
    (home / ".lionagi" / "settings.yaml").write_text(
        "guards:\n  email_in_headers:\n    allow:\n      - sample-person@project.example\n"
        "    note: Deployment policy\n"
    )
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    branch = await create_agent(AgentSpec.coding(cwd=str(project)), load_settings=False)
    bash = branch.acts.registry["bash"]
    allowed = {"action": "run", "command": LEAKS[0]}
    assert await bash.preprocessor(allowed) == allowed
    with pytest.raises(GateDeniedError) as error:
        await bash.preprocessor({"action": "run", "command": f'curl -H "X-Contact: {OTHER}"'})
    assert "Deployment policy" in error.value.result.reason
    assert OTHER not in error.value.result.reason


async def test_project_settings_cannot_add_allowed_addresses(tmp_path, monkeypatch):
    from lionagi.agent.factory import create_agent
    from lionagi.agent.gate import GateDeniedError

    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    (project / ".lionagi").mkdir(parents=True)
    (project / ".lionagi" / "settings.yaml").write_text(
        "guards:\n  email_in_headers:\n    allow:\n      - sample-person@project.example\n"
        "    note: Project policy\n"
    )
    monkeypatch.setenv("HOME", str(home))
    branch = await create_agent(AgentSpec.coding(cwd=str(project)), load_settings=False)
    with pytest.raises(GateDeniedError) as error:
        await branch.acts.registry["bash"].preprocessor({"action": "run", "command": LEAKS[0]})
    assert "Project policy" not in error.value.result.reason


async def test_global_allowlist_does_not_hide_later_disallowed_carrier(tmp_path, monkeypatch):
    from lionagi.agent.factory import create_agent
    from lionagi.agent.gate import GateDeniedError

    home = tmp_path / "home"
    (home / ".lionagi").mkdir(parents=True)
    (home / ".lionagi" / "settings.yaml").write_text(
        "guards:\n  email_in_headers:\n    allow:\n      - sample-person@project.example\n"
    )
    monkeypatch.setenv("HOME", str(home))
    branch = await create_agent(AgentSpec.coding(cwd=str(tmp_path)), load_settings=False)
    command = f'UA="{OTHER}"\ncurl -H "X-Contact: {ADDRESS} $UA" https://service.example/'
    with pytest.raises(GateDeniedError) as error:
        await branch.acts.registry["bash"].preprocessor({"action": "run", "command": command})
    assert "ano…@project.example" in error.value.result.reason


async def test_configured_agents_keep_independent_allowlists(tmp_path, monkeypatch):
    from lionagi.agent.factory import create_agent
    from lionagi.agent.gate import GateDeniedError

    home = tmp_path / "home"
    (home / ".lionagi").mkdir(parents=True)
    settings = home / ".lionagi" / "settings.yaml"
    monkeypatch.setenv("HOME", str(home))
    settings.write_text(
        "guards:\n  email_in_headers:\n    allow:\n      - SAMPLE-PERSON@PROJECT.EXAMPLE\n"
    )
    allowed_spec = AgentSpec.coding(cwd=str(tmp_path))
    settings.write_text("guards:\n  email_in_headers:\n    allow: []\n")
    denied_spec = AgentSpec.coding(cwd=str(tmp_path))
    first = await create_agent(allowed_spec, load_settings=False)
    second = await create_agent(denied_spec, load_settings=False)
    args = {"action": "run", "command": LEAKS[0]}
    assert await first.acts.registry["bash"].preprocessor(args) == args
    with pytest.raises(GateDeniedError):
        await second.acts.registry["bash"].preprocessor(args)
    assert await first.acts.registry["bash"].preprocessor(args) == args


async def test_engine_agent_uses_global_guard_settings(tmp_path, monkeypatch):
    from lionagi.agent.gate import GateDeniedError
    from lionhub.orchestration.engines import Engine

    home = tmp_path / "home"
    (home / ".lionagi").mkdir(parents=True)
    (home / ".lionagi" / "settings.yaml").write_text(
        "guards:\n  email_in_headers:\n    allow:\n      - sample-person@project.example\n"
    )
    monkeypatch.setenv("HOME", str(home))
    branch = (
        await Engine()
        .new_run()
        .make_agent("implementer", tools=("coding",), cwd=str(tmp_path), secure=True)
    )
    args = {"action": "run", "command": LEAKS[0]}
    assert await branch.acts.registry["bash"].preprocessor(args) == args
    with pytest.raises(GateDeniedError):
        await branch.acts.registry["bash"].preprocessor(
            {"action": "run", "command": f'curl -H "X-Contact: {OTHER}"'}
        )


def test_cli_api_preset_uses_global_guard_settings(tmp_path, monkeypatch):
    from lionagi.cli.agent import _make_api_agent_spec

    home = tmp_path / "home"
    (home / ".lionagi").mkdir(parents=True)
    (home / ".lionagi" / "settings.yaml").write_text(
        "guards:\n  email_in_headers:\n    allow:\n      - sample-person@project.example\n"
    )
    monkeypatch.setenv("HOME", str(home))
    spec = _make_api_agent_spec(
        cwd=str(tmp_path), effort=None, system_prompt=None, role="implementer", tools="read"
    )
    guard = next(
        h for h in spec.hook_handlers["security_pre:bash"] if h.__name__ == "guard_email_in_headers"
    )
    assert asyncio.run(guard("bash", "run", {"command": LEAKS[0]})) is None


@pytest.mark.parametrize(
    "policy",
    [
        "allow: not-a-list",
        "allow: [7]",
        "allow: [invalid]",
        "note: 7",
    ],
)
def test_invalid_global_guard_settings_fail_closed(tmp_path, monkeypatch, policy):
    home = tmp_path / "home"
    (home / ".lionagi").mkdir(parents=True)
    (home / ".lionagi" / "settings.yaml").write_text(
        f"guards:\n  email_in_headers:\n    {policy}\n"
    )
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(ValueError, match="guards.email_in_headers"):
        AgentSpec.coding(cwd=str(tmp_path))
