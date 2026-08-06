"""Shared live ASR reference pipeline for desktop and Android LiVerse."""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from bible_parser_core.book_aliases import book_synonyms
from bible_parser_core.parser import DEFAULT_BIBLE, NUMBER_WORDS, ParsedReference, book_candidates, normalize_text, parse_live_reference
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
    "следующая",
    "следующей",
    "следующую",
    "следующие",
    "следующим",
    "следующий",
    "следующем",
    "следующего",
    "слова",
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
CONFUSABLE_NUMBER_PAIRS = ((17, 18), (13, 30), (12, 13), (12, 19), (7, 8))
CONFUSABLE_NUMBER_MAP = {
    value: other
    for left, right in CONFUSABLE_NUMBER_PAIRS
    for value, other in ((left, right), (right, left))
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
BLOCKED_CONTEXT_MESSAGES = {
    "ambiguous_unnumbered_thessalonians": (
        "Номер книги не был назван или не был распознан программой. "
        "Введите номер послания Фессалоникийцам вручную."
    ),
    "ambiguous_numbered_timothy": (
        "Номер книги не был назван или не был распознан программой. "
        "Введите номер послания Тимофею вручную."
    ),
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


def normalize_sermon_plan_text(text: str) -> str:
    """Normalize a prepared sermon-plan slide and recognized speech alike."""
    without_item_number = re.sub(r"^\s*\d+\s*[.)]\s*", "", text or "")
    return normalize_text(without_item_number)


def sermon_plan_grammar_phrases(
    slides: list[dict[str, Any]],
    word_is_known: Callable[[str], bool] | None = None,
) -> list[str]:
    """Build extra Vosk phrases from non-empty sermon-plan slides."""
    phrases: set[str] = set()
    for slide in slides:
        for line in str(slide.get("text") or "").splitlines():
            without_item_number = re.sub(r"^\s*\d+\s*[.)]\s*", "", line)
            normalized = re.sub(r"[^а-яёa-z]+", " ", without_item_number.lower()).strip()
            words = [
                word
                for word in normalized.split()
                if len(word) > 1 and (word_is_known is None or word_is_known(word))
            ]
            if not words:
                continue
            phrases.update(words)
            phrases.add(" ".join(words))
    return sorted(phrases)


def sermon_plan_match_targets(text: str) -> list[str]:
    """Return plan text variants, excluding standalone Bible-reference lines."""
    targets: list[str] = []
    for line in (text or "").splitlines():
        if re.search(r"\d+\s*:\s*\d+", line):
            continue
        normalized = normalize_sermon_plan_text(line)
        if normalized and normalized not in targets:
            targets.append(normalized)
    return targets


def match_sermon_plan_slide(
    slides: list[dict[str, Any]],
    candidates: list[str],
    *,
    current_index: int = 0,
    lookahead: int = 2,
    threshold: float = 0.68,
) -> dict[str, Any] | None:
    """Match speech only against the current and nearest upcoming slides."""
    if not slides or not candidates:
        return None

    start = max(0, current_index)
    end = min(len(slides), start + max(0, lookahead) + 1)
    slide_indexes = list(range(start, end))
    nonempty_indexes = [
        index
        for index, slide in enumerate(slides)
        if sermon_plan_match_targets(str(slide.get("text") or ""))
    ]
    last_nonempty_index = nonempty_indexes[-1] if nonempty_indexes else -1
    if start >= last_nonempty_index > 0 and 0 not in slide_indexes:
        slide_indexes.append(0)
    best: dict[str, Any] | None = None
    for slide_index in slide_indexes:
        slide = slides[slide_index]
        for target in sermon_plan_match_targets(str(slide.get("text") or "")):
            target_words = target.split()
            target_set = set(target_words)
            if len(target_words) < 3:
                continue

            for raw_candidate in candidates:
                candidate = normalize_sermon_plan_text(raw_candidate)
                candidate_words = candidate.split()
                if len(candidate_words) < 3:
                    continue
                candidate_set = set(candidate_words)
                common = target_set & candidate_set
                target_coverage = len(common) / max(1, len(target_set))
                candidate_coverage = len(common) / max(1, len(candidate_set))
                sequence_score = SequenceMatcher(None, candidate, target).ratio()
                score = (0.55 * sequence_score) + (0.30 * target_coverage) + (0.15 * candidate_coverage)
                if target_coverage < 0.55 or score < threshold:
                    continue
                result = {
                    "slide_index": slide_index,
                    "slide_number": slide_index + 1,
                    "text": str(slide.get("text") or ""),
                    "matched_text": target,
                    "candidate": raw_candidate,
                    "score": round(score, 3),
                }
                if best is None or score > float(best["score"]):
                    best = result
    return best


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
    if parsed.end_chapter is not None:
        return None
    normalized = normalize_text(text)
    for match in re.finditer(r"\b(1[1-9]|[2-9]\d)\s+([1-9])(?:\s+стих)?\b", normalized):
        start_verse = int(match.group(1))
        end_digit = int(match.group(2))
        end_verse = (20 if start_verse <= 20 else (start_verse // 10) * 10) + end_digit
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


def colos_chapter_range_reference(
    text: str,
    parsed: ParsedReference,
    bible_path: Path = DEFAULT_BIBLE,
) -> ParsedReference | None:
    if parsed.end_chapter is not None:
        return None
    normalized = normalize_text(text)
    for match in re.finditer(r"\b(\d+)\s+колос\s+(\d+)\s+50\s+стих\b", normalized):
        chapter = int(match.group(1))
        start_verse = int(match.group(2))
        if chapter != parsed.chapter or start_verse != parsed.start_verse:
            continue
        repaired = parse_live_reference(f"{parsed.book} {chapter}:{start_verse}-10", bible_path=bible_path)
        if repaired and repaired.book == parsed.book and repaired.chapter == chapter:
            return repaired
    return None


def repeated_confusable_range_reference(
    text: str,
    parsed: ParsedReference,
    bible_path: Path = DEFAULT_BIBLE,
) -> ParsedReference | None:
    if parsed.start_verse != parsed.end_verse or parsed.start_verse not in {17, 18}:
        return None
    normalized = normalize_text(text)
    if not re.search(r"\bглава\b.*\b(17|18)\b\s+\1\s+стих\b", normalized):
        return None
    return parse_live_reference(f"{parsed.book} {parsed.chapter}:17-18", bible_path=bible_path)


def repeated_range_end_reference(
    text: str,
    parsed: ParsedReference,
    bible_path: Path = DEFAULT_BIBLE,
) -> ParsedReference | None:
    if parsed.start_verse != parsed.end_verse:
        return None
    normalized = normalize_text(text)
    for match in re.finditer(r"\b(\d+)\s+глава\s+(\d+)\s+(\d+)\s+\3\s+стих\b", normalized):
        chapter = int(match.group(1))
        start_verse = int(match.group(2))
        end_verse = int(match.group(3))
        if chapter != parsed.chapter or parsed.start_verse != end_verse:
            continue
        if start_verse >= end_verse:
            continue
        return parse_live_reference(f"{parsed.book} {chapter}:{start_verse}-{end_verse}", bible_path=bible_path)
    return None


def ambiguous_reference_alternatives(
    parsed: ParsedReference,
    source_text: str = "",
    bible_path: Path = DEFAULT_BIBLE,
) -> list[dict[str, Any]]:
    alternatives: list[dict[str, Any]] = []
    seen = {parsed.ref}

    def add(ref: str) -> None:
        candidate = parse_live_reference(ref, bible_path=bible_path)
        if candidate is None or candidate.ref in seen:
            return
        seen.add(candidate.ref)
        alternatives.append(asdict(candidate))

    chapter_alt = CONFUSABLE_NUMBER_MAP.get(parsed.chapter)
    if chapter_alt is not None and parsed.end_chapter is None:
        if parsed.start_verse == parsed.end_verse:
            add(f"{parsed.book} {chapter_alt}:{parsed.start_verse}")
        else:
            add(f"{parsed.book} {chapter_alt}:{parsed.start_verse}-{parsed.end_verse}")

    if parsed.end_chapter is None:
        start_alt = CONFUSABLE_NUMBER_MAP.get(parsed.start_verse)
        end_alt = CONFUSABLE_NUMBER_MAP.get(parsed.end_verse)
        if parsed.start_verse == parsed.end_verse:
            if start_alt is not None:
                add(f"{parsed.book} {parsed.chapter}:{start_alt}")
        else:
            if start_alt is not None and start_alt <= parsed.end_verse:
                add(f"{parsed.book} {parsed.chapter}:{start_alt}-{parsed.end_verse}")
            if end_alt is not None and parsed.start_verse <= end_alt:
                add(f"{parsed.book} {parsed.chapter}:{parsed.start_verse}-{end_alt}")
            if start_alt is not None and end_alt is not None and start_alt <= end_alt:
                add(f"{parsed.book} {parsed.chapter}:{start_alt}-{end_alt}")

    normalized = normalize_text(source_text)
    if (
        parsed.book in {"1 Коринфянам", "2 Коринфянам"}
        and re.search(r"\bпослани[ея]\s+коринфянам\b", normalized)
        and not re.search(r"\b(?:1|2|перв\w*|втор\w*)\s+(?:послани[ея]\s+)?коринфянам\b", normalized)
    ):
        if parsed.start_verse == parsed.end_verse:
            add(f"Колоссянам {parsed.chapter}:{parsed.start_verse}")
        else:
            add(f"Колоссянам {parsed.chapter}:{parsed.start_verse}-{parsed.end_verse}")

    if parsed.book == "Ефесянам":
        if parsed.start_verse == parsed.end_verse:
            add(f"Колоссянам {parsed.chapter}:{parsed.start_verse}")
        else:
            add(f"Колоссянам {parsed.chapter}:{parsed.start_verse}-{parsed.end_verse}")

    if (
        parsed.book == "Филиппийцам"
        and parsed.chapter == 1
        and re.search(r"\bпослани[ея]\s+фес+\b", normalized)
        and not re.search(r"\b(?:1|2|перв\w*|втор\w*)\s+(?:послани[ея]\s+)?фес+\b", normalized)
    ):
        if parsed.start_verse == parsed.end_verse:
            add(f"Филимону {parsed.start_verse}")
        else:
            add(f"Филимону {parsed.start_verse}-{parsed.end_verse}")

    return alternatives


def compact_reference_list(text: str, bible_path: Path = DEFAULT_BIBLE) -> list[dict[str, str]]:
    normalized = normalize_text(text)
    items: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_item(parsed: ParsedReference | None, segment: str, *, required_prefix: str | None = None) -> None:
        if not parsed:
            return
        if required_prefix and not parsed.ref.startswith(required_prefix):
            return
        if parsed.ref in seen:
            return
        seen.add(parsed.ref)
        items.append({"ref": parsed.ref, "source_text": segment})

    if normalized.count("псалом") >= 2:
        parts = re.split(r"\bпсалом\b", normalized)
        for part in parts[1:]:
            segment = f"псалом {part.strip()}".strip()
            if not re.search(r"\bстих\b", segment):
                continue
            add_item(parse_live_reference(segment, bible_path=bible_path), segment, required_prefix="Псалтирь ")
        if len(items) >= 2:
            return items

    all_books = book_candidates(normalized)
    exact_books = sorted(
        (
            candidate
            for candidate in all_books
            if candidate.score >= 0.999
            and not any(
                other is not candidate
                and other.score >= 0.999
                and other.start <= candidate.start
                and other.end >= candidate.end
                and (other.end - other.start) > (candidate.end - candidate.start)
                for other in all_books
            )
        ),
        key=lambda candidate: candidate.start,
    )
    if len(exact_books) < 2:
        return []

    items = []
    seen = set()
    for index, candidate in enumerate(exact_books):
        end = exact_books[index + 1].start if index + 1 < len(exact_books) else len(normalized)
        segment = normalized[candidate.start:end].strip()
        number_count = len(re.findall(r"\b\d+\b", segment))
        if number_count < 2 and not (candidate.book == "Псалтирь" and re.search(r"\bпсал\w*\b", segment)):
            continue
        parsed = parse_live_reference(segment, bible_path=bible_path)
        if not parsed or parsed.book != candidate.book:
            continue
        add_item(parsed, segment)
    return items if len(items) >= 2 else []


def resolve_reference_payload(text: str, bible_path: Path = DEFAULT_BIBLE, *, show_candidates: bool = False) -> dict:
    reference_list = compact_reference_list(text, bible_path=bible_path)
    if reference_list:
        return {
            "text": text,
            "source": "parser_reference_list",
            "resolved": None,
            "parsed": None,
            "reference_list": reference_list,
            "invalid_reference": None,
            "message": None,
            "matched": True,
            "bible_path": str(bible_path),
        }

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
    colos_range_parsed = colos_chapter_range_reference(text, parsed, bible_path=bible_path) if parsed else None
    if colos_range_parsed and colos_range_parsed.ref != parsed.ref:
        parsed = colos_range_parsed
        source = "parser_colos_chapter_range"
    repeated_confusable_parsed = repeated_confusable_range_reference(text, parsed, bible_path=bible_path) if parsed else None
    if repeated_confusable_parsed and repeated_confusable_parsed.ref != parsed.ref:
        parsed = repeated_confusable_parsed
        source = "parser_repeated_confusable_range"
    repeated_range_end_parsed = repeated_range_end_reference(text, parsed, bible_path=bible_path) if parsed else None
    if repeated_range_end_parsed and repeated_range_end_parsed.ref != parsed.ref:
        parsed = repeated_range_end_parsed
        source = "parser_repeated_range_end"
    resolved = None
    invalid_reference = None
    blocked_weak_context = None
    if parsed is not None:
        parsed_invalid_reference = diagnose_invalid_reference(text, bible_path=bible_path)
        if (
            parsed_invalid_reference
            and parsed_invalid_reference.book == parsed.book
            and parsed_invalid_reference.chapter == parsed.chapter
            and (parsed_invalid_reference.start_verse or 0) <= parsed.start_verse
            and (parsed_invalid_reference.end_verse or 0) > parsed.end_verse
        ):
            invalid_reference = parsed_invalid_reference
            parsed = None
    if parsed is None:
        if invalid_reference is None:
            resolved = resolve_best_reference_candidate(text, bible_path=bible_path)
            if resolved:
                blocked_weak_context = resolver_book_conflict_reason(text, resolved.ref)
                if blocked_weak_context:
                    resolved = None
                else:
                    parsed = parse_live_reference(resolved.ref, bible_path=bible_path)
                    source = "resolver"
        if parsed is None and invalid_reference is None:
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
    if blocked_weak_context:
        payload["blocked_weak_context"] = blocked_weak_context
        payload["message"] = BLOCKED_CONTEXT_MESSAGES.get(blocked_weak_context)
    if parsed is not None:
        payload["ambiguous_alternatives"] = ambiguous_reference_alternatives(
            parsed,
            text,
            bible_path=bible_path,
        )
    if show_candidates:
        payload["candidates"] = [
            asdict(candidate)
            for candidate in resolve_reference_candidates(text, bible_path=bible_path)
        ]
    return payload


def resolver_book_conflict_reason(text: str, ref: str) -> str | None:
    normalized = normalize_text(text)
    if re.search(r"\bтимофе[яю]\b", normalized) and not re.match(r"^[12]\s+Тимофею\b", ref):
        return "resolver_conflicts_with_timothy"
    if re.search(r"\bпетра\b", normalized) and not re.match(r"^[12]\s+Петра\b", ref):
        return "resolver_conflicts_with_peter"
    return None


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
    return 1 <= len(book_hits) <= 3


def explicit_reference_suffix(text: str) -> bool:
    normalized = normalize_book_form(text)
    return bool(re.search(r"\b(?:глава|главы|стих|стиха|стихи|стихов|с|по)\b", normalized))


def implicit_range_suffix(text: str) -> bool:
    normalized = normalize_text(text)
    return bool(
        re.search(
            r"\b(?:[1-3][0-9]|[1-9])\s+"
            r"(?:[1-3][0-9]|[1-9])\s+стих\b",
            normalized,
        )
    )


def acceptable_buffer_context(candidate: str, current_text: str) -> bool:
    prefix = context_prefix(candidate, current_text)
    if not prefix:
        return False
    if likely_book_only_fragment(prefix):
        return True
    if explicit_reference_context(prefix) and explicit_reference_suffix(current_text):
        return True
    if explicit_reference_context(prefix) and implicit_range_suffix(current_text):
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
    raw_text = text.lower().replace("ё", "е")
    normalized = normalize_text(text)
    words = normalized.split()
    ref = str(parsed.get("ref") or "")
    source = str(payload.get("source") or "")

    if (
        re.search(r"\bевангелие\s+от\b", normalized)
        and not re.search(r"\b(?:матфея|марка|луки|иоанна)\b", normalized)
    ):
        return "gospel_without_book_name"

    gospel_conflicts = {
        "матфея": "Матфей",
        "марка": "Марк",
        "луки": "Лука",
        "иоанна": "Иоанн",
    }
    for marker, book in gospel_conflicts.items():
        if re.search(rf"\bевангелие\s+от\s+{marker}\b", normalized) and not ref.startswith(book):
            return "gospel_book_conflict"

    thessalonian_form = r"(?:фес+с?\s+салон(?:ик|ики)?(?:\s+царств)?|фессалоникийцам)"
    if (
        re.search(rf"\b{thessalonian_form}\b", normalized)
        and not re.search(rf"\b(?:1|2|перв\w*|втор\w*)\s+(?:послани[ея]\s+)?{thessalonian_form}\b", normalized)
    ):
        return "ambiguous_unnumbered_thessalonians"

    if re.search(r"\bколоссянам\b|\bкол\s+осии\b", normalized) and not ref.startswith("Колоссянам"):
        return "colossians_book_conflict"

    if (
        ref.startswith("Руфь ")
        and re.search(r"\bрусь\b", raw_text)
        and re.search(r"\b2\s+3\s+4\s+5\b", normalized)
        and not re.search(r"\b(?:книга|руфь|руф)\b", raw_text)
    ):
        return "ruth_counting_rhyme"

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
        ref.startswith("Ездра ")
        and re.search(r"\bстих\w*\s+ездры\s*$", raw_text)
        and not re.search(r"\b(?:глав|книг)\w*\b", raw_text)
    ):
        return "weak_trailing_ezra_context"

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

    if (
        parsed.get("start_verse") == 1
        and parsed.get("end_verse") == 1
        and re.search(r"\b(?:первую|первая|первой|первое)\s+стих\b", raw_text)
    ):
        return "suspicious_first_verse_form"

    if (
        parsed.get("start_verse") == 1
        and parsed.get("end_verse") == 1
        and re.search(r"\bглава\b", normalized)
        and not re.search(r"\bстих", normalized)
        and re.search(r"\b(?:первый|первого|первую|первая|первой|первое)\s*$", raw_text)
    ):
        return "incomplete_first_verse_after_chapter"

    if (
        parsed.get("start_verse") == parsed.get("end_verse")
        and re.search(r"\bглав\w*\b", raw_text)
        and re.search(r"\bпо\s*$", normalized)
    ):
        return "incomplete_range_end_after_po"

    if parsed.get("start_verse") == parsed.get("end_verse"):
        clipped_cross_chapter = re.search(
            r"\bглава\s+(?:с\s+)?(\d+)(?:\s+стих)?\s+(?:и\s+)?(?:до|2)\s+(\d+)\s+стих\b",
            normalized,
        )
        if clipped_cross_chapter and int(clipped_cross_chapter.group(2)) < int(clipped_cross_chapter.group(1)):
            return "incomplete_cross_chapter_range_end"

    if (
        parsed.get("start_verse") == parsed.get("end_verse")
        and re.search(r"\bглав\w*\b", raw_text)
        and not re.search(r"\b(?:по|до)\b", normalized)
        and ends_with_genitive_ordinal_verse(raw_text)
    ):
        return "incomplete_range_start_after_chapter"

    if (
        parsed.get("start_verse") == parsed.get("end_verse")
        and re.search(r"\bглав\w*\b", raw_text)
        and not re.search(r"\b(?:стих|стиха|стихи|стихов)\b", raw_text)
        and (
            not re.search(r"\b(?:с|по)\b", normalized)
            or (re.search(r"\bс\b", normalized) and not re.search(r"\bпо\b", normalized))
        )
        and ends_with_genitive_ordinal(raw_text)
    ):
        return "incomplete_range_start_after_chapter"

    if (
        ref == "Числа 1:1"
        and re.search(r"\bчисла\b", normalized)
        and not re.search(r"\bкниг[аи]\b", normalized)
        and not re.search(r"\bглава\b", normalized)
    ):
        return "weak_bare_numbers_first_verse"

    if (
        ref.startswith("Числа ")
        and re.search(r"\bо\s+числа\s*$", normalized)
        and not has_reference_marker(text)
        and len(re.findall(r"\b\d+\b", normalized)) >= 2
    ):
        return "weak_trailing_numbers_context"

    if (
        ref.startswith("Ездра ")
        and re.search(r"\bездр[аы]\b", normalized)
        and re.search(r"\bсот\w*\b", raw_text)
        and not re.search(r"\b(?:книг|глав|стих)\w*\b", raw_text)
    ):
        return "weak_compact_ezra_hundred_context"

    return None


def ends_with_genitive_ordinal(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:"
            r"первого|второго|третьего|четвертого|пятого|шестого|седьмого|восьмого|девятого|"
            r"десятого|одиннадцатого|двенадцатого|тринадцатого|четырнадцатого|пятнадцатого|"
            r"шестнадцатого|семнадцатого|восемнадцатого|девятнадцатого|двадцатого|"
            r"тридцатого|сорокового|пятидесятого|шестидесятого|семидесятого|"
            r"восьмидесятого|девяностого|сотого"
            r")\s*$",
            text,
        )
    )


def ends_with_genitive_ordinal_verse(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:"
            r"первого|второго|третьего|четвертого|пятого|шестого|седьмого|восьмого|девятого|"
            r"десятого|одиннадцатого|двенадцатого|тринадцатого|четырнадцатого|пятнадцатого|"
            r"шестнадцатого|семнадцатого|восемнадцатого|девятнадцатого|двадцатого|"
            r"тридцатого|сорокового|пятидесятого|шестидесятого|семидесятого|"
            r"восьмидесятого|девяностого|сотого"
            r")\s+стих[а]?\s*$",
            text,
        )
    )


def blocked_payload(payload: dict, reason: str) -> dict:
    result = dict(payload)
    result["matched"] = False
    result["parsed"] = None
    result["source"] = None
    result["blocked_weak_context"] = reason
    result["message"] = BLOCKED_CONTEXT_MESSAGES.get(reason)
    return result


def book_fragment_followed_by_verse_without_chapter(payload: dict) -> bool:
    parts = [
        normalize_book_form(str(part or ""))
        for part in (payload.get("vosk_buffer") or [])
        if str(part or "").strip()
    ]
    if len(parts) < 2:
        return False
    for previous, current in zip(parts, parts[1:]):
        if not likely_book_only_fragment(previous):
            continue
        if re.search(r"\b\d+\b", previous):
            continue
        if not re.search(r"\bстих\w*\b", current):
            continue
        if re.search(r"\bглав\w*\b", current):
            continue
        return True
    return False


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
        if book_fragment_followed_by_verse_without_chapter(payload):
            score += 0.25
            reasons.append("book_fragment_without_chapter_marker")

    if re.search(r"\b(?:данила|яков|яна)\b", normalized):
        score += 0.15
        reasons.append("confusable_book_form")

    parsed = payload.get("parsed") or {}
    if (
        parsed
        and parsed.get("start_verse") == parsed.get("end_verse")
        and not re.search(r"\b(?:глава|стих|по|с)\b", normalized)
        and len(re.findall(r"\b\d+\b", normalized)) >= 2
    ):
        score += 0.15
        reasons.append("compact_reference_without_markers")

    if (
        parsed
        and parsed.get("start_verse") == parsed.get("end_verse")
        and re.search(r"\bглав\w*\b", normalized)
        and not re.search(r"\bстих\w*\b", normalized)
        and re.search(r"\b\d+\s*$", normalized)
    ):
        score += 0.15
        reasons.append("bare_verse_number_after_chapter")

    if payload.get("source") == "resolver":
        score += 0.15
        reasons.append("resolved_by_fuzzy_match")
    if payload.get("source") == "parser_missing_twenty_range":
        score += 0.2
        reasons.append("missing_twenty_range_repair")
    if payload.get("source") == "parser_colos_chapter_range":
        score += 0.2
        reasons.append("colos_chapter_range_repair")
    if payload.get("source") == "parser_repeated_confusable_range":
        score += 0.1
        reasons.append("repeated_confusable_range_repair")
    if payload.get("source") == "parser_repeated_range_end":
        score += 0.1
        reasons.append("repeated_range_end_repair")
    if payload.get("source") == "context_range":
        score += 0.5
        reasons.append("context_range_reference")
    if payload.get("ambiguous_alternatives"):
        score += 0.15
        alternative_books = {
            str(item.get("book") or "")
            for item in payload.get("ambiguous_alternatives") or []
            if isinstance(item, dict)
        }
        parsed_book = str((payload.get("parsed") or {}).get("book") or "")
        if alternative_books and any(book != parsed_book for book in alternative_books):
            score += 0.1
            reasons.append("confusable_book_alternative")
        else:
            reasons.append("confusable_number_alternative")
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


def safe_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def reference_range_context(reference: dict | ParsedReference | None) -> dict | None:
    if isinstance(reference, ParsedReference):
        data = asdict(reference)
    elif isinstance(reference, dict):
        data = reference.get("parsed") if isinstance(reference.get("parsed"), dict) else reference
    else:
        return None

    book = str(data.get("book") or "").strip()
    chapter = safe_int(data.get("chapter"))
    start_verse = safe_int(data.get("start_verse"))
    end_chapter = safe_int(data.get("end_chapter")) or chapter
    end_verse = safe_int(data.get("end_verse"))
    if not book or chapter is None or start_verse is None or end_chapter is None or end_verse is None:
        return None
    if end_chapter < chapter or (end_chapter == chapter and end_verse <= start_verse):
        return None

    ref = (
        f"{book} {chapter}:{start_verse}-{end_verse}"
        if end_chapter == chapter
        else f"{book} {chapter}:{start_verse}-{end_chapter}:{end_verse}"
    )
    return {
        "book": book,
        "chapter": chapter,
        "start_verse": start_verse,
        "end_chapter": end_chapter,
        "end_verse": end_verse,
        "ref": str(data.get("ref") or ref),
    }


def book_family(book: str) -> str:
    return re.sub(r"^[1234]\s+", "", book).strip().lower().replace("ё", "е")


def has_explicit_other_book_marker(normalized: str, context: dict) -> bool:
    if re.search(r"\b(?:евангелие|книга|книги|пророк|пророка|псал\w*)\b", normalized):
        return True
    if not re.search(r"\bпослани\w*\b", normalized):
        return False
    context_family = book_family(str(context.get("book") or ""))
    candidates = book_candidates(normalized)
    if not candidates:
        return False
    return not any(book_family(candidate.book) == context_family for candidate in candidates)


def context_range_contains(context: dict, chapter: int, verse: int) -> bool:
    start_chapter = int(context["chapter"])
    start_verse = int(context["start_verse"])
    end_chapter = int(context["end_chapter"])
    end_verse = int(context["end_verse"])
    if chapter < start_chapter or chapter > end_chapter:
        return False
    if chapter == start_chapter and verse < start_verse:
        return False
    if chapter == end_chapter and verse > end_verse:
        return False
    return True


def context_chapter_for_verse(
    context: dict,
    verse: int,
    *,
    preferred_chapter: int | None = None,
) -> int | None:
    if preferred_chapter is not None and context_range_contains(context, preferred_chapter, verse):
        return preferred_chapter
    for chapter in range(int(context["chapter"]), int(context["end_chapter"]) + 1):
        if context_range_contains(context, chapter, verse):
            return chapter
    return None


def contextual_short_reference(
    text: str,
    context: dict | None,
    bible_path: Path = DEFAULT_BIBLE,
    *,
    preferred_chapter: int | None = None,
) -> dict | None:
    if not context:
        return None
    normalized = normalize_text(text)
    if has_explicit_other_book_marker(normalized, context):
        return None

    chapter: int | None = None
    verse: int | None = None
    for pattern in (
        r"\b(?P<verse>\d{1,3})\s+стих\w*\s+(?P<chapter>\d{1,3})\s+глав\w*\b",
        r"\b(?P<chapter>\d{1,3})\s+глав\w*\s+(?P<verse>\d{1,3})(?:\s+стих\w*)?\b",
        r"\b(?P<verse>\d{1,3})\s+(?P<chapter>\d{1,3})\s+глав\w*\b",
    ):
        match = re.search(pattern, normalized)
        if match:
            chapter = int(match.group("chapter"))
            verse = int(match.group("verse"))
            break

    if verse is None:
        verse_match = re.search(r"\b(?P<verse>\d{1,3})\s+стих\w*\b", normalized)
        if verse_match:
            verse = int(verse_match.group("verse"))
        elif re.fullmatch(r"\s*\d{1,3}\s*", normalized):
            verse = int(normalized.strip())
    if verse is None:
        return None

    if chapter is None:
        chapter = context_chapter_for_verse(context, verse, preferred_chapter=preferred_chapter)
    if chapter is None or not context_range_contains(context, chapter, verse):
        return None

    parsed = parse_live_reference(f"{context['book']} {chapter}:{verse}", bible_path=bible_path)
    if parsed is None:
        return None
    return {
        "text": text,
        "source": "context_range",
        "resolved": None,
        "parsed": asdict(parsed),
        "invalid_reference": None,
        "message": None,
        "matched": True,
        "bible_path": str(bible_path),
        "context_range": dict(context),
        "context_reference": True,
    }


def contextual_short_reference_from_candidates(
    candidates: list[str],
    context: dict | None,
    bible_path: Path = DEFAULT_BIBLE,
    *,
    preferred_chapter: int | None = None,
) -> dict | None:
    for candidate in candidates:
        payload = contextual_short_reference(
            candidate,
            context,
            bible_path=bible_path,
            preferred_chapter=preferred_chapter,
        )
        if payload:
            return payload
    return None


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
    current_ref = (attempts[0].get("parsed") or {}).get("ref") if attempts else None
    for index, payload in enumerate(attempts):
        reference_list = payload.get("reference_list") or []
        if payload.get("source") != "parser_reference_list" or len(reference_list) < 2:
            continue
        refs = {str(item.get("ref") or "") for item in reference_list if isinstance(item, dict)}
        if current_ref and current_ref not in refs:
            continue
        if index > 0:
            stale_list_item = False
            for item in reference_list:
                if not isinstance(item, dict) or str(item.get("ref") or "") == current_ref:
                    continue
                source_text = str(item.get("source_text") or "")
                if not has_reference_marker(source_text):
                    stale_list_item = True
                    break
                item_payload = resolve_reference_payload(source_text, bible_path=bible_path)
                if should_block_matched_payload(item_payload):
                    stale_list_item = True
                    break
            if stale_list_item:
                continue
        payload["attempts"] = [
            summary
            for summary_index, summary in enumerate(attempt_summaries)
            if summary_index != index
        ]
        return payload
    if attempts:
        first_text = str(attempts[0].get("text") or "")
        first_parsed = attempts[0].get("parsed") or {}
        if (
            first_parsed.get("book") == "Псалтирь"
            and first_parsed.get("start_verse") == 1
            and first_parsed.get("end_verse") == 1
            and re.fullmatch(r"\s*псалом\s+\S+(?:\s+\S+)?\s*", normalize_book_form(first_text))
        ):
            for index, payload in enumerate(attempts[1:], start=1):
                parsed = payload.get("parsed") or {}
                if (
                    parsed.get("book") == "Псалтирь"
                    and parsed.get("chapter") == first_parsed.get("chapter")
                    and int(parsed.get("end_verse") or 0) > int(parsed.get("start_verse") or 0)
                    and (
                        acceptable_buffer_context(str(payload.get("text") or ""), first_text)
                        or re.search(r"\b\d+\s+по\s+\d+\s+стих\b", normalize_text(str(payload.get("text") or "")))
                    )
                ):
                    payload["attempts"] = [
                        summary
                        for summary_index, summary in enumerate(attempt_summaries)
                        if summary_index != index
                    ]
                    return payload
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


def asr_word_time_bounds(asr_result: dict | None) -> tuple[int | None, int | None]:
    if not isinstance(asr_result, dict):
        return None, None
    starts: list[float] = []
    ends: list[float] = []
    for item in asr_result.get("result") or []:
        if not isinstance(item, dict):
            continue
        try:
            starts.append(float(item.get("start")))
            ends.append(float(item.get("end")))
        except (TypeError, ValueError):
            continue
    if not starts or not ends:
        return None, None
    return int(min(starts) * 1000), int(max(ends) * 1000)


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
        self.last_asr_word_end_ms: int | None = None
        self.last_parsed: dict | None = None
        self.context_range: dict | None = None
        self.context_current_chapter: int | None = None

    def set_context_range(self, reference: dict | ParsedReference | None) -> bool:
        context = reference_range_context(reference)
        if not context:
            return False
        self.context_range = context
        self.context_current_chapter = int(context["chapter"])
        return True

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
        asr_start_ms, asr_end_ms = asr_word_time_bounds(asr_result)
        if asr_start_ms is not None and self.last_asr_word_end_ms is not None:
            delta_ms = asr_start_ms - self.last_asr_word_end_ms
            delta_source = "asr_words"
        else:
            delta_ms = None if self.last_text_ms is None else current_ms - self.last_text_ms
            delta_source = "now_ms"
        buffer_reset_by_gap = False
        if delta_ms is not None and delta_ms > self.buffer_window_ms:
            self.text_buffer.clear()
            buffer_reset_by_gap = True
        self.last_text_ms = current_ms
        if asr_end_ms is not None:
            self.last_asr_word_end_ms = asr_end_ms

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
        context_payload = contextual_short_reference_from_candidates(
            candidate_texts,
            self.context_range,
            bible_path=self.bible_path,
            preferred_chapter=self.context_current_chapter,
        )
        if context_payload:
            payload = context_payload
        payload["vosk_text"] = text
        payload["vosk_buffer"] = list(self.text_buffer.parts)
        payload["candidate_texts"] = candidate_texts
        payload["delta_ms"] = delta_ms
        payload["delta_source"] = delta_source
        payload["buffer_window_ms"] = self.buffer_window_ms
        payload["buffer_reset_by_gap"] = buffer_reset_by_gap
        add_risk_score(payload, asr_result=asr_result)
        if payload.get("blocked_weak_context") == "incomplete_first_verse_after_chapter":
            payload["buffer_kept_for_open_range"] = True
        if payload.get("parsed"):
            self.last_parsed = payload["parsed"]
            if payload.get("context_reference"):
                self.context_current_chapter = int(payload["parsed"]["chapter"])
            if open_range_start_fragment(str(payload.get("text") or "")):
                payload["buffer_kept_for_open_range"] = True
            else:
                self.text_buffer.clear()
                payload["buffer_cleared_after_match"] = True
        return payload
