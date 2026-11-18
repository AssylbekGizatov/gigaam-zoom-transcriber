"""Windows entry point. Run with py -3.13 app.py."""
if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()
    from gigaam_zoom.zoom_window import enable_process_dpi_awareness
    enable_process_dpi_awareness()
    from gigaam_zoom.app_log import configure_logging, log_exception
    configure_logging()
    import threading
    from gigaam_zoom.app_log import log_startup_environment
    threading.excepthook = lambda args: log_exception("thread_error", args.exc_value)
    threading.Thread(target=log_startup_environment, daemon=True).start()
    try:
        from gigaam_zoom.desktop_gui import main
        main()
    except Exception as exc:
        log_exception("startup_error", exc)
        from tkinter import messagebox
        if messagebox.askyesno("Ошибка запуска", "Не удалось запустить приложение. Открыть логи?"):
            import os
            from gigaam_zoom.app_log import log_directory
            os.startfile(log_directory())
