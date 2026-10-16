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


async def websocket_events(endpoint, token_provider, on_event, on_status, stop, ready,
                           *, heartbeat_seconds=30.0, renew_seconds=3300.0,
                           build_timeout_seconds=15.0, max_attempts=5,
                           retry_base_seconds=2.0, stable_seconds=60.0):
    """Receive account-bound lifecycle events over Zoom's outgoing Event WSS.

    ``ready`` means Zoom accepted ``build_connection``, not merely HTTP101.
    Application heartbeats run independently of event traffic. Renewing a token
    briefly reconnects; Zoom does not replay events missed during any outage.
    Optional timing parameters permit deterministic, fast protocol tests.
    """
    import inspect
    from websockets.asyncio.client import connect

    endpoint = subscription_url(endpoint)
    if (heartbeat_seconds <= 0 or renew_seconds <= 0 or build_timeout_seconds <= 0
            or not 1 <= max_attempts <= 20 or retry_base_seconds < 0 or stable_seconds <= 0):
        raise ValueError("Некорректные интервалы доставки событий Zoom.")

    class EventConnect(connect):
        def process_redirect(self, exc):
            target = super().process_redirect(exc)
            # The subscription endpoint is fixed. Don't let bearer tokens in
            # the URL follow an unexpected redirect to another service.
            if isinstance(target, str):
                return RuntimeError("Перенаправление Event WebSocket отклонено.")
            return target

    async def invoke(callback, *args):
        result = callback(*args)
        if inspect.isawaitable(result):
            await result

    async def receive(socket):
        raw = await socket.recv()
        if not isinstance(raw, (str, bytes)) or len(raw) > MAX_BODY:
            raise ValueError("Неверный размер события Zoom.")
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError("Неверный формат события Zoom.")
        return document

    async def session():
        token = await asyncio.to_thread(token_provider)
        if not isinstance(token, str) or not token or len(token) > 16_384:
            raise ValueError("Не удалось получить токен доставки событий Zoom.")
        url = endpoint + "&access_token=" + quote(token, safe="")
        async with EventConnect(url, proxy=None, max_size=MAX_BODY, max_queue=8,
                                open_timeout=15, ping_interval=20, close_timeout=2,
                                happy_eyeballs_delay=.25) as socket:
            deadline = time.monotonic() + build_timeout_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Zoom не подтвердил Event WebSocket.")
                doc = await asyncio.wait_for(receive(socket), remaining)
                if doc.get("module") == "build_connection":
                    if doc.get("success") is not True:
                        raise RuntimeError("Zoom отклонил подключение доставки событий.")
                    break
                if doc.get("module") != "heartbeat":
                    raise ValueError("Событие пришло до авторизации Event WebSocket.")
            ready.set()
            await invoke(on_status, "events_connected", {})
            last_heartbeat_ack = time.monotonic()

            async def heartbeat():
                while True:
                    await asyncio.sleep(heartbeat_seconds)
                    if time.monotonic() - last_heartbeat_ack > heartbeat_seconds * 2 + 10:
                        raise TimeoutError("Нет подтверждения heartbeat Zoom.")
                    await socket.send('{"module":"heartbeat"}')

            async def reader():
                nonlocal last_heartbeat_ack
                while True:
                    doc = await receive(socket)
                    module = doc.get("module")
                    if module in {"heartbeat", "build_connection"}:
                        if doc.get("success") is not True:
                            raise RuntimeError("Zoom отклонил контрольное сообщение Event WebSocket.")
                        if module == "heartbeat":
                            last_heartbeat_ack = time.monotonic()
                        continue
                    # Direct events and the documented event payload shape are
                    # primary. Some event gateways wrap that same JSON in
                    # content; unwrap only a bounded string/object, never code.
                    if "event" not in doc and "payload" not in doc and "content" in doc:
                        doc = doc["content"]
                    await invoke(on_event, decode_event(doc))

            tasks = [asyncio.create_task(reader()), asyncio.create_task(heartbeat()),
                     asyncio.create_task(asyncio.sleep(renew_seconds))]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                return "renew"
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    attempts = 0
    stopped = asyncio.create_task(stop.wait())
    ready.clear()
    try:
        while not stop.is_set():
            opened_at = time.monotonic()
            current = asyncio.create_task(session())
            try:
                done, _ = await asyncio.wait([current, stopped], return_when=asyncio.FIRST_COMPLETED)
                if stopped in done:
                    break
                outcome = current.result()
                if outcome == "renew":
                    attempts = 0
                    await invoke(on_status, "events_renewing", {
                        "message": "Обновляется токен событий; пропущенные события Zoom не повторяются.",
                    })
            except asyncio.CancelledError:
                raise
            except Exception:
                # A stable connection resets the consecutive failure budget;
                # rapid HTTP101/build/disconnect loops remain strictly bounded.
                was_ready = ready.is_set()
                ready.clear()
                if was_ready and time.monotonic() - opened_at >= stable_seconds:
                    attempts = 0
                attempts += 1
                await invoke(on_status, "events_reconnecting", {"attempt": attempts,
                    "message": "Доставка событий прервана; Zoom не повторяет пропущенные события WebSocket."})
                if attempts >= max_attempts:
                    raise RuntimeError("Не удалось восстановить доставку событий Zoom.") from None
                try:
                    await asyncio.wait_for(stop.wait(), min(retry_base_seconds * 2**(attempts - 1), 20))
                except asyncio.TimeoutError:
                    pass
            finally:
                ready.clear()
                current.cancel()
                await asyncio.gather(current, return_exceptions=True)
    finally:
        ready.clear()
        stopped.cancel()
        await asyncio.gather(stopped, return_exceptions=True)
