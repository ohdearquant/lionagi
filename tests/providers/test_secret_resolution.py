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

from lionagi.providers._cli_subprocess import ndjson_from_cli
from lionagi.providers._secret_resolution import (
    fill_declared_secrets,
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
