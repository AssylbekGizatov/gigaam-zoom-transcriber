# -*- coding: utf-8 -*-
"""Windows entry point. Run with py -3.13 app.py."""
from multiprocessing import freeze_support


def main():
    import os
    from gigaam_zoom.app_log import (
        breadcrumb, configure_logging, enable_crash_logging, log_directory, log_exception,
    )

    try:
        enable_crash_logging()
        breadcrumb("configure_logging")
        configure_logging()
        breadcrumb("enable_dpi_awareness")
        from gigaam_zoom.zoom_window import enable_process_dpi_awareness
        enable_process_dpi_awareness()
        breadcrumb("import_desktop_gui")
        from gigaam_zoom.desktop_gui import main as run_gui
        breadcrumb("start_desktop_gui")
        run_gui()
    except Exception as exc:
        log_exception("startup_error", exc)
        from tkinter import messagebox
        if messagebox.askyesno("Ошибка запуска", "Не удалось запустить приложение. Открыть логи?"):
            os.startfile(log_directory())
        return 1
    return 0


if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
