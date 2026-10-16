"""Authenticated Zoom lifecycle delivery. Only metadata, never meeting audio.

Primary transport: signed webhook through a user-configured HTTPS forwarder.
Experimental account-only WebSocket subscriptions are also implemented, but
their RTMS event availability must be checked in the account's Marketplace UI.
"""
from __future__ import annotations
import asyncio
from collections import OrderedDict
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.parse import quote

from .config import subscription_url

MAX_BODY = 65_536


class _BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


def verify_webhook(body: bytes, timestamp: str, signature: str, secret: str, now=None) -> bool:
    try:
        if len(body) > MAX_BODY or not secret or abs((now or time.time()) - int(timestamp)) > 300:
            return False
    except (ValueError, TypeError):
        return False
    expected = "v0=" + hmac.new(secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def decode_event(raw):
    if isinstance(raw, str):
        if len(raw) > MAX_BODY:
            raise ValueError("Слишком большое событие Zoom.")
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError("Неверное событие Zoom.")
    # WebSocket delivery can wrap the original event in payload.
    if "event" not in raw and "payload" in raw:
        return decode_event(raw["payload"])
    if not isinstance(raw.get("event"), str) or not isinstance(raw.get("payload"), dict):
        raise ValueError("Неверное событие Zoom.")
    return raw


class WebhookReceiver:
    def __init__(self, secret: str, on_event, port=8765):
        if not secret:
            raise ValueError("Укажите Webhook Secret Token в настройках приложения.")
        self.secret, self.on_event, self.port = secret, on_event, port
        self.server = None
        self.thread = None
        self.seen = OrderedDict()
        self.lock = threading.Lock()

    def start(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def reply(self, code, doc=None):
                value = json.dumps(doc or {}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(value)))
                self.end_headers()
                self.wfile.write(value)

            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if self.path != "/webhook" or not 0 < length <= MAX_BODY:
                        return self.reply(413 if length > MAX_BODY else 400)
                    body = self.rfile.read(length)
                    if not verify_webhook(body, self.headers.get("x-zm-request-timestamp", ""),
                                          self.headers.get("x-zm-signature", ""), owner.secret):
                        return self.reply(401)
                    event = decode_event(json.loads(body))
                    if event["event"] == "endpoint.url_validation":
                        token = event["payload"].get("plainToken", "")
                        if not isinstance(token, str) or len(token) > 1024:
                            return self.reply(400)
                        return self.reply(200, {"plainToken": token, "encryptedToken": hmac.new(
                            owner.secret.encode(), token.encode(), hashlib.sha256).hexdigest()})
                    digest = hashlib.sha256(body).digest()
                    with owner.lock:
                        if digest in owner.seen:
                            return self.reply(200)
                        owner.on_event(event)
                        owner.seen[digest] = time.monotonic()
                        while len(owner.seen) > 1024:
                            owner.seen.popitem(last=False)
                    self.reply(200)
                except (ValueError, KeyError, TypeError, UnicodeError, TimeoutError):
                    self.reply(400)
                except Exception:
                    # Zoom retries; never include event payload or internal details.
                    self.reply(503)

        self.server = _BoundedHTTPServer(("127.0.0.1", int(self.port)), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, name="zoom-webhook", daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread:
            self.thread.join(timeout=3)
            self.thread = None


async def websocket_events(endpoint, token_provider, on_event, on_status, stop, ready):
    """Account-only optional delivery. Events missed during outages aren't replayed."""
    from websockets.asyncio.client import connect
    endpoint = subscription_url(endpoint)
    attempts = 0
    while not stop.is_set() and attempts < 5:
        try:
            token = await asyncio.to_thread(token_provider)
            url = endpoint + "&access_token=" + quote(token, safe="")
            async with connect(url, proxy=None, max_size=MAX_BODY, max_queue=8, open_timeout=15,
                               ping_interval=20, close_timeout=2) as socket:
                ready.set()
                on_status("events_connected", {})
                renewed_at = time.monotonic()
                while not stop.is_set() and time.monotonic() - renewed_at < 3300:
                    try:
                        raw = await asyncio.wait_for(socket.recv(), 25)
                    except asyncio.TimeoutError:
                        await socket.send('{"module":"heartbeat"}')
                        continue
                    doc = json.loads(raw)
                    if isinstance(doc, dict) and doc.get("module") == "heartbeat":
                        continue
                    on_event(decode_event(doc))
        except Exception:
            attempts += 1
            on_status("events_reconnecting", {"attempt": attempts,
                "message": "Доставка событий прервана; Zoom не повторяет пропущенные события WebSocket."})
            try:
                await asyncio.wait_for(stop.wait(), min(2 ** attempts, 20))
            except asyncio.TimeoutError:
                pass
    if attempts >= 5:
        raise RuntimeError("Не удалось восстановить доставку событий Zoom.")
