"""Shared live ASR reference pipeline for desktop and Android LiVerse."""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any

from bible_parser_core.book_aliases import book_synonyms
from bible_parser_core.parser import DEFAULT_BIBLE, NUMBER_WORDS, ParsedReference, normalize_text, parse_live_reference
from bible_parser_core.parser import diagnose_invalid_reference
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
    "по",
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
VOSK_GRAMMAR_EXTRA_WORDS = {
    "четвёртая",
    "четвёртого",
    "четвёртое",
    "четвёртой",
    "четвёртую",
    "четвёртые",
    "четвёртый",
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

    def clear(self) -> None:
        self.parts.clear()

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
    for word in VOSK_GRAMMAR_EXTRA_WORDS:
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
            "четвёртого": "четвёртого" in grammar,
            "по": "по" in grammar,
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
EXPLICIT_GOSPEL_FORMS = {
    form for form in BOOK_ONLY_FORMS if form.startswith("евангелие от ")
}


def incomplete_reference_prefix(prefix: str) -> bool:
    normalized = normalize_book_form(prefix)
    return bool(
        re.search(
            r"(?:^|\s)(?:[1-4]\s+|перв\w*\s+|втор\w*\s+|трет\w*\s+|четверт\w*\s+)?послани[ея]\s+к\s*$",
            normalized,
        )
    )


def explicit_book_reference_text(text: str) -> bool:
    normalized = normalize_book_form(text)
    if not re.search(r"\b(?:глава|главы|главе|стих|стиха|стихи|стихов|псалом)\b", normalized):
        return False
    return any(re.search(rf"\b{re.escape(form)}\b", normalized) for form in BOOK_ONLY_FORMS)


def command_suffix_reference(text: str, bible_path: Path = DEFAULT_BIBLE) -> ParsedReference | None:
    normalized = normalize_book_form(text)
    for match in re.finditer(r"\b(?:читаем|прочитаем|откройте|откроем|открываем)\b", normalized):
        prefix = normalized[: match.start()].strip()
        suffix = normalized[match.start() :].strip()
        if not prefix or not suffix:
            continue
        if not incomplete_reference_prefix(prefix):
            continue
        if not explicit_book_reference_text(suffix):
            continue
        parsed = parse_live_reference(suffix, bible_path=bible_path)
        if parsed:
            return parsed
    return None


def missing_twenty_range_reference(
    text: str,
    parsed: ParsedReference,
    bible_path: Path = DEFAULT_BIBLE,
) -> ParsedReference | None:
    normalized = normalize_text(text)
    for match in re.finditer(r"\b(1[1-9]|20)\s+([1-9])\s+стих\b", normalized):
        start_verse = int(match.group(1))
        end_verse = 20 + int(match.group(2))
        if start_verse != parsed.start_verse or end_verse <= start_verse:
            continue
        repaired_text = f"{normalized[:match.start()]}{start_verse} {end_verse} стих{normalized[match.end():]}"
        repaired = parse_live_reference(repaired_text, bible_path=bible_path)
        if not repaired:
            continue
        if (
            repaired.book == parsed.book
            and repaired.start_verse == start_verse
            and repaired.end_verse == end_verse
        ):
            return repaired
    return None


def resolve_reference_payload(text: str, bible_path: Path = DEFAULT_BIBLE, *, show_candidates: bool = False) -> dict:
    parsed = parse_live_reference(text, bible_path=bible_path)
    source = "parser"
    suffix_parsed = command_suffix_reference(text, bible_path=bible_path) if parsed else None
    if suffix_parsed and suffix_parsed.ref != parsed.ref:
        parsed = suffix_parsed
        source = "parser_suffix"
    missing_twenty_parsed = missing_twenty_range_reference(text, parsed, bible_path=bible_path) if parsed else None
    if missing_twenty_parsed and missing_twenty_parsed.ref != parsed.ref:
        parsed = missing_twenty_parsed
        source = "parser_missing_twenty_range"
    resolved = None
    invalid_reference = None
    if parsed is None:
        resolved = resolve_best_reference_candidate(text, bible_path=bible_path)
        if resolved:
            parsed = parse_live_reference(resolved.ref, bible_path=bible_path)
            source = "resolver"
        if parsed is None:
            invalid_reference = diagnose_invalid_reference(text, bible_path=bible_path)

    payload = {
        "text": text,
        "source": source if parsed else None,
        "resolved": asdict(resolved) if resolved else None,
        "parsed": asdict(parsed) if parsed else None,
        "invalid_reference": asdict(invalid_reference) if invalid_reference else None,
        "message": invalid_reference.message if invalid_reference else None,
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


def open_range_start_fragment(text: str) -> bool:
    lowered = text.lower().replace("ё", "е")
    return bool(re.search(r"\bс\s+\S+", lowered)) and not bool(re.search(r"\bпо\b", lowered))


def context_prefix(candidate: str, current_text: str) -> str:
    candidate = re.sub(r"\s+", " ", candidate).strip()
    current_text = re.sub(r"\s+", " ", current_text).strip()
    if not current_text or not candidate.endswith(current_text):
        return ""
    return candidate[: -len(current_text)].strip()


def explicit_reference_context(prefix: str) -> bool:
    normalized_prefix = normalize_book_form(prefix)
    if not normalized_prefix:
        return False
    words = normalized_prefix.split()
    if len(words) > 8:
        return False
    if not re.search(
        r"\b(?:читаем|откроем|откройте|книг[аи]?|евангелие|послани[ея]|пророка|глава|главы)\b",
        normalized_prefix,
    ):
        return False
    book_hits = [
        form
        for form in BOOK_ONLY_FORMS
        if form and re.search(rf"\b{re.escape(form)}\b", normalized_prefix)
    ]
    return 1 <= len(book_hits) <= 2


def explicit_reference_suffix(text: str) -> bool:
    normalized = normalize_book_form(text)
    return bool(re.search(r"\b(?:глава|главы|стих|стиха|стихи|стихов|с|по)\b", normalized))


def acceptable_buffer_context(candidate: str, current_text: str) -> bool:
    prefix = context_prefix(candidate, current_text)
    if not prefix:
        return False
    if likely_book_only_fragment(prefix):
        return True
    if explicit_reference_context(prefix) and explicit_reference_suffix(current_text):
        return True
    normalized_prefix = normalize_book_form(prefix)
    return any(re.search(rf"\b{re.escape(form)}\b", normalized_prefix) for form in EXPLICIT_GOSPEL_FORMS)


def has_reference_marker(text: str) -> bool:
    normalized = normalize_book_form(text)
    return bool(
        re.search(
            r"\b(?:глава|главы|главе|стих|стиха|стихи|стихов|книга|книги|евангелие|послани[ея]|пророка|псалом)\b",
            normalized,
        )
    )


def should_block_matched_payload(payload: dict) -> str | None:
    parsed = payload.get("parsed") or {}
    if not parsed:
        return None

    text = str(payload.get("text") or "")
    normalized = normalize_text(text)
    words = normalized.split()
    ref = str(parsed.get("ref") or "")
    source = str(payload.get("source") or "")

    if (
        re.search(r"\bевангелие\s+от\b", normalized)
        and not re.search(r"\b(?:матфея|марка|луки|иоанна)\b", normalized)
    ):
        return "gospel_without_book_name"

    if (
        re.search(r"\b(?:книга\s+)?пророка\s+\S+\s+\d+\s+глава\b", normalized)
        and not re.search(r"\bстих", normalized)
    ):
        return "prophet_book_chapter_without_verse"

    if (
        ref in {"1 Тимофею", "2 Тимофею"} or re.match(r"^[12]\s+Тимофею\b", ref)
    ) and re.search(r"\bтимофе[яю]\b", normalized):
        if not re.search(r"\b[12]\s+тимофе[яю]\b", normalized):
            return "ambiguous_numbered_timothy"

    if (
        source == "resolver"
        and len(words) <= 3
        and not has_reference_marker(text)
        and re.search(r"\bяна\b", normalized)
    ):
        return "weak_short_yana_context"

    if (
        re.search(r"\bunk\s+\d+\s+стих\w*\s+\d+\s+глав", normalized)
        and re.search(r"\bкниг[аи]\b", normalized)
    ):
        return "unknown_prefix_before_reversed_verse"

    if re.fullmatch(r"\d+\s+\d+\s+моисея", normalized):
        return "weak_short_moses_context"

    if (
        re.search(r"\b[1234]\s+царств\b", ref, flags=re.IGNORECASE)
        and re.search(r"\bс\b|\bпо\b", normalized)
        and not re.search(r"\bглава|главы|главе\b", normalized)
        and re.search(r"\b[1234]\s+книги\s+царств\b", normalized)
    ):
        return "numbered_kingdoms_range_without_chapter"

    if (
        re.search(r"\bглава\s*$", normalized)
        and "стих" not in normalized
        and ref == "Иисус Навин 14:14"
    ):
        return "joshua_chapter_suffix_without_verse"

    return None


def blocked_payload(payload: dict, reason: str) -> dict:
    result = dict(payload)
    result["matched"] = False
    result["parsed"] = None
    result["source"] = None
    result["blocked_weak_context"] = reason
    return result


def score_reference_risk(payload: dict, asr_result: dict | None = None) -> dict:
    text = str(payload.get("text") or "")
    vosk_text = str(payload.get("vosk_text") or "")
    normalized = normalize_text(f"{text} {vosk_text}")
    words = asr_result.get("result") if isinstance(asr_result, dict) else []
    word_items = [item for item in words if isinstance(item, dict)]
    reasons: list[str] = []
    score = 0.0
    metrics: dict[str, Any] = {}

    unknown_count = len(re.findall(r"\bunk\b", normalized))
    if unknown_count:
        score += min(0.4, 0.25 * unknown_count)
        reasons.append("contains_unk")
    metrics["unknown_words"] = unknown_count

    confidences: list[float] = []
    starts: list[float] = []
    ends: list[float] = []
    for item in word_items:
        try:
            confidences.append(float(item.get("conf")))
        except (TypeError, ValueError):
            pass
        try:
            starts.append(float(item.get("start")))
            ends.append(float(item.get("end")))
        except (TypeError, ValueError):
            pass

    if confidences:
        min_confidence = min(confidences)
        avg_confidence = sum(confidences) / len(confidences)
        metrics["min_confidence"] = round(min_confidence, 3)
        metrics["avg_confidence"] = round(avg_confidence, 3)
        if min_confidence < 0.65:
            score += 0.25
            reasons.append("low_word_confidence")
        if avg_confidence < 0.8:
            score += 0.15
            reasons.append("low_average_confidence")
    elif payload.get("parsed"):
        score += 0.15
        reasons.append("missing_word_confidence")

    if starts and ends and max(ends) > min(starts):
        duration = max(ends) - min(starts)
        words_per_second = len(word_items) / duration
        metrics["speech_duration_seconds"] = round(duration, 3)
        metrics["words_per_second"] = round(words_per_second, 3)
        if words_per_second >= 4.8:
            score += 0.3
            reasons.append("very_fast_speech")
        elif words_per_second >= 3.6:
            score += 0.15
            reasons.append("fast_speech")

    if len(payload.get("vosk_buffer") or []) > 1 and text and vosk_text and text != vosk_text:
        score += 0.1
        reasons.append("assembled_from_buffer")

    if re.search(r"\b(?:данила|яков|яна)\b", normalized):
        score += 0.15
        reasons.append("confusable_book_form")

    if payload.get("source") == "resolver":
        score += 0.15
        reasons.append("resolved_by_fuzzy_match")
    if payload.get("source") == "parser_missing_twenty_range":
        score += 0.2
        reasons.append("missing_twenty_range_repair")
    if payload.get("blocked_weak_context"):
        score += 0.2
        reasons.append("blocked_weak_context")

    score = min(1.0, round(score, 3))
    if score >= 0.6:
        level = "high"
    elif score >= 0.3:
        level = "medium"
    else:
        level = "low"

    return {
        "score": score,
        "level": level,
        "reasons": reasons,
        "metrics": metrics,
    }


def add_risk_score(payload: dict, asr_result: dict | None = None) -> dict:
    risk = score_reference_risk(payload, asr_result=asr_result)
    payload["risk"] = risk
    payload["risk_score"] = risk["score"]
    payload["risk_level"] = risk["level"]
    payload["risk_reasons"] = risk["reasons"]
    return payload


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
            weak_context_reason = should_block_matched_payload(payload)
            if weak_context_reason:
                first_payload = attempts[0]
                first_payload["attempts"] = attempt_summaries[1:]
                return blocked_payload(first_payload, weak_context_reason)
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
            if index > 0 and not acceptable_buffer_context(str(payload.get("text") or ""), first_text):
                first_payload = attempts[0]
                first_payload["attempts"] = attempt_summaries[1:]
                first_payload["blocked_no_book_context"] = True
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
    def __init__(
        self,
        bible_path: Path = DEFAULT_BIBLE,
        *,
        buffer_parts: int = 5,
        buffer_window_ms: int = 2000,
    ) -> None:
        self.bible_path = bible_path
        self.text_buffer = VoskTextBuffer(buffer_parts)
        self.buffer_window_ms = max(0, buffer_window_ms)
        self.last_text_ms: int | None = None
        self.last_parsed: dict | None = None

    def process_text(
        self,
        text: str,
        *,
        asr_result: dict | None = None,
        show_candidates: bool = False,
        now_ms: int | float | None = None,
    ) -> dict:
        text = re.sub(r"\s+", " ", (text or "")).strip()
        if not text:
            return resolve_reference_payload("", bible_path=self.bible_path, show_candidates=show_candidates)

        current_ms = int(now_ms if now_ms is not None else time.monotonic() * 1000)
        delta_ms = None if self.last_text_ms is None else current_ms - self.last_text_ms
        buffer_reset_by_gap = False
        if delta_ms is not None and delta_ms > self.buffer_window_ms:
            self.text_buffer.clear()
            buffer_reset_by_gap = True
        self.last_text_ms = current_ms

        self.text_buffer.add(text)
        if likely_book_only_fragment(text) and len(self.text_buffer.parts) > 1:
            self.text_buffer.clear()
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
        payload["delta_ms"] = delta_ms
        payload["buffer_window_ms"] = self.buffer_window_ms
        payload["buffer_reset_by_gap"] = buffer_reset_by_gap
        add_risk_score(payload, asr_result=asr_result)
        if payload.get("parsed"):
            self.last_parsed = payload["parsed"]
            if open_range_start_fragment(str(payload.get("text") or "")):
                payload["buffer_kept_for_open_range"] = True
            else:
                self.text_buffer.clear()
                payload["buffer_cleared_after_match"] = True
        return payload
