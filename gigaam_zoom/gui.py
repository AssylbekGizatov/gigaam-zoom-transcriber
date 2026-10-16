"""Russian desktop UI; no network, inference or export work on Tk's thread."""
from __future__ import annotations
from collections import deque
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import webbrowser

from .auth import ZoomAPI, ZoomError
from .config import Settings, SecretStore, meeting_origin
from .controller import Controller
from .exports import EXPORT_FORMATS, export_transcript, default_export_name, format_time
from .store import Store


class Application:
    def __init__(self, root):
        self.root = root
        root.title("GigaAM · Zoom — локальная транскрипция")
        root.geometry("1080x720")
        root.minsize(900, 620)
        self.events = queue.Queue(maxsize=256)
        self.rows = deque(maxlen=500)
        self.busy = False
        self.closing = False
        self.operation = None
        self.login_cancel = threading.Event()
        try:
            self.settings = Settings.load()
        except Exception:
            self.settings = Settings()
        self.vault = SecretStore()
        self.controller = Controller(self.settings, self.vault, self.post)
        self.variables = {}
        self.status = tk.StringVar(value="Локальный тест. Zoom ещё не подключён.")
        self.account = tk.StringVar(value="Аккаунт не подключён")
        self.meeting = tk.StringVar()
        self.meeting_start = tk.StringVar()
        self.folder = tk.StringVar(value=self.settings.export_dir)
        self.consent = tk.BooleanVar()
        self.billing = tk.BooleanVar()
        self.format = tk.StringVar(value="DOCX")
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(120, self.poll)

    def post(self, kind, payload=None):
        try:
            self.events.put_nowait((kind, payload or {}))
        except queue.Full:
            # SQLite is authoritative; a dropped refresh cannot lose transcript.
            if kind in {"error", "finished", "stop_unconfirmed"}:
                try:
                    self.events.get_nowait()
                    self.events.put_nowait((kind, payload or {}))
                except queue.Empty:
                    pass

    def _build(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=5)
        main = ttk.Frame(self.root, padding=16)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="GigaAM · Zoom", font=("Segoe UI", 21, "bold")).pack(anchor="w")
        ttk.Label(main, text="Казахский · Русский · English   |   Распознавание на вашем CPU   |   Zoom Local Test").pack(anchor="w", pady=(3, 10))
        notebook = ttk.Notebook(main)
        notebook.pack(fill="both", expand=True)
        session = ttk.Frame(notebook, padding=12)
        setup = ttk.Frame(notebook, padding=12)
        notebook.add(session, text="Встреча и текст")
        notebook.add(setup, text="Разовая настройка")
        self.notebook = notebook

        account_line = ttk.Frame(session)
        account_line.pack(fill="x")
        ttk.Label(account_line, textvariable=self.account).pack(side="left")
        self.login_button = ttk.Button(account_line, text="Подключить Zoom в браузере", command=self.login)
        self.login_button.pack(side="right")
        meeting_line = ttk.Frame(session)
        meeting_line.pack(fill="x", pady=8)
        ttk.Label(meeting_line, text="Ссылка или ID встречи").pack(side="left")
        self.meetings = ttk.Combobox(meeting_line, textvariable=self.meeting, width=55)
        self.meetings.pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(meeting_line, text="Мои встречи", command=self.list_meetings).pack(side="right")
        folder_line = ttk.Frame(session)
        folder_line.pack(fill="x")
        ttk.Label(folder_line, text="Папка сеанса и экспорта").pack(side="left")
        ttk.Entry(folder_line, textvariable=self.folder).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(folder_line, text="Выбрать…", command=self.choose_folder).pack(side="right")
        clock_line = ttk.Frame(session)
        clock_line.pack(fill="x", pady=(7, 0))
        ttk.Label(clock_line, text="Фактическое начало встречи (UTC+05)").pack(side="left")
        ttk.Entry(clock_line, textvariable=self.meeting_start, width=23).pack(side="left", padx=8)
        ttk.Label(clock_line, text="ДД.ММ.ГГГГ ЧЧ:ММ:СС · пусто = отсчёт от включения RTMS").pack(side="left")
        ttk.Checkbutton(session, text="Участники уведомлены; у меня есть разрешение получать и сохранять звук и текст этой встречи.",
                        variable=self.consent).pack(anchor="w", pady=(10, 3))
        ttk.Checkbutton(session, text="Для этого сеанса разрешаю RTMS расходовать Developer Pack кредиты по тарифу моего Zoom-аккаунта.",
                        variable=self.billing).pack(anchor="w")
        ttk.Label(session, text="Тариф и баланс проверяются в Zoom Developer Billing. Приложение не покупает кредиты и не включает автопополнение.",
                  foreground="#805600").pack(anchor="w", pady=(2, 8))
        controls = ttk.Frame(session)
        controls.pack(fill="x")
        self.start_button = ttk.Button(controls, text="Начать", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Стоп и завершить текст", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(controls, text="Открыть локальный сеанс…", command=self.open_session).pack(side="right")
        ttk.Label(session, textvariable=self.status, wraplength=990, foreground="#155b79").pack(anchor="w", fill="x", pady=9)
        table_frame = ttk.Frame(session)
        table_frame.pack(fill="both", expand=True)
        self.table = ttk.Treeview(table_frame, columns=("time", "person", "status", "text"), show="headings", height=6)
        for key, label, width in (("time", "Время от начала RTMS", 170), ("person", "Имя · Zoom ID / источник", 220),
                                  ("status", "Статус", 85), ("text", "Текст", 520)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, minwidth=70, stretch=key == "text")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.bind("<<TreeviewSelect>>", self.show_selected)
        self.detail = tk.Text(session, height=2, wrap="word", font=("Segoe UI", 10), state="disabled")
        self.detail.pack(side="bottom", before=table_frame, fill="x", pady=6)
        footer = ttk.Frame(session)
        footer.pack(side="bottom", before=self.detail, fill="x")
        ttk.Label(footer, text="Последние 500 реплик · весь текст хранится в локальном SQLite.", wraplength=530).pack(side="left")
        ttk.Combobox(footer, textvariable=self.format, values=EXPORT_FORMATS, width=7, state="readonly").pack(side="left", padx=8)
        self.export_button = ttk.Button(footer, text="Экспорт…", command=self.export)
        self.export_button.pack(side="right")

        ttk.Label(setup, text="Сначала создайте собственный General App в Zoom Marketplace → Local Test.\n"
            "Не публикуйте приложение и не включайте организационное развёртывание.", wraplength=900).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 15))
        fields = [("client_id", "Client ID", False), ("public_client_id", "Public Client ID (PKCE)", False),
                  ("client_secret", "Client Secret", True), ("webhook_secret", "Webhook Secret Token", True),
                  ("model_dir", "Локальная папка модели", False), ("webhook_port", "Локальный порт webhook", False)]
        for row, (key, label, secret) in enumerate(fields, 1):
            ttk.Label(setup, text=label).grid(row=row, column=0, sticky="w", pady=6)
            var = tk.StringVar(value="" if secret else str(getattr(self.settings, key)))
            self.variables[key] = var
            entry = ttk.Entry(setup, textvariable=var, show="●" if secret else "", width=68)
            entry.grid(row=row, column=1, sticky="ew", padx=12)
            if key == "model_dir":
                ttk.Button(setup, text="Обзор…", command=self.choose_model).grid(row=row, column=2)
        ttk.Label(setup, text="Пустые поля секретов сохраняют ранее записанные значения. Секреты шифруются DPAPI текущего пользователя Windows.",
                  wraplength=900).grid(row=7, column=0, columnspan=3, sticky="w", pady=10)
        setup_controls = ttk.Frame(setup)
        setup_controls.grid(row=8, column=0, columnspan=3, sticky="w")
        ttk.Button(setup_controls, text="Сохранить настройки", command=self.save).pack(side="left")
        ttk.Button(setup_controls, text="Включить приёмник webhook", command=self.receiver).pack(side="left", padx=8)
        ttk.Button(setup_controls, text="Открыть Marketplace", command=lambda: webbrowser.open("https://marketplace.zoom.us/develop/create")).pack(side="left")
        ttk.Label(setup, text="Получение событий требует HTTPS webhook, направленного на http://127.0.0.1:8765/webhook.\n"
            "Аудио идёт напрямую Zoom → ваш компьютер по WSS. Через webhook проходят только служебные метаданные.\n"
            "OAuth redirect: http://127.0.0.1/callback (Public Client OAuth / PKCE).\n"
            "Разовые настройки, минимальные scopes и порядок проверки: docs/ZOOM_SETUP.md.", wraplength=900).grid(row=9, column=0, columnspan=3, sticky="w", pady=18)
        ttk.Label(setup, text="RTMS показывает приложение в Active Apps / уведомлении об обработке данных Zoom.\n"
            "Отдельного участника-бота нет. Общий микрофон остаётся одним источником.", wraplength=900).grid(row=10, column=0, columnspan=3, sticky="w")
        setup.columnconfigure(1, weight=1)

    def background(self, operation):
        if self.busy or self.controller.running:
            messagebox.showinfo("Подождите", "Дождитесь завершения текущей операции или сеанса.")
            return
        self.busy = True
        def run():
            try:
                operation()
            except Exception as error:
                safe = str(error) if isinstance(error, (ZoomError, ValueError, FileExistsError)) else "Операция не выполнена. Проверьте локальные файлы, права и настройки."
                self.post("error", {"message": safe})
            finally:
                self.post("operation_done")
        self.operation = threading.Thread(target=run, name="ui-operation", daemon=False)
        self.operation.start()

    def save(self):
        if self.controller.running or self.busy:
            messagebox.showinfo("Сеанс активен", "Остановите сеанс перед изменением настроек.")
            return False
        try:
            for key in ("client_id", "public_client_id", "model_dir"):
                setattr(self.settings, key, self.variables[key].get().strip())
            self.settings.webhook_port = int(self.variables["webhook_port"].get())
            self.settings.export_dir = self.folder.get().strip()
            self.settings.event_mode = "webhook"
            self.settings.validate()
            updates = {key: self.variables[key].get().strip() for key in ("client_secret", "webhook_secret") if self.variables[key].get().strip()}
            if updates:
                self.vault.update(**updates)
            self.settings.save()
            for key in ("client_secret", "webhook_secret"):
                self.variables[key].set("")
            self.status.set("Настройки сохранены локально. Секреты зашифрованы Windows DPAPI.")
            return True
        except Exception as error:
            messagebox.showerror("Настройки", str(error) if isinstance(error, ValueError) else "Не удалось сохранить настройки и секреты.")
            return False

    def login(self):
        if not self.save():
            self.notebook.select(1)
            return
        self.status.set("Завершите вход и разрешение scopes в официальном браузере Zoom…")
        self.login_cancel.clear()
        def operation():
            self.controller.api.login(self.login_cancel)
            profile = self.controller.api.profile()
            self.post("account", profile)
        self.background(operation)

    def list_meetings(self):
        def operation():
            items = self.controller.api.meetings()
            self.post("meetings", {"items": [str(item["id"]) for item in items]})
        self.background(operation)

    def receiver(self):
        if self.save():
            self.background(self.controller.ensure_receiver)

    def choose_model(self):
        value = filedialog.askdirectory(title="Папка GigaAM с config.json и pytorch_model.bin")
        if value:
            self.variables["model_dir"].set(value)

    def choose_folder(self):
        value = filedialog.askdirectory(title="Локальная папка для текста встречи")
        if value:
            self.folder.set(value)

    def start(self):
        if self.busy or self.controller.running:
            return
        if not self.save():
            return
        try:
            self.controller.start(self.meeting.get(), consent=self.consent.get(), billing_consent=self.billing.get(),
                                  meeting_origin_ms=meeting_origin(self.meeting_start.get()))
            self.rows.clear()
            self.table.delete(*self.table.get_children())
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
            self.export_button.configure(state="disabled")
            self.status.set("Запуск локальной модели…")
        except Exception as error:
            messagebox.showerror("Запуск", str(error) if isinstance(error, (ZoomError, ValueError)) else "Не удалось запустить приёмник. Проверьте порт и секреты.")

    def stop(self):
        self.controller.stop()
        self.stop_button.configure(state="disabled")
        self.status.set("Остановка Zoom RTMS и завершение принятого аудио…")

    def open_session(self):
        if self.controller.running or self.busy:
            return
        path = filedialog.askopenfilename(filetypes=[("Локальный сеанс", "*.sqlite")])
        if path:
            def operation():
                if self.controller.store:
                    self.controller.store.close()
                self.controller.store = Store(path)
                self.post("refresh")
            self.background(operation)

    def export(self):
        if not self.controller.store:
            messagebox.showinfo("Экспорт", "Сначала завершите или откройте локальный сеанс.")
            return
        kind = self.format.get()
        path = filedialog.asksaveasfilename(initialdir=self.folder.get() or None, initialfile=default_export_name(kind),
            defaultextension="." + kind.lower(), filetypes=[(kind, "*." + kind.lower())], confirmoverwrite=False)
        if path:
            def operation():
                export_transcript(self.controller.store, path, kind)
                self.post("exported", {"message": "Экспорт сохранён: " + path})
            self.background(operation)

    def show_selected(self, _=None):
        selected = self.table.selection()
        if selected:
            values = self.table.item(selected[0], "values")
            self.detail.configure(state="normal")
            self.detail.delete("1.0", "end")
            self.detail.insert("1.0", str(values[1]) + "\n" + str(values[3]))
            self.detail.configure(state="disabled")

    def refresh(self):
        if not self.controller.store:
            return
        records = self.controller.store.recent(500)
        self.rows = deque(records, maxlen=500)
        self.table.delete(*self.table.get_children())
        for s in records:
            self.table.insert("", "end", iid=s.id, values=(f"{format_time(s.start)}–{format_time(s.end)}",
                f"{s.display_name} · {s.speaker_id}/{s.source_epoch}", "финальный" if s.status == "final" else "предварит.", s.text))
        if records:
            self.table.see(records[-1].id)
        basis = self.controller.store.get_metadata("time_basis", "first_audio")
        self.table.heading("time", text="Время от начала встречи" if basis == "meeting" else "Время от начала RTMS" if basis == "rtms_session" else "Время от первого аудио")

    def poll(self):
        refresh = False
        for _ in range(256):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind in {"segment", "refresh", "drained"}:
                refresh = True
            if kind == "account":
                self.account.set(payload.get("display_name") or payload.get("email") or "Zoom подключён")
                self.status.set("Авторизация Zoom выполнена. Доступ RTMS проверяется при явном запуске.")
            elif kind == "meetings":
                self.meetings.configure(values=payload["items"])
                self.status.set("Встречи загружены. Можно ввести ID вручную.")
            elif kind == "operation_done":
                self.busy = False
            elif kind == "finished":
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
                self.export_button.configure(state="normal")
                self.billing.set(False)
                self.consent.set(False)
                refresh = True
            elif kind == "connected":
                self.status.set("● Получаю индивидуальное аудио Zoom. GigaAM обрабатывает его локально.")
            elif kind == "progress":
                self.status.set(f"● Аудио в обработке: {payload.get('pending_seconds', 0):.1f} с · пропущено: {payload.get('dropped_seconds', 0):.1f} с")
            elif kind == "stop_unconfirmed":
                self.status.set(payload.get("message", "Zoom не подтвердил Stop. Откройте Active Apps встречи и остановите приложение."))
                messagebox.showwarning("Проверьте Zoom", self.status.get())
            elif kind == "error":
                self.status.set(payload.get("message", "Ошибка обработки."))
                messagebox.showerror("GigaAM Zoom", self.status.get())
            elif kind in {"reconnecting", "audio_gap", "mixed_audio", "origin_unavailable", "overload", "gap"}:
                self.status.set(payload.get("message", {"mixed_audio": "Смешанный источник: персональная атрибуция недоступна.",
                    "origin_unavailable": "Zoom не передал начало шкалы. Время считается от первого аудио.",
                    "reconnecting": "Восстановление соединения Zoom. Возможен пропуск аудио."}.get(kind, "Есть пропуск аудио; подробности сохранены в сеансе.")))
            elif isinstance(payload, dict) and payload.get("message"):
                self.status.set(payload["message"])
        if refresh:
            try:
                self.refresh()
            except Exception:
                self.status.set("Не удалось прочитать локальный сеанс.")
        if self.closing and not self.controller.running and not self.busy:
            def finish():
                self.controller.close()
                self.post("closed")
            if self.closing is True:
                self.closing = "waiting"
                threading.Thread(target=finish, daemon=False).start()
            elif self.closing == "waiting" and self.controller.store is None and self.controller.receiver is None:
                self.root.destroy()
                return
        self.root.after(120, self.poll)

    def close(self):
        self.closing = True
        self.login_cancel.set()
        self.controller.stop()
        self.status.set("Завершаю потоки и сохраняю принятый текст…")
        self.start_button.configure(state="disabled")


def main():
    root = tk.Tk()
    Application(root)
    root.mainloop()
