#!/usr/bin/env python3
"""Find the end of the first singing block, preserving everything from 00:00."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SINGING = ("Singing", "Male singing", "Female singing", "Child singing", "Choir")


def merge_intervals(intervals, gap):
    merged = []
    for start, end in intervals:
        if merged and start - merged[-1][1] <= gap:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return merged


def singing_blocks(rows, music_threshold, singing_threshold, speech_threshold,
                   max_gap, min_singing):
    # Filter isolated detections BEFORE bridging prayers between songs.
    runs = merge_intervals(
        [(r["start"], r["end"]) for r in rows
         if r["music"] >= music_threshold and r["singing"] >= singing_threshold
         and r["speech"] < speech_threshold], 0)
    runs = [r for r in runs if r[1] - r[0] >= min_singing]
    return merge_intervals(runs, max_gap)


def analyze(source, model_dir, threads):
    import sherpa_onnx

    model = model_dir / "model.int8.onnx"
    labels = model_dir / "class_labels_indices.csv"
    if not model.is_file() or not labels.is_file():
        raise ValueError(f"Нет модели или списка классов в {model_dir}. См. README.")
    tagger = sherpa_onnx.AudioTagging(sherpa_onnx.AudioTaggingConfig(
        model=sherpa_onnx.AudioTaggingModelConfig(ced=str(model), num_threads=threads),
        labels=str(labels), top_k=527))
    # Decode as a stream: memory use does not grow with recording length.
    rate, window, hop = 16000, 5, 2.5
    window_bytes, hop_bytes = int(rate * window * 2), int(rate * hop * 2)
    rows, position, buffer = [], 0, b""
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([
            "ffmpeg", "-nostdin", "-v", "error", "-i", str(source),
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(rate),
            "-f", "s16le", "pipe:1"], stdout=subprocess.PIPE, stderr=errors)
        try:
            while True:
                data = process.stdout.read(window_bytes - len(buffer))
                buffer += data
                if not buffer:
                    break
                duration = len(buffer) / (rate * 2)
                # Pad only for inference; reported end stays inside the file.
                samples = np.frombuffer(buffer, dtype="<i2").astype(np.float32) / 32768
                samples = np.pad(samples, (0, max(0, rate * window - len(samples))))
                stream = tagger.create_stream()
                stream.accept_waveform(rate, samples)
                events = tagger.compute(stream)
                scores = {e.name: float(e.prob) for e in events}
                rows.append(dict(start=position, end=position + duration,
                    music=scores.get("Music", 0), speech=scores.get("Speech", 0),
                    singing=max(scores.get(k, 0) for k in SINGING),
                    guitar=max(scores.get("Guitar", 0), scores.get("Acoustic guitar", 0))))
                if len(rows) % 120 == 0:
                    print(f"Проверено {position / 60:.1f} мин", flush=True)
                if len(buffer) < window_bytes:
                    break
                buffer = buffer[hop_bytes:]
                position += hop
            return_code = process.wait()
            if return_code:
                errors.seek(0)
                raise ValueError(errors.read().decode(errors="replace"))
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
                process.wait()
    if not rows:
        raise ValueError("В файле нет аудио.")
    return rows


def timecode(seconds):
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:04.1f}"


def positive(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("Нужно конечное число больше нуля")
    return value


def probability(value):
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise argparse.ArgumentTypeError("Нужно число от 0 до 1")
    return value


def main():
    parser = argparse.ArgumentParser(description=(
        "Найти конец первого блока пения и сохранить фрагмент от начала записи."))
    parser.add_argument("audio", type=Path)
    parser.add_argument("--model-dir", type=Path,
                        default=ROOT / ".cache/liverse/models/ced-tiny")
    parser.add_argument("--output", type=Path, help="Вырезать WAV от начала до найденной границы")
    parser.add_argument("--report", type=Path, help="Сохранить JSON с границами и оценками")
    parser.add_argument("--max-gap", type=positive, default=120,
                        help="Максимальная пауза/молитва между песнями, секунды (120)")
    parser.add_argument("--min-singing", type=positive, default=20,
                        help="Минимальная длина устойчивого пения, секунды (20)")
    parser.add_argument("--music-threshold", type=probability, default=0.65)
    parser.add_argument("--singing-threshold", type=probability, default=0.05)
    parser.add_argument("--speech-threshold", type=probability, default=0.6)
    parser.add_argument("--block", type=int, default=1, help="Номер блока пения (1)")
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    if not shutil.which("ffmpeg"):
        parser.error("Не найден ffmpeg")
    if not args.audio.is_file() or args.block < 1 or args.threads < 1:
        parser.error("Нужны существующий аудиофайл и положительные block/threads")
    for path in (args.output, args.report):
        if path and (path.exists() or not path.parent.is_dir()):
            parser.error(f"Результат уже существует или нет каталога: {path}")
    if args.output and args.output.suffix.lower() != ".wav":
        parser.error("Для --output укажите файл .wav")
    if args.output and args.report and args.output.resolve() == args.report.resolve():
        parser.error("Аудио и отчёт должны иметь разные пути")
    try:
        rows = analyze(args.audio, args.model_dir, args.threads)
        blocks = singing_blocks(rows, args.music_threshold, args.singing_threshold,
                                args.speech_threshold, args.max_gap, args.min_singing)
        for i, (start, end) in enumerate(blocks, 1):
            print(f"Блок {i}: {timecode(start)} — {timecode(end)}")
        selected = blocks[args.block - 1] if args.block <= len(blocks) else None
        report = dict(source=str(args.audio.resolve()), duration=rows[-1]["end"],
                      parameters={k: getattr(args, k) for k in (
                          "max_gap", "min_singing", "music_threshold", "singing_threshold",
                          "speech_threshold", "block")},
                      model=str(args.model_dir.resolve()), blocks=blocks,
                      selected_interval=[0, selected[1]] if selected else None,
                      boundary_resolution_seconds=5, windows=rows)
        if args.report:
            with args.report.open("x", encoding="utf-8") as out:
                json.dump(report, out, ensure_ascii=False, indent=2)
                out.write("\n")
        if selected is None:
            raise ValueError("Устойчивое пение или выбранный блок не найден; аудио не вырезано.")
        end = selected[1]
        print(f"Предлагаемый фрагмент: 00:00:00.0 — {timecode(end)}. "
              "Граница приблизительная, проверьте последние 10 секунд на слух.")
        if end >= rows[-1]["end"] - 5:
            raise ValueError("Пение продолжается до конца файла: переход не подтверждён, аудио не вырезано.")
        if args.output:
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-n",
                "-i", str(args.audio), "-t", str(end), "-map", "0:a:0",
                "-vn", "-c:a", "pcm_s16le", str(args.output)], check=True)
            print(f"Создано: {args.output}")
    except (ValueError, OSError, subprocess.CalledProcessError, ImportError) as exc:
        parser.exit(1, f"Ошибка: {exc}\n")


if __name__ == "__main__":
    main()
