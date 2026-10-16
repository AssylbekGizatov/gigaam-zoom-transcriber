"""Bounded per-Zoom-source audio processing with one local inference worker.

Window ownership and overlap matching are adapted from the MIT upstream
gigaam-local-meeting-transcriber streaming processor. See NOTICE.md/LICENSE.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from functools import wraps
import math
import sqlite3
import threading
from time import monotonic, perf_counter
from uuid import uuid4

import numpy as np

from .models import AudioPacket, Segment, Word
from .store import Store

WINDOW_SECONDS = 8.0
OVERLAP_SECONDS = 1.0
PAUSE_SECONDS = 1.2
QUIET_RMS = 0.0001
TIMESTAMP_TOLERANCE = 0.002
MAX_ACTIVE_SOURCES = 128


def _storage_guard(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except (sqlite3.Error, OSError):
            self._storage_failure()
            return False
    return guarded


@dataclass
class _State:
    name: str
    rate: int = 0
    parts: list = field(default_factory=list)
    frames: int = 0
    start: float = 0.0
    own_start: float = 0.0
    previous_end: float | None = None
    quiet_seconds: float = 0.0
    dropped: bool = False
    last_received: float = field(default_factory=monotonic)
    recent: deque = field(default_factory=lambda: deque(maxlen=128))


@dataclass(frozen=True)
class _Window:
    key: tuple[str, str]
    name: str
    pcm: np.ndarray
    rate: int
    start: float
    own_start: float
    own_end: float
    received: float
    context: deque

    @property
    def duration(self):
        return len(self.pcm) / self.rate


@dataclass(frozen=True)
class _ContextWord:
    word: Word
    confirmed: bool


def _overlap_alignment(previous, current, overlap_start):
    """Match exact overlap tokens one-to-one with bounded timestamp drift.

    Repeated phrases outside retained PCM are never compared. A drift needs
    two different matched tokens, preventing broad text deduplication.
    """
    drift_limit, residual_limit = .4, .12
    prior = [entry for entry in previous if entry.word.end >= overlap_start - drift_limit
             and entry.word.start <= overlap_start + OVERLAP_SECONDS + drift_limit]
    prefix = [(index, word) for index, word in enumerate(current)
              if word.start <= overlap_start + OVERLAP_SECONDS + drift_limit]
    if len({entry.word.text.casefold() for entry in prior} & {word.text.casefold() for _, word in prefix}) < 2:
        return {}
    def center(word):
        return (word.start + word.end) / 2
    offsets = {round((center(word) - center(entry.word)) / .02) * .02
               for entry in prior for _, word in prefix
               if word.text.casefold() == entry.word.text.casefold()
               and abs(center(word) - center(entry.word)) <= drift_limit}
    best_score, best_pairs = 0.0, []
    for shift in offsets:
        rows = [[(0.0, []) for _ in range(len(prefix) + 1)] for _ in range(len(prior) + 1)]
        for i, entry in enumerate(prior, 1):
            for j, (index, word) in enumerate(prefix, 1):
                result = max(rows[i - 1][j], rows[i][j - 1], key=lambda cell: cell[0])
                residual = abs(center(word) - shift - center(entry.word))
                if word.text.casefold() == entry.word.text.casefold() and residual <= residual_limit:
                    intersection = min(word.end - shift, entry.word.end) - max(word.start - shift, entry.word.start)
                    longest = max(word.end - word.start, entry.word.end - entry.word.start)
                    if intersection > 0 and longest > 0:
                        score, pairs = rows[i - 1][j - 1]
                        candidate = (score + 2 + intersection / longest - residual * 3, pairs + [(index, entry)])
                        if candidate[0] > result[0]:
                            result = candidate
                rows[i][j] = result
        score, pairs = rows[-1][-1]
        score -= abs(shift) * .1
        if len(pairs) >= 2 and len({entry.word.text.casefold() for _, entry in pairs}) >= 2 and score > best_score:
            best_score, best_pairs = score, pairs
    return dict(best_pairs)


class Pipeline:
    def __init__(self, store: Store, transcriber, on_event=None, origin_ms: int | None = None,
                 *, max_pending_seconds: float = 48.0, idle_timeout_seconds: float = 1.2):
        if not math.isfinite(max_pending_seconds) or not 18 <= max_pending_seconds <= 48:
            raise ValueError("Лимит звука должен быть от 18 до 48 секунд.")
        if not .05 <= idle_timeout_seconds <= 30:
            raise ValueError("Некорректный таймаут тишины.")
        self.store, self.transcriber = store, transcriber
        self.on_event = on_event or (lambda kind, payload: None)
        self.origin_ms = origin_ms if origin_ms is not None else store.get_metadata("origin_ms")
        if self.origin_ms is not None:
            if not isinstance(self.origin_ms, int) or not 0 <= self.origin_ms <= 2**53:
                raise ValueError("Начало шкалы времени должно быть целым числом миллисекунд.")
            store.set_metadata("origin_ms", self.origin_ms)
            if store.get_metadata("clock_origin_kind") is None:
                store.set_metadata("clock_origin_kind", "explicit_meeting_start")
                store.set_metadata("time_basis", "meeting")
        self.max_pending_seconds = float(max_pending_seconds)
        self._idle_timeout = idle_timeout_seconds
        self._condition = threading.Condition(threading.RLock())
        self._states: dict[tuple[str, str], _State] = {}
        self._queue: deque[_Window] = deque()
        self._work_seconds = 0.0
        self._inflight_seconds = 0.0
        self.failure = None
        self._started = self._closing = self._closed = False
        self._thread = None
        self._stats = {"audio_seconds": 0.0, "inference_seconds": 0.0,
                       "peak_pending_seconds": 0.0, "dropped_seconds": 0.0,
                       "windows": 0, "segments": 0, "last_result_delay_seconds": 0.0}

    def _storage_failure(self):
        """Stop accepting PCM immediately when accepted text cannot be saved.

        Do not attempt to journal the failure to the same failed disk. The UI
        receives a safe fatal status, and metrics distinguish this from a drain.
        """
        with self._condition:
            if self.failure:
                return
            self.failure = "Не удалось записать журнал встречи. Проверьте свободное место и доступ к папке. Часть принятого аудио не сохранена."
            self._stats["failed"] = True
            self._stats["lost_seconds"] = self._pending_locked()
            self._closing = True
            self._queue.clear()
            self._work_seconds = self._inflight_seconds
            self._states.clear()
            self._condition.notify_all()
        self._emit("store_error", {"message": self.failure})

    def _emit(self, kind, payload):
        try:
            self.on_event(kind, payload)
        except Exception:
            pass  # A UI callback cannot kill capture/inference.

    def _pending_locked(self):
        return self._work_seconds + sum(s.frames / s.rate for s in self._states.values() if s.rate)

    @property
    def pending_seconds(self):
        with self._condition:
            return self._pending_locked()

    @property
    def metrics(self):
        with self._condition:
            stats = dict(self._stats)
            stats["pending_seconds"] = self._pending_locked()
            stats["active_sources"] = len(self._states)
            stats["real_time_factor"] = stats["inference_seconds"] / max(stats["audio_seconds"], 1e-9)
            return stats

    def start(self):
        with self._condition:
            if self._closed or self._closing:
                raise RuntimeError("Создайте новую сессию после остановки.")
            if self._started:
                return
            self._started = True
            self._thread = threading.Thread(target=self._run, name="gigaam-local-inference", daemon=True)
            self._thread.start()

    @_storage_guard
    def set_origin(self, timestamp_ms: int, basis: str = "meeting"):
        """Set the official RTMS clock origin before the first audio packet.

        Rebasing an active meeting would change already exported timestamps,
        so late or conflicting origins fail explicitly.
        """
        if not isinstance(timestamp_ms, int) or not 0 <= timestamp_ms <= 2**53:
            raise ValueError("Некорректное начало шкалы времени Zoom.")
        with self._condition:
            if self.origin_ms not in {None, timestamp_ms} and any(s.previous_end is not None for s in self._states.values()):
                raise ValueError("Нельзя менять начало времени после получения аудио.")
            self.origin_ms = timestamp_ms
            self.store.set_metadata("origin_ms", timestamp_ms)
            self.store.set_metadata("clock_origin_kind", "explicit_meeting_start" if basis == "meeting" else basis)
            self.store.set_metadata("time_basis", basis)
        self._emit("clock_origin", {"origin_ms": timestamp_ms, "kind": basis})

    @staticmethod
    def _display(user_id, name):
        return name if isinstance(name, str) and name.strip() else f"Участник Zoom #{user_id} (имя неизвестно)"

    def _retire_locked(self, key):
        state = self._states.pop(key)
        if state.previous_end is not None:
            self.store.set_source_cursor(key[1], key[0], state.previous_end)

    @_storage_guard
    def participant_event(self, user_id: str, name: str | None, event: str,
                          epoch: str = "0", timestamp_ms: int | None = None):
        if not isinstance(user_id, str) or not user_id or len(user_id) > 512 or len(epoch) > 512:
            raise ValueError("Некорректный идентификатор Zoom.")
        if name is not None and (not isinstance(name, str) or len(name) > 512):
            raise ValueError("Некорректное имя Zoom.")
        with self._condition:
            self.store.participant_event(user_id, name, event, epoch, timestamp_ms)
            key = (epoch, user_id)
            state = self._states.get(key)
            if state:
                display = self._display(user_id, name) if name is not None else state.name
                if display != state.name or event in {"leave", "left", "participant_left"}:
                    self._flush_locked(key, final=True)
                state.name = display
                if event in {"leave", "left", "participant_left"}:
                    self._retire_locked(key)
        self._emit("participant", {"user_id": user_id, "user_name": name, "event": event, "epoch": epoch})

    def _gap(self, key, start, end, reason):
        try:
            self.store.gap(key[1], max(0.0, start), max(0.0, start, end), reason, key[0])
        except (sqlite3.Error, OSError):
            self._storage_failure()
            return
        self._emit("gap", {"speaker_id": key[1], "source_epoch": key[0], "start": start, "end": end, "reason": reason})

    @_storage_guard
    def feed(self, packet: AudioPacket) -> bool:
        """Nonblocking with respect to inference; retain at most 48 seconds PCM.

        SQLite metadata writes are small synchronous local writes; the GUI and
        transport call this from their background workers, never the UI thread.
        """
        if (not isinstance(packet.user_id, str) or not packet.user_id or len(packet.user_id) > 512
                or not isinstance(packet.epoch, str) or len(packet.epoch) > 512
                or packet.user_name is not None and (not isinstance(packet.user_name, str) or len(packet.user_name) > 512)
                or not isinstance(packet.timestamp_ms, int) or not 0 <= packet.timestamp_ms <= 2**53
                or not isinstance(packet.sample_rate, int) or not 8000 <= packet.sample_rate <= 192000
                or not isinstance(packet.pcm, bytes) or len(packet.pcm) % 2
                or len(packet.pcm) > packet.sample_rate * 2 * WINDOW_SECONDS):
            self._emit("invalid_audio", {"message": "Некорректный PCM16, идентификатор или время Zoom."})
            return False
        if not packet.pcm:
            return True
        duration = len(packet.pcm) / 2 / packet.sample_rate
        pcm = np.frombuffer(packet.pcm, dtype="<i2").astype(np.float32) / np.float32(32768)
        key = (packet.epoch, packet.user_id)
        with self._condition:
            if not self._started or self._closing or self._closed:
                return False
            if self.origin_ms is None:
                self.origin_ms = packet.timestamp_ms
                self.store.set_metadata("origin_ms", self.origin_ms)
                self.store.set_metadata("clock_origin_kind", "first_received_audio")
                self.store.set_metadata("time_basis", "first_audio")
                self._emit("clock_origin", {"origin_ms": self.origin_ms, "kind": "first_received_audio",
                                           "message": "Время отсчитывается от первого полученного звука; начало встречи Zoom неизвестно."})
            start = (packet.timestamp_ms - self.origin_ms) / 1000
            end = start + duration
            if start < 0:
                self._emit("invalid_audio", {"message": "Время звука раньше начала шкалы встречи."})
                return False
            now = monotonic()
            for old_key, old in list(self._states.items()):
                if not old.frames and now - old.last_received > 60:
                    self._retire_locked(old_key)
            state = self._states.get(key)
            if state is None:
                if len(self._states) >= MAX_ACTIVE_SOURCES:
                    self._gap(key, start, end, "active_source_limit")
                    return False
                name = packet.user_name or self.store.participant_name(packet.user_id, packet.epoch)
                state = _State(self._display(packet.user_id, name), previous_end=self.store.source_cursor(packet.user_id, packet.epoch))
                self._states[key] = state
            if state.previous_end is not None and start < state.previous_end - TIMESTAMP_TOLERANCE:
                self._emit("capture_timestamp", {"speaker_id": packet.user_id, "source_epoch": packet.epoch,
                                                  "message": "Повторный или устаревший пакет пропущен."})
                return False
            if packet.user_name is not None and self._display(packet.user_id, packet.user_name) != state.name:
                self._flush_locked(key, final=True)
                state.name = self._display(packet.user_id, packet.user_name)
                self.store.participant_event(packet.user_id, packet.user_name, "rename", packet.epoch, packet.timestamp_ms)
            discontinuity = (state.dropped or state.rate and state.rate != packet.sample_rate
                             or state.previous_end is not None and start > state.previous_end + TIMESTAMP_TOLERANCE)
            if discontinuity:
                self._flush_locked(key, final=True)
                if state.previous_end is not None and start > state.previous_end + TIMESTAMP_TOLERANCE:
                    self._gap(key, state.previous_end, start, "transport_gap")
                    if self.failure:
                        return False
            state.previous_end, state.rate = end, packet.sample_rate
            state.last_received = now
            if not state.frames and not np.any(pcm):
                state.dropped = False
                return True
            window_frames = round(WINDOW_SECONDS * state.rate)
            step_frames = round((WINDOW_SECONDS - OVERLAP_SECONDS) * state.rate)
            prospective = state.frames + len(pcm)
            seams = 0 if prospective < window_frames else 1 + (prospective - window_frames) // step_frames
            if self._pending_locked() + duration + seams * OVERLAP_SECONDS > self.max_pending_seconds + 1e-7:
                state.dropped = True
                self._stats["dropped_seconds"] += duration
                self._gap(key, start, end, "queue_overload")
                self._emit("overload", {"message": "Локальная модель не успевает: новые пакеты пропущены и отмечены в журнале.",
                                        "pending_seconds": self._pending_locked()})
                return False
            state.dropped = False
            offset = 0
            # 20-ms silence decisions work equally for small RTMS packets and
            # an offline 8-second fixture packet; no volume gate drops speech.
            while offset < len(pcm):
                size = min(round(.02 * state.rate), window_frames - state.frames, len(pcm) - offset)
                piece = pcm[offset:offset + size]
                if not state.frames and not np.any(piece):
                    offset += size
                    continue
                if not state.frames:
                    state.start = state.own_start = start + offset / state.rate
                state.parts.append(piece.copy())
                state.frames += size
                offset += size
                rms = float(np.sqrt(np.mean(piece.astype(np.float64) ** 2)))
                state.quiet_seconds = state.quiet_seconds + size / state.rate if rms < QUIET_RMS else 0.0
                if state.frames >= window_frames:
                    self._flush_locked(key, final=False)
                elif state.quiet_seconds >= PAUSE_SECONDS:
                    self._flush_locked(key, final=True)
            self._stats["peak_pending_seconds"] = max(self._stats["peak_pending_seconds"], self._pending_locked())
            return True

    def _flush_locked(self, key, *, final):
        state = self._states[key]
        if not state.frames:
            return
        pcm = np.concatenate(state.parts)
        end = state.start + len(pcm) / state.rate
        own_end = end if final else end - OVERLAP_SECONDS / 2
        if own_end > state.own_start + 1e-8:
            self._queue.append(_Window(key, state.name, pcm, state.rate, state.start,
                                       state.own_start, own_end, state.last_received, state.recent))
            self._work_seconds += len(pcm) / state.rate
            self._condition.notify()
        if final:
            state.parts, state.frames, state.quiet_seconds = [], 0, 0.0
        else:
            retained = pcm[-round(OVERLAP_SECONDS * state.rate):].copy()
            state.parts, state.frames = [retained], len(retained)
            state.start = end - len(retained) / state.rate
            state.own_start = own_end

    def flush(self):
        with self._condition:
            for key in self._states:
                self._flush_locked(key, final=True)

    def stop(self):
        """Drain accepted audio. Call from a background thread; wait for model."""
        with self._condition:
            if self._closed:
                return
            self._closing = True
            self.flush()
            self._condition.notify_all()
            thread = self._thread
        if thread is threading.current_thread():
            raise RuntimeError("Остановка из callback распознавания не поддерживается.")
        if thread is not None:
            thread.join()
        with self._condition:
            try:
                for key in list(self._states):
                    self._retire_locked(key)
            except (sqlite3.Error, OSError):
                self._storage_failure()
            self._closed = True
        self._emit("stopped", self.metrics)

    def _run(self):
        while True:
            with self._condition:
                now = monotonic()
                for key, state in self._states.items():
                    if state.frames and now - state.last_received >= self._idle_timeout:
                        self._flush_locked(key, final=True)
                if not self._queue:
                    if self._closing:
                        return
                    self._condition.wait(timeout=min(.2, self._idle_timeout))
                    continue
                window = self._queue.popleft()
                self._inflight_seconds = window.duration
            started = perf_counter()
            try:
                words = self.transcriber.transcribe_words(window.pcm, window.rate)
                self._publish(window, words)
            except (sqlite3.Error, OSError):
                self._storage_failure()
            except Exception as error:
                self._gap(window.key, window.own_start, window.own_end, "inference_failed")
                self._emit("inference_failed", {"message": "Не удалось распознать фрагмент. Следующие фрагменты обрабатываются.",
                                                 "error_type": type(error).__name__})
            finally:
                elapsed = perf_counter() - started
                with self._condition:
                    self._work_seconds = max(0.0, self._work_seconds - window.duration)
                    self._inflight_seconds = 0.0
                    self._stats["audio_seconds"] += window.duration
                    self._stats["inference_seconds"] += elapsed
                    self._stats["windows"] += 1
                    self._stats["last_result_delay_seconds"] = max(0, monotonic() - window.received)
                self._emit("progress", self.metrics)

    def _publish(self, window, words):
        selected, raw = [], []
        recent = window.context
        prior = list(recent) if window.own_start > window.start + 1e-6 else []
        previous_start = -1.0
        for word in words:
            if (not isinstance(word.text, str) or not math.isfinite(word.start) or not math.isfinite(word.end)
                    or word.start < previous_start or word.start < 0 or word.end <= word.start
                    or word.end > window.duration + .1):
                raise ValueError("invalid_model_word")
            previous_start = word.start
            if word.text.strip():
                raw.append(Word(window.start + word.start, window.start + word.end, word.text.strip()))
        aligned = _overlap_alignment(prior, raw, window.start)
        context = []
        for index, word in enumerate(raw):
            previous = aligned.get(index)
            if previous is None:
                candidates = []
                for entry in prior:
                    earlier = entry.word
                    intersection = min(word.end, earlier.end) - max(word.start, earlier.start)
                    longest = max(word.end - word.start, earlier.end - earlier.start)
                    if earlier.text.casefold() == word.text.casefold() and intersection > longest * .65:
                        candidates.append((intersection / longest, entry))
                if candidates:
                    previous = max(candidates, key=lambda item: item[0])[1]
            confirmed = bool(previous and previous.confirmed)
            owns = window.own_start <= (word.start + word.end) / 2 < window.own_end
            if owns and not confirmed:
                start, end = max(word.start, window.own_start), min(word.end, window.own_end)
                if end > start:
                    selected.append(Word(start, end, word.text))
            context.append(_ContextWord(word, confirmed or owns))
        if selected:
            segment = Segment(uuid4().hex, window.key[1], window.name,
                              selected[0].start, max(w.end for w in selected),
                              " ".join(w.text for w in selected), "final", window.key[0])
            self.store.upsert(segment)
            with self._condition:
                self._stats["segments"] += 1
            self._emit("segment", segment)
        recent.clear()
        recent.extend(context)
