#!/usr/bin/env python3
"""Vosk probe wired to the LiVerse Bible reference resolver."""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import webbrowser
import wave
from datetime import datetime
from importlib import resources
from pathlib import Path
from urllib.parse import quote


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
CORE_SRC = PROJECT_ROOT / "packages" / "bible_parser_core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from bible_parser_core.live_pipeline import (
    LiveReferencePipeline,
    add_risk_score,
    build_grammar,
    expand_nehemiah_confusable_candidates,
    expand_joel_confusable_candidates,
    grammar_diagnostics,
    match_sermon_plan_slide,
    parsed_payload_from_candidates as core_parsed_payload_from_candidates,
    sermon_plan_grammar_phrases,
)
from bible_parser_core.parser import DEFAULT_BIBLE
from bible_parser_core.risk_model import load_risk_model, score_payload_with_model
from tools.holyrics import (
    MIN_RECOMMENDED_HOLYRICS_VERSION,
    REQUIRED_HOLYRICS_PERMISSIONS,
    THEME_HOLYRICS_PERMISSIONS,
    check_holyrics_api_server,
    default_holyrics_url,
    describe_holyrics_target,
    env_setting,
    get_holyrics_current_presentation,
    get_holyrics_theme_options,
    show_holyrics_text_slide,
    post_holyrics_update,
)


DEFAULT_MODEL_PATH = Path.cwd() / "models" / "vosk-model-small-ru-0.22"
DEFAULT_LOG_DIR = Path.cwd() / ".cache" / "liverse" / "vosk_probe"
HOLYRICS_SETUP_NOTICE_MARKER = Path.cwd() / ".cache" / "liverse" / "holyrics_setup_notice_shown"
STARTUP_SETTINGS_ENV = "LIVERSE_STARTUP_SETTINGS"
WELCOME_TEXT = (
    "LiVerse принимает на себя техническую задачу поиска и отображения "
    "библейских ссылок, чтобы вся церковь могла сосредоточиться на слушании, "
    "чтении и размышлении над Словом Божиим."
)
ENTER_KEYS = {"\r", "\n"}
SPACE_KEYS = {" "}
TAB_KEYS = {"\t"}
EDIT_SETTINGS_KEYS = {"e", "E"}
QUIT_KEYS = {"q", "Q"}


def startup_settings_path() -> Path:
    explicit_path = os.environ.get(STARTUP_SETTINGS_ENV)
    if explicit_path:
        return Path(explicit_path).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base_dir = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base_dir / "liverse" / "settings.json"


def load_startup_settings(path: Path | None = None) -> dict:
    selected_path = path or startup_settings_path()
    try:
        data = json.loads(selected_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"LiVerse: не удалось прочитать настройки запуска {selected_path}: {exc}", flush=True)
        return {}
    return data if isinstance(data, dict) else {}


def save_startup_settings(args: argparse.Namespace) -> None:
    if not getattr(args, "_liverse_startup_settings_enabled", False):
        return
    path = startup_settings_path()
    payload = {
        "version": 1,
        "run_mode": current_run_mode(args),
        "approval_ui": str(getattr(args, "approval_ui", "web") or "web"),
        "holyrics_theme": str(getattr(args, "holyrics_theme", "") or ""),
        "holyrics_quick_minutes": float(getattr(args, "holyrics_quick_minutes", 0.0) or 0.0),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"LiVerse: не удалось сохранить настройки запуска {path}: {exc}", flush=True)


def cli_option_present(*names: str) -> bool:
    arguments = sys.argv[1:]
    for argument in arguments:
        for name in names:
            if argument == name or argument.startswith(f"{name}="):
                return True
    return False


def setting_was_explicit(*cli_names: str, env_name: str | None = None) -> bool:
    if cli_option_present(*cli_names):
        return True
    return bool(env_name and env_setting(env_name))


def current_run_mode(args: argparse.Namespace) -> str:
    if getattr(args, "require_approval", False):
        return "approval"
    if getattr(args, "semi_auto_approval", False):
        return "semi_auto"
    return "auto"


def apply_run_mode(args: argparse.Namespace, mode: str) -> None:
    args.require_approval = mode == "approval"
    args.semi_auto_approval = mode == "semi_auto"


def run_mode_label(mode: str) -> str:
    if mode == "approval":
        return "подтверждать каждую ссылку"
    if mode == "semi_auto":
        return "полуавтомат"
    return "автомат"


def approval_ui_label(value: str) -> str:
    return "всплывающее окно" if value == "popup" else "web-интерфейс"


def format_timecode(seconds: float) -> str:
    total_milliseconds = max(0, int(round(seconds * 1000)))
    milliseconds = total_milliseconds % 1000
    total_seconds = total_milliseconds // 1000
    seconds_part = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours:02d}:{minutes:02d}:{seconds_part:02d}.{milliseconds:03d}"


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

    def write_trigger_case(self, payload: dict) -> None:
        if not self.enabled or self.run_dir is None:
            return
        path = self.run_dir / "trigger_cases.jsonl"
        row = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            **payload,
        }
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


def holyrics_output_enabled(args: argparse.Namespace) -> bool:
    return args.slide_output in {"holyrics", "both"}


def print_holyrics_setup_notice_once(args: argparse.Namespace) -> None:
    if not holyrics_output_enabled(args) or HOLYRICS_SETUP_NOTICE_MARKER.exists():
        return
    if os.name != "nt":
        return

    print("", flush=True)
    print("Первичная настройка Holyrics для LiVerse", flush=True)
    print("Включите Holyrics API Server Local. Порт по умолчанию: 8091.", flush=True)
    print("Если в Holyrics указан другой порт, запишите его в .env как HOLYRICS_PORT.", flush=True)
    print("Откройте Holyrics -> Settings -> API Server -> Manage permissions.", flush=True)
    print("Для API token, указанного в HOLYRICS_TOKEN, включите разрешения:", flush=True)
    for permission in REQUIRED_HOLYRICS_PERMISSIONS:
        print(f"  - {permission}", flush=True)
    print("", flush=True)
    print(
        f"Проверьте версию Holyrics. Если версия ниже {MIN_RECOMMENDED_HOLYRICS_VERSION}, "
        "обновите Holyrics с официальной страницы загрузки: https://holyrics.com.br/download.html",
        flush=True,
    )
    print("После проверки нажмите Enter.", flush=True)
    input()
    HOLYRICS_SETUP_NOTICE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    HOLYRICS_SETUP_NOTICE_MARKER.write_text("shown\n", encoding="utf-8")


