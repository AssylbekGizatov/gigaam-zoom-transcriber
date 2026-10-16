"""Zoom lifecycle Event WebSocket tests; no credentials or paid Zoom calls."""
import asyncio
import json
import ssl
from pathlib import Path
import shutil
import subprocess

import pytest

from gigaam_zoom.events import MAX_BODY, websocket_events

ENDPOINT = "wss://ws.zoom.us/ws?subscriptionId=test-subscription"
BUILD_OK = {"module": "build_connection", "success": True, "content": "success"}
HEARTBEAT_OK = {"module": "heartbeat", "success": True, "content": "live"}
LIFECYCLE = {"event": "meeting.rtms_started", "event_ts": 1,
             "payload": {"meeting_id": 123, "meeting_uuid": "test-uuid",
                         "rtms_stream_id": "test-stream", "server_urls": "wss://rtms.zoom.us"}}


class FakeSocket:
    def __init__(self, messages=(), send_hook=None):
        self.incoming = asyncio.Queue()
        for msg in messages:
            self.incoming.put_nowait(msg)
        self.sent = []
        self.closed = False
        self.send_hook = send_hook

    async def recv(self):
        value = await self.incoming.get()
        if isinstance(value, BaseException):
            raise value
        return json.dumps(value) if not isinstance(value, (str, bytes)) else value

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if self.send_hook:
            self.send_hook(message)

    async def close(self):
        self.closed = True


class FakeConnect:
    factory = None
    opened = []

    def __init__(self, url, **kwargs):
        self.url, self.kwargs = url, kwargs
        self.socket = self.factory(url, kwargs)
        self.opened.append(self)

    async def __aenter__(self):
        if isinstance(self.socket, BaseException):
            raise self.socket
        return self.socket

    async def __aexit__(self, *_):
        await self.socket.close()

    def process_redirect(self, error):
        return "wss://attacker.example/ws?access_token=must-not-escape"


def install_fake(monkeypatch, factory):
    import websockets.asyncio.client
    FakeConnect.factory = staticmethod(factory)
    FakeConnect.opened = []
    monkeypatch.setattr(websockets.asyncio.client, "connect", FakeConnect)


def execute(coroutine):
    return asyncio.run(coroutine)


def test_ready_requires_successful_build_connection_and_control_not_dispatched(monkeypatch):
    async def scenario():
        ready, stop = asyncio.Event(), asyncio.Event()
        source = FakeSocket()
        install_fake(monkeypatch, lambda *_: source)
        received, statuses = [], []

        def event(doc):
            received.append(doc)
            stop.set()

        task = asyncio.create_task(websocket_events(ENDPOINT, lambda: "test/token", event,
            lambda k, p: statuses.append((k, p)), stop, ready))
        await asyncio.sleep(.03)
        assert not ready.is_set() and not statuses
        source.incoming.put_nowait(BUILD_OK)
        source.incoming.put_nowait(HEARTBEAT_OK)
        await asyncio.wait_for(ready.wait(), 1)
        source.incoming.put_nowait(LIFECYCLE)
        await asyncio.wait_for(task, 1)
        assert received == [LIFECYCLE]
        assert [k for k, _ in statuses] == ["events_connected"]
        assert source.closed and not ready.is_set()
        connection = FakeConnect.opened[0]
        assert "test%2Ftoken" in connection.url
        assert connection.kwargs["max_size"] == MAX_BODY
        assert connection.kwargs["proxy"] is None
        redirect = connection.process_redirect(RuntimeError())
        assert isinstance(redirect, RuntimeError) and "must-not-escape" not in str(redirect)
    execute(scenario())


@pytest.mark.parametrize("frame", [LIFECYCLE, {"payload": LIFECYCLE},
    {"module": "event", "content": LIFECYCLE},
    {"module": "event", "content": json.dumps(LIFECYCLE)}],
    ids=["direct", "payload", "content-object", "content-string"])
def test_bounded_lifecycle_envelopes(monkeypatch, frame):
    async def scenario():
        ready, stop = asyncio.Event(), asyncio.Event()
        source = FakeSocket([BUILD_OK, frame])
        install_fake(monkeypatch, lambda *_: source)
        received = []

        def event(doc):
            received.append(doc)
            stop.set()

        await websocket_events(ENDPOINT, lambda: "test-token", event, lambda *_: None, stop, ready)
        assert received == [LIFECYCLE]
    execute(scenario())


@pytest.mark.parametrize("messages", [
    [{"module": "build_connection", "success": False, "content": "private-secret"}],
    [LIFECYCLE], [BUILD_OK, "x" * (MAX_BODY + 1)],
    [BUILD_OK, {"module": "heartbeat", "success": False, "content": "private-secret"}],
], ids=["rejected-build", "event-before-build", "oversized", "rejected-heartbeat"])
def test_failed_or_malformed_connections_have_bounded_retries_no_secrets(monkeypatch, messages):
    async def scenario():
        ready, stop = asyncio.Event(), asyncio.Event()
        install_fake(monkeypatch, lambda *_: FakeSocket(messages))
        statuses = []
        with pytest.raises(RuntimeError) as error:
            await websocket_events(ENDPOINT, lambda: "private-secret", lambda *_: None,
                lambda k, p: statuses.append((k, p)), stop, ready,
                max_attempts=2, retry_base_seconds=.001)
        assert len(FakeConnect.opened) == 2
        assert "private-secret" not in str(error.value) + str(statuses)
        assert not ready.is_set()
    execute(scenario())


