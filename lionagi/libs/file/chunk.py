# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import math
from collections.abc import Callable
from typing import Any, Literal


def chunk_by_chars(
    text: str, chunk_size: int = 2048, overlap: float = 0, threshold: int = 256
) -> list[str]:
    """
    Split a text into chunks of approximately equal size, with optional overlap.

    This function divides the input text into chunks based on the specified
    chunk size. It handles different scenarios based on the number of chunks
    required and provides options for overlap between chunks.

    Args:
        text (str): The input text to be chunked.
        chunk_size (int, optional): The target size for each chunk. Defaults to 2048.
        overlap (float, optional): The fraction of overlap between chunks. Defaults to 0.
        threshold (int, optional): The minimum size for the last chunk. Defaults to 256.

    Returns:
        List[str]: A list of text chunks.

    Raises:
        ValueError: If an error occurs during the chunking process.
    """
    try:
        n_chunks = math.ceil(len(text) / chunk_size)
        overlap_size = int(chunk_size * overlap / 2)

        if n_chunks == 1:
            return [text]
        elif n_chunks == 2:
            return _chunk_two_parts(text, chunk_size, overlap_size, threshold)
        else:
            return _chunk_multiple_parts(text, chunk_size, overlap_size, n_chunks, threshold)
    except Exception as e:
        raise ValueError(f"An error occurred while chunking the text: {e}") from e


def _chunk_two_parts(text: str, chunk_size: int, overlap_size: int, threshold: int) -> list[str]:
    """Handle chunking for two parts."""
    first_chunk = text[: chunk_size + overlap_size]
    if len(text) - chunk_size > threshold:
        return [first_chunk, text[chunk_size - overlap_size :]]
    return [text]


def _chunk_multiple_parts(
    text: str,
    chunk_size: int,
    overlap_size: int,
    n_chunks: int,
    threshold: int,
) -> list[str]:
    """Handle chunking for more than two parts."""
    chunks = [text[: chunk_size + overlap_size]]

    for i in range(1, n_chunks - 1):
        start_idx = chunk_size * i - overlap_size
        end_idx = chunk_size * (i + 1) + overlap_size
        chunks.append(text[start_idx:end_idx])

    last_chunk_start = chunk_size * (n_chunks - 1) - overlap_size
    if len(text) - last_chunk_start > threshold:
        chunks.append(text[last_chunk_start:])
    else:
        chunks[-1] += text[chunk_size * (n_chunks - 1) + overlap_size :]

    return chunks


def chunk_by_tokens(
    tokens: list[str],
    chunk_size: int = 1024,
    overlap: float = 0,
    threshold: int = 128,
    return_tokens: bool = False,
) -> list[str | list[str]]:
    """
    Split a list of tokens into chunks of approximately equal size, with optional overlap.

    This function divides the input tokens into chunks based on the specified
    chunk size. It handles different scenarios based on the number of chunks
    required and provides options for overlap between chunks.

    Args:
        tokens (list[str]): The input list of tokens to be chunked.
        chunk_size (int, optional): The target size for each chunk. Defaults to 1024.
        overlap (float, optional): The fraction of overlap between chunks. Defaults to 0.
        threshold (int, optional): The minimum size for the last chunk. Defaults to 128.
        return_tokens (bool, optional): If True, return chunks as lists of tokens;
                                        if False, return as joined strings. Defaults to False.

    Returns:
        list[Union[str, list[str]]]: A list of chunked tokens, either as strings or token lists.

    Raises:
        ValueError: If an error occurs during the chunking process.
    """
    try:
        n_chunks = math.ceil(len(tokens) / chunk_size)
        overlap_size = int(overlap * chunk_size / 2)
        residue = len(tokens) % chunk_size

        if n_chunks == 1:
            return _process_single_chunk(tokens, return_tokens)
        elif n_chunks == 2:
            return _chunk_token_two_parts(
                tokens,
                chunk_size,
                overlap_size,
                threshold,
                residue,
                return_tokens,
            )
        else:
            return _chunk_token_multiple_parts(
                tokens,
                chunk_size,
                overlap_size,
                n_chunks,
                threshold,
                residue,
                return_tokens,
            )
    except Exception as e:
        raise ValueError(f"An error occurred while chunking the tokens: {e}") from e


