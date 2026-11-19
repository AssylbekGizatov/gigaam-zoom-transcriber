"""Observe local OCR in a disposable process, independently of audio capture."""
from __future__ import annotations

from dataclasses import dataclass
import multiprocessing as mp
from multiprocessing.connection import wait as wait_connections
import math
import threading
import time
import uuid

from .app_log import log_ocr_frame, log_exception, log_event


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


class _WorkerExited(Exception):
    """The OS process sentinel fired, independently of pipe readability."""


class ZoomSpeakerObserver:
    """GUI reads never wait on a native OCR call. Hung providers are restarted."""

    def __init__(self, *, visual_config, poll_interval=.75, probe_timeout=15.0,
                 verbose=False, log_samples=True, retry_initial_delay=1.0,
                 retry_max_delay=30.0, _worker=None):
        from .zoom_visual import _visual_worker
        visual_config.validate()
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) or value <= 0
               for value in (poll_interval, probe_timeout, retry_initial_delay, retry_max_delay)):
            raise ValueError("OCR polling, timeout and retry delays must be positive finite numbers")
        if retry_max_delay < retry_initial_delay:
            raise ValueError("Maximum retry delay cannot be shorter than the initial delay")
        self.config = visual_config
        self.poll_interval = poll_interval
        self.probe_timeout = probe_timeout
        self.retry_initial_delay = retry_initial_delay
        self.retry_max_delay = retry_max_delay
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
            self._set(None, unknown("ocr_not_started"))
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

    def _publish(self, frame, observation):
        with self._lock:
            if not self._stop.is_set():
                self._frame, self._observation = frame, observation

    def _retry_delay(self, failures):
        return min(self.retry_max_delay, self.retry_initial_delay * 2 ** min(max(0, failures - 1), 16))

    def _receive_frame(self, process, connection, deadline):
        """Watch process death even if EOF and the Windows exit code race."""
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            ready = wait_connections((connection, process.sentinel), timeout=max(0, min(.05, remaining)))
            # Process death takes priority over a buffered result. A name from
            # a dead worker must not remain current while it is restarted.
            if process.sentinel in ready or process.exitcode is not None:
                raise _WorkerExited
            if connection in ready:
                frame = connection.recv()
                if process.exitcode is not None:
                    raise _WorkerExited
                return frame
            if remaining <= 0:
                raise TimeoutError
        return None

    def _wait_between_samples(self, process, duration):
        deadline = time.monotonic() + duration
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if wait_connections((process.sentinel,), timeout=min(.05, remaining)):
                raise _WorkerExited

    def _unexpected_exit_code(self, process, error):
        """Observe the native status BEFORE any intentional terminate/kill.

        A pipe can close slightly before Windows publishes the process exit
        status. Give that transition an interruptible grace period. A live
        process with a broken protocol is unavailable, not an invented crash.
        """
        if process.pid is None:
            return None
        if isinstance(error, (_WorkerExited, EOFError, BrokenPipeError, OSError)):
            deadline = time.monotonic() + 1.0
            while process.exitcode is None and not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if wait_connections((process.sentinel,), timeout=min(.05, remaining)):
                    break
        process.join(timeout=0)
        return process.exitcode

    @staticmethod
    def _dispose(process, connection, child):
        child.close()
        try:
            if process.pid is not None:
                # Normal stop lets an idle worker balance COM initialization.
                # A blocked native call is still forcibly bounded below.
                if process.is_alive():
                    try:
                        connection.send("stop")
                    except (EOFError, BrokenPipeError, OSError):
                        pass
                process.join(.15)
                if process.is_alive():
                    process.terminate()
                    process.join(.5)
                if process.is_alive():
                    process.kill()
                    process.join(.5)
        finally:
            connection.close()
            if not process.is_alive():
                process.close()

    def _run(self):
        ctx = mp.get_context("spawn")
        failures = 0
        while not self._stop.is_set():
            connection, child = ctx.Pipe()
            process = ctx.Process(target=self._worker,
                                  args=(child, self._session_id, self.config, self._verbose), daemon=True)
            spawned_at = time.monotonic()
            frames_received = 0
            retry_delay = self.retry_initial_delay
            try:
                process.start()
                child.close()
                while not self._stop.is_set():
                    start = time.monotonic()
                    connection.send("sample")
                    frame = self._receive_frame(process, connection, start + self.probe_timeout)
                    if self._stop.is_set():
                        break
                    from .zoom_visual import VisualFrame
                    if not isinstance(frame, VisualFrame) or not isinstance(frame.observation, SpeakerObservation):
                        raise ValueError("Invalid OCR worker response")
                    self._publish(frame, frame.observation)
                    frames_received += 1
                    # One good frame followed by a native crash must not reset
                    # backoff and cause an endless one-process-per-second loop.
                    if frames_received >= 3 and time.monotonic() - spawned_at >= 30:
                        failures = 0
                    if self._log:
                        log_ocr_frame(frame)
                    self._wait_between_samples(process, max(0, self.poll_interval - (time.monotonic() - start)))
            except Exception as error:
                if not self._stop.is_set():
                    # Clear a previous name as soon as the channel fails; do
                    # not keep it visible during the exit-status grace period.
                    self._publish(None, unknown("ocr_timeout" if isinstance(error, TimeoutError)
                                                else "ocr_unavailable"))
                    # Timeout is an intentional watchdog termination. Never
                    # misreport the ensuing terminate() exit status as a crash.
                    exit_code = None if isinstance(error, TimeoutError) else self._unexpected_exit_code(process, error)
                    if process.pid is None:
                        reason = "ocr_worker_start_failed"
                    elif isinstance(error, TimeoutError):
                        reason = "ocr_timeout"
                    elif exit_code is not None:
                        reason = "ocr_worker_crashed" if exit_code != 0 else "ocr_worker_exited"
                    else:
                        reason = "ocr_unavailable"
                    failures += 1
                    retry_delay = self._retry_delay(failures)
                    self._publish(None, unknown(reason))
                    # log_samples controls OCR content/telemetry only. Safe
                    # infrastructure failures must also reach preview logs.
                    if not self._stop.is_set():
                        if exit_code is not None:
                            log_event(reason, exit_code=exit_code, retry_count=failures,
                                      retry_delay_ms=round(retry_delay * 1000))
                        else:
                            log_exception(reason, error)
            finally:
                self._dispose(process, connection, child)
            self._stop.wait(retry_delay)
