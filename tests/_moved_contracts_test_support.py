"""One hand-declared contract for each module that moved out of ``lionagi``.

``lionagi._moved.MOVED`` says where an old import path goes. A ``MoveContract`` says what a
caller of that old path must still find: which names, which shared values, which state
read through either name, and in which import orders the module is exercised. The tests in
``tests/test_moved_modules.py`` read these contracts and nothing else for those questions.

``CONTRACTS`` is written out by hand and is never computed from ``MOVED``. A row added to
``MOVED`` without a contract, or a contract without a row, fails the comparison in
``assert_contracts_match_moved``, so the two cannot drift apart without a test saying so.

A contract states a lower bound for a module with callable definitions: every declared
name must be one the new module defines itself, but the module may define more, and the
tests still check every public callable it does define. A ``value-only`` module exports
constants and no pickleable definitions, so its contract states them exactly.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Literal

Kind = Literal["package", "module", "value-only"]
ColdOrder = Literal["old-first", "new-first"]

KINDS: tuple[Kind, ...] = ("package", "module", "value-only")
COLD_ORDERS: tuple[ColdOrder, ...] = ("old-first", "new-first")


@dataclass(frozen=True)
class StateConsumer:
    """Statements that read or write state a moved module shares between its two names.

    ``source`` runs at the top level of the cold import program, after both names are
    imported, so these names are bound: ``old``, ``new``, ``first`` and ``second`` (the module
    reached by the first and by the second import), ``sentinel``, ``patch``, ``importlib``
    and ``sys``.
    """

    name: str
    source: str


@dataclass(frozen=True)
class MoveContract:
    """What one moved row promises to the callers of its old import path.

    ``old`` and ``new`` are the row's dotted names. ``kind`` is ``package`` when the new
    name is a package (its submodules are walked as well), ``module`` when it is one file,
    and ``value-only`` when the module exports constants and no definitions to pickle.

    ``public_names`` and ``private_names`` name callables the new module defines itself.
    ``shared_values`` name module-level state that must be one binding under both names:
    the same object, and a patch through either name seen through the other.
    ``state_consumers`` are further checks on that state. ``cold_orders`` are the import
    orders the module is exercised in, each in a fresh process; a package row, which is
    covered by the walk of its submodules, runs none.

    A ``value-only`` contract carries ``expected_values``: each constant with the value and
    exact type it must have. ``environment`` holds the variables the module reads those
    values from when it is imported. Neither applies to any other kind.
    """

    old: str
    new: str
    kind: Kind
    public_names: frozenset[str] = frozenset()
    private_names: frozenset[str] = frozenset()
    shared_values: frozenset[str] = frozenset()
    state_consumers: tuple[StateConsumer, ...] = ()
    cold_orders: tuple[ColdOrder, ...] = COLD_ORDERS
    expected_values: tuple[tuple[str, object], ...] = ()
    environment: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"{self.old}: kind {self.kind!r} is not one of {KINDS}")
        unknown = [order for order in self.cold_orders if order not in COLD_ORDERS]
        if unknown or len(set(self.cold_orders)) != len(self.cold_orders):
            raise ValueError(f"{self.old}: cold orders {self.cold_orders!r} are not distinct")
        if self.kind == "value-only":
            if not self.expected_values or self.public_names or self.private_names:
                raise ValueError(f"{self.old}: a value-only module declares values, not names")
        elif self.expected_values or self.environment:
            raise ValueError(f"{self.old}: only a value-only module carries expected values")


# State a moved module shares between its two names, read and written through both.
# Each runs at the top level of the cold import program (see StateConsumer).
_JOB_ALLOCATION_STATE = """
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

    consumer = importlib.import_module("lionhub.serve.cli.main")
    assert consumer.run_was_allocated is canonical.run_was_allocated
    with patch(old + "._run_allocated", True):
        assert consumer.run_was_allocated()
    with patch(old + "._run_allocated", False):
        assert not consumer.run_was_allocated()
finally:
    canonical.clear_run_allocation()
