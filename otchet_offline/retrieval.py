"""Offline lexical retrieval for sanitized professional passages.

Scores are TF-IDF cosine similarities. They can order review suggestions;
they are not evidence that a requirement has been met.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from typing import NamedTuple


TOKEN = re.compile(r"\.?[^\W_]+(?:\.[^\W_]+)*(?:\+\+|#)?")
STOP_WORDS = frozenset(
    "а без был была было быть в во для до и из или к ко как мы на над не о об "
    "от по под при про с со у что это я of the in to for and or with by on a "
    "an as is at from be based work experience requirements required "
    "опыт работы работа опытный навыки навык знания знание владение "
    "умение уверенное наличие требуется".split()
)


class PassageMatch(NamedTuple):
    index: int
    score: float


def _term_counts(text: str) -> Counter[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    return Counter(
        token for token in TOKEN.findall(normalized)
        if token not in STOP_WORDS and not token.isdecimal()
    )


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    return min(1.0, math.fsum(weight * right.get(term, 0.0) for term, weight in left.items()))


class LexicalIndex:
    """Index zero or more applications, each split into ordered sanitized passages."""

    def __init__(self, documents: Sequence[Sequence[str]]) -> None:
        application_counts = []
        passage_counts = []
        frequency: Counter[str] = Counter()
        for passages in documents:
            counts_for_passages = tuple(_term_counts(passage) for passage in passages)
            counts_for_application: Counter[str] = Counter()
            for counts in counts_for_passages:
                counts_for_application.update(counts)
            frequency.update(counts_for_application.keys())
            application_counts.append(counts_for_application)
            passage_counts.append(counts_for_passages)

        corpus_size = len(application_counts)
        self._idf = {
            term: 1.0 + math.log((1 + corpus_size) / (1 + document_frequency))
            for term, document_frequency in frequency.items()
        }
        self._unseen_idf = 1.0 + math.log1p(corpus_size)
        self._applications = tuple(self._vector(counts) for counts in application_counts)
        self._passages = tuple(
            tuple(self._vector(counts) for counts in application)
            for application in passage_counts
        )

    def _vector(self, counts: Counter[str]) -> dict[str, float]:
        weighted = {
            term: (1.0 + math.log(count)) * self._idf.get(term, self._unseen_idf)
            for term, count in counts.items()
        }
        norm = math.sqrt(math.fsum(weight * weight for weight in weighted.values()))
        return {term: weight / norm for term, weight in weighted.items()} if norm else {}

    def _check_application_index(self, application_idx: int) -> None:
        if not 0 <= application_idx < len(self._applications):
            raise IndexError("Application index out of range.")

    def score_application(self, application_idx: int, requirements: str | Sequence[str]) -> float:
        """Compare one application to vacancy requirements in the shared corpus."""
        self._check_application_index(application_idx)
        text = requirements if isinstance(requirements, str) else " ".join(requirements)
        query = self._vector(_term_counts(text))
        return _cosine(query, self._applications[application_idx])

    def search(self, application_idx: int, query: str, limit: int = 3) -> tuple[PassageMatch, ...]:
        """Return matching passage indices in descending score and then input order."""
        self._check_application_index(application_idx)
        if limit <= 0:
            return ()
        query_vector = self._vector(_term_counts(query))
        matches = (
            PassageMatch(index, _cosine(query_vector, passage))
            for index, passage in enumerate(self._passages[application_idx])
        )
        ranked = sorted((match for match in matches if match.score > 0), key=lambda match: (-match.score, match.index))
        return tuple(ranked[:limit])
