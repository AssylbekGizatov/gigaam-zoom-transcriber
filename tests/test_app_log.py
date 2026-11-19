import json
from types import SimpleNamespace

from gigaam_zoom import app_log


def test_weighted_decisions_log_session_aliases_and_verbose_names(tmp_path):
    path = app_log.configure_logging(directory=tmp_path)
    candidates = [("Тестовое имя", 721.5), ("Second Name", 125)]
    app_log.log_attribution(500, 2500, candidates, "Тестовое имя", "weighted_overlap")
    app_log.log_attribution(1100, 3100, candidates, None, "speaker_conflict")
    contents = path.read_text(encoding="utf-8")
    assert all(name not in contents for name, _ in candidates)
    first, second = [json.loads(line) for line in contents.splitlines()][-2:]
    assert first["window_ms"] == [500, 2500] and first["reason"] == "weighted_overlap"
    assert first["candidates"] == second["candidates"]
    assert first["chosen"] == first["candidates"][0]["speaker"]
    assert first["candidates"][0]["weighted_ms"] == 721.5 and second["chosen"] is None
    app_log.set_verbose_logging(True)
    try:
        app_log.log_attribution(500, 2500, candidates, "Тестовое имя", "weighted_overlap")
        assert json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["chosen"] == "Тестовое имя"
    finally:
        app_log.set_verbose_logging(False)


def test_capture_attempt_logs_only_method_and_evidence(tmp_path):
    path = app_log.configure_logging(directory=tmp_path)
    app_log.log_event("ocr_observation", capture_method="printwindow",
                      capture_attempts=[{"method": "wgc", "evidence": "capture_blank", "window": "Private meeting"},
                                        {"method": "printwindow", "evidence": "ok"}])
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert row["capture_method"] == "printwindow"
    assert row["capture_attempts"] == [{"method": "wgc", "evidence": "capture_blank"},
                                       {"method": "printwindow", "evidence": "ok"}]


def test_logs_allowlist_frame_and_strip_sensitive_exception_messages(tmp_path):
    path = app_log.configure_logging(directory=tmp_path)
    word = "Alice Johnson"
    frame = SimpleNamespace(observation=SimpleNamespace(evidence="ocr_confirming", display_name=word),
                            raw_lines=[word], parser_reason="ocr_single_name", timings={"ocr_ms": 33.5},
                            max_age_ms=2075, window={"title": "secret meeting 1133", "dpi": 96},
                            language="en-US", line_count=1, word_count=2, image_size=(42, 12),
                            image_png=b"private image")
    app_log.log_ocr_frame(frame)
    app_log.log_event("device_selected", source="loopback", device_index=2,
                      device_name=word, path=r"C:\Users\private\export")
    try:
        raise ValueError(word + " full transcript/private-meeting-link")
    except ValueError as exc:
        app_log.log_exception("session_error", exc)
    content = path.read_text(encoding="utf-8")
    for private in (word, "secret meeting", "private", "transcript", "image_png"):
        assert private not in content
    events = [json.loads(line) for line in content.splitlines()]
    assert {event["session"] for event in events} == {app_log._session}
    assert events[1]["ocr_ms"] == 33.5
    assert events[1]["evidence"] == "ocr_confirming"
    assert events[-1]["error_type"] == "ValueError"
    assert events[-1]["stack"][0]["line"] > 0


def test_verbose_opt_in_only_enables_ocr_lines_and_masks_profile(tmp_path, monkeypatch):
    path = app_log.configure_logging(directory=tmp_path)
    monkeypatch.setenv("USERPROFILE", r"C:\Users\company.login")
    app_log.set_verbose_logging(True)
    app_log.log_event("ocr_observation", ocr_lines=[r"Лена C:\Users\company.login\Downloads",
                                                   "C:/users/company.login/Downloads"],
                      transcript="private speech", title="Private meeting")
    app_log.set_verbose_logging(False)
    app_log.log_event("ocr_observation", ocr_lines=["Should be invisible"])
    content = path.read_text(encoding="utf-8")
    assert "Лена" in content and "%USERPROFILE%" in content
    assert all(s not in content for s in ("company.login", "private speech", "Private meeting", "Should be invisible"))


