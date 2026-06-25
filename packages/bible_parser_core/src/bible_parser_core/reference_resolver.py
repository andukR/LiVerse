"""Candidate resolver for noisy live Bible references."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from bible_parser_core.parser import (
    DEFAULT_BIBLE,
    BookCandidate,
    RefCandidate,
    bible_map,
    book_candidate_specificity_bonus,
    book_candidates,
    compact_range,
    normalize_text,
    ref_candidates,
)


GOSPEL_BOOKS = {"Матфей", "Марк", "Лука", "Иоанн"}
JOHN_EPISTLES = {"1 Иоанна", "2 Иоанна", "3 Иоанна"}
EPISTLE_BOOKS = {
    "Римлянам",
    "1 Коринфянам",
    "2 Коринфянам",
    "Галатам",
    "Ефесянам",
    "Филиппийцам",
    "Колоссянам",
    "1 Фессалоникийцам",
    "2 Фессалоникийцам",
    "1 Тимофею",
    "2 Тимофею",
    "Титу",
    "Филимону",
    "Евреям",
    "Иаков",
    "1 Петра",
    "2 Петра",
    "1 Иоанна",
    "2 Иоанна",
    "3 Иоанна",
    "Иуда",
}
PROPHET_BOOKS = {
    "Исаия",
    "Иеремия",
    "Иезекииль",
    "Даниил",
    "Осия",
    "Иоиль",
    "Амос",
    "Авдий",
    "Иона",
    "Михей",
    "Наум",
    "Аввакум",
    "Софония",
    "Аггей",
    "Захария",
    "Малахия",
}
JOHN_CONFUSABLE_BOOKS = (
    "Иоанн",
    "Иона",
    "1 Иоанна",
    "2 Иоанна",
    "3 Иоанна",
)
BROAD_YANA_CONFUSABLE_BOOKS = (
    *JOHN_CONFUSABLE_BOOKS,
    "Римлянам",
    "Ефесянам",
    "1 Коринфянам",
    "2 Коринфянам",
)


@dataclass(frozen=True)
class ResolvedReferenceCandidate:
    ref: str
    book: str
    chapter: int
    start_verse: int
    end_verse: int
    score: float
    book_text: str
    source: str
    reasons: tuple[str, ...]
    end_chapter: int | None = None


def _synthetic_book_candidate(book: str, normalized: str, source: str) -> BookCandidate:
    match = re.search(r"\b(яна|иоанн[а-я]*|иона|римлян[а-я]*|ефесян[а-я]*|коринфян[а-я]*)\b", normalized)
    start = match.start() if match else 0
    end = match.end() if match else min(len(normalized), 1)
    score = 0.62 if match else 0.45
    return BookCandidate(book=book, score=score, start=start, end=end, text=source)


def _expanded_book_candidates(normalized: str) -> list[BookCandidate]:
    candidates = list(book_candidates(normalized))
    seen = {(candidate.book, candidate.start, candidate.end, candidate.text) for candidate in candidates}

    should_expand_john = bool(
        re.search(r"\b(яна|иоанн[а-я]*|иона)\b", normalized)
        or any(candidate.book in {"Иоанн", "Иона"} | JOHN_EPISTLES for candidate in candidates)
    )
    if should_expand_john:
        books = BROAD_YANA_CONFUSABLE_BOOKS if re.search(r"\bяна\b", normalized) else JOHN_CONFUSABLE_BOOKS
        for book in books:
            candidate = _synthetic_book_candidate(book, normalized, "john_confusable")
            key = (candidate.book, candidate.start, candidate.end, candidate.text)
            if key not in seen:
                candidates.append(candidate)
                seen.add(key)

    return sorted(candidates, key=lambda item: (-item.score, item.start, item.end, item.book))


def _context_score(book: str, normalized: str, reasons: list[str]) -> float:
    score = 0.0
    has_epistle_hint = bool(re.search(r"\bпослани[ея]\b", normalized))
    has_gospel_hint = bool(re.search(r"\bевангелие\b|\bот\s+(?:матфея|марка|луки|иоанна)\b", normalized))
    has_prophet_hint = bool(re.search(r"\bпророк[а-я]*\b", normalized))

    if has_epistle_hint:
        if book in EPISTLE_BOOKS:
            score += 35.0
            reasons.append("epistle_hint")
        elif book in GOSPEL_BOOKS:
            score -= 30.0
            reasons.append("epistle_hint_penalty")
    if has_gospel_hint:
        if book in GOSPEL_BOOKS:
            score += 35.0
            reasons.append("gospel_hint")
        elif book in EPISTLE_BOOKS:
            score -= 30.0
            reasons.append("gospel_hint_penalty")
    if has_prophet_hint:
        if book in PROPHET_BOOKS:
            score += 25.0
            reasons.append("prophet_hint")
        else:
            score -= 10.0
            reasons.append("prophet_hint_penalty")

    if re.search(r"\bяна\b", normalized):
        if book in JOHN_EPISTLES:
            score += 24.0
            reasons.append("yana_epistle_hint")
        elif book == "Иоанн":
            score += 10.0
            reasons.append("yana_gospel_hint")
        elif book in {"Римлянам", "Ефесянам", "1 Коринфянам", "2 Коринфянам"}:
            score -= 12.0
            reasons.append("yana_confusable_penalty")
    return score


def _reference_text(book: str, ref_candidate: RefCandidate) -> str | None:
    if ref_candidate.end_chapter is not None and ref_candidate.end_verse is not None:
        if not ref_candidate.verses:
            return None
        return f"{book} {ref_candidate.chapter}:{ref_candidate.verses[0]}-{ref_candidate.end_chapter}:{ref_candidate.end_verse}"
    verse_range = compact_range(ref_candidate.verses)
    if verse_range is None:
        return None
    start_verse, end_verse = verse_range
    if start_verse == end_verse:
        return f"{book} {ref_candidate.chapter}:{start_verse}"
    return f"{book} {ref_candidate.chapter}:{start_verse}-{end_verse}"


def _candidate_score(
    book_candidate: BookCandidate,
    ref_candidate: RefCandidate,
    all_books: list[BookCandidate],
    normalized: str,
) -> tuple[float, tuple[str, ...]]:
    reasons: list[str] = ["valid_chapter_verses"]
    book_center = (book_candidate.start + book_candidate.end) / 2
    ref_center = (ref_candidate.start + ref_candidate.end) / 2
    distance = abs(book_center - ref_center)
    proximity = 1 / (1 + distance / 35)
    score = (book_candidate.score * 75.0) + (ref_candidate.score * 110.0) + (proximity * 25.0)
    score += book_candidate_specificity_bonus(book_candidate, all_books) * 20.0
    score += _context_score(book_candidate.book, normalized, reasons)
    if book_candidate.end <= ref_candidate.start:
        score += 8.0
        reasons.append("book_before_numbers")
    is_cross_chapter = ref_candidate.end_chapter is not None
    if (is_cross_chapter or len(ref_candidate.verses) > 1) and re.search(r"\bпо\b|-|\bс\b", normalized):
        score += 16.0
        reasons.append("range_signal")
    elif len(ref_candidate.verses) == 1 and re.search(r"\bпо\b|-|\bс\b", normalized):
        score -= 6.0
        reasons.append("single_verse_with_range_signal")

    numbers = [int(match.group(0)) for match in re.finditer(r"\b\d+\b", normalized)]
    if len(numbers) >= 2:
        first, second = numbers[0], numbers[1]
        if ref_candidate.chapter == first and ref_candidate.verses[0] == second:
            score += 16.0
            reasons.append("spoken_number_order")
        elif ref_candidate.chapter == second and ref_candidate.verses[0] == first:
            score -= 10.0
            reasons.append("reversed_number_order_penalty")
    return score, tuple(reasons)


def _contains_candidate(candidate: ResolvedReferenceCandidate, other: ResolvedReferenceCandidate) -> bool:
    return (
        candidate.book == other.book
        and candidate.chapter == other.chapter
        and candidate.start_verse <= other.start_verse
        and candidate.end_verse >= other.end_verse
        and (candidate.start_verse, candidate.end_verse) != (other.start_verse, other.end_verse)
    )


def _adjust_iona_confusion(
    candidates: dict[str, ResolvedReferenceCandidate],
    normalized: str,
) -> dict[str, ResolvedReferenceCandidate]:
    if not re.search(r"\bиона\b", normalized):
        return candidates
    if any(candidate.book == "Иона" for candidate in candidates.values()):
        return candidates

    adjusted: dict[str, ResolvedReferenceCandidate] = {}
    for ref, candidate in candidates.items():
        bonus = 0.0
        reason = ""
        if candidate.book == "Иоанн":
            bonus = 30.0
            reason = "iona_invalid_john_hint"
        elif candidate.book in JOHN_EPISTLES:
            bonus = 6.0
            reason = "iona_invalid_john_epistle_hint"
        elif candidate.book in {"Римлянам", "Ефесянам", "1 Коринфянам", "2 Коринфянам"}:
            bonus = -14.0
            reason = "iona_invalid_confusable_penalty"

        if not reason:
            adjusted[ref] = candidate
            continue
        adjusted[ref] = ResolvedReferenceCandidate(
            ref=candidate.ref,
            book=candidate.book,
            chapter=candidate.chapter,
            start_verse=candidate.start_verse,
            end_verse=candidate.end_verse,
            score=round(candidate.score + bonus, 3),
            book_text=candidate.book_text,
            source=candidate.source,
            reasons=(*candidate.reasons, reason),
        )
    return adjusted


def resolve_reference_candidates(
    text: str,
    bible_path: Path = DEFAULT_BIBLE,
    *,
    limit: int = 5,
) -> list[ResolvedReferenceCandidate]:
    normalized = normalize_text(text)
    if not normalized:
        return []

    bible = bible_map(bible_path)
    books = _expanded_book_candidates(normalized)
    best_by_ref: dict[str, ResolvedReferenceCandidate] = {}

    for book_candidate in books:
        for ref_candidate in ref_candidates(normalized, book_candidate.book, bible):
            ref = _reference_text(book_candidate.book, ref_candidate)
            if not ref:
                continue
            score, reasons = _candidate_score(book_candidate, ref_candidate, books, normalized)
            if ref_candidate.end_chapter is not None and ref_candidate.end_verse is not None:
                if not ref_candidate.verses:
                    continue
                start_verse = ref_candidate.verses[0]
                end_verse = ref_candidate.end_verse
            else:
                verse_range = compact_range(ref_candidate.verses)
                if verse_range is None:
                    continue
                start_verse, end_verse = verse_range
            candidate = ResolvedReferenceCandidate(
                ref=ref,
                book=book_candidate.book,
                chapter=ref_candidate.chapter,
                start_verse=start_verse,
                end_verse=end_verse,
                score=round(score, 3),
                book_text=book_candidate.text,
                source="resolver",
                reasons=reasons,
                end_chapter=ref_candidate.end_chapter,
            )
            current = best_by_ref.get(ref)
            if current is None or candidate.score > current.score:
                best_by_ref[ref] = candidate

    best_by_ref = _adjust_iona_confusion(best_by_ref, normalized)
    return sorted(best_by_ref.values(), key=lambda item: (-item.score, item.ref))[:limit]


def resolve_best_reference_candidate(
    text: str,
    bible_path: Path = DEFAULT_BIBLE,
    *,
    min_margin: float = 18.0,
) -> ResolvedReferenceCandidate | None:
    candidates = resolve_reference_candidates(text, bible_path=bible_path, limit=5)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    best = candidates[0]
    competitors = [candidate for candidate in candidates[1:] if not _contains_candidate(best, candidate)]
    if not competitors:
        return best
    return best if best.score - competitors[0].score >= min_margin else None
