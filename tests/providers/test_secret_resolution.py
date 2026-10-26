# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""``secrets.lookup`` fills a declared variable into a CLI child's environment.

A CLI provider reads its credential from its own environment. When the value
lives in a keychain instead, the spawning process has nothing to pass and the
child dies on a missing variable. These cover the two halves: what counts as a
configured lookup, and what the child actually receives.

The failure mode worth guarding is quiet. A lookup that resolves nothing and a
machine that configured none both end with the child's environment untouched,
so the resolution keeps them apart explicitly rather than reporting one number
for both.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest
import yaml

from lionagi.providers import _secret_resolution
from lionagi.providers._cli_subprocess import ndjson_from_cli
from lionagi.providers._secret_resolution import (
    fill_declared_secrets,
    fill_declared_secrets_and_names,
    resolve_secret_lookup_config,
)

NAME = "LIONAGI_TEST_SECRET"
OTHER = "LIONAGI_TEST_SECRET_TWO"

# Prints a value derived from the name it was asked for, so an arm can tell
# "the lookup ran for the right variable" from "something produced a string".
_PRINTS_A_VALUE = [sys.executable, "-c", "import sys; print('resolved::' + sys.argv[1])", "{name}"]
# Prints a value AND fails. A lookup that only exited nonzero would be refused
# twice over -- by the exit status and by the empty result -- and an arm that
# two mechanisms reject stays green when the one it is named for is removed.
_EXITS_NONZERO = [
    sys.executable,
    "-c",
    "import sys; print('resolved::' + sys.argv[1]); sys.exit(3)",
    "{name}",
]
_PRINTS_NOTHING = [sys.executable, "-c", "pass", "{name}"]
_NO_SUCH_PROGRAM = ["/nonexistent/lionagi-secret-lookup", "{name}"]


def _block(argv, names=(NAME,), **extra):
    return {"secrets": {"lookup": {"argv": list(argv), "names": list(names), **extra}}}


@pytest.fixture
def lionagi_home(tmp_path, monkeypatch):
    """A throwaway ``~/.lionagi`` so the real one is never read or written."""
    home = tmp_path / "home"
    (home / ".lionagi").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _write_global(home, block):
    (home / ".lionagi" / "settings.yaml").write_text(yaml.safe_dump(block))


@pytest.fixture(autouse=True)
def _no_ambient_secret(monkeypatch):
    """The declared names must start absent, or an arm that asserts a fill
    would pass on a value the environment already carried."""
    monkeypatch.delenv(NAME, raising=False)
    monkeypatch.delenv(OTHER, raising=False)


