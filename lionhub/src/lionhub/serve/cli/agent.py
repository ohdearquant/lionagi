# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li agent` — one-shot or resumed single-agent conversation."""

from __future__ import annotations

import argparse
import dataclasses
import functools
import json
import math
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, field_validator

from lionagi import Branch
from lionagi._auto import CliDeclaration, auto_register
from lionagi._errors import (
    ConfigurationError,
    FailedRoundError,
    InputTokenCapError,
    RequireHooksRefusedError,
)
from lionagi._errors import TimeoutError as LionTimeoutError
from lionagi._provider_errors import ProviderError, ProviderSafetyError, _WorkerSpawnAdmissionError
from lionagi.ln.concurrency import (
    CancelScope,
    SigtermInterrupt,
    cache_cancelled_exc_class,
    cancelled_exc_classes,
    fail_after,
    run_async,
    run_sync,
    sleep,
)
from lionagi.protocols.generic.log import DataLoggerConfig
from lionagi.protocols.messages import ActionRequest, AssistantResponse
from lionagi.service._api_telemetry import collect_api_turn
from lionagi.service._subagent_admission import (
    DEFAULT_WORKLOAD,
    WORKLOADS,
    NestedAgentRefusedError,
    SubagentAdmissionRefusedError,
)
from lionagi.service.imodel import _endpoint_routing_enabled
from lionhub.agent.khive_actor import attribute_khive_servers, run_khive_actor
from lionhub.orchestration.arguments import BoundedInt
from lionhub.orchestration.console import hint, log_error
from lionhub.orchestration.job import (
    EXIT_CODE_BY_STATUS,
    EXIT_CODE_PARTIAL_OUTPUT,
    EXIT_CODE_SUBAGENT_REFUSED,
    classify_exception,
    validate_cwd_exists,
)
from lionhub.orchestration.mcp_config import (
    LegacyMcpReplayError,
    McpConfigError,
    McpResolution,
    _format_recorded_mcp_path,
    explicit_mcp_config_path,
    resolve_spawn_mcp_servers,
)
from lionhub.orchestration.runs import (
    _MCP_AGENT_RUN_ID,
    _MCP_JOB_RUN_ID_ENV_VAR,
    _SUBSCRIPTION_SWITCH_KEY,
    SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD,
    TERMINAL_WRITE_LOST_FIELD,
    RunDir,
    _signal_window_marker,
    _subscription_fallback_resume_refusal,
    _SubscriptionFallbackResumeError,
    _write_signal_persistence_degraded_at,
    _write_terminal_write_lost,
    allocate_run,
    current_run_id,
    find_branch,
    load_last_branch,
    lost_terminal_write,
    resolve_run_reason,
    save_last_branch_pointer,
    setup_agent_persist,
    teardown_agent_persist,
)
from lionhub.orchestration.spawn import (
    _CLAUDE_PROVIDER_NAMES,
    BACKENDS,
    MAX_TIMEOUT_SECONDS,
    PROVIDER_BYPASS_KWARGS,
    PROVIDER_EFFORT_KWARG,
    PROVIDER_FAST_KWARGS,
    PROVIDER_YOLO_KWARGS,
    PROVIDERS_EFFORT_VIA_MODEL_NAME,
    _add_claude_tool_kwargs,
    _add_codex_account_integration_kwargs,
    _add_workload_kwarg,
    _clamp_claude_effort,
    _clamp_codex_effort,
    _profile_tool_settings,
    add_common_cli_args,
    build_chat_model,
    build_deadline_preamble,
    load_agent_profile,
    normalize_effort,
    resolve_account_integrations,
    resolve_persisted_effort,
)
from lionhub.orchestration.spec_limits import MAX_SPEC_PROMPT_CHARS
from lionhub.state import provenance as _provenance
from lionhub.state.artifact_verifier import resolve_artifact_contract
from lionhub.state.cli_worker_context import _bind_cli_worker_recorder
from lionhub.state.cli_workers import _prepare_worker_spawn_continuation

from ._agent_depth import stamp_agent_depth
from ._api_usage import APIUsageCollector
from ._context_from import (
    DEFAULT_CONTEXT_BUDGET_TOKENS,
    ContextFromError,
    resolve_and_build_context_block,
)

# Preset names supported by --preset.
_PRESET_CHOICES = ("coding",)
# The name of the account whose API key the launcher handed this process, never the key
# itself. It follows the routed-account name rule (``_ACCOUNT_NAME_RE`` and ``_looks_like_a_key``
# in lionagi/config.py), so a mixed-case value or one containing "sk-ant" is refused. This field
# also refuses one starting "sk-", which that rule admits.
_API_ACCOUNT_ENV = "LIONAGI_API_ACCOUNT"
# The provider an Anthropic API run moves to when the account router refuses its first request:
# the Claude subscription route, which takes the same model id.
_SUBSCRIPTION_FALLBACK_PROVIDER = "claude_code"
# The Claude Code built-in tools that stand for an API tool set's own, for the sets that move to
# the subscription route. Only the read set moves: its `reader` is Read, and its `search` greps
# (Grep) and finds files by name (Glob); its `context` tool manages the API model's own window
# and has no built-in counterpart. The write set's editor and bash ran under the macOS sandbox
# and the API route's command checks, which Claude Code has no counterpart for, so a write-set
# run does not move.
_SUBSCRIPTION_API_SET_TOOLS: dict[str, str] = {
    "read": "Read,Grep,Glob",
}
# The one variable a run that moves to the subscription route keeps although its name matches a
# sensitive-name pattern: Claude Code's own login token, which a run authenticating that way reads
# from its environment. Every API key name is removed from the switched child's environment, so
# that no key is billed in place of the subscription.
_SUBSCRIPTION_ENV_ALLOWLIST: tuple[str, ...] = ("CLAUDE_CODE_OAUTH_TOKEN",)
# A workspace root a Claude Code permission rule can carry literally: an absolute path of
# letters, digits, `.`, `_`, `-` and `/` only. Any other character (a glob character, a
# parenthesis, whitespace) would change what the rule matches or how it parses, so a run
# whose root has one does not move.
_SUBSCRIPTION_RULE_ROOT = re.compile(r"/[A-Za-z0-9._/-]+")


@functools.cache
def _case_folds() -> dict[str, frozenset[str]]:
    """Each string some character case-folds to, mapped to the characters that fold to it
    (``s`` to ``s``, ``S`` and ``ſ``; ``ss`` to ``ß`` and ``ẞ``)."""
    folds: dict[str, set[str]] = {}
    for point in range(sys.maxunicode + 1):
        char = chr(point)
        folded = char.casefold()
        if folded != char:
            folds.setdefault(folded, set()).add(char)
    return {folded: frozenset(chars) for folded, chars in folds.items()}


def _any_case(name: str) -> list[str]:
    """Rule patterns that together match every name that case-folds to *name*'s fold, which
    is how the API reader compares a name with the protected ones: a case-insensitive file
    system opens ``.ENV`` as ``.env``, and ``id_rſa`` folds to ``id_rsa``. Each character
    becomes a class of every character folding to it, and each run of characters one
    character folds to (``ss`` in ``.htpasswd``, from ``ß``) may also be that character."""
    folds = _case_folds()
    target = name.casefold()

    def one(char: str) -> str:
        chars = sorted({char} | folds.get(char, frozenset()))
        return chars[0] if len(chars) == 1 else f"[{''.join(chars)}]"

    def spell(start: int) -> list[str]:
        if start == len(target):
            return [""]
        spelled = [one(target[start]) + rest for rest in spell(start + 1)]
        for folded, chars in folds.items():
            if len(folded) > 1 and target.startswith(folded, start):
                whole = f"[{''.join(sorted(chars))}]"
                spelled += [whole + rest for rest in spell(start + len(folded))]
        return spelled

    return spell(0)


def _subscription_read_rules(
    tools: str, cwd: str | None
) -> tuple[str, list[str], list[str]] | None:
    """The run's workspace root, the permission rules that grant each of *tools* (a
    ``--tools`` value) only under it, and the rules that deny each of them the protected
    names under it; None when the root cannot be written as a rule or holds a protected name.

    The root is the one the API read set's ``guard_paths`` holds its reader to: *cwd*, or the
    process's working directory, resolved. ``//`` begins an absolute path in a Claude Code
    rule, and ``/**`` matches everything below it. The API reader also refuses a protected
    name (``.env``, a private key) inside the root, in any letter case, and a deny rule wins
    over a grant, so each of those names is denied at any depth.
    """
    from lionagi.libs.path_safety import DENIED_NAMES, is_protected_name

    resolved = Path(cwd if cwd else Path.cwd()).expanduser().resolve(strict=False)
    root = str(resolved)
    if not _SUBSCRIPTION_RULE_ROOT.fullmatch(root):
        return None
    # The API search leaves out every result whose path holds a protected name in any
    # component, the root's own included, and no rule below the root can say that, so a run
    # whose root holds one does not move.
    if any(is_protected_name(part) for part in resolved.parts):
        return None
    names = tools.split(",")
    grants = [f"{tool}(/{root}/**)" for tool in names]
    # The API search also leaves out everything below a directory with a protected name, so
    # each name is denied as a file and as a directory's contents.
    denials = [
        f"{tool}(/{root}/**/{pattern}{below})"
        for tool in names
        for protected in sorted(DENIED_NAMES)
        for pattern in _any_case(protected)
        for below in ("", "/**")
    ]
    return root, grants, denials


# The handoff an automatic resume carries when the timed-out leg's setup returned no session:
# no running row exists for the resumed leg to adopt, so its setup starts as a fresh launch.
_NO_PERSISTED_EXECUTION = object()
# The MCP choice a branch was launched with, replayed by a resume that names none.
_MCP_LAUNCH_KEY = "mcp_launch"
# The config file a branch was named with, kept as provenance once the record points at the
# run directory's copy of the set it handed (ADR-0041 Amendment 8).
_MCP_CONFIG_NAMED_KEY = "mcp_config_named"
# The name of that copy inside a run directory.
_MCP_CONFIG_COPY_NAME = "mcp-config.json"
# Branch metadata naming the hooks file copy a leg started with, so a resume checks it again.
_HOOKS_FROM_KEY = "hooks_from"
_HOOKS_FROM_SPEC_FIELDS = ("copy", "sha256", "format", "carry", "source")
# The subscription account a branch was launched on (names and paths only, never a token); a
# resume runs on the same one unless a Claude Code resume names another.
_SUBSCRIPTION_ACCOUNT_KEY = "subscription_account"
# The accounts a Claude Code branch was later resumed on, in order, appended and never
# rewritten; the branch runs on the last one.
_SUBSCRIPTION_ACCOUNT_SWITCHES_KEY = "subscription_account_switches"
# An API model has no tool loop of its own, so lionagi runs one in-process, and
# only for a profile that names the tool set the leg may use (`api_tools`). The
# read set runs with this process's own permissions. The write set adds the editor
# and bash, whose writes and commands run in child processes under a macOS Seatbelt
# policy (ADR-0041 Amendment 2); without sandbox-exec it is refused.
_API_TOOL_SETS: dict[str, tuple[str, ...]] = {
    "read": ("reader", "search", "context"),
    "write": ("reader", "search", "context", "editor", "bash"),
}
# The tool sets whose editor and bash run under the Seatbelt policy.
_API_CONFINED_TOOL_SETS = ("write",)
# The tool set and round bound an API branch was launched with, read again on resume.
_API_LAUNCH_KEY = "api_launch"
# Set when a branch was launched under a profile's require_hooks; a resume is checked against
# it whatever profile the resume names, as it keeps the approval mode it was launched with.
_REQUIRE_HOOKS_KEY = "require_hooks"
# Rounds of the tool loop after the first; a profile's `max_extensions` replaces it.
_API_DEFAULT_MAX_EXTENSIONS = 10
# The tool loops an API leg can run (`api_loop`): ReAct, the default, batched LNDL rounds
# (ADR-0132), or those rounds with the tools shown only as LNDL calls (`lndl-calls`, ADR-0132
# Amendment 2). A launch record written before the key existed runs ReAct.
_API_LOOPS = ("react", "lndl", "lndl-calls")
_API_DEFAULT_LOOP = "react"
# The <lact> calls one LNDL round may declare; a round that declares more runs none.
_API_LNDL_MAX_ACTIONS = 16
# An LNDL leg keeps calls and the answer in separate rounds, so the answer is written after
# the results are read; this is the guidance that tells the model so.
_API_LNDL_GUIDANCE = (
    "A round either declares <lact> calls, with no OUT{}, or writes the answer, with no "
    "<lact>. Calls declared in a round run when it ends, and you read their results in the "
    "next round. Write the answer as text in an <lvar> and bind it in OUT{answer: [...]}; a "
    "tool result is never the answer."
)
# An lndl-calls leg is also asked to declare in one round every read it already knows it needs.
_API_LNDL_CALLS_GUIDANCE = (
    f"{_API_LNDL_GUIDANCE} In each calling round, declare every read you already know you "
    f"need, up to {_API_LNDL_MAX_ACTIONS} calls, and answer only after reading what the answer "
    "depends on."
)
# A profile's `max_input_tokens` for an API leg: a positive integer, "auto" (the model's first
# price-tier boundary, none for a model priced flat) or "none". Unset reads as "auto".
_API_INPUT_CAP_WORDS = ("auto", "none")
_API_DEFAULT_INPUT_CAP = "auto"
_API_POLICY_KEYS = (
    "tool_result_max_chars",
    "context_fold_at",
    "context_fold_target",
    "context_fold_tail_margin",
    "prompt_caching",
    "max_output_tokens",
)
# How many times one turn (the task, or one queued message) folds and continues after the
# input cap refused a request.
_API_FOLD_RESUMES = 2
_API_FOLD_RESUME_PROMPT = (
    "The host folded part of your context: the next request would have passed the input cap. "
    "The task instruction above is unchanged, and a note in the history lists what was folded "
    "and how to list or restore it. Continue the task from where you stopped."
)
# How many times one turn continues after a pass of the ReAct loop ended on a failed round: a
# response cut off at its output limit before it wrote any text or tool call, a response the
# provider refused, or a reply that did not parse. An LNDL turn repairs such a round inside its
# own round budget.
_API_ROUND_RESUMES = 2
_API_CAPPED_RESUME_PROMPT = (
    "Your last response reached its output token limit before it wrote any text or tool call, "
    "so nothing from it was received. The task instruction above is unchanged. Continue the "
    "task from where you stopped: keep the reasoning short, then act or answer."
)
_API_EMPTY_ANSWER_RESUME_PROMPT = (
    "Your last reply had no text, so no answer was received. The task instruction above is "
    "unchanged. Continue the task from where you stopped, then give your answer."
)
_API_UNPARSED_RESUME_PROMPT = (
    "Your last reply could not be read in the required response format ({reason}), so none of "
    "the actions in it ran. The task instruction above is unchanged. Continue the task from "
    "where you stopped, and send the actions again in the required format."
)
_API_REFUSED_RESUME_PROMPT = (
    "Your last response was stopped by the provider before it finished, so nothing in it was "
    "used. The task instruction above is unchanged. Continue the task from where you stopped."
)
# The output limit, thinking included, an Anthropic API leg's requests send when its model names
# none; the provider's own default is 8,192. Claude Haiku 5.5 thinks by default, and at 8,192 a
# request could spend the whole limit on thinking and write nothing. At about 200 output tokens a
# second this limit takes some 160 seconds, inside the endpoint's 600-second request timeout.
_API_MAX_OUTPUT_TOKENS = 32_768
# The same limit when the request carries effort xhigh or max: at those efforts a thinking model can
# spend more than 32,768 tokens before it writes anything. At about 200 output tokens a second this
# takes some 330 seconds, still inside the 600-second request timeout; the leg does not stream, so
# a larger limit would turn a long response into a timeout.
_API_HIGH_EFFORT_MAX_OUTPUT_TOKENS = 65_536
_API_HIGH_EFFORTS = frozenset({"xhigh", "max"})
# A limit above _API_MAX_OUTPUT_TOKENS raises the request timeout to at least one second per
# this many tokens of the limit (ADR-0137 Amendment 1 D1b), so a full response at the measured
# output rates finishes inside the timeout.
_API_TOKENS_PER_TIMEOUT_SECOND = 50
# Branch metadata recording, per model role, the output limit and the raised request timeout
# _install_api_input_cap set. Both persist with the branch's models, so a resumed leg reads them
# back as if its model had named them; the record lets each start re-derive them from the
# effort the models carry then (ADR-0137 Amendment 1 D1d) instead of keeping an earlier run's.
_API_INSTALLED_OUTPUT_KEY = "api_installed_output"


def _api_default_output_limit(kwargs: dict) -> int:
    """The output limit an Anthropic leg sends when neither its model nor its profile names one."""
    output_config = kwargs.get("output_config")
    effort = output_config.get("effort") if isinstance(output_config, dict) else None
    if effort in _API_HIGH_EFFORTS:
        return _API_HIGH_EFFORT_MAX_OUTPUT_TOKENS
    return _API_MAX_OUTPUT_TOKENS


_DETACHED_EXECUTION_BOUNDARY = """[DETACHED EXECUTION BOUNDARY]
This process was launched as a detached MCP job. No interactive harness is attached, so this turn cannot receive a background completion notification. Keep every command in the foreground. If a command tool yields a live execution handle, poll that same handle until it reaches a terminal result. Do not arm a monitor or finish a turn waiting for an external notification. Verify the requested outputs before reporting completion."""

# --image extension -> MIME type, matching InstructionContent's data-URI allowlist
# (lionagi/protocols/messages/instruction.py _DATA_IMAGE_RE: png/jpe?g/gif/webp).
_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_KHIVE_INJECTION_COUNTERS = (
    "recall_turns",
    "blocks_injected",
    "failed",
    "degraded_turns",
    "writeback_records",
    "writeback_failed",
)


def _apply_detached_execution_boundary(prompt: str) -> str:
    """Tell detached MCP legs which interactive wait channel they do not have."""
    if not os.environ.get(_MCP_JOB_RUN_ID_ENV_VAR):
        return prompt
    if prompt.startswith(_DETACHED_EXECUTION_BOUNDARY):
        return prompt
    return f"{_DETACHED_EXECUTION_BOUNDARY}\n\n{prompt}"


def _fold_injection_stats(totals: dict[str, int], stats: object) -> None:
    """Add one provider-stat snapshot into session totals.

    Unexpected values are ignored so telemetry cannot break teardown.
    """
    if not isinstance(stats, dict):
        return
    for counter in _KHIVE_INJECTION_COUNTERS:
        value = stats.get(counter)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            totals[counter] += value


async def _seed_injection_stats(live: dict | None, totals: dict[str, int]) -> None:
    """Seed a top-level resume from its durable session counters.

    Missing or malformed metadata leaves the new invocation at zero.
    """
    if not live or live.get("db") is None:
        return
    try:
        session = await live["db"].get_session(live["session_id"])
        metadata = session.get("node_metadata") if session else None
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        if isinstance(metadata, dict):
            _fold_injection_stats(totals, metadata.get("khive_injection"))
    except Exception:  # noqa: BLE001
        return


def _load_image_data_uris(paths: list[str]) -> list[str]:
    """Read each --image path and wrap it as a `data:image/...;base64,...` URI —
    the same shape InstructionContent._format_image_item already accepts, so this
    is a pure CLI-side reader, not a new content representation.

    Raises FileNotFoundError / ValueError (with the offending path named) for a
    missing path, a non-file path, or an unrecognized extension. Fails fast,
    before any LLM call — same contract as --form / --prompt-file.
    """
    import base64

    uris: list[str] = []
    for raw_path in paths:
        p = Path(raw_path)
        if not p.exists():
            raise FileNotFoundError(f"--image path not found: {raw_path!r}")
        if not p.is_file():
            raise ValueError(f"--image path is not a regular file: {raw_path!r}")
        media_type = _IMAGE_MEDIA_TYPES.get(p.suffix.lower())
        if media_type is None:
            allowed = ", ".join(sorted(_IMAGE_MEDIA_TYPES))
            raise ValueError(
                f"--image {raw_path!r}: unrecognized extension {p.suffix!r}; "
                f"supported extensions: {allowed}"
            )
        encoded = base64.b64encode(p.read_bytes()).decode("ascii")
        uris.append(f"data:{media_type};base64,{encoded}")
    return uris


def _make_coding_preset(
    cwd: str | None = None,
    effort: str | None = "high",
    system_prompt: str | None = None,
    role: str = "implementer",
):
    """Construct an AgentSpec.coding() instance; isolated for test monkeypatching."""
    from lionhub.agent.spec import AgentSpec

    return AgentSpec.coding(cwd=cwd, effort=effort, system_prompt=system_prompt, role=role)


def _recall_only(configured: object) -> object:
    """A profile's ``khive_injection`` setting with writeback turned off.

    An API leg runs its tools in this process, so writeback would receive the read
    set's outputs and store file text in khive. Recall stays as the profile sets it.
    """
    if configured is True:
        return {"writeback": {"enabled": False}}
    if isinstance(configured, dict):
        return {**configured, "writeback": {"enabled": False}}
    return configured


def _profile_api_launch(profile) -> dict | None:
    """The API tool loop a profile declares, or None when it declares none.

    `api_tools` names the tool set, `read` or `write`; `api_network` lets the write
    set's commands reach the network (default off); `max_extensions` bounds the rounds
    after the first (1 to 100); `api_loop` picks the loop, `react` or `lndl`;
    `max_input_tokens` caps each request's estimated input, a positive integer, `auto`
    or `none`. A value outside these policies, any API policy without `api_tools`, or
    the write set on a host without sandbox-exec, is refused here before a run exists.
    Additional controls remain in the profile's extra mapping.
    """
    extra = (getattr(profile, "extra", None) or {}) if profile is not None else {}
    name = getattr(profile, "name", "<unknown>")
    if "api_tools" not in extra:
        for key in ("api_loop", "max_input_tokens", "api_network", *_API_POLICY_KEYS):
            if key in extra:
                raise ConfigurationError(
                    f"agent profile {name!r} sets {key} but no api_tools; it applies only "
                    "to the tool loop of an API model ('api_tools: read' or 'api_tools: "
                    "write'), and a CLI provider runs its own."
                )
        return None
    tools = extra["api_tools"]
    if not isinstance(tools, str) or tools not in _API_TOOL_SETS:
        raise ConfigurationError(
            f"agent profile {name!r} sets api_tools to {tools!r}; an API model runs with "
            + " or ".join(
                f"the {key} set ('{key}': {', '.join(names)})"
                for key, names in _API_TOOL_SETS.items()
            )
            + "."
        )
    network = extra.get("api_network", False)
    if not isinstance(network, bool):
        raise ConfigurationError(
            f"agent profile {name!r} sets api_network to {network!r}; it must be true or false."
        )
    if network and tools not in _API_CONFINED_TOOL_SETS:
        raise ConfigurationError(
            f"agent profile {name!r} sets api_network with api_tools {tools!r}; it applies "
            "only to the write set's commands ('api_tools: write')."
        )
    _require_seatbelt(tools, label=f"agent profile {name!r}")
    rounds = extra.get("max_extensions", _API_DEFAULT_MAX_EXTENSIONS)
    if not _is_round_bound(rounds):
        raise ConfigurationError(
            f"agent profile {name!r} sets max_extensions to {rounds!r}; "
            "it must be a whole number from 1 to 100."
        )
    loop = extra.get("api_loop", _API_DEFAULT_LOOP)
    if not isinstance(loop, str) or loop not in _API_LOOPS:
        raise ConfigurationError(
            f"agent profile {name!r} sets api_loop to {loop!r}; an API model runs "
            f"{' or '.join(repr(choice) for choice in _API_LOOPS)}."
        )
    cap = extra.get("max_input_tokens", _API_DEFAULT_INPUT_CAP)
    if not _is_input_cap_policy(cap):
        raise ConfigurationError(
            f"agent profile {name!r} sets max_input_tokens to {cap!r}; it must be a "
            "positive whole number of tokens, 'auto' or 'none'."
        )
    settings = _api_requested_settings(extra, label=f"agent profile {name!r}")
    return {
        "tools": tools,
        "network": network,
        "max_extensions": rounds,
        "loop": loop,
        "max_input_tokens": cap,
        **settings,
    }


