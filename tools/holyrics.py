#!/usr/bin/env python3
"""Small Holyrics local API helpers for LiVerse."""

from __future__ import annotations

import json
import os
import re
import threading
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
DEFAULT_CROSS_CHAPTER_SLIDE_MAX_CHARS = 760
DEFAULT_CROSS_CHAPTER_SLIDE_MAX_VERSES = 9
DEFAULT_LONG_RANGE_SLIDE_MAX_CHARS = 620
DEFAULT_LONG_RANGE_SLIDE_MAX_VERSES = 7
DEFAULT_LONG_RANGE_MIN_VERSES = 5
MIN_RECOMMENDED_HOLYRICS_VERSION = "2.28.1"
HOLYRICS_JSLIB_DOC_URL = "https://github.com/holyrics/jslib/blob/main/README-en.md"
REQUIRED_HOLYRICS_PERMISSIONS = (
    "GetAPIServerInfo",
    "GetCurrentPresentation",
    "CloseCurrentPresentation",
    "SetBibleSettings",
    "ShowQuickPresentation",
    "ShowText",
    "ShowVerse",
)
SERMON_PLAN_HOLYRICS_PERMISSIONS = (
    "ActionGoToIndex",
)
THEME_HOLYRICS_PERMISSIONS = (
    "GetThemes",
)
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
_TEMPORARY_VERSE_RESTORE_TIMER: threading.Timer | None = None
_TEMPORARY_VERSE_RESTORE_LOCK = threading.Lock()
_VERSE_LINE_RE = re.compile(r"(?m)^(\d+):(\d+)\.\s*(.+)$")


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


def holyrics_show_verse_count(payload: dict) -> int:
    try:
        book = str(payload.get("book") or "").strip()
        chapter = int(payload.get("chapter") or 0)
        start_verse = int(payload.get("start_verse") or 0)
        end_verse = int(payload.get("end_verse") or start_verse)
        end_chapter = payload.get("end_chapter")
        end_chapter = int(end_chapter) if end_chapter is not None else chapter
    except (TypeError, ValueError):
        return 1

    if start_verse <= 0 or end_verse <= 0:
        return 1
    if chapter == end_chapter:
        if end_verse < start_verse:
            return 1
        return max(1, end_verse - start_verse + 1)

    try:
        from bible_parser_core.parser import bible_map
    except Exception:
        return 1

    chapters = bible_map().get(book, {})
    if end_chapter <= chapter or chapter not in chapters or end_chapter not in chapters:
        return 1

    count = 0
    for current_chapter in range(chapter, end_chapter + 1):
        chapter_map = chapters.get(current_chapter, {})
        if not chapter_map:
            return 1
        first = start_verse if current_chapter == chapter else min(chapter_map)
        last = end_verse if current_chapter == end_chapter else max(chapter_map)
        if first > last:
            return 1
        count += len([verse for verse in range(first, last + 1) if verse in chapter_map])
    return max(1, count)


def cross_chapter_range(payload: dict) -> tuple[str, int, int, int, int] | None:
    try:
        book = str(payload.get("book") or "").strip()
        chapter = int(payload.get("chapter") or 0)
        start_verse = int(payload.get("start_verse") or 0)
        end_chapter = int(payload.get("end_chapter") or 0)
        end_verse = int(payload.get("end_verse") or 0)
    except (TypeError, ValueError):
        return None

    if not book or chapter <= 0 or start_verse <= 0 or end_chapter <= chapter or end_verse <= 0:
        return None
    return book, chapter, start_verse, end_chapter, end_verse


def scripture_range(payload: dict, *, min_same_chapter_verses: int = DEFAULT_LONG_RANGE_MIN_VERSES) -> tuple[str, int, int, int, int] | None:
    try:
        book = str(payload.get("book") or "").strip()
        chapter = int(payload.get("chapter") or 0)
        start_verse = int(payload.get("start_verse") or 0)
        end_chapter_value = payload.get("end_chapter")
        end_chapter = int(end_chapter_value) if end_chapter_value is not None else chapter
        end_verse = int(payload.get("end_verse") or start_verse)
    except (TypeError, ValueError):
        return None

    if not book or chapter <= 0 or start_verse <= 0 or end_chapter < chapter or end_verse <= 0:
        return None
    if end_chapter > chapter:
        return book, chapter, start_verse, end_chapter, end_verse
    if end_verse < start_verse:
        return None
    if end_verse - start_verse + 1 >= max(2, min_same_chapter_verses):
        return book, chapter, start_verse, chapter, end_verse
    return None


