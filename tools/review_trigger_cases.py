#!/usr/bin/env python3
"""Review LiVerse trigger cases with local audio playback."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_RUNS_DIR = Path(".cache") / "liverse" / "vosk_probe"
CATEGORIES = {
    "1": ("true_reference", "верная ссылка"),
    "2": ("vosk_distortion", "Vosk исказил произнесённую ссылку"),
    "3": ("false_paronym", "ложное срабатывание: пароним, похожее по звучанию слово"),
    "4": ("false_homonym", "ложное срабатывание: омоним, то же звучание/другая мысль"),
    "5": ("false_plain_speech", "ложное срабатывание: обычная речь без ссылки"),
    "6": ("false_noise", "ложное срабатывание: шум, музыка или неречь"),
    "7": ("unclear", "непонятно, нужно переслушать позже"),
}
CATEGORY_LABELS = {category: label for category, label in CATEGORIES.values()}
CATEGORY_LABELS["wrong_reference"] = "ссылка была названа, но Vosk/LiVerse разобрал её неверно"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
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


def save_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def latest_cases_file(runs_dir: Path) -> Path:
    candidates = sorted(
        runs_dir.glob("*/trigger_cases.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise RuntimeError(f"Не найден trigger_cases.jsonl в {runs_dir}")
    return candidates[0]


def all_cases_files(runs_dir: Path) -> list[Path]:
    return sorted(
        runs_dir.glob("*/trigger_cases.jsonl"),
        key=lambda path: path.stat().st_mtime,
    )


def is_unreviewed(case: dict[str, Any]) -> bool:
    return str(case.get("status") or "unreviewed") == "unreviewed"


def has_reviewed_cases(cases: list[dict[str, Any]]) -> bool:
    return any(not is_unreviewed(case) for case in cases)


def has_unreviewed_cases(cases: list[dict[str, Any]]) -> bool:
    return any(is_unreviewed(case) for case in cases)


def latest_unreviewed_batch(cases_paths: list[Path]) -> list[Path]:
    batch: list[Path] = []
    for cases_path in reversed(cases_paths):
        cases = load_jsonl(cases_path)
        if not cases or not has_unreviewed_cases(cases):
            if batch:
                break
            continue
        if has_reviewed_cases(cases):
            break
        batch.append(cases_path)
    return list(reversed(batch))


def state_path_for(cases_path: Path) -> Path:
    return cases_path.with_name("trigger_cases_review_state.json")


def load_state(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    return str(data.get("case_id") or "")


def save_state(path: Path, case_id: str) -> None:
    path.write_text(
        json.dumps({"case_id": case_id}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def case_audio_path(case: dict[str, Any], cases_path: Path) -> Path:
    audio = str(case.get("audio") or "").strip()
    if audio:
        path = Path(audio)
        return path if path.is_absolute() else Path.cwd() / path
    return cases_path.parent / "audio.wav"


def float_value(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def play_case(case: dict[str, Any], cases_path: Path, *, long: bool = False) -> None:
    audio_path = case_audio_path(case, cases_path)
    if not audio_path.exists():
        print(f"Аудиофайл не найден: {audio_path}")
        return

    start = float_value(case.get("window_start_seconds"), float_value(case.get("timecode_seconds")))
    end = float_value(case.get("window_end_seconds"), start + 12.0)
    duration = max(3.0, end - start)
    if long:
        start = max(0.0, start - 10.0)
        duration += 20.0

    if shutil.which("ffplay"):
        subprocess.Popen(
            [
                "ffplay",
                "-nodisp",
                "-autoexit",
                "-ss",
                f"{start:.3f}",
                "-t",
                f"{duration:.3f}",
                str(audio_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"Проигрываю: {audio_path} [{start:.3f}s + {duration:.3f}s]")
        return

    if shutil.which("mpv"):
        subprocess.Popen(
            ["mpv", "--no-video", f"--start={start:.3f}", f"--length={duration:.3f}", "--quiet", str(audio_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"Проигрываю: {audio_path} [{start:.3f}s + {duration:.3f}s]")
        return

    print("Не найден ffplay или mpv. Установите один из них для прослушивания.")


def print_case(case: dict[str, Any], position: int, total: int, *, cases_path: Path | None = None) -> None:
    print("\n" + "=" * 80)
    print(f"Случай {position + 1}/{total}: {case.get('case_id')}")
    if cases_path:
        print(f"log={cases_path.parent}")
    print(f"timecode={case.get('timecode')} window={case.get('window_start')}..{case.get('window_end')}")
    print(f"ref={case.get('ref')} action={case.get('action')} status={case.get('status')}")
    if case.get("review_category"):
        category = str(case.get("review_category"))
        label = CATEGORY_LABELS.get(category, "")
        print(f"category={category}" + (f" ({label})" if label else ""))
    print(f"vosk_text: {case.get('vosk_text')}")
    vosk_buffer = case.get("vosk_buffer")
    if isinstance(vosk_buffer, list) and vosk_buffer:
        print(f"vosk_buffer: {' | '.join(str(part) for part in vosk_buffer)}")
    payload = case.get("payload") if isinstance(case.get("payload"), dict) else {}
    parser_text = str(payload.get("text") or "").strip()
    if parser_text and parser_text != str(case.get("vosk_text") or "").strip():
        print(f"parser_text: {parser_text}")
    risk_score = payload.get("risk_score")
    risk_level = payload.get("risk_level")
    risk_reasons = payload.get("risk_reasons") or []
    if risk_score is not None:
        print(f"risk: {risk_level} {risk_score} reasons={', '.join(str(reason) for reason in risk_reasons)}")
    output = case.get("output") if isinstance(case.get("output"), dict) else {}
    holyrics = output.get("holyrics") if isinstance(output.get("holyrics"), dict) else {}
    if holyrics:
        print(f"holyrics: ok={holyrics.get('ok')} reason={holyrics.get('reason')}")
    note = str(case.get("note") or "").strip()
    if note:
        print(f"note: {note}")
    print("\nКатегории:")
    for key, (_category, label) in CATEGORIES.items():
        print(f"  {key}. {label}")
    print(
        "\nКоманды: Enter/зв - прослушать | зв+ - длиннее | 1-7 - выбрать | "
        "к N/исправить N - перейти к случаю N | н заметка | п пропустить | вых выход"
    )


def find_start_position(cases: list[dict[str, Any]], state_case_id: str, no_resume: bool) -> int:
    if state_case_id and not no_resume:
        for index, case in enumerate(cases):
            if str(case.get("case_id") or "") == state_case_id:
                return index
    for index, case in enumerate(cases):
        if str(case.get("status") or "unreviewed") == "unreviewed":
            return index
    return 0


def parse_case_number_command(command: str, total: int) -> int | None:
    match = re.match(r"^(?:к|go|case|случай|исправить|edit)\s+(\d+)$", command)
    if not match:
        return None
    number = int(match.group(1))
    if 1 <= number <= total:
        return number - 1
    print(f"Нет случая {number}. Доступный диапазон: 1..{total}")
    return None


@dataclass
class CaseEntry:
    cases_path: Path
    cases: list[dict[str, Any]]
    case_index: int

    @property
    def case(self) -> dict[str, Any]:
        return self.cases[self.case_index]


def session_source_audio(cases_path: Path) -> str:
    session_path = cases_path.with_name("session.json")
    try:
        session = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(session.get("source_audio") or "")


def case_signature(case: dict[str, Any], cases_path: Path) -> tuple[str, str, str, str, str]:
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


def collect_unreviewed_entries(cases_paths: list[Path]) -> list[CaseEntry]:
    loaded: list[tuple[Path, list[dict[str, Any]]]] = [
        (cases_path, load_jsonl(cases_path)) for cases_path in cases_paths
    ]
    reviewed_signatures = {
        case_signature(case, cases_path)
        for cases_path, cases in loaded
        for case in cases
        if not is_unreviewed(case)
    }
    entries: list[CaseEntry] = []
    for cases_path, cases in loaded:
        for index, case in enumerate(cases):
            if is_unreviewed(case):
                if case_signature(case, cases_path) in reviewed_signatures:
                    continue
                entries.append(CaseEntry(cases_path=cases_path, cases=cases, case_index=index))
    return entries


def review(args: argparse.Namespace) -> None:
    if not args.cases and not args.latest:
        review_unreviewed_queue(args, all_unreviewed=args.all_unreviewed)
        return

    cases_path = Path(args.cases) if args.cases else latest_cases_file(Path(args.runs_dir))
    cases_path = cases_path.resolve()
    cases = load_jsonl(cases_path)
    if not cases:
        print(f"Нет случаев для разметки: {cases_path}")
        return

    state_path = Path(args.state) if args.state else state_path_for(cases_path)
    position = find_start_position(cases, load_state(state_path), args.no_resume)
    reviewed = 0

    print(f"Файл случаев: {cases_path}")
    print(f"Файл состояния: {state_path}")

    while 0 <= position < len(cases):
        case = cases[position]
        case_id = str(case.get("case_id") or f"case_{position + 1}")
        save_state(state_path, case_id)
        print_case(case, position, len(cases), cases_path=cases_path)
        command = input("> ").strip().lower()

        if command in {"", "зв", "p", "play"}:
            play_case(case, cases_path)
            continue
        if command in {"зв+", "p+", "play+"}:
            play_case(case, cases_path, long=True)
            continue
        if command in {"п", "skip", "s"}:
            position += 1
            continue
        if command in {"н", "note"}:
            case["note"] = input("Заметка: ").strip()
            save_jsonl(cases_path, cases)
            continue
        if command in {"вых", "выход", "q", "quit"}:
            break
        next_position = parse_case_number_command(command, len(cases))
        if next_position is not None:
            position = next_position
            continue
        if command in CATEGORIES:
            category, _label = CATEGORIES[command]
            case["status"] = "reviewed"
            case["review_category"] = category
            case["reviewed_at"] = datetime.now().isoformat(timespec="seconds")
            if not str(case.get("note") or "").strip():
                case["note"] = ""
            save_jsonl(cases_path, cases)
            reviewed += 1
            position += 1
            continue

        print("Неизвестная команда.")

    print(f"Готово. Размечено за этот запуск: {reviewed}")


def review_unreviewed_queue(args: argparse.Namespace, *, all_unreviewed: bool = False) -> None:
    runs_dir = Path(args.runs_dir)
    cases_paths = [path.resolve() for path in all_cases_files(runs_dir)]
    queue_paths = cases_paths if all_unreviewed else latest_unreviewed_batch(cases_paths)
    entries = collect_unreviewed_entries(queue_paths)
    if not entries:
        print(f"Неразмеченных случаев в новом пакете не найдено: {runs_dir}")
        if not all_unreviewed:
            old_entries = collect_unreviewed_entries(cases_paths)
            if old_entries:
                print(
                    f"В старых логах осталось неразмеченных случаев: {len(old_entries)} "
                    f"(для просмотра добавьте --all-unreviewed)."
                )
        return

    position = 0
    reviewed = 0
    if all_unreviewed:
        print(f"Файлов trigger_cases.jsonl: {len(cases_paths)}")
    else:
        print(f"Файлов в новом пакете: {len(queue_paths)}")
        skipped = len(cases_paths) - len(queue_paths)
        if skipped:
            print(f"Старых файлов вне этого пакета: {skipped} (для просмотра добавьте --all-unreviewed).")
    print(f"Неразмеченных случаев: {len(entries)}")

    while 0 <= position < len(entries):
        entry = entries[position]
        case = entry.case
        print_case(case, position, len(entries), cases_path=entry.cases_path)
        command = input("> ").strip().lower()

        if command in {"", "зв", "p", "play"}:
            play_case(case, entry.cases_path)
            continue
        if command in {"зв+", "p+", "play+"}:
            play_case(case, entry.cases_path, long=True)
            continue
        if command in {"п", "skip", "s"}:
            position += 1
            continue
        if command in {"н", "note"}:
            case["note"] = input("Заметка: ").strip()
            save_jsonl(entry.cases_path, entry.cases)
            continue
        if command in {"вых", "выход", "q", "quit"}:
            break
        next_position = parse_case_number_command(command, len(entries))
        if next_position is not None:
            position = next_position
            continue
        if command in CATEGORIES:
            category, _label = CATEGORIES[command]
            case["status"] = "reviewed"
            case["review_category"] = category
            case["reviewed_at"] = datetime.now().isoformat(timespec="seconds")
            if not str(case.get("note") or "").strip():
                case["note"] = ""
            save_jsonl(entry.cases_path, entry.cases)
            reviewed += 1
            position += 1
            continue

        print("Неизвестная команда.")

    remaining = sum(1 for entry in entries if is_unreviewed(entry.case))
    print(f"Готово. Размечено за этот запуск: {reviewed}. Осталось неразмеченных: {remaining}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Review LiVerse trigger cases.")
    parser.add_argument(
        "--cases",
        default="",
        help="Path to trigger_cases.jsonl. Default: newest unreviewed replay batch.",
    )
    parser.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR), help="Directory with LiVerse Vosk runs.")
    parser.add_argument("--latest", action="store_true", help="Review the latest trigger_cases.jsonl only.")
    parser.add_argument("--all-unreviewed", action="store_true", help="Review all unreviewed cases from all runs.")
    parser.add_argument("--state", default="", help="Path to review state JSON.")
    parser.add_argument("--no-resume", action="store_true", help="Start from first unreviewed case.")
    args = parser.parse_args()
    review(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
