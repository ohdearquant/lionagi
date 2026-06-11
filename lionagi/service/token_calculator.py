# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from collections.abc import Callable

import tiktoken


def get_encoding_name(value: str) -> str:
    try:
        enc = tiktoken.encoding_for_model(value)
        return enc.name
    except Exception:
        try:
            tiktoken.get_encoding(value)
            return value
        except Exception:
            return "o200k_base"


class TokenCalculator:
    @staticmethod
    def calculate_message_tokens(messages: list[dict], /, **kwargs) -> int:
        model = kwargs.get("model", "gpt-4o")
        tokenizer = tiktoken.get_encoding(get_encoding_name(model)).encode

        num_tokens = 0
        for msg in messages:
            num_tokens += 4
            _c = msg.get("content")
            num_tokens += TokenCalculator._calculate_chatitem(
                _c, tokenizer=tokenizer, model_name=model
            )
        return num_tokens  # buffer for chat

    @staticmethod
    def calculate_embed_token(inputs: list[str], /, **kwargs) -> int:
        try:
            tokenizer = tiktoken.get_encoding(
                get_encoding_name(kwargs.get("model", "text-embedding-3-small"))
            ).encode

            return sum(
                TokenCalculator._calculate_embed_item(i, tokenizer=tokenizer) for i in inputs
            )
        except Exception:
            return 0

    @staticmethod
    def tokenize(
        s_: str = None,
        /,
        encoding_name: str | None = None,
        tokenizer: Callable | None = None,
        decoder: Callable | None = None,
        return_tokens: bool = False,
        return_decoded: bool = False,
    ) -> int | list[int]:
        if not s_:
            return 0

        # Always resolve encoding_name first so decoder creation works
        # even when a custom tokenizer is provided without a decoder
        encoding_name = get_encoding_name(encoding_name)

        if not callable(tokenizer):
            tokenizer = tiktoken.get_encoding(encoding_name).encode
        if not callable(decoder):
            decoder = tiktoken.get_encoding(encoding_name).decode

        try:
            if return_tokens:
                if return_decoded:
                    a = tokenizer(s_)
                    return len(a), decoder(a)
                return tokenizer(s_)
            return len(tokenizer(s_))
        except Exception:
            return 0

    @staticmethod
    def _calculate_chatitem(i_, tokenizer: Callable, model_name: str) -> int:
        try:
            if isinstance(i_, str):
                return TokenCalculator.tokenize(i_, encoding_name=model_name, tokenizer=tokenizer)

            if isinstance(i_, dict):
                if "text" in i_:
                    return TokenCalculator._calculate_chatitem(
                        str(i_["text"]), tokenizer, model_name
                    )
                elif "image_url" in i_:
                    return 500  # fixed cost for image URL

            if isinstance(i_, list):
                return sum(
                    TokenCalculator._calculate_chatitem(x, tokenizer, model_name) for x in i_
                )
        except Exception:
            return 0

    @staticmethod
    def _calculate_embed_item(s_, tokenizer: Callable) -> int:
        try:
            if isinstance(s_, str):
                return TokenCalculator.tokenize(s_, tokenizer=tokenizer)

            if isinstance(s_, list):
                return sum(TokenCalculator._calculate_embed_item(x, tokenizer) for x in s_)
        except Exception:
            return 0
