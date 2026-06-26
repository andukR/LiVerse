#!/usr/bin/env python3
"""Local slide display server with Server-Sent Events updates."""

from __future__ import annotations

import argparse
import io
import json
import mimetypes
import queue
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote


__version__ = "0.1.0"
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
CORE_SRC = PROJECT_ROOT / "packages" / "bible_parser_core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

SLIDE_DIR = PROJECT_ROOT / "slide_display"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_QR_PATH = Path(".cache") / "liverse" / "operator_qr.png"

CURRENT_SLIDE = {
    "ref": "Иоанна 20:24-29",
    "verse": "Иисус говорит ему: ты поверил, потому что увидел Меня; блаженны невидевшие и уверовавшие.",
    "source": "initial",
}
CLIENTS: list[queue.Queue] = []
OPERATOR_CLIENTS: list[queue.Queue] = []
STATE_LOCK = threading.Lock()
PENDING_CANDIDATE: dict = {}
DECISION_CALLBACK = None
PROCESSING_STATE = {
    "stage": "listening",
    "message": "LiVerse слушает речь",
    "progress": 0,
    "chunk": None,
    "manual_required": False,
}


def event_payload(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def broadcast(payload: dict) -> None:
    with STATE_LOCK:
        clients = list(CLIENTS)
    for client in clients:
        client.put(payload)


def broadcast_operator(payload: dict) -> None:
    with STATE_LOCK:
        clients = list(OPERATOR_CLIENTS)
    for client in clients:
        client.put(payload)


def operator_state() -> dict:
    with STATE_LOCK:
        pending = dict(PENDING_CANDIDATE)
        processing = dict(PROCESSING_STATE)
    return {
        "status": "pending" if pending else "waiting",
        "candidate": pending or None,
        "processing": processing,
    }


def reset_operator_state(decision_callback=None) -> None:
    global DECISION_CALLBACK
    with STATE_LOCK:
        PENDING_CANDIDATE.clear()
        PROCESSING_STATE.update(
            {
                "stage": "listening",
                "message": "LiVerse слушает речь",
                "progress": 0,
                "chunk": None,
                "manual_required": False,
            }
        )
    DECISION_CALLBACK = decision_callback


def update_processing_status(
    stage: str,
    message: str,
    progress: int,
    *,
    chunk: int | None = None,
    manual_required: bool = False,
) -> dict:
    with STATE_LOCK:
        PROCESSING_STATE.update(
            {
                "stage": stage,
                "message": message,
                "progress": max(0, min(100, int(progress))),
                "chunk": chunk,
                "manual_required": manual_required,
            }
        )
    state = operator_state()
    broadcast_operator(state)
    return state["processing"]


def submit_candidate(payload: dict) -> dict:
    candidate = {
        "ref": str(payload.get("ref") or "").strip(),
        "verse": str(payload.get("verse") or "").strip(),
        "source": str(payload.get("source") or "").strip(),
        "asr": str(payload.get("asr") or "").strip(),
        "detected_text": str(payload.get("detected_text") or "").strip(),
        "chunk": payload.get("chunk"),
        "label": str(payload.get("label") or "").strip(),
    }
    with STATE_LOCK:
        PENDING_CANDIDATE.clear()
        PENDING_CANDIDATE.update(candidate)
        PROCESSING_STATE.update(
            {
                "stage": "candidate",
                "message": "Ссылка распознана — ожидает подтверждения",
                "progress": 100,
                "chunk": candidate.get("chunk"),
                "manual_required": False,
            }
        )
    state = operator_state()
    broadcast_operator(state)
    return candidate


def set_current_slide(payload: dict) -> dict:
    slide = {
        "ref": str(payload.get("ref") or "").strip(),
        "verse": str(payload.get("verse") or "").strip(),
        "source": str(payload.get("source") or "live"),
        "asr": str(payload.get("asr") or "").strip(),
        "detected_text": str(payload.get("detected_text") or "").strip(),
    }
    if not slide["ref"]:
        slide["ref"] = slide["detected_text"] or "Найдена ссылка"
    if not slide["verse"]:
        slide["verse"] = slide["asr"] or slide["detected_text"] or "Текст цитаты пока не найден."

    with STATE_LOCK:
        CURRENT_SLIDE.clear()
        CURRENT_SLIDE.update(slide)
    broadcast(slide)
    return slide


def bible_book_names() -> list[str]:
    from bible_parser_core.book_aliases import books_data

    return [book for book, _aliases in books_data]


def bible_structure() -> dict:
    from bible_parser_core.parser import bible_map

    structure = {}
    for book, chapters in bible_map().items():
        structure[book] = {
            str(chapter): sorted(verses)
            for chapter, verses in sorted(chapters.items())
        }
    return structure


def manual_reference_candidate(reference: str) -> tuple[dict | None, str]:
    from bible_parser_core.parser import parse_live_reference

    parsed = parse_live_reference(reference)
    if parsed is None:
        return None, "reference_not_found"
    with STATE_LOCK:
        current = dict(PENDING_CANDIDATE)
    return {
        "ref": parsed.ref,
        "verse": parsed.verse_text,
        "source": "operator:manual",
        "asr": str(current.get("asr") or ""),
        "detected_text": reference.strip(),
        "chunk": current.get("chunk"),
        "label": "manual",
    }, ""


def decide_candidate(action: str) -> tuple[bool, str, dict]:
    if action not in {"approve", "reject"}:
        return False, "unknown_action", {}
    with STATE_LOCK:
        candidate = dict(PENDING_CANDIDATE)
    if not candidate:
        return False, "no_pending_candidate", {}

    ok, reason = True, ""
    callback = DECISION_CALLBACK
    if callback is not None:
        try:
            result = callback(action, candidate)
            if isinstance(result, tuple):
                ok, reason = bool(result[0]), str(result[1] or "")
            elif result is False:
                ok, reason = False, "decision_callback_failed"
        except Exception as exc:
            ok, reason = False, f"decision_callback_error:{exc}"
    if not ok:
        return False, reason, candidate

    with STATE_LOCK:
        PENDING_CANDIDATE.clear()
        PROCESSING_STATE.update(
            {
                "stage": "approved" if action == "approve" else "rejected",
                "message": "Отправлено в Holyrics" if action == "approve" else "Цитата отклонена",
                "progress": 100 if action == "approve" else 0,
                "chunk": candidate.get("chunk"),
                "manual_required": action == "reject",
            }
        )
    broadcast_operator(operator_state())
    return True, reason, candidate


def get_local_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
        except OSError:
            return "127.0.0.1"


def operator_url(port: int) -> str:
    return f"http://{get_local_ip()}:{port}/operator"


def operator_qr_svg(url: str) -> bytes:
    import qrcode
    import qrcode.image.svg

    image = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage)
    stream = io.BytesIO()
    image.save(stream)
    return stream.getvalue()


