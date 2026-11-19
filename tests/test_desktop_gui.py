"""User-flow contracts that do not require a display, audio device or download."""
import queue
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gigaam_zoom.desktop_gui import DesktopApplication, select_ocr_language, speaker_status


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class DesktopGuiTests(unittest.TestCase):
    def application(self):
        app = DesktopApplication.__new__(DesktopApplication)
        app.busy = app.closing = False
        app.controller = types.SimpleNamespace(running=False)
        app.events = queue.Queue()
        return app

    def test_missing_speaker_status_explains_action_without_internal_codes(self):
        message = speaker_status({"name": None, "evidence": "ocr_recalibration_required"})
        self.assertIn("выберите область", message)
        self.assertNotIn("ocr_", message)
        self.assertEqual(speaker_status({"evidence": "unexpected_internal_code"}), "Говорящий: Неизвестно")
        self.assertEqual(speaker_status({"name": "Алия", "evidence": "ocr_speaker_view_hint"}), "Говорящий: Алия")

    def test_correct_name_discloses_inference_and_uses_current_stored_name(self):
        app = self.application()
        app.root = object()
        app.table = Mock()
        app.table.selection.return_value = ("selected",)
        app.controller.store = Mock()
        app.controller.store.segment_by_id.return_value = types.SimpleNamespace(
            id="selected", display_name="Алия", attribution="inferred_gap_fill")
        app.background = lambda operation: operation()
        with patch("gigaam_zoom.desktop_gui.simpledialog.askstring", return_value="Ирина") as ask:
            app.correct_name()
        self.assertIn("восстановлено по соседним репликам", ask.call_args.args[1])
        self.assertNotIn("inferred_gap_fill", ask.call_args.args[1])
        self.assertEqual(ask.call_args.kwargs["initialvalue"], "Алия")
        app.controller.store.segment_by_id.assert_called_once_with("selected")
        app.controller.store.recent.assert_not_called()
        app.controller.store.correct_speaker.assert_called_once_with("selected", "Ирина")
        self.assertEqual(list(app.events.queue), [("refresh", {})])

    def test_correct_name_preserves_manual_name_and_cancel_does_not_write(self):
        app = self.application()
        app.root = object()
        app.table = Mock()
        app.table.selection.return_value = ("selected",)
        app.controller.store = Mock()
        app.controller.store.segment_by_id.return_value = types.SimpleNamespace(
            id="selected", display_name="Ранее исправленное имя", attribution="manual")
        with patch("gigaam_zoom.desktop_gui.simpledialog.askstring", return_value=None) as ask:
            app.correct_name()
        self.assertIn("уже исправлено вручную", ask.call_args.args[1])
        self.assertNotIn("по соседним", ask.call_args.args[1])
        self.assertEqual(ask.call_args.kwargs["initialvalue"], "Ранее исправленное имя")
        app.controller.store.correct_speaker.assert_not_called()

    def test_correct_name_accepts_archived_row_without_new_provenance(self):
        app = self.application()
        app.root = object()
        app.table = Mock()
        app.table.selection.return_value = ("selected",)
        app.controller.store = Mock()
        app.controller.store.segment_by_id.return_value = types.SimpleNamespace(id="selected", display_name="Архивное имя")
        with patch("gigaam_zoom.desktop_gui.simpledialog.askstring", return_value=None) as ask:
            app.correct_name()
        self.assertEqual(ask.call_args.args[1], "Имя для выбранной реплики:")
        self.assertEqual(ask.call_args.kwargs["initialvalue"], "Архивное имя")

    def test_native_crash_discards_old_name_in_human_status(self):
        for code in ("ocr_worker_crashed", "ocr_worker_exited", "ocr_worker_start_failed"):
            message = speaker_status({"name": "Предыдущее имя", "evidence": code})
            self.assertIn("OCR недоступен", message)
            self.assertNotIn("Предыдущее имя", message)
            self.assertNotIn(code, message)

    def test_first_map_defers_one_probe_until_idle_and_ignores_child_maps(self):
        app = self.application()
        app.root = Mock()
        app.first_run_complete = True
        app._startup_probe_scheduled = False
        app._map_binding = "map-callback"
        with patch("gigaam_zoom.desktop_gui.breadcrumb"):
            app._on_first_map(types.SimpleNamespace(widget=object()))
            app.root.after_idle.assert_not_called()
            app._on_first_map(types.SimpleNamespace(widget=app.root))
            app._on_first_map(types.SimpleNamespace(widget=app.root))
        app.root.after_idle.assert_called_once_with(app._first_refresh)
        app.root.unbind.assert_called_once_with("<Map>", "map-callback")

    def test_language_defaults_to_russian_but_keeps_an_explicit_installed_choice(self):
        self.assertEqual(select_ocr_language(["en-US", "ru"]), "ru")
        self.assertEqual(select_ocr_language(["en-US", "ru-RU"], "en-US"), "en-US")
        self.assertEqual(select_ocr_language(["en-US", "ru-RU"], "ru-ru"), "ru-RU")
        self.assertEqual(select_ocr_language(["fr-FR", "en-US"]), "en-US")

    def test_probe_crash_is_visible_without_clearing_saved_language(self):
        app = self.application()
        app.ocr_language = Value("en-US")
        app.language_combo = {}
        app.language_status = Value("")
        app.zoom_status = Value("")
        app.status = Value("")
        app.speaker = Value("Говорящий: Предыдущее имя")
        app.update_checklist = Mock()
        app._apply_environment_probe({"languages": [], "zoom_found": False,
                                      "evidence": "ocr_worker_crashed", "exit_code": -1073741819})
        self.assertIn("OCR недоступен", app.status.get())
        self.assertEqual(app.speaker.get(), "Говорящий: Неизвестно")
        self.assertEqual(app.ocr_language.get(), "en-US")
        self.assertNotIn("1073741819", app.status.get())

    def test_environment_refresh_uses_isolated_probe(self):
        app = self.application()
        app.background = lambda operation: operation()
        result = {"languages": ["ru"], "zoom_found": True}
        with patch("gigaam_zoom.startup_probe.probe_environment", return_value=result) as probe, \
                patch("gigaam_zoom.desktop_gui.list_devices", return_value=[]):
            app.refresh_environment()
        probe.assert_called_once_with()
        self.assertEqual(list(app.events.queue), [("devices", []), ("environment_probe", result)])

    def test_show_zoom_focus_failure_gives_manual_switch_instruction(self):
        from gigaam_zoom.zoom_window import WindowProbeError
        app = self.application()
        app.background = lambda operation: operation()
        with patch("gigaam_zoom.zoom_window.bring_meeting_to_front", side_effect=WindowProbeError("ocr_focus_failed")), \
                patch("gigaam_zoom.desktop_gui.log_event") as log:
            app.show_zoom()
        events = list(app.events.queue)
        self.assertIn("переключитесь", events[-1][1]["message"])
        self.assertNotIn("ocr_focus_failed", events[-1][1]["message"])
        log.assert_called_once_with("zoom_window_check", evidence="ocr_focus_failed")

    def test_background_error_reaches_tk_queue_without_exposing_exception_text(self):
        app = self.application()
        def operation():
            raise RuntimeError("private participant or system detail")
        with patch("gigaam_zoom.desktop_gui.log_exception") as record:
            app.background(operation)
            app.job.join(2)
        self.assertFalse(app.job.is_alive())
        events = list(app.events.queue)
        self.assertEqual([kind for kind, _ in events], ["operation_error", "operation_done"])
        self.assertNotIn("private", events[0][1]["message"])
        record.assert_called_once()

    def test_download_button_calls_existing_verified_release_installer(self):
        app = self.application()
        app.model = Value("models/gigaam")
        app.status = Value("")
        app.background = lambda operation: operation()
        with patch("scripts.acquire_model.acquire") as acquire, patch("gigaam_zoom.desktop_gui.log_event"):
            app.download_model()
        acquire.assert_called_once_with(Path("models/gigaam"))
        self.assertEqual(app.events.get()[0], "model_ready")

    def test_calibration_uses_selected_installed_language_and_cancellation_unblocks_gui(self):
        app = self.application()
        app.root = object()
        app.onboarding = None
        app.ocr_language = Value("en-US")
        with patch("gigaam_zoom.zoom_calibration.begin_calibration") as calibrate:
            app.calibrate_name()
            self.assertTrue(app.busy)
            self.assertEqual(calibrate.call_args.kwargs["language"], "en-US")
            calibrate.call_args.args[1](None, None)
        self.assertFalse(app.busy)

    def test_settings_preserve_export_geometry_and_device_names_without_indices(self):
        app = self.application()
        app.root = types.SimpleNamespace(geometry=lambda: "1100x660+120+40")
        for key, value in {"model": "models/gigaam", "folder": "transcripts", "name": "Алия",
                           "microphone": True, "ocr_region": ".1,.8,.3,.1", "ocr_language": "ru-RU",
                           "ocr_client_size": "1280,720", "verbose": False, "format": "PDF",
                           "loopback": "7: Headphones", "mic_device": "2: Microphone"}.items():
            setattr(app, key, Value(value))
        app.first_run_complete = True
        app.saved_loopback = app.saved_microphone = ""
        app.devices = [types.SimpleNamespace(index=7, name="Headphones", kind="loopback"),
                       types.SimpleNamespace(index=2, name="Microphone", kind="microphone")]
        settings = app.settings()
        self.assertEqual(settings.loopback_name, "Headphones")
        self.assertEqual(settings.microphone_name, "Microphone")
        self.assertEqual(settings.export_format, "PDF")
        self.assertEqual(settings.window_geometry, "1100x660+120+40")
        self.assertTrue(settings.first_run_complete)


if __name__ == "__main__":
    unittest.main()
