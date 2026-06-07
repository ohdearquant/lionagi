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
    """Tests for the get_encoding_name helper function."""

    def test_valid_model_name_returns_encoding(self):
        """Known model names should resolve to their tiktoken encoding."""
        name = get_encoding_name("gpt-4o")
        assert name == "o200k_base"

    def test_valid_model_gpt35(self):
        """gpt-3.5-turbo uses cl100k_base encoding."""
        name = get_encoding_name("gpt-3.5-turbo")
        assert name == "cl100k_base"

    def test_valid_model_gpt4(self):
        """gpt-4 uses cl100k_base encoding."""
        name = get_encoding_name("gpt-4")
        assert name == "cl100k_base"

    def test_valid_encoding_name_passthrough(self):
        """When value is already an encoding name, return it directly."""
        name = get_encoding_name("cl100k_base")
        assert name == "cl100k_base"

    def test_valid_encoding_name_o200k(self):
        """o200k_base is a valid encoding name and should pass through."""
        name = get_encoding_name("o200k_base")
        assert name == "o200k_base"

    def test_valid_encoding_name_p50k(self):
        """p50k_base is a valid encoding name and should pass through."""
        name = get_encoding_name("p50k_base")
        assert name == "p50k_base"

    def test_unknown_model_falls_back_to_o200k_base(self):
        """Unknown model/encoding names should fall back to o200k_base."""
        name = get_encoding_name("totally-unknown-model-xyz")
        assert name == "o200k_base"

    def test_empty_string_falls_back(self):
        """Empty string is neither a valid model nor encoding."""
        name = get_encoding_name("")
        assert name == "o200k_base"

    def test_nonsense_string_falls_back(self):
        """Arbitrary nonsense string falls back to o200k_base."""
        name = get_encoding_name("!@#$%^&*()")
        assert name == "o200k_base"


# ---------------------------------------------------------------------------
# TokenCalculator.tokenize
# ---------------------------------------------------------------------------


