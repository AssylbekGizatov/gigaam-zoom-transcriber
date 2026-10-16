"""Check tracked publication only; no scanning of user credentials or meetings."""
from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parents[1]
files = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
for name in filter(None, files):
    path = Path(name)
    if (path.suffix.lower() in {".wav", ".flac", ".mp3", ".mp4", ".sqlite", ".dpapi", ".db"}
            or name.startswith(("models/", "sessions/", ".env")) and name != ".env.example"
            or path.name in {"settings.json", "credentials.json"}):
        raise SystemExit("Private/runtime artifact is tracked: " + name)
    data = (root / path).read_bytes()
    if re.search(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)", data):
        raise SystemExit("Credential pattern in tracked file: " + name)
print(f"Publication guard passed: {len(files)-1} tracked files. No runtime audio, models, databases or credential files.")
