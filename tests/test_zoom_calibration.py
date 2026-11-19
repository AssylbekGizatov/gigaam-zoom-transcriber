import unittest

from gigaam_zoom.zoom_calibration import normalized_selection


class CalibrationTests(unittest.TestCase):
    def test_scaled_preview_is_client_relative_in_both_drag_directions(self):
        expected = (.1, .8, .4, .1)
        forward = normalized_selection((80, 480), (400, 540), (800, 600))
        backward = normalized_selection((400, 540), (80, 480), (800, 600))
        self.assertEqual(forward, expected)
        self.assertEqual(backward, expected)
        self.assertEqual(normalized_selection((160, 960), (800, 1080), (1600, 1200)), expected)

    def test_drag_beyond_preview_clamps_to_client(self):
        self.assertEqual(normalized_selection((-20, 80), (120, 140), (100, 100)), (0, .8, 1, .2))

    def test_accidental_click_empty_preview_and_nonfinite_coordinates_rejected(self):
        for start, end, size in [((20, 20), (21, 21), (100, 100)),
                                 ((0, 0), (50, 50), (0, 100)),
                                 ((float("nan"), 0), (50, 50), (100, 100)),
                                 ((0, 0), (float("inf"), 50), (100, 100))]:
            with self.subTest(start=start, end=end, size=size), self.assertRaises(ValueError):
                normalized_selection(start, end, size)


# Physical coordinates, revalidation, and lifecycle use deterministic window
# fixtures; the transparent native overlay still requires a Windows UI check.
from dataclasses import dataclass, replace
from types import SimpleNamespace
import sys

import pytest
from gigaam_zoom import zoom_calibration as calibration


@dataclass(frozen=True)
class Meeting:
    hwnd: int = 17
    pid: int = 22
    client_bounds: tuple = (-1920, 0, -320, 900)
    monitor_id: str = "secondary"
    dpi: int = 144


def test_physical_drag_on_negative_origin_monitor_and_dpi_scaled_window():
    window = Meeting()
    assert calibration.screen_selection((-1760, 720), (-1120, 810), window.client_bounds) == (.1, .8, .4, .1)
    assert calibration.screen_selection((-1120, 810), (-1760, 720), window.client_bounds) == (.1, .8, .4, .1)
    assert calibration.screen_selection((240, 1080), (1200, 1215), (0, 0, 2400, 1350)) == (.1, .8, .4, .1)


def test_live_preview_stays_on_zoom_monitor_and_never_covers_selected_name():
    roi = (-1920, 760, -1200, 840)
    work = (-1920, 0, 0, 1040)
    chosen = calibration.preview_position(work, roi)
    assert chosen == (-560, 0, 0, 340)
    assert not calibration.intersects(chosen, roi)
    # A top-right name forces the preview to another corner.
    chosen = calibration.preview_position(work, (-600, 0, 0, 100))
    assert chosen == (-560, 700, 0, 1040)
    with pytest.raises(ValueError):
        calibration.preview_position(work, work)


@pytest.mark.parametrize("changes", [
    {"hwnd": 18}, {"pid": 23}, {"client_bounds": (-1919, 0, -319, 900)},
    {"client_bounds": (-1920, 0, -320, 901)}, {"monitor_id": "primary"}, {"dpi": 192},
])
def test_window_recreated_moved_resized_or_scaled_during_selection_requires_retry(changes):
    original = Meeting()
    assert calibration._same_window(original, original)
    assert not calibration._same_window(original, replace(original, **changes))


class Parent:
    def __init__(self):
        self.current_state = "zoomed"
        self.callbacks = []
        self.restored = []

    def state(self, value=None):
        if value is None:
            return self.current_state
        self.current_state = value
        self.restored.append(value)

    def after(self, delay, callback):
        self.callbacks.append(callback)
        return len(self.callbacks)

    def after_cancel(self, token):
        pass

    def winfo_exists(self):
        return True

    def lift(self):
        pass

    def flush(self):
        while self.callbacks:
            self.callbacks.pop(0)()


def setup_flow(monkeypatch, selections, actions):
    meeting, parent, events, results = Meeting(), Parent(), [], []
    windows = SimpleNamespace(find_meeting_window=lambda: meeting, bring_meeting_to_front=lambda: meeting)
    logs = SimpleNamespace(log_event=lambda *args, **kwargs: events.append(args[0]),
                           log_exception=lambda *args, **kwargs: events.append(args[0]))
    monkeypatch.setitem(sys.modules, "gigaam_zoom.zoom_window", windows)
    monkeypatch.setitem(sys.modules, "gigaam_zoom.app_log", logs)
    monkeypatch.setattr(calibration, "_desktop_layout", lambda *args: (False, (-1920, 0, 0, 1040)))
    select = iter(selections)
    preview = iter(actions)
    monkeypatch.setattr(calibration, "select_region", lambda *args: next(select))
    monkeypatch.setattr(calibration, "_preview", lambda *args, **kwargs: next(preview))
    calibration.begin_calibration(parent, lambda *args: results.append(args), language="en-US")
    return parent, events, results


def test_calibration_cancel_restores_parent_and_calls_callback_once(monkeypatch):
    parent, events, results = setup_flow(monkeypatch, [None], [])
    parent.flush()
    assert results == [(None, None)]
    assert parent.restored == ["zoomed"]
    assert events == ["calibration_started", "calibration_cancelled"]


