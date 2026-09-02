import unittest
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from bible_parser_core.live_pipeline import LiveReferencePipeline, build_grammar
from bible_parser_core.parser import normalize_text
from bible_parser_core.risk_model import load_risk_model, score_payload_with_model
from tools.holyrics import (
    cross_chapter_quick_presentation_slides,
    format_missing_holyrics_permissions,
    handle_scripture_range_reading_match,
    post_holyrics_api,
    post_holyrics_url,
    restore_holyrics_presentation,
    scripture_range_quick_presentation_body,
    scripture_range_quick_presentation_slides,
    scripture_range_reading_active,
    scripture_range_reading_state,
    sync_scripture_range_reading,
    temporary_verse_display_active,
)


class LiveReferencePipelineTest(unittest.TestCase):
    def test_regression_suite_does_not_shrink_silently(self):
        tests_dir = Path(__file__).resolve().parent
        suite = unittest.defaultTestLoader.discover(str(tests_dir), pattern="test_*.py")

        self.assertGreaterEqual(
            suite.countTestCases(),
            255,
            "Набор регрессионных тестов уменьшился; проверьте, какие проверки были удалены.",
        )

    def test_gui_engine_command_uses_saved_settings_without_exposing_token(self):
        from tools.liverse_gui import GuiConfig, engine_command

        config = GuiConfig(
            run_mode="semi_auto",
            approval_ui="popup",
            audio_device_name="Microphone (USB2.0 Device)",
            citation_detection_mode="hybrid_confirm",
            holyrics_token="secret-token",
            holyrics_port=8091,
            quick_seconds=5,
            open_operator_qr=False,
            text_detection_db=Path("bible_index.db"),
        )
        command = engine_command(
            config,
            project_root=Path("C:/LiVerse"),
            python_executable="pythonw.exe",
        )

        self.assertEqual("pythonw.exe", command[0])
        self.assertIn("--semi-auto-approval", command)
        self.assertIn("--device-name", command)
        self.assertIn("Microphone (USB2.0 Device)", command)
        self.assertIn("--no-open-operator-qr", command)
        self.assertNotIn("secret-token", command)

    def test_packaged_gui_engine_command_uses_sibling_executable(self):
        from tools.liverse_gui import GuiConfig, engine_command

        gui_executable = Path("C:/LiVerse/LiVerse.exe")
        database_path = Path("C:/LiVerse/_internal/bible_index/bible_index.db")
        command = engine_command(
            GuiConfig(text_detection_db=database_path),
            project_root=Path("C:/LiVerse/_internal"),
            python_executable="pythonw.exe",
            application_executable=gui_executable,
            frozen=True,
        )

        self.assertEqual(str(gui_executable.with_name("LiVerseEngine.exe")), command[0])
        self.assertNotIn("pythonw.exe", command)
        self.assertNotIn("vosk_grammar_probe.py", " ".join(command))
        self.assertIn(str(database_path), command)
        self.assertIn("--stop-file", command)
        self.assertIn("--no-open-operator-qr", command)

    def test_microphone_indicator_uses_decibel_scale(self):
        from tools.vosk_grammar_probe import audio_level_percent

        self.assertEqual(0, audio_level_percent(0))
        self.assertLess(audio_level_percent(300), audio_level_percent(3000))
        self.assertLess(audio_level_percent(3000), audio_level_percent(30000))
        self.assertEqual(100, audio_level_percent(32767))

    def test_stop_file_is_consumed_once(self):
        import tempfile

        from tools.vosk_grammar_probe import consume_stop_request

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "engine.stop"
            path.write_text("restart", encoding="utf-8")
            self.assertEqual("restart", consume_stop_request(path))
            self.assertFalse(path.exists())
            self.assertEqual("", consume_stop_request(path))

    def test_session_summary_fits_small_windows_desktop(self):
        from tools.vosk_grammar_probe import session_summary_dimensions

        self.assertEqual((760, 560), session_summary_dimensions(1920, 1080))
        self.assertEqual((720, 520), session_summary_dimensions(800, 600))
        self.assertEqual((500, 400), session_summary_dimensions(500, 400))

    def test_log_archive_contains_only_selected_diagnostic_files(self):
        from tools.liverse_gui import create_log_archive, list_log_sessions

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            older = root / "20260824_100000_000000"
            newer = root / "20260825_100000_000000"
            older.mkdir()
            newer.mkdir()
            (older / "events.jsonl").write_text("{}\n", encoding="utf-8")
            (newer / "session.json").write_text(
                '{"command":"liverse --holyrics-token private", "token":"private"}\n',
                encoding="utf-8",
            )
            (newer / "audio.wav").write_bytes(b"audio")
            (newer / ".env").write_text("HOLYRICS_TOKEN=secret\n", encoding="utf-8")
            destination = root / "logs.zip"

            self.assertEqual([newer, older], list_log_sessions(root))
            self.assertEqual(1, create_log_archive([newer], destination))
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual([f"{newer.name}/session.json"], archive.namelist())
                exported = archive.read(archive.namelist()[0]).decode("utf-8")
                self.assertNotIn("private", exported)
                self.assertIn("[скрыто]", exported)

    def test_engine_command_diagnostics_hide_holyrics_token(self):
        from tools.vosk_grammar_probe import safe_command_argv

        safe = safe_command_argv(
            [
                "LiVerseEngine.exe",
                "--holyrics-token",
                "first-secret",
                "--holyrics-token=second-secret",
                "--debug-console",
            ]
        )

        self.assertEqual(
            [
                "LiVerseEngine.exe",
                "--holyrics-token",
                "[скрыто]",
                "--holyrics-token=[скрыто]",
                "--debug-console",
            ],
            safe,
        )

    def test_holyrics_api_diagnostics_include_request_and_full_response_without_token(self):
        events: list[tuple[str, dict]] = []
        args = SimpleNamespace(
            holyrics_token="private-token",
            holyrics_timeout=3.0,
            _holyrics_event_logger=lambda event, payload: events.append((event, payload)),
        )

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return b'{"status":"ok","data":{"id":"theme-1","token":"response-secret"}}'

        with patch("tools.holyrics.request.urlopen", return_value=Response()) as urlopen:
            ok, reason, response = post_holyrics_api(
                args,
                "http://127.0.0.1:8091",
                "ShowQuickPresentation",
                {
                    "slides": [
                        {"text": "Иоанн 3:16", "theme": {"id": "theme-1"}}
                    ],
                    "diagnostic_note": "must also hide private-token here",
                },
            )

        self.assertTrue(ok)
        self.assertEqual("", reason)
        self.assertIn('"status":"ok"', response)
        self.assertEqual(["holyrics_api_request", "holyrics_api_response"], [item[0] for item in events])
        request_event = events[0][1]
        response_event = events[1][1]
        self.assertEqual("ShowQuickPresentation", request_event["endpoint"])
        self.assertEqual("theme-1", request_event["request_body"]["slides"][0]["theme"]["id"])
        self.assertIn("[скрыто]", request_event["request_body"]["diagnostic_note"])
        self.assertNotIn("token", request_event["base_url"])
        self.assertEqual(200, response_event["http_status"])
        self.assertEqual("[скрыто]", response_event["response_body"]["data"]["token"])
        self.assertNotIn("private-token", str(events))
        self.assertNotIn("private-token", urlopen.call_args.args[0].full_url.split("?")[0])

    def test_phone_operator_has_fullscreen_and_wake_lock_controls(self):
        root = Path(__file__).resolve().parents[3]
        html = (root / "slide_display" / "operator.html").read_text(encoding="utf-8")
        script = (root / "slide_display" / "operator.js").read_text(encoding="utf-8")

        self.assertIn('id="screenModeButton"', html)
        self.assertIn("requestFullscreen", script)
        self.assertIn('navigator.wakeLock.request("screen")', script)
        self.assertIn('document.addEventListener("visibilitychange"', script)
        self.assertIn('id="songModeButton"', html)
        self.assertIn('id="previousSongSlide"', html)
        self.assertIn('id="nextSongSlide"', html)
        self.assertIn('/api/presentation-${action}', script)

    def test_phone_song_controls_use_regular_holyrics_presentation_actions(self):
        from tools.holyrics import control_holyrics_presentation

        args = SimpleNamespace(
            holyrics_token="secret",
            holyrics_url="http://127.0.0.1:8091",
        )
        with patch("tools.holyrics.post_holyrics_api", return_value=(True, "", '{"status":"ok"}')) as api:
            self.assertEqual((True, ""), control_holyrics_presentation(args, "next"))
            self.assertEqual((True, ""), control_holyrics_presentation(args, "previous"))

        self.assertEqual("ActionNext", api.call_args_list[0].args[2])
        self.assertEqual("ActionPrevious", api.call_args_list[1].args[2])
        self.assertEqual({}, api.call_args_list[0].args[3])

    def test_slide_server_routes_phone_song_controls_to_callback(self):
        from tools.slide_server import reset_operator_state, run_presentation_action

        calls = []
        reset_operator_state(
            presentation_action_callback=lambda action: calls.append(action) or (True, "")
        )

        self.assertEqual((True, ""), run_presentation_action("next"))
        self.assertEqual((True, ""), run_presentation_action("previous"))
        self.assertEqual(["next", "previous"], calls)

    def test_long_passage_advance_discards_stale_phone_candidate(self):
        from tools.slide_server import operator_state, reset_operator_state, submit_candidate
        from tools.vosk_grammar_probe import clear_stale_approvals_after_range_action

        reset_operator_state()
        submit_candidate({"ref": "Иаков 2:20", "verse": "текст"})
        self.assertIsNotNone(operator_state()["candidate"])

        self.assertTrue(clear_stale_approvals_after_range_action({"advanced": True}))
        self.assertIsNone(operator_state()["candidate"])
        self.assertFalse(clear_stale_approvals_after_range_action({"matched_boundary": False}))

    def test_gui_keeps_taskbar_fallback_for_linux_wayland(self):
        from tools.liverse_gui import tray_can_hide_window, tray_needs_own_event_loop

        self.assertTrue(tray_can_hide_window(platform="win32", session_type=""))
        self.assertFalse(tray_can_hide_window(platform="linux", session_type="wayland"))
        self.assertTrue(
            tray_can_hide_window(
                platform="linux",
                session_type="wayland",
                backend="pystray._appindicator",
            )
        )
        self.assertTrue(tray_can_hide_window(platform="linux", session_type="x11"))
        self.assertTrue(
            tray_needs_own_event_loop(
                platform="linux", backend="pystray._appindicator"
            )
        )
        self.assertFalse(
            tray_needs_own_event_loop(platform="win32", backend="pystray._win32")
        )

    def test_full_setup_can_select_microphone_by_stable_name(self):
        from tools.vosk_grammar_probe import ask_audio_input_device

        devices = [
            {"name": "Built-in microphone", "max_input_channels": 1},
            {"name": "Microphone (USB2.0 Device)", "max_input_channels": 1},
            {"name": "Speakers", "max_input_channels": 0},
        ]
        fake_sounddevice = SimpleNamespace(
            query_devices=lambda: devices,
            default=SimpleNamespace(device=(0, 2)),
        )
        args = SimpleNamespace(text=None, device_name="", device=7)
        with (
            patch.dict("sys.modules", {"sounddevice": fake_sounddevice}),
            patch("tools.vosk_grammar_probe.sys.stdin", SimpleNamespace(isatty=lambda: True)),
            patch("builtins.input", return_value="2"),
            patch("builtins.print"),
        ):
            ask_audio_input_device(args)

        self.assertEqual("Microphone (USB2.0 Device)", args.device_name)
        self.assertIsNone(args.device)

    def test_startup_settings_save_and_restore_microphone_name(self):
        import os
        import tempfile

        from tools.vosk_grammar_probe import (
            apply_saved_startup_settings,
            load_startup_settings,
            save_startup_settings,
        )

        saved_args = SimpleNamespace(
            _liverse_startup_settings_enabled=True,
            require_approval=False,
            semi_auto_approval=True,
            approval_ui="popup",
            device_name="Microphone (USB2.0 Device)",
            holyrics_theme="",
            holyrics_quick_minutes=5 / 60,
        )
        restored_args = SimpleNamespace(
            approval_ui="web",
            device_name="",
            holyrics_theme="",
            holyrics_quick_minutes=0.0,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with (
                patch.dict(os.environ, {"LIVERSE_STARTUP_SETTINGS": str(path)}, clear=False),
                patch("tools.vosk_grammar_probe.sys.argv", ["vosk_grammar_probe.py"]),
            ):
                save_startup_settings(saved_args)
                settings = load_startup_settings()
                apply_saved_startup_settings(restored_args, settings)

        self.assertEqual("Microphone (USB2.0 Device)", restored_args.device_name)

    def test_audio_input_candidates_prefer_stable_name_over_indexes(self):
        from tools.vosk_grammar_probe import audio_input_candidate_indices

        devices = [
            {"name": "Microsoft Sound Mapper", "max_input_channels": 2},
            {"name": "Virtual Mic for AudioRelay", "max_input_channels": 2},
            {"name": "Microphone (USB2.0 Device)", "max_input_channels": 1},
            {"name": "HDMI Output", "max_input_channels": 0},
        ]

        result = audio_input_candidate_indices(
            devices,
            preferred_name="usb2.0 device",
            explicit_index=1,
            default_index=0,
        )

        self.assertEqual([2, 1, 0], result)

    def test_audio_input_candidates_fall_back_from_missing_name(self):
        from tools.vosk_grammar_probe import audio_input_candidate_indices

        devices = [
            {"name": "Default microphone", "max_input_channels": 1},
            {"name": "Speakers", "max_input_channels": 0},
            {"name": "Backup microphone", "max_input_channels": 1},
        ]

        result = audio_input_candidate_indices(
            devices,
            preferred_name="disconnected headset",
            default_index=0,
        )

        self.assertEqual([0, 2], result)

    def test_startup_update_uses_verified_main_fast_forward(self):
        import inspect

        from tools.vosk_grammar_probe import apply_startup_update

        source = inspect.getsource(apply_startup_update)

        self.assertIn('"merge", "--ff-only"', source)
        self.assertNotIn("update-liverse-windows.cmd", source)

    def test_liverse_version_is_consistent_across_packages_and_metadata(self):
        import tomllib

        from bible_parser_core import __version__ as core_version
        from tools import __version__ as tools_version
        from tools.slide_server import __version__ as slide_server_version

        project_root = Path(__file__).resolve().parents[3]
        metadata = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual("1.2.2", core_version)
        self.assertEqual(core_version, tools_version)
        self.assertEqual(core_version, slide_server_version)
        self.assertEqual(["version"], metadata["project"]["dynamic"])
        self.assertEqual(
            "bible_parser_core.version.__version__",
            metadata["tool"]["setuptools"]["dynamic"]["version"]["attr"],
        )

    def test_windows_upgrade_removes_running_previous_engine(self):
        project_root = Path(__file__).resolve().parents[3]
        installer = (project_root / "installer" / "LiVerse.iss").read_text(encoding="utf-8")
        build_script = (project_root / "tools" / "sync_windows_build.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn('Type: files; Name: "{app}\\LiVerseEngine.exe"', installer)
        self.assertIn("function PrepareToInstall", installer)
        self.assertIn("/F /T /IM LiVerseEngine.exe", installer)
        self.assertIn("not DeleteFile(EnginePath)", installer)
        self.assertIn("$oldEngineProcess = Start-Process", build_script)
        self.assertIn('"--installer-test-hold"', build_script)
        self.assertIn("Previous LiVerseEngine.exe remained running after upgrade", build_script)

    def test_holyrics_first_setup_saves_env_and_updates_runtime_args(self):
        import os
        import tempfile

        from tools.holyrics import load_env_file
        from tools.vosk_grammar_probe import run_holyrics_first_setup

        args = SimpleNamespace(
            slide_output="holyrics",
            text=None,
            holyrics_token="",
            holyrics_url="auto",
            sermon_plan=True,
            holyrics_theme="",
        )
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            fake_stdin = SimpleNamespace(isatty=lambda: True)
            with (
                patch.dict(os.environ, {"LIVE_VERSE_VOSK_ENV": str(env_path)}),
                patch("tools.vosk_grammar_probe.sys.stdin", fake_stdin),
                patch("tools.vosk_grammar_probe.getpass.getpass", return_value="secret-token"),
                patch("builtins.input", return_value=""),
                patch("builtins.print"),
            ):
                run_holyrics_first_setup(args)

            self.assertEqual("secret-token", args.holyrics_token)
            self.assertEqual("http://localhost:8091", args.holyrics_url)
            self.assertEqual(
                {
                    "HOLYRICS_TOKEN": "secret-token",
                    "HOLYRICS_HOST": "http://localhost",
                    "HOLYRICS_PORT": "8091",
                    "HOLYRICS_THEME": "",
                },
                load_env_file(env_path),
            )

    def test_holyrics_first_setup_lists_all_default_permissions(self):
        from tools.holyrics import required_holyrics_permissions

        permissions = required_holyrics_permissions(
            SimpleNamespace(sermon_plan=True, holyrics_theme="")
        )

        self.assertEqual(
            (
                "GetAPIServerInfo",
                "GetBibleSettings",
                "GetCurrentPresentation",
                "GetCurrentQuickPresentation",
                "ActionNext",
                "ActionPrevious",
                "CloseCurrentQuickPresentation",
                "CloseCurrentPresentation",
                "SetBibleSettings",
                "ShowQuickPresentation",
                "ShowText",
                "ShowVerse",
                "ActionGoToIndex",
            ),
            permissions,
        )

    def test_holyrics_startup_waits_until_server_is_available(self):
        from tools.vosk_grammar_probe import wait_for_holyrics_startup

        args = SimpleNamespace()
        fake_stdin = SimpleNamespace(isatty=lambda: True)
        with (
            patch(
                "tools.vosk_grammar_probe.check_holyrics_startup",
                side_effect=[False, True],
            ) as check,
            patch("tools.vosk_grammar_probe.sys.stdin", fake_stdin),
            patch("tools.vosk_grammar_probe.read_single_key", return_value="\r"),
            patch("builtins.print"),
        ):
            result = wait_for_holyrics_startup(args)

        self.assertEqual("ready", result)
        self.assertEqual(2, check.call_count)

    def test_holyrics_startup_can_be_closed_explicitly(self):
        from tools.vosk_grammar_probe import wait_for_holyrics_startup

        args = SimpleNamespace()
        fake_stdin = SimpleNamespace(isatty=lambda: True)
        with (
            patch("tools.vosk_grammar_probe.check_holyrics_startup", return_value=False),
            patch("tools.vosk_grammar_probe.sys.stdin", fake_stdin),
            patch("tools.vosk_grammar_probe.read_single_key", return_value="q"),
            patch("builtins.print"),
        ):
            result = wait_for_holyrics_startup(args)

        self.assertEqual("quit", result)

    def test_windows_runner_keeps_console_open_after_error(self):
        project_root = Path(__file__).resolve().parents[3]
        runner = (project_root / "run-liverse.cmd").read_text(encoding="utf-8")

        self.assertIn('set "LIVERSE_EXIT=%ERRORLEVEL%"', runner)
        self.assertIn('if not "%LIVERSE_EXIT%"=="0"', runner)
        self.assertIn("pause >nul", runner)

    def test_windows_shortcut_uses_graphical_python_without_console(self):
        project_root = Path(__file__).resolve().parents[3]
        updater = (project_root / "update-liverse-windows.ps1").read_text(encoding="utf-8")
        cmd_updater = (project_root / "update-liverse-windows.cmd").read_text(encoding="utf-8")

        self.assertIn('.venv\\Scripts\\pythonw.exe', updater)
        self.assertIn('tools\\liverse_gui.py', updater)
        self.assertIn("$shortcut.TargetPath = $pythonw", updater)
        self.assertIn('shortcut.TargetPath = "%TARGET_DIR%\\.venv\\Scripts\\pythonw.exe"', cmd_updater)
        self.assertIn('%TARGET_DIR%\\tools\\liverse_gui.py', cmd_updater)

    def test_full_startup_setup_reopens_holyrics_wizard_and_keeps_token(self):
        import os
        import tempfile

        from tools.holyrics import load_env_file
        from tools.vosk_grammar_probe import run_holyrics_first_setup

        args = SimpleNamespace(
            slide_output="holyrics",
            text=None,
            holyrics_token="saved-token",
            holyrics_url="http://localhost:8091",
            sermon_plan=True,
            holyrics_theme="",
            _liverse_full_startup_setup=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "HOLYRICS_TOKEN=saved-token\nHOLYRICS_PORT=8091\n",
                encoding="utf-8",
            )
            fake_stdin = SimpleNamespace(isatty=lambda: True)
            with (
                patch.dict(os.environ, {"LIVE_VERSE_VOSK_ENV": str(env_path)}),
                patch("tools.vosk_grammar_probe.sys.stdin", fake_stdin),
                patch("tools.vosk_grammar_probe.getpass.getpass", return_value=""),
                patch("tools.vosk_grammar_probe.env_setting", return_value="8091"),
                patch("builtins.input", return_value=""),
                patch("builtins.print"),
            ):
                run_holyrics_first_setup(args)

            self.assertEqual("saved-token", args.holyrics_token)
            self.assertEqual("saved-token", load_env_file(env_path)["HOLYRICS_TOKEN"])

    def test_holyrics_env_save_preserves_unrelated_settings(self):
        import tempfile

        from tools.holyrics import load_env_file, save_holyrics_env

        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "HOLYRICS_TOKEN=old\nHOLYRICS_PORT=9000\nLIVERSE_SETTING=keep\n",
                encoding="utf-8",
            )

            save_holyrics_env("new-token", 8091, env_path)

            self.assertEqual(
                {
                    "HOLYRICS_TOKEN": "new-token",
                    "HOLYRICS_PORT": "8091",
                    "LIVERSE_SETTING": "keep",
                    "HOLYRICS_HOST": "http://localhost",
                },
                load_env_file(env_path),
            )

    def test_windows_user_files_use_local_app_data_and_keep_legacy_fallbacks(self):
        from tools.holyrics import env_file_paths, env_write_path, liverse_config_dir

        local_app_data = Path("C:/Users/operator/AppData/Local")
        home = Path("C:/Users/operator")
        cwd = home / "LiVerse"
        environment = {"LOCALAPPDATA": str(local_app_data)}

        config_dir = liverse_config_dir(
            platform="nt", environ=environment, home=home
        )
        paths = env_file_paths(
            platform="nt", environ=environment, home=home, cwd=cwd
        )

        self.assertEqual(local_app_data / "LiVerse", config_dir)
        self.assertEqual(config_dir / ".env", env_write_path(
            platform="nt", environ=environment, home=home
        ))
        self.assertIn(home / "LiVerse" / ".env", paths)
        self.assertEqual(config_dir / ".env", paths[-1])

    def test_linux_env_file_precedence_stays_unchanged(self):
        from tools.holyrics import DEFAULT_ENV_PATH, env_file_paths

        explicit_path = Path("/tmp/liverse-explicit.env")
        cwd = Path("/tmp/liverse-cwd")

        self.assertEqual(
            [explicit_path, cwd / ".env", DEFAULT_ENV_PATH],
            env_file_paths(
                platform="posix",
                environ={"LIVE_VERSE_VOSK_ENV": str(explicit_path)},
                cwd=cwd,
            ),
        )

    def test_windows_startup_settings_read_legacy_file_before_migration(self):
        import json
        import tempfile

        from tools.vosk_grammar_probe import load_startup_settings, startup_settings_path

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            local_app_data = root / "local"
            legacy_path = home / ".config" / "liverse" / "settings.json"
            legacy_path.parent.mkdir(parents=True)
            legacy_path.write_text(
                json.dumps({"run_mode": "semi_auto"}), encoding="utf-8"
            )
            environment = {"LOCALAPPDATA": str(local_app_data)}

            self.assertEqual(
                local_app_data / "LiVerse" / "settings.json",
                startup_settings_path(
                    platform="nt", environ=environment, home=home
                ),
            )
            self.assertEqual(
                {"run_mode": "semi_auto"},
                load_startup_settings(
                    platform="nt", environ=environment, home=home
                ),
            )

    def test_startup_update_detects_and_applies_newer_main_commit(self):
        import subprocess
        import tempfile

        from tools.vosk_grammar_probe import apply_startup_update, check_startup_update

        def git(cwd: Path, *arguments: str) -> None:
            subprocess.run(
                ["git", *arguments],
                cwd=cwd,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            remote = root / "remote.git"
            local = root / "local"
            source.mkdir()
            git(source, "init", "-b", "main")
            git(source, "config", "user.name", "LiVerse Test")
            git(source, "config", "user.email", "liverse-test@example.invalid")
            version_file = source / "packages" / "bible_parser_core" / "src" / "bible_parser_core" / "version.py"
            version_file.parent.mkdir(parents=True)
            version_file.write_text('__version__ = "1.0.1"\n', encoding="utf-8")
            git(source, "add", str(version_file.relative_to(source)))
            git(source, "commit", "-m", "First version")
            git(root, "init", "--bare", str(remote))
            git(source, "remote", "add", "origin", str(remote))
            git(source, "push", "-u", "origin", "main")
            git(root, "clone", "--branch", "main", str(remote), str(local))

            version_file.write_text('__version__ = "1.1.0"\n', encoding="utf-8")
            git(source, "commit", "-am", "Second version")
            git(source, "push", "origin", "main")
            (local / "untracked.db").write_text("preserve me\n", encoding="utf-8")

            update = check_startup_update(local, repo_url=str(remote))

            self.assertEqual("available", update["status"])
            self.assertEqual("1.0.1", update["local_version"])
            self.assertEqual("1.1.0", update["remote_version"])
            self.assertIn("First version", update["local_label"])
            self.assertIn("Second version", update["remote_label"])
            with patch("tools.vosk_grammar_probe.install_updated_dependencies", return_value=True):
                self.assertTrue(apply_startup_update(update, local))
            installed_version = local / version_file.relative_to(source)
            self.assertEqual('__version__ = "1.1.0"\n', installed_version.read_text(encoding="utf-8"))
            self.assertEqual("preserve me\n", (local / "untracked.db").read_text(encoding="utf-8"))
            self.assertEqual("current", check_startup_update(local, repo_url=str(remote))["status"])

    def test_startup_update_preserves_tracked_local_changes(self):
        from tools.vosk_grammar_probe import check_startup_update

        with patch("tools.vosk_grammar_probe.run_update_git") as run_git:
            run_git.side_effect = [
                SimpleNamespace(returncode=0, stdout="main\n", stderr=""),
                SimpleNamespace(returncode=0, stdout="", stderr=""),
                SimpleNamespace(returncode=0, stdout="local\n", stderr=""),
                SimpleNamespace(returncode=0, stdout="remote\n", stderr=""),
                SimpleNamespace(returncode=0, stdout=" M README.md\n", stderr=""),
                SimpleNamespace(returncode=0, stdout="", stderr=""),
                SimpleNamespace(returncode=0, stdout="abc local", stderr=""),
                SimpleNamespace(returncode=0, stdout="def remote", stderr=""),
                SimpleNamespace(returncode=0, stdout='__version__ = "1.0.1"', stderr=""),
                SimpleNamespace(returncode=0, stdout='__version__ = "1.1.0"', stderr=""),
            ]
            with patch("pathlib.Path.exists", return_value=True):
                result = check_startup_update(Path("/test/repository"))

        self.assertEqual("tracked_changes", result["status"])

    def test_popup_uses_monitor_under_pointer_instead_of_combined_desktop(self):
        from tools.vosk_grammar_probe import center_tk_window, xrandr_monitor_bounds

        monitor_list = """Monitors: 2
 0: +*eDP-1 1920/344x1080/194+0+0  eDP-1
 1: +HDMI-1 1920/510x1080/290+1920+0  HDMI-1
"""
        self.assertEqual((0, 0, 1920, 1080), xrandr_monitor_bounds(monitor_list, 500, 400))
        self.assertEqual((1920, 0, 1920, 1080), xrandr_monitor_bounds(monitor_list, 2500, 400))

        geometries: list[str] = []
        root = SimpleNamespace(geometry=geometries.append)
        with patch("tools.vosk_grammar_probe.tk_monitor_bounds", return_value=(0, 0, 1920, 1080)):
            center_tk_window(root, 980, 360)
        with patch("tools.vosk_grammar_probe.tk_monitor_bounds", return_value=(-1920, 0, 1920, 1080)):
            center_tk_window(root, 980, 360)

        self.assertEqual(["980x360+470+360", "980x360-1450+360"], geometries)

    def test_ordinary_words_by_and_byt_do_not_become_genesis(self):
        samples = (
            "если глаз твой соблазняет тебя лучше войти в жизнь с одним глазом "
            "нежели с двумя глазами быть ввержену в геенну огненную",
            "лучше тебе с одним глазом нежели с двумя глазами бы тебе быть ввержену",
            "в этом стихе сказано что лучше быть верным в одном и в двух делах",
        )

        for text in samples:
            with self.subTest(text=text):
                result = LiveReferencePipeline().process_text(text)
                self.assertFalse(result.get("matched"))

    def test_compact_genesis_alias_still_matches(self):
        compact = LiveReferencePipeline().process_text("быт один два")
        full = LiveReferencePipeline().process_text("бытие первая глава второй стих")

        self.assertEqual("Бытие 1:2", compact.get("parsed", {}).get("ref"))
        self.assertEqual("Бытие 1:2", full.get("parsed", {}).get("ref"))

    def test_long_passage_disables_only_address_recognition(self):
        from tools.vosk_grammar_probe import (
            address_recognition_allowed,
            citation_recognition_paused,
        )

        self.assertFalse(address_recognition_allowed(True, True))
        self.assertTrue(address_recognition_allowed(True, False))
        self.assertFalse(address_recognition_allowed(False, False))

        args = SimpleNamespace(_holyrics_temporary_verse_display=object())
        self.assertTrue(citation_recognition_paused(args, False))
        self.assertFalse(citation_recognition_paused(args, True))

    def test_timed_verse_display_pauses_recognition_until_restore(self):
        from tools.holyrics import restore_holyrics_presentation_later

        args = SimpleNamespace()

        with patch("tools.holyrics.threading.Timer") as timer_class:
            restore_holyrics_presentation_later(
                args,
                "http://127.0.0.1:8091",
                {"type": "text", "text_id": "sermon-plan"},
                0.25,
            )

            self.assertTrue(temporary_verse_display_active(args))
            restore_callback = timer_class.call_args.args[1]

            with patch("tools.holyrics.restore_holyrics_presentation") as restore:
                restore_callback()

        restore.assert_called_once()
        self.assertFalse(temporary_verse_display_active(args))

    def test_confident_plan_match_is_automatic_in_semi_auto_mode(self):
        from tools.vosk_grammar_probe import sermon_plan_match_requires_approval

        args = SimpleNamespace(require_approval=False, semi_auto_approval=True)
        confident = {"score": 0.81, "matched_content_words": 5, "target_coverage": 0.8}
        uncertain = {"score": 0.61, "matched_content_words": 3, "target_coverage": 0.6}

        self.assertFalse(sermon_plan_match_requires_approval(args, confident))
        self.assertTrue(sermon_plan_match_requires_approval(args, uncertain))

    def test_confident_long_range_is_automatic_in_semi_auto_mode(self):
        from tools.vosk_grammar_probe import approval_required_for_payload

        args = SimpleNamespace(require_approval=False, semi_auto_approval=True)
        payload = {
            "slide": {"ref": "Матфей 18:3-9", "can_set_context": True},
            "ml_risk": {"needs_confirmation": False},
        }

        self.assertFalse(approval_required_for_payload(args, payload))

    def test_confident_context_range_is_not_forced_to_confirmation(self):
        from tools.vosk_grammar_probe import apply_ml_risk

        args = SimpleNamespace(
            require_approval=False,
            semi_auto_approval=True,
            risk_model_data={"loaded": True},
            risk_auto_reject_threshold=0.9,
        )
        payload = {
            "source": "context_range",
            "slide": {"ref": "Колоссянам 3:7-8"},
            "risk_score": 0.5,
        }
        with patch(
            "tools.vosk_grammar_probe.score_payload_with_model",
            return_value={"needs_confirmation": False, "decision_reasons": []},
        ):
            apply_ml_risk(args, payload)

        self.assertFalse(payload["ml_risk"]["needs_confirmation"])

    def test_high_risk_context_range_requires_confirmation_instead_of_auto_reject(self):
        from tools.vosk_grammar_probe import apply_ml_risk

        args = SimpleNamespace(
            require_approval=False,
            semi_auto_approval=True,
            risk_model_data={"loaded": True},
            risk_auto_reject_threshold=0.9,
        )
        payload = {
            "source": "context_range",
            "slide": {"ref": "Иаков 4:15"},
            "risk_score": 0.9,
        }
        with patch(
            "tools.vosk_grammar_probe.score_payload_with_model",
            return_value={"needs_confirmation": False, "decision_reasons": []},
        ):
            apply_ml_risk(args, payload)

        self.assertFalse(payload["ml_risk"].get("auto_reject"))
        self.assertTrue(payload["ml_risk"]["needs_confirmation"])
        self.assertIn(
            "manual_high_risk_context_requires_confirmation",
            payload["ml_risk"]["decision_reasons"],
        )

    def test_sermon_plan_startup_does_not_request_theme_list(self):
        from tools.vosk_grammar_probe import ask_holyrics_theme_name

        args = SimpleNamespace(slide_output="holyrics", text=None, sermon_plan=True)
        with (
            patch("tools.vosk_grammar_probe.sys.stdin.isatty", return_value=True),
            patch("tools.vosk_grammar_probe.get_holyrics_theme_options") as get_themes,
        ):
            ask_holyrics_theme_name(args)

        get_themes.assert_not_called()

    def test_interactive_duration_uses_bare_seconds_and_russian_m_for_minutes(self):
        from tools.vosk_grammar_probe import parse_holyrics_quick_duration_minutes

        self.assertEqual(0.5, parse_holyrics_quick_duration_minutes("30"))
        self.assertEqual(1.5, parse_holyrics_quick_duration_minutes("90"))
        self.assertEqual(1.0, parse_holyrics_quick_duration_minutes("1м"))
        self.assertEqual(0.5, parse_holyrics_quick_duration_minutes("0,5м"))
        self.assertEqual(0.5, parse_holyrics_quick_duration_minutes("30s"))
        self.assertEqual(2.0, parse_holyrics_quick_duration_minutes("2m"))

    def test_long_range_uses_sermon_plan_theme(self):
        args = SimpleNamespace(
            _holyrics_sermon_plan_theme_id="plan-theme",
            holyrics_theme="unused-fallback",
        )
        payload = {
            "ref": "1 Иоанна 2:1-20",
            "book": "1 Иоанна",
            "chapter": 2,
            "start_verse": 1,
            "end_verse": 20,
            "verse": "2:1. Начало\n2:20. Конец",
        }

        body = scripture_range_quick_presentation_body(args, "http://127.0.0.1:8091", payload)

        self.assertIsNotNone(body)
        self.assertEqual({"id": "plan-theme"}, body.get("theme"))

    def test_words2numsrus_normalizes_inflected_compound_numbers_safely(self):
        self.assertEqual(
            "в 121 стих",
            normalize_text("в ста двадцати первом стихе"),
        )
        self.assertEqual(
            "в 22 стих",
            normalize_text("в двадцатью двумя стихе"),
        )
        self.assertEqual("семью детьми", normalize_text("семью детьми"))
        self.assertEqual("3 16", normalize_text("три шестнадцать"))

    def test_successfully_shown_long_range_automatically_selects_context(self):
        from tools.vosk_grammar_probe import action_selects_context

        slide = {
            "book": "1 Иоанна",
            "chapter": 2,
            "start_verse": 10,
            "end_chapter": 2,
            "end_verse": 15,
        }

        self.assertTrue(action_selects_context("sent", slide))
        self.assertTrue(action_selects_context("approve", slide))
        self.assertFalse(action_selects_context("waiting", slide))

        pipeline = LiveReferencePipeline()
        self.assertTrue(pipeline.set_context_range(slide))
        result = pipeline.process_text("четырнадцатая стих")
        self.assertEqual("1 Иоанна 2:14", result.get("parsed", {}).get("ref"))
        self.assertEqual("context_range", result.get("source"))

        result = pipeline.process_text("в четырнадцатом стихе")
        self.assertEqual("1 Иоанна 2:14", result.get("parsed", {}).get("ref"))
        self.assertEqual("context_range", result.get("source"))

    def test_short_range_is_not_automatically_selected_as_context(self):
        from tools.vosk_grammar_probe import action_selects_context

        slide = {
            "book": "1 Иоанна",
            "chapter": 2,
            "start_verse": 10,
            "end_chapter": 2,
            "end_verse": 13,
        }

        self.assertFalse(action_selects_context("sent", slide))
        self.assertTrue(action_selects_context("approve_context", slide))

    def test_operator_feedback_keeps_only_unambiguous_training_labels(self):
        from tools.vosk_grammar_probe import approval_action, operator_feedback

        corrected = {
            "approval": {
                "action": "approve_alternative",
                "proposed_ref": "Иаков 3:3",
                "selected_ref": "Притчи 10:3",
            },
            "holyrics": {"ok": True},
        }
        not_citation = {
            "approval": {
                "action": "not_citation",
                "proposed_ref": "Марк 1:1",
                "selected_ref": "",
            }
        }

        self.assertEqual("approve", approval_action(corrected))
        self.assertEqual(
            {
                "action": "approve_alternative",
                "label": "corrected_reference",
                "proposed_ref": "Иаков 3:3",
                "selected_ref": "Притчи 10:3",
            },
            operator_feedback(corrected),
        )
        self.assertEqual("not_a_citation", operator_feedback(not_citation)["label"])
        self.assertIsNone(operator_feedback({"approval": {"action": "skip"}}))

    def test_sermon_plan_candidate_keeps_slide_data_and_operator_wording(self):
        from tools import slide_server

        slide_server.reset_operator_state()
        candidate = slide_server.submit_candidate(
            {
                "ref": "План: слайд 2",
                "verse": "Испытание производит терпение",
                "source": "sermon_plan",
                "slide_index": 1,
                "slide_number": 2,
                "score": 0.73,
            }
        )

        self.assertEqual(1, candidate["slide_index"])
        self.assertEqual(2, candidate["slide_number"])
        self.assertEqual("Пункт плана распознан — ожидает подтверждения", slide_server.operator_state()["processing"]["message"])
        ok, _reason, _candidate = slide_server.decide_candidate("reject")
        self.assertTrue(ok)
        self.assertEqual("Слайд плана отклонён", slide_server.operator_state()["processing"]["message"])

    def test_sermon_plan_verse_uses_quick_text_slide_with_plan_theme(self):
        args = SimpleNamespace(
            sermon_plan=True,
            holyrics_quick_minutes=0.0,
            holyrics_theme="",
            _holyrics_sermon_plan_theme_id="plan-theme",
            _holyrics_sermon_plan_presentation={"type": "text", "text_id": "sermon-plan"},
        )
        payload = {
            "ref": "Иоанн 3:16",
            "verse": "Ибо так возлюбил Бог мир...",
            "book": "Иоанн",
            "chapter": 3,
            "start_verse": 16,
            "end_verse": 16,
        }

        with (
            patch("tools.holyrics.get_holyrics_current_presentation", return_value=None),
            patch("tools.holyrics.post_holyrics_api", return_value=(True, "", "")) as api,
        ):
            ok, reason = post_holyrics_url(args, "http://127.0.0.1:8091", payload)

        self.assertTrue(ok)
        self.assertEqual("show_quick_presentation:sermon_verse;temporary_verse:0min", reason)
        api.assert_called_once_with(
            args,
            "http://127.0.0.1:8091",
            "ShowQuickPresentation",
            {
                "slides": [
                    {
                        "text": "Иоанн 3:16\n\nИбо так возлюбил Бог мир...",
                        "theme": {"id": "plan-theme"},
                    }
                ]
            },
        )

    def test_sermon_plan_verse_restores_actual_current_slide_and_theme(self):
        args = SimpleNamespace(
            sermon_plan=True,
            holyrics_quick_minutes=0.25,
            holyrics_theme="",
            _holyrics_sermon_plan_theme_id="old-theme",
            _holyrics_sermon_plan_presentation={
                "type": "text",
                "text_id": "sermon-plan",
                "slide_number": 4,
                "slides": [{"theme_id": "old-theme"}] * 5,
            },
        )
        current = {
            "type": "text",
            "text_id": "sermon-plan",
            "slide_number": 5,
            "slides": [
                {"theme_id": "theme-1"},
                {"theme_id": "theme-2"},
                {"theme_id": "theme-3"},
                {"theme_id": "theme-4"},
                {"theme_id": "theme-5"},
            ],
        }
        payload = {
            "ref": "Иоанн 3:16",
            "verse": "Ибо так возлюбил Бог мир...",
            "book": "Иоанн",
            "chapter": 3,
            "start_verse": 16,
            "end_verse": 16,
        }

        with (
            patch("tools.holyrics.get_holyrics_current_presentation", return_value=current),
            patch("tools.holyrics.post_holyrics_api", return_value=(True, "", "")) as api,
            patch("tools.holyrics.restore_holyrics_presentation_later") as restore_later,
        ):
            ok, _reason = post_holyrics_url(args, "http://127.0.0.1:8091", payload)

        self.assertTrue(ok)
        api.assert_called_once_with(
            args,
            "http://127.0.0.1:8091",
            "ShowQuickPresentation",
            {
                "slides": [
                    {
                        "text": "Иоанн 3:16\n\nИбо так возлюбил Бог мир...",
                        "theme": {"id": "theme-5"},
                    }
                ]
            },
        )
        restore_snapshot = restore_later.call_args.args[2]
        self.assertEqual(5, restore_snapshot["slide_number"])
        self.assertEqual(4, restore_snapshot["current_index"])
        self.assertEqual(5, args._holyrics_sermon_plan_presentation["slide_number"])
        self.assertEqual("theme-5", args._holyrics_sermon_plan_theme_id)

    def test_sermon_plan_verse_recovers_plan_when_cached_state_is_missing(self):
        args = SimpleNamespace(
            sermon_plan=True,
            holyrics_quick_minutes=0.0,
            holyrics_theme="",
        )
        payload = {
            "ref": "Иоанн 3:16",
            "verse": "Ибо так возлюбил Бог мир...",
            "book": "Иоанн",
            "chapter": 3,
            "start_verse": 16,
            "end_verse": 16,
        }

        with patch(
            "tools.holyrics.get_holyrics_current_presentation",
            return_value={
                "type": "text",
                "text_id": "sermon-plan",
                "slide_number": 1,
                "slides": [
                    {
                        "text": "Ибо так возлюбил Бог мир...",
                        "theme_id": "plan-theme",
                    }
                ],
                "name": "Проповедь",
            },
        ), patch(
            "tools.holyrics.post_holyrics_api",
            return_value=(True, "", ""),
        ) as api:
            ok, reason = post_holyrics_url(args, "http://127.0.0.1:8091", payload)

        self.assertTrue(ok)
        self.assertEqual(
            "show_quick_presentation:sermon_verse;temporary_verse:0min",
            reason,
        )
        self.assertEqual(
            [call(
                args,
                "http://127.0.0.1:8091",
                "ShowQuickPresentation",
                {
                    "slides": [
                        {
                            "text": "Иоанн 3:16\n\nИбо так возлюбил Бог мир...",
                            "theme": {"id": "plan-theme"},
                        }
                    ]
                },
            )],
            api.call_args_list,
        )

    def test_missing_holyrics_permissions_message_lists_exact_permissions(self):
        self.assertEqual(
            "Holyrics: в API token не хватает разрешений: ShowVerse, ActionGoToIndex",
            format_missing_holyrics_permissions(["ShowVerse", "ActionGoToIndex"]),
        )
        self.assertEqual(
            "Holyrics: в API token не хватает разрешения: ShowVerse",
            format_missing_holyrics_permissions(["ShowVerse"]),
        )

    def test_text_plan_restore_does_not_close_presentation_first(self):
        args = SimpleNamespace()
        previous = {
            "type": "text",
            "text_id": "sermon-plan",
            "slide_number": 4,
        }

        with patch("tools.holyrics.post_holyrics_api", return_value=(True, "", "")) as api:
            restore_holyrics_presentation(args, "http://127.0.0.1:8091", previous)

        api.assert_called_once_with(
            args,
            "http://127.0.0.1:8091",
            "ShowText",
            {"id": "sermon-plan", "initial_index": 3},
        )

    def test_long_passage_closes_quick_overlay_before_restoring_plan(self):
        from tools.holyrics import restore_sermon_plan_after_quick_presentation

        args = SimpleNamespace()
        presentation = {"type": "text", "text_id": "sermon-plan"}
        responses = [
            (True, "", '{"status":"ok"}'),
            (True, "", '{"status":"ok","data":null}'),
            (True, "", '{"status":"ok"}'),
            (
                True,
                "",
                '{"status":"ok","data":{"type":"text","id":"sermon-plan","slide_number":3}}',
            ),
        ]

        with (
            patch("tools.holyrics.post_holyrics_api", side_effect=responses) as api,
            patch("tools.holyrics.time.sleep"),
        ):
            ok, reason, diagnostics = restore_sermon_plan_after_quick_presentation(
                args,
                "http://127.0.0.1:8091",
                presentation,
                2,
            )

        self.assertTrue(ok)
        self.assertEqual("sermon_plan_restore_verified", reason)
        self.assertEqual(3, presentation["slide_number"])
        self.assertEqual(
            [
                ("CloseCurrentQuickPresentation", {}),
                ("GetCurrentQuickPresentation", {}),
                ("ShowText", {"id": "sermon-plan", "initial_index": 2}),
                ("GetCurrentPresentation", {}),
            ],
            [(item.args[2], item.args[3]) for item in api.call_args_list],
        )
        self.assertIsNone(diagnostics["quick_states"][0]["data"])
        self.assertEqual("text", diagnostics["presentation_states"][0]["data"]["type"])

    def test_long_passage_retries_quick_close_when_overlay_remains(self):
        from tools.holyrics import restore_sermon_plan_after_quick_presentation

        args = SimpleNamespace()
        presentation = {"type": "text", "text_id": "sermon-plan"}
        responses = [
            (True, "", '{"status":"ok"}'),
            (True, "", '{"status":"ok","data":{"id":"quick","slide_number":2}}'),
            (True, "", '{"status":"ok"}'),
            (True, "", '{"status":"ok","data":null}'),
            (True, "", '{"status":"ok"}'),
            (
                True,
                "",
                '{"status":"ok","data":{"type":"text","id":"sermon-plan","slide_number":1}}',
            ),
        ]

        with (
            patch("tools.holyrics.post_holyrics_api", side_effect=responses) as api,
            patch("tools.holyrics.time.sleep"),
        ):
            ok, reason, diagnostics = restore_sermon_plan_after_quick_presentation(
                args,
                "http://127.0.0.1:8091",
                presentation,
                0,
            )

        self.assertTrue(ok)
        self.assertEqual("sermon_plan_restore_verified", reason)
        self.assertEqual(2, len(diagnostics["close_responses"]))
        self.assertEqual(
            2,
            sum(item.args[2] == "CloseCurrentQuickPresentation" for item in api.call_args_list),
        )

    def test_finished_quick_presentation_still_restores_sermon_plan(self):
        from tools.holyrics import restore_sermon_plan_after_quick_presentation

        args = SimpleNamespace()
        presentation = {"type": "text", "text_id": "sermon-plan"}
        responses = [
            (
                False,
                "holyrics_error:No quick presentation available",
                '{"status":"error","error":"No quick presentation available"}',
            ),
            (True, "", '{"status":"ok"}'),
            (
                True,
                "",
                '{"status":"ok","data":{"type":"text","id":"sermon-plan","slide_number":3}}',
            ),
        ]

        with (
            patch("tools.holyrics.post_holyrics_api", side_effect=responses) as api,
            patch("tools.holyrics.time.sleep"),
        ):
            ok, reason, diagnostics = restore_sermon_plan_after_quick_presentation(
                args,
                "http://127.0.0.1:8091",
                presentation,
                2,
            )

        self.assertTrue(ok)
        self.assertEqual("sermon_plan_restore_verified", reason)
        self.assertEqual(
            [
                "CloseCurrentQuickPresentation",
                "ShowText",
                "GetCurrentPresentation",
            ],
            [item.args[2] for item in api.call_args_list],
        )
        self.assertEqual(1, len(diagnostics["close_responses"]))
        self.assertEqual([], diagnostics["quick_states"])

    def test_context_range_resolves_chapter_and_verse_without_book(self):
        pipeline = LiveReferencePipeline()
        context = pipeline.process_text("первое послание иоанна вторая глава с двенадцатого по семнадцатый стих")
        self.assertEqual("1 Иоанна 2:12-17", context.get("parsed", {}).get("ref"))
        self.assertTrue(pipeline.set_context_range(context))

        result = pipeline.process_text(
            "иоанн завершает этот отрывок удивительными словами семнадцатый стих второй главы"
        )

        self.assertEqual("1 Иоанна 2:17", result.get("parsed", {}).get("ref"))
        self.assertEqual("context_range", result.get("source"))
        self.assertIn("explicit_context_range_reference", result.get("risk_reasons") or [])

    def test_context_range_resolves_bare_verse_inside_current_context_chapter(self):
        pipeline = LiveReferencePipeline()
        context = pipeline.process_text("первое послание иоанна вторая глава с двенадцатого по семнадцатый стих")
        self.assertTrue(pipeline.set_context_range(context))

        result = pipeline.process_text("духовное детство радость спасения двенадцатый стих")

        self.assertEqual("1 Иоанна 2:12", result.get("parsed", {}).get("ref"))
        self.assertEqual("context_range", result.get("source"))

    def test_context_range_resolves_compound_ordinals_above_twenty_as_single_verses(self):
        for verse, ordinal in (
            (21, "двадцать первом"),
            (22, "двадцать втором"),
            (23, "двадцать третьем"),
            (24, "двадцать четвертом"),
            (25, "двадцать пятом"),
            (26, "двадцать шестом"),
        ):
            with self.subTest(verse=verse):
                pipeline = LiveReferencePipeline()
                self.assertTrue(
                    pipeline.set_context_range(
                        {
                            "book": "Иаков",
                            "chapter": 2,
                            "start_verse": 15,
                            "end_chapter": 2,
                            "end_verse": 26,
                        }
                    )
                )

                result = pipeline.process_text(f"в {ordinal} стихе Иаков пишет")

                self.assertEqual(f"Иаков 2:{verse}", result.get("parsed", {}).get("ref"))
                self.assertEqual("context_range", result.get("source"))

    def test_explicit_verse_inside_confirmed_context_is_automatic_in_semi_auto_mode(self):
        from tools.vosk_grammar_probe import add_slide_payload, apply_ml_risk, approval_required_for_payload

        pipeline = LiveReferencePipeline()
        self.assertTrue(
            pipeline.set_context_range(
                {
                    "book": "Иаков",
                    "chapter": 2,
                    "start_verse": 15,
                    "end_chapter": 2,
                    "end_verse": 26,
                }
            )
        )
        asr_result = {
            "text": "в двадцать первом стихе Иаков пишет",
            "result": [
                {"word": "в", "start": 0.0, "end": 0.2, "conf": 0.7},
                {"word": "двадцать", "start": 0.2, "end": 0.7, "conf": 0.55},
                {"word": "первом", "start": 0.7, "end": 1.2, "conf": 0.65},
                {"word": "стихе", "start": 1.2, "end": 1.7, "conf": 0.75},
                {"word": "Иаков", "start": 1.7, "end": 2.2, "conf": 0.6},
                {"word": "пишет", "start": 2.2, "end": 2.7, "conf": 0.7},
            ],
        }
        payload = add_slide_payload(
            pipeline.process_text(asr_result["text"], asr_result=asr_result)
        )
        model = load_risk_model(
            Path(__file__).resolve().parents[1]
            / "src"
            / "bible_parser_core"
            / "data"
            / "risk_model.json"
        )
        args = SimpleNamespace(
            require_approval=False,
            semi_auto_approval=True,
            risk_model_data=model,
            risk_auto_reject_threshold=0.9,
        )

        apply_ml_risk(args, payload, asr_result=asr_result)

        self.assertEqual("Иаков 2:21", payload.get("parsed", {}).get("ref"))
        self.assertEqual(0.4, payload.get("risk_score"))
        self.assertFalse(payload["ml_risk"]["needs_confirmation"])
        self.assertIn(
            "trusted_explicit_context_verse",
            payload["ml_risk"]["decision_reasons"],
        )
        self.assertFalse(approval_required_for_payload(args, payload))

    def test_bare_number_inside_confirmed_context_still_requires_confirmation(self):
        from tools.vosk_grammar_probe import add_slide_payload, apply_ml_risk, approval_required_for_payload

        pipeline = LiveReferencePipeline()
        self.assertTrue(
            pipeline.set_context_range(
                {
                    "book": "Иаков",
                    "chapter": 2,
                    "start_verse": 15,
                    "end_chapter": 2,
                    "end_verse": 26,
                }
            )
        )
        payload = add_slide_payload(pipeline.process_text("21"))
        model = load_risk_model(
            Path(__file__).resolve().parents[1]
            / "src"
            / "bible_parser_core"
            / "data"
            / "risk_model.json"
        )
        args = SimpleNamespace(
            require_approval=False,
            semi_auto_approval=True,
            risk_model_data=model,
            risk_auto_reject_threshold=0.9,
        )

        apply_ml_risk(args, payload)

        self.assertEqual("Иаков 2:21", payload.get("parsed", {}).get("ref"))
        self.assertTrue(payload["ml_risk"]["needs_confirmation"])
        self.assertTrue(approval_required_for_payload(args, payload))

    def test_context_range_repairs_observed_vosk_ordinal_distortions(self):
        for chapter, start_verse, end_verse, text, expected in (
            (4, 10, 17, "в десятом стезе яков пишет", "Иаков 4:10"),
            (4, 10, 17, "в четырнадцатая сессия яков пишет", "Иаков 4:14"),
            (4, 10, 17, "всем нация там стихи", "Иаков 4:17"),
            (4, 10, 17, "в шестнадцать там стихи в пишет", "Иаков 4:16"),
            (4, 10, 17, "в шестнадцатая стейси яков пишет", "Иаков 4:16"),
            (4, 10, 17, "я ещё раз прочитаем шестнадцать тысяч тех", "Иаков 4:16"),
            (2, 1, 15, "во втором стейси иаков пишут", "Иаков 2:2"),
            (2, 1, 15, "в десертом стихи иаков пишет", "Иаков 2:10"),
            (2, 1, 15, "в одиннадцать там стихи яков пишет", "Иаков 2:11"),
            (2, 1, 15, "в девятая сессия и орков пишет", "Иаков 2:9"),
        ):
            with self.subTest(text=text):
                pipeline = LiveReferencePipeline()
                self.assertTrue(
                    pipeline.set_context_range(
                        {
                            "book": "Иаков",
                            "chapter": chapter,
                            "start_verse": start_verse,
                            "end_chapter": chapter,
                            "end_verse": end_verse,
                        }
                    )
                )

                result = pipeline.process_text(text)

                self.assertEqual(expected, result.get("parsed", {}).get("ref"))
                self.assertEqual("context_range", result.get("source"))

    def test_context_range_resolves_spoken_subranges(self):
        from tools.vosk_grammar_probe import action_selects_context, add_slide_payload

        context = {
            "book": "Колоссянам",
            "chapter": 3,
            "start_verse": 6,
            "end_chapter": 3,
            "end_verse": 14,
        }
        for text, expected in (
            ("апостол павел в седьмом восьмом стихе пишет", "Колоссянам 3:7-8"),
            ("прочитаем шестого до седьмого стиха", "Колоссянам 3:6-7"),
            ("прочитаем с шестого до седьмого стиха", "Колоссянам 3:6-7"),
            ("прочитаем шестой седьмой стих", "Колоссянам 3:6-7"),
            ("в девятом и десятом стихи апостол павел пишет", "Колоссянам 3:9-10"),
        ):
            with self.subTest(text=text):
                pipeline = LiveReferencePipeline()
                self.assertTrue(pipeline.set_context_range(context))
                result = pipeline.process_text(text)
                self.assertEqual(expected, result.get("parsed", {}).get("ref"))
                self.assertEqual("context_range", result.get("source"))
                slide = add_slide_payload(result)["slide"]
                self.assertNotIn("can_set_context", slide)
                self.assertFalse(action_selects_context("approve_context", slide))

    def test_active_context_range_beats_stale_reference_for_observed_bare_range(self):
        pipeline = LiveReferencePipeline()
        previous = pipeline.process_text("иоанна три шестнадцать", now_ms=0)
        self.assertEqual("Иоанн 3:16", previous.get("parsed", {}).get("ref"))
        self.assertTrue(
            pipeline.set_context_range(
                {
                    "book": "Иаков",
                    "chapter": 3,
                    "start_verse": 6,
                    "end_chapter": 3,
                    "end_verse": 17,
                }
            )
        )

        pipeline.process_text("прочитаем ещё", now_ms=82_000)
        pipeline.process_text("раз", now_ms=83_000)
        result = pipeline.process_text("шестнадцатый семнадцатый стих", now_ms=84_000)

        self.assertEqual("Иаков 3:16-17", result.get("parsed", {}).get("ref"))
        self.assertEqual("context_range", result.get("source"))

    def test_active_context_range_beats_any_stale_book_for_bare_range(self):
        pipeline = LiveReferencePipeline()
        previous = pipeline.process_text("матфея пятая глава десятый стих", now_ms=0)
        self.assertEqual("Матфей 5:10", previous.get("parsed", {}).get("ref"))
        self.assertTrue(
            pipeline.set_context_range(
                {
                    "book": "Псалтирь",
                    "chapter": 22,
                    "start_verse": 1,
                    "end_chapter": 22,
                    "end_verse": 6,
                }
            )
        )

        result = pipeline.process_text("четвёртый пятый стих", now_ms=88_000)

        self.assertEqual("Псалтирь 22:4-5", result.get("parsed", {}).get("ref"))
        self.assertEqual("context_range", result.get("source"))

    def test_bare_range_still_uses_last_reference_without_active_context(self):
        pipeline = LiveReferencePipeline()
        previous = pipeline.process_text("иоанна третья глава пятнадцатый стих", now_ms=0)
        self.assertEqual("Иоанн 3:15", previous.get("parsed", {}).get("ref"))

        result = pipeline.process_text("шестнадцатый семнадцатый стих", now_ms=88_000)

        self.assertEqual("Иоанн 3:16-17", result.get("parsed", {}).get("ref"))
        self.assertEqual("parser", result.get("source"))

    def test_spoken_split_reference_beats_stale_same_place_range(self):
        pipeline = LiveReferencePipeline()
        previous = pipeline.process_text("иоанна три шестнадцать", now_ms=0)
        self.assertEqual("Иоанн 3:16", previous.get("parsed", {}).get("ref"))

        prefix = pipeline.process_text("иаково третья глава", now_ms=110_000)
        self.assertIsNone(prefix.get("parsed"))
        result = pipeline.process_text("пятый пятнадцатый стих", now_ms=111_500)

        self.assertEqual("Иаков 3:5-15", result.get("parsed", {}).get("ref"))
        self.assertEqual("parser", result.get("source"))

    def test_spoken_nehemiah_reference_beats_stale_ezra_context(self):
        pipeline = LiveReferencePipeline()
        previous = pipeline.process_text(
            "книга пророка ездры четвёртая глава третий четвёртый стих",
            now_ms=0,
        )
        self.assertEqual("Ездра 4:3-4", previous.get("parsed", {}).get("ref"))

        book = pipeline.process_text("книга пророка ниеми", now_ms=110_000)
        chapter = pipeline.process_text("восьмая глава", now_ms=111_000)
        result = pipeline.process_text("седьмой восьмой стих", now_ms=112_000)

        self.assertIsNone(book.get("parsed"))
        self.assertIsNone(chapter.get("parsed"))
        self.assertEqual("Неемия 8:7-8", result.get("parsed", {}).get("ref"))
        self.assertEqual("parser", result.get("source"))

    def test_observed_i_okolo_asr_distortion_resolves_to_james(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "послание и около четвёртую главу "
            "с пятнадцатого стиха и до конца главы"
        )

        self.assertEqual("Иаков 4:15-17", result.get("parsed", {}).get("ref"))

    def test_one_chapter_book_rejects_explicit_impossible_chapter(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "послание к филимону четвёртую главу "
            "с пятнадцатого стиха и до конца главы"
        )

        self.assertFalse(result.get("matched"))
        self.assertIsNone(result.get("parsed"))

    def test_long_context_subrange_does_not_replace_main_context(self):
        from tools.vosk_grammar_probe import action_selects_context, add_slide_payload

        pipeline = LiveReferencePipeline()
        self.assertTrue(
            pipeline.set_context_range(
                {
                    "book": "Колоссянам",
                    "chapter": 3,
                    "start_verse": 1,
                    "end_chapter": 3,
                    "end_verse": 20,
                }
            )
        )

        payload = pipeline.process_text("прочитаем с пятого по девятый стих")
        slide = add_slide_payload(payload)["slide"]

        self.assertEqual("Колоссянам 3:5-9", slide["ref"])
        self.assertNotIn("can_set_context", slide)
        self.assertFalse(action_selects_context("approve", slide))
        self.assertFalse(action_selects_context("approve_context", slide))

    def test_context_range_does_not_treat_compact_chapter_verse_as_subrange(self):
        pipeline = LiveReferencePipeline()
        self.assertTrue(
            pipeline.set_context_range(
                {
                    "book": "Иоанн",
                    "chapter": 3,
                    "start_verse": 1,
                    "end_chapter": 3,
                    "end_verse": 20,
                }
            )
        )

        result = pipeline.process_text("три шестнадцать стих")

        self.assertEqual("Иоанн 3:16", result.get("parsed", {}).get("ref"))
        self.assertEqual("context_range", result.get("source"))

    def test_context_range_does_not_override_explicit_other_book(self):
        pipeline = LiveReferencePipeline()
        context = pipeline.process_text("первое послание иоанна вторая глава с двенадцатого по семнадцатый стих")
        self.assertTrue(pipeline.set_context_range(context))

        result = pipeline.process_text("евангелие от иоанна второй главы семнадцатый стих")

        self.assertEqual("Иоанн 2:17", result.get("parsed", {}).get("ref"))
        self.assertNotEqual("context_range", result.get("source"))

    def test_context_range_yields_to_any_explicit_full_address(self):
        from tools.vosk_grammar_probe import add_slide_payload

        for text, expected in (
            ("бытие десятая глава с третьего по четвёртый стих", "Бытие 10:3-4"),
            ("притчи десятая глава с третьего по седьмой стих", "Притчи 10:3-7"),
            ("евангелие от матфея пятая глава с первого по второй стих", "Матфей 5:1-2"),
            ("римлянам восьмая глава с первого по третий стих", "Римлянам 8:1-3"),
            ("евреям одиннадцатая глава с первого по второй стих", "Евреям 11:1-2"),
            ("второе послание тимофею третья глава с первого по второй стих", "2 Тимофею 3:1-2"),
            ("откровение вторая глава с первого по третий стих", "Откровение 2:1-3"),
            ("иакова вторая глава с первого по второй стих", "Иаков 2:1-2"),
        ):
            with self.subTest(text=text):
                pipeline = LiveReferencePipeline()
                context = pipeline.process_text("послания якова первая глава с первого по десятое стих")
                self.assertTrue(pipeline.set_context_range(context))

                result = pipeline.process_text(text)
                slide = add_slide_payload(result)["slide"]

                self.assertEqual(expected, result.get("parsed", {}).get("ref"))
                self.assertEqual("parser", result.get("source"))
                if expected == "Притчи 10:3-7":
                    self.assertTrue(slide.get("can_set_context"))

    def test_context_range_yields_to_explicit_reference_split_between_chunks(self):
        pipeline = LiveReferencePipeline()
        context = pipeline.process_text("послание иакова третья глава с десятого по восемнадцатый стих")
        self.assertTrue(pipeline.set_context_range(context))

        first = pipeline.process_text("книга русь третья глава")
        second = pipeline.process_text("десятый одиннадцатый стих")

        self.assertFalse(first.get("matched"))
        self.assertEqual("Руфь 3:10-11", second.get("parsed", {}).get("ref"))
        self.assertEqual("parser", second.get("source"))

    def assert_book_only_fragment_does_not_reuse_previous_numbers(self, fragment):
        with self.subTest(fragment=fragment):
            pipeline = LiveReferencePipeline()

            first = pipeline.process_text("иоана три шестнадцать")
            self.assertEqual("Иоанн 3:16", first.get("parsed", {}).get("ref"))

            second = pipeline.process_text(fragment)
            self.assertFalse(second.get("matched"))
            self.assertEqual([fragment], second.get("vosk_buffer"))

    def test_bare_book_fragment_does_not_reuse_previous_numbers(self):
        for fragment in (
            "матфей",
            "паралипоменон",
            "коринфянам",
            "петра",
            "фессалоникийцам",
            "царств",
        ):
            self.assert_book_only_fragment_does_not_reuse_previous_numbers(fragment)

    def test_bare_book_fragment_can_start_next_reference(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text("матфей")
        self.assertFalse(first.get("matched"))
        self.assertEqual(["матфей"], first.get("vosk_buffer"))

        second = pipeline.process_text("третья глава шестнадцатый стих")
        self.assertEqual("Матфей 3:16", second.get("parsed", {}).get("ref"))

    def test_bare_book_fragment_can_start_short_numeric_reference(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text("лука")
        self.assertFalse(first.get("matched"))
        self.assertEqual(["лука"], first.get("vosk_buffer"))

        second = pipeline.process_text("четырнадцать двадцать восемь тридцать")
        self.assertEqual("Лука 14:28-30", second.get("parsed", {}).get("ref"))

    def test_old_book_fragment_does_not_survive_buffer_timeout(self):
        pipeline = LiveReferencePipeline(buffer_window_ms=2000)

        pipeline.process_text("навин", now_ms=0)
        chapter = pipeline.process_text("четвёртая глава", now_ms=21000)
        verse = pipeline.process_text("семнадцатого по девятнадцатый стих", now_ms=21700)

        self.assertTrue(chapter.get("buffer_reset_by_gap"))
        self.assertFalse(verse.get("matched"))
        self.assertNotIn("навин", verse.get("vosk_buffer") or [])

    def test_philippians_short_grammar_alias(self):
        pipeline = LiveReferencePipeline()

        for text in (
            "послание фил вторая глава пятый стих",
            "послание фи лип вторая глава пятый стих",
            "послание фи лип пи вторая глава пятый стих",
            "послание фи лип пи царств вторая глава пятый стих",
            "послание филип вторая глава пятый стих",
            "послание филипп вторая глава пятый стих",
        ):
            with self.subTest(text=text):
                result = pipeline.process_text(text)

                self.assertEqual("Филиппийцам 2:5", result.get("parsed", {}).get("ref"))

        grammar = build_grammar()
        self.assertIn("фи лип пи царств", grammar)
        self.assertIn("послание фи лип пи царств", grammar)

    def test_philippians_fi_levit_asr_distortion(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("послание фи левит первая глава седьмой восьмой стих")

        self.assertEqual("Филиппийцам 1:7-8", result.get("parsed", {}).get("ref"))

    def test_philemon_safe_grammar_alias(self):
        pipeline = LiveReferencePipeline()

        for text in (
            "послание фи лимон первая глава одиннадцатый двенадцатый стих",
            "послание фи мона первая глава одиннадцатый двенадцатый стих",
            "послание фи мону первая глава одиннадцатый двенадцатый стих",
            "послание филимон первая глава одиннадцатый двенадцатый стих",
        ):
            with self.subTest(text=text):
                result = pipeline.process_text(text)

                self.assertEqual("Филимону 1:11-12", result.get("parsed", {}).get("ref"))

    def test_ambiguous_fi_abbreviation_does_not_select_a_book(self):
        pipeline = LiveReferencePipeline()

        for text in (
            "фи первая глава одиннадцатый двенадцатый стих",
            "послание фи первая глава одиннадцатый двенадцатый стих",
            "послание фес одиннадцатый двенадцатые стих первое главы",
        ):
            with self.subTest(text=text):
                result = pipeline.process_text(text)

                self.assertFalse(result.get("matched"))
                self.assertIsNone(result.get("parsed"))

    def test_missing_vosk_book_names_have_safe_split_aliases(self):
        pipeline = LiveReferencePipeline()

        for text, expected in (
            ("книга не ем и я вторая глава первый стих", "Неемия 2:1"),
            ("не ем и я вторая глава первый стих", "Неемия 2:1"),
            ("не михея вторая глава первый стих", "Неемия 2:1"),
            ("книга ио иль вторая глава первый стих", "Иоиль 2:1"),
            ("пророка ио иль вторая глава первый стих", "Иоиль 2:1"),
            ("книга со фон и я третья глава первый стих", "Софония 3:1"),
            ("пророка со фон и я третья глава первый стих", "Софония 3:1"),
            ("книга михея первая глава первый стих", "Михей 1:1"),
        ):
            with self.subTest(text=text):
                result = pipeline.process_text(text)

                self.assertEqual(expected, result.get("parsed", {}).get("ref"))

    def test_ephesians_safe_grammar_aliases(self):
        pipeline = LiveReferencePipeline()

        for text in (
            "послание еф вторая глава девятый десятый стих",
            "послание ефес вторая глава девятый десятый стих",
            "послание е фес вторая глава девятый десятый стих",
            "послание ефес нам вторая глава девятый десятый стих",
            "послание вся на вторая глава девятый десятый стих",
            "послание и вся на вторая глава девятый десятый стих",
        ):
            with self.subTest(text=text):
                result = pipeline.process_text(text)

                self.assertEqual("Ефесянам 2:9-10", result.get("parsed", {}).get("ref"))

    def test_numbered_fes_still_resolves_to_thessalonians(self):
        pipeline = LiveReferencePipeline()

        for text, expected in (
            ("первое фес первая глава третий стих", "1 Фессалоникийцам 1:3"),
            ("первое фес салон первая глава третий стих", "1 Фессалоникийцам 1:3"),
            ("первое фесс салоники первая глава третий стих", "1 Фессалоникийцам 1:3"),
            ("первое фес салоники царств первая глава третий стих", "1 Фессалоникийцам 1:3"),
            ("первое фесс салоники царств первая глава третий стих", "1 Фессалоникийцам 1:3"),
            ("второе фес салон вторая глава первый стих", "2 Фессалоникийцам 2:1"),
            ("второе послание фесс салоник вторая глава первый стих", "2 Фессалоникийцам 2:1"),
            ("второе фес салоники царств вторая глава первый стих", "2 Фессалоникийцам 2:1"),
            ("второе фесс салоники царств вторая глава первый стих", "2 Фессалоникийцам 2:1"),
        ):
            with self.subTest(text=text):
                result = pipeline.process_text(text)

                self.assertEqual(expected, result.get("parsed", {}).get("ref"))

        grammar = build_grammar()
        self.assertIn("первое фес салоники царств", grammar)
        self.assertIn("второе фес салоники царств", grammar)
        self.assertIn("первое фесс", grammar)
        self.assertIn("второе фесс", grammar)

    def test_unnumbered_fes_saloniki_does_not_resolve_to_ephesians(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("фес салоники четвёртая глава девятые десятая стих")

        self.assertFalse(result.get("matched"))
        self.assertEqual("ambiguous_unnumbered_thessalonians", result.get("blocked_weak_context"))
        self.assertIn("Номер книги не был назван", result.get("message", ""))

        result = pipeline.process_text("фес салоники царств первая глава третий стих")

        self.assertFalse(result.get("matched"))
        self.assertEqual("ambiguous_unnumbered_thessalonians", result.get("blocked_weak_context"))
        self.assertIn("Номер книги не был назван", result.get("message", ""))

    def test_spoken_first_corinthians_reference(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("первое коринфянам вторая глава шестнадцатый стих")

        self.assertEqual("1 Коринфянам 2:16", result.get("parsed", {}).get("ref"))

    def test_short_first_john_with_single_n_asr_variant(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("первое иоана три два")

        self.assertEqual("1 Иоанна 3:2", result.get("parsed", {}).get("ref"))

        result = pipeline.process_text("первое иоана четыре восемнадцать")

        self.assertEqual("1 Иоанна 4:18", result.get("parsed", {}).get("ref"))

    def test_numbered_yana_epistle_keeps_spoken_book_number(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("второе послание яна первое глава четвёртую стих")

        self.assertEqual("2 Иоанна 1:4", result.get("parsed", {}).get("ref"))

    def test_split_john_alias(self):
        pipeline = LiveReferencePipeline()

        gospel = pipeline.process_text("евангелие от и о анна три шестнадцать")
        epistle = pipeline.process_text("первое послание и о анна пятая глава тринадцатый стих")

        self.assertEqual("Иоанн 3:16", gospel.get("parsed", {}).get("ref"))
        self.assertEqual("1 Иоанна 5:13", epistle.get("parsed", {}).get("ref"))

    def test_john_3_16_does_not_require_ml_confirmation_when_clean(self):
        pipeline = LiveReferencePipeline()
        model = load_risk_model(
            Path(__file__).resolve().parents[1]
            / "src"
            / "bible_parser_core"
            / "data"
            / "risk_model.json"
        )

        result = pipeline.process_text(
            "иоанна три шестнадцать",
            asr_result={
                "text": "иоанна три шестнадцать",
                "result": [
                    {"word": "иоанна", "start": 0.0, "end": 0.5, "conf": 1.0},
                    {"word": "три", "start": 0.5, "end": 0.8, "conf": 1.0},
                    {"word": "шестнадцать", "start": 0.8, "end": 1.4, "conf": 1.0},
                ],
            },
        )
        ml_risk = score_payload_with_model(result, model)

        self.assertEqual("Иоанн 3:16", result.get("parsed", {}).get("ref"))
        self.assertFalse(ml_risk.get("needs_confirmation"))
        self.assertIn("trusted_john_3_16", ml_risk.get("decision_reasons"))

    def test_nonexistent_first_corinthians_verse_does_not_match(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("первое коринфянам вторая глава двадцать пятый стих")

        self.assertFalse(result.get("matched"))
        self.assertEqual("invalid_verse", result.get("invalid_reference", {}).get("reason"))
        self.assertEqual("1 Коринфянам 2:25", result.get("invalid_reference", {}).get("ref"))
        self.assertIn("Такого стиха нет", result.get("message", ""))

    def test_invalid_reversed_range_does_not_fall_back_to_first_existing_verse(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("двадцатый двадцать второе стих шестой главы послание евреям")

        self.assertFalse(result.get("matched"))
        self.assertEqual("invalid_verse", result.get("invalid_reference", {}).get("reason"))
        self.assertEqual("Евреям 6:20-22", result.get("invalid_reference", {}).get("ref"))
        self.assertIn("Такого стиха нет", result.get("message", ""))

    def test_command_suffix_overrides_incomplete_epistle_prefix(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("первое послание к читаем бытие третья глава шестой стих")

        self.assertEqual("Бытие 3:6", result.get("parsed", {}).get("ref"))

    def test_complete_epistle_reference_still_works(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("первое послание петра третья глава шестой стих")

        self.assertEqual("1 Петра 3:6", result.get("parsed", {}).get("ref"))

    def test_gospel_without_book_name_does_not_create_reference(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("евангелие от пятнадцать тринадцать откройте")

        self.assertFalse(result.get("matched"))
        self.assertEqual("gospel_without_book_name", result.get("blocked_weak_context"))

    def test_gospel_with_book_name_still_works(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("евангелие от иоанна пятнадцать тринадцать")

        self.assertEqual("Иоанн 15:13", result.get("parsed", {}).get("ref"))

    def test_gospel_history_year_does_not_create_reference(self):
        pipeline = LiveReferencePipeline()

        history = pipeline.process_text(
            "первыми появились послания потому что евангелие самое первое евангелие "
            "это евангелие от марка нам было написании где-то шестидесятый "
            "шестьдесят пятый год поражеству христово то есть"
        )
        explicit = pipeline.process_text("евангелие от марка первая глава первый стих")

        self.assertFalse(history.get("matched"))
        self.assertEqual(
            "gospel_history_year_not_reference",
            history.get("blocked_weak_context"),
        )
        self.assertEqual("Марк 1:1", explicit.get("parsed", {}).get("ref"))

    def test_gospel_book_conflict_does_not_auto_match(self):
        pipeline = LiveReferencePipeline()

        distorted = pipeline.process_text("евангелие от матфея два вторая глава двадцать девятой стихов")
        explicit = pipeline.process_text("евангелие от матфея двадцать вторая глава двадцать девятый стих")

        self.assertFalse(distorted.get("matched"))
        self.assertEqual("gospel_book_conflict", distorted.get("blocked_weak_context"))
        self.assertEqual("Матфей 22:29", explicit.get("parsed", {}).get("ref"))

    def test_prophet_book_chapter_without_verse_does_not_create_epistle_reference(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("послание второе книга пророка иеремии восьмая глава")

        self.assertFalse(result.get("matched"))
        self.assertEqual("prophet_book_chapter_without_verse", result.get("blocked_weak_context"))

    def test_prophet_book_with_verse_still_works(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("книга пророка иеремии восьмая глава первый стих")

        self.assertEqual("Иеремия 8:1", result.get("parsed", {}).get("ref"))

    def test_vosk_grammar_contains_range_words_with_yo_forms(self):
        grammar = set(build_grammar())

        self.assertIn("по", grammar)
        self.assertIn("слова", grammar)
        self.assertIn("четвёртого", grammar)
        self.assertIn("четвёртом", grammar)
        self.assertIn("четвёртая", grammar)
        self.assertNotIn("четвертом", grammar)
        self.assertNotIn("сотом", grammar)
        self.assertIn("следующей", grammar)
        self.assertIn("следующий", grammar)

    def test_sermon_plan_grammar_and_ordered_match(self):
        from bible_parser_core.live_pipeline import match_sermon_plan_slide, sermon_plan_grammar_phrases

        slides = [
            {"text": "Тема сегодняшней проповеди\nЖизнь с избытком."},
            {"text": "1. Сегодня мы с вами будем читать из книги пророка Исайя"},
            {"text": "2. Затем прочитаем из Евангелия от Иоанна 3 глава 16 стих."},
            {"text": ""},
        ]

        grammar = sermon_plan_grammar_phrases(slides)
        self.assertIn("сегодня", grammar)
        self.assertIn("затем прочитаем из евангелия от иоанна глава стих", grammar)

        match = match_sermon_plan_slide(
            slides,
            ["затем прочитаем из евангелия от иоанна третья глава шестнадцатый стих"],
            current_index=1,
        )
        self.assertIsNotNone(match)
        self.assertEqual(3, match["slide_number"])

    def test_sermon_plan_matches_text_line_without_standalone_reference(self):
        from bible_parser_core.live_pipeline import match_sermon_plan_slide, sermon_plan_grammar_phrases

        slides = [
            {"text": "Тема демо-проповеди: Жизнь с избытком"},
            {"text": "1. Бог даёт человеку настоящую жизнь.\nИоанна 10:10"},
            {"text": "2. Грех лишает человека полноты и мира.\nРимлянам 3:23"},
        ]

        grammar = sermon_plan_grammar_phrases(slides)
        self.assertIn("даёт", grammar)
        filtered_grammar = sermon_plan_grammar_phrases(slides, lambda word: word != "даёт")
        self.assertFalse(any("даёт" in phrase.split() for phrase in filtered_grammar))

        match = match_sermon_plan_slide(
            slides,
            ["бог человеку настоящую жизнь"],
            current_index=1,
        )
        self.assertIsNotNone(match)
        self.assertEqual(2, match["slide_number"])

        reference_only = match_sermon_plan_slide(
            slides,
            ["иоанна десять десять"],
            current_index=1,
        )
        self.assertIsNone(reference_only)

    def test_sermon_plan_matches_demo_recognition_in_order(self):
        from bible_parser_core.live_pipeline import match_sermon_plan_slide

        slides = [
            {"text": "Тема демо-проповеди: Жизнь с избытком"},
            {"text": "1. Бог даёт человеку настоящую жизнь.\nИоанна 10:10"},
            {"text": "2. Грех лишает человека полноты и мира.\nРимлянам 3:23"},
            {"text": "3. Бог показал Свою любовь во Христе.\nИоанна 3:16"},
            {"text": "4. Христос пришёл, чтобы спасти и обновить.\nИоанна 12:47"},
            {"text": "5. Новая жизнь начинается с веры и послушания.\nГалатам 2:20"},
            {"text": "Заключение: примем Божий дар и будем жить для Его славы."},
        ]
        recognized = [
            "тема демо проповеди жизнь с избытком",
            "бог человеку настоящую жизнь",
            "грех лишает человека полноты и мира",
            "бог показал свою любовь во христе",
            "христос чтобы спасти и обновить",
            "новая жизнь начинается с веры и послушания",
            "заключение примем божий дар и будем жить для его для его славы",
        ]

        next_index = 0
        for expected_slide_number, candidate in enumerate(recognized, start=1):
            match = match_sermon_plan_slide(slides, [candidate], current_index=next_index)
            self.assertIsNotNone(match, candidate)
            self.assertEqual(expected_slide_number, match["slide_number"])
            next_index = int(match["slide_index"]) + 1

    def test_sermon_plan_does_not_jump_far_forward(self):
        from bible_parser_core.live_pipeline import match_sermon_plan_slide

        slides = [
            {"text": "Первая достаточно длинная строка плана"},
            {"text": "Вторая достаточно длинная строка плана"},
            {"text": "Третья достаточно длинная строка плана"},
            {"text": "Четвёртая далёкая строка плана проповеди"},
        ]

        match = match_sermon_plan_slide(
            slides,
            ["четвертая далекая строка плана проповеди"],
            current_index=0,
            lookahead=2,
        )
        self.assertIsNone(match)

    def test_sermon_plan_ignores_ordinary_sermon_words(self):
        from bible_parser_core.live_pipeline import match_sermon_plan_slide

        slides = [
            {"text": "Бог даёт человеку настоящую жизнь"},
            {"text": "Грех лишает человека полноты и мира"},
        ]
        match = match_sermon_plan_slide(
            slides, ["бог хочет чтобы человек жил в мире"], current_index=0
        )
        self.assertIsNone(match)

    def test_sermon_plan_approval_match_accepts_vosk_word_endings(self):
        from bible_parser_core.live_pipeline import match_sermon_plan_slide

        slides = [{"text": "Испытание производит терпение"}]
        recognized = "ключевые слова якому из производит терпения"

        strict_match = match_sermon_plan_slide(slides, [recognized], current_index=0)
        approval_match = match_sermon_plan_slide(
            slides,
            [recognized],
            current_index=0,
            threshold=0.52,
            min_content_words=2,
            min_target_coverage=0.35,
        )

        self.assertIsNone(strict_match)
        self.assertIsNotNone(approval_match)
        self.assertEqual(1, approval_match["slide_number"])

    def test_sermon_plan_allows_only_strong_return_to_skipped_slide(self):
        from bible_parser_core.live_pipeline import match_sermon_plan_slide

        slides = [
            {"text": "Бог даёт человеку настоящую жизнь"},
            {"text": "Грех лишает человека полноты и мира"},
            {"text": "Христос пришёл чтобы спасти и обновить"},
        ]
        match = match_sermon_plan_slide(
            slides, ["грех лишает человека полноты и мира"], current_index=2
        )
        self.assertIsNotNone(match)
        self.assertEqual(2, match["slide_number"])
        self.assertTrue(match["backtrack"])

    def test_sermon_plan_can_restart_from_first_after_last_slide(self):
        from bible_parser_core.live_pipeline import match_sermon_plan_slide

        slides = [
            {"text": "Тема демо-проповеди: Жизнь с избытком"},
            {"text": "Первый достаточно длинный пункт проповеди"},
            {"text": "Заключение: примем Божий дар и будем жить для Его славы."},
            {"text": ""},
        ]

        match = match_sermon_plan_slide(
            slides,
            ["тема демо проповеди жизнь избытком"],
            current_index=2,
        )

        self.assertIsNotNone(match)
        self.assertEqual(1, match["slide_number"])

    def test_slow_split_deuteronomy_range_with_yo_form(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(
            pipeline.process_text("из книги второзаконие двадцать седьмая глава", now_ms=1_000).get("matched")
        )
        result = pipeline.process_text("с двадцать четвёртого по двадцать шестой стих", now_ms=2_000)

        self.assertEqual("Второзаконие 27:24-26", result.get("parsed", {}).get("ref"))

    def test_noise_does_not_report_invalid_reference(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("коринфянам просто параллельно")

        self.assertFalse(result.get("matched"))
        self.assertIsNone(result.get("invalid_reference"))

    def test_gospel_phrase_in_noisy_context_can_start_next_reference(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text("числа откроем евангелие от матфея")
        self.assertFalse(first.get("matched"))

        second = pipeline.process_text("восьмая глава первого пятые стих")
        self.assertEqual("Матфей 8:1-5", second.get("parsed", {}).get("ref"))

    def test_slow_split_reference_accumulates_inside_time_window(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(
            pipeline.process_text("давайте откроем евангелие от матфея", now_ms=1_000).get("matched")
        )
        self.assertFalse(pipeline.process_text("восьмая глава", now_ms=2_100).get("matched"))
        third = pipeline.process_text("с первого", now_ms=3_000)
        self.assertFalse(third.get("matched"))
        self.assertEqual("incomplete_first_verse_after_chapter", third.get("blocked_weak_context"))
        self.assertTrue(third.get("buffer_kept_for_open_range"))

        fourth = pipeline.process_text("по пятый стих", now_ms=4_000)
        self.assertEqual("Матфей 8:1-5", fourth.get("parsed", {}).get("ref"))
        self.assertFalse(fourth.get("buffer_reset_by_gap"))

    def test_slow_split_epistle_reference_uses_explicit_context(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("читаем", now_ms=1_000).get("matched"))
        self.assertFalse(pipeline.process_text("первое послание ефесянам", now_ms=2_000).get("matched"))
        result = pipeline.process_text("вторая глава девятая десятая стих", now_ms=3_000)

        self.assertEqual("Ефесянам 2:9-10", result.get("parsed", {}).get("ref"))

    def test_slow_split_numbered_epistle_reference_uses_explicit_context(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("читаем второе тимофею", now_ms=1_000).get("matched"))
        self.assertFalse(pipeline.process_text("вторая глава", now_ms=2_000).get("matched"))
        result = pipeline.process_text("девятнадцатый двадцать первое стих", now_ms=3_000)

        self.assertEqual("2 Тимофею 2:19-21", result.get("parsed", {}).get("ref"))

    def test_split_open_range_without_po_uses_explicit_context(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(
            pipeline.process_text("первое послание коринфянам третья глава", now_ms=1_000).get("matched")
        )
        result = pipeline.process_text("девятого двадцатую стих", now_ms=2_000)

        self.assertEqual("1 Коринфянам 3:9-20", result.get("parsed", {}).get("ref"))

    def test_ambiguous_timothy_without_number_does_not_auto_match(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("тимофею третья глава четвёртого по пятой стих")

        self.assertFalse(result.get("matched"))
        self.assertEqual("ambiguous_numbered_timothy", result.get("blocked_weak_context"))

    def test_timothy_text_does_not_resolve_to_john(self):
        pipeline = LiveReferencePipeline()

        pipeline.process_text("первого послания тимофею", now_ms=1_000)
        pipeline.process_text("восьмую стих", now_ms=2_000)
        result = pipeline.process_text(
            "откройте послания тимофею первое тимофею пятую",
            now_ms=3_000,
        )

        self.assertFalse(result.get("matched"))
        self.assertEqual("resolver_conflicts_with_timothy", result.get("blocked_weak_context"))

    def test_explicit_numbered_timothy_still_works(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text("первое тимофею третья глава четвёртого по пятой стих")
        second = pipeline.process_text("второе тимофею третья глава четвёртого по пятой стих")

        self.assertEqual("1 Тимофею 3:4-5", first.get("parsed", {}).get("ref"))
        self.assertEqual("2 Тимофею 3:4-5", second.get("parsed", {}).get("ref"))

    def test_numbered_epistle_with_poslanie_does_not_use_book_number_as_chapter(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("второе послание коринфянам пятого восемнадцатый стих")

        self.assertEqual("2 Коринфянам 5:18", result.get("parsed", {}).get("ref"))

    def test_numbered_corinthians_chapter_only_does_not_become_john(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("первого послания коринфянам шестая глава")

        self.assertFalse(result.get("matched"))
        self.assertIsNone(result.get("parsed"))

    def test_split_reference_uses_asr_word_timestamps_for_buffer_gap(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text(
            "первого послания коринфянам шестая глава",
            now_ms=1_068_250,
            asr_result={
                "result": [
                    {"start": 1066.03, "end": 1066.27, "word": "первого"},
                    {"start": 1066.27, "end": 1066.66, "word": "послания"},
                    {"start": 1066.66, "end": 1067.11, "word": "коринфянам"},
                    {"start": 1067.11, "end": 1067.4279, "word": "шестая"},
                    {"start": 1067.44, "end": 1067.8, "word": "глава"},
                ],
                "text": "первого послания коринфянам шестая глава",
            },
        )
        self.assertFalse(first.get("matched"))

        result = pipeline.process_text(
            "девятнадцатый двадцатая стих",
            now_ms=1_071_250,
            asr_result={
                "result": [
                    {"start": 1069.36, "end": 1069.96, "word": "девятнадцатый"},
                    {"start": 1069.96, "end": 1070.44, "word": "двадцатая"},
                    {"start": 1070.44, "end": 1070.74, "word": "стих"},
                ],
                "text": "девятнадцатый двадцатая стих",
            },
        )

        self.assertEqual("1 Коринфянам 6:19-20", result.get("parsed", {}).get("ref"))
        self.assertEqual("asr_words", result.get("delta_source"))
        self.assertLess(result.get("delta_ms"), 2_000)

    def test_suspicious_feminine_first_stich_does_not_auto_match(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("послание ефесянам третью", now_ms=1_000).get("matched"))
        result = pipeline.process_text("первую стих", now_ms=2_000)

        self.assertFalse(result.get("matched"))
        self.assertEqual("suspicious_first_verse_form", result.get("blocked_weak_context"))

    def test_incomplete_first_verse_after_chapter_waits_for_range(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text("откроем первая яна вторая глава первого", now_ms=1_000)
        self.assertFalse(first.get("matched"))
        self.assertEqual("incomplete_first_verse_after_chapter", first.get("blocked_weak_context"))

        result = pipeline.process_text("по шестой стих", now_ms=2_000)
        self.assertEqual("1 Иоанна 2:1-6", result.get("parsed", {}).get("ref"))

    def test_genitive_ordinal_after_chapter_waits_for_range_end(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("евангелие от луки двадцать четвёртая глава тринадцатого")

        self.assertFalse(result.get("matched"))
        self.assertEqual("incomplete_range_start_after_chapter", result.get("blocked_weak_context"))

    def test_genitive_ordinal_verse_after_chapter_waits_for_range_end(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("евангелие от иоанна третья глава шестнадцатого стих")

        self.assertFalse(result.get("matched"))
        self.assertEqual("incomplete_range_start_after_chapter", result.get("blocked_weak_context"))

        single_verse = pipeline.process_text("евангелие от иоанна третья глава шестнадцатый стих")

        self.assertEqual("Иоанн 3:16", single_verse.get("parsed", {}).get("ref"))

    def test_from_genitive_ordinal_after_chapter_waits_for_range_end(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text("ефесянам шестая глава с восьмого", now_ms=1_000)

        self.assertFalse(first.get("matched"))
        self.assertEqual("incomplete_range_start_after_chapter", first.get("blocked_weak_context"))

        result = pipeline.process_text("по девятый стих", now_ms=2_000)

        self.assertEqual("Ефесянам 6:8-9", result.get("parsed", {}).get("ref"))

    def test_range_fragment_ending_with_po_waits_for_end_verse(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("книга откровений третья глава первого по")

        self.assertFalse(result.get("matched"))
        self.assertEqual("incomplete_range_end_after_po", result.get("blocked_weak_context"))

    def test_complete_range_after_po_still_matches(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("книга откровений третья глава первого по шестой стих")

        self.assertEqual("Откровение 3:1-6", result.get("parsed", {}).get("ref"))

    def test_cross_chapter_range(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "евангелие от иоанна третья глава с шестнадцатого стиха до четвёртой главы второго стиха"
        )
        asr_variant = pipeline.process_text(
            "евангелие от иоанна третье шестнадцатого стиха два второго стиха четвёртые главы"
        )
        reversed_end = pipeline.process_text(
            "евангелие от иоанна третья глава с шестнадцатого стиха до второго стиха четвёртой главы"
        )
        next_chapter = pipeline.process_text(
            "евангелие от иоанна третья глава с шестнадцатого стиха и до второго стиха следующей главы"
        )
        next_chapter_without_start_verse_word = pipeline.process_text(
            "евангелие от иоанна третья глава с шестнадцатого и до второго стиха следующей главы"
        )
        next_chapter_without_from = pipeline.process_text(
            "евангелие от иоанна третья глава шестнадцатого до второго стиха следующей главы"
        )
        compact = pipeline.process_text("иоана три шестнадцатая четыре два")

        self.assertEqual("Иоанн 3:16-4:2", result.get("parsed", {}).get("ref"))
        self.assertEqual(4, result.get("parsed", {}).get("end_chapter"))
        self.assertEqual("Иоанн 3:16-4:2", asr_variant.get("parsed", {}).get("ref"))
        self.assertEqual("Иоанн 3:16-4:2", reversed_end.get("parsed", {}).get("ref"))
        self.assertEqual("Иоанн 3:16-4:2", next_chapter.get("parsed", {}).get("ref"))
        self.assertEqual("Иоанн 3:16-4:2", next_chapter_without_start_verse_word.get("parsed", {}).get("ref"))
        self.assertEqual("Иоанн 3:16-4:2", next_chapter_without_from.get("parsed", {}).get("ref"))
        self.assertEqual("Иоанн 3:16-4:2", compact.get("parsed", {}).get("ref"))

    def test_cross_chapter_range_builds_quick_presentation_slides(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "евангелие от иоанна третья глава с шестнадцатого стиха до четвёртой главы второго стиха"
        )
        slides = cross_chapter_quick_presentation_slides(
            result.get("slide") or result.get("parsed") or {},
            max_chars=360,
            max_verses=3,
        )

        self.assertGreater(len(slides), 2)
        self.assertTrue(slides[0]["text"].startswith("Иоанн 3:16-4:2\n\n3:16."))
        self.assertIn("3:17.", slides[0]["text"])
        self.assertNotIn("Иоанн 3:16-4:2", slides[1]["text"])
        self.assertTrue(any("4:1." in slide["text"] for slide in slides))
        self.assertTrue(any("4:2." in slide["text"] for slide in slides))

    def test_clipped_next_chapter_range_does_not_fall_back_to_single_verse(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "евангелие от и о анна третья глава шестнадцатого стиха до второго стиха"
        )

        self.assertFalse(result.get("matched"))
        self.assertEqual("incomplete_cross_chapter_range_end", result.get("blocked_weak_context"))

    def test_open_range_to_end_of_chapter_without_verse_word(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("евангелие от иоанна третья глава с шестнадцатого и до конца главы")
        without_from = pipeline.process_text("евангелие от иоанна третья глава шестнадцатого до конца главы")
        compact = pipeline.process_text("иоанна три шестнадцать до конца главы")

        self.assertEqual("Иоанн 3:16-36", result.get("parsed", {}).get("ref"))
        self.assertEqual("Иоанн 3:16-36", without_from.get("parsed", {}).get("ref"))
        self.assertEqual("Иоанн 3:16-36", compact.get("parsed", {}).get("ref"))

    def test_long_same_chapter_range_builds_quick_presentation_slides(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("евангелие от иоанна третья глава с шестнадцатого и до конца главы")
        slides = scripture_range_quick_presentation_slides(
            result.get("slide") or result.get("parsed") or {},
            max_chars=360,
            max_verses=3,
        )

        self.assertGreater(len(slides), 3)
        self.assertTrue(slides[0]["text"].startswith("Иоанн 3:16-36\n\n3:16."))
        self.assertNotIn("Иоанн 3:16-36", slides[1]["text"])
        self.assertTrue(any("3:36." in slide["text"] for slide in slides))

    def test_long_range_state_tracks_each_slides_last_verse(self):
        payload = {
            "ref": "1 Иоанна 2:1-20",
            "book": "1 Иоанна",
        }
        slides = [
            {"text": "1 Иоанна 2:1-20\n\n2:1. Начало\n2:6. Конец первого слайда"},
            {"text": "2:7. Начало второго\n2:11. Конец второго слайда"},
        ]

        state = scripture_range_reading_state(payload, slides)

        self.assertIsNotNone(state)
        self.assertEqual([6, 11], [item["verse"] for item in state["targets"]])

    def test_showing_long_range_activates_reading_state(self):
        pipeline = LiveReferencePipeline()
        parsed = pipeline.process_text(
            "первая иоанна вторая глава с первого по двадцатый стих"
        )["parsed"]
        args = SimpleNamespace(
            holyrics_theme="",
            holyrics_quick_minutes=0.0,
        )

        with (
            patch(
                "tools.holyrics.get_holyrics_current_presentation",
                return_value=None,
            ),
            patch(
                "tools.holyrics.post_holyrics_api",
                side_effect=[
                    (True, "", '{"data": {}}'),
                    (True, "", ""),
                ],
            ),
        ):
            ok, reason = post_holyrics_url(args, "http://127.0.0.1:8091", parsed)

        self.assertTrue(ok)
        self.assertIn("show_quick_presentation:long_range", reason)
        self.assertTrue(scripture_range_reading_active(args))
        self.assertEqual(
            [6, 11, 15, 20],
            [item["verse"] for item in args._holyrics_scripture_range_reading["targets"]],
        )

    def test_showing_long_range_caches_current_text_presentation_for_restore(self):
        pipeline = LiveReferencePipeline()
        parsed = pipeline.process_text(
            "первая иоанна вторая глава с первого по двадцатый стих"
        )["parsed"]
        args = SimpleNamespace(holyrics_theme="", holyrics_quick_minutes=0.0)
        current = {
            "type": "text",
            "text_id": "sermon-plan",
            "slide_number": 4,
        }

        with (
            patch(
                "tools.holyrics.get_holyrics_current_presentation",
                return_value=current,
            ),
            patch(
                "tools.holyrics.post_holyrics_api",
                side_effect=[
                    (True, "", '{"data": {}}'),
                    (True, "", ""),
                ],
            ),
        ):
            ok, _reason = post_holyrics_url(args, "http://127.0.0.1:8091", parsed)

        self.assertTrue(ok)
        self.assertEqual(
            {
                "type": "text",
                "text_id": "sermon-plan",
                "slide_number": 4,
                "current_index": 3,
            },
            args._holyrics_scripture_range_reading["restore_presentation"],
        )

    def test_last_verse_advances_long_range_and_final_verse_completes_it(self):
        args = SimpleNamespace(
            holyrics_url="http://127.0.0.1:8091",
            holyrics_token="token",
            holyrics_timeout=1.0,
            _holyrics_scripture_range_reading={
                "ref": "1 Иоанна 2:1-20",
                "book": "1 Иоанна",
                "book_id": 62,
                "current_index": 0,
                "targets": [
                    {"slide_index": 0, "chapter": 2, "verse": 6, "text": "конец"},
                    {"slide_index": 1, "chapter": 2, "verse": 11, "text": "конец"},
                ],
            },
        )
        verse_six = SimpleNamespace(book_id=62, chapter=2, start_verse=6, end_verse=6)
        verse_eleven = SimpleNamespace(book_id=62, chapter=2, start_verse=11, end_verse=11)

        with patch("tools.holyrics.post_holyrics_api", return_value=(True, "", "")) as api:
            advanced = handle_scripture_range_reading_match(args, verse_six)

        self.assertTrue(advanced["advanced"])
        self.assertEqual(1, args._holyrics_scripture_range_reading["current_index"])
        api.assert_called_once_with(
            args,
            "http://127.0.0.1:8091",
            "ActionGoToIndex",
            {"index": 1},
        )

        with patch(
            "tools.holyrics.close_holyrics_quick_presentation_verified",
            return_value=(True, "quick_presentation_closed", {"verified": True}),
        ) as close_quick:
            completed = handle_scripture_range_reading_match(args, verse_eleven)

        self.assertTrue(completed["completed"])
        self.assertFalse(scripture_range_reading_active(args))
        close_quick.assert_called_once_with(args, "http://127.0.0.1:8091")

    def test_non_boundary_verse_is_consumed_without_advancing_long_range(self):
        args = SimpleNamespace(
            _holyrics_scripture_range_reading={
                "ref": "1 Иоанна 2:1-20",
                "book": "1 Иоанна",
                "book_id": 62,
                "current_index": 0,
                "targets": [
                    {"slide_index": 0, "chapter": 2, "verse": 6, "text": "конец"},
                ],
            }
        )
        verse_four = SimpleNamespace(book_id=62, chapter=2, start_verse=4, end_verse=4)

        result = handle_scripture_range_reading_match(args, verse_four)

        self.assertTrue(result["active"])
        self.assertFalse(result["matched_boundary"])
        self.assertTrue(scripture_range_reading_active(args))

    def test_manual_right_arrow_synchronizes_long_range_slide(self):
        args = SimpleNamespace(
            holyrics_url="http://127.0.0.1:8091",
            _holyrics_scripture_range_reading={
                "current_index": 0,
                "targets": [{"verse": 6}, {"verse": 11}],
            },
        )
        with patch(
            "tools.holyrics.get_holyrics_current_presentation",
            return_value={"type": "quick_presentation", "slide_number": 2},
        ):
            result = sync_scripture_range_reading(args)

        self.assertTrue(result["manual_advance"])
        self.assertEqual(1, args._holyrics_scripture_range_reading["current_index"])

    def test_manual_sermon_plan_restore_ends_long_range_mode(self):
        plan = {"type": "text", "text_id": "sermon-plan", "current_index": 0}
        args = SimpleNamespace(
            holyrics_url="http://127.0.0.1:8091",
            _holyrics_sermon_plan_presentation=plan,
            _holyrics_scripture_range_reading={
                "current_index": 0,
                "targets": [{"verse": 6}, {"verse": 11}],
            },
        )
        with patch(
            "tools.holyrics.get_holyrics_current_presentation",
            return_value={"type": "text", "text_id": "sermon-plan", "slide_number": 3},
        ):
            result = sync_scripture_range_reading(args)

        self.assertTrue(result["manual_restore"])
        self.assertFalse(scripture_range_reading_active(args))
        self.assertEqual(2, plan["current_index"])
        self.assertEqual(3, plan["next_index"])

    def test_final_long_range_verse_restores_current_sermon_plan_slide(self):
        presentation = {
            "type": "text",
            "text_id": "sermon-plan",
            "current_index": 2,
        }
        args = SimpleNamespace(
            holyrics_url="http://127.0.0.1:8091",
            _holyrics_sermon_plan_presentation=presentation,
            _holyrics_scripture_range_reading={
                "ref": "1 Иоанна 2:1-20",
                "book": "1 Иоанна",
                "book_id": 62,
                "current_index": 0,
                "targets": [
                    {"slide_index": 0, "chapter": 2, "verse": 20, "text": "конец"},
                ],
            },
        )
        verse_twenty = SimpleNamespace(book_id=62, chapter=2, start_verse=20, end_verse=20)

        with patch(
            "tools.holyrics.restore_sermon_plan_after_quick_presentation",
            return_value=(True, "sermon_plan_restore_verified", {"verified": True}),
        ) as show:
            result = handle_scripture_range_reading_match(args, verse_twenty)

        self.assertTrue(result["completed"])
        self.assertTrue(result["restored_sermon_plan"])
        self.assertFalse(scripture_range_reading_active(args))
        show.assert_called_once_with(
            args,
            "http://127.0.0.1:8091",
            presentation,
            2,
        )

    def test_final_long_range_verse_restores_presentation_cached_in_range_state(self):
        cached = {
            "type": "text",
            "text_id": "sermon-plan",
            "current_index": 6,
        }
        args = SimpleNamespace(
            holyrics_url="http://127.0.0.1:8091",
            _holyrics_scripture_range_reading={
                "ref": "Иаков 3:5-15",
                "book": "Иаков",
                "book_id": 59,
                "current_index": 0,
                "restore_presentation": cached,
                "targets": [
                    {"slide_index": 0, "chapter": 3, "verse": 15, "text": "конец"},
                ],
            },
        )
        verse_fifteen = SimpleNamespace(book_id=59, chapter=3, start_verse=15, end_verse=15)

        with patch(
            "tools.holyrics.restore_sermon_plan_after_quick_presentation",
            return_value=(True, "sermon_plan_restore_verified", {"verified": True}),
        ) as restore:
            result = handle_scripture_range_reading_match(args, verse_fifteen)

        self.assertTrue(result["completed"])
        self.assertTrue(result["restored_sermon_plan"])
        restore.assert_called_once_with(
            args,
            "http://127.0.0.1:8091",
            cached,
            6,
        )

    def test_failed_final_restore_keeps_long_passage_active_for_retry(self):
        presentation = {
            "type": "text",
            "text_id": "sermon-plan",
            "current_index": 0,
        }
        state = {
            "ref": "1 Иоанна 2:1-20",
            "book": "1 Иоанна",
            "book_id": 62,
            "current_index": 0,
            "targets": [
                {"slide_index": 0, "chapter": 2, "verse": 20, "text": "конец"},
            ],
        }
        args = SimpleNamespace(
            holyrics_url="http://127.0.0.1:8091",
            _holyrics_sermon_plan_presentation=presentation,
            _holyrics_scripture_range_reading=state,
        )
        verse_twenty = SimpleNamespace(book_id=62, chapter=2, start_verse=20, end_verse=20)

        with patch(
            "tools.holyrics.restore_sermon_plan_after_quick_presentation",
            return_value=(False, "quick_presentation_still_active", {"quick_states": []}),
        ):
            result = handle_scripture_range_reading_match(args, verse_twenty)

        self.assertFalse(result["completed"])
        self.assertTrue(result["completion_failed"])
        self.assertTrue(scripture_range_reading_active(args))
        self.assertIs(state, args._holyrics_scripture_range_reading)

    def test_complete_single_verse_after_chapter_still_matches(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("евангелие от луки двадцать четвёртая глава тринадцатый стих")

        self.assertEqual("Лука 24:13", result.get("parsed", {}).get("ref"))

    def test_compact_reference_without_markers_has_extra_risk(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "римлянам четвёртого шестнадцать",
            asr_result={
                "result": [
                    {"conf": 1.0, "start": 3538.08, "end": 3538.53, "word": "римлянам"},
                    {"conf": 0.809894, "start": 3538.53, "end": 3539.165215, "word": "четвёртого"},
                    {"conf": 0.642576, "start": 3539.19, "end": 3539.655645, "word": "шестнадцать"},
                ],
                "text": "римлянам четвёртого шестнадцать",
            },
        )

        self.assertEqual("Римлянам 4:16", result.get("parsed", {}).get("ref"))
        self.assertEqual("medium", result.get("risk_level"))
        self.assertIn("compact_reference_without_markers", result.get("risk_reasons"))

    def test_ordinary_numbered_statements_do_not_become_compact_references(self):
        samples = (
            (
                "ещё раз первый пункт сегодняшний проповеди слышания это первая реакция "
                "на божье слово что говорить яков он говорит возлюбленной"
            ),
            "интересные яков выделяют три три вещи слышания слова и гнев",
        )

        for text in samples:
            with self.subTest(text=text):
                result = LiveReferencePipeline().process_text(text)

                self.assertIsNone(result.get("parsed"))
                self.assertEqual(
                    "compact_reference_numbers_not_after_book",
                    result.get("blocked_weak_context"),
                )

    def test_bare_verse_number_after_chapter_has_extra_risk(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "лука пятнадцатая глава двадцать",
            asr_result={
                "result": [
                    {"conf": 1.0, "start": 4138.33, "end": 4138.63, "word": "лука"},
                    {"conf": 1.0, "start": 4138.63, "end": 4139.35, "word": "пятнадцатая"},
                    {"conf": 1.0, "start": 4139.35, "end": 4139.65, "word": "глава"},
                    {"conf": 0.624578, "start": 4139.65, "end": 4139.92, "word": "двадцать"},
                ],
                "text": "лука пятнадцатая глава двадцать",
            },
        )

        self.assertEqual("Лука 15:20", result.get("parsed", {}).get("ref"))
        self.assertEqual("medium", result.get("risk_level"))
        self.assertIn("bare_verse_number_after_chapter", result.get("risk_reasons"))

    def test_book_fragment_then_verse_without_chapter_has_extra_risk(self):
        pipeline = LiveReferencePipeline()

        book_only = pipeline.process_text(
            "второе коринфянам",
            asr_result={
                "result": [
                    {"conf": 1.0, "start": 4078.82, "end": 4079.12, "word": "второе"},
                    {"conf": 1.0, "start": 4079.12, "end": 4079.51, "word": "коринфянам"},
                ],
                "text": "второе коринфянам",
            },
        )
        result = pipeline.process_text(
            "первое вторую стих",
            asr_result={
                "result": [
                    {"conf": 0.937384, "start": 4080.14, "end": 4080.35, "word": "первое"},
                    {"conf": 0.704042, "start": 4080.35, "end": 4080.62, "word": "вторую"},
                    {"conf": 1.0, "start": 4080.62, "end": 4080.86, "word": "стих"},
                ],
                "text": "первое вторую стих",
            },
        )

        self.assertFalse(book_only.get("matched"))
        self.assertEqual("2 Коринфянам 1:2", result.get("parsed", {}).get("ref"))
        self.assertEqual("medium", result.get("risk_level"))
        self.assertIn("book_fragment_without_chapter_marker", result.get("risk_reasons"))

    def test_explicit_verse_after_chapter_does_not_add_bare_number_risk(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("лука пятнадцатая глава двадцатый стих")

        self.assertEqual("Лука 15:20", result.get("parsed", {}).get("ref"))
        self.assertNotIn("bare_verse_number_after_chapter", result.get("risk_reasons"))

    def test_bare_numbers_first_verse_does_not_auto_match(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("числа первое первого стих")

        self.assertFalse(result.get("matched"))
        self.assertEqual("weak_bare_numbers_first_verse", result.get("blocked_weak_context"))

        explicit = pipeline.process_text("книга числа первая глава первый стих")
        self.assertEqual("Числа 1:1", explicit.get("parsed", {}).get("ref"))

    def test_weak_trailing_numbers_context_does_not_auto_match(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("второе один о числа")

        self.assertFalse(result.get("matched"))
        self.assertEqual("weak_trailing_numbers_context", result.get("blocked_weak_context"))

        explicit = pipeline.process_text("книга числа вторая глава первый стих")
        self.assertEqual("Числа 2:1", explicit.get("parsed", {}).get("ref"))

    def test_weak_trailing_ezra_context_does_not_auto_match(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("второе сорок четвёртую стих ездры")

        self.assertFalse(result.get("matched"))
        self.assertEqual("weak_trailing_ezra_context", result.get("blocked_weak_context"))

    def test_explicit_ezra_reference_still_matches(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("книга ездры вторая глава сорок четвертый стих")

        self.assertEqual("Ездра 2:44", result.get("parsed", {}).get("ref"))

    def test_weak_compact_ezra_hundred_context_does_not_auto_match(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("ездра восьмой сотая вторым")

        self.assertFalse(result.get("matched"))
        self.assertEqual("weak_compact_ezra_hundred_context", result.get("blocked_weak_context"))

    def test_missing_chapter_word_after_ordinal_tens_does_not_merge_chapter_and_verse(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("исайя сороковая первого девятый стих")

        self.assertEqual("Исаия 40:1-9", result.get("parsed", {}).get("ref"))

    def test_cardinal_tens_can_still_form_compound_chapter_number(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("исайя сорок первого девятый стих")

        self.assertEqual("Исаия 41:9", result.get("parsed", {}).get("ref"))

    def test_descending_repeated_verse_is_treated_as_speaker_correction(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("бытие двадцать четвёртая глава пятьдесят вторую стих пятьдесят первое стих")

        self.assertEqual("Бытие 24:51", result.get("parsed", {}).get("ref"))

    def test_repeated_range_end_is_treated_as_speaker_hesitation(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "евангелие от матфея двадцать пятой главе тридцать четвёртого сороковой сорокового стихи"
        )

        self.assertEqual("Матфей 25:34-40", result.get("parsed", {}).get("ref"))
        self.assertEqual("parser_repeated_range_end", result.get("source"))
        self.assertIn("repeated_range_end_repair", result.get("risk_reasons"))

    def test_confusable_seventeen_eighteen_verse_adds_alternative(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("римлянам восьмая глава восемнадцатый стих")

        self.assertEqual("Римлянам 8:18", result.get("parsed", {}).get("ref"))
        refs = {item.get("ref") for item in result.get("ambiguous_alternatives") or []}
        self.assertIn("Римлянам 8:17", refs)
        self.assertIn("confusable_number_alternative", result.get("risk_reasons"))

    def test_confusable_seven_eight_verse_adds_alternative(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("восьмой стих деяния апостолов первой главы")

        self.assertEqual("Деяния 1:8", result.get("parsed", {}).get("ref"))
        refs = {item.get("ref") for item in result.get("ambiguous_alternatives") or []}
        self.assertIn("Деяния 1:7", refs)
        self.assertIn("confusable_number_alternative", result.get("risk_reasons"))

    def test_explicit_seven_eight_range_still_matches_range(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("деяния апостолов первая глава седьмой восьмой стих")

        self.assertEqual("Деяния 1:7-8", result.get("parsed", {}).get("ref"))

    def test_confusable_thirteen_thirty_chapter_adds_existing_alternative(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("бытие тридцатая глава первый стих")

        self.assertEqual("Бытие 30:1", result.get("parsed", {}).get("ref"))
        refs = {item.get("ref") for item in result.get("ambiguous_alternatives") or []}
        self.assertIn("Бытие 13:1", refs)

    def test_confusable_twelve_thirteen_verse_adds_alternative(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("римлянам восьмая глава тринадцатый стих")

        self.assertEqual("Римлянам 8:13", result.get("parsed", {}).get("ref"))
        refs = {item.get("ref") for item in result.get("ambiguous_alternatives") or []}
        self.assertIn("Римлянам 8:12", refs)

    def test_confusable_twelve_thirteen_chapter_adds_existing_alternative(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("бытие тринадцатая глава первый стих")

        self.assertEqual("Бытие 13:1", result.get("parsed", {}).get("ref"))
        refs = {item.get("ref") for item in result.get("ambiguous_alternatives") or []}
        self.assertIn("Бытие 12:1", refs)

    def test_confusable_twelve_nineteen_chapter_adds_existing_alternative(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("притчи двенадцать восемнадцать")

        self.assertEqual("Притчи 12:18", result.get("parsed", {}).get("ref"))
        refs = {item.get("ref") for item in result.get("ambiguous_alternatives") or []}
        self.assertIn("Притчи 19:18", refs)
        self.assertIn("confusable_number_alternative", result.get("risk_reasons"))

    def test_repeated_tail_number_prefers_first_number_as_chapter(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "притчи девятнадцатого двадцатый двадцатая",
            asr_result={
                "result": [
                    {"conf": 0.656636, "start": 3320.95, "end": 3321.34, "word": "притчи"},
                    {"conf": 0.691675, "start": 3321.34, "end": 3322.06, "word": "девятнадцатого"},
                    {"conf": 0.639596, "start": 3322.06, "end": 3322.294, "word": "двадцатый"},
                    {"conf": 0.301239, "start": 3322.294, "end": 3322.54, "word": "двадцатая"},
                ],
                "text": "притчи девятнадцатого двадцатый двадцатая",
            },
        )

        self.assertEqual("Притчи 19:20", result.get("parsed", {}).get("ref"))
        self.assertEqual("high", result.get("risk_level"))

    def test_unnumbered_corinthians_epistle_adds_colossians_alternative(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("послания коринфянам первого глава девятнадцать два второе стих")

        self.assertEqual("2 Коринфянам 1:19-22", result.get("parsed", {}).get("ref"))
        refs = {item.get("ref") for item in result.get("ambiguous_alternatives") or []}
        self.assertIn("Колоссянам 1:19-22", refs)
        self.assertEqual("medium", result.get("risk_level"))
        self.assertIn("confusable_book_alternative", result.get("risk_reasons"))

    def test_ephesians_adds_colossians_alternative(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("послание к ефесянам вторая глава девятой десятый стих")

        self.assertEqual("Ефесянам 2:9-10", result.get("parsed", {}).get("ref"))
        refs = {item.get("ref") for item in result.get("ambiguous_alternatives") or []}
        self.assertIn("Колоссянам 2:9-10", refs)
        self.assertEqual("medium", result.get("risk_level"))
        self.assertIn("confusable_book_alternative", result.get("risk_reasons"))

    def test_colossians_spoken_and_split_forms(self):
        pipeline = LiveReferencePipeline()

        for text in (
            "послание колосянам вторая глава двадцатый двадцать второй стих",
            "вторая глава двадцатый двадцать второе стих послание кол осии яна",
            "послание кол осия нам третья глава первый стих",
            "послание кол оси нам третья глава первый стих",
            "послание колоса нам третья глава первый стих",
            "послание колос са нам третья глава первый стих",
            "послание кол оси яна третья глава первый стих",
            "послание кол о сия нам третья глава первый стих",
            "послание колос нам третья глава первый стих",
            "сия нам первое глава девятой одиннадцатый стих",
        ):
            with self.subTest(text=text):
                result = pipeline.process_text(text)
                self.assertEqual("Колоссянам", result.get("parsed", {}).get("book"))

    def test_colossians_new_phonetic_forms_keep_long_range(self):
        for book_words in ("кол ось яна", "ко лось яна", "кол сям"):
            with self.subTest(book_words=book_words):
                pipeline = LiveReferencePipeline()
                result = pipeline.process_text(
                    f"послание {book_words} третья глава с первого по десятое стих"
                )
                self.assertEqual("Колоссянам 3:1-10", result.get("parsed", {}).get("ref"))

    def test_bare_poslanie_syam_does_not_become_first_john(self):
        pipeline = LiveReferencePipeline()

        pipeline.process_text("сегодняшнее проповедь будет по отрыв ко из")
        result = pipeline.process_text(
            "послание сям третья глава с первого по десятое стих"
        )

        self.assertFalse(result.get("matched"))
        self.assertEqual("unrecognized_epistle_book", result.get("blocked_weak_context"))

    def test_colossians_chapter_without_verse_does_not_become_philemon(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("послание к из послание кол осии яна третья глава")

        self.assertFalse(result.get("matched"))
        self.assertEqual("colossians_book_conflict", result.get("blocked_weak_context"))

    def test_repeated_seventeen_or_eighteen_range_is_repaired(self):
        pipeline = LiveReferencePipeline()

        seventeen = pipeline.process_text("римлянам восьмая глава семнадцатый семнадцатый стих")
        eighteen = pipeline.process_text("римлянам восьмая глава восемнадцатый восемнадцатый стих")

        self.assertEqual("Римлянам 8:17-18", seventeen.get("parsed", {}).get("ref"))
        self.assertEqual("parser_repeated_confusable_range", seventeen.get("source"))
        self.assertEqual("Римлянам 8:17-18", eighteen.get("parsed", {}).get("ref"))
        self.assertEqual("parser_repeated_confusable_range", eighteen.get("source"))

    def test_repeated_psalm_references_are_returned_as_compact_list(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "псалом девятый девятнадцатый стих "
            "псалом тридцать восьмой восьмой стих "
            "псалом тридцать девять пятой стих "
            "псалом шестьдесят первый пятой стих "
            "псалом семидесятый пятой стих седьмой стих псалом"
        )

        self.assertTrue(result.get("matched"))
        self.assertIsNone(result.get("parsed"))
        self.assertEqual("parser_reference_list", result.get("source"))
        refs = [item.get("ref") for item in result.get("reference_list") or []]
        self.assertEqual(
            [
                "Псалтирь 9:19",
                "Псалтирь 38:8",
                "Псалтирь 39:5",
                "Псалтирь 61:5",
                "Псалтирь 70:5-7",
            ],
            refs,
        )

    def test_compact_references_from_different_books_are_returned_as_list(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("один пять и о анна три четыре иакова один два")

        self.assertTrue(result.get("matched"))
        self.assertIsNone(result.get("parsed"))
        self.assertEqual("parser_reference_list", result.get("source"))
        refs = [item.get("ref") for item in result.get("reference_list") or []]
        self.assertEqual(["Иоанн 3:4", "Иаков 1:2"], refs)

    def test_buffered_reference_list_preempts_last_single_reference(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("матфей седьмая глава первое стих", now_ms=1_000).get("matched"))
        self.assertFalse(pipeline.process_text("не судьи", now_ms=1_500).get("matched"))
        result = pipeline.process_text("лука шестая глава тридцать шестой стих", now_ms=2_000)

        self.assertTrue(result.get("matched"))
        self.assertIsNone(result.get("parsed"))
        self.assertEqual("parser_reference_list", result.get("source"))
        refs = [item.get("ref") for item in result.get("reference_list") or []]
        self.assertEqual(["Матфей 7:1", "Лука 6:36"], refs)

    def test_compact_reference_list_accepts_whole_psalm_reference(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "иов третье глава седьмой восьмая стих "
            "иов тридцать третье глава одиннадцатый двенадцатые стих "
            "псалтырь сто двадцать второе псалом"
        )

        self.assertTrue(result.get("matched"))
        self.assertIsNone(result.get("parsed"))
        self.assertEqual("parser_reference_list", result.get("source"))
        refs = [item.get("ref") for item in result.get("reference_list") or []]
        self.assertEqual(["Иов 3:7-8", "Иов 33:11-12", "Псалтирь 122:1-4"], refs)

    def test_split_psalm_range_before_psalm_title_uses_full_buffer(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("первого по", now_ms=1_000).get("matched"))
        self.assertFalse(pipeline.process_text("четырнадцатый стих", now_ms=2_000).get("matched"))
        result = pipeline.process_text("псалом семьдесят второй", now_ms=3_000)

        self.assertEqual("Псалтирь 72:1-14", result.get("parsed", {}).get("ref"))

    def test_psalm_range_accepts_stih_misheard_as_seven_before_psalm_title(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("четвёртого по двенадцатые семь семьдесят второго псалмы")

        self.assertEqual("Псалтирь 72:4-12", result.get("parsed", {}).get("ref"))

    def test_psalm_range_accepts_psalm_number_after_stich(self):
        for text, expected in (
            (
                "с пятого по тринадцатый стих семьдесят второго псалма",
                "Псалтирь 72:5-13",
            ),
            (
                "с четвёртого по двенадцатый стих семьдесят второго псалма",
                "Псалтирь 72:4-12",
            ),
            (
                "с четвёртого по двенадцатый стих семьдесят второго салма",
                "Псалтирь 72:4-12",
            ),
        ):
            with self.subTest(text=text):
                result = LiveReferencePipeline().process_text(text)

                self.assertEqual(expected, result.get("parsed", {}).get("ref"))

    def test_unconnected_verse_range_reuses_last_book_and_chapter(self):
        pipeline = LiveReferencePipeline()
        previous = pipeline.process_text("псалом семьдесят второй первый стих")

        result = pipeline.process_text(
            "двадцать третий двадцать шестой стих но я всегда с тобою "
            "ты держишь меня за правую руку"
        )

        self.assertEqual("Псалтирь 72:1", previous.get("parsed", {}).get("ref"))
        self.assertEqual("Псалтирь 72:23-26", result.get("parsed", {}).get("ref"))

    def test_psalm_without_stich_keeps_ordinal_tens_as_compound_psalm_number(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("псалом девяностый девять")

        self.assertEqual("Псалтирь 99:1-5", result.get("parsed", {}).get("ref"))

    def test_short_psalm_chapter_verse_without_stich_still_works(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("псалом двадцать два четыре")

        self.assertEqual("Псалтирь 22:4", result.get("parsed", {}).get("ref"))

    def test_psalm_asr_aliases(self):
        pipeline = LiveReferencePipeline()

        for text in ("салом двадцать два четыре", "салон двадцать два четыре"):
            with self.subTest(text=text):
                result = pipeline.process_text(text)

                self.assertEqual("Псалтирь 22:4", result.get("parsed", {}).get("ref"))

    def test_numbered_general_epistle_with_poslanie_still_works(self):
        pipeline = LiveReferencePipeline()

        peter = pipeline.process_text("второе послание петра третья глава четвёртый стих")
        john = pipeline.process_text("первое послание иоанна вторая глава восьмой стих")

        self.assertEqual("2 Петра 3:4", peter.get("parsed", {}).get("ref"))
        self.assertEqual("1 Иоанна 2:8", john.get("parsed", {}).get("ref"))

    def test_resolver_does_not_choose_numbers_when_peter_is_explicit(self):
        pipeline = LiveReferencePipeline()

        distorted = pipeline.process_text("числа второе петра первое")
        peter = pipeline.process_text("второе петра первая глава шестнадцатый стих")
        numbers = pipeline.process_text("числа вторая глава первый стих")

        self.assertFalse(distorted.get("matched"))
        self.assertEqual("resolver_conflicts_with_peter", distorted.get("blocked_weak_context"))
        self.assertEqual("2 Петра 1:16", peter.get("parsed", {}).get("ref"))
        self.assertEqual("Числа 2:1", numbers.get("parsed", {}).get("ref"))

    def test_split_two_digit_range_start_uses_end_tens(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("евангелие диана пятая глава третий три четвёртую стих")
        compact = pipeline.process_text("евангелие иоанна пятая глава третий тридцать четвёртый стих")
        twenties = pipeline.process_text("евангелие от иоанна пятая глава первый двадцать второй стих")

        self.assertEqual("Иоанн 5:33-34", result.get("parsed", {}).get("ref"))
        self.assertEqual("Иоанн 5:33-34", compact.get("parsed", {}).get("ref"))
        self.assertEqual("Иоанн 5:21-22", twenties.get("parsed", {}).get("ref"))

    def test_short_single_digit_range_still_works(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("евангелие от иоанна пятая глава третий четвёртый стих")

        self.assertEqual("Иоанн 5:3-4", result.get("parsed", {}).get("ref"))

    def test_slow_split_old_testament_reference_uses_explicit_context(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("читаем из книги второзаконие", now_ms=1_000).get("matched"))
        self.assertFalse(pipeline.process_text("двадцать шестая глава", now_ms=2_000).get("matched"))
        self.assertFalse(pipeline.process_text("девятого", now_ms=3_000).get("matched"))
        result = pipeline.process_text("четырнадцатая стих", now_ms=4_000)

        self.assertEqual("Второзаконие 26:9-14", result.get("parsed", {}).get("ref"))

    def test_slow_split_genesis_reference_uses_explicit_context(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("откроем книга бытие", now_ms=1_000).get("matched"))
        self.assertFalse(pipeline.process_text("двадцать седьмую главы", now_ms=2_000).get("matched"))
        result = pipeline.process_text("с тридцатого тридцать четвёртая стих", now_ms=3_000)

        self.assertEqual("Бытие 27:30-34", result.get("parsed", {}).get("ref"))

    def test_split_reference_resets_after_long_pause(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(
            pipeline.process_text("давайте откроем евангелие от матфея", now_ms=1_000).get("matched")
        )

        second = pipeline.process_text("восьмая глава с первого по пятый стих", now_ms=4_500)
        self.assertFalse(second.get("matched"))
        self.assertTrue(second.get("buffer_reset_by_gap"))
        self.assertEqual(["восьмая глава с первого по пятый стих"], second.get("vosk_buffer"))

    def test_stale_buffer_does_not_repeat_previous_reference(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text("иоана три шестнадцать")
        self.assertEqual("Иоанн 3:16", first.get("parsed", {}).get("ref"))

        second = pipeline.process_text("мих от до с ины")
        self.assertFalse(second.get("matched"))
        self.assertEqual(["мих от до с ины"], second.get("vosk_buffer"))

    def test_stale_buffer_does_not_cascade_false_reference(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text("два же второе десятую притч")
        self.assertEqual("Притчи 2:2-10", first.get("parsed", {}).get("ref"))

        second = pipeline.process_text("четвертая из")
        self.assertFalse(second.get("matched"))
        self.assertEqual(["четвертая из"], second.get("vosk_buffer"))

    def test_short_moses_noise_does_not_create_reference(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("десять три моисея")

        self.assertFalse(result.get("matched"))
        self.assertEqual("weak_short_moses_context", result.get("blocked_weak_context"))

    def test_levit_range_after_short_moses_noise_still_works(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("десять три моисея", now_ms=1_000).get("matched"))
        self.assertFalse(pipeline.process_text("читаем", now_ms=2_000).get("matched"))
        self.assertFalse(pipeline.process_text("книга левит двадцать четвёртая глава", now_ms=3_000).get("matched"))
        result = pipeline.process_text("двадцатого по двадцать второе стих", now_ms=4_000)

        self.assertEqual("Левит 24:20-22", result.get("parsed", {}).get("ref"))

    def test_short_yana_noise_does_not_create_reference(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("десять яна семь")

        self.assertFalse(result.get("matched"))
        self.assertEqual("weak_short_yana_context", result.get("blocked_weak_context"))

    def test_numbered_yana_epistle_normalizes_to_john(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("первая яна пять тринадцать яна")

        self.assertEqual("1 Иоанна 5:13", result.get("parsed", {}).get("ref"))

    def test_unknown_prefix_before_reversed_verse_context_does_not_create_reference(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("[unk] шестой стих двадцать седьмой главы книги второзаконие")

        self.assertFalse(result.get("matched"))
        self.assertEqual("unknown_prefix_before_reversed_verse", result.get("blocked_weak_context"))

    def test_unknown_prefix_inside_book_chapter_context_still_works(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("из книги второзаконие двадцать седьмая глава [unk] двадцать шестой стих")

        self.assertEqual("Второзаконие 27:26", result.get("parsed", {}).get("ref"))

    def test_clean_reference_has_low_risk_score(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "евангелие от иоанна три шестнадцать",
            asr_result={
                "result": [
                    {"word": "евангелие", "start": 0.0, "end": 0.5, "conf": 1.0},
                    {"word": "от", "start": 0.5, "end": 0.7, "conf": 1.0},
                    {"word": "иоанна", "start": 0.7, "end": 1.1, "conf": 1.0},
                    {"word": "три", "start": 1.1, "end": 1.3, "conf": 1.0},
                    {"word": "шестнадцать", "start": 1.3, "end": 1.9, "conf": 1.0},
                ]
            },
        )

        self.assertEqual("Иоанн 3:16", result.get("parsed", {}).get("ref"))
        self.assertLess(result.get("risk_score"), 0.3)
        self.assertEqual("low", result.get("risk_level"))

    def test_distorted_fast_reference_has_high_risk_score(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "пророк данила один пятой стих",
            asr_result={
                "result": [
                    {"word": "пророк", "start": 0.0, "end": 0.15, "conf": 0.72},
                    {"word": "данила", "start": 0.16, "end": 0.31, "conf": 0.62},
                    {"word": "один", "start": 0.32, "end": 0.42, "conf": 0.58},
                    {"word": "пятой", "start": 0.43, "end": 0.54, "conf": 0.61},
                    {"word": "стих", "start": 0.55, "end": 0.68, "conf": 0.91},
                ]
            },
        )

        self.assertEqual("Даниил 1:5", result.get("parsed", {}).get("ref"))
        self.assertGreaterEqual(result.get("risk_score"), 0.6)
        self.assertEqual("high", result.get("risk_level"))

    def test_missing_twenty_before_range_end_is_restored(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("послание яков вторая восемнадцатого второе стих")

        self.assertEqual("Иаков 2:18-22", result.get("parsed", {}).get("ref"))
        self.assertEqual("parser_missing_twenty_range", result.get("source"))
        self.assertIn("missing_twenty_range_repair", result.get("risk_reasons"))

    def test_missing_twenty_before_range_end_can_override_wrong_chapter_parse(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("послание яков вторая восемнадцатого третьего стих")

        self.assertEqual("Иаков 2:18-23", result.get("parsed", {}).get("ref"))
        self.assertEqual("parser_missing_twenty_range", result.get("source"))

    def test_missing_twenty_before_ninth_range_end_is_restored(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("деяния вторая глава восемнадцатого девятого стих")

        self.assertEqual("Деяния 2:18-29", result.get("parsed", {}).get("ref"))
        self.assertEqual("parser_missing_twenty_range", result.get("source"))

    def test_missing_tens_before_range_end_uses_start_verse_tens(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("послание евреям двенадцатая глава двадцать четвёртый шестой")

        self.assertEqual("Евреям 12:24-26", result.get("parsed", {}).get("ref"))
        self.assertEqual("parser_missing_twenty_range", result.get("source"))

    def test_missing_tens_range_repair_requires_ml_confirmation(self):
        pipeline = LiveReferencePipeline()
        model = load_risk_model(
            Path(__file__).resolve().parents[1]
            / "src"
            / "bible_parser_core"
            / "data"
            / "risk_model.json"
        )

        result = pipeline.process_text("послание евреям двенадцатая глава двадцать пятый восьмой")
        ml_risk = score_payload_with_model(result, model)

        self.assertEqual("Евреям 12:25-28", result.get("parsed", {}).get("ref"))
        self.assertEqual("parser_missing_twenty_range", result.get("source"))
        self.assertTrue(ml_risk.get("needs_confirmation"))
        self.assertIn("missing_tens_range_repair", ml_risk.get("decision_reasons"))

    def test_missing_twenty_range_does_not_restore_nonexistent_end_verse(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("послание яков вторая восемнадцатого девятого стих")

        self.assertEqual("Иаков 2:18", result.get("parsed", {}).get("ref"))

    def test_missing_twenty_range_does_not_apply_to_tenth(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("послание яков вторая восемнадцатого десятого стих")

        self.assertEqual("Иаков 2:18", result.get("parsed", {}).get("ref"))
        self.assertEqual("parser", result.get("source"))

    def test_colos_after_chapter_repairs_first_to_tenth_range(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "матфея четвёртая колос первого пятидесятый стих",
            asr_result={
                "result": [
                    {"conf": 0.8, "start": 4245.8, "end": 4246.1, "word": "матфея"},
                    {"conf": 0.7, "start": 4246.1, "end": 4246.4, "word": "четвёртая"},
                    {"conf": 0.45, "start": 4246.4, "end": 4246.7, "word": "колос"},
                    {"conf": 0.75, "start": 4246.7, "end": 4247.0, "word": "первого"},
                    {"conf": 0.55, "start": 4247.0, "end": 4247.4, "word": "пятидесятый"},
                    {"conf": 0.9, "start": 4247.4, "end": 4247.7, "word": "стих"},
                ],
                "text": "матфея четвёртая колос первого пятидесятый стих",
            },
        )

        self.assertEqual("Матфей 4:1-10", result.get("parsed", {}).get("ref"))
        self.assertEqual("parser_colos_chapter_range", result.get("source"))
        self.assertIn("colos_chapter_range_repair", result.get("risk_reasons"))

    def test_reversed_chapter_after_range_with_self_correction(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("евангелие от луки первое четыре первый четвёртый стих пятое главы")

        self.assertEqual("Лука 5:1-4", result.get("parsed", {}).get("ref"))

    def test_later_explicit_book_correction_overrides_earlier_book_fragment(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "первое фесс первое послание петра первое глава третье четвёртый стих"
        )

        self.assertEqual("1 Петра 1:3-4", result.get("parsed", {}).get("ref"))

    def test_counting_rhyme_does_not_resolve_to_ruth(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("русь два три четыре пять по главе")

        self.assertFalse(result.get("matched"))
        self.assertEqual("ruth_counting_rhyme", result.get("blocked_weak_context"))

        short_result = pipeline.process_text("русь два три четыре пять")

        self.assertFalse(short_result.get("matched"))
        self.assertEqual("ruth_counting_rhyme", short_result.get("blocked_weak_context"))

        normal = pipeline.process_text("книга руфь третья глава четвёртый пятый стих")

        self.assertEqual("Руфь 3:4-5", normal.get("parsed", {}).get("ref"))

        for distorted, expected in (
            ("книга рощ третья глава десятый одиннадцатый из тех", "Руфь 3:10-11"),
            ("воров третья глава пятая шестой из тех", "Руфь 3:5-6"),
            ("ров три пять шесть", "Руфь 3:5-6"),
        ):
            with self.subTest(distorted=distorted):
                result = pipeline.process_text(distorted)
                self.assertEqual(expected, result.get("parsed", {}).get("ref"))

    def test_paralipomenon_range_survives_vosk_stikh_distortion(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "первая книга паралипоменон шестая глава десятая одиннадцатая из тех"
        )

        self.assertEqual("1 Паралипоменон 6:10-11", result.get("parsed", {}).get("ref"))

    def test_numbered_kingdoms_range_waits_for_chapter_context(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("первое книги царств с четвёртого по восьмой стих")

        self.assertFalse(result.get("matched"))
        self.assertEqual("numbered_kingdoms_range_without_chapter", result.get("blocked_weak_context"))

    def test_numbered_kingdoms_range_works_with_chapter_context(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "читаем из двадцать седьмой главы первое книги царств четвёртого по восьмой стих"
        )

        self.assertEqual("1 Царств 27:4-8", result.get("parsed", {}).get("ref"))

    def test_joshua_chapter_suffix_waits_for_verse_context(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("читаем из книга иисуса навина четырнадцатую из четырнадцатый главы")

        self.assertFalse(result.get("matched"))
        self.assertEqual("joshua_chapter_suffix_without_verse", result.get("blocked_weak_context"))

    def test_joshua_range_works_with_verse_context(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("четырнадцатая глава книга иисуса навина двенадцатый четырнадцатая стих")

        self.assertEqual("Иисус Навин 14:12-14", result.get("parsed", {}).get("ref"))

    def test_noise_context_does_not_create_new_reference(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text("десятая притч девять числа")
        self.assertEqual("Числа 10:9", first.get("parsed", {}).get("ref"))

        second = pipeline.process_text("оны сто")
        self.assertFalse(second.get("matched"))
        self.assertEqual(["оны сто"], second.get("vosk_buffer"))

    def test_noise_context_does_not_create_daniel_reference(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("оны сто").get("matched"))
        self.assertFalse(pipeline.process_text("данила к до").get("matched"))

        third = pipeline.process_text("восьмого")
        self.assertFalse(third.get("matched"))
        self.assertTrue(third.get("blocked_no_book_context"))

    def test_non_gospel_noise_suffix_does_not_create_reference(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("с главы даниил").get("matched"))

        second = pipeline.process_text("шестого")
        self.assertFalse(second.get("matched"))

    def test_noisy_book_phrase_suffix_does_not_create_reference(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("от шесть пророка ионы иакова книга амоса").get("matched"))

        second = pipeline.process_text("послание евр")
        self.assertFalse(second.get("matched"))


if __name__ == "__main__":
    unittest.main()