class TestWhatCountsAsAConfiguredLookup:
    def test_a_valid_block_resolves_to_its_argv_and_names(self):
        resolved = resolve_secret_lookup_config(settings=_block(_PRINTS_A_VALUE))
        assert resolved.reason is None
        assert resolved.lookup.argv == tuple(_PRINTS_A_VALUE)
        assert resolved.lookup.names == (NAME,)

    def test_argv_must_be_a_list_not_a_command_string(self):
        """A string would have to be split, and splitting is where a value
        with a space in it stops meaning what it says."""
        resolved = resolve_secret_lookup_config(
            settings={
                "secrets": {"lookup": {"argv": "security find-generic-password", "names": [NAME]}}
            }
        )
        assert resolved.reason == "lookup_argv_not_a_list_of_strings"

    def test_argv_without_the_placeholder_cannot_say_what_it_is_asked_for(self):
        resolved = resolve_secret_lookup_config(settings=_block([sys.executable, "-c", "pass"]))
        assert resolved.reason == "lookup_argv_has_no_name_placeholder"

    def test_the_program_may_not_vary_with_the_variable(self):
        resolved = resolve_secret_lookup_config(settings=_block(["/bin/{name}", "-w", "{name}"]))
        assert resolved.reason == "lookup_argv_program_is_not_fixed"

    def test_one_bad_name_rejects_the_whole_block(self):
        """Dropping just the bad name would leave the block reading as
        configured while quietly resolving less than it says."""
        resolved = resolve_secret_lookup_config(
            settings=_block(_PRINTS_A_VALUE, names=[NAME, "not a var name"])
        )
        assert resolved.reason == "lookup_names_has_an_invalid_environment_variable_name"
        assert resolved.lookup is None

    def test_unknown_keys_are_refused_rather_than_ignored(self):
        resolved = resolve_secret_lookup_config(
            settings=_block(_PRINTS_A_VALUE, argvv=["typo"]),
        )
        assert resolved.reason == "lookup_has_unknown_keys"

    def test_an_unknown_key_is_reported_by_position_not_echoed(self, caplog, capsys):
        # Synthetic value pasted where a key belongs: report where, never what.
        secret = "example-pasted-secret-key"
        caplog.set_level(logging.WARNING, logger=_secret_resolution.__name__)
        settings = _block(_PRINTS_A_VALUE)
        settings["secrets"]["lookup"][secret] = True

        result = resolve_secret_lookup_config(settings=settings)

        assert result.reason == "lookup_has_unknown_keys"
        assert "unknown keys at position 2;" in caplog.text
        assert secret not in caplog.text
        captured = capsys.readouterr()
        assert secret not in captured.out + captured.err

    @pytest.mark.parametrize(
        ("settings", "reason"),
        [
            ({"secrets": {"lookup": ["not", "a", "mapping"]}}, "lookup_not_a_mapping"),
            ({"secrets": {"lookup": {"argv": [], "names": [NAME]}}}, "lookup_argv_is_empty"),
            (
                {"secrets": {"lookup": {"argv": ["p", "{name}"], "names": []}}},
                "lookup_names_is_empty",
            ),
            (
                {"secrets": {"lookup": {"argv": ["p", "{name}"], "names": "NOT_A_LIST"}}},
                "lookup_names_not_a_list_of_strings",
            ),
        ],
    )
    def test_each_malformed_shape_names_why(self, settings, reason):
        assert resolve_secret_lookup_config(settings=settings).reason == reason

    def test_malformed_lookup_value_is_not_echoed(self, caplog, capsys):
        # Synthetic URL-shaped value: the malformed setting must report its type, not content.
        secret = "dbuser:example-source-secret@vault"
        caplog.set_level(logging.WARNING, logger=_secret_resolution.__name__)

        result = resolve_secret_lookup_config(settings={"secrets": {"lookup": secret}})

        assert result.reason == "lookup_not_a_mapping"
        assert "must be a mapping" in caplog.text
        assert "got str" in caplog.text
        assert secret not in caplog.text
        captured = capsys.readouterr()
        assert secret not in captured.out + captured.err

    def test_malformed_argv_is_not_echoed(self, caplog, capsys):
        # Synthetic URL-shaped argument: malformed argv diagnostics must not echo arguments.
        secret = "dbuser:example-argv-secret@vault"
        caplog.set_level(logging.WARNING, logger=_secret_resolution.__name__)

        result = resolve_secret_lookup_config(
            settings={"secrets": {"lookup": {"argv": ["vault-cli", secret, None], "names": [NAME]}}}
        )

        assert result.reason == "lookup_argv_not_a_list_of_strings"
        assert "argv must be a list of strings" in caplog.text
        assert secret not in caplog.text
        assert "got list" in caplog.text
        assert "contains non-string entries" in caplog.text
        captured = capsys.readouterr()
        assert secret not in captured.out + captured.err

    def test_malformed_names_are_not_echoed(self, caplog, capsys):
        # Synthetic value: a string where the list of names belongs reports its type only.
        secret = "dbuser:example-names-secret@vault"
        caplog.set_level(logging.WARNING, logger=_secret_resolution.__name__)

        result = resolve_secret_lookup_config(
            settings={"secrets": {"lookup": {"argv": ["p", "{name}"], "names": secret}}}
        )

        assert result.reason == "lookup_names_not_a_list_of_strings"
        assert "got str (a single string is not a list)" in caplog.text
        assert secret not in caplog.text
        captured = capsys.readouterr()
        assert secret not in captured.out + captured.err

    def test_an_invalid_name_is_reported_by_position_not_echoed(self, caplog, capsys):
        # Synthetic value pasted where a variable name belongs: report where, never what.
        secret = "example-pasted-secret-value"
        caplog.set_level(logging.WARNING, logger=_secret_resolution.__name__)

        result = resolve_secret_lookup_config(
            settings=_block(_PRINTS_A_VALUE, names=[NAME, secret])
        )

        assert result.reason == "lookup_names_has_an_invalid_environment_variable_name"
        assert "invalid entries at index 1;" in caplog.text
        assert secret not in caplog.text
        captured = capsys.readouterr()
        assert secret not in captured.out + captured.err


