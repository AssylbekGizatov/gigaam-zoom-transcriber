"""Ten real app.py launches through the Windows GUI/native startup path.

This test opens and closes the application; it never starts audio capture,
loads model weights, uses a real meeting, or makes network requests. The Zoom
fixture is a local WinForms executable named Zoom.exe, not a Zoom client. It
exercises process/window discovery and version metadata only.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import psutil
import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Real Windows GUI/native relaunch smoke test")
ROOT = Path(__file__).resolve().parents[1]
APP_TITLE = "GigaAM · Живой текст Zoom"


class NativeWindows:
    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user.EnumWindows.argtypes = [self.callback_type, wintypes.LPARAM]
        self.user.EnumWindows.restype = wintypes.BOOL
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user.GetWindowTextW.restype = ctypes.c_int
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.IsWindowVisible.restype = wintypes.BOOL
        self.user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user.PostMessageW.restype = wintypes.BOOL

    def window(self, pids, title):
        matches = []
        @self.callback_type
        def visit(hwnd, _):
            pid = wintypes.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and self.user.IsWindowVisible(hwnd):
                text = ctypes.create_unicode_buffer(512)
                self.user.GetWindowTextW(hwnd, text, len(text))
                if text.value == title:
                    matches.append(int(hwnd))
            return True
        assert self.user.EnumWindows(visit, 0), "EnumWindows failed in external smoke harness"
        assert len(matches) <= 1, "More than one application root window"
        return matches[0] if matches else None

    def close(self, hwnd):
        assert self.user.PostMessageW(hwnd, 0x0010, 0, 0), "Could not post WM_CLOSE"


def _remember_descendants(process, remembered):
    try:
        parent = psutil.Process(process.pid)
        remembered[parent.pid] = parent
        for child in parent.children(recursive=True):
            remembered[child.pid] = child
    except psutil.Error:
        pass


def _alive(remembered):
    result = []
    for process in remembered.values():
        try:
            if process.is_running():
                result.append(process)
        except psutil.Error:
            pass
    return result


def _cleanup(process, remembered=None):
    remembered = {} if remembered is None else remembered
    _remember_descendants(process, remembered)
    live = _alive(remembered)
    for child in reversed(live):
        try:
            child.terminate()
        except psutil.Error:
            pass
    _, remaining = psutil.wait_procs(live, timeout=3)
    for child in remaining:
        try:
            child.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(remaining, timeout=3)
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def _events(path):
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # The current final line may still be in the writer's buffer.
    return records


def _wait(predicate, *, process, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        if process.poll() is not None:
            pytest.fail(f"Application exited before readiness: code={process.returncode}, "
                        f"hex=0x{process.returncode & 0xffffffff:08x}")
        time.sleep(.1)
    pytest.fail("Timed out waiting for the real Windows application/native startup probe")


@pytest.fixture(scope="module")
def native_fixture(tmp_path_factory):
    # Never close, modify, or confuse a user's actual Zoom session with this fixture.
    if any((item.info.get("name") or "").casefold() == "zoom.exe"
           for item in psutil.process_iter(["name"])):
        pytest.skip("Close the real Zoom client before the isolated Windows startup smoke test")
    assert importlib.util.find_spec("pyaudiowpatch") is not None, "Install Windows desktop dependencies first"
    assert importlib.util.find_spec("winrt") is not None, "Install Windows OCR dependencies first"
    folder = tmp_path_factory.mktemp("synthetic_zoom_startup")
    source = folder / "Zoom.cs"
    source.write_text('''using System;
using System.Drawing;
using System.Windows.Forms;
using System.Reflection;
[assembly: AssemblyVersion("7.1.5.43453")]
[assembly: AssemblyFileVersion("7.1.5.43453")]
static class StartupFixture {
    [STAThread] static void Main(string[] args) {
        Application.EnableVisualStyles();
        var window = new Form();
        window.Text = "Zoom Meeting";
        window.ClientSize = new Size(1000, 600);
        window.StartPosition = FormStartPosition.Manual;
        window.Location = new Point(20, 20);
        if (Array.IndexOf(args, "--covered") >= 0) {
            var label = new Label();
            label.Dock = DockStyle.Fill;
            label.BackColor = Color.White;
            label.ForeColor = Color.Black;
            label.Font = new Font("Segoe UI", 36);
            label.TextAlign = ContentAlignment.MiddleCenter;
            label.Text = "Anna Miller";
            window.Controls.Add(label);
            window.Shown += delegate {
                var cover = new Form();
                cover.Text = "Opaque fixture cover";
                cover.FormBorderStyle = FormBorderStyle.None;
                cover.StartPosition = FormStartPosition.Manual;
                cover.Bounds = window.Bounds;
                cover.BackColor = Color.Blue;
                cover.TopMost = true;
                cover.Show();
            };
        }
        Application.Run(window);
    }
}
''', encoding="utf-8")
    framework = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET"
    compiler = next((candidate for candidate in (
        framework / "Framework64" / "v4.0.30319" / "csc.exe",
        framework / "Framework" / "v4.0.30319" / "csc.exe",
    ) if candidate.is_file()), None)
    assert compiler is not None, "The Windows smoke fixture requires the stock .NET Framework C# compiler"
    executable = folder / "Zoom.exe"
    result = subprocess.run([str(compiler), "/nologo", "/target:winexe", "/platform:x64",
                             "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll",
                             "/out:" + str(executable), str(source)],
                            cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    assert result.returncode == 0, "Could not build the local synthetic Zoom window fixture"
    return NativeWindows(), executable


def test_covered_window_name_reaches_observer_from_native_helper(native_fixture):
    """Exercise HWND selection, nested helper, client crop and real Windows OCR together."""
    from gigaam_zoom.speaker_observer import ZoomSpeakerObserver
    from gigaam_zoom.startup_probe import probe_environment
    from gigaam_zoom.zoom_visual import VisualSpeakerConfig
    environment = probe_environment()
    assert not environment["evidence"], environment["evidence"]
    if "en-us" not in {tag.casefold() for tag in environment["languages"]}:
        pytest.skip("Covered-window OCR fixture requires the installed en-US recognizer")
    native, executable = native_fixture
    fixture = subprocess.Popen([str(executable), "--covered"])
    observer = ZoomSpeakerObserver(visual_config=VisualSpeakerConfig((.15, .36, .7, .28), language="en-US"),
                                   log_samples=False)
    try:
        _wait(lambda: native.window({fixture.pid}, "Opaque fixture cover"), process=fixture, timeout=10)
        observer.start()
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline:
            frame = observer.latest_frame()
            if frame is not None and frame.observation.display_name == "Anna Miller":
                assert frame.capture_method in {"wgc", "printwindow"}
                return
            time.sleep(.1)
        details = frame.capture_attempts if frame else ()
        pytest.fail(f"No confirmed name under opaque cover: {observer.latest().evidence}, {details}")
    finally:
        observer.stop()
        _cleanup(fixture)


def _launcher_command(run):
    # Exercise the shipped launcher when it chooses this test's interpreter.
    # The 3.12 CI job still runs all ten app launches using its own interpreter.
    launcher = shutil.which("py")
    if run == 1 and launcher:
        check = subprocess.run([launcher, "-3.13", "-c", "import json,sys; print(json.dumps(sys.executable))"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
        if check.returncode == 0:
            resolved = Path(json.loads(check.stdout)).resolve()
            if os.path.normcase(str(resolved)) == os.path.normcase(str(Path(sys.executable).resolve())):
                return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(ROOT / "Start_GigaAM.cmd")], "cmd"
    return [sys.executable, "app.py"], "python"


@pytest.mark.parametrize("run", range(10), ids=lambda run: f"launch-{run + 1:02}")
def test_windows_real_app_relaunch(native_fixture, tmp_path, run, record_property):
    native, zoom_executable = native_fixture
    zoom_present = run % 4 in (1, 2)
    restored_settings = run % 2 == 1
    record_property("synthetic_zoom_window", zoom_present)
    record_property("restored_calibration", restored_settings)
    data = tmp_path / "localappdata" / "GigaAMZoom"
    data.mkdir(parents=True)
    settings = data / "desktop-settings.json"
    if restored_settings:
        settings.write_text(json.dumps({
            "model_dir": str(tmp_path / "missing-model"), "export_dir": str(tmp_path),
            "zoom_ocr_region": "0.05,0.8,0.3,0.08", "zoom_ocr_client_size": "1000,600",
            "zoom_ocr_language": "en-US", "first_run_complete": True,
            "window_geometry": "1000x700+100+100", "use_microphone": False,
        }), encoding="utf-8")
    assert settings.exists() is restored_settings
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(data.parent)
    environment["PYTHONUNBUFFERED"] = "1"
    command, launcher_kind = _launcher_command(run)
    record_property("launcher", launcher_kind)
    zoom = app = None
    remembered = {}
    try:
        if zoom_present:
            zoom = subprocess.Popen([str(zoom_executable)], cwd=zoom_executable.parent,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _wait(lambda: native.window({zoom.pid}, "Zoom Meeting"), process=zoom, timeout=15)
        with (tmp_path / "app-console.txt").open("wb") as console:
            app = subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=console, stderr=subprocess.STDOUT)
            log = data / "logs" / "app.log"
            def ready():
                _remember_descendants(app, remembered)
                records = _events(log)
                codes = {row.get("event") for row in records}
                assert not codes.intersection({"startup_error", "startup_probe_failed", "ocr_worker_crashed", "ocr_timeout"}), \
                    "The real startup native child failed; inspect the isolated test logs"
                mapped = any(row.get("event") == "startup_step" and row.get("step") == "gui_mapped"
                             for row in records)
                metadata = next((row for row in records if row.get("event") == "windows_environment"), None)
                hwnd = native.window(set(remembered), APP_TITLE)
                if mapped and metadata is not None and "environment_probe_complete" in codes and hwnd:
                    return hwnd, metadata
                return None
            hwnd, metadata = _wait(ready, process=app)
            assert metadata["monitors"], "Native monitor enumeration must complete"
            assert not metadata.get("errors"), "Native startup metadata had a caught error"
            assert metadata["zoom_window"]["found"] is zoom_present
            assert metadata["zoom_window"]["evidence"] == (
                "zoom_window_detected" if zoom_present else "zoom_window_not_found")
            if zoom_present:
                assert metadata["zoom_version"] == "7.1.5.43453"
            else:
                assert metadata["zoom_version"] is None
            native.close(hwnd)
            assert app.wait(timeout=15) == 0, "WM_CLOSE must shut down the real application cleanly"
            deadline = time.monotonic() + 5
            while _alive(remembered) and time.monotonic() < deadline:
                time.sleep(.1)
            assert not _alive(remembered), "An application child or fault-log relay survived shutdown"
            if restored_settings:
                persisted = json.loads(settings.read_text(encoding="utf-8"))
                assert persisted["zoom_ocr_region"] == "0.05,0.8,0.3,0.08"
                assert persisted["zoom_ocr_client_size"] == "1000,600"
            crash_log = data / "logs" / "crash.log"
            assert crash_log.exists(), "Native crash breadcrumbs must be enabled at startup"
            assert "Native fault:" not in crash_log.read_text(encoding="ascii")
    finally:
        if app is not None:
            _cleanup(app, remembered)
        if zoom is not None:
            _cleanup(zoom)
