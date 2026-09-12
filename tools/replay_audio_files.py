#!/usr/bin/env python3
"""Replay LiVerse recognition against saved sermon audio files."""

from __future__ import annotations

import argparse
import copy
import html
import json
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECTS_ROOT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
CORE_SRC = PROJECT_ROOT / "packages" / "bible_parser_core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from vosk import KaldiRecognizer, Model, SetLogLevel

from bible_parser_core.bible_text_search import BibleTextSearcher
from bible_parser_core.live_pipeline import LiveReferencePipeline, build_grammar, grammar_diagnostics
from bible_parser_core.parser import DEFAULT_BIBLE, bible_map, normalize_text, parse_live_reference
from bible_parser_core.sequence_advancer import (
    decide_sequence_advance_from_text,
    decide_sequence_progress_from_text,
)
from bible_parser_core.sherpa_streaming import (
    DEFAULT_SHERPA_THREADS,
    SherpaReplayRecognizer,
    load_sherpa_recognizer,
    sherpa_result_to_vosk_result,
)
from bible_parser_core.text_citation_detector import (
    ScriptureTextDetector,
)
from bible_parser_core.verse_text_search import CANONICAL_BOOK_NAMES_BY_ID
from tools.holyrics import (
    DEFAULT_LONG_RANGE_MIN_VERSES,
    DEFAULT_LONG_RANGE_SLIDE_MAX_VERSES,
    scripture_range,
    scripture_range_quick_presentation_slides,
    scripture_range_reading_state,
)
from tools.vosk_grammar_probe import (
    DEFAULT_LOG_DIR,
    DEFAULT_MODEL_PATH,
    DEFAULT_TEXT_DETECTION_DB,
    JsonlLogger,
    add_slide_payload,
    accumulate_reading_list,
    address_recognition_allowed,
    format_timecode,
    payload_summary,
    text_citation_payload,
    text_operator_hint_payload,
    text_decision_ready_for_scripture_range,
    trigger_time_info,
)


AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".opus", ".ogg", ".flac", ".webm", ".mp4"}
SUBTITLE_EXTENSIONS = {".srt", ".txt", ".vtt"}
TIMED_SUBTITLE_EXTENSIONS = {".srt", ".vtt"}
YOUTUBE_ID_RE = re.compile(r"(?<![A-Za-z0-9-])([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")
YOUTUBE_URL_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)
SKIP_DIR_NAMES = {
    ".git",
    ".gradle",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".tber",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "venv",
}
DEFAULT_SEARCH_ROOTS = (
    PROJECTS_ROOT / "bible_parser_cli" / ".cache" / "whisper_runs",
    PROJECTS_ROOT / "live_scripture_presenter" / ".cache" / "live_case_replay" / "audio",
    PROJECTS_ROOT / "liveverse-public-release" / ".cache" / "live_emulator" / "audio",
)
DEFAULT_SUBTITLE_ROOTS = (
    PROJECTS_ROOT / "bible_parser_cli" / "transcripts",
    PROJECTS_ROOT / "bible_parser_cli" / ".cache" / "whisper_runs",
    Path(".cache") / "liverse" / "replay_subtitles",
)
LATEST_REPLAY_BATCH = "latest_replay_batch.json"
DEFAULT_TARGET_ANNOTATIONS = 200
DEFAULT_WINDOW_PLAN_DIR = Path(".cache") / "liverse" / "replay_window_plans"
DEFAULT_WINDOW_AUDIO_DIR = Path(".cache") / "liverse" / "replay_window_audio"
WINDOW_AUDIO_PART_RE = re.compile(
    r"^(?P<window>\d+)_(?:citation|control)_part\d+_"
    r"(?P<start>\d+)_(?P<end>\d+)\.wav$"
)
DEFAULT_CONTROL_WINDOWS_PER_PLAN = 3
CONTROL_WINDOW_SECONDS = 45.0
MAX_REPLAY_WINDOW_SECONDS = 120.0
REPLAY_WINDOW_OVERLAP_SECONDS = 5.0
MARKER_PADDING_BEFORE_SECONDS = 15.0
MARKER_PADDING_AFTER_SECONDS = 45.0
LONG_RANGE_REPLAY_MIN_VERSES = 5
# The ordinary 45-second tail normally contains the address, a short pause,
# and roughly the first three verses.  Keep enough additional source audio for
# each remaining verse of an announced long reading.
LONG_RANGE_BASE_INCLUDED_VERSES = 3
LONG_RANGE_EXTRA_SECONDS_PER_VERSE = 12.0
TEXT_PADDING_BEFORE_SECONDS = 12.0
TEXT_PADDING_AFTER_SECONDS = 15.0
CANDIDATE_MERGE_GAP_SECONDS = 30.0
ADDRESS_MARKER_RE = re.compile(
    r"\b(?:стих\w*|глав\w*|послани\w*|евангели\w*|пророк\w*|книг\w*|"
    r"прочита\w*|откро\w*|псал\w*|пса\s+лом\w*|"
    r"сало(?=\s+(?:\d{1,3}|перв\w*|втор\w*|трет\w*|четв[её]рт\w*|пят\w*|"
    r"шест\w*|седьм\w*|восьм\w*|девят\w*|десят\w*|двадцат\w*|тридцат\w*|"
    r"сорок\w*|пятидесят\w*|шестидесят\w*|семидесят\w*|восьмидесят\w*|"
    r"девяност\w*|сот\w*)))\b",
    re.IGNORECASE,
)
REFERENCE_NUMBER_RE = re.compile(
    r"\b(?:\d{1,3}|одн\w*|два|две|три|четыре|перв\w*|втор\w*|трет\w*|четв[её]рт\w*|пят\w*|"
    r"шест\w*|седьм\w*|восьм\w*|девят\w*|десят\w*|одиннадцат\w*|"
    r"двенадцат\w*|тринадцат\w*|четырнадцат\w*|пятнадцат\w*|"
    r"шестнадцат\w*|семнадцат\w*|восемнадцат\w*|девятнадцат\w*|"
    r"двадцат\w*|тридцат\w*|сорок\w*|пятидесят\w*|шестидесят\w*|"
    r"семидесят\w*|восьмидесят\w*|девяност\w*|сот\w*)\b",
    re.IGNORECASE,
)
REFERENCE_STRUCTURE_RE = re.compile(
    r"\b(?:стих\w*|глав(?:а|ы|е|у|ой|ою|ам|ами|ах)?|псал\w*|пса\s+лом\w*)\b",
    re.IGNORECASE,
)
DEFAULT_SHERPA_MODEL_PATH = (
    PROJECT_ROOT
    / ".cache"
    / "liverse"
    / "models"
    / "vosk-model-small-streaming-ru-0.54"
)
BOOK_IDS_BY_CANONICAL_NAME = {
    book_name: book_id for book_id, book_name in CANONICAL_BOOK_NAMES_BY_ID.items()
}
CHAPTER_READING_ANNOUNCEMENT_RE = re.compile(
    r"\b(?:будем\s+читать|давайте\s+(?:сейчас\s+)?(?:читать|прочитаем|откроем))\b"
)
# This inference is replay-only and deliberately excludes very long chapters:
# an ordinary announcement of Psalm 119 must not create 176 virtual slides.
MAX_INFERRED_CHAPTER_READING_VERSES = 40
SEQUENTIAL_TEXT_READING_MAX_GAP_SECONDS = 20.0


def replay_long_passage(payload: dict) -> dict | None:
    """Represent a long passage that the replay assumes the operator accepted."""
    minimum_verses = 2 if payload.get("replay_inferred_sequential_text_reading") else DEFAULT_LONG_RANGE_MIN_VERSES
    selected = scripture_range(
        payload.get("parsed") or {},
        min_same_chapter_verses=minimum_verses,
    )
    if selected is None:
        return None
    book, chapter, start_verse, end_chapter, end_verse = selected
    book_id = BOOK_IDS_BY_CANONICAL_NAME.get(book)
    if book_id is None:
        return None
    return {
        "book": book,
        "book_id": book_id,
        "chapter": chapter,
        "start_verse": start_verse,
        "end_chapter": end_chapter,
        "end_verse": end_verse,
        "ref": str((payload.get("parsed") or {}).get("ref") or ""),
    }


def infer_replay_chapter_reading(payload: dict) -> dict | None:
    """Expand an explicit announcement of reading a small whole chapter.

    A bare chapter name is still a normal reference.  This applies only to
    replay when the speaker explicitly says that the congregation will read,
    the parser supplied its implicit first verse, and the chapter is small.
    """
    parsed = payload.get("parsed") if isinstance(payload.get("parsed"), dict) else {}
    if str(payload.get("source") or "") != "parser":
        return None
    try:
        chapter = int(parsed.get("chapter") or 0)
        start_verse = int(parsed.get("start_verse") or 0)
        end_chapter = int(parsed.get("end_chapter") or chapter)
        end_verse = int(parsed.get("end_verse") or start_verse)
    except (TypeError, ValueError):
        return None
    book = str(parsed.get("book") or "")
    spoken = normalize_text(str(payload.get("text") or ""))
    if (
        not book
        or chapter <= 0
        or (start_verse, end_chapter, end_verse) != (1, chapter, 1)
        or "стих" in spoken
        or not CHAPTER_READING_ANNOUNCEMENT_RE.search(spoken)
    ):
        return None
    verses = bible_map(DEFAULT_BIBLE).get(book, {}).get(chapter, {})
    last_verse = max(verses, default=0)
    if not 1 < last_verse <= MAX_INFERRED_CHAPTER_READING_VERSES:
        return None
    expanded = parse_live_reference(f"{book} {chapter}:1-{last_verse}")
    if expanded is None:
        return None
    inferred = copy.deepcopy(payload)
    inferred["source"] = "replay_inferred_chapter_reading"
    inferred["parsed"] = {
        "book": expanded.book,
        "chapter": expanded.chapter,
        "start_verse": expanded.start_verse,
        "end_chapter": expanded.end_chapter or expanded.chapter,
        "end_verse": expanded.end_verse,
        "ref": expanded.ref,
        "verse_text": expanded.verse_text,
    }
    inferred["replay_inferred_chapter_reading"] = True
    return add_slide_payload(inferred)


def infer_replay_sequential_text_reading(
    payload: dict,
    replay_state: dict[str, object],
    replay_seconds: float,
) -> dict | None:
    """Open a replay-only chapter range after two consecutive text matches."""
    if str(payload.get("source") or "") != "text_citation":
        return None
    parsed = payload.get("parsed") if isinstance(payload.get("parsed"), dict) else {}
    try:
        chapter = int(parsed.get("chapter") or 0)
        start_verse = int(parsed.get("start_verse") or 0)
        end_chapter = int(parsed.get("end_chapter") or chapter)
        end_verse = int(parsed.get("end_verse") or start_verse)
    except (TypeError, ValueError):
        return None
    book = str(parsed.get("book") or "")
    if not book or chapter <= 0 or end_chapter != chapter or end_verse < start_verse:
        return None
    current = {
        "book": book,
        "chapter": chapter,
        "start_verse": start_verse,
        "end_verse": end_verse,
        "replay_seconds": float(replay_seconds),
    }
    previous = replay_state.get("sequential_text_reading")
    replay_state["sequential_text_reading"] = current
    if not isinstance(previous, dict):
        return None
    try:
        previous_seconds = float(previous.get("replay_seconds") or 0.0)
        previous_start = int(previous.get("start_verse") or 0)
        previous_end = int(previous.get("end_verse") or previous_start)
    except (TypeError, ValueError):
        return None
    if (
        str(previous.get("book") or "") != book
        or int(previous.get("chapter") or 0) != chapter
        or replay_seconds - previous_seconds > SEQUENTIAL_TEXT_READING_MAX_GAP_SECONDS
        # The ranges must overlap or touch and the new one must extend them.
        or start_verse > previous_end + 1
        or end_verse <= previous_end
    ):
        return None
    range_start = min(previous_start, start_verse)
    # The endpoint has not been announced.  Keep it open and grow the range
    # only when the following verses are actually recognized; guessing the
    # end of John 14 as verse 31 would mislead the congregation.
    if end_verse - range_start + 1 < 3:
        return None
    expanded = parse_live_reference(f"{book} {chapter}:{range_start}-{end_verse}")
    if expanded is None:
        return None
    inferred = copy.deepcopy(payload)
    inferred["source"] = "replay_inferred_sequential_text_reading"
    inferred["parsed"] = {
        "book": expanded.book,
        "chapter": expanded.chapter,
        "start_verse": expanded.start_verse,
        "end_chapter": expanded.end_chapter or expanded.chapter,
        "end_verse": expanded.end_verse,
        "ref": expanded.ref,
        "verse_text": expanded.verse_text,
    }
    inferred["replay_inferred_sequential_text_reading"] = True
    return add_slide_payload(inferred)


