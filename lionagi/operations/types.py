# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol

from pydantic import BaseModel, JsonValue

from lionagi.ln import AlcallParams
from lionagi.ln.fuzzy import FuzzyMatchKeysParams
from lionagi.ln.types import ModelConfig, Params
from lionagi.operations.schema.structure import Structure
from lionagi.protocols.action.tool import ToolRef
from lionagi.protocols.types import ID, SenderRecipient
from lionagi.service.imodel import iModel
from lionagi.utils import LIONAGI_HOME

from ._turn_origin import TurnOrigin

if TYPE_CHECKING:
    from lionagi.protocols.messages.instruction import Instruction
    from lionagi.session.branch import Branch

HandleValidation = Literal["raise", "return_value", "return_none"]


@dataclass(slots=True, frozen=True, init=False)
class MorphParam(Params):
    """Frozen, hashable base for morphism parameters."""

    _config: ClassVar[ModelConfig] = ModelConfig(none_as_sentinel=True)


@dataclass(slots=True, frozen=True, init=False)
class ChatParam(MorphParam):
    """Parameters for the chat/communicate morphism (guidance, context, response format, tool schemas)."""

    _config: ClassVar[ModelConfig] = ModelConfig(none_as_sentinel=True)
    guidance: JsonValue = None
    context: JsonValue = None
    sender: SenderRecipient = None
    recipient: SenderRecipient = None
    response_format: type[BaseModel] | dict = None
    structure: type[Structure] | str | None = None
    progression: ID.RefSeq = None
    tool_schemas: list[dict] = None
    images: list = None
    image_detail: Literal["low", "high", "auto"] = None
    plain_content: str = None
    include_token_usage_to_model: bool = False  # deprecated
    imodel: iModel = None
    imodel_kw: dict = None
    # Tri-state USER_PROMPT_SUBMIT disposition (see ._turn_origin.TurnOrigin).
    # Unset by default: a genuine outside caller lets the model-submission
    # boundary mint and fire. Internal callers set an explicit forwarded/
    # no-origin value to control whether that boundary fires again.
    turn_origin: TurnOrigin = None

    @classmethod
    def from_branch(cls, branch: "Branch", **overrides) -> "ChatParam":
        defaults = dict(
            sender=branch.user or "user",
            recipient=branch.id,
            images=[],
            image_detail="auto",
            plain_content="",
            imodel=branch.chat_model,
            imodel_kw={},
        )
        defaults.update(overrides)
        return cls(**defaults)


@dataclass(slots=True, frozen=True, init=False)
class RunParam(ChatParam):
    stream_persist: bool = False
    persist_dir: str | Path = LIONAGI_HOME / "logs" / "runs"
    snapshot_dir: str | Path | None = None


@dataclass(slots=True, frozen=True, init=False)
class InterpretParam(MorphParam):
    """Parameters for the interpret morphism (style, domain, sample writing)."""

    _config: ClassVar[ModelConfig] = ModelConfig(none_as_sentinel=True)
    domain: str = None
    style: str = None
    sample_writing: str = None
    imodel: iModel = None
    imodel_kw: dict = None


@dataclass(slots=True, frozen=True, init=False)
class ParseParam(MorphParam):
    """Parameters for the parse morphism (response format, fuzzy matching, error handling)."""

    _config: ClassVar[ModelConfig] = ModelConfig(none_as_sentinel=True)
    response_format: type[BaseModel] | dict = None
    structure: Structure | None = None
    fuzzy_match_params: FuzzyMatchKeysParams | dict = None
    handle_validation: HandleValidation = "raise"
    alcall_params: AlcallParams | dict = None
    imodel: iModel = None
    imodel_kw: dict = None


@dataclass(slots=True, frozen=True, init=False)
class ActionParam(MorphParam):
    """Parameters for the action/tool execution morphism (strategy, error handling, verbosity)."""

    _config: ClassVar[ModelConfig] = ModelConfig(none_as_sentinel=True)
    action_call_params: AlcallParams = None
    tools: ToolRef = None
    strategy: Literal["concurrent", "sequential"] = "concurrent"
    suppress_errors: bool = True
    verbose_action: bool = False


class Middle(Protocol):
    """Callable protocol advancing a branch by one assistant turn; canonical impls: ``communicate`` (API) and ``run_and_collect`` (CLI)."""

    async def __call__(
        self,
        branch: "Branch",
        instruction: "JsonValue | Instruction",
        chat_param: ChatParam,
        parse_param: ParseParam | None = None,
        clear_messages: bool = False,
        skip_validation: bool = False,
    ) -> Any: ...
