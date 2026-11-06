"""Install pinned voice models from this GitHub repository; no Hugging Face."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gigaam_zoom.model_assets import ROOT, install_artifact, manifest, release_url, write_provenance


def acquire(directory, model="campplus", *, source_directory=None, check=False):
    if model not in ("campplus", "wespeaker"):
        raise ValueError("Unknown speaker model")
    name = f"{model}.onnx"
    install_artifact(name, directory, source_directory=source_directory, check=check)
    spec = manifest()["artifacts"][name]
    write_provenance(Path(directory) / f"{model}.provenance.json", {
        "model": model, "url": release_url(name), **spec,
        "notices": "assets/README.md and assets/licenses (included in the project ZIP)",
    }, check=check)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("campplus", "wespeaker"), default="campplus")
    parser.add_argument("--directory", type=Path, default=ROOT / "models/diarization")
    parser.add_argument("--source-directory", type=Path, help="Copy .onnx files from a local directory instead of downloading")
    parser.add_argument("--check", action="store_true", help="Verify existing files only; no network requests or writes")
    args = parser.parse_args()
    try:
        acquire(args.directory, args.model, source_directory=args.source_directory, check=args.check)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"Speaker model installation/check failed: {exc}\n")
    print("Verified local speaker model:", args.directory / f"{args.model}.onnx")


if __name__ == "__main__":
    main()
