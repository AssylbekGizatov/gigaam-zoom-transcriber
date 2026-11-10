"""In-memory selection of the displayed main-video name in Zoom Speaker View."""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import messagebox, ttk


def normalized_selection(start, end, preview_size) -> tuple[float, float, float, float]:
    """Convert a drag in a scaled preview to client-relative coordinates."""
    width, height = preview_size
    values = (*start, *end, width, height)
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
        raise ValueError("Некорректные координаты области.")
    if width <= 0 or height <= 0:
        raise ValueError("Пустое изображение окна Zoom.")
    x1, x2 = sorted(min(width, max(0, point[0])) for point in (start, end))
    y1, y2 = sorted(min(height, max(0, point[1])) for point in (start, end))
    if x2 - x1 < 12 or y2 - y1 < 8:
        raise ValueError("Выделите полную строку имени с небольшим отступом.")
    return x1 / width, y1 / height, (x2 - x1) / width, (y2 - y1) / height


def select_region(parent, captured_image) -> str | None:
    """Display a disposable client snapshot; never save screenshot bytes."""
    from PIL import Image, ImageTk

    preview = captured_image.copy()
    preview.thumbnail((min(1200, parent.winfo_screenwidth() - 100),
                       min(720, parent.winfo_screenheight() - 220)), Image.Resampling.LANCZOS)
    window = tk.Toplevel(parent)
    window.title("Имя главного видео в Zoom Speaker View")
    window.transient(parent)
    window.resizable(False, False)
    ttk.Label(window, text="Обведите только полное имя главного видео с небольшим отступом. "
              "Оставьте место для более длинных имён.\nНе включайте значок микрофона, другие плитки или панель Zoom.",
              padding=10, wraplength=max(400, preview.width)).pack(fill="x")
    photo = ImageTk.PhotoImage(preview, master=window)
    canvas = tk.Canvas(window, width=preview.width, height=preview.height,
                       highlightthickness=0, cursor="crosshair")
    canvas.pack()
    canvas.create_image(0, 0, image=photo, anchor="nw")
    error = tk.StringVar(master=window)
    ttk.Label(window, textvariable=error, foreground="#a02020", padding=6).pack(fill="x")
    result = None
    start = end = None
    rectangle = None

    def press(event):
        nonlocal start, end, rectangle
        start = end = (event.x, event.y)
        if rectangle is not None:
            canvas.delete(rectangle)
        rectangle = canvas.create_rectangle(*start, *end, outline="#00ffff", width=2)
        error.set("")

    def drag(event):
        nonlocal end
        if start is not None:
            end = (event.x, event.y)
            canvas.coords(rectangle, *start, *end)

    def accept():
        nonlocal result
        if start is None or end is None:
            error.set("Сначала выделите область имени мышью.")
            return
        try:
            region = normalized_selection(start, end, preview.size)
        except ValueError as exc:
            error.set(str(exc))
            return
        result = ",".join(f"{value:.8f}" for value in region)
        window.destroy()

    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<B1-Motion>", drag)
    canvas.bind("<ButtonRelease-1>", drag)
    buttons = ttk.Frame(window, padding=10)
    buttons.pack(fill="x")
    ttk.Button(buttons, text="Использовать область", command=accept).pack(side="left")
    ttk.Button(buttons, text="Отмена", command=window.destroy).pack(side="right")
    window.bind("<Escape>", lambda _: window.destroy())
    window.grab_set()
    parent.wait_window(window)
    preview.close()
    return result


def begin_calibration(parent, on_finished):
    """Give the user time to foreground Zoom before taking one client snapshot."""
    if not messagebox.askokcancel(
        "Выбор области имени", "Откройте в Zoom Speaker View без закрепления и Spotlight. "
        "Включите постоянный показ имён участников.\n\n"
        "После нажатия OK приложение свернётся. За 4 секунды переключитесь в Zoom, "
        "чтобы окно встречи было на переднем плане и имя главного видео было видно. "
        "Затем выделите имя на снимке. Снимок используется только в памяти.", parent=parent):
        on_finished(None, None)
        return
    parent.iconify()

    def capture():
        image = None
        region = None
        client_size = None
        try:
            from .zoom_visual import capture_meeting_client
            image, _bounds = capture_meeting_client()
            client_size = f"{image.width},{image.height}"
            parent.deiconify()
            parent.lift()
            region = select_region(parent, image)
        except Exception as exc:
            parent.deiconify()
            parent.lift()
            code = getattr(exc, "code", type(exc).__name__)
            messagebox.showerror("Область имени не выбрана",
                                 "Не удалось получить окно встречи Zoom. Оставьте одно окно встречи "
                                 "на переднем плане и проверьте установку OCR-зависимостей.\n\n"
                                 f"Диагностика: {code}", parent=parent)
        finally:
            if image is not None:
                image.close()
            on_finished(region, client_size if region else None)

    parent.after(4000, capture)
