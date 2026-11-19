"""Select and verify a live Zoom nameplate without saving screen images."""
from __future__ import annotations

from io import BytesIO
import math
import tkinter as tk
from tkinter import messagebox, ttk


def normalized_selection(start, end, preview_size) -> tuple[float, float, float, float]:
    """Convert client-pixel drag coordinates to client-relative coordinates."""
    width, height = preview_size
    values = (*start, *end, width, height)
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
               and math.isfinite(value) for value in values):
        raise ValueError("Некорректные координаты области.")
    if width <= 0 or height <= 0:
        raise ValueError("Пустое изображение окна Zoom.")
    x1, x2 = sorted(min(width, max(0, point[0])) for point in (start, end))
    y1, y2 = sorted(min(height, max(0, point[1])) for point in (start, end))
    if x2 - x1 < 12 or y2 - y1 < 8:
        raise ValueError("Выделите полную строку имени с небольшим отступом.")
    region = x1 / width, y1 / height, (x2 - x1) / width, (y2 - y1) / height
    if region[2] * region[3] > .25:
        raise ValueError("Выделите только строку имени: область не должна занимать больше четверти окна Zoom.")
    return region


def screen_selection(start, end, bounds):
    """Keep negative-monitor origins and mixed Windows DPI out of Tk coordinates."""
    left, top, right, bottom = bounds
    return normalized_selection((start[0] - left, start[1] - top),
                                (end[0] - left, end[1] - top), (right - left, bottom - top))


def intersects(first, second):
    return (first[0] < second[2] and second[0] < first[2]
            and first[1] < second[3] and second[1] < first[3])


def preview_position(workarea, roi, size=(560, 340)):
    """Find a visible preview position that cannot cover the OCR source crop."""
    left, top, right, bottom = workarea
    width, height = min(size[0], right - left), min(size[1], bottom - top)
    if width < 200 or height < 150:
        raise ValueError("Недостаточно места для предварительного просмотра.")
    candidates = [(right - width, top), (right - width, bottom - height),
                  (left, top), (left, bottom - height)]
    for x, y in candidates:
        result = (x, y, x + width, y + height)
        if not intersects(result, roi):
            return result
    raise ValueError("Область слишком велика: выделите только строку имени, чтобы оставить место для проверки OCR.")


def _log(event, **details):
    from .app_log import log_event
    log_event(event, **details)


def _reason_message(reason):
    from .desktop_gui import speaker_status
    if reason == "ocr_speaker_view_hint":
        return "Имя прочитано. Подтвердите, что оно совпадает с подписью в Zoom."
    if reason == "ocr_not_started":
        return "Подготовка чтения выбранной области…"
    return speaker_status({"evidence": reason})


def _error_message(error):
    code = getattr(error, "code", None)
    if code:
        return _reason_message(code)
    if isinstance(error, ValueError):
        return str(error)
    return "Не удалось проверить область имени. Повторите выбор. Подробности сохранены в журнале приложения."


def _native():
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    return user32


def _cursor_position():
    import ctypes
    from ctypes import wintypes
    from .zoom_window import physical_pixels
    with physical_pixels():
        point = wintypes.POINT()
        if not _native().GetCursorPos(ctypes.byref(point)):
            raise OSError("Не удалось прочитать положение указателя.")
        return point.x, point.y


def _window_handle(window):
    window.update_idletasks()
    return _native().GetAncestor(window.winfo_id(), 2)


def _position_window(window, bounds, *, topmost=True):
    from .zoom_window import physical_pixels
    with physical_pixels():
        left, top, right, bottom = bounds
        # Mark the size as explicit before native placement: adding preview
        # widgets must not let Tk resize the dialog across the selected crop.
        window.geometry(f"{right - left}x{bottom - top}")
        if not _native().SetWindowPos(_window_handle(window), -1 if topmost else -2,
                                       left, top, right - left, bottom - top, 0x0040):
            raise OSError("Не удалось расположить окно выбора области.")


