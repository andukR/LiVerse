"""Shared live ASR reference pipeline for desktop and Android LiVerse."""

from __future__ import annotations

import re
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any

from bible_parser_core.book_aliases import book_synonyms
from bible_parser_core.parser import DEFAULT_BIBLE, NUMBER_WORDS, ParsedReference, parse_live_reference
from bible_parser_core.reference_resolver import (
    resolve_best_reference_candidate,
    resolve_reference_candidates,
)


REFERENCE_WORDS = {
    "апостол",
    "богослова",
    "глава",
    "главы",
    "главе",
    "до",
    "евангелие",
    "из",
    "книга",
    "книги",
    "от",
    "откровение",
    "откройте",
    "откроем",
    "послание",
    "послания",
    "пророк",
    "пророка",
    "псалом",
    "с",
    "стих",
    "стиха",
    "стихи",
    "стихов",
    "там",
    "же",
    "конец",
    "конца",
    "читаем",
}
VOSK_SMALL_RU_MISSING_WORDS = {
    "авакум",
    "авдия",
    "авдя",
    "аггея",
    "агей",
    "адия",
    "бытиев",
    "бытья",
    "восемнадцатые",
    "восьмидесятая",
    "восьмые",
    "девятнадцатые",
    "девяностая",
    "диания",
    "дияни",
    "ёны",
    "эмии",
    "езекиля",
    "еклесиаста",
    "есфири",
    "иана",
    "ианна",
    "иезекиля",
    "иоиль",
    "иоиля",
    "иоля",
    "иранно",
    "иссаии",
    "иссайи",
    "иуд",
    "калася",
    "каласянам",
    "колоссянам",
    "колосянам",
    "кохелет",
    "малахии",
    "моса",
    "немии",
    "немия",
    "неемии",
    "неемия",
    "ниемии",
    "одиннадцатые",
    "оиля",
    "парапоменон",
    "римлиным",
    "семнадцатые",
    "софонии",
    "софония",
    "сотого",
    "тринадцатые",
    "фесалоникийцам",
    "фессалоникийцам",
    "филимону",
    "филипийцам",
    "филиппийцам",
    "цартвтретья",
    "четвертая",
    "четвертого",
    "четвертое",
    "четвертой",
    "четвертую",
    "четвертые",
    "четвертый",
    "четырнадцатые",
    "шестнадцатые",
}


class VoskTextBuffer:
    def __init__(self, max_parts: int = 3) -> None:
        self.parts: deque[str] = deque(maxlen=max(1, max_parts))

    def add(self, text: str) -> None:
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            self.parts.append(text)

    def candidates(self) -> list[str]:
        values = list(self.parts)
        candidates: list[str] = []
        for size in range(1, len(values) + 1):
            candidate = " ".join(values[-size:]).strip()
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        return candidates


def usable_grammar_phrase(phrase: str) -> bool:
    if not phrase or re.search(r"\d", phrase):
        return False
    return not any(token in VOSK_SMALL_RU_MISSING_WORDS for token in phrase.split())


def build_grammar() -> list[str]:
    phrases: set[str] = set()

    def add_phrase(phrase: str) -> None:
        phrase = phrase.lower()
        if usable_grammar_phrase(phrase):
            phrases.add(phrase)

    for canonical, aliases in book_synonyms.items():
        add_phrase(canonical)
        for alias in aliases:
            add_phrase(alias)
    for word in REFERENCE_WORDS:
        add_phrase(word)
    for word in NUMBER_WORDS:
        add_phrase(word)
    phrases.add("[unk]")
    return sorted(phrases)


def grammar_diagnostics(grammar: list[str]) -> dict[str, Any]:
    return {
        "size": len(grammar),
        "contains": {
            "ефесянам": "ефесянам" in grammar,
            "бытие": "бытие" in grammar,
            "псалом": "псалом" in grammar,
            "двадцать": "двадцать" in grammar,
            "четыре": "четыре" in grammar,
            "седьмой": "седьмой" in grammar,
        },
        "filtered_missing_words_count": len(VOSK_SMALL_RU_MISSING_WORDS),
    }


