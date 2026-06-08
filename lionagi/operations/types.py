# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol

from pydantic import BaseModel, JsonValue

from lionagi.ln import AlcallParams
from lionagi.ln.fuzzy import FuzzyMatchKeysParams
from lionagi.ln.types import ModelConfig, Params
from lionagi.protocols.action.tool import ToolRef
from lionagi.protocols.structure.base import Structure
from lionagi.protocols.types import ID, SenderRecipient
from lionagi.service.imodel import iModel
from lionagi.utils import LIONAGI_HOME

if TYPE_CHECKING:
    from lionagi.protocols.messages.instruction import Instruction
    from lionagi.session.branch import Branch

HandleValidation = Literal["raise", "return_value", "return_none"]


@dataclass(slots=True, frozen=True, init=False)
class MorphParam(Params):
    """Base class for morphism parameters (invariants).

    MorphParams represent the invariant properties that define a morphism
    in LionAGI's categorical framework. They are frozen (immutable) and
    hashable, enabling reproducible operations and efficient caching.

    Morphisms are the fundamental abstraction in LionAGI - they represent
    transformations between message states with well-defined parameters.
    """

    _config: ClassVar[ModelConfig] = ModelConfig(none_as_sentinel=True)


@dataclass(slots=True, frozen=True, init=False)
class ChatParam(MorphParam):
    """Parameters for chat/communicate morphism.

    Defines the invariant properties of a chat operation, including
    guidance, context, response format, and LLM-visible content.

    Note: 'context' field contains prompt context (LLM-visible facts).
    This gets mapped to InstructionContent.prompt_context during message creation.
    """

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
    """Parameters for interpret morphism.

    Defines interpretation style, domain, and sample writing for
    transforming content according to specified guidelines.
    """

    _config: ClassVar[ModelConfig] = ModelConfig(none_as_sentinel=True)
    domain: str = None
    style: str = None
    sample_writing: str = None
    imodel: iModel = None
    imodel_kw: dict = None


@dataclass(slots=True, frozen=True, init=False)
class ParseParam(MorphParam):
    """Parameters for parse morphism.

    Defines parsing behavior including response format validation,
    fuzzy matching, and error handling strategies.
    """

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
    """Parameters for action/tool execution morphism.

    Defines tool execution strategy, error handling, and verbosity
    for action-based operations.
    """

    _config: ClassVar[ModelConfig] = ModelConfig(none_as_sentinel=True)
    action_call_params: AlcallParams = None
    tools: ToolRef = None
    strategy: Literal["concurrent", "sequential"] = "concurrent"
    suppress_errors: bool = True
    verbose_action: bool = False


class Middle(Protocol):
    """Middle stage of ``operate()`` — advances the branch by one assistant turn.

    A middle takes a new user turn (``instruction``) and the conversation
    invariants (``chat_param``), runs the model, optionally parses the
    response into a typed model, and returns raw text, a dict, or a
    validated ``BaseModel``.

    Canonical implementations in the operations package:

    - ``lionagi.operations.communicate.communicate`` — one-shot chat + parse,
      used for API endpoints.
    - ``lionagi.operations.run.run.run_and_collect`` — stream via ``run()``,
      accumulate assistant text, then parse; used for CLI endpoints.

    Custom middles are valid as long as they honor this contract. Examples:
    a recorded-replay middle for tests, a cached middle for deterministic
    pipelines, or a retry-wrapped middle.

    Required positional/keyword contract (mirrors ``communicate`` so the
    two are substitutable):

    - ``branch`` — the Branch executing the turn; the middle MUST attach
      the user instruction and assistant response to ``branch.msgs``.
    - ``instruction`` — the new user turn (JSON value or ``Instruction``).
    - ``chat_param`` — frozen chat/run parameters (may be ``RunParam``).
    - ``parse_param`` — if provided and has ``response_format``, the middle
      MUST parse the assistant text into that format.
    - ``clear_messages`` — if True, clear prior branch messages before the
      turn.
    - ``skip_validation`` — if True, return raw text regardless of
      ``parse_param``.

    Return: the assistant response as text, dict, or validated BaseModel;
    ``None`` if the model produced no text.
    """

    async def __call__(
        self,
        branch: "Branch",
        instruction: "JsonValue | Instruction",
        chat_param: ChatParam,
        parse_param: ParseParam | None = None,
        clear_messages: bool = False,
        skip_validation: bool = False,
    ) -> Any: ...
