'''Stable parser patterns preserve results when the global cache churns.'''
import re
import unittest
from unittest.mock import patch
from bible_parser_core import parser


class ParserPatternCacheTest(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