def normalize_book_form(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()


def strip_leading_book_number(text: str) -> str:
    return re.sub(
        r"^(?:[1-4]|перв\w*|втор\w*|трет\w*|четверт\w*)\s+",
        "",
        text,
    ).strip()


def build_book_only_forms() -> set[str]:
    forms: set[str] = {
        "паралипоминон",
        "паралипомином",
    }
    for canonical, aliases in book_synonyms.items():
        for name in (canonical, *aliases):
            normalized_name = normalize_book_form(name)
            if normalized_name:
                forms.add(normalized_name)
            without_number = strip_leading_book_number(normalized_name)
            if without_number and without_number != normalized_name:
                forms.add(without_number)
    return forms


BOOK_ONLY_FORMS = build_book_only_forms()


def resolve_reference_payload(text: str, bible_path: Path = DEFAULT_BIBLE, *, show_candidates: bool = False) -> dict:
    parsed = parse_live_reference(text, bible_path=bible_path)
    source = "parser"
    resolved = None
    if parsed is None:
        resolved = resolve_best_reference_candidate(text, bible_path=bible_path)
        if resolved:
            parsed = parse_live_reference(resolved.ref, bible_path=bible_path)
            source = "resolver"

    payload = {
        "text": text,
        "source": source if parsed else None,
        "resolved": asdict(resolved) if resolved else None,
        "parsed": asdict(parsed) if parsed else None,
        "matched": parsed is not None,
        "bible_path": str(bible_path),
    }
    if show_candidates:
        payload["candidates"] = [
            asdict(candidate)
            for candidate in resolve_reference_candidates(text, bible_path=bible_path)
        ]
    return payload


def low_confidence_jeremiah(asr_result: dict | None, *, threshold: float = 0.76) -> bool:
    if not asr_result:
        return False
    for item in asr_result.get("result") or []:
        word = str(item.get("word") or "").lower()
        if not re.fullmatch(r"иереми[яи]", word):
            continue
        try:
            confidence = float(item.get("conf"))
        except (TypeError, ValueError):
            continue
        if confidence <= threshold:
            return True
    return False


def nehemiah_confusable_text(
    text: str,
    bible_path: Path = DEFAULT_BIBLE,
    *,
    asr_result: dict | None = None,
) -> str | None:
    if not re.search(r"\bиереми[яи]\b", text, flags=re.IGNORECASE):
        return None
    if re.search(r"\bпророк[а-я]*\s+иереми[яи]\b", text, flags=re.IGNORECASE):
        return None

    replacement = re.sub(r"\bиеремии\b", "неемии", text, flags=re.IGNORECASE)
    replacement = re.sub(r"\bиеремия\b", "неемия", replacement, flags=re.IGNORECASE)
    if replacement == text:
        return None

    nehemiah = parse_live_reference(replacement, bible_path=bible_path)
    if not nehemiah or nehemiah.book != "Неемия":
        return None

    original = parse_live_reference(text, bible_path=bible_path)
    if original is None or low_confidence_jeremiah(asr_result):
        return replacement
    return None


def expand_nehemiah_confusable_candidates(
    candidates: list[str],
    bible_path: Path = DEFAULT_BIBLE,
    *,
    asr_result: dict | None = None,
) -> list[str]:
    expanded: list[str] = []
    for candidate in candidates:
        replacement = nehemiah_confusable_text(candidate, bible_path=bible_path, asr_result=asr_result)
        if replacement and replacement not in expanded:
            expanded.append(replacement)
        if candidate not in expanded:
            expanded.append(candidate)
    return expanded


def joel_confusable_texts(text: str) -> list[str]:
    replacements: list[str] = []
    patterns = (
        (r"\b((?:книга|книги)\s+пророка\s+)ион[аы]\s+иереми[яи]\b", r"\1иоиля", True),
        (r"\b(пророка\s+)ион[аы]\s+иереми[яи]\b", r"\1иоиля", True),
        (r"\b((?:книга|книги)\s+пророка\s+)иов\b", r"\1иоиля", True),
        (r"\b(пророка\s+)иов\b", r"\1иоиля", True),
        (r"\b((?:книга|книги)\s+пророка\s+)иеремии\b", r"\1иоиля", False),
        (r"\b(пророка\s+)иеремии\b", r"\1иоиля", False),
    )
    for pattern, replacement, prefer_replacement in patterns:
        candidate = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        if candidate == text or candidate in replacements:
            continue
        if prefer_replacement:
            replacements.insert(0, candidate)
        else:
            replacements.append(candidate)
    return replacements


def expand_joel_confusable_candidates(candidates: list[str]) -> list[str]:
    expanded: list[str] = []
    for candidate in candidates:
        replacements = joel_confusable_texts(candidate)
        prefer_first = bool(
            re.search(r"\bпророк[а-я]*\s+иов\b", candidate, flags=re.IGNORECASE)
            or re.search(r"\bпророк[а-я]*\s+ион[аы]\s+иереми[яи]\b", candidate, flags=re.IGNORECASE)
        )
        if prefer_first:
            for replacement in replacements:
                if replacement not in expanded:
                    expanded.append(replacement)
        if candidate not in expanded:
            expanded.append(candidate)
        if not prefer_first:
            for replacement in replacements:
                if replacement not in expanded:
                    expanded.append(replacement)
    return expanded


def likely_explicit_reference(text: str) -> bool:
    lowered = text.lower().replace("ё", "е")
    if not re.search(r"\b(глава|стих|псалом)\b", lowered):
        return False
    for canonical, aliases in book_synonyms.items():
        names = [canonical, *aliases]
        for name in names:
            normalized_name = name.lower().replace("ё", "е")
            if normalized_name and re.search(rf"\b{re.escape(normalized_name)}\b", lowered):
                return True
    return False


def likely_book_only_fragment(text: str) -> bool:
    lowered = text.lower().replace("ё", "е").strip()
    if not lowered or re.search(r"\b(глава|стих|псалом)\b", lowered):
        return False
    if re.fullmatch(r"(?:книг[аи]\s+)?пророка\s+\S+", lowered):
        return True
    words = lowered.split()
    if len(words) > 4:
        return False
    forms = {lowered}
    if len(words) == 1 and lowered.endswith("а") and len(lowered) > 3:
        forms.add(lowered[:-1])
    return any(form in BOOK_ONLY_FORMS for form in forms)


def same_place_only_fragment(text: str) -> bool:
    return re.fullmatch(r"\s*там\s+же\s*", text.lower().replace("ё", "е")) is not None


def same_place_candidates(candidates: list[str], last_parsed: dict | ParsedReference | None) -> list[str]:
    if isinstance(last_parsed, ParsedReference):
        book = last_parsed.book
        chapter = last_parsed.chapter
    elif isinstance(last_parsed, dict):
        book = last_parsed.get("book")
        chapter = last_parsed.get("chapter")
    else:
        book = None
        chapter = None
    if not book or not chapter:
        return candidates

    expanded: list[str] = []
    for candidate in candidates:
        expanded.append(candidate)
        if not re.search(r"\bтам\s+же\b", candidate.lower().replace("ё", "е")):
            continue
        suffix = re.sub(r"\bтам\s+же\b", "", candidate, flags=re.IGNORECASE).strip()
        if suffix:
            expanded.append(f"{book} {chapter} глава {suffix}")
    return expanded


def parsed_payload_from_candidates(
    candidates: list[str],
    bible_path: Path = DEFAULT_BIBLE,
    *,
    last_parsed: dict | ParsedReference | None = None,
    show_candidates: bool = False,
) -> dict:
    attempts = [
        resolve_reference_payload(candidate, bible_path=bible_path, show_candidates=show_candidates)
        for candidate in candidates
    ]
    attempt_summaries = [
        {
            "text": attempt.get("text"),
            "ref": (attempt.get("parsed") or {}).get("ref"),
            "source": attempt.get("source"),
            "matched": bool(attempt.get("matched")),
        }
        for attempt in attempts
    ]
    for index, payload in enumerate(attempts):
        if payload.get("matched"):
            first_text = str(attempts[0].get("text") or "") if attempts else ""
            parsed = payload.get("parsed") or {}
            previous_ref = (
                last_parsed.ref
                if isinstance(last_parsed, ParsedReference)
                else (last_parsed or {}).get("ref")
            )
            if index > 0 and previous_ref and parsed.get("ref") == previous_ref:
                first_payload = attempts[0]
                first_payload["attempts"] = attempt_summaries[1:]
                first_payload["blocked_stale_repeat"] = True
                return first_payload
            if index > 0 and (
                likely_explicit_reference(first_text)
                or likely_book_only_fragment(first_text)
                or same_place_only_fragment(first_text)
            ):
                first_payload = attempts[0]
                first_payload["attempts"] = attempt_summaries[1:]
                first_payload["blocked_stale_context"] = True
                return first_payload
            payload["attempts"] = [
                summary
                for summary_index, summary in enumerate(attempt_summaries)
                if summary_index != index
            ]
            return payload
    payload = attempts[0] if attempts else resolve_reference_payload("", bible_path=bible_path, show_candidates=show_candidates)
    payload["attempts"] = attempt_summaries[1:] if len(attempt_summaries) > 1 else []
    return payload


class LiveReferencePipeline:
    def __init__(self, bible_path: Path = DEFAULT_BIBLE, *, buffer_parts: int = 3) -> None:
        self.bible_path = bible_path
        self.text_buffer = VoskTextBuffer(buffer_parts)
        self.last_parsed: dict | None = None

    def process_text(self, text: str, *, asr_result: dict | None = None, show_candidates: bool = False) -> dict:
        text = re.sub(r"\s+", " ", (text or "")).strip()
        if not text:
            return resolve_reference_payload("", bible_path=self.bible_path, show_candidates=show_candidates)

        self.text_buffer.add(text)
        if likely_book_only_fragment(text) and len(self.text_buffer.parts) > 1:
            self.text_buffer.parts.clear()
            self.text_buffer.add(text)
        candidate_texts = same_place_candidates(self.text_buffer.candidates(), self.last_parsed)
        candidate_texts = expand_joel_confusable_candidates(candidate_texts)
        candidate_texts = expand_nehemiah_confusable_candidates(
            candidate_texts,
            bible_path=self.bible_path,
            asr_result=asr_result,
        )
        payload = parsed_payload_from_candidates(
            candidate_texts,
            bible_path=self.bible_path,
            last_parsed=self.last_parsed,
            show_candidates=show_candidates,
        )
        payload["vosk_text"] = text
        payload["vosk_buffer"] = list(self.text_buffer.parts)
        payload["candidate_texts"] = candidate_texts
        if payload.get("parsed"):
            self.last_parsed = payload["parsed"]
        return payload
