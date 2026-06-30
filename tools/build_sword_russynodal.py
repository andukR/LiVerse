#!/usr/bin/env python3
"""Build LiVerse JSON Bible data from the CrossWire RusSynodal SWORD module."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path
from tempfile import NamedTemporaryFile

from pysword.modules import SwordModules


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_DIR = PROJECT_ROOT / ".cache" / "liverse" / "sword"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "packages"
    / "bible_parser_core"
    / "src"
    / "bible_parser_core"
    / "data"
    / "sword_russinodal.json"
)

MODULE_NAME = "RusSynodal"
MODULE_URL = "https://www.crosswire.org/ftpmirror/pub/sword/packages/rawzip/RusSynodal.zip"
EXPECTED_ARCHIVE_SHA256 = "b802570e1783c326552b9e810786efe3df4efcd615f28ccf3a86bae27dbc5022"
EXPECTED_CONF = {
    "version": "1.9.1",
    "swordversiondate": "2020-12-21",
    "distributionlicense": "Public Domain",
    "textsource": "http://www.rbo.ru/reading/articles/show/?4&start=0 or http://www.patriarchia.ru/bible/mf",
    "moddrv": "zText",
    "sourcetype": "OSIS",
    "encoding": "UTF-8",
    "versification": "Synodal",
}

EXPECTED_BOOK_COUNT = 66
EXPECTED_CHAPTER_COUNT = 1192
EXPECTED_VERSE_COUNT = 31350

EXTRACTION_FIXES = [
    {
        "ref": "Пс. 114:8-9",
        "reason": (
            "pysword/CrossWire zText extraction returns Psalm 114:9 appended to 114:8 "
            "and leaves 114:9 empty; split the two sentences without changing wording."
        ),
    }
]

BOOKS = [
    ("Genesis", "Быт."),
    ("Exodus", "Исх."),
    ("Leviticus", "Лев."),
    ("Numbers", "Чис."),
    ("Deuteronomy", "Втор."),
    ("Joshua", "Нав."),
    ("Judges", "Суд."),
    ("Ruth", "Руф."),
    ("I Samuel", "1Цар."),
    ("II Samuel", "2Цар."),
    ("I Kings", "3Цар."),
    ("II Kings", "4Цар."),
    ("I Chronicles", "1Пар."),
    ("II Chronicles", "2Пар."),
    ("Ezra", "Езд."),
    ("Nehemiah", "Неем."),
    ("Esther", "Есф."),
    ("Job", "Иов."),
    ("Psalms", "Пс."),
    ("Proverbs", "Прит."),
    ("Ecclesiastes", "Еккл."),
    ("Song of Solomon", "Песн."),
    ("Isaiah", "Ис."),
    ("Jeremiah", "Иер."),
    ("Lamentations", "Плач."),
    ("Ezekiel", "Иез."),
    ("Daniel", "Дан."),
    ("Hosea", "Ос."),
    ("Joel", "Иоил."),
    ("Amos", "Ам."),
    ("Obadiah", "Авд."),
    ("Jonah", "Ион."),
    ("Micah", "Мих."),
    ("Nahum", "Наум."),
    ("Habakkuk", "Авв."),
    ("Zephaniah", "Соф."),
    ("Haggai", "Агг."),
    ("Zechariah", "Зах."),
    ("Malachi", "Мал."),
    ("Matthew", "Мф."),
    ("Mark", "Мк."),
    ("Luke", "Лк."),
    ("John", "Ин."),
    ("Acts", "Деян."),
    ("Romans", "Рим."),
    ("I Corinthians", "1Кор."),
    ("II Corinthians", "2Кор."),
    ("Galatians", "Гал."),
    ("Ephesians", "Еф."),
    ("Philippians", "Флп."),
    ("Colossians", "Кол."),
    ("I Thessalonians", "1Фес."),
    ("II Thessalonians", "2Фес."),
    ("I Timothy", "1Тим."),
    ("II Timothy", "2Тим."),
    ("Titus", "Тит."),
    ("Philemon", "Флм."),
    ("Hebrews", "Евр."),
    ("James", "Иак."),
    ("I Peter", "1Пет."),
    ("II Peter", "2Пет."),
    ("I John", "1Ин."),
    ("II John", "2Ин."),
    ("III John", "3Ин."),
    ("Jude", "Иуд."),
    ("Revelation of John", "Откр."),
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, destination: Path, *, refresh: bool = False) -> None:
    if destination.exists() and not refresh:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read()
    destination.write_bytes(data)


def normalize_verse_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def module_conf_from_zip(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        with archive.open("mods.d/russynodal.conf") as file:
            raw = file.read().decode("utf-8")

    values: dict[str, str] = {}
    module_seen = False
    current_key = ""
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line == f"[{MODULE_NAME}]":
            module_seen = True
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            current_key = key.strip().lower()
            values[current_key] = value.strip()
        elif current_key:
            values[current_key] += "\n" + line

    if not module_seen:
        raise RuntimeError(f"{MODULE_NAME} section not found in module conf")
    return values


def validate_source(path: Path, *, allow_new_source: bool = False) -> dict[str, str]:
    archive_hash = sha256_file(path)
    if archive_hash != EXPECTED_ARCHIVE_SHA256 and not allow_new_source:
        raise RuntimeError(
            "RusSynodal archive SHA256 changed. "
            f"Expected {EXPECTED_ARCHIVE_SHA256}, got {archive_hash}. "
            "Re-check CrossWire metadata before updating the pinned hash."
        )

    conf = module_conf_from_zip(path)
    for key, expected in EXPECTED_CONF.items():
        actual = conf.get(key)
        if actual != expected and not allow_new_source:
            raise RuntimeError(
                f"RusSynodal metadata changed for {key}: expected {expected!r}, got {actual!r}"
            )
    conf["archive_sha256"] = archive_hash
    return conf


def build_json(module_zip: Path, conf: dict[str, str]) -> dict:
    modules = SwordModules(str(module_zip))
    metadata = modules.parse_modules()
    if MODULE_NAME not in metadata:
        raise RuntimeError(f"{MODULE_NAME} not found in {module_zip}")

    bible = modules.get_bible_from_module(MODULE_NAME)
    structure = bible.get_structure()
    books = []
    chapter_count = 0
    verse_count = 0

    for book_id, (sword_name, book_name) in enumerate(BOOKS, start=1):
        _testament, book_structure = structure.find_book(sword_name)
        chapters = []
        for chapter_id, chapter_length in enumerate(book_structure.chapter_lengths, start=1):
            verse_texts = {
                verse_id: normalize_verse_text(
                    bible.get(books=sword_name, chapters=chapter_id, verses=verse_id)
                )
                for verse_id in range(1, chapter_length + 1)
            }
            apply_extraction_fixes(sword_name, chapter_id, verse_texts)

            verses = []
            for verse_id in range(1, chapter_length + 1):
                verse_text = verse_texts[verse_id]
                if not verse_text:
                    raise RuntimeError(f"Empty verse text at {sword_name} {chapter_id}:{verse_id}")
                verses.append({"VerseId": verse_id, "Text": verse_text})
            chapters.append({"ChapterId": chapter_id, "Verses": verses})
            chapter_count += 1
            verse_count += len(verses)
        books.append({"BookId": book_id, "BookName": book_name, "Chapters": chapters})

    if len(books) != EXPECTED_BOOK_COUNT:
        raise RuntimeError(f"Expected {EXPECTED_BOOK_COUNT} books, got {len(books)}")
    if chapter_count != EXPECTED_CHAPTER_COUNT:
        raise RuntimeError(f"Expected {EXPECTED_CHAPTER_COUNT} chapters, got {chapter_count}")
    if verse_count != EXPECTED_VERSE_COUNT:
        raise RuntimeError(f"Expected {EXPECTED_VERSE_COUNT} verses, got {verse_count}")

    john_316 = books[42]["Chapters"][2]["Verses"][15]["Text"]
    if "Ибо так возлюбил Бог мир" not in john_316:
        raise RuntimeError(f"Unexpected John 3:16 text: {john_316}")

    return {
        "Translation": "RusSynodal",
        "Source": {
            "Module": MODULE_NAME,
            "Description": conf.get("description", ""),
            "DescriptionEn": conf.get("description_en", ""),
            "Version": conf["version"],
            "SwordVersionDate": conf["swordversiondate"],
            "DistributionLicense": conf["distributionlicense"],
            "TextSource": conf["textsource"],
            "DownloadUrl": MODULE_URL,
            "ArchiveSha256": conf["archive_sha256"],
            "ModDrv": conf["moddrv"],
            "SourceType": conf["sourcetype"],
            "Encoding": conf["encoding"],
            "Versification": conf["versification"],
            "BookCount": len(books),
            "ChapterCount": chapter_count,
            "VerseCount": verse_count,
            "Builder": "tools/build_sword_russynodal.py",
            "ExtractionFixes": EXTRACTION_FIXES,
        },
        "Books": books,
    }


def apply_extraction_fixes(sword_name: str, chapter_id: int, verse_texts: dict[int, str]) -> None:
    if sword_name != "Psalms" or chapter_id != 114:
        return
    verse_8 = verse_texts.get(8, "")
    verse_9 = verse_texts.get(9, "")
    marker = " Буду ходить пред лицем Господним на земле живых."
    if not verse_9 and marker.strip() in verse_8:
        verse_texts[8] = verse_8.replace(marker, "").strip()
        verse_texts[9] = marker.strip()


def stable_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def write_or_check(data: dict, output: Path, *, check: bool) -> None:
    text = stable_json(data)
    if check:
        existing = output.read_text(encoding="utf-8")
        if existing != text:
            raise RuntimeError(f"{output} is not up to date. Run this script without --check.")
        return

    output.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=output.parent, delete=False) as file:
        file.write(text)
        temp_path = Path(file.name)
    temp_path.replace(output)
    output.chmod(0o644)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--refresh", action="store_true", help="Download RusSynodal.zip even when cached.")
    parser.add_argument("--check", action="store_true", help="Verify that output is already up to date.")
    parser.add_argument(
        "--allow-new-source",
        action="store_true",
        help="Allow changed upstream metadata/hash. Use only after manually reviewing CrossWire changes.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    module_zip = args.cache_dir / "RusSynodal.zip"
    download(MODULE_URL, module_zip, refresh=args.refresh)
    conf = validate_source(module_zip, allow_new_source=args.allow_new_source)
    data = build_json(module_zip, conf)
    write_or_check(data, args.output, check=args.check)
    source = data["Source"]
    print(
        "Built "
        f"{args.output} from {source['Module']} {source['Version']} "
        f"({source['SwordVersionDate']}), "
        f"{source['BookCount']} books, {source['ChapterCount']} chapters, {source['VerseCount']} verses."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