def _desktop_layout(parent, meeting):
    import ctypes
    from ctypes import wintypes
    from .zoom_window import physical_pixels

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                    ("work", wintypes.RECT), ("flags", wintypes.DWORD)]

    with physical_pixels():
        native = _native()
        parent_rect = wintypes.RECT()
        if not native.GetWindowRect(_window_handle(parent), ctypes.byref(parent_rect)):
            raise OSError("Не удалось определить положение приложения.")
        native.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        native.MonitorFromWindow.restype = wintypes.HANDLE
        native.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        native.GetMonitorInfoW.restype = wintypes.BOOL
        monitor = native.MonitorFromWindow(meeting.hwnd, 2)
        info = MonitorInfo(size=ctypes.sizeof(MonitorInfo))
        if not native.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise OSError("Не удалось определить монитор окна Zoom.")
        bounds = (parent_rect.left, parent_rect.top, parent_rect.right, parent_rect.bottom)
        hide = native.GetSystemMetrics(80) <= 1 or intersects(bounds, meeting.client_bounds)
        work = (info.work.left, info.work.top, info.work.right, info.work.bottom)
        return hide, work


def _same_window(before, after):
    return (before.hwnd == after.hwnd and before.pid == after.pid
            and before.client_bounds == after.client_bounds
            and before.dpi == after.dpi and before.monitor_id == after.monitor_id)


def select_region(parent, meeting):
    """Draw a translucent selection surface over the actual Zoom client area."""
    from .zoom_window import get_meeting_window, physical_pixels
    result = None
    error = None
    start = end = None
    rectangle = None
    timer = None
    with physical_pixels():
        window = tk.Toplevel(parent)
        window.withdraw()
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.attributes("-alpha", .28)
        canvas = tk.Canvas(window, bg="black", highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)
        window.deiconify()
        _position_window(window, meeting.client_bounds)
    canvas.create_text(20, 20, text="Обведите только имя говорящего • Esc: отмена",
                       anchor="nw", fill="white", font=("Segoe UI", 14, "bold"))

    def close():
        if timer is not None:
            window.after_cancel(timer)
        window.destroy()

    def validate():
        nonlocal error, timer
        try:
            if not _same_window(meeting, get_meeting_window(meeting.hwnd)):
                raise ValueError("Окно Zoom переместилось или изменилось. Повторите выделение.")
        except Exception as exc:
            error = exc
            close()
            return
        timer = window.after(250, validate)

    def canvas_point(point):
        left, top, right, bottom = meeting.client_bounds
        return ((point[0] - left) * canvas.winfo_width() / (right - left),
                (point[1] - top) * canvas.winfo_height() / (bottom - top))

    def press(_event):
        nonlocal start, end, rectangle
        start = end = _cursor_position()
        if rectangle is not None:
            canvas.delete(rectangle)
        rectangle = canvas.create_rectangle(*canvas_point(start), *canvas_point(end),
                                             outline="#00ffff", width=4)

    def drag(_event):
        nonlocal end
        if start is not None:
            end = _cursor_position()
            canvas.coords(rectangle, *canvas_point(start), *canvas_point(end))

    def release(event):
        nonlocal result, error
        if start is None:
            return
        drag(event)
        try:
            if not _same_window(meeting, get_meeting_window(meeting.hwnd)):
                raise ValueError("Окно Zoom изменилось. Повторите выделение.")
            result = screen_selection(start, end, meeting.client_bounds)
        except ValueError as exc:
            canvas.delete("error")
            canvas.create_text(20, 52, text=str(exc), anchor="nw", fill="#ffb0b0", tags="error")
            return
        except Exception as exc:
            error = exc
        close()

    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<B1-Motion>", drag)
    canvas.bind("<ButtonRelease-1>", release)
    window.bind("<Escape>", lambda _event: close())
    window.bind("<ButtonPress-3>", lambda _event: close())
    window.protocol("WM_DELETE_WINDOW", close)
    window.grab_set()
    window.focus_force()
    timer = window.after(250, validate)
    try:
        parent.wait_window(window)
    finally:
        if window.winfo_exists():
            close()
    if error is not None:
        raise error
    return result