"""

_SPAWN_PROFILE_TOOL_FIELDS = """
from types import SimpleNamespace

profile = SimpleNamespace(
    name="example", extra={"tools": ["Read"], "allowed_tools": ["Read"]}
)
with patch(old + "._PROFILE_TOOL_FIELDS", ("tools",)):
    assert second._profile_tool_settings(profile) == {"tools": "Read"}
with patch(new + "._PROFILE_TOOL_FIELDS", ("allowed_tools",)):
    assert first._profile_tool_settings(profile) == {"allowed_tools": ["Read"]}
"""

_MIRROR_TOOL_NAME_RETENTION = """
entries = {"earlier": "read", "middle": "write", "latest": "search"}
with patch(old + "._LEGACY_TOOL_NAMES_KEEP", 1):
    assert second._recent_tool_names(entries) == {"latest": "search"}
with patch(new + "._LEGACY_TOOL_NAMES_KEEP", 2):
    assert first._recent_tool_names(entries) == {"middle": "write", "latest": "search"}
assert first._log is second._log
assert first._log.name == "lionagi.cli.mirror"
"""

_SCHEDULE_API_SUFFIX_WARNING = """
import os

with patch.dict(os.environ, {"LIONAGI_STUDIO_URL": "http://localhost:8765/api"}):
    with patch(new + "._warned_api_suffix", False), patch(old + ".warn") as warning:
        assert first._base_url() == "http://localhost:8765"
        assert second._warned_api_suffix is True
        assert second._base_url() == "http://localhost:8765"
        warning.assert_called_once()
