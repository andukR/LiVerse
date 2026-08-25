#!/usr/bin/env python3
"""Quiet desktop controller for the existing LiVerse recognition engine."""

from __future__ import annotations

import os
import queue
import json
import re
import signal
import socket
import subprocess
import sys
import threading
import traceback
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    PROJECT_ROOT = Path(sys._MEIPASS)
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
CORE_SRC = PROJECT_ROOT / "packages" / "bible_parser_core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from tools.holyrics import (  # noqa: E402
    DEFAULT_PORT,
    check_holyrics_api_server,
    env_setting,
    liverse_config_dir,
    required_holyrics_permissions,
    save_holyrics_env,
)
from tools import __version__  # noqa: E402
from tools.release_updater import (  # noqa: E402
    ReleaseUpdateError,
    check_windows_release_update,
    download_windows_release_installer,
    launch_windows_release_installer,
)
from tools.vosk_grammar_probe import (  # noqa: E402
    DEFAULT_LOG_DIR,
    DEFAULT_TEXT_DETECTION_DB,
    apply_startup_update,
    check_startup_update,
    load_startup_settings,
    save_startup_settings,
)


RUN_MODE_LABELS = {
    "semi_auto": "Осторожный полуавтомат (рекомендуется)",
    "auto": "Полностью автоматически",
    "approval": "Подтверждать каждую ссылку",
}
APPROVAL_LABELS = {
    "popup": "Всплывающее окно",
    "web": "Телефон",
}
DETECTION_LABELS = {
    "hybrid_confirm": "Адреса и текст стихов, сомнительное подтверждать",
    "hybrid_auto": "Адреса и текст стихов автоматически",
    "address_only": "Только произнесённые адреса",
    "text_only": "Только поиск стихов по тексту",
}
RUN_MODE_VALUES = {label: value for value, label in RUN_MODE_LABELS.items()}
APPROVAL_VALUES = {label: value for value, label in APPROVAL_LABELS.items()}
DETECTION_VALUES = {label: value for value, label in DETECTION_LABELS.items()}
INSTANCE_PORT = 45871
BRAND_BLUE = "#0B5EA8"
BRAND_BLUE_ACTIVE = "#084A84"
HEADER_BORDER = "#D9E2EC"
STATE_COLORS = {
    "good": ("#E6F4EA", "#176B35"),
    "attention": ("#FFF4CE", "#7A4D00"),
    "error": ("#FDE8E7", "#A12622"),
    "neutral": ("#E9EEF3", "#465463"),
}
LOG_EXPORT_NAMES = ("session.json", "events.jsonl", "trigger_cases.jsonl")
SENSITIVE_LOG_KEYS = ("token", "password", "secret", "authorization")


