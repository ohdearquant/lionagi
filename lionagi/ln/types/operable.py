"""Operable: ordered Spec collection with adapter-based model generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ._sentinel import MaybeUnset, Unset

if TYPE_CHECKING:
    from .spec import Spec

__all__ = ("Operable",)


@dataclass(frozen=True, slots=True, init=False)
class Operable:
    """Immutable ordered Spec collection; use create_model() to emit a Pydantic model."""

    __op_fields__: tuple[Spec, ...]
    name: str | None

    def __init__(
        self,
        specs: tuple[Spec, ...] | list[Spec] = (),
        *,
        name: str | None = None,
    ):
        """Validate and store specs; raises TypeError on non-Spec items or ValueError on duplicate names."""
        # Import here to avoid circular import
        from .spec import Spec

        if isinstance(specs, list):
            specs = tuple(specs)

        for i, item in enumerate(specs):
            if not isinstance(item, Spec):
                raise TypeError(
                    f"All specs must be Spec objects, got {type(item).__name__} at index {i}"
                )

        names = [s.name for s in specs if s.name is not None]
        if len(names) != len(set(names)):
            from collections import Counter

            duplicates = [name for name, count in Counter(names).items() if count > 1]
            raise ValueError(
                f"Duplicate field names found: {duplicates}. Each spec must have a unique name."
            )

        object.__setattr__(self, "__op_fields__", specs)
        object.__setattr__(self, "name", name)

    def allowed(self) -> set[str]:
        """Return set of field names across all specs."""
        return {i.name for i in self.__op_fields__}

    def check_allowed(self, *args, as_boolean: bool = False):
        """Return True if all args are allowed field names; raise ValueError (or return False) otherwise."""
        if not set(args).issubset(self.allowed()):
            if as_boolean:
                return False
            raise ValueError(
                f"Some specified fields are not allowed: {set(args).difference(self.allowed())}"
            )
        return True

    def get(self, key: str, /, default=Unset) -> MaybeUnset[Spec]:
        """Return Spec for key, or default if not found."""
        if not self.check_allowed(key, as_boolean=True):
            return default
        for i in self.__op_fields__:
            if i.name == key:
                return i
        return default

    def get_specs(
        self,
        *,
        include: set[str] | None = None,
        exclude: set[str] | None = None,
    ) -> tuple[Spec, ...]:
        """Return filtered specs; raises ValueError if both include and exclude are given or names are invalid."""
        if include is not None and exclude is not None:
            raise ValueError("Cannot specify both include and exclude")

        if include:
            if self.check_allowed(*include, as_boolean=True) is False:
                raise ValueError(
                    "Some specified fields are not allowed: "
                    f"{set(include).difference(self.allowed())}"
                )
            return tuple(self.get(i) for i in include if self.get(i) is not Unset)

        if exclude:
            _discards = {self.get(i) for i in exclude if self.get(i) is not Unset}
            return tuple(s for s in self.__op_fields__ if s not in _discards)

        return self.__op_fields__

    def create_model(
        self,
        adapter: Literal["pydantic"] = "pydantic",
        model_name: str | None = None,
        include: set[str] | None = None,
        exclude: set[str] | None = None,
        **kw,
    ):
        """Build and return a model class from specs via the named adapter (currently only "pydantic")."""
        match adapter:
            case "pydantic":
                try:
                    from lionagi.adapters.spec_adapters import PydanticSpecAdapter
                except ImportError as e:
                    raise ImportError(
                        "PydanticSpecAdapter requires Pydantic. Install with: pip install pydantic"
                    ) from e

                kws = {
                    "model_name": model_name or self.name or "DynamicModel",
                    "include": include,
                    "exclude": exclude,
                    **kw,
                }
                return PydanticSpecAdapter.create_model(self, **kws)
            case _:
                raise ValueError(f"Unsupported adapter: {adapter}")