class TestSilenceIsToldApartFromRefusal:
    """Both end with an untouched environment. Only the reason separates a
    machine that configured nothing from one whose config does not work."""

    @pytest.mark.parametrize(
        "settings",
        [
            {},
            {"secrets": {}},
            {"secrets": {"lookup": None}},
            _block(_PRINTS_A_VALUE, enabled=False),
        ],
        ids=["nothing", "no-lookup-key", "lookup-null", "disabled"],
    )
    def test_chosen_silence_carries_no_reason(self, settings):
        resolved = resolve_secret_lookup_config(settings=settings)
        assert resolved.lookup is None
        assert resolved.reason is None

    def test_a_broken_config_carries_one(self):
        resolved = resolve_secret_lookup_config(settings=_block(["p"], names=[NAME]))
        assert resolved.lookup is None
        assert resolved.reason is not None


class TestACheckedOutRepoCannotNameTheProgram:
    """The lookup command reads this machine's secret store. Project settings
    are the content of whatever tree is checked out, so they are not read."""

    def test_a_project_settings_file_is_not_honoured(self, lionagi_home, tmp_path, monkeypatch):
        project = tmp_path / "some-checkout"
        (project / ".lionagi").mkdir(parents=True)
        (project / ".lionagi" / "settings.yaml").write_text(yaml.safe_dump(_block(_PRINTS_A_VALUE)))
        monkeypatch.chdir(project)

        # The plant is real: the same file IS picked up by a project-merging
        # load. Without this arm, an unreadable or misplaced fixture would make
        # the assertion below pass for the wrong reason.
        from lionagi.agent.settings import load_settings

        assert load_settings()["secrets"]["lookup"]["names"] == [NAME]

        assert resolve_secret_lookup_config().lookup is None

    def test_the_global_file_is_honoured(self, lionagi_home, tmp_path, monkeypatch):
        """The other half: the mechanism does read a file, so the arm above is
        about which file and not about the loader being inert."""
        monkeypatch.chdir(tmp_path)
        _write_global(lionagi_home, _block(_PRINTS_A_VALUE))
        assert resolve_secret_lookup_config().lookup.names == (NAME,)


@pytest.mark.asyncio
class TestFillingTheChildEnvironment:
    async def test_a_missing_declared_name_is_filled(self):
        env = await fill_declared_secrets({"PATH": "/usr/bin"}, settings=_block(_PRINTS_A_VALUE))
        assert env[NAME] == f"resolved::{NAME}"
        assert env["PATH"] == "/usr/bin"

    async def test_a_name_already_set_is_never_looked_up(self):
        """The lookup is live and would return something different, so an
        unchanged value can only mean it was not consulted."""
        env = await fill_declared_secrets(
            {NAME: "already-exported", OTHER: ""},
            settings=_block(_PRINTS_A_VALUE, names=[NAME, OTHER]),
        )
        assert env[NAME] == "already-exported"
        # Same call, same lookup: the empty one WAS filled, which is what
        # proves the lookup was working while the set one went untouched.
        assert env[OTHER] == f"resolved::{OTHER}"

    async def test_an_undeclared_missing_variable_is_not_filled(self):
        env = await fill_declared_secrets({}, settings=_block(_PRINTS_A_VALUE, names=[NAME]))
        assert OTHER not in env

    async def test_nothing_configured_returns_the_env_unchanged(self):
        base = {"PATH": "/usr/bin"}
        assert await fill_declared_secrets(base, settings={}) is base

    async def test_an_inheriting_child_stays_inheriting(self):
        """``None`` means inherit. Handing back a snapshot instead would freeze
        the environment at spawn-decision time for every child."""
        assert await fill_declared_secrets(None, settings={}) is None

    async def test_inheriting_plus_a_fill_yields_a_whole_environment(self, monkeypatch):
        monkeypatch.setenv("LIONAGI_TEST_MARKER", "inherited")
        env = await fill_declared_secrets(None, settings=_block(_PRINTS_A_VALUE))
        assert env[NAME] == f"resolved::{NAME}"
        # Replacing rather than extending would strip everything the child
        # needs, PATH included.
        assert env["LIONAGI_TEST_MARKER"] == "inherited"
        assert "PATH" in env

    @pytest.mark.parametrize(
        "argv",
        [_EXITS_NONZERO, _PRINTS_NOTHING, _NO_SUCH_PROGRAM],
        ids=["exits-3", "empty", "no-program"],
    )
    async def test_a_lookup_that_does_not_work_leaves_the_env_alone(self, argv):
        base = {"PATH": "/usr/bin"}
        assert await fill_declared_secrets(base, settings=_block(argv)) is base


