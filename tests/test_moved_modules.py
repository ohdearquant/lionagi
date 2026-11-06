"""Every module that moved out of lionagi keeps its old import path until its removal release.

The table is ``lionagi._moved.MOVED``. For every module under each moved package these
tests check that the old name imports, that it is the same module object as the new
name, that it warns once and points the warning at the importing code, that the
removal release is still ahead of this version, that data naming a moved Element
class by either path loads in a fresh process and saves under the new path, that the
class-path allow-list adds only the moved packages' new prefixes to ``lionagi.``, that no
source file in lionagi or lionhub imports an old name, and that the CHANGELOG names every
move.
"""

from __future__ import annotations

import ast
import importlib
import json
import pickle
import pkgutil
import re
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from lionagi import _moved
from lionagi.version import __version__

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (ROOT / "lionagi", ROOT / "lionhub" / "src" / "lionhub")


def _release(text: str) -> tuple[int, ...]:
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", text)
    assert match, f"not a release number: {text!r}"
    return tuple(int(part) for part in match.groups())


def _inner_rows(row: _moved.Moved) -> list[_moved.Moved]:
    """Rows whose new package sits inside ``row``'s new package."""
    return [other for other in _moved.MOVED if other.new.startswith(row.new + ".")]


def _module_pairs() -> list[tuple[str, str]]:
    pairs = []
    for row in _moved.MOVED:
        package = importlib.import_module(row.new)
        names = [row.new]
        search_path = getattr(package, "__path__", None)  # a lone module has no submodules
        if search_path is not None:
            names += [info.name for info in pkgutil.walk_packages(search_path, row.new + ".")]
        # A package moved inside this one is paired under its own row, by the name it had.
        inner = tuple(other.new for other in _inner_rows(row))
        names = [n for n in names if not any(n == p or n.startswith(p + ".") for p in inner)]
        pairs += [(row.old + name[len(row.new) :], name) for name in sorted(names)]
    return pairs


PAIRS = _module_pairs()

# These whole modules deliberately export values rather than pickleable definitions.
# Any other row without a callable subject still fails the population check.
CONSTANT_ONLY_MOVED_MODULES = {
    "lionagi.cli._mirror_settings": (
        "lionhub.serve.mirror_settings",
        {"MIRROR_PREVIEW_CHARS": 37},
    ),
}


def test_the_table_has_moves_and_every_package_has_modules():
    assert _moved.MOVED
    for row in _moved.MOVED:
        assert any(new == row.new for _, new in PAIRS), row
    assert len(PAIRS) > len(_moved.MOVED)


@pytest.mark.parametrize(("old", "new"), PAIRS, ids=[old for old, _ in PAIRS])
def test_old_name_is_the_new_module_and_warns_once(old, new):
    parent = old.rpartition(".")[0]
    if _moved.resolve(parent) is not None:
        importlib.import_module(parent)
    sys.modules.pop(old, None)
    target = importlib.import_module(new)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        module = importlib.import_module(old)
        again = importlib.import_module(old)

    assert module is target
    assert again is target
    assert sys.modules[old] is target
    assert target.__spec__.name == new
    assert target.__name__ == new

    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1, [str(w.message) for w in deprecations]
    message = str(deprecations[0].message)
    row = _moved._row(old)
    assert old in message and new in message and row.removed_in in message
    assert deprecations[0].filename == __file__


