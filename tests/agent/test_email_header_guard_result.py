# Copyright (c) 2023-2026, LionAGI contributors
# SPDX-License-Identifier: Apache-2.0

"""The editor guard checks the file an edit would write."""

from __future__ import annotations

import asyncio
import os
from functools import wraps
from inspect import getclosurevars

import pytest

from lionagi.agent.factory import create_agent
from lionagi.agent.gate import GateDeniedError
from lionagi.agent.spec import AgentSpec, _wire_secure_guards
from lionagi.protocols.generic.event import EventStatus

ADDRESS = "sample-person@project.example"
UNSAFE = f'headers = {{"User-Agent": "client {ADDRESS}"}}\n'


async def _editor(variant, tmp_path, monkeypatch):
    # The standalone editor resolves its workspace at construction time.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    if variant == "toolkit":
        spec = AgentSpec.coding(cwd=str(tmp_path))
        name = "editor"
    else:
        spec = AgentSpec.compose("implementer", tools=["editor"], cwd=str(tmp_path))
        _wire_secure_guards(spec, str(tmp_path))
        name = "editor_tool"
    branch = await create_agent(spec, load_settings=False)
    return branch, name


async def _call(branch, name, arguments):
    return await branch.acts.invoke({"function": name, "arguments": arguments})


async def _prepare_read(branch, variant, target):
    if variant == "toolkit":
        reader = branch.acts.registry["reader"]
        response = await reader.func_callable(action="read", path=str(target))
        assert response["success"] is True


def _edit(target, old, new, *, replace_all=False):
    return {
        "action": "edit",
        "file_path": str(target),
        "old_string": old,
        "new_string": new,
        "replace_all": replace_all,
    }


def _assert_denied(call, *, edit=True):
    assert call.status is EventStatus.FAILED
    assert isinstance(call.execution.error, GateDeniedError)
    assert call.execution.error.result.control == "guard_email_in_headers"
    assert ADDRESS not in str(call.execution.error)
    if edit:
        assert "resulting file" in call.execution.error.result.reason


@pytest.mark.parametrize("variant", ["toolkit", "standalone"])
async def test_edit_reconstructs_header_from_old_file_and_inserted_address(
    variant, tmp_path, monkeypatch
):
    branch, name = await _editor(variant, tmp_path, monkeypatch)
    target = tmp_path / "client.py"
    original = 'headers = {"User-Agent": "client PLACEHOLDER"}\n'
    target.write_text(original)
    await _prepare_read(branch, variant, target)

    call = await _call(branch, name, _edit(target, "PLACEHOLDER", ADDRESS))

    _assert_denied(call)
    assert target.read_text() == original


@pytest.mark.parametrize("variant", ["toolkit", "standalone"])
async def test_edit_refuses_header_using_existing_address_carrier(variant, tmp_path, monkeypatch):
    from lionagi.agent._email_in_headers import find_leak

    branch, name = await _editor(variant, tmp_path, monkeypatch)
    target = tmp_path / "client.sh"
    original = f'UA="client ({ADDRESS})"\nPLACEHOLDER\n'
    header_use = 'curl -H "X-Contact: $UA" https://service.example/'
    target.write_text(original)
    await _prepare_read(branch, variant, target)
    assert find_leak(original) is None
    assert find_leak(header_use) is None

    call = await _call(branch, name, _edit(target, "PLACEHOLDER", header_use))

    _assert_denied(call)
    assert target.read_text() == original


@pytest.mark.parametrize("variant", ["toolkit", "standalone"])
async def test_edit_refuses_an_unrelated_change_that_preserves_a_leak(
    variant, tmp_path, monkeypatch
):
    branch, name = await _editor(variant, tmp_path, monkeypatch)
    target = tmp_path / "client.py"
    original = UNSAFE + "version = 'one'\n"
    target.write_text(original)
    await _prepare_read(branch, variant, target)

    call = await _call(branch, name, _edit(target, "'one'", "'two'"))

    _assert_denied(call)
    assert target.read_text() == original


async def test_toolkit_unread_safe_edit_keeps_native_guard_and_tracking(tmp_path, monkeypatch):
    branch, name = await _editor("toolkit", tmp_path, monkeypatch)
    target = tmp_path / "client.py"
    original = "version = 'one'\n"
    target.write_text(original)
    editor = branch.acts.registry[name]
    check_read = getclosurevars(editor.func_callable).nonlocals["_check_read_guard"]
    tracking = getclosurevars(check_read).nonlocals
    invalidate = getclosurevars(tracking["_invalidate_stale_reads"]).nonlocals
    state = {key: invalidate[key] for key in ("file_state", "read_tracked", "dormant_reads")}
    before = {key: value.copy() for key, value in state.items()}
    args = _edit(target, "'one'", "'two'")
    assert await editor.preprocessor(args) == args
    assert state == before
    call = await _call(branch, name, args)

    assert call.status is EventStatus.COMPLETED
    assert call.response["success"] is False
    assert call.response["error"] == f"Must read file before editing: {target}"
    assert target.read_text() == original


