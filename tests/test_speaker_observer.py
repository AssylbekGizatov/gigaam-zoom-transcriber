"""Real spawned processes exercise EOF/exit-code races on Linux and Windows."""
from functools import partial
import os
from pathlib import Path
import time

import pytest

from gigaam_zoom import speaker_observer as module
from gigaam_zoom.speaker_observer import SpeakerObservation, ZoomSpeakerObserver, monotonic_ms
from gigaam_zoom.zoom_visual import VisualFrame, VisualSpeakerConfig


def _frame():
    return VisualFrame(SpeakerObservation(monotonic_ms(), "local:zoom-ocr:test", "Alice", "ocr_speaker_view_hint"))


def _native_exit(connection, session_id, config, verbose):
    connection.recv()
    os._exit(37)


def _name_then_native_exit(connection, session_id, config, verbose):
    connection.recv()
    connection.send(_frame())
    time.sleep(.2)  # Give the parent a deterministic observable named frame.
    connection.recv()
    os._exit(47)


def _eof_before_native_exit(connection, session_id, config, verbose):
    connection.recv()
    connection.close()
    # Windows can report pipe closure before publishing the process status.
    # Reproduce that ordering with a delay longer than the old cleanup grace.
    time.sleep(.3)
    os._exit(49)


def _clean_exit(connection, session_id, config, verbose):
    connection.recv()
    connection.close()


def _hung_worker(connection, session_id, config, verbose):
    connection.recv()
    time.sleep(60)


def _crash_once(marker, connection, session_id, config, verbose):
    path = Path(marker)
    if not path.exists():
        connection.recv()
        path.write_text("started", encoding="ascii")
        os._exit(53)
    while connection.recv() == "sample":
        connection.send(_frame())
    connection.close()


def _windows_exception_status(connection, session_id, config, verbose):
    connection.recv()
    import ctypes
    from ctypes import wintypes
    exit_process = ctypes.WinDLL("kernel32").ExitProcess
    exit_process.argtypes = [wintypes.UINT]
    exit_process.restype = None
    exit_process(0xC0000005)


def _posix_signal_exit(connection, session_id, config, verbose):
    import signal
    connection.recv()
    os.kill(os.getpid(), signal.SIGTERM)


def _wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    pytest.fail("OCR observer did not reach the expected state")


def _observer(worker, **kwargs):
    return ZoomSpeakerObserver(visual_config=VisualSpeakerConfig((0, .8, .4, .1)),
                               _worker=worker, poll_interval=.15, probe_timeout=4,
                               **kwargs)


def test_actual_nonzero_exit_is_logged_and_exposed(monkeypatch):
    events = []
    errors = []
    monkeypatch.setattr(module, "log_event", lambda event, **fields: events.append((event, fields)))
    monkeypatch.setattr(module, "log_exception", lambda event, error: errors.append((event, repr(error))))
    observer = _observer(_native_exit)
    observer.start()
    try:
        _wait_until(lambda: observer.latest().evidence == "ocr_worker_crashed")
        assert observer.latest().display_name is None
        assert observer.latest_frame() is None
        _wait_until(lambda: bool(events))
        assert not errors
        assert events[0] == ("ocr_worker_crashed", {
            "exit_code": 37, "retry_count": 1, "retry_delay_ms": 1000})
    finally:
        observer.stop()
    assert not observer._thread.is_alive()
    assert observer.latest().evidence == "ocr_stopped"


def test_preview_disabling_sample_logs_still_logs_native_crash(monkeypatch):
    events = []
    monkeypatch.setattr(module, "log_event", lambda event, **fields: events.append((event, fields)))
    observer = _observer(_native_exit, log_samples=False)
    observer.start()
    try:
        _wait_until(lambda: bool(events))
        assert events[0][0] == "ocr_worker_crashed"
        assert events[0][1]["exit_code"] == 37
    finally:
        observer.stop()


def test_native_crash_invalidates_previously_displayed_name(monkeypatch):
    monkeypatch.setattr(module, "log_event", lambda *args, **kwargs: None)
    observer = _observer(_name_then_native_exit)
    observer.start()
    try:
        _wait_until(lambda: observer.latest().display_name == "Alice")
        _wait_until(lambda: observer.latest().evidence == "ocr_worker_crashed")
        assert observer.latest_frame() is None
        assert observer.latest().display_name is None
    finally:
        observer.stop()


def test_pipe_eof_waits_for_real_native_exit_status(monkeypatch):
    events = []
    monkeypatch.setattr(module, "log_event", lambda event, **fields: events.append((event, fields)))
    observer = _observer(_eof_before_native_exit)
    observer.start()
    try:
        _wait_until(lambda: bool(events))
        assert events[0][0] == "ocr_worker_crashed"
        assert events[0][1]["exit_code"] == 49
        assert observer.latest().evidence == "ocr_worker_crashed"
    finally:
        observer.stop()


def test_clean_unexpected_exit_is_not_native_crash(monkeypatch):
    events = []
    monkeypatch.setattr(module, "log_event", lambda event, **fields: events.append((event, fields)))
    observer = _observer(_clean_exit)
    observer.start()
    try:
        _wait_until(lambda: observer.latest().evidence == "ocr_worker_exited")
        _wait_until(lambda: bool(events))
        assert events[0][0] == "ocr_worker_exited"
        assert events[0][1]["exit_code"] == 0
    finally:
        observer.stop()


