#!/usr/bin/env python3
"""Vosk probe wired to the LiVerse Bible reference resolver."""

from __future__ import annotations

import argparse
import json
import queue
import re
import sys
import webbrowser
import wave
from collections import deque
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote


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
    default_holyrics_url,
    describe_holyrics_target,
    env_setting,
    live_parsed_ref_to_slide_payload_with_source_text,
    post_holyrics_update,
)


DEFAULT_MODEL_PATH = Path.cwd() / "models" / "vosk-model-small-ru-0.22"
DEFAULT_LOG_DIR = Path.cwd() / ".cache" / "liverse" / "vosk_probe"
WELCOME_TEXT = (
    "LiVerse принимает на себя техническую задачу поиска и отображения "
    "библейских ссылок, чтобы вся церковь могла сосредоточиться на слушании, "
    "чтении и размышлении над Словом Божиим."
)
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
    "там",
    "же",
    "конец",
    "конца",
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
    "восьмидесятая",
    "восьмые",
    "девятнадцатые",
    "девяностая",
    "диания",
    "дияни",
    "ёны",
    "эмии",
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
    "немии",
    "немия",
    "неемии",
    "неемия",
    "ниемии",
    "одиннадцатые",
    "оиля",
    "парапоменон",
    "римлиным",
    "семнадцатые",
    "софонии",
    "софония",
    "сотого",
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


class ConsoleStatus:
    def __init__(self, *, debug: bool = False) -> None:
        self.debug = debug
        self.last = ""

    def status(self, message: str) -> None:
        if self.debug or message == self.last:
            return
        self.last = message
        print(f"Статус: {message}", flush=True)

    def debug_json(self, payload: dict) -> None:
        if self.debug:
            print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)


def session_reference_record(payload: dict, action: str = "recognized") -> dict | None:
    slide = payload.get("slide") or {}
    ref = str(slide.get("ref") or "").strip()
    if not ref:
        return None
    return {
        "ref": ref,
        "action": action,
        "asr": str(payload.get("vosk_text") or payload.get("text") or "").strip(),
        "detected_text": str(slide.get("detected_text") or "").strip(),
    }


def append_session_reference(records: list[dict], payload: dict, action: str = "recognized") -> None:
    record = session_reference_record(payload, action=action)
    if not record:
        return
    if records and records[-1].get("ref") == record["ref"] and records[-1].get("action") == record["action"]:
        return
    records.append(record)


def session_references_text(records: list[dict]) -> str:
    refs: list[str] = []
    for record in records:
        ref = str(record.get("ref") or "").strip()
        if ref and ref not in refs:
            refs.append(ref)
    if not refs:
        return ""
    lines = ["Цитаты из проповеди:"]
    lines.extend(f"{index}. {ref}" for index, ref in enumerate(refs, start=1))
    return "\n".join(lines)


