#!/usr/bin/env python3
"""Replay LiVerse recognition against saved sermon audio files."""

from __future__ import annotations

import argparse
import json
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

from bible_parser_core.live_pipeline import LiveReferencePipeline, build_grammar, grammar_diagnostics
from bible_parser_core.parser import DEFAULT_BIBLE
from tools.vosk_grammar_probe import (
    DEFAULT_LOG_DIR,
    DEFAULT_MODEL_PATH,
    JsonlLogger,
    add_slide_payload,
    format_timecode,
    payload_summary,
    trigger_time_info,
)


AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".opus", ".ogg", ".flac", ".webm", ".mp4"}
DEFAULT_SEARCH_ROOTS = (
    PROJECTS_ROOT / "bible_parser_cli" / ".cache" / "whisper_runs",
    PROJECTS_ROOT / "live_scripture_presenter" / ".cache" / "live_case_replay" / "audio",
    PROJECTS_ROOT / "liveverse-public-release" / ".cache" / "live_emulator" / "audio",
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
            "bestaudio/best",
            "-o",
            str(output_dir / "%(title).120s_%(id)s.%(ext)s"),
            url,
        ]
        subprocess.run(command, check=True)
        for path in output_dir.glob("*"):
            if path.is_file() and path.resolve() not in before and path.suffix.lower() in AUDIO_EXTENSIONS:
                downloaded.append(path)
    return downloaded


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


def replay_audio_file(path: Path, args: argparse.Namespace, model: Model, grammar: list[str] | None) -> Path | None:
    logger = JsonlLogger(args.log_dir, enabled=not args.no_log)
    pipeline = LiveReferencePipeline(args.bible, buffer_parts=args.vosk_buffer_parts)
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
            "model": str(args.model),
            "bible": str(args.bible),
            "samplerate": args.samplerate,
            "chunk_bytes": args.chunk_bytes,
            "open_vocabulary": args.open_vocabulary,
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
            if recognizer.AcceptWaveform(data):
                result = json.loads(recognizer.Result())
                trigger_case_count += handle_result(
                    result,
                    pipeline,
                    logger,
                    audio_path,
                    replay_seconds,
                    trigger_case_count=trigger_case_count,
                    args=args,
                )

        final_result = json.loads(recognizer.FinalResult())
        if final_result.get("text"):
            replay_seconds = audio_bytes_seen / float(args.samplerate * 2)
            trigger_case_count += handle_result(
                final_result,
                pipeline,
                logger,
                audio_path,
                replay_seconds,
                trigger_case_count=trigger_case_count,
                args=args,
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
) -> int:
    text = str(result.get("text") or "").strip()
    logger.write("final_raw", {"result": result, "text": text, "replay_seconds": replay_seconds})
    if not text:
        return 0

    payload = add_slide_payload(pipeline.process_text(
        text,
        asr_result=result,
        show_candidates=args.show_candidates,
        now_ms=int(replay_seconds * 1000),
    ))
    output = {"replay": {"enabled": True, "sent": bool(payload.get("parsed"))}}
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
    if not payload.get("parsed"):
        return 0

    case_number = trigger_case_count + 1
    parsed = payload.get("parsed") or {}
    ref = str(parsed.get("ref") or "")
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
    parser.add_argument("--audio", action="append", type=Path, help="Specific audio file to replay.")
    parser.add_argument("--include-chunks", action="store_true", help="Include *_chunks directories in auto search.")
    parser.add_argument("--limit", type=int, default=0, help="Limit auto-selected files.")
    parser.add_argument("--run", action="store_true", help="Actually run replay. Without this, only list files.")
    parser.add_argument("--download-url", action="append", default=[], help="YouTube URL to download before replay.")
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
    parser.add_argument("--bible", type=Path, default=DEFAULT_BIBLE)
    parser.add_argument("--samplerate", type=int, default=16000)
    parser.add_argument("--chunk-bytes", type=int, default=8000)
    parser.add_argument("--open-vocabulary", action="store_true")
    parser.add_argument("--vosk-buffer-parts", type=int, default=3)
    parser.add_argument("--vosk-log-level", type=int, default=-1)
    parser.add_argument("--show-candidates", action="store_true")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--no-log", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    search_roots = args.search_root or list(DEFAULT_SEARCH_ROOTS)
    downloaded = download_audio(args.download_url, args.download_dir)
    explicit_audio = list(args.audio or [])
    files = [path for path in explicit_audio if path.exists()]
    auto_selected = not explicit_audio
    if not files:
        files = collect_audio_files(search_roots, args.include_chunks)
    files.extend(downloaded)
    skipped_processed: list[Path] = []
    if auto_selected and not args.include_processed:
        processed = collect_processed_audio_files(args.log_dir)
        files, skipped_processed = skip_processed_audio_files(files, processed)
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

    SetLogLevel(args.vosk_log_level)
    model = Model(str(args.model))
    grammar = None if args.open_vocabulary else build_grammar()
    for index, path in enumerate(files, start=1):
        print(f"\nReplay {index}/{len(files)}: {path}", flush=True)
        replay_audio_file(path, args, model, grammar)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
