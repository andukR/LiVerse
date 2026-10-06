import json
import sys
from datetime import datetime, timedelta
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from bible_parser_core.live_pipeline import (
    LiveReferencePipeline,
    build_grammar,
    should_block_matched_payload,
)
from bible_parser_core.parser import normalize_text, parse_live_reference
from bible_parser_core.risk_model import load_risk_model, score_payload_with_model
from tools.holyrics import (
    capture_holyrics_current_appearance,
    cross_chapter_quick_presentation_slides,
    apply_scripture_range_operator_hint,
    format_missing_holyrics_permissions,
    handle_scripture_range_reading_match,
    prepare_sermon_plan_custom_theme,
    parse_holyrics_response,
    post_holyrics_api,
    post_holyrics_url,
    restore_holyrics_presentation,
    scripture_range_quick_presentation_body,
    scripture_range_quick_presentation_slides,
    scripture_range_reading_active,
    scripture_range_reading_state,
    slide_payload_to_holyrics_body,
    sync_scripture_range_reading,
    temporary_verse_display_active,
)


class LiveReferencePipelineTest(unittest.TestCase):
    def test_low_confidence_chapter_word_is_resolved_by_full_quoted_verse(self):
        address = "и павел говорит послание римляну четырнадцати два двенадцатый стих"
        quote = "итак каждый из нас за себя даст отчёт богу"
        words = [
            {"word": "четырнадцати", "conf": 0.608367},
            {"word": "два", "conf": 0.147535},
            {"word": "двенадцатый", "conf": 0.749157},
        ]
        result = LiveReferencePipeline().process_text(
            f"{address} {quote}", asr_result={"result": words},
        )
        self.assertEqual("Римлянам 14:12", result["parsed"]["ref"])
        self.assertEqual("Римлянам 14:2-12", result["address_text_correction"]["original_ref"])
        self.assertEqual([], result["ambiguous_alternatives"])

        for text, asr in (
            (address, {"result": words}),
            (f"{address} итак каждый из нас", {"result": words}),
            (f"{address} совсем другой прочитанный текст", {"result": words}),
            (f"{address} {quote}", None),
            (f"{address} {quote}", {"result": [words[0], {"word": "два", "conf": 0.9}, words[2]]}),
            (f"послание римляну четырнадцати с два по двенадцатый стих {quote}", {"result": words}),
            (f"послание римляну четырнадцати глава два двенадцатый стих {quote}", {"result": words}),
        ):
            with self.subTest(text=text, asr=asr):
                unchanged = LiveReferencePipeline().process_text(text, asr_result=asr)
                self.assertEqual("Римлянам 14:2-12", unchanged["parsed"]["ref"])
                self.assertNotIn("address_text_correction", unchanged)

    def test_generic_epistle_phrase_does_not_override_revelation_reference(self):
        result = parse_live_reference(
            "иисуса обращается к сэр к верующим филадельфии давайте сейчас "
            "откроем послание книгу откровений третью главу и прочитаем "
            "с седьмого по тринадцатый стих"
        )

        self.assertIsNotNone(result)
        self.assertEqual("Откровение 3:7-13", result.ref)
        self.assertEqual("Откровение", result.book)

    @unittest.skipUnless(sys.platform == "win32", "Real Tk popup regression on Windows")
    def test_missing_chapter_window_returns_without_wait_and_validates_chapter(self):
        import tkinter as tk
        from tools import vosk_grammar_probe as probe

        decisions = []
        hint = {"book": "Иаков", "start_verse": 6, "end_verse": 6}
        try:
            with patch.object(tk.Misc, "wait_variable", side_effect=AssertionError("blocks audio")):
                probe.popup_missing_chapter(hint, on_decision=decisions.append)
            root = probe._POPUP_TK_ROOT
            root.update()
            entry = next(w for w in root.winfo_children() if isinstance(w, tk.Entry))
            submit = next(w for w in root.winfo_children()
                          if isinstance(w, tk.Button) and "показать" in str(w.cget("text")))
            entry.insert(0, "999")
            submit.invoke()
            self.assertEqual([], decisions)
            entry.delete(0, "end")
            entry.insert(0, "5")
            submit.invoke()
            submit.invoke()
            self.assertEqual(1, len(decisions))
            self.assertEqual("Иаков 5:6", decisions[0]["parsed"]["ref"])
            closed_messages = []
            with patch.object(tk.Misc, "wait_variable", side_effect=AssertionError("blocks audio")):
                probe.show_popup_message("LiVerse", "Неверный адрес", on_decision=closed_messages.append)
            root.update()
            message_button = next(
                button for frame in root.winfo_children() for button in frame.winfo_children()
                if isinstance(button, tk.Button)
            )
            message_button.invoke()
            root.update()
            self.assertEqual([None], closed_messages)
        finally:
            probe.close_popup_tk_root()

    def test_failed_new_verse_or_list_keeps_previous_restore_timer(self):
        for slide_type in ("verse", "reference_list"):
            with self.subTest(slide_type=slide_type):
                args = SimpleNamespace(sermon_plan=True, holyrics_quick_minutes=20 / 60,
                    _holyrics_sermon_plan_presentation={"type": "text", "text_id": "plan"})
                payload = {"ref": "1 Коринфянам 11:25-26", "verse": "Текст"}
                if slide_type == "reference_list":
                    payload["slide_type"] = slide_type
                with (
                    patch("tools.holyrics.get_holyrics_current_presentation", return_value=None),
                    patch("tools.holyrics.prepare_sermon_plan_custom_theme", return_value=None),
                    patch("tools.holyrics.capture_holyrics_current_appearance"),
                    patch("tools.holyrics.post_holyrics_api", return_value=(False, "holyrics_error", "")),
                    patch("tools.holyrics.cancel_holyrics_restore_timer") as cancel,
                    patch("tools.holyrics.restore_holyrics_presentation_later") as schedule,
                ):
                    self.assertFalse(post_holyrics_url(args, "http://localhost:8091", payload)[0])
                cancel.assert_not_called()
                schedule.assert_not_called()

    def test_chapter_prompt_and_plan_share_nonblocking_queue(self):
        from tools.vosk_grammar_probe import JsonlLogger, PopupApprovalQueue
        from bible_parser_core.parser import DEFAULT_BIBLE

        args = SimpleNamespace(bible=DEFAULT_BIBLE, _popup_event_logger=None)
        queue = PopupApprovalQueue(args, JsonlLogger(None, enabled=False), [])
        hint = {"book": "Иаков", "start_verse": 6, "end_verse": 6}
        queue.submit_missing_chapter(hint, {})
        chapter_callbacks, plan_callbacks, message_callbacks, decisions = [], [], [], []
        with (
            patch("tools.vosk_grammar_probe.popup_missing_chapter",
                  side_effect=lambda *a, on_decision, **kw: chapter_callbacks.append(on_decision)),
            patch("tools.vosk_grammar_probe.popup_approval_decision",
                  side_effect=lambda *a, on_decision, **kw: plan_callbacks.append(on_decision)),
            patch("tools.vosk_grammar_probe.show_popup_message",
                  side_effect=lambda *a, on_decision, **kw: message_callbacks.append(on_decision)),
            patch("tools.vosk_grammar_probe.publish_after_approval") as publish,
        ):
            queue.pump()
            active = queue.active
            # New recognition can enqueue a plan while chapter entry stays open.
            queue.submit_operator_prompt("sermon_plan", {"ref": "План: слайд 5"}, decisions.append)
            queue.submit_operator_prompt("sermon_plan", {"ref": "План: слайд 5"}, decisions.append)
            queue.submit_operator_prompt("message", {"ref": "Ошибка", "message": "Ошибка"}, decisions.append)
            for _ in range(10):
                queue.pump()
            self.assertIs(active, queue.active)
            self.assertEqual(2, len(queue.pending))
            self.assertEqual([], plan_callbacks)
            self.assertEqual([], message_callbacks)
            chapter_callbacks[0](None)
            publish.assert_not_called()
            queue.pump()
            plan_callbacks[0]("approve")
            plan_callbacks[0]("approve")
            self.assertEqual(["approve"], decisions)
            self.assertIsNone(queue.active)
            queue.pump()
            message_callbacks[0](None)
            self.assertEqual(["approve", None], decisions)

    def test_chapter_completion_publishes_once_and_queue_survives_failure(self):
        from tools.vosk_grammar_probe import JsonlLogger, PopupApprovalQueue, complete_incomplete_reference
        from bible_parser_core.parser import DEFAULT_BIBLE

        args = SimpleNamespace(bible=DEFAULT_BIBLE, _popup_event_logger=None)
        queue = PopupApprovalQueue(args, JsonlLogger(None, enabled=False), [])
        hint = {"book": "Иаков", "start_verse": 6, "end_verse": 6}
        payload = complete_incomplete_reference(hint, 5)
        self.assertIsNotNone(payload)
        callbacks = []
        with (
            patch("tools.vosk_grammar_probe.popup_missing_chapter",
                  side_effect=lambda *a, on_decision, **kw: callbacks.append(on_decision)),
            patch("tools.vosk_grammar_probe.publish_after_approval", side_effect=RuntimeError("network failure")) as publish,
        ):
            queue.submit_missing_chapter(hint, {})
            queue.pump()
            callbacks[0](payload)
            callbacks[0](payload)
            publish.assert_called_once()
            self.assertIsNone(queue.active)
            queue.submit_missing_chapter(hint, {})
            queue.pump()
            self.assertEqual(2, len(callbacks))

    def test_church_song_and_quick_verse_discard_missing_plan_theme(self):
        args = SimpleNamespace(
            sermon_plan=True, holyrics_quick_minutes=0,
            _holyrics_sermon_plan_theme_id="1791089311750",
            _holyrics_sermon_plan_presentation={
                "type": "text", "text_id": "plan",
                "slides": [{"text": "План", "theme_id": "1791089311750"}],
            },
        )
        payload = {"ref": "1 Коринфянам 11:23", "verse": "Ибо я от Самого Господа принял",
                   "book": "1 Коринфянам", "chapter": 11, "start_verse": 23, "end_verse": 23}
        shown = []
        current = {"type": "song", "id": "song"}
        def api(_args, _url, endpoint, body):
            data = {
                "GetCurrentPresentation": current,
                "GetCurrentTheme": {"id": "temporary", "name": "Тема 9"},
                "GetCurrentBackground": {"id": "1E1E1E", "type": "color", "name": "Color #1E1E1E"},
                "GetThemes": [{"id": "bible-theme", "name": "Holyrics 01"}],
                "GetBackgrounds": [],
                "GetBibleSettings": {"theme": {"public": "bible-theme"}},
            }
            if endpoint == "ShowQuickPresentation":
                shown.append(body)
                self.assertEqual({"id": "bible-theme"}, body["slides"][0]["theme"])
            return True, "", json.dumps({"status": "ok", "data": data.get(endpoint, {})})

        with patch("tools.holyrics.post_holyrics_api", side_effect=api):
            self.assertTrue(post_holyrics_url(args, "http://localhost:8091", payload)[0])
            current = {"type": "quick_presentation", "id": "temporary-verse"}
            payload = {**payload, "ref": "1 Коринфянам 11:25-26", "start_verse": 25, "end_verse": 26}
            self.assertTrue(post_holyrics_url(args, "http://localhost:8091", payload)[0])
        self.assertEqual(2, len(shown))

    def test_missing_theme_and_unavailable_bible_theme_never_send_stale_id(self):
        args = SimpleNamespace(_holyrics_sermon_plan_theme_id="missing")
        responses = [
            (True, "", '{"data":{"name":"Transient"}}'),
            (True, "", '{"data":{"name":"Color","type":"color"}}'),
            (True, "", '{"data":[]}'),
            (True, "", '{"data":[]}'),
            (False, "timeout", ""),
        ]
        with patch("tools.holyrics.post_holyrics_api", side_effect=responses):
            self.assertIsNone(prepare_sermon_plan_custom_theme(args, "http://localhost:8091"))
        body = slide_payload_to_holyrics_body(args, {"ref": "Иаков 5:6"})
        self.assertNotIn("theme", body["slides"][0])

    def test_live_presentation_latency_reports_each_observable_stage(self):
        from tools.holyrics import build_live_presentation_latency_event

        fields = build_live_presentation_latency_event(
            {
                "audio_callback_monotonic": 10.0,
                "asr_final_monotonic": 10.2,
                "decision_ready_monotonic": 10.35,
                "approval_queued_monotonic": 10.4,
                "approval_decided_monotonic": 14.0,
                "approval_id": "approval_00001",
                "reference": "Иоанн 3:16",
            },
            "ShowQuickPresentation",
            completed_at=15.0,
            api_elapsed_ms=25.0,
        )

        self.assertIsNotNone(fields)
        self.assertEqual("Иоанн 3:16", fields["reference"])
        self.assertEqual("approval_00001", fields["approval_id"])
        self.assertEqual(200.0, fields["audio_callback_to_asr_final_ms"])
        self.assertEqual(150.0, fields["asr_final_to_decision_ready_ms"])
        self.assertEqual(4650.0, fields["decision_ready_to_holyrics_ack_ms"])
        self.assertEqual(5000.0, fields["audio_callback_to_holyrics_ack_ms"])
        self.assertEqual(3600.0, fields["approval_queue_wait_ms"])
        self.assertEqual(25.0, fields["holyrics_api_elapsed_ms"])
        self.assertIn("physical display time is not observed", fields["endpoint_semantics"])

    def test_popup_approval_queue_keeps_asr_side_nonblocking_and_fifo(self):
        from tools.vosk_grammar_probe import JsonlLogger, PopupApprovalQueue

        with tempfile.TemporaryDirectory() as temp_dir:
            args = SimpleNamespace(_popup_event_logger=None)
            logger = JsonlLogger(Path(temp_dir), enabled=False)
            approval_queue = PopupApprovalQueue(args, logger, [])
            first = {"slide": {"ref": "Иоанн 3:16"}}
            second = {"slide": {"ref": "Римлянам 6:23"}}
            first_result = approval_queue.submit(args, first)
            second_result = approval_queue.submit(args, second)
            self.assertEqual("waiting", first_result["action"])
            self.assertEqual(1, first_result["queue_position"])
            self.assertEqual(2, second_result["queue_position"])

            displayed = []

            def show(_slide, *, on_decision, **_kwargs):
                displayed.append(on_decision)

            with patch("tools.vosk_grammar_probe.popup_approval_decision", side_effect=show), patch(
                "tools.vosk_grammar_probe.finish_popup_approval",
                side_effect=lambda _args, _payload, action: {
                    "enabled": True,
                    "ok": True,
                    "action": action,
                    "proposed_ref": "",
                    "selected_ref": "",
                },
            ):
                approval_queue.pump()
                self.assertEqual(1, len(displayed))
                self.assertIs(first, approval_queue.active)
                displayed[0]("reject")
                self.assertIsNone(approval_queue.active)
                approval_queue.pump()
                self.assertEqual(2, len(displayed))
                self.assertIs(second, approval_queue.active)

    def test_queued_popup_retries_focus_only_briefly_when_not_focused(self):
        from tools.vosk_grammar_probe import schedule_popup_focus_retry

        class FakeRoot:
            def __init__(self):
                self.after_calls = []

            def after(self, delay, callback):
                self.after_calls.append((delay, callback))

        root = FakeRoot()
        retries = []
        self.assertTrue(
            schedule_popup_focus_retry(
                root,
                retries.append,
                focused=False,
                retry_number=1,
            )
        )
        self.assertEqual([200], [delay for delay, _callback in root.after_calls])
        root.after_calls[0][1]()
        self.assertEqual([2], retries)
        self.assertFalse(
            schedule_popup_focus_retry(
                root,
                retries.append,
                focused=True,
                retry_number=1,
            )
        )
        self.assertFalse(
            schedule_popup_focus_retry(
                root,
                retries.append,
                focused=False,
                retry_number=4,
            )
        )

    def test_performance_diagnostics_write_to_separate_optional_log(self):
        import json

        from tools.vosk_grammar_probe import JsonlLogger

        with tempfile.TemporaryDirectory() as temporary:
            logger = JsonlLogger(Path(temporary))
            logger.write("ordinary_event", {"value": 1})
            logger.write_performance("LIVE_PROCESSING_TIMING", {"process_cpu_percent": 12.5})

            self.assertEqual(
                "ordinary_event",
                json.loads((logger.run_dir / "events.jsonl").read_text(encoding="utf-8"))["event"],
            )
            performance = json.loads(
                (logger.run_dir / "performance.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual("LIVE_PROCESSING_TIMING", performance["event"])
            self.assertEqual(12.5, performance["process_cpu_percent"])

    def test_system_cpu_sample_calculates_delta_without_a_background_sampler(self):
        from tools.vosk_grammar_probe import system_cpu_percent_between

        self.assertEqual(25.0, system_cpu_percent_between((100, 60), (200, 135)))
        self.assertIsNone(system_cpu_percent_between(None, (200, 135)))

    def test_performance_summary_matches_known_correlations_and_weighted_cpu(self):
        from tools.analyze_vosk_probe_logs import summarize_performance

        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary) / "session"
            session.mkdir()
            rows = [
                {"event": "LIVE_PROCESSING_TIMING", "system_cpu_percent": cpu,
                 "asr_final_to_decision_ready_ms": duration, "pipeline_call_ms": duration / 2}
                for cpu, duration in ((0, 10), (50, 20), (100, 30), (None, 40))
            ]
            (session / "performance.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
            )
            result = summarize_performance(Path(temporary))["sessions"][0]

        self.assertEqual(4, result["measurements"])
        self.assertEqual(3, result["metrics"]["system_cpu_percent"]["count"])
        self.assertEqual(66.667, result["sampled_system_cpu_weighted_mean"])
        self.assertEqual(0.06, result["sampled_interval_seconds"])
        self.assertEqual(1.0, result["correlations_pearson"]["system_cpu_vs_processing"])
        self.assertEqual(1.0, result["correlations_pearson"]["parser_vs_processing"])
        self.assertEqual([2, 0, 1], [group["measurements"] for group in result["cpu_groups"]])

    def test_performance_summary_does_not_correlate_constant_or_missing_cpu(self):
        from tools.analyze_vosk_probe_logs import summarize_performance

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "performance.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in (
                {"event": "LIVE_PROCESSING_TIMING", "system_cpu_percent": 80,
                 "asr_final_to_decision_ready_ms": 10},
                {"event": "LIVE_PROCESSING_TIMING", "system_cpu_percent": 80,
                 "asr_final_to_decision_ready_ms": 20},
                {"event": "ordinary_event", "system_cpu_percent": 100},
            )), encoding="utf-8")
            result = summarize_performance(path)["sessions"][0]

        self.assertEqual(2, result["measurements"])
        self.assertIsNone(result["correlations_pearson"]["system_cpu_vs_processing"])
        self.assertIsNone(result["correlations_pearson"]["parser_vs_processing"])
        self.assertEqual({"count": 0}, result["metrics"]["audio_queue_items"])

    def test_popup_queue_confirms_references_individually_then_builds_list(self):
        from tools.vosk_grammar_probe import JsonlLogger, PopupApprovalQueue

        with tempfile.TemporaryDirectory() as temp_dir:
            args = SimpleNamespace(_popup_event_logger=None)
            logger = JsonlLogger(Path(temp_dir), enabled=False)
            approval_queue = PopupApprovalQueue(args, logger, [])
            payload = {
                "text": "Притчи один десять, Притчи два тринадцать",
                "reference_list_collection_id": "list_test",
                "session_plan_context": {
                    "presentation_id": "plan-1",
                    "presentation_name": "План проповеди",
                    "slide_number": 2,
                    "slide_text": "Пункт второй",
                },
                "reference_list": [
                    {"ref": "Притчи 1:10"},
                    {"ref": "Притчи 2:13"},
                ],
                "slide": {"ref": "Ссылки для чтения", "slide_type": "reference_list"},
            }
            result = approval_queue.submit(args, payload)
            self.assertEqual(2, result["queued_references"])
            self.assertEqual("Притчи 1:10", approval_queue.pending[0]["slide"]["ref"])
            self.assertEqual("Притчи 2:13", approval_queue.pending[1]["slide"]["ref"])
            second_candidate = approval_queue.pending[1]

            decisions = []

            def show(_slide, *, on_decision, **_kwargs):
                decisions.append(on_decision)

            with patch("tools.vosk_grammar_probe.popup_approval_decision", side_effect=show), patch(
                "tools.vosk_grammar_probe.finish_popup_approval",
                side_effect=lambda _args, item, action: {
                    "enabled": True,
                    "ok": True,
                    "action": action,
                    "proposed_ref": (item.get("slide") or {}).get("ref"),
                    "selected_ref": (item.get("slide") or {}).get("ref"),
                },
            ):
                approval_queue.pump()
                decisions[0]("approve")
                approval_queue.pump()
                self.assertEqual(2, len(decisions))
                decisions[1]("approve")
                self.assertEqual("reference_list", second_candidate["slide"]["slide_type"])
            self.assertEqual(1, len(approval_queue.session_refs))
            self.assertEqual("reference_list", approval_queue.session_refs[0]["kind"])
            self.assertEqual(["Притчи 1:10", "Притчи 2:13"], approval_queue.session_refs[0]["references"])
            self.assertEqual(2, approval_queue.session_refs[0]["plan_context"]["slide_number"])

    def test_session_summary_groups_references_and_multiple_lists_by_plan_slide(self):
        from tools.vosk_grammar_probe import session_references_text

        context = {
            "presentation_id": "plan-1",
            "presentation_name": "План проповеди",
            "slide_number": 3,
            "slide_text": "Бог показал Свою любовь во Христе",
        }
        text = session_references_text(
            [
                {"kind": "reference", "ref": "Иоанн 3:16", "plan_context": context},
                {
                    "kind": "reference_list",
                    "collection_id": "list-a",
                    "references": ["Римлянам 5:8", "1 Иоанна 4:9"],
                    "plan_context": context,
                },
                {
                    "kind": "reference_list",
                    "collection_id": "list-b",
                    "references": ["Галатам 2:20", "Иоанн 12:47"],
                    "plan_context": context,
                },
                {"kind": "reference", "ref": "Псалом 22:1", "plan_context": None},
            ]
        )
        self.assertIn("План проповеди — пункт/слайд 3", text)
        self.assertIn("Отдельные цитаты и диапазоны:\n1. Иоанн 3:16", text)
        self.assertIn("Список ссылок 1:\n1. Римлянам 5:8\n2. 1 Иоанна 4:9", text)
        self.assertIn("Список ссылок 2:\n1. Галатам 2:20\n2. Иоанн 12:47", text)
        self.assertIn("Вне пункта плана\nОтдельные цитаты и диапазоны:\n1. Псалом 22:1", text)

    def test_session_summary_captures_actual_active_text_plan_slide(self):
        from tools.vosk_grammar_probe import session_plan_context

        args = SimpleNamespace(sermon_plan=True, holyrics_url="http://localhost:8090")
        current = {
            "type": "text",
            "text_id": "plan-1",
            "name": "План",
            "slide_number": 2,
            "slides": [{"text": "Вступление"}, {"text": "Главный пункт"}],
        }
        with patch("tools.vosk_grammar_probe.get_holyrics_current_presentation", return_value=current):
            context = session_plan_context(args)
        self.assertEqual(
            {
                "presentation_id": "plan-1",
                "presentation_name": "План",
                "slide_number": 2,
                "slide_text": "Главный пункт",
            },
            context,
        )

    def test_session_collection_updates_keep_one_list_group(self):
        from tools.vosk_grammar_probe import append_session_reference

        records = []
        base = {
            "reference_list_collection_id": "list-1",
            "session_plan_context": {"slide_number": 4, "slide_text": "Пункт плана"},
            "slide": {"slide_type": "reference_list", "ref": "Ссылки для чтения"},
        }
        append_session_reference(
            records,
            {**base, "reference_list": [{"ref": "Матфей 5:7"}, {"ref": "Матфей 6:3"}]},
        )
        append_session_reference(
            records,
            {**base, "reference_list": [{"ref": "Матфей 5:7"}, {"ref": "Матфей 6:3"}, {"ref": "Матфей 7:8"}]},
        )

        self.assertEqual(1, len(records))
        self.assertEqual(
            ["Матфей 5:7", "Матфей 6:3", "Матфей 7:8"],
            records[0]["references"],
        )

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
            long_range_slide_mode="one_verse",
            long_range_operator_hints=True,
            smart_slide_streaming_control=True,
            text_operator_hints=True,
            open_operator_qr=False,
            text_detection_db=Path("bible_index.db"),
        )
        command = engine_command(
            config,
            project_root=Path("C:/LiVerse"),
            python_executable="pythonw.exe",
            popup_anchor=(640, 360),
        )

        self.assertEqual("pythonw.exe", command[0])
        self.assertIn("--semi-auto-approval", command)
        self.assertIn("--device-name", command)
        self.assertIn("Microphone (USB2.0 Device)", command)
        self.assertIn("--no-open-operator-qr", command)
        self.assertEqual(
            "one_verse",
            command[command.index("--long-range-slide-mode") + 1],
        )
        self.assertIn("--long-range-operator-hints", command)
        self.assertIn("--smart-slide-streaming-control", command)
        self.assertNotIn("--no-smart-slide-streaming-control", command)
        self.assertIn("--text-operator-hints", command)
        self.assertIn("--no-performance-diagnostics", command)
        self.assertEqual("640", command[command.index("--popup-anchor-x") + 1])
        self.assertEqual("360", command[command.index("--popup-anchor-y") + 1])
        self.assertNotIn("secret-token", command)

        diagnostic_command = engine_command(
            GuiConfig(performance_diagnostics=True),
            project_root=Path("C:/LiVerse"),
            python_executable="pythonw.exe",
        )
        self.assertIn("--performance-diagnostics", diagnostic_command)
        self.assertNotIn("--no-performance-diagnostics", diagnostic_command)

    def test_gui_holyrics_permission_help_sorts_permissions_by_action_name(self):
        from tools.liverse_gui import LiVerseGui

        app = LiVerseGui.__new__(LiVerseGui)
        with patch("tools.liverse_gui.messagebox.showinfo") as showinfo:
            app.show_permissions()

        message = showinfo.call_args.args[1]
        permissions = [
            line.removeprefix("• ")
            for line in message.splitlines()
            if line.startswith("• ")
        ]
        self.assertEqual(permissions, sorted(permissions))
        for prefix in ("Action", "Close", "Get", "Set", "Show"):
            positions = [index for index, item in enumerate(permissions) if item.startswith(prefix)]
            self.assertEqual(positions, list(range(positions[0], positions[-1] + 1)))
        self.assertIn("GetCurrentTheme", permissions)

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
        self.assertIn("--no-smart-slide-streaming-control", command)

    def test_gui_can_disable_streaming_slide_control_for_legacy_fallback(self):
        from tools.liverse_gui import GuiConfig, engine_command

        command = engine_command(
            GuiConfig(
                citation_detection_mode="hybrid_confirm",
                long_range_slide_mode="one_verse",
                smart_slide_streaming_control=False,
            )
        )

        self.assertIn("--no-smart-slide-streaming-control", command)
        self.assertNotIn("--smart-slide-streaming-control", command)

    def test_microphone_indicator_uses_decibel_scale(self):
        from tools.vosk_grammar_probe import audio_level_percent

        self.assertEqual(0, audio_level_percent(0))
        self.assertLess(audio_level_percent(300), audio_level_percent(3000))
        self.assertLess(audio_level_percent(3000), audio_level_percent(30000))
        self.assertEqual(100, audio_level_percent(32767))

    def test_pipeline_can_report_parse_and_rule_risk_timings_on_request(self):
        pipeline = LiveReferencePipeline()
        timings = {}

        result = pipeline.process_text(
            "Иоанн третья глава шестнадцатый стих",
            timing=timings,
        )

        self.assertEqual("Иоанн 3:16", result.get("parsed", {}).get("ref"))
        self.assertGreaterEqual(timings["reference_parse_ms"], 0.0)
        self.assertGreaterEqual(timings["rule_risk_ms"], 0.0)
        self.assertGreaterEqual(timings["pipeline_total_ms"], 0.0)

    def test_missing_chapter_is_not_invented_from_distorted_deuteronomy(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "есть ещё последовательность чем важнее истина тем она проще "
            "сказано слове божье почему второй законе считаю была "
            "четвёртый стих слушая израиля"
        )

        self.assertFalse(result.get("matched"))
        self.assertIsNone(result.get("parsed"))
        self.assertEqual(
            {
                "book": "Второзаконие",
                "start_verse": 4,
                "end_verse": 4,
                "source_text": result["text"],
            },
            result.get("incomplete_reference"),
        )

    def test_operator_can_complete_missing_chapter_with_validated_number(self):
        from tools.vosk_grammar_probe import complete_incomplete_reference

        hint = {"book": "Второзаконие", "start_verse": 4, "end_verse": 4}

        payload = complete_incomplete_reference(hint, 6)

        self.assertEqual("Второзаконие 6:4", payload.get("parsed", {}).get("ref"))
        self.assertEqual("operator_completed_reference", payload.get("source"))
        self.assertIsNone(complete_incomplete_reference(hint, 99))

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
        from tools.vosk_grammar_probe import approval_popup_dimensions, session_summary_dimensions

        self.assertEqual((760, 560), session_summary_dimensions(1920, 1080))
        self.assertEqual((720, 520), session_summary_dimensions(800, 600))
        self.assertEqual((500, 400), session_summary_dimensions(500, 400))
        self.assertEqual((760, 620), approval_popup_dimensions(1920, 1080, 620))
        self.assertEqual((720, 540), approval_popup_dimensions(800, 600, 900))

    def test_popup_windows_reuse_one_tk_interpreter_and_close_it_once(self):
        import tools.vosk_grammar_probe as probe

        class FakeRoot:
            def __init__(self):
                self.withdraw_calls = 0
                self.deiconify_calls = 0
                self.destroy_calls = 0
                self.title_value = ""
                self.unbound = []

            def withdraw(self):
                self.withdraw_calls += 1

            def deiconify(self):
                self.deiconify_calls += 1

            def destroy(self):
                self.destroy_calls += 1

            def winfo_children(self):
                return []

            def unbind(self, sequence):
                self.unbound.append(sequence)

            def title(self, value):
                self.title_value = value

        class FakeTk:
            def __init__(self):
                self.root = FakeRoot()
                self.tk_calls = 0

            def Tk(self):
                self.tk_calls += 1
                return self.root

        fake_tk = FakeTk()
        with patch.object(probe, "_POPUP_TK_ROOT", None), patch.object(
            probe, "_POPUP_TK_THREAD_ID", None
        ), patch("tools.vosk_grammar_probe.threading.get_ident", return_value=17):
            first = probe.popup_tk_window(fake_tk, "Первая цитата")
            second = probe.popup_tk_window(fake_tk, "Вторая цитата")
            probe.close_popup_tk_root()

        self.assertIs(first, fake_tk.root)
        self.assertIs(second, fake_tk.root)
        self.assertEqual("Вторая цитата", second.title_value)
        self.assertEqual(1, fake_tk.tk_calls)
        self.assertEqual(1, fake_tk.root.withdraw_calls)
        self.assertEqual(2, fake_tk.root.deiconify_calls)
        self.assertIn("<Tab>", fake_tk.root.unbound)
        self.assertEqual(1, fake_tk.root.destroy_calls)

    def test_approval_popup_stays_hidden_until_its_geometry_is_ready(self):
        import tools.vosk_grammar_probe as probe

        class FakeRoot:
            def __init__(self):
                self.withdraw_calls = 0
                self.deiconify_calls = 0

            def withdraw(self):
                self.withdraw_calls += 1

            def deiconify(self):
                self.deiconify_calls += 1

            def winfo_children(self):
                return []

            def unbind(self, _sequence):
                return None

            def title(self, _value):
                return None

        class FakeTk:
            def __init__(self):
                self.root = FakeRoot()

            def Tk(self):
                return self.root

        fake_tk = FakeTk()
        with patch.object(probe, "_POPUP_TK_ROOT", None), patch.object(
            probe, "_POPUP_TK_THREAD_ID", None
        ), patch("tools.vosk_grammar_probe.threading.get_ident", return_value=17):
            probe.popup_tk_window(fake_tk, "LiVerse", show=False)

        self.assertEqual(0, fake_tk.root.deiconify_calls)
        self.assertEqual(2, fake_tk.root.withdraw_calls)

    def test_windows_popup_focus_actions_detaches_foreground_input_queue(self):
        import tools.vosk_grammar_probe as probe

        class FakeUser32:
            def __init__(self):
                self.calls = []
                self.foreground = 700

            def GetAncestor(self, window, flag):
                self.calls.append(("GetAncestor", window, flag))
                return 500

            def GetForegroundWindow(self):
                self.calls.append(("GetForegroundWindow",))
                return self.foreground

            def GetWindowThreadProcessId(self, window, _process_id):
                self.calls.append(("GetWindowThreadProcessId", window))
                return 41

            def AttachThreadInput(self, current, foreground, attach):
                self.calls.append(("AttachThreadInput", current, foreground, attach))
                return 1

            def ShowWindow(self, window, command):
                self.calls.append(("ShowWindow", window, command))
                return 1

            def BringWindowToTop(self, window):
                self.calls.append(("BringWindowToTop", window))
                return 1

            def SetForegroundWindow(self, window):
                self.calls.append(("SetForegroundWindow", window))
                self.foreground = window
                return 1

            def SetActiveWindow(self, window):
                self.calls.append(("SetActiveWindow", window))
                return 1

            def SetFocus(self, window):
                self.calls.append(("SetFocus", window))
                return 1

        class FakeKernel32:
            def GetCurrentThreadId(self):
                return 17

        user32 = FakeUser32()
        result = probe.windows_popup_focus_actions(
            123,
            user32=user32,
            kernel32=FakeKernel32(),
        )

        self.assertEqual(500, result["top_level"])
        self.assertEqual(700, result["foreground_before"])
        self.assertEqual(500, result["foreground_after"])
        self.assertTrue(result["input_attached"])
        self.assertTrue(result["foreground_requested"])
        self.assertIn(("AttachThreadInput", 17, 41, True), user32.calls)
        self.assertIn(("AttachThreadInput", 17, 41, False), user32.calls)

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
            (newer / "performance.jsonl").write_text(
                '{"event":"LIVE_PROCESSING_TIMING","process_cpu_percent":12.5}\n',
                encoding="utf-8",
            )
            (newer / "audio.wav").write_bytes(b"audio")
            (newer / ".env").write_text("HOLYRICS_TOKEN=secret\n", encoding="utf-8")
            destination = root / "logs.zip"

            self.assertEqual([newer, older], list_log_sessions(root))
            self.assertEqual(2, create_log_archive([newer], destination))
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(
                    [f"{newer.name}/session.json", f"{newer.name}/performance.jsonl"],
                    archive.namelist(),
                )
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
        from tools.holyrics import set_live_latency_context

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

        set_live_latency_context(
            {
                "audio_callback_monotonic": 10.0,
                "asr_final_monotonic": 10.1,
                "decision_ready_monotonic": 10.2,
                "reference": "Иоанн 3:16",
            }
        )
        try:
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
        finally:
            set_live_latency_context(None)

        self.assertTrue(ok)
        self.assertEqual("", reason)
        self.assertIn('"status":"ok"', response)
        self.assertEqual(
            ["holyrics_api_request", "holyrics_api_response", "LIVE_PRESENTATION_LATENCY"],
            [item[0] for item in events],
        )
        request_event = events[0][1]
        response_event = events[1][1]
        latency_event = events[2][1]
        self.assertEqual("ShowQuickPresentation", request_event["endpoint"])
        self.assertEqual("theme-1", request_event["request_body"]["slides"][0]["theme"]["id"])
        self.assertIn("[скрыто]", request_event["request_body"]["diagnostic_note"])
        self.assertNotIn("token", request_event["base_url"])
        self.assertEqual(200, response_event["http_status"])
        self.assertEqual("Иоанн 3:16", latency_event["reference"])
        self.assertEqual("ShowQuickPresentation", latency_event["endpoint"])
        self.assertIn("physical display time is not observed", latency_event["endpoint_semantics"])
        self.assertEqual("[скрыто]", response_event["response_body"]["data"]["token"])
        self.assertNotIn("private-token", str(events))
        self.assertNotIn("private-token", urlopen.call_args.args[0].full_url.split("?")[0])

    def test_holyrics_transport_failures_return_failure_and_allow_next_request(self):
        from http.client import IncompleteRead, RemoteDisconnected
        from urllib.error import URLError

        class Response:
            status = 200

            def __init__(self, error=None):
                self.error = error

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                if self.error is not None:
                    raise self.error
                return b'{"status":"ok"}'

        failures = (
            URLError("connection refused"),
            TimeoutError("read timed out"),
            ConnectionResetError("connection reset"),
            RemoteDisconnected("server closed connection"),
            IncompleteRead(b'{"status":', 20),
        )
        for error in failures:
            for stage in ("connect", "read"):
                with self.subTest(error=type(error).__name__, stage=stage):
                    events = []
                    args = SimpleNamespace(
                        holyrics_token="secret",
                        _holyrics_event_logger=lambda name, row: events.append((name, row)),
                    )
                    failure = error if stage == "connect" else Response(error)
                    with patch(
                        "tools.holyrics.request.urlopen",
                        side_effect=[failure, Response()],
                    ) as urlopen:
                        ok, reason, _body = post_holyrics_api(
                            args, "http://127.0.0.1:8091", "ShowVerse", {"id": "43003016"}
                        )
                        self.assertFalse(ok)
                        self.assertTrue(reason.startswith("holyrics_unavailable:"))
                        # A recovered connection is usable for the next explicit request.
                        self.assertTrue(post_holyrics_api(
                            args, "http://127.0.0.1:8091", "ShowVerse", {"id": "43003017"}
                        )[0])
                        self.assertEqual(2, urlopen.call_count)
                    responses = [row for name, row in events if name == "holyrics_api_response"]
                    self.assertEqual([False, True], [row["ok"] for row in responses])

    def test_holyrics_response_requires_explicit_api_success(self):
        replies = (
            ('{"status":"ok"}', True),
            ('{"status":"ok","data":null}', True),
            ('{"map":{"key_ok":true}}', True),
            ('{"map":{"key_ok":"true"}}', True),
            ('{"status":"error","error":"cannot create slide"}', False),
            ('{"status":"ok","response":{"status":"error","error":"cannot create slide"}}', False),
            ('{"map":{"key_ok":true},"response":{"status":"error","error":"cannot create slide"}}', False),
            ('{"status":"error","map":{"key_ok":true}}', False),
            ('{"map":{"key_ok":false,"key_error":"not_found"}}', False),
            ("", False),
            ("<html>Server unavailable</html>", False),
            ('{"status":', False),
            ("null", False),
            ("[]", False),
            ('"ok"', False),
            ("true", False),
            ("200", False),
            ("{}", False),
        )
        for reply, expected in replies:
            with self.subTest(reply=reply):
                ok, reason = parse_holyrics_response(reply)
                self.assertEqual(expected, ok)
                if not expected:
                    self.assertTrue(reason)

    def test_holyrics_invalid_reply_does_not_report_successful_display(self):
        from tools.holyrics import set_live_latency_context

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"<html>Server unavailable</html>"

        events = []
        args = SimpleNamespace(
            holyrics_token="secret",
            _holyrics_event_logger=lambda name, row: events.append((name, row)),
        )
        set_live_latency_context({"audio_callback_monotonic": 1.0})
        try:
            with patch("tools.holyrics.request.urlopen", return_value=Response()):
                ok, _reason, _body = post_holyrics_api(
                    args, "http://127.0.0.1:8091", "ShowQuickPresentation", {"slides": []}
                )
        finally:
            set_live_latency_context(None)
        self.assertFalse(ok)
        self.assertEqual(
            ["holyrics_api_request", "holyrics_api_response"],
            [name for name, _row in events],
        )
        self.assertFalse(events[-1][1]["ok"])
        self.assertEqual(200, events[-1][1]["http_status"])

    def test_holyrics_http_error_body_timeout_preserves_http_failure(self):
        from urllib.error import HTTPError

        class BrokenBody:
            def read(self):
                raise TimeoutError("error body timed out")

            def close(self):
                pass

        events = []
        args = SimpleNamespace(
            holyrics_token="secret",
            _holyrics_event_logger=lambda name, row: events.append((name, row)),
        )
        error = HTTPError("http://127.0.0.1:8091", 401, "Unauthorized", None, BrokenBody())
        with patch("tools.holyrics.request.urlopen", side_effect=error):
            ok, reason, _body = post_holyrics_api(
                args, "http://127.0.0.1:8091", "ShowVerse", {"id": "43003016"}
            )
        self.assertFalse(ok)
        self.assertEqual("holyrics_http_401", reason)
        self.assertEqual(401, events[-1][1]["http_status"])

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
            long_range_slide_mode="one_verse",
            smart_slide_streaming_control=True,
        )
        restored_args = SimpleNamespace(
            approval_ui="web",
            device_name="",
            holyrics_theme="",
            holyrics_quick_minutes=0.0,
            long_range_slide_mode="compact",
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
        self.assertEqual("one_verse", restored_args.long_range_slide_mode)
        self.assertTrue(settings["smart_slide_streaming_control"])

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

        self.assertRegex(core_version, r"^\d+\.\d+\.\d+$")
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
                "GetThemes",
                "GetBackgrounds",
                "GetCurrentTheme",
                "GetCurrentBackground",
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
        self.assertEqual(
            (0, 0, 1920, 1080),
            xrandr_monitor_bounds(monitor_list, -1_000_000, -1_000_000),
        )

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

    def test_biblical_da_ne_phrase_does_not_fuzzy_match_acts(self):
        result = LiveReferencePipeline().process_text(
            "давайте посмотрим восемнадцатый девятнадцатый стих никто да не "
            "обольщает вас самовольным смиренномудрием и служением ангелов"
        )

        self.assertFalse(result.get("matched"))
        self.assertIsNone(result.get("parsed"))

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
        self.assertFalse(citation_recognition_paused(args, False, reference_list_collecting=True))
        self.assertFalse(citation_recognition_paused(args, True))

    def test_timed_verse_display_pauses_recognition_until_restore(self):
        from tools.holyrics import restore_holyrics_presentation_later

        diagnostic_events = []
        args = SimpleNamespace(
            _holyrics_event_logger=lambda event, payload: diagnostic_events.append((event, payload))
        )

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
        self.assertEqual(
            [
                "temporary_presentation_restore_timer_scheduled",
                "temporary_presentation_restore_timer_fired",
            ],
            [event for event, _payload in diagnostic_events],
        )

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

    def test_fuzzy_reference_list_requires_confirmation_even_if_model_rejects(self):
        from tools.vosk_grammar_probe import apply_ml_risk, approval_required_for_payload

        args = SimpleNamespace(
            require_approval=False,
            semi_auto_approval=True,
            risk_model_data={"loaded": True},
            risk_auto_reject_threshold=0.9,
        )
        payload = {
            "source": "parser_reference_list",
            "slide": {"reference_list": ["Галатам 2:20", "Римлянам 3:23"]},
            "risk_score": 0.95,
            "risk_reasons": ["fuzzy_book_match"],
        }
        with patch(
            "tools.vosk_grammar_probe.score_payload_with_model",
            return_value={
                "needs_confirmation": False,
                "auto_reject": True,
                "decision_reasons": ["model_low_risk"],
            },
        ):
            apply_ml_risk(args, payload)

        self.assertFalse(payload["ml_risk"]["auto_reject"])
        self.assertTrue(payload["ml_risk"]["needs_confirmation"])
        self.assertIn(
            "fuzzy_reference_list_requires_confirmation",
            payload["ml_risk"]["decision_reasons"],
        )
        self.assertTrue(approval_required_for_payload(args, payload))

    def test_fuzzy_reference_list_requires_confirmation_without_risk_model(self):
        from tools.vosk_grammar_probe import approval_required_for_payload

        args = SimpleNamespace(require_approval=False, semi_auto_approval=True)
        payload = {
            "source": "parser_reference_list",
            "slide": {"reference_list": ["Галатам 2:20", "Иоанн 12:47"]},
            "risk_reasons": ["fuzzy_book_match"],
        }

        self.assertTrue(approval_required_for_payload(args, payload))

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

    def test_legacy_theme_setting_is_not_applied_to_startup_args(self):
        from tools.vosk_grammar_probe import apply_saved_startup_settings

        args = SimpleNamespace(holyrics_theme="")
        with patch("tools.vosk_grammar_probe.setting_was_explicit", return_value=False):
            apply_saved_startup_settings(args, {"holyrics_theme": "deleted-user-theme"})

        self.assertEqual("", args.holyrics_theme)

    def test_legacy_theme_name_never_reaches_holyrics_bible_settings(self):
        from tools.holyrics import post_holyrics_url

        args = SimpleNamespace(holyrics_theme="deleted-user-theme", holyrics_quick_minutes=0)
        payload = {
            "ref": "Иоанн 3:16",
            "book": "Иоанн",
            "chapter": 3,
            "start_verse": 16,
            "end_verse": 16,
        }
        with patch("tools.holyrics.post_holyrics_api", return_value=(True, "", "")) as api:
            ok, _reason = post_holyrics_url(args, "http://127.0.0.1:8091", payload)

        self.assertTrue(ok)
        self.assertEqual("SetBibleSettings", api.call_args_list[0].args[2])
        self.assertEqual({"show_x_verses": 1}, api.call_args_list[0].args[3])
        self.assertEqual("ShowVerse", api.call_args_list[1].args[2])

    def test_saving_holyrics_connection_removes_legacy_theme_name(self):
        import tempfile

        from tools.holyrics import load_env_file, save_holyrics_env

        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "HOLYRICS_TOKEN=old-token\nHOLYRICS_PORT=8091\nHOLYRICS_THEME=deleted-user-theme\n",
                encoding="utf-8",
            )

            save_holyrics_env("new-token", 8091, env_path)

            self.assertNotIn("HOLYRICS_THEME", load_env_file(env_path))

    def test_startup_settings_do_not_persist_theme(self):
        import json
        import tempfile

        from tools.vosk_grammar_probe import save_startup_settings

        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            args = SimpleNamespace(
                _liverse_startup_settings_enabled=True,
                holyrics_theme="deleted-user-theme",
                holyrics_quick_minutes=0,
            )
            with patch("tools.vosk_grammar_probe.startup_settings_path", return_value=settings_path):
                save_startup_settings(args)

            self.assertNotIn("holyrics_theme", json.loads(settings_path.read_text(encoding="utf-8")))

    def test_sermon_plan_recovers_after_starting_on_quick_presentation(self):
        from bible_parser_core.live_pipeline import match_sermon_plan_slide
        from tools.vosk_grammar_probe import ensure_sermon_plan_for_recognition

        args = SimpleNamespace(
            sermon_plan=True,
            holyrics_url="http://127.0.0.1:8091/",
        )
        quick_presentation = {
            "type": "quick_presentation",
            "name": "Иаков 2:14-26",
        }
        text_presentation = {
            "type": "text",
            "name": "Иакова 4",
            "text_id": "plan-4",
            "slide_number": 1,
            "slides": [
                {"text": "Вступление"},
                {"text": "Вера без дел мёртвая вера"},
            ],
        }

        with patch(
            "tools.holyrics.get_holyrics_current_presentation",
            side_effect=[quick_presentation, text_presentation],
        ):
            plan = ensure_sermon_plan_for_recognition(
                args,
                None,
                pipeline_matched=False,
                long_passage_reading=False,
            )
            self.assertIsNone(plan)

            plan = ensure_sermon_plan_for_recognition(
                args,
                plan,
                pipeline_matched=False,
                long_passage_reading=False,
            )

        self.assertIsNotNone(plan)
        match = match_sermon_plan_slide(
            plan["slides"],
            ["вера без дел мёртвая вера"],
            current_index=int(plan["next_index"]),
        )
        self.assertIsNotNone(match)
        self.assertEqual(2, match["slide_number"])

    def test_sermon_plan_recovers_after_starting_on_song_presentation(self):
        from bible_parser_core.live_pipeline import match_sermon_plan_slide
        from tools.vosk_grammar_probe import ensure_sermon_plan_for_recognition

        args = SimpleNamespace(
            sermon_plan=True,
            holyrics_url="http://127.0.0.1:8091/",
        )
        song_presentation = {
            "type": "song",
            "name": "Благослови, душа моя",
        }
        text_presentation = {
            "type": "text",
            "name": "Иакова 4",
            "text_id": "plan-4",
            "slide_number": 1,
            "slides": [
                {"text": "Вступление"},
                {"text": "Вера без дел мёртвая вера"},
            ],
        }

        with patch(
            "tools.holyrics.get_holyrics_current_presentation",
            side_effect=[song_presentation, text_presentation],
        ):
            plan = ensure_sermon_plan_for_recognition(
                args,
                None,
                pipeline_matched=False,
                long_passage_reading=False,
            )
            self.assertIsNone(plan)

            plan = ensure_sermon_plan_for_recognition(
                args,
                plan,
                pipeline_matched=False,
                long_passage_reading=False,
            )

        self.assertIsNotNone(plan)
        match = match_sermon_plan_slide(
            plan["slides"],
            ["вера без дел мёртвая вера"],
            current_index=int(plan["next_index"]),
        )
        self.assertIsNotNone(match)
        self.assertEqual(2, match["slide_number"])

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
            "end_verse": 11,
        }

        self.assertFalse(action_selects_context("sent", slide))
        self.assertTrue(action_selects_context("approve_context", slide))

    def test_nested_reading_preserves_broad_context_and_restores_overlapping_range(self):
        pipeline = LiveReferencePipeline()
        broad = pipeline.process_text("Иаков пятая глава с первого по шестой стих")
        self.assertTrue(pipeline.set_context_range(broad))
        nested = pipeline.process_text("Иаков пятая глава с первого по третий стих")
        self.assertTrue(pipeline.set_context_range(nested))
        self.assertEqual("Иаков 5:1-6", pipeline.context_range["ref"])
        self.assertEqual("Иаков 5:1-3", nested["parsed"]["ref"])
        self.assertEqual("Иаков 5:4", pipeline.process_text("четвёртый стих")["parsed"]["ref"])
        for phrase in ("с четвёртого по восьмой стих", "пятая глава с четвёртого по восьмой стих"):
            with self.subTest(phrase=phrase):
                result = pipeline.process_text(phrase)
                self.assertEqual("Иаков 5:4-8", result["parsed"]["ref"])
                self.assertTrue(result["context_reference"])

    def test_overlapping_context_does_not_guess_far_end_or_other_chapter(self):
        for phrase in ("с четвёртого по девятый стих", "шестая глава с четвёртого по восьмой стих"):
            with self.subTest(phrase=phrase):
                pipeline = LiveReferencePipeline()
                pipeline.set_context_range({"book": "Иаков", "chapter": 5,
                                            "start_verse": 1, "end_verse": 6})
                result = pipeline.process_text(phrase)
                self.assertFalse(result.get("context_reference"))

    def test_new_book_or_noncontained_range_replaces_context(self):
        for reference in (
            {"book": "Иаков", "chapter": 5, "start_verse": 4, "end_verse": 8},
            {"book": "Иаков", "chapter": 4, "start_verse": 1, "end_verse": 3},
            {"book": "Иоанн", "chapter": 5, "start_verse": 1, "end_verse": 3},
        ):
            with self.subTest(reference=reference):
                pipeline = LiveReferencePipeline()
                pipeline.set_context_range({"book": "Иаков", "chapter": 5,
                                            "start_verse": 1, "end_verse": 6})
                self.assertTrue(pipeline.set_context_range(reference))
                self.assertEqual(reference["book"], pipeline.context_range["book"])
                self.assertEqual(reference["chapter"], pipeline.context_range["chapter"])
                self.assertEqual(reference["end_verse"], pipeline.context_range["end_verse"])

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
            patch("tools.holyrics.get_holyrics_current_presentation", return_value={
                "type": "text", "text_id": "sermon-plan", "slide_number": 1,
                "slides": [{"text": "План", "theme_id": "plan-theme"}],
            }),
            patch("tools.holyrics.prepare_sermon_plan_custom_theme", return_value=None),
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

    def test_reference_list_schedules_sermon_plan_restore(self):
        args = SimpleNamespace(
            sermon_plan=True,
            holyrics_quick_minutes=5 / 60.0,
            holyrics_theme="",
            _holyrics_sermon_plan_presentation={
                "type": "text",
                "text_id": "sermon-plan",
                "slide_number": 3,
            },
        )
        payload = {
            "slide_type": "reference_list",
            "ref": "Ссылки для чтения",
            "verse": "Притчи 1:10\nПритчи 2:13",
            "reference_list": [
                {"ref": "Притчи 1:10"},
                {"ref": "Притчи 2:13"},
            ],
            "slide": {
                "slide_type": "reference_list",
                "references": [
                    {"ref": "Притчи 1:10"},
                    {"ref": "Притчи 2:13"},
                ],
            },
        }

        with (
            patch("tools.holyrics.get_holyrics_current_presentation", return_value=None),
            patch("tools.holyrics.prepare_sermon_plan_custom_theme", return_value=None),
            patch("tools.holyrics.cancel_holyrics_restore_timer"),
            patch("tools.holyrics.restore_holyrics_presentation_later") as restore_later,
            patch("tools.holyrics.post_holyrics_api", return_value=(True, "", "")) as api,
        ):
            ok, reason = post_holyrics_url(args, "http://127.0.0.1:8091", payload)

        self.assertTrue(ok)
        self.assertEqual("show_quick_presentation:reference_list;temporary_list:0.0833333min", reason)
        restore_later.assert_called_once_with(
            args,
            "http://127.0.0.1:8091",
            {
                "type": "text",
                "text_id": "sermon-plan",
                "slide_number": 3,
            },
            5 / 60.0,
        )
        api.assert_called_once()

    def test_reference_list_without_active_plan_still_has_a_display_timer(self):
        payload = {
            "slide_type": "reference_list",
            "ref": "Ссылки для чтения",
            "verse": "Притчи 1:10\nПритчи 2:13",
        }
        for minutes, show_ok in ((5 / 60, True), (0.0, True), (5 / 60, False)):
            with self.subTest(minutes=minutes, show_ok=show_ok):
                args = SimpleNamespace(holyrics_quick_minutes=minutes)
                with (
                    patch("tools.holyrics.active_sermon_display_presentation", return_value=(None, False)),
                    patch("tools.holyrics.cancel_holyrics_restore_timer"),
                    patch("tools.holyrics.restore_holyrics_presentation_later") as restore_later,
                    patch("tools.holyrics.post_holyrics_api", return_value=(show_ok, "", "")),
                ):
                    ok, _reason = post_holyrics_url(args, "http://127.0.0.1:8091", payload)
                self.assertEqual(show_ok, ok)
                if show_ok and minutes > 0:
                    restore_later.assert_called_once_with(
                        args, "http://127.0.0.1:8091", None, minutes, quick_presentation=True
                    )
                else:
                    restore_later.assert_not_called()

    def test_reference_list_return_without_plan_closes_only_quick_presentation(self):
        args = SimpleNamespace()
        with patch(
            "tools.holyrics.post_holyrics_api",
            side_effect=[
                (True, "", '{"status":"ok"}'),
                (True, "", '{"status":"ok","data":null}'),
            ],
        ) as api:
            restore_holyrics_presentation(
                args, "http://127.0.0.1:8091", None, quick_presentation=True
            )
        self.assertEqual(
            ["CloseCurrentQuickPresentation", "GetCurrentQuickPresentation"],
            [entry.args[2] for entry in api.call_args_list],
        )

    def test_failed_quick_show_records_current_theme_and_background(self):
        args = SimpleNamespace(holyrics_token="secret")
        with patch(
            "tools.holyrics.post_holyrics_api",
            side_effect=[
                (True, "", '{"status":"ok","data":{"id":"theme-1","type":"theme"}}'),
                (True, "", '{"status":"ok","data":{"id":"image-1","type":"my_image"}}'),
                (True, "", '{"status":"ok","data":[{"id":"theme-1","font":{"name":"Arial"}},{"id":"theme-2"}]}'),
                (True, "", '{"status":"ok","data":[{"id":"image-1","type":"my_image"},{"id":"image-2"}]}'),
            ],
        ) as api:
            appearance = capture_holyrics_current_appearance(args, "http://127.0.0.1:8091")

        self.assertEqual(
            {
                "theme": {"ok": True, "reason": "", "data": {"id": "theme-1", "type": "theme"}},
                "background": {"ok": True, "reason": "", "data": {"id": "image-1", "type": "my_image"}},
                "theme_records": {"ok": True, "reason": "", "data": [{"id": "theme-1", "font": {"name": "Arial"}}]},
                "background_records": {"ok": True, "reason": "", "data": [{"id": "image-1", "type": "my_image"}]},
            },
            appearance,
        )
        self.assertEqual(
            [
                call(args, "http://127.0.0.1:8091", "GetCurrentTheme", {}),
                call(args, "http://127.0.0.1:8091", "GetCurrentBackground", {}),
                call(args, "http://127.0.0.1:8091", "GetThemes", {}),
                call(args, "http://127.0.0.1:8091", "GetBackgrounds", {}),
            ],
            api.call_args_list,
        )

    def test_dragged_background_uses_saved_theme_and_background_ids(self):
        args = SimpleNamespace(holyrics_token="secret", _holyrics_sermon_plan_theme_id="transient-id")
        with patch(
            "tools.holyrics.post_holyrics_api",
            side_effect=[
                (True, "", '{"status":"ok","data":{"id":"transient-id","type":"theme","name":"Тема 8"}}'),
                (True, "", '{"status":"ok","data":{"id":"transient-id","type":"my_image","name":"IMG"}}'),
                (True, "", '{"status":"ok","data":[{"id":"saved-theme","name":"Тема 8","font":{"name":"Arial"},"background":{"type":"image","id":"-4","adjust_type":"fill"}}]}'),
                (True, "", '{"status":"ok","data":[{"id":"saved-image","type":"my_image","name":"IMG"}]}'),
            ],
        ):
            custom_theme = prepare_sermon_plan_custom_theme(args, "http://127.0.0.1:8091")

        self.assertEqual(
            {"font": {"name": "Arial"}, "background": {"type": "my_image", "id": "saved-image"}},
            custom_theme,
        )
        self.assertEqual(
            {
                "slides": [{"text": "Иоанн 3:16", "custom_theme": custom_theme}],
            },
            slide_payload_to_holyrics_body(args, {"ref": "Иоанн 3:16"}),
        )

    def test_blank_text_presentation_theme_keeps_image_and_makes_text_readable(self):
        args = SimpleNamespace(holyrics_token="secret")
        with patch(
            "tools.holyrics.post_holyrics_api",
            side_effect=[
                (True, "", '{"data":{"name":"Holyrics 01"}}'),
                (True, "", '{"data":{"name":"Backdrop","type":"my_image"}}'),
                (True, "", '{"data":[{"id":"theme-1","name":"Holyrics 01",'
                            '"font":{"color":"F5F5F5"},'
                            '"shape_fill":{"enabled":false},'
                            '"background":{"type":"my_image","id":"null"}}]}'),
                (True, "", '{"data":[{"id":"image-1","name":"Backdrop",'
                            '"type":"my_image"}]}'),
            ],
        ):
            theme = prepare_sermon_plan_custom_theme(
                args, "http://127.0.0.1:8091", blank_presentation=True
            )

        self.assertEqual({"type": "my_image", "id": "image-1"}, theme["background"])
        self.assertEqual("FFFFFF", theme["font"]["color"])
        self.assertFalse(theme["shape_fill"]["enabled"])
        self.assertEqual("000000", theme["effect"]["outline_color"])
        self.assertEqual(1.5, theme["effect"]["outline_weight"])

    def test_blank_text_presentation_reuses_original_theme_after_quick_slide(self):
        args = SimpleNamespace(
            _holyrics_blank_text_restore_presentation={"type": "text", "text_id": "empty-plan"},
            _holyrics_blank_text_custom_theme_snapshot={
                "text_id": "empty-plan",
                "custom_theme": {"font": {"color": "FFFFFF"}, "background": {"id": "-4"}},
            },
        )

        with patch("tools.holyrics.post_holyrics_api") as api:
            custom_theme = prepare_sermon_plan_custom_theme(
                args,
                "http://127.0.0.1:8091",
                blank_presentation=True,
            )

        self.assertEqual(
            {"font": {"color": "FFFFFF"}, "background": {"id": "-4"}},
            custom_theme,
        )
        self.assertEqual(custom_theme, args._holyrics_sermon_plan_custom_theme)
        api.assert_not_called()

    def test_blank_text_presentation_uses_quick_verse_and_restores_blank_slide(self):
        blank = {
            "type": "text",
            "text_id": "blank-sermon",
            "slide_number": 2,
            "slides": [
                {"text": "", "theme_id": "image-1"},
                {"text": "", "theme_id": "image-1"},
            ],
        }
        args = SimpleNamespace(
            sermon_plan=True,
            holyrics_quick_minutes=5 / 60,
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
        readable_theme = {
            "background": {"type": "my_image", "id": "image-1"},
            "font": {"color": "FFFFFF"},
            "effect": {"outline_color": "000000", "outline_weight": 1.5},
            "shape_fill": {"enabled": False},
        }

        def prepare_theme(theme_args, _base_url, *, blank_presentation=False):
            self.assertTrue(blank_presentation)
            theme_args._holyrics_sermon_plan_custom_theme = readable_theme
            return readable_theme

        with (
            patch("tools.holyrics.get_holyrics_current_presentation", return_value=blank),
            patch("tools.holyrics.prepare_sermon_plan_custom_theme", side_effect=prepare_theme),
            patch("tools.holyrics.post_holyrics_api", return_value=(True, "", "")) as api,
            patch("tools.holyrics.restore_holyrics_presentation_later") as restore_later,
        ):
            ok, reason = post_holyrics_url(args, "http://127.0.0.1:8091", payload)

        self.assertTrue(ok)
        self.assertEqual(
            "show_quick_presentation:sermon_verse;temporary_verse:0.0833333min",
            reason,
        )
        self.assertEqual(
            [call(
                args,
                "http://127.0.0.1:8091",
                "ShowQuickPresentation",
                {"slides": [{
                    "text": "Иоанн 3:16\n\nИбо так возлюбил Бог мир...",
                    "custom_theme": readable_theme,
                }]},
            )],
            api.call_args_list,
        )
        restore_snapshot = restore_later.call_args.args[2]
        self.assertEqual("blank-sermon", restore_snapshot["text_id"])
        self.assertEqual(2, restore_snapshot["slide_number"])
        self.assertEqual(1, restore_snapshot["current_index"])
        self.assertEqual(blank["slides"], restore_snapshot["slides"])
        self.assertFalse(hasattr(args, "_holyrics_sermon_plan_presentation"))
        self.assertEqual("blank-sermon", args._holyrics_blank_text_restore_presentation["text_id"])

    def test_blank_text_restore_snapshot_survives_first_quick_presentation(self):
        args = SimpleNamespace(
            sermon_plan=True,
            holyrics_quick_minutes=5 / 60,
            holyrics_theme="",
            _holyrics_blank_text_restore_presentation={
                "type": "text",
                "text_id": "blank-sermon",
                "slide_number": 2,
                "slides": [{"text": ""}, {"text": ""}],
            },
        )
        quick = {"type": "quick_presentation", "slides": [{"text": "Иоанн 3:16"}]}
        payload = {
            "slide_type": "reference_list",
            "ref": "Ссылки для чтения",
            "verse": "Иоанн 3:16\nМатфей 5:7",
            "reference_list": [{"ref": "Иоанн 3:16"}, {"ref": "Матфей 5:7"}],
            "slide": {"slide_type": "reference_list", "references": []},
        }

        with (
            patch("tools.holyrics.get_holyrics_current_presentation", return_value=quick),
            patch("tools.holyrics.prepare_sermon_plan_custom_theme", return_value={"background": {"id": "image"}}),
            patch("tools.holyrics.cancel_holyrics_restore_timer"),
            patch("tools.holyrics.restore_holyrics_presentation_later") as restore_later,
            patch("tools.holyrics.post_holyrics_api", return_value=(True, "", "")),
        ):
            ok, reason = post_holyrics_url(args, "http://127.0.0.1:8091", payload)

        self.assertTrue(ok)
        self.assertEqual("show_quick_presentation:reference_list;temporary_list:0.0833333min", reason)
        self.assertEqual("blank-sermon", restore_later.call_args.args[2]["text_id"])

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
            patch("tools.holyrics.prepare_sermon_plan_custom_theme", return_value=None),
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
            "tools.holyrics.prepare_sermon_plan_custom_theme",
            return_value=None,
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

    def test_standalone_ordinal_in_speech_is_not_a_contextual_verse(self):
        pipeline = LiveReferencePipeline()
        context = pipeline.process_text("послание иакова пятая глава с первого по шестой стих")
        self.assertEqual("Иаков 5:1-6", context.get("parsed", {}).get("ref"))
        self.assertTrue(pipeline.set_context_range(context))

        ordinary_numbering = pipeline.process_text("третье")
        self.assertIsNone(ordinary_numbering.get("parsed"))
        self.assertFalse(ordinary_numbering.get("matched"))

        explicit_verse = pipeline.process_text("третье стих")
        self.assertEqual("Иаков 5:3", explicit_verse.get("parsed", {}).get("ref"))
        self.assertEqual("context_range", explicit_verse.get("source"))

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

    def test_context_range_preserves_explicit_compound_ordinal_subrange(self):
        pipeline = LiveReferencePipeline()
        self.assertTrue(pipeline.set_context_range({
            "book": "Марк",
            "chapter": 1,
            "start_verse": 21,
            "end_chapter": 1,
            "end_verse": 34,
        }))

        result = pipeline.process_text(
            "давайте прочитаем с двадцать первого по двадцать восьмой стих "
            "и приходит в капернаум и вскоре в субботу вошёл он в синагогу"
        )

        self.assertEqual("Марк 1:21-28", result.get("parsed", {}).get("ref"))
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

    def test_context_range_resolves_first_n_verses_as_range(self):
        pipeline = LiveReferencePipeline()
        self.assertTrue(pipeline.set_context_range({
            "book": "Ефесянам", "chapter": 5,
            "start_verse": 1, "end_chapter": 5, "end_verse": 14,
        }))
        result = pipeline.process_text(
            "первые два стиха сегодняшнего отрывка"
        )
        self.assertEqual("Ефесянам 5:1-2", result["parsed"]["ref"])
        self.assertEqual("context_range", result.get("source"))

    def test_context_range_ignores_quantity_phrase_in_two_verses(self):
        pipeline = LiveReferencePipeline()
        self.assertTrue(pipeline.set_context_range({
            "book": "Ефесянам", "chapter": 5,
            "start_verse": 1, "end_chapter": 5, "end_verse": 14,
        }))
        result = pipeline.process_text("в двух стихах")
        self.assertNotEqual("Ефесянам 5:2", (result.get("parsed") or {}).get("ref"))

    def test_context_range_ignores_quantity_phrase_in_these_six_verses(self):
        pipeline = LiveReferencePipeline()
        self.assertTrue(pipeline.set_context_range({
            "book": "Ефесянам", "chapter": 4,
            "start_verse": 1, "end_chapter": 4, "end_verse": 6,
        }))
        result = pipeline.process_text("в этих шести стихах")
        self.assertNotEqual("Ефесянам 4:6", (result.get("parsed") or {}).get("ref"))

    def test_ivan_says_ona_is_not_misread_as_jonah(self):
        pipeline = LiveReferencePipeline()
        result = pipeline.process_text(
            "иван говорит она третья глава двадцатый двадцать первый стих"
        )
        self.assertEqual("Иоанн 3:20-21", result["parsed"]["ref"])

    def test_first_john_reduced_to_ya_in_apostle_context(self):
        pipeline = LiveReferencePipeline()
        phrases = (
            "апостол я сказал в первом послании пятая глава десятая одиннадцати",
            "апостол я в первом послании пятая глава десятая одиннадцати",
            "я в первом послании пятая глава десятая одиннадцати",
            "я первом послании пятая глава десятая одиннадцати",
        )

        for phrase in phrases:
            with self.subTest(phrase=phrase):
                result = pipeline.process_text(phrase)
                self.assertEqual("1 Иоанна 5:10-11", result["parsed"]["ref"])

    def test_clipped_mark_address_and_incomplete_john_tail(self):
        mark = LiveReferencePipeline().process_text(
            "мар шестнадцатая глава пятнадцатый стих"
        )
        self.assertEqual("Марк 16:15", mark["parsed"]["ref"])

        john = LiveReferencePipeline().process_text(
            "иван евангелий от иада пятнадцатая гла"
        )
        self.assertIsNone(john.get("parsed"))
        self.assertEqual(
            {"book": "Иоанн", "chapter": 15},
            {
                "book": john["incomplete_reference"]["book"],
                "chapter": john["incomplete_reference"]["chapter"],
            },
        )

    def test_galatians_asr_phrase_does_not_borrow_previous_roman_context(self):
        result = LiveReferencePipeline().process_text(
            "римлянам 3 глава только вера действующая любовью познание "
            "голова там пятая шестой стихов"
        )

        self.assertEqual("Галатам 5:6", result.get("parsed", {}).get("ref"))

    def test_unique_thessalonians_distortion_does_not_fallback_to_first_john(self):
        result = LiveReferencePipeline().process_text(
            "первое послание числанник видится вот третья глава десятый стих"
        )

        self.assertIsNone(result.get("parsed"))
        self.assertFalse(result.get("matched"))
        self.assertEqual(
            "unreliable_fuzzy_book_fragment",
            result.get("blocked_weak_context"),
        )

    def test_fila_nikiison_is_first_thessalonians_not_philippians(self):
        result = LiveReferencePipeline().process_text(
            "перво послание фила никийсон вторая глава тринадцатый стих"
        )

        self.assertEqual("1 Фессалоникийцам 2:13", result.get("parsed", {}).get("ref"))

    def test_tens_misheard_as_single_digit_verse_in_chapter_range(self):
        result = LiveReferencePipeline().process_text(
            "второй тимофеич четвёртая глава седьмого восемьдесят "
            "подвигом добрым я подвязался течение совершил вируса хранил"
        )

        self.assertEqual("2 Тимофею 4:7-8", result.get("parsed", {}).get("ref"))

    def test_restarted_number_before_chapter_uses_number_next_to_chapter(self):
        pipeline = LiveReferencePipeline()
        observed = pipeline.process_text(
            "после того как бог вернулся к людям евангелия от иоанна один из "
            "первая глава одиннадцати стих пришёл к своим"
        )
        conflicting = pipeline.process_text(
            "евангелие от иоанна два совершенно первая глава одиннадцатый стих"
        )

        self.assertEqual("Иоанн 1:11", observed.get("parsed", {}).get("ref"))
        self.assertEqual("Иоанн 1:11", conflicting.get("parsed", {}).get("ref"))

    def test_ordinary_chapter_and_numbered_epistle_references_are_unchanged(self):
        chapter_ref = LiveReferencePipeline().process_text(
            "евангелие от иоанна первая глава одиннадцатый стих"
        )
        epistle_ref = LiveReferencePipeline().process_text(
            "второе послание коринфянам четвёртая глава пятый стих"
        )

        self.assertEqual("Иоанн 1:11", chapter_ref.get("parsed", {}).get("ref"))
        self.assertEqual("2 Коринфянам 4:5", epistle_ref.get("parsed", {}).get("ref"))

    def test_ephesians_ovsyana_alias(self):
        pipeline = LiveReferencePipeline()
        result = pipeline.process_text(
            "послание и овсяна в там третья глава шестнадцатый семнадцатый стих"
        )
        self.assertEqual("Ефесянам 3:16-17", result["parsed"]["ref"])

    def test_ephesians_k_ofisyana_alias_beats_fuzzy_epistle_fragment(self):
        result = LiveReferencePipeline().process_text(
            "давайте сейчас мы откроем сегодняшний отрывок это послание к "
            "офисянам пятая глава с пятнадцатого по двадцать первый стих "
            "если у кого-то на руках нет писания"
        )
        self.assertEqual("Ефесянам 5:15-21", result.get("parsed", {}).get("ref"))

    def test_second_timothy_nominative_form_preserves_chapter_three(self):
        pipeline = LiveReferencePipeline()
        result = pipeline.process_text("второй тимофей три двенадцать")
        self.assertEqual("2 Тимофею 3:12", result["parsed"]["ref"])

    def test_corinthians_replay_aliases(self):
        pipeline = LiveReferencePipeline()
        result = pipeline.process_text(
            "название коррейфеном второе послание карете на "
            "четвёртая глава с тринадцатого по восемнадцатый стих"
        )
        self.assertEqual("2 Коринфянам 4:13-18", result["parsed"]["ref"])

    def test_corinthians_standalone_replay_alias(self):
        result = LiveReferencePipeline().process_text(
            "второе послание карете на четвёртая глава с тринадцатого по восемнадцатый стих"
        )
        self.assertEqual("2 Коринфянам 4:13-18", result["parsed"]["ref"])

    def test_hebrews_asr_case_ending_is_not_replaced_by_later_revelation(self):
        result = LiveReferencePipeline().process_text(
            "послание евреи тринадцатого пятнадцатое шестнадцатый стих "
            "это предпоследняя книга нового зарыве это дал сразу перед "
            "книгооткровения вы можете открыть его откровению первую главу"
        )
        self.assertEqual("Евреям 13:15-16", result["parsed"]["ref"])

    def test_tenth_verse_as_tensok_range_distortion(self):
        pipeline = LiveReferencePipeline()
        result = pipeline.process_text(
            "послание ефесянам вторая глава с первого по десяток стих"
        )
        self.assertEqual("Ефесянам 2:1-10", result["parsed"]["ref"])

    def test_other_masculine_ordinal_ok_endings_in_ranges(self):
        for distorted, expected in (("первок", 1), ("четверток", 4), ("девяток", 9)):
            with self.subTest(distorted=distorted):
                result = LiveReferencePipeline().process_text(
                    f"послание ефесянам вторая глава с первого по {distorted} стих"
                )
                expected_ref = (
                    f"Ефесянам 2:{expected}"
                    if expected == 1
                    else f"Ефесянам 2:1-{expected}"
                )
                self.assertEqual(expected_ref, result["parsed"]["ref"])

    def test_non_y_ordinal_ok_forms_are_not_assumed(self):
        for distorted in ("второк", "треток", "шесток", "восьмок"):
            self.assertIn(
                f"по {distorted} стих",
                normalize_text(f"с первого по {distorted} стих"),
            )

    def test_compound_ordinal_ok_ending_in_range(self):
        result = LiveReferencePipeline().process_text(
            "послание ефесянам вторая глава с первого по двадцать первок стих"
        )
        self.assertEqual("Ефесянам 2:1-21", result["parsed"]["ref"])

    def test_psalm_quantity_uses_active_psalm_context(self):
        pipeline = LiveReferencePipeline()
        self.assertTrue(pipeline.set_context_range({
            "book": "Псалтирь", "chapter": 125,
            "start_verse": 1, "end_chapter": 125, "end_verse": 14,
        }))
        result = pipeline.process_text("псалом дачу пятый шестой стих")
        self.assertEqual("Псалтирь 125:5-6", result["parsed"]["ref"])

    def test_context_allows_only_nearby_two_verse_same_chapter_follow_up(self):
        pipeline = LiveReferencePipeline()
        self.assertTrue(pipeline.set_context_range({
            "book": "Иоанн", "chapter": 10, "start_verse": 1,
            "end_chapter": 10, "end_verse": 10,
        }))

        result = pipeline.process_text("четырнадцатый пятнадцатый стих")

        self.assertEqual("Иоанн 10:14-15", result.get("parsed", {}).get("ref"))
        self.assertEqual("context_nearby_same_chapter", result.get("source"))

    def test_nearby_context_beats_observed_false_fuzzy_book_without_book_words(self):
        pipeline = LiveReferencePipeline()
        self.assertTrue(pipeline.set_context_range({
            "book": "Иоанн", "chapter": 10, "start_verse": 1,
            "end_chapter": 10, "end_verse": 10,
        }))

        result = pipeline.process_text(
            "второй чем сегодня хочу сказать этот добрый пастырь он знает своих "
            "опять же он знает своих обеды четырнадцать пятнадцатый степень я ей с"
        )

        self.assertEqual("Иоанн 10:14-15", result.get("parsed", {}).get("ref"))
        self.assertEqual("context_nearby_same_chapter", result.get("source"))

    def test_explicit_split_address_beats_nearby_verse_from_previous_book_context(self):
        pipeline = LiveReferencePipeline()
        previous = {
            "book": "Ефесянам",
            "chapter": 3,
            "start_verse": 5,
            "end_chapter": 3,
            "end_verse": 9,
            "ref": "Ефесянам 3:5-9",
        }
        self.assertTrue(pipeline.set_context_range(previous))
        pipeline.last_parsed = dict(previous)

        first = {
            "text": "давайте сейчас прочитаем послание титу третья глава с шестого по",
            "result": [
                {"word": "давайте", "start": 995.82, "end": 996.58, "conf": 0.509223},
                {"word": "сейчас", "start": 996.58, "end": 996.9, "conf": 0.327818},
                {"word": "прочитаем", "start": 996.9, "end": 997.7, "conf": 0.69996},
                {"word": "посланиетиту", "start": 997.7, "end": 998.98, "conf": 0.655006},
                {"word": "третья", "start": 998.98, "end": 999.46, "conf": 0.525677},
                {"word": "глава", "start": 999.46, "end": 1000.3, "conf": 0.534846},
                {"word": "сшестого", "start": 1000.3, "end": 1001.26, "conf": 0.521081},
                {"word": "по", "start": 1001.26, "end": 1001.46, "conf": 0.449298},
            ],
        }
        last = {
            "text": "тринадцатый стих",
            "result": [
                {"word": "тринадцатый", "start": 1002.64, "end": 1003.48, "conf": 0.643806},
                {"word": "стих", "start": 1003.48, "end": 1004.04, "conf": 0.614346},
            ],
        }

        first_result = pipeline.process_text(first["text"], asr_result=first)
        result = pipeline.process_text(last["text"], asr_result=last)

        self.assertIsNone(first_result.get("parsed"))
        self.assertEqual("Титу 3:6-13", result.get("parsed", {}).get("ref"))
        self.assertEqual("parser", result.get("source"))
        self.assertEqual(1180, result.get("delta_ms"))
        self.assertEqual(
            [first["text"], last["text"]],
            result.get("vosk_buffer"),
        )

    def test_explicit_split_address_beats_nearby_verse_in_same_book_and_chapter(self):
        for prefix, expected, expected_source in (
            (
                "прочитаем Марка первая глава с шестого по",
                "Марк 1:6-13",
                "parser",
            ),
            (
                "прочитаем Марка первая глава",
                "Марк 1:13",
                "context_nearby_same_chapter",
            ),
        ):
            with self.subTest(expected=expected):
                pipeline = LiveReferencePipeline()
                self.assertTrue(
                    pipeline.set_context_range(
                        {
                            "book": "Марк",
                            "chapter": 1,
                            "start_verse": 5,
                            "end_chapter": 1,
                            "end_verse": 9,
                            "ref": "Марк 1:5-9",
                        }
                    )
                )

                first = pipeline.process_text(prefix, now_ms=1_000)
                result = pipeline.process_text("тринадцатый стих", now_ms=1_500)

                self.assertIsNone(first.get("parsed"))
                self.assertEqual(expected, result.get("parsed", {}).get("ref"))
                self.assertEqual(expected_source, result.get("source"))

    def test_john_316_asr_intro_does_not_turn_scripture_into_isaiah(self):
        result = LiveReferencePipeline().process_text(
            "сейчас скажу вам два отрыжка из писания я на три шестнадцать"
        )

        self.assertEqual("Иоанн 3:16", result.get("parsed", {}).get("ref"))
        self.assertNotEqual("Исаия 2:3-16", result.get("parsed", {}).get("ref"))

    def test_context_does_not_extend_to_a_distant_or_wide_follow_up(self):
        pipeline = LiveReferencePipeline()
        self.assertTrue(pipeline.set_context_range({
            "book": "Иоанн", "chapter": 10, "start_verse": 1,
            "end_chapter": 10, "end_verse": 10,
        }))

        distant = pipeline.process_text("пятнадцатый шестнадцатый стих")
        wide = pipeline.process_text("одиннадцатый тринадцатый стих")

        self.assertNotEqual("context_nearby_same_chapter", distant.get("source"))
        self.assertNotEqual("context_nearby_same_chapter", wide.get("source"))

    def test_context_preserves_two_spoken_subranges_without_filling_the_gap(self):
        pipeline = LiveReferencePipeline()
        self.assertTrue(pipeline.set_context_range({
            "book": "Ефесянам",
            "chapter": 3,
            "start_verse": 14,
            "end_chapter": 3,
            "end_verse": 21,
        }))

        result = pipeline.process_text(
            "давайте посмотрим четырнадцатый шестнадцатый стих и с двадцатого по двадцать первое"
        )

        self.assertTrue(result.get("matched"))
        self.assertEqual("context_subrange_list", result.get("source"))
        self.assertEqual(
            ["Ефесянам 3:14-16", "Ефесянам 3:20-21"],
            [item["ref"] for item in result.get("reference_list") or []],
        )

    def test_context_keeps_book_for_compact_follow_up_range(self):
        pipeline = LiveReferencePipeline()
        first = pipeline.process_text(
            "послание ефесянам четвёртая глава с одиннадцатого по "
            "тринадцатый стих"
        )
        self.assertEqual("Ефесянам 4:11-13", first.get("parsed", {}).get("ref"))
        self.assertTrue(pipeline.set_context_range(first))

        result = pipeline.process_text(
            "образом наше служение в церкви четвёртая глава "
            "одиннадцать тринадцати и он поставил одних апостолами "
            "других пророками иных евангелистами и учителями"
        )

        self.assertEqual("Ефесянам 4:11-13", result.get("parsed", {}).get("ref"))
        self.assertEqual("context_range", result.get("source"))

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

    def test_range_with_i_before_po_keeps_its_last_verse(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "послание иаково вторая глава с пятого стиха и по пятнадцатый стих"
        )

        self.assertEqual("Иаков 2:5-15", result.get("parsed", {}).get("ref"))

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

    def test_karenkoma_asr_alias_resolves_to_first_corinthians(self):
        result = LiveReferencePipeline().process_text(
            "первое послание каренкома одиннадцатая глава девятнадцатый стих"
        )

        self.assertEqual("1 Коринфянам 11:19", result.get("parsed", {}).get("ref"))

    def test_numbered_epistle_chapter_only_does_not_become_false_verse(self):
        result = LiveReferencePipeline().process_text(
            "в первом послании каренкома в одиннадцатой главе"
        )

        self.assertTrue(result.get("chapter_reference"))
        self.assertEqual("1 Коринфянам 11", result.get("parsed", {}).get("ref"))
        self.assertIsNone(result.get("parsed", {}).get("start_verse"))

    def test_second_corinthians_constant_root_vinova_asr_alias(self):
        result = LiveReferencePipeline().process_text(
            "второй постоянный корень винова третья глава четырнадцатый шестнадцатый стих"
        )

        self.assertEqual("2 Коринфянам 3:14-16", result.get("parsed", {}).get("ref"))

    def test_second_corinthians_karifinam_asr_alias(self):
        result = LiveReferencePipeline().process_text(
            "второму посланию карифинам четвёртая глава пятый шестой стих"
        )

        self.assertEqual("2 Коринфянам 4:5-6", result.get("parsed", {}).get("ref"))

    def test_second_corinthians_karefenom_asr_alias(self):
        result = LiveReferencePipeline().process_text(
            "второе послание карефеном пятая глава семнадцатый стих"
        )

        self.assertEqual("2 Коринфянам 5:17", result.get("parsed", {}).get("ref"))

    def test_numbered_epistle_number_is_not_used_as_chapter_without_stich(self):
        result = LiveReferencePipeline().process_text(
            "второму посланию карифинам четвёртая глава пятый"
        )

        self.assertEqual("2 Коринфянам 4:5", result.get("parsed", {}).get("ref"))

    def test_fused_po_ordinal_keeps_efesians_range(self):
        result = LiveReferencePipeline().process_text(
            "послание офися нам четвёртая глава с первого полшестой стих прочитаем"
        )

        self.assertEqual("Ефесянам 4:1-6", result.get("parsed", {}).get("ref"))

    def test_book_after_cross_chapter_range_is_recovered(self):
        result = LiveReferencePipeline().process_text(
            "семнадцатого стиха по первый стих четвёртой главы "
            "послание колося там третья глава"
        )

        self.assertEqual("Колоссянам 3:17-4:1", result.get("parsed", {}).get("ref"))

    def test_psalm_number_followed_by_pisala_asr_alias(self):
        result = LiveReferencePipeline().process_text(
            "сто сорок четвёртый писала первый второй стих "
            "всякий день буду благословлять тебя"
        )

        self.assertEqual("Псалтирь 144:1-2", result.get("parsed", {}).get("ref"))

    def test_psalm_fused_hundred_ordinal_asr_forms(self):
        for distorted, verse in (
            ("ступервый", 101),
            ("стувторой", 102),
            ("стутретьим", 103),
            ("стучетвертым", 104),
            ("ступятый", 105),
            ("стушестым", 106),
            ("стуседьмым", 107),
            ("стувосьмым", 108),
            ("студевятым", 109),
        ):
            with self.subTest(distorted=distorted):
                result = LiveReferencePipeline().process_text(
                    f"сто восемнадцатый псалом {distorted} стих"
                )
                self.assertEqual(
                    f"Псалтирь 118:{verse}", result.get("parsed", {}).get("ref")
                )

    def test_listed_psalm_number_before_book_word_is_preserved(self):
        result = LiveReferencePipeline().process_text(
            "тридцать третий псалом четвёртый стих уповайте на меня "
            "в салон пятьдесят один одиннадцать петь имени"
        )

        self.assertEqual("parser_reference_list", result.get("source"))
        self.assertEqual(
            ["Псалтирь 33:4", "Псалтирь 51:11"],
            [item.get("ref") for item in result.get("reference_list") or []],
        )

    def test_buffered_two_references_with_reading_text_forms_a_list(self):
        result = LiveReferencePipeline().process_text(
            "исследуйте писание либо вы думаете что через них имеете жизнь вечную "
            "они свидетельствуют обо мне можете записать еще два местописания "
            "второй тимофей а третья глава шестнадцатый семнадцатый стих и "
            "деяние семнадцатое глава одиннадцатый сих там говорится о том же"
        )

        self.assertEqual("parser_reference_list", result.get("source"))
        self.assertEqual(
            ["2 Тимофею 3:16-17", "Деяния 17:11"],
            [item.get("ref") for item in result.get("reference_list") or []],
        )

    def test_sherpa_sehi_range_keeps_both_verses(self):
        result = LiveReferencePipeline().process_text(
            "евангелие от матфея шестнадцатая глава и прочитаем с шестнадцатого по девятнадцатой сехи"
        )

        self.assertEqual("Матфей 16:16-19", result.get("parsed", {}).get("ref"))

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

    def test_iezekiel_sherpa_book_aliases(self):
        pipeline = LiveReferencePipeline()

        for alias in (
            "языкель",
            "книга пророка языке или",
            "языкиль",
            "книга пророка и языке или",
            "пророк языки",
            "книга про рокаизикиля",
            "книга пророка языке",
        ):
            with self.subTest(alias=alias):
                result = pipeline.process_text(f"{alias} десятая глава первый стих")
                self.assertEqual("Иезекииль 10:1", result.get("parsed", {}).get("ref"))

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

    def test_ephesians_recorded_asr_aliases(self):
        pipeline = LiveReferencePipeline()
        aliases = (
            "офисянам", "фисе", "послание ефестской церкви",
            "послание и фисиалам", "послание диффессиана", "ефисянам", "ефисяна",
            "и все нам", "ефисианам", "и фестианам", "ивстяном", "евстяном", "евсянам", "и сян",
            "и фестиана", "ефисиана", "послание ефисиана", "послание ефисианам",
        )
        for alias in aliases:
            with self.subTest(alias=alias):
                result = pipeline.process_text(f"{alias} четвёртая глава первый стих")
                self.assertEqual("Ефесянам 4:1", result.get("parsed", {}).get("ref"))

    def test_ephesians_sixth_chapter_sherpa_distortion_keeps_range(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "послание вся нам читаю глава с первого очетвёртый стих"
        )

        self.assertEqual("Ефесянам 6:1-4", result.get("parsed", {}).get("ref"))

    def test_ephesians_fused_vsyana_sherpa_distortion(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "послание всяна пятая глава четырнадцать стих встане спящий и воскреснее из мёртвых осветить тебя христос"
        )

        self.assertEqual("Ефесянам 5:14", result.get("parsed", {}).get("ref"))

    def test_ephesians_poslanie_vse_na_sherpa_distortion(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "послание все на пятое глава первой второй стих"
        )

        self.assertEqual("Ефесянам 5:1-2", result.get("parsed", {}).get("ref"))
        self.assertNotIn("fuzzy_book_match", result.get("risk_reasons") or [])

    def test_ephesians_vse_na_repair_requires_epistle_word(self):
        self.assertNotIn(
            "ефесянам",
            normalize_text("мы всё на пятое место положили"),
        )

    def test_ephesians_professionam_sherpa_distortion(self):
        result = LiveReferencePipeline().process_text(
            "и посмотрим опять же в послании профессионам первая глава давайте "
            "посмотрим четвёртый стих так как он избрал нас в нем прежде создания "
            "мира чтобы мы были святы и непорочны"
        )

        self.assertEqual("Ефесянам 1:4", result.get("parsed", {}).get("ref"))

    def test_ephesians_ofisya_na_sherpa_distortion_keeps_last_announced_range(self):
        pipeline = LiveReferencePipeline()
        incomplete = pipeline.process_text(
            "и прочитаем сегодня с первого по шестой стих два месяца назад я читал "
            "с первого по четырнадцати но сегодня будем читать с первого по шестой стих "
            "послание офися на четвёртая",
            now_ms=23_250,
        )
        self.assertFalse(incomplete.get("matched"))
        self.assertEqual("Ефесянам", incomplete.get("incomplete_reference", {}).get("book"))

        pipeline.process_text(
            "у вас первого по шестой стих если вы готовы давайте сейчас прочитаем",
            now_ms=29_000,
        )
        result = pipeline.process_text(
            "послание вся на четвёртая глава с первого пол шестого стиха",
            now_ms=33_500,
        )

        self.assertEqual("Ефесянам 4:1-6", result.get("parsed", {}).get("ref"))

    def test_explicit_book_and_chapter_do_not_use_earlier_discourse_numbers(self):
        pipeline = LiveReferencePipeline()
        incomplete = pipeline.process_text(
            "и сегодня мы увидим два вида мудрости первая земная вторая небесная "
            "и давайте сегодня прочитаем послание иакова третья глава",
            now_ms=0,
        )

        self.assertFalse(incomplete.get("matched"))
        self.assertIsNone(incomplete.get("parsed"))
        self.assertEqual("Иаков", incomplete.get("incomplete_reference", {}).get("book"))
        self.assertEqual(3, incomplete.get("incomplete_reference", {}).get("chapter"))

        result = pipeline.process_text(
            "с тринадцатого по восемнадцатый стих",
            now_ms=1_000,
        )
        self.assertEqual("Иаков 3:13-18", result.get("parsed", {}).get("ref"))

    def test_feminine_chapter_pair_without_verse_marker_is_ignored(self):
        result = LiveReferencePipeline().process_text(
            "воздающим агидрон его история рассказана в книге судей "
            "шестая седьмая книги судьи"
        )

        self.assertFalse(result.get("matched"))
        self.assertIsNone(result.get("parsed"))
        self.assertEqual(
            "ambiguous_feminine_chapter_pair",
            result.get("blocked_weak_context"),
        )

    def test_simple_compact_book_numbers_remain_supported(self):
        result = LiveReferencePipeline().process_text("судьи шесть семь")

        self.assertEqual("Судьи 6:7", result.get("parsed", {}).get("ref"))

    def test_one_verse_epistle_mention_without_address_is_ignored(self):
        samples = (
            "но всегда когда вопрос касается веры я могу сказать одну простую "
            "вещь я всегда привожу один стиха послание иакова веру",
            "месяц назад мы вместе с вами начали разбирать послание иакова "
            "послание одного из первых руководителей церкви одного из первых "
            "пасторов послание апостола якова оно явля",
        )

        for text in samples:
            with self.subTest(text=text):
                result = LiveReferencePipeline().process_text(text)
                self.assertFalse(result.get("matched"))
                self.assertIsNone(result.get("parsed"))
                self.assertEqual(
                    "ordinary_verse_mention",
                    result.get("blocked_weak_context"),
                )

        compact = LiveReferencePipeline().process_text("послание иакова один один")
        self.assertEqual("Иаков 1:1", compact.get("parsed", {}).get("ref"))

    def test_failed_numbered_colossians_attempt_is_not_a_reference_list_item(self):
        result = LiveReferencePipeline().process_text(
            "как мы это все используем и давайте сейчас откроем послание колосяну "
            "первую главу первое послание колосян послание колосяна первая глава "
            "и прочитаем с девятого по четырнадцатый стих"
        )

        self.assertEqual("Колоссянам 1:9-14", result.get("parsed", {}).get("ref"))
        self.assertEqual([], result.get("reference_list") or [])

    def test_leviticus_limits_sherpa_distortion(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("книга лимитов двадцатое глава девятый стиль")

        self.assertEqual("Левит 20:9", result.get("parsed", {}).get("ref"))

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

    def test_split_fessola_ni_keitsa_resolves_first_thessalonians_without_fuzzy_book_risk(self):
        text = (
            "первое послание фессола ни кейтса четвёртая глава "
            "с восьмого по пятнадцатый стих"
        )
        result = LiveReferencePipeline().process_text(text)

        self.assertEqual("1 Фессалоникийцам 4:8-15", result.get("parsed", {}).get("ref"))
        self.assertEqual(1.0, result["parsed"].get("confidence"))
        self.assertNotIn("fuzzy_book_match", result.get("risk_reasons") or [])
        self.assertLess(result.get("risk_score", 1.0), 0.9)
        self.assertTrue(
            all(
                item.get("book") == "1 Фессалоникийцам"
                for item in result.get("ambiguous_alternatives") or []
            )
        )

        unnumbered = LiveReferencePipeline().process_text(
            "фессола ни кейтса четвёртая глава восьмой стих"
        )
        self.assertFalse(unnumbered.get("matched"))

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

    def test_first_john_poznanie_ana_sherpa_distortion(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "первое познание ана четвёртая глава седьмой одиннадцатый стих"
        )

        self.assertEqual("1 Иоанна 4:7-11", result.get("parsed", {}).get("ref"))

    def test_poznanie_ana_repair_requires_named_chapter(self):
        self.assertNotIn(
            "1 иоанна",
            normalize_text("первое познание анализа помогает человеку"),
        )

    def test_split_john_alias(self):
        pipeline = LiveReferencePipeline()

        gospel = pipeline.process_text("евангелие от и о анна три шестнадцать")
        epistle = pipeline.process_text("первое послание и о анна пятая глава тринадцатый стих")

        self.assertEqual("Иоанн 3:16", gospel.get("parsed", {}).get("ref"))
        self.assertEqual("1 Иоанна 5:13", epistle.get("parsed", {}).get("ref"))

    def test_full_gospel_title_disambiguates_asr_iona_from_prophet_iona(self):
        pipeline = LiveReferencePipeline()

        self.assertEqual("евангелие", normalize_text("и в ангелие"))
        gospel = pipeline.process_text(
            "есть ещё да вот один отрывок которого мы тоже сегодня ещё обратимся "
            "и в ангелие от иона три шестнадцать ибо так возлюбил бок мир "
            "что отдал сына своего единородного"
        )
        full_title = pipeline.process_text("Евангелие от Иона три шестнадцать")
        prophet = pipeline.process_text("книга пророка Ионы первая глава третий стих")

        self.assertEqual("Иоанн 3:16", gospel.get("parsed", {}).get("ref"))
        self.assertEqual("Иоанн 3:16", full_title.get("parsed", {}).get("ref"))
        self.assertEqual("Иона 1:3", prophet.get("parsed", {}).get("ref"))

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

    def test_weak_fuzzy_prophet_book_does_not_combine_ordinary_ones_into_nehemiah(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "и вновь и вновь ещё напомнить одну просто один простой стих "
            "пророк или в общели про имею говорит я имею его основная намерение "
            "во благо а не во зло"
        )

        self.assertFalse(result.get("matched"))
        self.assertEqual("ordinary_one_expression", result.get("blocked_weak_context"))

    def test_ordinary_one_expressions_do_not_supply_reference_numbers(self):
        samples = (
            "послание евреям первая глава по одной простой причине",
            "послание евреям первая глава с одной стороны",
            "послание евреям первая глава одна хорошая книга",
            "послание евреям первая глава один хороший стих",
            "послание евреям первая глава одно дело",
            "послание евреям первая глава одна мысль",
            "послание евреям первая глава одна простая идея",
            "послание евреям первая глава одну простую вещь",
            "послание евреям с одной стороны вторая мысль",
        )

        for text in samples:
            with self.subTest(text=text):
                result = LiveReferencePipeline().process_text(text)

                self.assertFalse(result.get("matched"))
                self.assertEqual(
                    "ordinary_one_expression",
                    result.get("blocked_weak_context"),
                )

    def test_descriptive_good_verse_phrase_uses_general_one_expression_guard(self):
        reason = should_block_matched_payload(
            {
                "text": (
                    "послание евреям тоже есть одним один очень хороший стих "
                    "мы имеем такого первосещенника который знает все наши немощи"
                ),
                "source": "parser",
                "parsed": {
                    "book": "Евреям",
                    "ref": "Евреям 1:1",
                    "start_verse": 1,
                    "end_verse": 1,
                },
            }
        )

        self.assertEqual("ordinary_one_expression", reason)

    def test_explicit_references_with_one_survive_ordinary_one_filter(self):
        samples = (
            ("иеремия один один", "Иеремия 1:1"),
            ("послание иакова вторая глава первый стих", "Иаков 2:1"),
            (
                "иоанна первая глава шестнадцатый стих с одной стороны это важно",
                "Иоанн 1:16",
            ),
            ("псалом первый", "Псалтирь 1:1"),
        )

        for text, expected in samples:
            with self.subTest(text=text):
                result = LiveReferencePipeline().process_text(text)

                self.assertEqual(expected, result.get("parsed", {}).get("ref"))

    def test_person_named_yakov_does_not_supply_book_for_later_chapter_only_range(self):
        text = (
            "яков алфеев и семён зелот иуда брата иакова ксении "
            "единодушно пребывали в молитве и молениях с некоторыми жёнами "
            "и марию матерью иисусом из братьями его а давайте прочитаем ещё "
            "вторая глава с первого по восьмой стих"
        )

        self.assertIsNone(parse_live_reference(text))
        result = LiveReferencePipeline().process_text(text)
        self.assertFalse(result.get("matched"))
        self.assertNotEqual("Иаков 2:1-8", (result.get("parsed") or {}).get("ref"))

    def test_confident_book_without_chapter_marker_still_parses(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("иеремия один один")

        self.assertEqual("Иеремия 1:1", result.get("parsed", {}).get("ref"))

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

    def test_sherpa_rechi_in_open_proverbs_range_keeps_address_priority(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(
            pipeline.process_text("и если мы откроем речи первого главу", now_ms=1_000).get("matched")
        )
        result = pipeline.process_text(
            "мы прочитаем очень простые слова первая глава с первого по шестой стих "
            "три часа сына давидова царя израильского чтобы",
            now_ms=2_000,
        )

        self.assertEqual("Притчи 1:1-6", result.get("parsed", {}).get("ref"))

    def test_sherpa_matveevich_gospel_distortion_keeps_full_range(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "еванглий от матвеевич двенадцатая глава сорок шестой пятидесятый стих"
        )

        self.assertEqual("Матфей 12:46-50", result.get("parsed", {}).get("ref"))

    def test_sherpa_incomplete_philippians_does_not_become_jude(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "и знаете до апостола павел говорит послание филиппицом "
            "презервал восьмой стих все почитая миром ради превосходства "
            "познания христа иисуса"
        )

        self.assertFalse(result.get("matched"))
        self.assertIsNone(result.get("parsed"))

    def test_sherpa_bytie_reverse_range_keeps_chapter_and_verses(self):
        pipeline = LiveReferencePipeline()
        text = (
            "и давайте прочитаем с первого по третий стих "
            "двадцать второй ваутни и бытья"
        )

        self.assertIn("бытия", normalize_text(text))
        result = pipeline.process_text(text)

        self.assertEqual("Бытие 22:1-3", result.get("parsed", {}).get("ref"))

    def test_sherpa_truncated_twenty_fourth_verse_does_not_become_range(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("деньга бытие вторая глава двадцать четвёрт стих")

        self.assertEqual("Бытие 2:24", result.get("parsed", {}).get("ref"))

    def test_sherpa_truncated_twenty_ordinal_verse_endings_are_single_verses(self):
        pipeline = LiveReferencePipeline()
        endings = (
            ("перв", 21),
            ("втор", 22),
            ("трет", 23),
            ("четверт", 24),
            ("пят", 25),
            ("шест", 26),
            ("седьм", 27),
            ("восьм", 28),
            ("девят", 29),
        )

        for ending, verse in endings:
            with self.subTest(ending=ending):
                result = pipeline.process_text(
                    f"бытие двадцать четвертая глава двадцать {ending} стих"
                )
                self.assertEqual(f"Бытие 24:{verse}", result.get("parsed", {}).get("ref"))

    def test_sherpa_missing_final_letter_in_twenty_second_is_not_a_range(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "в послании галатер пятой главе двадца втором стихе перечисляют плоды духа"
        )

        self.assertEqual("Галатам 5:22", result.get("parsed", {}).get("ref"))

    def test_sherpa_missing_final_letter_in_thirty_second_is_a_single_verse(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("деяния седьмая глава тридца втором стихе")

        self.assertEqual("Деяния 7:32", result.get("parsed", {}).get("ref"))

    def test_sherpa_other_truncated_ordinal_verse_endings_are_single_verses(self):
        pipeline = LiveReferencePipeline()
        endings = (
            ("одиннад", 11),
            ("двенад", 12),
            ("тринад", 13),
            ("четырнад", 14),
            ("пятнад", 15),
            ("шестнад", 16),
            ("семнад", 17),
            ("восемнад", 18),
            ("девятнад", 19),
            ("двад", 20),
            ("трид", 30),
        )

        for ending, verse in endings:
            with self.subTest(ending=ending):
                result = pipeline.process_text(
                    f"бытие двадцать четвертая глава {ending} стих"
                )
                self.assertEqual(f"Бытие 24:{verse}", result.get("parsed", {}).get("ref"))

    def test_full_jude_apostle_name_still_parses(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("послание апостола иуды восьмой стих")

        self.assertEqual("Иуда 1:8", result.get("parsed", {}).get("ref"))

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
        self.assertTrue(result.get("blocked_no_book_context"))

    def test_explicit_numbered_timothy_still_works(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text("первое тимофею третья глава четвёртого по пятой стих")
        second = pipeline.process_text("второе тимофею третья глава четвёртого по пятой стих")

        self.assertEqual("1 Тимофею 3:4-5", first.get("parsed", {}).get("ref"))
        self.assertEqual("2 Тимофею 3:4-5", second.get("parsed", {}).get("ref"))

    def test_sherpa_compact_second_timothy_does_not_reuse_book_number_as_chapter(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "второго тимофея четыре-пять",
            asr_result={
                "text": "второго тимофея четыре-пять",
                "result": [
                    {"word": "второготимофея", "start": 13.78, "end": 15.06, "conf": 0.581297},
                    {"word": "четыре-пять", "start": 15.06, "end": 15.82, "conf": 0.640752},
                ],
            },
        )

        self.assertEqual("2 Тимофею 4:5", result.get("parsed", {}).get("ref"))

    def test_numbered_epistle_with_poslanie_does_not_use_book_number_as_chapter(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("второе послание коринфянам пятого восемнадцатый стих")

        self.assertEqual("2 Коринфянам 5:18", result.get("parsed", {}).get("ref"))

    def test_sherpa_distorted_first_corinthians_does_not_fall_back_to_naum(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "первая коренция нам тринадцать один",
            asr_result={
                "text": "первая коренция нам тринадцать один",
                "result": [
                    {"word": "первая", "start": 114.28, "end": 115.08, "conf": 0.468662},
                    {"word": "коренция", "start": 115.08, "end": 115.56, "conf": 0.207677},
                    {"word": "нам", "start": 115.56, "end": 115.64, "conf": 0.409035},
                    {"word": "тринадцать", "start": 115.64, "end": 116.04, "conf": 0.895366},
                    {"word": "один", "start": 116.04, "end": 116.24, "conf": 0.436365},
                ],
            },
        )

        self.assertEqual("1 Коринфянам 13:1", result.get("parsed", {}).get("ref"))
        self.assertFalse(pipeline.process_text("нам тринадцать один").get("matched"))

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

    def test_incomplete_address_bridges_short_asr_pause_before_long_range(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text(
            "сделать но я могу прочитать послание римляном восьмая глава",
            now_ms=18_750,
            asr_result={
                "result": [
                    {"start": 15.36, "end": 16.84, "word": "посланиеримляном"},
                    {"start": 16.84, "end": 17.16, "word": "восьмая"},
                    {"start": 17.16, "end": 17.56, "word": "глава"},
                ],
            },
        )
        self.assertFalse(first.get("matched"))

        result = pipeline.process_text(
            "тридцать пятый тридцать девятый с тридцать пятого по тридцать девятый стих",
            now_ms=25_250,
            asr_result={
                "result": [
                    {"start": 20.67, "end": 21.31, "word": "тридцать"},
                    {"start": 23.99, "end": 24.47, "word": "стих"},
                ],
            },
        )

        self.assertEqual("Римлянам 8:35-39", result.get("parsed", {}).get("ref"))
        self.assertTrue(result.get("buffer_kept_for_incomplete_address"))
        self.assertFalse(result.get("buffer_reset_by_gap"))

    def test_incomplete_address_does_not_bridge_long_pause(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(
            pipeline.process_text(
                "прочитать послание римляном восьмая глава", now_ms=1_000
            ).get("matched")
        )
        result = pipeline.process_text(
            "с тридцать пятого по тридцать девятый стих", now_ms=7_000
        )

        self.assertFalse(result.get("matched"))
        self.assertTrue(result.get("buffer_reset_by_gap"))

    def test_suspicious_feminine_first_stich_does_not_auto_match(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("послание ефесянам третью", now_ms=1_000).get("matched"))
        result = pipeline.process_text("первую стих", now_ms=2_000)

        self.assertFalse(result.get("matched"))
        self.assertEqual("suspicious_first_verse_form", result.get("blocked_weak_context"))

    def test_explicit_book_and_chapter_allow_asr_feminine_first_verse(self):
        result = LiveReferencePipeline().process_text(
            "послание ефисианам четвёртая глава первой стих"
        )

        self.assertEqual("Ефесянам 4:1", result.get("parsed", {}).get("ref"))

    def test_known_ephesians_alias_is_not_marked_fuzzy(self):
        result = LiveReferencePipeline().process_text(
            "так а теперь ещё раз прочитаем послание ефисянам третья глава тринадцатый стих"
        )

        self.assertEqual("Ефесянам 3:13", result.get("parsed", {}).get("ref"))
        self.assertEqual(1.0, result.get("parsed", {}).get("confidence"))
        self.assertNotIn("fuzzy_book_match", result.get("risk_reasons") or [])

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

    def test_false_start_before_feminine_chapter_is_not_a_cross_chapter_range(self):
        for text in (
            "лука двадцать четырнадцатая глава с двадцать восьмого по тридцатый стих",
            "лука двадцать семь четырнадцатая глава двадцать восьмой тридцатый стих",
        ):
            with self.subTest(text=text):
                result = LiveReferencePipeline().process_text(text)
                self.assertEqual("Лука 14:28-30", result.get("parsed", {}).get("ref"))

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

    def test_cross_chapter_range_with_sherpa_seha_distortion(self):
        result = LiveReferencePipeline().process_text(
            "если мы еванглие от матфея двадцать третьего главу "
            "с тридцать седьмого сеха по двадцать четвёртого глава второй стих"
        )

        self.assertEqual("Матфей 23:37-24:2", result.get("parsed", {}).get("ref"))

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

    def test_three_verse_reading_builds_ups_slides_but_two_verses_remain_temporary(self):
        from tools.vosk_grammar_probe import action_selects_context
        from tools.replay_audio_files import replay_long_passage
        for end_verse, expected_count in ((2, 0), (3, 3)):
            with self.subTest(end_verse=end_verse):
                result = LiveReferencePipeline().process_text(f"Иаков 5:1-{end_verse}")
                slides = scripture_range_quick_presentation_slides(result["parsed"], max_verses=1)
                self.assertEqual(expected_count, len(slides))
                self.assertEqual(bool(expected_count), action_selects_context("sent", result["parsed"]))
                self.assertEqual(bool(expected_count), replay_long_passage(result) is not None)
                if slides:
                    state = scripture_range_reading_state(result["parsed"], slides)
                    self.assertEqual([1, 2, 3], [t["verse"] for t in state["targets"]])

    def test_long_range_one_verse_mode_builds_one_verse_per_slide(self):
        pipeline = LiveReferencePipeline()
        result = pipeline.process_text(
            "евангелие от иоанна третья глава с шестнадцатого и до конца главы"
        )
        payload = result.get("slide") or result.get("parsed") or {}
        args = SimpleNamespace(holyrics_theme="", long_range_slide_mode="one_verse")

        body = scripture_range_quick_presentation_body(args, "http://127.0.0.1:8091", payload)

        self.assertIsNotNone(body)
        slides = body["slides"]
        self.assertEqual(21, len(slides))
        self.assertTrue(
            all(len(re.findall(r"(?m)^[0-9]+:[0-9]+[.]", slide["text"])) == 1 for slide in slides)
        )
        self.assertIn("3:16.", slides[0]["text"])
        self.assertIn("3:17.", slides[1]["text"])
        self.assertIn("3:36.", slides[-1]["text"])

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
        self.assertEqual(
            [1, 7, 12, 16],
            [item["start_verse"] for item in args._holyrics_scripture_range_reading["targets"]],
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
            patch("tools.holyrics.prepare_sermon_plan_custom_theme", return_value=None),
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

    def test_later_verse_synchronizes_one_verse_long_range_forward(self):
        args = SimpleNamespace(
            holyrics_url="http://127.0.0.1:8091",
            _holyrics_scripture_range_reading={
                "ref": "Матфей 24:40-46",
                "book": "Матфей",
                "book_id": 40,
                "current_index": 2,
                "targets": [
                    {
                        "slide_index": index,
                        "start_chapter": 24,
                        "start_verse": verse,
                        "chapter": 24,
                        "verse": verse,
                        "text": "стих",
                    }
                    for index, verse in enumerate(range(40, 47))
                ],
            },
        )
        verse_forty_three = SimpleNamespace(
            book_id=40,
            chapter=24,
            start_verse=43,
            end_verse=43,
        )

        with patch("tools.holyrics.post_holyrics_api", return_value=(True, "", "")) as api:
            result = handle_scripture_range_reading_match(args, verse_forty_three)

        self.assertTrue(result["advanced"])
        self.assertTrue(result["synchronized_forward"])
        self.assertEqual(3, args._holyrics_scripture_range_reading["current_index"])
        api.assert_called_once_with(
            args,
            "http://127.0.0.1:8091",
            "ActionGoToIndex",
            {"index": 3},
        )

    def test_operator_hint_keeps_current_slide_without_holyrics_command(self):
        args = SimpleNamespace(
            _holyrics_scripture_range_reading={
                "current_index": 0,
                "targets": [{"verse": 16}, {"verse": 17}],
            }
        )
        hint = {"current_index": 0, "target_index": 1}

        with patch("tools.holyrics.post_holyrics_api") as api:
            ok, reason = apply_scripture_range_operator_hint(args, "keep", hint)

        self.assertTrue(ok)
        self.assertEqual("operator_kept_current_slide", reason)
        self.assertEqual(0, args._holyrics_scripture_range_reading["current_index"])
        api.assert_not_called()

    def test_operator_hint_rejects_stale_manual_slide_before_moving(self):
        args = SimpleNamespace(
            holyrics_url="http://127.0.0.1:8091",
            _holyrics_scripture_range_reading={
                "current_index": 0,
                "targets": [{"verse": 16}, {"verse": 17}, {"verse": 18}],
            },
        )
        hint = {"current_index": 0, "target_index": 1}

        with patch(
            "tools.holyrics.get_holyrics_current_presentation",
            return_value={"type": "quick_presentation", "slide_number": 3},
        ), patch("tools.holyrics.post_holyrics_api") as api:
            ok, reason = apply_scripture_range_operator_hint(args, "apply", hint)

        self.assertFalse(ok)
        self.assertEqual("range_hint_stale", reason)
        self.assertEqual(2, args._holyrics_scripture_range_reading["current_index"])
        api.assert_not_called()

    def test_verse_inside_current_compact_slide_does_not_advance(self):
        args = SimpleNamespace(
            _holyrics_scripture_range_reading={
                "ref": "Матфей 24:40-46",
                "book": "Матфей",
                "book_id": 40,
                "current_index": 0,
                "targets": [
                    {
                        "slide_index": 0,
                        "start_chapter": 24,
                        "start_verse": 40,
                        "chapter": 24,
                        "verse": 44,
                        "text": "стих",
                    },
                    {
                        "slide_index": 1,
                        "start_chapter": 24,
                        "start_verse": 45,
                        "chapter": 24,
                        "verse": 46,
                        "text": "стих",
                    },
                ],
            }
        )
        verse_forty_three = SimpleNamespace(
            book_id=40,
            chapter=24,
            start_verse=43,
            end_verse=43,
        )

        with patch("tools.holyrics.post_holyrics_api") as api:
            result = handle_scripture_range_reading_match(args, verse_forty_three)

        self.assertFalse(result["matched_boundary"])
        self.assertNotIn("advanced", result)
        self.assertEqual(0, args._holyrics_scripture_range_reading["current_index"])
        api.assert_not_called()

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

    def test_stale_sermon_plan_is_ignored_while_long_range_opens(self):
        plan = {"type": "text", "text_id": "sermon-plan", "current_index": 0}
        args = SimpleNamespace(
            holyrics_url="http://127.0.0.1:8091",
            _holyrics_sermon_plan_presentation=plan,
            _holyrics_scripture_range_reading={
                "started_at_monotonic": 100.0,
                "current_index": 0,
                "targets": [{"verse": 14}, {"verse": 15}],
            },
        )
        with (
            patch(
                "tools.holyrics.get_holyrics_current_presentation",
                return_value={"type": "text", "text_id": "sermon-plan", "slide_number": 3},
            ),
            patch("tools.holyrics.time.monotonic", return_value=100.2),
        ):
            result = sync_scripture_range_reading(args)

        self.assertTrue(result["active"])
        self.assertEqual("waiting_for_quick_presentation", result["reason"])
        self.assertTrue(scripture_range_reading_active(args))
        self.assertEqual(0, plan["current_index"])

    def test_sermon_plan_restore_after_startup_grace_ends_long_range_mode(self):
        plan = {"type": "text", "text_id": "sermon-plan", "current_index": 0}
        args = SimpleNamespace(
            holyrics_url="http://127.0.0.1:8091",
            _holyrics_sermon_plan_presentation=plan,
            _holyrics_scripture_range_reading={
                "started_at_monotonic": 100.0,
                "current_index": 0,
                "targets": [{"verse": 14}, {"verse": 15}],
            },
        )
        with (
            patch(
                "tools.holyrics.get_holyrics_current_presentation",
                return_value={"type": "text", "text_id": "sermon-plan", "slide_number": 3},
            ),
            patch("tools.holyrics.time.monotonic", return_value=102.0),
        ):
            result = sync_scripture_range_reading(args)

        self.assertTrue(result["manual_restore"])
        self.assertEqual("sermon_plan_restored_manually", result["reason"])
        self.assertFalse(scripture_range_reading_active(args))

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

    def test_truncated_feminine_fourth_preserves_compound_chapter(self):
        result = LiveReferencePipeline().process_text(
            "бытие двадцать четвёрта глава второй четвёртый стих и сказал авраам "
            "рабу своему старшему в доме его управляющему всем что у него было положи руку"
        )

        self.assertEqual("Бытие 24:2-4", result.get("parsed", {}).get("ref"))

    def test_truncated_feminine_ordinals_are_restored_only_before_chapter(self):
        for text, expected in (
            ("матфея двадцать перва глава первый стих", "Матфей 21:1"),
            ("второзаконие тридцать втора глава первый стих", "Второзаконие 32:1"),
            ("исход сорокова глава первый стих", "Исход 40:1"),
        ):
            with self.subTest(text=text):
                result = LiveReferencePipeline().process_text(text)
                self.assertEqual(expected, result.get("parsed", {}).get("ref"))

        self.assertEqual("обычная пята попытка", normalize_text("обычная пята попытка"))

    def test_split_ordinal_before_verse_marker_restores_third_list_reference(self):
        from bible_parser_core.live_pipeline import resolve_reference_payload

        text = (
            "матфея пятая глава седьмой стих матфея шестая глава третий стих "
            "и матфея седьмая глава восьм ой стих"
        )
        result = resolve_reference_payload(text)

        self.assertEqual(
            ["Матфей 5:7", "Матфей 6:3", "Матфей 7:8"],
            [item["ref"] for item in result["reference_list"]],
        )
        self.assertEqual("восьм ой день", normalize_text("восьм ой день"))

    def test_fused_verse_marker_and_mark_book_do_not_hide_middle_list_item(self):
        from bible_parser_core.live_pipeline import resolve_reference_payload

        text = (
            "итак марка третья глава пятый стихмарка четвёртая глава "
            "шестой стих марка пятая глава седьмой стих"
        )
        result = resolve_reference_payload(text)

        self.assertEqual(
            ["Марк 3:5", "Марк 4:6", "Марк 5:7"],
            [item["ref"] for item in result["reference_list"]],
        )

    def test_truncated_tenth_verse_is_retained_as_third_list_item(self):
        from bible_parser_core.live_pipeline import resolve_reference_payload

        text = (
            "иакова третья глава восьмой стих иакова четвёртая глава девятый стих "
            "и иакова пятая глава десят стих"
        )
        result = resolve_reference_payload(text)

        self.assertEqual(
            ["Иаков 3:8", "Иаков 4:9", "Иаков 5:10"],
            [item["ref"] for item in result["reference_list"]],
        )

    def test_verse_range_without_recovered_chapter_does_not_default_to_chapter_one(self):
        for text in (
            "книга пророка истая глава с первого по третий стих",
            "книга пророка исаии глава с первого по третий стих",
            "книга пророка исаии с первого по третий стих",
        ):
            with self.subTest(text=text):
                result = LiveReferencePipeline().process_text(text)
                self.assertIsNone(result.get("parsed"))
                self.assertFalse(result.get("matched"))

        explicit = LiveReferencePipeline().process_text(
            "книга пророка исаии шестая глава с первого по третий стих"
        )
        self.assertEqual("Исаия 6:1-3", explicit.get("parsed", {}).get("ref"))

    def test_trailing_book_name_starts_next_reference_instead_of_stealing_numbers(self):
        result = LiveReferencePipeline().process_text("ивана три шестнадцать лука")

        self.assertEqual("Иоанн 3:16", result.get("parsed", {}).get("ref"))

    def test_masculine_asr_forms_restore_feminine_chapter_context(self):
        for distorted, expected in (
            ("возьмого", "Исаия 58:3"),
            ("тредьего", "Исаия 53:3"),
            ("сетьмого", "Исаия 57:3"),
            ("седьмого", "Исаия 57:3"),
        ):
            with self.subTest(distorted=distorted):
                result = LiveReferencePipeline().process_text(
                    f"исаия пятьдесят {distorted} три стих"
                )
                self.assertEqual(expected, result.get("parsed", {}).get("ref"))

        self.assertEqual(
            "возьмого 3 стих",
            normalize_text("возьмого три стих"),
        )

    def test_fused_feminine_chapter_forms_restore_compound_chapter(self):
        for text, expected in (
            ("откровение двадцать первоего шестой стих", "Откровение 21:6"),
            ("матфей второего первый стих", "Матфей 2:1"),
            ("исаия пятого шестой стих", "Исаия 5:6"),
            ("матфей двадцать третьего шестой стих", "Матфей 23:6"),
        ):
            with self.subTest(text=text):
                result = LiveReferencePipeline().process_text(text)
                self.assertEqual(expected, result.get("parsed", {}).get("ref"))

        self.assertNotIn("глава", normalize_text("обычная пятого раза"))

    def test_inflected_full_revelation_title_does_not_fall_back_to_john(self):
        result = LiveReferencePipeline().process_text(
            "об этом нам говорит откровением иоанна богослова "
            "двадцать первой глава шестой стих"
        )

        self.assertEqual("Откровение 21:6", result.get("parsed", {}).get("ref"))

        gospel = LiveReferencePipeline().process_text(
            "евангелие от иоанна двадцать первая глава шестой стих"
        )
        self.assertEqual("Иоанн 21:6", gospel.get("parsed", {}).get("ref"))

    def test_i_ona_before_chapter_is_john_but_ordinary_phrase_is_unchanged(self):
        result = LiveReferencePipeline().process_text(
            "и она пятнадцатая глава с четвертого по шестой стих"
        )
        self.assertEqual("Иоанн 15:4-6", result.get("parsed", {}).get("ref"))
        self.assertNotIn("иоанн", normalize_text("и она пришла домой"))

    def test_grala_distortion_restores_chapter_marker(self):
        result = LiveReferencePipeline().process_text(
            "и она пятнадцатая грала четвертой пятой стих"
        )
        self.assertEqual("Иоанн 15:4-5", result.get("parsed", {}).get("ref"))

    def test_plyasyat_distortion_restores_ecclesiastes_range(self):
        result = LiveReferencePipeline().process_text(
            "плясят пятая глава девятой пятнадцатый стих"
        )
        self.assertEqual(
            "Екклесиаст 5:9-15", result.get("parsed", {}).get("ref")
        )

    def test_thousand_noise_between_verse_bounds_becomes_range(self):
        result = LiveReferencePipeline().process_text(
            "евреям четвёртая глава четырнадцать тысяч шестнадцатая стих итак "
            "имея первосвященника великого прошедшего небеса иисуса сына божьего"
        )

        self.assertEqual("Евреям 4:14-16", result.get("parsed", {}).get("ref"))
        self.assertIn("4 глава 14-16 стих", normalize_text(
            "евреям четвёртая глава четырнадцать тысяч шестнадцатая стих"
        ))
        self.assertIn("14 1000", normalize_text("в 14 тысяч километрах"))

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

    def test_book_of_acts_asr_alias_and_split_chapter_context(self):
        pipeline = LiveReferencePipeline()

        book = pipeline.process_text("книга ения апостолов", now_ms=0)
        chapter = pipeline.process_text("вторая голова", now_ms=500)

        self.assertFalse(book.get("matched"))
        self.assertFalse(chapter.get("matched"))
        self.assertEqual(
            {"book": "Деяния", "chapter": 2, "source_text": "книга ения апостолов вторая голова"},
            chapter.get("book_chapter_context"),
        )

        expired = pipeline.process_text("продолжаем читать", now_ms=91_000)
        self.assertEqual({}, expired.get("book_chapter_context"))

    def test_book_of_acts_asr_alias_assembles_explicit_verse(self):
        pipeline = LiveReferencePipeline()
        pipeline.process_text("книга ения апостолов", now_ms=0)

        result = pipeline.process_text(
            "вторая голова сорок четвертый стих",
            now_ms=500,
        )

        self.assertEqual("Деяния 2:44", result.get("parsed", {}).get("ref"))

    def test_apostolov_without_deyaniya_is_book_only_in_full_address_context(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "апостолов в первую главу восьмой стих и когда примете силу блоха святого"
        )

        self.assertEqual("Деяния 1:8", result.get("parsed", {}).get("ref"))

        ordinary = pipeline.process_text("история апостолов в первую главу книги")
        self.assertFalse(ordinary.get("matched"))

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

    def test_colossians_kalachana_sherpa_distortion(self):
        result = LiveReferencePipeline().process_text(
            "прежде всего и все им стоит они послание калачана первая глава "
            "шестнадцатый семнадцатый стих"
        )

        self.assertEqual("Колоссянам 1:16-17", result.get("parsed", {}).get("ref"))

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

    def test_mentioned_chapter_is_single_address_not_a_reading_list(self):
        result = LiveReferencePipeline().process_text(
            "об этом мы можем прочитать о деяния апостолов пятнадцатой главе"
        )

        self.assertTrue(result.get("matched"))
        self.assertEqual("Деяния 15", result.get("parsed", {}).get("ref"))
        self.assertEqual("parser_mentioned_chapter_reference", result.get("source"))
        self.assertTrue(result.get("chapter_reference"))
        self.assertEqual([], result.get("reference_list") or [])

    def test_single_chapter_address_has_an_address_only_slide(self):
        from tools.vosk_grammar_probe import add_slide_payload

        result = add_slide_payload(
            LiveReferencePipeline().process_text(
                "об этом мы можем прочитать о деяния апостолов пятнадцатой главе"
            )
        )

        self.assertEqual("Деяния 15", result["slide"]["ref"])
        self.assertEqual("chapter_reference", result["slide"]["slide_type"])
        self.assertEqual("", result["slide"]["verse"])

    def test_overlapping_ranges_are_not_a_reading_list(self):
        result = LiveReferencePipeline().process_text(
            "марк первая глава с первого по третий стих "
            "марк первая глава со второго по четвёртый стих"
        )

        self.assertNotEqual("parser_reference_list", result.get("source"))
        self.assertEqual([], result.get("reference_list") or [])

    def test_repeated_range_with_same_start_uses_later_correction(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "и давайте сейчас откроем его нагляд марка первую ногу "
            "будем считать двадцать первого по тридцать четвёртый стих "
            "евангелие от марка первая глава с двадцать первого по двадцать четвёртый стих"
        )

        self.assertTrue(result.get("matched"))
        self.assertEqual("parser_repeated_range_correction", result.get("source"))
        self.assertEqual("Марк 1:21-24", result.get("parsed", {}).get("ref"))
        self.assertEqual("Марк 1:21-34", result.get("corrected_reference"))

    def test_repeated_identical_compact_reference_is_not_a_verse_range(self):
        cases = (
            (
                "числа двадцать третьего девятнадцатый стиль если вы не верите "
                "можете прочитать числа двадцать три девятнадцать",
                "Числа 23:19",
            ),
            (
                "евангелие от иоанна три шестнадцать иоанн три шестнадцать",
                "Иоанн 3:16",
            ),
        )
        for text, expected_ref in cases:
            with self.subTest(text=text):
                result = LiveReferencePipeline().process_text(text)
                self.assertEqual("parser_repeated_compact_reference", result.get("source"))
                self.assertEqual(expected_ref, result.get("parsed", {}).get("ref"))
                self.assertEqual([], result.get("reference_list") or [])

    def test_different_explicit_compact_references_are_still_a_list(self):
        result = LiveReferencePipeline().process_text(
            "числа двадцать третьего девятнадцатый стих "
            "числа двадцать третьего двадцатый стих"
        )

        self.assertEqual("parser_reference_list", result.get("source"))
        self.assertEqual(
            ["Числа 23:19", "Числа 23:20"],
            [item.get("ref") for item in result.get("reference_list") or []],
        )

    def test_pol_connector_and_partial_repeat_keep_complete_first_range(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "евангелие от матвея двадцать первая глава и давайте сначала прочитаем "
            "двадцать третьего пол двадцать седьмой стих иванка атмосфея "
            "двадцать первая глава двадцать третьего"
        )

        self.assertTrue(result.get("matched"))
        self.assertEqual("parser_repeated_range_fragment", result.get("source"))
        self.assertEqual("Матфей 21:23-27", result.get("parsed", {}).get("ref"))
        self.assertEqual("Матфей 21:23", result.get("corrected_reference"))

    def test_repeated_long_range_with_paused_second_end_keeps_first_range(self):
        result = LiveReferencePipeline().process_text(
            "и сегодня отрывок такой небольшой будет да это бытие двадцать четвёртая "
            "глава с первого по шестьдесят седьмой стих такая вот маленькая история да "
            "бытие двадцать четвёртая глава с первого по"
        )

        self.assertEqual("parser_repeated_range_fragment", result.get("source"))
        self.assertEqual("Бытие 24:1-67", result.get("parsed", {}).get("ref"))
        self.assertEqual("Бытие 24:1", result.get("corrected_reference"))
        self.assertEqual([], result.get("reference_list") or [])

    def test_polu_connector_keeps_both_bounds_of_announced_range(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "евангелие от марка вторая глава с первого полу двенадцатый стих"
        )

        self.assertTrue(result.get("matched"))
        self.assertEqual("Марк 2:1-12", result.get("parsed", {}).get("ref"))

    def test_two_separately_announced_verses_do_not_become_a_range(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "иван послание евреем одиннадцатого первый стих и евреям "
            "одиннадцатое глав шестой стихов а без веры угодить богу невозможно"
        )

        self.assertEqual("parser_reference_list", result.get("source"))
        self.assertEqual(["Евреям 11:1", "Евреям 11:6"], [
            item.get("ref") for item in result.get("reference_list") or []
        ])

    def test_two_chapter_references_with_one_named_book_are_a_list(self):
        result = LiveReferencePipeline().process_text(
            "потому что было время гонений да и мы опять же об этом времени мы читаем "
            "их деяния апостолов восьмая глава первый стих и одиннадцатая глава "
            "девятнадцатый стих многие были"
        )

        self.assertEqual("parser_same_book_chapter_reference_list", result.get("source"))
        self.assertEqual(["Деяния 8:1", "Деяния 11:19"], [
            item.get("ref") for item in result.get("reference_list") or []
        ])

    def test_reading_plan_keeps_single_verse_and_later_adjacent_range(self):
        result = LiveReferencePipeline().process_text(
            "положение очень серьёзное послание римляном десятая глава "
            "девятый тринадцатый и четырнадцатый стих ибо если устами твоими "
            "будешь исповедовать иисуса господам и стерством твоим вера что "
            "бог воскресил его из мёртвых"
        )

        self.assertTrue(result.get("matched"))
        self.assertEqual("parser_reference_list", result.get("source"))
        self.assertIsNone(result.get("parsed"))
        self.assertEqual(
            ["Римлянам 10:9", "Римлянам 10:13-14"],
            [item.get("ref") for item in result.get("reference_list") or []],
        )

    def test_reading_plan_does_not_reinterpret_continuous_range(self):
        result = LiveReferencePipeline().process_text(
            "послание римлянам десятая глава с девятого по четырнадцатый стих "
            "ибо всякий кто призовет имя господне спасется"
        )

        self.assertNotEqual("parser_reference_list", result.get("source"))
        self.assertEqual("Римлянам 10:9-14", result.get("parsed", {}).get("ref"))
        self.assertEqual([], result.get("reference_list") or [])

    def test_bare_verse_enumeration_does_not_become_a_reading_plan(self):
        result = LiveReferencePipeline().process_text(
            "послание римлянам десятая глава девятый тринадцатый и "
            "четырнадцатый стих"
        )

        self.assertNotEqual("parser_reference_list", result.get("source"))
        self.assertEqual([], result.get("reference_list") or [])

    def test_repeated_verse_numbers_do_not_become_a_reading_plan(self):
        result = LiveReferencePipeline().process_text(
            "послание римлянам десятая глава десятый и десятый стих "
            "ибо так возлюбил бог мир"
        )

        self.assertNotEqual("parser_reference_list", result.get("source"))
        self.assertEqual([], result.get("reference_list") or [])

    def test_cross_chapter_range_is_not_converted_to_a_reference_list(self):
        result = LiveReferencePipeline().process_text(
            "деяния апостолов восьмая глава первый стих по одиннадцатая глава "
            "девятнадцатый стих"
        )

        self.assertNotEqual("parser_same_book_chapter_reference_list", result.get("source"))

    def test_distorted_romans_book_is_recognized(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "послание аква первый стих послание риммина первая глава "
            "первой стихи павел раб иисуса христа"
        )

        self.assertEqual("Римлянам 1:1", result.get("parsed", {}).get("ref"))

    def test_ivan_gret_matfeev_does_not_become_john(self):
        result = LiveReferencePipeline().process_text(
            "давайте откроем и прочитаем иван грет матфеев двадцатая глава "
            "с первого по шестнадцатый стих"
        )

        self.assertEqual("Матфей 20:1-16", result.get("parsed", {}).get("ref"))

    def test_evangetana_shows_the_announced_john_range_without_waiting_for_repeat(self):
        result = LiveReferencePipeline().process_text(
            "евангетана двадцатая глава с двадцать четвертого по "
            "двадцать девятый стих"
        )

        self.assertEqual("Иоанн 20:24-29", result.get("parsed", {}).get("ref"))

    def test_u_vas_moy_recovers_eighth_verse_range_start(self):
        result = LiveReferencePipeline().process_text(
            "и вся на вторая глава у вас мой девятый стих апостол павел говорит "
            "ибо благодати вы спасены через веру не от дел"
        )

        self.assertEqual("Ефесянам 2:8-9", result.get("parsed", {}).get("ref"))

    def test_first_epistle_iana_keeps_the_first_john_book_number(self):
        result = LiveReferencePipeline().process_text(
            "первое послание иана третья глава с первого пол третий стих "
            "давайте сейчас прочитаем"
        )

        self.assertEqual("1 Иоанна 3:1-3", result.get("parsed", {}).get("ref"))

    def test_three_distorted_numbers_do_not_invent_a_long_range(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "и других пророков то есть автор послания время да вот он до "
            "тридцать второй головы а до три второго сека одиннадцатой головой "
            "он перечисляет многие и он говорит а"
        )

        self.assertFalse(result.get("matched"))
        self.assertIsNone(result.get("parsed"))

    def test_different_range_starts_remain_a_reference_list(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "евангелие от марка первая глава с двадцать первого по двадцать четвёртый стих "
            "евангелие от марка первая глава с двадцать пятого по двадцать восьмой стих"
        )

        self.assertEqual("parser_reference_list", result.get("source"))
        self.assertEqual(["Марк 1:21-24", "Марк 1:25-28"], [
            item.get("ref") for item in result.get("reference_list") or []
        ])

    def test_compact_references_from_different_books_are_returned_as_list(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("один пять и о анна три четыре иакова один два")

        self.assertTrue(result.get("matched"))
        self.assertIsNone(result.get("parsed"))
        self.assertEqual("parser_reference_list", result.get("source"))
        refs = [item.get("ref") for item in result.get("reference_list") or []]
        self.assertEqual(["Иоанн 3:4", "Иаков 1:2"], refs)

    def test_yuan_asr_alias_keeps_all_john_addresses_in_compact_list(self):
        result = LiveReferencePipeline().process_text(
            "иоанна три шестнадцать иоанна четыре семнадцать "
            "ивана пять восемнадцать юан шесть девятнадцать"
        )

        self.assertTrue(result.get("matched"))
        self.assertIsNone(result.get("parsed"))
        self.assertEqual("parser_reference_list", result.get("source"))
        self.assertEqual(
            ["Иоанн 3:16", "Иоанн 4:17", "Иоанн 5:18", "Иоанн 6:19"],
            [item.get("ref") for item in result.get("reference_list") or []],
        )

    def test_fuzzy_book_names_are_kept_in_compact_list_for_confirmation(self):
        from bible_parser_core.live_pipeline import add_risk_score

        result = LiveReferencePipeline().process_text(
            "галатом два двадцать римляным три двадцать три "
            "иоанна двенадцать сорок семь"
        )

        self.assertEqual("parser_reference_list", result.get("source"))
        self.assertEqual(
            ["Галатам 2:20", "Римлянам 3:23", "Иоанн 12:47"],
            [item.get("ref") for item in result.get("reference_list") or []],
        )
        add_risk_score(result)
        self.assertIn("fuzzy_book_match", result.get("risk_reasons") or [])

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

    def test_psalm_list_drops_range_before_psalm_number(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "и прочитаем о чем же этот салон да и мы прочитаем сначала с первого по "
            "четырнадцатый стих включительно в салон семьдесят второй с первого по "
            "четырнадцатый стих если вы готовы давайте сейчас прочитаем"
        )

        self.assertEqual("Псалтирь 72:1-14", result.get("parsed", {}).get("ref"))
        self.assertEqual([], result.get("reference_list") or [])

    def test_ecclesiastes_asr_alias_does_not_fall_back_to_hosea(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "книга и клисиаста пятая глава первый стих говорит очень интересные слова "
            "не торопись языком твоим"
        )

        self.assertEqual("Екклесиаст 5:1", result.get("parsed", {}).get("ref"))

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
            (
                "давайте сейчас пришла с четвёр по двенадцатый стих семьдесят второго салман",
                "Псалтирь 72:4-12",
            ),
        ):
            with self.subTest(text=text):
                result = LiveReferencePipeline().process_text(text)

                self.assertEqual(expected, result.get("parsed", {}).get("ref"))

    def test_truncated_genitive_range_starts_are_restored_only_in_verse_ranges(self):
        from bible_parser_core.parser import normalize_text

        for token, value in (
            ("перв", 1), ("втор", 2), ("треть", 3), ("четвер", 4),
            ("пят", 5), ("шест", 6), ("седьм", 7), ("седь", 7),
            ("восьм", 8), ("вось", 8), ("девят", 9), ("десят", 10),
            ("одиннадцат", 11), ("двенадцат", 12), ("девятнадцат", 19),
        ):
            with self.subTest(token=token):
                self.assertIn(
                    f"с {value} по 12 стих",
                    normalize_text(f"с {token} по двенадцатый стих"),
                )

        self.assertEqual("с перв по делам", normalize_text("с перв по делам"))

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

    def test_dangling_range_end_yields_complete_repeated_reference(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(
            pipeline.process_text(
                "небольшой отрывок который я сейчас хочу прочитать давайте откроем послание филиппийцам",
                now_ms=0,
            ).get("matched")
        )
        self.assertFalse(pipeline.process_text("первая глава", now_ms=500).get("matched"))
        self.assertFalse(
            pipeline.process_text(
                "мы знаем да что павел мафией да они обращаются к этой церкви да вот они "
                "не обозначают себя как официально не обозначают себя как друзья и прочитаем с третьего",
                now_ms=1_000,
            ).get("matched")
        )

        result = pipeline.process_text(
            "по седьмой стих послание филипсам первого глава с третьего по седьму стилю",
            now_ms=1_500,
        )

        self.assertEqual("Филиппийцам 1:3-7", result.get("parsed", {}).get("ref"))

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

    def test_fuzzy_book_match_with_low_confidence_requires_high_risk(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "написано небытие вторая глава от вас четвёртой стены а ставит",
            asr_result={
                "result": [
                    {"word": "написано", "start": 46.28, "end": 47.24, "conf": 0.588385},
                    {"word": "небытие", "start": 47.24, "end": 47.88, "conf": 0.410196},
                    {"word": "вторая", "start": 47.88, "end": 48.24, "conf": 0.852452},
                    {"word": "глава", "start": 48.24, "end": 48.52, "conf": 0.642504},
                    {"word": "от", "start": 48.52, "end": 48.68, "conf": 0.166842},
                    {"word": "вас", "start": 48.68, "end": 48.84, "conf": 0.195274},
                    {"word": "четвёртой", "start": 48.84, "end": 49.24, "conf": 0.626118},
                    {"word": "стены", "start": 49.24, "end": 50.12, "conf": 0.314994},
                    {"word": "а", "start": 50.12, "end": 50.16, "conf": 0.329638},
                    {"word": "ставит", "start": 50.16, "end": 50.64, "conf": 0.197898},
                ]
            },
        )

        self.assertEqual("Бытие 2:4", result.get("parsed", {}).get("ref"))
        self.assertEqual("high", result.get("risk_level"))
        self.assertIn("fuzzy_book_match", result.get("risk_reasons"))
        self.assertEqual(0.833, result.get("risk", {}).get("metrics", {}).get("book_match_confidence"))

    def test_ordinary_apostle_i_said_phrase_cannot_open_james_range(self):
        result = LiveReferencePipeline().process_text(
            "апостол я сказал в первом послании пятая глава десятая одиннадцати "
            "верующий сына божий имеет свидетельственность в себе самом"
        )

        self.assertFalse(result.get("matched"))
        self.assertIsNone(result.get("parsed"))

    def test_isaiah_soroka_alias_is_used_in_reference_list(self):
        result = LiveReferencePipeline().process_text(
            "пророк и слаия сорока третий стих малахия третья глава первый стих "
            "книга исход двадцать три"
        )

        references = [item["ref"] for item in result.get("reference_list") or []]
        self.assertIn("Исаия 40:3", references)
        self.assertIn("Малахия 3:1", references)

    def test_exact_ephesians_alias_beats_earlier_fuzzy_buffer_candidate(self):
        result = LiveReferencePipeline().process_text(
            "мы или вы мы вместе ну так как я часть церкви я должен сегодня уметь "
            "увидеть что бог с в слове сегодня я обращается конкретно ко мне "
            "давайте сейчас мы откроем сегодняшний отрывок это послание к офисянам "
            "пятая глава с пятнадцатого по двадцать первый стих"
        )

        self.assertEqual("Ефесянам 5:15-21", result.get("parsed", {}).get("ref"))

    def test_standalone_inflected_epistle_word_does_not_beat_james_book_mention(self):
        result = LiveReferencePipeline().process_text(
            "пишет в своём послании четвёртая глава восьмой десятый стих "
            "апостол веков пишет очень простые слова"
        )

        self.assertEqual("Иаков 4:8-10", result.get("parsed", {}).get("ref"))

    def test_verse_range_before_book_without_chapter_stays_incomplete(self):
        result = LiveReferencePipeline().process_text(
            "с первого по пятый стих послание к римлянам"
        )

        self.assertFalse(result.get("matched"))
        self.assertIsNone(result.get("parsed"))
        self.assertEqual("Римлянам", result.get("incomplete_reference", {}).get("book"))
        self.assertEqual(1, result.get("incomplete_reference", {}).get("start_verse"))
        self.assertEqual(5, result.get("incomplete_reference", {}).get("end_verse"))

    def test_first_n_chapters_discussion_is_not_a_scripture_reference(self):
        for phrase in (
            "первые три главы",
            "первые пять глав",
            "в первых десяти главах",
            "первых трёх глав",
        ):
            with self.subTest(phrase=phrase):
                result = LiveReferencePipeline().process_text(
                    f"без понимания {phrase} послания к ефесянам "
                    "мы не поймём четвёртую пятую и шестую главу послания"
                )

                self.assertFalse(result.get("matched"))

    def test_first_n_chapters_discussion_keeps_explicit_verse_reference(self):
        result = LiveReferencePipeline().process_text(
            "первые три главы послания к ефесянам важны, "
            "но прочитаем ефесянам четвёртая глава пятый стих"
        )

        self.assertEqual("Ефесянам 4:5", result.get("parsed", {}).get("ref"))

    def test_discussion_of_psalms_does_not_turn_unrelated_one_into_psalm_one(self):
        result = LiveReferencePipeline().process_text(
            "и когда мы читаем салмы там многие псаумы начинаются песни восхождение "
            "почему они так называется песн восхождения а не назвать так письма "
            "скажи мне по одной простой причине потому"
        )

        self.assertFalse(result.get("matched"))

    def test_kofessionam_distortion_selects_ephesians_not_first_john(self):
        result = LiveReferencePipeline().process_text(
            "и давайте для этого мы прочитаем первую половину сегодняшнего отрывка "
            "до послания кофессионам в третья глава с четырнадцатого девятнадцатый стих"
        )

        self.assertEqual("Ефесянам 3:14-19", result.get("parsed", {}).get("ref"))

    def test_standalone_plural_epistles_is_not_a_book_name(self):
        result = LiveReferencePipeline().process_text(
            "мы изучаем послания третья глава с четырнадцатого по девятнадцатый стих"
        )

        self.assertFalse(result.get("matched"))

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

    def test_joshua_recorded_asr_alias_иисуса_наверное(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("да иисуса наверное первая глава восьмой стих")

        self.assertEqual("Иисус Навин 1:8", result.get("parsed", {}).get("ref"))
        self.assertFalse(LiveReferencePipeline().process_text("иисуса наверное").get("matched"))

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

    def test_promotes_explicit_reference_assembled_from_book_and_range_fragments(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text(
            "итак еще раз давайте прочитаем с вами послание ефисианам"
        )
        self.assertFalse(first.get("matched"))

        second = pipeline.process_text("вторая глава одиннадцатая стих")

        self.assertTrue(second.get("matched"))
        self.assertEqual("Ефесянам 2:11", second.get("parsed", {}).get("ref"))
        self.assertTrue(second.get("promoted_assembled_reference"))

    def test_book_only_fragment_survives_short_pause_before_chapter_and_verse(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text(
            "итак сегодня будем читать послание ефисианам",
            now_ms=0,
        )
        self.assertFalse(first.get("matched"))

        second = pipeline.process_text(
            "пятая глава десятый стих",
            now_ms=5_400,
        )

        self.assertTrue(second.get("matched"))
        self.assertEqual("Ефесянам 5:10", second.get("parsed", {}).get("ref"))


class LiveSessionCheckTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.session = Path(self.temporary.name)
        self.metadata = {"liverse_version": "1.2.12", "mode": "microphone", "samplerate": 16000, "blocksize": 8000}
        start = datetime(2026, 10, 3, 10)
        self.events, self.timing = [], []
        for i in range(20):
            ts = (start + timedelta(seconds=i * 16)).isoformat()
            self.events.append({"event": "final_raw", "ts": ts, "text": "распознанная фраза",
                "audio_bytes_seen": (i+1)*32000, "live_timing": {"audio_callback_to_asr_final_ms": 100}})
            self.timing.append({"event": "LIVE_PROCESSING_TIMING", "ts": ts, "audio_bytes_seen": (i+1)*32000,
                "audio_callback_to_decision_ready_ms": 500, "asr_final_to_decision_ready_ms": 400,
                "pipeline_call_ms": 300, "reference_parse_ms": 250, "audio_queue_items": 0, "system_cpu_percent": 99})
        self.events.extend([
            {"event": "parsed", "payload": {"ref": "Иоанн 3:16", "reference_list": [{"ref": "Иоанн 3:16"}]}},
            {"event": "holyrics_api_response", "ok": True, "endpoint": "ShowVerse"},
            {"event": "STREAMING_SLIDE_CONTROL", "ok": True, "current_index": 0, "target_index": 1},
            {"event": "session_stopped", "reason": "operator_stop", "audio_queue_items": 0},
        ])

    def check(self):
        from tools.analyze_vosk_probe_logs import check_live_session
        (self.session / "session.json").write_text(json.dumps(self.metadata))
        for name, rows in (("events.jsonl", self.events), ("performance.jsonl", self.timing)):
            (self.session / name).write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        return check_live_session(self.session)

    def test_complete_fast_session_passes_only_conditionally_despite_high_cpu(self):
        from tools.analyze_vosk_probe_logs import format_session_check
        report = self.check()
        self.assertEqual("conditional", report["status"])
        self.assertEqual(20, report["measurements"])
        self.assertEqual(304, report["speech_span_seconds"])
        self.assertEqual(500, report["metrics"]["audio_callback_to_decision_ready_ms"]["p95"])
        self.assertIn("не подтверждают изображение", format_session_check(report))

    def test_a_single_large_delay_is_not_hidden_by_good_percentile(self):
        self.timing[4]["audio_callback_to_decision_ready_ms"] = 9000
        report = self.check()
        self.assertEqual("failed", report["status"])
        self.assertTrue(any("максимальная задержка" in r for r in report["failures"]))

    def plan_restore_after_absent_quick(self):
        # Recorded sequence from the live 03.10 test, including matched API IDs.
        start = datetime(2026, 10, 3, 10, 5, 5)
        rows = [
            {"event": "holyrics_api_request", "endpoint": "CloseCurrentQuickPresentation", "request_id": "close", "base_url": "http://localhost:8091"},
            {"event": "holyrics_api_response", "endpoint": "CloseCurrentQuickPresentation", "request_id": "close", "ok": False,
             "http_status": 200, "reason": "holyrics_error:No quick presentation available",
             "response_body": {"status": "error", "error": "No quick presentation available"}},
            {"event": "holyrics_api_request", "endpoint": "ShowText", "request_id": "show", "base_url": "http://localhost:8091",
             "request_body": {"id": "sermon-plan", "initial_index": 2}},
            {"event": "holyrics_api_response", "endpoint": "ShowText", "request_id": "show", "ok": True, "http_status": 200,
             "response_body": {"status": "ok"}},
            {"event": "holyrics_api_request", "endpoint": "GetCurrentPresentation", "request_id": "state", "base_url": "http://localhost:8091"},
            {"event": "holyrics_api_response", "endpoint": "GetCurrentPresentation", "request_id": "state", "ok": True, "http_status": 200,
             "response_body": {"status": "ok", "data": {"type": "text", "text_id": "sermon-plan", "slide_number": 3}}},
            {"event": "STREAMING_SLIDE_CONTROL", "ok": True, "reason": "sermon_plan_restore_verified", "action": "complete_range",
             "completed": True, "restored_sermon_plan": True},
        ]
        for i, row in enumerate(rows):
            row["ts"] = (start + timedelta(milliseconds=i * 30)).isoformat()
        return rows

    def test_verified_already_closed_plan_return_is_not_a_display_failure(self):
        from tools.analyze_vosk_probe_logs import format_session_check
        self.events[-1:-1] = self.plan_restore_after_absent_quick()
        report = self.check()
        self.assertEqual("conditional", report["status"])
        self.assertEqual(1, report["api_failed_replies"])
        self.assertEqual(1, report["api_verified_absent_quick_closes"])
        self.assertIn("возврат к нужному слайду плана подтверждён", format_session_check(report))

    def test_already_closed_does_not_hide_incomplete_wrong_or_unrelated_restore(self):
        original = list(self.events)
        changes = ("missing_state", "wrong_plan", "wrong_slide", "quick_still_active", "failed_show",
                   "wrong_request_id", "other_server", "missing_completion", "unrelated_output", "late_completion",
                   "transport_error", "other_endpoint", "missing_close_request", "unknown_result", "malformed_body",
                   "reused_request_id", "bad_http_status")
        for change in changes:
            with self.subTest(change=change):
                rows = self.plan_restore_after_absent_quick()
                if change == "missing_state":
                    rows.pop(5)
                elif change in ("wrong_plan", "wrong_slide", "quick_still_active"):
                    data = rows[5]["response_body"]["data"]
                    field, value = {"wrong_plan": ("text_id", "other-plan"), "wrong_slide": ("slide_number", 4),
                                    "quick_still_active": ("type", "quick_presentation")}[change]
                    data[field] = value
                elif change == "failed_show":
                    rows[3]["ok"] = False
                elif change == "wrong_request_id":
                    rows[5]["request_id"] = "other-state"
                elif change == "other_server":
                    rows[4]["base_url"] = "http://other-server:8091"
                elif change == "missing_completion":
                    rows.pop()
                elif change == "unrelated_output":
                    rows.insert(6, {"event": "holyrics_api_request", "endpoint": "ShowVerse"})
                elif change == "late_completion":
                    rows[-1]["ts"] = "2026-10-03T10:05:11"
                elif change == "transport_error":
                    rows[1]["reason"], rows[1]["http_status"] = "timeout", None
                elif change == "other_endpoint":
                    rows[0]["endpoint"] = rows[1]["endpoint"] = "ShowVerse"
                elif change == "missing_close_request":
                    rows.pop(0)
                elif change == "unknown_result":
                    rows[1]["ok"] = None
                elif change == "reused_request_id":
                    rows[4]["request_id"] = rows[5]["request_id"] = "show"
                elif change == "bad_http_status":
                    rows[5]["http_status"] = 500
                else:
                    rows[1]["response_body"] = '{'
                self.events = original[:-1] + rows + original[-1:]
                report = self.check()
                self.assertEqual("failed", report["status"])
                self.assertEqual(0, report["api_verified_absent_quick_closes"])

    def test_verified_close_keeps_other_failures_and_short_speech_visible(self):
        rows = self.plan_restore_after_absent_quick()
        self.events[-1:-1] = rows
        for i in range(20):
            ts = (datetime(2026, 10, 3, 10) + timedelta(seconds=279 * i / 19)).isoformat()
            self.events[i]["ts"] = self.timing[i]["ts"] = ts
        report = self.check()
        self.assertEqual("insufficient", report["status"])
        self.assertEqual(279, report["speech_span_seconds"])
        self.assertEqual([], report["failures"])
        self.events.insert(-1, {"event": "holyrics_api_response", "endpoint": "ShowVerse", "ok": False})
        report = self.check()
        self.assertEqual("failed", report["status"])
        self.assertEqual(2, report["api_failed_replies"])
        self.assertEqual(1, report["api_verified_absent_quick_closes"])
        self.assertTrue(any("Holyrics: 1" in r for r in report["failures"]))

    def test_growing_queue_and_undrained_stop_are_failures(self):
        for i, row in enumerate(self.timing):
            row["audio_queue_items"] = i // 2
        self.events[-1]["audio_queue_items"] = 7
        report = self.check()
        self.assertEqual("failed", report["status"])
        self.assertTrue(any("выросла" in r for r in report["failures"]))
        self.assertEqual(3.5, report["queue_at_stop_seconds"])

    def test_same_count_with_mismatched_or_invalid_samples_cannot_pass(self):
        for change in ("ids", "nan", "negative"):
            with self.subTest(change=change):
                original = dict(self.timing[4])
                if change == "ids":
                    self.timing[4]["audio_bytes_seen"] = 1
                else:
                    self.timing[4]["audio_callback_to_decision_ready_ms"] = float("nan") if change == "nan" else -1
                self.assertEqual("insufficient", self.check()["status"])
                self.timing[4] = original

    def test_intentionally_paused_recognition_is_not_a_missing_processing_sample(self):
        sample = self.timing.pop(4)
        self.events.insert(5, {"event": "TEMPORARY_VERSE_READING", "action": "recognition_paused", "audio_bytes_seen": sample["audio_bytes_seen"]})
        self.assertEqual("conditional", self.check()["status"])

    def test_replay_incomplete_scenarios_or_missing_stop_cannot_pass(self):
        self.metadata["mode"] = "audio_replay"
        self.assertEqual("insufficient", self.check()["status"])
        self.metadata["mode"] = "microphone"
        self.events[-1]["event"] = "other"
        self.assertEqual("insufficient", self.check()["status"])
        self.events[-1]["event"] = "session_stopped"
        self.events[-3]["endpoint"] = "GetCurrentPresentation"
        self.assertEqual("insufficient", self.check()["status"])
        self.events[-3]["endpoint"] = "ShowVerse"
        self.events[-4]["payload"]["reference_list"] = []
        self.assertEqual("insufficient", self.check()["status"])

    def test_missing_or_damaged_logs_never_pass(self):
        from tools.analyze_vosk_probe_logs import check_live_session
        self.check()
        path = self.session / "performance.jsonl"
        path.unlink()
        self.assertEqual("insufficient", check_live_session(self.session)["status"])
        for contents in ('{', '[1,2]', '\ufffd'):
            path.write_text(contents, encoding="utf-8")
            self.assertEqual("insufficient", check_live_session(self.session)["status"])

    def test_output_failures_take_precedence_over_short_test(self):
        self.events[-3]["ok"] = False
        self.events = [r for i,r in enumerate(self.events) if i >= 10]
        report = self.check()
        self.assertEqual("failed", report["status"])
        self.assertTrue(report["missing"])

    def test_gui_check_requires_stop_and_one_session_without_network(self):
        from tools.liverse_gui import LiVerseGui
        gui = SimpleNamespace(logs_listbox=SimpleNamespace(curselection=lambda: (0,)), process=SimpleNamespace(poll=lambda: None))
        with patch("tools.liverse_gui.messagebox.showinfo") as info, patch("tools.liverse_gui.check_live_session") as analyze:
            LiVerseGui._check_selected_log(gui)
        info.assert_called_once()
        analyze.assert_not_called()

    def test_diagnostic_gui_does_not_persist_test_settings(self):
        from tools.liverse_gui import LiVerseGui, GuiConfig
        gui = SimpleNamespace(diagnostic_test=True, _collect_config=lambda: GuiConfig(),
            _refresh_database_status=lambda: None, process=None, start_engine=lambda: None)
        with patch("tools.liverse_gui.save_gui_config") as save:
            LiVerseGui.save_and_start(gui)
        save.assert_not_called()

    def test_release_guide_is_compact_and_leaves_ups_for_general_test(self):
        from tools.benchmark_local import church_reading_text
        text = church_reading_text()
        first = next(line for line in text.splitlines() if line.startswith("Откроем Евангелие"))
        self.assertEqual("Иоанн 3:16", LiveReferencePipeline().process_text(first, now_ms=0)["parsed"]["ref"])
        self.assertNotIn("УПС должен переходить", text)
        self.assertNotIn("Лука, пятнадцатую главу", text)
        self.assertIn("тест последнего релиза", text)

    def test_general_church_guide_keeps_ups_and_list_coverage(self):
        from tools.benchmark_local import church_general_reading_text
        text = church_general_reading_text()
        long = next(line for line in text.splitlines() if line.startswith("Прочитаем Евангелие"))
        self.assertEqual("Лука 15:11-24", LiveReferencePipeline().process_text(long, now_ms=0)["parsed"]["ref"])
        self.assertIn("УПС должен переходить", text)
        lines = text.splitlines()
        start = next(i for i,line in enumerate(lines) if line.startswith("Запишем четыре"))
        result = LiveReferencePipeline().process_text(" ".join(lines[start:start+4]), now_ms=0)
        self.assertEqual(["Иоанн 3:16", "Римлянам 8:1", "Ефесянам 2:8", "Матфей 5:9"], [r["ref"] for r in result["reference_list"]])

    def test_gui_and_console_guides_use_the_same_spoken_text_with_correct_setup(self):
        from tools.benchmark_local import church_reading_text
        console = church_reading_text()
        gui = church_reading_text(diagnostic_test=False)
        self.assertEqual(re.sub(r"\[.*?\]", "", console, flags=re.S),
                         re.sub(r"\[.*?\]", "", gui, flags=re.S))
        self.assertIn("Настройки теста временные", console)
        self.assertNotIn("Настройки теста временные", gui)
        self.assertIn("настройки и запуск распознавания выполняет оператор", gui)
        self.assertNotIn("длинный отрывок", gui)

    def test_cpu_selection_keeps_two_distinct_physical_cores(self):
        from tools.benchmark_local import church_cpu_ids
        for cpu, core in ((0,0), (1,0), (2,1), (3,1), (4,2)):
            root = self.session / f"cpu{cpu}" / "topology"
            root.mkdir(parents=True)
            (root / "core_id").write_text(str(core))
            (root / "physical_package_id").write_text("0")
        self.assertEqual([0,1,2,3], church_cpu_ids(self.session, allowed=[0,1,2,3,4]))
        with self.assertRaises(ValueError):
            church_cpu_ids(self.session, allowed=[0,1])

    def test_worker_refuses_unlimited_or_incorrect_cpu_quota(self):
        from tools.benchmark_local import verify_church_limits
        for raw in ("max 20000", "10000 20000"):
            with self.subTest(quota=raw), patch("tools.benchmark_local.os.sched_getaffinity", return_value={0,1,2,3}, create=True), patch("tools.benchmark_local.Path.read_text", side_effect=["0::/test", raw]):
                with self.assertRaises(ValueError):
                    verify_church_limits([0,1,2,3], 100)

    def test_corrupted_host_information_is_reported_without_crashing_renderer(self):
        from tools.analyze_vosk_probe_logs import format_session_check
        self.metadata["host"] = "not a hardware object"
        report = self.check()
        self.assertEqual("insufficient", report["status"])
        self.assertIn("Повреждены сведения", format_session_check(report))


if __name__ == "__main__":
    unittest.main()
