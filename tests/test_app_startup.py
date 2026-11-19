"""Real app.py/Tcl lifecycle with explicit external-device and Zoom fixtures.

This verifies parent import order and persisted calibration loading; it is not
an assertion that a real Zoom meeting or Windows OCR engine was exercised.
Native subprocess probing and Windows restart-soak tests cover those boundaries.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
HARNESS = Path(__file__).with_name("startup_harness")


@pytest.mark.skipif(sys.platform != "win32" and not os.environ.get("DISPLAY"),
                    reason="Real Tcl/Tk startup requires Windows desktop or a Linux DISPLAY (e.g. Xvfb)")
@pytest.mark.parametrize("settings_present,zoom_open", [(True, True), (False, False)],
                         ids=["saved-calibration-zoom-open", "fresh-settings-zoom-closed"])
def test_real_entrypoint_parent_has_no_winrt_and_renders_utf8_dialog(tmp_path, settings_present, zoom_open):
    local = tmp_path / "local-app-data"
    data = local / "GigaAMZoom"
    data.mkdir(parents=True)
    model = tmp_path / "model"
    model.mkdir()
    settings = data / "desktop-settings.json"
    if settings_present:
        settings.write_text(json.dumps({
            "model_dir": str(model), "export_dir": str(tmp_path), "first_run_complete": True,
            "zoom_ocr_region": "0.12000000,0.81000000,0.32000000,0.05000000",
            "zoom_ocr_client_size": "1280,720", "zoom_ocr_language": "ru-RU",
            "window_geometry": "1000x720", "local_name": "Тестовый участник",
        }, ensure_ascii=False), encoding="utf-8")
    result = tmp_path / "result.json"
    spec = tmp_path / "fixture.json"
    spec.write_text(json.dumps({"settings_present": settings_present, "zoom_open": zoom_open,
                               "entrypoint": str(ROOT / "app.py"), "result": str(result)}), encoding="utf-8")
    env = os.environ.copy()
    env.update({"LOCALAPPDATA": str(local), "GIGAAM_TEST_STARTUP_SPEC": str(spec),
                "PYTHONPATH": os.pathsep.join((str(HARNESS), str(ROOT), env.get("PYTHONPATH", "")))})
    # Exact production entrypoint; sitecustomize only belongs to this test's path.
    process = subprocess.run([sys.executable, str(ROOT / "app.py")], cwd=ROOT, env=env,
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    diagnostic = process.stdout + process.stderr
    assert process.returncode == 0, diagnostic
    assert result.exists(), diagnostic
    state = json.loads(result.read_text(encoding="utf-8"))
    assert "harness_error" not in state, state
    assert state["constructed"] and state["ready"] and state["closed"], state
    assert state["utf8_dialog"], state
    assert state["loaded_calibration"] is settings_present
    assert state["probe_calls"] >= 1 and all(state["root_before_probe"]), state
    assert state["winrt_attempts"] == [], state
    assert state["unexpected_dialogs"] == [], state
    # The real GUI close path persists settings, including non-ASCII strings.
    saved = json.loads(settings.read_text(encoding="utf-8"))
    assert bool(saved["zoom_ocr_region"]) is settings_present
    assert not any(data.rglob("*.png")), "The startup harness must not save screen images"
