"""Transcribe Zoom's local Audio Record directory without Zoom API or RTMS."""

import argparse
from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaam_zoom.config import local_directory
from gigaam_zoom.engine import Transcriber
from gigaam_zoom.exports import EXPORT_FORMATS, export_transcript
from gigaam_zoom.models import AudioPacket
from gigaam_zoom.pipeline import Pipeline
from gigaam_zoom.store import Store


def display_name(path):
    match = re.fullmatch(r"audio[ _-]*(.*?)[ _-]*\d+", path.stem, re.I)
    return (match[1].strip() if match else path.stem) or "Имя неизвестно"


def import_tracks(tracks, output, engine, ffmpeg):
    engine.load()
    output.mkdir(exist_ok=False)
    store = Store(output / "recording.sqlite")
    try:
        store.set_metadata("transport", "Zoom computer recording, separate tracks")
        for index, path in enumerate(tracks, 1):
            print(f"Обработка дорожки {index}/{len(tracks)}", flush=True)
            failed = []

            def status(kind, payload):
                if kind in {"inference_failed", "store_error"} and not failed:
                    failed.append(kind)

            pipeline = Pipeline(store, engine, status, origin_ms=0)
            store.set_metadata("time_basis", "track")
            store.set_metadata("clock_origin_kind", "start_of_each_audio_file")
            pipeline.start()
            try:
                with subprocess.Popen(
                    [ffmpeg, "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
                     "-f", "mov", "-i", str(path), "-map", "0:a:0", "-ac", "1",
                     "-ar", "16000", "-f", "s16le", "pipe:1"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                ) as decoder:
                    frames = 0
                    try:
                        while pcm := decoder.stdout.read(3200):
                            while pipeline.pending_seconds > 16 and not failed:
                                time.sleep(.02)
                            if failed or len(pcm) % 2 or not pipeline.feed(AudioPacket(
                                f"track-{index}", display_name(path), frames // 16,
                                pcm, epoch="local-recording",
                            )):
                                raise RuntimeError(f"Ошибка обработки дорожки {index}.")
                            frames += len(pcm) // 2
                        if decoder.wait() or not frames:
                            raise RuntimeError(f"Не удалось декодировать дорожку {index}.")
                    finally:
                        if decoder.poll() is None:
                            decoder.kill()
            finally:
                pipeline.stop()
            if failed or pipeline.failure or pipeline.metrics["dropped_seconds"]:
                raise RuntimeError(f"Дорожка {index} обработана с потерями; проверьте SQLite.")
        for kind in EXPORT_FORMATS:
            export_transcript(store, output / f"transcript.{kind.lower()}", kind)
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio_record", type=Path)
    parser.add_argument("output", type=Path, help="New directory on a local disk")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable path")
    args = parser.parse_args()
    source = local_directory(str(args.audio_record))
    output = local_directory(str(args.output.parent)) / args.output.name
    if output.exists():
        parser.error("Папка результата уже существует.")
    tracks = sorted(p for p in source.glob("*.m4a") if p.is_file())
    if not tracks:
        parser.error("Укажите папку Audio Record с отдельными M4A участников.")
    import_tracks(tracks, output, Transcriber(args.model_dir), args.ffmpeg)
    print("Готово: SQLite и документы сохранены локально.")


if __name__ == "__main__":
    main()