class TestTokenize:
    """Tests for TokenCalculator.tokenize."""

    def test_basic_string_returns_count(self):
        """Tokenizing a simple string returns an integer token count."""
        result = TokenCalculator.tokenize("hello world")
        assert isinstance(result, int)
        assert result > 0

    def test_empty_string_returns_zero(self):
        """Empty string should return 0."""
        assert TokenCalculator.tokenize("") == 0

    def test_none_returns_zero(self):
        """None input should return 0."""
        assert TokenCalculator.tokenize(None) == 0

    def test_return_tokens_flag(self):
        """return_tokens=True should return the list of token IDs."""
        result = TokenCalculator.tokenize("hello world", return_tokens=True)
        assert isinstance(result, list)
        assert all(isinstance(t, int) for t in result)
        assert len(result) > 0

    def test_return_tokens_and_decoded(self):
        """return_tokens=True + return_decoded=True returns (count, decoded_str)."""
        result = TokenCalculator.tokenize("hello world", return_tokens=True, return_decoded=True)
        assert isinstance(result, tuple)
        count, decoded = result
        assert isinstance(count, int)
        assert count > 0
        assert isinstance(decoded, str)
        assert "hello" in decoded

    def test_explicit_encoding_name(self):
        """Passing an explicit encoding_name should work."""
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

    def test_explicit_tokenizer_with_encoding_name(self):
        """Passing tokenizer + encoding_name allows decoder to be created."""
        enc = tiktoken.get_encoding("cl100k_base")
        result = TokenCalculator.tokenize(
            "hello world",
            encoding_name="cl100k_base",
            tokenizer=enc.encode,
        )
        assert isinstance(result, int)
        assert result > 0

    def test_explicit_tokenizer_and_decoder(self):
        """Custom tokenizer + decoder should work for return_decoded."""
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

    def test_explicit_tokenizer_and_decoder_count_only(self):
        """Custom tokenizer + decoder, default mode returns int count."""
        enc = tiktoken.get_encoding("cl100k_base")
        result = TokenCalculator.tokenize(
            "hello world",
            tokenizer=enc.encode,
            decoder=enc.decode,
        )
        assert isinstance(result, int)
        assert result > 0

    def test_long_string_returns_reasonable_count(self):
        """A longer string should return a proportionally larger count."""
        short = TokenCalculator.tokenize("hi")
        long_ = TokenCalculator.tokenize("hi " * 100)
        assert long_ > short

    def test_encoding_name_none_uses_fallback(self):
        """When encoding_name is None, get_encoding_name(None) falls back to o200k_base."""
        result = TokenCalculator.tokenize("hello", encoding_name=None)
        assert isinstance(result, int)
        assert result > 0

    def test_default_encoding_name_when_no_tokenizer(self):
        """When neither tokenizer nor encoding_name is given, falls back properly."""
        result = TokenCalculator.tokenize("some text here")
        assert isinstance(result, int)
        assert result > 0

    def test_different_encodings_may_give_different_counts(self):
        """Different encoding names can produce different token counts."""
        text = "The quick brown fox jumps over the lazy dog."
        count_cl100k = TokenCalculator.tokenize(text, encoding_name="cl100k_base")
        count_p50k = TokenCalculator.tokenize(text, encoding_name="p50k_base")
        # Both should be positive
        assert count_cl100k > 0
        assert count_p50k > 0

    def test_return_tokens_list_content(self):
        """Token IDs from return_tokens should re-encode back to same count."""
        text = "testing token IDs"
        token_ids = TokenCalculator.tokenize(text, return_tokens=True)
        count = TokenCalculator.tokenize(text)
        assert len(token_ids) == count

    def test_whitespace_only_string(self):
        """Whitespace-only string should return positive token count."""
        result = TokenCalculator.tokenize("   ")
        assert isinstance(result, int)
        assert result > 0

    def test_unicode_string(self):
        """Unicode string should be tokenizable."""
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
        """String content returns a positive token count."""
        result = TokenCalculator._calculate_chatitem("hello world", tokenizer, "gpt-4o")
        assert isinstance(result, int)
        assert result > 0

    def test_dict_with_text_key_returns_positive(self, tokenizer):
        """Dict with 'text' key returns a positive token count."""
        result = TokenCalculator._calculate_chatitem({"text": "hello world"}, tokenizer, "gpt-4o")
        assert isinstance(result, int)
        assert result > 0

    def test_dict_with_image_url_returns_fixed_cost(self, tokenizer):
        """Dict with 'image_url' key should return the fixed image cost (500)."""
        result = TokenCalculator._calculate_chatitem(
            {"image_url": "https://example.com/image.png"},
            tokenizer,
            "gpt-4o",
        )
        assert result == 500

    def test_list_with_only_image_urls(self, tokenizer):
        """List of image_url items should correctly sum the fixed costs."""
        items = [
            {"image_url": "https://example.com/img1.png"},
            {"image_url": "https://example.com/img2.png"},
        ]
        result = TokenCalculator._calculate_chatitem(items, tokenizer, "gpt-4o")
        assert result == 1000

    def test_list_of_mixed_items(self, tokenizer):
        """Mixed list: text items return counts, image returns 500."""
        items = [
            {"text": "hello"},
            {"image_url": "https://example.com/img.png"},
            "world",
        ]
        result = TokenCalculator._calculate_chatitem(items, tokenizer, "gpt-4o")
        # text items now return real counts + 500 for image
        assert result > 500

    def test_empty_string(self, tokenizer):
        """Empty string returns 0 (from tokenize's early return)."""
        result = TokenCalculator._calculate_chatitem("", tokenizer, "gpt-4o")
        assert result == 0

    def test_none_input_returns_none(self, tokenizer):
        """None input doesn't match any isinstance check; returns None implicitly."""
        result = TokenCalculator._calculate_chatitem(None, tokenizer, "gpt-4o")
        assert result is None

    def test_integer_input_returns_none(self, tokenizer):
        """Integer input doesn't match any isinstance check; returns None."""
        result = TokenCalculator._calculate_chatitem(42, tokenizer, "gpt-4o")
        assert result is None

    def test_dict_without_text_or_image_url(self, tokenizer):
        """Dict without 'text' or 'image_url' returns None (no branch matches)."""
        result = TokenCalculator._calculate_chatitem({"role": "user"}, tokenizer, "gpt-4o")
        assert result is None

    def test_empty_list(self, tokenizer):
        """Empty list returns 0 (sum of nothing)."""
        result = TokenCalculator._calculate_chatitem([], tokenizer, "gpt-4o")
        assert result == 0

    def test_dict_text_value_is_number_returns_positive(self, tokenizer):
        """Dict with 'text' whose value is a number: str-converted, then counted."""
        result = TokenCalculator._calculate_chatitem({"text": 42}, tokenizer, "gpt-4o")
        assert isinstance(result, int)
        assert result > 0


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
        """String returns a positive token count."""
        result = TokenCalculator._calculate_embed_item("hello world", tokenizer)
        assert isinstance(result, int)
        assert result > 0

    def test_list_of_strings_returns_positive(self, tokenizer):
        """List of strings: each item contributes tokens."""
        result = TokenCalculator._calculate_embed_item(["hello", "world"], tokenizer)
        assert isinstance(result, int)
        assert result > 0

    def test_empty_string(self, tokenizer):
        """Empty string returns 0 (from tokenize early return)."""
        result = TokenCalculator._calculate_embed_item("", tokenizer)
        assert result == 0

    def test_empty_list(self, tokenizer):
        """Empty list returns 0 (sum of nothing)."""
        result = TokenCalculator._calculate_embed_item([], tokenizer)
        assert result == 0

    def test_invalid_type_returns_none(self, tokenizer):
        """Non-str, non-list input returns None (falls through isinstance checks)."""
        result = TokenCalculator._calculate_embed_item(42, tokenizer)
        assert result is None

    def test_none_input_returns_none(self, tokenizer):
        """None input doesn't match any isinstance check."""
        result = TokenCalculator._calculate_embed_item(None, tokenizer)
        assert result is None

    def test_nested_list_returns_positive(self, tokenizer):
        """Nested list items return actual token counts."""
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
        """A single message returns overhead + content tokens."""
        messages = [{"role": "user", "content": "hello world"}]
        result = TokenCalculator.calculate_message_tokens(messages)
        # 4 overhead + actual content tokens
        assert result > 4

    def test_multiple_messages_with_content(self):
        """Multiple messages return overhead + content for each."""
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "4"},
        ]
        result = TokenCalculator.calculate_message_tokens(messages)
        # 3 * 4 overhead + actual content tokens
        assert result > 12

    def test_empty_message_list(self):
        """Empty list should return 0 tokens."""
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
        """Message content as dict with 'text' key returns overhead + tokens."""
        messages = [{"role": "user", "content": {"text": "what is the weather?"}}]
        result = TokenCalculator.calculate_message_tokens(messages)
        assert result > 4  # 4 overhead + actual content tokens

    def test_message_with_list_content_image_only(self):
        """Multimodal message with image_url only: overhead + 500."""
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
        """Multimodal: text tokens + image 500 + overhead."""
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

    def test_custom_model_kwarg(self):
        """Passing model= kwarg should change the tokenizer used.

        Both should return overhead + content (not just overhead).
        """
        messages = [{"role": "user", "content": "hello world"}]
        result_default = TokenCalculator.calculate_message_tokens(messages)
        result_gpt35 = TokenCalculator.calculate_message_tokens(messages, model="gpt-3.5-turbo")
        assert result_default > 4
        assert result_gpt35 > 4

    def test_message_missing_content_key_raises_typeerror(self):
        """Message without 'content' key: .get("content") returns None.

        _calculate_chatitem(None, ...) returns None, then
        num_tokens += None raises TypeError.
        """
        messages = [{"role": "user"}]
        with pytest.raises(TypeError):
            TokenCalculator.calculate_message_tokens(messages)

    def test_overhead_per_message_is_four(self):
        """Each message adds exactly 4 tokens of overhead."""
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
        """50 messages with content produce more than just overhead."""
        messages = [{"role": "user", "content": f"Message number {i}"} for i in range(50)]
        result = TokenCalculator.calculate_message_tokens(messages)
        # 50 * 4 overhead + actual content tokens
        assert result > 200