def _api_requested_settings(values: dict, *, label: str, stored: bool = False) -> dict:
    """Validate requested settings, preserving unset values separately from defaults."""
    from lionhub.agent.context_fold import _FOLD_AT, _FOLD_TARGET, _TAIL_MARGIN

    settings = {key: values.get(key) for key in _API_POLICY_KEYS}
    for key, value in settings.items():
        if key not in values or (stored and value is None):
            continue
        if key == "tool_result_max_chars":
            valid = isinstance(value, int) and not isinstance(value, bool) and value >= 1024
        elif key == "max_output_tokens":
            valid = isinstance(value, int) and not isinstance(value, bool) and value >= 1
        elif key == "prompt_caching":
            valid = isinstance(value, bool)
        elif key == "context_fold_tail_margin":
            # 0 is a value here: it turns the end-of-turn skip before a fold off.
            valid = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and 0 <= value < 1
                and math.isfinite(value)
            )
        else:
            valid = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and 0 < value <= 1
                and math.isfinite(value)
            )
        if not valid:
            raise ConfigurationError(f"{label} sets invalid {key}: {value!r}")
    fold_at = settings["context_fold_at"]
    target = settings["context_fold_target"]
    fold_at = _FOLD_AT if fold_at is None else fold_at
    target = _FOLD_TARGET if target is None else target
    if not 0 < target < fold_at <= 1:
        raise ConfigurationError(
            f"{label} needs 0 < context_fold_target < context_fold_at <= 1; "
            f"the effective pair is {target!r}, {fold_at!r}."
        )
    margin = settings["context_fold_tail_margin"]
    margin = _TAIL_MARGIN if margin is None else margin
    if not margin < fold_at:
        raise ConfigurationError(
            f"{label} needs 0 <= context_fold_tail_margin < context_fold_at; "
            f"the effective pair is {margin!r}, {fold_at!r}."
        )
    return settings


def _require_seatbelt(tools: str, *, label: str) -> None:
    from lionhub.tools._seatbelt import SANDBOX_EXEC, seatbelt_available

    if tools in _API_CONFINED_TOOL_SETS and not seatbelt_available():
        raise ConfigurationError(
            f"{label} names the {tools} set, whose editor and bash run only under the macOS "
            f"Seatbelt sandbox, and this host has no executable {SANDBOX_EXEC}; use "
            "'api_tools: read' here."
        )


def _is_round_bound(rounds: object) -> bool:
    return not isinstance(rounds, bool) and isinstance(rounds, int) and 1 <= rounds <= 100


def _is_input_cap_policy(cap: object) -> bool:
    if isinstance(cap, str):
        return cap in _API_INPUT_CAP_WORDS
    return not isinstance(cap, bool) and isinstance(cap, int) and cap >= 1


def _recorded_api_launch(recorded: object) -> dict:
    """The tool loop a stored branch was launched with.

    A record naming a tool set or a round bound this version does not run is
    refused here, before a run exists.
    """
    if isinstance(recorded, dict):
        tools, rounds = recorded.get("tools"), recorded.get("max_extensions")
        loop = recorded.get("loop", _API_DEFAULT_LOOP)
        # A record written before the cap existed carries none and reads as unset.
        cap = recorded.get("max_input_tokens", _API_DEFAULT_INPUT_CAP)
        # A record written before the write set existed carries no network switch.
        network = recorded.get("network", False)
        if (
            isinstance(tools, str)
            and tools in _API_TOOL_SETS
            and isinstance(network, bool)
            and (tools in _API_CONFINED_TOOL_SETS or not network)
            and _is_round_bound(rounds)
            and isinstance(loop, str)
            and loop in _API_LOOPS
            and _is_input_cap_policy(cap)
        ):
            _require_seatbelt(tools, label="this branch's stored API launch")
            return {
                "tools": tools,
                "network": network,
                "max_extensions": rounds,
                "loop": loop,
                "max_input_tokens": cap,
                **_api_requested_settings(recorded, label="stored API launch", stored=True),
            }
    raise ConfigurationError(
        f"this branch was launched on an API model's tool loop, but its stored launch "
        f"record {recorded!r} names a tool set, a round bound, a loop or an input cap this "
        "version does not run; start a new branch."
    )


def _make_api_agent_spec(
    *,
    cwd: str | None,
    effort: str | None,
    system_prompt: str | None,
    role: str,
    tools: str,
    network: bool = False,
    _command_env_names: frozenset[str] | None = None,
    profile_deny_read: tuple[str, ...] = (),
):
    """The AgentSpec for an API-model leg: the named tool set, path-guarded to cwd, and for
    the write set the Seatbelt policy its editor and bash run under.

    ``profile_deny_read`` holds the agent profile's ``deny_read`` entries. The write set's
    policy makes them unreadable too; the read set has no sandbox to hold them, so it refuses.
    """
    from lionhub.agent.spec import AgentSpec, _wire_secure_guards

    if profile_deny_read and tools not in _API_CONFINED_TOOL_SETS:
        from lionhub.agent.child_sandbox import _PROFILE_DENY_READ_REFUSAL_API_READ

        raise ConfigurationError(
            "the agent profile declares deny_read, which this run cannot hold: "
            f"{_PROFILE_DENY_READ_REFUSAL_API_READ}. Run it with 'api_tools: write' or on a CLI "
            "model, or remove deny_read from the profile."
        )
    spec = AgentSpec.compose(
        role, effort=effort, tools=["coding"], system_prompt=system_prompt, cwd=cwd
    )
    spec.coding_tools = _API_TOOL_SETS[tools]
    if tools in _API_CONFINED_TOOL_SETS:
        from lionhub.agent.child_sandbox import configured_deny_read
        from lionhub.tools._seatbelt import SeatbeltPolicy

        # The locations a CLI child may not read, the settings' child_sandbox.deny_read entries
        # and the agent profile's own included, are unreadable to this loop's bash and editor
        # too, and its commands get a uv cache of their own.
        deny_read = configured_deny_read()
        spec.seatbelt = SeatbeltPolicy.for_workspace(
            cwd or Path.cwd(),
            network=network,
            deny_read=(*deny_read, *(p for p in profile_deny_read if p not in deny_read)),
            per_leg_uv_cache=True,
            _command_env_names=_command_env_names,
        )
        # Read by create_agent, which refuses an API model whose spec does not hold them.
        spec._profile_deny_read = tuple(profile_deny_read)
        from lionhub.tools import seatbelt_link_probe

        # On a volume where Seatbelt does not judge a hard link's source, a link out of the roots
        # lets the editor and bash write a file outside them; the launch needs a passing link
        # probe record for every writable root's volume.
        seatbelt_link_probe.require_passing_link_record(spec.seatbelt)
    _wire_secure_guards(spec, cwd)
    return spec


