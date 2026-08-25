"""Updater for the packaged LiVerse Windows installer."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


LATEST_RELEASE_API = "https://api.github.com/repos/andukR/LiVerse/releases/latest"
GITHUB_API_VERSION = "2022-11-28"
UPDATE_TIMEOUT_SECONDS = 12.0
DOWNLOAD_TIMEOUT_SECONDS = 60.0
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


class ReleaseUpdateError(RuntimeError):
    """A release is incomplete or its installer cannot be verified."""


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
    local_version = parse_release_version(current_version)
    if local_version is None:
        return {"status": "invalid_local_version", "local_version": current_version}
    try:
        with urlopen(_github_request(api_url), timeout=timeout) as response:
            release = json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return {"status": "no_release", "local_version": current_version}
        return {
            "status": "network_unavailable",
            "local_version": current_version,
            "reason": f"HTTP {exc.code}",
        }
    except (OSError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
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
        if progress:
            progress(expected_size, expected_size)
        return target

    partial = target.with_suffix(target.suffix + ".download")
    if partial.exists():
        partial.unlink()
    received = 0
    digest = hashlib.sha256()
    try:
        with urlopen(_github_request(url), timeout=timeout) as response, partial.open("wb") as output:
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                received += len(chunk)
                if received > expected_size:
                    raise ReleaseUpdateError("Полученный установщик больше заявленного размера")
                digest.update(chunk)
                output.write(chunk)
                if progress:
                    progress(received, expected_size)
        if received != expected_size:
            raise ReleaseUpdateError(
                f"Установщик скачан не полностью: {received} из {expected_size} байт"
            )
        actual_hash = digest.hexdigest()
        if actual_hash != expected_hash:
            raise ReleaseUpdateError(
                "SHA-256 скачанного установщика не совпадает с данными выпуска"
            )
        os.replace(partial, target)
        return target
    except Exception:
        try:
            partial.unlink()
        except FileNotFoundError:
            pass
        raise


def launch_windows_release_installer(installer: Path) -> None:
    """Start the verified interactive installer and return immediately."""
    if not installer.is_file() or installer.suffix.lower() != ".exe":
        raise ReleaseUpdateError(f"Установщик не найден: {installer}")
    subprocess.Popen([str(installer)], cwd=str(installer.parent), close_fds=True)