async def test_toolkit_unread_leaking_edit_gets_guard_refusal(tmp_path, monkeypatch):
    branch, name = await _editor("toolkit", tmp_path, monkeypatch)
    target = tmp_path / "client.py"
    original = UNSAFE + "version = 'one'\n"
    target.write_text(original)
    call = await _call(branch, name, _edit(target, "'one'", "'two'"))
    _assert_denied(call)
    assert target.read_text() == original


async def test_toolkit_read_then_safe_edit_succeeds(tmp_path, monkeypatch):
    branch, name = await _editor("toolkit", tmp_path, monkeypatch)
    target = tmp_path / "client.py"
    target.write_text("version = 'one'\n")
    await _prepare_read(branch, "toolkit", target)
    call = await _call(branch, name, _edit(target, "'one'", "'two'"))
    assert call.status is EventStatus.COMPLETED
    assert call.response["success"] is True
    assert target.read_text() == "version = 'two'\n"


@pytest.mark.parametrize("variant", ["toolkit", "standalone"])
async def test_edit_allows_removing_the_only_header_address(variant, tmp_path, monkeypatch):
    branch, name = await _editor(variant, tmp_path, monkeypatch)
    target = tmp_path / "client.py"
    target.write_text(UNSAFE)
    await _prepare_read(branch, variant, target)

    call = await _call(branch, name, _edit(target, ADDRESS, "redacted"))

    assert call.status is EventStatus.COMPLETED
    assert call.response["success"] is True
    assert target.read_text() == UNSAFE.replace(ADDRESS, "redacted")


@pytest.mark.parametrize("variant", ["toolkit", "standalone"])
async def test_replace_all_checks_the_complete_result(variant, tmp_path, monkeypatch):
    branch, name = await _editor(variant, tmp_path, monkeypatch)
    target = tmp_path / "client.py"
    original = 'headers = {"User-Agent": "client PLACEHOLDER"}\n' * 2
    target.write_text(original)
    await _prepare_read(branch, variant, target)

    call = await _call(branch, name, _edit(target, "PLACEHOLDER", ADDRESS, replace_all=True))

    _assert_denied(call)
    assert target.read_text() == original


@pytest.mark.parametrize("variant", ["toolkit", "standalone"])
async def test_replace_all_allows_removing_all_header_addresses(variant, tmp_path, monkeypatch):
    branch, name = await _editor(variant, tmp_path, monkeypatch)
    target = tmp_path / "client.py"
    original = UNSAFE * 2
    target.write_text(original)
    await _prepare_read(branch, variant, target)

    call = await _call(branch, name, _edit(target, ADDRESS, "redacted", replace_all=True))

    assert call.status is EventStatus.COMPLETED
    assert call.response["success"] is True
    assert target.read_text() == original.replace(ADDRESS, "redacted")


@pytest.mark.parametrize("variant", ["toolkit", "standalone"])
async def test_not_found_edit_delegates_exact_native_error(variant, tmp_path, monkeypatch):
    branch, name = await _editor(variant, tmp_path, monkeypatch)
    target = tmp_path / "client.py"
    original = "safe existing content\n"
    target.write_text(original)
    await _prepare_read(branch, variant, target)

    call = await _call(branch, name, _edit(target, "missing-value", UNSAFE))

    assert call.status is EventStatus.COMPLETED
    assert call.response["success"] is False
    assert call.response["error"] == (
        f"old_string not found in {target}. "
        "Re-read the file and copy the exact text including all whitespace."
    )
    assert target.read_text() == original


@pytest.mark.parametrize("variant", ["toolkit", "standalone"])
async def test_ambiguous_edit_delegates_exact_native_error(variant, tmp_path, monkeypatch):
    branch, name = await _editor(variant, tmp_path, monkeypatch)
    target = tmp_path / "client.py"
    original = "match match\n"
    target.write_text(original)
    await _prepare_read(branch, variant, target)

    call = await _call(branch, name, _edit(target, "match", UNSAFE))

    if variant == "toolkit":
        expected = (
            "old_string appears 2 times. Either set replace_all=True or expand old_string "
            "with more surrounding context."
        )
    else:
        expected = (
            f"old_string appears 2 times in {target}. Either set replace_all=True to replace "
            "all occurrences, or expand old_string with more surrounding context to make it unique."
        )
    assert call.status is EventStatus.COMPLETED
    assert call.response["success"] is False
    assert call.response["error"] == expected
    assert target.read_text() == original