def replay_smart_slide_state(payload: dict, slide_mode: str) -> dict | None:
    """Build the same ordered slide bounds as a live Holyrics presentation."""
    range_payload = payload.get("slide") or payload.get("parsed") or payload
    max_verses = 1 if slide_mode == "one_verse" else DEFAULT_LONG_RANGE_SLIDE_MAX_VERSES
    minimum_verses = 2 if payload.get("replay_inferred_sequential_text_reading") else DEFAULT_LONG_RANGE_MIN_VERSES
    slides = scripture_range_quick_presentation_slides(
        range_payload,
        max_verses=max_verses,
        min_same_chapter_verses=minimum_verses,
    )
    state = scripture_range_reading_state(range_payload, slides)
    if state is not None:
        state["slide_mode"] = slide_mode
    return state


def manual_smart_slide_state(
    reference: str,
    *,
    bible_path: Path,
    slide_mode: str,
) -> dict:
    """Build replay-only slide state for a range announced outside the WAV."""
    parsed = parse_live_reference(reference, bible_path=bible_path)
    if parsed is None:
        raise ValueError(f"Не удалось разобрать диапазон УПС: {reference}")
    end_chapter = int(parsed.end_chapter or parsed.chapter)
    end_position = (end_chapter, int(parsed.end_verse))
    start_position = (int(parsed.chapter), int(parsed.start_verse))
    if end_position <= start_position:
        raise ValueError("Для --smart-slide-passage нужен диапазон минимум из двух стихов.")
    payload = {
        "book": parsed.book,
        "chapter": parsed.chapter,
        "start_verse": parsed.start_verse,
        "end_chapter": end_chapter,
        "end_verse": parsed.end_verse,
        "ref": parsed.ref,
    }
    max_verses = 1 if slide_mode == "one_verse" else DEFAULT_LONG_RANGE_SLIDE_MAX_VERSES
    slides = scripture_range_quick_presentation_slides(
        payload,
        max_verses=max_verses,
        min_same_chapter_verses=2,
    )
    state = scripture_range_reading_state(payload, slides)
    if state is None:
        raise ValueError(f"Не удалось построить слайды УПС для: {parsed.ref}")
    state["slide_mode"] = slide_mode
    state["manual_context"] = True
    return state


def apply_replay_smart_slide_decision(state: dict, decision: dict) -> bool:
    """Update only replay's virtual slide; return False when the range is complete."""
    if decision.get("action") == "complete":
        return False
    if decision.get("action") == "activate":
        state["current_slide_visible"] = True
    target_index = decision.get("target_index")
    if isinstance(target_index, int):
        state["current_index"] = target_index
    return True


def replay_smart_slide_decision(
    state: dict,
    global_decision: object | None,
    sequence_decision: object | None,
) -> dict:
    """Prefer nearby sequence evidence, retaining strong global catch-up."""
    decision = dict(decide_sequence_advance_from_text(state, global_decision, sequence_decision))
    return defer_open_ended_replay_completion(state, decision)


def defer_open_ended_replay_completion(state: dict, decision: dict) -> dict:
    """Keep a text-derived passage open until later text disproves it.

    The current final slide is merely the final verse observed so far, not the
    end of a range announced by the preacher.  A later text match can extend
    its targets, so a sequence-level complete must become a hold.
    """
    if not state.get("open_ended") or decision.get("action") != "complete":
        return decision
    deferred = dict(decision)
    deferred.update({
        "action": "keep",
        "action_label": "ожидать следующий стих",
        "will_transition": False,
        "target_index": None,
        "target_element": None,
        "reason": "await_next_element_for_open_reading",
    })
    return deferred


def handle_replay_smart_slide_partial(
    partial: str,
    replay_seconds: float,
    text_detector: ScriptureTextDetector | None,
    replay_state: dict[str, object],
    logger: JsonlLogger,
) -> None:
    """Advance replay's virtual slide from evolving Sherpa text."""
    state = replay_state.get("smart_slide_shadow")
    if state is None or text_detector is None:
        return
    decision, evidence = decide_sequence_progress_from_text(
        state,
        None,
        lambda current: text_detector.evaluate_known_sequence_text(
            current,
            partial,
            replay_seconds,
        ),
    )
    decision = defer_open_ended_replay_completion(state, decision)
    if decision.get("action") not in {
        "activate", "advance", "assisted_advance", "synchronize_forward",
        "assisted_synchronize_forward", "complete",
    }:
        return
    logger.write(
        "SMART_SLIDE_SHADOW",
        {
            **decision,
            "passage": str(state.get("ref") or ""),
            "slide_mode": str(state.get("slide_mode") or ""),
            "window": str(getattr(evidence, "window_text", "") or partial),
            "replay_seconds": replay_seconds,
            "recognition_result": "partial",
        },
    )
    if not apply_replay_smart_slide_decision(state, decision):
        replay_state["smart_slide_shadow"] = None


def replay_long_passage_match(decision: object, passage: dict) -> dict:
    """Check whether Bible-text recognition has reached the passage's last verse."""
    if passage.get("open_ended"):
        return {"active": True, "completed": False, "reason": "sequential_reading_open"}
    if not text_decision_ready_for_scripture_range(decision):
        return {"active": True, "completed": False, "reason": "boundary_not_ready"}
    candidate = getattr(decision, "top_candidate", None)
    candidate_start = int(getattr(candidate, "start_verse", 0) or 0)
    candidate_end = int(getattr(candidate, "end_verse", candidate_start) or candidate_start)
    completed = bool(
        int(getattr(candidate, "book_id", 0) or 0) == int(passage["book_id"])
        and int(getattr(candidate, "chapter", 0) or 0) == int(passage["end_chapter"])
        and candidate_start <= int(passage["end_verse"]) <= candidate_end
    )
    return {
        "active": not completed,
        "completed": completed,
        "reason": "long_passage_completed" if completed else "inside_long_passage",
        "candidate": str(getattr(candidate, "reference", "") or ""),
    }


def extend_open_ended_replay_passage(
    passage: dict,
    state: object,
    decision: object,
) -> bool:
    """Append newly confirmed verses to a text-derived one-verse sequence."""
    if not passage.get("open_ended") or not isinstance(state, dict):
        return False
    if str(state.get("slide_mode") or "") != "one_verse":
        return False
    candidate = getattr(decision, "top_candidate", None)
    if candidate is None:
        return False
    try:
        candidate_book = int(getattr(candidate, "book_id", 0) or 0)
        candidate_chapter = int(getattr(candidate, "chapter", 0) or 0)
        candidate_end = int(getattr(candidate, "end_verse", 0) or 0)
        current_end = int(passage.get("end_verse") or 0)
    except (TypeError, ValueError):
        return False
    if (
        candidate_book != int(passage.get("book_id") or 0)
        or candidate_chapter != int(passage.get("end_chapter") or 0)
        or candidate_end <= current_end
    ):
        return False
    book = str(passage.get("book") or "")
    chapter = int(passage.get("chapter") or 0)
    targets = state.get("targets")
    if not book or chapter <= 0 or not isinstance(targets, list):
        return False
    for verse in range(current_end + 1, candidate_end + 1):
        parsed = parse_live_reference(f"{book} {chapter}:{verse}")
        if parsed is None:
            return False
        targets.append({
            "slide_index": len(targets),
            "start_chapter": chapter,
            "start_verse": verse,
            "chapter": chapter,
            "verse": verse,
            "text": parsed.verse_text,
        })
    passage["end_verse"] = candidate_end
    passage["ref"] = f"{book} {chapter}:{int(passage['start_verse'])}-{candidate_end}"
    state["ref"] = passage["ref"]
    return True


def restore_replay_session_context(
    pipeline: LiveReferencePipeline,
    replay_state: dict[str, object],
    session_state: dict[str, object] | None,
) -> bool:
    """Restore only semantic state when the next WAV continues one window.

    ASR buffers deliberately remain per WAV: neighbouring parts overlap by a
    few seconds and carrying recognised words would process that speech twice.
    """
    if not session_state:
        return False
    context_range = session_state.get("context_range")
    if isinstance(context_range, dict):
        pipeline.context_range = dict(context_range)
        pipeline.context_current_chapter = int(
            session_state.get("context_current_chapter") or context_range.get("chapter") or 0
        ) or None
    long_passage = session_state.get("long_passage")
    if isinstance(long_passage, dict):
        replay_state["long_passage"] = dict(long_passage)
    smart_slide_shadow = session_state.get("smart_slide_shadow")
    if isinstance(smart_slide_shadow, dict):
        replay_state["smart_slide_shadow"] = dict(smart_slide_shadow)
    return bool(
        pipeline.context_range is not None
        or replay_state.get("long_passage") is not None
        or replay_state.get("smart_slide_shadow") is not None
    )


def save_replay_session_context(
    pipeline: LiveReferencePipeline,
    replay_state: dict[str, object],
    session_state: dict[str, object] | None,
) -> None:
    """Keep selected range state for the following part of the same window."""
    if session_state is None:
        return
    session_state["context_range"] = (
        dict(pipeline.context_range) if isinstance(pipeline.context_range, dict) else None
    )
    session_state["context_current_chapter"] = pipeline.context_current_chapter
    long_passage = replay_state.get("long_passage")
    session_state["long_passage"] = dict(long_passage) if isinstance(long_passage, dict) else None
    smart_slide_shadow = replay_state.get("smart_slide_shadow")
    session_state["smart_slide_shadow"] = (
        dict(smart_slide_shadow) if isinstance(smart_slide_shadow, dict) else None
    )


def audio_duration(path: Path) -> float | None:
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as audio:
                return audio.getnframes() / float(audio.getframerate())
        except Exception:
            return None
    if not shutil.which("ffprobe"):
        return None
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(command, check=True, text=True, capture_output=True)
        return float(result.stdout.strip())
    except Exception:
        return None


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def collect_audio_files(search_roots: list[Path], include_chunks: bool) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for root in search_roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            if not include_chunks and "_chunks" in str(path):
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(path)

    files = dedupe_audio_files(files)

    def sort_key(path: Path) -> tuple[int, int, str]:
        preferred = 0 if path.name.endswith("_16k_mono.wav") else 1
        return preferred, -path.stat().st_size, str(path)

    return sorted(files, key=sort_key)


def collect_subtitle_youtube_ids(search_roots: list[Path]) -> dict[str, Path]:
    ids: dict[str, Path] = {}
    for root in search_roots:
        if not root.exists():
            continue
        for path in iter_subtitle_files(root):
            for video_id in youtube_ids_from_path(path):
                current = ids.get(video_id)
                if current is None or subtitle_file_preference(path) < subtitle_file_preference(current):
                    ids[video_id] = path
            if path.name.lower().endswith(".url.txt"):
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    text = ""
                for video_id in youtube_ids_from_text(text):
                    ids.setdefault(video_id, path)
    return ids


def collect_timed_subtitle_youtube_ids(search_roots: list[Path]) -> dict[str, Path]:
    """Return only SRT/VTT sources usable for choosing replay windows."""
    ids: dict[str, Path] = {}
    for root in search_roots:
        if not root.exists():
            continue
        for path in iter_subtitle_files(root):
            if path.suffix.lower() not in TIMED_SUBTITLE_EXTENSIONS:
                continue
            for video_id in youtube_ids_from_path(path):
                current = ids.get(video_id)
                if current is None or subtitle_file_preference(path) < subtitle_file_preference(current):
                    ids[video_id] = path
    return ids


