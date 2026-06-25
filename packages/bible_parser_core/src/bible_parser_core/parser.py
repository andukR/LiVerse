#!/usr/bin/env python3
"""Direct parser for live ASR Bible references."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher, get_close_matches
from pathlib import Path

from bible_parser_core.book_aliases import book_lookup, books_data


DEFAULT_BIBLE = Path(__file__).resolve().parent / "data" / "rst.json"
_BIBLE_CACHE: dict[Path, dict[str, dict[int, dict[int, str]]]] = {}
_NUMBER_EXTRACTOR = None
_NUMBER_EXTRACTOR_UNAVAILABLE = False
NUMBER_WORD_BLACKLIST = {"семью"}
ROMAN_NUMERALS = {
    "i": 1,
    "ii": 2,
    "iii": 3,
    "iv": 4,
    "v": 5,
    "vi": 6,
    "vii": 7,
    "viii": 8,
    "ix": 9,
    "x": 10,
    "xi": 11,
    "xii": 12,
    "xiii": 13,
    "xiv": 14,
    "xv": 15,
    "xvi": 16,
    "xvii": 17,
    "xviii": 18,
    "xix": 19,
    "xx": 20,
}


@dataclass(frozen=True)
class ParsedReference:
    book: str
    chapter: int
    start_verse: int
    end_verse: int
    ref: str
    verse_text: str
    source_text: str
    confidence: float
    end_chapter: int | None = None


@dataclass(frozen=True)
class BookCandidate:
    book: str
    score: float
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class RefCandidate:
    chapter: int
    verses: list[int]
    start: int
    end: int
    score: float
    end_chapter: int | None = None
    end_verse: int | None = None


ORDINALS = {
    "первый": 1,
    "первая": 1,
    "первые": 1,
    "первую": 1,
    "первое": 1,
    "первого": 1,
    "первой": 1,
    "второй": 2,
    "вторая": 2,
    "вторые": 2,
    "вторую": 2,
    "второе": 2,
    "второго": 2,
    "вторым": 2,
    "третий": 3,
    "третья": 3,
    "третьи": 3,
    "третью": 3,
    "третье": 3,
    "третьего": 3,
    "третьей": 3,
    "третей": 3,
    "четвертый": 4,
    "четвертая": 4,
    "четвертые": 4,
    "четвертую": 4,
    "четвертое": 4,
    "четвертого": 4,
    "четвертой": 4,
    "пятый": 5,
    "пятая": 5,
    "пятые": 5,
    "пятую": 5,
    "пятое": 5,
    "пятого": 5,
    "пятой": 5,
    "шестой": 6,
    "шестая": 6,
    "шестые": 6,
    "шестую": 6,
    "шестого": 6,
    "седьмой": 7,
    "седьмая": 7,
    "седьмые": 7,
    "седьмую": 7,
    "седьмого": 7,
    "восьмой": 8,
    "восьмая": 8,
    "восьмые": 8,
    "восьмую": 8,
    "восьмого": 8,
    "девятый": 9,
    "девятая": 9,
    "девятые": 9,
    "девятую": 9,
    "девятого": 9,
    "девятой": 9,
    "десятый": 10,
    "десятая": 10,
    "десятые": 10,
    "десятую": 10,
    "десятого": 10,
    "одиннадцатый": 11,
    "одиннадцатая": 11,
    "одиннадцатые": 11,
    "одиннадцатую": 11,
    "одиннадцатого": 11,
    "двенадцатый": 12,
    "двенадцатая": 12,
    "двенадцатые": 12,
    "двенадцатую": 12,
    "двенадцатого": 12,
    "тринадцатый": 13,
    "тринадцатая": 13,
    "тринадцатые": 13,
    "тринадцатую": 13,
    "тринадцатого": 13,
    "четырнадцатый": 14,
    "четырнадцатая": 14,
    "четырнадцатые": 14,
    "четырнадцатую": 14,
    "четырнадцатого": 14,
    "пятнадцатый": 15,
    "пятнадцатая": 15,
    "пятнадцатые": 15,
    "пятнадцатую": 15,
    "пятнадцатого": 15,
    "шестнадцатый": 16,
    "шестнадцатая": 16,
    "шестнадцатые": 16,
    "шестнадцатую": 16,
    "шестнадцатого": 16,
    "шестнадцатой": 16,
    "семнадцатый": 17,
    "семнадцатая": 17,
    "семнадцатые": 17,
    "семнадцатую": 17,
    "семнадцатого": 17,
    "восемнадцатый": 18,
    "восемнадцатая": 18,
    "восемнадцатые": 18,
    "восемнадцатую": 18,
    "восемнадцатого": 18,
    "девятнадцатый": 19,
    "девятнадцатая": 19,
    "девятнадцатые": 19,
    "девятнадцатую": 19,
    "девятнадцатого": 19,
    "двадцать": 20,
    "двадцатый": 20,
    "двадцатая": 20,
    "двадцатую": 20,
    "двадцатого": 20,
    "тридцатый": 30,
    "тридцатая": 30,
    "тридцатого": 30,
    "шестидесятый": 60,
    "шестидесятая": 60,
    "шестидесятого": 60,
    "семидесятый": 70,
    "семидесятая": 70,
    "семидесятого": 70,
    "восьмидесятый": 80,
    "восьмидесятая": 80,
    "восьмидесятого": 80,
    "девяностый": 90,
    "девяностая": 90,
    "девяностого": 90,
    "сотый": 100,
    "сотая": 100,
    "сотого": 100,
    "сороковой": 40,
    "сороковая": 40,
    "сорок": 40,
    "сорокового": 40,
    "пятидесятый": 50,
    "пятидесятая": 50,
    "пятидесятого": 50,
}

CARDINALS = {
    "один": 1,
    "одна": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
    "семьдесят": 70,
    "восемьдесят": 80,
    "девяносто": 90,
    "сто": 100,
}

NUMBER_WORDS = {**CARDINALS, **ORDINALS}
ONE_CHAPTER_BOOKS = {"Филимону", "2 Иоанна", "3 Иоанна", "Иуда"}
FUZZY_NUMBER_HINTS = {
    "пер": [1],
    "вто": [2],
    "тре": [3],
    "трей": [3],
    "три": [3],
    "чет": [4, 14, 40],
    "четы": [4, 14, 40],
    "детый": [9],
    "детри": [29],
    "девя": [9, 19],
    "двад": [20],
    "трид": [30],
    "тридцат": [30],
    "тть": [30],
    "сор": [40],
    "пят": [5, 15, 50],
}
ADJACENT_ORDINAL_ASR_PAIRS = (
    (r"перв\w*", r"втор\w*"),
    (r"втор\w*", r"трет\w*"),
    (r"трет\w*", r"четвер\w*"),
    (r"четвер\w*", r"пят\w*"),
    (r"пят\w*", r"шест\w*"),
    (r"шест\w*", r"седьм\w*"),
    (r"седьм\w*", r"восьм\w*"),
    (r"восьм\w*", r"девят\w*"),
)

ASR_REPLACEMENTS = (
    (r"\bи\s+у\s*ван(?:на|ов)\b", "иоанна"),
    (r"\bи\s+уанна\b", "иоанна"),
    (
        r"\bтретье\s+послание[.,]?\s+апостол\s+и\s+иоанна\s+(\d+)\s*[-–]\s*(\d+)\b",
        r"третье послание иоанна \1-\2 стих",
    ),
    (
        r"\b(там\s+же(?:\s+\w+){0,4}\s+)(\d+)\s*[-–]\s*(\d+)\b",
        r"\1\2-\3 стих",
    ),
    (r"\bпро\s+рокас\s+афони[йяи]\b", "пророка софонии"),
    (r"\b(?:пророк\s+)?аста?фони[яи]\b", "пророка софонии"),
    (r"\bивреем\b", "евреям"),
    (r"\bниэмии\b", "неемии"),
    (r"\bниемии\b", "неемии"),
    (r"\bикресяст[а-я]*\b", "екклесиаст"),
    (r"\bиклисиаст[а-я]*\b", "екклесиаст"),
    (r"\bеклисиаст[а-я]*\b", "екклесиаст"),
    (r"\bеклисяст[а-я]*\b", "екклесиаст"),
    (r"\bклесяст[а-я]*\b", "екклесиаст"),
    (r"\bи\s+клесяст[а-я]*\b", "екклесиаст"),
    (r"\bизход\b", "исход"),
    (r"\b([1234])\s+мега\s+царств\b", r"\1 книга царств"),
    (r"\b2\s+законе\b", "второзаконие"),
    (r"\bтаразакон[а-я]*\s+1\s+глава\s+22\s+стих\b", "второзаконие 11 глава 22 стих"),
    (r"\bтаразакон[а-я]*\b", "второзаконие"),
    (r"\bвторозакон[а-я]*\s+ста[ея]\s+глава\b", "второзаконие 6 глава"),
    (r"\bи\s+лова\b", "иова"),
    (r"\bиоф\b", "иов"),
    (r"\b(книга|книги)\s+ио\b", r"\1 иова"),
    (r"\bивангел[а-я]*\b", "евангелие"),
    (r"\bивангед[а-я]*\b", "евангелие"),
    (r"\bевангелия\b", "евангелие"),
    (r"\bевангелие\s+атеана\b", "евангелие от иоанна"),
    (r"\bиван\s+гелятматфе[яа]\b", "евангелие от матфея"),
    (r"\bиван\s+гелиатриан[а-я]*\b", "евангелие от иоанна"),
    (r"\bевангелие\s+под\s+иоанна\b", "евангелие от иоанна"),
    (r"\bпод\s+иуанна\b", "от иоанна"),
    (r"\bевангелие\s+гуана\b", "евангелие от иоанна"),
    (r"\bи\s+у\s+ванна\b", "иоанна"),
    (r"\bот\s+илана\b", "от иоанна"),
    (r"\bот\s+яна\b", "от иоанна"),
    (r"\biii\b", "3"),
    (r"\bпереми[яюи]\b", "иеремия"),
    (r"\bтремя\s+я\b", "иеремия"),
    (r"\bи\s+ремью\b", "иеремия"),
    (r"\bремью\b", "иеремия"),
    (r"\bпаророка\s+и\s+языки\b", "пророка иезекииля"),
    (r"\bко[\s-]*ко[\s-]*и[\s-]*с[\s-]*а[\s-]*и\b", "исай"),
    (r"\bмалахи\b", "малахия"),
    (r"\bпервым\s+посланником\b", "первое послание"),
    (r"\bвторым\s+посланником\b", "второе послание"),
    (r"\bдеяния апостола\b", "деяния апостолов"),
    (r"\bидея боссов\b", "деяния апостолов"),
    (r"\bдиаметр\W+опослов\b", "деяния апостолов"),
    (r"\b(?:апостол\s+)?пет(?:е)?р\s+(?:в|во)\s+первом\s+послании\b", "1 послание петра"),
    (r"\b(?:апостол\s+)?пет(?:е)?р\s+(?:в|во)\s+втором\s+послании\b", "2 послание петра"),
    (r"\bпервом\s+послании\b", "первое послание"),
    (r"\bвтором\s+послании\b", "второе послание"),
    (r"\bапостол\s+иоанн\s+в\s+первом\s+послании\b", "1 послание иоанна"),
    (r"\bапостол\s+иоанн\s+(?:в|во)\s+первое\s+послание\b", "1 послание иоанна"),
    (r"\bапостол\s+иоанн\s+(?:в|во)\s+втором\s+послании\b", "2 послание иоанна"),
    (r"\bапостол\s+иоанн\s+(?:в|во)\s+второе\s+послание\b", "2 послание иоанна"),
    (r"\bапостол\s+иоанн\s+в\s+третьем\s+послании\b", "3 послание иоанна"),
    (r"\b(?:1|первая|первое)\s+темофе[яю]\b", "1 тимофею"),
    (r"\b(?:2|вторая|второе)\s+темофе[яю]\b", "2 тимофею"),
    (r"\bкол\s+осии\s+яна\b", "колоссянам 1"),
    (r"\bкол\s+осии\b", "колоссянам"),
    (r"\bкаринфен[а-я]*\b", "коринфянам"),
    (r"\bкаримфин[а-я]*\b", "коринфянам"),
    (r"\bфилиппитс\b", "филиппийцам"),
    (r"\bантрите\s+огова\b", "3 глава"),
    (r"\bсолм[а-я]*\b", "псалом"),
    (r"\bаге[яй]\b", "аггей"),
    (r"\b(\d+)\s+агла\b", r"\1 глава"),
    (r"\bпервоглава\b", "1 глава"),
    (r"\bпервостих\b", "1 стих"),
    (r"\bс\s+29\s+подлит\s+с\s+3\s+стих[а-я]*\b", "с 27 по 33 стих"),
    (r"\bс\s+(\d+)(?:[-\s]?го)?\s+под\s+(\d+)(?:[-\s]?й)?\b", r"с \1 по \2"),
    (r"\bс\s+(\d+)(?:[-\s]?го)?\s+года\W+20(?:[-\s]?е)?\s+месяц\b", r"с \1 по 21 стих"),
    (r"\bвашей\s+(?:сестой|стой)\s+стивы\b", "6 стих"),
    (r"\bдостая\s+глава\b", "6 глава"),
    (r"\bатмосфея\b", "от матфея"),
    (r"\bмотфей\b", "матфей"),
    (r"\bматвея\b", "матфея"),
    (r"\bот\s+фея\b", "матфея"),
    (r"\bперв\w*\s+книг\w*\W+(?:пар|пара)липомин[а-я]*\b", "1 паралипоменон"),
    (r"\bвтор\w*\s+книг\w*\W+(?:пар|пара)липомин[а-я]*\b", "2 паралипоменон"),
    (r"\bперв\w*\s+книг\w*\W+(?:пар|пара)ли\s+помин[а-я]*\b", "1 паралипоменон"),
    (r"\bвтор\w*\s+книг\w*\W+(?:пар|пара)ли\s+помин[а-я]*\b", "2 паралипоменон"),
    (r"\bи\s+вся\s+на\b", "ефесянам"),
    (r"\bвся\s+на\b", "ефесянам"),
    (r"\bпервое\s+послание\s+(?:апостола\s+павл[аы]\s+)?ф[еэ]с[еоа]лоник[еий]*ц[ае]м\b", "1 фессалоникийцам"),
    (r"\bвторое\s+послание\s+(?:апостола\s+павл[аы]\s+)?ф[еэ]с[еоа]лоник[еий]*ц[ае]м\b", "2 фессалоникийцам"),
    (r"\b(?:первое|первая|1)\s+(?:послание\s+)?фес+с?\b", "1 фессалоникийцам"),
    (r"\b(?:второе|вторая|2)\s+(?:послание\s+)?фес+с?\b", "2 фессалоникийцам"),
    (r"\bф[еэ]с[еоа]лоник[еий]*ц[ае]м\b", "фессалоникийцам"),
    (r"\bистих\b", "стих"),
    (r"\bиз\s+стих\b", "стих"),
    (r"\bиз\s+их\b", "стих"),
    (r"\bиз\s+тихо\b", "стих"),
    (r"\bиз\s+тих\b", "стих"),
    (r"\bтестих\b", "стих"),
    (r"\bстив\b", "стих"),
    (r"\bсих\b", "стих"),
    (r"\b(открыть|откройте|открываем|откроем)[^0-9а-яa-z]+при\s+(\d+\s+глав[аеуы])\b", r"\1 притчи \2"),
    (r"\bглавус\b", "глава"),
    (r"\bглавоз\b", "глава"),
    (r"\b(\d+)\s+голова\b", r"\1 глава"),
    (r"\bглавы\b", "глава"),
    (r"\bглаве\b", "глава"),
    (r"\bглаву\b", "глава"),
    (r"\bстиха\b", "стих"),
    (r"\bстихе\b", "стих"),
    (r"\bстихи\b", "стих"),
    (r"\bстихии\b", "стих"),
)

GENERIC_BOOK_VARIANTS = {
    "итак",
    "книга",
    "книги",
    "книга пророка",
    "пророка",
    "послание",
    "евангелие",
}


def normalize_text(text: str) -> str:
    normalized = text.lower().replace("ё", "е")
    for pattern, replacement in ASR_REPLACEMENTS:
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    for first_ordinal, next_ordinal in ADJACENT_ORDINAL_ASR_PAIRS:
        normalized = re.sub(
            rf"\b({first_ordinal})\s+{next_ordinal}\s+стих\b",
            r"\1 стих",
            normalized,
        )
    normalized = re.sub(
        r"\b([ivxlcdm]+)\s*,?\s+глав[аеуы]\b",
        lambda match: f"{ROMAN_NUMERALS.get(match.group(1), match.group(1))} глава",
        normalized,
    )
    normalized = re.sub(
        r"\b(евангелие\s+от\s+иоанна|от\s+иоанна|иоанна)\s+(\d)(\d{2})\b",
        r"\1 \2 \3",
        normalized,
    )
    normalized = re.sub(r"(\d+)\s*:\s*(\d+)\s*[-–]\s*(\d+)\s*:\s*(\d+)", r"\1 глава \2 стих по \3 глава \4 стих", normalized)
    normalized = re.sub(r"(\d+)\s*:\s*(\d+)\s*-\s*(\d+)", r"\1 глава \2-\3 стих", normalized)
    normalized = re.sub(r"(\d+)\s*:\s*(\d+)", r"\1 глава \2 стих", normalized)
    normalized = re.sub(r"(\d+)\s*[-–]\s*(\d+)\s*[-–]?\s*х\b", r"\1-\2 стих", normalized)
    normalized = re.sub(r"(\d+)[-–]?(?:й|я|ю|е|го|му|м)\b", r"\1", normalized)
    normalized = re.sub(r"[^0-9а-яa-z]+", " ", normalized)
    normalized = replace_number_phrases(normalized)
    tokens = normalized.split()
    tokens = replace_number_words(tokens)
    normalized = " ".join(tokens)
    normalized = re.sub(r"\b1\s+книг\w*\s+(?:пар|пара)липомин[а-я]*\b", "1 паралипоменон", normalized)
    normalized = re.sub(r"\b2\s+книг\w*\s+(?:пар|пара)липомин[а-я]*\b", "2 паралипоменон", normalized)
    normalized = re.sub(r"\b1\s+послание\s+(?:апостола\s+павл[аы]\s+)?фессалоникийцам\b", "1 фессалоникийцам", normalized)
    normalized = re.sub(r"\b2\s+послание\s+(?:апостола\s+павл[аы]\s+)?фессалоникийцам\b", "2 фессалоникийцам", normalized)
    normalized = re.sub(r"\b(\d+)\s+голова\b", r"\1 глава", normalized)
    normalized = re.sub(r"\b(\d+)\s+лас\b", r"\1 глава", normalized)
    normalized = re.sub(r"\b(\d+)\s+рюмке\b", r"\1 глава", normalized)
    normalized = re.sub(r"\b(\d+)\s+из\s+них\b", r"\1 стих", normalized)
    normalized = re.sub(
        r"\b(\d+)\s+глава\s+\d+\s+(\d+)\s+стих\s+(\d+)\s+(давайте|прочитаем)\b",
        r"\1 глава \3 \2 стих \4",
        normalized,
    )
    return normalized


def replace_number_phrases(text: str) -> str:
    global _NUMBER_EXTRACTOR, _NUMBER_EXTRACTOR_UNAVAILABLE
    if _NUMBER_EXTRACTOR_UNAVAILABLE:
        return text
    if _NUMBER_EXTRACTOR is None:
        try:
            from words2numsrus import NumberExtractor

            _NUMBER_EXTRACTOR = NumberExtractor()
        except Exception:
            _NUMBER_EXTRACTOR_UNAVAILABLE = True
            return text
    converted: list[str] = []
    for token in text.split():
        if token in NUMBER_WORD_BLACKLIST or not re.fullmatch(r"[а-яa-z]+", token):
            converted.append(token)
            continue
        try:
            replacement = _NUMBER_EXTRACTOR.replace(token)
        except Exception:
            converted.append(token)
            continue
        if re.fullmatch(r"\d+", replacement):
            converted.append(f"n{replacement}")
        else:
            converted.append(token)
    return " ".join(converted)


def number_word_value(token: str) -> tuple[int | None, bool]:
    if token.startswith("n") and token[1:].isdigit():
        return int(token[1:]), True
    value = NUMBER_WORDS.get(token)
    return value, False


def replace_number_words(tokens: list[str]) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(tokens):
        current, current_from_extractor = number_word_value(tokens[index])
        following, following_from_extractor = (
            number_word_value(tokens[index + 1]) if index + 1 < len(tokens) else (None, False)
        )
        if (
            current is not None
            and following is not None
            and ((current >= 20 and following < 10) or (current >= 100 and following < 100))
        ):
            result.append(str(current + following))
            index += 2
            continue
        if current is not None:
            result.append(str(current))
            index += 1
            continue
        result.append(tokens[index])
        index += 1
    return result


def fuzzy_number_candidates(token: str) -> list[int]:
    if token.isdigit():
        return [int(token)]
    values: set[int] = set()
    direct = NUMBER_WORDS.get(token)
    if direct is not None:
        values.add(direct)
    if len(token) >= 2:
        for prefix, prefix_values in FUZZY_NUMBER_HINTS.items():
            if token.startswith(prefix) or prefix.startswith(token):
                values.update(prefix_values)
    if len(token) >= 3:
        for word, value in NUMBER_WORDS.items():
            if word.startswith(token) or token.startswith(word):
                values.add(value)
    return sorted(value for value in values if 0 < value <= 150)


def fuzzy_phrase_candidates(tokens: list[str], index: int) -> list[tuple[int, int]]:
    if index >= len(tokens):
        return []
    candidates: list[tuple[int, int]] = []
    current_values = fuzzy_number_candidates(tokens[index])
    for value in current_values:
        candidates.append((value, index + 1))

    if index + 1 < len(tokens):
        next_values = fuzzy_number_candidates(tokens[index + 1])
        for first in current_values:
            for second in next_values:
                if first in {20, 30, 40, 50} and 1 <= second <= 9:
                    candidates.append((first + second, index + 2))
                if 2 <= first <= 5 and 1 <= second <= 9:
                    candidates.append((first * 10 + second, index + 2))

    unique: dict[tuple[int, int], tuple[int, int]] = {}
    for value, next_index in candidates:
        unique[(value, next_index)] = (value, next_index)
    return sorted(unique.values(), key=lambda item: (item[1], item[0]))


def fuzzy_range_before_stich(tokens: list[str], stich_index: int, chapter_map: dict[int, str]) -> list[list[int]]:
    ranges: list[list[int]] = []
    seen: set[tuple[int, int]] = set()
    for start_index in range(max(0, stich_index - 5), stich_index):
        if start_index > 0 and tokens[start_index - 1] == "не":
            continue
        for first_value, first_next in fuzzy_phrase_candidates(tokens, start_index):
            shadowed = False
            if start_index > 0:
                for previous_value, previous_next in fuzzy_phrase_candidates(tokens, start_index - 1):
                    if previous_next == first_next and previous_value > first_value:
                        shadowed = True
                        break
            if shadowed:
                continue
            if first_next >= stich_index:
                continue
            for second_value, second_next in fuzzy_phrase_candidates(tokens, first_next):
                if second_next != stich_index:
                    continue
                if second_value < 10 and first_value >= 10:
                    adjusted = (first_value // 10) * 10 + second_value
                    if adjusted > first_value and adjusted in chapter_map:
                        second_value = adjusted
                if first_value >= second_value:
                    continue
                if first_value not in chapter_map or second_value not in chapter_map:
                    continue
                key = (first_value, second_value)
                if key in seen:
                    continue
                seen.add(key)
                ranges.append(list(range(first_value, second_value + 1)))
    return ranges


def bible_map(path: Path = DEFAULT_BIBLE) -> dict[str, dict[int, dict[int, str]]]:
    path = path.resolve()
    if path in _BIBLE_CACHE:
        return _BIBLE_CACHE[path]
    data = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, dict[int, dict[int, str]]] = {}
    for index, book in enumerate(data.get("Books", [])):
        if index >= len(books_data):
            continue
        canonical = books_data[index][0]
        chapters: dict[int, dict[int, str]] = {}
        for chapter in book.get("Chapters", []):
            chapter_id = int(chapter.get("ChapterId"))
            chapters[chapter_id] = {
                int(verse.get("VerseId")): str(verse.get("Text") or "")
                for verse in chapter.get("Verses", [])
            }
        result[canonical] = chapters
    _BIBLE_CACHE[path] = result
    return result


def book_variants() -> dict[str, str]:
    variants: dict[str, str] = {}
    for variant, canonical in book_lookup.items():
        normalized = normalize_text(variant)
        if normalized in GENERIC_BOOK_VARIANTS:
            continue
        variants[normalized] = canonical
        variants[normalized.replace("послание к ", "послание ")] = canonical
        variants[normalized.replace("книга ", "")] = canonical
    for canonical, _synonyms in books_data:
        variants[normalize_text(canonical)] = canonical
    variants.update(
        {
            "матфея": "Матфей",
            "от матфея": "Матфей",
            "евангелие от матфея": "Матфей",
            "марка": "Марк",
            "луки": "Лука",
            "иоанна": "Иоанн",
            "евангелие от иоанна": "Иоанн",
            "послание галатам": "Галатам",
            "послание иуды": "Иуда",
            "1 послание иоанна": "1 Иоанна",
        }
    )
    return variants


BOOK_VARIANTS = book_variants()


def token_spans(normalized: str) -> list[tuple[str, int, int]]:
    return [(match.group(0), match.start(), match.end()) for match in re.finditer(r"\S+", normalized)]


def book_candidates(normalized: str) -> list[BookCandidate]:
    candidates: list[BookCandidate] = []
    seen: dict[tuple[str, int, int], BookCandidate] = {}
    for variant, canonical in BOOK_VARIANTS.items():
        if not variant or variant in GENERIC_BOOK_VARIANTS:
            continue
        for match in re.finditer(rf"(?<!\S){re.escape(variant)}(?!\S)", normalized):
            score = min(1.0, 0.72 + len(variant) / 45)
            candidate = BookCandidate(canonical, score, match.start(), match.end(), variant)
            key = (candidate.book, candidate.start, candidate.end)
            if key not in seen or candidate.score > seen[key].score:
                seen[key] = candidate

    tokens = token_spans(normalized)
    variant_keys = [key for key in BOOK_VARIANTS if key and key not in GENERIC_BOOK_VARIANTS]
    for size in (4, 3, 2, 1):
        for index in range(0, max(0, len(tokens) - size + 1)):
            start = tokens[index][1]
            end = tokens[index + size - 1][2]
            candidate_text = " ".join(token for token, _start, _end in tokens[index : index + size])
            if candidate_text in GENERIC_BOOK_VARIANTS:
                continue
            if re.search(r"\b(глава|стих|псалом|пророка|паророка|парарока)\b", candidate_text):
                continue
            if re.fullmatch(r"[123]\s+послание\s+\d+|послание\s+\d+", candidate_text):
                continue
            match = get_close_matches(candidate_text, variant_keys, n=1, cutoff=0.78)
            if not match:
                continue
            ratio = SequenceMatcher(None, candidate_text, match[0]).ratio()
            candidate = BookCandidate(BOOK_VARIANTS[match[0]], ratio, start, end, candidate_text)
            key = (candidate.book, candidate.start, candidate.end)
            if key not in seen or candidate.score > seen[key].score:
                seen[key] = candidate

    candidates = sorted(seen.values(), key=lambda item: (-item.score, item.start, item.end))
    exact_candidates = [candidate for candidate in candidates if candidate.score >= 0.999]
    return [
        candidate
        for candidate in candidates
        if not any(
            exact.book != candidate.book
            and candidate.score < 0.999
            and candidate.start <= exact.start
            and candidate.end >= exact.end
            for exact in exact_candidates
        )
    ]


def detect_book(normalized: str) -> tuple[str | None, float]:
    candidates = book_candidates(normalized)
    if candidates:
        return candidates[0].book, candidates[0].score
    return None, 0.0


def book_candidate_family(book: str) -> str:
    return re.sub(r"^\d+\s+", "", book).strip().lower()


def book_candidate_specificity_bonus(candidate: BookCandidate, candidates: list[BookCandidate]) -> float:
    bonus = 0.0
    family = book_candidate_family(candidate.book)
    text = candidate.text
    if re.match(r"^\d+\s", text):
        bonus += 0.75
    if any(keyword in text for keyword in ("послание", "евангелие", "книга")):
        bonus += 0.3
    bonus += min(0.35, len(text) / 80)

    for other in candidates:
        if other is candidate:
            continue
        if book_candidate_family(other.book) != family:
            continue
        if other.start <= candidate.start and other.end >= candidate.end and len(other.text) > len(candidate.text):
            bonus -= 0.7
        if candidate.start <= other.start and candidate.end >= other.end and len(candidate.text) > len(other.text):
            bonus += 0.25

    if family == candidate.book.lower() and len(text) <= len(family) + 2:
        for other in candidates:
            if other is candidate:
                continue
            if book_candidate_family(other.book) == family and other.book != candidate.book:
                bonus -= 0.85
                break
    return bonus


def reference_numbers(normalized: str, book: str) -> list[tuple[int, int, int]]:
    numbers = [(int(match.group(0)), match.start(), match.end()) for match in re.finditer(r"\b\d+\b", normalized)]
    book_family = book_candidate_family(book)
    if not book_family:
        return numbers

    book_number_spans = {
        (match.start(1), match.end(1))
        for match in re.finditer(rf"\b([1234])\s+{re.escape(book_family)}\b", normalized)
    }
    return [number for number in numbers if (number[1], number[2]) not in book_number_spans]


def ref_candidates(normalized: str, book: str, bible: dict[str, dict[int, dict[int, str]]]) -> list[RefCandidate]:
    chapters = bible.get(book, {})
    candidates: list[RefCandidate] = []

    def add(chapter: int, verses: list[int], start: int, end: int, score: float) -> None:
        if chapter not in chapters:
            return
        chapter_map = chapters[chapter]
        clean = [verse for verse in verses if verse in chapters[chapter]]
        candidate_score = score
        if not clean and len(verses) == 1:
            verse = verses[0]
            if verse >= 20 and verse % 10 == 0 and verse // 10 in chapter_map:
                clean = [verse // 10]
                candidate_score = min(score, 0.82)
        if clean and len(verses) > 1:
            last_verse = verses[-1]
            corrected_last = last_verse // 10
            if (
                last_verse >= 20
                and last_verse % 10 == 0
                and last_verse not in chapter_map
                and corrected_last in chapter_map
                and verses[0] <= corrected_last
            ):
                clean = [verse for verse in verses if verses[0] <= verse <= corrected_last and verse in chapter_map]
                candidate_score = min(score, 0.90)
        if not clean:
            return
        candidates.append(RefCandidate(chapter, clean, start, end, candidate_score))

    def add_cross_chapter(
        start_chapter: int,
        start_verse: int,
        end_chapter: int,
        end_verse: int,
        start: int,
        end: int,
        score: float,
    ) -> None:
        if end_chapter <= start_chapter:
            return
        start_map = chapters.get(start_chapter, {})
        end_map = chapters.get(end_chapter, {})
        if start_verse not in start_map or end_verse not in end_map:
            return
        candidates.append(
            RefCandidate(
                chapter=start_chapter,
                verses=[start_verse],
                start=start,
                end=end,
                score=score,
                end_chapter=end_chapter,
                end_verse=end_verse,
            )
        )

    explicit_patterns = (
        (r"(\d+)\s*:\s*(\d+)\s*-\s*(\d+)", 1, 2, 3, 1.0),
        (r"(\d+)\s*:\s*(\d+)", 1, 2, None, 1.0),
        (r"(\d+)\s+глава\s+(\d+)\s+стих", 1, 2, None, 1.0),
        (r"(\d+)\s+глава\s+[a-z]{1,12}\s+(\d+)\s+стих", 1, 2, None, 0.985),
        (r"(\d+)\s+стих\s+(\d+)\s+глава", 2, 1, None, 1.0),
        (r"стих\s+(\d+)\s+(\d+)\s+глава", 2, 1, None, 0.97),
        (r"(\d+)\s+(?:из\s+тех|из|стих)\s+(\d+)\s+глава", 2, 1, None, 0.96),
        (r"с\s+(\d+)\s+(?:из\s+тех|из)\s+(\d+)\s+глава", 2, 1, None, 0.96),
        (r"(\d+)\s+псалом\s+(\d+)\s+стих", 1, 2, None, 0.98),
        (r"(\d+)\s+(\d+)\s+стих\s+(\d+)\s+псал", 3, 1, 2, 0.985),
        (r"(\d+)\s+стих\s+(\d+)\s+псал", 2, 1, None, 0.98),
    )
    for pattern, chapter_group, verse_group, end_group, score in explicit_patterns:
        for match in re.finditer(pattern, normalized):
            chapter = int(match.group(chapter_group))
            verse = int(match.group(verse_group))
            verses = [verse]
            if end_group is not None:
                end_verse = int(match.group(end_group))
                if verse <= end_verse:
                    verses = list(range(verse, end_verse + 1))
            add(
                chapter,
                verses,
                match.start(),
                match.end(),
                score,
            )

    for match in re.finditer(r"стих\s+(\d+)\s+(\d+)\s+из\s+(\d+)\s+глава", normalized):
        start_verse = int(match.group(1))
        end_verse = int(match.group(2))
        chapter = int(match.group(3))
        if start_verse <= end_verse:
            add(chapter, list(range(start_verse, end_verse + 1)), match.start(), match.end(), 0.99)

    for match in re.finditer(r"(\d+)\s+(\d+)\s+из\s+стих\s+(\d+)\s+глава", normalized):
        start_verse = int(match.group(1))
        end_verse = int(match.group(2))
        chapter = int(match.group(3))
        if start_verse <= end_verse:
            add(chapter, list(range(start_verse, end_verse + 1)), match.start(), match.end(), 0.99)

    for match in re.finditer(r"стих\s+с\s+(\d+)\s+по\s+(\d+)\s+(\d+)\s+глава", normalized):
        start_verse = int(match.group(1))
        end_verse = int(match.group(2))
        chapter = int(match.group(3))
        if start_verse <= end_verse:
            add(chapter, list(range(start_verse, end_verse + 1)), match.start(), match.end(), 0.99)

    cross_chapter_patterns = (
        (r"(\d+)\s+глава\s+(?:с\s+)?(\d+)\s+стих\s+(?:по|до)\s+(\d+)\s+глава\s+(\d+)\s+стих", 0.995),
        (r"(\d+)\s+глава\s+(?:с\s+)?(\d+)\s+(?:по|до)\s+(\d+)\s+глава\s+(\d+)\s+стих", 0.985),
    )
    for pattern, score in cross_chapter_patterns:
        for match in re.finditer(pattern, normalized):
            add_cross_chapter(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                int(match.group(4)),
                match.start(),
                match.end(),
                score,
            )

    range_patterns = (
        (r"(\d+)\s+глава\s+с\s+(\d+)\s+по\s+(\d+)\s+стих", 1, 2, 3, 0.99),
        (r"(\d+)\s+глава\s+(\d+)\s+по\s+(\d+)\s+стих", 1, 2, 3, 0.99),
        (r"(\d+)\s+глава\s+с\s+(\d+)\s+стих\s+по\s+(\d+)(?:\s+стих)?", 1, 2, 3, 0.99),
        (r"(\d+)\s+глава\s+(\d+)\s+стих\s+по\s+(\d+)(?:\s+стих)?", 1, 2, 3, 0.99),
        (r"(\d+)\s+глава\s+с\s+(\d+)\s+стих\s+и\s+до\s+(\d+)(?:\s+стих)?", 1, 2, 3, 0.99),
        (r"(\d+)\s+глава\s+(\d+)\s*-\s*(\d+)\s+стих", 1, 2, 3, 0.99),
        (r"(\d+)\s+глава\s+(\d+)\s+и\s+(\d+)\s+стих", 1, 2, 3, 0.98),
        (r"(\d+)\s+глава\s+в\s+(\d+)\s+и\s+(\d+)\s+стих", 1, 2, 3, 0.98),
        (r"(\d+)\s+глава\s+(\d+)\s+и\s+(\d+)\s+и\s+(?:есть|там|мы|будем|написано|такие)\b", 1, 2, 3, 0.985),
        (r"(\d+)\s+глава\s+(\d+)\s+(\d+)\s+стих", 1, 2, 3, 0.94),
    )
    for pattern, chapter_group, start_group, end_group, score in range_patterns:
        for match in re.finditer(pattern, normalized):
            start_verse = int(match.group(start_group))
            end_verse = int(match.group(end_group))
            if start_verse <= end_verse:
                add(
                    int(match.group(chapter_group)),
                    list(range(start_verse, end_verse + 1)),
                    match.start(),
                    match.end(),
                    score,
                )

    if book in ONE_CHAPTER_BOOKS:
        chapter_map = chapters.get(1, {})
        for match in re.finditer(r"\b(\d+)\s*-\s*(\d+)\s+стих", normalized):
            start_verse = int(match.group(1))
            end_verse = int(match.group(2))
            if start_verse <= end_verse:
                add(1, list(range(start_verse, end_verse + 1)), match.start(), match.end(), 0.98)
        for match in re.finditer(r"\b(\d+)\s+(\d+)\s+стих", normalized):
            start_verse = int(match.group(1))
            end_verse = int(match.group(2))
            if start_verse <= end_verse and start_verse in chapter_map and end_verse in chapter_map:
                add(1, list(range(start_verse, end_verse + 1)), match.start(), match.end(), 0.96)
        for match in re.finditer(r"\b(?:с\s+)?(\d+)\s+по\s+(\d+)\s+стих", normalized):
            start_verse = int(match.group(1))
            end_verse = int(match.group(2))
            if start_verse <= end_verse:
                add(1, list(range(start_verse, end_verse + 1)), match.start(), match.end(), 0.96)
        for match in re.finditer(r"\b(\d+)\s+стих\b", normalized):
            add(1, [int(match.group(1))], match.start(), match.end(), 0.93)

    chapter = None
    for match in re.finditer(r"(\d+)\s+глава", normalized):
        value = int(match.group(1))
        if value in chapters:
            chapter = (value, match.start(), match.end())
    if chapter:
        chapter_map = chapters.get(chapter[0], {})
        chapter_end = max(chapter_map) if chapter_map else None
        tokens = [token for token, _start, _end in token_spans(normalized)]
        for match in re.finditer(
            r"(?:с\s+)?(\d+)\s+стих\s+и\s+до\s+конца\s+глава",
            normalized,
        ):
            start_verse = int(match.group(1))
            if chapter_end is not None and start_verse <= chapter_end:
                add(
                    chapter[0],
                    list(range(start_verse, chapter_end + 1)),
                    min(chapter[1], match.start()),
                    max(chapter[2], match.end()),
                    0.96,
                )
        for match in re.finditer(r"с\s+(\d+)\s+по\s+(\d+)(?:\s+стих)?", normalized):
            start_verse = int(match.group(1))
            end_verse = int(match.group(2))
            if start_verse <= end_verse:
                add(
                    chapter[0],
                    list(range(start_verse, end_verse + 1)),
                    min(chapter[1], match.start()),
                    max(chapter[2], match.end()),
                    0.95,
                )
        for match in re.finditer(r"с\s+(\d+)\s+стих\s+по\s+(\d+)(?:\s+стих)?", normalized):
            start_verse = int(match.group(1))
            end_verse = int(match.group(2))
            if start_verse <= end_verse:
                add(
                    chapter[0],
                    list(range(start_verse, end_verse + 1)),
                    min(chapter[1], match.start()),
                    max(chapter[2], match.end()),
                    0.95,
                )
        for match in re.finditer(r"\b(\d+)\s+стих\s+по\s+(\d+)(?:\s+стих)?", normalized):
            start_verse = int(match.group(1))
            end_verse = int(match.group(2))
            if start_verse <= end_verse:
                add(
                    chapter[0],
                    list(range(start_verse, end_verse + 1)),
                    min(chapter[1], match.start()),
                    max(chapter[2], match.end()),
                    0.95,
                )
        for match in re.finditer(r"\b(\d+)\s+и\s+(\d+)\s+стих\b", normalized):
            start_verse = int(match.group(1))
            end_verse = int(match.group(2))
            if start_verse <= end_verse:
                add(
                    chapter[0],
                    list(range(start_verse, end_verse + 1)),
                    min(chapter[1], match.start()),
                    max(chapter[2], match.end()),
                    0.95,
                )
        for match in re.finditer(r"\bстих\s+(\d+)\s+и\s+(\d+)\b", normalized):
            start_verse = int(match.group(1))
            end_verse = int(match.group(2))
            if start_verse <= end_verse:
                add(
                    chapter[0],
                    list(range(start_verse, end_verse + 1)),
                    min(chapter[1], match.start()),
                    max(chapter[2], match.end()),
                    0.95,
                )
        for match in re.finditer(r"с\s+(\d+)\s+стих\s+и\s+до\s+(\d+)(?:\s+стих)?", normalized):
            start_verse = int(match.group(1))
            end_verse = int(match.group(2))
            if start_verse <= end_verse:
                add(
                    chapter[0],
                    list(range(start_verse, end_verse + 1)),
                    min(chapter[1], match.start()),
                    max(chapter[2], match.end()),
                    0.95,
                )
        for match in re.finditer(r"(?:\d+\s+)?(\d+)\s+по\s+(\d+)\s+стих", normalized):
            start_verse = int(match.group(1))
            end_verse = int(match.group(2))
            if start_verse <= end_verse:
                add(
                    chapter[0],
                    list(range(start_verse, end_verse + 1)),
                    min(chapter[1], match.start()),
                    max(chapter[2], match.end()),
                    0.93,
                )
        for index, token in enumerate(tokens):
            if token != "по" or index == 0:
                continue
            start_candidates: list[tuple[int, int]] = []
            for start_index in (index - 2, index - 1):
                if start_index < 0:
                    continue
                for value, next_index in fuzzy_phrase_candidates(tokens, start_index):
                    if next_index == index:
                        start_candidates.append((value, next_index))
            end_candidates = fuzzy_phrase_candidates(tokens, index + 1)
            for start_verse, _start_end in start_candidates:
                for end_verse, end_next_index in end_candidates:
                    if start_verse >= end_verse:
                        continue
                    if start_verse not in chapter_map or end_verse not in chapter_map:
                        continue
                    if end_next_index < len(tokens) and tokens[end_next_index] not in {"стих", "стиха"}:
                        continue
                    add(
                        chapter[0],
                        list(range(start_verse, end_verse + 1)),
                        chapter[1],
                        chapter[2],
                        0.92,
                    )
        for match in re.finditer(r"\b(\d{3,4})\b(?:\s+стих)?", normalized):
            verses = split_compact_range_token(match.group(1), chapter_map, normalized=normalized)
            if verses:
                add(
                    chapter[0],
                    verses,
                    min(chapter[1], match.start()),
                    max(chapter[2], match.end()),
                    0.91,
                )
        for index, token in enumerate(tokens):
            if token not in {"стих", "стиха"}:
                continue
            if (
                index >= 2
                and tokens[index - 2].isdigit()
                and tokens[index - 1].isdigit()
                and len(tokens[index - 2]) == 1
                and len(tokens[index - 1]) == 1
            ):
                verse = int(tokens[index - 2] + tokens[index - 1])
                if verse in chapter_map:
                    add(chapter[0], [verse], chapter[1], chapter[2], 0.90)
            for verses in fuzzy_range_before_stich(tokens, index, chapter_map):
                add(
                    chapter[0],
                    verses,
                    chapter[1],
                    chapter[2],
                    0.905,
                )
        for match in re.finditer(r"(\d+)\s+стих|стих\s+(\d+)", normalized):
            verse = int(next(group for group in match.groups() if group))
            add(chapter[0], [verse], min(chapter[1], match.start()), max(chapter[2], match.end()), 0.88)

    numbers = reference_numbers(normalized, book)
    if not candidates and len(numbers) >= 3:
        for first, second, third in ((numbers[0], numbers[1], numbers[2]), (numbers[-3], numbers[-2], numbers[-1])):
            chapter_value, chapter_start, _chapter_end = first
            start_verse, _start_start, _start_end = second
            end_verse, _end_start, end_end = third
            chapter_map = chapters.get(chapter_value, {})
            if start_verse < end_verse and start_verse in chapter_map and end_verse in chapter_map:
                add(
                    chapter_value,
                    list(range(start_verse, end_verse + 1)),
                    chapter_start,
                    end_end,
                    0.73,
                )
    has_range_word = bool(re.search(r"\bпо\b", normalized))
    if not candidates and chapter is not None:
        numbers_after_chapter = [number for number in numbers if number[1] >= chapter[2]]
        if numbers_after_chapter:
            verse, verse_start, verse_end = numbers_after_chapter[0]
            add(chapter[0], [verse], min(chapter[1], verse_start), max(chapter[2], verse_end), 0.74)
    if not candidates and chapter is None and len(numbers) >= 2 and not has_range_word:
        for first, second in ((numbers[0], numbers[1]), (numbers[-2], numbers[-1])):
            first_value, first_start, first_end = first
            second_value, second_start, second_end = second
            add(first_value, [second_value], first_start, second_end, 0.72)
            add(second_value, [first_value], first_start, second_end, 0.70)

    return candidates


def numbers_near_keywords(normalized: str) -> tuple[int | None, list[int]]:
    chapter = None
    verses: list[int] = []

    for match in re.finditer(r"(\d+)\s+глава", normalized):
        value = int(match.group(1))
        if 1 <= value <= 150:
            chapter = value
    verse_patterns = (
        r"(\d+)\s+стих",
        r"стих\s+(\d+)\b(?!\s+глава)",
        r"с\s+(\d+)\s+по\s+(\d+)\s+стих",
        r"с\s+(\d+)\s+стих\s+по\s+(\d+)\s+стих",
        r"(\d+)\s+и\s+(\d+)\s+стих",
        r"(\d+)\s*-\s*(\d+)\s+стих",
        r"(\d+)\s+(\d+)\s+стих",
    )
    for pattern in verse_patterns:
        for match in re.finditer(pattern, normalized):
            values = [int(group) for group in match.groups() if group]
            if len(values) == 2 and values[0] < values[1]:
                verses.extend(range(values[0], values[1] + 1))
            else:
                verses.extend(values)

    return chapter, verses


def infer_chapter_and_verses(normalized: str, book: str, bible: dict[str, dict[int, dict[int, str]]]) -> tuple[int | None, list[int]]:
    candidates = ref_candidates(normalized, book, bible)
    if candidates:
        best = max(candidates, key=lambda item: (item.score, len(item.verses)))
        return best.chapter, best.verses

    chapter, verses = numbers_near_keywords(normalized)
    if chapter and verses:
        return chapter, verses

    numbers = [value for value, _start, _end in reference_numbers(normalized, book)]
    if chapter is None and book == "Псалтирь" and len(numbers) == 1:
        chapters = bible.get(book, {})
        if numbers[0] in chapters and 1 in chapters[numbers[0]]:
            return numbers[0], [1]
    if chapter is None and len(numbers) >= 2:
        chapters = bible.get(book, {})
        for first, second in ((numbers[0], numbers[1]), (numbers[-2], numbers[-1])):
            if first in chapters and second in chapters[first]:
                return first, [second]
            if second in chapters and first in chapters[second]:
                return second, [first]
    return chapter, verses


def compact_range(values: list[int]) -> tuple[int, int] | None:
    clean = sorted({value for value in values if value > 0})
    if not clean:
        return None
    if len(clean) == 2 and clean[0] < clean[1]:
        return clean[0], clean[1]
    return clean[0], clean[-1]


def cross_chapter_verse_text(
    book: str,
    start_chapter: int,
    start_verse: int,
    end_chapter: int,
    end_verse: int,
    bible: dict[str, dict[int, dict[int, str]]],
) -> str:
    chapters = bible.get(book, {})
    parts: list[str] = []
    for chapter in range(start_chapter, end_chapter + 1):
        chapter_map = chapters.get(chapter, {})
        if not chapter_map:
            continue
        first_verse = start_verse if chapter == start_chapter else min(chapter_map)
        last_verse = end_verse if chapter == end_chapter else max(chapter_map)
        for verse in range(first_verse, last_verse + 1):
            text = chapter_map.get(verse)
            if text:
                parts.append(f"{chapter}:{verse}. {text}")
    return " ".join(parts).strip()


def split_compact_range_token(token: str, chapter_map: dict[int, str], require_range_signal: bool = True, normalized: str = "") -> list[int]:
    if not token.isdigit() or len(token) < 3 or len(token) > 4:
        return []
    full_value = int(token)
    has_range_word = bool(re.search(r"\bпо\b", normalized)) if normalized else False
    if require_range_signal and normalized and not has_range_word and "-" not in normalized and full_value in chapter_map:
        return []
    values: list[tuple[int, int]] = []
    for split_at in range(1, len(token)):
        start = int(token[:split_at])
        end = int(token[split_at:])
        if start <= 0 or end <= 0 or start >= end:
            continue
        if start in chapter_map and end in chapter_map:
            values.append((start, end))
    if not values:
        return []
    if require_range_signal and normalized and not has_range_word and "-" not in normalized and len(values) != 1:
        return []
    values.sort(key=lambda item: (item[1] - item[0], item[0]), reverse=True)
    best_start, best_end = values[0]
    return list(range(best_start, best_end + 1))


def parse_live_reference(text: str, bible_path: Path = DEFAULT_BIBLE) -> ParsedReference | None:
    normalized = normalize_text(text)
    bible = bible_map(bible_path)
    books = book_candidates(normalized)
    if not books:
        return None
    explicit_numbered_books = [
        candidate
        for candidate in books
        if re.match(r"^[1234]\s", candidate.book) and re.match(r"^[1234]\s", candidate.text)
    ]
    if explicit_numbered_books:
        explicit_families = {
            book_candidate_family(candidate.book): candidate.book
            for candidate in explicit_numbered_books
        }
        books = [
            candidate
            for candidate in books
            if explicit_families.get(book_candidate_family(candidate.book), candidate.book) == candidate.book
        ]
    book_bonuses = {id(candidate): book_candidate_specificity_bonus(candidate, books) for candidate in books}
    best: tuple[float, BookCandidate, RefCandidate] | None = None
    for book_candidate in books:
        for ref_candidate in ref_candidates(normalized, book_candidate.book, bible):
            book_center = (book_candidate.start + book_candidate.end) / 2
            ref_center = (ref_candidate.start + ref_candidate.end) / 2
            distance = abs(book_center - ref_center)
            proximity = 1 / (1 + distance / 35)
            score = (book_candidate.score * 1.4) + (ref_candidate.score * 1.8) + proximity + book_bonuses[id(book_candidate)]
            before_book = normalized[max(0, book_candidate.start - 40) : book_candidate.start]
            if re.search(r"\b(откройте|откроем|открываем|открыть|прочитаем|читаем)\b", before_book):
                score += 0.25
            if re.search(r"\bвать\b", book_candidate.text):
                score -= 0.35
            if book_candidate.end <= ref_candidate.start:
                score += 0.2
            if ref_candidate.end_chapter is not None:
                score += 0.9
            if len(ref_candidate.verses) > 1:
                score += 0.7
            if best is None or score > best[0]:
                best = (score, book_candidate, ref_candidate)

    if best is None:
        book, confidence = books[0].book, books[0].score
        chapter, verses = infer_chapter_and_verses(normalized, book, bible)
        ref_candidate = None
    else:
        _score, book_candidate, ref_candidate = best
        book, confidence = book_candidate.book, book_candidate.score
        chapter, verses = ref_candidate.chapter, ref_candidate.verses

    if not chapter or not verses:
        return None
    if ref_candidate and ref_candidate.end_chapter is not None and ref_candidate.end_verse is not None:
        start_verse = verses[0]
        end_chapter = ref_candidate.end_chapter
        end_verse = ref_candidate.end_verse
        verse_text = cross_chapter_verse_text(book, chapter, start_verse, end_chapter, end_verse, bible)
        if not verse_text:
            return None
        ref = f"{book} {chapter}:{start_verse}-{end_chapter}:{end_verse}"
        return ParsedReference(
            book=book,
            chapter=chapter,
            start_verse=start_verse,
            end_verse=end_verse,
            ref=ref,
            verse_text=verse_text,
            source_text=text,
            confidence=confidence,
            end_chapter=end_chapter,
        )
    verse_range = compact_range(verses)
    if verse_range is None:
        return None
    start_verse, end_verse = verse_range
    chapter_map = bible.get(book, {}).get(chapter, {})
    if start_verse not in chapter_map:
        return None
    if (
        start_verse == end_verse
        and re.search(r"\b(?:с\s+)?\d+\s+стих\s+и\s+до\s+конца\s+глава\b", normalized)
        and chapter_map
    ):
        chapter_end = max(chapter_map)
        if start_verse < chapter_end:
            end_verse = chapter_end
    if end_verse not in chapter_map:
        end_verse = start_verse
    verse_ids = list(range(start_verse, end_verse + 1))
    verse_text = " ".join(
        f"{verse}. {chapter_map[verse]}" if len(verse_ids) > 1 else chapter_map[verse]
        for verse in verse_ids
        if verse in chapter_map
    ).strip()
    if not verse_text:
        return None
    ref = f"{book} {chapter}:{start_verse}" if start_verse == end_verse else f"{book} {chapter}:{start_verse}-{end_verse}"
    return ParsedReference(
        book=book,
        chapter=chapter,
        start_verse=start_verse,
        end_verse=end_verse,
        ref=ref,
        verse_text=verse_text,
        source_text=text,
        confidence=confidence,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse a live ASR Bible reference.")
    parser.add_argument("text", nargs="+")
    args = parser.parse_args()
    parsed = parse_live_reference(" ".join(args.text))
    if not parsed:
        print("not found")
        return 1
    print(parsed.ref)
    print(parsed.verse_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
