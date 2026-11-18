"""The packaged desktop paths must not require removed native or network adapters."""
import ast
from pathlib import Path


def test_desktop_modules_have_no_removed_dependencies():
    package = Path(__file__).resolve().parents[1] / "gigaam_zoom"
    retired = {"comtypes", "sherpa_onnx", "diarization", "zoom_accessibility", "rtms"}
    assert not any((package / f"{name}.py").exists() for name in retired)
    for path in package.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module] if node.module else [alias.name for alias in node.names]
            else:
                continue
            assert all(not retired.intersection(module.split(".")) for module in modules), path
