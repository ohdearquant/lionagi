# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The projector reads argparse internals, which are not a supported API.

The golden schemas below are the containment for that: a change to how the CLI
declares a flag, or to how argparse represents one, fails a test here with a
readable diff instead of quietly producing a schema that no longer matches the
command it claims to describe. Regenerate them deliberately with
``LIONAGI_UPDATE_GOLDEN=1``, and read the diff before committing it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from importlib import import_module
from pathlib import Path

import pytest

from lionagi.mcp.projection import (
    PlaybookResolutionError,
    SchemaProjectionError,
    available_paths,
    build_parser_for,
    playbook_fingerprint,
    project,
    project_parser,
)

GOLDEN_DIR = Path(__file__).parent / "golden_projections"

# Everything the MCP surface is a candidate to dispatch. `mirror` is
# deliberately absent — see test_mirror_since_is_unrepresentable.
CANDIDATE_VERBS = (
    "agent",
    "casts",
    "dispatch ack",
    "dispatch ls",
    "dispatch retry",
    "dispatch show",
    "doctor",
    "invoke end",
    "invoke list",
    "invoke start",
    "kill",
    "monitor",
    "orchestrate fanout",
    "orchestrate flow",
    "plugin info",
    "plugin list",
    "schedule create",
    "schedule delete",
    "schedule disable",
    "schedule enable",
    "schedule export",
    "schedule get",
    "schedule limits",
    "schedule list",
    "schedule runs",
    "schedule status",
    "schedule trigger",
    "schedule validate",
    "state ls",
    "state stats",
    "stats runs",
    "team create",
    "team list",
    "team receive",
    "team send",
    "team show",
)

# Every dispatchable path is pinned, not a sample of them. A caller acts on the
# schema it is handed, so an argparse change under any of these must fail here
# with a diff rather than quietly reshape a schema someone already validated
# against. `lifecycle` and `studio start` are pinned too: neither is a dispatch
# candidate, but both project and both are read by the surface.
GOLDEN_VERBS = CANDIDATE_VERBS + ("lifecycle", "studio start")


def _golden_path(verb: str) -> Path:
    return GOLDEN_DIR / f"{verb.replace(' ', '__')}.json"


@pytest.mark.parametrize("verb", GOLDEN_VERBS)
def test_golden_projection(verb: str) -> None:
    produced = project(verb).to_dict()
    path = _golden_path(verb)
    if os.environ.get("LIONAGI_UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(produced, indent=2, sort_keys=True) + "\n")
    assert path.is_file(), f"missing golden for {verb!r}; regenerate with LIONAGI_UPDATE_GOLDEN=1"
    expected = json.loads(path.read_text())
    assert produced == expected


@pytest.mark.parametrize("verb", CANDIDATE_VERBS)
def test_candidate_verb_projects(verb: str) -> None:
    projection = project(verb)
    assert projection.schema["type"] == "object"
    assert projection.schema["additionalProperties"] is False


def test_available_paths_lists_canonical_spellings_only() -> None:
    paths = available_paths()
    assert "orchestrate flow" in paths
    assert "monitor" in paths
    # `o` and `mon` are aliases; they resolve but are not separate commands.
    assert "o flow" not in paths
    assert "mon" not in paths
    assert build_parser_for("o flow") is not None
    assert set(project("o flow").schema["properties"]) == set(
        project("orchestrate flow").schema["properties"]
    )


# ── the seam ─────────────────────────────────────────────────────────────────


_ATTRIBUTE_IMPORT_PROBE = """\
import sys
from lionagi.cli import main as attribute_import
import lionagi.cli.main as dotted_import
from importlib import import_module

print(type(attribute_import).__name__)
print(type(dotted_import).__name__)
print(type(import_module("lionagi.cli.main")).__name__)
print(getattr(import_module("lionagi.cli.main"), "build_cli_parser", None) is not None)
"""


def test_attribute_style_import_of_cli_main_yields_the_function(tmp_path) -> None:
    """`lionagi.cli.__init__` resolves `main` lazily and pins the *callable*
    into its own globals. A process that reaches for the name that way first
    gets the function from then on, from both spellings, so the registry looks
    absent. `import_module` reads `sys.modules` and is unaffected by the order,
    which is why the projector uses it.

    Run in a subprocess because the outcome is decided by which import happens
    first in a process, and this one has already imported the module.
    """
    import subprocess

    script = tmp_path / "probe.py"
    script.write_text(_ATTRIBUTE_IMPORT_PROBE)
    out = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, check=True
    ).stdout.split()
    assert out == ["function", "function", "module", "True"]


