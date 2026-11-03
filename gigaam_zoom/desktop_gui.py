"""Small native UI for live, offline transcription; no web service or login."""
from __future__ import annotations

from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from .desktop import DesktopController, DesktopSettings
from .desktop_audio import list_devices
from .exports import EXPORT_FORMATS, default_export_name, export_transcript, format_time
from .store import Store


class DesktopApplication:
    def __init__(self, root):
        self.root = root
        root.title("GigaAM · Живой текст Zoom")
        root.geometry("1100x800")
        root.minsize(900, 690)
        self.events = queue.Queue(maxsize=256)
        self.controller = DesktopController(self.post)
        self.busy = self.closing = False
        self.job = None
        self.devices = []
        self.dirty = True
        self.snapshot_busy = False
        try:
            config = DesktopSettings.load()
        except (ValueError, OSError):
            config = DesktopSettings()
        self.model = tk.StringVar(value=config.model_dir)
        self.folder = tk.StringVar(value=config.export_dir)
        self.name = tk.StringVar(value=config.local_name)
        self.microphone = tk.BooleanVar(value=config.use_microphone)
        self.names = tk.BooleanVar(value=config.read_zoom_names)
        self.voices = tk.BooleanVar(value=config.use_diarization)
        self.voice_model = tk.StringVar(value=config.diarization_model)
        self.loopback = tk.StringVar()
        self.mic_device = tk.StringVar()
        self.status = tk.StringVar(value="Откройте встречу в Zoom, выберите звук и нажмите «Начать».")
        self.speaker = tk.StringVar(value="Имя говорящего: ещё не проверено")
        self.metrics = tk.StringVar(value="Аудио и видео не сохраняются · модель работает локально")
        self.format = tk.StringVar(value="DOCX")
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)
        root.after(200, self.refresh_devices)

    def post(self, kind, payload=None):
        item = (kind, payload if payload is not None else {})
        try:
            self.events.put_nowait(item)
        except queue.Full:
            if kind in {"finished", "error", "operation_done", "ready", "devices", "snapshot"}:
                try:
                    self.events.get_nowait()
                    self.events.put_nowait(item)
                except (queue.Empty, queue.Full):
                    pass

    def _build(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=6)
        base = ttk.Frame(self.root, padding=18)
        base.pack(fill="both", expand=True)
        ttk.Label(base, text="GigaAM · Живой текст", font=("Segoe UI", 23, "bold")).pack(anchor="w")
        ttk.Label(base, text="Zoom → звук компьютера → локальная GigaAM    •    Қазақша / Русский / English",
                  foreground="#376577").pack(anchor="w", pady=(4, 10))
        tabs = ttk.Notebook(base)
        tabs.pack(fill="both", expand=True)
        live = ttk.Frame(tabs, padding=12)
        settings = ttk.Frame(tabs, padding=15)
        tabs.add(live, text="Встреча")
        tabs.add(settings, text="Модель и помощь")
        device_row = ttk.Frame(live)
        device_row.pack(fill="x")
        ttk.Label(device_row, text="Звук Zoom").pack(side="left")
        self.output_combo = ttk.Combobox(device_row, textvariable=self.loopback, state="readonly", width=55)
        self.output_combo.pack(side="left", fill="x", expand=True, padx=8)
        self.refresh_button = ttk.Button(device_row, text="Обновить устройства", command=self.refresh_devices)
        self.refresh_button.pack(side="right")
        ttk.Label(live, text="Выберите тот же выход, что в Zoom. Он также захватывает другие приложения на этом выходе.",
                  foreground="#67502c").pack(anchor="w", pady=(3, 7))
        mic_row = ttk.Frame(live)
        mic_row.pack(fill="x")
        ttk.Checkbutton(mic_row, text="Добавить мой микрофон", variable=self.microphone).pack(side="left")
        self.mic_combo = ttk.Combobox(mic_row, textvariable=self.mic_device, state="readonly", width=40)
        self.mic_combo.pack(side="left", fill="x", expand=True, padx=8)
        ttk.Label(mic_row, text="Моё имя").pack(side="left")
        ttk.Entry(mic_row, textvariable=self.name, width=16).pack(side="right", padx=(5, 0))
        ttk.Checkbutton(live, text="Проверять имя активного говорящего в окне Zoom (подсказка, возможны ошибки)",
                        variable=self.names).pack(anchor="w", pady=6)
        ttk.Checkbutton(live, text="Различать голоса локально: Говорящий 1, 2… (приблизительно; нужна модель голосов)",
                        variable=self.voices).pack(anchor="w", pady=(0, 6))
        folder_row = ttk.Frame(live)
        folder_row.pack(fill="x", pady=4)
        ttk.Label(folder_row, text="Папка текста").pack(side="left")
        ttk.Entry(folder_row, textvariable=self.folder).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(folder_row, text="Выбрать…", command=self.choose_folder).pack(side="right")
        controls = ttk.Frame(live)
        controls.pack(fill="x", pady=(8, 4))
        self.start_button = ttk.Button(controls, text="▶ Начать", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="■ Стоп и завершить текст", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(controls, text="Открыть прошлый сеанс…", command=self.open_session).pack(side="right")
        ttk.Label(live, textvariable=self.status, foreground="#145970", wraplength=1000).pack(anchor="w", fill="x", pady=7)
        meter_row = ttk.Frame(live)
        meter_row.pack(fill="x")
        ttk.Label(meter_row, text="Выход").pack(side="left")
        self.remote_meter = ttk.Progressbar(meter_row, maximum=1, length=100)
        self.remote_meter.pack(side="left", padx=6)
        ttk.Label(meter_row, text="Микрофон").pack(side="left")
        self.mic_meter = ttk.Progressbar(meter_row, maximum=1, length=100)
        self.mic_meter.pack(side="left", padx=6)
        ttk.Label(meter_row, textvariable=self.speaker, wraplength=610).pack(side="left", padx=8)
        ttk.Label(live, textvariable=self.metrics, foreground="#555555").pack(anchor="w", pady=5)
        table_frame = ttk.Frame(live)
        table_frame.pack(fill="both", expand=True)
        self.table = ttk.Treeview(table_frame, columns=("time", "name", "method", "text"), show="headings", height=6)
        for key, label, width in (("time", "От старта захвата", 175), ("name", "Говорящий", 190),
                                  ("method", "Источник имени", 125), ("text", "Финальный текст", 440)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, minwidth=60, stretch=key == "text")
        scroll = ttk.Scrollbar(table_frame, command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.bind("<<TreeviewSelect>>", self.show_selected)
        self.detail = tk.Text(live, height=2, wrap="word", font=("Segoe UI", 10), state="disabled")
        self.detail.pack(fill="x", pady=6)
        footer = ttk.Frame(live)
        footer.pack(fill="x")
        ttk.Button(footer, text="Исправить имя выбранной реплики…", command=self.correct_name).pack(side="left")
        ttk.Button(footer, text="Подписать этот голос…", command=lambda: self.correct_name(whole_source=True)).pack(side="left", padx=6)
        self.export_button = ttk.Button(footer, text="Экспорт полного текста…", command=self.export)
        self.export_button.pack(side="right")
        ttk.Combobox(footer, textvariable=self.format, values=EXPORT_FORMATS, state="readonly", width=8).pack(side="right", padx=8)
        ttk.Label(live, text="В окне последние 500 реплик. Весь текст — в выбранной папке. Уведомите участников о транскрипции.",
                  foreground="#555555").pack(anchor="w", pady=(7, 0))
        ttk.Label(settings, text="Локальная модель GigaAM Multilingual 220M CTC", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        model_row = ttk.Frame(settings)
        model_row.pack(fill="x", pady=12)
        ttk.Entry(model_row, textvariable=self.model).pack(side="left", fill="x", expand=True)
        ttk.Button(model_row, text="Выбрать папку…", command=self.choose_model).pack(side="right", padx=(8, 0))
        ttk.Label(settings, text="Необязательная модель голосов CAM++ (ONNX, 28 МБ)").pack(anchor="w")
        voice_row = ttk.Frame(settings)
        voice_row.pack(fill="x", pady=8)
        ttk.Entry(voice_row, textvariable=self.voice_model).pack(side="left", fill="x", expand=True)
        ttk.Button(voice_row, text="Выбрать файл…", command=self.choose_voice_model).pack(side="right", padx=(8, 0))
        ttk.Button(settings, text="Сохранить настройки", command=self.save).pack(anchor="w")
        help_text = (
            "1. Войдите в звонок в обычном Zoom Workplace. Включать Record не нужно.\n\n"
            "2. Выберите тот же выход звука, что использует Zoom. Наденьте наушники: иначе микрофон может повторно услышать собеседников.\n\n"
            "3. Нажмите «Начать» до разговора. Во время звонка модель превращает короткие буферы звука в текст и освобождает их. "
            "После окончания получить пропущенную речь без записи невозможно.\n\n"
            "4. «Стоп» завершает принятые фразы. Экспорт доступен в TXT, Markdown, DOCX, PDF и JSON. "
            "SQLite хранит только текст и служебные метки для восстановления после перезапуска.\n\n"
            "Имена: это не API Zoom. Если окно отдаёт явную метку активного говорящего, имя помечено «по окну Zoom». "
            "Оно может запаздывать. Если метки нет — «не определён». Общий микрофон, одновременная речь и скрытые метки не разделяются. "
            "Любую реплику можно подписать вручную. ID здесь локальные, не Zoom ID.\n\n"
            "Нет RTMS, облачного STT, серверов, подписок или счётчика минут. Используются ваш компьютер, электричество и обычное соединение Zoom. "
            "Модель загружается отдельно при установке; во время сеанса интернет для распознавания не нужен.\n\n"
            "Подключение/отключение наушников: остановите сеанс, обновите устройства и начните новый. "
            "При ошибке захвата или нехватке ресурсов приложение сообщает о пропусках; оно не обещает полноту потерянного звука."
        )
        ttk.Label(settings, text=help_text, wraplength=980, justify="left").pack(anchor="w", pady=20)

    def settings(self):
        return DesktopSettings(self.model.get().strip(), self.folder.get().strip(), self.name.get().strip(),
                               self.microphone.get(), self.names.get(), self.voices.get(), self.voice_model.get().strip())

    def background(self, operation):
        if self.busy or self.controller.running:
            messagebox.showinfo("Сеанс активен", "Дождитесь завершения операции или остановите сеанс.")
            return
        self.busy = True
        def run():
            try:
                operation()
            except Exception as error:
                self.post("error", {"message": str(error) if isinstance(error, (ValueError, OSError, RuntimeError)) else type(error).__name__})
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

    def choose_model(self):
        value = filedialog.askdirectory(title="Папка config.json и pytorch_model.bin")
        if value:
            self.model.set(value)

    def choose_voice_model(self):
        value = filedialog.askopenfilename(title="Проверенная модель голосов ONNX", filetypes=[("ONNX", "*.onnx")])
        if value:
            self.voice_model.set(value)

    def save(self):
        settings = self.settings()
        self.background(lambda: (settings.save(), self.post("status", {"message": "Настройки сохранены локально."})))

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
            self.status.set("Загрузка модели…")
            self.table.delete(*self.table.get_children())
            self.dirty = True
        except (ValueError, OSError, RuntimeError) as error:
            messagebox.showerror("Не удалось начать", str(error))

    def stop(self):
        self.controller.stop()
        self.status.set("Завершаю захват и принятый текст…")

    def show_selected(self, event=None):
        selected = self.table.selection()
        if selected:
            value = self.table.item(selected[0], "values")
            self.detail.configure(state="normal")
            self.detail.delete("1.0", "end")
            self.detail.insert("end", value[3])
            self.detail.configure(state="disabled")

    def correct_name(self, whole_source=False):
        if self.busy or self.controller.running:
            messagebox.showinfo("Завершите сеанс", "Исправление имён доступно после остановки.")
            return
        selected = self.table.selection()
        if not selected or not self.controller.store:
            return
        name = simpledialog.askstring("Имя говорящего", "Имя для выбранной реплики:", parent=self.root)
        if name:
            identifier = selected[0]
            self.background(lambda: (self.controller.store.correct_speaker(identifier, name, whole_source=whole_source), self.post("refresh")))

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
            self.background(lambda: (export_transcript(self.controller.store, target, kind),
                                     self.post("status", {"message": f"Экспорт сохранён: {target}"})))

    def poll(self):
        try:
            for _ in range(100):
                kind, payload = self.events.get_nowait()
                if kind in {"status", "ready", "finished", "error", "capture_error", "capture_gap", "overload", "store_error", "inference_failed", "attribution_failed"}:
                    self.status.set(payload.get("message", kind))
                if kind in {"segment", "ready", "finished", "refresh"}:
                    self.dirty = True
                if kind == "operation_done":
                    self.busy = False
                elif kind == "levels":
                    self.remote_meter["value"] = payload.get("remote", 0)
                    self.mic_meter["value"] = payload.get("microphone", 0)
                elif kind == "speaker":
                    self.speaker.set("Имя по окну Zoom: " + (payload.get("name") or "не определено"))
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
                            default = next((d for d in devices if d.is_default), devices[0] if devices else None)
                            var.set(self.device_label(default) if default else "")
                elif kind == "snapshot":
                    self.snapshot_busy = False
                    store = self.controller.store
                    if store and str(store.path) == payload.get("path"):
                        segments = payload.get("segments", [])
                        self.table.delete(*self.table.get_children())
                        labels = {"zoom_ui_hint": "Окно Zoom (?)", "unattributed_mix": "Не определён", "local_microphone": "Мой микрофон", "manual": "Исправлено", "zoom_source_id": "Zoom ID", "voice_cluster": "По голосу (?)"}
                        for item in segments:
                            self.table.insert("", "end", iid=item.id, values=(f"{format_time(item.start)}–{format_time(item.end)}", item.display_name,
                                                                               labels.get(item.attribution, item.attribution), item.text))
                        if segments:
                            self.table.see(segments[-1].id)
        except queue.Empty:
            pass
        running = self.controller.running
        self.start_button.configure(state="disabled" if running or self.busy else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.refresh_button.configure(state="disabled" if running or self.busy else "normal")
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
            self.root.destroy()
            return
        self.root.after(150, self.poll)

    def close(self):
        self.closing = True
        self.stop()


def main():
    import multiprocessing
    multiprocessing.freeze_support()
    root = tk.Tk()
    DesktopApplication(root)
    root.mainloop()