def _preview(parent, meeting, region, workarea, *, language):
    """Show fresh image/text from the same killable sampler used by recording."""
    from PIL import Image, ImageTk
    from .speaker_observer import ZoomSpeakerObserver, monotonic_ms
    from .zoom_visual import VisualSpeakerConfig, region_bounds
    from .zoom_window import get_meeting_window

    left, top, right, bottom = meeting.client_bounds
    config = VisualSpeakerConfig(region=region, language=language,
        client_size=(right - left, bottom - top),
        include_image=True, dump_ocr_text=True)
    crop = region_bounds(meeting.client_bounds, config)
    bounds = preview_position(workarea, crop)
    observer = ZoomSpeakerObserver(visual_config=config, log_samples=False)
    window = tk.Toplevel(parent)
    window.title("Проверка имени Zoom")
    window.attributes("-topmost", True)
    window.resizable(False, False)
    _position_window(window, bounds)
    frame = ttk.Frame(window, padding=12)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Проверьте полное имя и смену имени при смене говорящего.",
              wraplength=510).pack(anchor="w")
    image_label = ttk.Label(frame)
    image_label.pack(fill="x", pady=8)
    name = tk.StringVar(value="Чтение выбранной области…")
    ttk.Label(frame, textvariable=name, font=("Segoe UI", 12, "bold"), wraplength=510).pack(anchor="w")
    raw = tk.StringVar()
    ttk.Label(frame, textvariable=raw, wraplength=510).pack(anchor="w", pady=5)
    status = tk.StringVar()
    ttk.Label(frame, textvariable=status, wraplength=510).pack(anchor="w")
    buttons = ttk.Frame(frame)
    buttons.pack(side="bottom", fill="x", pady=8)
    result = None
    timer = None
    last_frame = None
    photo = None
    last_error = None

    def finish(value):
        nonlocal result
        if value == "confirm":
            try:
                valid = (last_frame is not None and last_frame.observation.display_name
                         and 0 <= monotonic_ms() - last_frame.observation.observed_at_ms <= last_frame.max_age_ms
                         and _same_window(meeting, get_meeting_window(meeting.hwnd)))
            except Exception:
                valid = False
            if not valid:
                status.set("Имя или окно изменилось. Дождитесь нового результата либо повторите выделение.")
                return
        result = value
        window.destroy()

    confirm = ttk.Button(buttons, text="Имя верно — сохранить", command=lambda: finish("confirm"), state="disabled")
    confirm.pack(side="left")
    ttk.Button(buttons, text="Выделить заново", command=lambda: finish("retry")).pack(side="left", padx=6)
    ttk.Button(buttons, text="Отмена", command=lambda: finish(None)).pack(side="right")
    window.bind("<Escape>", lambda _event: finish(None))
    window.protocol("WM_DELETE_WINDOW", lambda: finish(None))

    def update():
        nonlocal timer, last_frame, photo, last_error
        try:
            current_window = get_meeting_window(meeting.hwnd)
            if not _same_window(meeting, current_window):
                confirm.configure(state="disabled")
                status.set("Окно Zoom изменилось. Нажмите «Выделить заново».")
                name.set("Имя не подтверждено")
                raw.set("")
                last_frame = None
            else:
                current = observer.latest_frame()
                if current is None:
                    last_frame = None
                    confirm.configure(state="disabled")
                    name.set("Ожидание нового чтения имени…")
                    raw.set("")
                    status.set(_reason_message(observer.latest().evidence))
                else:
                    last_frame = current
                    observation = current.observation
                    name.set(observation.display_name or "Имя пока не распознано")
                    raw.set("Текст в области: " + " | ".join(current.raw_lines))
                    status.set(_reason_message(observation.evidence))
                    fresh = 0 <= monotonic_ms() - observation.observed_at_ms <= current.max_age_ms
                    confirm.configure(state="normal" if fresh and observation.display_name else "disabled")
                    if current.image_png:
                        with Image.open(BytesIO(current.image_png)) as source:
                            image = source.copy()
                        image.thumbnail((510, 100))
                        photo = ImageTk.PhotoImage(image, master=window)
                        image_label.configure(image=photo)
                        image.close()
            last_error = None
        except Exception as exc:
            error_key = (type(exc).__name__, getattr(exc, "code", None))
            if error_key != last_error:
                from .app_log import log_exception
                log_exception("calibration_preview_error", exc)
                last_error = error_key
            last_frame = None
            confirm.configure(state="disabled")
            name.set("Имя не подтверждено")
            raw.set("")
            status.set(_error_message(exc))
        timer = window.after(200, update)

    try:
        observer.start()
        timer = window.after(100, update)
        parent.wait_window(window)
    finally:
        if timer is not None:
            try:
                parent.after_cancel(timer)
            except tk.TclError:
                pass
        observer.stop()
        if window.winfo_exists():
            window.destroy()
    return result