class _LndlAnswer(BaseModel):
    """The answer an LNDL leg ends its turn with: text the model wrote.

    A tool's result is a mapping, and a failed call's is nothing or an error
    mapping; placed in ``OUT{}`` it would hand back a result nobody read, so it is
    refused and the refusal is the next round's repair message.
    """

    answer: str

    @field_validator("answer", mode="before")
    @classmethod
    def _written_text(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError(
                "the answer must be text you write. A tool result cannot be the answer: "
                "read the results above and write the answer as text in OUT{answer: ...}."
            )
        return value


def _resolve_api_input_cap(policy: object, chat_model) -> tuple[int | None, str]:
    """The input cap an API leg runs under, and where it came from.

    `auto` is the first price-tier boundary of a model the pricing table prices by prompt
    length; a model priced flat, or not in the table, gets no cap and the source says so.
    """
    if policy == "none":
        return None, "max_input_tokens: none"
    if isinstance(policy, int):
        return policy, "max_input_tokens"
    model = getattr(chat_model, "model_name", "") or ""
    config = getattr(getattr(chat_model, "endpoint", None), "config", None)
    if getattr(config, "provider", None) == "anthropic":
        from lionagi.service.accounts._pricing import CURRENT_REVISION

        price = CURRENT_REVISION.models.get(model)
        if price is not None and len(price.tiers) > 1:
            return price.tiers[0].max_prompt_tokens, f"auto: {model}'s first price tier"
    return None, f"auto: {model or 'this model'} is not priced by prompt length"


def _api_models(branch) -> list:
    """The branch's chat model, and its parse model when that is another object: parse
    repairs send through the parse model, so its refusals belong to the run too."""
    models = [branch.chat_model]
    parse_model = getattr(branch, "parse_model", None)
    if parse_model is not None and parse_model is not branch.chat_model:
        models.append(parse_model)
    return models


def _first_input_cap_refusal(branch) -> Exception | None:
    for api_model in _api_models(branch):
        refusal = getattr(api_model, "input_cap_refusal", None)
        if refusal is not None:
            return refusal
    return None


def _install_api_input_cap(
    branch,
    policy: object,
    *,
    tool_result_max_chars: int | None = None,
    context_fold_at: float | None = None,
    context_fold_target: float | None = None,
    context_fold_tail_margin: float | None = None,
    prompt_caching: bool | None = None,
    max_output_tokens: int | None = None,
):
    """Put the leg's input cap on its models and fold its context under it.

    An Anthropic model is given *max_output_tokens* when the profile names it; otherwise a
    model that names no output limit is given ``_API_MAX_OUTPUT_TOKENS``, or
    ``_API_HIGH_EFFORT_MAX_OUTPUT_TOKENS`` when its requests carry effort xhigh or max. A limit
    set here above ``_API_MAX_OUTPUT_TOKENS`` raises the model's request timeout to at least one
    second per ``_API_TOKENS_PER_TIMEOUT_SECOND`` tokens of it. A limit and a raised timeout an
    earlier start set, recorded in the branch's metadata, are re-derived here rather than kept, so
    a resumed leg's limit follows the effort its models carry now. A profile limit on a model of
    another provider is refused before anything changes.
    Returns the branch's ContextFold, or None when the leg runs without a cap.
    """
    from lionagi.ln import copy
    from lionhub.agent.context_fold import (
        _FOLD_AT,
        _FOLD_TARGET,
        _TAIL_MARGIN,
        install_context_fold,
    )

    # Profile nulls have already been refused. Here None is an unset requested value.
    _api_requested_settings(
        {
            "tool_result_max_chars": tool_result_max_chars,
            "context_fold_at": context_fold_at,
            "context_fold_target": context_fold_target,
            "context_fold_tail_margin": context_fold_tail_margin,
            "prompt_caching": prompt_caching,
            "max_output_tokens": max_output_tokens,
        },
        label="API launch policy",
        stored=True,
    )
    if max_output_tokens is not None:
        for api_model in _api_models(branch):
            provider = getattr(api_model.endpoint.config, "provider", None)
            if provider != "anthropic":
                raise ConfigurationError(
                    f"max_output_tokens sets an Anthropic model's output limit; this leg's "
                    f"model {api_model.model_name!r} runs on {provider!r}."
                )
    fold_at = _FOLD_AT if context_fold_at is None else context_fold_at
    target = _FOLD_TARGET if context_fold_target is None else context_fold_target
    tail_margin = _TAIL_MARGIN if context_fold_tail_margin is None else context_fold_tail_margin
    cap, source = _resolve_api_input_cap(policy, branch.chat_model)
    # Check an existing fold before changing models. A rejected pair must leave its
    # model policy and pending pre/post-call state intact.
    fold = (
        install_context_fold(
            branch, cap=cap, fold_at=fold_at, target=target, tail_margin=tail_margin, log=hint
        )
        if cap is not None
        else None
    )
    earlier = branch.metadata.get(_API_INSTALLED_OUTPUT_KEY)
    earlier = earlier if isinstance(earlier, dict) else {}
    installed = {}
    models = {}
    for role, api_model in zip(("chat", "parse"), _api_models(branch), strict=False):
        config = api_model.endpoint.config
        kwargs = config.kwargs or {}
        prior = earlier.get(role)
        prior = prior if isinstance(prior, dict) else {}
        # A limit an earlier start set, still in place, is that start's and not the model's.
        inherited_limit = (
            prior.get("max_tokens") is not None and kwargs.get("max_tokens") == prior["max_tokens"]
        )
        base_timeout = config.timeout
        prior_timeout = prior.get("timeout")
        if (
            isinstance(prior_timeout, list)
            and len(prior_timeout) == 2
            and config.timeout == prior_timeout[0]
        ):
            base_timeout = prior_timeout[1]
        output_limit = None
        if getattr(config, "provider", None) == "anthropic":
            if max_output_tokens is not None:
                output_limit = max_output_tokens
            elif inherited_limit or kwargs.get("max_tokens") is None:
                output_limit = _api_default_output_limit(kwargs)
        timeout_floor = None
        if output_limit is not None and output_limit > _API_MAX_OUTPUT_TOKENS:
            timeout_floor = -(-output_limit // _API_TOKENS_PER_TIMEOUT_SECOND)
            if base_timeout >= timeout_floor:
                timeout_floor = None
        new_timeout = base_timeout if timeout_floor is None else timeout_floor
        drop_limit = inherited_limit and output_limit is None
        if output_limit is not None:
            installed[role] = {
                "max_tokens": output_limit,
                "timeout": [timeout_floor, base_timeout] if timeout_floor is not None else None,
            }
        changes = (
            output_limit is not None
            or drop_limit
            or new_timeout != config.timeout
            or getattr(api_model, "tool_result_max_chars", None) != tool_result_max_chars
            or (prompt_caching is not None and config.prompt_caching != prompt_caching)
            or (
                cap is not None
                and (api_model.max_input_tokens != cap or config.context_window != cap)
            )
        )
        selected = api_model
        if changes:
            # Isolate configuration without constructing a fresh logical model. Preserve
            # its refusal, calibration, call count, identity, hooks and executor state.
            selected = copy(api_model, deep=False)
            selected.endpoint = copy(api_model.endpoint, deep=False)
            selected.endpoint.config = config.model_copy(deep=True)
            selected.tool_result_max_chars = tool_result_max_chars
            if prompt_caching is not None:
                selected.endpoint.config.prompt_caching = prompt_caching
            if cap is not None:
                selected.max_input_tokens = cap
                selected.endpoint.config.context_window = cap
            if output_limit is not None:
                selected.endpoint.config.kwargs = {
                    **(selected.endpoint.config.kwargs or {}),
                    "max_tokens": output_limit,
                }
            elif drop_limit:
                selected.endpoint.config.kwargs = {
                    k: v
                    for k, v in (selected.endpoint.config.kwargs or {}).items()
                    if k != "max_tokens"
                }
            selected.endpoint.config.timeout = new_timeout
        models[id(api_model)] = selected
    if installed:
        branch.metadata[_API_INSTALLED_OUTPUT_KEY] = installed
    else:
        branch.metadata.pop(_API_INSTALLED_OUTPUT_KEY, None)
    chat, parse = branch.chat_model, getattr(branch, "parse_model", None)
    branch.chat_model = models[id(chat)]
    if parse is not None:
        branch.parse_model = models[id(parse)]
    if cap is None:
        hint(f"[api] no input cap ({source})")
        return None
    hint(
        f"[api] input cap {cap:,} tokens ({source}); the host folds at "
        f"fold_at={fold.fold_at} ({int(fold.fold_at * cap):,} tokens) toward "
        f"target={fold.target} ({int(fold.target * cap):,} tokens)"
    )
    return fold


def _calls_sent(branch) -> dict[int, int]:
    """Calls sent so far by each model this branch uses, keyed by instance."""
    models = (getattr(branch, "chat_model", None), getattr(branch, "parse_model", None))
    return {id(m): m.calls_sent for m in models if hasattr(m, "calls_sent")}


def _tool_results(branch) -> int:
    from lionagi.protocols.messages import ActionResponse

    return sum(
        isinstance(m, ActionResponse) and not m.metadata.get("clipped_from")
        for m in branch.msgs.messages
    )


async def _run_api_turn(
    branch,
    instruction: str,
    *,
    timeout: float | None,
    max_extensions: int,
    snapshot_dir: str,
    loop: str = _API_DEFAULT_LOOP,
    fold=None,
) -> str:
    """One turn of an API-model leg: lionagi's tool loop over the branch's tools.

    The loop is ``Branch.ReAct`` or, for ``loop="lndl"``, batched LNDL rounds
    (ADR-0132); *timeout* bounds the whole turn and raises ``TimeoutError`` past
    it. However the turn ends, what it spent is added to the launch record's
    ``spend``, the invocation's total, and printed, and then the branch is written
    to *snapshot_dir*, so a resume and a reader of the run directory find both.
    Returns the answer's text.

    A refusal by the input cap fails the turn even when the loop caught it: a refused
    request must never hand back an earlier round's text as the answer. With a *fold*
    (lionhub.agent.context_fold), a request the cap refused despite the fold's own
    threshold is folded instead, calibrated by the refusal's estimate, and the loop
    continues, at most ``_API_FOLD_RESUMES`` times; a refusal the fold cannot get under the
    cap fails the turn as before.

    A ReAct pass that ended on a failed round (``_failed_round``) is continued with a prompt
    naming what failed, at most ``_API_ROUND_RESUMES`` times, and then fails the turn with
    ``FailedRoundError``, so a round cut off at its output limit or a reply that did not parse
    is never taken as the end of the work.
    """
    import contextlib

    from lionagi.operations.run.run import _write_branch_snapshot

    calls_before = _calls_sent(branch)
    tools_before = _tool_results(branch)
    started = time.monotonic()
    deadline = fail_after(timeout) if timeout is not None else contextlib.nullcontext()
    result = None
    failed = None
    round_resumes = 0
    try:
        with deadline:
            resumes = 0
            while True:
                before = {m.id for m in branch.msgs.messages}
                rounds: list = []
                failed = None
                try:
                    result = await _api_loop_pass(
                        branch, instruction, loop, max_extensions, rounds=rounds
                    )
                except InputTokenCapError:
                    # The model keeps the refusal, so it is folded below or raised after the
                    # snapshot like one the loop caught.
                    if _first_input_cap_refusal(branch) is None:
                        raise
                    result = None
                refusal = _first_input_cap_refusal(branch)
                if refusal is None:
                    failed = _failed_round(branch, before, rounds) if rounds else None
                    if failed is None or round_resumes >= _API_ROUND_RESUMES:
                        break
                    round_resumes += 1
                    instruction = failed[0]
                    continue
                if fold is None or resumes >= _API_FOLD_RESUMES:
                    break
                observed = (getattr(refusal, "details", None) or {}).get("estimate")
                # Cleared before the fold so its summary request is sent; put back when the
                # fold cannot land, so the refusal still fails the turn below.
                refused = [(m, m.input_cap_refusal) for m in _api_models(branch)]
                for api_model, _ in refused:
                    api_model.input_cap_refusal = None
                try:
                    landed = await fold.fold(reason="refused", observed=observed)
                except Exception as exc:  # noqa: BLE001 — the refusal below is the outcome
                    log_error(f"context fold after a refusal failed: {exc!r}")
                    landed = False
                # The estimate is calibrated by the refusal's own count: still over the cap,
                # another pass would only be refused again.
                if not landed or fold.estimate() > fold.cap:
                    for api_model, kept in refused:
                        api_model.input_cap_refusal = kept
                    break
                resumes += 1
                instruction = _API_FOLD_RESUME_PROMPT
    finally:
        with CancelScope(shield=True):
            calls_after = _calls_sent(branch)
            record = branch.metadata.get(_API_LAUNCH_KEY)
            # The record totals this invocation's turns: a queued message drained as a
            # further turn adds to it, and a resume, a new invocation, clears it first.
            prior = record.get("spend") if isinstance(record, dict) else None
            prior = prior if isinstance(prior, dict) else {}
            spend = {
                "requests": prior.get("requests", 0)
                + sum(count - calls_before.get(key, 0) for key, count in calls_after.items()),
                "tool_calls": prior.get("tool_calls", 0) + _tool_results(branch) - tools_before,
                "wall_seconds": round(
                    prior.get("wall_seconds", 0.0) + time.monotonic() - started, 3
                ),
            }
            if isinstance(record, dict):
                record["spend"] = spend
            hint(
                f"[api] spend: {spend['requests']} request(s), {spend['tool_calls']} tool "
                f"call(s), {spend['wall_seconds']:.1f}s"
            )
            await _write_branch_snapshot(branch, snapshot_dir)
    refusal = _first_input_cap_refusal(branch)
    if refusal is not None:
        raise refusal
    if failed is not None:
        details: dict[str, Any] = {"continued": round_resumes}
        refused = _first_refusal(_pass_responses(branch, before))
        if refused is not None:
            details["stop_reason"] = refused[0]
            details.update(_refusal_details(refused[1]))
        failure = FailedRoundError(
            f"{round_resumes + 1} passes of the tool loop in a row ended on a failed round: "
            f"{failed[1]}",
            details=details,
        )
        if refused is not None:
            # A provider refusal is a provider failure that retrying the same request will not
            # clear, so the run records it as one rather than as a generic exception.
            raise failure from ProviderSafetyError(failed[1])
        raise failure
    answer = getattr(result, "answer", result)
    return "" if answer is None else str(answer)


def _failed_round(branch, before: set, rounds: list) -> tuple[str, str] | None:
    """Why a ReAct pass ended on a failed round, as (the prompt that continues the pass, the
    reason the turn fails with), or None when it did not.

    ``before`` holds the ids of the branch's messages before the pass, and ``rounds`` is what the
    pass returned with ``return_analysis=True``: each round's analysis, then the answer. The
    responses checked for a refusal and then against the output limit are the pass's last two,
    the round that ended the loop and the answer turn after it. A refused response (see
    ``_provider_refusal``) wins over an output-limit stop in that window and prints one
    ``[api] refused`` line; an answer with no text fails the pass whatever its stop. A round
    whose reply did not parse is reported as having stopped at its output token limit when its
    response did.
    """
    from lionagi.operations.types import UnparsedResponse

    window = _pass_responses(branch, before)
    refused = _first_refusal(window)
    if refused is not None:
        marker, response = refused
        tokens = _output_tokens(response)
        hint(
            f"[api] refused: stop reason {marker}"
            + ("" if tokens is None else f", {tokens} output token(s)")
        )
        return (
            _API_REFUSED_RESUME_PROMPT,
            f"the provider refused the response (stop reason: {marker})",
        )
    if any(_stopped_at_output_limit_with_nothing(response) for response in window):
        return (
            _API_CAPPED_RESUME_PROMPT,
            "a response stopped at its output token limit with no text and no tool call",
        )
    last_round = rounds[-2] if len(rounds) > 1 else None
    if isinstance(last_round, UnparsedResponse):
        if last_round.validation_error is not None:
            detail = str(last_round.validation_error).strip()
            if len(detail) > 800:
                detail = detail[:800] + " ...(truncated)"
            reason = f"the response schema rejected it: {detail}"
        else:
            from lionagi.service._output_limit import read_output_limit_stop

            reason = "no JSON object could be read from it"
            if len(window) > 1 and read_output_limit_stop(window[-2])[0]:
                reason = "the response stopped at its output token limit, so its text is incomplete"
        return _API_UNPARSED_RESUME_PROMPT.format(reason=reason), f"a reply did not parse: {reason}"
    # An answer turn whose reply had no text, cut off or not, ends ReAct with that empty text.
    answer = rounds[-1] if rounds else None
    text = getattr(answer, "answer", answer)
    if text is None or not str(text).strip():
        return _API_EMPTY_ANSWER_RESUME_PROMPT, "the answer had no text"
    return None


def _stopped_at_output_limit_with_nothing(response: Any) -> bool:
    """Whether a raw provider response stopped at its output token limit before it wrote any
    text or tool call. Reads the Anthropic Messages, OpenAI chat completions and OpenAI
    Responses shapes; any other shape, a list of responses included, reads False."""
    from lionagi.service._output_limit import read_output_limit_stop

    stopped, wrote = read_output_limit_stop(response)
    return stopped and not wrote


def _new_assistant_replies(branch: Branch, before: set) -> list[AssistantResponse]:
    """Return replies added since ``before``, skipping copies made by context folding."""
    return [
        m
        for m in branch.msgs.messages
        if m.id not in before
        and isinstance(m, AssistantResponse)
        and not m.metadata.get("clipped_from")
    ]


def _pass_responses(branch, before: set) -> list:
    """The raw provider responses a pass ended on: the last two it added, the round that ended
    the loop and the answer turn after it. ``before`` holds the ids of the branch's messages
    before the pass."""
    return [m.model_response for m in _new_assistant_replies(branch, before)[-2:]]


def _first_refusal(responses: list) -> tuple[str, Any] | None:
    """The refusal marker and the raw response of the first refused response in ``responses``,
    or None when none was refused."""
    for response in responses:
        marker = _provider_refusal(response)
        if marker is not None:
            return marker, response
    return None


def _refusal_details(response: Any) -> dict[str, Any]:
    """The provider's own account of a refusal, copied verbatim from a raw response: an Anthropic
    Messages response's ``stop_details`` when it is a non-empty JSON object, as ``stop_details``;
    an OpenAI chat completion's non-empty ``message.refusal`` string, or the first non-empty text
    of an OpenAI Responses ``refusal`` content part, as ``refusal``. Empty when the response
    carries none of these (a ``content_filter`` stop has no detail), so the key is then absent."""
    if not isinstance(response, dict):
        return {}
    if "stop_reason" in response:
        stop_details = response.get("stop_details")
        if isinstance(stop_details, dict) and stop_details:
            return {"stop_details": stop_details}
        return {}
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        refusal = message.get("refusal") if isinstance(message, dict) else None
        return {"refusal": refusal} if isinstance(refusal, str) and refusal else {}
    for item in response.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "refusal":
                    text = part.get("refusal")
                    if isinstance(text, str) and text:
                        return {"refusal": text}
    return {}


def _provider_refusal(response: Any) -> str | None:
    """The provider's refusal marker of a raw response, or None when it was not refused. Reads
    the Anthropic Messages (``stop_reason`` ``refusal``), OpenAI chat completions
    (``finish_reason`` ``content_filter``, or a non-empty ``message.refusal``) and OpenAI
    Responses (a ``message`` output item with a ``refusal`` content part) shapes; any other
    shape, a list of responses included, reads None."""
    if not isinstance(response, dict):
        return None
    if "stop_reason" in response:
        return "refusal" if response["stop_reason"] == "refusal" else None
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        if choices[0].get("finish_reason") == "content_filter":
            return "content_filter"
        message = choices[0].get("message")
        refusal = message.get("refusal") if isinstance(message, dict) else None
        return "refusal" if isinstance(refusal, str) and refusal else None
    for item in response.get("output") or []:
        if (
            isinstance(item, dict)
            and item.get("type") == "message"
            and any(
                isinstance(part, dict) and part.get("type") == "refusal"
                for part in item.get("content") or []
            )
        ):
            return "refusal"
    return None


def _output_tokens(response: Any) -> int | None:
    """The output token count a raw provider response carries in its ``usage``, or None."""
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return None
    for key in ("output_tokens", "completion_tokens"):
        count = usage.get(key)
        if isinstance(count, int) and not isinstance(count, bool):
            return count
    return None


async def _api_loop_pass(
    branch, instruction: str, loop: str, max_extensions: int, rounds: list | None = None
):
    """One pass of an API leg's tool loop: ReAct, or LNDL rounds for ``loop="lndl"``.

    For ReAct, *rounds* (when given) receives each round's analysis and then the answer.
    """
    if loop in ("lndl", "lndl-calls"):
        from lionhub.lndl.middle import build_lndl_middle

        schemas = branch.acts.get_tool_schema(tools=True)
        return await branch.operate(
            instruction=instruction,
            guidance=_API_LNDL_CALLS_GUIDANCE if loop == "lndl-calls" else _API_LNDL_GUIDANCE,
            response_format=_LndlAnswer,
            tool_schemas=schemas.get("tools", []) if isinstance(schemas, dict) else [],
            invoke_actions=False,
            handle_validation="raise",
            middle=build_lndl_middle(
                round_budget=1 + max_extensions,
                max_actions=_API_LNDL_MAX_ACTIONS,
                lacts_with_out=False,
                tools_as_calls=loop == "lndl-calls",
            ),
        )
    outs = await branch.ReAct(
        instruction=instruction,
        tools=True,
        max_extensions=max_extensions,
        return_analysis=True,
    )
    if rounds is not None:
        rounds.extend(outs)
    # What ReAct returns without return_analysis.
    final = outs[-1]
    return final.answer if hasattr(final, "answer") else final


# WorkForm loading helpers (for --form).


_FORM_SPEC_ALLOWED_KEYS = frozenset({"title", "fields", "values"})


def _load_form_spec(path: str) -> dict:
    """Load a YAML or JSON work-form spec file; raises ValueError/FileNotFoundError on failure."""
    from pathlib import Path as _Path

    p = _Path(path)
    if not p.exists():
        raise FileNotFoundError(f"form spec file not found: {path!r}")
    if not p.is_file():
        raise ValueError(f"form spec path is not a regular file: {path!r}")

    with open(path) as fh:
        raw = fh.read()

    # Try YAML first (superset of JSON), then fall back to plain JSON.
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(raw)
    except Exception as yaml_err:
        try:
            data = json.loads(raw)
        except Exception:
            raise ValueError(f"could not parse form spec {path!r}: {yaml_err}") from yaml_err

    if not isinstance(data, dict):
        raise ValueError(
            f"form spec {path!r} must be a YAML/JSON mapping, got {type(data).__name__}"
        )
    return data


def _build_work_form(spec: dict, spec_path: str):
    """Construct a WorkForm from a parsed spec dict (keys: title, fields, values)."""
    from lionhub.orchestration.work import FieldSpec, WorkForm, fill_form

    # Enforce closed top-level schema.
    unknown_keys = set(spec) - _FORM_SPEC_ALLOWED_KEYS
    if unknown_keys:
        bad = ", ".join(sorted(f"{k!r}" for k in unknown_keys))
        raise ValueError(
            f"form spec {spec_path!r}: unknown top-level key(s) {bad}; "
            f"allowed: {sorted(_FORM_SPEC_ALLOWED_KEYS)}"
        )

    title = spec.get("title", spec_path)
    raw_fields_raw = spec.get("fields")
    raw_values_raw = spec.get("values")

    # Validate types: 'fields' and 'values' must be mappings when present.
    if raw_fields_raw is not None and not isinstance(raw_fields_raw, dict):
        raise ValueError(
            f"form spec {spec_path!r}: 'fields' must be a mapping, "
            f"got {type(raw_fields_raw).__name__!r}"
        )
    if raw_values_raw is not None and not isinstance(raw_values_raw, dict):
        raise ValueError(
            f"form spec {spec_path!r}: 'values' must be a mapping, "
            f"got {type(raw_values_raw).__name__!r}"
        )

    raw_fields: dict = raw_fields_raw or {}
    raw_values: dict = raw_values_raw or {}

    # --form is a validation gate; values without declared fields would be
    # forwarded unvalidated, defeating that purpose.
    if raw_values and not raw_fields:
        raise ValueError(
            f"form spec {spec_path!r}: 'values' are declared but 'fields' is "
            "absent or empty; declare fields to validate values against"
        )

    # When fields are declared, reject undeclared value keys.
    if raw_fields:
        undeclared = set(raw_values) - set(raw_fields)
        if undeclared:
            bad = ", ".join(sorted(f"{k!r}" for k in undeclared))
            raise ValueError(
                f"form spec {spec_path!r}: values contain undeclared key(s) {bad}; "
                f"declared fields: {sorted(raw_fields)}"
            )

    fields: dict[str, FieldSpec] = {}
    for name, fspec in raw_fields.items():
        if not isinstance(fspec, dict):
            raise ValueError(
                f"form spec {spec_path!r}: field {name!r} must be a mapping, "
                f"got {type(fspec).__name__}"
            )
        try:
            fields[name] = FieldSpec(name=name, **fspec)
        except Exception as exc:
            raise ValueError(f"form spec {spec_path!r}: invalid field {name!r}: {exc}") from exc

    form = WorkForm(title=title, fields=fields)
    if raw_values or fields:
        form = fill_form(form, raw_values)
    return form


def _form_to_context_block(form) -> str:
    """Render a validated WorkForm's values as a structured context preamble
    to prepend to the user's prompt."""
    lines = [f"[Work Form: {form.title}]"]
    for key, value in form.values.items():
        lines.append(f"  {key}: {value!r}")
    return "\n".join(lines)


# Heartbeat tick interval. Module-level so a test can shrink it rather than
# waiting out a real 60s tick.
_HEARTBEAT_INTERVAL_S = 60


class _ProgressReport:
    """Heartbeat text that distinguishes a working agent from a merely live one.

    A bare elapsed-seconds timer reports that the event loop is scheduling, which
    a reader takes as evidence that work is happening. Assistant responses and
    action requests are added to the branch as the stream arrives, so counting
    them says whether anything has actually landed since the run started.
    """

    def __init__(self, branch, now: float):
        self._branch = branch
        self._start = now
        # The only read taken before the run starts, so every later count is a
        # delta from it. Do not retry it on failure: a baseline adopted once
        # messages have arrived would take them as the starting point and report
        # work already done as no progress at all.
        self._base = self._counts()
        self._last = self._base
        self._changed_at = now

    def _counts(self) -> tuple[int, int, int] | None:
        """(turns, tool calls, total messages), or None if they cannot be read."""
        try:
            messages = list(self._branch.msgs.messages)
        except Exception:
            return None
        turns = sum(1 for m in messages if isinstance(m, AssistantResponse))
        calls = sum(1 for m in messages if isinstance(m, ActionRequest))
        return turns, calls, len(messages)

    def line(self, now: float) -> str:
        elapsed = int(now - self._start)
        current = self._counts()
        # An unreadable baseline (never retried) means no delta for the rest
        # of this run; a single unreadable snapshot is only this tick's own
        # gap. Neither falls back to the previous reading — that would
        # present a stale count as current.
        if self._base is None:
            return (
                f"[progress] {elapsed}s elapsed — progress is not observable for "
                "this run; this line means alive, not working"
            )
        if current is None:
            return (
                f"[progress] {elapsed}s elapsed — progress could not be read this "
                "tick; this line means alive, not working"
            )
        if current[2] != self._last[2]:
            self._last = current
            self._changed_at = now
        turns = self._last[0] - self._base[0]
        calls = self._last[1] - self._base[1]
        if turns <= 0 and calls <= 0:
            return f"[progress] {elapsed}s elapsed — no completed turn yet ({elapsed}s since start)"
        return (
            f"[progress] {elapsed}s elapsed — {turns} turn{'' if turns == 1 else 's'}, "
            f"{calls} tool call{'' if calls == 1 else 's'}, "
            f"last activity {int(now - self._changed_at)}s ago"
        )


def _report_mcp_resolution(
    resolution: McpResolution, *, provider: str, cwd: str | None, forwarded: bool
) -> None:
    """Say at spawn time what tool surface the leg is actually getting.

    The whole point of resolving here is that a leg starting without the
    servers its instructions assume should be visible when it is launched
    rather than inferred from its output an hour later. A request for no
    servers is silent, and a forwarded set is reported as a hint. The global
    ``mcp.quiet_discovery`` preference can quiet the warning when the search
    from the launch directory found no configuration file.

    ``forwarded`` is the caller's answer to "does this spawn hand the resolved
    set to the leg?", read off the request that spawn built. This function must
    not re-derive it from the provider name: a second list here is what let the
    message contradict the spawn.
    """
    from lionhub.orchestration.console import warn

    if resolution.declared_empty:
        return  # --no-mcp-config or a named config listing no servers: chosen, not degraded

    if resolution.servers is not None:
        if forwarded:
            names = ", ".join(sorted(resolution.servers))
            hint(f"[mcp] {len(resolution.servers)} server(s) from {resolution.source}: {names}")
            return
        warn(
            f"MCP servers resolved from {resolution.source} are not carried to "
            f"provider {provider!r} on this spawn path. This leg gets whatever "
            "its own provider resolves for itself."
        )
        return

    if resolution.reason == "no_mcp_config_found":
        from lionagi._settings import load_global_settings_layer

        try:
            settings = load_global_settings_layer()
        except Exception:  # noqa: BLE001 -- a failed preference read preserves the warning
            settings = None
        mcp_settings = settings.get("mcp") if isinstance(settings, dict) else None
        if isinstance(mcp_settings, dict) and mcp_settings.get("quiet_discovery") is True:
            return

    target = cwd or os.getcwd()
    warn(
        f"no MCP servers are being handed to this leg ({resolution.reason}; searched "
        f"from {resolution.searched_from}). It will start with only whatever the "
        f"{provider} CLI discovers from {target} itself, which is where a leg pointed at "
        "a checkout silently loses them. Pass --mcp-config PATH, or --no-mcp-config "
        "to state that this is intended."
    )


"""Framing for a steer delivered as a continuation turn.

Deliberately mirrors the flow-side operator-steer vocabulary and, like it,
claims no authority to override: the operator's own words say what to change.
An earlier draft announced that the steer "supersedes conflicting parts of the
original instruction", and a live leg correctly refused it — a banner asserting
override authority reads exactly like injected content trying to redirect the
model away from what its user asked for. The channel already carries the
operator's authority (this is the same instruction slot the original prompt
arrived through), so the framing only has to say who is speaking and when.
"""
_AGENT_STEER_TEMPLATE = """\
[OPERATOR STEER]
The operator who started this run sent this while it was running. It is a live
correction to the task you are already working on, from the same person who
gave you that task — not a message from a third party. Attend to it before
continuing. Most recent last.
{lines}
[/OPERATOR STEER]
"""


async def _drain_pending_steers(
    live: dict | None,
    branch,
    *,
    operate_kwargs: dict,
    deadline: float | None,
    owner: str | None = None,
    run_turn=None,
) -> object | None:
    """Consume queued `message` session controls as warm continuation turns.

    Called after a successful operate() and before the run finalizes. Each
    drain batch joins all pending steers (arrival order) into one continuation
    turn on the same branch; steers enqueued during a continuation are caught
    by the next iteration. Continuations spend the leg's original wall clock:
    past *deadline* the loop stops without consuming (teardown tombstones the
    remainder), so steering can never keep a leg alive past its budget.

    *owner* names this leg on the rows it claims, so a concurrent leg and the
    teardown can both tell this leg's in-flight work from their own.

    *run_turn*, when given, runs each continuation as ``run_turn(instruction,
    timeout)`` instead of ``branch.operate``: an API-model leg's turn is its tool
    loop, not one model call.

    Returns the last continuation result, or None if nothing was consumed.
    """
    if not live or not live.get("session_id"):
        return None
    import time as _time

    from lionhub.orchestration.console import hint, log_error, warn

    db = live.get("db")
    if db is None:
        # A persistence context carrying a session but no handle to read it
        # with. Nothing can be drained, and returning quietly would make that
        # indistinguishable from "no steers were queued" -- so say which one
        # this is. setup_agent_persist always supplies both.
        log_error("steer: session is persisted but no database handle came with it; not draining")
        return None
    session_id = live["session_id"]
    last_res = None
    while True:
        if deadline is not None and _time.monotonic() >= deadline:
            break
        pending = await db.list_pending_session_controls(session_id)
        steers = [row for row in pending if row.get("verb") == "message"]
        if not steers:
            break
        if str(steers[0].get("result") or "").startswith("applying"):
            # A consumer stamped this row and did not finish. Re-applying it
            # could deliver the same operator message a second time, so it is
            # left untouched, and the rows behind it wait rather than jumping
            # it. This is the rule the flow poller already follows. It holds
            # whoever the claimant is, including this leg on a later pass:
            # a claim that outlived its apply is exactly the case where nobody
            # can say whether the message landed.
            break
        # The deadline was checked before the queue read above, and that read is
        # I/O that can cross it. Rechecking here, before anything is claimed or
        # sent, is what keeps the run inside the timeout the caller gave it.
        remaining = None
        if deadline is not None:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                break
        texts = []
        claimed = []
        for row in steers:
            # Carry the claim the database actually wrote rather than rebuilding
            # it here: the finalize below is only guarded if the two agree, and
            # two copies of the same expression are what stop agreeing.
            claim_token = await db.mark_session_control_applying(row["id"], owner=owner)
            if claim_token is None:
                # Another consumer claimed it between the read and here.
                continue
            claimed.append((row, claim_token))
            payload = row.get("payload") or {}
            texts.append(str(payload.get("text") or ""))
        if not claimed:
            break
        joined = "\n".join(f"- {t}" for t in texts if t.strip())
        hint(f"[steer] applying {len(claimed)} queued operator message(s) as a continuation turn")
        steer_instruction = _AGENT_STEER_TEMPLATE.format(lines=joined)
        if run_turn is not None:
            # The same budget rule as below: what is left, never a floor.
            last_res = await run_turn(steer_instruction, remaining)
        else:
            kwargs = dict(operate_kwargs)
            if remaining is not None:
                # What is actually left, never a floor. Flooring the budget would
                # hand the continuation a fresh second that the caller's deadline
                # has already spent.
                kwargs["timeout"] = remaining
            last_res = await branch.operate(instruction=steer_instruction, **kwargs)
        for row, claim_token in claimed:
            # Unconditional on the clock, deliberately. The deadline gates when
            # new provider work may start, which the recheck above enforces;
            # recording the outcome of work already performed is exempt, because
            # a skipped finalize would leave a delivered message on record as
            # undelivered. The claim token is the guard here instead: this write
            # lands only while the row still carries this leg's claim.
            stamped = await db.finalize_session_control(
                row["id"], result="applied", expect_claim=claim_token
            )
            if not stamped:
                # Somebody resolved the row while the continuation was running.
                # Their outcome stands, which is the point of the guard, but the
                # message was already delivered to the branch by then, so the
                # record now disagrees with what happened. Say so: the delivery
                # cannot be taken back, and a silent refusal here is the same
                # defect as the overwrite, one level up.
                warn(
                    f"operator message {row['id']} was delivered, but the control row "
                    f"was resolved by someone else first and now records their outcome "
                    f"instead of 'applied'. The message reached the agent."
                )
    return last_res


async def _tombstone_pending_steers(
    live: dict | None,
    *,
    result: str = (
        "rejected: run reached terminal status before the steer could land — use `li agent -r`"
    ),
) -> None:
    """Finalize never-claimed session controls as rejected at run teardown.

    *result* is the outcome recorded on each closed row; a flow run's teardown
    passes its own, since its controls are not steers.

    A steer enqueued while the run was live but never drained must not sit
    pending forever. Best-effort: a failure here logs and leaves the row
    visibly pending, since the status surface independently renders a pending
    control on a terminal run as never-landed.

    Only rows no consumer ever claimed are tombstoned, enforced at the write
    rather than read off the snapshot -- a claimed row belongs to whichever
    leg took it (still in its provider call, or dead between claim and apply),
    and rejecting it would assert non-delivery that nothing here actually
    knows, so it stays visible as claimed for an operator to resolve. Called
    after teardown, so the terminal check at the top is what makes this hold
    even when teardown failed to persist the transition it was asked for.
    """
    if not live or not live.get("session_id"):
        return
    try:
        from lionhub.state.db import SESSION_TERMINAL_STATUSES

        session = await live["db"].get_session(live["session_id"])
        if (session or {}).get("status") not in SESSION_TERMINAL_STATUSES:
            # The precondition this sweep's correctness rests on, asserted
            # rather than assumed. Rejecting a control on a session that is
            # still running would destroy a steer a live consumer was about to
            # take, and the sweep only closes the terminal race at all because
            # the transition it follows is what stops new controls being
            # admitted. Refusing here rather than sweeping keeps a call-site
            # ordering mistake from turning into a deleted operator message.
            log_error(
                "steer tombstone skipped: session "
                f"{str(live['session_id'])[:8]} is not terminal, so a pending "
                "control may still have a consumer"
            )
            return
        stale = await live["db"].list_pending_session_controls(live["session_id"])
        for row in stale:
            if row.get("result") is not None:
                continue
            # The snapshot above says the row was unclaimed when it was read,
            # which is not the same as unclaimed when this writes. Another leg
            # that read the row at its own turn boundary can claim it and hand
            # the steer to the model in between, so the guard has to travel with
            # the write: only_if_unclaimed makes the database re-check, and a
            # row that got claimed in the window is left alone rather than
            # recorded as never delivered.
            await live["db"].finalize_session_control(
                row["id"],
                result=result,
                only_if_unclaimed=True,
            )
    except Exception as exc:  # noqa: BLE001 — teardown must not raise
        log_error(f"steer tombstone write failed: {exc!r}")


def _admission_refusal(exc: BaseException) -> SubagentAdmissionRefusedError | None:
    """The admission refusal *exc* is or wraps: iModel re-raises a stream failure as a
    ``ValueError`` caused by the provider's own error."""
    cause: BaseException | None = exc
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        if isinstance(cause, SubagentAdmissionRefusedError):
            return cause
        seen.add(id(cause))
        cause = cause.__cause__
    return None


def _first_request_route_refusal(exc: BaseException, branch, usage) -> Exception | None:
    """The account router's refusal when *exc* is or wraps one that ended the run's first
    request before it left the process, else None.

    The router refuses a request with ``budget_exhausted`` or ``account_blocked`` when every
    configured account is out of budget or blocked, and the Branch re-raises the failed call
    as an ``ExecutionError`` caused by that refusal. The code alone does not say nothing was
    sent, because settling a response can also raise ``budget_exhausted``. So the refusal
    counts only when the run's usage record shows no request ever had an account selected
    and the branch holds no provider reply: no request reached the provider, and no earlier
    reply exists whose conversation would have to move with it.

    Whether a refusal that qualifies here moves the run is decided by
    ``_subscription_switch_objection``.
    """
    from lionagi.service.accounts._types import AccountRoutingError, RouterErrorCode

    refusal: AccountRoutingError | None = None
    cause: BaseException | None = exc
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        if isinstance(cause, AccountRoutingError):
            refusal = cause
            break
        seen.add(id(cause))
        cause = cause.__cause__
    if refusal is None or refusal.code not in (
        RouterErrorCode.BUDGET_EXHAUSTED,
        RouterErrorCode.ACCOUNT_BLOCKED,
    ):
        return None
    if usage is None or any(row["selected_attempts"] for row in usage.snapshot()["requests"]):
        return None
    if any(isinstance(m, AssistantResponse) for m in branch.msgs.messages):
        return None
    return refusal


async def _subscription_switch_objection(refusal, api_account: str | None) -> str | None:
    """The warning to print when a refused first request must not move the run to the
    subscription route, or None when it may move.

    A refusal moves the run only when it is about spend. A run pinned to an account with
    ``LIONAGI_API_ACCOUNT`` asked for that account and no other route, so it never moves.
    ``budget_exhausted`` is a spend refusal. ``account_blocked`` carries no reason of its
    own, so the ledger's status is read at the time of the refusal: the run moves only when
    at least one account is blocked and every blocked account is blocked for
    ``ceiling_exceeded``. A block for any other reason (a settlement conflict, an
    authentication failure, a block set by hand) is not a spend problem the subscription
    route solves, and a status that cannot be read, or that shows no blocked account, leaves
    the run where it is. The warning names accounts and block reasons only, never key
    material.
    """
    from lionagi.service.accounts._types import RouterErrorCode

    not_moving = "not moving to the subscription route because"
    if api_account is not None:
        return f"{not_moving} LIONAGI_API_ACCOUNT pins it to account {api_account}"
    if refusal.code == RouterErrorCode.BUDGET_EXHAUSTED:
        return None
    try:
        from lionagi.service.accounts._router import get_router

        statuses = await get_router().ledger.status("anthropic")
    except Exception as exc:  # noqa: BLE001 — a status that cannot be read ends the decision
        return f"{not_moving} the blocked accounts could not be read ({type(exc).__name__})"
    blocked = [(s.name, s.block_reason) for s in statuses if s.block_reason]
    if not blocked:
        return f"{not_moving} no account is reported blocked"
    other = [(name, reason) for name, reason in blocked if reason != "ceiling_exceeded"]
    if not other:
        return None
    names = ", ".join(sorted(name for name, _ in other))
    reasons = ", ".join(sorted({reason for _, reason in other}))
    return f"{not_moving} account(s) {names} are blocked for {reasons}"


def _read_api_account() -> str | None:
    """The account name in ``LIONAGI_API_ACCOUNT`` (ADR-0126 Amendment 4), None when it is unset.

    A value outside the routed-account name rule (including its ``sk-ant`` bar), one starting
    ``sk-``, or one equal to a key this process holds raises ``ConfigurationError`` without
    repeating it. A held key is the
    value of a credential-named or configured-account variable, in the environment or in the
    dotenv files the settings were built from.
    """
    api_account = os.environ.get(_API_ACCOUNT_ENV)
    if api_account is None:
        return None
    from pydantic_settings import DotEnvSettingsSource

    from lionagi import config
    from lionagi.libs.credential_fields import is_secret_field_name

    if (
        config._ACCOUNT_NAME_RE.fullmatch(api_account) is None
        or api_account.startswith("sk-")
        or config._looks_like_a_key(api_account)
    ):
        raise ConfigurationError(
            f"{_API_ACCOUNT_ENV} must be an account name: a lowercase letter, then up to 31 "
            "lowercase letters, digits, '_' or '-', not starting with 'sk-' and not containing "
            "'sk-ant'; the value is not shown"
        )
    settings = config.settings
    key_names = {account.key_env for account in settings.ANTHROPIC_ACCOUNTS}
    sources: list[Mapping[str, str | None]] = [os.environ]
    if settings._dotenv_files:
        try:
            sources.append(
                DotEnvSettingsSource(
                    config.AppSettings,
                    env_file=settings._dotenv_files,
                    env_file_encoding=settings.model_config.get("env_file_encoding"),
                    case_sensitive=True,
                ).env_vars
            )
        except Exception:  # noqa: BLE001 -- a parser error can quote the file
            raise ConfigurationError(
                f"{_API_ACCOUNT_ENV} could not be checked against the dotenv files; "
                "the value is not shown"
            ) from None
    for source in sources:
        for name, value in source.items():
            if (
                name != _API_ACCOUNT_ENV
                and value == api_account
                and (name in key_names or is_secret_field_name(name))
            ):
                raise ConfigurationError(
                    f"{_API_ACCOUNT_ENV} holds the value of {name}; it must name the account, "
                    "never carry its key; the value is not shown"
                )
    return api_account


def _attributed_resolution(
    resolution: McpResolution, agent_name: str | None, *, cwd: str | None
) -> McpResolution:
    """Attribute each khive server to the run's minted or profile actor."""
    return dataclasses.replace(
        resolution,
        servers=attribute_khive_servers(resolution.servers, run_khive_actor(agent_name), cwd=cwd),
    )


def _servers_as_read(resolution: McpResolution) -> dict[str, Any] | None:
    """A copy of the servers *resolution* read, detached from anything that later changes it."""
    if resolution.servers is None:
        return None
    return json.loads(json.dumps(resolution.servers))


def _write_mcp_config_copy(state_root: Path, servers: dict[str, Any] | None) -> Path:
    """Write the server set a named config handed this run into its run directory.

    The file is created, never replaced, and readable by its owner alone: a resume reads this
    copy rather than the named file, so the set it replays is the set this run was handed.
    """
    path = Path(state_root) / _MCP_CONFIG_COPY_NAME
    content = json.dumps({"mcpServers": servers or {}}, indent=2) + "\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    except OSError as exc:
        raise McpConfigError(
            f"could not copy the named MCP config into the run directory ({path}): "
            f"{exc.strerror or type(exc).__name__}"
        ) from None
    return path


#: Why a run whose profile declares ``scratch`` is refused on a provider other than a Claude Code
#: CLI child (ADR-0041 Amendment 6). Each names what is missing, not a preference.
_SCRATCH_REFUSAL_API = (
    "an API-model run's tools run under lionagi's own seatbelt policy, which has no scratch "
    "root yet"
)
_SCRATCH_REFUSAL_CODEX_BYPASS = (
    "bypass_approvals turns codex's sandbox off, so the scratch directory would not be the one "
    "place the child writes beyond its permission profile"
)
_SCRATCH_REFUSAL_CODEX_NO_SANDBOX = (
    "the child sandbox is switched off, so a codex child has no permission profile to add the "
    "scratch directory to"
)
_SCRATCH_REFUSAL_GEMINI = (
    "the Gemini CLI's sandbox does not bound reads and grants no single writable directory this "
    "launcher can verify"
)


def _hooks_from_spec(
    snapshot: str | None, branch: Any, is_resumed_branch: bool
) -> dict[str, Any] | None:
    """The hooks file copy this leg carries guards from, or None (ADR-0048 Amendment 1).

    A fresh leg reads it from ``--hooks-from-snapshot``, which the MCP server sets when a
    submission names ``hooks_from``. A resumed branch reads the one it started with from its
    metadata and takes no new one, so a resume can neither drop nor swap its guards.
    """
    if is_resumed_branch:
        if snapshot is not None:
            raise ConfigurationError(
                "--hooks-from-snapshot cannot be given to a resumed branch: a resumed run keeps "
                "the guards it started with"
            )
        spec: Any = branch.metadata.get(_HOOKS_FROM_KEY)
        if spec is None:
            return None
        where = "the branch's hooks_from record"
    else:
        if snapshot is None:
            return None
        where = "--hooks-from-snapshot"
        try:
            spec = json.loads(snapshot)
        except ValueError:
            raise ConfigurationError(f"{where} is not valid JSON") from None
    if (
        not isinstance(spec, dict)
        or not all(
            isinstance(spec.get(field), str)
            for field in _HOOKS_FROM_SPEC_FIELDS
            if field != "carry"
        )
        or not isinstance(spec.get("carry"), list)
    ):
        raise ConfigurationError(f"{where} must name {', '.join(_HOOKS_FROM_SPEC_FIELDS)}")
    return {field: spec[field] for field in _HOOKS_FROM_SPEC_FIELDS}


def _apply_hooks_from(spec: dict[str, Any], branch: Any, cwd: str | None) -> list[str]:
    """Check this leg's hooks file copy and apply what it carries.

    The copy must hash to the digest recorded at submit (H2 rule 6). It is planned again for
    this process's working directory, and a guard no model of this leg can apply refuses the
    leg before the run exists (H3, H5). Deny rules go to the leg's Claude Code models; hook
    entries are wired onto an API-model leg's branch after each command passes the trust and
    writable-directory checks again from this directory (H4). Returns the place in the file of
    each hook entry wired, which ``require_hooks`` counts (ADR-0048 Amendment 2 R3).
    """
    from lionhub.agent.factory import _wire_external_hook_entries
    from lionhub.agent.subscription_accounts import account_provider
    from lionhub.hooks.hooks_from import (
        HooksFromError,
        carry_refusal,
        hook_command_refusals,
        hook_entries,
        plan_carry,
        read_snapshot,
        writable_roots,
    )

    leg_cwd = os.path.abspath(cwd) if cwd else os.getcwd()
    try:
        snapshot = read_snapshot(spec["copy"], spec["sha256"])
        plan = plan_carry(
            snapshot.document,
            fmt=spec["format"],
            carry=spec["carry"],
            source_path=spec["source"],
            leg_cwd=leg_cwd,
        )
    except HooksFromError as exc:
        raise ConfigurationError(f"cannot start this leg with hooks_from: {exc}") from exc
    present = [m for m in (branch.chat_model, branch.parse_model) if m is not None]
    models = [m for m in present if m.is_cli]
    claude_code = branch.chat_model in models and all(
        account_provider(m.endpoint.config.provider or "") == "claude_code" for m in models
    )
    api = bool(present) and not models
    reason = carry_refusal(plan, claude_code=claude_code, api=api)
    if reason is not None:
        raise ConfigurationError(f"cannot start this leg with hooks_from: {reason}")
    if plan.hooks:
        refused = hook_command_refusals(plan, leg_cwd=leg_cwd, writable=writable_roots(leg_cwd))
        if refused:
            listed = "; ".join(f"{where}: {why}" for where, why in refused)
            raise ConfigurationError(
                f"cannot start this leg with hooks_from: {len(refused)} hook command(s) "
                f"cannot run: {listed}"
            )
    wired = hook_entries(plan) if plan.hooks else []
    if wired:
        _wire_external_hook_entries(branch, wired, cwd=leg_cwd)
    if plan.deny_rules:
        for model in models:
            model.endpoint.config.kwargs["carried_deny"] = list(plan.deny_rules)
    return [entry["where"] for entry in wired]


def _scratch_refusal(
    provider: str,
    *,
    api_launch: bool,
    is_cli: bool,
    endpoint_kwargs: Mapping[str, Any] | None = None,
) -> str | None:
    """Why a run on *provider* cannot have a scratch directory, or None when it can.

    For codex this is what can be known before the run exists: ``bypass_approvals`` in the
    endpoint's settings, or the child sandbox switched off. What depends on the scratch path
    itself (a denied path or the probe record directory over it, a read-only temp directory
    holding it, a link in its place) and the probe record's ``scratch_env`` arm are refused when
    the codex command line is built, after the run directory exists.
    """
    if api_launch or not is_cli:
        return _SCRATCH_REFUSAL_API
    if provider == "claude_code":
        return None
    if provider == "codex":
        if (endpoint_kwargs or {}).get("bypass_approvals") is True:
            return _SCRATCH_REFUSAL_CODEX_BYPASS
        from lionhub.agent.child_sandbox import resolve_child_sandbox

        if not resolve_child_sandbox(None).enabled:
            return _SCRATCH_REFUSAL_CODEX_NO_SANDBOX
        return None
    if provider == "gemini_code":
        return _SCRATCH_REFUSAL_GEMINI
    return f"the {provider or 'unknown'} provider has no scratch directory support"


def _finish_run_scratch(scratch: Any) -> dict[str, Any]:
    """End a run's scratch directory and return its run.json entry; nothing it raises escapes.

    The removal walks a tree the child wrote, and it runs first in the run's shielded teardown,
    ahead of the final reply, the terminal notice and the model shutdown that teardown exists to
    reach. Whatever leaves it becomes the entry's ``removal_error``, and ``removed`` says whether
    the directory is still there.
    """
    try:
        return scratch.finish()
    except Exception as exc:  # noqa: BLE001 — see the docstring
        return {
            **scratch.record(),
            "removed": not os.path.lexists(scratch.path),
            "removal_error": f"scratch removal raised {type(exc).__name__}: {exc}",
        }


def _bind_run_scratch(chat_model: Any, scratch: Any) -> None:
    """Hand the run's scratch directory to its CLI endpoint, or take away one left from before.

    The path lives in the endpoint's runtime state, never in its config, so no saved branch or
    run record carries it into a later run.
    """
    endpoint = chat_model.endpoint
    state = getattr(endpoint, "_runtime_state", None)
    takes_it = "scratch_dir" in getattr(endpoint, "_runtime_state_fields", ())
    if scratch is None:
        if isinstance(state, dict):
            state.pop("scratch_dir", None)
        return
    if not takes_it or not isinstance(state, dict):
        raise ConfigurationError(
            "this run's endpoint cannot take a scratch directory, so the run is not started"
        )
    state["scratch_dir"] = str(scratch.path)


async def _run_agent(
    model_str: str | None,
    prompt: str,
    yolo: bool = False,
    verbose: bool = False,
    theme: str | None = None,
    resume: str | None = None,
    continue_last: bool = False,
    effort: str | None = None,
    agent_name: str | None = None,
    cwd: str | None = None,
    timeout: int | None = None,
    fast: bool = False,
    invocation_id: str | None = None,
    project: str | None = None,
    bypass: bool = False,
    preset: str | None = None,
    resume_on_timeout: bool = False,
    context_from: list[str] | None = None,
    context_budget: int | None = None,
    notify: str | None = None,
    images: list[str] | None = None,
    mcp_config: str | None = None,
    no_mcp_config: bool = False,
    _auto_resumed: bool = False,
    _injection_totals: dict[str, int] | None = None,
    # Last, so every parameter before them keeps the position it had
    idle_timeout: int | None = None,
    workload: str | None = None,
    *,
    notify_sender: str | None = None,
    mcp_submit_snapshot: str | None = None,
    hooks_from_snapshot: str | None = None,
    account: str | None = None,
    _parent_marker: Callable[[], None] | None = None,
    _lost_write_out: dict[str, Any] | None = None,
    _worker_spawn_continuation: object | None = None,
    _job_result_run: RunDir | None = None,
) -> tuple[str, str, str, str, str | None]:
    """Execute one agent turn; returns (result, provider, branch_id, terminal_status, session_id).

    session_id is None whenever live persistence never started. An auto-resumed leg runs in a
    run directory of its own while the job reads the first run's run.json, so the first run hands
    it *_parent_marker*, which records a signal persistence skip window on that run.json too, and
    *_lost_write_out*, where the leg leaves a terminal write its store refused. *_job_result_run*
    names the original MCP run directory where the last leg publishes its final reply.
    """
    async with AsyncExitStack() as _worker_spawn_bindings:
        _worker_spawn_bindings.enter_context(_bind_cli_worker_recorder(None))
        if _auto_resumed and _worker_spawn_continuation is None:
            raise _WorkerSpawnAdmissionError("automatic continuation requires an execution handoff")
        prompt = _apply_detached_execution_boundary(prompt)
        seed_injection_totals = _injection_totals is None
        _injection_totals = (
            dict.fromkeys(_KHIVE_INJECTION_COUNTERS, 0)
            if _injection_totals is None
            else dict(_injection_totals)
        )
        effort = normalize_effort(effort)
        effort_origin = "explicit" if effort is not None else "default"
        model_origin = "explicit" if model_str is not None else "default"
        # Fail fast before any run is allocated: a nonexistent --cwd must not spawn
        # into a provider-created dir. Forward the tilde-expanded path — providers
        # never expand `~`.
        cwd = validate_cwd_exists(cwd)
        # Read from *this* process's directory, before --cwd is applied to anything.
        # The child's tool surface is meant to be a property of the submission, and
        # this is the only point at which the submitting directory is still known.
        launch_dir = Path(os.getcwd()).resolve()
        if resume and continue_last:
            raise ConfigurationError(
                "--resume / -r and --continue-last / -c are mutually exclusive."
            )
        if idle_timeout is not None and idle_timeout <= 0:
            raise ConfigurationError(
                f"--idle-timeout must be a positive number of seconds, not {idle_timeout}; "
                "the LIONAGI_WORKER_IDLE_TIMEOUT setting is the switch that turns the watchdog off."
            )
        if idle_timeout is not None and idle_timeout > MAX_TIMEOUT_SECONDS:
            raise ConfigurationError(
                f"--idle-timeout must be at most {MAX_TIMEOUT_SECONDS} seconds, not {idle_timeout}."
            )
        # Refused before anything is written, so a refused run leaves no run.json.
        api_account = _read_api_account()
        if preset and (resume or continue_last):
            raise ConfigurationError(
                "--preset only applies to new branches; cannot combine with --resume / --continue-last."
            )
        if context_from and (resume or continue_last):
            raise ContextFromError(
                "--context-from cannot be combined with --resume / -r or --continue-last / -c "
                "(resume already carries the source context)."
            )
        if context_from:
            effective_context_budget = (
                context_budget if context_budget is not None else DEFAULT_CONTEXT_BUDGET_TOKENS
            )
            context_block = await resolve_and_build_context_block(
                context_from, effective_context_budget
            )
            if context_block:
                prompt = f"{context_block}\n\n{prompt}"

        # Cache cancellation exception class while event loop is running;
        # cancelled_exc_classes() in the error path needs it after loop exit.
        try:
            cache_cancelled_exc_class()
        except Exception as _cache_err:
            import logging as _logging

            _logging.getLogger("lionagi.cli").debug(
                "cache_cancelled_exc_class() failed (non-fatal): %s", _cache_err
            )

        profile = None
        if agent_name:
            profile = load_agent_profile(agent_name)
            if profile.model and model_str is None:
                model_str = profile.model
                model_origin = "profile"
            if profile.effort and effort is None:
                effort = normalize_effort(profile.effort)
                effort_origin = "profile"
            if profile.yolo and not yolo:
                yolo = True
            if profile.bypass and not bypass:
                bypass = True
            if profile.fast_mode and not fast:
                fast = True
            if profile.timeout and timeout is None:
                timeout = profile.timeout
            if profile.idle_timeout and idle_timeout is None:
                idle_timeout = profile.idle_timeout
            if profile.resume_on_timeout and not resume_on_timeout:
                resume_on_timeout = True
            if (getattr(profile, "extra", None) or {}).get("hooks"):
                # A saved guard the leg silently does not run is worse than no
                # guard: say so at launch until a run consumes the assembly.
                from lionhub.orchestration.console import warn

                warn(
                    f"agent profile {agent_name!r} declares a hooks assembly; "
                    "no run applies a profile's hooks assembly yet (the Studio "
                    "Operator applies only its own, from operator_hooks.json), so "
                    "those hooks are NOT active for this run"
                )

        # The line's --account wins over the role's account or codex_home.
        if account is not None:
            requested_account: dict[str, str] | None = {"account": account}
        elif getattr(profile, "account", None) is not None:
            requested_account = {"account": profile.account}
        elif getattr(profile, "codex_home", None) is not None:
            requested_account = {"codex_home": profile.codex_home}
        else:
            requested_account = None
        leg_account = None
        account_switches: list[dict[str, Any]] = []
        account_switched_from: dict[str, Any] | None = None

        profile_disables_mcp = getattr(profile, "mcp", None) == "none"
        if mcp_config is not None and mcp_submit_snapshot is not None:
            raise McpConfigError("--mcp-config cannot be combined with --mcp-submit-snapshot")
        if profile_disables_mcp and mcp_config is not None:
            raise McpConfigError(
                f"agent profile {agent_name!r} declares mcp: none; "
                "it cannot be combined with --mcp-config"
            )
        if profile_disables_mcp:
            no_mcp_config = True

        account_connectors, contain_account_integrations = resolve_account_integrations(profile)
        # A role's tool set is checked here, before any run is allocated; whether the agent's
        # model can take it is decided where the request is built.
        tool_settings = _profile_tool_settings(profile)
        # The flag wins over the role; with neither the leg is heavy, which keeps its configured
        # sandbox and counts against both machine-wide caps.
        if workload is None:
            workload = getattr(profile, "workload", None) or DEFAULT_WORKLOAD

        # Validate a declared profile `role:` key up front: a falsy-but-present
        # value must fail loudly here, not silently fall back to "implementer".
        profile_role_extra = (getattr(profile, "extra", None) or {}) if profile else {}
        has_role_key = "role" in profile_role_extra
        profile_role = profile_role_extra.get("role") if has_role_key else None
        if has_role_key and (not isinstance(profile_role, str) or not profile_role.strip()):
            raise ConfigurationError(
                f"agent profile {getattr(profile, 'name', '<unknown>')!r} declares a "
                f"`role` key but its value {profile_role!r} is not a non-empty "
                "string; set it to a valid role name, or remove the key to keep "
                "the plain profile path (no role/policy composition)."
            )

        # A profile's loop must name the read set it loops over, whatever the
        # provider or the branch: a CLI provider runs its own loop and a branch
        # without a stored record runs as before, so `api_loop` there would be
        # silently ignored. Without `api_tools` the call below only refuses that, and
        # it runs before any run is allocated.
        profile_api_extra = getattr(profile, "extra", None) or {}
        if "api_tools" not in profile_api_extra or any(
            key in profile_api_extra for key in _API_POLICY_KEYS
        ):
            _profile_api_launch(profile)

        stamp_agent_depth(agent_name)

        # True only when a NEW branch took the create_agent path (--preset coding
        # or an opted-in profile `role:` key) — see the add_message guard below.
        took_create_agent_path = False

        branch: Branch | None = None
        if continue_last:
            _, branch_id = load_last_branch()
            _, branch_path = find_branch(branch_id)
            branch = Branch.from_dict(json.loads(branch_path.read_text()))
        elif resume:
            branch_path = None
            if _MCP_AGENT_RUN_ID.fullmatch(resume):
                from lionhub.serve.mcp.jobs import _load_saved_agent_branch

                try:
                    expected_branch_id, branch_data = _load_saved_agent_branch(resume)
                except _SubscriptionFallbackResumeError as exc:
                    raise ConfigurationError(str(exc)) from None
                except (OSError, RuntimeError, ValueError):
                    raise ConfigurationError(
                        "That run does not have a readable saved agent branch in this home."
                    ) from None
            else:
                try:
                    canonical = str(UUID(resume))
                except ValueError:
                    canonical = None
                expected_branch_id = canonical if canonical == resume.lower() else None
                _, branch_path = find_branch(expected_branch_id or resume)
                branch_data = json.loads(branch_path.read_text())
            branch = Branch.from_dict(branch_data)
            if expected_branch_id is not None and str(branch.id) != expected_branch_id:
                await branch.mdls.shutdown()
                raise ConfigurationError(
                    "The saved branch ID does not match the requested branch ID."
                )
            if branch_path is not None and branch_path.stem != (expected_branch_id or resume):
                hint(f"[resume] prefix-matched {resume} → {branch_path.stem}")

        # Capture before the new-branch block below reassigns `branch`: the only
        # reliable "reopened existing" vs "minting new" signal once that block runs.
        is_resumed_branch = branch is not None

        if branch is not None and (
            reason := _subscription_fallback_resume_refusal(branch.metadata)
        ):
            raise ConfigurationError(reason)

        if is_resumed_branch and mcp_config is not None:
            launched = branch.metadata.get(_MCP_LAUNCH_KEY)
            if (
                isinstance(launched, dict)
                and launched.get("no_mcp_config") is True
                and launched.get("no_mcp_config_source") == "profile"
            ):
                raise McpConfigError(
                    "this branch was launched with profile mcp: none; "
                    "it cannot be resumed with --mcp-config"
                )

        replay_recorded_choice = is_resumed_branch and mcp_config is None and not no_mcp_config
        recorded_launch = branch.metadata.get(_MCP_LAUNCH_KEY) if replay_recorded_choice else None
        if replay_recorded_choice:
            if isinstance(recorded_launch, dict):
                origin = recorded_launch.get("mcp_config_origin")
                if origin is not None and (
                    origin not in ("submit_snapshot", "explicit")
                    or not isinstance(recorded_launch.get("mcp_config"), str)
                    or not recorded_launch["mcp_config"].strip()
                    or not os.path.isabs(recorded_launch["mcp_config"])
                    or not isinstance(recorded_launch.get("launch_dir"), str)
                    or not recorded_launch["launch_dir"].strip()
                    or recorded_launch.get("no_mcp_config") is not False
                ):
                    raise McpConfigError("this branch has an invalid recorded MCP snapshot origin")
                if _MCP_CONFIG_NAMED_KEY in recorded_launch and (
                    origin != "explicit"
                    or not isinstance(recorded_launch[_MCP_CONFIG_NAMED_KEY], str)
                    or not os.path.isabs(recorded_launch[_MCP_CONFIG_NAMED_KEY])
                ):
                    raise McpConfigError(
                        "this branch has an invalid record of the MCP config it was named; "
                        "start a fresh run"
                    )
                if (
                    "no_mcp_config" in recorded_launch
                    and type(recorded_launch["no_mcp_config"]) is not bool
                ):
                    raise McpConfigError(
                        "this branch has an invalid recorded MCP configuration flag; start a fresh run"
                    )
                launch = recorded_launch.get("launch_dir")
                has_recorded_path = (
                    recorded_launch.get("mcp_config") is not None or "launch_dir" in recorded_launch
                )
                if has_recorded_path and (not isinstance(launch, str) or not os.path.isabs(launch)):
                    raise McpConfigError(
                        "this branch has an invalid recorded MCP launch directory; start a fresh run"
                    )

        # A pre-carrier record naming a file is ambiguous until that file has
        # been read. Do not discover the current directory before deciding it.
        legacy_recorded_file = (
            isinstance(recorded_launch, dict)
            and recorded_launch.get("mcp_config_origin") is None
            and recorded_launch.get("mcp_config") is not None
            and not bool(recorded_launch.get("no_mcp_config"))
        )
        if legacy_recorded_file and (
            not isinstance(recorded_launch.get("launch_dir"), str)
            or not recorded_launch["launch_dir"].strip()
        ):
            raise McpConfigError(
                f"this branch has an invalid recorded MCP config "
                f"{_format_recorded_mcp_path(recorded_launch['mcp_config'])}; start a fresh run"
            )
        # The servers a named config handed this run, as read from it and before the run's own
        # actor is attributed to them: what the run directory's copy holds (ADR-0041 Amendment 8).
        handed_servers: dict[str, Any] | None = None
        if not replay_recorded_choice:
            mcp_resolution = resolve_spawn_mcp_servers(
                mcp_config,
                launch_dir=launch_dir,
                disabled=no_mcp_config,
                submit_snapshot=mcp_submit_snapshot if not is_resumed_branch else None,
            )
            handed_servers = _servers_as_read(mcp_resolution)
            mcp_resolution = _attributed_resolution(mcp_resolution, agent_name, cwd=cwd)

        # A resume that names no MCP flag replays the choice its branch was launched
        # with, searched from where it was launched rather than from where it is
        # resumed. A branch from before that choice was recorded resolves nothing
        # new, so its request keeps the servers its earlier run was handed.
        if is_resumed_branch and mcp_config is None and not no_mcp_config:
            launched = branch.metadata.get(_MCP_LAUNCH_KEY)
            if isinstance(launched, dict) and isinstance(launched.get("launch_dir"), str):
                recorded_config = launched.get("mcp_config")
                disabled = bool(launched.get("no_mcp_config"))
                no_mcp_config = disabled
                # The resolver's own refusals name --mcp-config, which this caller
                # never passed, so a recorded config is refused here in its own words.
                how_to_go_on = (
                    "start a fresh run"
                    if legacy_recorded_file
                    else "resume it with --mcp-config naming the config to use, or with --no-mcp-config"
                )
                if recorded_config is not None and not disabled:
                    try:
                        recorded_path = explicit_mcp_config_path(
                            recorded_config, launch_dir=launched["launch_dir"]
                        )
                    except (TypeError, RuntimeError, ValueError, OSError):
                        # Stored fields may contain arbitrary values: do not echo an
                        # object repr or an exception built from those values.
                        raise McpConfigError(
                            f"this branch was launched with the MCP config "
                            f"{_format_recorded_mcp_path(recorded_config)}, "
                            f"which can no longer be used; {how_to_go_on}"
                        ) from None
                    if not recorded_path.is_file():
                        raise McpConfigError(
                            f"this branch was launched with the MCP config "
                            f"{_format_recorded_mcp_path(recorded_config)}, "
                            f"which is no longer a readable file; {how_to_go_on}"
                        )
                try:
                    is_submit_snapshot = launched.get("mcp_config_origin") == "submit_snapshot"
                    mcp_resolution = resolve_spawn_mcp_servers(
                        None if is_submit_snapshot else recorded_config,
                        launch_dir=launched["launch_dir"],
                        disabled=disabled,
                        submit_snapshot=recorded_config if is_submit_snapshot else None,
                        **({"reject_legacy_snapshot": True} if legacy_recorded_file else {}),
                    )
                    handed_servers = _servers_as_read(mcp_resolution)
                    mcp_resolution = _attributed_resolution(mcp_resolution, agent_name, cwd=cwd)
                except LegacyMcpReplayError as exc:
                    # The resolver constructed this fixed, safely-labelled message.
                    raise McpConfigError(str(exc)) from None
                except McpConfigError:
                    # Do not expose arbitrary stored metadata or exception content.
                    raise McpConfigError(
                        f"this branch was launched with the MCP config "
                        f"{_format_recorded_mcp_path(recorded_config)}, "
                        f"which can no longer be used; {how_to_go_on}"
                    ) from None
            else:
                mcp_resolution = McpResolution(
                    None, "no_mcp_launch_recorded", None, Path(os.getcwd()).resolve()
                )

        if model_str is not None:
            from lionhub.agent.factory import _parse_agent_model_spec

            ms = _parse_agent_model_spec(model_str, origin=model_origin)
            if branch is not None and "/" not in ms.model and ms.model not in BACKENDS:
                # A bare token that isn't a known backend name is almost always a
                # mangled command (e.g. a --resume id split across two argv tokens).
                log_error(
                    f"resume model override {model_str!r} does not look like a "
                    "model spec (expected 'provider/model', or a known name "
                    "like 'claude', 'codex', 'gemini-code'). Positionals are "
                    "[MODEL] PROMPT — this looks like a mangled command, e.g. "
                    "a --resume id accidentally split across two arguments."
                )
                return "", "", str(branch.id), "failed", None
            if "/" in ms.model:
                provider, model = ms.model.split("/", 1)
            else:
                provider, model = ms.model, ms.model
            if ms.effort and not effort:
                effort = ms.effort
                effort_origin = "profile" if model_origin == "profile" else "explicit"
        elif branch is not None:
            ep_cfg = branch.chat_model.endpoint.config
            provider = ep_cfg.provider
            model = ep_cfg.kwargs.get("model")
        else:
            raise ValueError(
                "Provide a model spec (e.g. 'claude') for a new branch, "
                "or use --resume / --continue-last to reopen an existing one."
            )

        from lionhub.agent.factory import _canonical_provider, _reject_unforwardable_explicit_mcp

        try:
            _reject_unforwardable_explicit_mcp(
                provider,
                named_explicitly=mcp_resolution.explicit,
                asked_for_servers=bool(mcp_resolution.servers),
            )
        except ConfigurationError as exc:
            if not mcp_resolution.snapshot_marker:
                raise
            raise ConfigurationError(
                f"{exc} This file looks like a submit snapshot; an MCP server started "
                "before this version must be restarted."
            ) from None
        # Settings are looked up under the provider the name resolves to, so an alias such as
        # "openai-codex" is clamped and flagged as codex; `provider` stays as written.
        canonical = _canonical_provider(provider)

        # A require_hooks refusal is held until the run is under way and raised there, so the
        # job ends failed with the refusal's text and no provider child is started
        # (ADR-0048 Amendment 2 R3).
        _require_hooks_refusal: RequireHooksRefusedError | None = None
        _requires_hooks = bool(getattr(profile, "require_hooks", False))
        if branch is None:
            # Codex blocks tool calls until file access is enabled. Surface this
            # even without verbose output; CLI or profile approval flags suppress it.
            if canonical == "codex" and not yolo and not bypass:
                from lionhub.orchestration.console import warn

                message = (
                    "codex models need file access enabled or the agent may hang "
                    "silently on its first tool call."
                )
                if profile is None:
                    message += (
                        " Re-run with --yolo for the sandboxed default, or use an agent "
                        "profile (-a). --bypass also works but disables the sandbox."
                    )
                else:
                    message += " Set yolo: true in the agent profile for sandboxed file access."
                warn(message)
            if requested_account is not None:
                from lionhub.agent.subscription_accounts import resolve_leg_account

                # Read now, before any run is allocated: a credential that cannot be read
                # refuses the launch, with no fallback to this machine's own login.
                leg_account = resolve_leg_account(provider, **requested_account)
            chat_model = build_chat_model(
                provider,
                model,
                yolo,
                verbose,
                theme,
                effort,
                fast,
                bypass,
                mcp_servers=mcp_resolution.servers,
                mcp_exclusive=mcp_resolution.exclusive,
                account_connectors=account_connectors,
                contain_account_integrations=contain_account_integrations,
                workload=workload,
                tool_settings=tool_settings,
                env=leg_account.env if leg_account is not None else None,
                secret_fill_skip=leg_account.secret_fill_skip if leg_account is not None else None,
                require_hooks=bool(getattr(profile, "require_hooks", False)),
            )
            if leg_account is not None:
                from lionhub.agent.subscription_accounts import leg_account_reached

                if isinstance(chat_model, str) or not leg_account_reached(
                    leg_account, getattr(chat_model.endpoint, "_runtime_state", None)
                ):
                    raise ConfigurationError(
                        f"the {provider!r} model was built without the account's environment "
                        "or its secret-lookup skip set, so the leg would not run on the "
                        "account alone."
                    )
            requested_effort = effort
            effort = resolve_persisted_effort(provider, chat_model, effort)
            if not isinstance(chat_model, str) and not chat_model.is_cli:
                # build_chat_model's options are CLI options, and the model it builds
                # for them carries a placeholder key; an API model is its spec alone.
                chat_model = f"{provider}/{model}"
            # A bare spec string is what the branch would turn into a model anyway; it is
            # turned here so the provider's kind is known before the spawn shape is chosen.
            if isinstance(chat_model, str):
                from lionagi.service.imodel import iModel

                chat_model = iModel(model=chat_model)
            # A codex request carries the key and refuses at its own launch; the rest is
            # decided here, once the model's kind is known (ADR-0048 Amendment 2).
            from lionhub.orchestration.spawn import refuse_require_hooks

            try:
                refuse_require_hooks(agent_name, profile, chat_model)
            except RequireHooksRefusedError as exc:
                _require_hooks_refusal = exc
            if not chat_model.is_cli and effort is not None:
                from lionhub.agent.factory import _set_api_effort

                if not _set_api_effort(
                    chat_model.endpoint.config.kwargs,
                    canonical,
                    model,
                    effort,
                    origin=effort_origin,
                ):
                    effort = None
            elif not chat_model.is_cli and requested_effort is not None:
                from lionhub.agent.factory import _set_api_effort

                if _set_api_effort(
                    chat_model.endpoint.config.kwargs,
                    canonical,
                    model,
                    requested_effort,
                    origin=effort_origin,
                ):
                    effort = requested_effort
            # An API model runs lionagi's own tool loop, only for a profile that declares it.
            api_launch = None if chat_model.is_cli else _profile_api_launch(profile)
            if api_launch is not None and preset == "coding":
                raise ConfigurationError(
                    "--preset coding binds tools that write files and run commands; an API "
                    "model runs only with its profile's tool set (api_tools: read, or write "
                    "under the macOS sandbox)."
                )
            if api_launch is not None and images:
                raise ConfigurationError("--image is not supported for an API model yet.")
            # Two spawn shapes hand the set over in two different places, so the
            # message has to read the one that applies. A plain leg gets only what
            # build_chat_model already put on the request, which is knowable here;
            # a create_agent leg is handed the set inside create_agent, so its
            # message waits for the request that call produces (below).
            takes_create_agent_path = preset == "coding" or has_role_key or api_launch is not None
            if not takes_create_agent_path:
                from lionhub.agent.factory import request_kwargs_carry_forwarded_mcp

                # Read the request build_chat_model produced: the two transports
                # put the set in different places, and a provider name is what got
                # this wrong before.
                built_config = getattr(getattr(chat_model, "endpoint", None), "config", None)
                forwarded = request_kwargs_carry_forwarded_mcp(
                    getattr(built_config, "kwargs", None)
                )
                _report_mcp_resolution(
                    mcp_resolution, provider=provider, cwd=cwd, forwarded=forwarded
                )

            # Opt-in profile `role:` key switches a plain `-a <profile>` leg onto
            # the same create_agent path as --preset coding, parameterized by role.
            if takes_create_agent_path:
                took_create_agent_path = True
                # Use create_agent so CodingToolkit tools and path-guards are wired;
                # compose the profile extension into the spec before calling it.
                from lionhub.agent.factory import (
                    create_agent,
                    request_carries_forwarded_mcp,
                )

                # Use profile.raw_body, not profile.system_prompt, to avoid
                # duplicating LION_SYSTEM_MESSAGE (see docs/internals/cli.md).
                profile_extra = (getattr(profile, "raw_body", None) or "") if profile else ""
                if api_launch is not None:
                    command_names = None
                    if api_launch["tools"] in _API_CONFINED_TOOL_SETS:
                        from lionhub.tools.command_env_metadata import configured_command_env_names

                        active_reference = chat_model.endpoint.config.api_key_env
                        command_names = configured_command_env_names(
                            active_reference_names=(active_reference,)
                            if active_reference is not None
                            else (),
                        )
                    spec = _make_api_agent_spec(
                        cwd=cwd,
                        effort=effort,
                        system_prompt=profile_extra or None,
                        role=profile_role if has_role_key else "reviewer",
                        tools=api_launch["tools"],
                        network=api_launch["network"],
                        _command_env_names=command_names,
                        profile_deny_read=tuple(getattr(profile, "deny_read", ()) or ()),
                    )
                else:
                    spec = _make_coding_preset(
                        cwd=cwd,
                        effort=effort,
                        system_prompt=profile_extra or None,
                        role=profile_role if has_role_key else "implementer",
                    )
                # The CLI already prepared the request with the resolved effort.
                spec._effort_origin = effort_origin
                spec._effort_in_supplied_model = True
                if profile is not None:
                    spec.khive_injection = getattr(profile, "khive_injection", None)
                    if api_launch is not None:
                        spec.khive_injection = _recall_only(spec.khive_injection)
                # AgentSpec.coding()/compose() default lion_system=True regardless
                # of the profile's frontmatter — propagate an explicit opt-out.
                if profile is not None and not profile.lion_system:
                    spec.lion_system = False
                # Hand over the set resolved from the submitting directory. Without
                # it the factory looks for a config of its own, and a leg pointed
                # at a checkout gets whatever is found near the target instead of
                # what this command resolved and reported.
                branch = await create_agent(
                    spec,
                    chat_model=chat_model,
                    log_config=DataLoggerConfig(auto_save_on_exit=False),
                    load_settings=False,
                    # An API-model leg is handed no MCP server: its tools are the read set.
                    resolved_mcp_servers=(
                        {}
                        if api_launch is not None or mcp_resolution.declared_empty
                        else mcp_resolution.servers
                    ),
                    resolved_mcp_explicit=mcp_resolution.explicit,
                    # The child sandbox exemption is keyed on the agent profile file
                    # named by -a, not on the role the spec casts the leg as.
                    sandbox_profile=agent_name or None,
                    # The pre-turn khive client is given the actor the khive
                    # servers handed to the child above are given.
                    khive_actor=run_khive_actor(agent_name),
                )
                if api_launch is not None:
                    branch.metadata[_API_LAUNCH_KEY] = dict(api_launch)
                    confined = ""
                    if spec.seatbelt is not None:
                        confined = (
                            "; editor and bash run under the macOS sandbox, writing only to "
                            f"{', '.join(spec.seatbelt.writable_roots)}, network "
                            f"{'on' if spec.seatbelt.network else 'off'}"
                        )
                        if spec.seatbelt.deny_read:
                            confined += (
                                f", {len(spec.seatbelt.deny_read)} credential and store "
                                "locations unreadable"
                            )
                    hint(
                        f"[api] {provider} runs lionagi's {api_launch['loop']} tool loop with the "
                        f"{api_launch['tools']} set ({', '.join(_API_TOOL_SETS[api_launch['tools']])}), "
                        f"up to {api_launch['max_extensions']} more round(s); no MCP server is handed "
                        f"to it{confined}"
                    )
                else:
                    # The hand-over happened inside create_agent, so the request it
                    # produced is the only honest source for what this leg is getting.
                    _report_mcp_resolution(
                        mcp_resolution,
                        provider=provider,
                        cwd=cwd,
                        forwarded=request_carries_forwarded_mcp(branch),
                    )
            else:
                # Built here rather than by create_agent, so the profile's sandbox policy is
                # set here too.
                from lionhub.serve.cli.orchestrate._orchestration import _confine_claude_worker

                _confine_claude_worker(chat_model, agent_name if profile is not None else None)
                branch = Branch(
                    chat_model=chat_model,
                    log_config=DataLoggerConfig(auto_save_on_exit=False),
                )
                # A bare `-a <profile>` leg still honors the profile's khive_injection
                # opt-in (a context-provider concern, independent of the coding preset),
                # keyed on `{agent_name}-recall-v1` to match the orchestrate path. Pass
                # agent_name (guaranteed non-empty here), NOT profile.name — the latter's
                # eager attribute access fires before the helper's guard can early-return.
                if profile is not None:
                    from lionhub.agent.factory import register_profile_injection

                    # The CLI child starts in --cwd, which is passed per call rather
                    # than set on the model, so the injection server is told directly.
                    register_profile_injection(
                        branch, agent_name, profile, cwd=cwd if chat_model.is_cli else None
                    )

            # Fail fast, before allocate_run/setup_agent_persist: a bad provider
            # prefix (e.g. 'gpt-5.3-codex-spark' vs 'codex/gpt-5.3-codex-spark')
            # must not persist a run/session recorded as a failed reliability event.
            # New-branch only — a resume model override never swaps the provider.
            if not branch.chat_model.is_cli and api_launch is None:
                cli_provider = getattr(branch.chat_model.endpoint.config, "provider", provider)
                raise ConfigurationError(
                    f"run operation only supports CLI endpoints, but got provider={cli_provider!r}. "
                    "Use one of the CLI endpoint prefixes: claude_code, codex, gemini-cli, pi. "
                    "Did you mean 'gemini-cli/<model>' instead of 'gemini/<model>'? "
                    "The 'gemini' prefix routes to the REST API, not the local Gemini CLI. "
                    "An API model runs through an agent profile (-a) that declares "
                    "'api_tools: read'."
                )
        else:
            cfg = branch.chat_model.endpoint.config.kwargs
            recorded_account = branch.metadata.get(_SUBSCRIPTION_ACCOUNT_KEY)
            if not isinstance(recorded_account, dict):
                recorded_account = None
            prior_switches = branch.metadata.get(_SUBSCRIPTION_ACCOUNT_SWITCHES_KEY)
            if isinstance(prior_switches, list):
                account_switches = [s for s in prior_switches if isinstance(s, dict)]
            if account is None:
                # A role's account or codex_home picks the account of a new run; a resumed
                # branch keeps the one it ran on unless the line names another.
                requested_account = None
            # The branch now runs on the account of its latest switch, else the one it started on.
            current_account = account_switches[-1] if account_switches else recorded_account
            recorded_home = current_account.get("codex_home") if current_account else None
            recorded_request: dict[str, str] | None = None
            if current_account is not None and "account" in current_account:
                recorded_request = {"account": current_account["account"]}
            elif recorded_home is not None:
                recorded_request = {"codex_home": recorded_home}
            resumed_provider = branch.chat_model.endpoint.config.provider
            if requested_account is not None and requested_account != recorded_request:
                from lionhub.agent.subscription_accounts import (
                    account_provider,
                    resolve_leg_account,
                )

                kind = account_provider(resumed_provider)
                if kind is None:
                    raise ConfigurationError(
                        f"this branch's model provider {resumed_provider!r} takes no subscription "
                        "account; resume it without --account."
                    )
                if kind == "claude_code":
                    # A Claude Code transcript is kept on this machine, not under the login, so
                    # a session can move to another account, such as off one that has run out.
                    account_switched_from = recorded_request or {}
                    recorded_request = requested_account
                else:
                    # A codex session lives under the home it ran in: the leg resumes there,
                    # under whichever account names that home.
                    requested_home = (
                        resolve_leg_account(resumed_provider, **requested_account).record.get(
                            "codex_home"
                        )
                        if kind == "codex" and recorded_home is not None
                        else None
                    )
                    if requested_home is None or requested_home != recorded_home:
                        raise ConfigurationError(
                            f"this branch ran on {current_account or 'no named account'}; it "
                            f"cannot be resumed on {requested_account}. Resume it without "
                            "--account, or with an account on the same Codex home."
                        )
                    # Recorded as a move, so a later resume without --account stays on this
                    # account even after the one before it is pointed at another home.
                    account_switched_from = recorded_request or {}
                    recorded_request = requested_account
            if recorded_request is not None:
                from lionhub.agent.subscription_accounts import (
                    account_provider,
                    resolve_leg_account,
                )

                leg_account = resolve_leg_account(resumed_provider, **recorded_request)
                kind_of_leg = account_provider(resumed_provider)
                if (
                    recorded_home is not None
                    and leg_account.record.get("codex_home") != recorded_home
                ):
                    raise ConfigurationError(
                        f"account {recorded_request.get('account')!r} now names the Codex home "
                        f"{leg_account.record.get('codex_home')!r}; this branch ran in "
                        f"{recorded_home!r}, where its session is kept. Resume it with --account "
                        "naming an account on that home."
                    )
                placed_runtime = {"env": leg_account.env}
                if leg_account.secret_fill_skip:
                    placed_runtime["secret_fill_skip"] = leg_account.secret_fill_skip
                # The parse model is its own endpoint when it is not the chat model; one of the
                # same account provider spawns the same child and runs on the same account.
                parse = getattr(branch, "parse_model", None)
                endpoints = [branch.chat_model.endpoint]
                if (
                    parse is not None
                    and parse is not branch.chat_model
                    and getattr(parse, "is_cli", False)
                    and account_provider(parse.endpoint.config.provider) == kind_of_leg
                ):
                    endpoints.append(parse.endpoint)
                for endpoint in endpoints:
                    adopt = getattr(endpoint, "adopt_runtime_state", None)
                    if adopt is None or adopt(placed_runtime):
                        raise ConfigurationError(
                            "this branch's model cannot take the account's environment, so "
                            "the resumed leg would run on this machine's own login."
                        )
            if account_switched_from is not None:
                from lionhub.orchestration.console import warn

                warn(
                    "account changed on resume: "
                    f"{account_switched_from.get('account') or 'no named account'} -> "
                    f"{recorded_request['account']}"
                )
            on_tool_loop = (
                not getattr(branch.chat_model, "is_cli", False)
                and _API_LAUNCH_KEY in branch.metadata
            )
            candidate = None
            if on_tool_loop:
                from lionhub.agent.factory import _set_api_effort

                actual_provider = _canonical_provider(branch.chat_model.endpoint.config.provider)
                if model_str is not None and canonical != actual_provider:
                    raise ConfigurationError(
                        f"cannot resume {actual_provider!r} API endpoint as {canonical!r}; "
                        "start a new branch to change providers."
                    )
                candidate = dict(cfg)
                candidate_model = model if model_str is not None else cfg.get("model")
                stored_effort = (
                    (cfg.get("output_config") or {}).get("effort")
                    if actual_provider == "anthropic"
                    else cfg.get("reasoning_effort")
                )
                if effort is not None or stored_effort is not None:
                    selected_effort = effort or stored_effort
                    selected_origin = (
                        effort_origin
                        if effort is not None
                        else "stored"
                        if actual_provider == "openai"
                        else "explicit"
                    )
                    if effort is None and actual_provider == "anthropic":
                        selected_effort = _clamp_claude_effort(stored_effort, candidate_model)
                    applied = _set_api_effort(
                        candidate,
                        actual_provider,
                        candidate_model,
                        selected_effort,
                        origin=selected_origin,
                    )
                    if not applied:
                        effort = None
                        if actual_provider == "openai":
                            candidate.pop("reasoning_effort", None)
                if model_str is not None:
                    candidate["model"] = model
            old_model = cfg.get("model")
            if candidate is not None:
                cfg.clear()
                cfg.update(candidate)
            if model_str is not None:
                if model != old_model:
                    from lionhub.orchestration.console import warn

                    warn(f"resume model override: {old_model} → {model}")
                cfg["model"] = model
                if model != old_model and effort is None:
                    # The persisted effort was clamped for the OLD model; the new
                    # one has its own ceiling, so re-clamp instead of replaying a
                    # value that model may reject (a sol-derived 'ultra' is a 400
                    # on a spark resume).
                    from lionagi.service.providers import reclamp_effort_for_model

                    kwarg = PROVIDER_EFFORT_KWARG.get(canonical)
                    if kwarg and cfg.get(kwarg) is not None:
                        cfg[kwarg] = reclamp_effort_for_model(canonical, cfg[kwarg], model)
            # The display flags are CLI endpoint options; an API model sends its
            # config's kwargs with each request, so a branch on the tool loop is
            # handed neither.
            if verbose and not on_tool_loop:
                cfg["verbose_output"] = True
            if theme is not None and not on_tool_loop:
                cfg["cli_display_theme"] = theme
            if effort is not None:
                kwarg = PROVIDER_EFFORT_KWARG.get(canonical)
                if on_tool_loop:
                    pass  # The candidate above has already validated and applied it.
                elif kwarg:
                    if canonical == "codex":
                        effort = _clamp_codex_effort(effort, cfg.get("model"))
                    elif canonical in _CLAUDE_PROVIDER_NAMES:
                        effort = _clamp_claude_effort(effort, cfg.get("model") or "")
                    cfg[kwarg] = effort
                elif canonical in PROVIDERS_EFFORT_VIA_MODEL_NAME:
                    # agy (Antigravity CLI) has no effort kwarg — fold effort into
                    # the resolved --model name instead (see resolve_agy_model).
                    from lionhub.providers.google.gemini_code import resolve_agy_model

                    cfg["model"] = resolve_agy_model(
                        cfg.get("model"),
                        effort=effort,
                        reapply_effort=model_str is None,
                    )
            if bypass:
                cfg.update(PROVIDER_BYPASS_KWARGS.get(canonical, {}))
            elif yolo:
                cfg.update(PROVIDER_YOLO_KWARGS.get(canonical, {}))
            if fast:
                cfg.update(PROVIDER_FAST_KWARGS.get(canonical, {}))
            # The stored row never carries a connector grant; its containment value also belongs
            # to the previous role, so both settings come from the current role.
            if canonical == "codex":
                cfg.pop("contain_account_integrations", None)
                if branch.chat_model.endpoint.is_cli:
                    _add_codex_account_integration_kwargs(
                        cfg, account_connectors, contain_account_integrations
                    )
            # Like the connectors: the class belongs to this launch, not to the stored row.
            cfg.pop("workload", None)
            _add_workload_kwarg(cfg, branch.chat_model.endpoint, workload)
            # A role's tool-set fields replace the stored ones; a field it leaves out keeps
            # the stored value, so resuming without a role does not widen a narrowed agent.
            if tool_settings:
                _add_claude_tool_kwargs(cfg, branch.chat_model.endpoint, tool_settings)
            # A resumed leg re-spawns a CLI child, so it needs the server set handed
            # to it just as a new one does; the persisted branch carries the model,
            # not the caller's directory. Which providers can be given a set, and
            # over which transport, is not this call site's question to answer.
            from lionhub.agent.factory import (
                _drop_forwarded_mcp_servers,
                _missing_generated_codex_profile,
                apply_forwarded_mcp_servers,
                request_kwargs_carry_forwarded_mcp,
            )

            if mcp_resolution.servers is not None:
                # A set resolved for this leg replaces the one its earlier run was
                # handed. The Claude map is overwritten below anyway; codex overrides
                # are merged, so without this the leg would start both sets. An earlier
                # empty set was carried as the ignored user config, which goes with it.
                _drop_forwarded_mcp_servers(cfg)
                cfg.pop("ignore_user_config", None)
            apply_forwarded_mcp_servers(
                cfg,
                mcp_resolution.servers,
                provider=provider,
                env=leg_account.env if leg_account is not None else None,
                # A set named by a config file, or declared empty, is the whole set: the provider's
                # own servers are left out too. A set found by search joins what the provider finds
                # for itself, and a failed search leaves the earlier run's servers, as it leaves a new
                # leg to what its CLI finds.
                exclusive=mcp_resolution.exclusive,
            )
            missing_profile = _missing_generated_codex_profile(
                cfg, leg_account.env if leg_account is not None else None
            )
            if missing_profile is not None:
                raise McpConfigError(
                    f"this branch's earlier run gave codex its MCP servers' env and headers "
                    f"through the generated profile {missing_profile}, which is no longer a readable file "
                    "(it is removed when the run that wrote it exits); resume it with "
                    "--mcp-config naming the config to use, or with --no-mcp-config"
                )
            # A resumed leg re-spawns from the persisted request, which is the only
            # thing that decides what it carries — read the answer off it.
            forwarded = request_kwargs_carry_forwarded_mcp(cfg)
            if mcp_resolution.servers is None and mcp_resolution.reason and forwarded:
                hint(
                    f"[mcp] no server set resolved ({mcp_resolution.reason}); this resumed "
                    "leg keeps the servers its earlier run was handed"
                )
            else:
                _report_mcp_resolution(
                    mcp_resolution,
                    provider=provider,
                    cwd=cwd,
                    forwarded=forwarded,
                )
            # The requirement a branch was launched with stays with it: a resume naming no
            # profile, or one without the key, is checked as its launch was (ADR-0048
            # Amendment 2 R3). A codex request carries it and refuses at its own launch.
            _requires_hooks = _requires_hooks or bool(branch.metadata.get(_REQUIRE_HOOKS_KEY))
            if _requires_hooks:
                from lionhub.orchestration.spawn import refuse_require_hooks

                if (
                    _canonical_provider(branch.chat_model.endpoint.config.provider) == "codex"
                    and branch.chat_model.endpoint.is_cli
                ):
                    cfg["require_hooks"] = True
                try:
                    refuse_require_hooks(agent_name, profile, branch.chat_model, required=True)
                except RequireHooksRefusedError as exc:
                    _require_hooks_refusal = exc

        # A branch launched onto the tool loop records the tool set and bound it was
        # launched with, and a resume runs the loop again from that record. A branch
        # without the record runs as it did before the loop existed.
        api_launch = None
        stored_api_launch = branch.metadata.get(_API_LAUNCH_KEY)
        if getattr(branch.chat_model, "is_cli", False) and (
            any(key in profile_api_extra for key in _API_POLICY_KEYS)
            or (
                isinstance(stored_api_launch, dict)
                and any(stored_api_launch.get(key) is not None for key in _API_POLICY_KEYS)
            )
        ):
            raise ConfigurationError(
                "profile or stored API policy settings cannot be declared on a CLI-provider leg."
            )
        if not getattr(branch.chat_model, "is_cli", False) and _API_LAUNCH_KEY in branch.metadata:
            api_launch = _recorded_api_launch(branch.metadata[_API_LAUNCH_KEY])
            # What an earlier invocation spent is not this one's.
            branch.metadata[_API_LAUNCH_KEY].pop("spend", None)
            declared = _profile_api_launch(profile) if is_resumed_branch else None
            if is_resumed_branch and profile is not None and declared is None:
                raise ConfigurationError(
                    f"agent profile {getattr(profile, 'name', agent_name)!r} declares no "
                    "api_tools, but this branch was launched on an API model's tool loop; "
                    "resume it with a profile that declares 'api_tools: read', or without -a."
                )
            if declared is not None:
                api_launch["max_extensions"] = declared["max_extensions"]
                api_launch["max_input_tokens"] = declared["max_input_tokens"]
                for key in _API_POLICY_KEYS:
                    api_launch[key] = declared[key]
                branch.metadata[_API_LAUNCH_KEY]["max_input_tokens"] = declared["max_input_tokens"]
            branch.metadata[_API_LAUNCH_KEY].update(
                {key: api_launch[key] for key in _API_POLICY_KEYS}
            )
            if is_resumed_branch and images:
                raise ConfigurationError("--image is not supported for an API model yet.")
            if is_resumed_branch:
                # A stored branch keeps its messages, not its tools, so the read set
                # is bound again, guarded to this invocation's working directory.
                from lionhub.agent.factory import _register_coding_tools

                command_names = None
                if api_launch["tools"] in _API_CONFINED_TOOL_SETS:
                    from lionhub.tools.command_env_metadata import configured_command_env_names

                    active_reference = branch.chat_model.endpoint.config.api_key_env
                    command_names = configured_command_env_names(
                        active_reference_names=(active_reference,)
                        if active_reference is not None
                        else (),
                    )
                _register_coding_tools(
                    branch,
                    _make_api_agent_spec(
                        cwd=cwd,
                        effort=None,
                        system_prompt=None,
                        role="reviewer",
                        tools=api_launch["tools"],
                        network=api_launch["network"],
                        _command_env_names=command_names,
                        profile_deny_read=tuple(getattr(profile, "deny_read", ()) or ()),
                    ),
                )

        # Skip the profile system prompt for a branch that carries (or would carry)
        # a create_agent-composed system message — full rationale in
        # docs/internals/cli.md. Brand-new branch: `took_create_agent_path` is
        # authoritative. Resumed branch: only the immutable
        # CREATE_AGENT_BRANCH_ORIGIN_KEY marker counts as provenance — never
        # re-derived from persisted content (a markerless branch with an
        # explicitly requested role must get that role's
        # system prompt rather than have it silently dropped).
        if leg_account is not None and not is_resumed_branch:
            branch.metadata[_SUBSCRIPTION_ACCOUNT_KEY] = dict(leg_account.record)
        if account_switched_from is not None and leg_account is not None:
            account_switches.append(dict(leg_account.record))
            branch.metadata[_SUBSCRIPTION_ACCOUNT_SWITCHES_KEY] = account_switches
        if _requires_hooks:
            branch.metadata[_REQUIRE_HOOKS_KEY] = True
        if is_resumed_branch:
            from lionhub.agent.factory import CREATE_AGENT_BRANCH_ORIGIN_KEY

            composed_via_create_agent = bool(branch.metadata.get(CREATE_AGENT_BRANCH_ORIGIN_KEY))
        else:
            composed_via_create_agent = took_create_agent_path
        if profile and profile.system_prompt and not composed_via_create_agent:
            branch.msgs.add_message(system=profile.system_prompt)

        if timeout is not None:
            preamble = build_deadline_preamble(timeout)
            prompt = preamble + prompt

        # Refused here, before the run exists, so a scratch request this run cannot honour leaves
        # no run directory behind (ADR-0041 Amendment 6).
        scratch_mode = getattr(profile, "scratch", None)
        if scratch_mode is not None:
            _scratch_reason = _scratch_refusal(
                _canonical_provider(branch.chat_model.endpoint.config.provider),
                api_launch=api_launch is not None,
                is_cli=branch.chat_model.is_cli,
                endpoint_kwargs=getattr(branch.chat_model.endpoint.config, "kwargs", None),
            )
            if _scratch_reason is not None:
                raise ConfigurationError(
                    f"agent profile {agent_name!r} declares scratch, which this run cannot "
                    f"give it: {_scratch_reason}. Run the profile on a claude_code model or a "
                    "sandboxed codex model, or remove scratch from it."
                )
        # The same for the profile's deny_read: refused before the run exists on a route with
        # nothing to hold it (ADR-0041 Amendment 7), and handed to every CLI model otherwise.
        profile_reads = tuple(getattr(profile, "deny_read", ()) or ())
        if profile_reads:
            from lionhub.agent.child_sandbox import (
                profile_deny_read_refusal,
                resolve_child_sandbox,
                with_profile_deny_read,
            )

            # Raises while the child sandbox is switched off: nothing would hold the entries.
            with_profile_deny_read(resolve_child_sandbox(None), profile_reads, profile=agent_name)
            _reads_reason = profile_deny_read_refusal(
                _canonical_provider(branch.chat_model.endpoint.config.provider),
                is_cli=branch.chat_model.is_cli,
                api_tools=api_launch["tools"] if api_launch is not None else None,
            )
            if _reads_reason is not None:
                raise ConfigurationError(
                    f"agent profile {agent_name!r} declares deny_read, which this run cannot "
                    f"hold: {_reads_reason}. Run the profile where its entries hold, or remove "
                    "deny_read from it."
                )
            for _model in (branch.chat_model, branch.parse_model):
                if _model is not None and _model.is_cli:
                    _model.endpoint.config.kwargs["profile_deny_read"] = list(profile_reads)

        # A hooks file named at submit: checked against its recorded digest and applied before
        # the run exists. A resumed branch checks the copy it started with again.
        hooks_from_spec = _hooks_from_spec(hooks_from_snapshot, branch, is_resumed_branch)
        if hooks_from_spec is not None:
            carried_hooks = _apply_hooks_from(hooks_from_spec, branch, cwd)
            if not is_resumed_branch:
                branch.metadata[_HOOKS_FROM_KEY] = hooks_from_spec
            if _require_hooks_refusal is not None and carried_hooks:
                # The key's refusal was decided before the file was read; the entries it
                # carries run on this leg, so the refusal is stated again counting them.
                from lionhub.orchestration.spawn import refuse_require_hooks

                try:
                    refuse_require_hooks(
                        agent_name, profile, branch.chat_model, required=True, carried=carried_hooks
                    )
                except RequireHooksRefusedError as exc:
                    _require_hooks_refusal = exc

        inherited_run_id = current_run_id()
        job_run_id = os.environ.get(_MCP_JOB_RUN_ID_ENV_VAR)
        run = allocate_run()
        if isinstance(run, RunDir):
            run = run.with_execution_provenance(
                cwd=cwd or os.getcwd(),
                mcp_resolution=mcp_resolution,
                mcp_disabled=no_mcp_config,
            )
            # Descendants inherit the job marker. Only the first leg also consumes
            # the matching launch ID when it allocates the job's run directory.
            if not _auto_resumed and inherited_run_id == job_run_id == run.run_id:
                _job_result_run = run
        # Recorded once the run directory exists, because a named set is copied into it: no
        # branch is persisted before this point, so none can carry a record without its copy.
        # Whether this run's record now points at a copy it wrote; an automatic resume then
        # replays that copy rather than reading the named file again.
        mcp_copy_written = False
        if not is_resumed_branch:
            # The file on the line is what a resume reads again, named or not: a
            # leg submitted through the MCP server is pointed at a snapshot of the
            # config its submission found, and that snapshot is its set.
            # A snapshot's resolution reports the config it was taken from and the
            # directory its search began in, both provenance; a resume must read the
            # snapshot itself, from this process's own directory.
            selected_config = mcp_config or mcp_submit_snapshot
            named_on_the_line = selected_config is not None and mcp_resolution.source is not None
            line_file = (
                explicit_mcp_config_path(selected_config, launch_dir=launch_dir)
                if named_on_the_line
                else None
            )
            branch.metadata[_MCP_LAUNCH_KEY] = {
                "mcp_config": str(line_file) if line_file is not None else None,
                "no_mcp_config": no_mcp_config,
                "launch_dir": str(launch_dir),
            }
            if profile_disables_mcp:
                branch.metadata[_MCP_LAUNCH_KEY]["no_mcp_config_source"] = "profile"
            elif mcp_submit_snapshot is not None and not no_mcp_config:
                branch.metadata[_MCP_LAUNCH_KEY]["mcp_config_origin"] = "submit_snapshot"
            elif mcp_config is not None and not no_mcp_config:
                # A named file can change, or sit where the leg can write: the record points
                # at the copy of the set this run was handed, and keeps the name as provenance.
                branch.metadata[_MCP_LAUNCH_KEY].update(
                    {
                        "mcp_config": str(_write_mcp_config_copy(run.state_root, handed_servers)),
                        "mcp_config_origin": "explicit",
                        _MCP_CONFIG_NAMED_KEY: str(line_file),
                    }
                )
                mcp_copy_written = True
        elif profile_disables_mcp:
            # Once a resumed leg is contained by its profile, later resumes
            # without that profile must not restore the earlier server set.
            branch.metadata[_MCP_LAUNCH_KEY] = {
                "mcp_config": None,
                "no_mcp_config": True,
                "no_mcp_config_source": "profile",
                "launch_dir": str(launch_dir),
            }
        elif (
            replay_recorded_choice
            and isinstance(recorded_launch, dict)
            and _MCP_CONFIG_NAMED_KEY in recorded_launch
        ):
            # A resume that replayed a copy copies the set again into its own run directory, so
            # the set lasts as long as the branch's newest run rather than its first.
            branch.metadata[_MCP_LAUNCH_KEY] = {
                **recorded_launch,
                "mcp_config": str(_write_mcp_config_copy(run.state_root, handed_servers)),
            }
        branch_id = str(branch.id)
        resolved_model_spec = _provenance.resolve_model_spec(provider, model)
        run_manifest = {
            "branch_id": branch_id,
            "agent_name": agent_name,
            "provider": provider,
            "model": resolved_model_spec,
            "status": "running",
            "started_at": time.time(),
            "ended_at": None,
        }
        if (
            _canonical_provider(branch.chat_model.endpoint.config.provider)
            in PROVIDERS_EFFORT_VIA_MODEL_NAME
        ):
            from lionhub.providers.google.gemini_code import resolve_agy_model

            # The exact `agy --model` name the spec resolved to, beside the spec as given:
            # a spec the resolver maps to another family or passes through shows here.
            run_manifest["resolved_model"] = resolve_agy_model(branch.chat_model.model_name)
        _api_usage = (
            APIUsageCollector(run_id=run.run_id, branch_id=branch_id)
            if api_launch is not None
            else None
        )
        if _api_usage is not None:
            run_manifest["api_usage"] = _api_usage.snapshot()
        if context_from:
            run_manifest["context_from"] = list(context_from)
        # A run whose own endpoint sends through the account router does not use the launcher's
        # account name: the router picks the account for each request and ignores the variable.
        # Its api_usage record names the account that paid.
        if api_account is not None and not _endpoint_routing_enabled(branch.chat_model.endpoint):
            run_manifest["api_account"] = api_account
        if leg_account is not None:
            # Names and paths only; the token never reaches a record.
            run_manifest.update(leg_account.record)
        if account_switches:
            # The account the branch started on (null: this machine's own login), then each
            # account it was resumed on since, this run's last.
            started_on = branch.metadata.get(_SUBSCRIPTION_ACCOUNT_KEY)
            run_manifest["account_history"] = [
                started_on.get("account") if isinstance(started_on, dict) else None,
                *(switch.get("account") for switch in account_switches),
            ]
        _write_run_manifest = getattr(run, "write_manifest", None)
        if _write_run_manifest is not None:
            _write_run_manifest(run_manifest)

        artifact_contract = resolve_artifact_contract(
            playbook_artifacts=None,
            agent_defaults=profile.artifact_defaults if profile else None,
        )
        from lionhub.serve.cli.orchestrate._notify import notice_context_for_run
        from lionhub.state.compat import StateCompatibilityRefused

        notice_context = notice_context_for_run(run, cwd=cwd, sender=notify_sender)
        try:
            live = await setup_agent_persist(
                branch,
                agent_name=agent_name,
                artifacts_path=str(run.artifact_root),
                artifact_contract=artifact_contract,
                invocation_id=invocation_id,
                model=resolved_model_spec,
                provider=provider,
                effort=effort,
                project=project,
                run_id=run.run_id,
                run_manifest=run_manifest,
                run=run if isinstance(run, RunDir) else None,
                # This runner drains queued operator messages at turn end and
                # tombstones whatever it did not take, so controls aimed at it have a
                # consumer.
                drains_controls=True,
                parent_marker=_parent_marker,
                _worker_spawn_continuation=(
                    None
                    if _worker_spawn_continuation is _NO_PERSISTED_EXECUTION
                    else _worker_spawn_continuation
                ),
            )
        except (StateCompatibilityRefused, _WorkerSpawnAdmissionError) as exc:
            # The run stops before it starts (ADR-0128 D6), and its run.json says so rather than
            # reading `running` with no process behind it.
            if _write_run_manifest is not None:
                run_manifest.update(
                    status="failed",
                    ended_at=time.time(),
                    error=(
                        f"The worker owner refused admission: {exc}"
                        if isinstance(exc, _WorkerSpawnAdmissionError)
                        else f"The store refused a write: {exc}"
                    ),
                )
                _write_run_manifest(run_manifest)
            raise
        _worker_spawn_owner = live.get("worker_spawn_owner") if live is not None else None
        if (
            live is not None
            and _worker_spawn_owner is None
            and live.get("worker_spawns_unrecorded") is not True
        ):
            raise _WorkerSpawnAdmissionError("the persisted execution has no admitted worker owner")
        if _worker_spawn_owner is not None:
            _worker_spawn_bindings.enter_context(_worker_spawn_owner.bind())
            _worker_spawn_bindings.push_async_callback(_worker_spawn_owner.aclose)
        if live is None and _write_run_manifest is not None:
            # Setup returned no session (it may have committed a row before it failed, which `li
            # monitor` lists under that row's own id; nothing here prints it), so the run id names no
            # row: the run records its own process on run.json, where `li kill <run id>` reads it, and
            # says how to stop it (ADR-0104 Amendment 2).
            from lionagi.ln._proc import current_pid_markers
            from lionhub.orchestration.console import warn

            run_manifest.update(current_pid_markers())
            _write_run_manifest(run_manifest)
            warn(
                f"run {run.run_id}: persistence setup returned no session; "
                f"stop it with: li kill {run.run_id}"
            )
        if seed_injection_totals:
            await _seed_injection_stats(live, _injection_totals)

        # A resumed branch can already contain an assistant reply from an earlier leg.
        before_leg = (
            {message.id for message in branch.msgs.messages}
            if notify or _job_result_run is not None
            else set()
        )
        final_response: str | None = None

        # Session-scoped: teardown_agent_persist terminalizes only the session;
        # invocation records are finalized externally and would never fire. Deferred
        # auto-resume legs unregister without firing; the recursed leg registers anew.
        #
        # If persistence setup failed there's no session entity to fire a terminal
        # transition on, so register_flow_notify_scope can't be used -- this run
        # instead delivers the notice itself once its own terminal status is known,
        # in the `finally` block below (see `deliver_flow_notify_now`): same
        # resolution and payload, run directly instead of registered for a
        # transition that will never happen. The refusal record this run can still
        # write applies only when delivery is actually attempted and genuinely
        # can't complete, never merely because persistence failed.
        _notify_scope_name: str | None = None
        _notify_session_id = live.get("session_id") if live else None
        if notify and _notify_session_id is not None:
            from lionhub.serve.cli.orchestrate._notify import (
                register_flow_notify_scope,
                unregister_flow_notify_scope,
            )
            from lionhub.state.lifecycle.notify_settings import (
                record_notify_rejection_to_run,
            )

            def _notify_override_refused(reason: str) -> None:
                # This run explicitly asked for a notifier and will not get one.
                # Recording it here is what keeps a refusal distinguishable from
                # never having configured one; both otherwise register nothing.
                record_notify_rejection_to_run(run, reason)

            _notify_scope_name = register_flow_notify_scope(
                override=notify,
                entity_kind="session",
                entity_id=_notify_session_id,
                invocation_id=invocation_id,
                flow_kind="agent",
                playbook=None,
                save_dir=str(run.artifact_root),
                cwd=cwd or os.getcwd(),
                started_at=run_manifest["started_at"],
                on_rejection=_notify_override_refused,
                run=run,
                final_response=lambda: final_response,
            )

        # Bind this run into the notify.on_terminal handler at registration time so
        # a late outcome lands here or nowhere, never on a later run. Skipped when
        # --notify already owns this entity (a second override would double-fire).
        _notify_outcome_scope_name: str | None = None
        if not notify and _notify_session_id is not None:
            from lionhub.state.lifecycle.notify_settings import (
                register_run_notify_outcome_scope,
                unregister_run_notify_outcome_scope,
            )

            _notify_outcome_scope_name = register_run_notify_outcome_scope(
                run,
                entity_kind="session",
                entity_id=_notify_session_id,
                project_dir=cwd,
                notice_context=notice_context,
            )

        _terminal_status = "completed"
        _terminal_exc: BaseException | None = None
        _propagating = False
        _direct_notice_sent = False

        async def _deliver_direct_notice() -> None:
            """Send this run's one terminal notice on the no-persistence route.

            No session entity ever existed for this run, so the registered path
            was never reached and nothing else will ever deliver for it. *When*
            this is called is the whole question: it reads ``_terminal_status`` at
            call time, so it must run only after every line that can still change
            that status has run.

            Shielded, since a cancellation is exactly what's expected from the
            teardown path that calls this, and the guarantee this exists to
            provide is that a terminal notice arrives regardless. Idempotent by
            flag (reached from both a ``finally`` and the ordinary tail) rather
            than by relying on those two call sites never overlapping.
            """
            nonlocal _direct_notice_sent

            if not notify or _notify_session_id is not None or _direct_notice_sent:
                return
            _direct_notice_sent = True

            with CancelScope(shield=True):
                try:
                    from lionhub.serve.cli.orchestrate._notify import deliver_flow_notify_now

                    _reason_code, _, _ = resolve_run_reason(
                        status=_terminal_status, exception=_terminal_exc
                    )
                    await deliver_flow_notify_now(
                        override=notify,
                        run=run,
                        entity_kind="session",
                        entity_id=branch_id,
                        invocation_id=invocation_id,
                        flow_kind="agent",
                        playbook=None,
                        save_dir=str(run.artifact_root),
                        cwd=cwd or os.getcwd(),
                        started_at=run_manifest["started_at"],
                        terminal_status=_terminal_status,
                        reason_code=_reason_code,
                        occurred_at=time.time(),
                        final_response=final_response,
                    )
                except Exception as _notify_exc:  # noqa: BLE001 — a notifier failure must never affect the run
                    log_error(f"direct-path notify.on_terminal delivery failed: {_notify_exc!r}")

        # Armed unconditionally, not only when --timeout is set: the steer receipt
        # ack below is the operator's only signal that a queued message was
        # received (vs. lost) before the turn ends, and a leg spawned without a
        # timeout is exactly the case where a turn can run long enough for that
        # distinction to matter. The extra task is one sleeping coroutine per leg,
        # negligible next to the provider call it watches.
        _heartbeat_task = None
        import asyncio as _asyncio
        import time as _hb_time

        _hb_report = _ProgressReport(branch, _hb_time.monotonic())

        async def _heartbeat_loop():
            while True:
                await sleep(_HEARTBEAT_INTERVAL_S)
                line = _hb_report.line(_hb_time.monotonic())
                # Steer receipt ack: a queued operator message cannot land
                # mid-turn, so tell the operator it was received and when it
                # will apply. Fail-safe — the heartbeat never crashes the leg.
                if live and live.get("session_id"):
                    try:
                        _pending = await live["db"].list_pending_session_controls(
                            live["session_id"]
                        )
                        _n = sum(1 for c in _pending if c.get("verb") == "message")
                        if _n:
                            line += f"  [steer queued x{_n} — lands at end of current turn]"
                    except Exception:  # noqa: BLE001, S110 — ack is best-effort color
                        pass
                hint(line)

        try:
            _heartbeat_task = _asyncio.ensure_future(_heartbeat_loop())
        except RuntimeError:
            _heartbeat_task = None

        _leg_deadline = (time.monotonic() + timeout) if timeout is not None else None
        _api_turn = None
        _api_fold = None
        if api_launch is not None:
            from lionagi.service.accounts._pricing import estimate_response_cost

            _api_turn_index = 0

            async def _api_turn(instruction: str, turn_timeout: float | None):
                nonlocal _api_turn_index
                _api_turn_index += 1
                try:
                    with collect_api_turn(
                        _api_usage, _api_turn_index, estimate_cost=estimate_response_cost
                    ):
                        return await _run_api_turn(
                            branch,
                            instruction,
                            timeout=turn_timeout,
                            max_extensions=api_launch["max_extensions"],
                            snapshot_dir=str(run.branches_dir),
                            loop=api_launch["loop"],
                            fold=_api_fold,
                        )
                finally:
                    run_manifest["api_usage"] = _api_usage.snapshot()

        # Passed only when set: an absent key is what lets the run fall back to
        # LIONAGI_WORKER_IDLE_TIMEOUT, and an explicit None would switch the
        # watchdog off instead.
        _idle = {"idle_timeout": idle_timeout} if idle_timeout is not None else {}
        _worker_spawn_teardown_cancel = None
        _run_scratch = None
        try:
            if _require_hooks_refusal is not None:
                raise _require_hooks_refusal
            if scratch_mode is not None:
                from lionhub.orchestration.run_scratch import RunScratch

                _run_scratch = RunScratch.create(run.state_root, keep=scratch_mode == "keep")
                run_manifest["scratch"] = _run_scratch.record()
                if _write_run_manifest is not None:
                    _write_run_manifest(run_manifest)
            _bind_run_scratch(branch.chat_model, _run_scratch)
            if _api_turn is not None:
                _api_fold = _install_api_input_cap(
                    branch,
                    api_launch["max_input_tokens"],
                    **{key: api_launch[key] for key in _API_POLICY_KEYS},
                )
                run_manifest.update(
                    {
                        "prompt_caching": branch.chat_model.endpoint.places_prompt_cache,
                        "tool_result_max_chars": branch.chat_model.tool_result_max_chars,
                        "context_fold": (
                            {
                                "fold_at": _api_fold.fold_at,
                                "target": _api_fold.target,
                                "tail_margin": _api_fold.tail_margin,
                            }
                            if _api_fold is not None
                            else None
                        ),
                    }
                )
                if _write_run_manifest is not None:
                    _write_run_manifest(run_manifest)
                try:
                    res = await _api_turn(prompt, timeout)
                except Exception as refused:
                    # The router refused the run's first request before anything was sent, so
                    # the same model is asked on the subscription route, at most once. Every
                    # other failure, and a refusal after a reply, ends the run as it did.
                    refusal = _first_request_route_refusal(refused, branch, _api_usage)
                    if refusal is None or is_resumed_branch or canonical != "anthropic":
                        raise
                    from lionhub.orchestration.console import warn

                    # Only a refusal about spend moves the run, and never one pinned to an
                    # account: the run prints why it stays and fails with the routing error.
                    objection = await _subscription_switch_objection(refusal, api_account)
                    if objection is not None:
                        warn(objection)
                        raise
                    # The CLI has every built-in tool unless the request names some, so the
                    # run names the tools of the set it ran with on the API route. A set with
                    # no entry (the write set among them) does not move: the run prints why and
                    # fails with the routing error instead of gaining what its API set lacked.
                    # This is decided before the branch's model or anything else is changed.
                    set_tools = _SUBSCRIPTION_API_SET_TOOLS.get(api_launch["tools"])
                    if set_tools is None:
                        warn(
                            "not moving to the subscription route because only a read-set run "
                            f"moves, and this run has the {api_launch['tools']} set"
                        )
                        raise
                    # The API read set's reader is held to the workspace root, while Claude
                    # Code's Read, Grep and Glob read anywhere this account can. The switched
                    # run is granted each tool only under that root, and `dontAsk` refuses every
                    # call no rule grants. A root that a rule cannot carry literally does not move.
                    read_grant = _subscription_read_rules(set_tools, cwd)
                    if read_grant is None:
                        warn(
                            "not moving to the subscription route because the working "
                            "directory's path cannot be written as a Claude Code permission rule "
                            "or holds a protected name"
                        )
                        raise
                    workspace_root, read_rules, read_denials = read_grant
                    from lionagi.service.imodel import iModel

                    original_spec = f"{provider}/{model}"
                    fallback_spec = f"{_SUBSCRIPTION_FALLBACK_PROVIDER}/{model}"
                    # The CLI child inherits this whole process's environment unless the request
                    # carries one. It is handed this environment without the credential names the
                    # API route's write-set commands went without (the declared names, the shared
                    # sensitive name patterns and the shell startup names), so that no API key
                    # reaches Claude Code, where an
                    # `ANTHROPIC_API_KEY` would be billed in place of the subscription. Only Claude
                    # Code's own login token is kept. The names come from the API model's key
                    # reference, read before the branch's model is replaced.
                    from lionagi.service._subagent_admission import without_account_names
                    from lionhub.tools.command_env import scrub_command_env
                    from lionhub.tools.command_env_metadata import configured_command_env_names

                    # A settings file that cannot be read leaves the names to remove unknown, so
                    # the run does not move; it fails with the routing error.
                    env_failure: str | None = None
                    try:
                        api_key_reference = branch.chat_model.endpoint.config.api_key_env
                        parent_env = dict(os.environ)
                        # An account name this process inherited is no account of the child's.
                        child_env = without_account_names(
                            scrub_command_env(
                                parent_env,
                                declared_names=configured_command_env_names(
                                    active_reference_names=(api_key_reference,)
                                    if api_key_reference is not None
                                    else (),
                                ),
                                command_env_allowlist=_SUBSCRIPTION_ENV_ALLOWLIST,
                            )
                        )
                        # Names only, never values.
                        env_removed = sorted(set(parent_env) - set(child_env))
                        # At spawn, the configured secret lookup fills each name it declares that the
                        # child's environment lacks, which would put a removed credential back. Every
                        # name the lookup declares is skipped for this child except Claude Code's own
                        # login token, so the lookup adds nothing the API route's commands lacked.
                        from lionagi.service.accounts._secret_resolution import (
                            resolve_secret_lookup_config,
                        )

                        lookup = resolve_secret_lookup_config().lookup
                        kept = {name.upper() for name in _SUBSCRIPTION_ENV_ALLOWLIST}
                        secret_fill_skip = frozenset(
                            name
                            for name in (() if lookup is None else lookup.names)
                            if name.upper() not in kept
                        )
                    except Exception as env_exc:  # noqa: BLE001 — the run stays where it is
                        env_failure = type(env_exc).__name__
                    if env_failure is not None:
                        warn(
                            "not moving to the subscription route because the credential names "
                            f"to remove from its environment could not be read ({env_failure})"
                        )
                        raise
                    subscription_model = build_chat_model(
                        _SUBSCRIPTION_FALLBACK_PROVIDER,
                        model,
                        # Read, Grep and Glob need no approval, so the request carries no
                        # permission bypass whatever the run's `yolo` and `bypass` were.
                        False,
                        verbose,
                        theme,
                        requested_effort,
                        fast,
                        False,
                        # The API leg was handed no MCP server, so this one gets none either:
                        # an empty set that is the whole set keeps the CLI from loading any
                        # config of its own.
                        mcp_servers={},
                        mcp_exclusive=True,
                        account_connectors=account_connectors,
                        contain_account_integrations=contain_account_integrations,
                        workload=workload,
                        # No user, project or local Claude Code settings are read, so their
                        # hooks, permission rules and plugins do not reach the switched leg, and
                        # the only rules it runs under are the workspace grants.
                        tool_settings={
                            "tools": set_tools,
                            "allowed_tools": read_rules,
                            "disallowed_tools": read_denials,
                            "permission_mode": "dontAsk",
                            "setting_sources": "",
                        },
                        env=child_env,
                        secret_fill_skip=secret_fill_skip,
                    )
                    if isinstance(subscription_model, str):
                        subscription_model = iModel(model=subscription_model)
                    # The endpoint says whether it took the environment: one with no child process
                    # would leave it unread, and the CLI would then inherit this process's whole
                    # environment. The run does not switch; it fails with the routing error.
                    # The same holds for the lookup's skip set: left unread, the lookup would fill
                    # the removed names back in.
                    taken = getattr(subscription_model.endpoint, "_runtime_state", {})
                    if "env" not in taken or (
                        secret_fill_skip and taken.get("secret_fill_skip") != secret_fill_skip
                    ):
                        warn(
                            "not moving to the subscription route because the Claude Code "
                            "request did not take the child environment"
                        )
                        raise
                    # Without `dontAsk` and the workspace grants, the CLI's own defaults would
                    # decide where its reads reach, so the run does not switch.
                    granted = subscription_model.endpoint.config.kwargs
                    if (
                        granted.get("permission_mode") != "dontAsk"
                        or list(granted.get("allowed_tools") or ()) != read_rules
                        or not set(read_denials) <= set(granted.get("disallowed_tools") or ())
                    ):
                        warn(
                            "not moving to the subscription route because the Claude Code "
                            "request did not take the workspace permission rules"
                        )
                        raise
                    api_model = branch.chat_model
                    parse_follows_chat = branch.parse_model is api_model
                    branch.chat_model = subscription_model
                    if parse_follows_chat:
                        branch.parse_model = subscription_model
                    # The branch now runs a CLI provider, which carries no API tool-loop record.
                    branch.metadata.pop(_API_LAUNCH_KEY, None)
                    branch.metadata[_SUBSCRIPTION_SWITCH_KEY] = {"from": original_spec}
                    # The API turn saved the branch before this move; save it again, so that the
                    # branch a resume would read already records the move.
                    from lionagi.operations.run.run import _write_branch_snapshot

                    await _write_branch_snapshot(branch, str(run.branches_dir))
                    await api_model.close()
                    provider = _SUBSCRIPTION_FALLBACK_PROVIDER
                    effort = resolve_persisted_effort(
                        provider, subscription_model, requested_effort
                    )
                    resolved_model_spec = _provenance.resolve_model_spec(provider, model)
                    for api_only in ("prompt_caching", "tool_result_max_chars", "context_fold"):
                        run_manifest.pop(api_only, None)
                    switched_kwargs = subscription_model.endpoint.config.kwargs
                    run_manifest.update(
                        provider=provider,
                        model=resolved_model_spec,
                        subscription_fallback={
                            "original_model": original_spec,
                            "model_used": fallback_spec,
                            "refusal_code": refusal.code.value,
                            # The `--tools` value the switched leg runs with; null when it
                            # runs with every built-in tool.
                            "tools": switched_kwargs.get("tools"),
                            # The rules granting those tools under the workspace root, and the
                            # permission mode that refuses every call they do not grant.
                            "allowed_tools": switched_kwargs.get("allowed_tools"),
                            "disallowed_tools": switched_kwargs.get("disallowed_tools"),
                            "permission_mode": switched_kwargs.get("permission_mode"),
                            # The sorted names, never the values, of the variables this process
                            # held that the switched child's environment does not carry.
                            "env_removed": env_removed,
                            # The names the configured secret lookup may not fill into it.
                            "env_fill_skipped": sorted(secret_fill_skip),
                        },
                    )
                    if _write_run_manifest is not None:
                        _write_run_manifest(run_manifest)
                    if live is not None and live.get("db") is not None:
                        try:
                            await live["db"].update_session(
                                live["session_id"],
                                model=resolved_model_spec,
                                provider=provider,
                                effort=effort,
                            )
                            await live["db"].update_branch(
                                branch_id, model=resolved_model_spec, provider=provider
                            )
                        except Exception as record_exc:  # noqa: BLE001 — the run goes on
                            log_error(
                                f"could not record the subscription route on the session: "
                                f"{record_exc!r}"
                            )
                    # The code after this block needs these three None: the `if _api_turn is None:`
                    # turn, the steer routing (`run_turn=_api_turn`) and the context-fold
                    # manifest write.
                    api_launch = _api_turn = _api_fold = None
                    warning = [
                        f"the Anthropic API route refused the first request "
                        f"({refusal.code.value}); continuing on {fallback_spec}"
                    ]
                    warning.append(
                        f"Claude Code's {', '.join(set_tools.split(','))} are granted only under "
                        f"{workspace_root}, and every other tool call is refused"
                    )
                    warn(". ".join(warning))
            if _api_turn is None:
                res = await branch.operate(
                    instruction=prompt,
                    stream_persist=True,
                    persist_dir=str(run.stream_dir),
                    snapshot_dir=str(run.branches_dir),
                    timeout=timeout,
                    **_idle,
                    **({"images": images} if images else {}),
                    **({"repo": cwd} if cwd else {}),
                )
            steer_res = await _drain_pending_steers(
                live,
                branch,
                operate_kwargs={
                    "stream_persist": True,
                    "persist_dir": str(run.stream_dir),
                    "snapshot_dir": str(run.branches_dir),
                    "timeout": timeout,
                    **_idle,
                    **({"repo": cwd} if cwd else {}),
                },
                deadline=_leg_deadline,
                # The run id, because it is what identifies THIS leg. The branch is
                # shared with every leg that resumes it, so a branch-keyed claim
                # could not tell two legs apart, which is the whole point of naming
                # the claimant.
                owner=run.run_id,
                run_turn=_api_turn,
            )
            if steer_res is not None:
                res = steer_res
        except (TimeoutError, LionTimeoutError) as exc:
            _terminal_status = "timed_out"
            _terminal_exc = exc
            from lionhub.serve.mcp._terminal_cause import write_terminal_cause

            # Written on the timeout path too, even though a timeout is never one of
            # the typed provider errors. A recorded `unknown` says the cause was
            # looked at and was not a provider error; no record at all says nobody
            # looked, and a reader cannot tell those apart after the fact.
            write_terminal_cause(exc)
            from lionhub.orchestration.console import warn

            warn(f"agent timed out after {timeout}s")
            last = branch.msgs.last_response
            res = (last.response if last else "") or None
        except BaseException as exc:
            _terminal_status = classify_exception(exc)
            _terminal_exc = exc
            from lionhub.serve.mcp._terminal_cause import write_terminal_cause

            # Before the re-raise: this is the last point that still holds the
            # exception object, and the hook that records the run's end runs in a
            # different process where only its own class name would survive.
            write_terminal_cause(exc)
            # Nothing after this try block runs, so the teardown below is this
            # run's last chance to notify. Every other path falls through to the
            # tail, where the status is still being decided.
            _propagating = True
            if _terminal_status == "failed":
                # Default traceback printing is unreliable under SIGTERM/process
                # death — leave a one-line diagnostic before it propagates.
                shown = _admission_refusal(exc) or exc
                log_error(f"{type(shown).__name__}: {shown}")
            raise
        finally:
            # See docs/internals/cli.md for why an about-to-auto-resume leg must
            # not stamp a terminal status here (ADR-0035 terminal guard).
            if _api_usage is not None:
                run_manifest["api_usage"] = _api_usage.close()
                if _write_run_manifest is not None:
                    _write_run_manifest(run_manifest)
            will_auto_resume = (
                _terminal_status == "timed_out" and resume_on_timeout and not _auto_resumed
            )
            if _heartbeat_task is not None:
                _heartbeat_task.cancel()
                import asyncio as _asyncio2
                import contextlib as _contextlib

                with _contextlib.suppress(_asyncio2.CancelledError, Exception):
                    await _heartbeat_task

            # Shield teardown so iModel shutdown always runs (avoids leaked
            # rate-limit replenisher tasks that hang anyio.run forever).
            with CancelScope(shield=True):
                if _run_scratch is not None:
                    # The child's process group has ended by now, so nothing writes into the
                    # directory while it goes. An automatic resume makes a scratch of its own.
                    run_manifest["scratch"] = await run_sync(_finish_run_scratch, _run_scratch)
                    if run_manifest["scratch"]["removal_error"]:
                        log_error(run_manifest["scratch"]["removal_error"])
                    if _write_run_manifest is not None:
                        try:
                            _write_run_manifest(run_manifest)
                        except Exception as _manifest_exc:  # noqa: BLE001 — teardown goes on
                            log_error(
                                f"could not record the scratch directory's end: {_manifest_exc!r}"
                            )
                job_result_run = None if will_auto_resume else _job_result_run
                if notify or job_result_run is not None:
                    replies = _new_assistant_replies(branch, before_leg)
                    if replies:
                        final_response = replies[-1].response
                # Teardown can send the terminal notice while this process is live.
                # Publish the answer before that notice can mark the job finished.
                if job_result_run is not None and final_response and final_response.strip():
                    try:
                        await run_sync(job_result_run._write_mcp_final_response, final_response)
                    except (OSError, UnicodeEncodeError):
                        log_error("Could not write the final reply for the MCP job.")
                # Engine session id, used by teardown to tell a genuine failure
                # from a wrapper exception racing a still-live engine session. Only an
                # id the CLI reported in this process: one restored from the saved
                # branch belongs to an earlier turn, whose status says nothing of
                # this one.
                _engine_session_uid = getattr(
                    branch.chat_model.endpoint, "reported_session_id", None
                )
                registry = getattr(branch, "_context_providers", None)
                injection_stats = getattr(registry, "stats", None) if registry is not None else None
                _fold_injection_stats(_injection_totals, injection_stats)
                telemetry_extras = (
                    {"khive_injection": dict(_injection_totals)}
                    if not will_auto_resume and any(_injection_totals.values())
                    else None
                )
                telemetry_kw = {"extras": telemetry_extras} if telemetry_extras else {}
                try:
                    effective_status = await teardown_agent_persist(
                        live,
                        status=_terminal_status,
                        exception=_terminal_exc,
                        cwd=cwd,
                        engine_session_uid=_engine_session_uid,
                        defer_terminal=will_auto_resume,
                        **telemetry_kw,
                    )
                except cancelled_exc_classes() as exc:
                    # The owner drained before teardown propagated this cancellation.
                    _worker_spawn_teardown_cancel = exc
                    _terminal_status = "cancelled"
                    effective_status = (live or {}).get("worker_spawn_teardown_status")
                    if effective_status is None:
                        effective_status = "unknown"
                    _terminal_exc = exc
                    _propagating = True
                    will_auto_resume = False
                # Terminal-race tombstone, ordered after the teardown above (skipped
                # when auto-resume keeps the run alive -- the resumed leg's drain
                # consumes the steer instead). This ordering leaves no gap for a
                # control to slip through: the writer admits one only while the
                # session reads 'running', so anything admitted lands before the
                # transition and is visible to the sweep below, while anything
                # arriving later is refused at the writer. Teardown can also fail
                # and return the requested status without writing it, so the sweep
                # re-reads the stored session and declines a non-terminal one
                # rather than trusting call order.
                #
                # The `live` handle is closed by teardown's own `finally` by this
                # point, so the sweep gets a fresh connection rather than the
                # corpse -- a sweep that fails on a closed engine would otherwise
                # turn this whole tombstone path into one log line while the rows
                # it exists to close stay pending forever. Opened here (not inside
                # the sweep) so callers supplying their own handle, including
                # tests, keep doing so; it's still inside the sweep's must-not-raise
                # boundary, since StateDB re-raises out of __aenter__ and an
                # unguarded `async with` here would turn a completed run into a
                # reported infrastructure exception.
                # Not on a store in compatibility mode: its control rows are not ones this run
                # created, the only rows the profile lets it write (ADR-0128 D4), and the sweep's
                # native open would apply this package's schema to the store.
                from lionhub.state.compat import is_compatibility_store

                if not will_auto_resume and live and not is_compatibility_store(live.get("db")):
                    from lionhub.state.db import StateDB

                    try:
                        async with StateDB() as _sweep_db:
                            await _tombstone_pending_steers({**live, "db": _sweep_db})
                    except Exception as _sweep_exc:  # noqa: BLE001 — teardown must not raise
                        log_error(f"steer tombstone write failed: {_sweep_exc!r}")
                # Read before the status below is replaced by the one teardown handed back: a run
                # whose store did not take its terminal write records what it decided in the same
                # write as the status that is durable. A leg that defers its end to an auto-resume
                # decided none.
                _lost_write = (
                    None
                    if will_auto_resume
                    else lost_terminal_write(
                        live,
                        requested=_terminal_status,
                        exception=_terminal_exc,
                        returned=effective_status,
                    )
                )
                if effective_status != _terminal_status:
                    _terminal_status = effective_status
                from lionhub.state.db import SESSION_TERMINAL_STATUSES

                if _lost_write is not None:
                    run_manifest[TERMINAL_WRITE_LOST_FIELD] = _lost_write
                    if _lost_write_out is not None:
                        _lost_write_out[TERMINAL_WRITE_LOST_FIELD] = _lost_write
                run_manifest["status"] = _terminal_status
                if _terminal_status in SESSION_TERMINAL_STATUSES:
                    run_manifest["ended_at"] = time.time()
                if api_launch is not None:
                    # Every fold of the leg's context: what was folded, the estimate before and
                    # after, the cap. Empty when nothing was folded or the leg has no cap.
                    run_manifest["context_folds"] = list(_api_fold.records) if _api_fold else []
                if live and "signal_persist_dropped" in live:
                    # Signals this run's persistence could not confirm it wrote, 0 included; the field is
                    # absent when teardown had no session of this run to unbind.
                    run_manifest["signal_persist_dropped"] = live["signal_persist_dropped"]
                if _terminal_status == "failed":
                    # The reason teardown decided for this run, so a reader of the run directory
                    # alone can tell a provider's refusal from a crash. A failure teardown decides
                    # (a missing required artifact, a failed operation) raises nothing here, and the
                    # session row is not a reliable record of it: a resume reuses the row, and one
                    # whose reopen or terminal write missed still carries an earlier run's reason. The
                    # exception speaks for a run teardown decided nothing for.
                    _decided = (live or {}).get("terminal_reason")
                    _error = _decided[1] if _decided and _decided[0] == _terminal_status else None
                    if _decided and _decided[0] != _terminal_status:
                        # Teardown writes only the outcome it decided, so a row reading another one
                        # was ended by another writer: a concurrent run of the session, or the state
                        # doctor sweeping it as stale. The error names this run's outcome and leaves
                        # the other writer's reason on the row.
                        _error = (
                            f"The session row reads {_terminal_status}, which this run's teardown "
                            f"did not write; this run's own outcome was {_decided[0]}."
                        )
                    if _error is None and _terminal_exc is not None:
                        _, _error, _ = resolve_run_reason(
                            status=_terminal_status, exception=_terminal_exc
                        )
                    run_manifest["error"] = (
                        _error or "The run failed; no reason was recorded for it."
                    )
                    if isinstance(_terminal_exc, ProviderError) and _terminal_exc.partial_text:
                        # The turn delivered text before the provider error ended it. The text
                        # is in the saved branch and the command writes it out; the status
                        # stays failed.
                        run_manifest["partial"] = True
                    if isinstance(_terminal_exc, FailedRoundError):
                        _stop_reason = _terminal_exc.details.get("stop_reason")
                        if _stop_reason:
                            run_manifest["stop_reason"] = _stop_reason
                        for _detail in ("stop_details", "refusal"):
                            if _terminal_exc.details.get(_detail):
                                run_manifest[_detail] = _terminal_exc.details[_detail]
                if _write_run_manifest is not None:
                    _write_run_manifest(run_manifest)
                # Only when an exception is on its way out, because then this is
                # the last code this run executes. On every other path the status
                # is not settled yet: an empty resumed stream becomes `failed`
                # below, and a leg about to auto-resume has no terminal answer of
                # its own at all — the resumed leg carries the notice. Sending
                # from here regardless is how a notifier was told `timed_out`
                # about a run that went on to complete.
                if _propagating:
                    await _deliver_direct_notice()
                # Unregister after teardown fires the terminal transition.
                if _notify_scope_name is not None:
                    unregister_flow_notify_scope(_notify_scope_name)
                if _notify_outcome_scope_name is not None:
                    from lionhub.state.lifecycle.notify_settings import (
                        unregister_run_notify_outcome_scope,
                    )

                    unregister_run_notify_outcome_scope(_notify_outcome_scope_name)
                await branch.mdls.shutdown()
            if _worker_spawn_teardown_cancel is not None:
                raise _worker_spawn_teardown_cancel

        is_resume = bool(resume or continue_last)
        if is_resume and _terminal_status == "completed" and not (res or "").strip():
            log_error(
                f"resume produced empty stream — session may be expired; "
                f"re-run without -r (resume target: {resume or 'last'})"
            )
            _terminal_status = "failed"
            run_manifest["status"] = _terminal_status
            run_manifest["ended_at"] = time.time()
            # Failed only now, after teardown wrote run.json, so the reason is written here.
            run_manifest["error"] = (
                "Resume produced an empty stream; the session may have expired "
                f"(resume target: {resume or 'last'})."
            )
            if _write_run_manifest is not None:
                _write_run_manifest(run_manifest)

        # Bookkeeping, and the terminal notice below has not gone out yet. This call
        # reports its own failures rather than raising for exactly that reason: a
        # run that finished still owes its answer to whoever asked to be told, and
        # an unwritable convenience pointer is not a reason to withhold it.
        save_last_branch_pointer(run.run_id, branch_id)

        session_id = live.get("session_id") if live else None

        if _terminal_status == "timed_out" and resume_on_timeout and not _auto_resumed:
            from lionhub.orchestration.console import warn

            # Deliberately keyed on this branch rather than on the `will_auto_resume`
            # the teardown computed: that one is read before the effective status is
            # applied, so the two can disagree, and a notice suppressed on a
            # recursion that then does not happen is a leg that never reports at all.
            # The recursive call owns the notice, which is what makes it the final
            # status rather than an interim one.
            warn(
                f"[auto-resume] session {session_id or branch_id} timed out after "
                f"{timeout}s — resuming once with 'continue and conclude the task'"
            )
            # Carry the model forward explicitly — None would let the profile's
            # model silently re-apply on the resumed leg, switching models.
            _effective_cfg = branch.chat_model.endpoint.config
            _effective_model_str = f"{_effective_cfg.provider}/{_effective_cfg.kwargs.get('model')}"
            # The resumed leg allocates a run directory of its own, while the job reads this run's
            # run.json: a skip window the leg opens is marked here too, and a terminal write its
            # store refused is copied here when it ends.
            _resumed_lost: dict[str, Any] = {}
            if _worker_spawn_owner is None:
                _resumed_continuation = _NO_PERSISTED_EXECUTION
            else:
                _resumed_continuation = _prepare_worker_spawn_continuation(
                    _worker_spawn_owner,
                    db_close_witness=live.get("worker_spawn_db_closed"),
                )
            try:
                return await _run_agent(
                    _effective_model_str,
                    "continue and conclude the task",
                    yolo=yolo,
                    verbose=verbose,
                    theme=theme,
                    resume=branch_id,
                    effort=effort,
                    agent_name=agent_name,
                    cwd=cwd,
                    timeout=timeout,
                    fast=fast,
                    invocation_id=invocation_id,
                    project=project,
                    bypass=bypass,
                    resume_on_timeout=resume_on_timeout,
                    idle_timeout=idle_timeout,
                    workload=workload,
                    notify=notify,
                    notify_sender=notify_sender,
                    mcp_config=None if mcp_copy_written else mcp_config,
                    no_mcp_config=no_mcp_config,
                    **(
                        {"mcp_submit_snapshot": mcp_submit_snapshot}
                        if mcp_submit_snapshot is not None
                        else {}
                    ),
                    account=account,
                    _auto_resumed=True,
                    _worker_spawn_continuation=_resumed_continuation,
                    _injection_totals=_injection_totals,
                    _parent_marker=(
                        _signal_window_marker(run_manifest, run.state_root)
                        if isinstance(run, RunDir)
                        else None
                    ),
                    _lost_write_out=_resumed_lost,
                    _job_result_run=_job_result_run,
                )
            finally:
                # The marker's own write runs on a daemon thread that can fail or not finish, and this
                # run writes no manifest after its leg, so the time it holds is written again here.
                _degraded_at = run_manifest.get(SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD)
                if _degraded_at is not None and isinstance(run, RunDir):
                    _write_signal_persistence_degraded_at(run.state_root, _degraded_at)
                if _resumed_lost.get(TERMINAL_WRITE_LOST_FIELD) is not None:
                    run_manifest[TERMINAL_WRITE_LOST_FIELD] = _resumed_lost[
                        TERMINAL_WRITE_LOST_FIELD
                    ]
                    if isinstance(run, RunDir):
                        try:
                            _write_terminal_write_lost(
                                run.state_root, run_manifest[TERMINAL_WRITE_LOST_FIELD]
                            )
                        except Exception as _copy_exc:  # noqa: BLE001 — the leg's own answer stands
                            log_error(
                                f"could not record the resumed leg's lost terminal write: {_copy_exc!r}"
                            )

        # Past every line that can still move the status, and past the recursion
        # that would have made this leg an interim one, so this is the run's answer.
        await _deliver_direct_notice()

        return res or "", provider, branch_id, _terminal_status, session_id


def add_agent_subparser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    agent = subparsers.add_parser(
        "agent",
        help="Spawn one-shot subagent (blocking); prints final response.",
        description=(
            "Spawn a single subagent and wait for its final response. "
            "Flags may appear anywhere relative to the positionals. "
            "Use -r / -c to continue a previous conversation. "
            "Use -a to load a profile from .lionagi/agents/. "
            "Use --preset to apply a built-in agent configuration. "
            "Use --form to load and validate structured inputs before invoking. "
            "Use --context-from to hand a new agent distilled context from a "
            "prior session/branch/run/file."
        ),
    )
    agent.add_argument(
        "query",
        nargs="*",
        metavar="[MODEL] PROMPT",
        help=(
            "Optional model spec followed by the prompt. Model is one of "
            "'claude', 'codex', 'gemini-code' (defaults), or a full spec like "
            "'claude/opus'; omit it when -a / --resume / -c provides one. "
            "The prompt may instead be passed via --prompt or --prompt-file."
        ),
    )
    agent.add_argument(
        "--prompt",
        dest="prompt_flag",
        metavar="TEXT",
        default=None,
        help=(
            "The instruction, as a flag instead of the positional PROMPT. Give it one way or "
            "the other, never both."
        ),
    )
    agent.add_argument(
        "--prompt-file",
        metavar="PATH",
        default=None,
        help=(
            "Read the instruction from a file; '-' reads stdin, which is heredoc-friendly. The "
            "file is read once at spawn, so editing it afterwards cannot change the run."
        ),
    )
    agent.add_argument(
        "-a",
        "--agent",
        metavar="NAME",
        default=None,
        help=(
            "Load agent profile by name. Resolves "
            ".lionagi/agents/<NAME>/<NAME>.md first, then .lionagi/agents/<NAME>.md, "
            "then a trusted+enabled plugin's declared profile "
            "('<plugin>/<NAME>', or a bare NAME when only one plugin declares it). "
            "Profile provides system prompt, default model, effort, yolo, "
            "timeout, idle_timeout, resume_on_timeout, workload. CLI flags override "
            "profile settings."
        ),
    )
    agent.add_argument(
        "--list-profiles",
        action="store_true",
        help=(
            "Print every agent profile -a would resolve, as JSON, and exit without running "
            "anything. Use it to find out what names are available here."
        ),
    )
    agent.add_argument(
        "-r",
        "--resume",
        metavar="BRANCH_ID",
        default=None,
        help=(
            "Continue a previous run by its branch id, keeping that conversation's history. "
            "Cannot be combined with --context-from, which is for starting fresh."
        ),
    )
    agent.add_argument(
        "-c",
        "--continue-last",
        action="store_true",
        help=(
            "Continue the most recently used branch, keeping its history, without having to "
            "look up its id."
        ),
    )
    agent.add_argument(
        "--preset",
        choices=_PRESET_CHOICES,
        default=None,
        metavar="NAME",
        help=(
            "Apply a built-in agent configuration preset. "
            f"Supported values: {', '.join(_PRESET_CHOICES)}. "
            "'coding' wires CodingToolkit with path-guard hooks "
            "and a coding system prompt; cwd defaults to the invocation directory."
        ),
    )
    agent.add_argument(
        "--form",
        metavar="SPEC",
        default=None,
        help=(
            "Path to a YAML or JSON work-form spec file. "
            "The spec declares typed fields and values; validation runs "
            "BEFORE any LLM call. Exits rc=1 on validation error. "
            "Validated values are injected into the prompt as structured context."
        ),
    )

    agent.add_argument(
        "--image",
        dest="image",
        metavar="PATH",
        action="append",
        default=None,
        help=(
            "Attach an image file to the prompt (repeatable, e.g. "
            "--image a.png --image b.jpg). Supported extensions: "
            f"{', '.join(sorted(_IMAGE_MEDIA_TYPES))}. Each file is read, "
            "base64-encoded, and attached as an image content part on the "
            "user message."
        ),
    )
    agent.add_argument(
        "--context-from",
        dest="context_from",
        metavar="REF",
        action="append",
        default=None,
        help=(
            "Inject distilled context from a prior session id, branch id, run id, "
            "or file path into this new branch's first instruction, above the "
            "prompt. Repeatable (concatenated in argv order); the total budget "
            "(see --context-budget) is shared across all refs. Rejected in "
            "combination with -r / --resume or -c / --continue-last."
        ),
    )
    agent.add_argument(
        "--context-budget",
        dest="context_budget",
        type=int,
        default=None,
        metavar="N",
        help=(
            f"Total token budget for --context-from content "
            f"(default {DEFAULT_CONTEXT_BUDGET_TOKENS}; ~4 chars/token)."
        ),
    )

    agent.add_argument("--mcp-submit-snapshot", default=None, help=argparse.SUPPRESS)
    agent.add_argument("--hooks-from-snapshot", default=None, help=argparse.SUPPRESS)
    agent.add_argument(
        "--mcp-config",
        dest="mcp_config",
        metavar="PATH",
        default=None,
        help=(
            "Read this MCP config and hand its servers to the leg explicitly. "
            "By default the nearest .mcp.json at or above the directory this "
            "command was run in is used, so the leg's tools come from the "
            "submission rather than from --cwd. The file is read once at spawn."
        ),
    )
    agent.add_argument(
        "--account",
        default=None,
        help=(
            "Run the leg on this named subscription account: a Claude Code token or a Codex "
            "home listed in LIONAGI_LOGINS_DIR (default ~/.lionagi/logins). Wins over the "
            "profile's 'account' and 'codex_home'. A name that does not resolve, or a token "
            "that cannot be read, refuses the launch."
        ),
    )
    agent.add_argument(
        "--workload",
        choices=WORKLOADS,
        default=None,
        help=(
            "How the leg counts against the machine-wide agent caps "
            "(LIONAGI_MAX_SUBAGENTS, LIONAGI_MAX_SUBAGENTS_HEAVY and the engine's own) and "
            "what it is held to. 'light' holds a CLI worker to what its engine can enforce "
            "(codex: workspace-write sandbox with no network; gemini: --sandbox; claude: "
            "without the Edit, Write and NotebookEdit tools, Bash kept, no sandbox; pi: "
            "refused) and counts against the total cap and its engine cap. 'heavy' keeps the "
            "configured sandbox and also counts against the heavy cap. Defaults to the "
            "profile's 'workload', else heavy."
        ),
    )
    agent.add_argument(
        "--idle-timeout",
        dest="idle_timeout",
        metavar="SECONDS",
        type=BoundedInt(1, MAX_TIMEOUT_SECONDS),
        default=None,
        help=(
            "Stop the run as stalled when the worker goes this many seconds without "
            "output after it has started answering. Defaults to the "
            "LIONAGI_WORKER_IDLE_TIMEOUT setting (600); a profile's 'idle_timeout' "
            "sets it for a role. Raise it for a model that thinks long between outputs. "
            "It watches a CLI worker's streamed output; an API-model leg's turn is bounded "
            "by --timeout instead."
        ),
    )
    agent.add_argument(
        "--no-mcp-config",
        dest="no_mcp_config",
        action="store_true",
        help=(
            "Hand the leg no MCP servers, and say so deliberately instead of "
            "arriving there by an empty search."
        ),
    )
    add_common_cli_args(agent)
    return agent


def _resolve_model_and_prompt(args: argparse.Namespace) -> tuple[str | None, str] | None:
    """Assign the positional bucket + --prompt/--prompt-file to (model, prompt).

    Returns None after logging a clear error."""
    query: list[str] = getattr(args, "query", None) or []
    flag_prompt = args.prompt_flag
    if args.prompt_file:
        if flag_prompt is not None:
            log_error("pass --prompt or --prompt-file, not both")
            return None
        if args.prompt_file == "-":
            flag_prompt = sys.stdin.read(MAX_SPEC_PROMPT_CHARS + 1)
        else:
            try:
                with Path(args.prompt_file).open() as prompt_stream:
                    flag_prompt = prompt_stream.read(MAX_SPEC_PROMPT_CHARS + 1)
            except OSError as exc:
                log_error(f"could not read --prompt-file: {exc}")
                return None
        if not flag_prompt.strip():
            log_error(f"--prompt-file {args.prompt_file!r} is empty")
            return None
    if len(query) > 2:
        log_error(
            "too many positional arguments — expected [MODEL] PROMPT. "
            "Did you forget to quote the prompt?"
        )
        return None
    if flag_prompt is not None:
        if len(query) == 2:
            log_error("prompt given twice (positionally and via --prompt/--prompt-file)")
            return None
        return (query[0] if query else None), flag_prompt
    if len(query) == 2:
        return query[0], query[1]
    if len(query) == 1:
        return None, query[0]
    log_error("no prompt given — pass it positionally, or via --prompt / --prompt-file")
    return None


@auto_register(area="agent", cli=CliDeclaration(seed="agent", parser_factory=add_agent_subparser))
def run_agent(args: argparse.Namespace) -> int:
    """Dispatch agent command."""
    if getattr(args, "list_profiles", False):
        from lionhub.orchestration.spawn import build_agent_profile_catalog

        print(json.dumps(build_agent_profile_catalog(), indent=2, sort_keys=True))
        return 0
    resolved = _resolve_model_and_prompt(args)
    if resolved is None:
        return 1
    model, prompt_text = resolved
    # --form: load, build, and validate BEFORE any LLM call.
    form_prompt_prefix: str = ""
    if getattr(args, "form", None):
        try:
            spec = _load_form_spec(args.form)
        except FileNotFoundError as exc:
            log_error(str(exc))
            return 1
        except ValueError as exc:
            log_error(str(exc))
            return 1

        try:
            work_form = _build_work_form(spec, args.form)
        except ValueError as exc:
            log_error(str(exc))
            return 1

        if work_form.status == "error":
            errs = "; ".join(work_form.validation_errors)
            log_error(f"form validation failed ({args.form}): {errs}")
            return 1

        # Validated — build a context block to prepend to the prompt.
        if work_form.values:
            form_prompt_prefix = _form_to_context_block(work_form) + "\n\n"

    prompt = form_prompt_prefix + prompt_text
    if len(prompt) > MAX_SPEC_PROMPT_CHARS:
        log_error(f"agent prompt exceeds maximum length of {MAX_SPEC_PROMPT_CHARS} characters")
        return 1

    # --image: load and validate BEFORE any LLM call, same contract as --form.
    image_uris: list[str] | None = None
    if getattr(args, "image", None):
        try:
            image_uris = _load_image_data_uris(args.image)
        except (FileNotFoundError, ValueError) as exc:
            log_error(str(exc))
            return 1

    has_model = model is not None or args.agent is not None
    if not has_model and not (args.resume or args.continue_last):
        log_error(
            "model or --agent is required unless --resume / -r or --continue-last / -c is set"
        )
        return 1

    try:
        result, provider, branch_id, terminal_status, session_id = run_async(
            _run_agent(
                model,
                prompt,
                yolo=args.yolo,
                verbose=args.verbose,
                theme=args.theme,
                resume=args.resume,
                continue_last=args.continue_last,
                effort=args.effort,
                agent_name=args.agent,
                cwd=args.cwd,
                timeout=args.timeout,
                fast=args.fast,
                invocation_id=getattr(args, "invocation", None),
                project=getattr(args, "project", None),
                bypass=getattr(args, "bypass", False),
                preset=getattr(args, "preset", None),
                resume_on_timeout=getattr(args, "resume_on_timeout", False),
                idle_timeout=getattr(args, "idle_timeout", None),
                workload=getattr(args, "workload", None),
                context_from=getattr(args, "context_from", None),
                context_budget=getattr(args, "context_budget", None),
                notify=getattr(args, "notify", None),
                notify_sender=getattr(args, "notify_sender", None),
                images=image_uris,
                mcp_config=getattr(args, "mcp_config", None),
                no_mcp_config=getattr(args, "no_mcp_config", False),
                account=getattr(args, "account", None),
                **(
                    {"mcp_submit_snapshot": args.mcp_submit_snapshot}
                    if getattr(args, "mcp_submit_snapshot", None) is not None
                    else {}
                ),
                **(
                    {"hooks_from_snapshot": args.hooks_from_snapshot}
                    if getattr(args, "hooks_from_snapshot", None) is not None
                    else {}
                ),
            )
        )
    except McpConfigError as exc:
        # An explicitly named --mcp-config that cannot be used: refuse at spawn,
        # rather than starting a leg whose tool surface is not what was asked for.
        log_error(str(exc))
        return 2
    except ContextFromError as exc:
        log_error(str(exc))
        return 2
    except NestedAgentRefusedError as exc:
        # Refused before any run exists; not retryable, so not the admission exit code.
        log_error(str(exc))
        return EXIT_CODE_BY_STATUS["failed"]
    except (InputTokenCapError, FailedRoundError):
        # The run logged it and recorded it in run.json as failed: exit with that status
        # rather than a traceback.
        return EXIT_CODE_BY_STATUS["failed"]
    except KeyboardInterrupt:
        return EXIT_CODE_BY_STATUS["aborted"]
    except SigtermInterrupt as exc:
        from lionhub.orchestration.console import warn

        warn(f"agent terminated by SIGTERM: {exc}")
        return EXIT_CODE_BY_STATUS["cancelled"]
    except BaseException as exc:
        if isinstance(exc, cancelled_exc_classes()):
            return EXIT_CODE_BY_STATUS["cancelled"]
        if _admission_refusal(exc) is not None:
            # The run already printed the one refusal line and recorded the cause on its row.
            return EXIT_CODE_SUBAGENT_REFUSED
        if isinstance(exc, ProviderError) and exc.partial_text:
            # The run recorded it as failed with partial=true. Write out what the turn
            # delivered (a verbose run already streamed it), then one line naming the error.
            if not args.verbose:
                print(f"\n{exc.partial_text}", flush=True)
            log_error(f"output is partial: the turn ended on {type(exc).__name__}: {exc}")
            return EXIT_CODE_PARTIAL_OUTPUT
        log_error(f"{type(exc).__name__}: {exc}")
        raise

    if not args.verbose:
        print(f"\n{result}" if result is not None else "", flush=True)

    hint(f'\n[to resume] li agent -r {branch_id} "..."')
    if session_id:
        hint(f"[status]    li agent status {session_id}")
    return EXIT_CODE_BY_STATUS.get(terminal_status, 1)
