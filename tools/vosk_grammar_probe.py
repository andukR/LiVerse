#!/usr/bin/env python3
"""Vosk probe wired to the LiVerse Bible reference resolver."""

from __future__ import annotations

import argparse
import json
import queue
import re
import sys
from collections import deque
from dataclasses import asdict
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
CORE_SRC = PROJECT_ROOT / "packages" / "bible_parser_core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from bible_parser_core.book_aliases import book_synonyms
from bible_parser_core.parser import DEFAULT_BIBLE, NUMBER_WORDS, parse_live_reference
from bible_parser_core.reference_resolver import (
    resolve_best_reference_candidate,
    resolve_reference_candidates,
)
from tools.holyrics import (
    DEFAULT_HOLYRICS_ACTION,
    default_holyrics_url,
    describe_holyrics_target,
    env_setting,
    live_parsed_ref_to_slide_payload_with_source_text,
    post_holyrics_update,
)


DEFAULT_MODEL_PATH = Path.cwd() / "models" / "vosk-model-small-ru-0.22"
DEFAULT_LOG_DIR = Path.cwd() / ".cache" / "live_verse_vosk" / "vosk_probe"
REFERENCE_WORDS = {
    "апостол",
    "богослова",
    "глава",
    "главы",
    "главе",
    "до",
    "евангелие",
    "из",
    "книга",
    "книги",
    "от",
    "откровение",
    "откройте",
    "откроем",
    "послание",
    "послания",
    "пророк",
    "пророка",
    "псалом",
    "с",
    "стих",
    "стиха",
    "стихи",
    "стихов",
    "читаем",
}
VOSK_SMALL_RU_MISSING_WORDS = {
    "авакум",
    "авдия",
    "авдя",
    "аггея",
    "агей",
    "адия",
    "бытиев",
    "бытья",
    "восемнадцатые",
    "восьмые",
    "девятнадцатые",
    "диания",
    "дияни",
    "езекиля",
    "еклесиаста",
    "есфири",
    "иана",
    "ианна",
    "иезекиля",
    "иоиль",
    "иоиля",
    "иоля",
    "иранно",
    "иссаии",
    "иссайи",
    "иуд",
    "калася",
    "каласянам",
    "колоссянам",
    "колосянам",
    "кохелет",
    "малахии",
    "моса",
    "неемии",
    "неемия",
    "одиннадцатые",
    "оиля",
    "парапоменон",
    "римлиным",
    "семнадцатые",
    "софонии",
    "софония",
    "тринадцатые",
    "фесалоникийцам",
    "фессалоникийцам",
    "филимону",
    "филипийцам",
    "филиппийцам",
    "цартвтретья",
    "четвертая",
    "четвертого",
    "четвертое",
    "четвертой",
    "четвертую",
    "четвертые",
    "четвертый",
    "четырнадцатые",
    "шестнадцатые",
}


class VoskTextBuffer:
    def __init__(self, max_parts: int = 3) -> None:
        self.parts: deque[str] = deque(maxlen=max(1, max_parts))

    def add(self, text: str) -> None:
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            self.parts.append(text)

    def candidates(self) -> list[str]:
        values = list(self.parts)
        candidates: list[str] = []
        for size in range(1, len(values) + 1):
            candidate = " ".join(values[-size:]).strip()
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        return candidates


