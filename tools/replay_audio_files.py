#!/usr/bin/env python3
"""Replay LiVerse recognition against saved sermon audio files."""

from __future__ import annotations

import argparse
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
from bible_parser_core.parser import DEFAULT_BIBLE, parse_live_reference
from bible_parser_core.sherpa_streaming import (
    DEFAULT_SHERPA_THREADS,
    SherpaReplayRecognizer,
    load_sherpa_recognizer,
    sherpa_result_to_vosk_result,
)
from bible_parser_core.text_citation_detector import ScriptureTextDetector
from bible_parser_core.verse_text_search import CANONICAL_BOOK_NAMES_BY_ID
from tools.holyrics import scripture_range
from tools.vosk_grammar_probe import (
    DEFAULT_LOG_DIR,
    DEFAULT_MODEL_PATH,
    DEFAULT_TEXT_DETECTION_DB,
    JsonlLogger,
    add_slide_payload,
    address_recognition_allowed,
    format_timecode,
    payload_summary,
    text_citation_payload,
    text_decision_ready_for_scripture_range,
    trigger_time_info,
)


AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".opus", ".ogg", ".flac", ".webm", ".mp4"}
SUBTITLE_EXTENSIONS = {".srt", ".txt"}
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
LATEST_REPLAY_BATCH = "latest_replay_batch.json"
DEFAULT_TARGET_ANNOTATIONS = 200
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


def replay_long_passage(payload: dict) -> dict | None:
    """Represent a long passage that the replay assumes the operator accepted."""
    selected = scripture_range(payload.get("parsed") or {})
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


def replay_long_passage_match(decision: object, passage: dict) -> dict:
    """Check whether Bible-text recognition has reached the passage's last verse."""
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
            for video_id in youtube_ids_from_text(path.stem):
                ids.setdefault(video_id, path)
            if path.name.lower().endswith(".url.txt"):
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    text = ""
                for video_id in youtube_ids_from_text(text):
                    ids.setdefault(video_id, path)
    return ids


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


def collect_audio_youtube_ids(search_roots: list[Path], download_dir: Path, include_chunks: bool) -> set[str]:
    audio_files = collect_audio_files([*search_roots, download_dir], include_chunks)
    ids: set[str] = set()
    for path in audio_files:
        ids.update(youtube_ids_from_text(path.stem))
    return ids


def collect_audio_files_by_youtube_ids(root: Path, video_ids: list[str]) -> list[Path]:
    if not root.exists() or not video_ids:
        return []
    by_id: dict[str, list[Path]] = {video_id: [] for video_id in video_ids}
    for path in collect_audio_files([root], include_chunks=False):
        for video_id in youtube_ids_from_text(path.stem):
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


def download_audio(urls: list[str], output_dir: Path) -> list[Path]:
    if not urls:
        return []
    if not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp не найден. Установите yt-dlp или положите аудиофайлы вручную.")
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []
    for url in urls:
        before = {path.resolve() for path in output_dir.glob("*")}
        command = [
            "yt-dlp",
            "-f",
            "bestaudio",
            "-o",
            str(output_dir / "%(title).120s_%(id)s.%(ext)s"),
            url,
        ]
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