def test_command_registry_is_reached_through_the_module() -> None:
    module = import_module("lionagi.cli.main")
    assert callable(module.build_cli_parser)
    assert callable(module.seed_for)


# ── unrepresentable means unavailable ────────────────────────────────────────


def test_mirror_since_is_unrepresentable_and_names_the_action() -> None:
    with pytest.raises(SchemaProjectionError) as excinfo:
        project("mirror")
    assert excinfo.value.action == "--since"
    assert "_since_window" in excinfo.value.reason


def test_callable_type_is_refused_rather_than_coerced_to_string() -> None:
    parser = argparse.ArgumentParser(prog="probe")
    parser.add_argument("--ok", type=str)
    parser.add_argument("--weird", type=json.loads)
    with pytest.raises(SchemaProjectionError) as excinfo:
        project_parser(parser, path="probe")
    assert excinfo.value.action == "--weird"
    assert "no scalar JSON counterpart" in excinfo.value.reason


def test_unknown_action_subclass_is_refused() -> None:
    class Doubling(argparse._StoreAction):
        def __call__(self, parser, namespace, values, option_string=None):
            setattr(namespace, self.dest, values * 2)

    parser = argparse.ArgumentParser(prog="probe")
    parser.add_argument("--n", action=Doubling)
    with pytest.raises(SchemaProjectionError) as excinfo:
        project_parser(parser, path="probe")
    assert excinfo.value.action == "--n"
    assert "Doubling" in excinfo.value.reason


def test_count_action_is_refused() -> None:
    parser = argparse.ArgumentParser(prog="probe")
    parser.add_argument("-v", "--verbose", action="count")
    with pytest.raises(SchemaProjectionError) as excinfo:
        project_parser(parser, path="probe")
    assert excinfo.value.action == "-v/--verbose"


def test_remainder_nargs_is_refused() -> None:
    parser = argparse.ArgumentParser(prog="probe")
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    with pytest.raises(SchemaProjectionError) as excinfo:
        project_parser(parser, path="probe")
    assert excinfo.value.action == "rest"
    assert "consumes argv verbatim" in excinfo.value.reason


def test_a_command_group_is_not_a_verb() -> None:
    with pytest.raises(SchemaProjectionError) as excinfo:
        project("schedule")
    assert "unresolved subcommand" in excinfo.value.reason
    assert "create" in excinfo.value.reason


def test_unknown_path_is_refused() -> None:
    with pytest.raises(SchemaProjectionError):
        project("agent nope")
    with pytest.raises(SchemaProjectionError):
        project("no-such-command")


# ── the bounded subset ───────────────────────────────────────────────────────


def test_store_true_and_store_false_project_as_booleans() -> None:
    parser = argparse.ArgumentParser(prog="probe")
    parser.add_argument("--on", action="store_true")
    parser.add_argument("--off", action="store_false")
    props = project_parser(parser, path="probe")["properties"]
    assert props["on"] == {"type": "boolean", "default": False, "x-flag": "--on"}
    assert props["off"] == {"type": "boolean", "default": True, "x-flag": "--off"}


def test_choices_project_as_enums() -> None:
    parser = argparse.ArgumentParser(prog="probe")
    parser.add_argument("--mode", choices=["a", "b"], default="a")
    props = project_parser(parser, path="probe")["properties"]
    assert props["mode"]["enum"] == ["a", "b"]
    assert props["mode"]["type"] == "string"


