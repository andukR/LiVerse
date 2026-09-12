#!/usr/bin/env python3
"""Review LiVerse trigger cases with local audio playback."""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import shutil
import subprocess
import threading
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from bible_parser_core.parser import parse_live_reference
from tools.holyrics import scripture_range


DEFAULT_RUNS_DIR = Path(".cache") / "liverse" / "vosk_probe"
LATEST_REPLAY_BATCH = "latest_replay_batch.json"
SLIDE_DISPLAY_DIR = Path(__file__).resolve().parent.parent / "slide_display"
CATEGORIES = {
    "1": ("true_reference", "верная ссылка"),
    "2": ("vosk_distortion", "Vosk исказил произнесённую ссылку"),
    "3": ("false_paronym", "ложное срабатывание: пароним, похожее по звучанию слово"),
    "4": ("false_homonym", "ложное срабатывание: омоним, то же звучание/другая мысль"),
    "5": ("false_plain_speech", "ложное срабатывание: обычная речь без ссылки"),
    "6": ("false_noise", "ложное срабатывание: шум, музыка или неречь"),
    "7": ("unclear", "непонятно, нужно переслушать позже"),
    "8": ("parser_error", "Vosk услышал достаточно, но мозг LiVerse разобрал неверно"),
    "9": ("speaker_error", "ошибка произношения: говорящий назвал ссылку неполно или оговорился"),
    "0": (
        "excluded_cascade",
        "исключить из обучения: следствие ошибки или повтор уже показанной/активной цитаты",
    ),
}
CATEGORY_LABELS = {category: label for category, label in CATEGORIES.values()}
CATEGORY_LABELS["wrong_reference"] = "ссылка была названа, но Vosk/LiVerse разобрал её неверно"
SMART_SLIDE_CATEGORIES = {
    "1": ("correct_transition", "предложенный переход верный"),
    "2": ("correct_hold", "правильно было оставить текущий слайд"),
    "3": ("wrong_transition", "переход неверный или выбран не тот слайд"),
    "4": ("missed_transition", "нужно было перейти, но алгоритм не предложил переход"),
    "5": ("unclear", "непонятно, фрагмента недостаточно"),
}
SMART_SLIDE_ACTION_LABELS = {
    "ignore": "не менять слайд",
    "keep": "оставить текущий слайд",
    "activate": "показать первый стих диапазона",
    "advance": "перейти на следующий слайд",
    "assisted_advance": "перейти на следующий слайд по последовательному чтению",
    "synchronize_forward": "перейти к найденному более позднему слайду",
    "assisted_synchronize_forward": "перейти к ближайшему ожидаемому слайду",
    "complete": "завершить диапазон",
}
SMART_SLIDE_TRANSITION_ACTIONS = {
    "advance", "assisted_advance", "synchronize_forward", "assisted_synchronize_forward",
}
SMART_SLIDE_DISPLAY_ACTIONS = SMART_SLIDE_TRANSITION_ACTIONS | {"activate"}


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
        key=lambda path: path.parent.name,
        reverse=True,
    )
    if not candidates:
        raise RuntimeError(f"Не найден trigger_cases.jsonl в {runs_dir}")
    return candidates[0]


