from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class _IdentityMorph:
    def parse(self, token: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(normal_form=token)]


class BibleTextSearcherTest(unittest.TestCase):
    def test_search_ranks_matching_verse_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "bible_index.db"
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE verses (
                    id INTEGER PRIMARY KEY, reference TEXT, text TEXT, lemma_text TEXT,
                    book_id INTEGER, chapter INTEGER, verse INTEGER
                );
                CREATE TABLE lemma_index (lemma TEXT, verse_id INTEGER, frequency INTEGER);
                CREATE TABLE tokens (wordform TEXT, lemma TEXT);
                """
            )
            connection.executemany(
                "INSERT INTO verses VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (1, "Ин. 3:16", "Ибо так возлюбил Бог мир", "ибо так возлюбил бог мир", 43, 3, 16),
                    (2, "Ин. 8:42", "Если бы Бог был Отец ваш", "если бы бог быть отец ваш", 43, 8, 42),
                    (3, "Ин. 11:35", "Иисус прослезился и сказал важную мысль", "иисус прослезиться и сказать важный мысль", 43, 11, 35),
                    (4, "Ин. 11:36", "Продолжение этой мысли услышали люди", "продолжение этот мысль услышать человек", 43, 11, 36),
                    (5, "Ин. 11:37", "Завершение общей мысли", "завершение общий мысль", 43, 11, 37),
                ],
            )
            connection.executemany(
                "INSERT INTO lemma_index VALUES (?, ?, 1)",
                [
                    (lemma, verse_id)
                    for verse_id, text in [
                        (1, "ибо так возлюбил бог мир"),
                        (2, "если бы бог быть отец ваш"),
                        (3, "иисус прослезиться и сказать важный мысль"),
                        (4, "продолжение этот мысль услышать человек"),
                        (5, "завершение общий мысль"),
                    ]
                    for lemma in text.split()
                ],
            )
            connection.executemany(
                "INSERT INTO tokens VALUES (?, ?)",
                [
                    (word, word)
                    for word in {
                        "ибо", "так", "возлюбил", "бог", "мир", "если", "бы",
                        "быть", "отец", "ваш", "иисус", "прослезиться", "и",
                        "сказать", "важный", "мысль", "продолжение", "этот",
                        "услышать", "человек",
                        "завершение", "общий",
                    }
                ],
            )
            connection.commit()
            connection.close()

            fake_module = SimpleNamespace(MorphAnalyzer=lambda: _IdentityMorph())
            with patch.dict(sys.modules, {"pymorphy3": fake_module}):
                from bible_parser_core.bible_text_search import BibleTextSearcher

                with BibleTextSearcher(db_path) as searcher:
                    lemmas, results = searcher.search("ибо так возлюбил бог мир")
                    _range_lemmas, range_results = searcher.search(
                        "сказать важный мысль продолжение этот мысль"
                    )
                    _three_range_lemmas, three_range_results = searcher.search(
                        "сказать важный мысль продолжение этот мысль услышать человек "
                        "завершение общий мысль",
                        max_range_verses=3,
                    )

            self.assertEqual(["ибо", "так", "возлюбил", "бог", "мир"], lemmas)
            self.assertEqual("Ин. 3:16", results[0].reference)
            self.assertGreater(results[0].score, results[1].score)
            self.assertEqual(("ибо", "так", "возлюбил", "бог", "мир"), results[0].matched_lemmas)
            self.assertEqual((43, 3, 16, 16), (
                results[0].book_id,
                results[0].chapter,
                results[0].start_verse,
                results[0].end_verse,
            ))
            self.assertEqual("Ин. 11:35-36", range_results[0].reference)
            self.assertEqual((35, 36), (
                range_results[0].start_verse,
                range_results[0].end_verse,
            ))
            self.assertEqual("Ин. 11:35-37", three_range_results[0].reference)
            self.assertEqual((35, 37), (
                three_range_results[0].start_verse,
                three_range_results[0].end_verse,
            ))

            with BibleTextSearcher(db_path, use_database_lemmas=True) as searcher:
                database_lemmas, database_results = searcher.search(
                    "ибо так возлюбил бог мир"
                )
            self.assertEqual(["ибо", "так", "возлюбил", "бог", "мир"], database_lemmas)
            self.assertEqual("Ин. 3:16", database_results[0].reference)


if __name__ == "__main__":
    unittest.main()