def scripture_range_verse_lines(
    payload: dict,
    *,
    min_same_chapter_verses: int = DEFAULT_LONG_RANGE_MIN_VERSES,
) -> list[str]:
    selected_range = scripture_range(payload, min_same_chapter_verses=min_same_chapter_verses)
    if not selected_range:
        return []
    book, chapter, start_verse, end_chapter, end_verse = selected_range

    try:
        from bible_parser_core.parser import bible_map
    except Exception:
        return []

    chapters = bible_map().get(book, {})
    if chapter not in chapters or end_chapter not in chapters:
        return []

    lines: list[str] = []
    for current_chapter in range(chapter, end_chapter + 1):
        chapter_map = chapters.get(current_chapter, {})
        if not chapter_map:
            return []
        first_verse = start_verse if current_chapter == chapter else min(chapter_map)
        last_verse = end_verse if current_chapter == end_chapter else max(chapter_map)
        if first_verse > last_verse:
            return []
        for verse in range(first_verse, last_verse + 1):
            text = chapter_map.get(verse)
            if text:
                lines.append(f"{current_chapter}:{verse}. {text}")
    return lines


def cross_chapter_verse_lines(payload: dict) -> list[str]:
    if not cross_chapter_range(payload):
        return []
    return scripture_range_verse_lines(payload, min_same_chapter_verses=10**9)