class JsonlLogger:
    def __init__(self, log_dir: Path, enabled: bool = True) -> None:
        self.enabled = enabled
        self.run_dir: Path | None = None
        self.events_path: Path | None = None
        if not enabled:
            return
        self.run_dir = log_dir / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"

    def write(self, event: str, payload: dict) -> None:
        if not self.enabled or self.events_path is None:
            return
        row = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "event": event,
            **payload,
        }
        with self.events_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    def write_session(self, payload: dict) -> None:
        if not self.enabled or self.run_dir is None:
            return
        (self.run_dir / "session.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def usable_grammar_phrase(phrase: str) -> bool:
    if not phrase or re.search(r"\d", phrase):
        return False
    return not any(token in VOSK_SMALL_RU_MISSING_WORDS for token in phrase.split())


def build_grammar() -> list[str]:
    phrases: set[str] = set()

    def add_phrase(phrase: str) -> None:
        phrase = phrase.lower()
        if usable_grammar_phrase(phrase):
            phrases.add(phrase)

    for canonical, aliases in book_synonyms.items():
        add_phrase(canonical)
        for alias in aliases:
            add_phrase(alias)
    for word in REFERENCE_WORDS:
        add_phrase(word)
    for word in NUMBER_WORDS:
        add_phrase(word)
    phrases.add("[unk]")
    return sorted(phrases)


def grammar_diagnostics(grammar: list[str]) -> dict:
    return {
        "size": len(grammar),
        "contains": {
            "ефесянам": "ефесянам" in grammar,
            "бытие": "бытие" in grammar,
            "псалом": "псалом" in grammar,
            "двадцать": "двадцать" in grammar,
            "четыре": "четыре" in grammar,
            "седьмой": "седьмой" in grammar,
        },
        "filtered_missing_words_count": len(VOSK_SMALL_RU_MISSING_WORDS),
    }


def parsed_payload(text: str, bible_path: Path = DEFAULT_BIBLE, *, show_candidates: bool = False) -> dict:
    parsed = parse_live_reference(text, bible_path=bible_path)
    source = "parser"
    resolved = None
    if parsed is None:
        resolved = resolve_best_reference_candidate(text, bible_path=bible_path)
        if resolved:
            parsed = parse_live_reference(resolved.ref, bible_path=bible_path)
            source = "resolver"

    slide = None
    if parsed:
        slide = live_parsed_ref_to_slide_payload_with_source_text(parsed, f"vosk:{source}", text)

    payload = {
        "text": text,
        "source": source if parsed else None,
        "resolved": asdict(resolved) if resolved else None,
        "parsed": asdict(parsed) if parsed else None,
        "slide": slide,
    }
    if show_candidates:
        payload["candidates"] = [
            asdict(candidate)
            for candidate in resolve_reference_candidates(text, bible_path=bible_path)
        ]
    return payload


def parsed_payload_from_candidates(
    candidates: list[str],
    bible_path: Path = DEFAULT_BIBLE,
    *,
    show_candidates: bool = False,
) -> dict:
    attempts = [
        parsed_payload(candidate, bible_path=bible_path, show_candidates=show_candidates)
        for candidate in candidates
    ]
    attempt_summaries = [
        {
            "text": attempt.get("text"),
            "ref": (attempt.get("parsed") or {}).get("ref"),
            "source": attempt.get("source"),
            "matched": bool(attempt.get("slide")),
        }
        for attempt in attempts
    ]
    for index, payload in enumerate(attempts):
        if payload.get("slide"):
            payload["attempts"] = [
                summary
                for summary_index, summary in enumerate(attempt_summaries)
                if summary_index != index
            ]
            return payload
    payload = attempts[0] if attempts else parsed_payload("", bible_path=bible_path, show_candidates=show_candidates)
    payload["attempts"] = attempt_summaries[1:] if len(attempt_summaries) > 1 else []
    return payload


def payload_summary(payload: dict) -> dict:
    parsed = payload.get("parsed") or {}
    slide = payload.get("slide") or {}
    return {
        "text": payload.get("text"),
        "ref": parsed.get("ref"),
        "book": parsed.get("book"),
        "chapter": parsed.get("chapter"),
        "start_verse": parsed.get("start_verse"),
        "end_verse": parsed.get("end_verse"),
        "source": payload.get("source"),
        "has_slide": bool(slide),
        "attempts": payload.get("attempts") or [],
    }


def publish_holyrics_if_needed(args: argparse.Namespace, payload: dict) -> dict:
    if args.slide_output == "none" or not payload.get("slide"):
        return {"enabled": False}

    ok, reason = post_holyrics_update(args, payload["slide"])
    return {
        "enabled": True,
        "ok": ok,
        "reason": reason,
        "target": describe_holyrics_target(args),
    }


def run_microphone(args: argparse.Namespace) -> int:
    import sounddevice as sd
    from vosk import KaldiRecognizer, Model, SetLogLevel

    audio_queue: queue.Queue[bytes] = queue.Queue()
    grammar = None if args.open_vocabulary else build_grammar()
    logger = JsonlLogger(Path(args.log_dir), enabled=not args.no_log)
    logger.write_session(
        {
            "command": " ".join(sys.argv),
            "model": str(args.model),
            "bible": str(args.bible),
            "samplerate": args.samplerate,
            "blocksize": args.blocksize,
            "device": args.device,
            "open_vocabulary": args.open_vocabulary,
            "vosk_buffer_parts": args.vosk_buffer_parts,
            "slide_output": args.slide_output,
            "holyrics_target": describe_holyrics_target(args),
            "grammar": None if grammar is None else grammar_diagnostics(grammar),
        }
    )
    if logger.run_dir:
        print(f"Vosk log: {logger.run_dir / 'events.jsonl'}")

    def callback(indata, frames, time, status):
        if status:
            print(status, file=sys.stderr)
            logger.write("audio_status", {"status": str(status)})
        audio_queue.put(bytes(indata))

    SetLogLevel(args.vosk_log_level)
    model = Model(str(args.model))
    recognizer_args = [model, args.samplerate]
    if grammar is not None:
        recognizer_args.append(json.dumps(grammar, ensure_ascii=False))
    recognizer = KaldiRecognizer(*recognizer_args)
    recognizer.SetWords(True)
    text_buffer = VoskTextBuffer(args.vosk_buffer_parts)

    stream_kwargs = {
        "samplerate": args.samplerate,
        "blocksize": args.blocksize,
        "dtype": "int16",
        "channels": 1,
        "callback": callback,
    }
    if args.device is not None:
        stream_kwargs["device"] = args.device

    print("Слушаю Vosk. Ctrl+C для выхода.")
    with sd.RawInputStream(**stream_kwargs):
        try:
            while True:
                data = audio_queue.get()
                if recognizer.AcceptWaveform(data):
                    result = json.loads(recognizer.Result())
                    text = result.get("text", "").strip()
                    logger.write("final_raw", {"result": result, "text": text})
                    if text:
                        text_buffer.add(text)
                        candidate_texts = text_buffer.candidates()
                        payload = parsed_payload_from_candidates(
                            candidate_texts,
                            bible_path=args.bible,
                            show_candidates=args.show_candidates,
                        )
                        payload["asr"] = result
                        payload["vosk_text"] = text
                        payload["vosk_buffer"] = list(text_buffer.parts)
                        payload["holyrics"] = publish_holyrics_if_needed(args, payload)
                        logger.write(
                            "parsed",
                            {
                                "vosk_text": text,
                                "vosk_buffer": list(text_buffer.parts),
                                "candidate_texts": candidate_texts,
                                "payload": payload_summary(payload),
                                "holyrics": payload["holyrics"],
                            },
                        )
                        print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    partial_result = json.loads(recognizer.PartialResult())
                    partial = partial_result.get("partial", "")
                    if partial:
                        if args.log_partials:
                            logger.write("partial", {"result": partial_result, "partial": partial})
                        print("...", partial)
        except KeyboardInterrupt:
            print("\nОстановлено.")
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Recognize and resolve Russian live Bible references.")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--bible", type=Path, default=DEFAULT_BIBLE)
    parser.add_argument("--samplerate", type=int, default=16000)
    parser.add_argument("--blocksize", type=int, default=8000)
    parser.add_argument("--device", type=int)
    parser.add_argument("--open-vocabulary", action="store_true", help="Run Vosk without generated grammar.")
    parser.add_argument(
        "--vosk-buffer-parts",
        type=int,
        default=3,
        help="How many final Vosk text fragments to join before parsing.",
    )
    parser.add_argument("--vosk-log-level", type=int, default=-1, help="Vosk log level. Use 0 to show Vosk warnings.")
    parser.add_argument("--show-candidates", action="store_true", help="Print resolver candidate list.")
    parser.add_argument("--text", nargs="+", help="Resolve text without opening the microphone.")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--no-log", action="store_true", help="Disable JSONL logging.")
    parser.add_argument("--log-partials", action="store_true", help="Log Vosk partial results too.")
    parser.add_argument(
        "--slide-output",
        choices=["holyrics", "none"],
        default="holyrics",
        help="Where to send detected references. Default: holyrics.",
    )
    parser.add_argument(
        "--holyrics-url",
        default=default_holyrics_url(),
        help="Holyrics local API base URL. Default: HOLYRICS_URL, HOLYRICS_HOST/HOLYRICS_API_PORT, or auto.",
    )
    parser.add_argument(
        "--holyrics-token",
        default=env_setting("HOLYRICS_TOKEN"),
        help="Holyrics API token. Can also be set via HOLYRICS_TOKEN or .env.",
    )
    parser.add_argument(
        "--holyrics-action",
        default=env_setting("HOLYRICS_ACTION", DEFAULT_HOLYRICS_ACTION),
        help="Holyrics API action. Default: ShowQuickPresentation.",
    )
    parser.add_argument(
        "--holyrics-theme",
        default=env_setting("HOLYRICS_THEME"),
        help="Optional Holyrics theme name for quick presentations.",
    )
    parser.add_argument("--holyrics-timeout", type=float, default=float(env_setting("HOLYRICS_TIMEOUT", "1.5")))
    args = parser.parse_args()

    if args.text:
        grammar = None if args.open_vocabulary else build_grammar()
        logger = JsonlLogger(Path(args.log_dir), enabled=not args.no_log)
        logger.write_session(
            {
                "command": " ".join(sys.argv),
                "mode": "text",
                "model": str(args.model),
                "bible": str(args.bible),
                "open_vocabulary": args.open_vocabulary,
                "slide_output": args.slide_output,
                "holyrics_target": describe_holyrics_target(args),
                "grammar": None if grammar is None else grammar_diagnostics(grammar),
            }
        )
        payload = parsed_payload_from_candidates(
            [" ".join(args.text)],
            bible_path=args.bible,
            show_candidates=args.show_candidates,
        )
        payload["holyrics"] = publish_holyrics_if_needed(args, payload)
        logger.write(
            "text_probe",
            {
                "candidate_texts": [" ".join(args.text)],
                "payload": payload_summary(payload),
                "holyrics": payload["holyrics"],
            },
        )
        if logger.run_dir:
            print(f"Vosk log: {logger.run_dir / 'events.jsonl'}")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["parsed"] else 1

    return run_microphone(args)


if __name__ == "__main__":
    raise SystemExit(main())
