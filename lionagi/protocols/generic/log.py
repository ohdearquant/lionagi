# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import atexit
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr, field_validator

from lionagi.models.hashable_model import HashableModel
from lionagi.utils import create_path, to_dict

from .element import Element
from .pile import Pile

__all__ = (
    "DataLoggerConfig",
    "LogManagerConfig",
    "Log",
    "DataLogger",
    "LogManager",
)

logger = logging.getLogger(__name__)


class DataLoggerConfig(BaseModel):
    persist_dir: str | Path = "./data/logs"
    subfolder: str | None = None
    file_prefix: str | None = None
    capacity: int | None = None  # None means unbounded; set a value for long-running sessions
    extension: str = ".json"
    use_timestamp: bool = True
    hash_digits: int | None = Field(5, ge=0, le=10)
    auto_save_on_exit: bool = True
    clear_after_dump: bool = True

    @field_validator("capacity", "hash_digits", mode="before")
    def _validate_non_negative(cls, value):
        if value is not None:
            if not isinstance(value, int) or value < 0:
                raise ValueError("Capacity and hash_digits must be non-negative.")
        return value

    @field_validator("extension")
    def _ensure_dot_extension(cls, value):
        if not value.startswith("."):
            return "." + value
        if value not in {".csv", ".json", ".jsonl"}:
            raise ValueError("Extension must be '.csv', '.json' or '.jsonl'.")
        return value


class Log(Element):
    """
    An immutable log entry that wraps a dictionary of content.

    Once created or restored from a dictionary, the log is marked
    as read-only.
    """

    content: dict[str, Any]
    _immutable: bool = PrivateAttr(False)

    def __setattr__(self, name: str, value: Any) -> None:
        """Prevent mutation if log is immutable."""
        if getattr(self, "_immutable", False):
            raise AttributeError("This Log is immutable.")
        super().__setattr__(name, value)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Log:
        """
        Create a Log from a dictionary previously produced by `to_dict`.

        The dictionary must contain keys in `serialized_keys`.
        """
        self = cls.model_validate(data)
        self._immutable = True
        return self

    @classmethod
    def create(cls, content: Element | dict) -> Log:
        """
        Create a new Log from an Element, storing a dict snapshot
        of the element's data.
        """
        if isinstance(content, Element | HashableModel):
            content = content.to_dict(mode="json")
        else:
            content = to_dict(content, recursive=True, suppress=True)

        if content == {}:
            logger.warning(
                "No content to log, or original data was of invalid type. Making an empty log..."
            )
            return cls(content={"error": "No content to log."})

        return cls(content=content)


class DataLogger:
    """
    Manages a collection of logs, optionally auto-dumping them
    to CSV or JSON when capacity is reached or at program exit.
    """

    def __init__(
        self,
        *,
        logs: Any = None,
        _config: DataLoggerConfig = None,
        **kwargs,
    ):
        """
        Args:
            logs: Initial logs (list or Pile of Log objects).
            persist_dir: Directory for log files (default "./data/logs").
            subfolder: Subdirectory within `persist_dir`.
            file_prefix: Filename prefix for log files.
            capacity: Max number of logs before auto-dump. None = unlimited.
            extension: File extension (".csv" or ".json").
            use_timestamp: Whether to include timestamps in filenames.
            hash_digits: Random hash length in filenames.
            auto_save_on_exit: Auto-save logs at program exit.
            clear_after_dump: Whether to clear logs after saving.
        """
        if _config is None:
            _config = DataLoggerConfig(**kwargs)

        if isinstance(logs, dict):
            self.logs = Pile.from_dict(logs)
        else:
            self.logs = Pile(collections=logs, item_type=Log, strict_type=True)
        self._config = _config

        # Auto-dump on exit
        if self._config.auto_save_on_exit:
            atexit.register(self.save_at_exit)

    def log(self, log_: Any) -> None:
        """
        Add a log synchronously. If capacity is reached, auto-dump to file.
        """
        log_ = Log.create(log_) if not isinstance(log_, Log) else log_
        if self._config.capacity and len(self.logs) >= self._config.capacity:
            try:
                self.dump(clear=self._config.clear_after_dump)
            except Exception as e:
                logger.error(f"Failed to auto-dump logs: {e}")
        self.logs.include(log_)

    async def alog(self, log_: Any) -> None:
        """
        Add a log asynchronously. If capacity is reached, auto-dump to file.
        """
        async with self.logs:
            self.log(log_)

    def dump(
        self,
        clear: bool | None = None,
        persist_path: str | Path | None = None,
    ) -> None:
        """
        Dump the logs to a file (CSV or JSON). If file extension is
        unsupported, raise ValueError. Optionally clear logs after.
        """
        if not self.logs:
            logger.debug("No logs to dump.")
            return

        fp = persist_path or self._create_path()
        suffix = fp.suffix.lower()
        try:
            if suffix == ".csv":
                self.logs.dump(fp, "csv")
            elif suffix == ".json":
                self.logs.dump(fp, "json")
            else:
                raise ValueError(f"Unsupported file extension: {suffix}")

            logger.info(f"Dumped logs to {fp}")
            do_clear = self._config.clear_after_dump if clear is None else clear
            if do_clear:
                self.logs.clear()
        except Exception as e:
            # Check if it's a JSON serialization error with complex objects
            if "JSON serializable" in str(e):
                logger.debug(f"Could not serialize logs to JSON: {e}")
                # Don't raise for JSON serialization issues during dumps
                if clear is not False:
                    self.logs.clear()  # Still clear if requested
            else:
                logger.error(f"Failed to dump logs: {e}")
                raise

    async def adump(
        self,
        clear: bool | None = None,
        persist_path: str | Path | None = None,
    ) -> None:
        """Asynchronously dump the logs to a file."""
        async with self.logs:
            self.dump(clear=clear, persist_path=persist_path)

    def _create_path(self) -> Path:
        """
        Build a file path from the manager's config using
        `create_path`.
        """
        path_str = str(self._config.persist_dir)
        if self._config.subfolder:
            path_str = f"{path_str}/{self._config.subfolder}"
        return create_path(
            directory=path_str,
            filename=self._config.file_prefix or "",
            extension=self._config.extension,
            timestamp=self._config.use_timestamp,
            random_hash_digits=self._config.hash_digits,
        )

    def save_at_exit(self) -> None:
        """Dump logs on program exit."""
        if self.logs:
            try:
                self.dump(clear=self._config.clear_after_dump)
            except Exception as e:
                # Only log debug level for JSON serialization errors during exit
                # These are non-critical and often occur with complex objects
                if "JSON serializable" in str(e):
                    logger.debug(f"Could not serialize logs to JSON: {e}")
                else:
                    logger.error(f"Failed to save logs on exit: {e}")

    @classmethod
    def from_config(cls, config: DataLoggerConfig, logs: Any = None) -> DataLogger:
        """
        Construct a LogManager from a LogManagerConfig.
        """
        return cls(_config=config, logs=logs)


LogManagerConfig = DataLoggerConfig
LogManager = DataLogger

# File: lionagi/protocols/generic/log.py
