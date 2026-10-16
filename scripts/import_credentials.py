"""Import local setup through stdin; never accept credentials on argv or log them.

Used by a trusted local setup workflow after the owner approves their Zoom app.
The input stays in process memory; SecretStore writes only Windows DPAPI bytes.
"""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gigaam_zoom.config import Settings, SecretStore


def main():
    try:
        raw = sys.stdin.buffer.read(8193)
        if len(raw) > 8192:
            raise ValueError
        doc = json.loads(raw)
        allowed = {"client_id", "public_client_id", "client_secret", "event_endpoint", "event_mode", "model_dir"}
        if not isinstance(doc, dict) or set(doc) - allowed or not all(isinstance(v, str) for v in doc.values()):
            raise ValueError
        settings = Settings.load()
        previous_identity = (settings.client_id, settings.public_client_id)
        for key in allowed - {"client_secret"}:
            if key in doc:
                setattr(settings, key, doc[key])
        settings.validate()
        secret = doc.get("client_secret", "")
        if not 8 <= len(secret) <= 4096:
            raise ValueError
        updates = {"client_secret": secret}
        if (settings.client_id, settings.public_client_id) != previous_identity:
            updates["tokens"] = {}
        SecretStore().update(**updates)
        settings.save()
        print("Local setup saved. Credentials encrypted with Windows user-scope DPAPI.")
        return 0
    except Exception:
        print("Local setup import failed; verify input and Windows DPAPI.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
