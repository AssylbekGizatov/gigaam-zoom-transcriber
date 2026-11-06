"""Explicit, checksum-pinned GitHub model installation; never used by inference.

Only release_url is used for network access. Original upstream URLs in the
manifest are provenance, not fallback endpoints. Requires only Python stdlib.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"


def manifest():
    return json.loads((ASSETS / "model-manifest.json").read_text(encoding="utf-8"))


def release_url(name):
    record = manifest()
    if name not in record["artifacts"]:
        raise ValueError("Unknown pinned model artifact")
    return (f"https://github.com/{record['release_repository']}/releases/download/"
            f"{record['release_tag']}/{urllib.parse.quote(name)}")


def verify_file(path, sha256, size=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Missing regular model file: {path}")
    if size is not None and path.stat().st_size != size:
        raise RuntimeError(f"Existing {path.name} failed size verification; use a new directory")
    with path.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != sha256:
        raise RuntimeError(f"Existing {path.name} failed SHA256; use a new directory")


def install_file(destination, sha256, *, size=None, source=None, url=None, check=False):
    """Reuse valid files, refuse corrupt ones, and publish only verified bytes.

    A failed download never becomes a final model file. Windows rename refuses
    existing destinations (including FAT/exFAT); POSIX uses atomic hard-link
    creation because rename there could silently replace an existing file.
    """
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        verify_file(destination, sha256, size)
        return
    if check:
        raise RuntimeError(f"Missing local file: {destination}; run installation first")
    if (source is None) == (url is None):
        raise ValueError("Exactly one local source or release URL is required")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".model-", dir=destination.parent) as scratch:
        partial = Path(scratch) / destination.name
        if source is not None:
            verify_file(source, sha256, size)
            shutil.copyfile(source, partial)
        else:
            # Only this repository's HTTPS release is accepted. Normal GitHub
            # redirects to its release CDN are followed; no upstream fallback.
            if url != release_url(destination.name):
                raise ValueError("Only the pinned GitHub release URL is allowed")
            print(f"Downloading {destination.name} from GitHub...", flush=True)
            try:
                with urllib.request.urlopen(url, timeout=120) as response, partial.open("xb") as output:
                    total = 0
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if size is not None and total > size:
                            raise RuntimeError(f"Downloaded {destination.name} exceeds expected size")
                        output.write(chunk)
            except (urllib.error.URLError, TimeoutError) as exc:
                raise RuntimeError(
                    f"GitHub release download failed for {destination.name}. "
                    "Check access to github.com and release-assets.githubusercontent.com, "
                    "or copy approved release files with --source-directory. "
                    "No Hugging Face fallback is used."
                ) from exc
        verify_file(partial, sha256, size)
        try:
            if os.name == "nt":
                partial.rename(destination)
            else:
                os.link(partial, destination)
        except FileExistsError:
            verify_file(destination, sha256, size)


def install_artifact(name, directory, *, source_directory=None, check=False):
    spec = manifest()["artifacts"][name]
    source = Path(source_directory) / name if source_directory is not None else None
    install_file(Path(directory) / name, spec["sha256"], size=spec["bytes"],
                 source=source, url=None if source is not None else release_url(name), check=check)


def write_provenance(path, record, *, check=False):
    """Record installation source without rewriting existing provenance."""
    path = Path(path)
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(previous, dict):
            raise RuntimeError("Existing provenance is not an object; use a new directory")
        for key in ("model", "repository", "revision", "code_revision", "sha256", "license"):
            if key in previous and key in record and previous[key] != record[key]:
                raise RuntimeError("Existing model provenance differs from verified pins; use a new directory")
        return
    if check:
        return  # Older manually copied folders may have no provenance.
    with path.open("x", encoding="utf-8") as output:
        json.dump(record, output, ensure_ascii=False, indent=2)
        output.write("\n")