def test_import_statement_warning_points_at_the_importing_line():
    old, new = PAIRS[0]
    sys.modules.pop(old, None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        exec(f"import {old}", {"__name__": "user_code"})  # noqa: S102
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1
    assert deprecations[0].filename == "<string>"
    assert sys.modules[old] is sys.modules[new]


def test_objects_are_shared_and_pickles_naming_the_old_path_load():
    checked = {row.old: 0 for row in _moved.MOVED}
    for old, new in PAIRS:
        importlib.import_module(old)
        assert sys.modules[old] is sys.modules[new]
        module = sys.modules[new]
        for name, obj in vars(module).items():
            if name.startswith("_") or getattr(obj, "__module__", None) != new:
                continue
            if getattr(obj, "__qualname__", None) != name:
                continue
            # Protocol 0 writes module names as text lines, so the rename is byte-safe.
            payload = pickle.dumps(obj, protocol=0).replace(new.encode(), old.encode())
            assert old.encode() in payload
            assert pickle.loads(payload) is obj  # noqa: S301
            checked[_moved._row(old).old] += 1
    rows = {row.old: row.new for row in _moved.MOVED}
    for old, (new, values) in CONSTANT_ONLY_MOVED_MODULES.items():
        assert rows.get(old) == new, (old, new, rows.get(old))
        assert set(values) <= vars(importlib.import_module(new)).keys()
    assert all(count for old, count in checked.items() if old not in CONSTANT_ONLY_MOVED_MODULES), (
        checked
    )
    assert all(checked[old] == 0 for old in CONSTANT_ONLY_MOVED_MODULES), checked


def _moved_element_classes() -> list[tuple[str, type]]:
    from lionagi.protocols.generic.element import Element

    found = []
    for old, new in PAIRS:
        for obj in vars(importlib.import_module(new)).values():
            if isinstance(obj, type) and issubclass(obj, Element) and obj.__module__ == new:
                found.append((old, obj))
    return found


ELEMENT_CLASSES = _moved_element_classes()


def _work_form(cls: type) -> object:
    field_spec = importlib.import_module(cls.__module__).FieldSpec
    # The field is given in its saved form, as a form loaded from storage holds it, so the
    # nested element's metadata names its class too.
    field = field_spec(name="amount", type="int").to_dict()
    return cls(title="intake", fields={"amount": field})


# One instance of every Element subclass a moved module defines. Element data names its
# class by module path (metadata["lion_class"]), so each one is loaded below from data
# naming either path.
SAMPLES = {
    "lionhub.orchestration.work.form.FieldSpec": lambda cls: cls(name="amount", type="int"),
    "lionhub.orchestration.work.form.WorkForm": _work_form,
}

# Runs in a fresh interpreter, where nothing from lionhub is imported until the data
# names it: a process that already imported the class resolves it from sys.modules and
# would pass whatever the allow-list says.
_LOAD_IN_FRESH_PROCESS = """
import importlib, json, sys
from lionagi.protocols.generic.element import Element
text, how, target = sys.argv[1:4]
cold = not any(name == "lionhub" or name.startswith("lionhub.") for name in sys.modules)
try:
    if how == "from_dict":
        obj = Element.from_dict(json.loads(text))
    else:
        module, _, name = target.rpartition(".")
        obj = getattr(importlib.import_module(module), name).model_validate(json.loads(text))
    result = {"class": type(obj).__module__ + "." + type(obj).__qualname__, "json": obj.to_json()}
except Exception as exc:
    result = {"error": type(exc).__name__ + ": " + str(exc)}
result["cold"] = cold
result["lionhub_after"] = sorted(
    name for name in sys.modules if name == "lionhub" or name.startswith("lionhub.")
)
print(json.dumps(result))
"""


def _load_in_fresh_process(text: str, how: str, target: str = "") -> dict:
    proc = subprocess.run(  # noqa: S603 -- this interpreter, a fixed script
        [sys.executable, "-c", _LOAD_IN_FRESH_PROCESS, text, how, target],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_every_moved_element_class_has_a_sample():
    assert {f"{cls.__module__}.{cls.__qualname__}" for _, cls in ELEMENT_CLASSES} == set(SAMPLES)


def test_the_form_sample_nests_an_element_that_names_its_class():
    # The test below rewrites every class path in the data, the nested one included, and
    # expects each to be saved under the new path again.
    cls = next(cls for _, cls in ELEMENT_CLASSES if cls.__qualname__ == "WorkForm")
    saved = json.loads(SAMPLES[f"{cls.__module__}.WorkForm"](cls).to_json())
    assert saved["fields"]["amount"]["metadata"]["lion_class"] == f"{cls.__module__}.FieldSpec"


@pytest.mark.parametrize(
    ("old", "cls"), ELEMENT_CLASSES, ids=[cls.__qualname__ for _, cls in ELEMENT_CLASSES]
)
def test_moved_element_data_loads_in_a_fresh_process_under_either_path(old, cls):
    new = cls.__module__
    path = f"{new}.{cls.__qualname__}"
    text = SAMPLES[path](cls).to_json()
    expected = json.loads(text)
    old_text = text.replace(f'"{new}.', f'"{old}.')
    assert old_text.count(f'"{old}.') == text.count(f'"{new}.') > 0

    for data in (text, old_text):
        result = _load_in_fresh_process(data, "from_dict")
        assert result["cold"], "lionhub was imported before the data was read"
        assert "error" not in result, result["error"]
        assert result["class"] == path
        assert json.loads(result["json"]) == expected

    # Metadata written before the move names the old path; the class accepts it as its own.
    result = _load_in_fresh_process(old_text, "model_validate", path)
    assert "error" not in result, result["error"]
    assert json.loads(result["json"]) == expected


@pytest.mark.parametrize(
    "module",
    [
        # Names that share letters with an allowed prefix but not the prefix itself.
        "lionhubx.orchestration.work.form",
        "lionhub_x.orchestration.work.form",
        # lionhub code outside the moved packages: the top-level package and a sibling.
        "lionhub",
        "lionhub.canon.work.form",
    ],
)
def test_a_path_outside_the_allowed_prefixes_is_refused_in_a_fresh_process(module):
    _, cls = ELEMENT_CLASSES[0]
    new = cls.__module__
    text = SAMPLES[f"{new}.{cls.__qualname__}"](cls).to_json()
    outside = text.replace(f'"{new}.', f'"{module}.')
    assert outside != text

    result = _load_in_fresh_process(outside, "from_dict")
    assert result["cold"]
    assert "register_type_prefix" in result["error"], result
    assert result["lionhub_after"] == []


@pytest.mark.parametrize(
    "path",
    [
        "lionhub..orchestration.work.form.WorkForm",
        "lionhub.orchestration..work.form.WorkForm",
        ".lionhub.orchestration.work.form.WorkForm",
    ],
)
def test_a_malformed_lionhub_path_is_refused_before_anything_is_imported(path):
    _, cls = ELEMENT_CLASSES[0]
    name = f"{cls.__module__}.{cls.__qualname__}"
    text = SAMPLES[name](cls).to_json()
    malformed = text.replace(json.dumps(name), json.dumps(path), 1)
    assert malformed != text

    result = _load_in_fresh_process(malformed, "from_dict")
    assert result["cold"]
    assert "not a dotted class path" in result["error"], result
    assert result["lionhub_after"] == []


def test_the_allow_list_adds_only_the_moved_packages_to_lionagi():
    # Data may import a module under a moved package's new prefix (ADR-0001 Amendment 2)
    # because the finder serves every module under that prefix by its old lionagi name,
    # which "lionagi." already admits; the old-name tests above import each one that way.
    # Any other prefix would reach code no lionagi name reaches and needs its own decision.
    from lionagi.ln._utils import _DEFAULT_ALLOWED_PREFIXES

    added = _DEFAULT_ALLOWED_PREFIXES - {"lionagi."}
    moved = {f"{row.new}." for row in _moved.MOVED}
    assert added
    assert added <= moved
    assert all(any(prefix.startswith(a) for a in added) for prefix in moved)


# A module that moved on its own (one file, not a package) is a row of the table too. Its old
# name sits inside a package that stays in lionagi/, so the finder must serve it while that
# package is still on disk.
MOVED_CORE_MODULES = (
    ("lionagi.providers._provider_errors", "lionagi._provider_errors"),
    ("lionagi.providers._secret_resolution", "lionagi.service.accounts._secret_resolution"),
    ("lionagi.providers._subagent_admission", "lionagi.service._subagent_admission"),
    ("lionagi.providers.openai._chat_schemas", "lionagi.service.connections._chat_schemas"),
)
MOVED_MODULES = (
    *MOVED_CORE_MODULES,
    ("lionagi.cli._argtypes", "lionhub.orchestration.arguments"),
    ("lionagi.cli._code_identity", "lionhub.serve.diagnostics"),
    ("lionagi.cli._logging", "lionhub.orchestration.console"),
    ("lionagi.cli._project", "lionhub.orchestration.project"),
    ("lionagi.cli._mcp_resolve", "lionhub.orchestration.mcp_config"),
    ("lionagi.cli._team_service", "lionhub.orchestration.team"),
    ("lionagi.cli._engine_definitions", "lionhub.orchestration.engine_definitions"),
    ("lionagi.cli._util", "lionhub.orchestration.job"),
    ("lionagi.cli._inspection", "lionhub.orchestration.inspection"),
    ("lionagi.cli._providers", "lionhub.orchestration.spawn"),
    ("lionagi.cli._orchestrate_parsing", "lionhub.orchestration.flow.parsing"),
    ("lionagi.cli._schedule_parsing", "lionhub.orchestration.schedule.parsing"),
    ("lionagi.cli._mirror_service", "lionhub.serve.mirror"),
    ("lionagi.cli._runs", "lionhub.orchestration.runs"),
    ("lionagi.cli._kill_service", "lionhub.orchestration.kill"),
    ("lionagi.cli.orchestrate._checkpoint", "lionhub.orchestration.flow.checkpoint"),
)
_MOVED_MODULE_IDS = [old for old, _ in MOVED_MODULES]


@pytest.mark.parametrize(("old", "new"), MOVED_MODULES, ids=_MOVED_MODULE_IDS)
def test_an_old_module_name_warns_once_and_is_the_new_module(old, new):
    sys.modules.pop(old, None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        module = importlib.import_module(old)
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1, [str(w.message) for w in deprecations]
    assert new in str(deprecations[0].message)
    assert deprecations[0].filename == __file__
    assert module is importlib.import_module(new)
    assert sys.modules[old] is module


# What each moved module offers its callers. Naming some of it keeps the identity check
# below from passing on a module that defines nothing.
MOVED_MODULE_PUBLIC_NAMES = {
    "lionhub.orchestration.runs": {
        "RunDir",
        "active_run_id",
        "allocate_run",
        "current_run_id",
        "find_branch",
        "find_incomplete_session_for_run",
        "list_runs",
        "load_last_branch",
        "lost_terminal_write",
        "register_branch_hook",
        "resolve_run_reason",
        "save_last_branch_pointer",
        "setup_agent_persist",
        "teardown_orchestration_persist",
        "teardown_persist",
    },
    "lionhub.orchestration.kill": {
        "check_pid_identity",
        "imported_degraded_process_is_live",
        "kill_one",
        "list_running_children",
        "read_pid_from_entity",
        "unaddressable_pid_reason",
    },
    "lionhub.orchestration.flow.checkpoint": {
        "CheckpointWriter",
        "FlowResumeError",
        "UnsupportedFlowCheckpointVersionError",
        "load_checkpoint",
        "resolve_checkpoint_target",
    },
    "lionhub.orchestration.spawn": {
        "AgentProfile",
        "AgentProfileNotFoundError",
        "add_common_cli_args",
        "build_agent_profile_catalog",
        "build_chat_model",
        "build_deadline_preamble",
        "build_imodel_from_spec",
        "list_agents",
        "load_agent_profile",
        "profile_config",
        "resolve_persisted_effort",
    },
    "lionhub.orchestration.flow.parsing": {
        "add_orchestrate_subparser",
    },
    "lionhub.orchestration.schedule.parsing": {
        "add_schedule_subparser",
        "build_create_body",
        "normalize_host",
        "suggest_schedule_flag",
    },
    "lionhub.serve.mirror": {
        "mirror_forever",
    },
    "lionagi._provider_errors": {"ProviderError", "EmissionError"},
    "lionagi.service.accounts._secret_resolution": {
        "SecretLookupResolution",
        "fill_declared_secrets",
        "lookup_one_name",
        "resolve_secret_lookup_config",
    },
    "lionagi.service._subagent_admission": {
        "SubagentLease",
        "admit_subagent",
        "child_agent_depth",
        "refuse_nested_agent",
    },
    "lionhub.orchestration.arguments": {"BoundedInt", "JsonArgument"},
    "lionhub.serve.diagnostics": {
        "code_identity",
        "git_identity",
        "loaded_package_path",
        "snapshot_git_position",
    },
    "lionhub.orchestration.console": {"configure_cli_logging"},
    "lionhub.orchestration.project": {"detect_project"},
    "lionhub.orchestration.mcp_config": {
        "McpConfigError",
        "LegacyMcpReplayError",
        "McpResolution",
        "discover_mcp_config",
        "explicit_mcp_config_path",
        "resolve_spawn_mcp_servers",
    },
    "lionhub.orchestration.team": {"read_team_json"},
    "lionhub.orchestration.engine_definitions": {"import_engine_class"},
    "lionhub.orchestration.job": {
        "AmbiguousIdError",
        "mark_run_allocated",
        "begin_invocation",
        "end_invocation",
        "clear_run_allocation",
        "run_was_allocated",
        "validate_cwd_exists",
        "classify_exception",
        "pid_alive",
        "recorded_pid_is_foreign",
        "recorded_identity_mode",
        "fetch_unique_row",
        "resolve_entity",
    },
    "lionhub.orchestration.inspection": {
        "fetch_by_id",
        "run_id_candidates",
        "resolve_agent_target",
        "resolve_play_target",
        "resolve_any_target",
        "resolve_primary_session",
    },
    # The request model, every class it is built from, and the developer-message helper.
    "lionagi.service.connections._chat_schemas": {
        "AssistantMessage",
        "ChatRole",
        "DeveloperMessage",
        "FunctionCall",
        "FunctionDef",
        "FunctionTool",
        "ImageURLObject",
        "ImageURLPart",
        "JSONSchemaFormat",
        "OpenAIChatCompletionsRequest",
        "ResponseFormatJSONObject",
        "ResponseFormatJSONSchema",
        "ResponseFormatText",
        "StreamOptions",
        "SystemMessage",
        "TextPart",
        "ToolCall",
        "ToolCallFunction",
        "ToolChoiceFunction",
        "ToolMessage",
        "UserMessage",
        "uses_developer_messages",
    },
}

MOVED_MODULE_PRIVATE_NAMES = {
    "lionhub.orchestration.runs": {
        "_TerminalStatus",
        "_atomic_write_json",
        "_branch_dirs",
        "_branch_end_is_this_runs",
        "_carry_earliest_time",
        "_drop_a_time_that_is_not_one",
        "_flush_pending_message_events",
        "_has_assistant_output_evidence",
        "_is_time",
        "_linked_engine_session",
        "_live_message_dict",
        "_make_message_handler",
        "_manifest_run_id_matches",
        "_new_run_id",
        "_open_shared_db",
        "_record_persistence_degraded",
        "_reopen_session_for_resume",
        "_resolve_project",
        "_resolved_run_state_dir",
        "_run_manifest_lock",
        "_run_may_clear_current_phase",
        "_signal_persist_refusal",
        "_signal_window_marker",
        "_store_error_in",
        "_teardown_common",
        "_teardown_write",
        "_terminal_status_extra_fields",
        "_unroute_message_hooks",
        "_validate_manifest_run_id",
        "_write_signal_persistence_degraded_at",
        "_write_terminal_session_status",
        "_write_terminal_write_lost",
    },
    "lionhub.orchestration.kill": {
        "_CancelPersistenceError",
        "_WorkerCleanup",
        "_cmdline_is_lionagi",
        "_degraded_run",
        "_imported_degraded_process_identity",
        "_kill_degraded_run",
        "_persist_cancel",
        "_pid_addressability",
        "_pid_is_zombie",
        "_run_manifest_path",
        "_runner_process_create_time",
        "_same_cleanup_session",
        "_settle_degraded_run",
        "_signal_degraded_process",
        "_terminate_pid",
        "_try_recorded_worker_cleanup",
    },
    "lionhub.orchestration.flow.checkpoint": {
        "_find_run_dir_by_id",
        "_raise_if_persistence_degraded",
    },
    "lionhub.orchestration.spawn": {
        "_add_claude_tool_kwargs",
        "_add_codex_account_integration_kwargs",
        "_add_workload_kwarg",
        "_check_profile_mcp_frontmatter",
        "_contain_flag_of",
        "_contain_flag_values",
        "_is_light_codex",
        "_keys_like_the_contain_flag",
        "_parse_profile",
        "_parse_profile_account_connectors",
        "_parse_profile_contain_account_integrations",
        "_parse_profile_contain_user_settings",
        "_parse_profile_mcp",
        "_parse_profile_resume_on_timeout",
        "_parse_profile_timeout",
        "_parse_profile_workload",
        "_plugin_agent_profiles",
        "_profile_path_candidates",
        "_profile_tool_settings",
        "_resolve_plugin_profile_path",
        "_settings_of_unread_frontmatter",
        "_validate_bare_name",
        "_warn_if_shadowing_plugin_profile",
    },
    "lionhub.orchestration.flow.parsing": {
        "_add_mcp_config_args",
    },
    "lionhub.orchestration.schedule.parsing": {
        "_base_url",
        "_chain_action_with_usable_selected_kind",
        "_effective_chain_kind",
        "_is_schedule_request_timeout",
        "_merge_chain_action",
        "_normalize_schedule_reason",
        "_parse_chain_action",
        "_schedule_request_timeout_message",
        "_selected_chain_kind",
        "_url_authority",
        "_validate_chain_action_node",
        "_validate_chain_kinds",
        "_validate_identifier",
        "_validate_reachable_chain_actions",
        "_warn_if_cron_far_out",
    },
    "lionhub.serve.mirror": {
        "_FileState",
        "_Lineage",
        "_absorb_backfill",
        "_attribute_idle",
        "_attribute_idle_codex",
        "_codex_pass",
        "_derive_codex_metadata",
        "_derive_metadata",
        "_display_path",
        "_fallback_project",
        "_first_codex_prompt",
        "_first_prompt",
        "_load_states",
        "_mirror_one",
        "_mirror_one_codex",
        "_needs_status_reconciliation",
        "_one_pass",
        "_parse_window",
        "_peek_codex_head",
        "_peek_head",
        "_read_new_events",
        "_recent_tool_names",
        "_resolve_project_for_mirror",
        "_save_states",
        "_seed_lineage",
        "_since_window",
    },
    "lionhub.orchestration.job": {
        "_like_prefix_pattern",
        "_fetch_exact_row",
        "_fetch_prefix_rows",
    },
    "lionhub.orchestration.inspection": {
        "_latest_session",
        "_resolve_session_by_branch_id",
        "_resolve_session_by_run_id",
    },
    "lionhub.orchestration.console": {"_ErrorFormatter", "_LazyStderrHandler"},
    "lionhub.orchestration.project": {
        "_from_config_toml",
        "_read_project_from_toml",
        "_from_global_overrides",
        "_git_remote_slug",
        "_parse_remote_url",
    },
    "lionhub.orchestration.mcp_config": {
        "_reject_json_constant",
        "_read_servers",
        "_read_config",
        "_snapshot_path",
        "_format_recorded_mcp_path",
    },
    "lionhub.orchestration.engine_definitions": {"_is_registered_engine_kind"},
}
MOVED_MODULE_SHARED_VALUES = {
    "lionhub.orchestration.runs": {
        "LIONAGI_HOME",
        "PERSISTENCE_DEGRADED_REASON_FIELD",
        "RUNS_ROOT",
        "SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD",
        "TERMINAL_WRITE_LOST_FIELD",
        "_ALLOCATED_RUN_ID",
        "_LAST_BRANCH_POINTER",
        "_LEGACY_AGENTS_ROOT",
        "_RUN_ID_ENV_VAR",
        "_SETUP_RECOVERY_TIMEOUT_SECONDS",
        "_log",
        "teardown_agent_persist",
    },
    "lionhub.orchestration.kill": {
        "BOOT_TIME_TOLERANCE",
        "NOT_JUDGEABLE_HERE",
        "NOT_STOPPED_SIGNALS",
        "_BOOT_TIME_TOLERANCE",
        "_IdentityVerdict",
        "_LOCALLY_SIGNALLABLE_MODES",
        "_REFUSED_SIGNAL_REASONS",
        "_STALE_SWEEP_ORDER",
    },
    "lionhub.orchestration.flow.checkpoint": {
        "CHECKPOINT_VERSION",
        "PERSISTENCE_DEGRADED_REASON_FIELD",
        "RUNS_ROOT",
        "RunDir",
    },
    "lionhub.orchestration.spawn": {
        "BACKENDS",
        "CLI_PROVIDERS",
        "EFFORT_LEVELS",
        "MAX_TIMEOUT_SECONDS",
        "PROVIDERS_EFFORT_VIA_MODEL_NAME",
        "PROVIDERS_NO_EFFORT",
        "PROVIDER_BYPASS_KWARGS",
        "PROVIDER_EFFORT_KWARG",
        "PROVIDER_FAST_KWARGS",
        "PROVIDER_TO_ALIAS",
        "PROVIDER_YOLO_KWARGS",
        "_CONTAIN_KEY",
        "_DASHES",
        "_FLAG_KEY_LINE",
        "_PROFILE_TOOL_FIELDS",
    },
    "lionhub.orchestration.flow.parsing": {
        "add_common_cli_args",
    },
    "lionhub.orchestration.schedule.parsing": {
        "_ALIAS_ACTION_KINDS",
        "_ALL_SCHEDULE_FLAGS",
        "_CHAIN_ACTION_ALLOWED_KEYS",
        "_IDENT_RE",
        "_MAX_CHAIN_DEPTH",
        "_MODEL_RE",
        "_SCHEDULE_FLAG_SYNONYMS",
        "_VALID_ACTION_KINDS",
        "_warned_api_suffix",
    },
    "lionhub.serve.mirror": {
        "CLAUDE_PROJECTS_DIR",
        "CODEX_SESSIONS_DIR",
        "_COMMAND_NOISE",
        "_DEFAULT_LIVE_WINDOW",
        "_LEGACY_TOOL_NAMES_KEEP",
        "_OFFSETS_PATH",
        "_log",
    },
    "lionhub.orchestration.job": {
        "EXIT_CODE_BY_STATUS",
        "EXIT_CODE_ENVIRONMENT_ERROR",
        "EXIT_CODE_SUBAGENT_REFUSED",
        "UNRECOGNIZED_IDENTITY_MODE",
        "BOOT_TIME_TOLERANCE",
        "_allocation_lock",
        "_invocations_in_flight",
        "_run_allocated",
        "_SEARCH_ORDER",
        "_TABLE_TO_ENTITY_TYPE",
        "_CANDIDATES_SHOWN",
    },
    "lionhub.orchestration.inspection": {
        "_SESSION_SUCCESS",
        "_SESSION_FAILURE",
        "_PLAY_SUCCESS",
        "_PLAY_FAILURE",
        "EXIT_RUNNING",
        "EXIT_UNKNOWN",
        "_DB_BUSY_TIMEOUT_S",
        "RUN_ID_THROUGH_T",
    },
    "lionhub.orchestration.project": {
        "_SOURCE_CONFIG_TOML",
        "_SOURCE_GLOBAL_OVERRIDE",
        "_SOURCE_GIT_REMOTE",
    },
    "lionhub.orchestration.mcp_config": {
        "MCP_CONFIG_FILENAME",
        "SNAPSHOT_SOURCE_KEY",
        "SNAPSHOT_SEARCHED_FROM_KEY",
    },
    "lionhub.orchestration.engine_definitions": {"KIND_META"},
}


@pytest.mark.parametrize(("old", "new"), MOVED_MODULES, ids=_MOVED_MODULE_IDS)
def test_every_public_object_of_a_moved_module_is_the_same_object_under_the_old_name(old, new):
    module = importlib.import_module(new)
    public = [
        name
        for name, obj in vars(module).items()
        if not name.startswith("_") and callable(obj) and getattr(obj, "__module__", None) == new
    ]
    assert MOVED_MODULE_PUBLIC_NAMES[new] <= set(public)
    for name in public:
        namespace: dict = {}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            exec(f"from {old} import {name}", namespace)  # noqa: S102
        assert namespace[name] is getattr(module, name), name


def test_a_moved_module_keeps_the_logger_name_it_had():
    module = importlib.import_module("lionagi.service.accounts._secret_resolution")
    assert module.logger.name == "lionagi.providers._secret_resolution"


@pytest.mark.parametrize(
    ("old", "new"), MOVED_CORE_MODULES, ids=[old for old, _ in MOVED_CORE_MODULES]
)
def test_a_module_moved_into_the_core_keeps_its_own_row_after_its_package_moved(old, new):
    # The package around each of these modules later moved to lionhub as a whole. The longer,
    # module-level row still wins for the module's own name, and only for it.
    assert not _moved.still_on_disk("lionagi.providers")
    assert not _moved.still_on_disk(old)
    assert _moved.resolve(old) == new
    hub_twin = "lionhub.providers" + old[len("lionagi.providers") :]
    assert _moved.resolve(old + "x") == hub_twin + "x"
    assert _moved.resolve("lionagi.providers") == "lionhub.providers"
    assert _moved.resolve("lionagi.providers.errors") == "lionhub.providers.errors"
    finder = next(f for f in sys.meta_path if getattr(f, "lionagi_moved", False))
    spec = finder.find_spec(old)
    assert spec is not None
    assert spec.submodule_search_locations is None
    package = finder.find_spec("lionagi.providers")
    assert package is not None
    assert package.submodule_search_locations is not None


def test_a_lazy_type_naming_the_old_chat_schema_module_resolves_to_the_same_class():
    # Provider configs name their request model as a "module:Class" string, which reaches the
    # module through importlib, so a string written against the old name must keep working.
    from lionagi.service.connections.provider_config import LazyType

    old = "lionagi.providers.openai._chat_schemas"
    new = "lionagi.service.connections._chat_schemas"
    sys.modules.pop(old, None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        resolved = LazyType(f"{old}:OpenAIChatCompletionsRequest").resolve()
    assert resolved is importlib.import_module(new).OpenAIChatCompletionsRequest
    assert any(issubclass(w.category, DeprecationWarning) and new in str(w.message) for w in caught)


def test_reload_through_the_old_name_keeps_the_real_spec():
    old, new = PAIRS[0]
    module = importlib.import_module(old)
    assert importlib.reload(module) is sys.modules[new]
    assert module.__spec__.name == new
    assert module.__name__ == new


def _run_fresh(code: str, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(  # noqa: S603 -- this interpreter, a fixed program
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    return proc.stdout


_MOVED_MODULE_COLD_IDENTITY = """
import importlib, pickle, sys, warnings
from unittest.mock import patch

def alias_deprecations(items):
    return [
        item for item in items
        if issubclass(item.category, DeprecationWarning)
        and str(item.message).startswith(old + " moved to ")
    ]

assert old not in sys.modules and new not in sys.modules
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    first = importlib.import_module(old if old_first else new)
    second = importlib.import_module(new if old_first else old)
    again = importlib.import_module(old)

assert first is second is again is sys.modules[old] is sys.modules[new]
assert first.__spec__.name == new and first.__name__ == new
deprecations = alias_deprecations(caught)
assert len(deprecations) == 1, [str(item.message) for item in deprecations]
assert old in str(deprecations[0].message) and new in str(deprecations[0].message)
assert deprecations[0].filename == "<string>"

sys.modules.pop(old)
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    for name in names:
        obj = getattr(first, name)
        assert obj is getattr(second, name)
        payload = pickle.dumps(obj, protocol=0)
        assert new.encode() in payload
        payload = payload.replace(new.encode(), old.encode())
        assert old.encode() in payload
        assert pickle.loads(payload) is obj
assert len(alias_deprecations(caught)) == 1

sentinel = object()
with patch(old + "." + names[0], sentinel):
    assert getattr(sys.modules[new], names[0]) is sentinel

for name in value_names:
    value = getattr(first, name)
    assert value is getattr(second, name)
    with patch(old + "." + name, sentinel):
        assert getattr(sys.modules[new], name) is sentinel
    assert getattr(first, name) is value
    with patch(new + "." + name, sentinel):
        assert getattr(sys.modules[old], name) is sentinel
    assert getattr(second, name) is value

if new == "lionhub.orchestration.job":
    legacy = sys.modules[old]
    canonical = sys.modules[new]
    legacy.clear_run_allocation()
    try:
        legacy._invocations_in_flight = 3
        assert canonical._invocations_in_flight == 3
        canonical._invocations_in_flight = 0
        assert legacy._invocations_in_flight == 0
        legacy._run_allocated = True
        assert canonical.run_was_allocated()
        canonical._run_allocated = False
        assert not legacy.run_was_allocated()

        legacy.begin_invocation()
        assert canonical._invocations_in_flight == 1
        canonical.mark_run_allocated()
        assert legacy.run_was_allocated()
        canonical.end_invocation()
        assert legacy._invocations_in_flight == 0
        canonical.begin_invocation()
        assert legacy._invocations_in_flight == 1
        assert not legacy.run_was_allocated()
        legacy.end_invocation()
        assert canonical._invocations_in_flight == 0

        consumer = importlib.import_module("lionagi.cli.main")
        assert consumer.run_was_allocated is canonical.run_was_allocated
        with patch(old + "._run_allocated", True):
            assert consumer.run_was_allocated()
        with patch(old + "._run_allocated", False):
            assert not consumer.run_was_allocated()
    finally:
        canonical.clear_run_allocation()

if new == "lionhub.orchestration.spawn":
    from types import SimpleNamespace

    profile = SimpleNamespace(
        name="example", extra={"tools": ["Read"], "allowed_tools": ["Read"]}
    )
    with patch(old + "._PROFILE_TOOL_FIELDS", ("tools",)):
        assert second._profile_tool_settings(profile) == {"tools": "Read"}
    with patch(new + "._PROFILE_TOOL_FIELDS", ("allowed_tools",)):
        assert first._profile_tool_settings(profile) == {"allowed_tools": ["Read"]}

if new == "lionhub.serve.mirror":
    entries = {"earlier": "read", "middle": "write", "latest": "search"}
    with patch(old + "._LEGACY_TOOL_NAMES_KEEP", 1):
        assert second._recent_tool_names(entries) == {"latest": "search"}
    with patch(new + "._LEGACY_TOOL_NAMES_KEEP", 2):
        assert first._recent_tool_names(entries) == {"middle": "write", "latest": "search"}
    assert first._log is second._log
    assert first._log.name == "lionagi.cli.mirror"

if new == "lionhub.orchestration.schedule.parsing":
    import os

    with patch.dict(os.environ, {"LIONAGI_STUDIO_URL": "http://localhost:8765/api"}):
        with patch(new + "._warned_api_suffix", False), patch(old + ".warn") as warning:
            assert first._base_url() == "http://localhost:8765"
            assert second._warned_api_suffix is True
            assert second._base_url() == "http://localhost:8765"
            warning.assert_called_once()

if new == "lionhub.orchestration.runs":
    legacy, canonical = sys.modules[old], sys.modules[new]
    saved = canonical._ALLOCATED_RUN_ID
    try:
        legacy._ALLOCATED_RUN_ID = "first-run"
        assert canonical.active_run_id() == "first-run"
        canonical._ALLOCATED_RUN_ID = "second-run"
        assert legacy.active_run_id() == "second-run"
        with patch(new + "._ALLOCATED_RUN_ID", None), patch(old + ".current_run_id", return_value="inherited-run"):
            assert canonical.active_run_id() == "inherited-run"
    finally:
        canonical._ALLOCATED_RUN_ID = saved

if new == "lionhub.orchestration.kill":
    import asyncio

    legacy, canonical = sys.modules[old], sys.modules[new]
    saved = canonical._REFUSED_SIGNAL_REASONS["foreign_mode"]
    try:
        with patch(new + ".unaddressable_pid_reason", return_value="foreign_mode"), patch(new + ".warn") as warning:
            with patch(new + ".read_pid_from_entity", side_effect=AssertionError("unexpected pid read")):
                for writer, reader, text in ((legacy, canonical, "first refusal"), (canonical, legacy, "second refusal")):
                    writer._REFUSED_SIGNAL_REASONS["foreign_mode"] = text
                    outcome = asyncio.run(reader.kill_one(None, "invocation", "sample", {}, user_reason="test"))
                    assert outcome["signal"] == "foreign_mode" and outcome["pid"] is None
                    warning.assert_called_with("  invocation sample: " + text)
    finally:
        canonical._REFUSED_SIGNAL_REASONS["foreign_mode"] = saved

if new == "lionhub.orchestration.flow.checkpoint":
    from pathlib import Path

    legacy, canonical = sys.modules[old], sys.modules[new]
    legacy_writer = legacy.CheckpointWriter(Path("unused-checkpoint"), "session", "prompt", [], {})
    canonical_writer = canonical.CheckpointWriter(Path("unused-checkpoint"), "session", "prompt", [], {})
    version = canonical.CHECKPOINT_VERSION
    with patch(old + ".CHECKPOINT_VERSION", version + 1):
        assert canonical_writer.to_dict()["version"] == version + 1
    with patch(new + ".CHECKPOINT_VERSION", version + 2):
        assert legacy_writer.to_dict()["version"] == version + 2
    legacy_writer.ops["sample"] = {"status": "done"}
    assert legacy_writer.to_dict()["ops"] is legacy_writer.ops
    assert legacy_writer.to_dict.__func__ is canonical.CheckpointWriter.to_dict

assert importlib.reload(first) is first is sys.modules[old] is sys.modules[new]
assert first.__spec__.name == new and first.__name__ == new
assert all(getattr(first, name) is getattr(second, name) for name in names)
assert all(getattr(first, name) is getattr(second, name) for name in value_names)
print("ok")
"""


@pytest.mark.parametrize("old_first", [True, False], ids=["old-first", "new-first"])
@pytest.mark.parametrize(("old", "new"), MOVED_MODULES, ids=_MOVED_MODULE_IDS)
def test_a_moved_module_keeps_its_identity_in_either_cold_import_order(old, new, old_first):
    names = MOVED_MODULE_PUBLIC_NAMES[new] | MOVED_MODULE_PRIVATE_NAMES.get(new, set())
    value_names = MOVED_MODULE_SHARED_VALUES.get(new, set())
    setup = (
        f"old, new, old_first = {old!r}, {new!r}, {old_first!r}\n"
        f"names = {sorted(names)!r}\nvalue_names = {sorted(value_names)!r}\n"
    )
    assert _run_fresh(setup + _MOVED_MODULE_COLD_IDENTITY).strip() == "ok"


@pytest.mark.parametrize("old_first", [True, False], ids=["old-first", "new-first"])
@pytest.mark.parametrize("old", CONSTANT_ONLY_MOVED_MODULES)
def test_a_constant_only_module_keeps_values_and_patches_in_either_cold_order(
    old, old_first, tmp_path
):
    new, values = CONSTANT_ONLY_MOVED_MODULES[old]
    environment = {
        **_isolated_environment(tmp_path),
        "LIONAGI_HOME": str(tmp_path / "lionagi"),
        "TMPDIR": str(tmp_path),
        "LIONAGI_STUDIO_MIRROR_PREVIEW_CHARS": "37",
    }
    program = f"""
import importlib, sys, warnings
from pathlib import Path
from unittest.mock import patch

root = Path({str(ROOT)!r}).resolve()
sys.path[:0] = [str(root), str(root / 'lionhub/src')]
old, new, old_first = {old!r}, {new!r}, {old_first!r}
expected = {values!r}
assert old not in sys.modules and new not in sys.modules
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter('always')
    first = importlib.import_module(old if old_first else new)
    second = importlib.import_module(new if old_first else old)
    again = importlib.import_module(old)

assert first is second is again is sys.modules[old] is sys.modules[new]
assert first.__name__ == first.__spec__.name == new
assert Path(first.__file__).resolve() == root / 'lionhub/src/lionhub/serve/mirror_settings.py'
deprecations = [
    item for item in caught
    if issubclass(item.category, DeprecationWarning)
    and str(item.message).startswith(old + ' moved to ')
]
assert len(deprecations) == 1
assert deprecations[0].filename == '<string>'
assert {{name for name in vars(first) if name.isupper()}} == set(expected)
for name, value in expected.items():
    assert type(getattr(first, name)) is int
    assert getattr(first, name) == value
    sentinel = object()
    with patch(old + '.' + name, sentinel):
        assert getattr(sys.modules[new], name) is sentinel
    assert getattr(first, name) == value
assert importlib.reload(first) is first is sys.modules[old] is sys.modules[new]
assert all(getattr(first, name) == value for name, value in expected.items())
print('ok')
"""
    process = subprocess.run(  # noqa: S603 -- this interpreter and a fixed test program
        [sys.executable, "-I", "-c", program],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert process.returncode == 0, process.stdout[-2000:] + process.stderr[-2000:]
    assert process.stdout.strip() == "ok"


# `python -m <old name>` runs a package's __main__ without importing it, through the
# loader's code (runpy, as `-m` does). MCP client configs start the server this way.
_SCRIPT_ROWS = [
    row
    for row in _moved.MOVED
    if (ROOT / "lionhub" / "src" / Path(*row.new.split(".")) / "__main__.py").is_file()
]


@pytest.mark.parametrize("row", _SCRIPT_ROWS, ids=lambda row: row.old)
def test_an_old_package_with_a_main_module_still_runs_as_a_script(row):
    out = _run_fresh(
        "import importlib, runpy, sys\n"
        f"g = runpy.run_module({row.old!r}, run_name='not_main')\n"
        f"new = importlib.import_module({row.new!r})\n"
        f"print(g['__spec__'].name, g['__package__'], sys.modules[{row.old!r}] is new)\n"
    )
    assert out.split() == [f"{row.old}.__main__", row.old, "True"]


def test_some_moved_package_has_a_main_module():
    assert _SCRIPT_ROWS


# Two old names for one module, imported for the first time on two threads. The loader
# hooks pause each thread at a fixed point so the interleaving is the same on every run:
# the first thread is inside its load when the second starts its own. The second old name
# comes from an extra row for the same package, the shape later nested rows produce.
_TWO_OLD_NAMES_AT_ONCE = """
import importlib, sys, threading
from lionagi import _moved

row = _moved.MOVED[0]
twin = row.old + "_twin"
_moved.MOVED = _moved.MOVED + (_moved.Moved(twin, row.new, row.deprecated_in, row.removed_in),)
old, new = {old!r}, {new!r}
other = twin + old[len(row.old):]
moved = importlib.import_module(new)
importlib.import_module(old.rpartition(".")[0])
importlib.import_module(other.rpartition(".")[0])
assert old not in sys.modules and other not in sys.modules

first_inside, second_started, first_done = threading.Event(), threading.Event(), threading.Event()
create, execute = _moved._AliasLoader.create_module, _moved._AliasLoader.exec_module

def paused_create(loader, spec):
    module = create(loader, spec)
    if spec.name == other:
        second_started.set()
        assert first_done.wait(10)
    return module

def paused_execute(loader, module):
    if threading.current_thread().name == "first":
        first_inside.set()
        assert second_started.wait(10)
        execute(loader, module)
        first_done.set()
    else:
        execute(loader, module)

_moved._AliasLoader.create_module = paused_create
_moved._AliasLoader.exec_module = paused_execute
errors = []

def load(name):
    try:
        importlib.import_module(name)
    except BaseException as exc:
        errors.append(repr(exc))

first = threading.Thread(target=load, args=(old,), name="first")
second = threading.Thread(target=load, args=(other,), name="second")
first.start()
assert first_inside.wait(10)
second.start()
first.join(15)
second.join(15)
assert not first.is_alive() and not second.is_alive()
assert not errors, errors
assert sys.modules[old] is moved and sys.modules[other] is moved
print(moved.__spec__.name, moved.__name__)
"""


def test_two_old_names_imported_at_once_leave_the_module_spec_alone():
    old, new = next((o, n) for o, n in PAIRS if n.count(".") > _moved.MOVED[0].new.count("."))
    out = _run_fresh(_TWO_OLD_NAMES_AT_ONCE.format(old=old, new=new))
    assert out.split() == [new, new]


# An old and a new name of one submodule, imported for the first time on two threads at once,
# forty times over. The old name reaches the new package through its top, and the package's
# __init__ imports the submodule, so without the retry in _moved most rounds fail with
# importlib's deadlock error on the old-name thread. The retry covers that thread only.
# importlib can also fail the thread importing the new name, on the new package's lock: it
# does the same for a package and its submodule imported at once with no old name involved,
# so the new-name thread may see that one error and no other.
_OLD_AND_NEW_AT_ONCE = """
import importlib, json, sys, threading, warnings
import lionagi

warnings.simplefilter("ignore")
old, new = {old!r}, {new!r}
packages = ({old_package!r}, {new_package!r})
errors = dict(old=[], new=[])

def load(barrier, name, side):
    barrier.wait()
    try:
        importlib.import_module(name)
    except BaseException as exc:
        errors[side].append([type(exc).__name__, str(exc)])

for _ in range(40):
    for name in [m for m in sys.modules if m in packages or m.startswith(tuple(p + "." for p in packages))]:
        del sys.modules[name]
    failed = len(errors["old"]) + len(errors["new"])
    barrier = threading.Barrier(2)
    threads = [threading.Thread(target=load, args=(barrier, n, side)) for n, side in ((new, "new"), (old, "old"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert not any(thread.is_alive() for thread in threads)
    if len(errors["old"]) + len(errors["new"]) == failed:
        assert sys.modules[old] is sys.modules[new]
print(json.dumps(errors))
"""


def test_an_old_and_a_new_name_imported_at_once_both_load():
    row = _moved.MOVED[0]
    # The package's __init__ imports this submodule, which is what lets the two imports cross.
    old, new = row.old + ".patterns", row.new + ".patterns"
    assert (old, new) in PAIRS
    out = _run_fresh(
        _OLD_AND_NEW_AT_ONCE.format(old=old, new=new, old_package=row.old, new_package=row.new)
    )
    errors = json.loads(out)
    assert errors["old"] == [], errors
    package_lock = f"_ModuleLock({row.new!r})"
    assert all(kind == "_DeadlockError" and package_lock in text for kind, text in errors["new"]), (
        errors
    )


def test_a_python_without_the_private_deadlock_error_still_reports_import_errors():
    # The retry names a private importlib class. Without it, a failed import of a moved module
    # must still raise its own error rather than one from evaluating the except clause.
    out = _run_fresh(
        "import importlib._bootstrap as b\n"
        "del b._DeadlockError\n"
        "from lionagi import _moved\n"
        "assert _moved._DEADLOCK_ERROR == ()\n"
        "try:\n"
        "    _moved._import_new('lionhub.no_such_module')\n"
        "except ModuleNotFoundError as exc:\n"
        "    print(type(exc).__name__, exc.name)\n"
    )
    assert out.split() == ["ModuleNotFoundError", "lionhub.no_such_module"], out


def test_an_old_package_not_yet_imported_is_described_as_a_package():
    row = _moved.MOVED[0]
    out = _run_fresh(
        "import importlib.util, pkgutil, sys\n"
        "import lionagi\n"
        f"assert {row.old!r} not in sys.modules\n"
        f"spec = importlib.util.find_spec({row.old!r})\n"
        f"new = importlib.util.find_spec({row.new!r})\n"
        "assert list(spec.submodule_search_locations) == list(new.submodule_search_locations)\n"
        f"old_bytes = pkgutil.get_data({row.old!r}, '__init__.py')\n"
        f"assert old_bytes and old_bytes == pkgutil.get_data({row.new!r}, '__init__.py')\n"
        "print('ok')\n"
    )
    assert out.split() == ["ok"]


def _lionhub_modules_after(statement: str) -> list[str]:
    code = (
        f"import sys; {statement}; "
        "print(sorted(m for m in sys.modules if m == 'lionhub' or m.startswith('lionhub.')))"
    )
    proc = subprocess.run(  # noqa: S603 -- this interpreter, a fixed statement
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return ast.literal_eval(proc.stdout.strip().splitlines()[-1])


def test_importing_lionagi_imports_nothing_from_lionhub():
    # Old names resolve on demand, so the core alone loads no lionhub module. The second
    # import shows the same probe does see lionhub modules once an old name is imported.
    assert _lionhub_modules_after("import lionagi") == []
    old = _moved.MOVED[0].old
    assert _moved.resolve(old) in _lionhub_modules_after(f"import lionagi, {old}")


def test_finder_is_installed_once_and_first_on_the_import_path():
    finders = [f for f in sys.meta_path if getattr(f, "lionagi_moved", False)]
    assert len(finders) == 1
    _moved.install()
    assert [f for f in sys.meta_path if getattr(f, "lionagi_moved", False)] == finders
    assert sys.meta_path.index(finders[0]) < next(
        i for i, f in enumerate(sys.meta_path) if f is importlib.machinery.PathFinder
    )


def test_names_outside_the_table_are_left_to_the_normal_import_system():
    first = _moved.MOVED[0]
    assert _moved.resolve(first.old + "x") is None
    assert _moved.resolve("lionagi") is None
    assert _moved.resolve(first.old) == first.new
    assert _moved.resolve(first.old + ".a.b") == first.new + ".a.b"
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(first.old + ".no_such_module")


def test_a_package_moved_inside_another_has_one_module_under_every_name():
    # lionhub.orchestration holds lionhub.orchestration.engines, so the old orchestration
    # prefix also names the engines ("lionagi.orchestration.engines"), a name that never
    # existed. It imports as the same module object: left to the normal import system it
    # would load a second copy of the engines from the aliased parent package's path.
    nested = [(outer, inner) for outer in _moved.MOVED for inner in _inner_rows(outer)]
    assert nested
    for outer, inner in nested:
        name = outer.old + inner.new[len(outer.new) :]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            assert importlib.import_module(name) is importlib.import_module(inner.new)


@pytest.mark.parametrize(("old", "new"), PAIRS, ids=[old for old, _ in PAIRS])
def test_no_old_module_is_still_on_disk(old, new):
    assert old.startswith("lionagi.")
    assert not _moved.still_on_disk(old), f"{old} exists in lionagi/ and in {new}"


def test_finder_steps_aside_for_a_module_still_at_its_old_path(tmp_path, monkeypatch):
    old = _moved.MOVED[0].old
    submodule = next(name for name, _ in PAIRS if name.startswith(old + "."))
    package = tmp_path.joinpath(*old.split(".")[1:])
    package.mkdir(parents=True)
    finder = next(f for f in sys.meta_path if getattr(f, "lionagi_moved", False))
    monkeypatch.setattr(_moved, "_LIONAGI_DIR", str(tmp_path))
    assert finder.find_spec(old) is not None
    (package / "__init__.py").write_text("")
    assert _moved.still_on_disk(old)
    assert finder.find_spec(old) is None
    assert finder.find_spec(submodule) is not None


@pytest.mark.parametrize("row", _moved.MOVED, ids=[row.old for row in _moved.MOVED])
def test_removal_release_is_ahead_of_this_version(row):
    # Fails on the release that reaches ``removed_in``: delete the row and its old
    # callers' last chance is over, as the CHANGELOG said it would be.
    assert _release(row.deprecated_in) < _release(row.removed_in)
    assert _release(__version__) < _release(row.removed_in), (
        f"{row.old} was due for removal in {row.removed_in}; delete its row"
    )


def _absolute(module: str | None, level: int, path: Path) -> str | None:
    if level == 0:
        return module
    root = next((r for r in SOURCE_ROOTS if path.is_relative_to(r)), None)
    if root is None:
        return None
    package = [root.name, *path.relative_to(root).with_suffix("").parts][:-1]
    package = package[: len(package) - (level - 1)]
    return ".".join([*package, module] if module else package)


def _module_of(path: Path) -> str | None:
    root = next((r for r in SOURCE_ROOTS if path.is_relative_to(r)), None)
    if root is None:
        return None
    parts = [root.name, *path.relative_to(root).with_suffix("").parts]
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


_RETAINED_DEPRECATION_MESSAGES = {
    "lionhub.orchestration.runs": {
        "lionagi.cli._runs.teardown_orchestration_persist is deprecated; "
        "use lionagi.cli._runs.teardown_persist instead."
    }
}


def _old_references(path: Path, source: str | None = None) -> list[str]:
    if source is None:
        source = path.read_text(encoding="utf-8")
    found = []
    for node in ast.walk(ast.parse(source, filename=str(path))):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(node.module, node.level, path)
            if base:
                names = [base, *(f"{base}.{alias.name}" for alias in node.names)]
        elif isinstance(node, ast.Call):
            func = node.func
            callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            names = [
                arg.value
                for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
            # A moved module keeps the logger name it had before the move, so logging
            # configuration keyed on that name still applies. Only its own old name or an
            # old package containing it; any other old name is a reference.
            if callee == "getLogger":
                own = _module_of(path)
                names = [
                    name
                    for name in names
                    if not (
                        own
                        and (new := _moved.resolve(name))
                        and (own == new or own.startswith(new + "."))
                    )
                ]
            # The original deprecation text names its old callable without importing it.
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "warnings"
                and func.attr == "warn"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Name)
                and node.args[1].id == "DeprecationWarning"
            ):
                retained = _RETAINED_DEPRECATION_MESSAGES.get(_module_of(path), set())
                names = [
                    arg.value
                    for index, arg in enumerate(node.args)
                    if isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and not (index == 0 and arg.value in retained)
                ]
        found += [
            f"{path.relative_to(ROOT)}:{node.lineno}: {name}"
            for name in names
            if _moved.resolve(name) is not None
        ]
    return found


def test_reference_check_preserves_only_the_original_own_deprecation_text():
    path = SOURCE_ROOTS[1] / "orchestration" / "runs.py"
    other = path.with_name("_probe.py")
    message = (
        "lionagi.cli._runs.teardown_orchestration_persist is deprecated; "
        "use lionagi.cli._runs.teardown_persist instead."
    )
    warning = f"warnings.warn({message!r}, DeprecationWarning)\n"
    changed_message = message + " "
    assert _old_references(path, warning) == []
    assert _old_references(other, warning)
    for source in (
        f"warnings.warn({message!r}, RuntimeWarning)\n",
        f"other.warn({message!r}, DeprecationWarning)\n",
        f"warnings.warn({changed_message!r}, DeprecationWarning)\n",
        f"importlib.import_module({message!r})\n",
        f"warnings.warn('safe', DeprecationWarning, {message!r})\n",
    ):
        assert _old_references(path, source), source
    repeated = f"warnings.warn({message!r}, DeprecationWarning, {message!r})\n"
    assert len(_old_references(path, repeated)) == 1
    imports = "import lionagi.cli._runs\nimportlib.import_module('lionagi.cli._runs')\n"
    assert len(_old_references(path, warning + imports)) == 2


def test_no_source_file_imports_an_old_name():
    files = [p for root in SOURCE_ROOTS for p in root.rglob("*.py")]
    assert any(p.name == "_moved.py" for p in files)
    assert any(p.is_relative_to(SOURCE_ROOTS[1]) for p in files)
    offenders = [hit for p in files if p.name != "_moved.py" for hit in _old_references(p)]
    assert offenders == []


def test_the_reference_check_sees_each_form_of_old_reference():
    old = _moved.MOVED[0].old
    leaf = old.rpartition(".")[2]
    probe = ROOT / "lionagi" / "cli" / "_probe.py"
    source = (
        f"import {old}\n"
        f"from {old} import x\n"
        f"from ..{leaf} import y\n"
        f"importlib.import_module({old!r})\n"
        f"logging.getLogger({old!r})\n"
    )
    hits = _old_references(probe, source)
    assert [int(hit.split(":")[1]) for hit in hits] == [1, 2, 2, 3, 3, 4, 5], hits


def test_a_moved_module_may_keep_only_its_own_old_logger_name():
    row = _moved.MOVED[0]
    inside = ROOT / "lionhub" / "src" / Path(*row.new.split(".")) / "_probe.py"
    source = (
        f"logging.getLogger({row.old!r})\n"
        f"logging.getLogger({row.old + '._probe'!r})\n"
        f"logging.getLogger({row.old + '._other'!r})\n"
    )
    hits = _old_references(inside, source)
    assert [int(hit.split(":")[1]) for hit in hits] == [3], hits


# The core packages under lionagi/ that name a lionhub module: they hold a listed
# interim edge into the hub (see test_provider_interim_edge_inventory) or, for cli and
# studio, import a hub orchestration or serve module. Every other package never names
# lionhub. The set must match what the tree does, and it shrinks as each interim edge is
# removed, so a package that stops naming lionhub is removed from it too.
LIONHUB_USERS_UNDER_LIONAGI = frozenset({"agent", "cli", "operations", "service", "studio"})

_LIONHUB_NAME = re.compile(r"lionhub(\.[A-Za-z_][A-Za-z0-9_]*)*")


def _deprecated_catalog_target_nodes(path: Path, tree: ast.Module) -> set[int]:
    """Exempt only the declared lazy compatibility targets in the core compiler."""
    if path != ROOT / "lionagi" / "_auto.py":
        return set()
    expected = {
        name: f"lionhub.cli.catalog.{name}"
        for name in (
            "build_cli_parser",
            "command_exists",
            "iter_cli_seeds",
            "load_cli_command",
            "seed_for",
        )
    }
    for node in tree.body:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "_DEPRECATED_ALIASES"
            and isinstance(node.value, ast.Dict)
            and len(node.value.keys) == len(expected)
        ):
            continue
        targets = {}
        for key, value in zip(node.value.keys, node.value.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and key.value in expected
                and isinstance(value, ast.Tuple)
                and len(value.elts) == 2
                and isinstance(value.elts[0], ast.Constant)
                and value.elts[0].value == expected[key.value]
            ):
                targets[key.value] = id(value.elts[0])
        if targets.keys() == expected.keys():
            return set(targets.values())
    return set()


def _lionhub_names(path: Path, source: str | None = None) -> list[str]:
    """Every import of lionhub, or string naming a lionhub module, in ``path``."""
    text = path.read_text(encoding="utf-8") if source is None else source
    tree = ast.parse(text, filename=str(path))
    compatibility_targets = _deprecated_catalog_target_nodes(path, tree)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in compatibility_targets:
                continue
            names = [node.value]
        else:
            continue
        hits += [f"{path}:{node.lineno}: {n}" for n in names if _LIONHUB_NAME.fullmatch(n)]
    return hits


def test_only_packages_with_listed_interim_edges_name_lionhub():
    lionagi = SOURCE_ROOTS[0]
    users = {}
    # _moved.py names lionhub as data: the table of new paths it serves old names from.
    for path in lionagi.rglob("*.py"):
        if path.name != "_moved.py" and _lionhub_names(path):
            top = path.relative_to(lionagi).parts[0]
            users.setdefault(top.removesuffix(".py"), []).append(str(path.relative_to(ROOT)))
    assert set(users) == LIONHUB_USERS_UNDER_LIONAGI, users


def test_the_lionhub_check_sees_each_form_of_reference():
    probe = ROOT / "lionagi" / "cli" / "_probe.py"
    source = (
        "import lionhub.orchestration\n"
        "from lionhub.orchestration import patterns\n"
        "importlib.import_module('lionhub.orchestration.patterns')\n"
        "PREFIXES = ('lionhub.', 'lionhubx.orchestration')\n"
    )
    hits = _lionhub_names(probe, source)
    assert [int(hit.split(":")[1]) for hit in hits] == [1, 2, 3], hits


def test_the_lionhub_check_exempts_only_declared_catalog_alias_targets():
    path = ROOT / "lionagi" / "_auto.py"
    names = (
        "build_cli_parser",
        "command_exists",
        "iter_cli_seeds",
        "load_cli_command",
        "seed_for",
    )
    aliases = {name: (f"lionhub.cli.catalog.{name}", "2.0.0") for name in names}
    source = f"_DEPRECATED_ALIASES = {aliases!r}\n"
    assert _lionhub_names(path, source) == []
    additional = "import lionhub.cli.catalog\nEXTRA = 'lionhub.cli.catalog.seed_for'\n"
    assert [hit.rsplit(": ", 1)[1] for hit in _lionhub_names(path, source + additional)] == [
        "lionhub.cli.catalog",
        "lionhub.cli.catalog.seed_for",
    ]
    assert len(_lionhub_names(ROOT / "lionagi" / "_other.py", source)) == len(names)
    aliases["other"] = ("lionhub.cli.catalog.other", "2.0.0")
    assert len(_lionhub_names(path, f"_DEPRECATED_ALIASES = {aliases!r}\n")) == len(aliases)


# The hub's provider modules, as dotted names. A reference is an import, a from-import or a
# string that is exactly one such name: ``lionhub.providersx`` is another package, and a
# prefix such as ``lionhub.providers.`` names no module.
_HUB_PROVIDER = re.compile(r"lionhub\.providers(\.[A-Za-z_][A-Za-z0-9_]*)*")


def _provider_references(path: Path, source: str | None = None) -> list[tuple[str, int, str]]:
    """Every reference in ``path`` to a ``lionhub.providers`` module: ``(module, line, scope)``.

    ``scope`` is ``"function"`` for a reference in the body of a ``def``, ``async def`` or
    ``lambda``, and ``"module"`` for any other: the body of a class, a decorator and a default
    value all run when the module is imported. An import under ``if TYPE_CHECKING:`` is at
    module scope too, because this reads the source and does not run it. A from-import names
    the module it imports from, so ``from lionhub.providers.openai import codex`` refers to
    ``lionhub.providers.openai`` and ``from lionhub import providers`` to ``lionhub.providers``.
    """
    text = path.read_text(encoding="utf-8") if source is None else source
    hits: list[tuple[str, int, str]] = []
    pending: list[tuple[ast.AST, str]] = [(ast.parse(text, filename=str(path)), "module")]
    while pending:
        node, scope = pending.pop()
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(node.module, node.level, path)
            if base and _HUB_PROVIDER.fullmatch(base):
                names = [base]
            elif base:
                names = [f"{base}.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names = [node.value]
        hits += [(name, node.lineno, scope) for name in names if _HUB_PROVIDER.fullmatch(name)]
        body: list = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
        elif isinstance(node, ast.Lambda):
            body = [node.body]
        pending += [
            (child, "function" if child in body else scope) for child in ast.iter_child_nodes(node)
        ]
    return sorted(hits, key=lambda hit: (hit[1], hit[0]))


def _provider_inventory() -> dict[str, list[str]]:
    """The hub provider modules each file under lionagi/ refers to, one name per reference."""
    lionagi = SOURCE_ROOTS[0]
    found = {}
    # _moved.py names lionhub as data: the table of new paths it serves old names from.
    for path in sorted(lionagi.rglob("*.py")):
        if path.name == "_moved.py":
            continue
        modules = sorted(module for module, _, _ in _provider_references(path))
        if modules:
            found[path.relative_to(lionagi).as_posix()] = modules
    return found


# Every reference lionagi still holds to a lionhub provider module, by file (relative to
# lionagi/) and target module, one entry per reference. Each is a listed interim edge: this
# table changes when an edge is removed or a new one is added, never as a side effect.
PROVIDER_INTERIM_EDGES = {
    # Every import here is inside a function (see the test of that below).
    "agent/factory.py": [
        *["lionhub.providers._cli_subprocess"] * 2,
        *["lionhub.providers.openai._codex_profile"] * 8,
        # The forwarded-server check reads the project config layers a codex run would load.
        "lionhub.providers.openai._codex_project_layers",
        *["lionhub.providers.openai.codex"] * 6,
    ],
    "operations/run/run.py": ["lionhub.providers.google.gemini_code"],
    "service/accounts/_router.py": ["lionhub.providers.anthropic._prompt_cache"] * 2,
    "service/providers.py": ["lionhub.providers.openai._codex_profile"],
    # ``_MANIFEST_MODULE`` in the compatibility loader: the hub's manifest module, imported on
    # the first call that needs the default provider catalog.
    "service/connections/_provider_compat.py": ["lionhub.providers.manifest"],
    "cli/agent.py": ["lionhub.providers.google.gemini_code"],
    "cli/orchestrate/_orchestration.py": ["lionhub.providers.openai._codex_profile"],
    "studio/operator/engine.py": [
        "lionhub.providers.anthropic.claude_code",
        "lionhub.providers.google.gemini_code",
    ],
}


def test_provider_interim_edge_inventory():
    expected = {path: sorted(names) for path, names in PROVIDER_INTERIM_EDGES.items()}
    assert _provider_inventory() == expected


def test_the_agent_factory_imports_the_hub_providers_only_inside_functions():
    hits = _provider_references(SOURCE_ROOTS[0] / "agent" / "factory.py")
    assert hits, "agent/factory.py names no hub provider module, so this checks nothing"
    assert [hit for hit in hits if hit[2] == "module"] == []


def test_the_provider_check_sees_each_form_of_reference():
    # A relative import resolves only inside lionagi or lionhub, so the probe sits in the hub.
    probe = ROOT / "lionhub" / "src" / "lionhub" / "orchestration" / "_probe.py"
    source = (
        "import lionhub.providers.openai.chat\n"
        "from lionhub.providers.google.gemini_code import resolve\n"
        "from lionhub import providers\n"
        "from ..providers.anthropic import messages\n"
        "NAMES = ['lionhub.providers.groq.chat']\n"
        "BY_KEY = {'pi': 'lionhub.providers.pi.cli'}\n"
        "def lazy():\n"
        "    import lionhub.providers.exa.search\n"
        "if TYPE_CHECKING:\n"
        "    from lionhub.providers.ollama.chat import Endpoint\n"
        "async def later():\n"
        "    from lionhub.providers.nvidia_nim.chat import Endpoint\n"
        "def with_default(path='lionhub.providers.lattice.chat'):\n"
        "    return path\n"
        "import lionhub.providersx\n"
        "PREFIX = 'lionhub.providers.'\n"
        "import lionhub.orchestration\n"
        "OTHER = 'lionhub.orchestration.patterns'\n"
    )
    assert _provider_references(probe, source) == [
        ("lionhub.providers.openai.chat", 1, "module"),
        ("lionhub.providers.google.gemini_code", 2, "module"),
        ("lionhub.providers", 3, "module"),
        ("lionhub.providers.anthropic", 4, "module"),
        ("lionhub.providers.groq.chat", 5, "module"),
        ("lionhub.providers.pi.cli", 6, "module"),
        ("lionhub.providers.exa.search", 8, "function"),
        ("lionhub.providers.ollama.chat", 10, "module"),
        ("lionhub.providers.nvidia_nim.chat", 12, "function"),
        ("lionhub.providers.lattice.chat", 13, "module"),
    ]


# Every provider key the endpoint registry accepts: a provider name or one of its aliases.
PROVIDER_KEYS = (
    "ag2",
    "anthropic",
    "autogen",
    "claude",
    "claude-code",
    "claude_code",
    "codex",
    "deepseek",
    "exa",
    "firecrawl",
    "gemini",
    "gemini-api",
    "gemini-cli",
    "gemini-code",
    "gemini_cli",
    "gemini_code",
    "groq",
    "lattice",
    "nim",
    "nvidia",
    "nvidia_nim",
    "ollama",
    "open-router",
    "openai",
    "openai-codex",
    "openrouter",
    "perplexity",
    "pi",
    "pi-code",
    "pi_code",
    "scripted",
    "tavily",
)


def _isolated_environment(home: Path) -> dict[str, str]:
    """The environment of a child interpreter whose home and config directories are ``home``."""
    import os

    return {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "CODEX_HOME": str(home / ".codex"),
    }


def test_the_provider_keys_are_the_keys_the_registry_accepts(tmp_path):
    out = _run_fresh(
        "from lionagi.service.connections.registry import EndpointRegistry\n"
        "EndpointRegistry.list_providers()\n"
        "print(sorted(EndpointRegistry._alias_owners))\n",
        env=_isolated_environment(tmp_path),
    )
    assert ast.literal_eval(out.strip().splitlines()[-1]) == sorted(PROVIDER_KEYS)


# A key builds its endpoint from the hub, or is the core scripted provider. The model name
# and key are placeholders: nothing is called, the iModel is only constructed. exa, firecrawl
# and tavily offer several endpoints, so each key is built with the first one registered for
# its provider; without one they build the generic core endpoint.
_PROVIDER_PROBE = (
    "import sys\n"
    "import lionagi\n"
    "loaded = [m for m in sys.modules if m == 'lionhub' or m.startswith('lionhub.')]\n"
    "assert not loaded, loaded\n"
    "from lionagi.service.connections.registry import EndpointRegistry\n"
    "from lionagi.service.imodel import iModel\n"
    "key = {key!r}\n"
    "owner = EndpointRegistry.canonical_provider(key)\n"
    "endpoint = next(\n"
    "    e['endpoint'] for e in EndpointRegistry.list_providers() if e['provider'] == owner\n"
    ")\n"
    "try:\n"
    "    model = iModel(provider=key, model='placeholder-model', api_key='x', endpoint=endpoint)\n"
    "except ModuleNotFoundError as exc:\n"
    "    if (exc.name or '').split('.')[0] in ('lionagi', 'lionhub'):\n"
    "        raise\n"
    "    print('SKIP', exc.name)\n"
    "    raise SystemExit(0)\n"
    "print('MODULE', type(model.endpoint).__module__)\n"
)


@pytest.mark.parametrize("key", PROVIDER_KEYS)
def test_a_provider_key_builds_its_endpoint_in_a_fresh_interpreter(key, tmp_path):
    out = _run_fresh(_PROVIDER_PROBE.format(key=key), env=_isolated_environment(tmp_path))
    probed = out.strip().splitlines()[-1].split()
    if probed[0] == "SKIP":
        reason = f"{key}: optional dependency {probed[1]} is not installed"
        print(reason)
        pytest.skip(reason)
    assert probed[0] == "MODULE", out
    module = probed[1]
    assert module.startswith("lionhub.providers.") or module == "lionagi.testing._endpoint", (
        f"{key} built its endpoint from {module}"
    )


# Wave 2 moves these out of the core (D6), so the rest of lionagi must not import them. Logger
# names such as "lionagi.cli.warn" are strings, not imports, and D5 keeps them.
WAVE_2 = ("cli", "mcp", "studio")
_WAVE_2_NAME = re.compile(rf"lionagi\.({'|'.join(WAVE_2)})(\.[A-Za-z_][A-Za-z0-9_]*)*")
_IMPORT_CALLS = frozenset({"import_module", "__import__"})


def _call_argument(node: ast.Call, position: int, keyword: str) -> ast.expr | None:
    if len(node.args) > position:
        return node.args[position]
    return next((kw.value for kw in node.keywords if kw.arg == keyword), None)


def _literal_import_name(node: ast.Call, call: str, package: tuple[str, ...]) -> str | None:
    """The module a literal ``import_module``/``__import__`` call names, resolved like importlib."""
    name = _call_argument(node, 0, "name")
    if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
        return None
    if not name.value.startswith("."):
        return name.value
    if call != "import_module":
        # ``__import__`` takes ``globals`` second and its level as an argument of its own.
        return None
    # A relative name resolves against the ``package`` argument; ``__package__`` is this module's.
    anchor = _call_argument(node, 1, "package")
    if isinstance(anchor, ast.Constant) and isinstance(anchor.value, str):
        parts = tuple(anchor.value.split("."))
    elif isinstance(anchor, ast.Name) and anchor.id == "__package__":
        parts = package
    else:
        return None
    level = len(name.value) - len(name.value.lstrip("."))
    return ".".join((*parts[: len(parts) - level + 1], *filter(None, [name.value[level:]])))


def _wave_2_imports(path: Path, source: str | None = None) -> list[str]:
    """Every import of a wave-2 package in ``path``: absolute, relative, or a literal dynamic one."""
    text = path.read_text(encoding="utf-8") if source is None else source
    package = path.relative_to(ROOT).with_suffix("").parts[:-1]
    tree = ast.parse(text, filename=str(path))
    # ``from importlib import import_module as load`` makes ``load(...)`` an import call too.
    calls = {name: name for name in _IMPORT_CALLS} | {
        alias.asname: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "importlib"
        for alias in node.names
        if alias.name in _IMPORT_CALLS and alias.asname
    }
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else ()
            module = ".".join((*base, *filter(None, [node.module])))
            names = [module, *(f"{module}.{alias.name}" for alias in node.names)]
        elif (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", None)) in calls
        ):
            call = calls[getattr(node.func, "attr", getattr(node.func, "id", None))]
            names = [n for n in [_literal_import_name(node, call, package)] if n]
        else:
            continue
        hits += [f"{path}:{node.lineno}: {n}" for n in names if _WAVE_2_NAME.fullmatch(n)][:1]
    return hits


def test_the_core_imports_no_package_that_moves_in_wave_2():
    lionagi = SOURCE_ROOTS[0]
    hits = [
        hit
        for path in sorted(lionagi.rglob("*.py"))
        if path.relative_to(lionagi).parts[0] not in WAVE_2
        for hit in _wave_2_imports(path)
    ]
    assert hits == [], hits


def test_the_wave_2_check_sees_each_form_of_import():
    probe = ROOT / "lionagi" / "state" / "_probe.py"
    source = (
        "from lionagi.cli._runs import RunDir\n"
        "from ..mcp import server\n"
        "importlib.import_module('lionagi.studio.app')\n"
        "import lionagi.cli\n"
        "from lionagi import studio\n"
        "from .. import cli\n"
        "importlib.import_module(name='lionagi.mcp')\n"
        "importlib.import_module('.cli._runs', package='lionagi')\n"
        "importlib.import_module('..studio', __package__)\n"
        "from importlib import import_module as load\n"
        "load('lionagi.cli')\n"
        "WARN = 'lionagi.cli.warn'\n"
        "from lionagi.client import x\n"
        "from . import cli\n"
        "importlib.import_module('.client', package='lionagi')\n"
        "__import__('.cli', globals())\n"
        "importlib.import_module(name=module_name)\n"
    )
    hits = _wave_2_imports(probe, source)
    assert sorted(int(hit.split(":")[1]) for hit in hits) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 11], hits


def test_the_frame_walk_skips_the_root_lazy_export_frames_and_nothing_else_in_their_files():
    import lionagi
    from lionagi.ln import _lazy_init

    # A root lazy export reaches a moved module through these two frames.
    assert _moved._is_import_machinery(lionagi.__getattr__.__code__)
    assert _moved._is_import_machinery(_lazy_init.lazy_import.__code__)
    # Other code in the same files is still the caller's code.
    assert not _moved._is_import_machinery(_lazy_init.LazyInit.ensure.__code__)
    assert not _moved._is_import_machinery(
        test_the_frame_walk_skips_the_root_lazy_export_frames_and_nothing_else_in_their_files.__code__
    )


_ROOT_LNDL_EXPORTS = (
    "InvalidConstructorError",
    "LNDLError",
    "LNDLOutput",
    "MissingFieldError",
    "MissingLvarError",
    "TypeMismatchError",
    "extract_lndl_blocks",
    "get_lndl_system_prompt",
    "normalize_lndl_text",
)

# The line that first reads a root LNDL export is line 6 of this program.
_ROOT_LNDL_ACCESS = """\
import importlib, json, sys, warnings
import lionagi
before = sorted(n for n in sys.modules if n == "lionhub" or n.startswith("lionhub."))
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    first = lionagi.LNDLOutput
    rest = {name: getattr(lionagi, name) for name in sys.argv[1:]}
hub = importlib.import_module("lionhub.lndl")
print(json.dumps({
    "before": before,
    "same": {name: rest[name] is getattr(hub, name) for name in rest},
    "first_same": first is hub.LNDLOutput,
    "warnings": [
        [w.category.__name__, w.filename, w.lineno, str(w.message)] for w in caught
    ],
}))
"""


def test_a_root_lndl_export_is_the_moved_object_and_warns_at_the_callers_line():
    proc = subprocess.run(  # noqa: S603 -- this interpreter, a fixed program
        [sys.executable, "-c", _ROOT_LNDL_ACCESS, *_ROOT_LNDL_EXPORTS],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    result = json.loads(proc.stdout)
    assert result["before"] == []
    assert result["first_same"] is True
    assert result["same"] == dict.fromkeys(_ROOT_LNDL_EXPORTS, True)
    moved = [w for w in result["warnings"] if "lionhub.lndl" in w[3]]
    assert [(w[0], w[1], w[2]) for w in moved] == [("DeprecationWarning", "<string>", 6)], result


# Every lionhub.lndl module a file under lionagi/ names, by file (relative to lionagi/). The CLI's
# API leg builds the LNDL Middle lazily until the CLI move; no other file names the language.
LNDL_INTERIM_EDGES = {"cli/agent.py": ["lionhub.lndl.middle"]}

_HUB_LNDL = re.compile(r"lionhub\.lndl(\.[A-Za-z_][A-Za-z0-9_]*)*")


def test_lndl_interim_edge_inventory():
    lionagi = SOURCE_ROOTS[0]
    found = {}
    # _moved.py names lionhub as data: the table of new paths it serves old names from.
    for path in sorted(lionagi.rglob("*.py")):
        if path.name == "_moved.py":
            continue
        names = sorted(
            name
            for name in (hit.rsplit(": ", 1)[1] for hit in _lionhub_names(path))
            if _HUB_LNDL.fullmatch(name)
        )
        if names:
            found[path.relative_to(lionagi).as_posix()] = names
    assert found == LNDL_INTERIM_EDGES
