"""Model installers must stay GitHub-only, transactional and usable from ZIP."""
import hashlib
import io
import subprocess
import sys
import urllib.error

import pytest

from gigaam_zoom import model_assets as assets
from gigaam_zoom.engine import MODEL_FILES
from gigaam_zoom.diarization import SPEAKER_MODELS


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def tiny_manifest(monkeypatch):
    data = b"verified model bytes"
    record = {"release_repository": "AssylbekGizatov/gigaam-zoom-transcriber", "release_tag": "models-v1",
              "artifacts": {"pytorch_model.bin": {"bytes": len(data), "sha256": digest(data),
                            "upstream_url": "https://huggingface.co/must-never-be-called"}}}
    monkeypatch.setattr(assets, "manifest", lambda: record)
    return data


def no_network(*args, **kwargs):
    pytest.fail("Unexpected network access")


def test_release_urls_and_runtime_pins_match():
    record = assets.manifest()
    assert record["artifacts"]["pytorch_model.bin"]["sha256"] == MODEL_FILES["pytorch_model.bin"]
    for spec in SPEAKER_MODELS.values():
        artifact = record["artifacts"][spec.filename]
        assert artifact["sha256"] == spec.sha256
        assert artifact["bytes"] == spec.size_bytes
        assert spec.url == assets.release_url(spec.filename)
    for name in record["artifacts"]:
        assert assets.release_url(name).startswith(
            "https://github.com/AssylbekGizatov/gigaam-zoom-transcriber/releases/download/models-v1/")
    assets.verify_file(assets.ASSETS / "gigaam/config.json", MODEL_FILES["config.json"])


def test_unknown_release_artifact_rejected():
    with pytest.raises(ValueError):
        assets.release_url("../untrusted.bin")


def test_local_copy_and_check_never_use_network(tmp_path, monkeypatch, tiny_manifest):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    (source / "pytorch_model.bin").write_bytes(tiny_manifest)
    monkeypatch.setattr(assets.urllib.request, "urlopen", no_network)
    assets.install_artifact("pytorch_model.bin", target, source_directory=source)
    assert (target / "pytorch_model.bin").read_bytes() == tiny_manifest
    assets.install_artifact("pytorch_model.bin", target, check=True)
    assets.install_artifact("pytorch_model.bin", target)  # idempotent without source
    assert not list(target.glob(".model-*"))


def test_check_missing_does_not_create_directory(tmp_path, monkeypatch, tiny_manifest):
    target = tmp_path / "missing"
    monkeypatch.setattr(assets.urllib.request, "urlopen", no_network)
    with pytest.raises(RuntimeError, match="Missing local file"):
        assets.install_artifact("pytorch_model.bin", target, check=True)
    assert not target.exists()


def test_corrupt_existing_file_is_never_replaced(tmp_path, monkeypatch, tiny_manifest):
    target = tmp_path / "pytorch_model.bin"
    target.write_bytes(b"corrupt")
    monkeypatch.setattr(assets.urllib.request, "urlopen", no_network)
    with pytest.raises(RuntimeError, match="failed size"):
        assets.install_artifact("pytorch_model.bin", tmp_path)
    assert target.read_bytes() == b"corrupt"


def test_download_uses_github_not_provenance_url(tmp_path, monkeypatch, tiny_manifest):
    urls = []
    def download(url, **kwargs):
        urls.append(url)
        return io.BytesIO(tiny_manifest)
    monkeypatch.setattr(assets.urllib.request, "urlopen", download)
    assets.install_artifact("pytorch_model.bin", tmp_path)
    assert urls == [assets.release_url("pytorch_model.bin")]
    assert (tmp_path / "pytorch_model.bin").read_bytes() == tiny_manifest


@pytest.mark.parametrize("bad", [b"truncated", b"x" * 20, b"x" * 100])
def test_bad_download_never_becomes_final_file(tmp_path, monkeypatch, tiny_manifest, bad):
    monkeypatch.setattr(assets.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(bad))
    with pytest.raises(RuntimeError):
        assets.install_artifact("pytorch_model.bin", tmp_path)
    assert not (tmp_path / "pytorch_model.bin").exists()
    assert not list(tmp_path.glob(".model-*"))


def test_network_failure_does_not_fallback(tmp_path, monkeypatch, tiny_manifest):
    urls = []
    def fail(url, **kwargs):
        urls.append(url)
        raise urllib.error.URLError("blocked by corporate proxy")
    monkeypatch.setattr(assets.urllib.request, "urlopen", fail)
    with pytest.raises(RuntimeError, match="No Hugging Face fallback"):
        assets.install_artifact("pytorch_model.bin", tmp_path)
    assert urls == [assets.release_url("pytorch_model.bin")]
    assert not (tmp_path / "pytorch_model.bin").exists()
    assert not list(tmp_path.glob(".model-*"))


def test_local_copy_checks_source(tmp_path, monkeypatch, tiny_manifest):
    source = tmp_path / "source"
    source.mkdir()
    (source / "pytorch_model.bin").write_bytes(b"not the expected artifact")
    monkeypatch.setattr(assets.urllib.request, "urlopen", no_network)
    with pytest.raises(RuntimeError):
        assets.install_artifact("pytorch_model.bin", tmp_path / "target", source_directory=source)
    assert not (tmp_path / "target/pytorch_model.bin").exists()


def test_downloader_cli_works_without_installed_packages_or_git(tmp_path):
    # -S disables site-packages: both commands must need only the standard library.
    for script in ("acquire_model.py", "download_diarization.py"):
        result = subprocess.run([sys.executable, "-S", str(assets.ROOT / "scripts" / script), "--help"],
                                cwd=tmp_path, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert "--check" in result.stdout
        assert "--source-directory" in result.stdout


def test_no_git_or_hub_dependency_in_asr_requirements():
    requirements = (assets.ROOT / "requirements-asr.txt").read_text()
    assert "./vendor/gigaam" in requirements
    assert "git+" not in requirements
    import tomllib
    project = tomllib.loads((assets.ROOT / "vendor/gigaam/pyproject.toml").read_text())["project"]
    assert {x.split("==")[0] for x in project["dependencies"]} == {"hydra-core", "omegaconf"}
    source = assets.ROOT / "vendor/gigaam/gigaam"
    for path in source.glob("*.py"):
        text = path.read_text()
        for forbidden in ("import huggingface_hub", "import transformers", "import requests",
                          "import urllib", "import socket", "torch.hub", "from_pretrained"):
            assert forbidden not in text, f"{path.name}: {forbidden}"


def test_provenance_is_preserved_but_conflicting_identity_is_rejected(tmp_path):
    path = tmp_path / "provenance.json"
    assets.write_provenance(path, {"revision": "pinned", "source": "original"})
    before = path.read_bytes()
    assets.write_provenance(path, {"revision": "pinned", "source": "mirror"})
    assert path.read_bytes() == before
    with pytest.raises(RuntimeError, match="provenance differs"):
        assets.write_provenance(path, {"revision": "different"})
    assert path.read_bytes() == before


def test_check_does_not_write_provenance(tmp_path):
    path = tmp_path / "provenance.json"
    assets.write_provenance(path, {"revision": "pinned"}, check=True)
    assert not path.exists()