def citation_detection_label(case: dict) -> str:
    payload = case.get("payload") if isinstance(case.get("payload"), dict) else {}
    return "по тексту" if payload.get("source") == "text_citation" else "по адресу"


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
    all_stats = annotation_stats(trigger_case_files(log_dir))
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
        f"  Всего в логах: файлов {all_stats['files']}, "
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
) -> Path | None:
    logger = JsonlLogger(args.log_dir, enabled=not args.no_log)
    pipeline = LiveReferencePipeline(args.bible, buffer_parts=args.vosk_buffer_parts)
    text_detector = (
        ScriptureTextDetector(text_searcher, event_callback=logger.write)
        if text_searcher is not None
        else None
    )
    replay_state: dict[str, dict | None] = {"long_passage": None}
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
            "text_detection_db": str(args.text_detection_db) if text_searcher is not None else None,
            "vosk_buffer_parts": args.vosk_buffer_parts,
            "audio": "audio.wav" if audio_path else "",
            "grammar": None if grammar is None else grammar_diagnostics(grammar),
        }
    )

    audio_bytes_seen = 0
    trigger_case_count = 0
    try:
        for data in pcm_chunks(path, args.samplerate, args.chunk_bytes):
            if audio_log:
                audio_log.writeframes(data)
            audio_bytes_seen += len(data)
            replay_seconds = audio_bytes_seen / float(args.samplerate * 2)
            if args.asr_engine == "sherpa-0.54":
                results = recognizer.accept_waveform(data, replay_seconds)
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
            )
    finally:
        if audio_log:
            audio_log.close()
        recognizer = None

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
    replay_state: dict[str, dict | None],
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

    if text_detector is not None and pipeline_payload.get("matched"):
        explicit_ref = str((pipeline_payload.get("parsed") or {}).get("ref") or "")
        text_detector.suppress_after_address(explicit_ref, replay_seconds)

    text_decision = None
    if text_detector is not None and not pipeline_payload.get("matched"):
        text_decision = text_detector.process_fragment(text, replay_seconds)

    if long_passage is not None:
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

    accepted_passage = replay_long_passage(payload)
    if (
        text_detector is not None
        and accepted_passage is not None
        and pipeline_payload.get("matched")
    ):
        if pipeline.set_context_range(payload.get("slide")):
            replay_state["long_passage"] = accepted_passage
            logger.write(
                "REPLAY_CONTEXT_RANGE_SELECTED",
                {"passage": accepted_passage, "replay_seconds": replay_seconds},
            )
    output = {"replay": {"enabled": True, "sent": bool(payload.get("slide"))}}
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
    parser.add_argument("--audio", action="append", type=Path, help="Specific audio file to replay.")
    parser.add_argument("--include-chunks", action="store_true", help="Include *_chunks directories in auto search.")
    parser.add_argument("--limit", type=int, default=0, help="Limit auto-selected files.")
    parser.add_argument("--offset", type=int, default=0, help="Skip this many auto-selected files before --limit.")
    parser.add_argument("--run", action="store_true", help="Actually run replay. Without this, only list files.")
    parser.add_argument("--download-url", action="append", default=[], help="YouTube URL to download before replay.")
    parser.add_argument(
        "--download-from-subtitles",
        action="store_true",
        help="Find YouTube IDs in .srt/.txt files and download missing audio before replay.",
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
    if args.results_only:
        print_latest_citation_summary(args.log_dir, bible_path=args.bible)
        return 0
    search_roots = args.search_root or list(DEFAULT_SEARCH_ROOTS)
    subtitle_roots = args.subtitle_root or [PROJECTS_ROOT]
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
    downloaded = download_audio(download_urls, args.download_dir)
    if args.download_from_subtitles and args.run and download_video_ids:
        downloaded = collect_audio_files_by_youtube_ids(args.download_dir, download_video_ids)
    explicit_audio = list(args.audio or [])
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
    try:
        for index, path in enumerate(files, start=1):
            print(f"\nReplay {index}/{len(files)}: {path}", flush=True)
            run_dir = replay_audio_file(path, args, model, grammar, text_searcher)
            if run_dir:
                run_dirs.append(run_dir)
    finally:
        if text_searcher is not None:
            text_searcher.close()
    batch_path = write_latest_replay_batch(args.log_dir, run_dirs)
    print_annotation_summary(
        args.log_dir,
        run_dirs,
        target_annotations=max(0, args.target_annotations),
    )
    if batch_path:
        print("", flush=True)
        print(f"Последняя пачка replay: {batch_path}", flush=True)
        print(
            ".venv/bin/python tools/review_trigger_cases.py --latest-batch",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
