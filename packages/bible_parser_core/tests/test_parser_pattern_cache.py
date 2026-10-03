'''Stable parser patterns preserve results when the global cache churns.'''
import re
import unittest
from unittest.mock import patch
from bible_parser_core import parser


class ParserPatternCacheTest(unittest.TestCase):
    def setUp(self):
        parser._cached_book_candidates.cache_clear()
        self.addCleanup(parser._cached_book_candidates.cache_clear)

    def test_patterns_are_not_recompiled_after_cache_eviction(self):
        text = 'иоанна три шестнадцать'
        before = parser.book_candidates(text)
        normalized_before = parser.normalize_text(text)
        for i in range(2048):
            re.compile(f'cache_eviction_{i}')
        stable = {(p.pattern, 0) for _, _, p in parser._BOOK_VARIANT_PATTERNS}
        stable.update((p.pattern, re.IGNORECASE) for p, _ in parser._COMPILED_ASR_REPLACEMENTS)
        original_compile = re._compiler.compile

        def reject_recompile(pattern, flags=0):
            self.assertNotIn((pattern, flags), stable)
            return original_compile(pattern, flags)

        with patch.object(re._compiler, 'compile', side_effect=reject_recompile):
            parser._cached_book_candidates.cache_clear()
            after = parser.book_candidates(text)
            normalized = parser.normalize_text(text)
        self.assertEqual(before, after)
        self.assertEqual(normalized_before, normalized)
        self.assertTrue(any(c.book == 'Иоанн' and c.start == 0 and c.end == 6 for c in after))

    def test_alias_boundaries_and_spans(self):
        for variant, _, pattern in parser._BOOK_VARIANT_PATTERNS:
            with self.subTest(variant=variant):
                self.assertIsNotNone(pattern.fullmatch(variant))
                match = pattern.search(f'prefix {variant} suffix')
                self.assertEqual((7, 7 + len(variant)), match.span())
                self.assertIsNone(pattern.search(f'x{variant}x'))

    def test_repeated_text_skips_fuzzy_search_and_returns_independent_list(self):
        text = 'прочитаем евангелие от иоана 3 16'
        with patch.object(parser, 'get_close_matches', wraps=parser.get_close_matches) as search:
            before = parser.book_candidates(text)
            self.assertTrue(search.called)
            self.assertTrue(any(c.book == 'Иоанн' and c.score < 1 for c in before))
            calls = search.call_count
            after = parser.book_candidates(text)
            self.assertEqual(before, after)
            self.assertEqual(calls, search.call_count)
            after.clear()
            self.assertEqual(before, parser.book_candidates(text))

    def test_cached_candidates_preserve_scores_order_and_spans(self):
        # Changed verse numbers, distorted names and ordinary sermon words
        # must keep their own results even after a similar window was cached.
        for text in (
            'иоанна 3 16', 'иоанна 3 17', 'прочитаем от иоана 3 16',
            'в послании апостола к ефесянам 2 8', 'нам надо быть в себе самом',
            'никто да не обольщает вас 2 3', 'первое послание петра 1 3',
        ):
            with self.subTest(text=text):
                expected = list(parser._cached_book_candidates.__wrapped__(text))
                self.assertEqual(expected, parser.book_candidates(text))
                self.assertEqual(expected, parser.book_candidates(text))

    def test_old_windows_are_evicted_and_can_be_recomputed(self):
        first = '1000'
        expected = parser.book_candidates(first)
        capacity = parser._cached_book_candidates.cache_info().maxsize
        for number in range(1001, 1001 + capacity):
            parser.book_candidates(str(number))
        info = parser._cached_book_candidates.cache_info()
        self.assertEqual(capacity, info.currsize)
        self.assertEqual(expected, parser.book_candidates(first))
        self.assertEqual(info.misses + 1, parser._cached_book_candidates.cache_info().misses)


if __name__ == '__main__':
    unittest.main()