def subtitle_file_preference(path: Path) -> tuple[int, str]:
    """Prefer timed subtitle formats over plain transcript text."""
    return (0 if path.suffix.lower() in TIMED_SUBTITLE_EXTENSIONS else 1, str(path))


def parse_subtitle_time(value: str) -> float:
    hours, minutes, seconds = value.strip().replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def read_timed_subtitle_cues(path: Path) -> list[dict[str, object]]:
    """Read the small SRT/VTT subset needed for selecting replay windows."""
    if path.suffix.lower() not in TIMED_SUBTITLE_EXTENSIONS:
        raise ValueError(f"У субтитров нет таймкодов: {path}")
    text = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    cues: list[dict[str, object]] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        start_text, end_text = (part.strip().split()[0] for part in lines[timing_index].split("-->", 1))
        try:
            start_seconds = parse_subtitle_time(start_text)
            end_seconds = parse_subtitle_time(end_text)
        except (ValueError, IndexError):
            continue
        cue_text = html.unescape(" ".join(lines[timing_index + 1 :]))
        cue_text = re.sub(r"<[^>]+>", "", cue_text).strip()
        if cue_text and end_seconds >= start_seconds:
            cues.append({"start_seconds": start_seconds, "end_seconds": end_seconds, "text": cue_text})
    return cues


def announced_range_verse_count(parsed: object) -> int | None:
    """Return a same-chapter range size known from its spoken address."""
    if parsed is None:
        return None
    try:
        chapter = int(getattr(parsed, "chapter"))
        end_chapter = int(getattr(parsed, "end_chapter") or chapter)
        start_verse = int(getattr(parsed, "start_verse"))
        end_verse = int(getattr(parsed, "end_verse"))
    except (TypeError, ValueError, AttributeError):
        return None
    if end_chapter != chapter or end_verse < start_verse:
        return None
    return end_verse - start_verse + 1


def announced_range_padding_after(parsed: object, default_padding: float) -> float:
    """Reserve source audio for reading a long, explicitly announced range."""
    verse_count = announced_range_verse_count(parsed)
    if verse_count is None or verse_count < LONG_RANGE_REPLAY_MIN_VERSES:
        return default_padding
    remaining_verses = max(0, verse_count - LONG_RANGE_BASE_INCLUDED_VERSES)
    return default_padding + remaining_verses * LONG_RANGE_EXTRA_SECONDS_PER_VERSE


def subtitle_marker_candidates(
    cues: list[dict[str, object]],
    *,
    padding_before_seconds: float = MARKER_PADDING_BEFORE_SECONDS,
    padding_after_seconds: float = MARKER_PADDING_AFTER_SECONDS,
) -> list[dict[str, object]]:
    """Return context candidates around explicit Bible-reference markers."""
    candidates: list[dict[str, object]] = []
    for index, cue in enumerate(cues):
        text = str(cue["text"])
        markers = sorted({match.group(0).lower() for match in ADDRESS_MARKER_RE.finditer(text)})
        if not markers:
            continue
        context = cues[max(0, index - 1) : min(len(cues), index + 2)]
        context_text = " ".join(str(item["text"]) for item in context)
        parsed = parse_live_reference(context_text)
        if parsed is None and not (
            REFERENCE_NUMBER_RE.search(context_text)
            and REFERENCE_STRUCTURE_RE.search(context_text)
        ):
            continue
        core_start = float(cue["start_seconds"])
        core_end = float(cue["end_seconds"])
        range_verse_count = announced_range_verse_count(parsed)
        reading_padding_after = announced_range_padding_after(parsed, padding_after_seconds)
        candidates.append(
            {
                "start_seconds": core_start,
                "end_seconds": core_end,
                "core_start_seconds": core_start,
                "core_end_seconds": core_end,
                "padding_before_seconds": padding_before_seconds,
                "padding_after_seconds": reading_padding_after,
                "sources": ["explicit_address_marker"],
                "markers": markers,
                "references": [parsed.ref] if parsed is not None else [],
                "range_verse_count": range_verse_count,
                "cue_text": text,
            }
        )
    return candidates


def subtitle_text_candidates(
    cues: list[dict[str, object]],
    searcher: BibleTextSearcher,
    *,
    padding_before_seconds: float = TEXT_PADDING_BEFORE_SECONDS,
    padding_after_seconds: float = TEXT_PADDING_AFTER_SECONDS,
) -> list[dict[str, object]]:
    """Find high-confidence Bible-text matches in three consecutive subtitle cues."""
    candidates: list[dict[str, object]] = []
    for index in range(max(0, len(cues) - 2)):
        fragment = cues[index : index + 3]
        text = " ".join(str(cue["text"]) for cue in fragment)
        _lemmas, results = searcher.search(text, limit=1)
        if not results:
            continue
        top = results[0]
        if not (
            top.score >= 85.0
            and top.coverage >= 80.0
            and top.bigram_overlap >= 50.0
            and len(top.matched_lemmas) >= 3
        ):
            continue
        candidates.append(
            {
                "start_seconds": float(fragment[0]["start_seconds"]),
                "end_seconds": float(fragment[-1]["end_seconds"]),
                "core_start_seconds": float(fragment[0]["start_seconds"]),
                "core_end_seconds": float(fragment[-1]["end_seconds"]),
                "padding_before_seconds": padding_before_seconds,
                "padding_after_seconds": padding_after_seconds,
                "sources": ["bible_text_similarity"],
                "matches": [{
                    "reference": top.reference,
                    "score": round(top.score, 3),
                    "coverage": round(top.coverage, 3),
                }],
                "cue_text": text,
            }
        )
    return candidates


def merge_subtitle_window_candidates(
    candidates: list[dict[str, object]],
    *,
    max_core_gap_seconds: float = CANDIDATE_MERGE_GAP_SECONDS,
) -> list[dict[str, object]]:
    """Merge nearby subtitle evidence, then add its audio context once."""
    ordered = sorted(
        (dict(item) for item in candidates),
        key=lambda item: float(item.get("core_start_seconds", item["start_seconds"])),
    )
    merged: list[dict[str, object]] = []
    for candidate in ordered:
        candidate_core_start = float(candidate.get("core_start_seconds", candidate["start_seconds"]))
        candidate_core_end = float(candidate.get("core_end_seconds", candidate["end_seconds"]))
        candidate["core_start_seconds"] = candidate_core_start
        candidate["core_end_seconds"] = candidate_core_end
        candidate["padding_before_seconds"] = float(candidate.get("padding_before_seconds", 0.0))
        candidate["padding_after_seconds"] = float(candidate.get("padding_after_seconds", 0.0))
        candidate_texts = list(candidate.get("cue_texts") or [candidate.get("cue_text") or ""])
        if merged and candidate_core_start <= float(merged[-1]["core_end_seconds"]) + max_core_gap_seconds:
            previous = merged[-1]
            previous["core_end_seconds"] = max(float(previous["core_end_seconds"]), candidate_core_end)
            previous["padding_before_seconds"] = max(
                float(previous["padding_before_seconds"]),
                float(candidate["padding_before_seconds"]),
            )
            previous["padding_after_seconds"] = max(
                float(previous["padding_after_seconds"]),
                float(candidate["padding_after_seconds"]),
            )
            previous["sources"] = sorted(set(previous["sources"]) | set(candidate["sources"]))
            previous["markers"] = sorted(set(previous["markers"]) | set(candidate.get("markers") or []))
            previous["matches"].extend(candidate.get("matches") or [])
            previous["references"] = sorted(
                set(previous.get("references") or []) | set(candidate.get("references") or [])
            )
            previous["cue_texts"].extend(candidate_texts)
            continue
        merged.append(
            {
                "start_seconds": candidate["start_seconds"],
                "end_seconds": candidate["end_seconds"],
                "sources": candidate["sources"],
                "markers": candidate.get("markers") or [],
                "matches": candidate.get("matches") or [],
                "references": candidate.get("references") or [],
                "cue_texts": candidate_texts,
                "core_start_seconds": candidate_core_start,
                "core_end_seconds": candidate_core_end,
                "padding_before_seconds": candidate["padding_before_seconds"],
                "padding_after_seconds": candidate["padding_after_seconds"],
            }
        )
    for window in merged:
        window["start_seconds"] = max(
            0.0,
            float(window["core_start_seconds"]) - float(window["padding_before_seconds"]),
        )
        window["end_seconds"] = (
            float(window["core_end_seconds"]) + float(window["padding_after_seconds"])
        )
    return merged


def subtitle_marker_windows(cues: list[dict[str, object]], *, padding_seconds: float = 45.0) -> list[dict[str, object]]:
    return merge_subtitle_window_candidates(
        subtitle_marker_candidates(
            cues,
            padding_before_seconds=padding_seconds,
            padding_after_seconds=padding_seconds,
        )
    )


def subtitle_control_candidates(
    cues: list[dict[str, object]],
    selected_windows: list[dict[str, object]],
    *,
    limit: int = DEFAULT_CONTROL_WINDOWS_PER_PLAN,
) -> list[dict[str, object]]:
    """Select a few subtitle-backed ordinary-speech windows between candidates."""
    if limit <= 0 or not cues:
        return []
    duration = max(float(cue["end_seconds"]) for cue in cues)
    gaps: list[tuple[float, float]] = []
    previous_end = 0.0
    for window in selected_windows:
        start_seconds = float(window["start_seconds"])
        if start_seconds - previous_end >= CONTROL_WINDOW_SECONDS:
            gaps.append((previous_end, start_seconds))
        previous_end = max(previous_end, float(window["end_seconds"]))
    if duration - previous_end >= CONTROL_WINDOW_SECONDS:
        gaps.append((previous_end, duration))
    controls: list[dict[str, object]] = []
    for gap_start, gap_end in gaps:
        midpoint = (gap_start + gap_end) / 2.0
        speech_cues = [
            cue for cue in cues
            if gap_start <= float(cue["start_seconds"])
            and float(cue["end_seconds"]) <= gap_end
            and not ADDRESS_MARKER_RE.search(str(cue["text"]))
            and len(str(cue["text"]).split()) >= 4
        ]
        if not speech_cues:
            continue
        cue = min(
            speech_cues,
            key=lambda item: abs((float(item["start_seconds"]) + float(item["end_seconds"])) / 2.0 - midpoint),
        )
        cue_middle = (float(cue["start_seconds"]) + float(cue["end_seconds"])) / 2.0
        start_seconds = max(gap_start, cue_middle - CONTROL_WINDOW_SECONDS / 2.0)
        start_seconds = min(start_seconds, gap_end - CONTROL_WINDOW_SECONDS)
        controls.append(
            {
                "start_seconds": round(start_seconds, 3),
                "end_seconds": round(start_seconds + CONTROL_WINDOW_SECONDS, 3),
                "sources": ["plain_speech_control"],
                "cue_text": str(cue["text"]),
            }
        )
    controls.sort(key=lambda item: (float(item["end_seconds"]) - float(item["start_seconds"])), reverse=True)
    return sorted(controls[:limit], key=lambda item: float(item["start_seconds"]))


