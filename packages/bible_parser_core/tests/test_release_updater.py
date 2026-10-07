import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import __version__ as tools_version
from tools.liverse_gui import (
    check_gui_update,
    packaged_windows_runtime,
    run_packaged_update_smoke_test,
)
from tools.release_updater import (
    ReleaseUpdateError,
    ReleaseDownloadCancelled,
    check_windows_release_update,
    download_windows_release_installer,
    launch_windows_release_installer,
    parse_release_version,
    windows_update_dir,
)


class FakeResponse:
    def __init__(self, content: bytes):
        self.stream = io.BytesIO(content)

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)


def release_payload(version: str, installer: bytes, *, digest: str | None = None) -> bytes:
    installer_name = f"LiVerse-Setup-{version}.exe"
    data = {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": False,
        "html_url": f"https://github.com/andukR/LiVerse/releases/tag/v{version}",
        "assets": [
            {
                "name": installer_name,
                "state": "uploaded",
                "size": len(installer),
                "digest": digest or f"sha256:{hashlib.sha256(installer).hexdigest()}",
                "browser_download_url": (
                    "https://github.com/andukR/LiVerse/releases/download/"
                    f"v{version}/{installer_name}"
                ),
            }
        ],
    }
    return json.dumps(data).encode("utf-8")


