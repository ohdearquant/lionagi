from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, field_validator

from lionagi.ln.types import ModelConfig

from .message import Message, MessageContent, MessageRole
from .validators import validate_image_url

# Pattern for a well-formed inline image data URI.  Only image/* MIME types
# are accepted; the payload must be non-empty base64.  This is intentionally
# restrictive — other data: schemes (HTML, SVG, JS) are rejected.
_DATA_IMAGE_RE = re.compile(r"^data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/]+=*$")

_INSTRUCTION_SERIALIZE_EXCLUDE: frozenset[str] = frozenset(
    {"response_format", "structure", "_structure_instance"}
)


@dataclass(slots=True)
class InstructionContent(MessageContent):
    """Structured content for user instructions."""

    _config: ClassVar[ModelConfig] = ModelConfig(
        none_as_sentinel=True,
        empty_as_sentinel=True,
        serialize_exclude=frozenset({"response_format", "structure", "_structure_instance"}),
    )

    instruction: str | None = None
    guidance: str | None = None
    prompt_context: list[Any] = field(default_factory=list)
    plain_content: str | None = None
    tool_schemas: list[dict[str, Any]] = field(default_factory=list)
    response_format: type[BaseModel] | dict[str, Any] | BaseModel | None = None
    structure: type | str | None = None
    _structure_instance: Any = field(default=None, repr=False)
    images: list[str] = field(default_factory=list)
    image_detail: Literal["low", "high", "auto"] | None = None

    def __init__(
        self,
        instruction: str | None = None,
        guidance: str | None = None,
        prompt_context: list[Any] | None = None,
        plain_content: str | None = None,
        tool_schemas: list[dict[str, Any]] | None = None,
        response_format: type[BaseModel] | dict[str, Any] | BaseModel | None = None,
        images: list[str] | None = None,
        image_detail: Literal["low", "high", "auto"] | None = None,
        structure: type | str | None = None,
    ):
        structure_cls = _resolve_structure_cls(structure)
        structure_inst = _build_structure(response_format, structure_cls)

        object.__setattr__(self, "instruction", instruction)
        object.__setattr__(self, "guidance", guidance)
        object.__setattr__(
            self, "prompt_context", prompt_context if prompt_context is not None else []
        )
        object.__setattr__(self, "plain_content", plain_content)
        object.__setattr__(self, "tool_schemas", tool_schemas if tool_schemas is not None else [])
        object.__setattr__(self, "response_format", response_format)
        object.__setattr__(self, "structure", structure_cls)
        object.__setattr__(self, "_structure_instance", structure_inst)
        object.__setattr__(self, "images", images if images is not None else [])
        object.__setattr__(self, "image_detail", image_detail)

    def to_dict(self, exclude: set[str] | frozenset[str] | None = None) -> dict[str, Any]:
        # Conditionally include response_format when its value is a plain dict
        # (JSON-serializable); keep excluding it for type/BaseModel references
        # which cannot survive a round-trip through to_dict → from_dict.
        base_exclude = set(_INSTRUCTION_SERIALIZE_EXCLUDE)
        if isinstance(self.response_format, dict):
            base_exclude.discard("response_format")
        if exclude is not None:
            base_exclude.update(exclude)
        # Use explicit class reference for Python 3.10 slots-dataclass compat.
        from lionagi.ln.types import DataClass

        return DataClass.to_dict(self, exclude=frozenset(base_exclude))

    def with_updates(self, **kwargs: Any) -> InstructionContent:
        # to_dict excludes response_format/structure for type/BaseModel refs
        # (they can't survive a JSON round-trip), so the generic with_updates —
        # which goes through to_dict → constructor — silently drops the response
        # schema (and its rendered "ResponseFormat" / action_requests section).
        # Carry them over for in-memory copies (e.g. _prepare folding the system
        # message into the instruction's guidance), UNLESS the caller overrides
        # them explicitly (a None passed for response_format intentionally
        # clears it — that path strips prior turns' schemas in chat prep).
        if "response_format" not in kwargs and self.response_format is not None:
            kwargs["response_format"] = self.response_format
            if "structure" not in kwargs and self.structure is not None:
                kwargs["structure"] = self.structure
        dict_ = self.to_dict()
        dict_.update(kwargs)
        return type(self)(**dict_)

    @property
    def role(self) -> MessageRole:
        return MessageRole.USER

    @property
    def rendered(self) -> str | list[dict[str, Any]]:
        text = self._format_text_content()
        if not self.images:
            return text
        return self._format_image_content(text, self.images, self.image_detail)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InstructionContent:
        inst = cls()

        for k in ("instruction", "guidance", "plain_content", "image_detail"):
            if k in data and data[k]:
                setattr(inst, k, data[k])

        handle_context = data.get("handle_context", "extend")
        if handle_context not in {"extend", "replace"}:
            raise ValueError("handle_context must be either 'extend' or 'replace'")

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
            # Accept either a flat list of schemas or the {"tools": [...]} wrapper
            # that ActionManager.get_tool_schema returns; store flat so the
            # rendered "Tools:" section isn't nested under a spurious `- tools:`.
            if isinstance(ts, dict):
                ts = ts.get("tools", [ts])
            inst.tool_schemas.extend(ts if isinstance(ts, list) else [ts])

        if "images" in data:
            imgs = data.get("images") or []
            imgs_list = imgs if isinstance(imgs, list) else [imgs]
            inst.images.extend(imgs_list)
            inst.image_detail = data.get("image_detail") or inst.image_detail or "auto"

        response_format = data.get("response_format") or data.get("request_model")
        structure = data.get("structure")

        if response_format is not None:
            valid = (
                isinstance(response_format, dict)
                or (isinstance(response_format, type) and issubclass(response_format, BaseModel))
                or isinstance(response_format, BaseModel)
            )
            if valid:
                structure_cls = _resolve_structure_cls(structure)
                structure_inst = _build_structure(response_format, structure_cls)
                object.__setattr__(inst, "response_format", response_format)
                object.__setattr__(inst, "structure", structure_cls)
                object.__setattr__(inst, "_structure_instance", structure_inst)

        return inst

    def _format_text_content(self) -> str:
        from lionagi.libs.schema.minimal_yaml import minimal_yaml

        if self.plain_content:
            return self.plain_content

        schema_for_display = None
        if self._structure_instance is not None and not self._structure_instance.is_dict_mode:
            schema_for_display = self._structure_instance.request_schema().model_json_schema()

        doc: dict[str, Any] = {
            "Guidance": self.guidance,
            "Instruction": self.instruction,
            "Context": self.prompt_context,
            "Tools": self.tool_schemas,
            "ResponseSchema": schema_for_display,
        }

        if self._structure_instance is not None:
            doc["ResponseFormat"] = self._structure_instance.render()

        doc = {k: v for k, v in doc.items() if v not in (None, "", [], {})}
        return minimal_yaml(doc).strip()

    @staticmethod
    def _format_image_item(idx: str, detail: str) -> dict[str, Any]:
        """Format a single image entry for a provider payload.

        Validation (fail-closed):
        - http:// and https:// URLs are passed through ``validate_image_url``,
          which rejects null bytes, non-rooted URLs, and disallowed schemes.
        - data: URIs are accepted only when they match the ``data:image/*``
          base64 pattern — other data: payloads (HTML, SVG, JS) are rejected
          to limit DoS and XSS vectors.
        - Anything else is treated as raw base64 and wrapped into a
          ``data:image/jpeg;base64,`` URI (pre-existing behaviour).

        Raises:
            ValueError: If the URL fails validation.
        """
        if idx.startswith("http://") or idx.startswith("https://"):
            # Delegates null-byte, scheme, and missing-netloc checks.
            validate_image_url(idx)
            url = idx
        elif idx.startswith("data:"):
            # Accept only well-formed inline image data URIs.
            if not _DATA_IMAGE_RE.match(idx):
                raise ValueError(
                    f"Rejected data: URI — only data:image/*;base64,… is allowed. Got: {idx[:80]!r}"
                )
            url = idx
        elif "://" in idx or (idx.split(":")[0].isalpha() and ":" in idx):
            # Looks like a URL with a non-http/https/data scheme (e.g. file://,
            # javascript:, ftp://).  Delegate to validate_image_url which will
            # reject all disallowed schemes with a clear error.
            validate_image_url(idx)
            url = idx  # unreachable if validate_image_url raises
        else:
            # Raw base64 payload — wrap as an inline JPEG data URI.
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


def _resolve_structure_cls(structure: type | str | None) -> type | None:
    if structure is None or structure == "json":
        from lionagi.protocols.structure.json_structure import JsonStructure

        return JsonStructure
    if isinstance(structure, str):
        from lionagi.protocols.structure.json_structure import JsonStructure

        return JsonStructure
    return structure


def _build_structure(
    response_format: type[BaseModel] | dict[str, Any] | BaseModel | None,
    structure_cls: type | None,
) -> Any | None:
    if response_format is None or structure_cls is None:
        return None

    if isinstance(response_format, dict):
        return structure_cls(response_format)
    if isinstance(response_format, type) and issubclass(response_format, BaseModel):
        return structure_cls(response_format)
    if isinstance(response_format, BaseModel):
        return structure_cls(type(response_format))
    return None


class Instruction(Message):
    """User instruction message with structured content."""

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
