# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import atexit
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr, field_validator

from lionagi.ln import create_path
from lionagi.models.hashable_model import HashableModel
from lionagi.utils import to_dict

from .element import Element
from .pile import Pile, _serialize_records

__all__ = (
    "DataLoggerConfig",
    "LogManagerConfig",
    "Log",
    "DataLogger",
    "LogManager",
    "redact_binary_content",
)

logger = logging.getLogger(__name__)


# A base64 data URI, e.g. "data:image/png;base64,iVBOR..." — the media type is
# optional per RFC 2397 and further parameters may sit between it and ";base64".
_DATA_URI_RE = re.compile(r"^data:([-\w.+]+/[-\w.+]+)?(?:;[-\w.+=]+)*;base64,")

# Bare (not data-URI-wrapped) base64 is only redacted under keys that provider
# schemas use for binary: Anthropic image blocks, Ollama multimodal messages,
# OpenAI image generation, and audio input. Under any other key a base64-shaped
# string is more likely to be an id or a token than an image.
_BINARY_KEYS = frozenset({"data", "b64_json", "image", "images", "audio", "input_audio"})

_BARE_B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


def _b64_decoded_size(payload: str) -> int:
    """Decoded byte length of a base64 string, computed without decoding it."""
    return max(0, len(payload) * 3 // 4 - payload.count("="))


def _placeholder(key: str | None, media_type: str | None, size: int) -> str:
    parts = ["<lionagi:redacted-binary"]
    if key:
        parts.append(f"field={key}")
    if media_type:
        parts.append(f"media_type={media_type}")
    parts.append(f"bytes={size}>")
    return " ".join(parts)


def _redact_str(value: str, key: str | None, media_type: str | None, threshold: int) -> str:
    # Cheap length gate first: nothing under the threshold can be redacted, and
    # this keeps the regexes off the short strings that dominate a payload.
    if len(value) * 3 // 4 < threshold:
        return value

    if match := _DATA_URI_RE.match(value):
        payload = value[match.end() :]
        size = _b64_decoded_size(payload)
        if size >= threshold:
            return _placeholder(key, match.group(1) or media_type, size)
        return value

    # Real base64 is padded to a multiple of 4; requiring that, plus an
    # alphabet with no whitespace or punctuation, keeps prose out.
    if key in _BINARY_KEYS and len(value) % 4 == 0 and _BARE_B64_RE.match(value):
        size = _b64_decoded_size(value)
        if size >= threshold:
            return _placeholder(key, media_type, size)

    return value


def redact_binary_content(
    data: Any,
    *,
    threshold: int = 1024,
    _key: str | None = None,
    _media_type: str | None = None,
) -> Any:
    """Return `data` with base64 binary payloads replaced by placeholders.

    Never mutates its input: subtrees with nothing to redact are returned by
    identity, so the caller's object -- including a payload already queued for a
    provider -- is unchanged.
    """
    if isinstance(data, dict):
        media_type = data.get("media_type") or data.get("mime_type") or _media_type
        out = None
        for k, v in data.items():
            new = redact_binary_content(v, threshold=threshold, _key=k, _media_type=media_type)
            if new is not v:
                if out is None:
                    out = dict(data)
                out[k] = new
        return data if out is None else out

    if isinstance(data, list):
        out = None
        for i, v in enumerate(data):
            new = redact_binary_content(v, threshold=threshold, _key=_key, _media_type=_media_type)
            if new is not v:
                if out is None:
                    out = list(data)
                out[i] = new
        return data if out is None else out

    if isinstance(data, str):
        return _redact_str(data, _key, _media_type, threshold)

    return data


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
    # Base64 binary (images, audio) is replaced by a placeholder naming the
    # field, media type and byte length. On by default: a vision workload logs
    # its payloads verbatim otherwise, and the bytes are unreadable in a log
    # while being most of its size.
    redact_binary: bool = True
    redact_binary_threshold: int = Field(1024, ge=0)  # decoded bytes

    @field_validator("capacity", "hash_digits", mode="before")
    def _validate_non_negative(cls, value):
        if value is not None:
            if not isinstance(value, int) or value < 0:
                raise ValueError("Capacity and hash_digits must be non-negative.")
        return value

    @field_validator("extension")
    def _ensure_dot_extension(cls, value):
        # Normalize the leading dot first, then validate: an undotted spelling
        # must be held to the same allowlist as its dotted form.
        if not value.startswith("."):
            value = "." + value
        if value not in {".csv", ".json", ".jsonl"}:
            raise ValueError("Extension must be '.csv', '.json' or '.jsonl'.")
        return value


class Log(Element):
    """Immutable log entry wrapping a dict snapshot; mutations raise AttributeError."""

    content: dict[str, Any]
    _immutable: bool = PrivateAttr(False)

    def __setattr__(self, name: str, value: Any) -> None:
        """Raise AttributeError if the log is immutable."""
        if getattr(self, "_immutable", False):
            raise AttributeError("This Log is immutable.")
        super().__setattr__(name, value)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Log:
        """Deserialize a dict from to_dict() into an immutable Log."""
        self = cls.model_validate(data)
        self._immutable = True
        return self

    @classmethod
    def create(cls, content: Element | dict) -> Log:
        """Create a mutable Log from an Element or dict; marks immutable on from_dict only."""
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
    """Log collection with optional auto-dump to CSV/JSON at capacity or program exit."""

    def __init__(
        self,
        *,
        logs: Any = None,
        _config: DataLoggerConfig = None,
        **kwargs,
    ):
        if _config is None:
            _config = DataLoggerConfig(**kwargs)

        if isinstance(logs, dict):
            self.logs = Pile.from_dict(logs)
        else:
            self.logs = Pile(collections=logs, item_type=Log, strict_type=True)
        self._config = _config

        if self._config.auto_save_on_exit:
            atexit.register(self.save_at_exit)

    def log(self, log_: Any) -> None:
        """Add a log entry; auto-dumps to file if capacity is reached."""
        log_ = Log.create(log_) if not isinstance(log_, Log) else log_
        log_ = self._redact(log_)
        if self._config.capacity and len(self.logs) >= self._config.capacity:
            try:
                self.dump(clear=self._config.clear_after_dump)
            except Exception as e:
                logger.error(f"Failed to auto-dump logs: {e}")
        self.logs.include(log_)

    def _redact(self, log_: Log) -> Log:
        """Swap binary payloads for placeholders on the way into the store.

        Works on the Log's own snapshot dict, never on the object that was
        logged -- an APICalling's payload is still queued for the provider when
        this runs, and rewriting it would change the request.
        """
        if not self._config.redact_binary:
            return log_
        content = redact_binary_content(
            log_.content, threshold=self._config.redact_binary_threshold
        )
        if content is log_.content:
            return log_
        return Log.from_dict({**log_.to_dict(), "content": content})

    async def alog(self, log_: Any) -> None:
        """Async variant of log(); auto-dumps to file if capacity is reached."""
        async with self.logs:
            self.log(log_)

    def dump(
        self,
        clear: bool | None = None,
        persist_path: str | Path | None = None,
    ) -> None:
        """Write logs to CSV or JSON; clears afterward if configured."""
        if not self.logs:
            logger.debug("No logs to dump.")
            return

        fp = persist_path or self._create_path()
        suffix = fp.suffix.lower()
        try:
            if suffix == ".csv":
                self.logs.dump(fp, "csv")
            elif suffix in (".json", ".jsonl"):
                self.logs.dump(fp, "json")
            else:
                raise ValueError(f"Unsupported file extension: {suffix}")

            logger.info(f"Dumped logs to {fp}")
            do_clear = self._config.clear_after_dump if clear is None else clear
            if do_clear:
                self.logs.clear()
        except Exception as e:
            # JSON serialization errors on complex objects are swallowed, not raised
            if "JSON serializable" in str(e):
                logger.debug(f"Could not serialize logs to JSON: {e}")
                if clear is not False:
                    self.logs.clear()
            else:
                logger.error(f"Failed to dump logs: {e}")
                raise

    async def adump(
        self,
        clear: bool | None = None,
        persist_path: str | Path | None = None,
    ) -> None:
        """Async dump: snapshot under lock, write outside lock, clear only on success."""
        from lionagi.ln.concurrency import run_sync

        async with self.logs:
            if not self.logs:
                logger.debug("No logs to dump.")
                return
            fp = persist_path or self._create_path()
            suffix = fp.suffix.lower()
            if suffix not in (".csv", ".json", ".jsonl"):
                raise ValueError(f"Unsupported file extension: {suffix}")
            snapshot_ids = set(self.logs.collections.keys())
            records = self.logs._ordered_records()

        do_clear = self._config.clear_after_dump if clear is None else clear
        obj_key = "csv" if suffix == ".csv" else "json"

        def _write() -> None:
            text = _serialize_records(records, obj_key)
            with open(fp, "w", encoding="utf-8", newline="") as f:
                f.write(text)

        try:
            await run_sync(_write)
            logger.info(f"Dumped logs to {fp}")
        except Exception as e:
            if "JSON serializable" in str(e):
                logger.debug(f"Could not serialize logs to JSON: {e}")
            else:
                logger.error(f"Failed to dump logs: {e}")
                raise
            return

        if do_clear:
            async with self.logs:
                self.logs.progression.exclude(list(snapshot_ids))
                for uid in snapshot_ids:
                    self.logs.collections.pop(uid, None)

    def _create_path(self) -> Path:
        """Build an output file path from the logger config."""
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
                # JSON serialization errors during exit are non-critical, log at debug
                if "JSON serializable" in str(e):
                    logger.debug(f"Could not serialize logs to JSON: {e}")
                else:
                    logger.error(f"Failed to save logs on exit: {e}")

    @classmethod
    def from_config(cls, config: DataLoggerConfig, logs: Any = None) -> DataLogger:
        """Construct a DataLogger from a DataLoggerConfig."""
        return cls(_config=config, logs=logs)


LogManagerConfig = DataLoggerConfig
LogManager = DataLogger

# File: lionagi/protocols/generic/log.py
