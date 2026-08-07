"""Long-lived SQLite search for Bible verses spoken as text.

This is the importable counterpart of the experimental ``search_bible.py``
script.  It deliberately returns scores for comparing candidates, not
probabilities.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


TOKEN_RE = re.compile(r"[а-яё]+(?:-[а-яё]+)*|\d+", re.IGNORECASE)

try:
    from rapidfuzz.fuzz import ratio as fuzzy_ratio
    from rapidfuzz.fuzz import token_set_ratio
except ImportError:  # pragma: no cover - only used before normal installation
    from difflib import SequenceMatcher

    def fuzzy_ratio(left: str, right: str) -> float:
        return 100.0 * SequenceMatcher(None, left, right).ratio()

    def token_set_ratio(left: str, right: str) -> float:
        left_words, right_words = set(left.split()), set(right.split())
        if not left_words or not right_words:
            return 0.0
        common = " ".join(sorted(left_words & right_words))
        return max(
            fuzzy_ratio(common, " ".join(sorted(left_words))),
            fuzzy_ratio(common, " ".join(sorted(right_words))),
            fuzzy_ratio(" ".join(sorted(left_words)), " ".join(sorted(right_words))),
        )


@dataclass(frozen=True)
class BibleTextSearchResult:
    reference: str
    text: str
    score: float
    coverage: float
    ordered_similarity: float
    token_similarity: float
    bigram_overlap: float
    trigram_overlap: float
    matched_lemmas: tuple[str, ...]
    book_id: int = 0
    chapter: int = 0
    start_verse: int = 0
    end_verse: int = 0


def normalize_bible_text(text: str) -> list[str]:
    return TOKEN_RE.findall((text or "").lower().replace("ё", "е"))


def _ngrams(tokens: Sequence[str], size: int) -> set[tuple[str, ...]]:
    return {tuple(tokens[index : index + size]) for index in range(len(tokens) - size + 1)}


class BibleTextSearcher:
    """Search a prebuilt ``bible_index.db`` without reopening it per query."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.is_file():
            raise FileNotFoundError(f"Индекс библейского текста не найден: {self.db_path}")
        try:
            import pymorphy3
        except ImportError as exc:
            raise RuntimeError(
                "Для текстового поиска установите pymorphy3 и pymorphy3-dicts-ru."
            ) from exc
        self._morph = pymorphy3.MorphAnalyzer()
        self._lemma_cache: dict[str, str] = {}
        self._connection = sqlite3.connect(str(self.db_path))
        self._connection.row_factory = sqlite3.Row
        self._total_documents = int(
            self._connection.execute("SELECT COUNT(*) FROM verses").fetchone()[0]
        )
        if self._total_documents <= 0:
            self.close()
            raise ValueError(f"Индекс не содержит стихов: {self.db_path}")

    def close(self) -> None:
        if getattr(self, "_connection", None) is not None:
            self._connection.close()
            self._connection = None  # type: ignore[assignment]

    def __enter__(self) -> "BibleTextSearcher":
        return self

    def __exit__(self, *_unused: object) -> None:
        self.close()

    def _lemma(self, token: str) -> str:
        cached = self._lemma_cache.get(token)
        if cached is not None:
            return cached
        if token.isdigit():
            lemma = token
        else:
            parses = self._morph.parse(token)
            lemma = parses[0].normal_form if parses else token
        self._lemma_cache[token] = lemma
        return lemma

    def _candidate_ids(self, lemmas: list[str], limit: int) -> tuple[list[int], dict[str, int]]:
        unique = list(dict.fromkeys(lemmas))
        if not unique:
            return [], {}
        placeholders = ",".join("?" for _ in unique)
        rows = self._connection.execute(
            f"SELECT lemma, COUNT(*) FROM lemma_index WHERE lemma IN ({placeholders}) GROUP BY lemma",
            unique,
        ).fetchall()
        frequencies = {str(row[0]): int(row[1]) for row in rows}
        weights = {
            lemma: math.log((self._total_documents + 1) / (frequencies.get(lemma, 0) + 1)) + 1.0
            for lemma in unique
        }
        rows = self._connection.execute(
            f"SELECT verse_id, lemma FROM lemma_index WHERE lemma IN ({placeholders})",
            unique,
        ).fetchall()
        score_by_id: Counter[int] = Counter()
        count_by_id: Counter[int] = Counter()
        for verse_id, lemma in rows:
            score_by_id[int(verse_id)] += weights[str(lemma)]
            count_by_id[int(verse_id)] += 1
        ids = sorted(
            score_by_id,
            key=lambda verse_id: (count_by_id[verse_id], score_by_id[verse_id]),
            reverse=True,
        )[:limit]
        return ids, frequencies

    def search(
        self,
        text: str,
        *,
        limit: int = 5,
        candidate_limit: int = 100,
        min_score: float = 0.0,
        max_range_verses: int = 2,
    ) -> tuple[list[str], list[BibleTextSearchResult]]:
        """Return query lemmas and ranked candidates for one spoken window."""
        tokens = normalize_bible_text(text)
        lemmas = [self._lemma(token) for token in tokens]
        candidate_ids, frequencies = self._candidate_ids(lemmas, candidate_limit)
        if not candidate_ids:
            return lemmas, []
        expanded_ids = set(candidate_ids)
        if max_range_verses >= 2:
            for verse_id in candidate_ids:
                expanded_ids.update((verse_id - 1, verse_id + 1))
        selected_ids = sorted(verse_id for verse_id in expanded_ids if verse_id > 0)
        placeholders = ",".join("?" for _ in selected_ids)
        rows = self._connection.execute(
            f"SELECT id, reference, text, lemma_text, book_id, chapter, verse "
            f"FROM verses WHERE id IN ({placeholders})",
            selected_ids,
        ).fetchall()
        rows_by_id = {int(row["id"]): row for row in rows}
        results = [
            self._score_candidate(lemmas, rows_by_id[verse_id], frequencies)
            for verse_id in candidate_ids
            if verse_id in rows_by_id
        ]
        if max_range_verses >= 2:
            range_starts = set(candidate_ids) | {verse_id - 1 for verse_id in candidate_ids}
            for verse_id in sorted(range_starts):
                first = rows_by_id.get(verse_id)
                second = rows_by_id.get(verse_id + 1)
                if first is None or second is None:
                    continue
                if (
                    int(first["book_id"]) != int(second["book_id"])
                    or int(first["chapter"]) != int(second["chapter"])
                    or int(second["verse"]) != int(first["verse"]) + 1
                ):
                    continue
                first_lemmas = str(first["lemma_text"]).split()
                second_lemmas = str(second["lemma_text"]).split()
                if (
                    not first_lemmas
                    or not second_lemmas
                    or (first_lemmas[-1], second_lemmas[0]) not in _ngrams(lemmas, 2)
                ):
                    continue
                reference_prefix = str(first["reference"]).rsplit(":", 1)[0]
                combined = {
                    "reference": (
                        f"{reference_prefix}:{int(first['verse'])}-{int(second['verse'])}"
                    ),
                    "text": f"{str(first['text']).strip()} {str(second['text']).strip()}",
                    "lemma_text": (
                        f"{str(first['lemma_text']).strip()} {str(second['lemma_text']).strip()}"
                    ),
                    "book_id": int(first["book_id"]),
                    "chapter": int(first["chapter"]),
                    "verse": int(first["verse"]),
                    "end_verse": int(second["verse"]),
                }
                results.append(self._score_candidate(lemmas, combined, frequencies))
        results = [item for item in results if item.score >= min_score]
        results.sort(key=lambda item: item.score, reverse=True)
        return lemmas, results[:limit]

    def _score_candidate(
        self,
        query: list[str],
        row: sqlite3.Row | dict,
        frequencies: dict[str, int],
    ) -> BibleTextSearchResult:
        verse = str(row["lemma_text"]).split()
        query_counts, verse_counts = Counter(query), Counter(verse)
        numerator = denominator = 0.0
        matched: list[str] = []
        for lemma, query_count in query_counts.items():
            weight = math.log((self._total_documents + 1) / (frequencies.get(lemma, 0) + 1)) + 1.0
            denominator += weight * query_count
            count = min(query_count, verse_counts.get(lemma, 0))
            numerator += weight * count
            matched.extend([lemma] * count)
        coverage = numerator / denominator if denominator else 0.0
        query_text, verse_text = " ".join(query), " ".join(verse)
        ordered = fuzzy_ratio(query_text, verse_text) / 100.0
        token_similarity = token_set_ratio(query_text, verse_text) / 100.0
        query_bigrams, verse_bigrams = _ngrams(query, 2), _ngrams(verse, 2)
        bigram_overlap = len(query_bigrams & verse_bigrams) / len(query_bigrams) if query_bigrams else 0.0
        query_trigrams, verse_trigrams = _ngrams(query, 3), _ngrams(verse, 3)
        trigram_overlap = (
            len(query_trigrams & verse_trigrams) / len(query_trigrams)
            if query_trigrams
            else 0.0
        )
        substring_bonus = 1.0 if query_text and query_text in verse_text else 0.0
        score = 100.0 * (
            0.48 * coverage + 0.18 * token_similarity + 0.17 * ordered
            + 0.12 * bigram_overlap + 0.05 * substring_bonus
        )
        return BibleTextSearchResult(
            reference=str(row["reference"]), text=str(row["text"]), score=score,
            coverage=100.0 * coverage, ordered_similarity=100.0 * ordered,
            token_similarity=100.0 * token_similarity, bigram_overlap=100.0 * bigram_overlap,
            trigram_overlap=100.0 * trigram_overlap,
            matched_lemmas=tuple(matched),
            book_id=int(row["book_id"]), chapter=int(row["chapter"]),
            start_verse=int(row["verse"]), end_verse=int(row.get("end_verse", row["verse"]))
            if isinstance(row, dict) else int(row["verse"]),
        )