@pytest.mark.parametrize("variant", ["toolkit", "standalone"])
async def test_missing_edit_argument_delegates_native_error(variant, tmp_path, monkeypatch):
    branch, name = await _editor(variant, tmp_path, monkeypatch)
    target = tmp_path / "client.py"
    original = "safe existing content\n"
    target.write_text(original)
    await _prepare_read(branch, variant, target)

    call = await _call(
        branch, name, {"action": "edit", "file_path": str(target), "new_string": UNSAFE}
    )

    assert call.status is EventStatus.COMPLETED
    assert call.response["success"] is False
    expected = (
        "'old_string' required for edit"
        if variant == "toolkit"
        else "'old_string' is required for action='edit'. Read the file first, then copy the exact text to replace."
    )
    assert call.response["error"] == expected
    assert target.read_text() == original


@pytest.mark.parametrize("variant", ["toolkit", "standalone"])
async def test_write_refuses_complete_header(variant, tmp_path, monkeypatch):
    branch, name = await _editor(variant, tmp_path, monkeypatch)
    target = tmp_path / "client.py"

    call = await _call(
        branch, name, {"action": "write", "file_path": str(target), "content": UNSAFE}
    )

    _assert_denied(call, edit=False)
    assert not target.exists()


async def test_standalone_guard_reads_the_editor_instance_root(tmp_path, monkeypatch):
    root = tmp_path / "nested"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "client.py").write_text("PLACEHOLDER")
    target = root / "client.py"
    original = 'headers = {"User-Agent": "PLACEHOLDER"}'
    target.write_text(original)
    spec = AgentSpec.compose("implementer", tools=["editor"], cwd=str(tmp_path))
    _wire_secure_guards(spec, str(tmp_path))
    branch = await create_agent(spec, load_settings=False)

    call = await _call(branch, "editor_tool", _edit("client.py", "PLACEHOLDER", ADDRESS))

    _assert_denied(call)
    assert target.read_text() == original


