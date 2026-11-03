"""Optional CPU voice clustering. Local labels are estimates, never Zoom IDs.

Bounded three-second windows and sixteen centroids; no audio files or network.
This is not a trained overlap detector: simultaneous voices may remain merged.
Speaker changes/uncertain matches fail closed rather than force a known name.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import threading

import numpy as np


SAMPLE_RATE = 16000


@dataclass(frozen=True, slots=True)
class SpeakerModelSpec:
    model_id: str
    filename: str
    size_bytes: int
    sha256: str
    url: str
    license: str
    license_url: str
    model_card: str
    attribution: str
    # Match / novelty / top-two margin / half-window consistency.
    thresholds: tuple[float, float, float, float]


SPEAKER_MODELS = {
    "campplus": SpeakerModelSpec(
        "campplus", "campplus.onnx", 28281164,
        "aa3cfc16963a10586a9393f5035d6d6b57e98d358b347f80c2a30bf4f00ceba2",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx",
        "Apache-2.0", "https://www.apache.org/licenses/LICENSE-2.0",
        "https://modelscope.cn/models/iic/speech_campplus_sv_zh_en_16k-common_advanced",
        "3D-Speaker / Tongyi Lab; Hui Wang et al., CAM++, arXiv:2303.00332; ModelScope v1.0.0; Sherpa ONNX export",
        (.60, .35, .10, .45),
    ),
    "wespeaker": SpeakerModelSpec(
        "wespeaker", "wespeaker.onnx", 26530550,
        "e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx",
        "CC-BY-4.0", "https://creativecommons.org/licenses/by/4.0/",
        "https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/tree/f0c48c298fd835726c27956a5d617bad7115627e",
        "WeSpeaker team; Wang et al., ICASSP 2023; Sherpa ONNX export",
        (.73, .68, .06, .60),
    ),
}
DEFAULT_MODEL = SPEAKER_MODELS["campplus"]
MODEL_URL, MODEL_SHA256 = DEFAULT_MODEL.url, DEFAULT_MODEL.sha256
MODEL_BYTES, MODEL_LICENSE = DEFAULT_MODEL.size_bytes, DEFAULT_MODEL.license
MODEL_CARD = DEFAULT_MODEL.model_card


class DiarizationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SpeakerSpan:
    start: float
    end: float
    local_speaker_id: str | None
    confidence: float
    reason: str

    @property
    def display_name(self) -> str:
        if self.local_speaker_id is None:
            return "Неизвестный говорящий"
        return f"Говорящий {int(self.local_speaker_id.rsplit(':', 1)[1])} (по голосу)"


def identify_model(path: str | Path) -> tuple[Path, SpeakerModelSpec]:
    path = Path(path)
    if not path.is_file():
        raise DiarizationError("Модель голосов отсутствует. Запустите scripts/download_diarization.py.")
    candidates = [spec for spec in SPEAKER_MODELS.values() if spec.size_bytes == path.stat().st_size]
    if not candidates:
        raise DiarizationError("Неподдерживаемая модель голосов. Запустите scripts/download_diarization.py.")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    for spec in candidates:
        if digest == spec.sha256:
            return path, spec
    raise DiarizationError("Контрольная сумма модели голосов не совпала. Загрузите её заново.")


def verify_model(path: str | Path) -> Path:
    return identify_model(path)[0]


class SherpaSpeakerEmbedder:
    """Pinned ONNX via Sherpa, with a single inference CPU thread."""

    def __init__(self, model_path: str | Path):
        self.model_path = Path(model_path)
        self._extractor = None
        self.model_spec: SpeakerModelSpec | None = None

    def load(self) -> None:
        if self._extractor is not None:
            return
        path, spec = identify_model(self.model_path)
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise DiarizationError("Установите requirements-diarization.txt для разделения голосов.") from exc
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(path), num_threads=1, debug=False, provider="cpu",
        )
        if not config.validate():
            raise DiarizationError("Неподдерживаемая модель разделения голосов.")
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        self.model_spec = spec

    def __call__(self, samples: np.ndarray) -> np.ndarray:
        self.load()
        stream = self._extractor.create_stream()
        stream.accept_waveform(sample_rate=SAMPLE_RATE,
                               waveform=np.ascontiguousarray(samples, dtype=np.float32))
        stream.input_finished()
        if not self._extractor.is_ready(stream):
            raise DiarizationError("Недостаточно речи для определения голоса.")
        return np.asarray(self._extractor.compute(stream), dtype=np.float32)


def _normalized(vector) -> np.ndarray:
    values = np.asarray(vector, dtype=np.float32)
    if values.ndim != 1 or not 2 <= len(values) <= 2048 or not np.isfinite(values).all():
        raise DiarizationError("Модель вернула некорректный вектор голоса.")
    norm = float(np.linalg.norm(values))
    if norm < 1e-8:
        raise DiarizationError("Модель вернула пустой вектор голоса.")
    return values / norm


class OnlineDiarizer:
    """Chronological float32 mono 16 kHz input. Use one instance per meeting.

    The score is cosine similarity, not calibrated identity confidence. Silence
    gating is an energy heuristic, not a speech classifier. Language switching,
    compression and overlapping speech can split/merge clusters incorrectly.
    """

    def __init__(self, embedder, *, max_speakers: int = 16, match_threshold: float | None = None,
                 new_threshold: float | None = None, margin: float | None = None,
                 half_similarity: float | None = None, rms_threshold: float = 0.004,
                 window_seconds: int = 3):
        if isinstance(embedder, SherpaSpeakerEmbedder):
            embedder.load()
        profile = getattr(embedder, "model_spec", None) or DEFAULT_MODEL
        match_threshold = profile.thresholds[0] if match_threshold is None else match_threshold
        new_threshold = profile.thresholds[1] if new_threshold is None else new_threshold
        margin = profile.thresholds[2] if margin is None else margin
        half_similarity = profile.thresholds[3] if half_similarity is None else half_similarity
        if not 1 <= max_speakers <= 16:
            raise ValueError("max_speakers must be between 1 and 16")
        if not 0 < new_threshold < match_threshold < 1 or not 0 <= margin < 1:
            raise ValueError("Invalid clustering thresholds")
        if window_seconds not in (2, 3, 4):
            raise ValueError("Window must be 2, 3 or 4 seconds")
        self.embedder = embedder
        self.max_speakers = max_speakers
        self.match_threshold = match_threshold
        self.new_threshold = new_threshold
        self.margin = margin
        self.half_similarity = half_similarity
        self.rms_threshold = rms_threshold
        self.window_samples = window_seconds * SAMPLE_RATE
        self.reset()

    def reset(self) -> None:
        self._buffer = np.empty(0, dtype=np.float32)
        self._buffer_start = 0
        self._expected_sample: int | None = None
        self._centroids: list[np.ndarray] = []
        self._counts: list[int] = []

    @property
    def speaker_count(self) -> int:
        return len(self._centroids)

    @property
    def buffered_samples(self) -> int:
        return len(self._buffer)

    def analyze(self, samples16k, start_seconds: float) -> list[SpeakerSpan]:
        samples = np.asarray(samples16k, dtype=np.float32)
        if (samples.ndim != 1 or len(samples) > 30 * SAMPLE_RATE or
                not np.isfinite(samples).all() or not math.isfinite(start_seconds) or start_seconds < 0):
            raise ValueError("Expected at most 30 seconds of finite mono 16 kHz samples")
        if samples.size and np.max(np.abs(samples)) > 1.01:
            raise ValueError("Expected float audio in [-1, 1]")
        start = round(start_seconds * SAMPLE_RATE)
        output: list[SpeakerSpan] = []
        if self._expected_sample is not None:
            if start < self._expected_sample - 1:
                raise ValueError("Input must be chronological without overlap")
            if start > self._expected_sample + 1:
                output.extend(self.flush(reason="audio_gap"))
                self._buffer_start = start
        else:
            self._buffer_start = start
        self._expected_sample = start + len(samples)
        cursor = 0
        while cursor < len(samples):
            need = self.window_samples - len(self._buffer)
            piece = samples[cursor:cursor + need]
            self._buffer = np.concatenate((self._buffer, piece))
            cursor += len(piece)
            if len(self._buffer) < self.window_samples:
                break
            identity, confidence, reason = self._classify(self._buffer)
            output.append(SpeakerSpan(self._buffer_start / SAMPLE_RATE,
                                      (self._buffer_start + SAMPLE_RATE) / SAMPLE_RATE,
                                      identity, confidence, reason))
            self._buffer = self._buffer[SAMPLE_RATE:].copy()
            self._buffer_start += SAMPLE_RATE
        return output

    def flush(self, *, reason: str = "short_window") -> list[SpeakerSpan]:
        """Discard retained audio. An incomplete final window is deliberately unknown."""
        spans = []
        if len(self._buffer):
            spans.append(SpeakerSpan(self._buffer_start / SAMPLE_RATE,
                                     (self._buffer_start + len(self._buffer)) / SAMPLE_RATE,
                                     None, 0.0, reason))
        self._buffer = np.empty(0, dtype=np.float32)
        self._expected_sample = None
        return spans

    def _has_energy(self, samples: np.ndarray) -> bool:
        frames = samples.reshape(-1, 320)
        # Remove DC bias; clipping/nonfinite audio must not enroll a voice.
        centered = frames - frames.mean(axis=1, keepdims=True)
        rms = np.sqrt(np.mean(centered * centered, axis=1))
        return bool(np.mean(rms >= self.rms_threshold) >= 0.50 and
                    np.mean(np.abs(samples) >= 0.999) < 0.05)

    def _classify(self, window: np.ndarray) -> tuple[str | None, float, str]:
        if not self._has_energy(window):
            return None, 0.0, "quiet_or_clipped"
        vector = _normalized(self.embedder(window))
        if self._centroids and len(vector) != len(self._centroids[0]):
            raise DiarizationError("Размер вектора голоса изменился.")
        similarities = np.asarray([float(np.dot(vector, c)) for c in self._centroids])
        ranking = np.argsort(similarities)[::-1]
        if len(ranking):
            best = int(ranking[0])
            score = float(similarities[best])
            second = float(similarities[ranking[1]]) if len(ranking) > 1 else -1.0
            if score >= self.match_threshold and score - second >= self.margin:
                # Slow bounded adaptation limits drift from a single bad window.
                count = min(self._counts[best], 32)
                self._centroids[best] = _normalized(self._centroids[best] * count + vector)
                self._counts[best] = min(count + 1, 32)
                return f"local:voice:{best + 1:03d}", min(1.0, score), "voice_cluster_estimate"
            if score >= self.new_threshold:
                return None, max(0.0, score), "ambiguous_voice"
        # A new voice must be internally consistent across both halves. This
        # catches many change boundaries; it does NOT detect all speech overlap.
        half = len(window) // 2
        halves = (window[:half], window[half:])
        if not all(self._has_energy(half) for half in halves):
            return None, 0.0, "insufficient_speech"
        left, right = (_normalized(self.embedder(half)) for half in halves)
        consistency = float(np.dot(left, right))
        if consistency < self.half_similarity:
            return None, max(0.0, consistency), "unstable_or_mixed_voice"
        if len(self._centroids) >= self.max_speakers:
            return None, 0.0, "speaker_limit"
        self._centroids.append(vector.copy())
        self._counts.append(1)
        return (f"local:voice:{len(self._centroids):03d}", min(1.0, consistency),
                "voice_cluster_estimate")


class DiarizationTimeline:
    """Adapter for overlapping ASR windows and conservative word attribution.

    Only the mixed remote source is analyzed. Own microphone remains its known
    input-device source. Recent output is capped at 120 seconds / 256 spans.
    """

    def __init__(self, diarizer: OnlineDiarizer, *, retention_seconds: float = 120,
                 max_spans: int = 256):
        if retention_seconds <= 0 or not 1 <= max_spans <= 256:
            raise ValueError("Invalid timeline bounds")
        self.diarizer = diarizer
        self.retention_seconds = retention_seconds
        self._spans: deque[SpeakerSpan] = deque(maxlen=max_spans)
        self._seen_end: float | None = None
        self._source_id: str | None = None
        self._lock = threading.RLock()

    def analyze_window(self, source_id: str, samples, rate: int,
                       start_seconds: float) -> None:
        if not source_id.startswith("local:remote"):
            return
        if rate != SAMPLE_RATE:
            raise ValueError("Diarization requires 16 kHz audio")
        values = np.asarray(samples, dtype=np.float32)
        if values.ndim != 1 or len(values) > 30 * SAMPLE_RATE or not math.isfinite(start_seconds) or start_seconds < 0:
            raise ValueError("Invalid diarization window")
        with self._lock:
            if self._source_id is not None and self._source_id != source_id:
                self.reset()
            self._source_id = source_id
            skip = max(0, round(((self._seen_end or 0) - start_seconds) * SAMPLE_RATE))
            if skip >= len(values):
                return
            actual_start = start_seconds + skip / SAMPLE_RATE
            spans = self.diarizer.analyze(values[skip:], actual_start)
            self._seen_end = start_seconds + len(values) / SAMPLE_RATE
            self._spans.extend(spans)
            cutoff = self._seen_end - self.retention_seconds
            while self._spans and self._spans[0].end < cutoff:
                self._spans.popleft()

    def resolve(self, start: float, end: float) -> SpeakerSpan | None:
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            return None
        with self._lock:
            covered_to = start
            chosen = None
            confidence = 1.0
            for span in self._spans:
                if span.end <= start:
                    continue
                if span.start >= end:
                    break
                if (span.start > covered_to + 1 / SAMPLE_RATE or
                        span.local_speaker_id is None or span.confidence < 0.60):
                    return None
                if chosen is not None and chosen != span.local_speaker_id:
                    return None
                chosen = span.local_speaker_id
                confidence = min(confidence, span.confidence)
                covered_to = max(covered_to, span.end)
                if covered_to >= end - 1 / SAMPLE_RATE:
                    return SpeakerSpan(start, end, chosen, confidence, "voice_cluster_estimate")
            return None

    def flush(self) -> None:
        with self._lock:
            self._spans.extend(self.diarizer.flush())

    def reset(self) -> None:
        with self._lock:
            self.diarizer.reset()
            self._spans.clear()
            self._seen_end = None
            self._source_id = None
