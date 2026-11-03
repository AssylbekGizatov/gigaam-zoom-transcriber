"""Windows WASAPI output + optional microphone, entirely in memory.

This captures a selected *output device*, not an isolated Zoom process. Other
apps playing through that device are audible to this adapter too. No recording
file, network, Zoom API, virtual driver, or elevated permission is used.

``on_audio(source, pcm16, timestamp_ms, 16000)`` runs on a worker, never on the
PortAudio callback. ``source`` is ``remote`` or ``microphone``. Timestamps share
one monotonic origin advanced by input sample counts (``monotonic()*1000``).
They describe local capture, NOT server-side meeting time. ``on_event(kind,
payload)`` also runs on a worker; messages contain no captured audio.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import queue
import sys
import threading
import time

import numpy as np

OUTPUT_RATE = 16000
MAX_PENDING_SECONDS = 2.0


class CaptureError(RuntimeError):
    """Safe diagnostic intended for the UI."""


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    kind: str
    sample_rate: int
    channels: int
    is_default: bool = False


def _backend():
    if sys.platform != "win32":
        raise CaptureError("Живой захват звука доступен только в Windows.")
    try:
        import pyaudiowpatch as pa
    except ImportError as error:
        raise CaptureError("Установите зависимости настольного захвата: requirements-desktop.txt.") from error
    return pa


def list_devices() -> list[AudioDevice]:
    """Enumerate WASAPI inputs only; does not open/listen to any device."""
    pa = _backend()
    try:
        with pa.PyAudio() as manager:
            api = manager.get_host_api_info_by_type(pa.paWASAPI)
            default_in = int(api.get("defaultInputDevice", -1))
            try:
                default_loop = int(manager.get_default_wasapi_loopback()["index"])
            except (OSError, ValueError):
                default_loop = -1
            return [AudioDevice(int(d["index"]), str(d["name"]),
                                "loopback" if d.get("isLoopbackDevice") else "microphone",
                                int(d["defaultSampleRate"]), int(d["maxInputChannels"]),
                                int(d["index"]) in {default_in, default_loop})
                    for d in manager.get_device_info_generator()
                    if int(d["hostApi"]) == int(api["index"]) and d["maxInputChannels"] > 0]
    except (OSError, ValueError) as error:
        raise CaptureError("Не удалось получить устройства WASAPI. Проверьте подключение наушников и микрофона.") from error


class StreamingResampler:
    """Bounded causal FIR anti-aliasing + continuous fractional resampling.

    Chunk boundaries do not reset phase or filter history. The 127-tap FIR
    contributes 63 / input_rate seconds of delay when downsampling; callers
    can expose this small fixed latency. Output is mono PCM16 little-endian.
    """
    def __init__(self, input_rate: int, channels: int, output_rate: int = OUTPUT_RATE):
        if not isinstance(input_rate, int) or not 8000 <= input_rate <= 192000:
            raise ValueError("Unsupported sample rate")
        if not isinstance(channels, int) or not 1 <= channels <= 32:
            raise ValueError("Unsupported channel count")
        if output_rate != OUTPUT_RATE:
            raise ValueError("Output must be 16000 Hz")
        self.input_rate, self.channels, self.output_rate = input_rate, channels, output_rate
        self._received = self._produced = 0
        self._last = np.zeros(1, dtype=np.float64)
        self._coefficients = None
        self._history = np.empty(0, dtype=np.float64)
        if input_rate > output_rate:
            offset = np.arange(127, dtype=np.float64) - 63
            cutoff = .45 * output_rate / input_rate
            kernel = 2 * cutoff * np.sinc(2 * cutoff * offset) * np.hamming(127)
            self._coefficients = kernel / kernel.sum()
            self._history = np.zeros(126, dtype=np.float64)

    @property
    def delay_seconds(self):
        return 63 / self.input_rate if self._coefficients is not None else 0.0

    def process(self, pcm: bytes) -> bytes:
        if len(pcm) % (self.channels * 2):
            raise ValueError("Incomplete interleaved PCM frame")
        if len(pcm) > self.input_rate * self.channels * 2:
            raise ValueError("Audio block exceeds one second")
        if not pcm:
            return b""
        values = np.frombuffer(pcm, dtype="<i2").reshape(-1, self.channels).astype(np.float64).mean(axis=1)
        if self._coefficients is not None:
            extended = np.concatenate((self._history, values))
            values = np.convolve(extended, self._coefficients, mode="valid")
            self._history = extended[-126:].copy()
        start = self._received
        self._received += len(values)
        # Only produce sample positions for which both interpolation endpoints
        # exist. Keep one previous sample; no meeting-length array is retained.
        last_position = self._received - 1
        end_output = (last_position * self.output_rate) // self.input_rate + 1
        if end_output <= self._produced:
            self._last = values[-1:].copy()
            return b""
        indexes = np.arange(self._produced, end_output, dtype=np.int64)
        positions = indexes * (self.input_rate / self.output_rate) - start + 1
        joined = np.concatenate((self._last, values))
        result = np.interp(positions, np.arange(len(joined)), joined)
        self._produced = end_output
        self._last = values[-1:].copy()
        return np.clip(np.rint(result), -32768, 32767).astype("<i2").tobytes()


class _SourceClock:
    def __init__(self, rate, origin_ms, origin_clock):
        self.rate, self.origin_ms, self.origin_clock = rate, origin_ms, origin_clock
        self.anchor = None
        self.frames = 0
        self.last_discontinuity = False

    def stamp(self, frames, now, discontinuity=False):
        observed = max(0.0, now - self.origin_clock - frames / self.rate)
        expected = (self.anchor or 0) + self.frames / self.rate
        # A driver can stall without setting PortAudio overflow. Keep real
        # elapsed gaps rather than compress the resumed PCM onto old time.
        # The guard tolerates ordinary scheduling jitter; the quiet renderer
        # keeps normal long pauses delivering clocked zero-valued frames.
        unreported_stall = self.anchor is not None and observed - expected > .25
        self.last_discontinuity = bool(discontinuity or unreported_stall)
        if self.anchor is None or self.last_discontinuity:
            # Never move backwards after a hardware discontinuity.
            self.anchor = max(expected, observed)
            self.frames = 0
        result = self.origin_ms + round(1000 * (self.anchor + self.frames / self.rate))
        self.frames += frames
        return result


class DesktopCapture:
    def __init__(self, loopback_device: int | None = None, microphone_device: int | None = None,
                 on_audio=None, on_event=None):
        self.loopback_device, self.microphone_device = loopback_device, microphone_device
        self.on_audio = on_audio or (lambda *args: None)
        self.on_event = on_event or (lambda *args: None)
        self.origin_ms = None
        self._origin_clock = None
        self._manager = None
        self._streams = []
        self._thread = None
        self._queue = queue.Queue(maxsize=128)
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._failed = threading.Event()
        self._pending = 0.0
        self._stats = {"captured_seconds": 0.0, "dropped_seconds": 0.0,
                       "hardware_overflows": 0, "clock_discontinuities": 0, "peak_pending_seconds": 0.0,
                       "output_packets": 0, "remote_rms": 0.0, "microphone_rms": 0.0}
        self._reported_dropped = self._reported_overflows = self._reported_clock_discontinuities = 0
        self._started = False

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive() and not self._stopping.is_set() and not self._failed.is_set())

    @property
    def metrics(self):
        with self._lock:
            return dict(self._stats, pending_seconds=self._pending, failed=self._failed.is_set())

    def _emit(self, kind, **payload):
        try:
            self.on_event(kind, payload)
        except Exception:
            pass

    def _callback(self, source, rate, channels, pa):
        clock = _SourceClock(rate, self.origin_ms, self._origin_clock)
        def receive(data, frame_count, time_info, status):
            if self._stopping.is_set() or self._failed.is_set():
                return None, pa.paComplete
            seconds = frame_count / rate
            observed_end = time.monotonic()
            # PortAudio's ADC timestamp is the first input sample. Translate
            # its relative age, never its unrelated native clock's absolute
            # value, to the shared Python monotonic clock.
            age = time_info.get("current_time", 0) - time_info.get("input_buffer_adc_time", 0)
            if time_info.get("input_buffer_adc_time", 0) > 0 and math.isfinite(age) and 0 <= age <= 1:
                observed_end += seconds - age
            stamp = clock.stamp(frame_count, observed_end, bool(status))
            with self._lock:
                self._stats["captured_seconds"] += seconds
                if status:
                    self._stats["hardware_overflows"] += 1
                if clock.last_discontinuity:
                    self._stats["clock_discontinuities"] += 1
                # Hard bounds also defend against unexpected driver block sizes.
                if frame_count <= 0 or seconds > .2 or len(data) != frame_count * channels * 2 or self._pending + seconds > MAX_PENDING_SECONDS:
                    self._stats["dropped_seconds"] += seconds
                    return None, pa.paContinue
                try:
                    self._queue.put_nowait((source, data, stamp, rate, channels, clock.last_discontinuity))
                except queue.Full:
                    self._stats["dropped_seconds"] += seconds
                    return None, pa.paContinue
                self._pending += seconds
                self._stats["peak_pending_seconds"] = max(self._pending, self._stats["peak_pending_seconds"])
            return None, pa.paContinue
        return receive

    def start(self):
        if self._started:
            raise CaptureError("Создайте новый захват после остановки.")
        self._started = True
        pa = _backend()
        self._origin_clock = time.monotonic()
        self.origin_ms = round(self._origin_clock * 1000)
        try:
            self._manager = pa.PyAudio()
            api = self._manager.get_host_api_info_by_type(pa.paWASAPI)
            loopback = (self._manager.get_default_wasapi_loopback() if self.loopback_device is None
                        else self._manager.get_device_info_by_index(self.loopback_device))
            # WASAPI loopback may suspend callbacks while the endpoint is idle.
            # Keep a shared-mode silent renderer alive so silence has a clock
            # and speech after a pause is not lost to endpoint startup latency.
            output = None
            for candidate in self._manager.get_device_info_generator():
                if candidate["hostApi"] != api["index"] or candidate["maxOutputChannels"] < 1:
                    continue
                try:
                    analogue = self._manager.get_wasapi_loopback_analogue_by_dict(candidate)
                except (OSError, ValueError):
                    continue
                if analogue and analogue["index"] == loopback["index"]:
                    output = candidate
                    break
            if output is None:
                raise CaptureError("Не найден выход для выбранного loopback. Обновите устройства и выберите его заново.")
            output_rate, output_channels = int(output["defaultSampleRate"]), int(output["maxOutputChannels"])
            def silence(data, frame_count, time_info, status):
                flag = pa.paComplete if self._stopping.is_set() or self._failed.is_set() else pa.paContinue
                return bytes(frame_count * output_channels * 2), flag
            self._streams.append(self._manager.open(format=pa.paInt16, channels=output_channels,
                rate=output_rate, output=True, output_device_index=int(output["index"]),
                frames_per_buffer=max(80, output_rate // 50), stream_callback=silence, start=False))
            choices = [("remote", loopback)]
            if self.microphone_device is not None:
                choices.append(("microphone", self._manager.get_device_info_by_index(self.microphone_device)))
            for source, info in choices:
                if info["hostApi"] != api["index"] or info["maxInputChannels"] < 1 or bool(info.get("isLoopbackDevice")) != (source == "remote"):
                    raise CaptureError("Устройство изменилось. Обновите список и выберите звук Zoom и микрофон заново.")
                rate, channels = int(info["defaultSampleRate"]), int(info["maxInputChannels"])
                if not 8000 <= rate <= 192000 or not 1 <= channels <= 32:
                    raise CaptureError("Формат выбранного аудиоустройства не поддерживается.")
                stream = self._manager.open(format=pa.paInt16, channels=channels, rate=rate, input=True,
                    input_device_index=int(info["index"]), frames_per_buffer=max(80, rate // 50),
                    stream_callback=self._callback(source, rate, channels, pa), start=False)
                self._streams.append(stream)
            # Start all streams before watchdog begins observing them.
            for stream in self._streams:
                stream.start_stream()
            self._thread = threading.Thread(target=self._run, name="desktop-audio-worker", daemon=True)
            self._thread.start()
        except Exception as error:
            self.stop()
            if isinstance(error, CaptureError):
                raise
            raise CaptureError("Не удалось открыть звук. Проверьте выбранные устройства, доступ к микрофону и отсутствие эксклюзивного режима.") from error

    def _run(self):
        resamplers, next_stamp = {}, {}
        checked_at = time.monotonic()
        try:
            while not self._stopping.is_set() or not self._queue.empty():
                now = time.monotonic()
                if not self._stopping.is_set() and now - checked_at >= .5:
                    checked_at = now
                    if any(not stream.is_active() for stream in self._streams):
                        raise CaptureError("Аудиоустройство отключилось. Остановите сеанс, обновите устройства и начните новый захват.")
                try:
                    source, data, stamp, rate, channels, discontinuity = self._queue.get(timeout=.2)
                except queue.Empty:
                    if not self._stopping.is_set() and any(not stream.is_active() for stream in self._streams):
                        raise CaptureError("Аудиоустройство отключилось. Остановите сеанс, обновите устройства и начните новый захват.")
                    continue
                with self._lock:
                    self._pending = max(0.0, self._pending - len(data) / (2 * channels * rate))
                # A dropped block must not be bridged with old filter history or
                # compressed time. Preserve the missing interval in timestamps.
                expected = next_stamp.get(source)
                if source not in resamplers or discontinuity or expected is not None and abs(stamp - expected) > 3:
                    resamplers[source] = StreamingResampler(rate, channels)
                converter = resamplers[source]
                pcm = converter.process(data)
                next_stamp[source] = stamp + len(data) * 1000 / (2 * channels * rate)
                if pcm:
                    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
                    rms = float(np.sqrt(np.mean(samples * samples)))
                    with self._lock:
                        self._stats["output_packets"] += 1
                        self._stats[source + "_rms"] = rms
                    self.on_audio(source, pcm, stamp, OUTPUT_RATE)
                stats = self.metrics
                if (stats["dropped_seconds"] > self._reported_dropped or stats["hardware_overflows"] > self._reported_overflows
                        or stats["clock_discontinuities"] > self._reported_clock_discontinuities):
                    self._reported_dropped, self._reported_overflows = stats["dropped_seconds"], stats["hardware_overflows"]
                    self._reported_clock_discontinuities = stats["clock_discontinuities"]
                    self._emit("capture_gap", message="Часть аудио пропущена из-за задержки устройства или обработки.",
                               dropped_seconds=stats["dropped_seconds"], hardware_overflows=stats["hardware_overflows"],
                               clock_discontinuities=stats["clock_discontinuities"])
        except Exception as error:
            self._failed.set()
            message = str(error) if isinstance(error, CaptureError) else "Ошибка обработки живого звука. Захват остановлен; сохранённый текст доступен экспорту."
            self._emit("capture_error", message=message)
        finally:
            self._emit("capture_stopped", metrics=self.metrics)

    def stop(self, timeout=5.0):
        self._stopping.set()
        for stream in self._streams:
            try:
                stream.stop_stream()
            except (OSError, ValueError):
                pass
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        self._streams.clear()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(0, timeout))
            if self._thread.is_alive():
                raise CaptureError("Обработка звука ещё завершается. Дождитесь завершения перед закрытием окна.")
        if self._manager:
            self._manager.terminate()
            self._manager = None
        # Explicitly release pending PCM on a failed worker.
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        with self._lock:
            self._pending = 0.0