def test_busy_event_traffic_does_not_suppress_application_heartbeats(monkeypatch):
    async def scenario():
        ready, stop = asyncio.Event(), asyncio.Event()
        source = FakeSocket([BUILD_OK])
        received = []
        heartbeats = []

        def send_hook(message):
            heartbeats.append(message)
            source.incoming.put_nowait(HEARTBEAT_OK)
            if len(heartbeats) >= 3:
                stop.set()

        source.send_hook = send_hook
        install_fake(monkeypatch, lambda *_: source)

        async def produce():
            await ready.wait()
            while not stop.is_set():
                source.incoming.put_nowait(LIFECYCLE)
                await asyncio.sleep(.001)

        producer = asyncio.create_task(produce())
        await asyncio.wait_for(websocket_events(ENDPOINT, lambda: "test-token", received.append,
            lambda *_: None, stop, ready, heartbeat_seconds=.02), 1)
        await producer
        assert len(heartbeats) == 3 and len(received) > 10
        assert all(frame == {"module": "heartbeat"} for frame in heartbeats)
    execute(scenario())


def test_token_renewal_reconnects_without_using_failure_budget(monkeypatch):
    async def scenario():
        ready, stop = asyncio.Event(), asyncio.Event()
        tokens, statuses = [], []
        install_fake(monkeypatch, lambda *_: FakeSocket([BUILD_OK]))

        def token_provider():
            token = f"test-token-{len(tokens)}"
            tokens.append(token)
            return token

        def status(kind, payload):
            statuses.append(kind)
            if statuses.count("events_connected") == 3:
                stop.set()

        await asyncio.wait_for(websocket_events(ENDPOINT, token_provider, lambda *_: None,
            status, stop, ready, renew_seconds=.02, max_attempts=1), 1)
        assert len(tokens) == 3
        assert statuses.count("events_renewing") == 2
        assert "events_reconnecting" not in statuses
        assert all(connection.socket.closed for connection in FakeConnect.opened)
    execute(scenario())


def test_stop_interrupts_wait_for_build_and_backoff(monkeypatch):
    async def scenario():
        ready, stop = asyncio.Event(), asyncio.Event()
        source = FakeSocket()
        install_fake(monkeypatch, lambda *_: source)
        task = asyncio.create_task(websocket_events(ENDPOINT, lambda: "test-token", lambda *_: None,
            lambda *_: None, stop, ready))
        await asyncio.sleep(.02)
        stop.set()
        await asyncio.wait_for(task, .5)
        assert source.closed and not ready.is_set()

        stop = asyncio.Event()
        install_fake(monkeypatch, lambda *_: OSError("simulated offline"))

        def failed(kind, _):
            if kind == "events_reconnecting":
                stop.set()

        await asyncio.wait_for(websocket_events(ENDPOINT, lambda: "test-token", lambda *_: None,
            failed, stop, ready, retry_base_seconds=20), .5)
        assert len(FakeConnect.opened) == 1
    execute(scenario())


def test_build_timeout_cannot_claim_ready(monkeypatch):
    async def scenario():
        ready, stop = asyncio.Event(), asyncio.Event()
        install_fake(monkeypatch, lambda *_: FakeSocket())
        statuses = []
        with pytest.raises(RuntimeError):
            await websocket_events(ENDPOINT, lambda: "test-token", lambda *_: None,
                lambda kind, _: statuses.append(kind), stop, ready,
                build_timeout_seconds=.01, max_attempts=1)
        assert "events_connected" not in statuses and not ready.is_set()
    execute(scenario())


def test_event_websocket_real_tls_loopback(tmp_path, monkeypatch):
    """Real local WSS with trusted disposable certificate; not Zoom service."""
    from websockets.asyncio.client import connect as RealConnect
    from websockets.asyncio.server import serve
    import websockets.asyncio.client
    import gigaam_zoom.events as events_module

    openssl = shutil.which("openssl")
    git_openssl = Path("C:/Program Files/Git/usr/bin/openssl.exe")
    if not openssl and git_openssl.is_file():
        openssl = str(git_openssl)
    if not openssl:
        pytest.skip("openssl unavailable for disposable certificate")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=127.0.0.1",
        "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True, timeout=20)
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(str(cert), str(key))
    client_tls = ssl.create_default_context(cafile=str(cert))
    # Test-only routing replaces endpoint normalization, never TLS validation.
    monkeypatch.setattr(events_module, "subscription_url", lambda value: value)

    class LocalConnect(RealConnect):
        def __init__(self, uri, **kwargs):
            super().__init__(uri, ssl=client_tls, **kwargs)

    monkeypatch.setattr(websockets.asyncio.client, "connect", LocalConnect)

    async def scenario():
        ready, stop = asyncio.Event(), asyncio.Event()
        received, statuses, errors = [], [], []

        async def handler(ws):
            try:
                assert "access_token=test-token" in ws.request.path
                await ws.send(json.dumps(BUILD_OK))
                heartbeat = json.loads(await ws.recv())
                assert heartbeat == {"module": "heartbeat"}
                await ws.send(json.dumps(HEARTBEAT_OK))
                await ws.send(json.dumps({"module": "event", "content": LIFECYCLE}))
                await ws.wait_closed()
            except Exception as error:
                errors.append(type(error).__name__)

        def on_event(doc):
            assert ready.is_set()
            received.append(doc)
            stop.set()

        async with serve(handler, "127.0.0.1", 0, ssl=server_tls) as server:
            port = server.sockets[0].getsockname()[1]
            await asyncio.wait_for(websocket_events(f"wss://127.0.0.1:{port}/ws?subscriptionId=test",
                lambda: "test-token", on_event, lambda k, p: statuses.append(k), stop, ready,
                heartbeat_seconds=.02), 3)
        assert not errors and received == [LIFECYCLE]
        assert statuses == ["events_connected"]
        assert client_tls.verify_mode == ssl.CERT_REQUIRED and client_tls.check_hostname
    execute(scenario())