def show_session_summary_popup(records: list[dict]) -> None:
    try:
        import tkinter as tk
        from tkinter import font as tkfont
    except Exception as exc:
        print(f"Итоговое окно недоступно: {exc}", flush=True)
        text = session_references_text(records)
        if text:
            print(text, flush=True)
        return

    text = session_references_text(records)
    whatsapp_url = f"https://wa.me/?text={quote(text)}" if text else ""

    root = tk.Tk()
    root.title("LiVerse — итоги сеанса")
    root.attributes("-topmost", True)
    root.configure(bg="#101820")
    root.resizable(True, True)

    width, height = 760, 560
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    x = max(0, (screen_width - width) // 2)
    y = max(0, (screen_height - height) // 2)
    root.geometry(f"{width}x{height}+{x}+{y}")

    title_font = tkfont.Font(family="Segoe UI", size=28, weight="bold")
    body_font = tkfont.Font(family="Segoe UI", size=18)
    button_font = tkfont.Font(family="Segoe UI", size=18, weight="bold")

    tk.Label(
        root,
        text="Распознанные ссылки",
        bg="#101820",
        fg="#ffd166",
        font=title_font,
    ).pack(fill="x", padx=28, pady=(24, 10))

    body = tk.Text(
        root,
        bg="#0f1720",
        fg="#f5f7fa",
        insertbackground="#f5f7fa",
        font=body_font,
        relief="flat",
        wrap="word",
        padx=16,
        pady=16,
        height=12,
    )
    body.pack(fill="both", expand=True, padx=28, pady=(0, 18))
    body.insert("1.0", text or "За этот сеанс ссылки не были распознаны.")
    body.configure(state="disabled")

    buttons = tk.Frame(root, bg="#101820")
    buttons.pack(fill="x", padx=28, pady=(0, 24))

    def share_whatsapp() -> None:
        if whatsapp_url:
            webbrowser.open(whatsapp_url)

    share = tk.Button(
        buttons,
        text="Поделиться в WhatsApp",
        command=share_whatsapp,
        bg="#148447",
        fg="white",
        activebackground="#1aa158",
        activeforeground="white",
        disabledforeground="#9aa4ad",
        font=button_font,
        relief="flat",
        padx=20,
        pady=14,
        state="normal" if whatsapp_url else "disabled",
    )
    close = tk.Button(
        buttons,
        text="Закрыть",
        command=root.destroy,
        bg="#334155",
        fg="white",
        activebackground="#475569",
        activeforeground="white",
        font=button_font,
        relief="flat",
        padx=20,
        pady=14,
    )
    share.pack(side="left", fill="x", expand=True, padx=(0, 10))
    close.pack(side="left", fill="x", expand=True, padx=(10, 0))

    root.bind("<Escape>", lambda _event: root.destroy())
    root.after(100, root.focus_force)
    root.after(150, root.lift)
    root.mainloop()


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


def low_confidence_jeremiah(asr_result: dict | None, *, threshold: float = 0.76) -> bool:
    if not asr_result:
        return False
    for item in asr_result.get("result") or []:
        word = str(item.get("word") or "").lower()
        if not re.fullmatch(r"иереми[яи]", word):
            continue
        try:
            confidence = float(item.get("conf"))
        except (TypeError, ValueError):
            continue
        if confidence <= threshold:
            return True
    return False


def nehemiah_confusable_text(
    text: str,
    bible_path: Path = DEFAULT_BIBLE,
    *,
    asr_result: dict | None = None,
) -> str | None:
    if not re.search(r"\bиереми[яи]\b", text, flags=re.IGNORECASE):
        return None

    replacement = re.sub(r"\bиеремии\b", "неемии", text, flags=re.IGNORECASE)
    replacement = re.sub(r"\bиеремия\b", "неемия", replacement, flags=re.IGNORECASE)
    if replacement == text:
        return None

    nehemiah = parse_live_reference(replacement, bible_path=bible_path)
    if not nehemiah or nehemiah.book != "Неемия":
        return None

    original = parse_live_reference(text, bible_path=bible_path)
    if original is None or low_confidence_jeremiah(asr_result):
        return replacement
    return None


def expand_nehemiah_confusable_candidates(
    candidates: list[str],
    bible_path: Path = DEFAULT_BIBLE,
    *,
    asr_result: dict | None = None,
) -> list[str]:
    expanded: list[str] = []
    for candidate in candidates:
        replacement = nehemiah_confusable_text(candidate, bible_path=bible_path, asr_result=asr_result)
        if replacement and replacement not in expanded:
            expanded.append(replacement)
        if candidate not in expanded:
            expanded.append(candidate)
    return expanded


def likely_explicit_reference(text: str) -> bool:
    lowered = text.lower().replace("ё", "е")
    if not re.search(r"\b(глава|стих|псалом)\b", lowered):
        return False
    for canonical, aliases in book_synonyms.items():
        names = [canonical, *aliases]
        for name in names:
            normalized_name = name.lower().replace("ё", "е")
            if normalized_name and re.search(rf"\b{re.escape(normalized_name)}\b", lowered):
                return True
    return False


def likely_book_only_fragment(text: str) -> bool:
    lowered = text.lower().replace("ё", "е").strip()
    if not lowered or re.search(r"\b(глава|стих|псалом)\b", lowered):
        return False
    words = lowered.split()
    if len(words) > 4:
        return False
    forms = {lowered}
    if len(words) == 1 and lowered.endswith("а") and len(lowered) > 3:
        forms.add(lowered[:-1])
    for canonical, aliases in book_synonyms.items():
        names = [canonical, *aliases]
        for name in names:
            normalized_name = name.lower().replace("ё", "е")
            if normalized_name in forms:
                return True
    return False


def same_place_only_fragment(text: str) -> bool:
    return re.fullmatch(r"\s*там\s+же\s*", text.lower().replace("ё", "е")) is not None


def same_place_candidates(candidates: list[str], last_parsed: dict | None) -> list[str]:
    if not last_parsed:
        return candidates
    book = last_parsed.get("book")
    chapter = last_parsed.get("chapter")
    if not book or not chapter:
        return candidates

    expanded: list[str] = []
    for candidate in candidates:
        expanded.append(candidate)
        if not re.search(r"\bтам\s+же\b", candidate.lower().replace("ё", "е")):
            continue
        suffix = re.sub(r"\bтам\s+же\b", "", candidate, flags=re.IGNORECASE).strip()
        if suffix:
            expanded.append(f"{book} {chapter} глава {suffix}")
    return expanded


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
            first_text = str(attempts[0].get("text") or "") if attempts else ""
            if index > 0 and (
                likely_explicit_reference(first_text)
                or likely_book_only_fragment(first_text)
                or same_place_only_fragment(first_text)
            ):
                first_payload = attempts[0]
                first_payload["attempts"] = attempt_summaries[1:]
                first_payload["blocked_stale_context"] = True
                return first_payload
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
    if args.slide_output not in {"holyrics", "both"} or not payload.get("slide"):
        return {"enabled": False}

    ok, reason = post_holyrics_update(args, payload["slide"])
    return {
        "enabled": True,
        "ok": ok,
        "reason": reason,
        "target": describe_holyrics_target(args),
    }


def publish_web_if_needed(args: argparse.Namespace, payload: dict) -> dict:
    if args.slide_output not in {"web", "both"} or not payload.get("slide"):
        return {"enabled": False}
    from tools.slide_server import set_current_slide

    slide = set_current_slide(payload["slide"])
    return {"enabled": True, "ok": True, "slide": slide}


def popup_approval_decision(slide: dict) -> str:
    try:
        import tkinter as tk
        from tkinter import font as tkfont
    except Exception as exc:
        raise RuntimeError(f"popup_unavailable:{exc}") from exc

    decision = {"action": "reject"}
    root = tk.Tk()
    root.title("LiVerse")
    root.attributes("-topmost", True)
    root.configure(bg="#101820")
    root.resizable(True, True)

    width, height = 980, 360
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    x = max(0, (screen_width - width) // 2)
    y = max(0, (screen_height - height) // 2)
    root.geometry(f"{width}x{height}+{x}+{y}")

    ref_font = tkfont.Font(family="Segoe UI", size=54, weight="bold")
    hint_font = tkfont.Font(family="Segoe UI", size=24, weight="bold")
    button_font = tkfont.Font(family="Segoe UI", size=22, weight="bold")

    tk.Label(
        root,
        text=str(slide.get("ref") or "Найдена цитата"),
        bg="#101820",
        fg="#ffd166",
        font=ref_font,
        wraplength=900,
        justify="center",
    ).pack(fill="x", padx=36, pady=(34, 12))

    tk.Label(
        root,
        text="Enter - принять     Esc или Space - отклонить",
        bg="#101820",
        fg="#c8d2dc",
        font=hint_font,
    ).pack(fill="x", padx=36, pady=(8, 18))

    buttons = tk.Frame(root, bg="#101820")
    buttons.pack(fill="x", padx=36, pady=(0, 30))

    def close(action: str) -> None:
        decision["action"] = action
        root.destroy()

    approve = tk.Button(
        buttons,
        text="Принять",
        command=lambda: close("approve"),
        bg="#148447",
        fg="white",
        activebackground="#1aa158",
        activeforeground="white",
        font=button_font,
        relief="flat",
        padx=24,
        pady=16,
    )
    reject = tk.Button(
        buttons,
        text="Отклонить",
        command=lambda: close("reject"),
        bg="#9b3030",
        fg="white",
        activebackground="#b73a3a",
        activeforeground="white",
        font=button_font,
        relief="flat",
        padx=24,
        pady=16,
    )
    approve.pack(side="left", fill="x", expand=True, padx=(0, 10))
    reject.pack(side="left", fill="x", expand=True, padx=(10, 0))

    root.bind("<Return>", lambda _event: close("approve"))
    root.bind("<Escape>", lambda _event: close("reject"))
    root.bind("<space>", lambda _event: close("reject"))
    root.protocol("WM_DELETE_WINDOW", lambda: close("reject"))
    root.after(100, root.focus_force)
    root.after(150, root.lift)
    root.mainloop()
    return decision["action"]


def publish_after_approval(args: argparse.Namespace, payload: dict) -> dict:
    return {
        "holyrics": publish_holyrics_if_needed(args, payload),
        "web": publish_web_if_needed(args, payload),
    }


def approve_with_popup(args: argparse.Namespace, payload: dict) -> dict:
    slide = payload.get("slide")
    if not slide:
        return {"enabled": False}
    try:
        action = popup_approval_decision(slide)
    except Exception as exc:
        return {"enabled": True, "ok": False, "reason": str(exc)}
    if action != "approve":
        return {"enabled": True, "ok": True, "action": "reject"}
    output = publish_after_approval(args, payload)
    return {"enabled": True, "ok": True, "action": "approve", **output}


def submit_for_approval(args: argparse.Namespace, payload: dict) -> dict:
    if not payload.get("slide"):
        return {"enabled": False}
    from tools.slide_server import submit_candidate

    candidate = submit_candidate(payload["slide"])
    return {"enabled": True, "ok": True, "candidate": candidate}


def start_slide_server_if_needed(args: argparse.Namespace):
    web_approval = args.require_approval and args.approval_ui == "web"
    needs_server = args.start_slide_server or web_approval or args.slide_output in {"web", "both"}
    if not needs_server:
        return None

    from tools.slide_server import set_current_slide, start_server_thread

    def decision_callback(action: str, candidate: dict) -> tuple[bool, str]:
        if action == "reject":
            return True, ""

        if args.slide_output in {"holyrics", "both"}:
            ok, reason = post_holyrics_update(args, candidate)
            if not ok:
                return ok, reason
        if args.slide_output in {"web", "both"}:
            set_current_slide(candidate)
        return True, ""

    return start_server_thread(
        args.slide_host,
        args.slide_port,
        decision_callback=decision_callback if web_approval else None,
        open_qr=args.open_operator_qr and web_approval,
        open_browser=args.open_operator_browser,
        print_qr=args.print_operator_qr,
    )


def publish_payload(args: argparse.Namespace, payload: dict) -> dict:
    if args.require_approval:
        if args.approval_ui == "popup":
            popup_result = approve_with_popup(args, payload)
            return {
                "approval": popup_result,
                "holyrics": popup_result.get("holyrics", {"enabled": False, "reason": "rejected_or_no_slide"}),
                "web": popup_result.get("web", {"enabled": False, "reason": "rejected_or_no_slide"}),
            }
        return {
            "approval": submit_for_approval(args, payload),
            "holyrics": {"enabled": False, "reason": "waiting_for_approval"},
            "web": {"enabled": False, "reason": "waiting_for_approval"},
        }
    return {
        "approval": {"enabled": False},
        "holyrics": publish_holyrics_if_needed(args, payload),
        "web": publish_web_if_needed(args, payload),
    }


def approval_action(output: dict) -> str:
    approval = output.get("approval") or {}
    action = str(approval.get("action") or "")
    if action in {"approve", "reject"}:
        return action
    if approval.get("reason") == "waiting_for_approval" or output.get("holyrics", {}).get("reason") == "waiting_for_approval":
        return "waiting"
    if output.get("holyrics", {}).get("ok") or output.get("web", {}).get("ok"):
        return "sent"
    return "recognized"


def run_microphone(args: argparse.Namespace) -> int:
    import sounddevice as sd
    from vosk import KaldiRecognizer, Model, SetLogLevel

    audio_queue: queue.Queue[bytes] = queue.Queue()
    console = ConsoleStatus(debug=args.debug_console)
    session_refs: list[dict] = []
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
            "log_audio": args.log_audio,
            "slide_output": args.slide_output,
            "require_approval": args.require_approval,
            "approval_ui": args.approval_ui,
            "slide_server": f"http://{args.slide_host}:{args.slide_port}" if (
                args.start_slide_server
                or (args.require_approval and args.approval_ui == "web")
                or args.slide_output in {"web", "both"}
            ) else None,
            "holyrics_target": describe_holyrics_target(args),
            "grammar": None if grammar is None else grammar_diagnostics(grammar),
        }
    )
    print(WELCOME_TEXT, flush=True)
    if logger.run_dir and args.print_log_path:
        print(f"Vosk log: {logger.run_dir / 'events.jsonl'}")
    start_slide_server_if_needed(args)

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
    last_parsed: dict | None = None
    audio_log = None
    if args.log_audio and logger.run_dir:
        audio_log = wave.open(str(logger.run_dir / "audio.wav"), "wb")
        audio_log.setnchannels(1)
        audio_log.setsampwidth(2)
        audio_log.setframerate(args.samplerate)
        logger.write("audio_log", {"path": str(logger.run_dir / "audio.wav")})

    stream_kwargs = {
        "samplerate": args.samplerate,
        "blocksize": args.blocksize,
        "dtype": "int16",
        "channels": 1,
        "callback": callback,
    }
    if args.device is not None:
        stream_kwargs["device"] = args.device

    console.status("слушаю")
    with sd.RawInputStream(**stream_kwargs):
        try:
            while True:
                data = audio_queue.get()
                if audio_log:
                    audio_log.writeframes(data)
                if recognizer.AcceptWaveform(data):
                    result = json.loads(recognizer.Result())
                    text = result.get("text", "").strip()
                    logger.write("final_raw", {"result": result, "text": text})
                    if text:
                        console.status("распознаю")
                        text_buffer.add(text)
                        candidate_texts = same_place_candidates(text_buffer.candidates(), last_parsed)
                        candidate_texts = expand_nehemiah_confusable_candidates(
                            candidate_texts,
                            bible_path=args.bible,
                            asr_result=result,
                        )
                        payload = parsed_payload_from_candidates(
                            candidate_texts,
                            bible_path=args.bible,
                            show_candidates=args.show_candidates,
                        )
                        payload["asr"] = result
                        payload["vosk_text"] = text
                        payload["vosk_buffer"] = list(text_buffer.parts)
                        payload["output"] = publish_payload(args, payload)
                        if payload.get("slide"):
                            action = approval_action(payload["output"])
                            append_session_reference(session_refs, payload, action=action)
                            ref = str((payload.get("parsed") or {}).get("ref") or payload["slide"].get("ref"))
                            if action == "waiting":
                                console.status(f"найдена ссылка {ref}, ожидает подтверждения")
                            elif action == "approve":
                                console.status(f"отправлено в Holyrics: {ref}")
                            elif action == "reject":
                                console.status(f"отклонено: {ref}")
                            else:
                                console.status(f"найдена ссылка: {ref}")
                        else:
                            console.status("слушаю")
                        if payload.get("parsed"):
                            last_parsed = payload["parsed"]
                        logger.write(
                            "parsed",
                            {
                                "vosk_text": text,
                                "vosk_buffer": list(text_buffer.parts),
                                "candidate_texts": candidate_texts,
                                "payload": payload_summary(payload),
                                "output": payload["output"],
                            },
                        )
                        console.debug_json(payload)
                else:
                    partial_result = json.loads(recognizer.PartialResult())
                    partial = partial_result.get("partial", "")
                    if partial:
                        if args.log_partials:
                            logger.write("partial", {"result": partial_result, "partial": partial})
                        if args.debug_console:
                            print("...", partial, flush=True)
        except KeyboardInterrupt:
            print("\nОстановлено.", flush=True)
            if args.session_summary_popup:
                show_session_summary_popup(session_refs)
            return 0
        finally:
            if audio_log:
                audio_log.close()


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
    parser.add_argument("--debug-console", action="store_true", help="Print full JSON payloads and Vosk partials.")
    parser.add_argument("--print-log-path", action="store_true", help="Print JSONL log path on startup.")
    parser.add_argument("--text", nargs="+", help="Resolve text without opening the microphone.")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--no-log", action="store_true", help="Disable JSONL logging.")
    parser.add_argument("--log-partials", action="store_true", help="Log Vosk partial results too.")
    parser.add_argument("--log-audio", action="store_true", help="Save microphone audio to audio.wav in the run log.")
    parser.add_argument(
        "--slide-output",
        choices=["holyrics", "web", "both", "none"],
        default="holyrics",
        help="Where to send approved references. Default: holyrics.",
    )
    parser.add_argument(
        "--require-approval",
        action="store_true",
        help="Wait for operator approval before sending to slide output.",
    )
    parser.add_argument(
        "--approval-ui",
        choices=["web", "popup"],
        default="web",
        help="Approval UI for --require-approval. Use popup for a local keyboard-driven window.",
    )
    parser.add_argument("--start-slide-server", action="store_true", help="Start local web slide/operator server.")
    parser.add_argument("--slide-host", default="0.0.0.0", help="Web slide server host.")
    parser.add_argument("--slide-port", type=int, default=8765, help="Web slide server port.")
    parser.add_argument(
        "--open-operator-qr",
        dest="open_operator_qr",
        action="store_true",
        default=True,
        help="Open generated operator QR PNG. Enabled by default.",
    )
    parser.add_argument(
        "--no-open-operator-qr",
        dest="open_operator_qr",
        action="store_false",
        help="Do not open the generated operator QR PNG.",
    )
    parser.add_argument("--print-operator-qr", action="store_true", help="Print QR as ASCII in the console.")
    parser.add_argument("--open-operator-browser", action="store_true", help="Open operator UI on this computer.")
    parser.add_argument(
        "--no-session-summary-popup",
        dest="session_summary_popup",
        action="store_false",
        help="Do not show recognized references popup when LiVerse stops.",
    )
    parser.add_argument(
        "--holyrics-url",
        default=default_holyrics_url(),
        help="Holyrics local API base URL. Default: HOLYRICS_URL, HOLYRICS_HOST/HOLYRICS_PORT, or http://localhost:8091.",
    )
    parser.add_argument(
        "--holyrics-token",
        default=env_setting("HOLYRICS_TOKEN"),
        help="Holyrics API token. Can also be set via HOLYRICS_TOKEN or .env.",
    )
    parser.add_argument("--holyrics-timeout", type=float, default=float(env_setting("HOLYRICS_TIMEOUT", "1.5")))
    parser.set_defaults(session_summary_popup=True)
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
                "require_approval": args.require_approval,
                "approval_ui": args.approval_ui,
                "holyrics_target": describe_holyrics_target(args),
                "grammar": None if grammar is None else grammar_diagnostics(grammar),
            }
        )
        start_slide_server_if_needed(args)
        candidate_texts = expand_nehemiah_confusable_candidates(
            [" ".join(args.text)],
            bible_path=args.bible,
        )
        payload = parsed_payload_from_candidates(
            candidate_texts,
            bible_path=args.bible,
            show_candidates=args.show_candidates,
        )
        payload["output"] = publish_payload(args, payload)
        logger.write(
            "text_probe",
            {
                "candidate_texts": candidate_texts,
                "payload": payload_summary(payload),
                "output": payload["output"],
            },
        )
        if logger.run_dir and args.print_log_path:
            print(f"Vosk log: {logger.run_dir / 'events.jsonl'}")
        ConsoleStatus(debug=args.debug_console).debug_json(payload)
        if not args.debug_console:
            ref = (payload.get("parsed") or {}).get("ref")
            print(f"Результат: {ref or 'ссылка не найдена'}", flush=True)
        return 0 if payload["parsed"] else 1

    return run_microphone(args)


if __name__ == "__main__":
    raise SystemExit(main())
