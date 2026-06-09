# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Comprehensive unit tests for lionagi.service.token_calculator.

tokenize() always resolves encoding_name first via get_encoding_name(),
so both the tokenizer and decoder are created from a valid encoding even
when a custom tokenizer is provided without an explicit decoder.
"""

import pytest
import tiktoken

from lionagi.service.token_calculator import TokenCalculator, get_encoding_name

# ---------------------------------------------------------------------------
# get_encoding_name
# ---------------------------------------------------------------------------


class TestGetEncodingName:
    def test_valid_model_name_returns_encoding(self):
        name = get_encoding_name("gpt-4o")
        assert name == "o200k_base"

    def test_valid_encoding_name_passthrough(self):
        name = get_encoding_name("cl100k_base")
        assert name == "cl100k_base"

    def test_unknown_model_falls_back_to_o200k_base(self):
        name = get_encoding_name("totally-unknown-model-xyz")
        assert name == "o200k_base"

    def test_empty_string_falls_back(self):
        name = get_encoding_name("")
        assert name == "o200k_base"


# ---------------------------------------------------------------------------
# TokenCalculator.tokenize
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_basic_string_returns_count(self):
        result = TokenCalculator.tokenize("hello world")
        assert isinstance(result, int)
        assert result > 0

    def test_empty_string_returns_zero(self):
        assert TokenCalculator.tokenize("") == 0

    def test_none_returns_zero(self):
        assert TokenCalculator.tokenize(None) == 0

    def test_return_tokens_flag(self):
        result = TokenCalculator.tokenize("hello world", return_tokens=True)
        assert isinstance(result, list)
        assert all(isinstance(t, int) for t in result)
        assert len(result) > 0

    def test_return_tokens_and_decoded(self):
        result = TokenCalculator.tokenize("hello world", return_tokens=True, return_decoded=True)
        assert isinstance(result, tuple)
        count, decoded = result
        assert isinstance(count, int)
        assert count > 0
        assert isinstance(decoded, str)
        assert "hello" in decoded

    def test_explicit_encoding_name(self):
        result = TokenCalculator.tokenize("hello world", encoding_name="cl100k_base")
        assert isinstance(result, int)
        assert result > 0

    def test_explicit_tokenizer_without_encoding_works(self):
        """Passing tokenizer without encoding_name still works.

        encoding_name is always resolved first (None -> "o200k_base"),
        so the decoder is created from a valid encoding.
        """
        enc = tiktoken.get_encoding("cl100k_base")
        result = TokenCalculator.tokenize("hello world", tokenizer=enc.encode)
        assert isinstance(result, int)
        assert result > 0

    def test_explicit_tokenizer_and_decoder(self):
        enc = tiktoken.get_encoding("cl100k_base")
        count, decoded = TokenCalculator.tokenize(
            "hello world",
            tokenizer=enc.encode,
            decoder=enc.decode,
            return_tokens=True,
            return_decoded=True,
        )
        assert count > 0
        assert "hello" in decoded

    def test_long_string_returns_reasonable_count(self):
        short = TokenCalculator.tokenize("hi")
        long_ = TokenCalculator.tokenize("hi " * 100)
        assert long_ > short

    def test_different_encodings_may_give_different_counts(self):
        text = "The quick brown fox jumps over the lazy dog."
        count_cl100k = TokenCalculator.tokenize(text, encoding_name="cl100k_base")
        count_p50k = TokenCalculator.tokenize(text, encoding_name="p50k_base")
        # Both should be positive
        assert count_cl100k > 0
        assert count_p50k > 0

    def test_unicode_string(self):
        result = TokenCalculator.tokenize("hello in Japanese: \u3053\u3093\u306b\u3061\u306f")
        assert isinstance(result, int)
        assert result > 0


# ---------------------------------------------------------------------------
# TokenCalculator._calculate_chatitem
# ---------------------------------------------------------------------------


class TestCalculateChatitem:
    """Tests for the internal _calculate_chatitem method.

    _calculate_chatitem passes the raw tokenizer callable and model_name
    to tokenize(). tokenize() always resolves encoding_name first, so
    the decoder is created correctly and content is counted.
    """

    @pytest.fixture()
    def tokenizer(self):
        return tiktoken.get_encoding("o200k_base").encode

    def test_string_input_returns_positive(self, tokenizer):
        result = TokenCalculator._calculate_chatitem("hello world", tokenizer, "gpt-4o")
        assert isinstance(result, int)
        assert result > 0

    def test_dict_with_text_key_returns_positive(self, tokenizer):
        result = TokenCalculator._calculate_chatitem({"text": "hello world"}, tokenizer, "gpt-4o")
        assert isinstance(result, int)
        assert result > 0

    def test_dict_with_image_url_returns_fixed_cost(self, tokenizer):
        result = TokenCalculator._calculate_chatitem(
            {"image_url": "https://example.com/image.png"},
            tokenizer,
            "gpt-4o",
        )
        assert result == 500

    def test_list_with_only_image_urls(self, tokenizer):
        items = [
            {"image_url": "https://example.com/img1.png"},
            {"image_url": "https://example.com/img2.png"},
        ]
        result = TokenCalculator._calculate_chatitem(items, tokenizer, "gpt-4o")
        assert result == 1000

    def test_list_of_mixed_items(self, tokenizer):
        items = [
            {"text": "hello"},
            {"image_url": "https://example.com/img.png"},
            "world",
        ]
        result = TokenCalculator._calculate_chatitem(items, tokenizer, "gpt-4o")
        # text items now return real counts + 500 for image
        assert result > 500

    def test_empty_string(self, tokenizer):
        result = TokenCalculator._calculate_chatitem("", tokenizer, "gpt-4o")
        assert result == 0

    def test_none_input_returns_none(self, tokenizer):
        result = TokenCalculator._calculate_chatitem(None, tokenizer, "gpt-4o")
        assert result is None

    def test_integer_input_returns_none(self, tokenizer):
        result = TokenCalculator._calculate_chatitem(42, tokenizer, "gpt-4o")
        assert result is None

    def test_dict_without_text_or_image_url(self, tokenizer):
        result = TokenCalculator._calculate_chatitem({"role": "user"}, tokenizer, "gpt-4o")
        assert result is None

    def test_empty_list(self, tokenizer):
        result = TokenCalculator._calculate_chatitem([], tokenizer, "gpt-4o")
        assert result == 0


# ---------------------------------------------------------------------------
# TokenCalculator._calculate_embed_item
# ---------------------------------------------------------------------------


class TestCalculateEmbedItem:
    """Tests for the internal _calculate_embed_item method.

    tokenize() always resolves encoding_name first, so even when
    _calculate_embed_item passes only tokenizer= (no encoding_name),
    the decoder is created correctly from the fallback encoding.
    """

    @pytest.fixture()
    def tokenizer(self):
        return tiktoken.get_encoding("cl100k_base").encode

    def test_string_input_returns_positive(self, tokenizer):
        result = TokenCalculator._calculate_embed_item("hello world", tokenizer)
        assert isinstance(result, int)
        assert result > 0

    def test_list_of_strings_returns_positive(self, tokenizer):
        result = TokenCalculator._calculate_embed_item(["hello", "world"], tokenizer)
        assert isinstance(result, int)
        assert result > 0

    def test_empty_string(self, tokenizer):
        result = TokenCalculator._calculate_embed_item("", tokenizer)
        assert result == 0

    def test_empty_list(self, tokenizer):
        result = TokenCalculator._calculate_embed_item([], tokenizer)
        assert result == 0

    def test_invalid_type_returns_none(self, tokenizer):
        result = TokenCalculator._calculate_embed_item(42, tokenizer)
        assert result is None

    def test_none_input_returns_none(self, tokenizer):
        result = TokenCalculator._calculate_embed_item(None, tokenizer)
        assert result is None

    def test_nested_list_returns_positive(self, tokenizer):
        result = TokenCalculator._calculate_embed_item([["hello", "world"]], tokenizer)
        assert isinstance(result, int)
        assert result > 0


# ---------------------------------------------------------------------------
# TokenCalculator.calculate_message_tokens
# ---------------------------------------------------------------------------


class TestCalculateMessageTokens:
    """Tests for the top-level calculate_message_tokens static method.

    Each message adds 4 tokens of overhead plus actual content tokens.
    """

    def test_single_message_with_content(self):
        messages = [{"role": "user", "content": "hello world"}]
        result = TokenCalculator.calculate_message_tokens(messages)
        # 4 overhead + actual content tokens
        assert result > 4

    def test_multiple_messages_with_content(self):
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "4"},
        ]
        result = TokenCalculator.calculate_message_tokens(messages)
        # 3 * 4 overhead + actual content tokens
        assert result > 12

    def test_empty_message_list(self):
        result = TokenCalculator.calculate_message_tokens([])
        assert result == 0

    def test_message_with_none_content_raises_typeerror(self):
        """Message with None content: _calculate_chatitem returns None.

        Adding None to num_tokens (int) raises TypeError because
        None doesn't match any isinstance check in _calculate_chatitem,
        so it returns None implicitly, then num_tokens += None fails.
        """
        messages = [{"role": "assistant", "content": None}]
        with pytest.raises(TypeError):
            TokenCalculator.calculate_message_tokens(messages)

    def test_message_with_dict_content_text_key(self):
        messages = [{"role": "user", "content": {"text": "what is the weather?"}}]
        result = TokenCalculator.calculate_message_tokens(messages)
        assert result > 4  # 4 overhead + actual content tokens

    def test_message_with_list_content_image_only(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"image_url": "https://example.com/image.png"},
                ],
            }
        ]
        result = TokenCalculator.calculate_message_tokens(messages)
        assert result == 504  # 4 overhead + 500 image

    def test_message_with_list_content_text_and_image(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"text": "describe this image"},
                    {"image_url": "https://example.com/image.png"},
                ],
            }
        ]
        result = TokenCalculator.calculate_message_tokens(messages)
        # 4 overhead + content tokens + 500 image
        assert result > 504

    def test_message_missing_content_key_raises_typeerror(self):
        """Message without 'content' key: .get("content") returns None.

        _calculate_chatitem(None, ...) returns None, then
        num_tokens += None raises TypeError.
        """
        messages = [{"role": "user"}]
        with pytest.raises(TypeError):
            TokenCalculator.calculate_message_tokens(messages)

    def test_overhead_per_message_is_four(self):
        one = [{"role": "user", "content": ""}]
        two = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": ""},
        ]
        result_one = TokenCalculator.calculate_message_tokens(one)
        result_two = TokenCalculator.calculate_message_tokens(two)
        assert result_one == 4
        assert result_two == 8
        assert result_two - result_one == 4

    def test_large_conversation_overhead(self):
        messages = [{"role": "user", "content": f"Message number {i}"} for i in range(50)]
        result = TokenCalculator.calculate_message_tokens(messages)
        # 50 * 4 overhead + actual content tokens
        assert result > 200


# ---------------------------------------------------------------------------
# TokenCalculator.calculate_embed_token
# ---------------------------------------------------------------------------


class TestCalculateEmbedToken:
    def test_multiple_strings(self):
        result = TokenCalculator.calculate_embed_token(["hello world", "goodbye world"])
        assert isinstance(result, int)
        assert result > 0

    def test_empty_list(self):
        result = TokenCalculator.calculate_embed_token([])
        assert result == 0

    def test_empty_string_in_list(self):
        result = TokenCalculator.calculate_embed_token([""])
        assert result == 0

    def test_with_invalid_items_returns_zero(self):
        """Integers in input: _calculate_embed_item returns None for ints.

        Summing with None raises TypeError, caught by outer try/except.
        """
        result = TokenCalculator.calculate_embed_token([123, 456])
        assert result == 0


# ---------------------------------------------------------------------------
# Integration: tokenize standalone (works correctly)
# ---------------------------------------------------------------------------


class TestTokenizeStandalone:
    """Tests for tokenize when called directly (not via _calculate_*).

    When called without a pre-built tokenizer, tokenize resolves
    encoding_name via get_encoding_name and creates both tokenizer
    and decoder from the resolved encoding. This path works correctly.
    """

    def test_count_matches_token_list_length(self):
        text = "Some sample text for testing."
        count = TokenCalculator.tokenize(text)
        tokens = TokenCalculator.tokenize(text, return_tokens=True)
        assert len(tokens) == count

    def test_decoded_output_matches_input(self):
        text = "hello world"
        _, decoded = TokenCalculator.tokenize(text, return_tokens=True, return_decoded=True)
        assert decoded == text

    def test_different_encoding_names_produce_tokens(self):
        text = "The quick brown fox."
        for enc_name in ("cl100k_base", "o200k_base", "p50k_base"):
            count = TokenCalculator.tokenize(text, encoding_name=enc_name)
            assert count > 0, f"Failed for encoding {enc_name}"

    def test_tokenize_empty_with_return_tokens(self):
        result = TokenCalculator.tokenize("", return_tokens=True)
        assert result == 0

    def test_tokenize_very_long_string(self):
        text = "word " * 10000
        result = TokenCalculator.tokenize(text)
        assert result > 1000

    def test_broken_tokenizer_returns_zero(self):

        def bad_tokenizer(s):
            raise RuntimeError("broken")

        enc = tiktoken.get_encoding("cl100k_base")
        result = TokenCalculator.tokenize(
            "hello",
            encoding_name="cl100k_base",
            tokenizer=bad_tokenizer,
            decoder=enc.decode,
        )
        assert result == 0

    def test_broken_decoder_returns_zero(self):
        enc = tiktoken.get_encoding("cl100k_base")

        def bad_decoder(tokens):
            raise RuntimeError("decode failed")

        result = TokenCalculator.tokenize(
            "hello",
            encoding_name="cl100k_base",
            tokenizer=enc.encode,
            decoder=bad_decoder,
            return_tokens=True,
            return_decoded=True,
        )
        assert result == 0


# ---------------------------------------------------------------------------
# Edge cases: concurrent tokenize calls
# ---------------------------------------------------------------------------


class TestTokenizeConcurrent:
    @pytest.mark.asyncio
    async def test_concurrent_tokenize_calls_thread_safe(self):
        import asyncio

        texts = [f"hello world token test number {i}" for i in range(20)]
        results = await asyncio.gather(
            *[
                asyncio.get_event_loop().run_in_executor(None, TokenCalculator.tokenize, t)
                for t in texts
            ]
        )
        assert all(isinstance(r, int) and r > 0 for r in results)

    @pytest.mark.asyncio
    async def test_concurrent_tokenize_with_different_encodings(self):
        import asyncio

        encodings = ["cl100k_base", "o200k_base", "p50k_base"] * 4
        results = await asyncio.gather(
            *[
                asyncio.get_event_loop().run_in_executor(
                    None, lambda enc=e: TokenCalculator.tokenize("hello world", encoding_name=enc)
                )
                for e in encodings
            ]
        )
        assert all(isinstance(r, int) and r > 0 for r in results)

    def test_very_large_input_returns_count(self):
        text = "token " * 50000
        result = TokenCalculator.tokenize(text)
        assert isinstance(result, int)
        assert result > 10000

    def test_very_large_input_return_tokens(self):
        text = "word " * 20000
        tokens = TokenCalculator.tokenize(text, return_tokens=True)
        assert isinstance(tokens, list)
        assert len(tokens) > 5000


# ---------------------------------------------------------------------------
# Edge cases: deeply nested multimodal content
# ---------------------------------------------------------------------------


class TestCalculateMessageTokensNestedMultimodal:
    def test_deeply_nested_list_of_lists_with_dicts(self):
        messages = [
            {
                "role": "user",
                "content": [
                    [
                        {"text": "describe"},
                        {"image_url": "https://example.com/img1.png"},
                    ],
                    [
                        {"text": "and this"},
                        {"image_url": "https://example.com/img2.png"},
                    ],
                ],
            }
        ]
        result = TokenCalculator.calculate_message_tokens(messages)
        assert result > 4 + 500 + 500

    def test_list_content_with_nested_list_items(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"text": "outer"},
                    [{"text": "inner1"}, {"image_url": "https://example.com/x.png"}],
                ],
            }
        ]
        result = TokenCalculator.calculate_message_tokens(messages)
        assert result > 504

    def test_calculate_chatitem_nested_list_of_dicts(self):
        tokenizer = tiktoken.get_encoding("o200k_base").encode
        items = [
            [{"text": "a"}, {"image_url": "https://example.com/i.png"}],
            [{"text": "b"}, {"text": "c"}],
        ]
        result = TokenCalculator._calculate_chatitem(items, tokenizer, "gpt-4o")
        assert result > 500
