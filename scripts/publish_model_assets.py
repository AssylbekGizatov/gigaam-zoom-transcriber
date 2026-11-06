"""Maintainer-only GitHub release staging. Not used by application/setup.

Only this publishing tool fetches original upstream model URLs. Corporate
clients use acquire_model.py / download_diarization.py and the GitHub mirror.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def stage(directory: Path):
    manifest = json.loads((ROOT / "assets/model-manifest.json").read_text(encoding="utf-8"))
    directory.mkdir(parents=True, exist_ok=True)
    for name, spec in manifest["artifacts"].items():
        target = directory / name
        if not target.exists():
            with tempfile.TemporaryDirectory(dir=directory) as temporary:
                partial = Path(temporary) / name
                digest = hashlib.sha256()
                size = 0
                with urllib.request.urlopen(spec["upstream_url"], timeout=120) as source, partial.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        size += len(chunk)
                        if size > spec["bytes"]:
                            raise RuntimeError(f"Unexpected size: {name}")
                        digest.update(chunk)
                        output.write(chunk)
                if size != spec["bytes"] or digest.hexdigest() != spec["sha256"]:
                    raise RuntimeError(f"Upstream checksum/size mismatch: {name}")
                partial.rename(target)
        with target.open("rb") as source:
            if target.stat().st_size != spec["bytes"] or hashlib.file_digest(source, "sha256").hexdigest() != spec["sha256"]:
                raise RuntimeError(f"Existing staged asset mismatch: {name}")
        print(f"Verified {name}: {spec['bytes']} bytes", flush=True)
    for source, name in [
        ("assets/model-manifest.json", "model-manifest.json"),
        ("assets/gigaam/config.json", "config.json"),
        ("assets/gigaam/README.md", "GigaAM-MODEL-CARD.md"),
        ("assets/gigaam/LICENSE", "GigaAM-LICENSE.txt"),
        ("assets/licenses/Apache-2.0.txt", "Apache-2.0.txt"),
        ("assets/licenses/CC-BY-4.0.txt", "CC-BY-4.0.txt"),
        ("assets/README.md", "MODEL-NOTICES.md"),
    ]:
        shutil.copyfile(ROOT / source, directory / name)
    sums = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            with path.open("rb") as source:
                sums.append(f"{hashlib.file_digest(source, 'sha256').hexdigest()}  {path.name}\n")
    (directory / "SHA256SUMS.txt").write_text("".join(sums), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    stage(parser.parse_args().directory)
