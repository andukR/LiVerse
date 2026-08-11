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

    def __init__(self, db_path: Path, *, use_database_lemmas: bool = False) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.is_file():
            raise FileNotFoundError(f"Индекс библейского текста не найден: {self.db_path}")
        self._connection = sqlite3.connect(str(self.db_path))
        self._connection.row_factory = sqlite3.Row
        self._use_database_lemmas = bool(use_database_lemmas)
        self._morph = None
        if self._use_database_lemmas:
            try:
                self._connection.execute("SELECT wordform, lemma FROM tokens LIMIT 1").fetchone()
            except sqlite3.DatabaseError as exc:
                self.close()
                raise ValueError("Индекс не содержит словаря форм слов: таблица tokens.") from exc
        else:
            try:
                import pymorphy3
            except ImportError as exc:
                self.close()
                raise RuntimeError(
                    "Для текстового поиска установите pymorphy3 и pymorphy3-dicts-ru."
                ) from exc
            self._morph = pymorphy3.MorphAnalyzer()
        self._lemma_cache: dict[str, str] = {}
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
        elif self._use_database_lemmas:
            row = self._connection.execute(
                "SELECT lemma, COUNT(*) AS c FROM tokens "
                "WHERE wordform=? GROUP BY lemma ORDER BY c DESC LIMIT 1",
                (token,),
            ).fetchone()
            lemma = str(row[0]) if row is not None else token
        else:
            assert self._morph is not None
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
        max_range_verses: int = 3,
    ) -> tuple[list[str], list[BibleTextSearchResult]]:
        """Return query lemmas and ranked candidates for one spoken window."""
        tokens = normalize_bible_text(text)
        lemmas = [self._lemma(token) for token in tokens]
        candidate_ids, frequencies = self._candidate_ids(lemmas, candidate_limit)
        if not candidate_ids:
            return lemmas, []
        max_range_verses = max(1, int(max_range_verses))
        expanded_ids = set(candidate_ids)
        if max_range_verses >= 2:
            radius = max_range_verses - 1
            for verse_id in candidate_ids:
                expanded_ids.update(range(verse_id - radius, verse_id + radius + 1))
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
            range_starts = {
                verse_id - offset
                for verse_id in candidate_ids
                for offset in range(max_range_verses)
            }
            query_bigrams = _ngrams(lemmas, 2)
            for verse_id in sorted(range_starts):
                first = rows_by_id.get(verse_id)
                if first is None:
                    continue
                rows_in_range = [first]
                for offset in range(1, max_range_verses):
                    previous = rows_in_range[-1]
                    current = rows_by_id.get(verse_id + offset)
                    if current is None or (
                        int(first["book_id"]) != int(current["book_id"])
                        or int(first["chapter"]) != int(current["chapter"])
                        or int(current["verse"]) != int(previous["verse"]) + 1
                    ):
                        break
                    previous_lemmas = str(previous["lemma_text"]).split()
                    current_lemmas = str(current["lemma_text"]).split()
                    if (
                        not previous_lemmas
                        or not current_lemmas
                        or (previous_lemmas[-1], current_lemmas[0]) not in query_bigrams
                    ):
                        break
                    rows_in_range.append(current)
                    last = rows_in_range[-1]
                    reference_prefix = str(first["reference"]).rsplit(":", 1)[0]
                    combined = {
                        "reference": (
                            f"{reference_prefix}:{int(first['verse'])}-{int(last['verse'])}"
                        ),
                        "text": " ".join(str(row["text"]).strip() for row in rows_in_range),
                        "lemma_text": " ".join(
                            str(row["lemma_text"]).strip() for row in rows_in_range
                        ),
                        "book_id": int(first["book_id"]),
                        "chapter": int(first["chapter"]),
                        "verse": int(first["verse"]),
                        "end_verse": int(last["verse"]),
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
