"""Observe local OCR in a disposable process, independently of audio capture."""
from __future__ import annotations

from dataclasses import dataclass
import multiprocessing as mp
import threading
import time
import uuid

from .app_log import log_ocr_frame, log_exception


def monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


@dataclass(frozen=True, slots=True)
class SpeakerObservation:
    observed_at_ms: int
    source_id: str
    display_name: str | None
    evidence: str


def unknown(reason: str, now: int | None = None) -> SpeakerObservation:
    return SpeakerObservation(monotonic_ms() if now is None else now, "local:zoom-ocr:unknown", None, reason)


class ZoomSpeakerObserver:
    """GUI reads never wait on a native OCR call. Hung providers are restarted."""

    def __init__(self, *, visual_config, poll_interval=.75, probe_timeout=15.0,
                 verbose=False, log_samples=True, _worker=None):
        from .zoom_visual import _visual_worker
        visual_config.validate()
        self.config = visual_config
        self.poll_interval = poll_interval
        self.probe_timeout = probe_timeout
        self._worker = _worker or _visual_worker
        self._verbose = verbose
        self._log = log_samples
        self._session_id = uuid.uuid4().hex
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._frame = None
        self._observation = unknown("ocr_not_started")

    def latest_frame(self):
        with self._lock:
            return self._frame

    def latest(self, now_ms=None):
        now = monotonic_ms() if now_ms is None else now_ms
        with self._lock:
            obs, frame = self._observation, self._frame
        if obs.display_name and not 0 <= now - obs.observed_at_ms <= frame.max_age_ms:
            return unknown("ocr_stale", now)
        return obs

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="zoom-speaker", daemon=True)
            self._thread.start()

    def stop(self, timeout=2.0):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        self._set(None, unknown("ocr_stopped"))

    def _set(self, frame, observation):
        with self._lock:
            self._frame, self._observation = frame, observation

    def _run(self):
        ctx = mp.get_context("spawn")
        while not self._stop.is_set():
            connection, child = ctx.Pipe()
            process = ctx.Process(target=self._worker,
                                  args=(child, self._session_id, self.config, self._verbose), daemon=True)
            try:
                process.start()
                child.close()
                while not self._stop.is_set():
                    start = time.monotonic()
                    connection.send("sample")
                    while not connection.poll(.05):
                        if self._stop.is_set():
                            return
                        if time.monotonic() - start > self.probe_timeout:
                            raise TimeoutError
                    frame = connection.recv()
                    self._set(frame, frame.observation)
                    if self._log:
                        log_ocr_frame(frame)
                    self._stop.wait(max(0, self.poll_interval - (time.monotonic() - start)))
            except Exception as error:
                reason = "ocr_timeout" if isinstance(error, TimeoutError) else "ocr_unavailable"
                self._set(None, unknown(reason))
                if self._log:
                    log_exception(reason, error)
            finally:
                child.close()
                connection.close()
                if process.pid is not None:
                    process.join(.15)
                    if process.is_alive():
                        process.terminate()
                        process.join(.5)
                    if process.is_alive():
                        process.kill()
                        process.join(.5)
                if not process.is_alive():
                    process.close()
            self._stop.wait(.5)