@pytest.mark.parametrize("variant", ["toolkit", "standalone"])
async def test_rewritten_edit_is_rechecked_against_result(variant, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "client.py"
    original = 'headers = {"User-Agent": "PLACEHOLDER"}'
    target.write_text(original)
    if variant == "toolkit":
        spec = AgentSpec.coding(cwd=str(tmp_path))
        name = "editor"
    else:
        spec = AgentSpec.compose("implementer", tools=["editor"], cwd=str(tmp_path))
        _wire_secure_guards(spec, str(tmp_path))
        name = "editor_tool"

    async def rewrite(_tool_name, _action, args):
        return {**args, "new_string": ADDRESS}

    spec.pre("editor", rewrite)
    branch = await create_agent(spec, load_settings=False)
    await _prepare_read(branch, variant, target)
    call = await _call(branch, name, _edit(target, "PLACEHOLDER", "safe"))
    _assert_denied(call)
    assert target.read_text() == original


@pytest.mark.parametrize("variant", ["toolkit", "standalone"])
@pytest.mark.parametrize("wildcard", [False, True])
async def test_wrapped_guard_keeps_wrapper_and_refuses_unbound_edit(
    variant, wildcard, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = tmp_path / ".lionagi" / "settings.yaml"
    settings.parent.mkdir()
    settings.write_text("guards:\n  email_in_headers:\n    note: Deployment policy\n")
    if variant == "toolkit":
        spec = AgentSpec.coding(cwd=str(tmp_path))
        name = "editor"
    else:
        spec = AgentSpec.compose("implementer", tools=["editor"], cwd=str(tmp_path))
        _wire_secure_guards(spec, str(tmp_path))
        name = "editor_tool"
    hooks = spec.hook_handlers["security_pre:editor"]
    original_guard = hooks.pop(0)

    @wraps(original_guard)
    async def wrapped(tool_name, action, args):
        if args.get("new_string") == "wrapper-denied":
            raise PermissionError("wrapper rule")
        return await original_guard(tool_name, action, args)

    spec.security_pre("*" if wildcard else "editor", wrapped)
    branch = await create_agent(spec, load_settings=False)
    target = tmp_path / "client.py"
    original = 'headers = {"User-Agent": "PLACEHOLDER"}'
    target.write_text(original)
    await _prepare_read(branch, variant, target)

    rule = await _call(branch, name, _edit(target, "PLACEHOLDER", "wrapper-denied"))
    assert rule.status is EventStatus.FAILED
    assert "wrapper rule" in rule.execution.error.result.reason
    leak = await _call(branch, name, _edit(target, "PLACEHOLDER", ADDRESS))
    _assert_denied(leak)
    assert "unbound editor" in leak.execution.error.result.reason
    assert target.read_text() == original


@pytest.mark.parametrize("variant", ["toolkit", "standalone"])
async def test_wildcard_registered_guard_binds_to_editor(variant, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = tmp_path / ".lionagi" / "settings.yaml"
    settings.parent.mkdir()
    settings.write_text("guards:\n  email_in_headers:\n    note: Deployment policy\n")
    if variant == "toolkit":
        spec = AgentSpec.coding(cwd=str(tmp_path))
        name = "editor"
    else:
        spec = AgentSpec.compose("implementer", tools=["editor"], cwd=str(tmp_path))
        _wire_secure_guards(spec, str(tmp_path))
        name = "editor_tool"
    guard = spec.hook_handlers["security_pre:editor"].pop(0)
    spec.security_pre("*", guard)
    branch = await create_agent(spec, load_settings=False)
    target = tmp_path / "client.py"
    original = 'headers = {"User-Agent": "PLACEHOLDER"}'
    target.write_text(original)
    await _prepare_read(branch, variant, target)
    call = await _call(branch, name, _edit(target, "PLACEHOLDER", ADDRESS))
    _assert_denied(call)
    assert "Deployment policy" in call.execution.error.result.reason
    assert "unbound editor" not in call.execution.error.result.reason
    assert target.read_text() == original


async def test_two_toolkit_roots_stay_independent_during_interleaved_edits(tmp_path, monkeypatch):
    from lionagi.ln import concurrency

    monkeypatch.setenv("HOME", str(tmp_path))
    first_root, second_root = tmp_path / "first", tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    (first_root / "client.py").write_text('headers = {"User-Agent": "PLACEHOLDER"}')
    (second_root / "client.py").write_text("PLACEHOLDER")
    first = await create_agent(AgentSpec.coding(cwd=str(first_root)), load_settings=False)
    second = await create_agent(AgentSpec.coding(cwd=str(second_root)), load_settings=False)
    real_run_sync = concurrency.run_sync
    both_entered = asyncio.Event()
    seen = []

    async def interleave(*args, **kwargs):
        seen.append(args[-1])
        if len(seen) == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=5)
        return await real_run_sync(*args, **kwargs)

    monkeypatch.setattr(concurrency, "run_sync", interleave)
    args = _edit("client.py", "PLACEHOLDER", ADDRESS)
    results = await asyncio.gather(
        first.acts.registry["editor"].preprocessor(args),
        second.acts.registry["editor"].preprocessor(args),
        return_exceptions=True,
    )
    assert isinstance(results[0], GateDeniedError)
    assert results[1] == args
    assert set(seen) == {first_root, second_root}


async def test_two_configured_editor_guards_keep_independent_policies(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = tmp_path / ".lionagi" / "settings.yaml"
    settings.parent.mkdir()
    settings.write_text(
        "guards:\n  email_in_headers:\n    allow: [sample-person@project.example]\n"
        "    note: First policy\n"
    )
    first_spec = AgentSpec.coding(cwd=str(tmp_path))
    settings.write_text(
        "guards:\n  email_in_headers:\n    allow: [another-person@project.example]\n"
        "    note: Second policy\n"
    )
    second_spec = AgentSpec.coding(cwd=str(tmp_path))
    first = await create_agent(first_spec, load_settings=False)
    second = await create_agent(second_spec, load_settings=False)
    target = tmp_path / "client.py"
    target.write_text('headers = {"User-Agent": "PLACEHOLDER"}')
    first_args = _edit(target, "PLACEHOLDER", ADDRESS)
    second_args = _edit(target, "PLACEHOLDER", "another-person@project.example")
    first_pre = first.acts.registry["editor"].preprocessor
    second_pre = second.acts.registry["editor"].preprocessor
    assert await first_pre(first_args) == first_args
    assert await second_pre(second_args) == second_args
    with pytest.raises(GateDeniedError) as first_denial:
        await first_pre(second_args)
    with pytest.raises(GateDeniedError) as second_denial:
        await second_pre(first_args)
    assert "First policy" in first_denial.value.result.reason
    assert "Second policy" in second_denial.value.result.reason


async def test_nested_editor_uses_its_own_binding_and_direct_guard_refuses(tmp_path, monkeypatch):
    from lionagi.agent.hooks import guard_email_in_headers

    monkeypatch.setenv("HOME", str(tmp_path))
    outer_root, inner_root = tmp_path / "outer", tmp_path / "inner"
    outer_root.mkdir()
    inner_root.mkdir()
    (outer_root / "client.py").write_text("PLACEHOLDER")
    (inner_root / "client.py").write_text('headers = {"User-Agent": "PLACEHOLDER"}')
    inner = await create_agent(AgentSpec.coding(cwd=str(inner_root)), load_settings=False)
    spec = AgentSpec.coding(cwd=str(outer_root))
    inner_args = _edit("client.py", "PLACEHOLDER", ADDRESS)
    checks = []

    async def nested(_name, _action, args):
        with pytest.raises(GateDeniedError) as denial:
            await inner.acts.registry["editor"].preprocessor(inner_args)
        assert "unbound editor" not in denial.value.result.reason
        with pytest.raises(PermissionError, match="unbound editor"):
            await guard_email_in_headers("editor", "edit", inner_args)
        checks.append(True)
        return args

    spec.pre("editor", nested)
    outer = await create_agent(spec, load_settings=False)
    outer_args = _edit("client.py", "PLACEHOLDER", "safe")
    assert await outer.acts.registry["editor"].preprocessor(outer_args) == outer_args
    assert checks == [True]


def test_binding_leaves_nonfunction_callables_unchecked_and_unchanged(tmp_path):
    from lionagi.agent.hooks import _bind_email_header_editor_guard, guard_email_in_headers

    class Spoof:
        def __hash__(self):
            return hash(guard_email_in_headers)

        def __eq__(self, _other):
            raise AssertionError("binding must not invoke arbitrary callable equality")

        async def __call__(self, _name, _action, _args):
            return None

    class NoWeakReference:
        __slots__ = ()

        async def __call__(self, _name, _action, _args):
            return None

    for handler in (Spoof(), NoWeakReference()):
        assert _bind_email_header_editor_guard(handler, object(), tmp_path) is handler


@pytest.mark.parametrize("variant", ["toolkit", "standalone"])
async def test_edit_guard_keeps_configured_allowlist_and_note(variant, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = tmp_path / ".lionagi" / "settings.yaml"
    settings.parent.mkdir()
    settings.write_text(
        "guards:\n  email_in_headers:\n    allow: [sample-person@project.example]\n"
        "    note: Deployment policy\n"
    )
    if variant == "toolkit":
        spec = AgentSpec.coding(cwd=str(tmp_path))
        name = "editor"
    else:
        spec = AgentSpec.compose("implementer", tools=["editor"], cwd=str(tmp_path))
        _wire_secure_guards(spec, str(tmp_path))
        name = "editor_tool"
    branch = await create_agent(spec, load_settings=False)
    target = tmp_path / "client.py"
    target.write_text('headers = {"User-Agent": "PLACEHOLDER"}')
    await _prepare_read(branch, variant, target)
    allowed = await _call(branch, name, _edit(target, "PLACEHOLDER", ADDRESS))
    assert allowed.status is EventStatus.COMPLETED
    assert allowed.response["success"] is True
    await _prepare_read(branch, variant, target)
    denied = await _call(branch, name, _edit(target, ADDRESS, "another-person@project.example"))
    assert denied.status is EventStatus.FAILED
    assert "Deployment policy" in denied.execution.error.result.reason


@pytest.mark.parametrize("variant", ["toolkit", "standalone"])
async def test_nontext_edit_fails_in_the_editor(variant, tmp_path, monkeypatch):
    branch, name = await _editor(variant, tmp_path, monkeypatch)
    target = tmp_path / "client.py"
    original = b"\xff\xfe"
    if variant == "toolkit":
        target.write_text("safe")
        await _prepare_read(branch, variant, target)
        stat = target.stat()
    target.write_bytes(original)
    if variant == "toolkit":
        # Keep the read guard satisfied so the edit itself reads the nontext bytes.
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    args = _edit(target, "safe", UNSAFE)
    assert await branch.acts.registry[name].preprocessor(args) == args
    call = await _call(branch, name, args)
    assert call.status is EventStatus.FAILED
    assert isinstance(call.execution.error, UnicodeDecodeError)
    assert target.read_bytes() == original