"""

_RUNS_ALLOCATED_RUN_ID = """
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
"""

_KILL_REFUSED_SIGNAL_REASONS = """
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
"""

_CHECKPOINT_VERSION = """
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
"""


# One contract for each row of the move table, in the table's order. They are written out
# here and never derived from it: assert_contracts_match_moved compares the two.
CONTRACTS: tuple[MoveContract, ...] = (
    MoveContract(old="lionagi.tools", new="lionhub.tools", kind="package"),
    MoveContract(old="lionagi.agent", new="lionhub.agent", kind="package"),
    MoveContract(old="lionagi.casts", new="lionhub.agent.casts", kind="package"),
    MoveContract(
        old="lionagi.orchestration",
        new="lionhub.orchestration",
        kind="package",
        cold_orders=(),
    ),
    MoveContract(
        old="lionagi.engines",
        new="lionhub.orchestration.engines",
        kind="package",
        cold_orders=(),
    ),
    MoveContract(
        old="lionagi.dispatch",
        new="lionhub.orchestration.dispatch",
        kind="package",
        cold_orders=(),
    ),
    MoveContract(
        old="lionagi.work",
        new="lionhub.orchestration.work",
        kind="package",
        cold_orders=(),
    ),
    MoveContract(
        old="lionagi.mcp",
        new="lionhub.serve.mcp",
        kind="package",
        cold_orders=(),
    ),
    MoveContract(
        old="lionagi.providers",
        new="lionhub.providers",
        kind="package",
        cold_orders=(),
    ),
    MoveContract(
        old="lionagi.lndl",
        new="lionhub.lndl",
        kind="package",
        cold_orders=(),
    ),
    MoveContract(
        old="lionagi.operations.lndl_middle",
        new="lionhub.lndl.middle",
        kind="package",
        cold_orders=(),
    ),
    MoveContract(
        old="lionagi.providers._provider_errors",
        new="lionagi._provider_errors",
        kind="module",
        public_names=frozenset(
            {
                "EmissionError",
                "ProviderError",
            }
        ),
    ),
    MoveContract(
        old="lionagi.providers._secret_resolution",
        new="lionagi.service.accounts._secret_resolution",
        kind="module",
        public_names=frozenset(
            {
                "SecretLookupResolution",
                "fill_declared_secrets",
                "lookup_one_name",
                "resolve_secret_lookup_config",
            }
        ),
    ),
    MoveContract(
        old="lionagi.providers._subagent_admission",
        new="lionagi.service._subagent_admission",
        kind="module",
        public_names=frozenset(
            {
                "SubagentLease",
                "admit_subagent",
                "child_agent_depth",
                "refuse_nested_agent",
            }
        ),
    ),
    MoveContract(
        old="lionagi.providers.openai._chat_schemas",
        new="lionagi.service.connections._chat_schemas",
        kind="module",
        public_names=frozenset(
            {
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
            }
        ),
    ),
    MoveContract(
        old="lionagi.cli",
        new="lionhub.serve.cli",
        kind="package",
        cold_orders=(),
    ),
    MoveContract(
        old="lionagi.cli._argtypes",
        new="lionhub.orchestration.arguments",
        kind="module",
        public_names=frozenset(
            {
                "BoundedInt",
                "JsonArgument",
            }
        ),
    ),
    MoveContract(
        old="lionagi.cli._code_identity",
        new="lionhub.serve.diagnostics",
        kind="module",
        public_names=frozenset(
            {
                "code_identity",
                "git_identity",
                "loaded_package_path",
                "snapshot_git_position",
            }
        ),
    ),
    MoveContract(
        old="lionagi.cli._logging",
        new="lionhub.orchestration.console",
        kind="module",
        public_names=frozenset(
            {
                "configure_cli_logging",
            }
        ),
        private_names=frozenset(
            {
                "_ErrorFormatter",
                "_LazyStderrHandler",
            }
        ),
    ),
    MoveContract(
        old="lionagi.cli._mirror_settings",
        new="lionhub.serve.mirror_settings",
        kind="value-only",
        expected_values=(("MIRROR_PREVIEW_CHARS", 37),),
        environment=(("LIONAGI_STUDIO_MIRROR_PREVIEW_CHARS", "37"),),
    ),
    MoveContract(
        old="lionagi.cli._project",
        new="lionhub.orchestration.project",
        kind="module",
        public_names=frozenset(
            {
                "detect_project",
            }
        ),
        private_names=frozenset(
            {
                "_from_config_toml",
                "_from_global_overrides",
                "_git_remote_slug",
                "_parse_remote_url",
                "_read_project_from_toml",
            }
        ),
        shared_values=frozenset(
            {
                "_SOURCE_CONFIG_TOML",
                "_SOURCE_GIT_REMOTE",
                "_SOURCE_GLOBAL_OVERRIDE",
            }
        ),
    ),
    MoveContract(
        old="lionagi.cli._mcp_resolve",
        new="lionhub.orchestration.mcp_config",
        kind="module",
        public_names=frozenset(
            {
                "LegacyMcpReplayError",
                "McpConfigError",
                "McpResolution",
                "discover_mcp_config",
                "explicit_mcp_config_path",
                "resolve_spawn_mcp_servers",
            }
        ),
        private_names=frozenset(
            {
                "_format_recorded_mcp_path",
                "_read_config",
                "_read_servers",
                "_reject_json_constant",
                "_snapshot_path",
            }
        ),
        shared_values=frozenset(
            {
                "MCP_CONFIG_FILENAME",
                "SNAPSHOT_SEARCHED_FROM_KEY",
                "SNAPSHOT_SOURCE_KEY",
            }
        ),
    ),
    MoveContract(
        old="lionagi.cli._team_service",
        new="lionhub.orchestration.team",
        kind="module",
        public_names=frozenset(
            {
                "read_team_json",
            }
        ),
    ),
    MoveContract(
        old="lionagi.cli._engine_definitions",
        new="lionhub.orchestration.engine_definitions",
        kind="module",
        public_names=frozenset(
            {
                "import_engine_class",
            }
        ),
        private_names=frozenset(
            {
                "_is_registered_engine_kind",
            }
        ),
        shared_values=frozenset(
            {
                "KIND_META",
            }
        ),
    ),
    MoveContract(
        old="lionagi.cli._util",
        new="lionhub.orchestration.job",
        kind="module",
        public_names=frozenset(
            {
                "AmbiguousIdError",
                "begin_invocation",
                "classify_exception",
                "clear_run_allocation",
                "end_invocation",
                "fetch_unique_row",
                "mark_run_allocated",
                "pid_alive",
                "recorded_identity_mode",
                "recorded_pid_is_foreign",
                "resolve_entity",
                "run_was_allocated",
                "validate_cwd_exists",
            }
        ),
        private_names=frozenset(
            {
                "_fetch_exact_row",
                "_fetch_prefix_rows",
                "_like_prefix_pattern",
            }
        ),
        shared_values=frozenset(
            {
                "BOOT_TIME_TOLERANCE",
                "EXIT_CODE_BY_STATUS",
                "EXIT_CODE_ENVIRONMENT_ERROR",
                "EXIT_CODE_SUBAGENT_REFUSED",
                "UNRECOGNIZED_IDENTITY_MODE",
                "_CANDIDATES_SHOWN",
                "_SEARCH_ORDER",
                "_TABLE_TO_ENTITY_TYPE",
                "_allocation_lock",
                "_invocations_in_flight",
                "_run_allocated",
            }
        ),
        state_consumers=(StateConsumer("allocation-state", _JOB_ALLOCATION_STATE),),
    ),
    MoveContract(
        old="lionagi.cli._inspection",
        new="lionhub.orchestration.inspection",
        kind="module",
        public_names=frozenset(
            {
                "fetch_by_id",
                "resolve_agent_target",
                "resolve_any_target",
                "resolve_play_target",
                "resolve_primary_session",
                "run_id_candidates",
            }
        ),
        private_names=frozenset(
            {
                "_latest_session",
                "_resolve_session_by_branch_id",
                "_resolve_session_by_run_id",
            }
        ),
        shared_values=frozenset(
            {
                "EXIT_RUNNING",
                "EXIT_UNKNOWN",
                "RUN_ID_THROUGH_T",
                "_DB_BUSY_TIMEOUT_S",
                "_PLAY_FAILURE",
                "_PLAY_SUCCESS",
                "_SESSION_FAILURE",
                "_SESSION_SUCCESS",
            }
        ),
    ),
    MoveContract(
        old="lionagi.cli._providers",
        new="lionhub.orchestration.spawn",
        kind="module",
        public_names=frozenset(
            {
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
            }
        ),
        private_names=frozenset(
            {
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
            }
        ),
        shared_values=frozenset(
            {
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
            }
        ),
        state_consumers=(StateConsumer("profile-tool-fields", _SPAWN_PROFILE_TOOL_FIELDS),),
    ),
    MoveContract(
        old="lionagi.cli._orchestrate_parsing",
        new="lionhub.orchestration.flow.parsing",
        kind="module",
        public_names=frozenset(
            {
                "add_orchestrate_subparser",
            }
        ),
        private_names=frozenset(
            {
                "_add_mcp_config_args",
            }
        ),
        shared_values=frozenset(
            {
                "add_common_cli_args",
            }
        ),
    ),
    MoveContract(
        old="lionagi.cli._schedule_parsing",
        new="lionhub.orchestration.schedule.parsing",
        kind="module",
        public_names=frozenset(
            {
                "add_schedule_subparser",
                "build_create_body",
                "normalize_host",
                "suggest_schedule_flag",
            }
        ),
        private_names=frozenset(
            {
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
            }
        ),
        shared_values=frozenset(
            {
                "_ALIAS_ACTION_KINDS",
                "_ALL_SCHEDULE_FLAGS",
                "_CHAIN_ACTION_ALLOWED_KEYS",
                "_IDENT_RE",
                "_MAX_CHAIN_DEPTH",
                "_MODEL_RE",
                "_SCHEDULE_FLAG_SYNONYMS",
                "_VALID_ACTION_KINDS",
                "_warned_api_suffix",
            }
        ),
        state_consumers=(StateConsumer("api-suffix-warning", _SCHEDULE_API_SUFFIX_WARNING),),
    ),
    MoveContract(
        old="lionagi.cli._mirror_service",
        new="lionhub.serve.mirror",
        kind="module",
        public_names=frozenset(
            {
                "mirror_forever",
            }
        ),
        private_names=frozenset(
            {
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
            }
        ),
        shared_values=frozenset(
            {
                "CLAUDE_PROJECTS_DIR",
                "CODEX_SESSIONS_DIR",
                "_COMMAND_NOISE",
                "_DEFAULT_LIVE_WINDOW",
                "_LEGACY_TOOL_NAMES_KEEP",
                "_OFFSETS_PATH",
                "_log",
            }
        ),
        state_consumers=(StateConsumer("tool-name-retention", _MIRROR_TOOL_NAME_RETENTION),),
    ),
    MoveContract(
        old="lionagi.cli._runs",
        new="lionhub.orchestration.runs",
        kind="module",
        public_names=frozenset(
            {
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
            }
        ),
        private_names=frozenset(
            {
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
            }
        ),
        shared_values=frozenset(
            {
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
            }
        ),
        state_consumers=(StateConsumer("allocated-run-id", _RUNS_ALLOCATED_RUN_ID),),
    ),
    MoveContract(
        old="lionagi.cli._kill_service",
        new="lionhub.orchestration.kill",
        kind="module",
        public_names=frozenset(
            {
                "check_pid_identity",
                "imported_degraded_process_is_live",
                "kill_one",
                "list_running_children",
                "read_pid_from_entity",
                "unaddressable_pid_reason",
            }
        ),
        private_names=frozenset(
            {
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
            }
        ),
        shared_values=frozenset(
            {
                "BOOT_TIME_TOLERANCE",
                "NOT_JUDGEABLE_HERE",
                "NOT_STOPPED_SIGNALS",
                "_BOOT_TIME_TOLERANCE",
                "_IdentityVerdict",
                "_LOCALLY_SIGNALLABLE_MODES",
                "_REFUSED_SIGNAL_REASONS",
                "_STALE_SWEEP_ORDER",
            }
        ),
        state_consumers=(StateConsumer("refused-signal-reasons", _KILL_REFUSED_SIGNAL_REASONS),),
    ),
    MoveContract(
        old="lionagi.cli.orchestrate._checkpoint",
        new="lionhub.orchestration.flow.checkpoint",
        kind="module",
        public_names=frozenset(
            {
                "CheckpointWriter",
                "FlowResumeError",
                "UnsupportedFlowCheckpointVersionError",
                "load_checkpoint",
                "resolve_checkpoint_target",
            }
        ),
        private_names=frozenset(
            {
                "_find_run_dir_by_id",
                "_raise_if_persistence_degraded",
            }
        ),
        shared_values=frozenset(
            {
                "CHECKPOINT_VERSION",
                "PERSISTENCE_DEGRADED_REASON_FIELD",
                "RUNS_ROOT",
                "RunDir",
            }
        ),
        state_consumers=(StateConsumer("checkpoint-version", _CHECKPOINT_VERSION),),
    ),
    MoveContract(
        old="lionagi.studio.services._schedule_validation",
        new="lionhub.orchestration.schedule.validation",
        kind="module",
        public_names=frozenset({"validate_rate_limit"}),
    ),
    MoveContract(
        old="lionagi.studio.services.schedule_action_validation",
        new="lionhub.orchestration.schedule.action_validation",
        kind="module",
        public_names=frozenset({"validate_launch_fields"}),
    ),
    MoveContract(
        old="lionagi.studio.services.schedule_timing",
        new="lionhub.orchestration.schedule.timing",
        kind="module",
        public_names=frozenset({"resolve_schedule_timezone"}),
    ),
    MoveContract(
        old="lionagi.studio._timezone_rules",
        new="lionhub.orchestration.schedule._timezone_rules",
        kind="module",
        public_names=frozenset({"rules_equal"}),
    ),
    MoveContract(
        old="lionagi.studio.services.schedule_execution_root",
        new="lionhub.orchestration.schedule.execution_root",
        kind="module",
        public_names=frozenset({"SchedulerCwdInheritRefusedError"}),
    ),
    MoveContract(
        old="lionagi.studio.services.schedule_declaration",
        new="lionhub.orchestration.schedule.declaration",
        kind="module",
        public_names=frozenset({"ScheduleSetError"}),
    ),
    MoveContract(
        old="lionagi.studio.services.schedule_export",
        new="lionhub.orchestration.schedule.export",
        kind="module",
        public_names=frozenset({"ExportReportLine"}),
    ),
    MoveContract(
        old="lionagi.studio.services.project_lookup",
        new="lionhub.orchestration.project_store",
        kind="module",
        public_names=frozenset({"get_project"}),
    ),
    MoveContract(
        old="lionagi.studio.scheduler",
        new="lionhub.orchestration.scheduler",
        kind="package",
    ),
    MoveContract(
        old="lionagi.studio.services.scheduler_state",
        new="lionhub.orchestration.scheduler.scheduler_state",
        kind="module",
        public_names=frozenset({"SchedulerStateService"}),
        shared_values=frozenset({"default_scheduler_state"}),
    ),
    MoveContract(
        old="lionagi.studio.services.db_maintenance",
        new="lionhub.orchestration.scheduler.db_maintenance",
        kind="module",
        public_names=frozenset({"checkpoint_state_db", "prune_old_data"}),
    ),
    MoveContract(
        old="lionagi.studio.services.retention_archive",
        new="lionhub.orchestration.scheduler.retention_archive",
        kind="module",
        public_names=frozenset({"ArchiveWriteError", "write_archive_chunk"}),
    ),
    MoveContract(
        old="lionagi.studio.services.lifecycle",
        new="lionhub.orchestration.scheduler.lifecycle",
        kind="module",
        public_names=frozenset({"run_periodic_reapers", "reap_stale_plays"}),
    ),
    MoveContract(
        old="lionagi.studio.services.terminal_reconciliation",
        new="lionhub.orchestration.scheduler.terminal_reconciliation",
        kind="module",
        public_names=frozenset({"run_terminal_reconciliation"}),
    ),
    MoveContract(
        old="lionagi.hooks.external",
        new="lionhub.hooks.external",
        kind="module",
        public_names=frozenset(
            {
                "ExternalHookConfigError",
                "HookVerdict",
                "build_envelope",
                "compute_command_hash",
                "compute_executable_digest",
                "compute_trust_record",
                "external_hook_adapter",
                "is_command_trusted",
                "match_hook",
                "resolve_hook_executable",
                "validate_argv",
            }
        ),
    ),
    MoveContract(
        old="lionagi.studio.services._db",
        new="lionhub.orchestration.state_store",
        kind="module",
        public_names=frozenset({"StoreNotAddressableError"}),
    ),
)


def contract_for(name: str, contracts: Iterable[MoveContract]) -> MoveContract | None:
    """The contract with the longest ``old`` that is ``name`` or a package containing it."""
    best: MoveContract | None = None
    for contract in contracts:
        if name == contract.old or name.startswith(contract.old + "."):
            if best is None or len(contract.old) > len(best.old):
                best = contract
    return best


def row_set_gaps(contracts: Iterable[MoveContract], moved: Iterable) -> tuple[frozenset, frozenset]:
    """``(old, new)`` pairs of ``moved`` with no contract, then contracts with no ``moved`` row."""
    declared = {(contract.old, contract.new) for contract in contracts}
    actual = {(row.old, row.new) for row in moved}
    return frozenset(actual - declared), frozenset(declared - actual)


def assert_contracts_match_moved(contracts: Iterable[MoveContract], moved: Iterable) -> None:
    """Fail unless ``contracts`` and ``moved`` hold the same ``(old, new)`` rows, each once."""
    contracts = list(contracts)
    undeclared, stale = row_set_gaps(contracts, moved)
    assert not undeclared, f"rows of MOVED with no contract: {sorted(undeclared)}"
    assert not stale, f"contracts with no row in MOVED: {sorted(stale)}"
    pairs = [(contract.old, contract.new) for contract in contracts]
    assert len(pairs) == len(set(pairs)), "a row has more than one contract"


def own_public_callables(module: ModuleType, new: str) -> list[str]:
    """Public callables ``module`` defines itself, as opposed to ones it imported."""
    return [
        name
        for name, obj in vars(module).items()
        if not name.startswith("_") and callable(obj) and getattr(obj, "__module__", None) == new
    ]


def public_name_gaps(contract: MoveContract, module: ModuleType) -> list[str]:
    """Declared public names that ``module`` does not define itself."""
    return sorted(contract.public_names - set(own_public_callables(module, contract.new)))


def canonical_source(new: str, roots: Sequence[Path]) -> Path:
    """The source file of module ``new`` under the first of ``roots`` that holds it."""
    parts = new.split(".")
    for root in roots:
        for candidate in (
            root.joinpath(*parts[:-1], parts[-1] + ".py"),
            root.joinpath(*parts, "__init__.py"),
        ):
            if candidate.is_file():
                return candidate
    raise FileNotFoundError(f"no source file for {new} under {[str(root) for root in roots]}")


# The cold import program. The caller puts these names in front of it: ``old``, ``new``,
# ``old_first``, ``names`` (sorted public and private names), ``value_names`` (sorted shared
# values and expected constants), ``expected`` (constant -> value), ``search_path``, ``origin``
# (the source file ``new`` must come from, or None) and ``register_row`` (whether to add the
# row to ``MOVED`` in the child, for a contract that is not one of the real rows).
_COLD_HEAD = """
import importlib, pickle, sys, warnings
from pathlib import Path
from unittest.mock import patch

