"""User-flow contracts that do not require a display, audio device or download."""
import queue
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from gigaam_zoom.desktop_gui import DesktopApplication, speaker_status


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


if __name__ == "__main__":
    unittest.main()
