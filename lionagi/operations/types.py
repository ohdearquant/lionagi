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

# "extraction": the text never yielded JSON (prompt/model at fault).
# "validation": JSON was recovered but the schema refused it (schema/data at fault).
ParseFailureKind = Literal["extraction", "validation"]


class ParseError(ValueError):
    """A response could not be turned into the requested model.

    Subclasses ``ValueError`` so existing ``except ValueError`` handlers keep
    working; ``kind`` is what lets a caller tell the two causes apart.
    """

    kind: ClassVar[ParseFailureKind]

    def __init__(self, message: str, *, validation_error: Exception | None = None):
        super().__init__(message)
        self.validation_error = validation_error


class ExtractionError(ParseError):
    """No JSON could be recovered from the text at all."""

    kind: ClassVar[ParseFailureKind] = "extraction"


class SchemaRejectedError(ParseError):
    """JSON was recovered intact, but it does not satisfy the response model.

    ``validation_error`` carries the underlying pydantic error, which names the
    offending field and value.
    """

    kind: ClassVar[ParseFailureKind] = "validation"


class UnparsedResponse(str):
    """The raw model text, returned when parsing gave up.

    Subclasses ``str`` so callers that only ever wanted the text are unaffected
    — equality, formatting and ``isinstance(x, str)`` all behave as before —
    while ``failure_kind`` and ``validation_error`` make the reason reachable
    without a second round trip.
    """

    __slots__ = ("failure_kind", "validation_error")

    def __new__(
        cls,
        text: str,
        *,
        failure_kind: ParseFailureKind,
        validation_error: Exception | None = None,
    ) -> "UnparsedResponse":
        obj = super().__new__(cls, text)
        obj.failure_kind = failure_kind
        obj.validation_error = validation_error
        return obj

    def __getnewargs_ex__(self) -> tuple[tuple[str], dict[str, Any]]:
        # Without this, copy/deepcopy/pickle call __new__(text) alone and
        # raise on the keyword-only arguments.
        return (str(self),), {
            "failure_kind": self.failure_kind,
            "validation_error": self.validation_error,
        }


@dataclass(slots=True, frozen=True, init=False, eq=False)
class MorphParam(Params):
    """Shallow-frozen morphism parameters; hashable only when recursively immutable."""

    _config: ClassVar[ModelConfig] = ModelConfig(none_as_sentinel=True)


@dataclass(slots=True, frozen=True, init=False, eq=False)
class ChatParam(MorphParam):
    """Parameters for the chat/communicate morphism (guidance, context, response format, tool schemas)."""

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


@dataclass(slots=True, frozen=True, init=False, eq=False)
class RunParam(ChatParam):
    stream_persist: bool = False
    persist_dir: str | Path = LIONAGI_HOME / "logs" / "runs"
    snapshot_dir: str | Path | None = None


@dataclass(slots=True, frozen=True, init=False, eq=False)
class InterpretParam(MorphParam):
    """Parameters for the interpret morphism (style, domain, sample writing)."""

    domain: str = None
    style: str = None
    sample_writing: str = None
    imodel: iModel = None
    imodel_kw: dict = None


@dataclass(slots=True, frozen=True, init=False, eq=False)
class ParseParam(MorphParam):
    """Parameters for the parse morphism (response format, fuzzy matching, error handling)."""

    response_format: type[BaseModel] | dict = None
    structure: Structure | None = None
    fuzzy_match_params: FuzzyMatchKeysParams | dict = None
    handle_validation: HandleValidation = "raise"
    alcall_params: AlcallParams | dict = None
    imodel: iModel = None
    imodel_kw: dict = None


@dataclass(slots=True, frozen=True, init=False, eq=False)
class ActionParam(MorphParam):
    """Parameters for the action/tool execution morphism (strategy, error handling, verbosity)."""

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
