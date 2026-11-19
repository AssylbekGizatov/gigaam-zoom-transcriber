"""Test-only instrumentation for executing the unchanged ``python app.py`` command.

External devices and probe results are fixtures. Tk, settings, GUI construction,
event dispatch, and WM_DELETE_WINDOW shutdown remain the production code.
The production application never imports this module or reads its environment.
"""
from __future__ import annotations

import ast
import atexit
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import sys
import time
import tokenize
import traceback


_spec_path = os.environ.get("GIGAAM_TEST_STARTUP_SPEC")
if _spec_path:
    SPEC = json.loads(Path(_spec_path).read_text(encoding="utf-8"))
    STATE = {"winrt_attempts": [], "unexpected_dialogs": [], "probe_calls": 0,
             "root_before_probe": [], "ready": False, "closed": False}
    STARTED = time.monotonic()

    def save_state():
        Path(SPEC["result"]).write_text(json.dumps(STATE, ensure_ascii=False), encoding="utf-8")

    atexit.register(save_state)

    def root_exists():
        tkinter = sys.modules.get("tkinter")
        return tkinter is not None and getattr(tkinter, "_default_root", None) is not None

    def fixture_probe(*args, **kwargs):
        STATE["probe_calls"] += 1
        mapped = root_exists() and STATE.get("root_mapped", False)
        STATE["root_before_probe"].append(mapped)
        if not mapped:
            raise AssertionError("Native startup probe ran before the main Tk window was mapped")
        return {"languages": ["ru-RU", "en-US"], "zoom_found": SPEC["zoom_open"],
                "zoom_evidence": "zoom_window_found" if SPEC["zoom_open"] else "zoom_window_not_found"}

    def unexpected_dialog(*args, **kwargs):
        # Fixed test-generated GUI only. Keep incidental messages out of artifacts.
        STATE["unexpected_dialogs"].append("unexpected_modal")
        return False

    def verify_utf8_dialog(root):
        """Round-trip the actual error strings through real Tcl/Tk widgets."""
        import tkinter as tk
        import tkinter.font as tkfont
        with tokenize.open(SPEC["entrypoint"]) as source:
            literals = {node.value for node in ast.walk(ast.parse(source.read()))
                        if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        title = "Ошибка запуска"
        message = "Не удалось запустить приложение. Открыть логи?"
        assert title in literals and message in literals, "Startup dialog source has changed or is not UTF-8"
        dialog = tk.Toplevel(root)
        try:
            dialog.title(title)
            label = tk.Label(dialog, text=message, font="TkDefaultFont", padx=16, pady=12)
            label.pack()
            dialog.geometry("640x120")
            dialog.update()
            assert dialog.title() == title
            assert label.cget("text") == message
            assert str(root.tk.call(label._w, "cget", "-text")) == message
            assert label.winfo_viewable() and label.winfo_width() > 1
            assert tkfont.Font(root=root, font="TkDefaultFont").measure(message) > 0
            STATE["utf8_dialog"] = True
        finally:
            dialog.destroy()

    def install_gui_checks(module):
        original = module.DesktopApplication.__init__
        for name in ("askyesno", "askretrycancel", "showerror", "showinfo", "askokcancel"):
            setattr(module.messagebox, name, unexpected_dialog)

        def initialize(app, root):
            original(app, root)
            gui_started = time.monotonic()
            STATE["constructed"] = True
            STATE["loaded_calibration"] = bool(app.ocr_region.get())
            expected_region = "0.12000000,0.81000000,0.32000000,0.05000000" if SPEC["settings_present"] else ""
            if app.ocr_region.get() != expected_region:
                STATE["harness_error"] = "Saved calibration was not loaded verbatim"
            original_destroy = root.destroy

            def destroyed():
                STATE["closed"] = bool(app.closing and not app.controller.running and not app.busy)
                original_destroy()
            root.destroy = destroyed

            STATE["root_mapped"] = bool(root.winfo_viewable())
            def mapped(event):
                if event.widget is root:
                    STATE["root_mapped"] = True
            root.bind("<Map>", mapped, add="+")

            def check_ready():
                try:
                    if time.monotonic() - STARTED > 10:
                        raise AssertionError("GUI environment did not settle within ten seconds")
                    settled = (STATE["probe_calls"] > 0 and not app.busy
                               and app.ocr_languages == ["ru-RU", "en-US"]
                               and getattr(app, "zoom_found", None) == SPEC["zoom_open"]
                               and root.winfo_viewable())
                    # Let the real first-run callback run in the no-settings case.
                    if not settled or time.monotonic() - gui_started < .6:
                        root.after(50, check_ready)
                        return
                    assert not STATE["winrt_attempts"], "Parent attempted a WinRT import"
                    assert not STATE["unexpected_dialogs"], "Startup raised an unexpected error dialog"
                    assert all(STATE["root_before_probe"])
                    assert bool(app.onboarding) == (not SPEC["settings_present"])
                    assert root.title() == "GigaAM · Живой текст Zoom"
                    verify_utf8_dialog(root)
                    STATE["ready"] = True
                    # Exercise the actual WM close protocol, not root.destroy().
                    command = root.protocol("WM_DELETE_WINDOW")
                    assert command
                    root.tk.call(command)
                except Exception:
                    STATE["harness_error"] = traceback.format_exc()
                    app.close()
            root.after(50, check_ready)
        module.DesktopApplication.__init__ = initialize

    class Loader(importlib.abc.Loader):
        def __init__(self, delegate, name):
            self.delegate, self.name = delegate, name

        def create_module(self, spec):
            create = getattr(self.delegate, "create_module", None)
            return create(spec) if create else None

        def exec_module(self, module):
            self.delegate.exec_module(module)
            if self.name == "gigaam_zoom.startup_probe":
                module.probe_environment = fixture_probe
            elif self.name == "gigaam_zoom.desktop_audio":
                module.list_devices = lambda: [module.AudioDevice(
                    1, "Fixture Speakers", "loopback", 48000, 2, True)]
            elif self.name == "gigaam_zoom.desktop_gui":
                install_gui_checks(module)

    class StartupGuard(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "winrt" or fullname.startswith("winrt."):
                STATE["winrt_attempts"].append({"module": fullname, "root_exists": root_exists()})
                raise AssertionError("WinRT must not be imported in the GUI parent process")
            if fullname in {"gigaam_zoom.startup_probe", "gigaam_zoom.desktop_audio", "gigaam_zoom.desktop_gui"}:
                spec = importlib.machinery.PathFinder.find_spec(fullname, path)
                if spec is not None and spec.loader is not None:
                    spec.loader = Loader(spec.loader, fullname)
                return spec
            return None

    sys.meta_path.insert(0, StartupGuard())