def test_retry_does_not_save_unverified_region_and_confirm_persists_only_coordinates(monkeypatch):
    parent, events, results = setup_flow(monkeypatch, [(.1, .8, .3, .1), (.2, .7, .4, .1)], ["retry", "confirm"])
    parent.flush()
    assert results == [("0.20000000,0.70000000,0.40000000,0.10000000", "1600,900")]
    assert parent.restored == ["zoomed"]
    assert "calibration_retry" in events
    assert events[-1] == "calibration_complete"


def test_failed_preview_restores_parent_without_replacing_existing_config(monkeypatch):
    parent, events, results = setup_flow(monkeypatch, [(.1, .8, .3, .1)], [])
    def failed(*args, **kwargs):
        raise RuntimeError("ocr_window_changed")
    monkeypatch.setattr(calibration, "_preview", failed)
    monkeypatch.setattr(calibration.messagebox, "askretrycancel", lambda *args, **kwargs: False)
    parent.flush()
    assert results == [(None, None)]
    assert parent.restored == ["zoomed"]
    assert events[-2:] == ["calibration_error", "calibration_cancelled"]


class ScheduledParent(Parent):
    def __init__(self):
        super().__init__()
        self.delays = []
        self.cancelled = []
        self.token = 0
        self.hidden = False

    def withdraw(self):
        self.hidden = True
        self.current_state = "withdrawn"

    def after(self, delay, callback):
        self.token += 1
        self.delays.append(delay)
        self.callbacks.append((self.token, callback))
        return self.token

    def after_cancel(self, token):
        self.cancelled.append(token)
        self.callbacks = [(key, callback) for key, callback in self.callbacks if key != token]

    def flush(self):
        while self.callbacks:
            _key, callback = self.callbacks.pop(0)
            callback()


class Countdown:
    def __init__(self, *args, **kwargs):
        self.bindings = {}
        self.destroyed = False

    def overrideredirect(self, value):
        pass

    def attributes(self, *args):
        pass

    def bind(self, name, callback):
        self.bindings[name] = callback

    def focus_force(self):
        pass

    def destroy(self):
        self.destroyed = True


def single_monitor_flow(monkeypatch):
    parent, countdown, results, events = ScheduledParent(), Countdown(), [], []
    meeting = Meeting()
    monkeypatch.setitem(sys.modules, "gigaam_zoom.zoom_window", SimpleNamespace(
        find_meeting_window=lambda: meeting, bring_meeting_to_front=lambda: meeting))
    monkeypatch.setitem(sys.modules, "gigaam_zoom.app_log", SimpleNamespace(
        log_event=lambda *args, **kwargs: events.append(args[0]), log_exception=lambda *args: None))
    monkeypatch.setattr(calibration, "_desktop_layout", lambda *args: (True, (-1920, 0, 0, 1040)))
    monkeypatch.setattr(calibration, "_position_window", lambda *args: None)
    monkeypatch.setattr(calibration.tk, "Toplevel", lambda *args: countdown)
    monkeypatch.setattr(calibration.tk, "StringVar", lambda **kwargs: SimpleNamespace(set=lambda value: None))
    monkeypatch.setattr(calibration.ttk, "Label", lambda *args, **kwargs: SimpleNamespace(pack=lambda: None))
    monkeypatch.setattr(calibration, "select_region", lambda *args: None)
    calibration.begin_calibration(parent, lambda *args: results.append(args))
    return parent, countdown, results, events


def test_single_monitor_hides_parent_and_counts_down_three_seconds(monkeypatch):
    parent, countdown, results, events = single_monitor_flow(monkeypatch)
    assert parent.hidden
    assert results == []
    parent.flush()
    assert parent.delays == [1000, 1000, 1000]
    assert countdown.destroyed
    assert parent.current_state == "zoomed"
    assert results == [(None, None)]


def test_escape_during_countdown_cancels_pending_overlay_and_restores_app(monkeypatch):
    parent, countdown, results, events = single_monitor_flow(monkeypatch)
    countdown.bindings["<Escape>"](None)
    parent.flush()
    assert len(parent.cancelled) == 1
    assert countdown.destroyed
    assert parent.current_state == "zoomed"
    assert results == [(None, None)]
    assert events[-1] == "calibration_cancelled"


def test_picker_rejects_full_window_before_starting_expensive_ocr_preview():
    with pytest.raises(ValueError, match="четверти"):
        calibration.normalized_selection((0, 0), (1000, 800), (1000, 800))
    assert calibration.normalized_selection((0, 0), (1000, 200), (1000, 800)) == (0, 0, 1, .25)


def test_calibration_errors_show_readable_text_without_raw_native_codes(monkeypatch):
    monkeypatch.setitem(sys.modules, "gigaam_zoom.desktop_gui", SimpleNamespace(
        speaker_status=lambda payload: "Окно встречи Zoom не найдено"))
    error = RuntimeError("secret internal details")
    error.code = "zoom_window_not_found"
    assert calibration._error_message(error) == "Окно встречи Zoom не найдено"
    assert "secret" not in calibration._error_message(RuntimeError("secret internal details"))
    assert calibration._error_message(ValueError("Выделите только имя")) == "Выделите только имя"
