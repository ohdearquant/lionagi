# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

try:
    from rapidfuzz import distance as _rf_distance

    _HAS_RAPIDFUZZ = True
except ImportError:
    _rf_distance = None  # type: ignore[assignment]
    _HAS_RAPIDFUZZ = False


__all__ = ("string_similarity",)


def cosine_similarity(s1: str, s2: str) -> float:
    if not s1 or not s2:
        return 0.0

    set1, set2 = set(s1), set(s2)
    intersection = set1.intersection(set2)

    if not set1 or not set2:
        return 0.0

    return len(intersection) / ((len(set1) * len(set2)) ** 0.5)


def hamming_similarity(s1: str, s2: str) -> float:
    if not s1 or not s2 or len(s1) != len(s2):
        return 0.0

    matches = sum(c1 == c2 for c1, c2 in zip(s1, s2, strict=False))
    return matches / len(s1)


def jaro_distance(s: str, t: str) -> float:
    s_len = len(s)
    t_len = len(t)

    if s_len == 0 and t_len == 0:
        return 1.0
    elif s_len == 0 or t_len == 0:
        return 0.0

    match_distance = (max(s_len, t_len) // 2) - 1
    match_distance = max(0, match_distance)

    s_matches = [False] * s_len
    t_matches = [False] * t_len

    matches = 0
    transpositions = 0

    for i in range(s_len):
        start = max(0, i - match_distance)
        end = min(i + match_distance + 1, t_len)

        for j in range(start, end):
            if t_matches[j] or s[i] != t[j]:
                continue
            s_matches[i] = t_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    k = 0
    for i in range(s_len):
        if not s_matches[i]:
            continue
        while not t_matches[k]:
            k += 1
        if s[i] != t[k]:
            transpositions += 1
        k += 1

    transpositions //= 2

    return (matches / s_len + matches / t_len + (matches - transpositions) / matches) / 3.0


def jaro_winkler_similarity(s: str, t: str, scaling: float = 0.1) -> float:
    if not 0 <= scaling <= 0.25:
        raise ValueError("Scaling factor must be between 0 and 0.25")

    if _HAS_RAPIDFUZZ:
        return _rf_distance.JaroWinkler.similarity(s, t, prefix_weight=scaling)

    jaro_sim = jaro_distance(s, t)
    prefix_len = 0
    for s_char, t_char in zip(s, t, strict=False):
        if s_char != t_char:
            break
        prefix_len += 1
        if prefix_len == 4:
            break

    return jaro_sim + (prefix_len * scaling * (1 - jaro_sim))


def levenshtein_distance(a: str, b: str) -> int:
    from itertools import product

    if not a:
        return len(b)
    if not b:
        return len(a)

    m, n = len(a), len(b)
    d = [[0] * (n + 1) for _ in range(m + 1)]

    for i in range(m + 1):
        d[i][0] = i
    for j in range(n + 1):
        d[0][j] = j

    for i, j in product(range(1, m + 1), range(1, n + 1)):
        cost = 0 if a[i - 1] == b[j - 1] else 1
        d[i][j] = min(
            d[i - 1][j] + 1,
            d[i][j - 1] + 1,
            d[i - 1][j - 1] + cost,
        )

    return d[m][n]


def levenshtein_similarity(s1: str, s2: str) -> float:
    if _HAS_RAPIDFUZZ:
        return _rf_distance.Levenshtein.normalized_similarity(s1, s2)

    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0

    distance = levenshtein_distance(s1, s2)
    max_len = max(len(s1), len(s2))
    return 1 - (distance / max_len)


def sequence_matcher_similarity(s1: str, s2: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, s1, s2).ratio()


SIMILARITY_ALGO_MAP = {
    "jaro_winkler": jaro_winkler_similarity,
    "levenshtein": levenshtein_similarity,
    "sequence_matcher": sequence_matcher_similarity,
    "hamming": hamming_similarity,
    "cosine": cosine_similarity,
}


SIMILARITY_TYPE = Literal[
    "jaro_winkler",
    "levenshtein",
    "sequence_matcher",
    "hamming",
    "cosine",
]

SimilarityFunc = Callable[[str, str], float]


@dataclass(frozen=True)
class MatchResult:
    word: str
    score: float
    index: int


def string_similarity(
    word: str,
    correct_words: "Sequence[str]",
    algorithm: SIMILARITY_TYPE | Callable[[str, str], float] = "jaro_winkler",
    threshold: float = 0.0,
    case_sensitive: bool = False,
    return_most_similar: bool = False,
) -> str | list[str] | None:
    if not correct_words:
        raise ValueError("correct_words must not be empty")

    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0.0 and 1.0")

    compare_word = str(word)
    original_words = [str(w) for w in correct_words]

    if not case_sensitive:
        compare_word = compare_word.lower()
        compare_words = [w.lower() for w in original_words]
    else:
        compare_words = original_words.copy()

    if isinstance(algorithm, str):
        score_func = SIMILARITY_ALGO_MAP.get(algorithm)
        if score_func is None:
            raise ValueError(f"Unsupported algorithm: {algorithm}")
    elif callable(algorithm):
        score_func = algorithm
    else:
        raise ValueError("algorithm must be a string specifying a built-in algorithm or a callable")

    results = []
    for idx, (orig_word, comp_word) in enumerate(zip(original_words, compare_words, strict=False)):
        if algorithm == "hamming" and len(comp_word) != len(compare_word):
            continue

        score = score_func(compare_word, comp_word)
        if score >= threshold:
            results.append(MatchResult(orig_word, score, idx))

    if not results:
        return None

    results.sort(key=lambda x: (-x.score, x.index))

    if case_sensitive:
        max_score = results[0].score
        results = [r for r in results if r.score == max_score]

    if return_most_similar:
        return results[0].word

    return [r.word for r in results]