def ask_holyrics_theme_name(args: argparse.Namespace) -> None:
    if not holyrics_output_enabled(args) or args.text or not sys.stdin.isatty():
        return

    if getattr(args, "_liverse_skip_holyrics_theme_question", False):
        theme = str(getattr(args, "holyrics_theme", "") or "").strip()
        if theme:
            print(f"Holyrics: используется последняя тема: {theme}", flush=True)
        else:
            print("Holyrics: используется тема Bible module по умолчанию.", flush=True)
        return

    print("", flush=True)
    print("Выбор темы Holyrics", flush=True)
    if not args.holyrics_token:
        print("Holyrics: HOLYRICS_TOKEN не задан, список тем получить нельзя.", flush=True)
        args.holyrics_theme = ""
        setattr(args, "_holyrics_theme_id", "")
        return

    result = get_holyrics_theme_options(args)
    if not result.get("ok"):
        if result.get("permission_missing"):
            print(
                "Holyrics: в API token не хватает разрешения GetThemes, "
                "поэтому список тем получить нельзя.",
                flush=True,
            )
            print(
                "Откройте Holyrics -> Settings -> API Server -> Manage permissions "
                "и включите GetThemes, если хотите выбирать тему при запуске.",
                flush=True,
            )
        else:
            print("Holyrics: список тем получить не удалось.", flush=True)
            print(f"Техническая причина: {result.get('reason')}", flush=True)
        args.holyrics_theme = ""
        setattr(args, "_holyrics_theme_id", "")
        print("Holyrics: будет использована тема Bible module по умолчанию.", flush=True)
        return

    themes = list(result.get("themes") or [])
    if not themes:
        print("Holyrics: сохранённые темы не найдены.", flush=True)
        args.holyrics_theme = ""
        setattr(args, "_holyrics_theme_id", "")
        print("Holyrics: будет использована тема Bible module по умолчанию.", flush=True)
        return

    print("0. Тема Bible module по умолчанию", flush=True)
    for index, theme in enumerate(themes, start=1):
        print(f"{index}. {theme['name']}", flush=True)
    print("Введите номер темы. Enter - тема Bible module по умолчанию.", flush=True)

    while True:
        choice = input("> ").strip()
        if not choice or choice == "0":
            args.holyrics_theme = ""
            setattr(args, "_holyrics_theme_id", "")
            print("Holyrics: будет использована тема Bible module по умолчанию.", flush=True)
            return
        try:
            index = int(choice)
        except ValueError:
            print("Введите номер из списка или нажмите Enter для темы по умолчанию.", flush=True)
            continue
        if 1 <= index <= len(themes):
            theme = themes[index - 1]
            args.holyrics_theme = theme["name"]
            setattr(args, "_holyrics_theme_id", theme["id"])
            print(f"Holyrics: выбрана тема: {theme['name']}", flush=True)
            return
        print("Введите номер из списка или нажмите Enter для темы по умолчанию.", flush=True)


def parse_holyrics_quick_duration_minutes(value: str, *, default_minutes: float = 1.0) -> float | None:
    raw = value.strip().replace(",", ".").casefold()
    if not raw:
        return default_minutes
    seconds_match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(?:s|sec|secs|сек|секунд[аы]?|с)", raw)
    if seconds_match:
        return float(seconds_match.group(1)) / 60.0
    minutes_match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(?:m|min|mins|мин|минут[аы]?)?", raw)
    if minutes_match:
        return float(minutes_match.group(1))
    return None


def format_holyrics_quick_duration(minutes: float) -> str:
    if minutes == 0:
        return "0 мин."
    seconds = minutes * 60.0
    if seconds < 60:
        return f"{seconds:g} сек."
    return f"{minutes:g} мин."


def ask_holyrics_quick_presentation_minutes(args: argparse.Namespace) -> None:
    if not holyrics_output_enabled(args) or args.text or not sys.stdin.isatty():
        return
    try:
        current = float(getattr(args, "holyrics_quick_minutes", 0.0) or 0.0)
    except (TypeError, ValueError):
        current = 0.0
    if getattr(args, "_liverse_skip_holyrics_quick_question", False):
        if current == 0:
            print("Holyrics: автоматическое закрытие цитаты выключено.", flush=True)
        else:
            print(f"Holyrics: время показа цитаты {format_holyrics_quick_duration(current)}", flush=True)
        return
    full_startup_setup = bool(getattr(args, "_liverse_full_startup_setup", False))
    explicit_quick_minutes = setting_was_explicit("--holyrics-quick-minutes", env_name="HOLYRICS_QUICK_MINUTES")
    if current > 0 and (not full_startup_setup or explicit_quick_minutes):
        print(f"Holyrics: время показа цитаты {format_holyrics_quick_duration(current)}", flush=True)
        return

    print("", flush=True)
    print("Введите время показа цитаты.", flush=True)
    default_minutes = current if current > 0 else 1.0
    print(f"Enter — {format_holyrics_quick_duration(default_minutes)}; 0 — не закрывать автоматически.", flush=True)
    print("Можно ввести минуты: 1, 0.5; или секунды: 30s, 30 сек.", flush=True)
    while True:
        minutes = parse_holyrics_quick_duration_minutes(input("> "), default_minutes=default_minutes)
        if minutes is None:
            print("Введите число минут, например 1 или 0.5, либо секунды, например 30s или 30 сек.", flush=True)
            continue
        if minutes < 0:
            print("Введите 0 или положительное время.", flush=True)
            continue
        args.holyrics_quick_minutes = minutes
        if minutes == 0:
            print("Holyrics: автоматическое закрытие цитаты выключено.", flush=True)
        else:
            print(f"Holyrics: цитата будет показана {format_holyrics_quick_duration(minutes)}", flush=True)
        return


