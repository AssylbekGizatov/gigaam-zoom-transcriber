"""Background meeting lifecycle; the Tk thread only renders bounded snapshots."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
import threading
import time
from uuid import uuid4

from .auth import ZoomAPI, ZoomError
from .config import meeting_id
from .engine import Transcriber
from .events import WebhookReceiver, websocket_events
from .pipeline import Pipeline
from .rtms import RTMSClient
from .store import Store


class Controller:
    def __init__(self, settings, vault, on_event):
        self.settings, self.vault, self.on_event = settings, vault, on_event
        self.api = ZoomAPI(settings, vault)
        self.store = self.pipeline = self.client = None
        self.receiver = None
        self.thread = None
        self.loop = None
        self.stop_event = None
        self.events_queue = None
        self._cancel = threading.Event()
        self._active_id = None
        self._rtms_started = False
        self._last_gap_time = None
        self._failed = False
        self._stream_epoch = ""

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def _emit(self, kind, payload=None):
        self.on_event(kind, payload or {})

    def ensure_receiver(self):
        if self.settings.event_mode == "webhook":
            secret = self.vault.load().get("webhook_secret", "")
            if self.receiver and (self.receiver.secret != secret or self.receiver.port != self.settings.webhook_port):
                self.receiver.stop()
                self.receiver = None
            if self.receiver:
                return
            receiver = WebhookReceiver(secret, self._lifecycle_event, self.settings.webhook_port)
            receiver.start()
            self.receiver = receiver
            self._emit("receiver_ready", {"message": f"Приёмник: http://127.0.0.1:{receiver.port}/webhook. Нужен ваш HTTPS forwarder."})

    def _lifecycle_event(self, doc):
        if self.loop and self.events_queue and not self.loop.is_closed():
            def deliver():
                try:
                    self.events_queue.put_nowait(doc)
                except asyncio.QueueFull:
                    self._emit("error", {"message": "Очередь событий Zoom переполнена; сеанс остановлен."})
                    self.stop_event.set()
            self.loop.call_soon_threadsafe(deliver)

    def start(self, value, *, consent=False, billing_consent=False):
        if self.running:
            raise ZoomError("Сеанс уже запущен.")
        if not consent or not billing_consent:
            raise ZoomError("Подтвердите согласие участников и расход RTMS-кредитов для этого сеанса.")
        self.settings.validate()
        folder = Path(self.settings.export_dir).expanduser()
        if not folder.is_dir() or str(folder).startswith(("\\\\", "//")):
            raise ZoomError("Выберите существующую локальную папку для сеанса и экспорта.")
        self._active_id = meeting_id(value)
        self._cancel.clear()
        self.thread = threading.Thread(target=self._thread_main, name="zoom-session", daemon=False)
        self.thread.start()

    def _thread_main(self):
        try:
            asyncio.run(self._run())
        except Exception as error:
            # All lower-level safe errors omit content and credentials.
            if isinstance(error, (ZoomError, ValueError)):
                message = str(error)
            else:
                message = "Сеанс прерван. Проверьте модель, настройки Zoom и подключение. См. docs/ZOOM_SETUP.md."
            self._emit("error", {"message": message})
        finally:
            self.loop = None
            self.events_queue = None
            self._emit("finished", {})

    def _pipeline_event(self, kind, payload):
        self._emit(kind, payload)
        if kind in {"error", "inference_error", "store_error"}:
            self._failed = True
            self.stop()

    def _transport_event(self, kind, payload):
        if kind in {"participant_joined", "participant_left"}:
            self.pipeline.participant_event(str(payload["user_id"]), payload.get("user_name"),
                "joined" if kind.endswith("joined") else "left", self._source_epoch(payload.get("epoch", "0")), payload.get("timestamp_ms"))
        if kind == "reconnecting" and self.store:
            self._last_gap_time = time.time()
            try:
                recent = self.store.recent(1)
                marker = recent[-1].end if recent else 0.0
                self.store.gap("unknown", marker, marker, "transport_reconnect_unknown_duration", self._stream_epoch)
            except Exception:
                self._failed = True
                self.stop()
        if kind == "revoked":
            self.stop()
        self._emit(kind, payload)

    def _source_epoch(self, epoch):
        return epoch if epoch.startswith(self._stream_epoch + ":") else self._stream_epoch + ":" + epoch

    async def _run(self):
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        self.events_queue = asyncio.Queue(maxsize=64)
        self.client = None
        stream_task = events_task = None
        self._rtms_started = False
        self._failed = False
        self._emit("loading", {"message": "Проверка SHA256 и загрузка локальной модели…"})
        if self.settings.event_mode == "webhook":
            await asyncio.to_thread(self.ensure_receiver)
        engine = Transcriber(self.settings.model_dir)
        await asyncio.to_thread(engine.load)
        if self._cancel.is_set():
            return
        if self.store:
            self.store.close()
        name = "Zoom_" + datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid4().hex[:8] + ".sqlite"
        self.store = Store(Path(self.settings.export_dir) / name)
        self.store.set_metadata("meeting_id", self._active_id)
        self.store.set_metadata("created_at", datetime.now(timezone.utc).isoformat())
        self.store.set_metadata("transport", "Zoom RTMS audio-only PCM16 16kHz individual")
        self.store.set_metadata("shared_microphones", "A Zoom source may include multiple humans; names identify sources only.")
        self.pipeline = Pipeline(self.store, engine, self._pipeline_event)
        self.pipeline.start()
        self._emit("session_created", {"path": str(self.store.path)})
        try:
            if self.settings.event_mode == "websocket":
                ready = asyncio.Event()
                events_task = asyncio.create_task(websocket_events(self.settings.event_endpoint,
                    self.api.event_token, self._lifecycle_event, self._emit, self.stop_event, ready))
                await asyncio.wait_for(ready.wait(), 20)
            if self._cancel.is_set():
                return
            # Set before sending: a lost HTTP response can still mean Zoom started a paid stream.
            self._rtms_started = True
            await asyncio.to_thread(self.api.rtms, self._active_id, "start", billing_consent=True)
            self._emit("waiting_stream", {"message": "Zoom принял Start. Ожидание подписанного meeting.rtms_started…"})
            waiting_since = time.monotonic()
            while not self.stop_event.is_set() and not self._cancel.is_set():
                if events_task and events_task.done():
                    await events_task
                    raise ZoomError("Доставка событий Zoom остановлена.")
                if stream_task and stream_task.done():
                    await stream_task
                    break
                try:
                    doc = await asyncio.wait_for(self.events_queue.get(), .25)
                except asyncio.TimeoutError:
                    if not stream_task and time.monotonic() - waiting_since > 40:
                        raise ZoomError("RTMS-событие не пришло за 40 секунд. Проверьте HTTPS webhook, подпись и события Marketplace.")
                    continue
                event, payload = doc["event"], doc["payload"]
                if event == "app_deauthorized":
                    self.vault.clear_tokens()
                    raise ZoomError("Доступ приложения отозван в Zoom.")
                numeric_match = str(payload.get("meeting_id", "")) == self._active_id
                uuid_match = (self.store.get_metadata("meeting_uuid") is not None and
                              payload.get("meeting_uuid") == self.store.get_metadata("meeting_uuid"))
                if not numeric_match and not uuid_match:
                    continue
                if event == "meeting.rtms_started":
                    stream_id = payload.get("rtms_stream_id")
                    if not stream_id or not payload.get("meeting_uuid") or not isinstance(payload.get("server_urls"), str):
                        raise ZoomError("Неполное событие RTMS от Zoom.")
                    if self.client and self.client._stream_id == stream_id:
                        await self.client.reconnect(payload["server_urls"])
                        continue
                    if stream_task:
                        stream_task.cancel()
                        await asyncio.gather(stream_task, return_exceptions=True)
                        self.pipeline.flush()
                    def origin(timestamp):
                        if self.pipeline.origin_ms is None:
                            self.pipeline.set_origin(timestamp, basis="rtms_session")
                    self._stream_epoch = str(stream_id)
                    def audio(packet, prefix=str(stream_id)):
                        epoch = packet.epoch if packet.epoch.startswith(prefix + ":") else prefix + ":" + packet.epoch
                        return self.pipeline.feed(replace(packet, epoch=epoch))
                    self.client = RTMSClient(self.settings.client_id, self.vault.load()["client_secret"],
                        audio, self._transport_event, origin)
                    self.store.set_metadata("meeting_uuid", payload["meeting_uuid"])
                    stream_task = asyncio.create_task(self.client.run(payload["meeting_uuid"], stream_id, payload["server_urls"]))
                elif event == "meeting.rtms_stopped":
                    self._emit("zoom_stopped", {"message": "Zoom остановил передачу RTMS."})
                    break
                elif event == "meeting.rtms_interrupted":
                    self._emit("reconnecting", {"message": "Zoom прервал RTMS; возможен пропуск аудио."})
        finally:
            if self.client:
                try:
                    await asyncio.wait_for(self.client.stop(), 8)
                except Exception:
                    self._emit("stop_unconfirmed", {"message": "RTMS не подтвердил закрытие. Проверяется REST Stop…"})
            if self._rtms_started:
                try:
                    await asyncio.to_thread(self.api.rtms, self._active_id, "stop")
                    self._emit("zoom_stop_confirmed", {})
                except Exception:
                    self._emit("stop_unconfirmed", {"message": "Zoom не подтвердил Stop. Остановите приложение в Active Apps встречи и проверьте расход кредитов."})
            self.stop_event.set()
            for task in (events_task, stream_task):
                if task:
                    task.cancel()
            await asyncio.gather(*(t for t in (events_task, stream_task) if t), return_exceptions=True)
            self._emit("draining", {"message": "Аудиоприём завершён. Обработка принятой очереди…"})
            await asyncio.to_thread(self.pipeline.stop)
            try:
                self.store.set_metadata("metrics", self.pipeline.metrics)
            except Exception:
                self._failed = True
            if self._failed or self.pipeline.failure:
                self._emit("error", {"message": "Сеанс завершён с потерями обработки/сохранения. Проверьте журнал и свободное место; полнота текста не подтверждена."})
            else:
                self._emit("drained", {"message": "Принятая очередь обработана. Проверьте журнал пропусков перед экспортом."})
            self._rtms_started = False

    def stop(self):
        self._cancel.set()
        if self.loop and not self.loop.is_closed() and self.stop_event:
            self.loop.call_soon_threadsafe(self.stop_event.set)

    def close(self):
        self.stop()
        if self.thread:
            self.thread.join()
        if self.receiver:
            self.receiver.stop()
            self.receiver = None
        if self.store:
            self.store.close()
            self.store = None