def write_subtitle_window_plan(
    audio_paths: list[Path],
    subtitle_roots: list[Path],
    output_dir: Path,
    text_detection_db: Path = DEFAULT_TEXT_DETECTION_DB,
    control_windows: int = DEFAULT_CONTROL_WINDOWS_PER_PLAN,
    control_only: bool = False,
) -> list[Path]:
    """Write marker-based candidate windows; this never reads or changes audio."""
    subtitles = collect_subtitle_youtube_ids(subtitle_roots)
    if not text_detection_db.is_file():
        raise ValueError(f"Индекс библейского текста не найден: {text_detection_db}")
    output_dir.mkdir(parents=True, exist_ok=True)
    plans: list[Path] = []
    searcher = BibleTextSearcher(text_detection_db)
    try:
        for audio_path in audio_paths:
            video_ids = youtube_ids_from_path(audio_path)
            matching_ids = [video_id for video_id in video_ids if video_id in subtitles]
            if not matching_ids:
                raise ValueError(f"Не удалось сопоставить аудио и субтитры по YouTube ID: {audio_path}")
            # A temporary parent directory can accidentally look like an ID; the
            # closest matching component belongs to the actual recording path.
            video_id = matching_ids[-1]
            subtitle_path = subtitles.get(video_id)
            if subtitle_path is None:
                raise ValueError(f"Для {video_id} не найдены локальные субтитры.")
            cues = read_timed_subtitle_cues(subtitle_path)
            marker_candidates = subtitle_marker_candidates(cues)
            text_candidates = subtitle_text_candidates(cues, searcher)
            citation_windows = merge_subtitle_window_candidates([*marker_candidates, *text_candidates])
            control_candidates = subtitle_control_candidates(
                cues,
                citation_windows,
                limit=control_windows,
            )
            windows = control_candidates if control_only else sorted(
                [*citation_windows, *control_candidates],
                key=lambda item: float(item["start_seconds"]),
            )
            plan_path = output_dir / f"{video_id}.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "video_id": video_id,
                        "audio": str(audio_path),
                        "subtitle": str(subtitle_path),
                        "selection": (
                            "plain_speech_control"
                            if control_only
                            else "structured_address_markers_and_bible_text_similarity"
                        ),
                        "padding_before_seconds": MARKER_PADDING_BEFORE_SECONDS,
                        "padding_after_seconds": MARKER_PADDING_AFTER_SECONDS,
                        "windows": windows,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            plans.append(plan_path)
            print(
                f"План {video_id}: {len(windows)} объединённых окон "
                f"({len(marker_candidates)} адресных, {len(text_candidates)} текстовых, "
                f"{len(control_candidates)} контрольных) → {plan_path}",
                flush=True,
            )
    finally:
        searcher.close()
    return plans


