"""Resolve names against OCR and audible intervals on the capture clock."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import threading
from uuid import uuid4

import numpy as np

from .app_log import log_event
from .models import Segment, Word
from .speaker_observer import monotonic_ms


@dataclass(frozen=True, slots=True)
class AttributionIdentity:
    source_id: str
    display_name: str
    attribution: str


UNKNOWN = AttributionIdentity("local:remote", "Неизвестно", "unattributed_mix")


class Timeline:
    """Require matching OCR before AND after a word within the same audible turn.

    Signals are bounded to cover the pipeline backlog. The amplitude gate can
    rule out silence, but cannot count voices within one mixed output device.
    """
    RETENTION_MS = 120_000
    MAX_GAP_MS = 2000
    QUIET_MS = 240

    def __init__(self):
        self._entries = deque(maxlen=512)
        self._activity = {source: deque() for source in ("remote", "microphone")}
        self._lock = threading.Lock()

    def add(self, observation):
        at = observation.observed_at_ms
        if type(at) is not int or at < 0:
            raise ValueError("Некорректное время наблюдения.")
        identity = UNKNOWN
        source = observation.source_id
        if (observation.evidence == "ocr_speaker_view_hint" and isinstance(source, str)
                and source.startswith("local:zoom-ocr:") and source.removeprefix("local:zoom-ocr:") not in {"", "unknown"}
                and isinstance(observation.display_name, str) and observation.display_name.strip()):
            identity = AttributionIdentity(source, observation.display_name.strip(), "zoom_ocr_hint")
        with self._lock:
            if self._entries and self._entries[-1][0] > at:
                return
            if self._entries and self._entries[-1][0] == at:
                if self._entries.pop()[1] != identity:
                    identity = UNKNOWN
            self._entries.append((at, identity))
            while self._entries[0][0] < at - self.RETENTION_MS:
                self._entries.popleft()

    def add_audio(self, source, timestamp_ms, samples, rate):
        """Record only times of non-quiet PCM blocks. No audio is retained."""
        block = max(1, rate // 50)
        spans = []
        for offset in range(0, len(samples), block):
            chunk = samples[offset:offset + block].astype(np.float32)
            if np.mean(chunk * chunk) >= 64 ** 2:  # approx -54 dBFS, PCM16
                spans.append((timestamp_ms + offset * 1000 / rate,
                              timestamp_ms + (offset + len(chunk)) * 1000 / rate))
        with self._lock:
            history = self._activity[source]
            for start, end in spans:
                if history and start <= history[-1][1] + self.QUIET_MS:
                    history[-1] = (history[-1][0], max(end, history[-1][1]))
                else:
                    history.append((start, end))
            while history and history[0][1] < timestamp_ms - self.RETENTION_MS:
                history.popleft()

    def remote_active_now(self):
        with self._lock:
            spans = self._activity["remote"]
            return bool(spans and monotonic_ms() - spans[-1][1] <= self.QUIET_MS)

    def resolve(self, start_ms, end_ms):
        if not (math.isfinite(start_ms) and math.isfinite(end_ms) and 0 <= start_ms < end_ms):
            raise ValueError("Некорректный интервал слова.")
        with self._lock:
            span = next(((a, b) for a, b in reversed(self._activity["remote"])
                         if a <= start_ms and end_ms <= b), None)
            if span is None or any(a < end_ms and b > start_ms for a, b in self._activity["microphone"]):
                return UNKNOWN
            readings = [(t, identity) for t, identity in self._entries if span[0] <= t <= span[1]]
        # Only interpolate between actual observations. A missing side, a
        # conflicting reading, or a long interval between samples is uncertain.
        before = [i for i, (t, _) in enumerate(readings) if t <= start_ms]
        after = next((i for i, (t, _) in enumerate(readings) if t >= end_ms), None)
        if not before or after is None:
            return UNKNOWN
        bracket = readings[before[-1]:after + 1]
        identity = bracket[0][1]
        if any(item[1] != identity for item in bracket):
            return UNKNOWN
        if any(b[0] - a[0] > self.MAX_GAP_MS for a, b in zip(bracket, bracket[1:])):
            return UNKNOWN
        return identity


class DesktopSegmenter:
    def __init__(self, timeline: Timeline, local_name="Вы"):
        if not isinstance(local_name, str) or not 0 < len(local_name.strip()) <= 512:
            raise ValueError("Укажите имя для микрофона.")
        self.timeline = timeline
        self.local_identity = AttributionIdentity("local:microphone", local_name.strip(), "local_microphone")

    def __call__(self, source_id, epoch, default_name, selected_words: list[Word], origin_ms):
        if type(origin_ms) is not int or origin_ms < 0:
            raise ValueError("Некорректное начало сеанса.")
        groups = []
        previous_start = previous_end = -1
        for index, word in enumerate(selected_words):
            if (not math.isfinite(word.start) or not math.isfinite(word.end) or
                    not 0 <= word.start < word.end or word.start < previous_start):
                raise ValueError("Некорректный интервал слова.")
            previous_start = word.start
            if not word.text.strip():
                continue
            identity = UNKNOWN
            overlap = word.start < previous_end or (index + 1 < len(selected_words)
                                                    and selected_words[index + 1].start < word.end)
            if source_id == "local:microphone":
                identity = self.local_identity
            elif source_id == "local:remote" and not overlap:
                identity = self.timeline.resolve(origin_ms + word.start * 1000, origin_ms + word.end * 1000)
            if not groups or groups[-1][0] != identity or word.start - previous_end > self.timeline.QUIET_MS / 1000:
                groups.append((identity, []))
            groups[-1][1].append(word)
            previous_end = max(previous_end, word.end)
        segments = []
        for identity, words in groups:
            log_event("attribution_decision", decision=identity.attribution)
            segments.append(Segment(id=str(uuid4()), speaker_id=identity.source_id, display_name=identity.display_name,
                                    start=words[0].start, end=max(w.end for w in words),
                                    text=" ".join(w.text.strip() for w in words), status="final",
                                    source_epoch=epoch, attribution=identity.attribution))
        return segments
