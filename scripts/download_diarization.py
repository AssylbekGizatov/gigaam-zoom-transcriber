"""Explicit one-time public model download; inference itself stays offline."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gigaam_zoom.diarization import SPEAKER_MODELS


def main():
    parser = argparse.ArgumentParser(description="Download pinned local voice-clustering model (CAM++ 28.3 MB)")
    parser.add_argument("--model", choices=tuple(SPEAKER_MODELS), default="campplus")
    parser.add_argument("--directory", type=Path,
                        default=Path(__file__).resolve().parents[1] / "models" / "diarization")
    args = parser.parse_args()
    spec = SPEAKER_MODELS[args.model]
    args.directory.mkdir(parents=True, exist_ok=True)
    destination = args.directory / spec.filename
    temporary = None
    try:
        digest = hashlib.sha256()
        size = 0
        with tempfile.NamedTemporaryFile(dir=args.directory, suffix=".part", delete=False) as output:
            temporary = Path(output.name)
            with urllib.request.urlopen(spec.url, timeout=60) as response:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > spec.size_bytes:
                        raise RuntimeError("Unexpected model size")
                    digest.update(chunk)
                    output.write(chunk)
        if size != spec.size_bytes or digest.hexdigest() != spec.sha256:
            raise RuntimeError("Model checksum verification failed")
        temporary.replace(destination)
        (args.directory / f"{spec.model_id}.provenance.json").write_text(json.dumps({
            "model": spec.model_id,
            "url": spec.url, "sha256": spec.sha256, "bytes": spec.size_bytes,
            "license": spec.license, "license_url": spec.license_url,
            "model_card": spec.model_card, "attribution": spec.attribution,
            "modifications": "ONNX conversion by sherpa-onnx; no additional model modifications here",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("Проверенная модель голосов установлена:", destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


if __name__ == "__main__":
    main()
