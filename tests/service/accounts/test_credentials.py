# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""One routed account's key, from exactly one named reference."""

import asyncio
import logging
import sys
from pathlib import Path

import pytest

from lionagi.config import AppSettings
from lionagi.providers import _secret_resolution
from lionagi.providers._secret_resolution import resolve_secret_lookup_config
from lionagi.service.accounts._credentials import CredentialSources, resolve_account_key
from lionagi.service.accounts._types import AccountRoutingError, RouterErrorCode

KEY = "LIONAGI_TEST_ACCOUNT_KEY"
OTHER = "LIONAGI_TEST_OTHER_ACCOUNT_KEY"
VALUE = "test-key-value-0123456789"


def _lookup(*code: str, names=(KEY,)):
    argv = [sys.executable, "-c", "\n".join(code), "{name}"]
    return resolve_secret_lookup_config(
        settings={"secrets": {"lookup": {"argv": argv, "names": list(names)}}}
    )


def _sources(*dotenv: Path, lookup=None, names=(KEY, OTHER)):
    return CredentialSources(
        dotenv_files=tuple(dotenv),
        dotenv_encoding="utf-8",
        lookup=lookup if lookup is not None else resolve_secret_lookup_config(settings={}),
        account_key_names=frozenset(names),
    )


async def _code(key_env: str, sources: CredentialSources) -> RouterErrorCode:
    with pytest.raises(AccountRoutingError) as caught:
        await resolve_account_key(key_env, sources)
    # The error says what happened and nothing about the value.
    assert str(caught.value) == caught.value.code.value
    # Nothing chained: a decode or parser error can hold the value or quote the file.
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    return caught.value.code


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch):
    monkeypatch.delenv(KEY, raising=False)
    monkeypatch.delenv(OTHER, raising=False)


@pytest.mark.asyncio
async def test_the_process_environment_wins(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"{KEY}=from-the-dotenv-file\n")
    monkeypatch.setenv(KEY, VALUE)
    key = await resolve_account_key(KEY, _sources(env_file))
    assert key.get_secret_value() == VALUE


@pytest.mark.asyncio
async def test_a_present_but_empty_variable_refuses_instead_of_falling_through(
    monkeypatch, tmp_path
):
    env_file = tmp_path / ".env"
    env_file.write_text(f"{KEY}={VALUE}\n")
    monkeypatch.setenv(KEY, "")
    assert await _code(KEY, _sources(env_file)) is RouterErrorCode.CREDENTIAL_INVALID


@pytest.mark.asyncio
async def test_dotenv_files_are_read_in_order_with_the_later_one_winning(tmp_path):
    first, second = tmp_path / "a.env", tmp_path / "b.env"
    first.write_text(f"{KEY}=first-file-value\n")
    second.write_text(f"{KEY}={VALUE}\n")
    key = await resolve_account_key(KEY, _sources(first, second, tmp_path / "absent.env"))
    assert key.get_secret_value() == VALUE


@pytest.mark.asyncio
@pytest.mark.parametrize("line", [f"{KEY}=", f"{KEY}", f"{KEY}=short"])
async def test_a_bad_dotenv_assignment_refuses_instead_of_falling_through(tmp_path, line):
    env_file = tmp_path / ".env"
    env_file.write_text(line + "\n")
    lookup = _lookup(f"print({VALUE!r})")
    assert await _code(KEY, _sources(env_file, lookup=lookup)) is RouterErrorCode.CREDENTIAL_INVALID


@pytest.mark.asyncio
async def test_the_reference_is_never_case_folded(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"{KEY.lower()}={VALUE}\n")
    assert await _code(KEY, _sources(env_file)) is RouterErrorCode.CREDENTIAL_MISSING


@pytest.mark.asyncio
async def test_an_unreadable_dotenv_file_refuses(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"\xff\xfe" + f"{KEY}={VALUE}\n".encode())
    assert await _code(KEY, _sources(env_file)) is RouterErrorCode.CREDENTIAL_LOOKUP_FAILED


@pytest.mark.asyncio
async def test_without_a_lookup_an_absent_key_is_missing_and_a_refused_lookup_says_so():
    assert await _code(KEY, _sources()) is RouterErrorCode.CREDENTIAL_MISSING
    refused = resolve_secret_lookup_config(settings={"secrets": {"lookup": {"argv": "x"}}})
    assert refused.reason is not None
    assert await _code(KEY, _sources(lookup=refused)) is RouterErrorCode.CREDENTIAL_LOOKUP_FAILED


@pytest.mark.asyncio
async def test_a_name_the_lookup_does_not_declare_is_never_looked_up(tmp_path):
    marker = tmp_path / "ran"
    lookup = _lookup(f"open({str(marker)!r}, 'w').close()", f"print({VALUE!r})", names=(OTHER,))
    assert await _code(KEY, _sources(lookup=lookup)) is RouterErrorCode.CREDENTIAL_MISSING
    assert not marker.exists()


@pytest.mark.asyncio
async def test_the_lookup_is_asked_for_the_name_unchanged_and_one_newline_is_removed():
    lookup = _lookup("import sys", "print('v-' + sys.argv[1])", names=(KEY.lower(),))
    key = await resolve_account_key(KEY.lower(), _sources(lookup=lookup, names=(KEY.lower(),)))
    assert key.get_secret_value() == "v-" + KEY.lower()


