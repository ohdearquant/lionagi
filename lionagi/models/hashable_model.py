from typing import Literal

import orjson
from pydantic import BaseModel, ConfigDict
from typing_extensions import Self

from .. import ln

ConversionMode = Literal["python", "json", "db"]


__all__ = (
    "HashableModel",
    "ConversionMode",
)


class HashableModel(BaseModel):
    """Used as base class for models that need to be hashable."""

    model_config = ConfigDict(
        populate_by_name=True,
        validate_assignment=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        use_enum_values=True,
        validate_default=True,
    )

    def _to_dict(self, **kw) -> dict:
        dict_ = self.model_dump(**kw)
        return {k: v for k, v in dict_.items() if ln.not_sentinel(v)}

    def to_dict(self, mode: ConversionMode = "python", **kw) -> dict:
        """Converts this Element to a dictionary."""
        match mode:
            case "python":
                return self._to_dict(**kw)
            case "json":
                return orjson.loads(self.to_json(decode=False))
            case "db":
                dict_ = orjson.loads(self.to_json(decode=False))
                if "metadata" in dict_:
                    dict_["node_metadata"] = dict_.pop("metadata", {})
                return dict_
            case _:
                raise ValueError(f"Unsupported mode: {mode}")

    @classmethod
    def from_dict(cls, data: dict, mode: ConversionMode = "python", **kw) -> Self:
        match mode:
            case "python":
                return cls.model_validate(data, **kw)
            case "json":
                if isinstance(data, str | bytes):
                    data = orjson.loads(data)
                return cls.model_validate(data, **kw)
            case "db":
                if "node_metadata" in data:
                    data["metadata"] = data.pop("node_metadata")
                return cls.model_validate(data, **kw)
            case _:
                raise ValueError(f"Unsupported mode: {mode}")

    def to_json(self, decode: bool = True, **kw) -> bytes | str:
        """Converts this Element to a JSON string."""

        dict_ = self._to_dict(**kw)
        return ln.json_dumps(
            dict_,
            decode=decode,
            sort_keys=True,
            deterministic_sets=True,
            naive_utc=True,
            default=_get_default_hashable_serializer(),
        )

    @classmethod
    def from_json(cls, data: bytes | str, mode: ConversionMode = "json", **kwargs) -> Self:
        """Creates an instance of this class from a JSON string."""
        return cls.from_dict(data, mode=mode, **kwargs)

    def __hash__(self):
        return ln.hash_dict(self.to_dict())


def _get_default_hashable_serializer():
    # Imported here: lionagi.protocols.generic imports this module while it loads.
    from lionagi.protocols.generic.element import DEFAULT_ELEMENT_SERIALIZER

    return DEFAULT_ELEMENT_SERIALIZER