class ReleaseUpdaterTest(unittest.TestCase):
    def test_packaged_update_smoke_accepts_offline_binary_check(self):
        with (
            patch("tools.liverse_gui.packaged_windows_runtime", return_value=True),
            patch(
                "tools.liverse_gui.check_gui_update",
                return_value={"status": "network_unavailable", "kind": "binary"},
            ) as update_check,
            patch("tools.liverse_gui.write_gui_log") as log,
        ):
            self.assertEqual(0, run_packaged_update_smoke_test())

        update_check.assert_called_once_with()
        self.assertIn("channel=binary", log.call_args.args[0])

    def test_packaged_update_smoke_rejects_source_runtime(self):
        with (
            patch("tools.liverse_gui.packaged_windows_runtime", return_value=False),
            patch("tools.liverse_gui.check_gui_update") as update_check,
            patch("tools.liverse_gui.write_gui_log"),
        ):
            self.assertEqual(2, run_packaged_update_smoke_test())

        update_check.assert_not_called()

    def test_version_comparison_accepts_release_tags(self):
        self.assertEqual((1, 2, 3), parse_release_version("v1.2.3"))
        self.assertEqual((1, 2, 3), parse_release_version("1.2.3"))
        self.assertIsNone(parse_release_version("1.2"))
        self.assertIsNone(parse_release_version("version-1.2.3"))

    def test_packaged_windows_uses_binary_update_channel(self):
        self.assertTrue(packaged_windows_runtime(frozen=True, platform="win32"))
        self.assertFalse(packaged_windows_runtime(frozen=False, platform="win32"))
        self.assertFalse(packaged_windows_runtime(frozen=True, platform="linux"))

    def test_gui_routes_packaged_windows_to_release_updater(self):
        expected = {"status": "available", "kind": "binary"}
        with (
            patch(
                "tools.liverse_gui.check_windows_release_update",
                return_value=expected,
            ) as binary_check,
            patch("tools.liverse_gui.check_startup_update") as source_check,
        ):
            result = check_gui_update(frozen=True, platform="win32")

        self.assertEqual(expected, result)
        binary_check.assert_called_once_with(tools_version)
        source_check.assert_not_called()

    def test_gui_keeps_git_updater_for_source_installation(self):
        expected = {"status": "current"}
        with (
            patch("tools.liverse_gui.check_windows_release_update") as binary_check,
            patch("tools.liverse_gui.check_startup_update", return_value=expected) as source_check,
        ):
            result = check_gui_update(frozen=False, platform="win32")

        self.assertEqual(expected, result)
        source_check.assert_called_once_with()
        binary_check.assert_not_called()

    def test_release_check_returns_verified_newer_installer(self):
        installer = b"new LiVerse installer"
        with patch(
            "tools.release_updater.urlopen",
            return_value=FakeResponse(release_payload("1.1.1", installer)),
        ):
            result = check_windows_release_update("1.1.0")

        self.assertEqual("available", result["status"])
        self.assertEqual("binary", result["kind"])
        self.assertEqual("1.1.1", result["remote_version"])
        self.assertEqual(len(installer), result["installer_size"])
        self.assertEqual(hashlib.sha256(installer).hexdigest(), result["sha256"])

    def test_release_check_does_not_offer_same_or_older_version(self):
        installer = b"current installer"
        with patch(
            "tools.release_updater.urlopen",
            return_value=FakeResponse(release_payload("1.1.0", installer)),
        ):
            result = check_windows_release_update("1.1.0")

        self.assertEqual("current", result["status"])

    def test_release_without_sha256_is_rejected(self):
        installer = b"unverified installer"
        with patch(
            "tools.release_updater.urlopen",
            return_value=FakeResponse(
                release_payload("1.1.1", installer, digest="sha512:abc")
            ),
        ):
            result = check_windows_release_update("1.1.0")

        self.assertEqual("invalid_release", result["status"])
        self.assertEqual("missing SHA-256", result["reason"])

    def test_verified_installer_is_downloaded_atomically(self):
        installer = b"verified installer bytes"
        update = json.loads(release_payload("1.1.1", installer))["assets"][0]
        update = {
            "status": "available",
            "kind": "binary",
            "installer_name": update["name"],
            "installer_url": update["browser_download_url"],
            "installer_size": update["size"],
            "sha256": update["digest"].partition(":")[2],
        }
        progress = []
        with tempfile.TemporaryDirectory() as directory, patch(
            "tools.release_updater.urlopen", return_value=FakeResponse(installer)
        ):
            path = download_windows_release_installer(
                update,
                destination_dir=Path(directory),
                progress=lambda received, total: progress.append((received, total)),
            )

            self.assertEqual(installer, path.read_bytes())
            self.assertFalse(path.with_suffix(".exe.download").exists())
        self.assertEqual((len(installer), len(installer)), progress[-1])

    def test_incorrect_download_hash_is_rejected_and_partial_is_removed(self):
        expected = b"expected installer"
        received = b"tampered installer"
        name = "LiVerse-Setup-1.1.1.exe"
        update = {
            "status": "available",
            "kind": "binary",
            "installer_name": name,
            "installer_url": (
                "https://github.com/andukR/LiVerse/releases/download/v1.1.1/" + name
            ),
            "installer_size": len(received),
            "sha256": hashlib.sha256(expected).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as directory, patch(
            "tools.release_updater.urlopen", return_value=FakeResponse(received)
        ):
            target_dir = Path(directory)
            with self.assertRaises(ReleaseUpdateError):
                download_windows_release_installer(update, destination_dir=target_dir)
            self.assertFalse((target_dir / name).exists())
            self.assertFalse((target_dir / f"{name}.download").exists())

    def update_description(self, content):
        name = "LiVerse-Setup-1.1.1.exe"
        return {"status": "available", "kind": "binary", "installer_name": name,
                "installer_url": "https://github.com/andukR/LiVerse/releases/download/v1.1.1/" + name,
                "installer_size": len(content), "sha256": hashlib.sha256(content).hexdigest()}

    def test_timeout_resumes_from_received_bytes_and_logs_retry(self):
        content = b"abcdefghijk"
        class BrokenResponse(FakeResponse):
            def read(self, size=-1):
                if self.stream.tell():
                    raise TimeoutError("timed out")
                return self.stream.read(4)
        resumed = FakeResponse(content[4:])
        resumed.status = 206
        resumed.headers = {"Content-Range": "bytes 4-10/11"}
        with tempfile.TemporaryDirectory() as directory, patch("tools.release_updater.windows_update_dir", return_value=Path(directory) / "updates"), patch("tools.release_updater.urlopen", side_effect=[BrokenResponse(content), resumed]) as open_url:
            path = download_windows_release_installer(self.update_description(content), retry_delay=0)
            self.assertEqual(content, path.read_bytes())
            self.assertEqual("bytes=4-10", open_url.call_args_list[1].args[0].get_header("Range"))
            records = [json.loads(row) for row in (Path(directory) / "logs" / "updates.jsonl").read_text().splitlines()]
            self.assertIn("download_retry", [r["event"] for r in records])
            self.assertEqual("download_verified", records[-1]["event"])

    def test_retry_after_restart_preserves_partial_and_changed_release_discards_it(self):
        content = b"abcdefghijk"
        class BrokenResponse(FakeResponse):
            def read(self, size=-1):
                if self.stream.tell():
                    raise TimeoutError("timed out")
                return self.stream.read(4)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            update = self.update_description(content)
            with patch("tools.release_updater.urlopen", return_value=BrokenResponse(content)):
                with self.assertRaises(ReleaseUpdateError):
                    download_windows_release_installer(update, destination_dir=root, max_retries=0)
            self.assertEqual(content[:4], (root / (update["installer_name"] + ".download")).read_bytes())
            changed = b"different-new-release"
            with patch("tools.release_updater.urlopen", return_value=FakeResponse(changed)) as open_url:
                result = download_windows_release_installer(self.update_description(changed), destination_dir=root)
            self.assertEqual(changed, result.read_bytes())
            self.assertTrue(open_url.call_args.args[0].get_header("Range").startswith("bytes=0-"))

    def test_bounded_range_parts_are_assembled_correctly(self):
        content = b"abcdefghijk"
        ranges = []
        def serve(request, timeout):
            value = request.get_header("Range")
            ranges.append(value)
            start, end = map(int, value.removeprefix("bytes=").split("-"))
            response = FakeResponse(content[start:end + 1])
            response.status = 206
            response.headers = {"Content-Range": f"bytes {start}-{end}/{len(content)}"}
            return response
        with tempfile.TemporaryDirectory() as directory, patch("tools.release_updater.DOWNLOAD_PART_BYTES", 4), patch("tools.release_updater.urlopen", side_effect=serve):
            result = download_windows_release_installer(self.update_description(content), destination_dir=Path(directory))
            self.assertEqual(content, result.read_bytes())
        self.assertEqual(["bytes=0-3", "bytes=4-7", "bytes=8-10"], ranges)

    def test_cancel_keeps_partial_and_next_call_continues_even_if_range_ignored(self):
        content = b"x" * 70000
        stopped = [False]
        with tempfile.TemporaryDirectory() as directory, patch("tools.release_updater.urlopen", side_effect=lambda *args, **kwargs: FakeResponse(content)):
            root = Path(directory)
            with self.assertRaises(ReleaseDownloadCancelled):
                download_windows_release_installer(self.update_description(content), destination_dir=root,
                    cancelled=lambda: stopped[0], progress=lambda n, total: stopped.__setitem__(0, n > 0))
            self.assertEqual(65536, (root / "LiVerse-Setup-1.1.1.exe.download").stat().st_size)
            result = download_windows_release_installer(self.update_description(content), destination_dir=root)
            self.assertEqual(content, result.read_bytes())

    def test_incorrect_content_range_is_rejected(self):
        content = b"abc"
        response = FakeResponse(content)
        response.status = 206
        response.headers = {"Content-Range": "bytes 1-2/3"}
        with tempfile.TemporaryDirectory() as directory, patch("tools.release_updater.urlopen", return_value=response):
            with self.assertRaises(ReleaseUpdateError):
                download_windows_release_installer(self.update_description(content), destination_dir=Path(directory))
            self.assertFalse((Path(directory) / "LiVerse-Setup-1.1.1.exe").exists())

    def test_update_log_is_included_in_exported_archive(self):
        import zipfile
        from tools.liverse_gui import create_log_archive
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "updates.jsonl"
            log.write_text('{"event":"download_retry","downloaded":42}\n', encoding="utf-8")
            archive = root / "logs.zip"
            self.assertEqual(1, create_log_archive([], archive, update_log=log))
            with zipfile.ZipFile(archive) as package:
                self.assertEqual(42, json.loads(package.read("updates/updates.jsonl"))["downloaded"])

    def test_windows_download_directory_uses_local_app_data(self):
        self.assertEqual(
            Path("C:/Users/Test/AppData/Local") / "LiVerse" / "updates",
            windows_update_dir(
                platform="win32",
                environ={"LOCALAPPDATA": "C:/Users/Test/AppData/Local"},
                home=Path("C:/Users/Test"),
            ),
        )

    def test_verified_installer_launches_without_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            installer = Path(directory) / "LiVerse-Setup-1.1.1.exe"
            installer.write_bytes(b"installer")
            with patch("tools.release_updater.subprocess.Popen") as popen:
                launch_windows_release_installer(installer)

        popen.assert_called_once_with(
            [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"],
            cwd=str(installer.parent), close_fds=True
        )


if __name__ == "__main__":
    unittest.main()