def begin_calibration(parent, on_finished, *, language="ru-RU"):
    """Overlay selection, live OCR confirmation, and restoration on every exit."""
    from .zoom_window import bring_meeting_to_front, find_meeting_window
    from .app_log import log_exception

    original_state = parent.state()
    completed = False
    timer = None
    countdown = None

    def clear_countdown():
        nonlocal countdown, timer
        if timer is not None:
            parent.after_cancel(timer)
            timer = None
        if countdown is not None:
            countdown.destroy()
            countdown = None

    def complete(region=None, size=None):
        nonlocal completed
        if completed:
            return
        completed = True
        clear_countdown()
        try:
            if parent.winfo_exists():
                parent.state(original_state)
                if original_state != "iconic":
                    parent.lift()
        finally:
            on_finished(region, size)

    def fail(exc):
        clear_countdown()
        log_exception("calibration_error", exc)
        code = getattr(exc, "code", None)
        if code:
            _log("calibration_failed", evidence=code)
        # A modal error must not be owned by an unmapped parent on Windows.
        if parent.state() == "withdrawn":
            parent.state(original_state)
            parent.lift()
        if messagebox.askretrycancel("Не удалось выбрать имя", _error_message(exc), parent=parent):
            prepare()
        else:
            _log("calibration_cancelled")
            complete()

    def run():
        clear_countdown()
        try:
            meeting = bring_meeting_to_front()
            _hide, workarea = _desktop_layout(parent, meeting)
            region = select_region(parent, meeting)
            if region is None:
                _log("calibration_cancelled")
                complete()
                return
            _log("calibration_selected", region=list(region))
            action = _preview(parent, meeting, region, workarea, language=language)
            if action == "retry":
                _log("calibration_retry")
                prepare()
                return
            if action != "confirm":
                _log("calibration_cancelled")
                complete()
                return
            left, top, right, bottom = meeting.client_bounds
            _log("calibration_complete", region=list(region), client_size=[right - left, bottom - top])
            complete(",".join(f"{value:.8f}" for value in region), f"{right - left},{bottom - top}")
        except Exception as exc:
            fail(exc)

    def prepare():
        nonlocal timer, countdown
        try:
            meeting = find_meeting_window()
            hide, workarea = _desktop_layout(parent, meeting)
            if hide:
                parent.withdraw()
                countdown = tk.Toplevel(parent)
                countdown.overrideredirect(True)
                countdown.attributes("-topmost", True)
                text = tk.StringVar(value="Выбор области Zoom через 3…  Esc — отмена")
                ttk.Label(countdown, textvariable=text, padding=16).pack()
                _position_window(countdown, (workarea[0] + 20, workarea[1] + 20,
                                               workarea[0] + 470, workarea[1] + 80))
                countdown.bind("<Escape>", lambda _event: cancel_countdown())
                countdown.focus_force()
                def tick(remaining):
                    nonlocal timer
                    if remaining == 0:
                        run()
                    else:
                        text.set(f"Выбор области Zoom через {remaining}…  Esc — отмена")
                        timer = parent.after(1000, lambda: tick(remaining - 1))
                tick(3)
            else:
                timer = parent.after(50, run)
        except Exception as exc:
            fail(exc)

    def cancel_countdown():
        clear_countdown()
        _log("calibration_cancelled")
        complete()

    _log("calibration_started")
    prepare()