def check_holyrics_startup(args: argparse.Namespace, logger: JsonlLogger | None = None) -> None:
    if not holyrics_output_enabled(args):
        return

    if not args.holyrics_token:
        print("", flush=True)
        print("Holyrics: HOLYRICS_TOKEN не задан. Вывод в Holyrics работать не будет.", flush=True)
        print("Укажите token из Holyrics -> Settings -> API Server -> Manage permissions в файле .env.", flush=True)
        return

    result = check_holyrics_api_server(args)
    if logger:
        logger.write("holyrics_startup_check", result)

    if result.get("ok"):
        if not result.get("token_info_ok"):
            print("Holyrics: API Server доступен.", flush=True)
            print("Holyrics: автоматически проверить версию и разрешения token не удалось.", flush=True)
            print(
                f"Проверьте версию Holyrics вручную. Если версия ниже {MIN_RECOMMENDED_HOLYRICS_VERSION}, "
                "обновите Holyrics.",
                flush=True,
            )
            print("Также проверьте, что API token имеет разрешения:", flush=True)
            for permission in REQUIRED_HOLYRICS_PERMISSIONS:
                print(f"  - {permission}", flush=True)
            if str(getattr(args, "holyrics_theme", "") or "").strip():
                for permission in THEME_HOLYRICS_PERMISSIONS:
                    print(f"  - {permission}", flush=True)
            print(f"Техническая причина: {result.get('token_info_reason')}", flush=True)
            return

        version = str(result.get("version") or "").strip()
        if version:
            print(f"Holyrics: API Server доступен, версия {version}.", flush=True)
        else:
            print("Holyrics: API Server доступен. Версию автоматически определить не удалось.", flush=True)
            print(
                f"Проверьте версию Holyrics вручную. Если версия ниже {MIN_RECOMMENDED_HOLYRICS_VERSION}, "
                "обновите Holyrics.",
                flush=True,
            )
        missing_permissions = result.get("missing_permissions") or []
        if missing_permissions:
            print("Holyrics: в API token не хватает разрешений:", flush=True)
            for permission in missing_permissions:
                print(f"  - {permission}", flush=True)
            print("Откройте Holyrics -> Settings -> API Server -> Manage permissions.", flush=True)
        return

    print("", flush=True)
    print("Holyrics: API Server сейчас недоступен.", flush=True)
    print("Проверьте, что Holyrics запущен, API Server Local включён, а порт совпадает с .env.", flush=True)
    print("По умолчанию LiVerse пробует порт 8091. Если в Holyrics указан другой порт, задайте HOLYRICS_PORT в .env.", flush=True)
    print("Также проверьте, что API token имеет разрешения:", flush=True)
    for permission in REQUIRED_HOLYRICS_PERMISSIONS:
        print(f"  - {permission}", flush=True)
    if str(getattr(args, "holyrics_theme", "") or "").strip():
        for permission in THEME_HOLYRICS_PERMISSIONS:
            print(f"  - {permission}", flush=True)
    print(f"Техническая причина: {result.get('reason')}", flush=True)


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


def read_single_key() -> str:
    if os.name == "nt":
        import msvcrt

        return msvcrt.getwch()

    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def ask_enter_or_space(question: str, *, enter_label: str, space_label: str) -> bool:
    print(question, flush=True)
    print(f"Enter — {enter_label}; Space — {space_label}", flush=True)
    while True:
        key = read_single_key()
        if key in ENTER_KEYS:
            print(enter_label, flush=True)
            return True
        if key in SPACE_KEYS:
            print(space_label, flush=True)
            return False


def ask_run_mode() -> str:
    print("Режим работы LiVerse", flush=True)
    print("Enter — подтверждать каждую ссылку; Tab — полуавтомат; Space — полностью автоматический режим", flush=True)
    while True:
        key = read_single_key()
        if key in ENTER_KEYS:
            print("подтверждать каждую ссылку", flush=True)
            return "approval"
        if key in TAB_KEYS:
            print("полуавтомат", flush=True)
            return "semi_auto"
        if key in SPACE_KEYS:
            print("полностью автоматический режим", flush=True)
            return "auto"


def ask_priority_run_mode(settings: dict) -> tuple[str, bool]:
    print("Режим работы LiVerse", flush=True)
    if settings:
        print("Остальные настройки будут взяты из прошлого запуска.", flush=True)
        print(f"Последний режим: {run_mode_label(str(settings.get('run_mode') or 'semi_auto'))}", flush=True)
        print(f"Подтверждение: {approval_ui_label(str(settings.get('approval_ui') or 'web'))}", flush=True)
        theme = str(settings.get("holyrics_theme") or "").strip()
        print(f"Тема Holyrics: {theme or 'Bible module по умолчанию'}", flush=True)
        try:
            quick_minutes = float(settings.get("holyrics_quick_minutes") or 0.0)
        except (TypeError, ValueError):
            quick_minutes = 0.0
        print(f"Время показа: {format_holyrics_quick_duration(quick_minutes)}", flush=True)
    else:
        print("Сохранённых настроек пока нет: после выбора режима LiVerse задаст остальные вопросы.", flush=True)
    print("", flush=True)
    print("Enter — полуавтомат; Space — автомат; E — полная настройка; Q — выход", flush=True)
    while True:
        key = read_single_key()
        if key in ENTER_KEYS:
            print("полуавтомат", flush=True)
            return "semi_auto", not settings
        if key in SPACE_KEYS:
            print("автомат", flush=True)
            return "auto", not settings
        if key in EDIT_SETTINGS_KEYS:
            print("полная настройка", flush=True)
            return ask_run_mode(), True
        if key in QUIT_KEYS:
            print("выход", flush=True)
            raise SystemExit(0)


def apply_saved_startup_settings(args: argparse.Namespace, settings: dict) -> None:
    if not settings:
        return
    if not setting_was_explicit("--approval-ui"):
        approval_ui = str(settings.get("approval_ui") or "").strip()
        if approval_ui in {"web", "popup"}:
            args.approval_ui = approval_ui
    if not setting_was_explicit("--holyrics-theme", env_name="HOLYRICS_THEME"):
        args.holyrics_theme = str(settings.get("holyrics_theme") or "")
        setattr(args, "_holyrics_theme_id", "")
    if not setting_was_explicit("--holyrics-quick-minutes", env_name="HOLYRICS_QUICK_MINUTES"):
        try:
            args.holyrics_quick_minutes = float(settings.get("holyrics_quick_minutes") or 0.0)
        except (TypeError, ValueError):
            args.holyrics_quick_minutes = 0.0


def configure_interactive_approval_mode(args: argparse.Namespace) -> None:
    if not args.ask_approval_mode or args.text:
        return
    if not sys.stdin.isatty():
        print("Интерактивный выбор режима недоступен: консоль не принимает ввод.", flush=True)
        return

    settings = load_startup_settings()
    apply_saved_startup_settings(args, settings)
    setattr(args, "_liverse_startup_settings_enabled", True)

    if cli_option_present("--require-approval"):
        mode = "approval"
        full_setup = False
        print("Режим подтверждения включён параметром --require-approval.", flush=True)
    elif args.semi_auto_approval:
        mode = "semi_auto"
        full_setup = False
        print("Полуавтоматический режим включён параметром --semi-auto-approval.", flush=True)
    else:
        mode, full_setup = ask_priority_run_mode(settings)
    apply_run_mode(args, mode)

    setattr(args, "_liverse_full_startup_setup", full_setup)
    if not full_setup:
        setattr(args, "_liverse_skip_holyrics_theme_question", True)
        setattr(args, "_liverse_skip_holyrics_quick_question", True)
    if mode == "auto" or not full_setup:
        return

    use_web = ask_enter_or_space(
        "Подтверждение через веб-интерфейс или во всплывающем окне?",
        enter_label="веб-интерфейс",
        space_label="всплывающее окно",
    )
    args.approval_ui = "web" if use_web else "popup"


def default_risk_model_path() -> Path:
    return Path(str(resources.files("bible_parser_core").joinpath("data/risk_model.json")))


