"""Updater for the packaged LiVerse Windows installer."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


LATEST_RELEASE_API = "https://api.github.com/repos/andukR/LiVerse/releases/latest"
GITHUB_API_VERSION = "2022-11-28"
UPDATE_TIMEOUT_SECONDS = 12.0
DOWNLOAD_TIMEOUT_SECONDS = 20.0
DOWNLOAD_CHUNK_BYTES = 64 * 1024
DOWNLOAD_PART_BYTES = 1024 * 1024
DOWNLOAD_MAX_RETRIES = 6
_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


class ReleaseUpdateError(RuntimeError):
    """A release is incomplete or its installer cannot be verified."""


class ReleaseDownloadCancelled(ReleaseUpdateError):
    """The operator paused the download; verified installation never started."""


def update_log_path() -> Path:
    return windows_update_dir().parent / "logs" / "updates.jsonl"


def log_update(event: str, **details) -> None:
    try:
        path = update_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "event": event,
                                     **details}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def parse_release_version(value: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.fullmatch(str(value).strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _github_request(url: str) -> Request:
    return Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "LiVerse-Windows-Updater",
        },
    )


def check_windows_release_update(
    current_version: str,
    *,
    api_url: str = LATEST_RELEASE_API,
    timeout: float = UPDATE_TIMEOUT_SECONDS,
) -> dict:
    """Return a verified installer description from the latest GitHub release."""
    log_update("release_check_started", current_version=current_version)
    local_version = parse_release_version(current_version)
    if local_version is None:
        return {"status": "invalid_local_version", "local_version": current_version}
    try:
        with urlopen(_github_request(api_url), timeout=timeout) as response:
            release = json.load(response)
    except HTTPError as exc:
        log_update("release_check_failed", reason=f"HTTP {exc.code}")
        if exc.code == 404:
            return {"status": "no_release", "local_version": current_version}
        return {
            "status": "network_unavailable",
            "local_version": current_version,
            "reason": f"HTTP {exc.code}",
        }
    except (OSError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        log_update("release_check_failed", reason=str(exc))
        return {
            "status": "network_unavailable",
            "local_version": current_version,
            "reason": str(exc),
        }

    if not isinstance(release, dict) or release.get("draft"):
        return {"status": "invalid_release", "reason": "release metadata"}
    remote_text = str(release.get("tag_name") or "").strip().removeprefix("v")
    remote_version = parse_release_version(remote_text)
    if remote_version is None:
        return {"status": "invalid_release", "reason": "version tag"}
    if remote_version <= local_version:
        return {
            "status": "current",
            "kind": "binary",
            "local_version": current_version,
            "remote_version": remote_text,
        }

    installer_name = f"LiVerse-Setup-{remote_text}.exe"
    assets = release.get("assets")
    if not isinstance(assets, list):
        return {"status": "invalid_release", "reason": "release assets"}
    installer = next(
        (
            asset
            for asset in assets
            if isinstance(asset, dict)
            and asset.get("name") == installer_name
            and asset.get("state") == "uploaded"
        ),
        None,
    )
    if installer is None:
        return {
            "status": "invalid_release",
            "reason": f"missing {installer_name}",
        }
    digest = str(installer.get("digest") or "")
    algorithm, separator, expected_hash = digest.partition(":")
    if separator != ":" or algorithm.lower() != "sha256" or not _SHA256_RE.fullmatch(expected_hash):
        return {"status": "invalid_release", "reason": "missing SHA-256"}
    installer_url = str(installer.get("browser_download_url") or "")
    if not installer_url.startswith("https://github.com/andukR/LiVerse/releases/download/"):
        return {"status": "invalid_release", "reason": "installer URL"}
    try:
        installer_size = int(installer.get("size"))
    except (TypeError, ValueError):
        installer_size = 0
    if installer_size <= 0:
        return {"status": "invalid_release", "reason": "installer size"}
    return {
        "status": "available",
        "kind": "binary",
        "local_version": current_version,
        "remote_version": remote_text,
        "installer_name": installer_name,
        "installer_url": installer_url,
        "installer_size": installer_size,
        "sha256": expected_hash.lower(),
        "release_url": str(release.get("html_url") or ""),
        "release_notes": str(release.get("body") or "").strip(),
    }


def windows_update_dir(
    *,
    platform: str | None = None,
    environ: dict[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    selected_platform = sys.platform if platform is None else platform
    selected_environ = os.environ if environ is None else environ
    selected_home = Path.home() if home is None else home
    if selected_platform.startswith("win"):
        local_app_data = selected_environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "LiVerse" / "updates"
    cache_root = Path(selected_environ.get("XDG_CACHE_HOME") or selected_home / ".cache")
    return cache_root / "liverse" / "updates"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(DOWNLOAD_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_windows_release_installer(
    update: dict,
    *,
    destination_dir: Path | None = None,
    timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
    progress: Callable[[int, int], None] | None = None,
    status: Callable[[dict], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    max_retries: int = DOWNLOAD_MAX_RETRIES,
    retry_delay: float = 2.0,
) -> Path:
    if update.get("status") != "available" or update.get("kind") != "binary":
        raise ReleaseUpdateError("Нет проверенного обновления для скачивания")
    installer_name = str(update.get("installer_name") or "")
    if Path(installer_name).name != installer_name or not installer_name.endswith(".exe"):
        raise ReleaseUpdateError("Недопустимое имя установщика")
    expected_hash = str(update.get("sha256") or "").lower()
    if not _SHA256_RE.fullmatch(expected_hash):
        raise ReleaseUpdateError("У выпуска отсутствует корректный SHA-256")
    try:
        expected_size = int(update.get("installer_size"))
    except (TypeError, ValueError) as exc:
        raise ReleaseUpdateError("У выпуска отсутствует размер установщика") from exc
    if expected_size <= 0:
        raise ReleaseUpdateError("У выпуска отсутствует размер установщика")
    url = str(update.get("installer_url") or "")
    if not url.startswith("https://github.com/andukR/LiVerse/releases/download/"):
        raise ReleaseUpdateError("Недопустимая ссылка на установщик")

    target_dir = destination_dir or windows_update_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / installer_name
    if target.is_file() and target.stat().st_size == expected_size and file_sha256(target) == expected_hash:
        log_update("download_cached_verified", installer=installer_name, bytes=expected_size, sha256=expected_hash)
        if progress:
            progress(expected_size, expected_size)
        return target

    partial = target.with_suffix(target.suffix + ".download")
    identity_path = partial.with_suffix(partial.suffix + ".json")
    identity = {"url": url, "size": expected_size, "sha256": expected_hash}
    try:
        prior_identity = json.loads(identity_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prior_identity = None
    if prior_identity != identity or partial.exists() and partial.stat().st_size > expected_size:
        partial.unlink(missing_ok=True)
    identity_path.write_text(json.dumps(identity), encoding="utf-8")
    received = partial.stat().st_size if partial.exists() else 0
    log_update("download_started", installer=installer_name, expected_size=expected_size,
               expected_sha256=expected_hash, resumed_bytes=received)
    failures = 0
    last_logged = time.monotonic()
    logged_bytes = received

    def notify(phase: str, **details) -> None:
        if status:
            status({"phase": phase, "downloaded": received, "total": expected_size, **details})

    def check_cancelled() -> None:
        if cancelled and cancelled():
            raise ReleaseDownloadCancelled("Загрузка отменена. Полученная часть сохранена для продолжения.")

    try:
        if progress:
            progress(received, expected_size)
        while received < expected_size:
            check_cancelled()
            before = received
            end = min(expected_size - 1, received + DOWNLOAD_PART_BYTES - 1)
            request = _github_request(url)
            request.add_header("Accept-Encoding", "identity")
            request.add_header("Range", f"bytes={received}-{end}")
            notify("downloading")
            try:
                with urlopen(request, timeout=timeout) as response:
                    code = getattr(response, "status", 200)
                    if code == 206:
                        value = response.headers.get("Content-Range", "")
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", value)
                        if not match or int(match[1]) != received or int(match[3]) != expected_size or not received <= int(match[2]) <= end:
                            raise ReleaseUpdateError("Сервер вернул неверную часть установщика")
                        response_end = int(match[2]) + 1
                    elif code == 200:
                        # A server may ignore Range. Never append its entire file to a prefix.
                        if received:
                            received = 0
                            partial.unlink(missing_ok=True)
                            log_update("download_restarted", reason="server_ignored_range")
                        response_end = expected_size
                    else:
                        raise ReleaseUpdateError(f"Неожиданный ответ сервера: HTTP {code}")
                    with partial.open("ab") as output:
                        read = getattr(response, "read1", response.read)
                        while received < response_end:
                            check_cancelled()
                            chunk = read(min(DOWNLOAD_CHUNK_BYTES, response_end - received))
                            if not chunk:
                                raise ConnectionError("Соединение прервано до конца части файла")
                            if received + len(chunk) > response_end:
                                raise ReleaseUpdateError("Полученная часть больше заявленного размера")
                            output.write(chunk)
                            output.flush()
                            received += len(chunk)
                            now = time.monotonic()
                            if progress:
                                progress(received, expected_size)
                            if now - last_logged >= 10:
                                log_update("download_progress", downloaded=received, expected_size=expected_size,
                                           bytes_per_second=round(max(0, received - logged_bytes) / (now - last_logged), 1))
                                last_logged, logged_bytes = now, received
                failures = 0
            except (OSError, URLError, HTTPException) as exc:
                if isinstance(exc, HTTPError) and exc.code not in {408, 429, 500, 502, 503, 504}:
                    raise ReleaseUpdateError(f"Скачивание невозможно: HTTP {exc.code}") from exc
                failures = 1 if received > before else failures + 1
                log_update("download_retry", downloaded=received, attempt=failures,
                           reason=str(exc), error_type=type(exc).__name__)
                if failures > max_retries:
                    raise ReleaseUpdateError("Связь не восстановилась. Полученная часть сохранена; нажмите «Повторить».") from exc
                delay = min(30.0, retry_delay * 2 ** (failures - 1))
                notify("retrying", attempt=failures, delay=delay, reason=str(exc))
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    check_cancelled()
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        check_cancelled()
        notify("verifying")
        log_update("download_verification_started", downloaded=received)
        if file_sha256(partial) != expected_hash:
            partial.unlink(missing_ok=True)
            identity_path.unlink(missing_ok=True)
            raise ReleaseUpdateError("SHA-256 установщика не совпадает с выпуском. Повторите загрузку.")
        os.replace(partial, target)
        identity_path.unlink(missing_ok=True)
        log_update("download_verified", installer=installer_name, bytes=received, sha256=expected_hash)
        notify("complete")
        return target
    except Exception as exc:
        log_update("download_cancelled" if isinstance(exc, ReleaseDownloadCancelled) else "download_failed",
                   downloaded=received, expected_size=expected_size, reason=str(exc), error_type=type(exc).__name__)
        raise


def launch_windows_release_installer(installer: Path) -> None:
    """Start the verified installer without opening a second wizard window."""
    if not installer.is_file() or installer.suffix.lower() != ".exe":
        raise ReleaseUpdateError(f"Установщик не найден: {installer}")
    subprocess.Popen(
        [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"],
        cwd=str(installer.parent),
        close_fds=True,
    )
    log_update("installer_launched", installer=installer.name)
