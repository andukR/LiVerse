"""Streaming helpers for detecting Bible quotations in recognized speech."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from time import perf_counter
from typing import Callable, Iterable

from bible_parser_core.bible_text_search import (
    BibleTextSearcher,
    BibleTextSearchResult,
    normalize_bible_text,
)
from bible_parser_core.parser import parse_live_reference
from bible_parser_core.verse_text_search import CANONICAL_BOOK_NAMES_BY_ID


COMMON_SPEECH_LEMMAS = {
    "а", "без", "бы", "быть", "в", "весь", "во", "вот", "вы", "да", "для",
    "до", "же", "за", "и", "из", "или", "как", "к", "когда", "который", "мы",
    "на", "не", "но", "о", "он", "она", "они", "от", "по", "при", "с", "со",
    "так", "то", "у", "что", "это", "я",
}


def _reference_key(reference: str) -> tuple[str, int, int, int, int] | str:
    parsed = parse_live_reference(reference)
    if parsed is None:
        return reference.casefold().strip()
    return (
        parsed.book,
        parsed.chapter,
        parsed.start_verse,
        parsed.end_chapter or parsed.chapter,
        parsed.end_verse,
    )


def _candidate_key(candidate: BibleTextSearchResult) -> tuple[str, int, int, int, int]:
    return (
        CANONICAL_BOOK_NAMES_BY_ID.get(candidate.book_id, str(candidate.book_id)),
        candidate.chapter,
        candidate.start_verse,
        candidate.chapter,
        candidate.end_verse or candidate.start_verse,
    )


def _candidate_contains(
    outer: BibleTextSearchResult,
    inner: BibleTextSearchResult,
) -> bool:
    outer_end = outer.end_verse or outer.start_verse
    inner_end = inner.end_verse or inner.start_verse
    return bool(
        outer.book_id == inner.book_id
        and outer.chapter == inner.chapter
        and outer.start_verse <= inner.start_verse
        and outer_end >= inner_end
    )


def _candidate_span(candidate: BibleTextSearchResult) -> int:
    return (candidate.end_verse or candidate.start_verse) - candidate.start_verse + 1


@dataclass(frozen=True)
class SpeechWindow:
    """One suffix of the normalized rolling speech buffer."""

    size: int
    text: str
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class TextCitationDecision:
    accepted: bool
    reference: str | None
    score: float
    margin: float
    matched_words: int
    window_text: str
    reason: str
    confirmations: int = 0
    top_candidate: BibleTextSearchResult | None = None
    second_candidate: BibleTextSearchResult | None = None


@dataclass(frozen=True)
class TextDetectionConfig:
    min_words: int = 5
    buffer_words: int = 35
    window_sizes: tuple[int, ...] = (5, 7, 10, 15, 20)
    candidate_limit: int = 100
    result_limit: int = 5
    acceptance_score: float = 70.0
    immediate_score: float = 90.0
    minimum_margin: float = 12.0
    minimum_matched_content_words: int = 3
    confirmations_required: int = 2
    confirmation_window_seconds: float = 5.0
    duplicate_cooldown_seconds: float = 30.0
    address_suppression_seconds: float = 8.0
    search_interval_ms: int = 300
    max_range_verses: int = 3


class SlidingSpeechBuffer:
    """Keep recent recognized words and expose configured suffix windows."""

    def __init__(
        self,
        *,
        buffer_words: int = 35,
        window_sizes: Iterable[int] = (5, 7, 10, 15, 20),
        min_words: int = 5,
    ) -> None:
        if buffer_words <= 0:
            raise ValueError("buffer_words должен быть положительным")
        if min_words <= 0:
            raise ValueError("min_words должен быть положительным")
        sizes = tuple(sorted({int(size) for size in window_sizes if int(size) >= min_words}))
        if not sizes:
            raise ValueError("window_sizes не содержит допустимых размеров")
        if sizes[-1] > buffer_words:
            raise ValueError("размер окна не может превышать buffer_words")

        self.buffer_words = int(buffer_words)
        self.window_sizes = sizes
        self.min_words = int(min_words)
        self._tokens: deque[str] = deque(maxlen=self.buffer_words)

    @property
    def tokens(self) -> tuple[str, ...]:
        return tuple(self._tokens)

    def clear(self) -> None:
        self._tokens.clear()

    def add(self, text: str) -> list[SpeechWindow]:
        fragment_tokens = normalize_bible_text(text)
        self._tokens.extend(fragment_tokens)
        tokens = self.tokens
        windows: list[SpeechWindow] = []
        fragment_size = min(len(fragment_tokens), len(tokens))
        sizes = set(self.window_sizes)
        if fragment_size >= self.min_words:
            sizes.add(fragment_size)
        for size in sorted(sizes):
            if len(tokens) < size:
                continue
            suffix = tokens[-size:]
            windows.append(SpeechWindow(size=size, text=" ".join(suffix), tokens=suffix))
        return windows


class ScriptureTextDetector:
    """Search rolling speech windows and require stable evidence before accepting."""

    def __init__(
        self,
        searcher: BibleTextSearcher,
        config: TextDetectionConfig | None = None,
        *,
        event_callback: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.searcher = searcher
        self.config = config or TextDetectionConfig()
        self.buffer = SlidingSpeechBuffer(
            buffer_words=self.config.buffer_words,
            window_sizes=self.config.window_sizes,
            min_words=self.config.min_words,
        )
        self.event_callback = event_callback
        self._last_search_at = float("-inf")
        self._suppressed_until = float("-inf")
        self._pending_reference: str | None = None
        self._pending_window = ""
        self._pending_at = float("-inf")
        self._confirmations = 0
        self._shown_at: dict[tuple[str, int, int, int, int] | str, float] = {}
        self._search_cache: dict[str, tuple[list[str], list[BibleTextSearchResult]]] = {}

    def clear(self) -> None:
        self.buffer.clear()
        self._reset_pending()

    def suppress_after_address(self, reference: str, now: float) -> None:
        self._suppressed_until = max(
            self._suppressed_until,
            now + self.config.address_suppression_seconds,
        )
        if reference:
            self._shown_at[_reference_key(reference)] = now
        self._reset_pending()
        self._emit("TEXT_SUPPRESSED", {"reason": "explicit_address", "reference": reference})

    def mark_shown(self, reference: str, now: float) -> None:
        if reference:
            self._shown_at[_reference_key(reference)] = now

    def process_fragment(self, text: str, now: float) -> TextCitationDecision:
        fragment_tokens = normalize_bible_text(text)
        windows = self.buffer.add(text)
        if 2 <= len(fragment_tokens) < self.config.min_words:
            windows.append(
                SpeechWindow(
                    size=len(fragment_tokens),
                    text=" ".join(fragment_tokens),
                    tokens=tuple(fragment_tokens),
                ),
            )
        if not windows:
            return self._decision(reason="not_enough_words")
        if now < self._suppressed_until:
            return self._decision(reason="address_suppression")
        if (now - self._last_search_at) * 1000 < self.config.search_interval_ms:
            return self._decision(reason="search_interval")
        self._last_search_at = now

        evaluated: list[TextCitationDecision] = []
        for window in windows:
            search_started = perf_counter()
            lemmas, results = self._search(window.text)
            search_ms = 1000.0 * (perf_counter() - search_started)
            self._emit(
                "TEXT_QUERY",
                {
                    "window": window.text,
                    "window_size": window.size,
                    "lemmas": lemmas,
                    "search_ms": round(search_ms, 3),
                },
            )
            decision = self._evaluate(window.text, lemmas, results, now)
            evaluated.append(decision)
            self._emit(
                "TEXT_CANDIDATE" if results else "TEXT_REJECTED",
                self._event_payload(decision),
            )

        best = max(
            evaluated,
            key=lambda item: (
                item.accepted,
                item.score,
                item.margin,
                item.matched_words,
                len(item.window_text.split()),
            ),
        )
        if best.accepted and best.top_candidate is not None:
            broader = [
                item
                for item in evaluated
                if item.accepted
                and item.top_candidate is not None
                and _candidate_span(item.top_candidate) > _candidate_span(best.top_candidate)
                and _candidate_contains(item.top_candidate, best.top_candidate)
                and item.score >= best.score - 5.0
            ]
            if broader:
                best = max(
                    broader,
                    key=lambda item: (_candidate_span(item.top_candidate), item.score),
                )
        if best.accepted:
            assert best.top_candidate is not None
            self._shown_at[_candidate_key(best.top_candidate)] = now
            self._reset_pending()
            self._emit("TEXT_ACCEPTED", self._event_payload(best))
            return best
        if best.reason != "candidate_ready":
            self._reset_pending()
            self._emit("TEXT_REJECTED", self._event_payload(best))
            return best
        return self._confirm(best, now)

    def _search(self, window_text: str) -> tuple[list[str], list[BibleTextSearchResult]]:
        cached = self._search_cache.get(window_text)
        if cached is not None:
            return cached
        result = self.searcher.search(
            window_text,
            limit=self.config.result_limit,
            candidate_limit=self.config.candidate_limit,
            max_range_verses=self.config.max_range_verses,
        )
        if len(self._search_cache) >= 128:
            self._search_cache.pop(next(iter(self._search_cache)))
        self._search_cache[window_text] = result
        return result

    def _evaluate(
        self,
        window_text: str,
        lemmas: list[str],
        results: list[BibleTextSearchResult],
        now: float,
    ) -> TextCitationDecision:
        if not results:
            return self._decision(window_text=window_text, reason="no_candidates")
        top = results[0]
        second = next(
            (
                candidate
                for candidate in results[1:]
                if not _candidate_contains(top, candidate)
            ),
            None,
        )
        margin = top.score - (second.score if second else 0.0)
        content_lemmas = {lemma for lemma in lemmas if lemma not in COMMON_SPEECH_LEMMAS}
        matched_words = sum(
            1
            for lemma in top.matched_lemmas
            if lemma in content_lemmas and lemma not in COMMON_SPEECH_LEMMAS
        )
        shown_at = self._shown_at.get(_candidate_key(top))
        if shown_at is not None and now - shown_at < self.config.duplicate_cooldown_seconds:
            return self._decision(
                reference=top.reference, score=top.score, margin=margin,
                matched_words=matched_words, window_text=window_text,
                reason="duplicate_cooldown", top=top, second=second,
            )
        strong_phrase_evidence = (
            matched_words >= 5
            or (
                matched_words >= self.config.minimum_matched_content_words
                and top.trigram_overlap >= 50.0
                and top.bigram_overlap >= 50.0
            )
        )
        immediate = (
            top.score >= self.config.immediate_score
            and margin >= self.config.minimum_margin
            and strong_phrase_evidence
            and top.trigram_overlap > 0
        )
        exact_phrase = (
            len(lemmas) >= self.config.min_words
            and top.start_verse == top.end_verse
            and top.score >= self.config.acceptance_score
            and margin >= self.config.minimum_margin
            and matched_words >= self.config.minimum_matched_content_words
            and top.coverage >= 99.0
            and top.bigram_overlap >= 99.0
            and top.trigram_overlap >= 99.0
        )
        exact_short_verse = (
            2 <= len(lemmas) < self.config.min_words
            and top.start_verse == top.end_verse
            and top.score >= 99.0
            and margin >= self.config.minimum_margin + 20.0
            and matched_words >= 2
            and top.coverage >= 99.0
            and top.ordered_similarity >= 99.0
            and top.token_similarity >= 99.0
            and top.bigram_overlap >= 99.0
        )
        strong_range = (
            top.end_verse > top.start_verse
            and top.score >= max(0.0, self.config.acceptance_score - 8.0)
            and margin >= self.config.minimum_margin + 3.0
            and matched_words >= self.config.minimum_matched_content_words + 1
            and top.bigram_overlap >= 60.0
            and top.trigram_overlap >= 45.0
        )
        if immediate or exact_phrase or exact_short_verse or strong_range:
            return self._decision(
                accepted=True, reference=top.reference, score=top.score, margin=margin,
                matched_words=matched_words, window_text=window_text,
                reason=(
                    "immediate_strong_range_match"
                    if strong_range and not immediate
                    else (
                        "immediate_exact_short_verse_match"
                        if exact_short_verse
                        else (
                            "immediate_exact_phrase_match"
                            if exact_phrase and not immediate
                            else "immediate_strong_match"
                        )
                    )
                ),
                confirmations=1, top=top, second=second,
            )
        score_threshold = self.config.acceptance_score
        if self._pending_reference == top.reference:
            score_threshold = max(0.0, score_threshold - 5.0)
        if top.score < score_threshold:
            reason = "score_below_threshold"
        elif margin < self.config.minimum_margin:
            reason = "margin_below_threshold"
        elif matched_words < self.config.minimum_matched_content_words:
            reason = "not_enough_matched_content_words"
        elif top.bigram_overlap <= 0 and top.ordered_similarity < 70.0:
            reason = "weak_word_order_evidence"
        else:
            reason = "candidate_ready"
        return self._decision(
            reference=top.reference, score=top.score, margin=margin,
            matched_words=matched_words, window_text=window_text,
            reason=reason, top=top, second=second,
        )

    def _confirm(self, decision: TextCitationDecision, now: float) -> TextCitationDecision:
        same_candidate = (
            self._pending_reference == decision.reference
            and self._pending_window != decision.window_text
            and now - self._pending_at <= self.config.confirmation_window_seconds
        )
        if same_candidate:
            self._confirmations += 1
        else:
            self._pending_reference = decision.reference
            self._confirmations = 1
        self._pending_window = decision.window_text
        self._pending_at = now
        if self._confirmations >= self.config.confirmations_required:
            accepted = self._decision(
                accepted=True, reference=decision.reference, score=decision.score,
                margin=decision.margin, matched_words=decision.matched_words,
                window_text=decision.window_text, reason="confirmed_stable_match",
                confirmations=self._confirmations, top=decision.top_candidate,
                second=decision.second_candidate,
            )
            assert accepted.top_candidate is not None
            self._shown_at[_candidate_key(accepted.top_candidate)] = now
            self._reset_pending()
            self._emit("TEXT_ACCEPTED", self._event_payload(accepted))
            return accepted
        pending = self._decision(
            reference=decision.reference, score=decision.score, margin=decision.margin,
            matched_words=decision.matched_words, window_text=decision.window_text,
            reason="pending_confirmation", confirmations=self._confirmations,
            top=decision.top_candidate, second=decision.second_candidate,
        )
        self._emit("TEXT_PENDING", self._event_payload(pending))
        return pending

    def _reset_pending(self) -> None:
        self._pending_reference = None
        self._pending_window = ""
        self._pending_at = float("-inf")
        self._confirmations = 0

    def _decision(
        self,
        *,
        accepted: bool = False,
        reference: str | None = None,
        score: float = 0.0,
        margin: float = 0.0,
        matched_words: int = 0,
        window_text: str = "",
        reason: str,
        confirmations: int = 0,
        top: BibleTextSearchResult | None = None,
        second: BibleTextSearchResult | None = None,
    ) -> TextCitationDecision:
        return TextCitationDecision(
            accepted=accepted, reference=reference, score=score, margin=margin,
            matched_words=matched_words, window_text=window_text, reason=reason,
            confirmations=confirmations, top_candidate=top, second_candidate=second,
        )

    def _emit(self, event: str, payload: dict) -> None:
        if self.event_callback is not None:
            self.event_callback(event, payload)

    @staticmethod
    def _event_payload(decision: TextCitationDecision) -> dict:
        return {
            "accepted": decision.accepted,
            "reference": decision.reference,
            "score": round(decision.score, 3),
            "margin": round(decision.margin, 3),
            "matched_words": decision.matched_words,
            "window": decision.window_text,
            "reason": decision.reason,
            "confirmations": decision.confirmations,
            "top_candidate": decision.top_candidate.reference if decision.top_candidate else None,
            "top_score": round(decision.top_candidate.score, 3) if decision.top_candidate else None,
            "second_candidate": decision.second_candidate.reference if decision.second_candidate else None,
            "second_score": round(decision.second_candidate.score, 3) if decision.second_candidate else None,
        }