@pytest.mark.asyncio
class TestARefusedLookupIsDistinguishableFromAnAbsentOne:
    """Both return ``env`` untouched, so the return value cannot tell them
    apart and the child dies identically either way -- on a missing variable,
    naming the variable and never the lookup. The log is where they separate.

    These arms are paired on purpose: asserting only that the refused case
    warns would pass just as well with the warning emitted unconditionally,
    which is the outcome that makes the signal worthless.
    """

    async def test_a_refused_lookup_says_the_child_is_spawning_without_them(self, caplog):
        caplog.set_level(logging.WARNING)
        base = {"PATH": "/usr/bin"}
        # `argv` is not a list, so the config is refused rather than absent.
        assert (
            await fill_declared_secrets(
                base, settings={"secrets": {"lookup": {"argv": "not-a-list", "names": [NAME]}}}
            )
            is base
        )
        assert "spawning without declared secrets" in caplog.text
        assert "refused" in caplog.text

    async def test_nothing_configured_stays_silent(self, caplog):
        """The discriminating half. Chosen silence is not a problem to report,
        and a machine that configured no lookup must not be warned at every
        spawn -- an alarm that fires for everyone trains its reader to skip it.
        """
        caplog.set_level(logging.WARNING)
        base = {"PATH": "/usr/bin"}
        assert await fill_declared_secrets(base, settings={}) is base
        assert "spawning without declared secrets" not in caplog.text

    async def test_the_reason_reaches_the_operator_not_just_the_type(self, caplog):
        """The reason identifier is what says WHICH refusal happened. Without
        it the message narrows the problem to `your lookup config` and leaves
        the operator to diff it against the schema by hand."""
        caplog.set_level(logging.WARNING)
        await fill_declared_secrets(
            {}, settings={"secrets": {"lookup": {"argv": [], "names": [NAME]}}}
        )
        assert "lookup_argv" in caplog.text

    async def test_a_working_lookup_does_not_claim_the_child_went_without(self, caplog):
        """A lookup that resolves must not emit the refusal line. Sharing one
        branch with the success path would make the warning fire on the runs it
        has nothing to say about."""
        caplog.set_level(logging.WARNING)
        env = await fill_declared_secrets({}, settings=_block(_PRINTS_A_VALUE))
        assert env[NAME] == f"resolved::{NAME}"
        assert "spawning without declared secrets" not in caplog.text


@pytest.mark.asyncio
class TestTheValueDoesNotReachTheLogs:
    async def test_a_successful_lookup_logs_no_value(self, caplog):
        caplog.set_level(logging.DEBUG)
        env = await fill_declared_secrets({}, settings=_block(_PRINTS_A_VALUE))
        assert env[NAME] == f"resolved::{NAME}"  # the value exists to be leaked
        assert "resolved::" not in caplog.text

    async def test_a_failing_lookup_does_not_log_what_it_printed(self, caplog):
        """The leak-prone path: a store that prints the secret and then exits
        nonzero. stdout is the same channel the value arrives on."""
        caplog.set_level(logging.DEBUG)
        leaky = [
            sys.executable,
            "-c",
            "import sys; print('resolved::' + sys.argv[1]); sys.exit(4)",
            "{name}",
        ]
        base = {}
        assert await fill_declared_secrets(base, settings=_block(leaky)) is base
        assert "exited 4" in caplog.text  # the failure IS reported
        assert "resolved::" not in caplog.text


