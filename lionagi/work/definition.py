# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""WorkerDefinition: static descriptor for a worker type.

A WorkerDefinition describes *what a worker can do* — its name, the form
templates it accepts as input and emits as output, the callable that
processes a form, and operational constraints (concurrency, timeout).

Definitions are loaded from YAML/JSON files or constructed in Python.
They are immutable once loaded and can be shared across WorkEngine instances.

Usage::

    # From a dict (e.g., parsed YAML)
    defn = load_definition({
        "definition_id": "summarise",
        "name": "Summarise Worker",
        "description": "Summarises text fields.",
        "input_form": "text_input",
        "output_form": "summary_output",
        "handler": "mypackage.workers.summarise_handler",
        "max_concurrent": 4,
        "timeout_seconds": 60,
    })

    # From a YAML file path
    defn = load_definition("/path/to/worker.yaml")
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

__all__ = (
    "WorkerDefinition",
    "load_definition",
)


class WorkerDefinition(BaseModel):
    """Static descriptor for a worker type.

    Attributes:
        definition_id: Unique identifier for this worker type (e.g., ``"summarise"``).
        name: Human-readable display name.
        description: Purpose of this worker.
        input_form: Form template ID that this worker accepts as input.
        output_form: Form template ID that this worker produces as output.
        handler: Dotted Python path to the callable that processes a WorkForm.
            Must be importable at runtime.  Signature:
            ``handler(form: WorkForm) -> Any``.
        max_concurrent: Maximum number of simultaneous in-flight tasks for
            this worker type.  ``0`` means unlimited.
        timeout_seconds: How long (seconds) an individual task may run before
            the engine cancels it.  ``0`` means no timeout.
        tags: Optional list of string tags for categorisation and filtering.
        extra: Additional metadata preserved as-is (for tooling, documentation).
    """

    definition_id: str = Field(..., description="Unique worker type identifier.")
    name: str = Field(..., description="Display name.")
    description: str = Field("", description="Purpose of this worker.")
    input_form: str = Field(..., description="ID of the input form template.")
    output_form: str = Field(..., description="ID of the output form template.")
    handler: str = Field(
        ...,
        description="Dotted path to the handler callable, e.g. 'pkg.module.fn'.",
    )
    max_concurrent: int = Field(
        1,
        ge=0,
        description="Max simultaneous tasks (0 = unlimited).",
    )
    timeout_seconds: int = Field(
        0,
        ge=0,
        description="Per-task timeout in seconds (0 = no timeout).",
    )
    tags: list[str] = Field(default_factory=list, description="Optional tags.")
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary extra metadata.",
    )

    @model_validator(mode="after")
    def _validate_handler_format(self) -> WorkerDefinition:
        parts = self.handler.split(".")
        if len(parts) < 2:
            raise ValueError(
                f"handler must be a dotted path with at least one module component, "
                f"e.g. 'mymodule.fn'.  Got {self.handler!r}."
            )
        return self

    def resolve_handler(self) -> Callable[..., Any]:
        """Import and return the callable identified by :attr:`handler`.

        Raises:
            ImportError: If the module cannot be imported.
            AttributeError: If the callable cannot be found in the module.
        """
        module_path, attr = self.handler.rsplit(".", 1)
        mod = importlib.import_module(module_path)
        fn = getattr(mod, attr)
        if not callable(fn):
            raise TypeError(f"handler {self.handler!r} resolved to {fn!r}, which is not callable.")
        return fn


def load_definition(source: str | dict[str, Any]) -> WorkerDefinition:
    """Load a :class:`WorkerDefinition` from a file path or a dict.

    Supported file formats: JSON (``.json``), YAML (``.yaml`` / ``.yml``).
    When *source* is a ``dict``, it is passed directly to
    ``WorkerDefinition.model_validate``.

    Args:
        source: Path string (to a YAML or JSON file) or a plain dict.

    Returns:
        A validated :class:`WorkerDefinition` instance.

    Raises:
        FileNotFoundError: If *source* is a path that doesn't exist.
        ValueError: If the file extension is unsupported.
        pydantic.ValidationError: If required fields are missing or invalid.
    """
    if isinstance(source, dict):
        return WorkerDefinition.model_validate(source)

    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"WorkerDefinition file not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".json":
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    elif suffix in {".yaml", ".yml"}:
        try:
            import yaml  # optional dep — only needed when loading YAML files
        except ImportError as exc:
            raise ImportError(
                "PyYAML is required to load YAML worker definitions.  "
                "Install it with: pip install pyyaml"
            ) from exc
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    else:
        raise ValueError(
            f"Unsupported definition file format {suffix!r}.  Use .json, .yaml, or .yml."
        )

    return WorkerDefinition.model_validate(data)