# ---------------------------------------------------------------------------
# TokenCalculator.calculate_embed_token
# ---------------------------------------------------------------------------


class TestCalculateEmbedToken:
    """Tests for the top-level calculate_embed_token static method."""

    def test_single_string_input(self):
        """Single string in the list should return positive token count."""
        result = TokenCalculator.calculate_embed_token(["hello world"])
        assert isinstance(result, int)
        assert result > 0

    def test_multiple_strings(self):
        """Multiple strings: each contributes tokens."""
        result = TokenCalculator.calculate_embed_token(["hello world", "goodbye world"])
        assert isinstance(result, int)
        assert result > 0

    def test_empty_list(self):
        """Empty input list should return 0."""
        result = TokenCalculator.calculate_embed_token([])
        assert result == 0

    def test_empty_string_in_list(self):
        """List with an empty string should return 0 tokens."""
        result = TokenCalculator.calculate_embed_token([""])
        assert result == 0

    def test_custom_model_kwarg(self):
        """Passing model= kwarg for embedding model."""
        result = TokenCalculator.calculate_embed_token(
            ["hello world"], model="text-embedding-3-large"
        )
        assert isinstance(result, int)
        assert result > 0

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
        """Length of token list should equal the int count."""
        text = "Some sample text for testing."
        count = TokenCalculator.tokenize(text)
        tokens = TokenCalculator.tokenize(text, return_tokens=True)
        assert len(tokens) == count

    def test_decoded_output_matches_input(self):
        """Decoded output should reconstruct the original text."""
        text = "hello world"
        _, decoded = TokenCalculator.tokenize(text, return_tokens=True, return_decoded=True)
        assert decoded == text

    def test_different_encoding_names_produce_tokens(self):
        """Various encoding names all produce valid token counts."""
        text = "The quick brown fox."
        for enc_name in ("cl100k_base", "o200k_base", "p50k_base"):
            count = TokenCalculator.tokenize(text, encoding_name=enc_name)
            assert count > 0, f"Failed for encoding {enc_name}"

    def test_tokenize_with_model_name_as_encoding(self):
        """Passing a model name as encoding_name resolves via get_encoding_name."""
        text = "hello world"
        result = TokenCalculator.tokenize(text, encoding_name="gpt-4o")
        # get_encoding_name("gpt-4o") -> "o200k_base", then tokenize works
        assert isinstance(result, int)
        assert result > 0

    def test_tokenize_with_gpt35_model_name(self):
        """gpt-3.5-turbo as encoding_name resolves to cl100k_base."""
        text = "hello world"
        result = TokenCalculator.tokenize(text, encoding_name="gpt-3.5-turbo")
        assert isinstance(result, int)
        assert result > 0

    def test_tokenize_return_tokens_gives_ints(self):
        """return_tokens=True returns a list of integer token IDs."""
        tokens = TokenCalculator.tokenize("hello world", return_tokens=True)
        assert isinstance(tokens, list)
        assert all(isinstance(t, int) for t in tokens)

    def test_tokenize_empty_with_return_tokens(self):
        """Empty string with return_tokens still returns 0 (early exit)."""
        result = TokenCalculator.tokenize("", return_tokens=True)
        assert result == 0

    def test_tokenize_unicode(self):
        """Unicode characters should tokenize without error."""
        result = TokenCalculator.tokenize("\u4f60\u597d\u4e16\u754c")
        assert isinstance(result, int)
        assert result > 0

    def test_tokenize_very_long_string(self):
        """Very long strings should return large token counts."""
        text = "word " * 10000
        result = TokenCalculator.tokenize(text)
        assert result > 1000

    def test_tokenize_special_characters(self):
        """Special characters should be tokenizable."""
        result = TokenCalculator.tokenize("!@#$%^&*()_+-=[]{}|;':\",./<>?")
        assert isinstance(result, int)
        assert result > 0

    def test_broken_tokenizer_returns_zero(self):
        """If the tokenizer raises, the inner try/except returns 0."""

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
        """If decoder raises during return_decoded, inner try/except returns 0."""
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