def cross_chapter_quick_presentation_slides(
    payload: dict,
    *,
    max_chars: int = DEFAULT_CROSS_CHAPTER_SLIDE_MAX_CHARS,
    max_verses: int = DEFAULT_CROSS_CHAPTER_SLIDE_MAX_VERSES,
) -> list[dict[str, str]]:
    ref = str(payload.get("ref") or "").strip()
    existing_lines = payload.get("_scripture_range_lines")
    lines = (
        [str(line) for line in existing_lines if str(line).strip()]
        if isinstance(existing_lines, list)
        else cross_chapter_verse_lines(payload)
    )
    if not ref or not lines:
        return []

    max_chars = max(240, max_chars)
    max_verses = max(1, max_verses)

    def slide_text(slide_lines: list[str], *, first: bool) -> str:
        body = "\n".join(slide_lines).strip()
        if first:
            return f"{ref}\n\n{body}".strip()
        return body

    total_chars = len(ref) + 2 + sum(len(line) + 1 for line in lines)
    slide_count = max(
        1,
        (total_chars + max_chars - 1) // max_chars,
        (len(lines) + max_verses - 1) // max_verses,
    )
    slide_count = min(slide_count, len(lines))
    target_chars = max(1, (total_chars + slide_count - 1) // slide_count)

    line_lengths = [len(line) for line in lines]
    prefix_lengths = [0]
    for length in line_lengths:
        prefix_lengths.append(prefix_lengths[-1] + length)

    def chunk_length(start: int, end: int, *, first: bool) -> int:
        line_count = end - start
        if line_count <= 0:
            return 0
        length = prefix_lengths[end] - prefix_lengths[start] + max(0, line_count - 1)
        if first:
            length += len(ref) + 2
        return length

    dp: list[list[tuple[int, int] | None]] = [[None] * (len(lines) + 1) for _ in range(slide_count + 1)]
    parent: list[list[int | None]] = [[None] * (len(lines) + 1) for _ in range(slide_count + 1)]
    dp[0][0] = (0, 0)
    for slide_index in range(1, slide_count + 1):
        for end in range(1, len(lines) + 1):
            best: tuple[int, int] | None = None
            best_start: int | None = None
            min_start = max(slide_index - 1, end - max_verses)
            for start in range(min_start, end):
                previous = dp[slide_index - 1][start]
                if previous is None:
                    continue
                length = chunk_length(start, end, first=slide_index == 1)
                cost = (
                    max(previous[0], length),
                    previous[1] + (length - target_chars) * (length - target_chars),
                )
                if best is None or cost < best:
                    best = cost
                    best_start = start
            dp[slide_index][end] = best
            parent[slide_index][end] = best_start

    if dp[slide_count][len(lines)] is None:
        return [{"text": slide_text(lines, first=True)}]

    ranges: list[tuple[int, int]] = []
    end = len(lines)
    for slide_index in range(slide_count, 0, -1):
        start = parent[slide_index][end]
        if start is None:
            return [{"text": slide_text(lines, first=True)}]
        ranges.append((start, end))
        end = start
    ranges.reverse()

    return [
        {"text": slide_text(lines[start:end], first=index == 0)}
        for index, (start, end) in enumerate(ranges)
    ]


def scripture_range_quick_presentation_slides(
    payload: dict,
    *,
    max_chars: int = DEFAULT_LONG_RANGE_SLIDE_MAX_CHARS,
    max_verses: int = DEFAULT_LONG_RANGE_SLIDE_MAX_VERSES,
    min_same_chapter_verses: int = DEFAULT_LONG_RANGE_MIN_VERSES,
) -> list[dict[str, str]]:
    lines = scripture_range_verse_lines(payload, min_same_chapter_verses=min_same_chapter_verses)
    if not lines:
        return []
    return cross_chapter_quick_presentation_slides(
        {**payload, "_scripture_range_lines": lines},
        max_chars=max_chars,
        max_verses=max_verses,
    )


def slide_payload_to_holyrics_text(payload: dict) -> str:
    ref = str(payload.get("ref") or "").strip()
    verse = str(payload.get("verse") or "").strip()
    if ref and verse:
        return f"{ref}\n\n{verse}"
    return verse or ref


def slide_payload_to_holyrics_body(args: Any, payload: dict) -> dict:
    slide = {"text": slide_payload_to_holyrics_text(payload)}
    sermon_plan_theme_id = str(getattr(args, "_holyrics_sermon_plan_theme_id", "") or "").strip()
    theme_name = str(getattr(args, "holyrics_theme", "") or "").strip()
    if sermon_plan_theme_id:
        slide["theme"] = {"id": sermon_plan_theme_id}
    elif theme_name:
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


def holyrics_quick_minutes(args: Any) -> float:
    try:
        return max(0.0, float(getattr(args, "holyrics_quick_minutes", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def get_holyrics_current_presentation(
    args: Any,
    base_url: str,
    *,
    include_slides: bool = False,
) -> dict[str, Any] | None:
    ok, reason, body = post_holyrics_api(
        args,
        base_url,
        "GetCurrentPresentation",
        {"include_slides": include_slides},
    )
    if not ok:
        holyrics_log(f"GetCurrentPresentation response={body or reason or 'failed'}")
        return None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    data = parsed.get("data") if isinstance(parsed, dict) else None
    return data if isinstance(data, dict) else None


def restore_holyrics_presentation(args: Any, base_url: str, previous: dict[str, Any] | None) -> None:
    presentation_type = str((previous or {}).get("type") or "").strip()
    try:
        slide_number = int((previous or {}).get("slide_number") or 1)
    except (TypeError, ValueError):
        slide_number = 1
    initial_index = max(0, slide_number - 1)
    if presentation_type == "text":
        text_id = str((previous or {}).get("text_id") or (previous or {}).get("id") or "").strip()
        if not text_id:
            return
        ok, reason, body = post_holyrics_api(
            args,
            base_url,
            "ShowText",
            {"id": text_id, "initial_index": initial_index},
        )
        holyrics_log(f"ShowText restore response={body or reason or 'ok'}")
        return

    close_ok, close_reason, close_body = post_holyrics_api(args, base_url, "CloseCurrentPresentation", {})
    holyrics_log(f"CloseCurrentPresentation response={close_body or close_reason or 'ok'}")
    if not close_ok:
        holyrics_log(f"не удалось закрыть временный стих: {close_reason}")
        return

    if not previous:
        return

    holyrics_log(f"восстановление презентации типа {presentation_type or '(empty)'} пока не поддержано")


def restore_holyrics_presentation_later(args: Any, base_url: str, previous: dict[str, Any] | None, minutes: float) -> None:
    if minutes <= 0:
        return

    delay_seconds = max(1.0, minutes * 60.0)

    def restore() -> None:
        restore_holyrics_presentation(args, base_url, previous)

    global _TEMPORARY_VERSE_RESTORE_TIMER
    with _TEMPORARY_VERSE_RESTORE_LOCK:
        if _TEMPORARY_VERSE_RESTORE_TIMER is not None:
            _TEMPORARY_VERSE_RESTORE_TIMER.cancel()
        timer = threading.Timer(delay_seconds, restore)
        timer.daemon = True
        _TEMPORARY_VERSE_RESTORE_TIMER = timer
        timer.start()


def cancel_holyrics_restore_timer() -> None:
    global _TEMPORARY_VERSE_RESTORE_TIMER
    with _TEMPORARY_VERSE_RESTORE_LOCK:
        if _TEMPORARY_VERSE_RESTORE_TIMER is not None:
            _TEMPORARY_VERSE_RESTORE_TIMER.cancel()
            _TEMPORARY_VERSE_RESTORE_TIMER = None


def show_holyrics_text_slide(
    args: Any,
    base_url: str,
    presentation: dict[str, Any],
    slide_index: int,
) -> tuple[bool, str]:
    clear_scripture_range_reading(args)
    text_id = str(presentation.get("text_id") or presentation.get("id") or "").strip()
    if not text_id:
        return False, "sermon_plan_text_id_missing"

    current = get_holyrics_current_presentation(args, base_url)
    current_type = str((current or {}).get("type") or "").strip()
    current_text_id = str((current or {}).get("text_id") or (current or {}).get("id") or "").strip()
    current_index = max(0, int((current or {}).get("slide_number") or 1) - 1)
    if current_type == "text" and current_text_id == text_id:
        if current_index == slide_index:
            return True, "sermon_plan_already_current"
        ok, reason, _body = post_holyrics_api(
            args,
            base_url,
            "ActionGoToIndex",
            {"index": slide_index},
        )
        if ok:
            presentation["slide_number"] = slide_index + 1
        return ok, reason or "sermon_plan_action_go_to_index"

    cancel_holyrics_restore_timer()
    ok, reason, _body = post_holyrics_api(
        args,
        base_url,
        "ShowText",
        {"id": text_id, "initial_index": slide_index},
    )
    if ok:
        presentation["slide_number"] = slide_index + 1
    return ok, reason or "sermon_plan_show_text"


def get_holyrics_api_server_info(args: Any, base_url: str) -> tuple[bool, str, dict[str, Any] | None]:
    ok, reason, body = post_holyrics_api(args, base_url, "GetAPIServerInfo", {})
    if not ok:
        return False, reason, None
    if not body:
        return True, "", {}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return True, "non_json_response", {"raw": body}
    return True, "", parsed


def get_holyrics_token_info(args: Any, base_url: str) -> tuple[bool, str, dict[str, Any] | None]:
    ok, reason, body = post_holyrics_api(args, base_url, "GetTokenInfo", {})
    if not ok:
        return False, reason, None
    if not body:
        return True, "", {}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return True, "non_json_response", {"raw": body}
    return True, "", parsed


def extract_holyrics_version(info: dict[str, Any] | None) -> str:
    if not isinstance(info, dict):
        return ""

    stack: list[Any] = [info]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key).lower() in {"version", "app_version", "program_version", "holyrics_version"}:
                    text = str(nested or "").strip()
                    if text:
                        return text
                if isinstance(nested, (dict, list)):
                    stack.append(nested)
        elif isinstance(value, list):
            stack.extend(item for item in value if isinstance(item, (dict, list)))
    return ""


def extract_holyrics_permissions(info: dict[str, Any] | None) -> set[str]:
    if not isinstance(info, dict):
        return set()

    permissions = info.get("permissions")
    if isinstance(permissions, str):
        return {item.strip() for item in permissions.split(",") if item.strip()}
    if isinstance(permissions, list):
        return {str(item).strip() for item in permissions if str(item).strip()}

    data = info.get("data")
    if isinstance(data, dict) and data is not info:
        return extract_holyrics_permissions(data)
    return set()


def required_holyrics_permissions(args: Any) -> tuple[str, ...]:
    permissions = list(REQUIRED_HOLYRICS_PERMISSIONS)
    if bool(getattr(args, "sermon_plan", False)):
        permissions.extend(SERMON_PLAN_HOLYRICS_PERMISSIONS)
    if str(getattr(args, "holyrics_theme", "") or "").strip():
        permissions.extend(THEME_HOLYRICS_PERMISSIONS)
    return tuple(dict.fromkeys(permissions))


def check_holyrics_api_server(args: Any) -> dict[str, Any]:
    reasons: list[str] = []
    auto_target = str(getattr(args, "holyrics_url", "auto")).strip().lower() == "auto"
    for url in holyrics_candidate_urls(getattr(args, "holyrics_url", "auto")):
        ok, reason, api_server_info = get_holyrics_api_server_info(args, url)
        if ok:
            token_ok, token_reason, token_info = get_holyrics_token_info(args, url)
            permissions = extract_holyrics_permissions(token_info)
            missing_permissions = [
                permission for permission in required_holyrics_permissions(args) if permission not in permissions
            ] if permissions else []
            if auto_target:
                setattr(args, "holyrics_url", url)
            return {
                "ok": True,
                "url": url,
                "version": extract_holyrics_version(token_info) or extract_holyrics_version(api_server_info),
                "api_server_info": api_server_info,
                "token_info": token_info,
                "token_info_ok": token_ok,
                "token_info_reason": token_reason,
                "permissions": sorted(permissions),
                "missing_permissions": missing_permissions,
                "reason": reason,
            }
        reasons.append(f"{url}={reason}")
    return {
        "ok": False,
        "url": "",
        "version": "",
        "api_server_info": None,
        "token_info": None,
        "token_info_ok": False,
        "token_info_reason": "",
        "permissions": [],
        "missing_permissions": [],
        "reason": ";".join(reasons) or "holyrics_unavailable",
    }


def extract_holyrics_data_list(body: str) -> list[dict[str, Any]]:
    if not body:
        return []
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return []
    data = parsed.get("data") if isinstance(parsed, dict) else None
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def get_holyrics_theme_options(args: Any) -> dict[str, Any]:
    reasons: list[str] = []
    auto_target = str(getattr(args, "holyrics_url", "auto")).strip().lower() == "auto"
    for url in holyrics_candidate_urls(getattr(args, "holyrics_url", "auto")):
        api_ok, api_reason, _api_info = get_holyrics_api_server_info(args, url)
        if not api_ok:
            reasons.append(f"{url}={api_reason}")
            continue

        token_ok, token_reason, token_info = get_holyrics_token_info(args, url)
        if not token_ok:
            return {
                "ok": False,
                "url": url,
                "reason": token_reason,
                "permission_missing": False,
                "themes": [],
            }

        permissions = extract_holyrics_permissions(token_info)
        if "GetThemes" not in permissions:
            return {
                "ok": False,
                "url": url,
                "reason": "permission_missing:GetThemes",
                "permission_missing": True,
                "themes": [],
            }

        themes_ok, themes_reason, body = post_holyrics_api(args, url, "GetThemes", {})
        if not themes_ok:
            return {
                "ok": False,
                "url": url,
                "reason": themes_reason,
                "permission_missing": themes_reason == "holyrics_http_401",
                "themes": [],
            }

        themes = [
            {
                "id": str(theme.get("id") or "").strip(),
                "name": str(theme.get("name") or "").strip(),
            }
            for theme in extract_holyrics_data_list(body)
        ]
        themes = [theme for theme in themes if theme["id"] and theme["name"]]
        if auto_target:
            setattr(args, "holyrics_url", url)
        return {"ok": True, "url": url, "reason": "", "permission_missing": False, "themes": themes}

    return {
        "ok": False,
        "url": "",
        "reason": ";".join(reasons) or "holyrics_unavailable",
        "permission_missing": False,
        "themes": [],
    }


def resolve_holyrics_theme_id(args: Any, base_url: str, theme_name: str) -> tuple[str | None, str]:
    requested = theme_name.strip()
    if not requested:
        return None, ""

    ok, reason, body = post_holyrics_api(args, base_url, "GetThemes", {})
    if not ok:
        holyrics_log(f"GetThemes response={body or reason or 'error'}")
        if reason == "holyrics_http_401":
            return None, "holyrics_theme_permission_missing:GetThemes"
        return None, reason

    themes = extract_holyrics_data_list(body)
    requested_key = requested.casefold()
    for theme in themes:
        name = str(theme.get("name") or "").strip()
        if name.casefold() == requested_key:
            theme_id = str(theme.get("id") or "").strip()
            if theme_id:
                return theme_id, ""

    names = sorted(str(theme.get("name") or "").strip() for theme in themes if str(theme.get("name") or "").strip())
    if names:
        preview = ", ".join(names[:12])
        suffix = "" if len(names) <= 12 else ", ..."
        return None, f"holyrics_theme_not_found:{requested};available:{preview}{suffix}"
    return None, f"holyrics_theme_not_found:{requested}"


def current_bible_theme_filter(args: Any, base_url: str) -> dict[str, str]:
    sermon_plan_theme_id = str(getattr(args, "_holyrics_sermon_plan_theme_id", "") or "").strip()
    if sermon_plan_theme_id:
        return {"id": sermon_plan_theme_id}

    selected_theme_id = str(getattr(args, "_holyrics_theme_id", "") or "").strip()
    if selected_theme_id:
        return {"id": selected_theme_id}

    theme_name = str(getattr(args, "holyrics_theme", "") or "").strip()
    if theme_name:
        theme_id, reason = resolve_holyrics_theme_id(args, base_url, theme_name)
        if theme_id:
            setattr(args, "_holyrics_theme_id", theme_id)
            return {"id": theme_id}
        holyrics_log(f"не удалось выбрать тему для межглавного диапазона: {reason}")
        return {}

    ok, reason, body = post_holyrics_api(args, base_url, "GetBibleSettings", {})
    if not ok:
        holyrics_log(f"GetBibleSettings response={body or reason or 'failed'}")
        return {}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return {}
    data = parsed.get("data") if isinstance(parsed, dict) else None
    theme = data.get("theme") if isinstance(data, dict) else None
    public_theme_id = str((theme or {}).get("public") or "").strip() if isinstance(theme, dict) else ""
    if public_theme_id:
        return {"id": public_theme_id}
    return {}


def cross_chapter_quick_presentation_body(args: Any, base_url: str, payload: dict) -> dict | None:
    slides = cross_chapter_quick_presentation_slides(payload)
    if not slides:
        return None
    body: dict[str, Any] = {"slides": slides}
    theme = current_bible_theme_filter(args, base_url)
    if theme:
        body["theme"] = theme
    return body


def scripture_range_quick_presentation_body(args: Any, base_url: str, payload: dict) -> dict | None:
    slides = scripture_range_quick_presentation_slides(payload)
    if not slides:
        return None
    body: dict[str, Any] = {"slides": slides}
    theme = current_bible_theme_filter(args, base_url)
    if theme:
        body["theme"] = theme
    return body


def scripture_range_reading_state(payload: dict, slides: list[dict]) -> dict | None:
    """Describe the last Bible verse on each generated Holyrics slide."""
    book = str(payload.get("book") or "").strip()
    book_id = HOLYRICS_BOOK_INDEX.get(book)
    ref = str(payload.get("ref") or "").strip()
    if book_id is None or not ref or not slides:
        return None
    targets: list[dict[str, Any]] = []
    for slide_index, slide in enumerate(slides):
        matches = list(_VERSE_LINE_RE.finditer(str(slide.get("text") or "")))
        if not matches:
            return None
        last = matches[-1]
        targets.append(
            {
                "slide_index": slide_index,
                "chapter": int(last.group(1)),
                "verse": int(last.group(2)),
                "text": last.group(3).strip(),
            }
        )
    return {
        "ref": ref,
        "book": book,
        "book_id": book_id,
        "current_index": 0,
        "targets": targets,
    }


def clear_scripture_range_reading(args: Any) -> None:
    setattr(args, "_holyrics_scripture_range_reading", None)


def scripture_range_reading_active(args: Any) -> bool:
    state = getattr(args, "_holyrics_scripture_range_reading", None)
    return isinstance(state, dict) and bool(state.get("targets"))


def handle_scripture_range_reading_match(args: Any, candidate: Any) -> dict:
    """Advance a long-passage slide when its final verse was just read."""
    state = getattr(args, "_holyrics_scripture_range_reading", None)
    if not isinstance(state, dict):
        return {"active": False, "matched_boundary": False, "reason": "inactive"}
    targets = list(state.get("targets") or [])
    current_index = int(state.get("current_index") or 0)
    if current_index < 0 or current_index >= len(targets):
        clear_scripture_range_reading(args)
        return {"active": False, "matched_boundary": False, "reason": "invalid_state"}
    target = targets[current_index]
    try:
        candidate_book_id = int(getattr(candidate, "book_id", 0) or 0)
        candidate_chapter = int(getattr(candidate, "chapter", 0) or 0)
        candidate_start = int(getattr(candidate, "start_verse", 0) or 0)
        candidate_end = int(getattr(candidate, "end_verse", candidate_start) or candidate_start)
    except (TypeError, ValueError):
        return {"active": True, "matched_boundary": False, "reason": "invalid_candidate"}
    matched_boundary = (
        candidate_book_id == int(state.get("book_id") or 0)
        and candidate_chapter == int(target["chapter"])
        and candidate_start <= int(target["verse"]) <= candidate_end
    )
    if not matched_boundary:
        return {
            "active": True,
            "matched_boundary": False,
            "reason": "inside_long_passage",
            "current_index": current_index,
            "target": target,
        }
    next_index = current_index + 1
    if next_index >= len(targets):
        clear_scripture_range_reading(args)
        presentation = getattr(args, "_holyrics_sermon_plan_presentation", None)
        restored = False
        restore_reason = "sermon_plan_not_loaded"
        if isinstance(presentation, dict):
            try:
                return_index = max(0, int(presentation.get("current_index") or 0))
            except (TypeError, ValueError):
                return_index = 0
            restored, restore_reason = show_holyrics_text_slide(
                args,
                str(getattr(args, "holyrics_url", "")).rstrip("/"),
                presentation,
                return_index,
            )
        return {
            "active": False,
            "matched_boundary": True,
            "completed": True,
            "restored_sermon_plan": restored,
            "reason": restore_reason if isinstance(presentation, dict) else "long_passage_completed",
            "current_index": current_index,
            "target": target,
        }
    base_url = str(getattr(args, "holyrics_url", "")).rstrip("/")
    ok, reason, _body = post_holyrics_api(
        args,
        base_url,
        "ActionGoToIndex",
        {"index": next_index},
    )
    if ok:
        state["current_index"] = next_index
    return {
        "active": True,
        "matched_boundary": True,
        "advanced": ok,
        "reason": reason or ("long_passage_slide_advanced" if ok else "long_passage_advance_failed"),
        "current_index": current_index,
        "next_index": next_index,
        "target": target,
    }


def post_holyrics_url(args: Any, base_url: str, payload: dict) -> tuple[bool, str]:
    clear_scripture_range_reading(args)
    if str(payload.get("slide_type") or "").strip() == "reference_list":
        text = slide_payload_to_holyrics_text(payload)
        if not text:
            return False, "holyrics_reference_list_empty"
        show_ok, show_reason, show_body = post_holyrics_api(
            args,
            base_url,
            "ShowQuickPresentation",
            slide_payload_to_holyrics_body(args, payload),
        )
        holyrics_log(f"ShowQuickPresentation response={show_body or show_reason or 'ok'}")
        if not show_ok:
            return False, show_reason
        clear_scripture_range_reading(args)
        return True, "show_quick_presentation:reference_list"

    selected_scripture_range = scripture_range(payload)
    if selected_scripture_range:
        quick_body = scripture_range_quick_presentation_body(args, base_url, payload)
        if not quick_body:
            return False, "holyrics_scripture_range_empty"
        show_ok, show_reason, show_body = post_holyrics_api(
            args,
            base_url,
            "ShowQuickPresentation",
            quick_body,
        )
        range_kind = "cross_chapter" if selected_scripture_range[3] > selected_scripture_range[1] else "long_range"
        holyrics_log(f"ShowQuickPresentation {range_kind} response={show_body or show_reason or 'ok'}")
        if not show_ok:
            return False, show_reason
        state = scripture_range_reading_state(payload, list(quick_body["slides"]))
        setattr(args, "_holyrics_scripture_range_reading", state)
        return True, f"show_quick_presentation:{range_kind};slides:{len(quick_body['slides'])};manual_advance"

    sermon_plan_presentation = getattr(args, "_holyrics_sermon_plan_presentation", None)
    if isinstance(sermon_plan_presentation, dict):
        quick_body = slide_payload_to_holyrics_body(args, payload)
        if not str((quick_body.get("slides") or [{}])[0].get("text") or "").strip():
            return False, "holyrics_quick_presentation_empty"
        cancel_holyrics_restore_timer()
        show_ok, show_reason, show_body = post_holyrics_api(
            args,
            base_url,
            "ShowQuickPresentation",
            quick_body,
        )
        holyrics_log(f"ShowQuickPresentation sermon verse response={show_body or show_reason or 'ok'}")
        if not show_ok:
            return False, show_reason
        clear_scripture_range_reading(args)
        quick_minutes = holyrics_quick_minutes(args)
        if quick_minutes > 0:
            restore_holyrics_presentation_later(args, base_url, sermon_plan_presentation, quick_minutes)
        return True, f"show_quick_presentation:sermon_verse;temporary_verse:{quick_minutes:g}min"

    verse_id, reason = holyrics_verse_id(payload)
    ref = str(payload.get("ref") or "").strip()
    if not verse_id:
        return False, reason

    holyrics_log(f"recognized_ref={ref or '(empty)'}")
    holyrics_log(f"verse_id={verse_id}")
    show_x_verses = holyrics_show_verse_count(payload)
    holyrics_log(f"show_x_verses={show_x_verses}")

    settings_payload: dict[str, Any] = {"show_x_verses": show_x_verses}
    quick_minutes = holyrics_quick_minutes(args)
    previous_presentation = None
    if quick_minutes > 0:
        previous_presentation = get_holyrics_current_presentation(args, base_url)
        previous_type = str((previous_presentation or {}).get("type") or "").strip()
        previous_name = str((previous_presentation or {}).get("name") or "").strip()
        if previous_presentation:
            holyrics_log(f"current_presentation={previous_type}:{previous_name}")

    theme_name = str(getattr(args, "holyrics_theme", "") or "").strip()
    if theme_name:
        theme_id = str(getattr(args, "_holyrics_theme_id", "") or "").strip()
        if not theme_id:
            theme_id, theme_reason = resolve_holyrics_theme_id(args, base_url, theme_name)
            if not theme_id:
                if theme_reason == "holyrics_theme_permission_missing:GetThemes":
                    holyrics_log(
                        "не удалось выбрать тему: нет разрешения GetThemes; "
                        "использую тему Bible module по умолчанию"
                    )
                else:
                    return False, theme_reason
        if theme_id:
            setattr(args, "_holyrics_theme_id", theme_id)
            settings_payload["theme"] = {"public": theme_id}

    settings_ok, settings_reason, settings_body = post_holyrics_api(
        args,
        base_url,
        "SetBibleSettings",
        settings_payload,
    )
    holyrics_log(f"SetBibleSettings response={settings_body or settings_reason or 'ok'}")
    if not settings_ok:
        return False, settings_reason

    show_payload = {"id": verse_id}

    show_ok, show_reason, show_body = post_holyrics_api(
        args,
        base_url,
        "ShowVerse",
        show_payload,
    )
    holyrics_log(f"ShowVerse response={show_body or show_reason or 'ok'}")
    if not show_ok:
        return False, show_reason
    clear_scripture_range_reading(args)
    close_suffix = ""
    if quick_minutes > 0:
        restore_holyrics_presentation_later(args, base_url, previous_presentation, quick_minutes)
        close_suffix = f";temporary_verse:{quick_minutes:g}min"
    return True, f"verse_id:{verse_id};show_x_verses:{show_x_verses}{close_suffix}"


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
