#!/usr/bin/env python3
"""Small Holyrics local API helpers for LiVerse."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode


BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_HOST = "http://localhost"
DEFAULT_PORT = 8091
DEFAULT_TIMEOUT = 5.0
DEFAULT_HOLYRICS_ACTION = "ShowQuickPresentation"
HOLYRICS_BOOKS = (
    "Бытие",
    "Исход",
    "Левит",
    "Числа",
    "Второзаконие",
    "Иисус Навин",
    "Судьи",
    "Руфь",
    "1 Царств",
    "2 Царств",
    "3 Царств",
    "4 Царств",
    "1 Паралипоменон",
    "2 Паралипоменон",
    "Ездра",
    "Неемия",
    "Есфирь",
    "Иов",
    "Псалтирь",
    "Притчи",
    "Екклесиаст",
    "Песня Песней",
    "Исаия",
    "Иеремия",
    "Плач Иеремии",
    "Иезекииль",
    "Даниил",
    "Осия",
    "Иоиль",
    "Амос",
    "Авдий",
    "Иона",
    "Михей",
    "Наум",
    "Аввакум",
    "Софония",
    "Аггей",
    "Захария",
    "Малахия",
    "Матфей",
    "Марк",
    "Лука",
    "Иоанн",
    "Деяния",
    "Римлянам",
    "1 Коринфянам",
    "2 Коринфянам",
    "Галатам",
    "Ефесянам",
    "Филиппийцам",
    "Колоссянам",
    "1 Фессалоникийцам",
    "2 Фессалоникийцам",
    "1 Тимофею",
    "2 Тимофею",
    "Титу",
    "Филимону",
    "Евреям",
    "Иаков",
    "1 Петра",
    "2 Петра",
    "1 Иоанна",
    "2 Иоанна",
    "3 Иоанна",
    "Иуда",
    "Откровение",
)
HOLYRICS_BOOK_INDEX = {book: index for index, book in enumerate(HOLYRICS_BOOKS, start=1)}


def parse_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value


def env_file_paths() -> list[Path]:
    explicit_path = os.environ.get("LIVE_VERSE_VOSK_ENV")
    paths = [
        Path(explicit_path).expanduser() if explicit_path else None,
        Path.cwd() / ".env",
        DEFAULT_ENV_PATH,
    ]
    result: list[Path] = []
    for path in paths:
        if path is not None and path not in result:
            result.append(path)
    return result


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = parse_env_value(value)
    return values


def env_setting(name: str, default: str = "") -> str:
    file_env: dict[str, str] = {}
    for path in env_file_paths():
        file_env.update(load_env_file(path))
    return os.environ.get(name) or file_env.get(name) or default


def normalize_holyrics_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value or value.lower() == "auto":
        return value or "auto"
    if "://" not in value:
        host, separator, port = value.partition(":")
        if separator:
            return f"http://{host}:{port}"
        return f"http://{host}:{DEFAULT_PORT}"
    return value


def default_holyrics_url() -> str:
    explicit_url = env_setting("HOLYRICS_URL")
    if explicit_url:
        return normalize_holyrics_url(explicit_url)

    host = env_setting("HOLYRICS_HOST")
    port = env_setting("HOLYRICS_PORT") or env_setting("HOLYRICS_API_PORT")
    if host or port:
        base = normalize_holyrics_url(host or DEFAULT_HOST)
        if port and ":" not in base.rsplit("/", 1)[-1]:
            return f"{base}:{port}"
        return base

    return f"{DEFAULT_HOST}:{DEFAULT_PORT}"


def describe_holyrics_target(args: Any) -> str:
    if str(getattr(args, "holyrics_url", "")).strip().lower() != "auto":
        return f"{str(getattr(args, 'holyrics_url', '')).rstrip('/')}/api/ShowVerse"
    return "auto: " + ", ".join(
        f"{url}/api/ShowVerse"
        for url in holyrics_candidate_urls(getattr(args, "holyrics_url", "auto"))
    )


def holyrics_candidate_urls(holyrics_url: str) -> list[str]:
    value = normalize_holyrics_url(str(holyrics_url or ""))
    if value and value.lower() != "auto":
        return [value.rstrip("/")]
    return [f"{DEFAULT_HOST}:{DEFAULT_PORT}", f"http://127.0.0.1:{DEFAULT_PORT}"]


def holyrics_log(message: str) -> None:
    print(f"Holyrics: {message}", flush=True)


def holyrics_verse_id(payload: dict) -> tuple[str | None, str]:
    book = str(payload.get("book") or "").strip()
    try:
        chapter = int(payload.get("chapter") or 0)
        verse = int(payload.get("start_verse") or 0)
    except (TypeError, ValueError):
        return None, "invalid_chapter_or_verse"

    book_number = HOLYRICS_BOOK_INDEX.get(book)
    if book_number is None:
        return None, f"unknown_book:{book or 'empty'}"
    if chapter <= 0 or verse <= 0:
        return None, "invalid_chapter_or_verse"
    return f"{book_number:02d}{chapter:03d}{verse:03d}", ""


def slide_payload_to_holyrics_text(payload: dict) -> str:
    ref = str(payload.get("ref") or "").strip()
    verse = str(payload.get("verse") or "").strip()
    if ref and verse:
        return f"{ref}\n\n{verse}"
    return verse or ref


def slide_payload_to_holyrics_body(args: Any, payload: dict) -> dict:
    slide = {"text": slide_payload_to_holyrics_text(payload)}
    theme_name = getattr(args, "holyrics_theme", "")
    if theme_name:
        slide["theme"] = {"name": theme_name}
    return {"slides": [slide]}


def parse_holyrics_response(body: str) -> tuple[bool, str]:
    if not body:
        return True, ""
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return True, ""

    if parsed.get("status") == "ok":
        nested = parsed.get("response")
        if isinstance(nested, dict) and nested.get("status") == "error":
            return False, f"holyrics_error:{nested.get('error') or nested}"
        return True, ""

    api_map = parsed.get("map")
    if isinstance(api_map, dict):
        if str(api_map.get("key_ok")).lower() == "false":
            key_error = api_map.get("key_error") or "invalid"
            if key_error == "not_found":
                return False, "holyrics_token_not_found"
            return False, f"holyrics_token_error:{key_error}"
        if str(api_map.get("key_ok")).lower() == "true":
            return True, ""

    error = parsed.get("error") or parsed
    return False, f"holyrics_error:{error}"


def post_holyrics_api(args: Any, base_url: str, endpoint: str, body: dict) -> tuple[bool, str, str]:
    base_url = str(base_url).rstrip("/")
    query = urlencode({"token": getattr(args, "holyrics_token", "")})
    url = f"{base_url}/api/{endpoint}?{query}"

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=float(getattr(args, "holyrics_timeout", DEFAULT_TIMEOUT))) as response:
            body = response.read().decode("utf-8", errors="replace").strip()
            if not (200 <= response.status < 300):
                return False, f"holyrics_http_{response.status}", body
            ok, reason = parse_holyrics_response(body)
            return ok, reason, body
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace").strip()
        return False, f"holyrics_http_{exc.code}", body
    except URLError as exc:
        return False, f"holyrics_unavailable:{exc.reason}", ""


def post_holyrics_url(args: Any, base_url: str, payload: dict) -> tuple[bool, str]:
    verse_id, reason = holyrics_verse_id(payload)
    ref = str(payload.get("ref") or "").strip()
    if not verse_id:
        return False, reason

    holyrics_log(f"recognized_ref={ref or '(empty)'}")
    holyrics_log(f"verse_id={verse_id}")

    settings_ok, settings_reason, settings_body = post_holyrics_api(
        args,
        base_url,
        "SetBibleSettings",
        {"show_x_verses": 1},
    )
    holyrics_log(f"SetBibleSettings response={settings_body or settings_reason or 'ok'}")
    if not settings_ok:
        return False, settings_reason

    show_ok, show_reason, show_body = post_holyrics_api(
        args,
        base_url,
        "ShowVerse",
        {"id": verse_id},
    )
    holyrics_log(f"ShowVerse response={show_body or show_reason or 'ok'}")
    if not show_ok:
        return False, show_reason
    return True, f"verse_id:{verse_id}"


def post_holyrics_update(args: Any, payload: dict) -> tuple[bool, str]:
    if not getattr(args, "holyrics_token", ""):
        return False, "holyrics_token_missing"

    auto_target = str(getattr(args, "holyrics_url", "auto")).strip().lower() == "auto"
    reasons: list[str] = []
    for url in holyrics_candidate_urls(getattr(args, "holyrics_url", "auto")):
        ok, reason = post_holyrics_url(args, url, payload)
        if ok:
            if auto_target:
                setattr(args, "holyrics_url", url)
            return True, reason
        if not auto_target and (reason.startswith("holyrics_token_") or reason.startswith("holyrics_error:")):
            return False, reason
        reasons.append(f"{url}={reason}")
    return False, ";".join(reasons) or "holyrics_unavailable"


def live_parsed_ref_to_slide_payload_with_source_text(parsed, source: str, source_text: str) -> dict:
    return {
        "ref": parsed.ref,
        "verse": parsed.verse_text,
        "book": parsed.book,
        "chapter": parsed.chapter,
        "start_verse": parsed.start_verse,
        "end_verse": parsed.end_verse,
        "end_chapter": parsed.end_chapter,
        "source": source,
        "asr": source_text,
        "detected_text": source_text,
    }