def redact_log_value(value):
    if isinstance(value, dict):
        return {
            key: ("[скрыто]" if any(part in str(key).casefold() for part in SENSITIVE_LOG_KEYS) else redact_log_value(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_log_value(item) for item in value]
    if not isinstance(value, str):
        return value
    patterns = (
        r"(?i)(--holyrics-token(?:=|\s+))\S+",
        r"(?i)(HOLYRICS_TOKEN\s*=\s*)\S+",
        r"(?i)([?&]token=)[^&\s]+",
    )
    cleaned = value
    for pattern in patterns:
        cleaned = re.sub(pattern, lambda match: f"{match.group(1)}[скрыто]", cleaned)
    return cleaned


def sanitized_log_bytes(path: Path) -> bytes:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".json":
        try:
            payload = redact_log_value(json.loads(text))
            return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        except json.JSONDecodeError:
            return str(redact_log_value(text)).encode("utf-8")
    rows = []
    for line in text.splitlines():
        try:
            rows.append(json.dumps(redact_log_value(json.loads(line)), ensure_ascii=False))
        except json.JSONDecodeError:
            rows.append(str(redact_log_value(line)))
    return (("\n".join(rows) + "\n") if rows else "").encode("utf-8")


def list_log_sessions(log_dir: Path = DEFAULT_LOG_DIR) -> list[Path]:
    """Return newest LiVerse log sessions first."""
    if not log_dir.is_dir():
        return []
    return sorted(
        (
            path
            for path in log_dir.iterdir()
            if path.is_dir() and any((path / name).is_file() for name in LOG_EXPORT_NAMES)
        ),
        key=lambda path: path.name,
        reverse=True,
    )


def create_log_archive(
    sessions: list[Path],
    destination: Path,
    *,
    include_audio: bool = False,
) -> int:
    """Create a reviewable archive without settings, passwords, or HoLyrics token."""
    names = (*LOG_EXPORT_NAMES, "audio.wav") if include_audio else LOG_EXPORT_NAMES
    written = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for session in sessions:
            for name in names:
                source = session / name
                if source.is_file():
                    archive_name = f"{session.name}/{name}"
                    if name == "audio.wav":
                        archive.write(source, arcname=archive_name)
                    else:
                        archive.writestr(archive_name, sanitized_log_bytes(source))
                    written += 1
    if written == 0:
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        raise ValueError("В выбранных сеансах нет файлов журналов.")
    return written


def gui_log_path() -> Path:
    if os.name == "nt":
        return liverse_config_dir() / "logs" / "gui.log"
    cache_root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache_root / "liverse" / "gui.log"


def engine_stop_path() -> Path:
    return liverse_config_dir() / "engine.stop"


def write_gui_log(message: str) -> None:
    """Keep desktop-launch failures visible even when no console is open."""
    path = gui_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as log_file:
            log_file.write(message.rstrip() + "\n")
    except OSError:
        pass


def packaged_windows_runtime(
    *, frozen: bool | None = None, platform: str | None = None
) -> bool:
    selected_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    selected_platform = sys.platform if platform is None else platform
    return selected_frozen and selected_platform.startswith("win")


def check_gui_update(
    *, frozen: bool | None = None, platform: str | None = None
) -> dict:
    """Select the source or packaged-Windows update channel."""
    if packaged_windows_runtime(frozen=frozen, platform=platform):
        return check_windows_release_update(__version__)
    return check_startup_update()


def run_packaged_update_smoke_test() -> int:
    """Verify the packaged EXE selects its installer-based update channel."""
    if not packaged_windows_runtime():
        write_gui_log("Packaged update smoke test failed: runtime is not packaged Windows")
        return 2
    update = check_gui_update()
    status = str(update.get("status") or "")
    acceptable = {
        "available",
        "current",
        "no_release",
        "network_unavailable",
        "invalid_release",
    }
    if status not in acceptable:
        write_gui_log(f"Packaged update smoke test failed: status={status or 'missing'}")
        return 3
    write_gui_log(f"Packaged update smoke test passed: channel=binary; status={status}")
    return 0


@dataclass
class GuiConfig:
    run_mode: str = "semi_auto"
    approval_ui: str = "popup"
    audio_device_name: str = ""
    citation_detection_mode: str = "hybrid_confirm"
    holyrics_token: str = ""
    holyrics_port: int = DEFAULT_PORT
    quick_seconds: float = 5.0
    open_operator_qr: bool = True
    auto_hide: bool = True
    text_detection_db: Path = DEFAULT_TEXT_DETECTION_DB


class SingleInstanceGuard:
    """Keep one LiVerse controller and ask it to show itself on a second launch."""

    def __init__(self, port: int = INSTANCE_PORT):
        self.port = port
        self.socket: socket.socket | None = None
        self.on_show = None
        self.closed = False

    def acquire(self) -> bool:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", self.port))
            listener.listen(2)
            listener.settimeout(0.5)
        except OSError as bind_error:
            listener.close()
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5) as client:
                    client.sendall(b"show\n")
            except OSError:
                raise bind_error
            return False
        self.socket = listener
        self.port = int(listener.getsockname()[1])
        threading.Thread(target=self._listen, daemon=True).start()
        return True

    def _listen(self) -> None:
        while not self.closed and self.socket is not None:
            try:
                client, _address = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with client:
                try:
                    message = client.recv(32).strip()
                except OSError:
                    message = b""
            if message == b"show" and self.on_show is not None:
                self.on_show()

    def close(self) -> None:
        self.closed = True
        if self.socket is not None:
            try:
                self.socket.close()
            except OSError:
                pass


def _valid_choice(value: str, choices: dict[str, str], fallback: str) -> str:
    return value if value in choices else fallback


def _port_from_url(value: str) -> int | None:
    try:
        return urlsplit(value).port
    except ValueError:
        return None


def load_gui_config() -> GuiConfig:
    settings = load_startup_settings()
    run_mode = _valid_choice(str(settings.get("run_mode") or ""), RUN_MODE_LABELS, "semi_auto")
    approval_ui = _valid_choice(str(settings.get("approval_ui") or ""), APPROVAL_LABELS, "popup")
    detection_mode = _valid_choice(
        str(
            settings.get("citation_detection_mode")
            or env_setting("LIVERSE_CITATION_DETECTION_MODE", "hybrid_confirm")
        ),
        DETECTION_LABELS,
        "hybrid_confirm",
    )
    try:
        quick_seconds = max(0.0, float(settings.get("holyrics_quick_minutes") or 0.0) * 60.0)
    except (TypeError, ValueError):
        quick_seconds = 5.0
    if "holyrics_quick_minutes" not in settings:
        try:
            quick_seconds = max(0.0, float(env_setting("HOLYRICS_QUICK_MINUTES", "0") or 0.0) * 60.0)
        except ValueError:
            quick_seconds = 5.0

    port_text = env_setting("HOLYRICS_PORT") or env_setting("HOLYRICS_API_PORT")
    if not port_text:
        port_text = str(_port_from_url(env_setting("HOLYRICS_URL")) or DEFAULT_PORT)
    try:
        port = int(port_text)
    except ValueError:
        port = DEFAULT_PORT
    if not 1 <= port <= 65535:
        port = DEFAULT_PORT

    return GuiConfig(
        run_mode=run_mode,
        approval_ui=approval_ui,
        audio_device_name=str(settings.get("audio_device_name") or env_setting("LIVERSE_AUDIO_DEVICE")),
        citation_detection_mode=detection_mode,
        holyrics_token=env_setting("HOLYRICS_TOKEN"),
        holyrics_port=port,
        quick_seconds=quick_seconds,
        open_operator_qr=bool(settings.get("open_operator_qr", True)),
        auto_hide=bool(settings.get("gui_auto_hide", True)),
        text_detection_db=Path(
            env_setting("LIVERSE_TEXT_DETECTION_DB", str(DEFAULT_TEXT_DETECTION_DB))
        ).expanduser(),
    )


def save_gui_config(config: GuiConfig) -> None:
    save_holyrics_env(config.holyrics_token, config.holyrics_port)
    previous = load_startup_settings()
    args = SimpleNamespace(
        _liverse_startup_settings_enabled=True,
        require_approval=config.run_mode == "approval",
        semi_auto_approval=config.run_mode == "semi_auto",
        approval_ui=config.approval_ui,
        device_name=config.audio_device_name,
        citation_detection_mode=config.citation_detection_mode,
        open_operator_qr=config.open_operator_qr,
        gui_auto_hide=config.auto_hide,
        holyrics_theme=str(previous.get("holyrics_theme") or ""),
        holyrics_quick_minutes=config.quick_seconds / 60.0,
    )
    save_startup_settings(args)


def engine_command(
    config: GuiConfig,
    *,
    project_root: Path = PROJECT_ROOT,
    python_executable: str | None = None,
    application_executable: Path | None = None,
    frozen: bool | None = None,
) -> list[str]:
    selected_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if selected_frozen:
        gui_executable = application_executable or Path(sys.executable)
        command = [str(gui_executable.with_name("LiVerseEngine.exe"))]
    else:
        command = [
            python_executable or sys.executable,
            str(project_root / "tools" / "vosk_grammar_probe.py"),
        ]
    command.extend([
        "--slide-output",
        "holyrics",
        "--sermon-plan",
        "--citation-detection-mode",
        config.citation_detection_mode,
        "--approval-ui",
        config.approval_ui,
        "--holyrics-url",
        f"http://localhost:{config.holyrics_port}",
        "--holyrics-quick-minutes",
        f"{config.quick_seconds / 60.0:g}",
        "--text-detection-db",
        str(config.text_detection_db),
        "--print-log-path",
        "--stop-file",
        str(engine_stop_path()),
    ])
    if config.run_mode == "semi_auto":
        command.append("--semi-auto-approval")
    elif config.run_mode == "approval":
        command.append("--require-approval")
    if config.audio_device_name:
        command.extend(("--device-name", config.audio_device_name))
    # The GUI owns the QR window so an external image viewer cannot enlarge it.
    command.append("--no-open-operator-qr")
    return command


def audio_input_names() -> tuple[list[str], str]:
    import sounddevice as sd

    devices = list(sd.query_devices())
    default_device = sd.default.device
    default_index = default_device[0] if isinstance(default_device, (list, tuple)) else default_device
    names: list[str] = []
    default_name = ""
    for index, device in enumerate(devices):
        try:
            channels = int(device.get("max_input_channels") or 0)
        except (AttributeError, TypeError, ValueError):
            channels = 0
        if channels <= 0:
            continue
        name = str(device.get("name") or f"Устройство {index}").strip()
        if name and name not in names:
            names.append(name)
        if index == default_index:
            default_name = name
    return names, default_name


def holyrics_check_args(config: GuiConfig) -> SimpleNamespace:
    return SimpleNamespace(
        holyrics_url=f"http://localhost:{config.holyrics_port}",
        holyrics_token=config.holyrics_token,
        holyrics_timeout=1.5,
        sermon_plan=True,
        holyrics_theme="",
    )


def configure_linux_tray_backend() -> None:
    """Prefer the system Ayatana AppIndicator backend in Debian virtualenvs."""
    if not sys.platform.startswith("linux") or os.environ.get("PYSTRAY_BACKEND"):
        return
    if (os.environ.get("XDG_SESSION_TYPE") or "").casefold() != "wayland":
        return
    system_packages = Path("/usr/lib/python3/dist-packages")
    if (system_packages / "gi" / "__init__.py").is_file():
        system_path = str(system_packages)
        if system_path not in sys.path:
            sys.path.append(system_path)
        os.environ["PYSTRAY_BACKEND"] = "appindicator"


def tray_can_hide_window(
    *,
    platform: str | None = None,
    session_type: str | None = None,
    backend: str = "",
) -> bool:
    """Use taskbar minimising when Wayland cannot reliably restore a Tk window."""
    selected_platform = platform or sys.platform
    selected_session = (session_type or os.environ.get("XDG_SESSION_TYPE") or "").casefold()
    appindicator = backend.casefold().endswith("_appindicator")
    return selected_platform == "win32" or selected_session != "wayland" or appindicator


def tray_needs_own_event_loop(*, platform: str | None = None, backend: str = "") -> bool:
    """Gtk tray backends need their own event loop beside Tkinter."""
    selected_platform = (platform or sys.platform).casefold()
    selected_backend = backend.casefold()
    return selected_platform.startswith("linux") and selected_backend.endswith(
        ("_appindicator", "_gtk")
    )


class LiVerseGui:
    def __init__(self, root: tk.Tk, instance_guard: SingleInstanceGuard | None = None):
        self.root = root
        self.instance_guard = instance_guard
        self.config = load_gui_config()
        self.process: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.tray_icon = None
        self.tray_available = False
        self.tray_backend = ""
        self.stopping = False
        self.last_log_path = ""
        self._auto_hide_pending = False
        self._quit_after_engine_stop = False
        self._quit_restart = False
        self.qr_window = None
        self.qr_image = None
        self.help_images = []

        self.state_var = tk.StringVar(value="Подготовка к запуску")
        self.microphone_status_var = tk.StringVar(value="ещё не проверен")
        self.holyrics_status_var = tk.StringVar(value="ещё не проверен")
        self.database_status_var = tk.StringVar(value="ещё не проверена")
        self.activity_var = tk.StringVar(value="LiVerse запускается")
        self.run_button_var = tk.StringVar(value="Начать распознавание")
        self.microphone_level_var = tk.DoubleVar(value=0.0)

        self.run_mode_var = tk.StringVar(value=RUN_MODE_LABELS[self.config.run_mode])
        self.approval_var = tk.StringVar(value=APPROVAL_LABELS[self.config.approval_ui])
        self.microphone_var = tk.StringVar(value=self.config.audio_device_name or "Автоматический выбор")
        self.detection_var = tk.StringVar(value=DETECTION_LABELS[self.config.citation_detection_mode])
        self.quick_seconds_var = tk.StringVar(value=f"{self.config.quick_seconds:g}")
        self.token_var = tk.StringVar(value=self.config.holyrics_token)
        self.port_var = tk.StringVar(value=str(self.config.holyrics_port))
        self.auto_hide_var = tk.BooleanVar(value=self.config.auto_hide)
        self.open_qr_var = tk.BooleanVar(value=self.config.open_operator_qr)
        self.include_audio_logs_var = tk.BooleanVar(value=False)
        self.log_sessions: list[Path] = []

        self._configure_window()
        self._build_interface()
        self.state_var.trace_add("write", self._refresh_state_badge)
        self._refresh_state_badge()
        self._refresh_microphones()
        self._refresh_database_status()
        self._start_tray()
        self.root.after(100, self._poll_events)
        self.root.after(350, self._begin_update_check)

    def _configure_window(self) -> None:
        self.root.title("LiVerse")
        self.root.geometry("760x640")
        self.root.minsize(700, 560)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        try:
            self.root.iconphoto(True, tk.PhotoImage(file=str(PROJECT_ROOT / "LiVerse.png")))
        except tk.TclError:
            pass
        style = ttk.Style(self.root)
        for theme in ("vista", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure("Title.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("Card.TLabelframe", padding=12)
        style.configure("Card.TLabelframe.Label", font=("Segoe UI", 11, "bold"))
        style.configure("Large.TButton", padding=(14, 8))
        style.configure(
            "Primary.TButton",
            padding=(14, 8),
            background=BRAND_BLUE,
            foreground="#FFFFFF",
        )
        style.map(
            "Primary.TButton",
            background=[("active", BRAND_BLUE_ACTIVE), ("pressed", BRAND_BLUE_ACTIVE)],
            foreground=[("disabled", "#D8E4EE"), ("!disabled", "#FFFFFF")],
        )

    def _build_interface(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        header = tk.Frame(
            outer,
            background="#FFFFFF",
            padx=12,
            pady=8,
            highlightthickness=1,
            highlightbackground=HEADER_BORDER,
        )
        header.pack(fill="x", pady=(0, 12))
        try:
            from PIL import Image, ImageTk

            icon = Image.open(PROJECT_ROOT / "LiVerse.png").convert("RGBA")
            self.header_icon = ImageTk.PhotoImage(icon.resize((44, 44), Image.Resampling.LANCZOS))
            tk.Label(header, image=self.header_icon, background="#FFFFFF", borderwidth=0).pack(side="left")
        except Exception:
            self.header_icon = None
        tk.Label(
            header,
            text="LiVerse",
            background="#FFFFFF",
            foreground=BRAND_BLUE,
            font=("Segoe UI", 20, "bold"),
        ).pack(side="left", padx=(10, 0))
        self.state_badge = tk.Label(
            header,
            textvariable=self.state_var,
            font=("Segoe UI", 10, "bold"),
            padx=10,
            pady=5,
            borderwidth=0,
        )
        self.state_badge.pack(side="right", pady=7)

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self.status_tab = ttk.Frame(self.notebook, padding=14)
        self.settings_tab = ttk.Frame(self.notebook, padding=14)
        self.diagnostics_tab = ttk.Frame(self.notebook, padding=14)
        self.logs_tab = ttk.Frame(self.notebook, padding=14)
        self.help_tab = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(self.status_tab, text="Состояние")
        self.notebook.add(self.settings_tab, text="Настройки")
        self.notebook.add(self.diagnostics_tab, text="Диагностика")
        self.notebook.add(self.logs_tab, text="Журналы")
        self.notebook.add(self.help_tab, text="Помощь")
        self._build_status_tab()
        self._build_settings_tab()
        self._build_diagnostics_tab()
        self._build_logs_tab()
        self._build_help_tab()

    def _build_status_tab(self) -> None:
        checks = ttk.LabelFrame(self.status_tab, text="Готовность", style="Card.TLabelframe")
        checks.pack(fill="x")
        rows = (
            ("Микрофон", self.microphone_status_var),
            ("HoLyrics", self.holyrics_status_var),
            ("База Библии", self.database_status_var),
        )
        for row, (label, variable) in enumerate(rows):
            ttk.Label(checks, text=label, width=18).grid(row=row, column=0, sticky="w", pady=5)
            ttk.Label(checks, textvariable=variable).grid(row=row, column=1, sticky="w", pady=5)
        checks.columnconfigure(1, weight=1)

        activity = ttk.LabelFrame(self.status_tab, text="Текущая работа", style="Card.TLabelframe")
        activity.pack(fill="both", expand=True, pady=14)
        ttk.Label(activity, textvariable=self.activity_var, wraplength=640, justify="left").pack(
            anchor="w", fill="x", pady=8
        )
        level_row = ttk.Frame(activity)
        level_row.pack(fill="x", pady=(2, 8))
        ttk.Label(level_row, text="Уровень микрофона", width=18).pack(side="left")
        ttk.Progressbar(
            level_row,
            variable=self.microphone_level_var,
            maximum=100,
            mode="determinate",
        ).pack(side="left", fill="x", expand=True)

        buttons = ttk.Frame(self.status_tab)
        buttons.pack(fill="x")
        ttk.Button(
            buttons,
            textvariable=self.run_button_var,
            command=self.toggle_engine,
            style="Primary.TButton",
        ).pack(side="left")
        ttk.Button(buttons, text="Проверить HoLyrics", command=self.check_holyrics).pack(side="left", padx=8)
        ttk.Button(buttons, text="Завершить LiVerse", command=self.quit_application).pack(side="right")
        ttk.Button(buttons, text="Скрыть", command=self.hide_window).pack(side="right", padx=8)

    def _build_settings_tab(self) -> None:
        self.settings_tab.columnconfigure(1, weight=1)
        row = 0

        def add_combo(label: str, variable: tk.StringVar, values: list[str]) -> ttk.Combobox:
            nonlocal row
            ttk.Label(self.settings_tab, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=7)
            combo = ttk.Combobox(self.settings_tab, textvariable=variable, values=values, state="readonly")
            combo.grid(row=row, column=1, sticky="ew", pady=7)
            row += 1
            return combo

        add_combo("Режим работы", self.run_mode_var, list(RUN_MODE_LABELS.values()))
        add_combo("Подтверждение", self.approval_var, list(APPROVAL_LABELS.values()))
        self.microphone_combo = add_combo("Микрофон", self.microphone_var, ["Автоматический выбор"])
        add_combo("Распознавание", self.detection_var, list(DETECTION_LABELS.values()))

        ttk.Label(self.settings_tab, text="Время показа, секунд").grid(
            row=row, column=0, sticky="w", padx=(0, 12), pady=7
        )
        ttk.Entry(self.settings_tab, textvariable=self.quick_seconds_var, width=12).grid(
            row=row, column=1, sticky="w", pady=7
        )
        row += 1

        ttk.Separator(self.settings_tab).grid(row=row, column=0, columnspan=2, sticky="ew", pady=12)
        row += 1
        ttk.Label(self.settings_tab, text="HoLyrics token").grid(
            row=row, column=0, sticky="w", padx=(0, 12), pady=7
        )
        ttk.Entry(self.settings_tab, textvariable=self.token_var, show="●").grid(
            row=row, column=1, sticky="ew", pady=7
        )
        row += 1
        ttk.Label(self.settings_tab, text="Порт HoLyrics").grid(
            row=row, column=0, sticky="w", padx=(0, 12), pady=7
        )
        ttk.Entry(self.settings_tab, textvariable=self.port_var, width=12).grid(
            row=row, column=1, sticky="w", pady=7
        )
        row += 1

        ttk.Checkbutton(
            self.settings_tab,
            text="После успешного запуска скрывать окно",
            variable=self.auto_hide_var,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=6)
        row += 1
        ttk.Checkbutton(
            self.settings_tab,
            text="Показывать QR-код для подключения телефона",
            variable=self.open_qr_var,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=6)
        row += 1

        actions = ttk.Frame(self.settings_tab)
        actions.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        ttk.Button(actions, text="Сохранить и запустить", command=self.save_and_start, style="Primary.TButton").pack(
            side="left"
        )
        ttk.Button(actions, text="Обновить список микрофонов", command=self._refresh_microphones).pack(
            side="left", padx=8
        )
        ttk.Button(actions, text="Разрешения HoLyrics", command=self.show_permissions).pack(side="right")

    def _build_diagnostics_tab(self) -> None:
        self.diagnostics_text = tk.Text(
            self.diagnostics_tab,
            height=20,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
        )
        scrollbar = ttk.Scrollbar(self.diagnostics_tab, command=self.diagnostics_text.yview)
        self.diagnostics_text.configure(yscrollcommand=scrollbar.set)
        self.diagnostics_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        bottom = ttk.Frame(self.diagnostics_tab)
        bottom.place(relx=0, rely=1, anchor="sw")

    def _build_logs_tab(self) -> None:
        ttk.Label(
            self.logs_tab,
            text="Журналы для диагностики",
            font=("Segoe UI", 15, "bold"),
        ).pack(anchor="w", pady=(0, 8))
        ttk.Label(
            self.logs_tab,
            text=(
                "Журналы помогают понять, что услышала программа и почему предложила ссылку. "
                "Они могут содержать распознанные слова проповеди. Token HoLyrics и настройки "
                "в архив не включаются. Выберите только те сеансы, которыми готовы поделиться."
            ),
            wraplength=660,
            justify="left",
        ).pack(anchor="w", fill="x", pady=(0, 10))

        list_frame = ttk.Frame(self.logs_tab)
        list_frame.pack(fill="both", expand=True)
        self.logs_listbox = tk.Listbox(
            list_frame,
            selectmode="extended",
            exportselection=False,
            font=("Consolas", 9),
        )
        scrollbar = ttk.Scrollbar(list_frame, command=self.logs_listbox.yview)
        self.logs_listbox.configure(yscrollcommand=scrollbar.set)
        self.logs_listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        ttk.Checkbutton(
            self.logs_tab,
            text="Добавить аудиозапись (может быть большой и содержит речь)",
            variable=self.include_audio_logs_var,
        ).pack(anchor="w", pady=(10, 4))
        ttk.Label(
            self.logs_tab,
            text=(
                "Прямая отправка на сервер пока не настроена. Сначала создайте архив и "
                "передайте его вручную. Для Google Drive потребуется отдельное безопасное подключение."
            ),
            foreground="#7A4D00",
            wraplength=660,
            justify="left",
        ).pack(anchor="w", fill="x", pady=(0, 8))

        actions = ttk.Frame(self.logs_tab)
        actions.pack(fill="x")
        ttk.Button(actions, text="Обновить список", command=self._refresh_log_sessions).pack(side="left")
        ttk.Button(actions, text="Выбрать последний", command=self._select_latest_log).pack(
            side="left", padx=8
        )
        ttk.Button(
            actions,
            text="Создать архив…",
            command=self._export_selected_logs,
            style="Primary.TButton",
        ).pack(side="right")
        self._refresh_log_sessions()

    def _refresh_log_sessions(self) -> None:
        self.log_sessions = list_log_sessions()
        self.logs_listbox.delete(0, "end")
        for session in self.log_sessions:
            files = [name for name in (*LOG_EXPORT_NAMES, "audio.wav") if (session / name).is_file()]
            self.logs_listbox.insert("end", f"{session.name}   ({', '.join(files)})")

    def _select_latest_log(self) -> None:
        self.logs_listbox.selection_clear(0, "end")
        if self.log_sessions:
            self.logs_listbox.selection_set(0)
            self.logs_listbox.see(0)

    def _export_selected_logs(self) -> None:
        selected = [self.log_sessions[index] for index in self.logs_listbox.curselection()]
        if not selected:
            messagebox.showinfo("LiVerse", "Сначала выберите хотя бы один сеанс.")
            return
        suggested = f"LiVerse-logs-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        filename = filedialog.asksaveasfilename(
            parent=self.root,
            title="Сохранить журналы LiVerse",
            defaultextension=".zip",
            initialfile=suggested,
            filetypes=[("ZIP-архив", "*.zip")],
        )
        if not filename:
            return
        try:
            count = create_log_archive(
                selected,
                Path(filename),
                include_audio=bool(self.include_audio_logs_var.get()),
            )
        except (OSError, ValueError) as exc:
            messagebox.showerror("LiVerse", f"Не удалось создать архив:\n{exc}")
            return
        messagebox.showinfo(
            "LiVerse",
            f"Архив создан:\n{filename}\n\nДобавлено файлов: {count}",
        )

    def _build_help_tab(self) -> None:
        self.help_tab.rowconfigure(0, weight=1)
        self.help_tab.columnconfigure(0, weight=1)
        canvas = tk.Canvas(self.help_tab, highlightthickness=0)
        vertical = ttk.Scrollbar(self.help_tab, orient="vertical", command=canvas.yview)
        horizontal = ttk.Scrollbar(self.help_tab, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")

        content = ttk.Frame(canvas, padding=(2, 2, 12, 12))
        canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )

        ttk.Label(
            content,
            text="Первоначальная настройка LiVerse",
            font=("Segoe UI", 15, "bold"),
        ).pack(anchor="w", pady=(0, 10))
        ttk.Label(
            content,
            text=(
                "LiVerse настраивает и проверяет большую часть параметров автоматически. "
                "Если что-то не работает, программа сообщает, что нужно проверить. "
                "Первоначальную интеграцию с HoLyrics нужно выполнить вручную."
            ),
            wraplength=660,
            justify="left",
        ).pack(anchor="w", fill="x", pady=(0, 14))

        steps = (
            "1. Откройте HoLyrics.",
            "2. Откройте Файл → Настройки → API Server.",
            "3. Включите API Server Local и проверьте порт.",
            "4. Создайте token, включите необходимые разрешения и скопируйте token.",
            "5. Вставьте token и порт на вкладке «Настройки» LiVerse.",
        )
        for step in steps:
            ttk.Label(content, text=step, wraplength=900, justify="left").pack(
                anchor="w", fill="x", pady=3
            )
        ttk.Label(
            content,
            text="Не публикуйте token и не отправляйте его вместе с журналами.",
            foreground="#A12622",
            wraplength=660,
            justify="left",
        ).pack(anchor="w", fill="x", pady=(16, 10))
        ttk.Button(
            content,
            text="Показать необходимые разрешения HoLyrics",
            command=self.show_permissions,
        ).pack(anchor="w", pady=(0, 18))

        screenshots = (
            ("holyrics-api-server.png", "1. Включите API Server Local и проверьте порт."),
            ("holyrics-tokens.png", "2. Откройте управление разрешениями и выберите token."),
            ("holyrics-permissions.png", "3. Включите нужные флажки в столбце Local."),
        )
        try:
            from PIL import Image, ImageTk

            for filename, caption in screenshots:
                ttk.Label(content, text=caption, font=("Segoe UI", 11, "bold")).pack(
                    anchor="w", pady=(8, 6)
                )
                image = Image.open(PROJECT_ROOT / "assets" / "help" / filename).convert("RGB")
                if filename == "holyrics-permissions.png":
                    image = image.crop((0, 145, image.width, image.height))
                photo = ImageTk.PhotoImage(image)
                self.help_images.append(photo)
                tk.Label(content, image=photo, borderwidth=1, relief="solid").pack(anchor="w")
        except (OSError, tk.TclError) as exc:
            ttk.Label(
                content,
                text=f"Снимки справки недоступны: {exc}",
                foreground="#A12622",
                wraplength=660,
            ).pack(anchor="w")

    def _refresh_state_badge(self, *_args) -> None:
        state = self.state_var.get().casefold()
        if state == "работает":
            palette = STATE_COLORS["good"]
        elif "ошиб" in state or "не подключ" in state:
            palette = STATE_COLORS["error"]
        elif any(word in state for word in ("нуж", "вниман", "настрой", "обнов")):
            palette = STATE_COLORS["attention"]
        else:
            palette = STATE_COLORS["neutral"]
        self.state_badge.configure(background=palette[0], foreground=palette[1])

    def _refresh_microphones(self) -> None:
        try:
            names, default_name = audio_input_names()
        except Exception as exc:
            self.microphone_status_var.set(f"ошибка получения списка: {exc}")
            return
        values = ["Автоматический выбор", *names]
        self.microphone_combo.configure(values=values)
        current = self.microphone_var.get().strip()
        if current not in values:
            values.append(current)
            self.microphone_combo.configure(values=values)
        if self.config.audio_device_name:
            self.microphone_status_var.set(self.config.audio_device_name)
        elif default_name:
            self.microphone_status_var.set(f"автоматически; системный: {default_name}")
        else:
            self.microphone_status_var.set("автоматический выбор")

    def _refresh_database_status(self) -> None:
        if self.config.citation_detection_mode == "address_only":
            self.database_status_var.set("не требуется в выбранном режиме")
        elif self.config.text_detection_db.is_file():
            self.database_status_var.set("готова")
        else:
            self.database_status_var.set(f"не найдена: {self.config.text_detection_db}")

    def _collect_config(self) -> GuiConfig | None:
        try:
            port = int(self.port_var.get().strip())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror("LiVerse", "Порт HoLyrics должен быть целым числом от 1 до 65535.")
            return None
        try:
            quick_seconds = float(self.quick_seconds_var.get().strip().replace(",", "."))
            if quick_seconds < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("LiVerse", "Время показа должно быть числом не меньше нуля.")
            return None
        return GuiConfig(
            run_mode=RUN_MODE_VALUES.get(self.run_mode_var.get(), "semi_auto"),
            approval_ui=APPROVAL_VALUES.get(self.approval_var.get(), "popup"),
            audio_device_name="" if self.microphone_var.get() == "Автоматический выбор" else self.microphone_var.get(),
            citation_detection_mode=DETECTION_VALUES.get(self.detection_var.get(), "hybrid_confirm"),
            holyrics_token=self.token_var.get().strip(),
            holyrics_port=port,
            quick_seconds=quick_seconds,
            open_operator_qr=bool(self.open_qr_var.get()),
            auto_hide=bool(self.auto_hide_var.get()),
            text_detection_db=self.config.text_detection_db,
        )

    def save_and_start(self) -> None:
        config = self._collect_config()
        if config is None:
            return
        try:
            save_gui_config(config)
        except OSError as exc:
            messagebox.showerror("LiVerse", f"Не удалось сохранить настройки:\n{exc}")
            return
        self.config = config
        self._refresh_database_status()
        if self.process is not None and self.process.poll() is None:
            self.stop_engine(restart=True)
        else:
            self.start_engine()

    def toggle_engine(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.stop_engine()
        else:
            self.save_and_start()

    def start_engine(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        if not self.config.holyrics_token:
            self.state_var.set("Нужна настройка")
            self.activity_var.set("Введите token HoLyrics на вкладке «Настройки».")
            self.holyrics_status_var.set("token не задан")
            self.notebook.select(self.settings_tab)
            self.show_window()
            return
        if (
            self.config.citation_detection_mode != "address_only"
            and not self.config.text_detection_db.is_file()
        ):
            self.state_var.set("Нужна база Библии")
            self.activity_var.set(f"Не найден файл: {self.config.text_detection_db}")
            self.show_window()
            return

        command = engine_command(self.config)
        try:
            engine_stop_path().unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            write_gui_log(f"Не удалось удалить старую команду остановки: {exc}")
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        kwargs: dict[str, object] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        try:
            self.process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
                **kwargs,
            )
        except OSError as exc:
            self.state_var.set("Ошибка запуска")
            self.activity_var.set(str(exc))
            self.show_window()
            return
        self.stopping = False
        self._auto_hide_pending = self.config.auto_hide
        self.state_var.set("Запускается")
        self.activity_var.set("Проверяю HoLyrics и загружаю распознавание…")
        self.run_button_var.set("Остановить распознавание")
        threading.Thread(target=self._read_engine_output, daemon=True).start()
        if self.tray_icon is not None:
            try:
                self.tray_icon.update_menu()
            except Exception:
                pass

    def stop_engine(self, *, restart: bool = False) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            if restart:
                self.root.after(200, self.start_engine)
            return
        self.stopping = True
        self.state_var.set("Останавливается")
        try:
            path = engine_stop_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("restart" if restart else "stop", encoding="utf-8")
        except OSError as exc:
            write_gui_log(f"Не удалось запросить штатную остановку движка: {exc}")
            try:
                if os.name == "nt":
                    process.terminate()
                else:
                    os.killpg(process.pid, signal.SIGINT)
            except (OSError, ProcessLookupError):
                pass
        if restart:
            self.root.after(900, self._restart_after_stop)

    def _restart_after_stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.root.after(300, self._restart_after_stop)
            return
        self.start_engine()

    def _read_engine_output(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        for raw_line in process.stdout:
            self.output_queue.put(("line", raw_line.rstrip()))
        code = process.wait()
        self.output_queue.put(("exit", code))

    def _poll_events(self) -> None:
        try:
            while True:
                event, payload = self.output_queue.get_nowait()
                if event == "line":
                    self._handle_engine_line(str(payload))
                elif event == "exit":
                    self._handle_engine_exit(int(payload))
                elif event == "tray_error":
                    self.tray_available = False
                    message = f"Системный трей недоступен: {payload}"
                    self._append_diagnostic(message)
                    write_gui_log(message)
                elif event == "update_result":
                    self._handle_update_result(payload)
                elif event == "update_installed":
                    self._handle_update_installed(payload)
                elif event == "update_progress":
                    downloaded, total = payload
                    self._handle_update_progress(int(downloaded), int(total))
                elif event == "holyrics_result":
                    self._handle_holyrics_result(payload)
                elif event == "show":
                    self.show_window()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _handle_engine_line(self, line: str) -> None:
        if not line:
            return
        if line.startswith("Аудиоуровень:"):
            try:
                self.microphone_level_var.set(float(line.partition(":")[2].strip()))
            except ValueError:
                pass
            return
        self._append_diagnostic(line)
        if line.startswith("Статус:"):
            status = line.partition(":")[2].strip()
            self.activity_var.set(status)
            if status.startswith("отправлено в Holyrics:"):
                self.state_var.set("Работает")
        elif line.startswith("Микрофон:"):
            microphone = line.partition(":")[2].strip()
            self.microphone_status_var.set(microphone)
            self.state_var.set("Работает")
            self.activity_var.set("Распознаю проповедь")
            if self._auto_hide_pending:
                self._auto_hide_pending = False
                self.root.after(1500, self.hide_window)
        elif line.startswith("Vosk log:"):
            self.last_log_path = line.partition(":")[2].strip()
        elif line.startswith("Пульт подтверждения:"):
            operator_url = line.partition(":")[2].strip()
            if self.config.open_operator_qr and operator_url:
                self._show_operator_qr(operator_url)
        elif "API Server доступен" in line:
            self.holyrics_status_var.set("подключён")
        elif "API Server сейчас недоступен" in line:
            self.holyrics_status_var.set("не подключён")
            self.state_var.set("HoLyrics не подключён")
            self.activity_var.set("Запустите HoLyrics и нажмите «Начать распознавание».")
            self.show_window()
        elif "План проповеди не найден" in line:
            self.activity_var.set("Откройте в HoLyrics презентацию плана проповеди и запустите её показ.")
            self.show_window()
        elif "база поиска цитат по тексту не найдена" in line:
            self.database_status_var.set("не найдена")
            self.show_window()

    def _handle_engine_exit(self, code: int) -> None:
        expected = self.stopping
        self.process = None
        self.microphone_level_var.set(0.0)
        try:
            engine_stop_path().unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        self.run_button_var.set("Начать распознавание")
        if expected:
            self.state_var.set("Остановлен")
            self.activity_var.set("Распознавание приостановлено оператором")
        elif code == 0:
            self.state_var.set("Остановлен")
            self.activity_var.set("LiVerse завершил работу")
        else:
            self.state_var.set("Требуется внимание")
            self.activity_var.set(f"LiVerse остановился с кодом {code}. Подробности на вкладке «Диагностика».")
            self.show_window()
        if self.tray_icon is not None:
            try:
                self.tray_icon.update_menu()
            except Exception:
                pass
        if self._quit_after_engine_stop:
            restart = self._quit_restart
            self._quit_after_engine_stop = False
            self._quit_restart = False
            self.root.after(0, lambda: self._finalize_quit(restart=restart))

    def _append_diagnostic(self, line: str) -> None:
        self.diagnostics_text.configure(state="normal")
        self.diagnostics_text.insert("end", line + "\n")
        line_count = int(self.diagnostics_text.index("end-1c").split(".")[0])
        if line_count > 500:
            self.diagnostics_text.delete("1.0", f"{line_count - 500}.0")
        self.diagnostics_text.see("end")
        self.diagnostics_text.configure(state="disabled")

    def check_holyrics(self) -> None:
        config = self._collect_config()
        if config is None:
            return
        self.holyrics_status_var.set("проверяю…")
        threading.Thread(target=self._check_holyrics_worker, args=(config,), daemon=True).start()

    def _check_holyrics_worker(self, config: GuiConfig) -> None:
        result = check_holyrics_api_server(holyrics_check_args(config))
        self.output_queue.put(("holyrics_result", result))

    def _handle_holyrics_result(self, payload: object) -> None:
        result = payload if isinstance(payload, dict) else {}
        if not result.get("ok"):
            self.holyrics_status_var.set("не подключён")
            self.activity_var.set("Запустите HoLyrics и проверьте порт API Server Local.")
            return
        missing = list(result.get("missing_permissions") or [])
        if missing:
            self.holyrics_status_var.set("не хватает разрешений")
            self.activity_var.set("В токене HoLyrics включены не все разрешения: " + ", ".join(missing))
            return
        version = str(result.get("version") or "").strip()
        self.holyrics_status_var.set(f"подключён{', версия ' + version if version else ''}")
        self.activity_var.set("Связь с HoLyrics работает")

    def show_permissions(self) -> None:
        permissions = required_holyrics_permissions(SimpleNamespace(sermon_plan=True, holyrics_theme=""))
        messagebox.showinfo(
            "Разрешения HoLyrics",
            "В HoLyrics → Settings → API Server → Manage permissions\n"
            "включите в столбце Local:\n\n" + "\n".join(f"• {item}" for item in permissions),
        )

    def _show_operator_qr(self, url: str) -> None:
        if self.qr_window is not None and self.qr_window.winfo_exists():
            self.qr_window.lift()
            self.qr_window.focus_force()
            return
        try:
            import qrcode
            from PIL import ImageTk

            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=8,
                border=4,
            )
            qr.add_data(url)
            qr.make(fit=True)
            image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            self.qr_image = ImageTk.PhotoImage(image)
        except Exception as exc:
            write_gui_log(f"Не удалось создать QR-код: {exc}")
            return

        window = tk.Toplevel(self.root)
        self.qr_window = window
        window.title("LiVerse — подключение телефона")
        window.resizable(False, False)
        window.attributes("-topmost", True)
        ttk.Label(window, image=self.qr_image).pack(padx=16, pady=(16, 8))
        ttk.Label(
            window,
            text="Наведите камеру телефона на QR-код",
            justify="center",
        ).pack(padx=16, pady=(0, 8))
        ttk.Button(window, text="Закрыть", command=window.destroy).pack(
            fill="x", padx=16, pady=(0, 16)
        )
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        window.update_idletasks()
        width = window.winfo_reqwidth()
        height = window.winfo_reqheight()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 2)
        window.geometry(f"{width}x{height}{x:+d}{y:+d}")
        window.lift()

    def _begin_update_check(self) -> None:
        self.activity_var.set("Проверяю обновления…")
        threading.Thread(target=self._update_check_worker, daemon=True).start()

    def _update_check_worker(self) -> None:
        self.output_queue.put(("update_result", check_gui_update()))

    def _handle_update_result(self, payload: object) -> None:
        update = payload if isinstance(payload, dict) else {}
        if update.get("status") == "available":
            if update.get("kind") == "binary":
                local_label = f"LiVerse {update.get('local_version')}"
                remote_label = f"LiVerse {update.get('remote_version')}"
            else:
                local_label = str(update.get("local_label") or "установленная версия")
                remote_label = str(update.get("remote_label") or "новая версия")
            install = messagebox.askyesno(
                "Обновление LiVerse",
                f"Доступно обновление.\n\nСейчас: {local_label}\nНа GitHub: {remote_label}\n\nУстановить сейчас?",
            )
            if install:
                self.state_var.set("Обновляется")
                self.activity_var.set("Устанавливаю обновление LiVerse…")
                threading.Thread(target=self._install_update_worker, args=(update,), daemon=True).start()
                return
        elif update.get("status") not in {"current", "current_with_changes", "no_release"}:
            write_gui_log(f"Проверка обновления пропущена: {update}")
        self._start_after_update_check()

    def _install_update_worker(self, update: dict) -> None:
        if update.get("kind") == "binary":
            try:
                installer = download_windows_release_installer(
                    update,
                    progress=lambda downloaded, total: self.output_queue.put(
                        ("update_progress", (downloaded, total))
                    ),
                )
                launch_windows_release_installer(installer)
                result = {"ok": True, "kind": "binary", "installer": str(installer)}
            except (OSError, ReleaseUpdateError) as exc:
                result = {"ok": False, "kind": "binary", "reason": str(exc)}
            self.output_queue.put(("update_installed", result))
            return
        installed = apply_startup_update(update, hide_console=True)
        self.output_queue.put(
            ("update_installed", {"ok": installed, "kind": "source"})
        )

    def _handle_update_progress(self, downloaded: int, total: int) -> None:
        if total > 0:
            percent = min(100, max(0, round(downloaded * 100 / total)))
            self.activity_var.set(f"Скачиваю обновление: {percent}%")

    def _handle_update_installed(self, payload: object) -> None:
        result = payload if isinstance(payload, dict) else {"ok": bool(payload)}
        if not result.get("ok"):
            reason = str(result.get("reason") or "неизвестная ошибка")
            write_gui_log(f"Обновление не установлено: {reason}")
            messagebox.showerror(
                "LiVerse",
                "Обновление не завершилось. Установленная версия не повреждена.\n\n"
                f"Причина: {reason}\n\nПодробности находятся в журнале LiVerse.",
            )
            self._start_after_update_check()
            return
        if result.get("kind") == "binary":
            write_gui_log(f"Запущен проверенный установщик: {result.get('installer')}")
            self.quit_application()
            return
        messagebox.showinfo("LiVerse", "Обновление установлено. LiVerse сейчас перезапустится.")
        self.quit_application(restart=True)

    def _start_after_update_check(self) -> None:
        if not self.config.holyrics_token:
            self.state_var.set("Первоначальная настройка")
            self.activity_var.set("Введите token и порт HoLyrics, затем выберите микрофон.")
            self.notebook.select(self.settings_tab)
            return
        self.start_engine()

    def _start_tray(self) -> None:
        try:
            configure_linux_tray_backend()
            import pystray
            from PIL import Image

            self.tray_backend = str(pystray.Icon.__module__)
            image = Image.open(PROJECT_ROOT / "LiVerse.png").convert("RGBA")
            menu = pystray.Menu(
                pystray.MenuItem("Открыть LiVerse", lambda _icon, _item: self._tray_call(self.show_window), default=True),
                pystray.MenuItem(
                    "Начать распознавание",
                    lambda _icon, _item: self._tray_call(self.save_and_start),
                    enabled=lambda _item: self.process is None or self.process.poll() is not None,
                ),
                pystray.MenuItem(
                    "Остановить распознавание",
                    lambda _icon, _item: self._tray_call(self.stop_engine),
                    enabled=lambda _item: self.process is not None and self.process.poll() is None,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Завершить LiVerse", lambda _icon, _item: self._tray_call(self.quit_application)),
            )
            self.tray_icon = pystray.Icon("liverse", image, "LiVerse", menu)
            if tray_needs_own_event_loop(backend=self.tray_backend):
                def run_tray() -> None:
                    try:
                        self.tray_icon.run(setup=self._tray_ready)
                    except Exception as exc:
                        self.output_queue.put(("tray_error", str(exc)))

                threading.Thread(target=run_tray, daemon=True).start()
            else:
                self.tray_icon.run_detached(setup=self._tray_ready)
        except Exception as exc:
            self.output_queue.put(("tray_error", str(exc)))

    def _tray_ready(self, icon) -> None:
        self.tray_available = True
        icon.visible = True
        write_gui_log(f"Системный трей запущен: {self.tray_backend}")

    def _tray_call(self, callback) -> None:
        self.root.after(0, callback)

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.after(50, self.root.focus_force)

    def hide_window(self) -> None:
        if self.tray_available and tray_can_hide_window(backend=self.tray_backend):
            self.root.withdraw()
        else:
            self.root.iconify()

    def quit_application(self, *, restart: bool = False) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            self._quit_after_engine_stop = True
            self._quit_restart = restart
            self.stop_engine()
            return
        self._finalize_quit(restart=restart)

    def _finalize_quit(self, *, restart: bool = False) -> None:
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        if self.instance_guard is not None:
            self.instance_guard.close()
        self.root.destroy()
        if restart:
            if getattr(sys, "frozen", False):
                os.execv(sys.executable, [sys.executable])
            else:
                os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())])


def run_packaged_gui_smoke_test() -> int:
    """Open the real Tk/tray backend briefly and verify the packaged engine link."""
    instance_guard = SingleInstanceGuard(port=0)
    if not instance_guard.acquire():
        write_gui_log("Windows package smoke test failed: single-instance guard")
        return 2

    result = {"code": 3}
    root = tk.Tk(className="LiVerse")
    app = LiVerseGui(root, instance_guard)
    root.withdraw()

    def finish() -> None:
        failures: list[str] = []
        command = engine_command(app.config)
        engine_path = Path(command[0])
        if not engine_path.is_file():
            failures.append(f"packaged engine is missing: {engine_path}")
        else:
            run_options: dict[str, object] = {}
            if os.name == "nt":
                run_options["creationflags"] = subprocess.CREATE_NO_WINDOW
            try:
                engine_check = subprocess.run(
                    [str(engine_path), "--version"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                    **run_options,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                failures.append(f"packaged engine did not start: {exc}")
            else:
                if engine_check.returncode != 0 or "LiVerse " not in engine_check.stdout:
                    failures.append(
                        f"packaged engine check failed with code {engine_check.returncode}"
                    )
        if not app.tray_available:
            failures.append(f"tray backend did not start: {app.tray_backend or 'unknown'}")
        if failures:
            write_gui_log("Windows package smoke test failed: " + "; ".join(failures))
        else:
            result["code"] = 0
            write_gui_log(
                f"Windows package smoke test passed: engine={engine_path}; tray={app.tray_backend}"
            )
        app.quit_application()

    root.after(5000, finish)
    root.mainloop()
    instance_guard.close()
    return int(result["code"])


def main() -> int:
    try:
        if "--check-packaged-update" in sys.argv[1:]:
            return run_packaged_update_smoke_test()
        if "--check-packaged-gui" in sys.argv[1:]:
            return run_packaged_gui_smoke_test()
        instance_guard = SingleInstanceGuard()
        if not instance_guard.acquire():
            return 0
        root = tk.Tk(className="LiVerse")
        app = LiVerseGui(root, instance_guard)
        instance_guard.on_show = lambda: app.output_queue.put(("show", None))
        signal.signal(signal.SIGTERM, lambda _signum, _frame: root.after(0, app.quit_application))
        try:
            root.mainloop()
        except KeyboardInterrupt:
            app.quit_application()
        finally:
            instance_guard.close()
        return 0
    except Exception as exc:
        details = traceback.format_exc()
        write_gui_log(details)
        try:
            error_root = tk.Tk()
            error_root.withdraw()
            messagebox.showerror(
                "LiVerse — ошибка запуска",
                f"LiVerse не удалось запустить:\n\n{exc}\n\n"
                f"Подробности записаны в:\n{gui_log_path()}",
                parent=error_root,
            )
            error_root.destroy()
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
