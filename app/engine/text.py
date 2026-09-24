"""Tokenisation. One implementation, used by everything that compares text.

This exists as its own module because the previous version had three different
notions of "what is a word" — one in the relevance scorer, one in the skills
filter, one in the keyword extractor — and they disagreed. When matching is
split across several tokenisers you get several answers and no way to say which
is right.
"""
from __future__ import annotations

import re

# A leading dot is allowed so `.NET` survives as one token. Without it the
# token becomes `net`, which then fails to match a `.net` entry elsewhere.
_WORD = re.compile(r"(?:\.[A-Za-z]|[A-Za-z0-9])[A-Za-z0-9+#.\-]*")

STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "for", "with",
    "at", "by", "from", "as", "is", "are", "was", "were", "be", "been", "being",
    "will", "would", "can", "could", "should", "may", "might", "must", "this",
    "that", "these", "those", "you", "your", "our", "we", "they", "their", "it",
    "its", "has", "have", "had", "do", "does", "did", "not", "no", "so", "if",
    "than", "then", "there", "here", "who", "which", "what", "when", "how",
    "all", "any", "some", "more", "most", "other", "into", "over", "also",
    "across", "within", "while", "about", "up", "out", "new", "using", "use",
})


def tokens(text: str) -> list[str]:
    """Words with trailing sentence punctuation stripped, leading dot kept."""
    return [t.rstrip(".-") or t for t in _WORD.findall(text or "")]


def lower_tokens(text: str) -> list[str]:
    return [t.lower() for t in tokens(text)]


def content_words(text: str, min_length: int = 3) -> set[str]:
    """Meaningful words only — the basis for any overlap comparison.

    Stopwords and very short tokens are dropped. Without this, two unrelated
    sentences overlap heavily on "the", "and", "with" and every comparison
    trends towards "somewhat similar".
    """
    return {
        t for t in lower_tokens(text)
        if t not in STOPWORDS and len(t) >= min_length
    }


def ngrams(seq: list[str], n: int) -> list[tuple[str, ...]]:
    return [tuple(seq[i : i + n]) for i in range(len(seq) - n + 1)]
