"""Public settings and Windows user-bound encrypted credentials."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit, parse_qs


def data_dir() -> Path:
    path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GigaAMZoom"
    path.mkdir(parents=True, exist_ok=True)
    return path


def local_directory(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir() or str(value).startswith(("\\\\", "//")) or str(path).startswith(("\\\\", "//")):
        raise ValueError("Выберите существующую папку на локальном диске.")
    if os.name == "nt" and ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) == 4:
        raise ValueError("Сетевой диск не подходит: выберите локальную папку.")
    return path


def meeting_id(value: str) -> str:
    value = value.strip()
    if value.startswith("https://"):
        parsed = urlsplit(value)
        if not (parsed.hostname == "zoom.us" or (parsed.hostname or "").endswith(".zoom.us")):
            raise ValueError("Укажите ссылку zoom.us или цифровой ID встречи.")
        parts = parsed.path.strip("/").split("/")
        if len(parts) != 2 or parts[0] not in {"j", "s"}:
            raise ValueError("Нужна ссылка вида https://…zoom.us/j/12345678901.")
        value = parts[1]
    value = value.replace(" ", "").replace("-", "")
    if not value.isascii() or not value.isdigit() or not 9 <= len(value) <= 11:
        raise ValueError("ID встречи должен содержать 9–11 цифр.")
    return value


def meeting_origin(value: str) -> int | None:
    if not value.strip():
        return None
    try:
        actual = datetime.strptime(value.strip(), "%d.%m.%Y %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=5)))
    except ValueError:
        raise ValueError("Начало встречи: ДД.ММ.ГГГГ ЧЧ:ММ:СС, время UTC+05:00. Можно оставить пустым.") from None
    return round(actual.timestamp() * 1000)


def subscription_url(value: str) -> str:
    parsed = urlsplit(value)
    query = parse_qs(parsed.query)
    if (parsed.scheme != "wss" or parsed.hostname != "ws.zoom.us" or parsed.path != "/ws"
            or parsed.username or parsed.password or parsed.fragment
            or parsed.port not in {None, 443} or set(query) != {"subscriptionId"}
            or len(query["subscriptionId"]) != 1):
        raise ValueError("Скопируйте WebSocket Endpoint URL из Zoom Marketplace, без access_token.")
    return value


@dataclass
class Settings:
    client_id: str = ""
    public_client_id: str = ""
    event_endpoint: str = ""
    event_mode: str = "webhook"
    webhook_port: int = 8765
    model_dir: str = "models/gigaam"
    export_dir: str = ""

    def validate(self):
        if not self.client_id.strip() or not self.public_client_id.strip():
            raise ValueError("Заполните Client ID и Public Client ID вашего General App.")
        if self.event_mode == "websocket":
            subscription_url(self.event_endpoint)
        elif self.event_mode != "webhook":
            raise ValueError("Неизвестный способ доставки событий Zoom.")
        if not 1024 <= int(self.webhook_port) <= 65535:
            raise ValueError("Порт webhook должен быть от 1024 до 65535.")
        local_directory(self.model_dir)
        if self.export_dir:
            local_directory(self.export_dir)

    @classmethod
    def load(cls, path=None):
        target = Path(path) if path else data_dir() / "settings.json"
        if not target.exists():
            return cls()
        doc = json.loads(target.read_text(encoding="utf-8"))
        return cls(**{key: doc[key] for key in cls.__dataclass_fields__ if key in doc})

    def save(self, path=None):
        target = Path(path) if path else data_dir() / "settings.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _crypt(payload: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Сохранение секретов поддерживается только Windows DPAPI.")
    buffer = ctypes.create_string_buffer(payload)
    source = _Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    result = _Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt.CryptProtectData.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    crypt.CryptProtectData.restype = wintypes.BOOL
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # CRYPTPROTECT_UI_FORBIDDEN, user scope (never machine scope).
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), "GigaAM Zoom", None, None, None, 1, ctypes.byref(result))
    if not ok:
        raise RuntimeError("Windows DPAPI не смог обработать секреты текущего пользователя.")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel.LocalFree(result.pbData)


class SecretStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else data_dir() / "credentials.dpapi"
        self.lock = threading.RLock()

    def load(self):
        with self.lock:
            if not self.path.exists():
                return {}
            return json.loads(_crypt(self.path.read_bytes(), decrypt=True))

    def update(self, **values):
        with self.lock:
            doc = self.load()
            doc.update(values)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_bytes(_crypt(json.dumps(doc).encode("utf-8")))
            temporary.replace(self.path)

    def clear_tokens(self):
        with self.lock:
            self.update(tokens={})
