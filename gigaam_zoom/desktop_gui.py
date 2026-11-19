"""Small native UI for live, offline transcription; no web service or login."""
from __future__ import annotations

from pathlib import Path
import os
import webbrowser
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from .desktop import DesktopController, DesktopSettings
from .app_log import breadcrumb, log_event, log_exception, log_directory, set_verbose_logging
from .desktop_audio import list_devices
from .exports import default_export_name, export_transcript, format_time
from .store import Store


_OCR_UNAVAILABLE = frozenset({
    "ocr_worker_crashed", "ocr_worker_exited", "ocr_worker_start_failed", "ocr_timeout",
    "ocr_unavailable", "ocr_dependency_missing", "ocr_language_unavailable", "ocr_windows_only",
})


def select_ocr_language(languages, current=""):
    """Keep a saved installed choice; otherwise prefer Russian, then English."""
    from .zoom_visual import preferred_ocr_language
    saved = next((tag for tag in languages if tag.casefold() == current.casefold()), "")
    return saved or preferred_ocr_language(languages)


def speaker_status(payload):
    name, reason = payload.get("name"), payload.get("evidence", "")
    if name and reason not in _OCR_UNAVAILABLE:
        return f"Говорящий: {name}"
    messages = {
        "ocr_not_calibrated": "Говорящий: Неизвестно — выберите область имени",
        "ocr_region_offscreen": "Область имени за пределами экрана; переместите окно Zoom",
        "ocr_focus_failed": "Не удалось открыть окно Zoom; переключитесь в него вручную",
        "ocr_window_unavailable": "Окно встречи Zoom недоступно",
        "ocr_capture_blank": "Область имени пуста или закрыта другим окном",
        "ocr_name_ambiguous": "В области несколько имён; выделите одну подпись",
        "ocr_name_ui_only": "В области кнопки Zoom; выделите только имя",
        "ocr_unavailable": "OCR недоступен; проверьте зависимости Windows",
        "ocr_worker_crashed": "OCR недоступен: процесс распознавания завершился с ошибкой",
        "ocr_worker_exited": "OCR недоступен: процесс распознавания неожиданно остановился",
        "ocr_worker_start_failed": "OCR недоступен: не удалось запустить процесс распознавания",
        "ocr_dependency_missing": "OCR недоступен: установите зависимости по разделу «Помощь»",
        "ocr_windows_only": "OCR доступен только в Windows",
        "ocr_dpi_unavailable": "Не удалось определить масштаб экрана Windows",
        "ocr_language_unavailable": "В Windows не установлен выбранный язык OCR",
        "ocr_not_foreground": "Переключитесь в окно встречи Zoom",
        "ocr_window_not_foreground": "Переключитесь в окно встречи Zoom",
        "zoom_window_not_found": "Окно встречи Zoom не найдено",
        "zoom_window_ambiguous": "Открыто несколько окон встречи Zoom",
        "ocr_window_minimized": "Разверните окно встречи Zoom",
        "ocr_window_resized": "Размер окна Zoom изменился; заново выберите область имени",
        "ocr_recalibration_required": "Размер окна Zoom изменился; заново выберите область имени",
        "ocr_window_too_small": "Увеличьте окно встречи и заново выберите область",
        "ocr_window_changed": "Окно Zoom изменилось во время чтения; повторная проверка",
        "ocr_capture_unavailable": "Не удалось получить изображение окна Zoom",
        "ocr_region_too_small": "Область слишком мала; заново выделите полное имя",
        "ocr_region_occluded": "Другое окно перекрывает область имени",
        "ocr_name_clipped": "Имя обрезано; выберите область шире",
        "ocr_name_missing": "В выбранной области не видно полного имени",
        "ocr_confirming": "Ожидание повторного чтения имени",
        "ocr_missing": "В выбранной области не видно имени",
        "ocr_ambiguous": "В области несколько строк; выберите только имя",
        "ocr_unstable": "Ожидание повторного чтения имени",
        "ocr_timeout": "Превышено время чтения OCR",
        "ocr_stale": "Наблюдение OCR устарело",
    }
    detail = messages.get(reason, "Говорящий: Неизвестно")
    return detail