def all_cases_files(runs_dir: Path) -> list[Path]:
    return sorted(
        runs_dir.glob("*/trigger_cases.jsonl"),
        key=lambda path: path.parent.name,
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
    fallback_path = cases_path.parent / "audio.wav"
    audio = str(case.get("audio") or "").strip()
    if audio:
        path = Path(audio)
        candidate = path if path.is_absolute() else Path.cwd() / path
        if candidate.exists():
            return candidate
        if fallback_path.exists():
            return fallback_path
        return candidate
    return fallback_path


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

    play_audio_window(audio_path, start, duration)


def play_audio_window(audio_path: Path, start: float, duration: float) -> None:
    if shutil.which("mpv"):
        subprocess.Popen(
            ["mpv", "--no-video", f"--start={start:.3f}", f"--length={duration:.3f}", "--quiet", str(audio_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"Проигрываю: {audio_path} [{start:.3f}s + {duration:.3f}s]")
        return

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
    reference_list = payload.get("reference_list") if isinstance(payload.get("reference_list"), list) else []
    if reference_list:
        print(f"recognized_list ({len(reference_list)}):")
        for index, item in enumerate(reference_list, start=1):
            if not isinstance(item, dict):
                print(f"  {index}. {item}")
                continue
            ref = str(item.get("ref") or "").strip()
            source_text = str(item.get("source_text") or "").strip()
            suffix = f"  <-  {source_text}" if source_text else ""
            print(f"  {index}. {ref}{suffix}")
    risk_score = payload.get("risk_score")
    risk_level = payload.get("risk_level")
    risk_reasons = payload.get("risk_reasons") or []
    if risk_score is not None:
        print(f"risk: {risk_level} {risk_score} reasons={', '.join(str(reason) for reason in risk_reasons)}")
    ml_risk = payload.get("ml_risk") if isinstance(payload.get("ml_risk"), dict) else {}
    if ml_risk:
        print(
            "ml_risk: "
            f"p={ml_risk.get('confirm_probability')} "
            f"threshold={ml_risk.get('threshold')} "
            f"needs_confirmation={ml_risk.get('needs_confirmation')}"
        )
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
        "\nКоманды: Enter/зв - прослушать | зв+ - длиннее | 0-9 - выбрать | "
        "к N/исправить N - перейти к случаю N | н заметка | п пропустить | вых выход"
    )


def find_start_position(cases: list[dict[str, Any]], state_case_id: str, no_resume: bool) -> int:
    if state_case_id and not no_resume:
        for index, case in enumerate(cases):
            if str(case.get("case_id") or "") == state_case_id:
                if is_unreviewed(case):
                    return index
                for next_index in range(index + 1, len(cases)):
                    if is_unreviewed(cases[next_index]):
                        return next_index
                break
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


@dataclass
class SmartSlideEntry:
    events_path: Path
    event_id: str
    event: dict[str, Any]
    review: dict[str, Any]
    sequence_id: int
    sequence_position: int = 0
    sequence_total: int = 0

    @property
    def reviews_path(self) -> Path:
        return self.events_path.with_name("smart_slide_reviews.jsonl")


def smart_slide_event_paths(runs_dir: Path, *, latest_batch: bool) -> list[Path]:
    if not latest_batch:
        return sorted(path.resolve() for path in runs_dir.glob("*/events.jsonl"))
    batch_path = runs_dir / LATEST_REPLAY_BATCH
    if not batch_path.exists():
        raise RuntimeError(f"Не найден файл последней пачки: {batch_path}")
    try:
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Повреждён файл последней пачки: {batch_path}: {exc}") from exc
    paths: list[Path] = []
    for run_dir in batch.get("runs") or []:
        path = Path(str(run_dir))
        if not path.is_absolute():
            path = Path.cwd() / path
        events_path = path / "events.jsonl"
        if events_path.exists():
            paths.append(events_path.resolve())
    return paths


def collect_smart_slide_entries(events_paths: list[Path]) -> list[SmartSlideEntry]:
    entries: list[SmartSlideEntry] = []
    sequence_id = -1
    previous_passage = ""
    previous_completed = True
    for events_path in events_paths:
        reviews = {
            str(row.get("event_id") or ""): row
            for row in load_jsonl(events_path.with_name("smart_slide_reviews.jsonl"))
        }
        continued = bool(session_metadata(events_path).get("continued_window_context"))
        first_shadow_in_run = True
        for line_number, event in enumerate(load_jsonl(events_path), start=1):
            if event.get("event") != "SMART_SLIDE_SHADOW":
                continue
            passage = str(event.get("passage") or "").strip()
            starts_sequence = bool(
                sequence_id < 0
                or previous_completed
                or passage != previous_passage
                or (first_shadow_in_run and not continued)
            )
            if starts_sequence:
                sequence_id += 1
            event_id = f"{events_path.parent.name}:smart_slide:{line_number}"
            entries.append(SmartSlideEntry(
                events_path=events_path,
                event_id=event_id,
                event=event,
                review=dict(reviews.get(event_id) or {}),
                sequence_id=sequence_id,
            ))
            previous_passage = passage
            previous_completed = str(event.get("action") or "") == "complete"
            first_shadow_in_run = False
    counts: dict[int, int] = {}
    positions: dict[int, int] = {}
    for entry in entries:
        counts[entry.sequence_id] = counts.get(entry.sequence_id, 0) + 1
    for entry in entries:
        positions[entry.sequence_id] = positions.get(entry.sequence_id, 0) + 1
        entry.sequence_position = positions[entry.sequence_id]
        entry.sequence_total = counts[entry.sequence_id]
    return entries


def smart_slide_is_unreviewed(entry: SmartSlideEntry) -> bool:
    return not str(entry.review.get("review_category") or "").strip()


def smart_slide_audio_path(entry: SmartSlideEntry) -> Path:
    session = session_metadata(entry.events_path)
    local_audio = str(session.get("audio") or "").strip()
    if local_audio:
        candidate = Path(local_audio)
        candidate = candidate if candidate.is_absolute() else entry.events_path.parent / candidate
        if candidate.is_file():
            return candidate
    source_audio = str(session.get("source_audio") or "").strip()
    if not source_audio:
        return entry.events_path.with_name("audio.wav")
    candidate = Path(source_audio)
    return candidate if candidate.is_absolute() else Path.cwd() / candidate


def play_smart_slide_entry(entry: SmartSlideEntry, *, long: bool = False) -> None:
    audio_path = smart_slide_audio_path(entry)
    if not audio_path.exists():
        print(f"Аудиофайл не найден: {audio_path}")
        return
    moment = float_value(entry.event.get("replay_seconds"))
    before, after = (22.0, 10.0) if long else (12.0, 5.0)
    start = max(0.0, moment - before)
    play_audio_window(audio_path, start, before + after)


def smart_slide_element_label(element: object, index: object) -> str:
    number = int(index) + 1 if isinstance(index, int) else "?"
    if not isinstance(element, dict):
        return f"слайд {number}"
    start_chapter = int(element.get("start_chapter", element.get("chapter")) or 0)
    start_verse = int(element.get("start_verse", element.get("verse")) or 0)
    end_chapter = int(element.get("chapter") or start_chapter)
    end_verse = int(element.get("verse") or start_verse)
    bounds = f"{start_chapter}:{start_verse}"
    if (end_chapter, end_verse) != (start_chapter, start_verse):
        bounds += f"–{end_chapter}:{end_verse}" if end_chapter != start_chapter else f"–{end_verse}"
    return f"слайд {number} ({bounds})"


def print_smart_slide_entry(
    entries: list[SmartSlideEntry],
    position: int,
) -> None:
    entry = entries[position]
    event = entry.event
    action = str(event.get("action") or "ignore")
    print("\n" + "=" * 80)
    print(
        f"Решение {position + 1}/{len(entries)}; "
        f"диапазон {entry.sequence_id + 1}, "
        f"шаг {entry.sequence_position}/{entry.sequence_total}"
    )
    print(f"log={entry.events_path.parent}")
    print(f"audio={smart_slide_audio_path(entry)}")
    print(f"time={float_value(event.get('replay_seconds')):.3f}s passage={event.get('passage')}")
    print(
        "текущий: "
        + smart_slide_element_label(event.get("current_element"), event.get("current_index"))
    )
    target_index = event.get("target_index")
    if isinstance(target_index, int):
        print(
            "предложен: "
            + smart_slide_element_label(event.get("target_element"), target_index)
        )
    print(f"решение: {action} — {SMART_SLIDE_ACTION_LABELS.get(action, action)}")
    print(
        f"кандидат: {event.get('candidate')} score={event.get('score')} "
        f"margin={event.get('margin')} words={event.get('matched_words')}"
    )
    print(f"основание: {event.get('reason')} source={event.get('evidence_source')}")
    print(f"речь: {event.get('window')}")
    neighbours: list[str] = []
    for neighbour_index in (position - 1, position + 1):
        if 0 <= neighbour_index < len(entries):
            neighbour = entries[neighbour_index]
            if neighbour.sequence_id == entry.sequence_id:
                neighbour_action = str(neighbour.event.get("action") or "ignore")
                neighbours.append(
                    f"шаг {neighbour.sequence_position}: "
                    f"{neighbour.event.get('candidate')} -> {neighbour_action}"
                )
    if neighbours:
        print("соседние решения: " + " | ".join(neighbours))
    if entry.review:
        category = str(entry.review.get("review_category") or "")
        labels = {value: label for value, label in SMART_SLIDE_CATEGORIES.values()}
        print(f"разметка: {category} ({labels.get(category, '')})")
        if entry.review.get("note"):
            print(f"заметка: {entry.review['note']}")
    print("\nКатегории перелистывания:")
    for key, (_category, label) in SMART_SLIDE_CATEGORIES.items():
        print(f"  {key}. {label}")
    print(
        "\nКоманды: Enter/зв - прослушать | зв+ - длиннее | 1-5 - выбрать | "
        "к N/исправить N - перейти | н заметка | п пропустить | вых выход"
    )


def save_smart_slide_review(entry: SmartSlideEntry) -> None:
    rows = load_jsonl(entry.reviews_path)
    replacement = dict(entry.review)
    replacement["event_id"] = entry.event_id
    for index, row in enumerate(rows):
        if str(row.get("event_id") or "") == entry.event_id:
            rows[index] = replacement
            break
    else:
        rows.append(replacement)
    save_jsonl(entry.reviews_path, rows)


def update_smart_slide_review(entry: SmartSlideEntry, category: str, note: str = "") -> None:
    """Persist the same review record used by the terminal annotator."""
    entry.review.update({
        "event_id": entry.event_id,
        "status": "reviewed",
        "review_category": category,
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "passage": str(entry.event.get("passage") or ""),
        "replay_seconds": float_value(entry.event.get("replay_seconds")),
        "action": str(entry.event.get("action") or ""),
        "current_index": entry.event.get("current_index"),
        "target_index": entry.event.get("target_index"),
        "note": note,
    })
    save_smart_slide_review(entry)


def smart_slide_browser_payload(entry: SmartSlideEntry, position: int, total: int) -> dict[str, Any]:
    event = entry.event
    current = event.get("current_element") if isinstance(event.get("current_element"), dict) else {}
    target = event.get("target_element") if isinstance(event.get("target_element"), dict) else {}
    audio_path = smart_slide_audio_path(entry)
    return {
        "position": position + 1,
        "total": total,
        "event_id": entry.event_id,
        "passage": str(event.get("passage") or ""),
        "replay_seconds": float_value(event.get("replay_seconds")),
        "action": str(event.get("action") or ""),
        "action_label": SMART_SLIDE_ACTION_LABELS.get(str(event.get("action") or ""), ""),
        "reason": str(event.get("reason") or ""),
        "score": event.get("score"),
        "margin": event.get("margin"),
        "matched_words": event.get("matched_words"),
        "window": str(event.get("window") or ""),
        "current": dict(current),
        "target": dict(target),
        "will_transition": str(event.get("action") or "") in SMART_SLIDE_TRANSITION_ACTIONS,
        "will_display": str(event.get("action") or "") in SMART_SLIDE_DISPLAY_ACTIONS,
        "audio_available": audio_path.exists(),
        "review_category": str(entry.review.get("review_category") or ""),
        "note": str(entry.review.get("note") or ""),
    }


def next_unreviewed_smart_slide(entries: list[SmartSlideEntry], start: int) -> int:
    for index in range(start, len(entries)):
        if smart_slide_is_unreviewed(entries[index]):
            return index
    for index in range(0, min(start, len(entries))):
        if smart_slide_is_unreviewed(entries[index]):
            return index
    return len(entries)


def normal_slide_updates(events_path: Path) -> list[dict[str, Any]]:
    """Recreate ordinary LiVerse slide changes omitted by SMART_SLIDE_SHADOW."""
    updates: list[dict[str, Any]] = []
    replay_seconds = 0.0
    accumulated_list_refs: list[str] = []
    for line_number, event in enumerate(load_jsonl(events_path), start=1):
        if event.get("replay_seconds") is not None:
            replay_seconds = float_value(event.get("replay_seconds"))
        if event.get("event") != "parsed":
            continue
        output = event.get("output") if isinstance(event.get("output"), dict) else {}
        replay = output.get("replay") if isinstance(output.get("replay"), dict) else {}
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        reference_list = payload.get("reference_list") if isinstance(payload.get("reference_list"), list) else []
        if replay.get("sent") and reference_list:
            new_refs = [
                str(item.get("ref") or "").strip()
                for item in reference_list
                if isinstance(item, dict) and str(item.get("ref") or "").strip()
            ]
            for ref in new_refs:
                if ref not in accumulated_list_refs:
                    accumulated_list_refs.append(ref)
            refs = accumulated_list_refs
            if refs:
                updates.append({
                    "event_id": f"{events_path.parent.name}:display:{line_number}",
                    "replay_seconds": replay_seconds,
                    "ref": "Ссылки для чтения",
                    "element": {"text": "\n".join(refs)},
                    "kind": "reference_list",
                    "vosk_text": str(event.get("vosk_text") or ""),
                    "source": str(payload.get("source") or ""),
                })
            continue
        accumulated_list_refs.clear()
        reference = str(payload.get("ref") or "").strip()
        if not replay.get("sent") or not reference:
            continue
        parsed = parse_live_reference(reference)
        if parsed is None:
            continue
        parsed_payload = {
            "book": parsed.book,
            "chapter": parsed.chapter,
            "start_verse": parsed.start_verse,
            "end_chapter": parsed.end_chapter,
            "end_verse": parsed.end_verse,
        }
        inferred_sequential_reading = (
            str(payload.get("source") or "") == "replay_inferred_sequential_text_reading"
        )
        long_range = scripture_range(parsed_payload) is not None
        if inferred_sequential_reading:
            # This is not an announced range.  By now text matching has proved
            # that the reader reached the final observed verse, so show that
            # one verse immediately; the UPS will advance from it afterwards.
            current = parse_live_reference(f"{parsed.book} {parsed.end_chapter or parsed.chapter}:{parsed.end_verse}")
            if current is None:
                continue
            element = {
                "start_chapter": current.chapter,
                "start_verse": current.start_verse,
                "chapter": int(current.end_chapter or current.chapter),
                "verse": current.end_verse,
                "text": current.verse_text,
            }
            kind = "sequential_reading"
        else:
            element = {
                "start_chapter": parsed.chapter,
                "start_verse": parsed.start_verse,
                "chapter": int(parsed.end_chapter or parsed.chapter),
                "verse": parsed.end_verse,
                # An announced long range gets a title-only slide; its verses
                # are subsequently controlled one at a time by the UPS.
                "text": "" if long_range else parsed.verse_text,
            }
            kind = "range_announcement" if long_range else "ordinary_reference"
        updates.append({
            "event_id": f"{events_path.parent.name}:display:{line_number}",
            "replay_seconds": replay_seconds,
            "ref": parsed.ref,
            "element": element,
            "kind": kind,
            "vosk_text": str(event.get("vosk_text") or ""),
            "source": str(payload.get("source") or ""),
        })
    return updates


def diagnostic_events(events_path: Path, stop_seconds: float, radius: float = 12.0) -> list[dict[str, Any]]:
    """Return a compact, time-scoped snapshot useful for investigating a stopped replay."""
    result: list[dict[str, Any]] = []
    replay_seconds = 0.0
    useful_events = {
        "final_raw", "partial_raw", "parsed", "TEXT_CANDIDATE", "TEXT_ACCEPTED",
        "TEXT_REJECTED", "TEXT_SUPPRESSED", "SMART_SLIDE_SHADOW",
    }
    for event in load_jsonl(events_path):
        if event.get("replay_seconds") is not None:
            replay_seconds = float_value(event.get("replay_seconds"))
        event_name = str(event.get("event") or "")
        if event_name not in useful_events or abs(replay_seconds - stop_seconds) > radius:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        result.append({
            "time": round(replay_seconds, 3),
            "event": event_name,
            "text": str(event.get("text") or event.get("vosk_text") or event.get("window") or ""),
            "ref": str(event.get("reference") or payload.get("ref") or ""),
            "action": str(event.get("action") or ""),
            "reason": str(event.get("reason") or ""),
        })
    return result


class SmartSlideBrowserReview:
    def __init__(
        self,
        entries: list[SmartSlideEntry],
        *,
        no_resume: bool,
        report_dir: Path | None = None,
    ) -> None:
        self.entries = entries
        self.position = 0 if no_resume else next_unreviewed_smart_slide(entries, 0)
        if self.position >= len(entries):
            self.position = 0
        self.lock = threading.Lock()
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.report_dir = report_dir or entries[0].events_path.parent.parent
        self.session_incidents: list[dict[str, Any]] = []
        self.session_incident_paths: dict[str, Path] = {}

    def state(self) -> dict[str, Any]:
        with self.lock:
            payload = smart_slide_browser_payload(self.entries[self.position], self.position, len(self.entries))
            payload["remaining"] = sum(1 for item in self.entries if smart_slide_is_unreviewed(item))
            return payload

    def move(self, delta: int) -> dict[str, Any]:
        with self.lock:
            self.position = max(0, min(len(self.entries) - 1, self.position + delta))
        return self.state()

    def save_review(self, category: str, note: str) -> dict[str, Any]:
        categories = {value[0] for value in SMART_SLIDE_CATEGORIES.values()}
        if category not in categories:
            raise ValueError("Неизвестная категория разметки УПС.")
        with self.lock:
            update_smart_slide_review(self.entries[self.position], category, note)
            next_position = next_unreviewed_smart_slide(self.entries, self.position + 1)
            if next_position < len(self.entries):
                self.position = next_position
        return self.state()

    def audio_path(self) -> Path | None:
        with self.lock:
            path = smart_slide_audio_path(self.entries[self.position])
        return path if path.exists() else None

    def entry_by_id(self, event_id: str) -> SmartSlideEntry | None:
        return next((entry for entry in self.entries if entry.event_id == event_id), None)

    def audio_path_for_event(self, event_id: str) -> Path | None:
        with self.lock:
            entry = self.entry_by_id(event_id)
            path = smart_slide_audio_path(entry) if entry is not None else None
        return path if path is not None and path.is_file() else None

    def timeline(self) -> dict[str, Any]:
        with self.lock:
            sequence_id = self.entries[self.position].sequence_id
            sequence = [entry for entry in self.entries if entry.sequence_id == sequence_id]
            tracks: list[dict[str, Any]] = []
            for entry in sequence:
                audio_path = smart_slide_audio_path(entry)
                track_key = str(audio_path.resolve()) if audio_path.is_file() else ""
                if not tracks or tracks[-1]["audio_key"] != track_key:
                    tracks.append({
                        "audio_key": track_key,
                        "audio_available": bool(track_key),
                        "audio_event_id": entry.event_id,
                        "decisions": [],
                        "events_paths": [],
                    })
                events_path_text = str(entry.events_path.resolve())
                if events_path_text not in tracks[-1]["events_paths"]:
                    tracks[-1]["events_paths"].append(events_path_text)
                tracks[-1]["decisions"].append(
                    smart_slide_browser_payload(entry, self.entries.index(entry), len(self.entries))
                )
            for track in tracks:
                updates: list[dict[str, Any]] = []
                for events_path_text in track.pop("events_paths"):
                    updates.extend(normal_slide_updates(Path(events_path_text)))
                track["display_updates"] = sorted(updates, key=lambda item: float_value(item.get("replay_seconds")))
            return {
                "sequence_id": sequence_id,
                "sequence_number": sequence_id + 1,
                "sequence_total": len({entry.sequence_id for entry in self.entries}),
                "passage": str(sequence[0].event.get("passage") or "") if sequence else "",
                "tracks": tracks,
                "remaining": sum(1 for entry in self.entries if smart_slide_is_unreviewed(entry)),
            }

    def mark_error(
        self,
        event_id: str,
        category: str,
        note: str,
        operator_stop_seconds: object = None,
    ) -> dict[str, Any]:
        if category not in {"wrong_transition", "missed_transition"}:
            raise ValueError("Ошибка УПС должна быть отмечена как неверный или пропущенный переход.")
        with self.lock:
            entry = self.entry_by_id(event_id)
            if entry is None:
                raise ValueError("Решение УПС не найдено.")
            update_smart_slide_review(entry, category, note)
            if operator_stop_seconds is not None:
                entry.review["operator_stop_seconds"] = float_value(operator_stop_seconds)
                save_smart_slide_review(entry)
        return {"ok": True, "event_id": event_id, "category": category}

    def record_incident(
        self,
        event_id: str,
        operator_stop_seconds: object,
        displayed_ref: str,
        note: str,
    ) -> dict[str, Any]:
        """Keep stop-time evidence separate from labels used to train transition safety."""
        stop_seconds = float_value(operator_stop_seconds)
        with self.lock:
            entry = self.entry_by_id(event_id)
            if entry is None:
                raise ValueError("Решение УПС рядом с остановкой не найдено.")
            incident_path = entry.events_path.with_name("smart_slide_incidents.jsonl")
            evidence = diagnostic_events(entry.events_path, stop_seconds)
            incident_id = f"{entry.events_path.parent.name}:incident:{stop_seconds:.3f}"
            incident = {
                "incident_id": incident_id,
                "status": "unreviewed",
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
                "log": str(entry.events_path.parent.resolve()),
                "audio": str(smart_slide_audio_path(entry).resolve()),
                "operator_stop_seconds": round(stop_seconds, 3),
                "displayed_ref": displayed_ref,
                "active_passage": str(entry.event.get("passage") or ""),
                "nearest_smart_slide_event_id": entry.event_id,
                "nearest_smart_slide_seconds": float_value(entry.event.get("replay_seconds")),
                "nearest_smart_slide_action": str(entry.event.get("action") or ""),
                "nearest_smart_slide_reason": str(entry.event.get("reason") or ""),
                "browser_session_id": self.session_id,
                "note": note,
                "evidence": evidence,
            }
            rows = load_jsonl(incident_path)
            rows.append(incident)
            save_jsonl(incident_path, rows)
            self.session_incidents.append(incident)
            self.session_incident_paths[incident_id] = incident_path
        minutes, seconds = divmod(stop_seconds, 60)
        evidence_lines = [
            f"  {item['time']:.3f}s {item['event']}: "
            f"{item['ref'] or item['text'] or item['action'] or item['reason']}"
            for item in evidence
        ]
        report = "\n".join([
            "Ошибка браузерной эмуляции УПС",
            f"log={incident['log']}",
            f"audio={incident['audio']}",
            f"timecode={int(minutes):02d}:{seconds:06.3f}",
            f"displayed_ref={displayed_ref or 'не указан'}",
            f"active_passage={incident['active_passage']}",
            (
                f"nearest_decision={incident['nearest_smart_slide_action']} "
                f"at {incident['nearest_smart_slide_seconds']:.3f}s"
                if stop_seconds >= incident["nearest_smart_slide_seconds"]
                else f"first_decision_after_stop={incident['nearest_smart_slide_action']} "
                f"at {incident['nearest_smart_slide_seconds']:.3f}s"
            ),
            *(evidence_lines or ["  В ближайшем окне полезных событий нет."]),
        ])
        return {"ok": True, "incident_id": incident_id, "report": report}

    def update_incident_note(self, incident_id: str, note: str) -> None:
        """Save an operator observation without inferring a training label."""
        with self.lock:
            incident_path = self.session_incident_paths.get(incident_id)
            if incident_path is None:
                raise ValueError("Наблюдение относится к другой браузерной сессии.")
            rows = load_jsonl(incident_path)
            for row in rows:
                if str(row.get("incident_id") or "") == incident_id:
                    row["note"] = note
                    break
            else:
                raise ValueError("Наблюдение не найдено.")
            save_jsonl(incident_path, rows)
            for incident in self.session_incidents:
                if incident["incident_id"] == incident_id:
                    incident["note"] = note
                    break

    def write_error_report(self) -> Path:
        """Write one concise, session-scoped report containing only operator stops."""
        with self.lock:
            incidents = list(self.session_incidents)
        report_path = self.report_dir / "smart_slide_error_reports" / f"{self.session_id}.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Ошибки браузерной эмуляции УПС",
            "",
            f"Сессия: `{self.session_id}`",
            f"Зафиксировано остановок Enter: {len(incidents)}",
        ]
        for number, incident in enumerate(incidents, start=1):
            stop_seconds = float_value(incident["operator_stop_seconds"])
            minutes, seconds = divmod(stop_seconds, 60)
            decision_seconds = float_value(incident["nearest_smart_slide_seconds"])
            decision_relation = (
                f"первое решение после остановки: {incident['nearest_smart_slide_action']} в {decision_seconds:.3f} с"
                if stop_seconds < decision_seconds
                else f"ближайшее решение: {incident['nearest_smart_slide_action']} в {decision_seconds:.3f} с"
            )
            lines.extend([
                "",
                f"## {number}. {int(minutes):02d}:{seconds:06.3f}",
                f"- Экран: {incident['displayed_ref'] or 'не указан'}",
                f"- Активный диапазон: {incident['active_passage'] or 'не указан'}",
                f"- {decision_relation}",
                f"- Основание: {incident['nearest_smart_slide_reason'] or 'не указано'}",
                f"- Журнал: `{incident['log']}`",
            ])
            if incident["note"]:
                lines.append(f"- Примечание оператора: {incident['note']}")
        if not incidents:
            lines.extend(["", "Ошибок оператор не отметил."])
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return report_path

    def complete_sequence(self, observed_event_ids: list[str]) -> dict[str, Any]:
        observed = {str(value) for value in observed_event_ids}
        with self.lock:
            current_sequence = self.entries[self.position].sequence_id
            for entry in self.entries:
                if entry.sequence_id != current_sequence or entry.event_id not in observed:
                    continue
                if not smart_slide_is_unreviewed(entry):
                    continue
                action = str(entry.event.get("action") or "")
                category = "correct_transition" if action in SMART_SLIDE_DISPLAY_ACTIONS else "correct_hold"
                update_smart_slide_review(entry, category)
            next_position = next_unreviewed_smart_slide(self.entries, self.position + 1)
            if next_position < len(self.entries):
                self.position = next_position
                finished = False
            else:
                finished = True
        return {
            "finished": finished,
            "remaining": sum(1 for entry in self.entries if smart_slide_is_unreviewed(entry)),
            "timeline": None if finished else self.timeline(),
        }


def start_smart_slide_browser_review(entries: list[SmartSlideEntry], args: argparse.Namespace) -> None:
    controller = SmartSlideBrowserReview(entries, no_resume=args.no_resume, report_dir=Path(args.runs_dir))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def send_json(self, payload: dict, status: int = 200) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def read_json(self) -> dict | None:
            try:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                self.send_json({"ok": False, "error": "Нужен JSON-объект."}, status=400)
                return None
            if not isinstance(payload, dict):
                self.send_json({"ok": False, "error": "Нужен JSON-объект."}, status=400)
                return None
            return payload

        def serve_file(self, path: Path) -> None:
            try:
                path.resolve().relative_to(SLIDE_DISPLAY_DIR.resolve())
            except ValueError:
                self.send_error(403)
                return
            if not path.is_file():
                self.send_error(404)
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def serve_audio(self, path: Path) -> None:
            size = path.stat().st_size
            start, end = 0, size - 1
            range_header = self.headers.get("Range", "")
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip()) if range_header else None
            if match:
                start, end = int(match.group(1) or 0), int(match.group(2) or end)
                if start >= size or end < start:
                    self.send_error(416)
                    return
                end = min(end, size - 1)
            length = end - start + 1
            self.send_response(206 if match else 200)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "audio/wav")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if match:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    block = stream.read(min(64 * 1024, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)

        def do_GET(self) -> None:
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            if path == "/api/state":
                self.send_json(controller.state())
                return
            if path == "/api/timeline":
                self.send_json(controller.timeline())
                return
            if path == "/api/audio":
                event_id = str((parse_qs(parsed_url.query).get("event") or [""])[0])
                audio_path = controller.audio_path_for_event(event_id) if event_id else controller.audio_path()
                if audio_path is None:
                    self.send_json({"ok": False, "error": "WAV-файл для этого решения не найден."}, status=404)
                    return
                self.serve_audio(audio_path)
                return
            requested = "smart_slide_review.html" if path == "/" else unquote(path.lstrip("/"))
            self.serve_file(SLIDE_DISPLAY_DIR / requested)

        def do_POST(self) -> None:
            payload = self.read_json()
            if payload is None:
                return
            if self.path == "/api/review":
                try:
                    state = controller.save_review(str(payload.get("category") or ""), str(payload.get("note") or ""))
                except ValueError as exc:
                    self.send_json({"ok": False, "error": str(exc)}, status=422)
                    return
                self.send_json({"ok": True, "state": state})
                return
            if self.path == "/api/error":
                try:
                    result = controller.mark_error(
                        str(payload.get("event_id") or ""),
                        str(payload.get("category") or ""),
                        str(payload.get("note") or ""),
                        payload.get("operator_stop_seconds"),
                    )
                except ValueError as exc:
                    self.send_json({"ok": False, "error": str(exc)}, status=422)
                    return
                self.send_json(result)
                return
            if self.path == "/api/incident":
                try:
                    result = controller.record_incident(
                        str(payload.get("event_id") or ""),
                        payload.get("operator_stop_seconds"),
                        str(payload.get("displayed_ref") or ""),
                        str(payload.get("note") or ""),
                    )
                except ValueError as exc:
                    self.send_json({"ok": False, "error": str(exc)}, status=422)
                    return
                self.send_json(result)
                return
            if self.path == "/api/incident-note":
                try:
                    controller.update_incident_note(
                        str(payload.get("incident_id") or ""), str(payload.get("note") or "")
                    )
                except ValueError as exc:
                    self.send_json({"ok": False, "error": str(exc)}, status=422)
                    return
                self.send_json({"ok": True})
                return
            if self.path == "/api/complete":
                event_ids = payload.get("observed_event_ids")
                if not isinstance(event_ids, list):
                    self.send_json({"ok": False, "error": "Нужен список просмотренных решений."}, status=422)
                    return
                self.send_json({"ok": True, **controller.complete_sequence(event_ids)})
                return
            if self.path == "/api/move":
                try:
                    delta = int(payload.get("delta") or 0)
                except (TypeError, ValueError):
                    delta = 0
                self.send_json({"ok": True, "state": controller.move(-1 if delta < 0 else 1)})
                return
            self.send_error(404)

    server = ThreadingHTTPServer((args.review_host, args.review_port), Handler)
    url = f"http://{args.review_host}:{server.server_port}/"
    print(f"Визуальная разметка УПС: {url}")
    print("Оставьте это окно терминала открытым; Ctrl+C завершит локальный просмотр.")
    if not args.no_open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nВизуальная разметка УПС остановлена.")
    finally:
        server.server_close()
        report_path = controller.write_error_report()
        print(f"Отчёт ошибок этой браузерной сессии: {report_path}")


def review_smart_slides(args: argparse.Namespace) -> None:
    runs_dir = Path(args.runs_dir)
    events_paths = smart_slide_event_paths(runs_dir, latest_batch=args.latest_batch)
    entries = collect_smart_slide_entries(events_paths)
    if not entries:
        print(f"События SMART_SLIDE_SHADOW не найдены: {runs_dir}")
        return
    if args.browser:
        start_smart_slide_browser_review(entries, args)
        return
    unreviewed = sum(1 for entry in entries if smart_slide_is_unreviewed(entry))
    if not unreviewed and not args.no_resume:
        print(f"Все решения перелистывания уже размечены: {len(entries)}")
        print("Для просмотра с начала добавьте --no-resume.")
        return
    position = 0 if args.no_resume else next_unreviewed_smart_slide(entries, 0)
    reviewed = 0
    print(f"Последовательностей: {len({entry.sequence_id for entry in entries})}")
    print(f"Решений: {len(entries)}; неразмеченных: {unreviewed}")
    print("Разметка сохраняется отдельно и не используется для обучения НБА.")
    while 0 <= position < len(entries):
        entry = entries[position]
        print_smart_slide_entry(entries, position)
        command = input("> ").strip().lower()
        if command in {"", "зв", "p", "play"}:
            play_smart_slide_entry(entry)
            continue
        if command in {"зв+", "p+", "play+"}:
            play_smart_slide_entry(entry, long=True)
            continue
        if command in {"п", "skip", "s"}:
            position += 1
            continue
        if command in {"н", "note"}:
            entry.review["note"] = input("Заметка: ").strip()
            save_smart_slide_review(entry)
            continue
        if command in {"вых", "выход", "q", "quit"}:
            break
        next_position = parse_case_number_command(command, len(entries))
        if next_position is not None:
            position = next_position
            continue
        if command in SMART_SLIDE_CATEGORIES:
            category, _label = SMART_SLIDE_CATEGORIES[command]
            was_unreviewed = smart_slide_is_unreviewed(entry)
            update_smart_slide_review(entry, category, str(entry.review.get("note") or ""))
            if was_unreviewed:
                reviewed += 1
            position = next_unreviewed_smart_slide(entries, position + 1)
            continue
        print("Неизвестная команда.")
    remaining = sum(1 for entry in entries if smart_slide_is_unreviewed(entry))
    print(f"Готово. Размечено за этот запуск: {reviewed}. Осталось: {remaining}")


def session_source_audio(cases_path: Path) -> str:
    session = session_metadata(cases_path)
    return str(session.get("source_audio") or "")


def session_metadata(cases_path: Path) -> dict[str, Any]:
    session_path = cases_path.with_name("session.json")
    try:
        data = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def is_live_run(cases_path: Path) -> bool:
    session = session_metadata(cases_path)
    mode = str(session.get("mode") or "").strip()
    if mode == "audio_replay" or str(session.get("source_audio") or "").strip():
        return False
    return True


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


def latest_batch_cases_files(runs_dir: Path) -> list[Path]:
    batch_path = runs_dir / LATEST_REPLAY_BATCH
    if not batch_path.exists():
        raise RuntimeError(
            f"Не найден файл последней пачки: {batch_path}. "
            "Сначала запустите replay_audio_files.py с --run."
        )
    try:
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Повреждён файл последней пачки: {batch_path}: {exc}") from exc
    paths: list[Path] = []
    for run_dir in batch.get("runs") or []:
        cases_path = Path(str(run_dir)) / "trigger_cases.jsonl"
        if cases_path.exists():
            paths.append(cases_path.resolve())
    return sorted(paths, key=lambda path: path.parent.name)


def latest_live_cases_file(runs_dir: Path) -> Path:
    candidates = [
        path
        for path in all_cases_files(runs_dir)
        if is_live_run(path) and has_unreviewed_cases(load_jsonl(path))
    ]
    if not candidates:
        raise RuntimeError(f"Не найден live-запуск LiVerse с неразмеченными случаями в {runs_dir}")
    return sorted(candidates, key=lambda path: path.parent.name, reverse=True)[0]


def review(args: argparse.Namespace) -> None:
    if args.smart_slides:
        review_smart_slides(args)
        return
    if args.latest_batch:
        review_latest_batch(args)
        return
    if args.latest_live:
        args.cases = str(latest_live_cases_file(Path(args.runs_dir)))
        args.latest = True
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
    if args.latest:
        print("Режим --latest размечает только один самый новый файл. Для последней пачки replay запустите без --latest.")

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


def review_latest_batch(args: argparse.Namespace) -> None:
    runs_dir = Path(args.runs_dir)
    cases_paths = latest_batch_cases_files(runs_dir)
    if not cases_paths:
        print(f"В последней пачке нет trigger_cases.jsonl: {runs_dir / LATEST_REPLAY_BATCH}")
        return
    entries = collect_unreviewed_entries(cases_paths)
    if not entries:
        print(f"Неразмеченных случаев в последней пачке не найдено: {runs_dir / LATEST_REPLAY_BATCH}")
        return

    position = 0
    reviewed = 0
    print(f"Файлов trigger_cases.jsonl в последней пачке: {len(cases_paths)}")
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


def review_unreviewed_queue(args: argparse.Namespace, *, all_unreviewed: bool = False) -> None:
    runs_dir = Path(args.runs_dir)
    cases_paths = [path.resolve() for path in all_cases_files(runs_dir)]
    if args.from_run:
        cases_paths = [
            cases_path
            for cases_path in cases_paths
            if cases_path.parent.name >= args.from_run
        ]
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
    parser.add_argument(
        "--latest",
        action="store_true",
        help="Review only one newest trigger_cases.jsonl file. For the newest replay batch, omit --latest.",
    )
    parser.add_argument("--latest-batch", action="store_true", help="Review only the latest replay batch.")
    parser.add_argument(
        "--smart-slides",
        action="store_true",
        help="Review SMART_SLIDE_SHADOW decisions separately from citation labels.",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="For --smart-slides, open local visual review with simulated slides and synchronized audio.",
    )
    parser.add_argument("--review-host", default="127.0.0.1", help="Host for local visual smart-slide review.")
    parser.add_argument("--review-port", type=int, default=0, help="Port for local visual smart-slide review; 0 chooses a free port.")
    parser.add_argument("--no-open-browser", action="store_true", help="Do not open the visual review URL automatically.")
    parser.add_argument("--latest-live", action="store_true", help="Review only the newest live LiVerse run with unreviewed cases.")
    parser.add_argument("--all-unreviewed", action="store_true", help="Review all unreviewed cases from all runs.")
    parser.add_argument(
        "--from-run",
        default="",
        help="Review only runs whose directory name is this value or newer, for example 20260709_093034_146488.",
    )
    parser.add_argument("--state", default="", help="Path to review state JSON.")
    parser.add_argument("--no-resume", action="store_true", help="Start from first unreviewed case.")
    args = parser.parse_args()
    if args.browser and not args.smart_slides:
        parser.error("--browser доступен только вместе с --smart-slides")
    review(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
