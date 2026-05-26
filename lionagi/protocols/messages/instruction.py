from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, field_validator

from lionagi.ln.types import ModelConfig

from .message import Message, MessageContent, MessageRole


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

    @property
    def role(self) -> MessageRole:
        return MessageRole.USER

    def with_updates(self, **kwargs: Any) -> "InstructionContent":
        dict_ = self.to_dict()
        dict_.update(kwargs)
        return type(self)(**dict_)

    @property
    def rendered(self) -> str | list[dict[str, Any]]:
        text = self._format_text_content()
        if not self.images:
            return text
        return self._format_image_content(text, self.images, self.image_detail)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InstructionContent":
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
            inst.tool_schemas.extend(ts if isinstance(ts, list) else [ts])

        if "images" in data:
            imgs = data.get("images") or []
            imgs_list = imgs if isinstance(imgs, list) else [imgs]
            inst.images.extend(imgs_list)
            inst.image_detail = data.get("image_detail") or inst.image_detail or "auto"

        response_format = data.get("response_format")
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
