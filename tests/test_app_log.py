import json
from types import SimpleNamespace

from gigaam_zoom import app_log


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
    app_log.log_event("ocr_observation", ocr_lines=[r"Лена C:\Users\company.login\Downloads"],
                      transcript="private speech", title="Private meeting")
    app_log.set_verbose_logging(False)
    app_log.log_event("ocr_observation", ocr_lines=["Should be invisible"])
    content = path.read_text(encoding="utf-8")
    assert "Лена" in content and "%USERPROFILE%" in content
    assert all(s not in content for s in ("company.login", "private speech", "Private meeting", "Should be invisible"))