@pytest.mark.asyncio
class TestTheSpawnSeamActuallyUsesIt:
    """Every CLI provider spawns through ``ndjson_from_cli``. Resolution that
    never reaches the child is the whole defect this exists to prevent, and
    only reading it back out of a real child proves it did."""

    async def test_the_child_process_receives_the_resolved_value(
        self, lionagi_home, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_global(lionagi_home, _block(_PRINTS_A_VALUE))
        cmd = [
            sys.executable,
            "-c",
            f"import os, json; print(json.dumps({{'seen': os.environ.get({NAME!r})}}))",
        ]
        seen = [obj async for obj in ndjson_from_cli(cmd, env={**os.environ})]
        assert seen == [{"seen": f"resolved::{NAME}"}]

    async def test_a_child_spawned_with_no_env_still_receives_it(
        self, lionagi_home, tmp_path, monkeypatch
    ):
        """gemini passes no ``env`` at all, so the inherit path is a real one
        and not a theoretical branch."""
        monkeypatch.chdir(tmp_path)
        _write_global(lionagi_home, _block(_PRINTS_A_VALUE))
        cmd = [
            sys.executable,
            "-c",
            f"import os, json; print(json.dumps({{'seen': os.environ.get({NAME!r})}}))",
        ]
        seen = [obj async for obj in ndjson_from_cli(cmd)]
        assert seen == [{"seen": f"resolved::{NAME}"}]

    async def test_with_nothing_configured_the_child_environment_is_untouched(
        self, lionagi_home, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LIONAGI_TEST_MARKER", "inherited")
        cmd = [
            sys.executable,
            "-c",
            "import os, json; print(json.dumps({'marker': os.environ.get('LIONAGI_TEST_MARKER'),"
            f" 'seen': os.environ.get({NAME!r})}}))",
        ]
        seen = [obj async for obj in ndjson_from_cli(cmd)]
        assert seen == [{"marker": "inherited", "seen": None}]


@pytest.fixture
def routed_account(monkeypatch):
    """``ANTHROPIC_ACCOUNTS`` configured with one account whose key reference is ``NAME``."""
    import lionagi.config as config

    account = config.ProviderAccount.from_declaration(
        {"name": "a", "key_env": NAME, "budget_usd": "1"}
    )
    monkeypatch.setattr(
        config, "settings", config.settings.model_copy(update={"ANTHROPIC_ACCOUNTS": (account,)})
    )


@pytest.mark.asyncio
class TestARoutedAccountKeyIsNotFilledIntoACliChild:
    """A routed account's key is read by the account router for the one request
    it signs. Filling it into every CLI child would hand each child every
    account's key, so a declared name that is an account's ``key_env`` is not
    looked up for a child. Each arm declares a second name in the same call and
    asserts it IS filled, so the lookup is shown working while the account name
    goes unfilled."""

    async def test_a_configured_account_reference_is_not_filled(self, routed_account):
        env = await fill_declared_secrets({}, settings=_block(_PRINTS_A_VALUE, names=[NAME, OTHER]))
        assert NAME not in env
        assert env[OTHER] == f"resolved::{OTHER}"

    async def test_an_explicit_set_overrides_the_configured_one(self, routed_account):
        env = await fill_declared_secrets(
            {},
            settings=_block(_PRINTS_A_VALUE, names=[NAME, OTHER]),
            account_key_names=frozenset({OTHER}),
        )
        assert env[NAME] == f"resolved::{NAME}"
        assert OTHER not in env

    async def test_the_skipped_name_is_still_named_for_redaction(self, routed_account):
        env, names = await fill_declared_secrets_and_names(
            {}, settings=_block(_PRINTS_A_VALUE, names=[NAME, OTHER])
        )
        assert names == (NAME, OTHER)
        assert NAME not in env
        assert env[OTHER] == f"resolved::{OTHER}"

    async def test_a_value_the_child_already_has_is_left_alone(self, routed_account):
        """Only the fill is skipped: an environment that already carries the
        variable keeps it, as before."""
        env = await fill_declared_secrets(
            {NAME: "exported", OTHER: ""}, settings=_block(_PRINTS_A_VALUE, names=[NAME, OTHER])
        )
        assert env[NAME] == "exported"
        assert env[OTHER] == f"resolved::{OTHER}"

    async def test_the_child_process_does_not_receive_it(self, lionagi_home, tmp_path, monkeypatch):
        """A real CLI child, with an account's reference and OPENROUTER_API_KEY both
        declared: the account's name is absent from the child, the other is filled.
        The child reports presence and equality, not values, so redaction of its
        output cannot decide the outcome."""
        import lionagi.config as config

        account_name = "ANTHROPIC_API_KEY_TEAM"
        control_name = "OPENROUTER_API_KEY"
        monkeypatch.delenv(account_name, raising=False)
        monkeypatch.delenv(control_name, raising=False)
        account = config.ProviderAccount.from_declaration(
            {"name": "a", "key_env": account_name, "budget_usd": "1"}
        )
        monkeypatch.setattr(
            config,
            "settings",
            config.settings.model_copy(update={"ANTHROPIC_ACCOUNTS": (account,)}),
        )
        monkeypatch.chdir(tmp_path)
        _write_global(lionagi_home, _block(_PRINTS_A_VALUE, names=[account_name, control_name]))
        cmd = [
            sys.executable,
            "-c",
            "import os, json; print(json.dumps({"
            f"'account': {account_name!r} in os.environ, "
            f"'control': os.environ.get({control_name!r}) == 'resolved::' + {control_name!r}"
            "}))",
        ]
        seen = [obj async for obj in ndjson_from_cli(cmd)]
        assert seen == [{"account": False, "control": True}]


class TestOneConfigReadPerSpawn:
    """Filling and redacting must agree about which names are secrets.

    The config is re-read from disk on every resolve, and filling awaits a
    lookup, so two resolves around that await can disagree: the child gets a
    value the redactor was never told to remove.
    """

    async def test_filling_and_naming_come_from_a_single_resolve(self, lionagi_home, monkeypatch):
        _write_global(lionagi_home, _block(_PRINTS_A_VALUE))
        calls = []
        real = _secret_resolution.resolve_secret_lookup_config

        def counting(**kwargs):
            calls.append(kwargs)
            return real(**kwargs)

        monkeypatch.setattr(_secret_resolution, "resolve_secret_lookup_config", counting)

        env, names = await fill_declared_secrets_and_names({})

        assert len(calls) == 1, f"config resolved {len(calls)} times, so the two reads can disagree"
        assert names == (NAME,), names
        assert env is not None and env[NAME] == f"resolved::{NAME}"
        # The names describe the fill that actually happened, which is the
        # property a second resolve cannot guarantee.
        assert set(names) <= set(env)


@pytest.mark.asyncio
async def test_lookup_timeout_passes_spawn_bound_witnesses(monkeypatch):
    """Secret cleanup cannot acquire group authority from a recycled pid."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    lookup = _secret_resolution.ResolvedSecretLookup(
        argv=("lookup", "{name}"),
        names=(NAME,),
    )
    proc = MagicMock()
    proc.pid = 5151
    proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    proc.wait = AsyncMock(return_value=-9)
    witnesses = ((5151, 16.0),)
    cleanup = AsyncMock()

    monkeypatch.setattr(
        _secret_resolution.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=proc),
    )
    monkeypatch.setattr(
        _secret_resolution,
        "prepare_process_group_env",
        lambda env=None: ({"MARKED": "secret"}, "secret-token"),
    )
    monkeypatch.setattr(
        _secret_resolution,
        "observe_process_group_leader",
        lambda pid, *, expected_marker: (
            16.0 if (pid, expected_marker) == (5151, "secret-token") else None
        ),
    )
    monkeypatch.setattr(
        _secret_resolution,
        "capture_group_witnesses",
        lambda *args, **kwargs: witnesses,
    )
    monkeypatch.setattr(_secret_resolution, "aterminate_process_group", cleanup)

    assert await _secret_resolution._run_lookup(lookup, NAME) is None
    cleanup.assert_awaited_once_with(
        proc,
        grace=None,
        group_witnesses=witnesses,
    )
    assert _secret_resolution.asyncio.create_subprocess_exec.await_args.kwargs["env"] == {
        "MARKED": "secret"
    }