def load_runtime_risk_model(args: argparse.Namespace) -> None:
    args.risk_model_data = None
    if not args.semi_auto_approval or args.require_approval:
        return
    if not args.risk_model.exists():
        raise SystemExit(f"Модель полуавтоматического режима не найдена: {args.risk_model}")
    model = load_risk_model(args.risk_model)
    if args.risk_threshold > 0:
        model["recommended_threshold"] = args.risk_threshold
    args.risk_model_data = model
    threshold = float(model.get("recommended_threshold") or 0.3)
    print(f"Полуавтоматический режим: модель риска загружена, порог {threshold:.2f}.", flush=True)


def add_slide_payload(payload: dict) -> dict:
    parsed = payload.get("parsed") or {}
    reference_list = payload.get("reference_list") or []
    if reference_list:
        source_text = str(payload.get("text") or "")
        refs = [str(item.get("ref") or "").strip() for item in reference_list if str(item.get("ref") or "").strip()]
        payload["slide"] = {
            "ref": "Ссылки для чтения",
            "verse": "\n".join(refs),
            "source": "vosk:parser_reference_list",
            "asr": source_text,
            "detected_text": source_text,
            "slide_type": "reference_list",
            "references": reference_list,
        }
        return payload
    if not parsed:
        payload["slide"] = None
        return payload
    source = payload.get("source") or "parser"
    source_text = str(payload.get("text") or "")
    payload["slide"] = {
        "ref": parsed.get("ref"),
        "verse": parsed.get("verse_text"),
        "book": parsed.get("book"),
        "chapter": parsed.get("chapter"),
        "start_verse": parsed.get("start_verse"),
        "end_verse": parsed.get("end_verse"),
        "end_chapter": parsed.get("end_chapter"),
        "source": f"vosk:{source}",
        "asr": source_text,
        "detected_text": source_text,
    }
    try:
        chapter = int(parsed.get("chapter") or 0)
        start_verse = int(parsed.get("start_verse") or 0)
        end_chapter = int(parsed.get("end_chapter") or chapter)
        end_verse = int(parsed.get("end_verse") or 0)
    except (TypeError, ValueError):
        chapter = start_verse = end_chapter = end_verse = 0
    if end_chapter > chapter or (end_chapter == chapter and end_verse > start_verse):
        payload["slide"]["can_set_context"] = True
    alternatives = []
    for alternative in payload.get("ambiguous_alternatives") or []:
        alternatives.append(
            {
                "ref": alternative.get("ref"),
                "verse": alternative.get("verse_text"),
                "book": alternative.get("book"),
                "chapter": alternative.get("chapter"),
                "start_verse": alternative.get("start_verse"),
                "end_verse": alternative.get("end_verse"),
                "end_chapter": alternative.get("end_chapter"),
                "source": f"vosk:{source}:alternative",
                "asr": source_text,
                "detected_text": source_text,
            }
        )
    if alternatives:
        payload["slide"]["alternatives"] = alternatives
    return payload


def parsed_payload_from_candidates(
    candidates: list[str],
    bible_path: Path = DEFAULT_BIBLE,
    *,
    show_candidates: bool = False,
) -> dict:
    payload = core_parsed_payload_from_candidates(
        candidates,
        bible_path=bible_path,
        show_candidates=show_candidates,
    )
    return add_slide_payload(payload)