def test_isolated_probe_output_and_worker_exit_codes_are_privacy_filtered(tmp_path):
    path = app_log.configure_logging(directory=tmp_path)
    app_log.log_windows_environment({
        "zoom_version": "7.1.5.43453", "ocr_languages": ["en-US", "ru", "Private Name"],
        "monitors": [{"bounds": [-1920, 0, 0, 1080], "dpi": 144, "name": "Private Name"}],
        "errors": ["zoom_version_error", "Private Name"],
        "zoom_window": {"found": True, "title": "Private Name", "evidence": "zoom_window_detected",
                        "client_rect": [-1920, 0, 0, 1000], "dpi": 144}, "text": "Private Name"})
    for exit_code in (3221225477, -11, 1):
        app_log.log_event("ocr_worker_crashed", exit_code=exit_code, retry_count=2, retry_delay_ms=700)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert "Private Name" not in str(records)
    assert records[1]["ocr_languages"] == ["en-US", "ru"]
    assert records[1]["zoom_window"]["evidence"] == "zoom_window_detected"
    assert [item["exit_code"] for item in records[2:]] == [3221225477, -11, 1]


def test_fault_stack_and_last_breadcrumb_survive_process_exit(tmp_path):
    import subprocess
    import sys
    import time
    # Generate a real faulthandler frame, then terminate without atexit or file
    # cleanup. Avoid invoking Windows Error Reporting dialogs in CI.
    code = '''
import faulthandler, os, sys
from pathlib import Path
from gigaam_zoom.app_log import enable_crash_logging, breadcrumb, _close_crash_logging
from gigaam_zoom import app_log
enable_crash_logging(directory=Path(sys.argv[1]))
breadcrumb("before_native_probe")
exec(compile("faulthandler.dump_traceback(app_log._crash_relay.stdin); os._exit(17)",
             r"C:\\Users\\company.login\\Workspace\\Private meeting.py", "exec"))
'''
    crashed = subprocess.run([sys.executable, "-c", code, str(tmp_path)], timeout=12, check=False)
    assert crashed.returncode == 17
    until = time.monotonic() + 5
    crashfile = tmp_path / "crash.log"
    while time.monotonic() < until:
        saved = crashfile.read_text(encoding="utf-8")
        if "external:" in saved:
            break
        time.sleep(.05)
    assert "before_native_probe" in saved and "external:" in saved
    assert "company.login" not in saved and "Private meeting" not in saved
    assert str(tmp_path) not in saved and "<string>" not in saved


def test_probe_failure_preserves_other_results_and_never_returns_titles(monkeypatch):
    from gigaam_zoom import zoom_visual, zoom_window
    monkeypatch.setattr(app_log.sys, "platform", "win32")
    monkeypatch.setattr(app_log, "_zoom_version", lambda: "7.1.5.43453")
    monkeypatch.setattr(zoom_window, "list_monitors", lambda: [SimpleNamespace(bounds=(0, 0, 1000, 800), dpi=96)])
    monkeypatch.setattr(zoom_window, "find_meeting_window", lambda: SimpleNamespace(
        title="Private meeting", client_bounds=(100, 10, 600, 400), dpi=96))
    def fail_languages():
        raise RuntimeError("Private language error")
    monkeypatch.setattr(zoom_visual, "installed_ocr_languages", fail_languages)
    details = app_log.collect_startup_environment()
    assert details["zoom_window"]["found"] and details["errors"] == ["ocr_languages_error"]
    assert details["zoom_version"] == "7.1.5.43453" and details["monitors"][0]["dpi"] == 96
    assert "Private" not in json.dumps(details)
