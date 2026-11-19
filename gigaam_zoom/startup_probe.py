"""Run native Windows diagnostics in a short-lived interpreter after Tk is mapped."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from .app_log import breadcrumb, log_event, log_exception, log_windows_environment


def probe_environment() -> dict:
    """Called from the GUI's refresh job. No native extensions load in this process."""
    result = {"languages": [], "zoom_found": False, "zoom_evidence": "ocr_window_unavailable"}
    breadcrumb("probe_start")
    try:
        child = subprocess.run(
            [sys.executable, "-m", "gigaam_zoom.startup_probe"], cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=12, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if child.returncode:
            log_event("ocr_worker_crashed", exit_code=child.returncode, reason="environment_probe")
            return {**result, "evidence": "ocr_worker_crashed", "exit_code": child.returncode}
        details = json.loads(child.stdout)
        window = details["zoom_window"]
        languages = details["ocr_languages"]
        if (not isinstance(languages, list) or not all(isinstance(tag, str) for tag in languages)
                or type(window["found"]) is not bool or not isinstance(window["evidence"], str)):
            raise ValueError("Invalid startup probe response")
        log_windows_environment(details)
        return {"languages": languages, "zoom_found": window["found"],
                "zoom_evidence": window["evidence"],
                "evidence": "ocr_unavailable" if "ocr_languages_error" in details.get("errors", []) else ""}
    except subprocess.TimeoutExpired:
        log_event("ocr_timeout", reason="environment_probe")
        return {**result, "evidence": "ocr_timeout"}
    except (OSError, ValueError, TypeError, KeyError) as error:
        log_exception("environment_probe_error", error)
        return {**result, "evidence": "ocr_unavailable"}


def _main():
    # Only this child owns the WinRT apartment used by language enumeration.
    from .zoom_window import enable_process_dpi_awareness
    from .app_log import collect_startup_environment
    enable_process_dpi_awareness()
    print(json.dumps(collect_startup_environment(), ensure_ascii=True))


if __name__ == "__main__":
    _main()