@pytest.mark.asyncio
async def test_a_value_printed_without_a_newline_is_taken_as_is():
    lookup = _lookup(f"import sys; sys.stdout.write({VALUE!r})")
    assert (await resolve_account_key(KEY, _sources(lookup=lookup))).get_secret_value() == VALUE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        pytest.param(
            f"print({VALUE!r} + chr(10))", RouterErrorCode.CREDENTIAL_INVALID, id="two-newlines"
        ),
        pytest.param(
            f"print(' ' + {VALUE!r})", RouterErrorCode.CREDENTIAL_INVALID, id="leading-space"
        ),
        pytest.param("print('x' * 1025)", RouterErrorCode.CREDENTIAL_INVALID, id="too-long"),
        pytest.param("print('x' * 5000)", RouterErrorCode.CREDENTIAL_INVALID, id="far-too-long"),
        pytest.param("print('seven77')", RouterErrorCode.CREDENTIAL_INVALID, id="too-short"),
        pytest.param(f"print({KEY!r})", RouterErrorCode.CREDENTIAL_INVALID, id="the-name-itself"),
        pytest.param(
            "print('caf\\u00e9-value-123')", RouterErrorCode.CREDENTIAL_INVALID, id="non-ascii"
        ),
        pytest.param("pass", RouterErrorCode.CREDENTIAL_MISSING, id="prints-nothing"),
        pytest.param(
            f"print({VALUE!r}); raise SystemExit(3)",
            RouterErrorCode.CREDENTIAL_LOOKUP_FAILED,
            id="exits-nonzero",
        ),
    ],
)
async def test_a_lookup_result_is_accepted_only_as_one_header_safe_value(code, expected):
    assert await _code(KEY, _sources(lookup=_lookup(code))) is expected


@pytest.mark.asyncio
async def test_the_longest_key_with_its_newline_fits():
    lookup = _lookup("print('x' * 1024)")
    assert len((await resolve_account_key(KEY, _sources(lookup=lookup))).get_secret_value()) == 1024


@pytest.mark.asyncio
async def test_a_missing_program_or_a_slow_lookup_refuses(monkeypatch):
    missing = resolve_secret_lookup_config(
        settings={
            "secrets": {
                "lookup": {"argv": ["/nonexistent/lionagi-lookup", "{name}"], "names": [KEY]}
            }
        }
    )
    assert await _code(KEY, _sources(lookup=missing)) is RouterErrorCode.CREDENTIAL_LOOKUP_FAILED
    monkeypatch.setattr(_secret_resolution, "_LOOKUP_TIMEOUT_SECONDS", 0.5)
    slow = _lookup("import time; time.sleep(30)")
    assert await _code(KEY, _sources(lookup=slow)) is RouterErrorCode.CREDENTIAL_LOOKUP_FAILED


@pytest.mark.asyncio
async def test_the_lookup_child_sees_no_configured_account_key(monkeypatch):
    monkeypatch.setenv(OTHER, "the-other-accounts-key")
    monkeypatch.setenv("LIONAGI_TEST_UNRELATED", "kept")
    lookup = _lookup(
        "import os",
        f"print(('seen' if {OTHER!r} in os.environ else 'hidden') + '-' + os.environ['LIONAGI_TEST_UNRELATED'] + '-ok')",
    )
    key = await resolve_account_key(KEY, _sources(lookup=lookup))
    assert key.get_secret_value() == "hidden-kept-ok"


@pytest.mark.asyncio
async def test_nothing_about_the_value_is_logged(caplog):
    caplog.set_level(logging.DEBUG)
    await resolve_account_key(KEY, _sources(lookup=_lookup(f"print({VALUE!r})")))
    await _code(KEY, _sources(lookup=_lookup(f"print({VALUE!r}); raise SystemExit(2)")))
    assert VALUE not in caplog.text


def test_sources_are_captured_from_the_settings_instance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_ACCOUNTS", raising=False)
    settings = AppSettings(
        _env_file=("a.env",),
        ANTHROPIC_ACCOUNTS=[{"name": "primary", "key_env": KEY, "budget_usd": "1"}],
    )
    sources = CredentialSources.capture(settings, lookup_settings={})
    assert sources.dotenv_files == (tmp_path / "a.env",)
    assert sources.account_key_names == frozenset({KEY})
    assert sources.lookup.lookup is None and sources.lookup.reason is None


@pytest.mark.asyncio
async def test_a_lookup_that_never_stops_printing_is_cut_off_at_the_bound(monkeypatch):
    monkeypatch.setattr(_secret_resolution, "_LOOKUP_TIMEOUT_SECONDS", 5.0)
    lookup = _lookup(
        "import sys", "while True: sys.stdout.write('x' * 4096); sys.stdout.flush()"
    ).lookup
    # Bounded here so a cleanup that never returns fails this test alone.
    result = await asyncio.wait_for(
        _secret_resolution.lookup_one_name(lookup, KEY, env={}, max_bytes=1025), timeout=30
    )
    assert (result.value, result.failure) == (None, "too_long")
