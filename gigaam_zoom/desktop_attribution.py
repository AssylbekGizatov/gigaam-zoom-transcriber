"""Conservative attribution of a mixed desktop stream using observed UI hints.

UI observations are not Zoom participant/audio IDs. A word gets a display name
only when fresh, unambiguous observations agree throughout its padded interval.
The audio and observer must share a monotonic clock; ``origin_ms`` converts the
pipeline's relative word timestamps back to that clock.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import re
import threading
from typing import Protocol
from uuid import uuid4

from .models import Segment, Word


class Observation(Protocol):
    observed_at_ms: int
    source_id: str
    display_name: str | None
    evidence: str


@dataclass(frozen=True, slots=True)
class AttributionIdentity:
    source_id: str
    display_name: str
    attribution: str


UNKNOWN = AttributionIdentity(
    "local:remote", "Удалённый участник (не определён)", "unattributed_mix"
)


@dataclass(frozen=True, slots=True)
class _Entry:
    at_ms: int
    identity: AttributionIdentity


class Timeline:
    """Bounded thread-safe history; never fills absent UI metadata by guessing.

    Reading capture history must not refresh its age. Entries are retained for
    at most 120 seconds relative to the newest observation, and at most 512
    entries in total. The limits include repeated observations of one speaker.
    """

    MAX_AGE_MS = 2000
    GUARD_MS = 300
    RETENTION_MS = 120_000
    MAX_OBSERVATIONS = 512

    def __init__(self):
        self._entries: deque[_Entry] = deque(maxlen=self.MAX_OBSERVATIONS)
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    @staticmethod
    def _identity(observation: Observation) -> AttributionIdentity:
        source, name = observation.source_id, observation.display_name
        evidence = observation.evidence
        if (not isinstance(source, str) or not source.startswith("local:zoom-uia:")
                or source in {"local:zoom-uia:", "local:zoom-uia:unknown"}
                or len(source) > 512 or not isinstance(name, str)
                or not name.strip() or len(name) > 512
                or evidence != "uia_explicit_active_speaker_hint"):
            return UNKNOWN
        return AttributionIdentity(source, name.strip() + " (по окну Zoom)", "zoom_ui_hint")

    def add(self, observation: Observation) -> bool:
        at_ms = observation.observed_at_ms
        if isinstance(at_ms, bool) or not isinstance(at_ms, int) or at_ms < 0:
            raise ValueError("Наблюдение должно иметь монотонное время в миллисекундах.")
        entry = _Entry(at_ms, self._identity(observation))
        with self._lock:
            if self._entries and at_ms < self._entries[-1].at_ms:
                # A late callback cannot overwrite the current speaker. Reject
                # it without interrupting audio: a slow UIA scan can finish
                # after the observer has emitted a newer stale/unknown marker.
                return False
            if self._entries and at_ms == self._entries[-1].at_ms:
                previous = self._entries.pop()
                if previous.identity != entry.identity:
                    entry = _Entry(at_ms, UNKNOWN)
            self._entries.append(entry)
            cutoff = at_ms - self.RETENTION_MS
            while self._entries and self._entries[0].at_ms < cutoff:
                self._entries.popleft()
        return True

    def resolve(self, start_ms: float, end_ms: float) -> AttributionIdentity:
        """Resolve one word in absolute monotonic milliseconds.

        Every point in [start - 300 ms, end + 300 ms] must be covered by a
        non-expired observation of the same source and name. A rename, speaker
        switch, explicit failure, or metadata gap in that interval yields an
        unattributed mixed source. No future-only observation backfills a word.
        """
        if (not isinstance(start_ms, (int, float)) or isinstance(start_ms, bool)
                or not isinstance(end_ms, (int, float)) or isinstance(end_ms, bool)
                or not math.isfinite(start_ms) or not math.isfinite(end_ms)
                or start_ms < 0 or end_ms < start_ms):
            raise ValueError("Некорректный интервал слова для определения говорящего.")
        left, right = start_ms - self.GUARD_MS, end_ms + self.GUARD_MS
        with self._lock:
            entries = tuple(self._entries)
        current: _Entry | None = None
        index = 0
        while index < len(entries) and entries[index].at_ms <= left:
            if (entries[index].at_ms == left and current is not None
                    and entries[index].identity != current.identity):
                return UNKNOWN  # The uncertainty guard includes both edges.
            current = entries[index]
            index += 1
        if (current is None or current.identity == UNKNOWN
                or left - current.at_ms > self.MAX_AGE_MS):
            return UNKNOWN
        identity = current.identity
        for entry in entries[index:]:
            if entry.at_ms > right:
                break
            if (entry.identity != identity
                    or entry.at_ms - current.at_ms > self.MAX_AGE_MS):
                return UNKNOWN
            current = entry
        if right - current.at_ms > self.MAX_AGE_MS:
            return UNKNOWN
        return identity


class DesktopSegmenter:
    """Split decoded words at changes of observed, explicitly qualified names."""

    def __init__(self, timeline: Timeline, local_name: str = "Вы", diarization=None):
        if not isinstance(local_name, str) or not local_name.strip() or len(local_name) > 512:
            raise ValueError("Укажите имя местного источника длиной от 1 до 512 символов.")
        self.timeline = timeline
        self.diarization = diarization
        self.local_identity = AttributionIdentity(
            "local:microphone", local_name.strip(), "local_microphone"
        )

    def _voice_identity(self, word: Word) -> AttributionIdentity:
        """Optional cluster labels never establish a person's real name/Zoom ID.

        The diarization resolver owns continuity/overlap checks. Its cosine
        confidence is a heuristic rather than a calibrated probability. These
        checks also fail closed for an unavailable or malformed optional adapter.
        """
        if self.diarization is None:
            return UNKNOWN
        try:
            span = self.diarization.resolve(word.start, word.end)
            if (span is None or not isinstance(span.local_speaker_id, str)
                    or not math.isfinite(span.confidence) or not .60 <= span.confidence <= 1
                    or not math.isfinite(span.start) or not math.isfinite(span.end)
                    or span.start > word.start or span.end < word.end):
                return UNKNOWN
            match = re.fullmatch(r"local:voice:(\d{1,3})", span.local_speaker_id)
            if not match or int(match.group(1)) == 0:
                return UNKNOWN
            return AttributionIdentity(span.local_speaker_id,
                                       f"Говорящий {int(match.group(1))} (по голосу)", "voice_cluster")
        except Exception:
            return UNKNOWN

    def __call__(self, source_id: str, epoch: str, default_name: str,
                 selected_words: list[Word], origin_ms: int) -> list[Segment]:
        if isinstance(origin_ms, bool) or not isinstance(origin_ms, int) or origin_ms < 0:
            raise ValueError("Нужно монотонное начало локального сеанса.")
        groups: list[tuple[AttributionIdentity, list[Word]]] = []
        previous_start = -1.0
        for word in selected_words:
            if (not math.isfinite(word.start) or not math.isfinite(word.end)
                    or word.start < 0 or word.end < word.start or word.start < previous_start):
                raise ValueError("Слова должны иметь корректные упорядоченные таймкоды.")
            previous_start = word.start
            if not word.text.strip():
                continue
            if source_id == "local:microphone":
                identity = self.local_identity
            elif source_id == "local:remote":
                identity = self.timeline.resolve(
                    origin_ms + word.start * 1000, origin_ms + word.end * 1000
                )
                if identity == UNKNOWN:
                    identity = self._voice_identity(word)
            else:
                identity = UNKNOWN
            if not groups or groups[-1][0] != identity:
                groups.append((identity, []))
            groups[-1][1].append(word)

        return [Segment(
            id=str(uuid4()), speaker_id=identity.source_id,
            display_name=identity.display_name, start=words[0].start,
            end=max(word.end for word in words), text=" ".join(word.text.strip() for word in words),
            status="final", source_epoch=epoch, attribution=identity.attribution,
        ) for identity, words in groups]