def save_operator_qr_png(url: str, path: Path = DEFAULT_QR_PATH) -> Path:
    import qrcode

    path.parent.mkdir(parents=True, exist_ok=True)
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=16,
        border=8,
    )
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    image.save(path)
    return path.resolve()


def open_operator_windows(url: str, qr_path: Path, *, open_qr: bool, open_browser: bool) -> None:
    if open_qr:
        viewer = shutil.which("eog") or shutil.which("xdg-open")
        if viewer:
            subprocess.Popen(
                [viewer, str(qr_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            print(f"Откройте QR-код вручную: {qr_path}", flush=True)
    if open_browser:
        opener = shutil.which("xdg-open")
        if opener:
            subprocess.Popen(
                [opener, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            print(f"Откройте пульт на ноутбуке: {url}", flush=True)


def print_operator_qr(url: str) -> None:
    try:
        import qrcode
    except ImportError:
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    print("Сканируйте QR-код камерой телефона:", flush=True)
    qr.print_ascii(invert=True)


class SlideHandler(BaseHTTPRequestHandler):
    server_version = f"LiVerseSlideServer/{__version__}"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:
        if self.path == "/operator-events":
            self.handle_operator_events()
            return
        if self.path == "/api/pending":
            self.send_json(operator_state())
            return
        if self.path == "/api/books":
            self.send_json({"books": bible_book_names()})
            return
        if self.path == "/api/bible-structure":
            self.send_json({"books": bible_book_names(), "structure": bible_structure()})
            return
        if self.path == "/operator-qr.svg":
            self.send_qr()
            return
        if self.path == "/operator":
            self.path = "/operator.html"
        if self.path == "/events":
            self.handle_events()
            return
        if self.path == "/api/current":
            self.send_json(CURRENT_SLIDE)
            return
        self.serve_static()

    def do_POST(self) -> None:
        if self.path in {"/api/approve", "/api/reject"}:
            action = self.path.rsplit("/", 1)[-1]
            ok, reason, candidate = decide_candidate(action)
            self.send_json({"ok": ok, "reason": reason, "candidate": candidate}, status=200 if ok else 409)
            return
        if self.path == "/api/candidate":
            payload = self.read_json()
            if payload is None:
                return
            self.send_json({"ok": True, "candidate": submit_candidate(payload)})
            return
        if self.path == "/api/manual":
            payload = self.read_json()
            if payload is None:
                return
            candidate, reason = manual_reference_candidate(str(payload.get("ref") or ""))
            if candidate is None:
                self.send_json({"ok": False, "reason": reason}, status=422)
                return
            self.send_json({"ok": True, "candidate": submit_candidate(candidate)})
            return
        if self.path != "/api/current":
            self.send_error(404)
            return

        payload = self.read_json()
        if payload is None:
            return

        slide = set_current_slide(payload)
        self.send_json({"ok": True, "slide": slide})

    def read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return None
        if not isinstance(payload, dict):
            self.send_error(400, "Expected JSON object")
            return None
        return payload

    def handle_events(self) -> None:
        client: queue.Queue = queue.Queue()
        with STATE_LOCK:
            CLIENTS.append(client)
            initial = dict(CURRENT_SLIDE)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            self.wfile.write(event_payload(initial))
            self.wfile.flush()
            while True:
                payload = client.get()
                self.wfile.write(event_payload(payload))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with STATE_LOCK:
                if client in CLIENTS:
                    CLIENTS.remove(client)

    def handle_operator_events(self) -> None:
        client: queue.Queue = queue.Queue()
        with STATE_LOCK:
            OPERATOR_CLIENTS.append(client)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(event_payload(operator_state()))
            self.wfile.flush()
            while True:
                payload = client.get()
                self.wfile.write(event_payload(payload))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with STATE_LOCK:
                if client in OPERATOR_CLIENTS:
                    OPERATOR_CLIENTS.remove(client)

    def send_qr(self) -> None:
        try:
            data = operator_qr_svg(operator_url(self.server.server_port))
        except ImportError:
            self.send_error(503, "qrcode package is not installed")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def serve_static(self) -> None:
        requested = "/" if self.path == "/" else unquote(self.path.split("?", 1)[0])
        relative = "index.html" if requested == "/" else requested.lstrip("/")
        path = (SLIDE_DIR / relative).resolve()
        try:
            path.relative_to(SLIDE_DIR.resolve())
        except ValueError:
            self.send_error(403)
            return
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), SlideHandler)
    print(f"Slide display: http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSlide server stopped.", flush=True)
    finally:
        server.server_close()
    return server


def start_server_thread(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    decision_callback=None,
    open_qr: bool = False,
    open_browser: bool = False,
) -> ThreadingHTTPServer:
    reset_operator_state(decision_callback)
    server = ThreadingHTTPServer((host, port), SlideHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Slide display: http://{host}:{port}", flush=True)
    if decision_callback is not None:
        url = operator_url(port)
        desktop_url = f"http://127.0.0.1:{port}/operator"
        qr_path = save_operator_qr_png(url)
        print(f"Пульт подтверждения: {url}", flush=True)
        print(f"Пульт на ноутбуке: {desktop_url}", flush=True)
        print(f"QR PNG: {qr_path}", flush=True)
        print(f"QR-код: {url.rsplit('/operator', 1)[0]}/operator-qr.svg", flush=True)
        print_operator_qr(url)
        open_operator_windows(
            desktop_url,
            qr_path,
            open_qr=open_qr,
            open_browser=open_browser,
        )
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve slide display and accept live Bible reference updates.")
    parser.add_argument("--version", action="version", version=f"LiVerse {__version__}")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    run_server(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
