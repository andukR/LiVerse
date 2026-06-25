"""Fuzzy Bible verse text search.

This module keeps the quote-text lookup reusable outside the interactive
training annotator.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path


CANONICAL_BOOK_NAMES_BY_ID = {
    1: "Бытие",
    2: "Исход",
    3: "Левит",
    4: "Числа",
    5: "Второзаконие",
    6: "Иисус Навин",
    7: "Судьи",
    8: "Руфь",
    9: "1 Царств",
    10: "2 Царств",
    11: "3 Царств",
    12: "4 Царств",
    13: "1 Паралипоменон",
    14: "2 Паралипоменон",
    15: "Ездра",
    16: "Неемия",
    17: "Есфирь",
    18: "Иов",
    19: "Псалтирь",
    20: "Притчи",
    21: "Екклесиаст",
    22: "Песня Песней",
    23: "Исаия",
    24: "Иеремия",
    25: "Плач Иеремии",
    26: "Иезекииль",
    27: "Даниил",
    28: "Осия",
    29: "Иоиль",
    30: "Амос",
    31: "Авдий",
    32: "Иона",
    33: "Михей",
    34: "Наум",
    35: "Аввакум",
    36: "Софония",
    37: "Аггей",
    38: "Захария",
    39: "Малахия",
    40: "Матфей",
    41: "Марк",
    42: "Лука",
    43: "Иоанн",
    44: "Деяния",
    45: "Римлянам",
    46: "1 Коринфянам",
    47: "2 Коринфянам",
    48: "Галатам",
    49: "Ефесянам",
    50: "Филиппийцам",
    51: "Колоссянам",
    52: "1 Фессалоникийцам",
    53: "2 Фессалоникийцам",
    54: "1 Тимофею",
    55: "2 Тимофею",
    56: "Титу",
    57: "Филимону",
    58: "Евреям",
    59: "Иаков",
    60: "1 Петра",
    61: "2 Петра",
    62: "1 Иоанна",
    63: "2 Иоанна",
    64: "3 Иоанна",
    65: "Иуда",
    66: "Откровение",
}


@dataclass(frozen=True)
class QuoteHit:
    ref: str
    score: float
    text: str


_BIBLE_INDEX_CACHE: dict[tuple[str, int], list[dict]] = {}


def normalize_quote_text(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[^а-яa-z0-9]+", " ", text)
    text = re.sub(r"\bстрах\s+господи\b", "страх господень", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_bible_book_name(book: dict) -> str:
    book_id = book.get("BookId")
    if isinstance(book_id, int) and book_id in CANONICAL_BOOK_NAMES_BY_ID:
        return CANONICAL_BOOK_NAMES_BY_ID[book_id]
    return book.get("BookName") or str(book_id or "")


def load_bible_index(path: Path, max_window: int) -> list[dict]:
    cache_key = (str(path.resolve()), max_window)
    if cache_key in _BIBLE_INDEX_CACHE:
        return _BIBLE_INDEX_CACHE[cache_key]

    data = json.loads(path.read_text(encoding="utf-8"))
    items: list[dict] = []
    for book in data.get("Books", []):
        book_name = canonical_bible_book_name(book)
        for chapter in book.get("Chapters", []):
            chapter_id = chapter.get("ChapterId")
            verses = chapter.get("Verses", [])
            for start_index, verse in enumerate(verses):
                window_texts: list[str] = []
                for end_index in range(start_index, min(len(verses), start_index + max_window)):
                    window_texts.append(verses[end_index].get("Text", ""))
                    start_verse = verse.get("VerseId")
                    end_verse = verses[end_index].get("VerseId")
                    if start_verse == end_verse:
                        ref = f"{book_name} {chapter_id}:{start_verse}"
                    else:
                        ref = f"{book_name} {chapter_id}:{start_verse}-{end_verse}"
                    text = " ".join(window_texts).strip()
                    norm = normalize_quote_text(text)
                    if norm:
                        items.append({"ref": ref, "text": text, "norm": norm})

    _BIBLE_INDEX_CACHE[cache_key] = items
    return items


def score_quote_match(query: str, candidate: str) -> float:
    def prefix_span_score() -> float:
        query_tokens = query.split()
        candidate_tokens = candidate.split()
        if len(query_tokens) < 4 or len(candidate_tokens) < 4:
            return 0.0
        best = 0.0
        max_width = min(8, len(candidate_tokens), len(query_tokens))
        for width in range(4, max_width + 1):
            candidate_prefix = " ".join(candidate_tokens[:width])
            for start in range(0, len(query_tokens) - width + 1):
                query_span = " ".join(query_tokens[start : start + width])
                best = max(best, SequenceMatcher(None, query_span, candidate_prefix).ratio() * 100)
        return best

    try:
        from rapidfuzz import fuzz

        if len(candidate) <= len(query):
            partial = fuzz.partial_ratio(candidate, query)
        else:
            partial = fuzz.partial_ratio(query, candidate)
        token = fuzz.token_set_ratio(query, candidate)
        ratio = fuzz.ratio(query, candidate)
        return max(prefix_span_score(), partial * 0.55 + token * 0.20 + ratio * 0.25)
    except Exception:
        return max(prefix_span_score(), SequenceMatcher(None, query, candidate).ratio() * 100)


def search_quote(fragment: str, bible_path: Path, top: int, max_window: int) -> list[QuoteHit]:
    query = normalize_quote_text(fragment)
    if len(query) < 5:
        return []

    index = load_bible_index(bible_path, max_window=max_window)
    scored: list[QuoteHit] = []
    for item in index:
        if query == item["norm"]:
            scored.append(QuoteHit(ref=item["ref"], score=110.0, text=item["text"]))
            continue
        if query in item["norm"]:
            scored.append(QuoteHit(ref=item["ref"], score=100.0, text=item["text"]))
            continue
        if len(query) < 20 and item["norm"] not in query:
            continue
        score = score_quote_match(query, item["norm"])
        threshold = 80 if len(query) < 20 else 45
        if score >= threshold:
            scored.append(QuoteHit(ref=item["ref"], score=score, text=item["text"]))
    scored.sort(key=lambda item: (-item.score, len(normalize_quote_text(item.text))))
    return scored[:top]