def window_audio_jobs(
    plan_paths: list[Path],
    output_dir: Path,
    *,
    max_window_seconds: float = MAX_REPLAY_WINDOW_SECONDS,
) -> list[dict[str, object]]:
    """Read saved plans and describe the WAV copies that would be produced.

    Neighbouring planned windows may overlap because each has its own context
    padding.  Replay every moment only once: an overlap at the beginning of a
    new parent window would otherwise restart LiVerse and create a duplicate
    citation from speech that was already replayed in the previous WAV.
    """
    jobs: list[dict[str, object]] = []
    for plan_path in plan_paths:
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Не удалось прочитать план окон {plan_path}: {error}") from error
        video_id = str(plan.get("video_id") or "").strip()
        source_audio = Path(str(plan.get("audio") or "")).expanduser()
        if not source_audio.exists():
            raise ValueError(f"Исходное аудио из плана не найдено: {source_audio}")
        if not video_id:
            raise ValueError(f"В плане нет video_id: {plan_path}")
        if not plan.get("windows"):
            continue
        previous_end_seconds: float | None = None
        for index, window in enumerate(plan["windows"], start=1):
            try:
                requested_start_seconds = float(window["start_seconds"])
                end_seconds = float(window["end_seconds"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"Некорректные границы окна {index} в {plan_path}") from error
            if requested_start_seconds < 0 or end_seconds <= requested_start_seconds:
                raise ValueError(f"Некорректная длительность окна {index} в {plan_path}")
            start_seconds = max(requested_start_seconds, previous_end_seconds or 0.0)
            previous_end_seconds = max(previous_end_seconds or 0.0, end_seconds)
            if start_seconds >= end_seconds:
                continue
            sources = [str(source) for source in window.get("sources") or []]
            kind = "control" if sources == ["plain_speech_control"] else "citation"
            if max_window_seconds <= REPLAY_WINDOW_OVERLAP_SECONDS:
                raise ValueError("Максимальная длина фрагмента должна быть больше перекрытия.")
            part_start = start_seconds
            part_index = 1
            while part_start < end_seconds:
                part_end = min(part_start + max_window_seconds, end_seconds)
                output_path = output_dir / video_id / (
                    f"{index:02d}_{kind}_part{part_index:02d}_"
                    f"{int(part_start):06d}_{int(part_end):06d}.wav"
                )
                jobs.append(
                    {
                        "video_id": video_id,
                        "plan": str(plan_path),
                        "source_audio": str(source_audio),
                        "parent_window_index": index,
                        "planned_start_seconds": requested_start_seconds,
                        "start_seconds": part_start,
                        "end_seconds": part_end,
                        "duration_seconds": part_end - part_start,
                        "sources": sources,
                        "references": sorted({
                            *[str(reference) for reference in window.get("references") or [] if reference],
                            *[
                                str(match.get("reference"))
                                for match in window.get("matches") or []
                                if match.get("reference")
                            ],
                        }),
                        "output_audio": str(output_path),
                    }
                )
                if part_end >= end_seconds:
                    break
                part_start = part_end - REPLAY_WINDOW_OVERLAP_SECONDS
                part_index += 1
    return jobs


def print_window_audio_jobs(jobs: list[dict[str, object]]) -> None:
    total_seconds = sum(float(job["duration_seconds"]) for job in jobs)
    estimated_bytes = int(total_seconds * 16000 * 2)
    print(f"Окон для нарезки: {len(jobs)}", flush=True)
    print(f"Суммарная длительность: {format_timecode(total_seconds)}", flush=True)
    print(f"Оценка места для WAV: {format_size(estimated_bytes)}", flush=True)
    for job in jobs:
        kinds = ", ".join(job["sources"]) or "неизвестный источник"
        print(
            f"  {format_timecode(float(job['start_seconds']))}–"
            f"{format_timecode(float(job['end_seconds']))}  {kinds}\n"
            f"    → {job['output_audio']}",
            flush=True,
        )


def extract_window_audio(jobs: list[dict[str, object]]) -> None:
    """Create 16 kHz mono WAV copies without touching the original recordings."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg не найден; нарезка WAV невозможна.")
    manifests: dict[Path, list[dict[str, object]]] = {}
    for job in jobs:
        output_path = Path(str(job["output_audio"]))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        manifests.setdefault(output_path.parent, []).append(job)
        if output_path.exists() and output_path.stat().st_size > 0:
            print(f"Уже существует, пропускаю: {output_path}", flush=True)
            continue
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
            "-ss", f"{float(job['start_seconds']):.3f}",
            "-t", f"{float(job['duration_seconds']):.3f}",
            "-i", str(job["source_audio"]),
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(output_path),
        ]
        subprocess.run(command, check=True)
        print(f"Создано: {output_path}", flush=True)
    for directory, manifest_jobs in manifests.items():
        (directory / "window_manifest.json").write_text(
            json.dumps({"windows": manifest_jobs}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def iter_subtitle_files(root: Path):
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name in SKIP_DIR_NAMES:
                    continue
                stack.append(entry)
            elif entry.is_file() and entry.suffix.lower() in SUBTITLE_EXTENSIONS:
                yield entry


def youtube_ids_from_text(text: str) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for match in YOUTUBE_URL_RE.finditer(text):
        video_id = match.group(1)
        if video_id not in seen:
            ids.append(video_id)
            seen.add(video_id)
    for match in YOUTUBE_ID_RE.finditer(text):
        video_id = match.group(1)
        if not looks_like_youtube_id(video_id):
            continue
        if video_id not in seen:
            ids.append(video_id)
            seen.add(video_id)
    return ids


def looks_like_youtube_id(video_id: str) -> bool:
    has_digit = any(char.isdigit() for char in video_id)
    has_alpha = any(char.isalpha() for char in video_id)
    has_lower = any(char.islower() for char in video_id)
    has_upper = any(char.isupper() for char in video_id)
    return has_alpha and (has_digit or (has_lower and has_upper))


def youtube_ids_from_path(path: Path) -> list[str]:
    """Extract IDs only from path components that can genuinely identify a video."""
    ids: list[str] = []
    seen: set[str] = set()

    def add(video_id: str) -> None:
        if video_id not in seen and looks_like_youtube_id(video_id):
            ids.append(video_id)
            seen.add(video_id)

    for component in path.parts:
        if len(component) == 11:
            add(component)
    # yt-dlp writes subtitles as e.g. ``VIDEO_ID.ru.vtt``.  Recognise this
    # exact shape, but do not scan arbitrary filenames: an audio name can
    # itself contain an unrelated eleven-character fragment.
    language_suffixed = re.match(
        r"^([A-Za-z0-9_-]{11})\.[a-z]{2,3}(?:-[A-Za-z]+)?\.(?:srt|vtt)$",
        path.name,
        re.IGNORECASE,
    )
    if language_suffixed:
        add(language_suffixed.group(1))
    trailing = re.search(r"(?:^|_)([A-Za-z0-9_-]{11})$", path.stem)
    if trailing:
        add(trailing.group(1))
    return ids


def collect_audio_youtube_ids(search_roots: list[Path], download_dir: Path, include_chunks: bool) -> set[str]:
    return set(collect_audio_youtube_sources(search_roots, download_dir, include_chunks))


def collect_audio_youtube_sources(
    search_roots: list[Path],
    download_dir: Path,
    include_chunks: bool,
) -> dict[str, Path]:
    sources: dict[str, Path] = {}
    for path in collect_audio_files([*search_roots, download_dir], include_chunks):
        for video_id in youtube_ids_from_path(path):
            sources.setdefault(video_id, path)
    return sources


def collect_audio_files_by_youtube_ids(root: Path, video_ids: list[str]) -> list[Path]:
    if not root.exists() or not video_ids:
        return []
    by_id: dict[str, list[Path]] = {video_id: [] for video_id in video_ids}
    for path in collect_audio_files([root], include_chunks=False):
        for video_id in youtube_ids_from_path(path):
            if video_id in by_id:
                by_id[video_id].append(path)
    selected: list[Path] = []
    seen: set[Path] = set()
    for video_id in video_ids:
        for path in by_id.get(video_id) or []:
            resolved = resolved_path(path)
            if resolved in seen:
                continue
            selected.append(path)
            seen.add(resolved)
            break
    return selected


def youtube_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def dedupe_audio_files(files: list[Path]) -> list[Path]:
    best: dict[tuple[Path, str], Path] = {}
    for path in files:
        stem = path.stem
        while stem.endswith("_16k_mono"):
            stem = stem[: -len("_16k_mono")]
        key = (path.parent, stem)
        current = best.get(key)
        if current is None or audio_file_preference(path) < audio_file_preference(current):
            best[key] = path
    return list(best.values())


def audio_file_preference(path: Path) -> tuple[int, int, int]:
    mono_suffix_count = path.stem.count("_16k_mono")
    if path.name.endswith("_16k_mono.wav"):
        return 0, mono_suffix_count, -path.stat().st_size
    if path.suffix.lower() == ".wav":
        return 1, mono_suffix_count, -path.stat().st_size
    return 2, mono_suffix_count, -path.stat().st_size


def print_audio_list(files: list[Path], *, limit: int | None = None) -> None:
    selected = files[:limit] if limit else files
    if not selected:
        print("Аудиофайлы не найдены.", flush=True)
        return
    total_seconds = 0.0
    print("Найденные аудиофайлы:", flush=True)
    for index, path in enumerate(selected, start=1):
        duration = audio_duration(path)
        if duration:
            total_seconds += duration
            duration_text = format_timecode(duration)
        else:
            duration_text = "??:??:??"
        print(
            f"{index:02d}. {duration_text}  {format_size(path.stat().st_size):>9}  {path}",
            flush=True,
        )
    if total_seconds:
        print(f"Итого примерно: {format_timecode(total_seconds)}", flush=True)


def resolved_path(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def collect_processed_audio_files(log_dir: Path) -> set[Path]:
    processed: set[Path] = set()
    if not log_dir.exists():
        return processed
    for session_path in log_dir.glob("*/session.json"):
        try:
            session = json.loads(session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if session.get("mode") != "audio_replay":
            continue
        source_audio = str(session.get("source_audio") or "").strip()
        if not source_audio:
            continue
        processed.add(resolved_path(Path(source_audio)))
    return processed


def skip_processed_audio_files(files: list[Path], processed: set[Path]) -> tuple[list[Path], list[Path]]:
    selected: list[Path] = []
    skipped: list[Path] = []
    for path in files:
        if resolved_path(path) in processed:
            skipped.append(path)
        else:
            selected.append(path)
    return selected, skipped


def download_audio(
    urls: list[str], output_dir: Path, cookies_from_browser: str | None = None
) -> list[Path]:
    if not urls:
        return []
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []
    for url in urls:
        before = {path.resolve() for path in output_dir.glob("*")}
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "-f",
            "bestaudio",
            "-o",
            str(output_dir / "%(title).120s_%(id)s.%(ext)s"),
        ]
        if cookies_from_browser:
            command.extend(("--cookies-from-browser", cookies_from_browser))
        command.append(url)
        try:
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError as error:
            print(
                f"Не удалось скачать аудио: {url} "
                f"(yt-dlp завершился с кодом {error.returncode}). Пропускаю.",
                flush=True,
            )
            continue
        for path in output_dir.glob("*"):
            if path.is_file() and path.resolve() not in before and path.suffix.lower() in AUDIO_EXTENSIONS:
                downloaded.append(path)
    return downloaded


def download_timed_subtitles(video_ids: list[str], output_dir: Path) -> tuple[list[Path], list[str]]:
    """Download Russian YouTube subtitles only; never download media audio."""
    if not video_ids:
        return [], []
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []
    unavailable: list[str] = []
    for video_id in video_ids:
        before = {path.resolve() for path in iter_subtitle_files(output_dir)}
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            "ru",
            "--sub-format",
            "vtt",
            "--no-overwrites",
            "-o",
            str(output_dir / "%(id)s.%(ext)s"),
            youtube_watch_url(video_id),
        ]
        try:
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError:
            unavailable.append(video_id)
            continue
        new_paths = [path.resolve() for path in iter_subtitle_files(output_dir) if path.resolve() not in before]
        if new_paths:
            downloaded.extend(new_paths)
        else:
            unavailable.append(video_id)
    return downloaded, unavailable


def write_latest_replay_batch(log_dir: Path, run_dirs: list[Path]) -> Path | None:
    if not run_dirs:
        return None
    log_dir.mkdir(parents=True, exist_ok=True)
    batch_path = log_dir / LATEST_REPLAY_BATCH
    batch_path.write_text(
        json.dumps(
            {
                "created_at": run_dirs[-1].name,
                "runs": [str(path) for path in run_dirs],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return batch_path


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Повреждён JSONL: {path}:{line_number}: {exc}") from exc
        if isinstance(row, dict):
            rows.append(row)
    return rows


def save_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def replay_case_audio_position(case: dict, cases_path: Path) -> tuple[str, str, float, float, float] | None:
    """Return video/window and source-time position of a replay trigger."""
    source_path = Path(session_source_audio(cases_path))
    match = WINDOW_AUDIO_PART_RE.fullmatch(source_path.name)
    if not match:
        return None
    asr = case.get("asr") if isinstance(case.get("asr"), dict) else {}
    words = asr.get("result") if isinstance(asr.get("result"), list) else []
    if not words or not isinstance(words[0], dict):
        return None
    try:
        part_start = float(match.group("start"))
        part_end = float(match.group("end"))
        first_word = float(words[0].get("start"))
    except (TypeError, ValueError):
        return None
    return (
        source_path.parent.name,
        match.group("window"),
        part_start,
        part_end,
        part_start + first_word,
    )


def replay_case_identity(case: dict) -> tuple[str, tuple[str, ...]]:
    """Return the displayed reference together with list contents when present.

    All reading-list slides share the title ``Ссылки для чтения``.  The title
    alone therefore cannot identify a duplicated replay trigger.
    """
    payload = case.get("payload") if isinstance(case.get("payload"), dict) else {}
    references = payload.get("reference_list") if isinstance(payload.get("reference_list"), list) else []
    listed_refs = tuple(
        str(item.get("ref") or "").strip()
        for item in references
        if isinstance(item, dict) and str(item.get("ref") or "").strip()
    )
    return str(case.get("ref") or "").strip(), listed_refs


def exclude_replay_overlap_duplicates(run_dirs: list[Path]) -> int:
    """Exclude only triggers duplicated by overlapping WAV parts of one window."""
    seen: list[tuple[str, str, tuple[str, tuple[str, ...]], float, float, float]] = []
    excluded = 0
    for run_dir in run_dirs:
        cases_path = run_dir / "trigger_cases.jsonl"
        cases = load_jsonl(cases_path)
        changed = False
        for case in cases:
            if not is_unreviewed_case(case):
                continue
            identity = replay_case_identity(case)
            position = replay_case_audio_position(case, cases_path)
            if not identity[0] or position is None:
                continue
            video, window, start, end, first_word_at = position
            duplicate = False
            for prior_video, prior_window, prior_identity, prior_start, prior_end, prior_first_word_at in seen:
                overlap_start = max(start, prior_start)
                overlap_end = min(end, prior_end)
                if (
                    video == prior_video
                    and window == prior_window
                    and identity == prior_identity
                    and overlap_start <= overlap_end
                    and overlap_start <= first_word_at <= overlap_end
                    and overlap_start <= prior_first_word_at <= overlap_end
                ):
                    duplicate = True
                    break
            if duplicate:
                case["status"] = "reviewed"
                case["review_category"] = "excluded_cascade"
                case["note"] = "Автоматически исключено: повтор в перекрытии соседних WAV-фрагментов."
                changed = True
                excluded += 1
                continue
            seen.append((video, window, identity, start, end, first_word_at))
        if changed:
            save_jsonl(cases_path, cases)
    return excluded


def citation_detection_label(case: dict) -> str:
    payload = case.get("payload") if isinstance(case.get("payload"), dict) else {}
    source = str(payload.get("source") or "")
    return "по тексту" if source in {
        "text_citation", "replay_inferred_sequential_text_reading",
    } else "по адресу"


def citation_summary_lines(
    cases: list[dict],
    bible_path: Path = DEFAULT_BIBLE,
) -> list[str]:
    lines: list[str] = []
    for case in cases:
        ref = str(case.get("ref") or "").strip()
        if not ref:
            continue
        timecode = str(case.get("timecode") or "").strip()
        prefix = f"{timecode}  " if timecode else ""
        line = f"{len(lines) + 1}. {prefix}{ref} — {citation_detection_label(case)}"
        parsed = parse_live_reference(ref, bible_path=bible_path)
        if parsed is not None and parsed.verse_text:
            line += f"\n   Текст: {parsed.verse_text}"
        lines.append(line)
    return lines


def citation_event_groups(cases: list[dict], *, merge_window_seconds: float = 15.0) -> list[dict]:
    """Group overlapping detections that belong to one continuous quotation."""
    groups: list[dict] = []
    ordered = sorted(cases, key=lambda case: float_value(case.get("timecode_seconds")))
    for case in ordered:
        payload = case.get("payload") if isinstance(case.get("payload"), dict) else {}
        book = str(payload.get("book") or "").strip()
        chapter = int(payload.get("chapter") or 0)
        end_chapter = int(payload.get("end_chapter") or chapter)
        start_verse = int(payload.get("start_verse") or 0)
        end_verse = int(payload.get("end_verse") or start_verse)
        timecode_seconds = float_value(case.get("timecode_seconds"))
        ref = str(case.get("ref") or "").strip()
        source = citation_detection_label(case)

        can_merge = False
        if groups:
            previous = groups[-1]
            same_location = bool(
                book
                and book == previous["book"]
                and chapter == previous["chapter"]
                and end_chapter == previous["end_chapter"]
            )
            overlaps = bool(
                start_verse
                and previous["start_verse"]
                and start_verse <= previous["end_verse"]
                and end_verse >= previous["start_verse"]
            )
            same_reference = bool(ref and ref == previous["refs"][-1])
            recent = timecode_seconds - previous["last_time"] <= merge_window_seconds
            can_merge = recent and ((same_location and overlaps) or same_reference)

        if can_merge:
            previous = groups[-1]
            previous["last_time"] = timecode_seconds
            previous["start_verse"] = min(previous["start_verse"], start_verse)
            previous["end_verse"] = max(previous["end_verse"], end_verse)
            previous["refs"].append(ref)
            if source not in previous["sources"]:
                previous["sources"].append(source)
            continue

        groups.append(
            {
                "timecode": str(case.get("timecode") or ""),
                "first_time": timecode_seconds,
                "last_time": timecode_seconds,
                "book": book,
                "chapter": chapter,
                "end_chapter": end_chapter,
                "start_verse": start_verse,
                "end_verse": end_verse,
                "refs": [ref],
                "sources": [source],
            }
        )
    return groups


def citation_event_reference(group: dict) -> str:
    book = str(group.get("book") or "").strip()
    chapter = int(group.get("chapter") or 0)
    start_verse = int(group.get("start_verse") or 0)
    end_verse = int(group.get("end_verse") or start_verse)
    if book and chapter and start_verse:
        suffix = str(start_verse) if end_verse == start_verse else f"{start_verse}-{end_verse}"
        return f"{book} {chapter}:{suffix}"
    refs = [str(ref) for ref in group.get("refs") or [] if str(ref).strip()]
    return refs[0] if refs else ""


def citation_event_summary_lines(cases: list[dict]) -> list[str]:
    lines: list[str] = []
    for index, group in enumerate(citation_event_groups(cases), start=1):
        ref = citation_event_reference(group)
        timecode = str(group.get("timecode") or "").strip()
        sources = " + ".join(group.get("sources") or [])
        merged_count = len(group.get("refs") or [])
        suffix = f"; объединено окон: {merged_count}" if merged_count > 1 else ""
        lines.append(f"{index}. {timecode}  {ref} — {sources}{suffix}")
    return lines


def latest_replay_cases(log_dir: Path) -> list[dict]:
    batch_path = log_dir / LATEST_REPLAY_BATCH
    if not batch_path.exists():
        return []
    try:
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    cases: list[dict] = []
    for run_dir in batch.get("runs") or []:
        cases.extend(load_jsonl(Path(str(run_dir)) / "trigger_cases.jsonl"))
    return cases


def print_latest_citation_summary(log_dir: Path, bible_path: Path = DEFAULT_BIBLE) -> None:
    print("Найденные цитаты:", flush=True)
    cases = latest_replay_cases(log_dir)
    lines = citation_summary_lines(cases, bible_path=bible_path)
    if not lines:
        print("  цитаты не найдены", flush=True)
        return
    for line in lines:
        print(f"  {line}", flush=True)
    event_lines = citation_event_summary_lines(cases)
    print("", flush=True)
    print(
        f"Смысловых цитирований без перекрывающихся повторов: {len(event_lines)} "
        f"(окон распознавания: {len(lines)})",
        flush=True,
    )
    if len(event_lines) < len(lines):
        for line in event_lines:
            print(f"  {line}", flush=True)


def replay_batch_summary_lines(run_dirs: list[Path]) -> list[str]:
    """Describe every detected citation in a replay batch for the operator."""
    lines = ["Итоги эмуляции:"]
    for run_dir in run_dirs:
        source_audio = session_source_audio(run_dir / "trigger_cases.jsonl")
        display_name = Path(source_audio).name if source_audio else run_dir.name
        cases = load_jsonl(run_dir / "trigger_cases.jsonl")
        lines.append("")
        lines.append(f"Файл: {display_name}")
        if not cases:
            lines.append("  Цитаты не обнаружены.")
            continue
        for index, case in enumerate(cases, start=1):
            timecode = str(case.get("timecode") or "время не записано")
            ref = str(case.get("ref") or "ссылка не определена")
            lines.append(f"  {index}. {timecode}  {ref} — {citation_detection_label(case)}")
    return lines


def write_replay_batch_summary(log_dir: Path, run_dirs: list[Path]) -> Path | None:
    if not run_dirs:
        return None
    summary_path = log_dir / f"replay_summary_{run_dirs[-1].name}.txt"
    summary_path.write_text(
        "\n".join(replay_batch_summary_lines(run_dirs)) + "\n",
        encoding="utf-8",
    )
    return summary_path


def is_unreviewed_case(case: dict) -> bool:
    return str(case.get("status") or "unreviewed") == "unreviewed"


def session_source_audio(cases_path: Path) -> str:
    session_path = cases_path.parent / "session.json"
    if not session_path.exists():
        return ""
    try:
        session = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(session.get("source_audio") or "")


def float_value(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def case_signature(case: dict, cases_path: Path) -> tuple[str, str, str, str, str]:
    payload = case.get("payload") if isinstance(case.get("payload"), dict) else {}
    source_audio = session_source_audio(cases_path) or str(case.get("audio") or "")
    timecode = f"{float_value(case.get('timecode_seconds')):.2f}"
    return (
        source_audio,
        timecode,
        str(case.get("ref") or ""),
        str(case.get("vosk_text") or ""),
        str(payload.get("text") or ""),
    )


def trigger_case_files(log_dir: Path) -> list[Path]:
    if not log_dir.exists():
        return []
    return sorted(log_dir.glob("*/trigger_cases.jsonl"), key=lambda path: path.parent.name)


def annotation_history_case_files(log_dir: Path) -> tuple[list[Path], str]:
    """Return all Rodnik batches when replay is running inside that archive."""
    history_root = log_dir.parent.parent
    if log_dir.name == "logs" and history_root.name == "rodnik_replay_batches":
        return (
            sorted(history_root.glob("*/logs/*/trigger_cases.jsonl")),
            "Во всех пачках Родника",
        )
    return trigger_case_files(log_dir), "Всего в логах"


def annotation_stats(cases_paths: list[Path]) -> dict[str, int]:
    total = 0
    reviewed_signatures: set[tuple[str, str, str, str, str]] = set()
    unreviewed_signatures: set[tuple[str, str, str, str, str]] = set()
    files_with_cases = 0

    loaded: list[tuple[Path, list[dict]]] = []
    for cases_path in cases_paths:
        cases = load_jsonl(cases_path)
        if cases:
            files_with_cases += 1
        total += len(cases)
        loaded.append((cases_path, cases))

    for cases_path, cases in loaded:
        for case in cases:
            if not is_unreviewed_case(case):
                reviewed_signatures.add(case_signature(case, cases_path))

    for cases_path, cases in loaded:
        for case in cases:
            if not is_unreviewed_case(case):
                continue
            signature = case_signature(case, cases_path)
            if signature not in reviewed_signatures:
                unreviewed_signatures.add(signature)

    return {
        "files": files_with_cases,
        "total": total,
        "reviewed": len(reviewed_signatures),
        "unreviewed": len(unreviewed_signatures),
    }


def print_annotation_summary(log_dir: Path, run_dirs: list[Path], *, target_annotations: int) -> None:
    history_paths, history_label = annotation_history_case_files(log_dir)
    all_stats = annotation_stats(history_paths)
    batch_paths = [path / "trigger_cases.jsonl" for path in run_dirs if (path / "trigger_cases.jsonl").exists()]
    batch_stats = annotation_stats(batch_paths)
    remaining_to_target = max(0, target_annotations - all_stats["reviewed"])
    available_now = min(all_stats["unreviewed"], remaining_to_target) if remaining_to_target else 0
    shortage_after_review = max(0, remaining_to_target - all_stats["unreviewed"])

    print("", flush=True)
    print("Статистика разметки:", flush=True)
    print(
        f"  Последняя пачка: файлов {batch_stats['files']}, "
        f"срабатываний {batch_stats['total']}, неразмеченных {batch_stats['unreviewed']}",
        flush=True,
    )
    print(
        f"  {history_label}: файлов {all_stats['files']}, "
        f"срабатываний {all_stats['total']}, размечено {all_stats['reviewed']}, "
        f"неразмечено {all_stats['unreviewed']}",
        flush=True,
    )
    print(f"  Цель для оценки risk_score: {target_annotations} размеченных срабатываний", flush=True)
    if remaining_to_target:
        print(f"  Осталось до цели: {remaining_to_target}", flush=True)
        print(f"  Можно разметить сейчас: {available_now}", flush=True)
        if shortage_after_review:
            print(
                f"  После разметки текущих случаев нужно будет найти ещё примерно: {shortage_after_review}",
                flush=True,
            )
    else:
        print("  Цель уже достигнута.", flush=True)


def print_discovered_youtube_ids(
    ids_by_source: dict[str, Path],
    existing_ids: set[str],
    *,
    limit: int = 0,
    offset: int = 0,
) -> list[str]:
    if not ids_by_source:
        print("YouTube ID в файлах субтитров не найдены.", flush=True)
        return []
    missing = [video_id for video_id in ids_by_source if video_id not in existing_ids]
    print(f"Найдено YouTube ID в субтитрах: {len(ids_by_source)}", flush=True)
    if existing_ids:
        print(f"Из них уже есть в аудиофайлах: {len(ids_by_source) - len(missing)}", flush=True)
    if missing:
        selected = missing[offset:]
        if limit:
            selected = selected[:limit]
        if offset or (limit and len(missing) > limit):
            print(
                f"Новые записи для скачивания: {offset + 1}..{offset + len(selected)} из {len(missing)}",
                flush=True,
            )
        else:
            print("Новые записи для скачивания:", flush=True)
        for index, video_id in enumerate(selected, start=1):
            print(f"{index:02d}. {video_id}  {ids_by_source[video_id]}", flush=True)
    else:
        print("Новых записей для скачивания нет.", flush=True)
    return missing


def print_replay_inventory(
    search_roots: list[Path],
    subtitle_roots: list[Path],
    download_dir: Path,
    *,
    include_chunks: bool,
) -> None:
    audio_sources = collect_audio_youtube_sources(search_roots, download_dir, include_chunks)
    subtitle_sources = collect_subtitle_youtube_ids(subtitle_roots)
    both = sorted(set(audio_sources) & set(subtitle_sources))
    audio_without_subtitles = sorted(set(audio_sources) - set(subtitle_sources))
    subtitles_without_audio = sorted(set(subtitle_sources) - set(audio_sources))

    print("Инвентаризация набора Rodnik:", flush=True)
    print(f"  роликов с аудио: {len(audio_sources)}", flush=True)
    print(f"  роликов с субтитрами: {len(subtitle_sources)}", flush=True)
    print(f"  готовы для отбора окон: {len(both)}", flush=True)
    print(f"  аудио без субтитров: {len(audio_without_subtitles)}", flush=True)
    print(f"  субтитры без аудио: {len(subtitles_without_audio)}", flush=True)
    if not audio_without_subtitles:
        return
    print("", flush=True)
    print("Ролики, для которых нужно искать или скачать субтитры:", flush=True)
    for video_id in audio_without_subtitles:
        print(f"  {video_id}  audio={audio_sources[video_id]}", flush=True)


def pcm_chunks(path: Path, sample_rate: int, chunk_bytes: int):
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as audio:
                if audio.getnchannels() == 1 and audio.getsampwidth() == 2 and audio.getframerate() == sample_rate:
                    while True:
                        data = audio.readframes(max(1, chunk_bytes // 2))
                        if not data:
                            break
                        yield data
                    return
        except wave.Error:
            pass

    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg не найден. Он нужен для чтения/конвертации аудиофайлов.")
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(path),
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "pipe:1",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE)
    assert process.stdout is not None
    try:
        while True:
            data = process.stdout.read(chunk_bytes)
            if not data:
                break
            yield data
    finally:
        process.stdout.close()
        return_code = process.wait()
        if return_code != 0:
            raise RuntimeError(f"ffmpeg завершился с кодом {return_code}: {path}")


def replay_audio_file(
    path: Path,
    args: argparse.Namespace,
    model: object,
    grammar: list[str] | None,
    text_searcher: BibleTextSearcher | None,
    session_state: dict[str, object] | None = None,
) -> Path | None:
    logger = JsonlLogger(args.log_dir, enabled=not args.no_log)
    pipeline = LiveReferencePipeline(args.bible, buffer_parts=args.vosk_buffer_parts)
    text_detector = (
        ScriptureTextDetector(text_searcher, event_callback=logger.write)
        if text_searcher is not None
        else None
    )
    replay_state: dict[str, object] = {
        "long_passage": None,
        "smart_slide_shadow": None,
        "sequential_text_reading": None,
    }
    reading_list: list[dict] = []
    context_restored = restore_replay_session_context(pipeline, replay_state, session_state)
    manual_smart_slide = getattr(args, "_smart_slide_context", None)
    if isinstance(manual_smart_slide, dict):
        replay_state["smart_slide_shadow"] = copy.deepcopy(manual_smart_slide)
    if args.asr_engine == "sherpa-0.54":
        recognizer = SherpaReplayRecognizer(model, args.samplerate)
    else:
        recognizer_args = [model, args.samplerate]
        if grammar is not None:
            recognizer_args.append(json.dumps(grammar, ensure_ascii=False))
        recognizer = KaldiRecognizer(*recognizer_args)
        recognizer.SetWords(True)

    audio_path = ""
    audio_log = None
    if logger.run_dir:
        audio_path = str(logger.run_dir / "audio.wav")
        audio_log = wave.open(audio_path, "wb")
        audio_log.setnchannels(1)
        audio_log.setsampwidth(2)
        audio_log.setframerate(args.samplerate)

    logger.write_session(
        {
            "mode": "audio_replay",
            "source_audio": str(path),
            "asr_engine": args.asr_engine,
            "model": str(args.sherpa_model if args.asr_engine == "sherpa-0.54" else args.model),
            "sherpa_threads": (
                args.sherpa_threads if args.asr_engine == "sherpa-0.54" else None
            ),
            "asr_confidence": (
                "derived_from_subword_probabilities"
                if args.asr_engine == "sherpa-0.54"
                else "vosk_word_confidence"
            ),
            "bible": str(args.bible),
            "samplerate": args.samplerate,
            "chunk_bytes": args.chunk_bytes,
            "open_vocabulary": args.open_vocabulary,
            "citation_detection_mode": args.citation_detection_mode,
            "text_operator_hints": bool(args.text_operator_hints),
            "long_range_slide_mode": args.long_range_slide_mode,
            "text_detection_db": str(args.text_detection_db) if text_searcher is not None else None,
            "vosk_buffer_parts": args.vosk_buffer_parts,
            "audio": "audio.wav" if audio_path else "",
            "grammar": None if grammar is None else grammar_diagnostics(grammar),
            "continued_window_context": context_restored,
            "smart_slide_passage": str(getattr(args, "smart_slide_passage", "") or ""),
        }
    )
    if isinstance(manual_smart_slide, dict):
        logger.write(
            "REPLAY_SMART_SLIDE_CONTEXT_SELECTED",
            {
                "passage": str(manual_smart_slide.get("ref") or ""),
                "slide_mode": str(manual_smart_slide.get("slide_mode") or ""),
                "source": "manual_argument",
            },
        )

    audio_bytes_seen = 0
    trigger_case_count = 0
    last_sherpa_partial = ""
    try:
        for data in pcm_chunks(path, args.samplerate, args.chunk_bytes):
            if audio_log:
                audio_log.writeframes(data)
            audio_bytes_seen += len(data)
            replay_seconds = audio_bytes_seen / float(args.samplerate * 2)
            if args.asr_engine == "sherpa-0.54":
                results = recognizer.accept_waveform(data, replay_seconds)
                partial = "" if results else recognizer.partial_result()
                if partial and partial != last_sherpa_partial:
                    handle_replay_smart_slide_partial(
                        partial,
                        replay_seconds,
                        text_detector,
                        replay_state,
                        logger,
                    )
                last_sherpa_partial = partial
            elif recognizer.AcceptWaveform(data):
                results = [json.loads(recognizer.Result())]
            else:
                results = []
            for result in results:
                trigger_case_count += handle_result(
                    result,
                    pipeline,
                    logger,
                    audio_path,
                    replay_seconds,
                    trigger_case_count=trigger_case_count,
                    args=args,
                    text_detector=text_detector,
                    replay_state=replay_state,
                    reading_list=reading_list,
                )

        replay_seconds = audio_bytes_seen / float(args.samplerate * 2)
        if args.asr_engine == "sherpa-0.54":
            final_results = recognizer.final_results()
        else:
            final_results = [json.loads(recognizer.FinalResult())]
        for final_result in final_results:
            if not final_result.get("text"):
                continue
            trigger_case_count += handle_result(
                final_result,
                pipeline,
                logger,
                audio_path,
                replay_seconds,
                trigger_case_count=trigger_case_count,
                args=args,
                text_detector=text_detector,
                replay_state=replay_state,
                reading_list=reading_list,
            )
    finally:
        if audio_log:
            audio_log.close()
        recognizer = None
        save_replay_session_context(pipeline, replay_state, session_state)

    print(f"Готово: {path}", flush=True)
    if logger.run_dir:
        print(f"  Лог: {logger.run_dir}", flush=True)
    print(f"  Срабатываний: {trigger_case_count}", flush=True)
    return logger.run_dir


def handle_result(
    result: dict,
    pipeline: LiveReferencePipeline,
    logger: JsonlLogger,
    audio_path: str,
    replay_seconds: float,
    *,
    trigger_case_count: int,
    args: argparse.Namespace,
    text_detector: ScriptureTextDetector | None,
    replay_state: dict[str, object],
    reading_list: list[dict],
) -> int:
    text = str(result.get("text") or "").strip()
    logger.write("final_raw", {"result": result, "text": text, "replay_seconds": replay_seconds})
    if not text:
        return 0

    address_detection_enabled = args.citation_detection_mode != "text_only"
    long_passage = replay_state.get("long_passage")
    if address_recognition_allowed(address_detection_enabled, bool(long_passage)):
        pipeline_payload = pipeline.process_text(
            text,
            asr_result=result,
            show_candidates=args.show_candidates,
            now_ms=int(replay_seconds * 1000),
        )
    else:
        pipeline_payload = {
            "text": text,
            "matched": False,
            "parsed": None,
            "source": "text_only",
        }

    incomplete_reference = pipeline_payload.get("incomplete_reference")
    if incomplete_reference:
        replay_state["incomplete_reference"] = incomplete_reference
        logger.write(
            "INCOMPLETE_REFERENCE",
            {"reference": incomplete_reference, "replay_seconds": replay_seconds},
        )

    text_detection_for_high_risk_address = False
    if text_detector is not None and pipeline_payload.get("matched"):
        explicit_ref = str((pipeline_payload.get("parsed") or {}).get("ref") or "")
        # A high-risk spoken address may have lost a range boundary.  Keep the
        # displayed verse in the duplicate guard, but let the following Bible
        # text immediately widen or correct it when the evidence is stronger.
        text_detection_for_high_risk_address = pipeline_payload.get("risk_level") == "high"
        if text_detection_for_high_risk_address:
            text_detector.mark_shown(explicit_ref, replay_seconds)
        else:
            text_detector.suppress_after_address(explicit_ref, replay_seconds)

    text_decision = None
    if text_detector is not None and (
        not pipeline_payload.get("matched") or text_detection_for_high_risk_address
    ):
        text_decision = text_detector.process_fragment(
            text,
            replay_seconds,
            incomplete_address_correction=(
                replay_state.get("incomplete_reference") is not None
            ),
        )
        if replay_state.get("incomplete_reference") is not None:
            if text_decision.reason == "text_corrected_incomplete_address":
                logger.write(
                    "INCOMPLETE_REFERENCE_TEXT_CORRECTED",
                    {
                        "incomplete_reference": replay_state["incomplete_reference"],
                        "reference": text_decision.reference,
                        "score": round(text_decision.score, 3),
                        "margin": round(text_decision.margin, 3),
                        "matched_words": text_decision.matched_words,
                    },
                )
            if text_decision.accepted:
                replay_state["incomplete_reference"] = None

    if long_passage is not None:
        extend_open_ended_replay_passage(
            long_passage,
            replay_state.get("smart_slide_shadow"),
            text_decision,
        )
        range_action = replay_long_passage_match(text_decision, long_passage)
        logger.write(
            "REPLAY_LONG_PASSAGE",
            {
                **range_action,
                "passage": long_passage,
                "replay_seconds": replay_seconds,
            },
        )
        if range_action["completed"]:
            replay_state["long_passage"] = None
            if text_detector is not None:
                text_detector.clear()
        payload = add_slide_payload(pipeline_payload)
    elif text_decision is not None and text_decision.accepted:
        payload = text_citation_payload(text_decision, text)
    else:
        payload = add_slide_payload(pipeline_payload)
    accumulate_reading_list(payload, reading_list)

    if (
        long_passage is None
        and args.text_operator_hints
        and text_decision is not None
    ):
        operator_hint_payload = text_operator_hint_payload(text_decision, text)
        if operator_hint_payload is not None:
            hint_slide = operator_hint_payload["slide"]
            logger.write(
                "TEXT_OPERATOR_HINT",
                {
                    "reference": hint_slide.get("ref"),
                    "score": hint_slide.get("score"),
                    "margin": round(text_decision.margin, 3),
                    "matched_words": text_decision.matched_words,
                    "window": text_decision.window_text,
                    "replay_seconds": replay_seconds,
                },
            )
            payload = operator_hint_payload

    smart_slide_shadow = replay_state.get("smart_slide_shadow")
    if (
        smart_slide_shadow is not None
        and text_detector is not None
        and text_decision is not None
    ):
        shadow_decision, sequence_decision = decide_sequence_progress_from_text(
            smart_slide_shadow,
            text_decision,
            lambda state: text_detector.evaluate_known_sequence(state, replay_seconds),
        )
        shadow_decision = defer_open_ended_replay_completion(
            smart_slide_shadow,
            shadow_decision,
        )
        logger.write(
            "SMART_SLIDE_SHADOW",
            {
                **shadow_decision,
                "passage": str(smart_slide_shadow.get("ref") or ""),
                "slide_mode": str(smart_slide_shadow.get("slide_mode") or ""),
                "window": (
                    sequence_decision.window_text
                    if shadow_decision.get("evidence_source") == "sequence_scoped"
                    else str(getattr(text_decision, "window_text", "") or "")
                ),
                "replay_seconds": replay_seconds,
            },
        )
        if not apply_replay_smart_slide_decision(smart_slide_shadow, shadow_decision):
            replay_state["smart_slide_shadow"] = None

    inferred_chapter_reading = infer_replay_chapter_reading(payload)
    if inferred_chapter_reading is not None:
        payload = inferred_chapter_reading
    inferred_sequential_reading = None
    if long_passage is None and inferred_chapter_reading is None:
        inferred_sequential_reading = infer_replay_sequential_text_reading(
            payload, replay_state, replay_seconds
        )
        if inferred_sequential_reading is not None:
            payload = inferred_sequential_reading
    accepted_passage = replay_long_passage(payload)
    if (
        text_detector is not None
        and accepted_passage is not None
        and (
            pipeline_payload.get("matched")
            or bool((payload.get("text_citation") or {}).get("announced_range_expanded"))
            or inferred_sequential_reading is not None
        )
    ):
        if pipeline.set_context_range(payload.get("slide")):
            if inferred_sequential_reading is not None:
                accepted_passage["open_ended"] = True
            replay_state["long_passage"] = accepted_passage
            replay_state["smart_slide_shadow"] = replay_smart_slide_state(
                payload,
                args.long_range_slide_mode,
            )
            if inferred_sequential_reading is not None:
                smart_slide_shadow = replay_state.get("smart_slide_shadow")
                if isinstance(smart_slide_shadow, dict):
                    smart_slide_shadow["open_ended"] = True
                    targets = smart_slide_shadow.get("targets")
                    if isinstance(targets, list) and targets:
                        # Earlier text has already established the current
                        # verse.  Start there, rather than flashing a title or
                        # replaying verses the congregation just heard.
                        smart_slide_shadow["current_index"] = len(targets) - 1
                        smart_slide_shadow["current_slide_visible"] = True
            logger.write(
                "REPLAY_CONTEXT_RANGE_SELECTED",
                {"passage": accepted_passage, "replay_seconds": replay_seconds},
            )
            if inferred_chapter_reading is not None:
                logger.write(
                    "REPLAY_INFERRED_CHAPTER_READING_SELECTED",
                    {"passage": accepted_passage, "replay_seconds": replay_seconds},
                )
            if inferred_sequential_reading is not None:
                logger.write(
                    "REPLAY_INFERRED_SEQUENTIAL_TEXT_READING_SELECTED",
                    {"passage": accepted_passage, "replay_seconds": replay_seconds},
                )
    replay_operator_hint = (
        str((payload.get("slide") or {}).get("source") or "") == "text_operator_hint"
    )
    output = {
        "replay": {
            "enabled": True,
            "sent": bool(payload.get("slide")) and not replay_operator_hint,
            "operator_hint": replay_operator_hint,
        }
    }
    payload["output"] = output
    logger.write(
        "parsed",
        {
            "vosk_text": text,
            "vosk_buffer": list(payload.get("vosk_buffer") or []),
            "candidate_texts": list(payload.get("candidate_texts") or []),
            "payload": payload_summary(payload),
            "output": output,
        },
    )
    if not payload.get("slide"):
        return 0

    case_number = trigger_case_count + 1
    parsed = payload.get("parsed") or {}
    slide = payload.get("slide") or {}
    ref = str(parsed.get("ref") or slide.get("ref") or "")
    time_info = trigger_time_info(result, replay_seconds)
    logger.write_trigger_case(
        {
            "case_id": f"trigger_{case_number:04d}",
            "status": "unreviewed",
            "review_category": "",
            "audio": audio_path,
            **time_info,
            "action": "replay",
            "ref": ref,
            "vosk_text": text,
            "vosk_buffer": list(payload.get("vosk_buffer") or []),
            "payload": payload_summary(payload),
            "output": output,
            "asr": result,
            "note": "",
        }
    )
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay LiVerse over saved sermon audio files.")
    parser.add_argument("--search-root", action="append", type=Path, help="Directory to search for audio files.")
    parser.add_argument(
        "--subtitle-root",
        action="append",
        type=Path,
        help="Directory to search for .srt/.txt files with YouTube IDs.",
    )
    parser.add_argument(
        "--inventory",
        action="store_true",
        help="Print local audio/subtitle coverage by YouTube video ID without downloading or replaying.",
    )
    parser.add_argument(
        "--plan-subtitle-windows",
        action="store_true",
        help="Write candidate windows from timed local subtitles; does not process audio.",
    )
    parser.add_argument(
        "--window-plan-dir",
        type=Path,
        default=DEFAULT_WINDOW_PLAN_DIR,
        help="Directory for --plan-subtitle-windows JSON plans.",
    )
    parser.add_argument(
        "--control-windows",
        type=int,
        default=DEFAULT_CONTROL_WINDOWS_PER_PLAN,
        help="Number of ordinary-speech control windows per subtitle plan (default: 3).",
    )
    parser.add_argument(
        "--control-only",
        action="store_true",
        help="Write only ordinary-speech control windows for a separate false-positive audit.",
    )
    parser.add_argument(
        "--extract-window-plan",
        action="append",
        type=Path,
        help="Create WAV copies from one or more saved subtitle-window plans.",
    )
    parser.add_argument(
        "--window-audio-dir",
        type=Path,
        default=DEFAULT_WINDOW_AUDIO_DIR,
        help="Directory for WAV copies created by --extract-window-plan.",
    )
    parser.add_argument(
        "--replay-window-plan",
        action="append",
        type=Path,
        help="Replay all WAV copies belonging to one or more extracted window plans.",
    )
    parser.add_argument("--audio", action="append", type=Path, help="Specific audio file to replay.")
    parser.add_argument("--include-chunks", action="store_true", help="Include *_chunks directories in auto search.")
    parser.add_argument("--limit", type=int, default=0, help="Limit auto-selected files.")
    parser.add_argument("--offset", type=int, default=0, help="Skip this many auto-selected files before --limit.")
    parser.add_argument("--run", action="store_true", help="Actually run replay. Without this, only list files.")
    parser.add_argument("--download-url", action="append", default=[], help="YouTube URL to download before replay.")
    parser.add_argument(
        "--cookies-from-browser",
        help="Use cookies from a named browser profile only for this download, e.g. chrome.",
    )
    parser.add_argument(
        "--download-from-subtitles",
        action="store_true",
        help="Find YouTube IDs in .srt/.txt files and download missing audio before replay.",
    )
    parser.add_argument(
        "--download-subtitles",
        action="store_true",
        help="Download only missing Russian YouTube VTT subtitles for local audio; requires --run.",
    )
    parser.add_argument(
        "--subtitle-download-dir",
        type=Path,
        default=Path(".cache") / "liverse" / "replay_subtitles",
        help="Where --download-subtitles stores downloaded VTT files.",
    )
    parser.add_argument(
        "--include-processed",
        action="store_true",
        help="Include audio files that already have an audio_replay session log.",
    )
    parser.add_argument(
        "--download-dir",
        type=Path,
        default=Path(".cache") / "liverse" / "replay_audio",
        help="Where downloaded audio files are stored.",
    )
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument(
        "--asr-engine",
        choices=["vosk-0.22", "sherpa-0.54"],
        default="vosk-0.22",
        help="Speech engine used for replay; normal LiVerse remains on vosk-0.22.",
    )
    parser.add_argument("--sherpa-model", type=Path, default=DEFAULT_SHERPA_MODEL_PATH)
    parser.add_argument("--sherpa-threads", type=int, default=DEFAULT_SHERPA_THREADS)
    parser.add_argument("--bible", type=Path, default=DEFAULT_BIBLE)
    parser.add_argument("--samplerate", type=int, default=16000)
    parser.add_argument("--chunk-bytes", type=int, default=8000)
    parser.add_argument("--open-vocabulary", action="store_true")
    parser.add_argument(
        "--citation-detection-mode",
        choices=["address_only", "text_only", "hybrid_auto", "hybrid_confirm"],
        default="address_only",
        help="Use the same address/text citation channels as the normal LiVerse launch.",
    )
    parser.add_argument(
        "--text-detection-db",
        type=Path,
        default=DEFAULT_TEXT_DETECTION_DB,
        help="SQLite Bible text index used outside address_only mode.",
    )
    parser.add_argument(
        "--long-range-slide-mode",
        choices=("compact", "one_verse"),
        default="compact",
        help="Slide layout whose automatic transitions SMART_SLIDE_SHADOW evaluates.",
    )
    parser.add_argument(
        "--text-operator-hints",
        action="store_true",
        help="Record web-operator proposals for useful weak text matches; never send them as slides.",
    )
    parser.add_argument(
        "--smart-slide-passage",
        help=(
            "Replay-only Bible range for SMART_SLIDE_SHADOW when its spoken announcement "
            "is outside the WAV, for example 'Колоссянам 3:5-7'."
        ),
    )
    parser.add_argument("--vosk-buffer-parts", type=int, default=3)
    parser.add_argument("--vosk-log-level", type=int, default=-1)
    parser.add_argument("--show-candidates", action="store_true")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--no-log", action="store_true")
    parser.add_argument(
        "--results-only",
        action="store_true",
        help="Print citations from the latest saved replay without recognizing audio again.",
    )
    parser.add_argument(
        "--target-annotations",
        type=int,
        default=DEFAULT_TARGET_ANNOTATIONS,
        help="Target number of reviewed trigger cases for risk_score analysis.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.smart_slide_passage:
        try:
            args._smart_slide_context = manual_smart_slide_state(
                args.smart_slide_passage,
                bible_path=args.bible,
                slide_mode=args.long_range_slide_mode,
            )
        except ValueError as error:
            raise SystemExit(str(error)) from error
    if args.results_only:
        print_latest_citation_summary(args.log_dir, bible_path=args.bible)
        return 0
    search_roots = args.search_root or list(DEFAULT_SEARCH_ROOTS)
    subtitle_roots = args.subtitle_root or list(DEFAULT_SUBTITLE_ROOTS)
    if args.subtitle_download_dir not in subtitle_roots:
        subtitle_roots = [*subtitle_roots, args.subtitle_download_dir]
    if args.inventory:
        print_replay_inventory(
            search_roots,
            subtitle_roots,
            args.download_dir,
            include_chunks=args.include_chunks,
        )
        return 0
    if args.download_subtitles:
        if args.audio:
            audio_sources = {}
            for path in args.audio:
                for video_id in youtube_ids_from_path(path):
                    audio_sources.setdefault(video_id, path)
        else:
            audio_sources = collect_audio_youtube_sources(
                search_roots,
                args.download_dir,
                args.include_chunks,
            )
        subtitle_sources = collect_timed_subtitle_youtube_ids(subtitle_roots)
        missing_ids = sorted(set(audio_sources) - set(subtitle_sources))
        print(f"Аудиозаписей без субтитров с таймкодами: {len(missing_ids)}", flush=True)
        for video_id in missing_ids:
            print(f"  {video_id}  {youtube_watch_url(video_id)}", flush=True)
        if not args.run:
            print("Это только список. Для докачки одних субтитров добавьте --run.", flush=True)
            return 0
        downloaded, unavailable = download_timed_subtitles(missing_ids, args.subtitle_download_dir)
        print(f"Скачано VTT-файлов: {len(downloaded)}", flush=True)
        if unavailable:
            print("Не найдены или не скачаны русские субтитры:", flush=True)
            for video_id in unavailable:
                print(f"  {video_id}", flush=True)
        return 0
    if args.plan_subtitle_windows:
        audio_paths = list(args.audio or [])
        if not audio_paths:
            raise SystemExit("Для --plan-subtitle-windows укажите хотя бы один --audio файл.")
        missing_audio = [path for path in audio_paths if not path.exists()]
        if missing_audio:
            raise SystemExit("Указанные --audio файлы не найдены:\n" + "\n".join(map(str, missing_audio)))
        try:
            write_subtitle_window_plan(
                audio_paths,
                subtitle_roots,
                args.window_plan_dir,
                text_detection_db=args.text_detection_db,
                control_windows=max(0, args.control_windows),
                control_only=args.control_only,
            )
        except (OSError, ValueError) as error:
            raise SystemExit(str(error)) from error
        return 0
    if args.extract_window_plan:
        try:
            jobs = window_audio_jobs(list(args.extract_window_plan), args.window_audio_dir)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        print_window_audio_jobs(jobs)
        if not args.run:
            print("Это только предварительный просмотр. Для нарезки добавьте --run.", flush=True)
            return 0
        try:
            extract_window_audio(jobs)
        except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
            raise SystemExit(f"Нарезка не завершена: {error}") from error
        return 0
    planned_audio: list[Path] = []
    planned_session_keys: dict[Path, str] = {}
    if args.replay_window_plan:
        try:
            jobs = window_audio_jobs(list(args.replay_window_plan), args.window_audio_dir)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        planned_audio = [Path(str(job["output_audio"])) for job in jobs]
        planned_session_keys = {
            Path(str(job["output_audio"])).resolve(): (
                f"{Path(str(job['plan'])).resolve()}:{int(job['parent_window_index'])}"
            )
            for job in jobs
        }
        missing_planned_audio = [path for path in planned_audio if not path.is_file()]
        if missing_planned_audio:
            missing_list = "\n".join(f"  - {path}" for path in missing_planned_audio)
            raise SystemExit(
                "WAV-фрагменты ещё не созданы. Сначала выполните "
                "--extract-window-plan ... --run:\n" + missing_list
            )
        print(f"Фрагментов из плана для replay: {len(planned_audio)}", flush=True)
        if not planned_audio:
            print("В выбранных планах нет фрагментов для replay.", flush=True)
            return 0
    download_urls = list(args.download_url)
    download_video_ids: list[str] = []
    if args.download_from_subtitles:
        if args.download_dir not in search_roots:
            search_roots = [*search_roots, args.download_dir]
        ids_by_source = collect_subtitle_youtube_ids(subtitle_roots)
        existing_ids = collect_audio_youtube_ids(search_roots, args.download_dir, args.include_chunks)
        missing_ids = print_discovered_youtube_ids(
            ids_by_source,
            existing_ids,
            limit=args.limit,
            offset=args.offset,
        )
        if args.offset:
            missing_ids = missing_ids[args.offset :]
        if args.limit:
            missing_ids = missing_ids[: args.limit]
        if not args.run:
            print("", flush=True)
            print("Это был только список новых записей для скачивания. Для скачивания и replay добавьте --run.", flush=True)
            return 0
        if args.run:
            download_video_ids = list(missing_ids)
            download_urls.extend(youtube_watch_url(video_id) for video_id in missing_ids)
    downloaded = download_audio(download_urls, args.download_dir, args.cookies_from_browser)
    if args.download_from_subtitles and args.run and download_video_ids:
        downloaded = collect_audio_files_by_youtube_ids(args.download_dir, download_video_ids)
    explicit_audio = [*planned_audio, *(args.audio or [])]
    missing_explicit_audio = [path for path in explicit_audio if not path.exists()]
    if missing_explicit_audio:
        missing_list = "\n".join(f"  - {path}" for path in missing_explicit_audio)
        raise SystemExit(f"Указанные --audio файлы не найдены:\n{missing_list}")
    files = list(explicit_audio)
    auto_selected = not explicit_audio
    selected_downloaded = False
    if not files:
        if args.download_from_subtitles and args.run and download_video_ids:
            files = list(downloaded)
            selected_downloaded = True
        else:
            files = collect_audio_files(search_roots, args.include_chunks)
    if downloaded and not selected_downloaded:
        files.extend(downloaded)
    skipped_processed: list[Path] = []
    if auto_selected and not args.include_processed:
        processed = collect_processed_audio_files(args.log_dir)
        files, skipped_processed = skip_processed_audio_files(files, processed)
    if auto_selected and args.offset and not selected_downloaded:
        files = files[args.offset :]
    if args.limit:
        files = files[: args.limit]

    if skipped_processed:
        print(
            f"Пропущено уже обработанных файлов: {len(skipped_processed)} "
            f"(для повторного прогона добавьте --include-processed).",
            flush=True,
        )
    print_audio_list(files)
    if not args.run:
        print("", flush=True)
        print("Это был только список. Для запуска добавьте --run.", flush=True)
        print("Пример: .venv/bin/python tools/replay_audio_files.py --limit 1 --run", flush=True)
        return 0
    if not files:
        raise SystemExit("Нет аудиофайлов для replay.")

    if args.asr_engine == "sherpa-0.54":
        model = load_sherpa_recognizer(
            args.sherpa_model,
            sample_rate=args.samplerate,
            num_threads=args.sherpa_threads,
        )
    else:
        SetLogLevel(args.vosk_log_level)
        model = Model(str(args.model))
    text_detection_enabled = args.citation_detection_mode != "address_only"
    grammar = None if (args.open_vocabulary or text_detection_enabled) else build_grammar()
    text_searcher = None
    if text_detection_enabled:
        text_searcher = BibleTextSearcher(args.text_detection_db)
    run_dirs: list[Path] = []
    window_sessions: dict[str, dict[str, object]] = {}
    try:
        for index, path in enumerate(files, start=1):
            print(f"\nReplay {index}/{len(files)}: {path}", flush=True)
            session_key = planned_session_keys.get(path.resolve())
            session_state = window_sessions.setdefault(session_key, {}) if session_key else None
            run_dir = replay_audio_file(
                path,
                args,
                model,
                grammar,
                text_searcher,
                session_state=session_state,
            )
            if run_dir:
                run_dirs.append(run_dir)
    finally:
        if text_searcher is not None:
            text_searcher.close()
    overlap_duplicates = exclude_replay_overlap_duplicates(run_dirs)
    if overlap_duplicates:
        print(
            f"Автоматически исключено повторов из перекрытия WAV-фрагментов: {overlap_duplicates}",
            flush=True,
        )
    batch_path = write_latest_replay_batch(args.log_dir, run_dirs)
    summary_path = write_replay_batch_summary(args.log_dir, run_dirs)
    print_annotation_summary(
        args.log_dir,
        run_dirs,
        target_annotations=max(0, args.target_annotations),
    )
    if batch_path:
        print("", flush=True)
        print(f"Последняя пачка replay: {batch_path}", flush=True)
        if summary_path:
            print(f"Итоги эмуляции сохранены: {summary_path}", flush=True)
            for line in replay_batch_summary_lines(run_dirs):
                print(line, flush=True)
        print(
            ".venv/bin/python tools/review_trigger_cases.py --latest-batch",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