def payload_summary(payload: dict) -> dict:
    parsed = payload.get("parsed") or {}
    slide = payload.get("slide") or {}
    invalid_reference = payload.get("invalid_reference") or {}
    return {
        "text": payload.get("text"),
        "ref": parsed.get("ref"),
        "book": parsed.get("book"),
        "chapter": parsed.get("chapter"),
        "start_verse": parsed.get("start_verse"),
        "end_verse": parsed.get("end_verse"),
        "source": payload.get("source"),
        "has_slide": bool(slide),
        "can_set_context": bool(slide.get("can_set_context")),
        "context_reference": bool(payload.get("context_reference")),
        "context_range": payload.get("context_range") or {},
        "invalid_reference": invalid_reference,
        "message": payload.get("message"),
        "attempts": payload.get("attempts") or [],
        "reference_list": payload.get("reference_list") or [],
        "risk_score": payload.get("risk_score"),
        "risk_level": payload.get("risk_level"),
        "risk_reasons": payload.get("risk_reasons") or [],
        "risk": payload.get("risk") or {},
        "ml_risk": payload.get("ml_risk") or {},
        "ambiguous_alternatives": payload.get("ambiguous_alternatives") or [],
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

    alternatives = [item for item in slide.get("alternatives") or [] if isinstance(item, dict)]
    has_context_button = bool(slide.get("can_set_context"))
    width = 980
    if alternatives and has_context_button:
        height = 520
    elif alternatives or has_context_button:
        height = 430
    else:
        height = 360
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
        text=(
            "Enter - основной вариант     C - принять как контекст     1/2/... - альтернативы     Esc или Space - отклонить"
            if alternatives
            else (
                "Enter - принять     C - принять как контекст     Esc или Space - отклонить"
                if slide.get("can_set_context")
                else "Enter - принять     Esc или Space - отклонить"
            )
        ),
        bg="#101820",
        fg="#c8d2dc",
        font=hint_font,
        wraplength=900,
        justify="center",
    ).pack(fill="x", padx=36, pady=(8, 18))

    buttons = tk.Frame(root, bg="#101820")
    buttons.pack(fill="x", padx=36, pady=(0, 12 if has_context_button else 30))

    def close(action: str) -> None:
        decision["action"] = action
        root.destroy()

    approve = tk.Button(
        buttons,
        text=str(slide.get("ref") or "Принять"),
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
    approve.pack(side="left", fill="x", expand=True, padx=(0, 10))

    for index, alternative in enumerate(alternatives, start=1):
        button = tk.Button(
            buttons,
            text=str(alternative.get("ref") or f"Вариант {index}"),
            command=lambda choice=index - 1: close(f"alternative:{choice}"),
            bg="#315a99",
            fg="white",
            activebackground="#3c6fbd",
            activeforeground="white",
            font=button_font,
            relief="flat",
            padx=24,
            pady=16,
        )
        button.pack(side="left", fill="x", expand=True, padx=(0, 10))

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
    reject.pack(side="left", fill="x", expand=True, padx=(10, 0))

    if has_context_button:
        context_row = tk.Frame(root, bg="#101820")
        context_row.pack(fill="x", padx=36, pady=(0, 30))
        context_button = tk.Button(
            context_row,
            text="Принять и запомнить как контекстный отрывок",
            command=lambda: close("approve_context"),
            bg="#8a6d16",
            fg="white",
            activebackground="#a8841b",
            activeforeground="white",
            font=button_font,
            relief="flat",
            padx=24,
            pady=16,
        )
        context_button.pack(fill="x", expand=True)

    root.bind("<Return>", lambda _event: close("approve"))
    if has_context_button:
        root.bind("c", lambda _event: close("approve_context"))
        root.bind("C", lambda _event: close("approve_context"))
    for index, _alternative in enumerate(alternatives, start=1):
        root.bind(str(index), lambda _event, choice=index - 1: close(f"alternative:{choice}"))
    root.bind("<Escape>", lambda _event: close("reject"))
    root.bind("<space>", lambda _event: close("reject"))
    root.protocol("WM_DELETE_WINDOW", lambda: close("reject"))
    root.after(100, root.focus_force)
    root.after(150, root.lift)
    root.mainloop()
    return decision["action"]


def show_popup_message(title: str, message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import font as tkfont
    except Exception:
        return

    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    root.configure(bg="#101820")
    root.resizable(True, True)

    width, height = 980, 360
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    x = max(0, (screen_width - width) // 2)
    y = max(0, (screen_height - height) // 2)
    root.geometry(f"{width}x{height}+{x}+{y}")

    title_font = tkfont.Font(family="Segoe UI", size=38, weight="bold")
    body_font = tkfont.Font(family="Segoe UI", size=26, weight="bold")
    button_font = tkfont.Font(family="Segoe UI", size=22, weight="bold")

    tk.Label(
        root,
        text="Внимание",
        bg="#101820",
        fg="#ffd166",
        font=title_font,
        wraplength=900,
        justify="center",
    ).pack(fill="x", padx=36, pady=(34, 10))

    tk.Label(
        root,
        text=message,
        bg="#101820",
        fg="#f5f7fa",
        font=body_font,
        wraplength=900,
        justify="center",
    ).pack(fill="both", expand=True, padx=36, pady=(8, 22))

    buttons = tk.Frame(root, bg="#101820")
    buttons.pack(fill="x", padx=36, pady=(0, 30))

    def close() -> None:
        root.destroy()

    ok = tk.Button(
        buttons,
        text="ОК",
        command=close,
        bg="#148447",
        fg="white",
        activebackground="#1aa158",
        activeforeground="white",
        font=button_font,
        relief="flat",
        padx=24,
        pady=16,
    )
    ok.pack(fill="x", expand=True)

    root.bind("<Return>", lambda _event: close())
    root.bind("<Escape>", lambda _event: close())
    root.protocol("WM_DELETE_WINDOW", close)
    root.after(100, root.focus_force)
    root.after(150, root.lift)
    root.mainloop()


def notify_operator_message(args: argparse.Namespace, payload: dict) -> None:
    message = str(payload.get("message") or "").strip()
    if not message:
        return
    if args.approval_ui != "popup":
        return
    if not (args.require_approval or args.semi_auto_approval):
        return
    show_popup_message("LiVerse", message)


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
    if action.startswith("alternative:"):
        try:
            alternative_index = int(action.split(":", 1)[1])
            payload["slide"] = (slide.get("alternatives") or [])[alternative_index]
        except (ValueError, IndexError, TypeError):
            return {"enabled": True, "ok": False, "reason": "invalid_alternative_selection"}
        output = publish_after_approval(args, payload)
        return {"enabled": True, "ok": True, "action": "approve_alternative", **output}
    if action not in {"approve", "approve_context"}:
        return {"enabled": True, "ok": True, "action": "reject"}
    output = publish_after_approval(args, payload)
    return {"enabled": True, "ok": True, "action": action, **output}


def submit_for_approval(args: argparse.Namespace, payload: dict) -> dict:
    if not payload.get("slide"):
        return {"enabled": False}
    from tools.slide_server import submit_candidate

    candidate = submit_candidate(payload["slide"])
    return {"enabled": True, "ok": True, "candidate": candidate}


def approval_required_for_payload(args: argparse.Namespace, payload: dict) -> bool:
    if not payload.get("slide"):
        return False
    ml_risk = payload.get("ml_risk") or {}
    if ml_risk.get("auto_reject"):
        return False
    if args.require_approval:
        return True
    if args.semi_auto_approval and (payload.get("slide") or {}).get("can_set_context"):
        return True
    return bool(args.semi_auto_approval and ml_risk.get("needs_confirmation"))


def apply_ml_risk(args: argparse.Namespace, payload: dict, asr_result: dict | None = None) -> None:
    if not args.semi_auto_approval or args.require_approval or not payload.get("slide"):
        return
    model = getattr(args, "risk_model_data", None)
    if not model:
        return
    ml_risk = score_payload_with_model(payload, model, asr_result=asr_result)
    try:
        risk_score = float(payload.get("risk_score") or 0.0)
    except (TypeError, ValueError):
        risk_score = 0.0
    decision_reasons = list(ml_risk.get("decision_reasons") or [])
    auto_reject_threshold = float(getattr(args, "risk_auto_reject_threshold", 0.9) or 0.0)
    if auto_reject_threshold > 0 and risk_score >= auto_reject_threshold:
        ml_risk["auto_reject"] = True
        ml_risk["needs_confirmation"] = False
        decision_reasons.append("manual_very_high_risk_auto_reject")
    if not ml_risk.get("auto_reject") and risk_score >= 0.5 and not ml_risk.get("needs_confirmation"):
        ml_risk["needs_confirmation"] = True
        decision_reasons.append("manual_medium_or_high_risk_score")
    if decision_reasons:
        ml_risk["decision_reasons"] = decision_reasons
    payload["ml_risk"] = ml_risk


def start_slide_server_if_needed(args: argparse.Namespace, pipeline: LiveReferencePipeline | None = None):
    web_approval = (args.require_approval or args.semi_auto_approval) and args.approval_ui == "web"
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
        if action == "approve_context" and pipeline is not None:
            pipeline.set_context_range(candidate)
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
    ml_risk = payload.get("ml_risk") or {}
    if ml_risk.get("auto_reject"):
        return {
            "approval": {
                "enabled": True,
                "ok": True,
                "action": "reject",
                "reason": "auto_reject_high_risk",
            },
            "holyrics": {"enabled": False, "reason": "auto_reject_high_risk"},
            "web": {"enabled": False, "reason": "auto_reject_high_risk"},
        }
    if approval_required_for_payload(args, payload):
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
    if action == "reject":
        return "reject"
    if action in {"approve", "approve_context"}:
        if output.get("holyrics", {}).get("ok") or output.get("web", {}).get("ok"):
            return action
        return "output_failed"
    if approval.get("reason") == "waiting_for_approval" or output.get("holyrics", {}).get("reason") == "waiting_for_approval":
        return "waiting"
    if output.get("holyrics", {}).get("ok") or output.get("web", {}).get("ok"):
        return "sent"
    return "recognized"


def output_failure_reason(output: dict) -> str:
    for key in ("holyrics", "web", "approval"):
        value = output.get(key) or {}
        reason = str(value.get("reason") or "").strip()
        if reason and reason not in {"rejected_or_no_slide", "waiting_for_approval"}:
            return reason
    return "неизвестная ошибка"


def trigger_time_info(asr_result: dict, fallback_seconds: float, preroll: float = 8.0, postroll: float = 3.0) -> dict:
    words = asr_result.get("result") if isinstance(asr_result, dict) else None
    if isinstance(words, list) and words:
        starts = [
            float(item.get("start"))
            for item in words
            if isinstance(item, dict) and isinstance(item.get("start"), (int, float))
        ]
        ends = [
            float(item.get("end"))
            for item in words
            if isinstance(item, dict) and isinstance(item.get("end"), (int, float))
        ]
        start = min(starts) if starts else fallback_seconds
        end = max(ends) if ends else fallback_seconds
    else:
        start = fallback_seconds
        end = fallback_seconds

    center = max(0.0, end)
    window_start = max(0.0, start - preroll)
    window_end = max(window_start, end + postroll)
    return {
        "timecode_seconds": round(center, 3),
        "timecode": format_timecode(center),
        "window_start_seconds": round(window_start, 3),
        "window_start": format_timecode(window_start),
        "window_end_seconds": round(window_end, 3),
        "window_end": format_timecode(window_end),
    }


def list_audio_devices() -> int:
    import sounddevice as sd

    devices = list(sd.query_devices())
    default_device = sd.default.device
    default_input = default_device[0] if isinstance(default_device, (list, tuple)) else default_device
    print("Аудиоустройства:", flush=True)
    for index, device in enumerate(devices):
        input_channels = int(device.get("max_input_channels") or 0)
        output_channels = int(device.get("max_output_channels") or 0)
        marker = " *" if index == default_input else ""
        print(
            f"{index}{marker}: {device.get('name')} "
            f"(входов: {input_channels}, выходов: {output_channels})",
            flush=True,
        )
    print("", flush=True)
    print("* - вход по умолчанию. Для выбора микрофона запустите: make liverse ARGS=\"--device N\"", flush=True)
    return 0


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
            "trigger_cases": "trigger_cases.jsonl",
            "slide_output": args.slide_output,
            "require_approval": args.require_approval,
            "semi_auto_approval": args.semi_auto_approval,
            "risk_model": str(args.risk_model) if args.semi_auto_approval else None,
            "risk_threshold": args.risk_threshold,
            "risk_auto_reject_threshold": args.risk_auto_reject_threshold,
            "approval_ui": args.approval_ui,
            "slide_server": f"http://{args.slide_host}:{args.slide_port}" if (
                args.start_slide_server
                or ((args.require_approval or args.semi_auto_approval) and args.approval_ui == "web")
                or args.slide_output in {"web", "both"}
            ) else None,
            "holyrics_target": describe_holyrics_target(args),
            "holyrics_quick_minutes": args.holyrics_quick_minutes,
            "grammar": None if grammar is None else grammar_diagnostics(grammar),
        }
    )
    check_holyrics_startup(args, logger)
    SetLogLevel(args.vosk_log_level)
    model = Model(str(args.model))
    sermon_plan = None
    if args.sermon_plan:
        active = get_holyrics_current_presentation(
            args,
            str(args.holyrics_url).rstrip("/"),
            include_slides=True,
        )
        if active and str(active.get("type") or "") == "text":
            slides = list(active.get("slides") or [])
            nonempty_slide_count = sum(1 for slide in slides if str(slide.get("text") or "").strip())
            if nonempty_slide_count:
                sermon_plan = {
                    **active,
                    "slides": slides,
                    "current_index": max(0, int(active.get("slide_number") or 1) - 1),
                    "next_index": max(0, int(active.get("slide_number") or 1) - 1),
                }
                current_slide_index = max(0, int(active.get("slide_number") or 1) - 1)
                current_slide = slides[current_slide_index] if current_slide_index < len(slides) else {}
                sermon_plan_theme_id = str(current_slide.get("theme_id") or "").strip()
                if not sermon_plan_theme_id:
                    sermon_plan_theme_id = next(
                        (
                            str(slide.get("theme_id") or "").strip()
                            for slide in slides
                            if str(slide.get("theme_id") or "").strip()
                        ),
                        "",
                    )
                setattr(args, "_holyrics_sermon_plan_theme_id", sermon_plan_theme_id)
                setattr(args, "_holyrics_sermon_plan_presentation", sermon_plan)
                if grammar is not None:
                    grammar = sorted(
                        set(grammar)
                        | set(
                            sermon_plan_grammar_phrases(
                                slides,
                                word_is_known=lambda word: model.vosk_model_find_word(word) != -1,
                            )
                        )
                    )
                logger.write(
                    "sermon_plan_loaded",
                    {
                        "name": active.get("name"),
                        "text_id": active.get("text_id") or active.get("id"),
                        "slides": nonempty_slide_count,
                        "current_slide": active.get("slide_number"),
                    },
                )
                print(
                    f"План проповеди: {active.get('name')} ({nonempty_slide_count} непустых слайда)",
                    flush=True,
                )
        if sermon_plan is None:
            print("План проповеди не найден: откройте текстовую презентацию Holyrics до запуска LiVerse.", flush=True)
    print(WELCOME_TEXT, flush=True)
    if logger.run_dir and args.print_log_path:
        print(f"Vosk log: {logger.run_dir / 'events.jsonl'}")

    def callback(indata, frames, time, status):
        if status:
            print(status, file=sys.stderr)
            logger.write("audio_status", {"status": str(status)})
        data = bytes(indata)
        try:
            samples = memoryview(data).cast("h")
            peak = max((abs(sample) for sample in samples), default=0)
            if peak > audio_stats["peak"]:
                audio_stats["peak"] = peak
            audio_stats["chunks"] += 1
        except Exception:
            pass
        audio_queue.put(data)

    pipeline = LiveReferencePipeline(args.bible, buffer_parts=args.vosk_buffer_parts)
    start_slide_server_if_needed(args, pipeline=pipeline)
    audio_stats = {"chunks": 0, "peak": 0}
    empty_final_count = 0
    trigger_case_count = 0

    def sample_rate_candidates() -> list[int]:
        values = [args.samplerate, 16000, 48000, 44100]
        result: list[int] = []
        for value in values:
            if value > 0 and value not in result:
                result.append(value)
        return result

    def audio_device_name(device: dict, index: int) -> str:
        name = str(device.get("name") or f"device {index}")
        hostapi = device.get("hostapi")
        return f"{index}: {name} (hostapi {hostapi})"

    def input_device_candidates() -> tuple[list[int], list[dict]]:
        devices = list(sd.query_devices())
        result: list[int] = []

        def add(index: object) -> None:
            if not isinstance(index, int) or index < 0 or index >= len(devices):
                return
            if index not in result:
                result.append(index)

        add(args.device)
        default_device = sd.default.device
        if isinstance(default_device, (list, tuple)):
            add(default_device[0])
        else:
            add(default_device)

        for index, device in enumerate(devices):
            try:
                input_channels = int(device.get("max_input_channels") or 0)
            except (TypeError, ValueError):
                input_channels = 0
            if input_channels > 0:
                add(index)
        return result, devices

    def find_working_audio_input() -> tuple[dict | None, list[dict], list[str]]:
        errors: list[str] = []
        try:
            devices, all_devices = input_device_candidates()
        except Exception as exc:
            return None, [], [f"Не удалось получить список аудиоустройств: {exc}"]

        for device_index in devices:
            device = all_devices[device_index]
            try:
                input_channels = int(device.get("max_input_channels") or 0)
            except (TypeError, ValueError):
                input_channels = 0
            if input_channels < 1:
                errors.append(f"{audio_device_name(device, device_index)}: нет входных каналов")
                continue

            for samplerate in sample_rate_candidates():
                try:
                    sd.check_input_settings(
                        device=device_index,
                        channels=1,
                        samplerate=samplerate,
                        dtype="int16",
                    )
                except Exception as exc:
                    errors.append(f"{audio_device_name(device, device_index)}, {samplerate} Hz: {exc}")
                    continue
                return (
                    {
                        "device": device_index,
                        "samplerate": samplerate,
                        "name": audio_device_name(device, device_index),
                    },
                    all_devices,
                    errors,
                )
        return None, all_devices, errors

    def wait_for_audio_device(error: Exception) -> None:
        print("", flush=True)
        print("Не удалось открыть микрофон.", flush=True)
        print(
            "Проверьте, подключен ли микрофон к компьютеру, если не подключен, "
            "подключите микрофон, для продолжения нажмите Enter.",
            flush=True,
        )
        print(f"Техническая ошибка: {error}", flush=True)
        input()

    def legacy_audio_input() -> dict:
        name = "системный вход по умолчанию"
        if args.device is not None:
            try:
                devices = list(sd.query_devices())
                name = audio_device_name(devices[args.device], args.device)
            except Exception:
                name = f"устройство {args.device}"
        return {
            "device": args.device,
            "samplerate": args.samplerate,
            "name": name,
            "mode": "system-default",
        }

    console.status("слушаю")
    while True:
        audio_log = None
        audio_errors: list[str] = []
        if os.name == "nt":
            audio_input, all_devices, audio_errors = find_working_audio_input()
            if audio_input is None:
                logger.write(
                    "audio_input_not_found",
                    {
                        "device": args.device,
                        "samplerates": sample_rate_candidates(),
                        "errors": audio_errors,
                        "devices": [
                            {
                                "index": index,
                                "name": device.get("name"),
                                "max_input_channels": device.get("max_input_channels"),
                                "max_output_channels": device.get("max_output_channels"),
                            }
                            for index, device in enumerate(all_devices)
                        ],
                    },
                )
                wait_for_audio_device(RuntimeError("рабочий микрофон не найден"))
                continue
        else:
            audio_input = legacy_audio_input()

        stream_kwargs = {
            "samplerate": audio_input["samplerate"],
            "blocksize": args.blocksize,
            "dtype": "int16",
            "channels": 1,
            "callback": callback,
        }
        if audio_input["device"] is not None:
            stream_kwargs["device"] = audio_input["device"]
        try:
            stream = sd.RawInputStream(**stream_kwargs)
        except Exception as exc:
            logger.write(
                "audio_open_error",
                {"error": str(exc), "audio_input": audio_input, "errors": audio_errors},
            )
            wait_for_audio_device(exc)
            continue

        logger.write("audio_input_selected", audio_input)
        print(f"Микрофон: {audio_input['name']}, {audio_input['samplerate']} Hz", flush=True)
        recognizer_args = [model, audio_input["samplerate"]]
        if grammar is not None:
            recognizer_args.append(json.dumps(grammar, ensure_ascii=False))
        recognizer = KaldiRecognizer(*recognizer_args)
        recognizer.SetWords(True)
        audio_bytes_seen = 0
        audio_path = ""
        if args.log_audio and logger.run_dir:
            audio_path = str(logger.run_dir / "audio.wav")
            audio_log = wave.open(audio_path, "wb")
            audio_log.setnchannels(1)
            audio_log.setsampwidth(2)
            audio_log.setframerate(audio_input["samplerate"])
            logger.write("audio_log", {"path": audio_path})

        try:
            with stream:
                while True:
                    data = audio_queue.get()
                    if audio_log:
                        audio_log.writeframes(data)
                    audio_bytes_seen += len(data)
                    if recognizer.AcceptWaveform(data):
                        result = json.loads(recognizer.Result())
                        text = result.get("text", "").strip()
                        final_audio_stats = dict(audio_stats)
                        audio_stats["chunks"] = 0
                        audio_stats["peak"] = 0
                        logger.write(
                            "final_raw",
                            {"result": result, "text": text, "audio": final_audio_stats},
                        )
                        if text:
                            empty_final_count = 0
                            console.status("распознаю")
                            pipeline_payload = pipeline.process_text(
                                text,
                                asr_result=result,
                                show_candidates=args.show_candidates,
                            )
                            plan_match = None
                            if sermon_plan is not None:
                                plan_match = match_sermon_plan_slide(
                                    sermon_plan["slides"],
                                    list(pipeline_payload.get("candidate_texts") or [text]),
                                    current_index=int(sermon_plan["next_index"]),
                                )
                            if plan_match:
                                plan_ok, plan_reason = show_holyrics_text_slide(
                                    args,
                                    str(args.holyrics_url).rstrip("/"),
                                    sermon_plan,
                                    int(plan_match["slide_index"]),
                                )
                                logger.write(
                                    "sermon_plan_match",
                                    {**plan_match, "ok": plan_ok, "reason": plan_reason},
                                )
                                if plan_ok:
                                    sermon_plan["current_index"] = int(plan_match["slide_index"])
                                    sermon_plan["next_index"] = int(plan_match["slide_index"]) + 1
                                    pipeline.text_buffer.clear()
                                    console.status(
                                        f"план проповеди: слайд {plan_match['slide_number']}"
                                    )
                                    continue
                                console.status(f"ошибка показа плана: {plan_reason}")
                            payload = add_slide_payload(pipeline_payload)
                            payload["asr"] = result
                            apply_ml_risk(args, payload, asr_result=result)
                            payload["output"] = publish_payload(args, payload)
                            if payload.get("slide"):
                                action = approval_action(payload["output"])
                                if action == "approve_context" and pipeline.set_context_range(payload["slide"]):
                                    logger.write(
                                        "context_range_selected",
                                        {"ref": payload["slide"].get("ref"), "source": "popup"},
                                    )
                                append_session_reference(session_refs, payload, action=action)
                                ref = str((payload.get("parsed") or {}).get("ref") or payload["slide"].get("ref"))
                                if action == "waiting":
                                    console.status(f"найдена ссылка {ref}, ожидает подтверждения")
                                elif action == "approve_context":
                                    console.status(f"контекстный отрывок запомнен: {ref}")
                                elif action == "approve":
                                    console.status(f"отправлено в Holyrics: {ref}")
                                elif action == "output_failed":
                                    console.status(f"ошибка Holyrics: {output_failure_reason(payload['output'])}")
                                elif action == "reject":
                                    console.status(f"отклонено: {ref}")
                                else:
                                    console.status(f"найдена ссылка: {ref}")
                                trigger_case_count += 1
                                bytes_per_second = max(1, int(audio_input["samplerate"]) * 2)
                                fallback_seconds = audio_bytes_seen / bytes_per_second
                                time_info = trigger_time_info(result, fallback_seconds)
                                logger.write_trigger_case(
                                    {
                                        "case_id": f"trigger_{trigger_case_count:04d}",
                                        "status": "unreviewed",
                                        "review_category": "",
                                        "audio": audio_path,
                                        **time_info,
                                        "action": action,
                                        "ref": ref,
                                        "vosk_text": text,
                                        "vosk_buffer": list(payload.get("vosk_buffer") or []),
                                        "payload": payload_summary(payload),
                                        "output": payload["output"],
                                        "asr": result,
                                        "note": "",
                                    }
                                )
                            elif payload.get("message"):
                                notify_operator_message(args, payload)
                                console.status(str(payload["message"]))
                            else:
                                console.status("слушаю")
                            logger.write(
                                "parsed",
                                {
                                    "vosk_text": text,
                                    "vosk_buffer": list(payload.get("vosk_buffer") or []),
                                    "candidate_texts": list(payload.get("candidate_texts") or []),
                                    "payload": payload_summary(payload),
                                    "output": payload["output"],
                                },
                            )
                            console.debug_json(payload)
                        else:
                            empty_final_count += 1
                            if empty_final_count >= 3:
                                if final_audio_stats.get("peak", 0) < 200:
                                    console.status(
                                        "микрофон открыт, но звук почти не поступает"
                                    )
                                else:
                                    console.status(
                                        "звук поступает, но Vosk пока не распознал речь"
                                    )
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
    parser.add_argument("--list-audio-devices", action="store_true", help="Print microphone/input device list and exit.")
    parser.add_argument("--open-vocabulary", action="store_true", help="Run Vosk without generated grammar.")
    parser.add_argument(
        "--sermon-plan",
        action="store_true",
        help="Read the active Holyrics text presentation and follow its spoken sermon-plan lines.",
    )
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
    parser.add_argument(
        "--ask-approval-mode",
        action="store_true",
        help="Ask whether to use automatic mode, web approval, or popup approval before microphone startup.",
    )
    parser.add_argument("--text", nargs="+", help="Resolve text without opening the microphone.")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--no-log", action="store_true", help="Disable JSONL logging.")
    parser.add_argument("--log-partials", action="store_true", help="Log Vosk partial results too.")
    parser.add_argument(
        "--log-audio",
        dest="log_audio",
        action="store_true",
        help="Save microphone audio to audio.wav in the run log. Enabled by default.",
    )
    parser.add_argument(
        "--no-log-audio",
        dest="log_audio",
        action="store_false",
        help="Do not save microphone audio for this run.",
    )
    parser.add_argument(
        "--print-grammar-json",
        action="store_true",
        help="Print generated Vosk grammar JSON and exit.",
    )
    parser.add_argument(
        "--grammar-output",
        type=Path,
        help="Write generated Vosk grammar JSON to this file when --print-grammar-json is used.",
    )
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
        "--semi-auto-approval",
        action="store_true",
        help="Use ML risk model: send low-risk references automatically and ask the operator for risky references.",
    )
    parser.add_argument(
        "--risk-model",
        type=Path,
        default=default_risk_model_path(),
        help="JSON Naive Bayes risk model for --semi-auto-approval.",
    )
    parser.add_argument(
        "--risk-threshold",
        type=float,
        default=0.0,
        help="Override model confirmation threshold. 0 uses the threshold stored in the model.",
    )
    parser.add_argument(
        "--risk-auto-reject-threshold",
        type=float,
        default=float(env_setting("LIVERSE_RISK_AUTO_REJECT_THRESHOLD", "0.9") or "0.9"),
        help="In semi-auto mode, reject references with risk_score at or above this value without showing them to the operator. 0 disables it.",
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
    parser.add_argument(
        "--holyrics-theme",
        default=env_setting("HOLYRICS_THEME"),
        help="Holyrics theme name for Bible verse display. Empty uses Holyrics Bible module default.",
    )
    parser.add_argument("--holyrics-timeout", type=float, default=float(env_setting("HOLYRICS_TIMEOUT", "1.5")))
    parser.add_argument(
        "--holyrics-quick-minutes",
        type=float,
        default=float(env_setting("HOLYRICS_QUICK_MINUTES", "0") or "0"),
        help="Show Bible verses temporarily and restore the previous Holyrics text presentation after this many minutes. 0 disables it.",
    )
    parser.set_defaults(session_summary_popup=True, log_audio=True)
    args = parser.parse_args()
    if args.list_audio_devices:
        return list_audio_devices()
    if args.print_grammar_json:
        grammar_json = json.dumps(build_grammar(), ensure_ascii=False)
        if args.grammar_output:
            args.grammar_output.parent.mkdir(parents=True, exist_ok=True)
            args.grammar_output.write_text(grammar_json + "\n", encoding="utf-8")
        else:
            print(grammar_json, flush=True)
        return 0
    configure_interactive_approval_mode(args)
    load_runtime_risk_model(args)
    print_holyrics_setup_notice_once(args)
    ask_holyrics_theme_name(args)
    ask_holyrics_quick_presentation_minutes(args)
    save_startup_settings(args)

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
                "semi_auto_approval": args.semi_auto_approval,
                "risk_model": str(args.risk_model) if args.semi_auto_approval else None,
                "risk_threshold": args.risk_threshold,
                "risk_auto_reject_threshold": args.risk_auto_reject_threshold,
                "approval_ui": args.approval_ui,
                "holyrics_target": describe_holyrics_target(args),
                "holyrics_quick_minutes": args.holyrics_quick_minutes,
                "grammar": None if grammar is None else grammar_diagnostics(grammar),
            }
        )
        check_holyrics_startup(args, logger)
        candidate_texts = expand_nehemiah_confusable_candidates(
            [" ".join(args.text)],
            bible_path=args.bible,
        )
        candidate_texts = expand_joel_confusable_candidates(candidate_texts)
        payload = parsed_payload_from_candidates(
            candidate_texts,
            bible_path=args.bible,
            show_candidates=args.show_candidates,
        )
        add_risk_score(payload)
        apply_ml_risk(args, payload)
        if (
            approval_required_for_payload(args, payload)
            or args.start_slide_server
            or args.slide_output in {"web", "both"}
        ):
            start_slide_server_if_needed(args)
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
            print(f"Результат: {ref or payload.get('message') or 'ссылка не найдена'}", flush=True)
        return 0 if payload["parsed"] else 1

    return run_microphone(args)


if __name__ == "__main__":
    raise SystemExit(main())
