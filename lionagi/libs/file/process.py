# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Literal

from lionagi import ln
from lionagi.utils import is_import_installed

from .chunk import chunk_content


def dir_to_files(
    directory: str | Path,
    file_types: list[str] | None = None,
    max_workers: int | None = None,
    ignore_errors: bool = False,
    verbose: bool = False,
    recursive: bool = False,
) -> list[Path]:
    """Gather files under directory matching file_types; recursive=True descends subdirs; raises ValueError if path invalid."""
    directory_path = Path(directory)
    if not directory_path.is_dir():
        raise ValueError(f"The provided path is not a valid directory: {directory}")

    def process_file(file_path: Path) -> Path | None:
        try:
            if file_types is None or file_path.suffix in file_types:
                return file_path
        except Exception as e:
            if ignore_errors:
                if verbose:
                    logging.warning(f"Error processing {file_path}: {e}")
            else:
                raise ValueError(f"Error processing {file_path}: {e}") from e
        return None

    file_iterator = directory_path.rglob("*") if recursive else directory_path.glob("*")
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(process_file, f) for f in file_iterator if f.is_file()]
            files = [
                future.result() for future in as_completed(futures) if future.result() is not None
            ]

        if verbose:
            logging.info(f"Processed {len(files)} files from {directory}")

        return files
    except Exception as e:
        raise ValueError(f"Error processing directory {directory}: {e}") from e


def chunk(
    *,
    text: str | None = None,
    url_or_path: str | Path = None,
    file_types: list[str] | None = None,  # only local files
    recursive: bool = False,  # only local files
    tokenizer: Callable[[str], list[str]] = None,
    chunk_by: Literal["chars", "tokens"] = "chars",
    chunk_size: int = 1500,
    overlap: float = 0.1,
    threshold: int = 200,
    output_file: str | Path | None = None,
    metadata: dict[str, Any] | None = None,
    reader_tool: Callable = None,
    as_node: bool = False,
) -> list:
    texts = []
    if not text:
        if isinstance(url_or_path, str):
            url_or_path = Path(url_or_path)

        chunks = None
        files = None
        if url_or_path.exists():
            if url_or_path.is_dir():
                files = dir_to_files(
                    directory=url_or_path,
                    file_types=file_types,
                    recursive=recursive,
                )
            elif url_or_path.is_file():
                files = [url_or_path]
        else:
            files = [str(url_or_path)] if not isinstance(url_or_path, list) else url_or_path

        if reader_tool is None:

            def reader_tool(x):
                return Path(x).read_text(encoding="utf-8")

        if reader_tool == "docling":
            if not is_import_installed("docling"):
                raise ImportError(
                    "The 'docling' package is required for this feature. "
                    "Please install it via 'pip install lionagi[reader]'."
                )
            from docling.document_converter import DocumentConverter

            converter = DocumentConverter()

            def reader_tool(x):  # type: ignore[no-redef]
                return converter.convert(x).document.export_to_markdown()

        texts = ln.lcall(files, reader_tool)

    else:
        texts = [text]

    chunks = ln.lcall(
        texts,
        chunk_content,
        chunk_by=chunk_by,
        chunk_size=chunk_size,
        overlap=overlap,
        threshold=threshold,
        metadata=metadata,
        as_node=True,
        output_flatten=True,
        tokenizer=tokenizer or str.split,
    )
    if threshold:
        chunks = [c for c in chunks if len(c.content) > threshold]

    if output_file:
        from lionagi.protocols.generic.pile import Pile

        output_file = Path(output_file)
        if output_file.suffix == ".csv":
            p = Pile(chunks)
            p.dump(output_file, "csv")
        elif output_file.suffix == ".json":
            p = Pile(chunks)
            p.dump(output_file, "json")
        elif output_file.suffix == ".parquet":
            p = Pile(chunks)
            p.dump(output_file, "parquet")
        else:
            raise ValueError(f"Unsupported output file format: {output_file}")

    if as_node:
        return chunks

    return [c.content for c in chunks]