sys.path[:0] = search_path
if register_row:
    from lionagi import _moved

    _moved.MOVED = _moved.MOVED + (_moved.Moved(old, new, "0.38.0", "2.0.0"),)


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

if origin:
    assert Path(first.__file__).resolve() == Path(origin).resolve(), ("origin", first.__file__)
if expected:
    constants = {name for name in vars(first) if name.isupper()}
    assert constants == set(expected), ("constants", sorted(constants), sorted(expected))

sentinel = object()
if names:
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

    with patch(old + "." + names[0], sentinel):
        assert getattr(sys.modules[new], names[0]) is sentinel

for name in value_names:
    value = getattr(first, name)
    assert value is getattr(second, name)
    if name in expected:
        assert type(value) is type(expected[name]), ("type", name, type(value))
        assert value == expected[name], ("value", name, value)
    with patch(old + "." + name, sentinel):
        assert getattr(sys.modules[new], name) is sentinel
    assert getattr(first, name) is value
    with patch(new + "." + name, sentinel):
        assert getattr(sys.modules[old], name) is sentinel
    assert getattr(second, name) is value

"""

_COLD_TAIL = """
assert importlib.reload(first) is first is sys.modules[old] is sys.modules[new]
assert first.__spec__.name == new and first.__name__ == new
assert all(getattr(first, name) is getattr(second, name) for name in names)
assert all(getattr(first, name) is getattr(second, name) for name in value_names)
assert all(getattr(first, name) == value for name, value in expected.items())
print("ok")
"""


def cold_program(
    contract: MoveContract,
    order: ColdOrder,
    *,
    search_path: Sequence[str] = (),
    origin: str | None = None,
    register_row: bool = False,
) -> str:
    """The program a fresh interpreter runs to check ``contract`` in the given import order."""
    names = sorted(contract.public_names | contract.private_names)
    value_names = sorted(contract.shared_values | {name for name, _ in contract.expected_values})
    header = (
        f"old, new, old_first = {contract.old!r}, {contract.new!r}, {order == 'old-first'!r}\n"
        f"names = {names!r}\nvalue_names = {value_names!r}\n"
        f"expected = {dict(contract.expected_values)!r}\n"
        f"search_path = {list(search_path)!r}\norigin = {origin!r}\n"
        f"register_row = {register_row!r}\n"
    )
    consumers = "\n".join(consumer.source for consumer in contract.state_consumers)
    return header + _COLD_HEAD + consumers + "\n" + _COLD_TAIL


def run_cold_case(
    contract: MoveContract,
    order: ColdOrder,
    *,
    root: Path,
    workdir: Path | None = None,
    base_environment: dict[str, str] | None = None,
    extra_roots: Sequence[Path] = (),
    origin: Path | None = None,
    register_row: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run ``contract`` in its own fresh interpreter, imported in the given order.

    A module runs from ``root`` with the environment it inherits. A ``value-only`` module runs
    isolated (``-I``, in ``workdir``, with ``base_environment`` and the contract's own
    variables) with ``root``, ``root/lionhub/src`` and ``extra_roots`` in front of
    ``sys.path``, and must come from the source file the first of those roots holds, or from
    ``origin``. ``register_row`` adds the row to ``MOVED`` in the child, for a contract that
    names a module outside the real table.
    """
    if order not in contract.cold_orders:
        raise ValueError(f"{contract.old} does not run the {order} order")
    if not (contract.public_names or contract.expected_values):
        raise ValueError(f"{contract.old} declares nothing for a cold run to check")
    if contract.kind != "value-only":
        program = cold_program(contract, order, register_row=register_row)
        return subprocess.run(  # noqa: S603 -- this interpreter, a fixed program
            [sys.executable, "-c", program],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
        )
    if workdir is None:
        raise ValueError(f"{contract.old} runs isolated and needs a working directory")
    roots = (root, root / "lionhub" / "src", *extra_roots)
    program = cold_program(
        contract,
        order,
        search_path=[str(path) for path in roots],
        origin=str(origin if origin is not None else canonical_source(contract.new, roots)),
        register_row=register_row,
    )
    return subprocess.run(  # noqa: S603 -- this interpreter and a fixed program
        [sys.executable, "-I", "-c", program],
        cwd=workdir,
        env={**(base_environment or {}), **dict(contract.environment)},
        capture_output=True,
        text=True,
        timeout=60,
    )