def test_nargs_project_as_arrays_with_bounds_where_known() -> None:
    parser = argparse.ArgumentParser(prog="probe")
    parser.add_argument("--pair", nargs=2, type=int)
    parser.add_argument("--many", nargs="*")
    parser.add_argument("--some", nargs="+")
    parser.add_argument("--rep", action="append")
    props = project_parser(parser, path="probe")["properties"]
    assert props["pair"] == {
        "type": "array",
        "items": {"type": "integer"},
        "minItems": 2,
        "maxItems": 2,
        "x-flag": "--pair",
    }
    assert "minItems" not in props["many"]
    assert props["some"]["minItems"] == 1
    assert props["rep"] == {"type": "array", "items": {"type": "string"}, "x-flag": "--rep"}


def test_optional_value_flag_admits_the_bare_form() -> None:
    schema = project("orchestrate flow").schema["properties"]["team_mode"]
    assert schema["anyOf"] == [{"type": "string"}, {"const": True}]
    assert schema["x-bare-value"] == "flow"


def test_positionals_keep_parser_order() -> None:
    parser = argparse.ArgumentParser(prog="probe")
    parser.add_argument("first")
    parser.add_argument("--flag")
    parser.add_argument("second", nargs="?")
    schema = project_parser(parser, path="probe")
    assert schema["x-positional-order"] == ["first", "second"]
    assert schema["required"] == ["first"]
    assert schema["properties"]["first"]["x-positional"] is True


def test_a_positional_that_accepts_zero_values_is_not_required() -> None:
    """The parser's own `required` flag is wrong here, so it is not consulted.

    argparse marks an ``nargs="*"`` positional required and then parses happily
    without it. Carrying that into the schema tells a caller a parameter is
    mandatory when the command it describes does not think so, and the caller
    cannot check. Asserted against the parser's behaviour rather than against its
    flag, because the flag is what changed: Python 3.14 stopped setting it, so a
    schema that trusted it described one unchanged command differently on
    different interpreters.
    """
    parser = argparse.ArgumentParser(prog="probe")
    parser.add_argument("query", nargs="*")
    parser.add_argument("--flag")

    parser.parse_args([])  # the command runs with nothing supplied for `query`
    assert "required" not in project_parser(parser, path="probe")


def test_aliases_are_recorded_against_the_long_option() -> None:
    schema = project("agent").schema["properties"]["agent"]
    assert schema["x-flag"] == "--agent"
    assert schema["x-aliases"] == ["-a"]


def test_mutually_exclusive_groups_are_reported() -> None:
    groups = project("studio start").schema["x-mutually-exclusive"]
    assert groups == [{"parameters": ["web", "docker", "no_frontend", "dev"], "required": False}]


def test_help_and_version_actions_are_not_parameters() -> None:
    schema = project("doctor").schema
    assert "help" not in schema["properties"]
    assert "version" not in schema["properties"]


def test_int_and_float_types_are_distinguished() -> None:
    props = project("monitor").schema["properties"]
    assert props["interval"]["type"] == "number"
    assert props["max_wait"]["type"] == "number"
    assert props["refresh"]["type"] == "integer"


# ── playbooks resolve in two stages ──────────────────────────────────────────

_PLAYBOOK = """\
description: A probe playbook.
agent: implementer
args:
  target_repo:
    type: str
    default: lionagi
    help: Repository to work in.
  deep:
    type: bool
    help: Run the long pass.
nodes:
  - id: n1
    prompt: hello
"""


@pytest.fixture
def playbook_dir(tmp_path, monkeypatch):
    """A project-local `.lionagi/playbooks/` that `_resolve_playbook_path` finds."""
    books = tmp_path / ".lionagi" / "playbooks"
    books.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    return books


