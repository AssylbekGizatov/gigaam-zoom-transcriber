"""Match audio capture intervals against immutable OCR capture observations."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import threading
import time
from uuid import uuid4

import numpy as np

from .app_log import log_attribution
from .models import Segment, Word
from .speaker_observer import monotonic_ms


@dataclass(frozen=True, slots=True)
class AttributionIdentity:
    source_id: str
    display_name: str
    attribution: str


UNKNOWN = AttributionIdentity("local:remote", "Неизвестно", "unattributed_mix")


@dataclass(frozen=True, slots=True)
class AttributionDecision:
    identity: AttributionIdentity = UNKNOWN
    reason: str = "no_observations"
    candidates: tuple[tuple[str, float], ...] = ()  # Confidence-weighted supporting milliseconds.


@dataclass(frozen=True, slots=True)
class _Entry:
    at: int
    identity: AttributionIdentity
    recognized_name: str | None
    confidence: float | None
    evidence: str
    status: str


class Timeline:
    """Capture-time history; decoding latency never expires an observed name.

    Keep recent history plus the intervals explicitly retained by accepted ASR
    buffers/windows. A stalled model therefore does not lose its old evidence,
    and dropped live audio does not pin the intervening hours of metadata.
    """
    RETENTION_MS = 60_000
    MAX_RECENT_OBSERVATIONS = 4096
    MAX_GAP_MS = 2000
    QUIET_MS = 240
    LOOKBACK_MS = 500
    LOOKAHEAD_MS = 1500
    MIN_SUPPORT_MS = 200
    MIN_DOMINANCE = .75

    def __init__(self):
        self._entries = deque()
        self._activity = {source: deque() for source in ("remote", "microphone")}
        self._audio_watermark = {source: 0.0 for source in self._activity}
        self._pins = {}
        self._audio_finished = self._closed = False
        self._condition = threading.Condition(threading.RLock())

    def _prune_locked(self, latest):
        cutoff = latest - self.RETENTION_MS
        ranges = tuple(self._pins.values())
        recent = [entry for entry in self._entries if entry.at >= cutoff]
        count_cutoff = recent[-self.MAX_RECENT_OBSERVATIONS].at if len(recent) > self.MAX_RECENT_OBSERVATIONS else cutoff
        self._entries = deque(entry for entry in self._entries if entry.at >= count_cutoff
                              or any(a <= entry.at <= b for a, b in ranges))
        for source, spans in self._activity.items():
            self._activity[source] = deque((a, b) for a, b in spans
                if b >= cutoff or any(a <= right and b >= left for left, right in ranges))

    def retain(self, token, start_ms, end_ms):
        with self._condition:
            self._pins[token] = (start_ms - self.LOOKBACK_MS - self.MAX_GAP_MS,
                                 end_ms + self.LOOKAHEAD_MS + self.MAX_GAP_MS)

    def release(self, token):
        with self._condition:
            self._pins.pop(token, None)
            latest = max(self._audio_watermark.values(), default=0)
            if self._entries:
                latest = max(latest, self._entries[-1].at)
            self._prune_locked(latest)

    def finish_audio(self):
        with self._condition:
            self._audio_finished = True
            self._condition.notify_all()

    def wait_for_tail(self, timeout):
        with self._condition:
            spans = self._activity["remote"]
            tail = spans[-1] if spans else None
        if tail is not None:
            self.wait_for_lookahead(tail[0], tail[1], timeout)

    def close(self):
        with self._condition:
            self._closed = self._audio_finished = True
            self._condition.notify_all()

    def add(self, observation):
        at = observation.observed_at_ms
        if type(at) is not int or at < 0:
            raise ValueError("Некорректное время наблюдения.")
        identity = UNKNOWN
        source, evidence = observation.source_id, observation.evidence
        confidence = getattr(observation, "confidence", None)
        valid_confidence = (confidence is None or type(confidence) in (float, int)
                            and math.isfinite(confidence) and 0 <= confidence <= 1)
        weight = float(confidence) if confidence is not None and valid_confidence else None
        if (valid_confidence and evidence == "ocr_speaker_view_hint" and isinstance(source, str)
                and source.startswith("local:zoom-ocr:") and source.removeprefix("local:zoom-ocr:") not in {"", "unknown"}
                and isinstance(observation.display_name, str) and 0 < len(observation.display_name.strip()) <= 160):
            identity = AttributionIdentity(source, observation.display_name.strip(), "zoom_ocr_hint")
        recognized = getattr(observation, "recognized_name", None)
        if (not valid_confidence or evidence != "ocr_confirming" or not isinstance(recognized, str)
                or not 0 < len(recognized.strip()) <= 160):
            recognized = None
        entry = _Entry(at, identity, recognized.strip() if recognized else None, weight, evidence,
                       getattr(observation, "status", "ok" if identity != UNKNOWN else "missing"))
        with self._condition:
            if self._entries and self._entries[-1].at > at:
                return False
            if self._entries and self._entries[-1].at == at:
                previous = self._entries.pop()
                if previous != entry:
                    entry = _Entry(at, UNKNOWN, None, None, "ocr_conflicting_timestamp", "missing")
            self._entries.append(entry)
            self._prune_locked(at)
            self._condition.notify_all()
        return True

    def add_audio(self, source, timestamp_ms, samples, rate):
        """Record only times of non-quiet PCM blocks. No audio is retained."""
        block = max(1, rate // 50)
        spans = []
        for offset in range(0, len(samples), block):
            chunk = samples[offset:offset + block].astype(np.float32)
            if np.mean(chunk * chunk) >= 64 ** 2:
                spans.append((timestamp_ms + offset * 1000 / rate,
                              timestamp_ms + (offset + len(chunk)) * 1000 / rate))
        with self._condition:
            history = self._activity[source]
            for start, end in spans:
                if history and start <= history[-1][1] + self.QUIET_MS:
                    history[-1] = (history[-1][0], max(end, history[-1][1]))
                else:
                    history.append((start, end))
            self._audio_watermark[source] = max(self._audio_watermark[source], timestamp_ms + len(samples) * 1000 / rate)
            # OCR updates prune the full history. Audio-only sessions still
            # prune once enough capture time has elapsed, not every 20ms block.
            if history and history[0][1] < timestamp_ms - self.RETENTION_MS:
                self._prune_locked(timestamp_ms)
            self._condition.notify_all()

    def remote_active_now(self):
        with self._condition:
            spans = self._activity["remote"]
            return bool(spans and 0 <= monotonic_ms() - spans[-1][1] <= self.QUIET_MS)

    def _turn_locked(self, start, end):
        spans = self._activity["remote"]
        for index, (a, b) in enumerate(spans):
            if a <= start and end <= b:
                next_start = spans[index + 1][0] if index + 1 < len(spans) else math.inf
                return a, b, next_start
        return None

    def wait_for_lookahead(self, start_ms, end_ms, timeout=0):
        """Wait once per decoded window, only if useful OCR is already available.

        Background ASR may wait; capture and the GUI never do. A closed history,
        definite missing activity, old backlog, or unavailable OCR returns at
        once. The deadline limits waiting independently of the capture clock.
        """
        if timeout <= 0:
            return
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._closed:
                # A decoded window may contain several turns: use its last word.
                turn = self._turn_locked(max(start_ms, end_ms - 1), end_ms)
                if turn is None or not any(e.identity != UNKNOWN or e.recognized_name
                                           for e in self._entries if e.at >= start_ms - self.MAX_GAP_MS):
                    return
                target = min(end_ms + self.LOOKAHEAD_MS, turn[2])
                ocr_ready = self._entries and self._entries[-1].at >= target
                audio_ready = self._audio_finished or self._audio_watermark["remote"] >= target
                if ocr_ready and audio_ready:
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._condition.wait(min(remaining, .2))

    def decide(self, start_ms, end_ms, *, require_lookahead=False):
        if not (math.isfinite(start_ms) and math.isfinite(end_ms) and 0 <= start_ms < end_ms):
            raise ValueError("Некорректный интервал слова.")
        with self._condition:
            turn = self._turn_locked(start_ms, end_ms)
            if turn is None:
                return AttributionDecision(reason="audio_silence")
            if any(a < end_ms and b > start_ms for a, b in self._activity["microphone"]):
                return AttributionDecision(reason="microphone_overlap")
            left = max(start_ms - self.LOOKBACK_MS, turn[0])
            right = min(end_ms + self.LOOKAHEAD_MS, turn[2])
            if not self._entries or self._entries[-1].at < (right if require_lookahead else end_ms):
                return AttributionDecision(reason="missing_right_observation")
            prior_name = next((e.identity.display_name for e in reversed(self._entries)
                               if e.at < turn[0] and e.identity != UNKNOWN), None)
            # An adjacent observation after the score window can confirm the
            # interval ending at its right edge, but never from another turn.
            entries = [e for e in self._entries if turn[0] <= e.at < turn[2]
                       and left - self.MAX_GAP_MS <= e.at <= right + self.MAX_GAP_MS]
        scores = {}
        intervals = []
        for before, after in zip(entries, entries[1:]):
            if not 0 < after.at - before.at <= self.MAX_GAP_MS or after.identity == UNKNOWN:
                continue
            agrees = (before.identity == after.identity or
                      before.identity == UNKNOWN and before.evidence == "ocr_confirming"
                      and before.recognized_name == after.identity.display_name)
            if not agrees:
                continue
            a, b = max(left, before.at), min(right, after.at)
            if b <= a:
                continue
            score = (b - a) * min(1.0 if before.confidence is None else before.confidence,
                                  1.0 if after.confidence is None else after.confidence)
            if score <= 0:
                continue
            scores[after.identity] = scores.get(after.identity, 0.0) + score
            intervals.append((b, after.identity))
        candidates = tuple((identity.display_name, round(score, 3)) for identity, score in
                           sorted(scores.items(), key=lambda item: (-item[1], item[0].source_id)))
        observed_names = [(e.at, e.identity.display_name if e.identity != UNKNOWN else e.recognized_name)
                          for e in entries if left <= e.at <= right
                          and (e.identity != UNKNOWN or e.recognized_name)]
        failures = [entry for index, entry in enumerate(entries)
                    if entry.at < end_ms and (entries[index + 1].at if index + 1 < len(entries)
                                             else entry.at + self.MAX_GAP_MS) > start_ms
                    and entry.identity == UNKNOWN
                    and entry.evidence not in {"ocr_confirming", "ocr_not_started", "ocr_stopped"}]
        if failures:
            reason = ("speaker_conflict" if len({name for _, name in observed_names}) > 1 else
                      "temporary_ocr_absence" if all(e.evidence in {"ocr_name_missing", "ocr_region_occluded"}
                                                     for e in failures) else "explicit_ocr_failure")
            return AttributionDecision(reason=reason, candidates=candidates)
        if any(entry.evidence == "ocr_confirming" and entry.recognized_name is None
               and entry.at < end_ms and (entries[index + 1].at if index + 1 < len(entries)
                                          else entry.at + self.MAX_GAP_MS) > start_ms
               for index, entry in enumerate(entries)):
            return AttributionDecision(reason="observation_gap", candidates=candidates)
        if not scores:
            known = {e.identity for e in entries if e.identity != UNKNOWN and left <= e.at <= right}
            if len(known) > 1:
                reason = "speaker_conflict"
            elif not entries:
                reason = "no_observations"
            elif any(b.at - a.at > self.MAX_GAP_MS for a, b in zip(entries, entries[1:])):
                reason = "observation_gap"
            elif any(e.identity != UNKNOWN or e.recognized_name for e in entries):
                reason = "missing_right_observation"
            else:
                reason = "explicit_ocr_failure"
            return AttributionDecision(reason=reason, candidates=candidates)
        winner = max(scores, key=scores.get)
        support, total = scores[winner], sum(scores.values())
        # A delayed new label must never let the older, trailing name win a
        # mixed transition window merely because it was displayed for longer.
        if ((observed_names and observed_names[-1][1] != winner.display_name)
                or len(scores) > 1 and (support / total < self.MIN_DOMINANCE
                                       or max(intervals, key=lambda item: item[0])[1] != winner)):
            return AttributionDecision(reason="speaker_conflict", candidates=candidates)
        if support < self.MIN_SUPPORT_MS:
            return AttributionDecision(reason="observation_gap", candidates=candidates)
        if (winner.display_name == prior_name and not any(e.at <= start_ms and
                (e.identity == winner or e.recognized_name == winner.display_name) for e in entries)):
            # Zoom retains a name during silence. An unchanged retained label
            # cannot backfill the onset of a new turn before its first reading.
            return AttributionDecision(reason="missing_left_observation", candidates=candidates)
        return AttributionDecision(winner, "stable_ocr_overlap", candidates)

    def resolve(self, start_ms, end_ms):
        return self.decide(start_ms, end_ms).identity


class DesktopSegmenter:
    def __init__(self, timeline: Timeline, local_name="Вы", *, lookahead_timeout=0):
        if not isinstance(local_name, str) or not 0 < len(local_name.strip()) <= 512:
            raise ValueError("Укажите имя для микрофона.")
        self.timeline = timeline
        self.lookahead_timeout = lookahead_timeout
        self.local_identity = AttributionIdentity("local:microphone", local_name.strip(), "local_microphone")

    def retain(self, token, source_id, start_ms, end_ms):
        if source_id == "local:remote":
            self.timeline.retain(token, start_ms, end_ms)

    def release(self, token):
        self.timeline.release(token)

    def __call__(self, source_id, epoch, default_name, selected_words: list[Word], origin_ms):
        if type(origin_ms) is not int or origin_ms < 0:
            raise ValueError("Некорректное начало сеанса.")
        groups = []
        previous_start = previous_end = -1
        if selected_words and source_id == "local:remote":
            self.timeline.wait_for_lookahead(origin_ms + selected_words[0].start * 1000,
                origin_ms + max(w.end for w in selected_words) * 1000, self.lookahead_timeout)
        for index, word in enumerate(selected_words):
            if (not math.isfinite(word.start) or not math.isfinite(word.end) or
                    not 0 <= word.start < word.end or word.start < previous_start):
                raise ValueError("Некорректный интервал слова.")
            previous_start = word.start
            if not word.text.strip():
                continue
            decision = AttributionDecision(reason="invalid_source")
            overlap = word.start < previous_end or (index + 1 < len(selected_words)
                                                    and selected_words[index + 1].start < word.end)
            if source_id == "local:microphone":
                decision = AttributionDecision(self.local_identity, "local_microphone")
            elif source_id == "local:remote":
                decision = (AttributionDecision(reason="overlapping_word_times") if overlap else
                            self.timeline.decide(origin_ms + word.start * 1000, origin_ms + word.end * 1000,
                                                 require_lookahead=self.lookahead_timeout > 0))
            key = (decision.identity, decision.reason)
            if not groups or groups[-1][0] != key or word.start - previous_end > self.timeline.QUIET_MS / 1000:
                groups.append((key, []))
            groups[-1][1].append(word)
            previous_end = max(previous_end, word.end)
        segments = []
        for (identity, reason), words in groups:
            first, last = words[0].start, max(w.end for w in words)
            start, end = origin_ms + first * 1000, origin_ms + last * 1000
            # Recompute once across the actual emitted interval. A microphone
            # overlap or conflict in a short inter-word gap must not disappear
            # merely because each individual word had uncontested evidence.
            evidence = self.timeline.decide(start, end,
                require_lookahead=self.lookahead_timeout > 0) if source_id == "local:remote" else AttributionDecision()
            if source_id == "local:remote" and identity != UNKNOWN and evidence.identity != identity:
                identity = UNKNOWN
                reason = evidence.reason if evidence.identity == UNKNOWN else "speaker_conflict"
            segment = Segment(id=str(uuid4()), speaker_id=identity.source_id, display_name=identity.display_name,
                              start=first, end=last, text=" ".join(w.text.strip() for w in words),
                              status="final", source_epoch=epoch, attribution=identity.attribution,
                              attribution_reason=reason)
            log_attribution(max(0, start - self.timeline.LOOKBACK_MS), end + self.timeline.LOOKAHEAD_MS,
                            evidence.candidates, identity.display_name if identity != UNKNOWN else None,
                            reason, segment_id=segment.id)
            segments.append(segment)
        return segments