def assert_cold_ok(result: subprocess.CompletedProcess[str]) -> None:
    """Fail with the child's output unless it exited cleanly and printed ``ok``."""
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert result.stdout.strip() == "ok"


_README_HEADER = re.compile(r"\|\s*Old import\s*\|\s*Current import\s*\|")
_README_NAME = re.compile(r"`([^`]+)`")


def readme_rows(text: str) -> list[tuple[str, str]]:
    """Every ``(old, new)`` row of the "Old import | Current import" tables in ``text``."""
    rows = []
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()
        if _README_HEADER.fullmatch(stripped):
            in_table = True
            continue
        if not in_table:
            continue
        if not stripped.startswith("|"):
            in_table = False
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(cell and set(cell) <= set("-: ") for cell in cells):
            continue  # the rule under the header
        names = [_README_NAME.fullmatch(cell) for cell in cells]
        if len(cells) != 2 or not all(names):
            raise ValueError(f"not a row of two backticked import names: {line!r}")
        rows.append((names[0].group(1), names[1].group(1)))
    return rows


def readme_gaps(text: str, contracts: Iterable[MoveContract]) -> list[str]:
    """Rows of the README tables in ``text`` that disagree with ``contracts``."""
    new_by_old = {contract.old: contract.new for contract in contracts}
    gaps = []
    seen: set[str] = set()
    for old, new in readme_rows(text):
        if old in seen:
            gaps.append(f"the README lists {old} more than once")
        seen.add(old)
        if old not in new_by_old:
            gaps.append(f"the README lists {old} -> {new}; no contract has the old name {old}")
        elif new_by_old[old] != new:
            gaps.append(f"the README lists {old} -> {new}; the contract says {new_by_old[old]}")
    return gaps
