"""Install verified GigaAM from this repository's GitHub release, never a hub."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gigaam_zoom.model_assets import ASSETS, ROOT, install_artifact, install_file, manifest, release_url, write_provenance

REVISION = "2f8a57144e6ec3adfd32fe0484d9ea9913305bc8"
CODE_REVISION = "7447938d791c4f3e643386ee22c33777004293a5"
FILES = {
    "config.json": "c830232c7d51688a630a221517b52585ab5ee57e1d3c21bcbae01759351d2653",
    "pytorch_model.bin": "e1db43873ec5e296f229572e06e2470fc157ac9f8d4aacabda295630b9b91728",
    "LICENSE": "f00de6715714c7a63d08639cdbfaa40224eefc407302614bd19f1a8b98c875aa",
}


def acquire(directory, *, source_directory=None, check=False):
    directory = Path(directory)
    for name in ("config.json", "LICENSE"):
        install_file(directory / name, FILES[name], source=ASSETS / "gigaam" / name, check=check)
    install_artifact("pytorch_model.bin", directory, source_directory=source_directory, check=check)
    write_provenance(directory / "provenance.json", {
        "backend": "gigaam", "repository": "ai-sage/GigaAM-Multilingual",
        "revision": REVISION, "code_revision": CODE_REVISION, "license": "MIT", "sha256": FILES,
        "distribution": release_url("pytorch_model.bin"),
        "artifact_sources": manifest()["artifacts"],
        "model_card": "assets/gigaam/README.md (included in the project ZIP)",
    }, check=check)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "models/gigaam")
    parser.add_argument("--source-directory", type=Path, help="Copy weights from this local directory instead of downloading")
    parser.add_argument("--check", action="store_true", help="Verify existing files only; make no network requests or writes")
    args = parser.parse_args()
    try:
        acquire(args.directory, source_directory=args.source_directory, check=args.check)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"Model installation/check failed: {exc}\n")
    print("Verified local GigaAM model:", args.directory)


if __name__ == "__main__":
    main()