def _process_single_chunk(tokens: list[str], return_tokens: bool) -> list[str | list[str]]:
    """Handle processing for a single chunk."""
    return [tokens] if return_tokens else [" ".join(tokens).strip()]


def _chunk_token_two_parts(
    tokens: list[str],
    chunk_size: int,
    overlap_size: int,
    threshold: int,
    residue: int,
    return_tokens: bool,
) -> list[str | list[str]]:
    """Handle chunking for two parts."""
    chunks = [tokens[: chunk_size + overlap_size]]
    # When residue == 0, we have perfect division, create the second chunk
    # When residue > threshold, the leftover is big enough for a second chunk
    if residue == 0 or residue > threshold:
        chunks.append(tokens[chunk_size - overlap_size :])
    else:
        return _process_single_chunk(tokens, return_tokens)
    return _format_chunks(chunks, return_tokens)


def _chunk_token_multiple_parts(
    tokens: list[str],
    chunk_size: int,
    overlap_size: int,
    n_chunks: int,
    threshold: int,
    residue: int,
    return_tokens: bool,
) -> list[str | list[str]]:
    """Handle chunking for more than two parts."""
    chunks = [tokens[: chunk_size + overlap_size]]
    for i in range(1, n_chunks - 1):
        start_idx = chunk_size * i - overlap_size
        end_idx = chunk_size * (i + 1) + overlap_size
        chunks.append(tokens[start_idx:end_idx])

    last_chunk_start = chunk_size * (n_chunks - 1) - overlap_size
    if len(tokens) - last_chunk_start > threshold:
        chunks.append(tokens[last_chunk_start:])
    else:
        chunks[-1] += tokens[-residue:]

    return _format_chunks(chunks, return_tokens)


def _format_chunks(chunks: list[list[str]], return_tokens: bool) -> list[str | list[str]]:
    """Format chunks based on the return_tokens flag."""
    return chunks if return_tokens else [" ".join(chunk).strip() for chunk in chunks]


def chunk_content(
    content: str,
    chunk_by: Literal["chars", "tokens"] = "chars",
    tokenizer: Callable[[str], list[str]] = str.split,
    chunk_size: int = 1024,
    overlap: float = 0,
    threshold: int = 256,
    metadata: dict[str, Any] = None,
    return_tokens: bool = False,
    as_node: bool = False,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """
    Split content into chunks and add metadata.

    This function takes a string content, splits it into chunks using the provided
    chunking function, and adds metadata to each chunk.

    Args:
        content (str): The content to be chunked.
        chunk_by(str): The method to use for chunking: "chars" or "tokens".
        tokenizer (Callable): The function to use for tokenization. defaults to str.split.
        chunk_size (int): The target size for each chunk.
        overlap (float): The fraction of overlap between chunks.
        threshold (int): The minimum size for the last chunk.
        metadata (Dict[str, Any]): Metadata to be included with each chunk.
        kwargs for tokenizer, if needed.

    Returns:
        List[Dict[str, Any]]: A list of dictionaries, each representing a chunk with metadata.
    """

    if chunk_by == "tokens":
        chunks = chunk_by_tokens(
            tokens=tokenizer(content, **kwargs),
            chunk_size=chunk_size,
            overlap=overlap,
            threshold=threshold,
            return_tokens=return_tokens,
        )
    else:
        chunks = chunk_by_chars(
            text=content,
            chunk_size=chunk_size,
            overlap=overlap,
            threshold=threshold,
        )

    if as_node:
        from lionagi.protocols.graph.node import Node

        return [
            Node(
                content=chunk,
                metadata={
                    "chunk_id": i + 1,
                    "total_chunks": len(chunks),
                    "chunk_size": len(chunk),
                    **(metadata or {}),
                },
            )
            for i, chunk in enumerate(chunks)
        ]

    return [
        {
            "chunk_content": chunk,
            "chunk_id": i + 1,
            "total_chunks": len(chunks),
            "chunk_size": len(chunk),
            **(metadata or {}),
        }
        for i, chunk in enumerate(chunks)
    ]