def test_base_stage_advertises_the_playbook_parameter_only() -> None:
    base = project("orchestrate flow")
    assert base.stage == "base"
    assert base.playbook is None
    assert base.playbook_fingerprint is None
    assert "playbook" in base.schema["properties"]
    assert "x-playbook-arguments" in base.schema
    # The playbook's own arguments are not knowable yet.
    assert "target_repo" not in base.schema["properties"]


def test_resolved_stage_injects_the_playbook_arguments(playbook_dir) -> None:
    (playbook_dir / "probe.playbook.yaml").write_text(_PLAYBOOK)
    resolved = project("orchestrate flow", playbook="probe")

    assert resolved.stage == "resolved"
    assert resolved.playbook == "probe"
    assert set(resolved.playbook_parameters) == {"target_repo", "deep"}

    props = resolved.schema["properties"]
    assert props["target_repo"]["type"] == "string"
    assert props["target_repo"]["description"] == "Repository to work in."
    assert props["target_repo"]["x-from-playbook"] == "probe"
    assert props["deep"]["type"] == "boolean"
    assert props["deep"]["x-from-playbook"] == "probe"

    # Injection must not cost the built-in flags.
    assert set(project("orchestrate flow").schema["properties"]) < set(props)


def test_injection_does_not_leak_between_projections(playbook_dir) -> None:
    (playbook_dir / "probe.playbook.yaml").write_text(_PLAYBOOK)
    project("orchestrate flow", playbook="probe")
    assert "target_repo" not in project("orchestrate flow").schema["properties"]


def test_fingerprint_is_stable_for_an_unchanged_playbook(playbook_dir) -> None:
    (playbook_dir / "probe.playbook.yaml").write_text(_PLAYBOOK)
    first = project("orchestrate flow", playbook="probe")
    second = project("orchestrate flow", playbook="probe")
    assert first.playbook_fingerprint == second.playbook_fingerprint
    assert first.playbook_fingerprint.startswith("sha256:")
    assert first.schema["x-playbook-fingerprint"] == first.playbook_fingerprint


def test_fingerprint_changes_when_the_playbook_changes(playbook_dir) -> None:
    book = playbook_dir / "probe.playbook.yaml"
    book.write_text(_PLAYBOOK)
    before = project("orchestrate flow", playbook="probe").playbook_fingerprint

    book.write_text(_PLAYBOOK.replace("Run the long pass.", "Run the very long pass."))
    after = project("orchestrate flow", playbook="probe").playbook_fingerprint
    assert before != after


def test_fingerprint_changes_when_only_the_body_changes(playbook_dir) -> None:
    """The declared arguments are not the whole contract — the body is what
    runs, so a body edit must be visible to a caller that validated earlier."""
    book = playbook_dir / "probe.playbook.yaml"
    book.write_text(_PLAYBOOK)
    before = project("orchestrate flow", playbook="probe")

    book.write_text(_PLAYBOOK.replace("prompt: hello", "prompt: goodbye"))
    after = project("orchestrate flow", playbook="probe")
    assert before.schema["properties"].keys() == after.schema["properties"].keys()
    assert before.playbook_fingerprint != after.playbook_fingerprint


def test_unresolvable_playbook_is_an_error_not_a_base_schema(playbook_dir) -> None:
    with pytest.raises(PlaybookResolutionError):
        project("orchestrate flow", playbook="no-such-playbook-here")


def test_playbook_on_a_command_that_takes_none_is_refused() -> None:
    with pytest.raises(SchemaProjectionError) as excinfo:
        project("agent", playbook="probe")
    assert "takes no playbook" in excinfo.value.reason


def test_playbook_fingerprint_reports_the_resolved_path(playbook_dir) -> None:
    book = playbook_dir / "probe.playbook.yaml"
    book.write_text(_PLAYBOOK)
    fingerprint, resolved = playbook_fingerprint("probe")
    assert Path(resolved) == book
    assert len(fingerprint.removeprefix("sha256:")) == 32
