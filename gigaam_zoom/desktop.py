"""Offline desktop lifecycle. This module never imports a Zoom/API client."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

from .config import data_dir, local_directory
from .engine import Transcriber, EngineError
from .models import AudioPacket
from .pipeline import Pipeline
from .store import Store


@dataclass
class DesktopSettings:
    model_dir: str = "models/gigaam"
    export_dir: str = ""
    local_name: str = "Вы"
    use_microphone: bool = False
    read_zoom_names: bool = True
    use_diarization: bool = False
    diarization_model: str = "models/diarization/campplus.onnx"
    zoom_name_mode: str = "uia"
    zoom_ocr_region: str = ""
    zoom_ocr_language: str = "ru-RU"
    zoom_ocr_client_size: str = ""

    def visual_config(self):
        from .zoom_visual import VisualSpeakerConfig
        try:
            region = tuple(float(part.strip()) for part in self.zoom_ocr_region.split(","))
            size = (tuple(int(part.strip()) for part in self.zoom_ocr_client_size.split(","))
                    if self.zoom_ocr_client_size else None)
        except (ValueError, AttributeError):
            raise ValueError("Выберите область имени в окне Zoom для OCR.") from None
        config = VisualSpeakerConfig(region=region, language=self.zoom_ocr_language, client_size=size)
        config.validate()
        return config

    def validate(self):
        if not self.model_dir.strip() or not Path(self.model_dir).is_dir():
            raise ValueError("Выберите существующую папку модели GigaAM.")
        local_directory(self.model_dir)
        if not self.export_dir.strip():
            raise ValueError("Выберите папку для текста и экспорта.")
        local_directory(self.export_dir)
        if not self.local_name.strip() or len(self.local_name) > 150:
            raise ValueError("Имя должно содержать от 1 до 150 символов.")
        if self.zoom_name_mode not in {"uia", "ocr"}:
            raise ValueError("Выберите источник имён Zoom: UIA или OCR.")
        if self.read_zoom_names and self.zoom_name_mode == "ocr":
            self.visual_config()

    def save(self, path=None):
        self.validate()
        target = Path(path) if path else data_dir() / "desktop-settings.json"
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(target)

    @classmethod
    def load(cls, path=None):
        target = Path(path) if path else data_dir() / "desktop-settings.json"
        if not target.exists():
            # Migrate only two public paths, never OAuth/RTMS credentials.
            from .config import Settings
            try:
                old = Settings.load()
                return cls(model_dir=old.model_dir, export_dir=old.export_dir)
            except (ValueError, OSError, TypeError):
                return cls()
        values = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(values, dict) or set(values) - set(cls.__dataclass_fields__):
            raise ValueError("Недопустимые поля настроек локального режима.")
        for key, value in values.items():
            if key in {"use_microphone", "read_zoom_names", "use_diarization"}:
                if type(value) is not bool:
                    raise ValueError("Некорректный переключатель в настройках.")
            elif not isinstance(value, str):
                raise ValueError("Некорректный путь или имя в настройках.")
        return cls(**values)


class DesktopController:
    def __init__(self, on_event, *, engine_factory=Transcriber, capture_factory=None, observer_factory=None):
        self.on_event = on_event
        self.engine_factory, self.capture_factory, self.observer_factory = engine_factory, capture_factory, observer_factory
        self.thread = None
        self.store = self.pipeline = self.capture = self.observer = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._levels = {"remote": 0.0, "microphone": 0.0}
        self.failure = None
        self.partial = False

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def emit(self, kind, payload=None):
        self.on_event(kind, payload if payload is not None else {})

    def start(self, settings, loopback_device=None, microphone_device=None):
        with self._lock:
            if self.running:
                raise ValueError("Сеанс уже запущен.")
            settings.validate()
            if settings.use_microphone and microphone_device is None:
                raise ValueError("Выберите микрофон или отключите его захват.")
            if self.store is not None:
                self.store.close()
            self.store = None
            self.pipeline = None
            self.failure = None
            self.partial = False
            self._cancel.clear()
            self.thread = threading.Thread(target=self._run, args=(settings, loopback_device, microphone_device),
                                           name="desktop-session", daemon=False)
            self.thread.start()

    def stop(self):
        self._cancel.set()

    def _capture_event(self, kind, payload):
        self.emit(kind, payload)
        if kind == "capture_gap":
            self.partial = True
        if kind in {"error", "capture_error", "device_lost"}:
            self.failure = payload.get("message", "Источник звука недоступен.")
            self._cancel.set()

    def _run(self, settings, loopback_device, microphone_device):
        engine = None
        try:
            from .desktop_audio import DesktopCapture
            from .desktop_attribution import Timeline, DesktopSegmenter
            from .zoom_accessibility import ZoomSpeakerObserver
            self.emit("status", {"message": "Загрузка локальной модели… звук пока не захватывается."})
            engine = self.engine_factory(settings.model_dir)
            engine.load()
            diarization = None
            if settings.use_diarization:
                from .diarization import DiarizationTimeline, OnlineDiarizer, SherpaSpeakerEmbedder
                self.emit("status", {"message": "Загрузка локальной модели различения голосов…"})
                embedder = SherpaSpeakerEmbedder(settings.diarization_model)
                embedder.load()
                diarization = DiarizationTimeline(OnlineDiarizer(embedder))
            if self._cancel.is_set():
                return
            folder = local_directory(settings.export_dir)
            self.store = Store(folder / ("zoom-local-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8] + ".sqlite"))
            origin = round(time.monotonic() * 1000)
            for key, value in {
                "capture_mode": "desktop_loopback", "time_basis": "local_capture",
                "clock_origin_kind": "monotonic_capture_start", "origin_ms": origin,
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "audio_saved": False, "video_saved": False, "cloud_stt": False,
                "speaker_ids": "local_only_not_zoom_ids", "microphone_enabled": settings.use_microphone,
                "voice_clustering_enabled": settings.use_diarization,
                "zoom_name_mode": settings.zoom_name_mode if settings.read_zoom_names else "disabled",
                "attribution_note": "UI active-speaker hints are uncertain; overlapping voices cannot be separated.",
            }.items():
                self.store.set_metadata(key, value)
            # OCR polls every 750 ms. Cover the unobserved interval preceding
            # a detected switch; Zoom's own display delay remains uncertain.
            timeline = Timeline(guard_ms=1000 if settings.zoom_name_mode == "ocr" else 300)
            segmenter = DesktopSegmenter(timeline, local_name=settings.local_name, diarization=diarization)
            self.pipeline = Pipeline(self.store, engine, self._pipeline_event, origin_ms=origin,
                                     max_pending_seconds=24, segmenter=segmenter,
                                     window_analyzer=diarization.analyze_window if diarization else None)
            self.pipeline.start()
            epoch = "desktop:" + uuid4().hex

            def audio(source, pcm, timestamp_ms, rate):
                import numpy as np
                values = np.frombuffer(pcm, dtype="<i2")
                self._levels[source] = min(1.0, float(np.max(np.abs(values.astype(np.float32)))) / 32768) if len(values) else 0.0
                name = settings.local_name if source == "microphone" else "Удалённые участники"
                self.pipeline.feed(AudioPacket("local:" + source, name, timestamp_ms, pcm, rate, epoch))

            if settings.read_zoom_names:
                if self.observer_factory:
                    self.observer = self.observer_factory()
                else:
                    self.observer = ZoomSpeakerObserver(
                        visual_config=settings.visual_config() if settings.zoom_name_mode == "ocr" else None)
                self.observer.start()
            self.capture = (self.capture_factory or DesktopCapture)(
                loopback_device=loopback_device,
                microphone_device=microphone_device if settings.use_microphone else None,
                on_audio=audio, on_event=self._capture_event)
            self.capture.start()
            self.emit("ready", {"message": "Идёт локальная транскрипция. Запись Zoom не нужна.", "path": str(self.store.path)})
            last_observation = None
            while not self._cancel.wait(.2):
                if self.observer:
                    observation = self.observer.latest()
                    if observation != last_observation:
                        timeline.add(observation)
                        last_observation = observation
                    self.emit("speaker", {"name": observation.display_name, "evidence": observation.evidence})
                self.emit("levels", dict(self._levels))
                if self.pipeline.failure:
                    self.failure = self.pipeline.failure
                    break
                if not self.capture.running:
                    self.failure = self.failure or "Захват звука прекратился. Проверьте выбранное устройство и запустите новый сеанс."
                    break
        except Exception as error:
            self.failure = (str(error) if isinstance(error, (ValueError, EngineError, OSError, RuntimeError))
                            else f"Не удалось запустить локальный режим ({type(error).__name__}).")
            self.emit("error", {"message": self.failure})
        finally:
            self.emit("status", {"message": "Остановка захвата и завершение принятого текста…"})
            for component in (self.capture, self.observer):
                if component:
                    try:
                        component.stop()
                    except Exception:
                        self.failure = self.failure or "Не удалось корректно закрыть источник звука."
            try:
                if self.pipeline:
                    self.pipeline.stop()
                    capture_metrics = self.capture.metrics if self.capture else {}
                    self.partial = self.partial or any(capture_metrics.get(key, 0) > 0 for key in
                                                      ("dropped_seconds", "hardware_overflows", "clock_discontinuities"))
                    if self.store:
                        self.store.set_metadata("pipeline_metrics", self.pipeline.metrics)
                        self.store.set_metadata("capture_metrics", capture_metrics)
                        self.store.set_metadata("finished_at_utc", datetime.now(timezone.utc).isoformat())
                        self.store.set_metadata("session_error", self.failure)
                        self.store.set_metadata("partial_transcript", self.partial or bool(self.failure))
            except Exception:
                self.failure = self.failure or "Ошибка завершения или записи журнала. Проверьте сохранённые реплики."
            finally:
                if self.pipeline:
                    self.pipeline.transcriber = None
                    self.pipeline.segmenter = self.pipeline.window_analyzer = None
            self.capture = self.observer = None
            engine = None
            message = ("Запуск отменён до захвата звука." if self.store is None else
                       "Сеанс завершён с пропусками. Сохранённый текст доступен экспорту." if self.partial else
                       "Сеанс завершён. Полученный текст сохранён локально; доступен экспорт.")
            self.emit("finished", {"message": self.failure or message,
                                   "failed": bool(self.failure), "partial": self.partial})

    def _pipeline_event(self, kind, payload):
        if kind in {"overload", "inference_failed", "gap"}:
            self.partial = True
        if kind == "store_error":
            self.failure = payload.get("message", "Ошибка сохранения текста.")
            self._cancel.set()
        self.emit(kind, payload)
