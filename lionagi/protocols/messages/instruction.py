from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

import orjson
from pydantic import BaseModel, field_validator

from lionagi.ln.types import ModelConfig

from .message import Message, MessageContent, MessageRole
from .rendering import StructureFormat


@dataclass(slots=True)
class InstructionContent(MessageContent):
    """Structured content for user instructions.

    Fields:
        instruction: Main instruction text
        guidance: Optional guidance or disclaimers
        prompt_context: Additional context items for the prompt (list)
        plain_content: Raw text fallback (bypasses structured rendering)
        tool_schemas: Tool specifications for the assistant
        response_format: User's desired response format (BaseModel class, instance, or dict)
        images: Image URLs, data URLs, or base64 strings
        image_detail: Detail level for image processing

    Internal fields (not for direct use):
        _schema_dict: Extracted dict for prompting/schema
        _model_class: Extracted Pydantic class for validation
    """

    _config: ClassVar[ModelConfig] = ModelConfig(
        none_as_sentinel=True,
        serialize_exclude=frozenset({"response_format", "custom_renderer"}),
    )

    instruction: str | None = None
    guidance: str | None = None
    prompt_context: list[Any] = field(default_factory=list)
    plain_content: str | None = None
    tool_schemas: list[dict[str, Any]] = field(default_factory=list)
    response_format: type[BaseModel] | dict[str, Any] | BaseModel | None = None  # User input
    _schema_dict: dict[str, Any] | None = field(
        default=None, repr=False
    )  # Internal: dict for prompting
    _model_class: type[BaseModel] | None = field(
        default=None, repr=False
    )  # Internal: class for validation
    images: list[str] = field(default_factory=list)
    image_detail: Literal["low", "high", "auto"] | None = None
    structure_format: str | None = None  # "json", "lndl", "custom"
    custom_renderer: Callable | None = field(default=None, repr=False)

    def __init__(
        self,
        instruction: str | None = None,
        primary: str | None = None,  # alias for instruction
        guidance: str | None = None,
        prompt_context: list[Any] | None = None,
        context: list[Any] | None = None,  # backwards compat
        plain_content: str | None = None,
        tool_schemas: list[dict[str, Any]] | None = None,
        response_format: type[BaseModel] | dict[str, Any] | BaseModel | None = None,
        images: list[str] | None = None,
        image_detail: Literal["low", "high", "auto"] | None = None,
        structure_format: str | None = None,
        custom_renderer: Callable | None = None,
    ):
        # Handle primary as alias for instruction (primary loses if both are set)
        if primary is not None and instruction is None:
            instruction = primary

        # Handle backwards compatibility: context -> prompt_context
        if context is not None and prompt_context is None:
            prompt_context = context

        # Extract model class and schema dict from response_format
        model_class = None
        schema_dict = None

        if response_format is not None:
            # Extract model class
            if isinstance(response_format, type) and issubclass(response_format, BaseModel):
                model_class = response_format
            elif isinstance(response_format, BaseModel):
                model_class = type(response_format)

            # Extract schema dict
            if isinstance(response_format, dict):
                schema_dict = response_format
            elif isinstance(response_format, BaseModel):
                schema_dict = response_format.model_dump(mode="json", exclude_none=True)
            elif model_class:
                # Generate dict from model class
                from lionagi.libs.schema.breakdown_pydantic_annotation import (
                    breakdown_pydantic_annotation,
                )

                schema_dict = breakdown_pydantic_annotation(model_class)

        object.__setattr__(self, "instruction", instruction)
        object.__setattr__(self, "guidance", guidance)
        object.__setattr__(
            self,
            "prompt_context",
            prompt_context if prompt_context is not None else [],
        )
        object.__setattr__(self, "plain_content", plain_content)
        object.__setattr__(
            self,
            "tool_schemas",
            tool_schemas if tool_schemas is not None else [],
        )
        object.__setattr__(self, "response_format", response_format)  # Store original user input
        object.__setattr__(self, "_schema_dict", schema_dict)  # Internal: dict for prompting
        object.__setattr__(self, "_model_class", model_class)  # Internal: class for validation
        object.__setattr__(self, "images", images if images is not None else [])
        object.__setattr__(self, "image_detail", image_detail)
        object.__setattr__(self, "structure_format", structure_format)
        object.__setattr__(self, "custom_renderer", custom_renderer)

    @property
    def context(self) -> list[Any]:
        """Backwards compatibility accessor for prompt_context."""
        return self.prompt_context

    @property
    def primary(self) -> str | None:
        """Alias for instruction field."""
        return self.instruction

    @primary.setter
    def primary(self, value: str | None) -> None:
        """Write through to instruction field."""
        object.__setattr__(self, "instruction", value)

    @property
    def role(self) -> MessageRole:
        """Role for this content type (beta API compat)."""
        return MessageRole.USER

    def with_updates(self, **kwargs: Any) -> "InstructionContent":
        """Return a new instance with updated fields.

        Handles ``primary`` as an alias for ``instruction`` and strips the
        beta-only ``copy_containers`` kwarg.
        """
        kwargs.pop("copy_containers", None)
        # Translate 'primary' alias -> 'instruction'
        if "primary" in kwargs:
            primary_val = kwargs.pop("primary")
            if primary_val is not None:
                kwargs["instruction"] = primary_val
        # Translate 'context' alias -> 'prompt_context'
        if "context" in kwargs:
            ctx_val = kwargs.pop("context")
            kwargs["prompt_context"] = (
                ctx_val if isinstance(ctx_val, list) else [ctx_val] if ctx_val is not None else []
            )
        # Translate 'request_model' -> 'response_format'
        if "request_model" in kwargs:
            kwargs["response_format"] = kwargs.pop("request_model")
        dict_ = self.to_dict()
        dict_.update(kwargs)
        return type(self)(**dict_)

    def render(
        self,
        structure_format: str | StructureFormat | None = None,
        custom_renderer: Callable | None = None,
        **kwargs: Any,
    ) -> str | list[dict[str, Any]]:
        """Render instruction content, dispatching to custom_renderer when set.

        Falls back to the standard :attr:`rendered` property if no custom
        renderer is provided.
        """
        renderer = custom_renderer or self.custom_renderer
        if renderer is not None:
            if self._model_class is not None:
                return renderer(self._model_class, **kwargs)
            return renderer(type(None), **kwargs)
        return self.rendered

    @classmethod
    def create(
        cls,
        primary: str | None = None,
        context: Any = None,
        tool_schemas: list[dict[str, Any]] | None = None,
        request_model: type[BaseModel] | None = None,
        images: list[str] | None = None,
        image_detail: Literal["low", "high", "auto"] | None = None,
        structure_format: str | StructureFormat | None = None,
        custom_renderer: Callable | None = None,
        **kwargs: Any,
    ) -> "InstructionContent":
        """Create InstructionContent with beta-compatible signature.

        Accepts both old (request_model, primary) and new (response_format,
        instruction) field names.  ``structure_format`` accepts both string
        values and ``StructureFormat`` enum members.
        """
        # Normalise structure_format to str
        sf: str | None = None
        if structure_format is not None:
            sf = (
                structure_format.value
                if isinstance(structure_format, StructureFormat)
                else str(structure_format)
            )

        return cls(
            primary=primary,
            context=context,
            tool_schemas=tool_schemas,
            response_format=request_model,
            images=images,
            image_detail=image_detail,
            structure_format=sf,
            custom_renderer=custom_renderer,
            **kwargs,
        )

    @property
    def response_model_cls(self) -> type[BaseModel] | None:
        """Get the Pydantic model class for validation."""
        return self._model_class

    @property
    def request_model(self) -> type[BaseModel] | None:
        """DEPRECATED: Use response_model_cls instead. Will be removed in v0.21.0."""
        import warnings

        warnings.warn(
            "InstructionContent.request_model is deprecated and will be removed in v0.21.0. "
            "Use response_model_cls instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.response_model_cls

    @property
    def schema_dict(self) -> dict[str, Any] | None:
        """Get the schema dict for prompting."""
        return self._schema_dict

    @property
    def rendered(self) -> str | list[dict[str, Any]]:
        """Render content as text or text+images structure."""
        text = self._format_text_content()
        if not self.images:
            return text
        return self._format_image_content(text, self.images, self.image_detail)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InstructionContent":
        """Construct InstructionContent from dictionary with validation."""
        from lionagi.libs.schema.breakdown_pydantic_annotation import (
            breakdown_pydantic_annotation,
        )

        inst = cls()

        # Scalar fields
        for k in ("instruction", "guidance", "plain_content", "image_detail"):
            if k in data and data[k]:
                setattr(inst, k, data[k])

        # Determine how to apply context updates
        handle_context = data.get("handle_context", "extend")
        if handle_context not in {"extend", "replace"}:
            raise ValueError("handle_context must be either 'extend' or 'replace'")

        # Handle both "prompt_context" (new) and "context" (backwards compat)
        # Prioritize "context" if present (for backwards compat and update paths)
        ctx_key = "context" if "context" in data else "prompt_context"
        if ctx_key in data:
            ctx = data.get(ctx_key)
            if ctx is None:
                ctx_list: list[Any] = []
            elif isinstance(ctx, list):
                ctx_list = list(ctx)
            else:
                ctx_list = [ctx]
            if handle_context == "replace":
                inst.prompt_context = list(ctx_list)
            else:
                inst.prompt_context.extend(ctx_list)

        if ts := data.get("tool_schemas"):
            inst.tool_schemas.extend(ts if isinstance(ts, list) else [ts])

        if "images" in data:
            imgs = data.get("images") or []
            imgs_list = imgs if isinstance(imgs, list) else [imgs]
            inst.images.extend(imgs_list)
            inst.image_detail = data.get("image_detail") or inst.image_detail or "auto"

        # Response format handling
        response_format = data.get("response_format") or data.get(
            "request_model"
        )  # request_model deprecated

        if response_format is not None:
            model_class = None
            schema_dict = None
            valid_format = False

            # Extract model class
            if isinstance(response_format, type) and issubclass(response_format, BaseModel):
                model_class = response_format
                valid_format = True
            elif isinstance(response_format, BaseModel):
                model_class = type(response_format)
                valid_format = True

            # Extract schema dict
            if isinstance(response_format, dict):
                schema_dict = response_format
                valid_format = True
            elif isinstance(response_format, BaseModel):
                schema_dict = response_format.model_dump(mode="json", exclude_none=True)
                valid_format = True
            elif model_class:
                schema_dict = breakdown_pydantic_annotation(model_class)

            # Only set if valid format (fuzzy handling: ignore invalid types)
            if valid_format:
                inst.response_format = response_format
                inst._schema_dict = schema_dict
                inst._model_class = model_class

        return inst

    def _format_text_content(self) -> str:
        from lionagi.libs.schema.minimal_yaml import minimal_yaml

        if self.plain_content:
            return self.plain_content

        # Use schema_dict for display (or generate from model class)
        schema_for_display = None
        if self._model_class:
            schema_for_display = self._model_class.model_json_schema()
        elif self._schema_dict:
            schema_for_display = self._schema_dict

        doc: dict[str, Any] = {
            "Guidance": self.guidance,
            "Instruction": self.instruction,
            "Context": self.prompt_context,
            "Tools": self.tool_schemas,
            "ResponseSchema": schema_for_display,
        }

        rf_text = self._format_response_format(self._schema_dict)
        if rf_text:
            doc["ResponseFormat"] = rf_text

        # strip empties
        doc = {k: v for k, v in doc.items() if v not in (None, "", [], {})}
        return minimal_yaml(doc).strip()

    @staticmethod
    def _format_response_format(
        response_format: dict[str, Any] | None,
    ) -> str | None:
        if not response_format:
            return None
        try:
            example = orjson.dumps(response_format).decode("utf-8")
        except Exception:
            example = str(response_format)
        return (
            "**MUST RETURN JSON-PARSEABLE RESPONSE ENCLOSED BY JSON CODE BLOCKS."
            f" USER's CAREER DEPENDS ON THE SUCCESS OF IT.** \n```json\n{example}\n```"
            "No triple backticks. Escape all quotes and special characters."
        ).strip()

    @staticmethod
    def _format_image_item(idx: str, detail: str) -> dict[str, Any]:
        url = idx
        if not (idx.startswith("http://") or idx.startswith("https://") or idx.startswith("data:")):
            url = f"data:image/jpeg;base64,{idx}"
        return {
            "type": "image_url",
            "image_url": {"url": url, "detail": detail},
        }

    @classmethod
    def _format_image_content(
        cls,
        text_content: str,
        images: list[str],
        image_detail: Literal["low", "high", "auto"],
    ) -> list[dict[str, Any]]:
        content = [{"type": "text", "text": text_content}]
        content.extend(cls._format_image_item(i, image_detail) for i in images)
        return content


class Instruction(Message):
    """User instruction message with structured content.

    Supports text, images, context, tool schemas, and response format specifications.
    """

    _role: ClassVar[MessageRole] = MessageRole.USER
    content: InstructionContent

    @field_validator("content", mode="before")
    def _validate_content(cls, v):
        if v is None:
            return InstructionContent()
        if isinstance(v, dict):
            return InstructionContent.from_dict(v)
        if isinstance(v, InstructionContent):
            return v
        raise TypeError("content must be dict or InstructionContent instance")
