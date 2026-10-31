# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li agent` — one-shot or resumed single-agent conversation."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from pydantic import BaseModel, field_validator

from lionagi import Branch
from lionagi._auto import CliDeclaration, auto_register
from lionagi._cli_worker_context import _bind_cli_worker_recorder
from lionagi._errors import ConfigurationError, FailedRoundError, InputTokenCapError
from lionagi._errors import TimeoutError as LionTimeoutError
from lionagi._spec_limits import MAX_SPEC_PROMPT_CHARS
from lionagi.ln.concurrency import (
    CancelScope,
    SigtermInterrupt,
    cache_cancelled_exc_class,
    cancelled_exc_classes,
    fail_after,
    run_async,
    sleep,
)
from lionagi.protocols.generic.log import DataLoggerConfig
from lionagi.protocols.messages import ActionRequest, AssistantResponse
from lionagi.providers._provider_errors import _WorkerSpawnAdmissionError
from lionagi.providers._subagent_admission import (
    DEFAULT_WORKLOAD,
    WORKLOADS,
    NestedAgentRefusedError,
    SubagentAdmissionRefusedError,
)
from lionagi.service._api_telemetry import collect_api_turn
from lionagi.state import provenance as _provenance
from lionagi.state.artifact_verifier import resolve_artifact_contract
from lionagi.state.cli_workers import _prepare_worker_spawn_continuation
from lionhub.serve.mcp.config import JOB_MARKER_ENV_VAR

from ._agent_depth import stamp_agent_depth
from ._api_usage import APIUsageCollector
from ._argtypes import BoundedInt
from ._context_from import (
    DEFAULT_CONTEXT_BUDGET_TOKENS,
    ContextFromError,
    resolve_and_build_context_block,
)
from ._logging import hint, log_error
from ._mcp_resolve import (
    McpConfigError,
    McpResolution,
    explicit_mcp_config_path,
    resolve_spawn_mcp_servers,
)
from ._providers import (
    _CLAUDE_PROVIDER_NAMES,
    BACKENDS,
    MAX_TIMEOUT_SECONDS,
    PROVIDER_BYPASS_KWARGS,
    PROVIDER_EFFORT_KWARG,
    PROVIDER_FAST_KWARGS,
    PROVIDER_YOLO_KWARGS,
    PROVIDERS_EFFORT_VIA_MODEL_NAME,
    _add_codex_account_integration_kwargs,
    _add_workload_kwarg,
    _clamp_claude_effort,
    _clamp_codex_effort,
    add_common_cli_args,
    build_chat_model,
    build_deadline_preamble,
    load_agent_profile,
    normalize_effort,
    resolve_persisted_effort,
)
from ._runs import (
    SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD,
    TERMINAL_WRITE_LOST_FIELD,
    RunDir,
    _signal_window_marker,
    _write_signal_persistence_degraded_at,
    _write_terminal_write_lost,
    allocate_run,
    find_branch,
    load_last_branch,
    lost_terminal_write,
    resolve_run_reason,
    save_last_branch_pointer,
    setup_agent_persist,
    teardown_agent_persist,
)
from ._util import (
    EXIT_CODE_BY_STATUS,
    EXIT_CODE_SUBAGENT_REFUSED,
    classify_exception,
    validate_cwd_exists,
)

