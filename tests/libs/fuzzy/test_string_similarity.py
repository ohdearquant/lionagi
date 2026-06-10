"""Tests for lionagi/ln/fuzzy/_string_similarity.py

Target: Cover line 32 (cosine_similarity edge case)
"""

import pytest

from lionagi.ln.fuzzy._string_similarity import (
    cosine_similarity,
    hamming_similarity,
    jaro_distance,
    jaro_winkler_similarity,
    levenshtein_distance,
    levenshtein_similarity,
    sequence_matcher_similarity,
    string_similarity,
)

# ============================================================================
# Test cosine_similarity - Line 32
# ============================================================================


def test_cosine_similarity_basic():
    """Test basic cosine similarity"""
    assert cosine_similarity("hello", "hello") == 1.0
    assert 0 < cosine_similarity("hello", "help") < 1.0


def test_cosine_similarity_empty():
    """Test cosine similarity with empty strings - lines 25-26"""
    assert cosine_similarity("", "hello") == 0.0
    assert cosine_similarity("hello", "") == 0.0
    assert cosine_similarity("", "") == 0.0


def test_cosine_similarity_no_overlap():
    """Test cosine similarity with no character overlap"""
    assert cosine_similarity("abc", "def") == 0.0


# ============================================================================
# Test string_similarity function
# ============================================================================


def test_string_similarity_jaro_winkler():
    """Test string_similarity with jaro_winkler algorithm"""
    result = string_similarity("hello", ["hello", "help", "world"])
    assert "hello" in result


def test_string_similarity_threshold():
    """Test string_similarity with threshold"""
    result = string_similarity("hello", ["hello", "help", "world"], threshold=0.9)
    assert isinstance(result, list)


def test_string_similarity_most_similar():
    """Test return_most_similar option"""
    result = string_similarity("hello", ["hello", "help", "world"], return_most_similar=True)
    assert isinstance(result, str)


def test_string_similarity_invalid_threshold():
    """Test invalid threshold raises ValueError"""
    with pytest.raises(ValueError, match="threshold must be between"):
        string_similarity("hello", ["world"], threshold=1.5)


def test_string_similarity_empty_correct_words():
    """Test empty correct_words raises ValueError"""
    with pytest.raises(ValueError, match="correct_words must not be empty"):
        string_similarity("hello", [])


def test_string_similarity_invalid_algorithm():
    """Test invalid algorithm raises ValueError"""
    with pytest.raises(ValueError, match="Unsupported algorithm"):
        string_similarity("hello", ["world"], algorithm="invalid_algo")


def test_string_similarity_custom_function():
    """Test custom similarity function"""

    def custom_sim(s1, s2):
        return 1.0 if s1 == s2 else 0.0

    result = string_similarity("hello", ["hello", "world"], algorithm=custom_sim)
    assert "hello" in result


def test_string_similarity_case_sensitive():
    """Test case sensitive matching"""
    result = string_similarity("Hello", ["hello", "Hello"], case_sensitive=True)
    assert "Hello" in result


def test_string_similarity_hamming():
    """Test hamming algorithm"""
    result = string_similarity("hello", ["hello", "hallo"], algorithm="hamming")
    assert "hello" in result


def test_string_similarity_no_matches():
    """Test when no matches found"""
    result = string_similarity("hello", ["xyz", "abc"], threshold=0.9, algorithm="levenshtein")
    assert result is None


# ============================================================================
# Test other similarity algorithms
# ============================================================================


def test_hamming_similarity():
    """Test hamming similarity"""
    assert hamming_similarity("hello", "hello") == 1.0
    assert hamming_similarity("hello", "hallo") == 0.8
    assert hamming_similarity("hello", "help") == 0.0  # Different lengths


def test_jaro_distance():
    """Test jaro distance"""
    assert jaro_distance("", "") == 1.0
    assert jaro_distance("hello", "") == 0.0
    assert jaro_distance("", "hello") == 0.0
    assert 0 < jaro_distance("hello", "hallo") < 1.0


def test_jaro_winkler_similarity():
    """Test jaro winkler similarity"""
    assert jaro_winkler_similarity("hello", "hello") == 1.0
    assert 0 < jaro_winkler_similarity("hello", "hallo") < 1.0


def test_jaro_winkler_invalid_scaling():
    """Test jaro winkler with invalid scaling"""
    with pytest.raises(ValueError, match="Scaling factor must be between"):
        jaro_winkler_similarity("hello", "world", scaling=0.3)


def test_levenshtein_distance():
    """Test levenshtein distance"""
    assert levenshtein_distance("", "") == 0
    assert levenshtein_distance("hello", "") == 5
    assert levenshtein_distance("", "hello") == 5
    assert levenshtein_distance("hello", "hello") == 0
    assert levenshtein_distance("hello", "hallo") == 1


def test_levenshtein_similarity():
    """Test levenshtein similarity"""
    assert levenshtein_similarity("", "") == 1.0
    assert levenshtein_similarity("hello", "") == 0.0
    assert levenshtein_similarity("", "hello") == 0.0
    assert levenshtein_similarity("hello", "hello") == 1.0
    assert 0 < levenshtein_similarity("hello", "hallo") < 1.0


def test_sequence_matcher_similarity():
    """Test sequence matcher similarity"""
    assert sequence_matcher_similarity("hello", "hello") == 1.0
    assert 0 < sequence_matcher_similarity("hello", "hallo") < 1.0