def test_watchdog_termination_remains_timeout_not_crash(monkeypatch):
    events = []
    monkeypatch.setattr(module, "log_event", lambda event, **fields: events.append(event))
    monkeypatch.setattr(module, "log_exception", lambda event, error: events.append(event))
    observer = _observer(_hung_worker)
    observer.probe_timeout = .3
    observer.start()
    try:
        _wait_until(lambda: observer.latest().evidence == "ocr_timeout")
        _wait_until(lambda: "ocr_timeout" in events)
        assert "ocr_worker_crashed" not in events
    finally:
        observer.stop()
    assert not observer._thread.is_alive()
    assert "ocr_worker_crashed" not in events


def test_user_stop_during_native_work_is_bounded_and_not_reported_as_crash(monkeypatch):
    events = []
    monkeypatch.setattr(module, "log_event", lambda event, **fields: events.append(event))
    monkeypatch.setattr(module, "log_exception", lambda event, error: events.append(event))
    observer = _observer(_hung_worker)
    observer.start()
    time.sleep(.2)
    started = time.monotonic()
    observer.stop()
    assert time.monotonic() - started < 2
    assert not observer._thread.is_alive()
    assert observer.latest().evidence == "ocr_stopped"
    assert events == []


def test_repeated_native_crashes_back_off_and_stop_interrupts_retry(monkeypatch):
    events = []
    monkeypatch.setattr(module, "log_event", lambda event, **fields: events.append((event, fields)))
    observer = _observer(_native_exit, retry_initial_delay=.2, retry_max_delay=.8)
    observer.start()
    try:
        _wait_until(lambda: len(events) >= 3)
        assert [fields["retry_delay_ms"] for _, fields in events[:3]] == [200, 400, 800]
        assert [fields["retry_count"] for _, fields in events[:3]] == [1, 2, 3]
        assert observer.latest().evidence == "ocr_worker_crashed"
    finally:
        started = time.monotonic()
        observer.stop()
    assert time.monotonic() - started < 2
    assert not observer._thread.is_alive()


def test_worker_can_recover_after_native_crash_without_reusing_dead_frame(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(module, "log_event", lambda event, **fields: events.append((event, fields)))
    observer = _observer(partial(_crash_once, str(tmp_path / "attempt")), retry_initial_delay=.2)
    observer.start()
    try:
        _wait_until(lambda: bool(events))
        assert events[0][1]["exit_code"] == 53
        assert observer.latest().display_name is None
        _wait_until(lambda: observer.latest().display_name == "Alice")
        assert observer.latest_frame().observation.display_name == "Alice"
    finally:
        observer.stop()


def test_retry_delay_cap_and_input_validation():
    observer = _observer(_native_exit)
    assert [observer._retry_delay(count) for count in (1, 2, 3, 6, 100)] == [1, 2, 4, 30, 30]
    for values in ({"retry_initial_delay": 0}, {"retry_max_delay": float("nan")},
                   {"retry_initial_delay": 4, "retry_max_delay": 2}):
        with pytest.raises(ValueError):
            _observer(_native_exit, **values)


def test_spawn_failure_is_distinct_and_never_invents_exit_code(monkeypatch):
    from types import SimpleNamespace
    events = []
    closed = []

    class Process:
        pid = None

        def start(self):
            raise OSError("test executable denied")

        def is_alive(self):
            return False

        def close(self):
            closed.append("process")

    def pipe():
        return (SimpleNamespace(close=lambda: closed.append("parent")),
                SimpleNamespace(close=lambda: closed.append("child")))

    context = SimpleNamespace(Pipe=pipe, Process=lambda **kwargs: Process())
    monkeypatch.setattr(module.mp, "get_context", lambda method: context)
    monkeypatch.setattr(module, "log_event", lambda event, **fields: events.append(event))
    monkeypatch.setattr(module, "log_exception", lambda event, error: events.append(event))
    observer = _observer(_native_exit)
    observer.start()
    try:
        _wait_until(lambda: observer.latest().evidence == "ocr_worker_start_failed")
        _wait_until(lambda: bool(events))
        assert events == ["ocr_worker_start_failed"]
    finally:
        observer.stop()
    assert sorted(closed) == ["child", "parent", "process"]


@pytest.mark.skipif(os.name != "nt", reason="Windows native process exit status")
def test_windows_exception_exit_code_is_not_truncated_to_posix_byte(monkeypatch):
    events = []
    monkeypatch.setattr(module, "log_event", lambda event, **fields: events.append((event, fields)))
    observer = _observer(_windows_exception_status)
    observer.start()
    try:
        _wait_until(lambda: bool(events))
        assert events[0][0] == "ocr_worker_crashed"
        assert events[0][1]["exit_code"] == 0xC0000005
    finally:
        observer.stop()


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal exit status")
def test_unexpected_posix_signal_is_preserved_as_negative_exit_code(monkeypatch):
    import signal
    events = []
    monkeypatch.setattr(module, "log_event", lambda event, **fields: events.append((event, fields)))
    observer = _observer(_posix_signal_exit)
    observer.start()
    try:
        _wait_until(lambda: bool(events))
        assert events[0][0] == "ocr_worker_crashed"
        assert events[0][1]["exit_code"] == -signal.SIGTERM
    finally:
        observer.stop()