# Preset names supported by --preset.
_PRESET_CHOICES = ("coding",)
# The name of the account whose API key the launcher handed this process, never the key
# itself. It follows the routed-account name rule (``_ACCOUNT_NAME_RE`` and ``_looks_like_a_key``
# in lionagi/config.py), so a mixed-case value or one containing "sk-ant" is refused. This field
# also refuses one starting "sk-", which that rule admits.
_API_ACCOUNT_ENV = "LIONAGI_API_ACCOUNT"
# The handoff an automatic resume carries when the timed-out leg's setup returned no session:
# no running row exists for the resumed leg to adopt, so its setup starts as a fresh launch.
_NO_PERSISTED_EXECUTION = object()
# The MCP choice a branch was launched with, replayed by a resume that names none.
_MCP_LAUNCH_KEY = "mcp_launch"
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
    if not os.environ.get(JOB_MARKER_ENV_VAR):
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
    from lionagi.agent.spec import AgentSpec

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
    from lionagi.agent.context_fold import _FOLD_AT, _FOLD_TARGET, _TAIL_MARGIN

    settings = {key: values.get(key) for key in _API_POLICY_KEYS}
    for key, value in settings.items():
        if key not in values or (stored and value is None):
            continue
        if key == "tool_result_max_chars":
            valid = isinstance(value, int) and not isinstance(value, bool) and value >= 1024
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
    from lionagi.tools._seatbelt import SANDBOX_EXEC, seatbelt_available

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
):
    """The AgentSpec for an API-model leg: the named tool set, path-guarded to cwd, and for
    the write set the Seatbelt policy its editor and bash run under."""
    from lionagi.agent.spec import AgentSpec, _wire_secure_guards

    spec = AgentSpec.compose(
        role, effort=effort, tools=["coding"], system_prompt=system_prompt, cwd=cwd
    )
    spec.coding_tools = _API_TOOL_SETS[tools]
    if tools in _API_CONFINED_TOOL_SETS:
        from lionagi.tools._seatbelt import SeatbeltPolicy

        spec.seatbelt = SeatbeltPolicy.for_workspace(
            cwd or Path.cwd(), network=network, _command_env_names=_command_env_names
        )
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
):
    """Put the leg's input cap on its models and fold its context under it.

    An Anthropic model that names no output limit is given ``_API_MAX_OUTPUT_TOKENS``.
    Returns the branch's ContextFold, or None when the leg runs without a cap.
    """
    from lionagi.agent.context_fold import (
        _FOLD_AT,
        _FOLD_TARGET,
        _TAIL_MARGIN,
        install_context_fold,
    )
    from lionagi.ln import copy

    # Profile nulls have already been refused. Here None is an unset requested value.
    _api_requested_settings(
        {
            "tool_result_max_chars": tool_result_max_chars,
            "context_fold_at": context_fold_at,
            "context_fold_target": context_fold_target,
            "context_fold_tail_margin": context_fold_tail_margin,
            "prompt_caching": prompt_caching,
        },
        label="API launch policy",
        stored=True,
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
    models = {}
    for api_model in _api_models(branch):
        config = api_model.endpoint.config
        output_limit = (
            _API_MAX_OUTPUT_TOKENS
            if getattr(config, "provider", None) == "anthropic"
            and (config.kwargs or {}).get("max_tokens") is None
            else None
        )
        changes = (
            output_limit is not None
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
        models[id(api_model)] = selected
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
    (lionagi.agent.context_fold), a request the cap refused despite the fold's own
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
        raise FailedRoundError(
            f"{round_resumes + 1} passes of the tool loop in a row ended on a failed round: "
            f"{failed[1]}",
            details=details,
        )
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
    ``[api] refused`` line; an answer with no text fails the pass whatever its stop.
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
            reason = "no JSON object could be read from it"
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
    if not isinstance(response, dict):
        return False
    if "stop_reason" in response:
        if response["stop_reason"] != "max_tokens":
            return False
        return not any(
            isinstance(block, dict)
            and (
                block.get("type") == "tool_use"
                or (block.get("type") == "text" and str(block.get("text") or "").strip())
            )
            for block in response.get("content") or []
        )
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        if choices[0].get("finish_reason") != "length":
            return False
        message = choices[0].get("message") or {}
        return not str(message.get("content") or "").strip() and not message.get("tool_calls")
    if response.get("status") == "incomplete":
        if (response.get("incomplete_details") or {}).get("reason") != "max_output_tokens":
            return False
        for item in response.get("output") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                return False
            if item.get("type") == "message" and any(
                isinstance(part, dict) and str(part.get("text") or "").strip()
                for part in item.get("content") or []
            ):
                return False
        return True
    return False


def _pass_responses(branch, before: set) -> list:
    """The raw provider responses a pass ended on: the last two it added, the round that ended
    the loop and the answer turn after it. ``before`` holds the ids of the branch's messages
    before the pass."""
    added = [
        m for m in branch.msgs.messages if m.id not in before and isinstance(m, AssistantResponse)
    ]
    return [m.model_response for m in added[-2:]]


def _first_refusal(responses: list) -> tuple[str, Any] | None:
    """The refusal marker and the raw response of the first refused response in ``responses``,
    or None when none was refused."""
    for response in responses:
        marker = _provider_refusal(response)
        if marker is not None:
            return marker, response
    return None


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
        from lionagi.operations.lndl_middle import build_lndl_middle

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
    rather than inferred from its output an hour later. Silence is reserved
    for the two cases where it is accurate: a caller who asked for no servers
    at all, and a set that was resolved and handed over (which is reported as
    a hint, not a warning).

    ``forwarded`` is the caller's answer to "does this spawn hand the resolved
    set to the leg?", read off the request that spawn built. This function must
    not re-derive it from the provider name: a second list here is what let the
    message contradict the spawn. It decides what is said about a set that was
    resolved; it does not decide whether a resolution that produced no set at
    all is worth a word, since a search that found nothing has nothing to
    forward and would otherwise be reported by no caller at all.
    """
    from lionagi.cli._logging import warn

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

    from lionagi.cli._logging import hint, log_error, warn

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
        from lionagi.state.db import SESSION_TERMINAL_STATUSES

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
    _parent_marker: Callable[[], None] | None = None,
    _lost_write_out: dict[str, Any] | None = None,
    _worker_spawn_continuation: object | None = None,
) -> tuple[str, str, str, str, str | None]:
    """Execute one agent turn; returns (result, provider, branch_id, terminal_status, session_id).

    session_id is None whenever live persistence never started. An auto-resumed leg runs in a
    run directory of its own while the job reads the first run's run.json, so the first run hands
    it *_parent_marker*, which records a signal persistence skip window on that run.json too, and
    *_lost_write_out*, where the leg leaves a terminal write its store refused.
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
        mcp_resolution = resolve_spawn_mcp_servers(
            mcp_config,
            launch_dir=launch_dir,
            disabled=no_mcp_config,
        )
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
                # guard: say so at launch until CLI runs consume the assembly.
                from lionagi.cli._logging import warn

                warn(
                    f"agent profile {agent_name!r} declares a hooks assembly; "
                    "CLI-spawned runs do not apply profile hook assemblies yet "
                    "(Studio Operator turns do), so those hooks are NOT active "
                    "for this run"
                )

        # A codex leg's account integrations follow its role; with no role, or one that sets
        # neither field, the leg's argv is what it was before these fields existed.
        account_connectors = tuple(getattr(profile, "account_connectors", None) or ())
        contain_account_integrations = bool(getattr(profile, "contain_account_integrations", False))
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
            _, branch_path = find_branch(resume)
            resolved_branch_id = branch_path.stem
            if resolved_branch_id != resume:
                hint(f"[resume] prefix-matched {resume} → {resolved_branch_id}")
            branch = Branch.from_dict(json.loads(branch_path.read_text()))

        # Capture before the new-branch block below reassigns `branch`: the only
        # reliable "reopened existing" vs "minting new" signal once that block runs.
        is_resumed_branch = branch is not None

        # A resume that names no MCP flag replays the choice its branch was launched
        # with, searched from where it was launched rather than from where it is
        # resumed. A branch from before that choice was recorded resolves nothing
        # new, so its request keeps the servers its earlier run was handed.
        if is_resumed_branch and mcp_config is None and not no_mcp_config:
            launched = branch.metadata.get(_MCP_LAUNCH_KEY)
            if isinstance(launched, dict) and isinstance(launched.get("launch_dir"), str):
                recorded_config = launched.get("mcp_config")
                disabled = bool(launched.get("no_mcp_config"))
                # The resolver's own refusals name --mcp-config, which this caller
                # never passed, so a recorded config is refused here in its own words.
                how_to_go_on = (
                    "resume it with --mcp-config naming the config to use, or with --no-mcp-config"
                )
                if recorded_config is not None and not disabled:
                    try:
                        recorded_path = explicit_mcp_config_path(
                            recorded_config, launch_dir=launched["launch_dir"]
                        )
                    except (TypeError, RuntimeError) as exc:
                        # Not a path, or a home directory that does not resolve.
                        raise McpConfigError(
                            f"this branch was launched with the MCP config {recorded_config}, "
                            f"which can no longer be used ({exc}); {how_to_go_on}"
                        ) from exc
                    if not recorded_path.is_file():
                        raise McpConfigError(
                            f"this branch was launched with the MCP config {recorded_path}, "
                            f"which is no longer a readable file; {how_to_go_on}"
                        )
                try:
                    mcp_resolution = resolve_spawn_mcp_servers(
                        recorded_config,
                        launch_dir=launched["launch_dir"],
                        disabled=disabled,
                    )
                except McpConfigError as exc:
                    # Only a named file is refused; a search that finds nothing usable
                    # says so in the resolution instead.
                    raise McpConfigError(
                        f"this branch was launched with the MCP config {recorded_config}, "
                        f"which can no longer be used ({exc}); {how_to_go_on}"
                    ) from exc
            else:
                mcp_resolution = McpResolution(
                    None, "no_mcp_launch_recorded", None, Path(os.getcwd()).resolve()
                )

        if model_str is not None:
            from lionagi.agent.factory import _parse_agent_model_spec

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

        from lionagi.agent.factory import _canonical_provider, _reject_unforwardable_explicit_mcp

        _reject_unforwardable_explicit_mcp(
            provider,
            named_explicitly=mcp_resolution.explicit,
            asked_for_servers=bool(mcp_resolution.servers),
        )
        # Settings are looked up under the provider the name resolves to, so an alias such as
        # "openai-codex" is clamped and flagged as codex; `provider` stays as written.
        canonical = _canonical_provider(provider)

        if branch is None:
            # Codex blocks tool calls until file access is enabled. Surface this
            # even without verbose output; CLI or profile approval flags suppress it.
            if canonical == "codex" and not yolo and not bypass:
                from lionagi.cli._logging import warn

                warn(
                    "codex models need file access enabled or the agent may hang "
                    "silently on its first tool call. Re-run with --yolo for the "
                    "sandboxed default, or use an agent profile (-a). --bypass also "
                    "works but disables the sandbox."
                )
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
                mcp_exclusive=mcp_resolution.declared_empty or mcp_resolution.explicit,
                account_connectors=account_connectors,
                contain_account_integrations=contain_account_integrations,
                workload=workload,
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
            if not chat_model.is_cli and effort is not None:
                from lionagi.agent.factory import _set_api_effort

                if not _set_api_effort(
                    chat_model.endpoint.config.kwargs,
                    canonical,
                    model,
                    effort,
                    origin=effort_origin,
                ):
                    effort = None
            elif not chat_model.is_cli and requested_effort is not None:
                from lionagi.agent.factory import _set_api_effort

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
                from lionagi.agent.factory import request_kwargs_carry_forwarded_mcp

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
                from lionagi.agent.factory import (
                    create_agent,
                    request_carries_forwarded_mcp,
                )

                # Use profile.raw_body, not profile.system_prompt, to avoid
                # duplicating LION_SYSTEM_MESSAGE (see docs/internals/cli.md).
                profile_extra = (getattr(profile, "raw_body", None) or "") if profile else ""
                if api_launch is not None:
                    command_names = None
                    if api_launch["tools"] in _API_CONFINED_TOOL_SETS:
                        from lionagi._command_env_metadata import configured_command_env_names

                        active_reference = chat_model.endpoint.config.api_key_env
                        command_names = configured_command_env_names(
                            active_reference_names=(active_reference,)
                            if active_reference is not None
                            else (),
                        )
                    spec = _make_api_agent_spec(
                        cwd=cwd,
                        effort=effort or "high",
                        system_prompt=profile_extra or None,
                        role=profile_role if has_role_key else "reviewer",
                        tools=api_launch["tools"],
                        network=api_launch["network"],
                        _command_env_names=command_names,
                    )
                else:
                    spec = _make_coding_preset(
                        cwd=cwd,
                        effort=effort or "high",
                        system_prompt=profile_extra or None,
                        role=profile_role if has_role_key else "implementer",
                    )
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
                    from lionagi.agent.factory import register_profile_injection

                    register_profile_injection(branch, agent_name, profile)

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
            on_tool_loop = (
                not getattr(branch.chat_model, "is_cli", False)
                and _API_LAUNCH_KEY in branch.metadata
            )
            candidate = None
            if on_tool_loop:
                from lionagi.agent.factory import _set_api_effort

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
                    from lionagi.cli._logging import warn

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
                    from lionagi.providers.google.gemini_code import resolve_agy_model

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
            # A resumed leg re-spawns a CLI child, so it needs the server set handed
            # to it just as a new one does; the persisted branch carries the model,
            # not the caller's directory. Which providers can be given a set, and
            # over which transport, is not this call site's question to answer.
            from lionagi.agent.factory import (
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
                # A set named by a config file, or declared empty, is the whole set: the provider's
                # own servers are left out too. A set found by search joins what the provider finds
                # for itself, and a failed search leaves the earlier run's servers, as it leaves a new
                # leg to what its CLI finds.
                exclusive=mcp_resolution.declared_empty or mcp_resolution.explicit,
            )
            missing_profile = _missing_generated_codex_profile(cfg)
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
                from lionagi.agent.factory import _register_coding_tools

                command_names = None
                if api_launch["tools"] in _API_CONFINED_TOOL_SETS:
                    from lionagi._command_env_metadata import configured_command_env_names

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
        if not is_resumed_branch:
            # The file on the line is what a resume reads again, named or not: a
            # leg submitted through the MCP server is pointed at a snapshot of the
            # config its submission found, and that snapshot is its set.
            # A snapshot's resolution reports the config it was taken from and the
            # directory its search began in, both provenance; a resume must read the
            # snapshot itself, from this process's own directory.
            named_on_the_line = mcp_config is not None and mcp_resolution.source is not None
            line_file = (
                explicit_mcp_config_path(mcp_config, launch_dir=launch_dir)
                if named_on_the_line
                else None
            )
            branch.metadata[_MCP_LAUNCH_KEY] = {
                "mcp_config": str(line_file) if line_file is not None else None,
                "no_mcp_config": no_mcp_config,
                "launch_dir": str(launch_dir),
            }

        if is_resumed_branch:
            from lionagi.agent.factory import CREATE_AGENT_BRANCH_ORIGIN_KEY

            composed_via_create_agent = bool(branch.metadata.get(CREATE_AGENT_BRANCH_ORIGIN_KEY))
        else:
            composed_via_create_agent = took_create_agent_path
        if profile and profile.system_prompt and not composed_via_create_agent:
            branch.msgs.add_message(system=profile.system_prompt)

        if timeout is not None:
            preamble = build_deadline_preamble(timeout)
            prompt = preamble + prompt

        run = allocate_run()
        if isinstance(run, RunDir):
            run = run.with_execution_provenance(
                cwd=cwd or os.getcwd(),
                mcp_resolution=mcp_resolution,
                mcp_disabled=no_mcp_config,
            )
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
        _api_usage = (
            APIUsageCollector(run_id=run.run_id, branch_id=branch_id)
            if api_launch is not None
            else None
        )
        if _api_usage is not None:
            run_manifest["api_usage"] = _api_usage.snapshot()
        if context_from:
            run_manifest["context_from"] = list(context_from)
        if api_account is not None:
            run_manifest["api_account"] = api_account
        _write_run_manifest = getattr(run, "write_manifest", None)
        if _write_run_manifest is not None:
            _write_run_manifest(run_manifest)

        artifact_contract = resolve_artifact_contract(
            playbook_artifacts=None,
            agent_defaults=profile.artifact_defaults if profile else None,
        )
        from lionagi.state.compat import StateCompatibilityRefused

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
            from lionagi.cli._logging import warn
            from lionagi.ln._proc import current_pid_markers

            run_manifest.update(current_pid_markers())
            _write_run_manifest(run_manifest)
            warn(
                f"run {run.run_id}: persistence setup returned no session; "
                f"stop it with: li kill {run.run_id}"
            )
        if seed_injection_totals:
            await _seed_injection_stats(live, _injection_totals)

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
            from lionagi.cli.orchestrate._notify import (
                register_flow_notify_scope,
                unregister_flow_notify_scope,
            )
            from lionagi.state.lifecycle.notify_settings import (
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
            )

        # Bind this run into the notify.on_terminal handler at registration time so
        # a late outcome lands here or nowhere, never on a later run. Skipped when
        # --notify already owns this entity (a second override would double-fire).
        _notify_outcome_scope_name: str | None = None
        if not notify and _notify_session_id is not None:
            from lionagi.state.lifecycle.notify_settings import (
                register_run_notify_outcome_scope,
                unregister_run_notify_outcome_scope,
            )

            _notify_outcome_scope_name = register_run_notify_outcome_scope(
                run,
                entity_kind="session",
                entity_id=_notify_session_id,
                project_dir=cwd,
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
                    from lionagi.cli.orchestrate._notify import deliver_flow_notify_now

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
            _api_turn_index = 0

            async def _api_turn(instruction: str, turn_timeout: float | None):
                nonlocal _api_turn_index
                _api_turn_index += 1
                try:
                    with collect_api_turn(_api_usage, _api_turn_index):
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
        try:
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
                res = await _api_turn(prompt, timeout)
            else:
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
            from lionagi.cli._logging import warn

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
                # Engine session id, used by teardown to tell a genuine failure
                # from a wrapper exception racing a still-live engine session.
                _engine_session_uid = getattr(branch.chat_model.endpoint, "session_id", None)
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
                from lionagi.state.compat import is_compatibility_store

                if not will_auto_resume and live and not is_compatibility_store(live.get("db")):
                    from lionagi.state.db import StateDB

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
                from lionagi.state.db import SESSION_TERMINAL_STATUSES

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
                    if isinstance(_terminal_exc, FailedRoundError):
                        _stop_reason = _terminal_exc.details.get("stop_reason")
                        if _stop_reason:
                            run_manifest["stop_reason"] = _stop_reason
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
                    from lionagi.state.lifecycle.notify_settings import (
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
            from lionagi.cli._logging import warn

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
                    mcp_config=mcp_config,
                    no_mcp_config=no_mcp_config,
                    _auto_resumed=True,
                    _worker_spawn_continuation=_resumed_continuation,
                    _injection_totals=_injection_totals,
                    _parent_marker=(
                        _signal_window_marker(run_manifest, run.state_root)
                        if isinstance(run, RunDir)
                        else None
                    ),
                    _lost_write_out=_resumed_lost,
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
        from lionagi.cli._providers import build_agent_profile_catalog

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
                images=image_uris,
                mcp_config=getattr(args, "mcp_config", None),
                no_mcp_config=getattr(args, "no_mcp_config", False),
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
        from lionagi.cli._logging import warn

        warn(f"agent terminated by SIGTERM: {exc}")
        return EXIT_CODE_BY_STATUS["cancelled"]
    except BaseException as exc:
        if isinstance(exc, cancelled_exc_classes()):
            return EXIT_CODE_BY_STATUS["cancelled"]
        if _admission_refusal(exc) is not None:
            # The run already printed the one refusal line and recorded the cause on its row.
            return EXIT_CODE_SUBAGENT_REFUSED
        log_error(f"{type(exc).__name__}: {exc}")
        raise

    if not args.verbose:
        print(f"\n{result}" if result is not None else "", flush=True)

    hint(f'\n[to resume] li agent -r {branch_id} "..."')
    if session_id:
        hint(f"[status]    li agent status {session_id}")
    return EXIT_CODE_BY_STATUS.get(terminal_status, 1)
