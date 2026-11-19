import json
import subprocess
import sys

import pytest

from gigaam_zoom import startup_probe


@pytest.mark.parametrize("script, expected", [
    ("import os; os._exit(37)", "ocr_worker_crashed"),
    ("print('garbled')", "ocr_unavailable"),
    ("print('{}')", "ocr_unavailable"),
    ("print('null')", "ocr_unavailable"),
])
def test_environment_child_failure_is_contained(monkeypatch, script, expected):
    run = subprocess.run
    def launch(command, **kwargs):
        assert command == [sys.executable, "-m", "gigaam_zoom.startup_probe"]
        return run([sys.executable, "-c", script], **kwargs)
    monkeypatch.setattr(startup_probe.subprocess, "run", launch)
    result = startup_probe.probe_environment()
    assert (result["evidence"], result["languages"], result["zoom_found"]) == (expected, [], False)
    if expected == "ocr_worker_crashed":
        assert result["exit_code"] == 37


def test_environment_child_timeout(monkeypatch):
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr(startup_probe.subprocess, "run", timeout)
    assert startup_probe.probe_environment()["evidence"] == "ocr_timeout"


def test_environment_result_keeps_installed_windows_tags(monkeypatch):
    details = {"ocr_languages": ["en-US", "ru"],
               "zoom_window": {"found": True, "evidence": "zoom_window_detected"}}
    monkeypatch.setattr(startup_probe.subprocess, "run", lambda command, **kw:
                        subprocess.CompletedProcess(command, 0, stdout=json.dumps(details).encode("ascii")))
    result = startup_probe.probe_environment()
    assert result == {"languages": ["en-US", "ru"], "zoom_found": True,
                      "zoom_evidence": "zoom_window_detected", "evidence": ""}