class DesktopApplication:
    def __init__(self, root):
        self.root = root
        root.title("GigaAM · Живой текст Zoom")
        screen_width, screen_height = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{min(1100, screen_width - 40)}x{min(800, screen_height - 60)}")
        root.minsize(min(900, screen_width - 40), min(620, screen_height - 60))
        self.events = queue.Queue(maxsize=256)
        self.controller = DesktopController(self.post)
        self.busy = self.closing = False
        self.job = None
        self.devices = []
        self.dirty = True
        self.snapshot_busy = False
        breadcrumb("gui_load_settings")
        try:
            config = DesktopSettings.load()
        except (ValueError, OSError) as error:
            log_exception("settings_load_failed", error)
            config = DesktopSettings()
        self.model = tk.StringVar(value=config.model_dir)
        self.folder = tk.StringVar(value=config.export_dir)
        self.name = tk.StringVar(value=config.local_name)
        self.microphone = tk.BooleanVar(value=config.use_microphone)
        self.ocr_region = tk.StringVar(value=config.zoom_ocr_region)
        self.ocr_language = tk.StringVar(value=config.zoom_ocr_language)
        self.ocr_client_size = tk.StringVar(value=config.zoom_ocr_client_size)
        self.verbose = tk.BooleanVar(value=config.verbose_logging)
        self.first_run_complete = config.first_run_complete
        self.saved_loopback = config.loopback_name
        self.saved_microphone = config.microphone_name
        self.onboarding = None
        self.ocr_languages = []
        self.language_status = tk.StringVar(value="Проверка установленных языков OCR…")
        self.model_status = tk.StringVar()
        self.zoom_status = tk.StringVar(value="Zoom: проверка окна встречи…")
        self.checklist = tk.StringVar()
        try:
            if config.window_geometry:
                root.geometry(config.window_geometry)
        except tk.TclError:
            pass
        set_verbose_logging(config.verbose_logging)
        self.loopback = tk.StringVar()
        self.mic_device = tk.StringVar()
        self.status = tk.StringVar(value="Откройте встречу в Zoom, выберите звук и нажмите «Начать».")
        self.speaker = tk.StringVar(value="Имя говорящего: ещё не проверено")
        self.metrics = tk.StringVar(value="Аудио и видео не сохраняются · модель работает локально")
        self.format = tk.StringVar(value=config.export_format if config.export_format in {"DOCX", "PDF", "TXT"} else "DOCX")
        breadcrumb("gui_build_widgets")
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)
        root.report_callback_exception = self.callback_exception
        self._previous_thread_hook = threading.excepthook
        self._thread_hook = self.thread_exception
        threading.excepthook = self._thread_hook
        breadcrumb("gui_check_model")
        self.update_checklist()
        self._startup_probe_scheduled = False
        self._map_binding = root.bind("<Map>", self._on_first_map, add="+")
        if root.winfo_ismapped():
            self._on_first_map()

    def _on_first_map(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        if self._startup_probe_scheduled or self.closing:
            return
        self._startup_probe_scheduled = True
        self.root.unbind("<Map>", self._map_binding)
        breadcrumb("gui_mapped")
        self.root.after_idle(self._first_refresh)
        if not self.first_run_complete:
            self.root.after(400, self.show_first_run)

    def _first_refresh(self):
        if self.closing:
            return
        if self.busy or self.controller.running:
            self.root.after(100, self._first_refresh)
            return
        breadcrumb("gui_first_refresh")
        self.refresh_environment()

    def post(self, kind, payload=None):
        item = (kind, payload if payload is not None else {})
        try:
            self.events.put_nowait(item)
        except queue.Full:
            if kind in {"finished", "error", "operation_done", "ready", "devices", "snapshot", "operation_error", "zoom_window", "model_ready", "environment_probe"}:
                try:
                    self.events.get_nowait()
                    self.events.put_nowait(item)
                except (queue.Empty, queue.Full):
                    pass

    def _build(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=5)
        base = ttk.Frame(self.root, padding=14)
        base.pack(fill="both", expand=True)
        header = ttk.Frame(base)
        header.pack(fill="x")
        ttk.Label(header, text="GigaAM · Живой текст Zoom", font=("Segoe UI", 20, "bold")).pack(side="left")
        self.settings_toggle = ttk.Button(header, text="Настройки ▸", command=self.toggle_settings)
        self.settings_toggle.pack(side="right")

        device_row = ttk.Frame(base)
        device_row.pack(fill="x", pady=5)
        ttk.Label(device_row, text="Звук Zoom").pack(side="left")
        self.output_combo = ttk.Combobox(device_row, textvariable=self.loopback, state="readonly", width=50)
        self.output_combo.pack(side="left", fill="x", expand=True, padx=8)
        self.refresh_button = ttk.Button(device_row, text="Обновить", command=self.refresh_environment)
        self.refresh_button.pack(side="right")
        mic_row = ttk.Frame(base)
        mic_row.pack(fill="x", pady=5)
        ttk.Checkbutton(mic_row, text="Мой микрофон", variable=self.microphone).pack(side="left")
        self.mic_combo = ttk.Combobox(mic_row, textvariable=self.mic_device, state="readonly", width=35)
        self.mic_combo.pack(side="left", fill="x", expand=True, padx=8)
        ttk.Label(mic_row, text="Моё имя").pack(side="left")
        ttk.Entry(mic_row, textvariable=self.name, width=18).pack(side="left", padx=(5, 0))
        name_row = ttk.Frame(base)
        name_row.pack(fill="x", pady=5)
        self.calibrate_button = ttk.Button(name_row, text="Выбрать область имени…", command=self.calibrate_name)
        self.calibrate_button.pack(side="left")
        ttk.Label(name_row, text="Язык").pack(side="left", padx=(10, 4))
        self.language_combo = ttk.Combobox(name_row, textvariable=self.ocr_language, state="readonly", width=12)
        self.language_combo.pack(side="left")
        self.language_combo.bind("<<ComboboxSelected>>", lambda _: self.update_checklist())
        ttk.Label(name_row, textvariable=self.speaker, wraplength=570).pack(side="left", padx=8)


        folder_row = ttk.Frame(base)
        folder_row.pack(fill="x", pady=5)
        ttk.Label(folder_row, text="Папка текста").pack(side="left")
        ttk.Entry(folder_row, textvariable=self.folder).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(folder_row, text="Выбрать…", command=self.choose_folder).pack(side="right")
        self.settings_anchor = folder_row
        self.settings_frame = ttk.LabelFrame(base, text="Настройки", padding=8)
        model_row = ttk.Frame(self.settings_frame)
        model_row.pack(fill="x")
        ttk.Label(model_row, text="GigaAM").pack(side="left")
        ttk.Entry(model_row, textvariable=self.model).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(model_row, text="Папка…", command=self.choose_model).pack(side="left")
        options = ttk.Frame(self.settings_frame)
        options.pack(fill="x")
        ttk.Checkbutton(options, text="Подробные логи OCR (могут содержать имена)", variable=self.verbose,
                        command=self.change_verbose_logging).pack(side="left")
        ttk.Button(options, text="Сохранить настройки", command=self.save).pack(side="right")

        controls = ttk.Frame(base)
        controls.pack(fill="x", pady=(8, 4))
        self.start_button = ttk.Button(controls, text="▶ Начать", command=self.start)
        self.start_button.configure(width=18)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="■ Стоп и завершить текст", command=self.stop, state="disabled")
        self.stop_button.configure(width=26)
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(controls, text="Открыть прошлый сеанс…", command=self.open_session).pack(side="right")
        ttk.Label(base, textvariable=self.status, foreground="#145970", wraplength=980).pack(anchor="w", fill="x", pady=4)
        meter_row = ttk.Frame(base)
        meter_row.pack(fill="x")
        ttk.Label(meter_row, text="Выход").pack(side="left")
        self.remote_meter = ttk.Progressbar(meter_row, maximum=1, length=85)
        self.remote_meter.pack(side="left", padx=6)
        ttk.Label(meter_row, text="Микрофон").pack(side="left")
        self.mic_meter = ttk.Progressbar(meter_row, maximum=1, length=85)
        self.mic_meter.pack(side="left", padx=6)
        ttk.Label(meter_row, textvariable=self.metrics, foreground="#555555", wraplength=670).pack(side="left", padx=8)
        bottom = ttk.Frame(base)
        bottom.pack(side="bottom", fill="x")
        table_frame = ttk.Frame(base)
        table_frame.pack(fill="both", expand=True)
        self.table = ttk.Treeview(table_frame, columns=("time", "name", "text"), show="headings", height=6)
        for key, label, width in (("time", "Время", 135), ("name", "Говорящий", 230), ("text", "Текст", 555)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, minwidth=60, stretch=key == "text")
        scroll = ttk.Scrollbar(table_frame, command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.bind("<<TreeviewSelect>>", self.show_selected)
        self.detail = tk.Text(bottom, height=2, wrap="word", font=("Segoe UI", 10), state="disabled")
        footer = ttk.Frame(bottom)
        self.detail_anchor = footer
        footer.pack(fill="x")
        ttk.Button(footer, text="Исправить имя реплики…", command=self.correct_name).pack(side="left")
        self.export_button = ttk.Button(footer, text="Экспорт полного текста…", command=self.export)
        self.export_button.pack(side="right")
        ttk.Combobox(footer, textvariable=self.format, values=("DOCX", "PDF", "TXT"), state="readonly", width=8).pack(side="right", padx=8)
        model_bottom = ttk.Frame(bottom)
        model_bottom.pack(fill="x", pady=(6, 0))
        ttk.Label(model_bottom, textvariable=self.model_status).pack(side="left")
        self.download_button = ttk.Button(model_bottom, text="Скачать модель", command=self.download_model)
        self.download_button.pack(side="left", padx=8)
        ttk.Label(model_bottom, textvariable=self.zoom_status).pack(side="left", padx=8)
        ttk.Label(bottom, textvariable=self.language_status, foreground="#67502c", wraplength=980).pack(anchor="w")
        ttk.Button(model_bottom, text="?", command=self.open_help, width=3).pack(side="right")
        ttk.Button(model_bottom, text="Открыть логи", command=self.open_logs).pack(side="right", padx=6)

    def settings(self):
        return DesktopSettings(model_dir=self.model.get().strip(), export_dir=self.folder.get().strip(),
                               local_name=self.name.get().strip(), use_microphone=self.microphone.get(),
                               zoom_ocr_region=self.ocr_region.get().strip(), zoom_ocr_language=self.ocr_language.get().strip(),
                               zoom_ocr_client_size=self.ocr_client_size.get(), verbose_logging=self.verbose.get(),
                               window_geometry=self.root.geometry(), loopback_name=self.selected_device_name("loopback"),
                               microphone_name=self.selected_device_name("microphone"), first_run_complete=self.first_run_complete, export_format=self.format.get())

    def selected_device_name(self, kind):
        selected = self.loopback.get() if kind == "loopback" else self.mic_device.get()
        return next((device.name for device in self.devices if device.kind == kind and self.device_label(device) == selected),
                    self.saved_loopback if kind == "loopback" else self.saved_microphone)

    def toggle_settings(self, show=None):
        visible = bool(self.settings_frame.winfo_manager())
        show = not visible if show is None else show
        if show:
            self.settings_frame.pack(fill="x", after=self.settings_anchor, pady=(0, 4))
        else:
            self.settings_frame.pack_forget()
        self.settings_toggle.configure(text="Настройки ▾" if show else "Настройки ▸")

    def update_checklist(self):
        folder = Path(self.model.get()).expanduser()
        model_ok = all((folder / name).is_file() for name in ("config.json", "pytorch_model.bin"))
        if getattr(self, "_last_model_ok", None) != model_ok:
            log_event("model_check", model_ok=model_ok)
            self._last_model_ok = model_ok
        self.model_status.set("Модель: найдена" if model_ok else "Модель: не установлена")
        steps = [
            (model_ok, "Модель GigaAM — скачайте один раз или выберите готовую папку."),
            (bool(self.ocr_languages), "Язык OCR — выберите установленный язык Windows."),
            (getattr(self, "zoom_found", False), "Zoom — откройте одну встречу в Speaker View без Pin и Spotlight."),
            (bool(self.ocr_region.get()), "Имя — выделите подпись главного видео и проверьте результат."),
            (bool(self.folder.get()), "Текст — выберите папку для сохранения."),
        ]
        self.checklist.set("\n".join(("✓ " if ready else "○ ") + text for ready, text in steps))

    def show_first_run(self):
        if self.onboarding and self.onboarding.winfo_exists():
            self.onboarding.lift()
            return
        dialog = tk.Toplevel(self.root)
        self.onboarding = dialog
        dialog.title("Первый запуск · пять коротких шагов")
        dialog.transient(self.root)
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Подготовка к встрече", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(body, textvariable=self.checklist, justify="left", wraplength=680).pack(anchor="w", pady=12)
        for actions in ((("Скачать модель", self.download_model), ("Готовая модель…", self.choose_model),
                         ("Языки Windows", self.open_language_settings)),
                        (("Показать Zoom", self.show_zoom), ("Область имени", self.calibrate_name),
                         ("Папка текста", self.choose_folder))):
            row = ttk.Frame(body)
            row.pack(fill="x", pady=3)
            for label, action in actions:
                ttk.Button(row, text=label, command=action).pack(side="left", padx=(0, 6))
        ttk.Label(body, text="Без области имени транскрипция работает с подписью «Неизвестно». "
                  "Имена читаются с экрана и могут ошибаться; проверьте их перед экспортом.",
                  wraplength=680).pack(anchor="w", pady=12)
        ttk.Button(body, text="Перейти к встрече", command=self.finish_first_run).pack(anchor="e")
        dialog.protocol("WM_DELETE_WINDOW", self.finish_first_run)

    def finish_first_run(self):
        self.first_run_complete = True
        if self.onboarding and self.onboarding.winfo_exists():
            self.onboarding.destroy()
        self.onboarding = None
        try:
            self.settings().save(validate=False)
        except (OSError, ValueError) as error:
            self.show_failure(error, "Не удалось сохранить настройки")

    def refresh_environment(self):
        def inspect():
            from .startup_probe import probe_environment
            breadcrumb("enumerate_audio_devices")
            try:
                self.post("devices", list_devices())
            except Exception as error:
                log_exception("audio_devices_error", error)
                self.post("device_status", "Не удалось получить аудиоустройства. Нажмите «Обновить».")
            try:
                self.post("environment_probe", probe_environment())
            except Exception as error:
                log_exception("startup_probe_failed", error)
                self.post("environment_probe", {"languages": [], "zoom_found": False,
                                                 "evidence": "ocr_unavailable",
                                                 "zoom_evidence": "ocr_window_unavailable"})
        self.background(inspect)

    def _apply_environment_probe(self, result):
        self.ocr_languages = result.get("languages", [])
        self.language_combo["values"] = self.ocr_languages
        selected = select_ocr_language(self.ocr_languages, self.ocr_language.get())
        # A failed probe must not erase an explicit saved language choice.
        if selected:
            self.ocr_language.set(selected)
        self.zoom_found = result.get("zoom_found", False)
        self.zoom_status.set("Zoom: окно встречи найдено" if self.zoom_found else
                             speaker_status({"evidence": result.get("zoom_evidence", "zoom_window_not_found")}))
        evidence = result.get("evidence", "")
        if evidence in _OCR_UNAVAILABLE:
            self._show_ocr_unavailable(evidence)
        elif not self.ocr_languages:
            self._show_ocr_unavailable("ocr_language_unavailable")
        else:
            self.language_status.set("OCR: язык установлен; выберите область имени")
            if getattr(self, "_ocr_unavailable", False):
                self.status.set("Язык OCR доступен. Можно выбрать область имени и начать транскрипцию.")
                self._ocr_unavailable = False
        self.update_checklist()
        log_event("environment_probe_complete", evidence=evidence or result.get("zoom_evidence"))

    def _show_ocr_unavailable(self, evidence):
        self._ocr_unavailable = True
        detail = speaker_status({"evidence": evidence})
        self.speaker.set("Говорящий: Неизвестно")
        self.language_status.set(detail)
        self.status.set("OCR недоступен. Транскрипция звука доступна без имён. "
                        "Проверьте язык Windows и локальные логи.")

    def download_model(self):
        if self.busy or self.controller.running:
            return
        destination = self.model.get().strip()
        if not destination:
            destination = str(Path(__file__).resolve().parents[1] / "models" / "gigaam")
            self.model.set(destination)
        self.status.set("Загрузка модели из GitHub models-v1. Это может занять несколько минут…")
        def download():
            from scripts.acquire_model import acquire
            log_event("model_download_started")
            acquire(Path(destination).expanduser())
            log_event("model_download_complete")
            self.post("model_ready", {"message": "Модель скачана и проверена. Интернет для распознавания больше не нужен."})
        self.background(download)

    def show_zoom(self):
        def activate():
            from .zoom_window import bring_meeting_to_front
            try:
                bring_meeting_to_front()
                self.post("zoom_window", {"found": True})
            except Exception as error:
                code = getattr(error, "code", "ocr_window_unavailable")
                log_event("zoom_window_check", evidence=code)
                self.post("zoom_window", {"found": False, "evidence": code})
                self.post("status", {"message": speaker_status({"evidence": code})})
        self.background(activate)

    def open_language_settings(self):
        if os.name == "nt":
            os.startfile("ms-settings:regionlanguage")
        else:
            self.open_help()

    def change_verbose_logging(self):
        enabled = self.verbose.get()
        if enabled and not messagebox.askokcancel(
            "Подробные логи OCR", "Подробные логи могут содержать распознанные имена участников. "
            "Включайте их только для диагностики; перед передачей файлов проверьте содержимое. "
            "Снимки экрана и аудио в логи не записываются.", parent=self.root):
            self.verbose.set(False)
            enabled = False
        set_verbose_logging(enabled)

    @staticmethod
    def _open_local(path):
        path = Path(path).resolve()
        if os.name == "nt":
            os.startfile(str(path))
        else:
            webbrowser.open(path.as_uri())

    def open_logs(self):
        self._open_local(log_directory())

    def open_help(self):
        self._open_local(Path(__file__).resolve().parents[1] / "docs" / "LOCAL_HELP.html")

    def show_failure(self, error, title="Не удалось завершить действие"):
        log_exception("gui_exception", error)
        detail = str(error) if isinstance(error, ValueError) else "Проверьте выбранные папки, устройства и доступ к модели."
        if messagebox.askyesno(title, detail + "\n\nДиагностика сохранена в локальных логах. Открыть папку логов?", parent=self.root):
            self.open_logs()

    def callback_exception(self, exception_type, error, traceback):
        self.show_failure(error)

    def thread_exception(self, args):
        log_exception("thread_error", args.exc_value)
        self.post("operation_error", {"message": "Фоновая операция завершилась с ошибкой. "
                                     "Остановите сеанс и проверьте локальные логи."})

    def background(self, operation):
        if self.busy or self.controller.running:
            messagebox.showinfo("Сеанс активен", "Дождитесь завершения операции или остановите сеанс.")
            return
        self.busy = True
        def run():
            try:
                operation()
            except Exception as error:
                log_exception("background_operation_failed", error)
                self.post("operation_error", {"message": "Не удалось завершить действие. Проверьте выбранную папку и доступ к GitHub для загрузки модели."})
            finally:
                self.post("operation_done")
        self.job = threading.Thread(target=run, name="desktop-ui-job", daemon=False)
        self.job.start()

    @staticmethod
    def device_label(device):
        return f"{device.index}: {device.name}"

    def refresh_devices(self):
        self.background(lambda: self.post("devices", list_devices()))

    def choose_folder(self):
        value = filedialog.askdirectory(title="Локальная папка текста")
        if value:
            self.folder.set(value)
            self.update_checklist()

    def choose_model(self):
        value = filedialog.askdirectory(title="Папка config.json и pytorch_model.bin")
        if value:
            self.model.set(value)
            self.update_checklist()

    def calibrate_name(self):
        if self.busy or self.controller.running:
            return
        from .zoom_calibration import begin_calibration
        self.busy = True

        def finished(region, client_size=None):
            self.busy = False
            if region:
                self.ocr_region.set(region)
                self.ocr_client_size.set(client_size or "")
                self.status.set("Область имени выбрана. Оставьте её видимой во время встречи.")
                self.update_checklist()
                self.settings().save(validate=False)

        if self.onboarding and self.onboarding.winfo_exists():
            self.onboarding.withdraw()
        def calibration_done(region, client_size=None):
            if self.onboarding and self.onboarding.winfo_exists():
                self.onboarding.deiconify()
            finished(region, client_size)
        try:
            begin_calibration(self.root, calibration_done, language=self.ocr_language.get() or "auto")
        except Exception as error:
            calibration_done(None, None)
            self.show_failure(error, "Не удалось выбрать область имени")

    def save(self):
        settings = self.settings()
        self.background(lambda: (settings.save(validate=False), self.post("status", {"message": "Настройки сохранены локально."})))

    def start(self):
        if self.busy or self.controller.running:
            return
        try:
            config = self.settings()
            config.save()
            output = next((d.index for d in self.devices if d.kind == "loopback" and self.device_label(d) == self.loopback.get()), None)
            mic = next((d.index for d in self.devices if d.kind == "microphone" and self.device_label(d) == self.mic_device.get()), None)
            if output is None:
                raise ValueError("Выберите выход звука Zoom.")
            self.controller.start(config, output, mic)
            self.toggle_settings(False)
            self.status.set("Загрузка модели…")
            self.table.delete(*self.table.get_children())
            self.dirty = True
        except (ValueError, OSError, RuntimeError) as error:
            self.show_failure(error, "Не удалось начать")

    def stop(self):
        self.controller.stop()
        self.status.set("Завершаю захват и принятый текст…")

    def show_selected(self, event=None):
        selected = self.table.selection()
        if selected:
            value = self.table.item(selected[0], "values")
            self.detail.pack(fill="x", before=self.detail_anchor, pady=5)
            self.detail.configure(state="normal")
            self.detail.delete("1.0", "end")
            self.detail.insert("end", value[2])
            self.detail.configure(state="disabled")

    def correct_name(self):
        if self.busy or self.controller.running:
            messagebox.showinfo("Завершите сеанс", "Исправление имён доступно после остановки.")
            return
        selected = self.table.selection()
        if not selected or not self.controller.store:
            return
        name = simpledialog.askstring("Имя говорящего", "Имя для выбранной реплики:", parent=self.root)
        if name:
            identifier = selected[0]
            self.background(lambda: (self.controller.store.correct_speaker(identifier, name), self.post("refresh")))

    def open_session(self):
        if self.busy or self.controller.running:
            return
        path = filedialog.askopenfilename(title="Локальный журнал текста", filetypes=[("SQLite", "*.sqlite")])
        if path:
            def load():
                candidate = Store(path)
                try:
                    candidate.recent(1)
                except Exception:
                    candidate.close()
                    raise
                if self.controller.store:
                    self.controller.store.close()
                self.controller.store = candidate
                self.post("refresh")
                self.post("status", {"message": "Открыт локальный сеанс. Экспорт включает весь текст."})
            self.background(load)

    def export(self):
        if not self.controller.store or self.busy or self.controller.running:
            messagebox.showinfo("Экспорт", "Остановите текущий сеанс или откройте прошлый журнал.")
            return
        kind = self.format.get()
        folder = filedialog.askdirectory(title="Папка экспорта", initialdir=self.folder.get() or None)
        if folder:
            target = Path(folder) / default_export_name(kind)
            def write_export():
                log_event("export_started", format=kind)
                export_transcript(self.controller.store, target, kind)
                log_event("export_complete", format=kind)
                self.post("status", {"message": f"Экспорт сохранён: {target}"})
            self.background(write_export)

    def poll(self):
        try:
            for _ in range(100):
                kind, payload = self.events.get_nowait()
                if kind in {"status", "ready", "finished", "error", "capture_error", "capture_gap", "overload", "store_error", "inference_failed", "attribution_failed"}:
                    self.status.set(payload.get("message", kind))
                    if kind == "error" and not self.closing:
                        if messagebox.askyesno("Не удалось запустить транскрипцию", payload.get("message", "Проверьте настройки.") + "\n\nОткрыть папку логов?", parent=self.root):
                            self.open_logs()
                if kind in {"segment", "ready", "finished", "refresh"}:
                    self.dirty = True
                if kind == "operation_done":
                    self.busy = False
                    self.update_checklist()
                elif kind == "operation_error":
                    self.status.set(payload["message"])
                    if messagebox.askyesno("Не удалось завершить действие", payload["message"] + "\n\nОткрыть папку логов?", parent=self.root):
                        self.open_logs()
                elif kind == "model_ready":
                    self.status.set(payload["message"])
                    self.update_checklist()
                elif kind == "device_status":
                    self.status.set(payload)
                elif kind == "zoom_window":
                    self.zoom_found = payload.get("found", False)
                    self.zoom_status.set("Zoom: окно встречи найдено" if self.zoom_found else
                                         speaker_status({"evidence": payload.get("evidence", "zoom_window_not_found")}))
                    self.update_checklist()
                elif kind == "environment_probe":
                    self._apply_environment_probe(payload)
                elif kind == "levels":
                    self.remote_meter["value"] = payload.get("remote", 0)
                    self.mic_meter["value"] = payload.get("microphone", 0)
                elif kind == "speaker":
                    self.speaker.set(speaker_status(payload))
                    reason = payload.get("evidence", "")
                    if reason == "ocr_speaker_view_hint" and getattr(self, "_ocr_unavailable", False):
                        self._ocr_unavailable = False
                        self.language_status.set("OCR: чтение имени восстановлено")
                        self.status.set("Идёт транскрипция. Чтение имён OCR восстановлено.")
                    if reason in _OCR_UNAVAILABLE:
                        self._show_ocr_unavailable(reason)
                        self.zoom_status.set("Zoom: OCR недоступен")
                    elif reason == "ocr_window_minimized":
                        self.zoom_status.set("Zoom: свёрнут")
                    elif reason in {"ocr_region_occluded", "ocr_capture_blank"}:
                        self.zoom_status.set("Zoom: область перекрыта или пуста")
                    elif reason in {"zoom_window_not_found", "zoom_window_ambiguous"}:
                        self.zoom_status.set("Zoom: нужна одна открытая встреча")
                    elif reason in {"ocr_recalibration_required", "ocr_window_changed"}:
                        self.zoom_status.set("Zoom: повторите выбор области")
                    elif reason == "ocr_not_calibrated":
                        self.zoom_status.set("Zoom: область имени не выбрана")
                    else:
                        self.zoom_status.set("Zoom: окно доступно")
                elif kind == "progress":
                    self.metrics.set(f"Очередь: {payload.get('pending_seconds',0):.1f} с · "
                                     f"Обработка/звук: {payload.get('real_time_factor',0):.2f} · "
                                     f"Пропущено: {payload.get('dropped_seconds',0):.1f} с")
                elif kind == "devices":
                    self.devices = payload
                    for name, combo, var in (("loopback", self.output_combo, self.loopback), ("microphone", self.mic_combo, self.mic_device)):
                        devices = [d for d in payload if d.kind == name]
                        labels = [self.device_label(d) for d in devices]
                        combo["values"] = labels
                        if var.get() not in labels:
                            saved = self.saved_loopback if name == "loopback" else self.saved_microphone
                            default = next((d for d in devices if d.name == saved), None)
                            if default is None:
                                default = next((d for d in devices if d.is_default), devices[0] if devices else None)
                            var.set(self.device_label(default) if default else "")
                elif kind == "snapshot":
                    self.snapshot_busy = False
                    store = self.controller.store
                    if store and str(store.path) == payload.get("path"):
                        segments = payload.get("segments", [])
                        self.table.delete(*self.table.get_children())
                        for item in segments:
                            self.table.insert("", "end", iid=item.id, values=(f"{format_time(item.start)}–{format_time(item.end)}", item.display_name,
                                                                               item.text))
                        if segments:
                            self.table.see(segments[-1].id)
        except queue.Empty:
            pass
        running = self.controller.running
        self.start_button.configure(state="disabled" if running or self.busy else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.refresh_button.configure(state="disabled" if running or self.busy else "normal")
        self.calibrate_button.configure(state="disabled" if running or self.busy else "normal")
        self.download_button.configure(state="disabled" if running or self.busy else "normal")
        self.settings_toggle.configure(state="disabled" if running else "normal")
        self.language_combo.configure(state="disabled" if running else "readonly")
        if self.dirty and self.controller.store and not self.busy and not self.snapshot_busy:
            self.snapshot_busy = True
            self.dirty = False
            store = self.controller.store
            def snapshot():
                try:
                    self.post("snapshot", {"path": str(store.path), "segments": store.recent(500)})
                except Exception:
                    self.post("error", {"message": "Не удалось прочитать локальный журнал текста."})
                    self.post("snapshot", {})
            threading.Thread(target=snapshot, name="text-snapshot", daemon=True).start()
        if self.closing and not running and not self.busy and not self.snapshot_busy:
            if self.controller.store:
                self.controller.store.close()
            if threading.excepthook is self._thread_hook:
                threading.excepthook = self._previous_thread_hook
            self.root.destroy()
            return
        self.root.after(150, self.poll)

    def close(self):
        try:
            self.settings().save(validate=False)
        except (OSError, ValueError) as error:
            log_exception("settings_save_failed", error)
        self.closing = True
        self.stop()


def main():
    import multiprocessing
    multiprocessing.freeze_support()
    breadcrumb("gui_before_tk")
    root = tk.Tk()
    breadcrumb("gui_before_application")
    DesktopApplication(root)
    breadcrumb("gui_before_mainloop")
    root.mainloop()
