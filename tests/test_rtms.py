"""Deterministic protocol sessions. No Zoom credentials or paid calls."""
import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess

import pytest

from gigaam_zoom.rtms import (
    AUDIO_PARAMS, MAX_MESSAGE_BYTES, RTMSClient, RTMSError,
    RTMSProtocolError, _ZoomConnect, generate_signature,
    parse_message, validate_server_url,
)


def audio(timestamp=1100, uid=7, name="Әлия", pcm=b"\x01\x00" * 320):
    return {"msg_type": 14, "content": {
        "user_id": uid, "user_name": name, "timestamp": timestamp,
        "data": base64.b64encode(pcm).decode(), "length": len(pcm),
    }}


class Socket:
    def __init__(self, messages=(), send_hook=None):
        self.incoming = asyncio.Queue()
        for message in messages:
            self.incoming.put_nowait(message)
        self.sent = []
        self.closed = False
        self.send_hook = send_hook

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if self.send_hook:
            self.send_hook(message)
        if message["msg_type"] == 21:
            self.incoming.put_nowait({"msg_type": 22, "status_code": 0})

    async def recv(self):
        message = await self.incoming.get()
        if isinstance(message, Exception):
            raise message
        return json.dumps(message)

    async def close(self):
        self.closed = True
        self.incoming.put_nowait(OSError("simulated closed socket"))


class Context:
    def __init__(self, socket):
        self.socket = socket

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *_):
        await self.socket.close()


def run(coroutine):
    return asyncio.run(coroutine)


def test_signature_matches_documented_hex_hmac():
    assert generate_signature("id", "test-secret", "uuid/+", "stream") == hmac.new(
        b"test-secret", b"id,uuid/+,stream", hashlib.sha256,
    ).hexdigest()


@pytest.mark.parametrize("url", [
    "ws://rtms.zoom.us", "wss://zoom.us.attacker.org", "wss://127.0.0.1",
    "wss://zoom.us:444", "wss://secret@zoom.us", "wss://zoom.us/#fragment",
    "wss://zoom.us.", "https://rtms.zoom.us", "wss://evilzoom.us",
])
def test_ssrf_and_credential_destination_guard(url):
    with pytest.raises(RTMSProtocolError):
        validate_server_url(url)


def test_zoom_endpoint_and_exact_override():
    assert validate_server_url("wss://zoomsjc144-195-19-85zssgw.sjc.zoom.us:443/path")
    assert validate_server_url("wss://localhost/path", ["localhost"])
    with pytest.raises(RTMSProtocolError):
        validate_server_url("wss://a.localhost", ["localhost"])


def test_redirect_destination_checked(monkeypatch):
    from websockets.asyncio.client import connect
    monkeypatch.setattr(connect, "process_redirect", lambda *args: "wss://attacker.example/ws")
    transport = _ZoomConnect("wss://rtms.zoom.us")
    assert isinstance(transport.process_redirect(RuntimeError()), RTMSProtocolError)


@pytest.mark.parametrize("raw", ["[]", "null", '{"msg_type":true}', "{", "x" * (MAX_MESSAGE_BYTES + 1)],
                         ids=["array", "null", "boolean-type", "bad-json", "oversized"])
def test_invalid_and_oversized_messages(raw):
    with pytest.raises(RTMSProtocolError):
        parse_message(raw)


def test_audio_identity_timestamps_replay_overlap_and_mixed():
    async def scenario():
        packets, events = [], []
        client = RTMSClient("id", "secret", packets.append, lambda k, p: events.append((k, p)))
        client.origin_ms = 1000
        await client._audio_message(audio())
        await client._audio_message(audio())  # replay after reconnect
        await client._audio_message(audio(1110))  # 10 ms overlaps previous packet
        await client._audio_message(audio(1300, uid=8, name="Әлия"))  # identical names, different IDs
        await client._audio_message(audio(1500, uid=0, name="Unreliable name"))
        assert [packet.user_id for packet in packets] == ["7", "7", "8", "0"]
        assert packets[1].timestamp_ms == 1120
        assert len(packets[1].pcm) == 320
        assert packets[-1].user_name is None
        assert {k for k, _ in events} >= {"duplicate_audio", "mixed_audio"}
    run(scenario())


@pytest.mark.parametrize("mutation", [
    {"data": "invalid!"}, {"length": 1}, {"timestamp": -1},
    {"user_id": None}, {"data": "AA==", "length": 1},
])
def test_bad_audio_rejected(mutation):
    async def scenario():
        client = RTMSClient("id", "secret", lambda _: None)
        client.origin_ms = 1000
        message = audio()
        message["content"].update(mutation)
        with pytest.raises(RTMSProtocolError):
            await client._audio_message(message)
    run(scenario())


def test_origin_preserved_and_rejoin_gets_new_epoch():
    async def scenario():
        origins, events = [], []
        client = RTMSClient("id", "secret", lambda _: None, lambda k, p: events.append((k, p)), origins.append)
        await client._signal_event({"event": {"event_type": 1, "timestamp": 1000}})
        await client._signal_event({"event": {"event_type": 1, "timestamp": 9000}})
        for event_type in (3, 4, 3):
            await client._signal_event({"event": {"event_type": event_type,
                "timestamp": 1100, "participants": [{"user_id": 7, "user_name": "Алия"}]}})
        assert origins == [1000]
        assert client.origin_ms == 1000
        joins = [p for k, p in events if k == "participant_joined"]
        assert joins[0]["epoch"] != joins[1]["epoch"]
    run(scenario())


def test_complete_protocol_session_and_confirmed_stop():
    async def scenario():
        packets, events, origins = [], [], []
        client = None
        stop_task = None

        def on_audio(packet):
            nonlocal stop_task
            packets.append(packet)
            stop_task = asyncio.create_task(client.stop())

        signaling = Socket([
            {"msg_type": 12, "timestamp": 10},
            {"msg_type": 2, "status_code": 0,
             "media_server": {"server_urls": {"audio": "wss://media.zoom.us"}}},
            {"msg_type": 6, "event": {"event_type": 1, "timestamp": 1000}},
            {"msg_type": 6, "event": {"event_type": 3, "timestamp": 1000,
                                     "participants": [{"user_id": 7, "user_name": "Әлия"}]}},
        ])
        media = Socket([
            {"msg_type": 4, "status_code": 0, "payload_encrypted": False,
             "media_params": {"audio": dict(AUDIO_PARAMS)}},
            {"msg_type": 12, "timestamp": 20}, audio(),
        ])
        client = RTMSClient("id", "test-secret", on_audio,
                            lambda k, p: events.append((k, p)), origins.append, retry_base=0.001)
        client._connect = lambda url: Context(media if "media" in url else signaling)
        await asyncio.wait_for(client.run("uuid", "stream", "wss://rtms.zoom.us"), 5)
        if stop_task:
            await stop_task
        assert len(packets) == 1 and packets[0].user_id == "7"
        assert packets[0].epoch == "stream:1.0"
        assert origins == [1000]
        assert signaling.sent[0]["sequence"] == 1
        assert signaling.sent[0]["signature"] == generate_signature("id", "test-secret", "uuid", "stream")
        assert any(item == {"msg_type": 13, "timestamp": 10} for item in signaling.sent)
        assert any(item == {"msg_type": 13, "timestamp": 20} for item in media.sent)
        assert any(item["msg_type"] == 7 for item in signaling.sent)
        assert any(item["msg_type"] == 21 for item in signaling.sent)
        assert media.sent[0]["media_type"] == 1
        assert media.sent[0]["media_params"]["audio"]["data_opt"] == 2
        assert media.closed and signaling.closed
        assert not any(kind == "stop_unconfirmed" for kind, _ in events)
    run(scenario())


def test_bounded_signaling_retries_and_no_secret_in_error():
    async def scenario():
        events, calls = [], []
        client = RTMSClient("id", "sentinel-secret", lambda _: None,
                            lambda k, p: events.append((k, p)),
                            max_reconnect_attempts=2, retry_base=0.001)

        class FailingContext:
            async def __aenter__(self):
                calls.append(1)
                raise OSError("sentinel-secret should not escape")

            async def __aexit__(self, *_):
                pass

        client._connect = lambda _: FailingContext()
        with pytest.raises(RTMSError) as error:
            await client.run("uuid", "stream", "wss://rtms.zoom.us")
        assert "sentinel-secret" not in str(error.value)
        assert len(calls) == 3
        assert len([k for k, _ in events if k == "reconnecting"]) == 2
    run(scenario())


def test_media_only_reconnection_keeps_signaling_and_origin():
    async def scenario():
        packets, events, connections = [], [], []
        client = RTMSClient("id", "test-secret", packets.append,
                            lambda k, p: events.append((k, p)), retry_base=0.001)
        client._meeting_uuid, client._stream_id = "uuid", "stream"
        client.origin_ms = 1000
        signal = Socket()
        first = Socket([{"msg_type": 4, "status_code": 0}, OSError("network drop")])
        second = Socket([{"msg_type": 4, "status_code": 0}, audio()])

        async def on_audio(packet):
            packets.append(packet)
            client._stopping.set()

        client.on_audio = on_audio

        def connect_media(_):
            connections.append(1)
            return Context(first if len(connections) == 1 else second)

        client._connect = connect_media
        await asyncio.wait_for(client._media_loop("wss://media.zoom.us", signal), 2)
        assert len(packets) == 1 and len(connections) == 2
        assert client.origin_ms == 1000
        assert not signal.closed
        assert [p["connection"] for k, p in events if k == "reconnecting"] == ["audio"]
    run(scenario())


def test_cancellation_disconnects_without_stream_close():
    async def scenario():
        events = []
        connected = asyncio.Event()

        def on_event(kind, payload):
            events.append((kind, payload))
            if kind == "connected":
                connected.set()

        signaling = Socket([
            {"msg_type": 2, "status_code": 0,
             "media_server": {"server_urls": {"audio": "wss://media.zoom.us"}}},
            {"msg_type": 6, "event": {"event_type": 1, "timestamp": 1000}},
        ])
        media = Socket([{"msg_type": 4, "status_code": 0}])
        client = RTMSClient("id", "test-secret", lambda _: None, on_event)
        client._connect = lambda url: Context(media if "media" in url else signaling)
        task = asyncio.create_task(client.run("uuid", "stream", "wss://rtms.zoom.us"))
        await asyncio.wait_for(connected.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not any(item["msg_type"] == 21 for item in signaling.sent)
        assert signaling.closed and media.closed
        assert events[-1][0] == "disconnected"
        assert not any(kind == "stop_unconfirmed" for kind, _ in events)
    run(scenario())


def test_server_failover_switches_url_without_closing_stream():
    async def scenario():
        packets, origins, events = [], [], []
        opened = []
        pending_stop = []
        signaling1 = Socket([
            {"msg_type": 2, "status_code": 0,
             "media_server": {"server_urls": {"audio": "wss://audio1.zoom.us"}}},
            {"msg_type": 6, "event": {"event_type": 1, "timestamp": 1000}},
        ])
        signaling2 = Socket([
            {"msg_type": 2, "status_code": 0,
             "media_server": {"server_urls": {"audio": "wss://audio2.zoom.us"}}},
            {"msg_type": 6, "event": {"event_type": 1, "timestamp": 2000}},
        ])
        media1 = Socket([{"msg_type": 4, "status_code": 0}, audio(1100)])
        media2 = Socket([{"msg_type": 4, "status_code": 0}, audio(1300)])

        async def on_audio(packet):
            packets.append(packet)
            if len(packets) == 1:
                await client.reconnect("wss://signaling2.zoom.us")
            else:
                pending_stop.append(asyncio.create_task(client.stop()))

        client = RTMSClient("id", "test-secret", on_audio,
                            lambda k, p: events.append((k, p)), origins.append,
                            retry_base=0.001)
        sockets = {"wss://signaling1.zoom.us": signaling1,
                   "wss://signaling2.zoom.us": signaling2,
                   "wss://audio1.zoom.us": media1, "wss://audio2.zoom.us": media2}

        def connector(url):
            opened.append(url)
            return Context(sockets[url])

        client._connect = connector
        await asyncio.wait_for(client.run("uuid", "stream", "wss://signaling1.zoom.us"), 5)
        await asyncio.gather(*pending_stop)
        assert origins == [1000] and client.origin_ms == 1000
        assert len(packets) == 2 and packets[0].epoch != packets[1].epoch
        assert "wss://signaling2.zoom.us" in opened
        assert not any(item["msg_type"] == 21 for item in signaling1.sent)
        assert any(item["msg_type"] == 21 for item in signaling2.sent)
    run(scenario())


def test_real_tls_loopback_websocket_session(tmp_path, monkeypatch):
    """Actual TLS/TCP/WebSocket, ephemeral certificate; never a real Zoom call.

    Production endpoint validation requires port443. Skip only when that port or
    an openssl executable is unavailable; fake protocol tests still run in CI.
    """
    from websockets.asyncio.server import serve
    import gigaam_zoom.rtms as rtms_module

    openssl = shutil.which("openssl")
    git_openssl = Path("C:/Program Files/Git/usr/bin/openssl.exe")
    if not openssl and git_openssl.is_file():
        openssl = str(git_openssl)
    if not openssl:
        pytest.skip("openssl unavailable for disposable TLS certificate")
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 443))
    except OSError:
        pytest.skip("loopback port443 unavailable")
    finally:
        probe.close()
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run([
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "1",
        "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
    ], check=True, capture_output=True, timeout=20)
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(str(cert), str(key))
    trusted_context = ssl.create_default_context(cafile=str(cert))
    monkeypatch.setattr(rtms_module.ssl, "create_default_context", lambda: trusted_context)

    async def scenario():
        packets, origins, errors, control = [], [], [], []
        ready = asyncio.Event()
        pending_stop = []

        async def handler(ws):
            try:
                first = json.loads(await ws.recv())
                control.append(first)
                if ws.request.path == "/signal":
                    assert first["msg_type"] == 1
                    await ws.send(json.dumps({"msg_type": 2, "status_code": 0,
                        "media_server": {"server_urls": {"audio": "wss://127.0.0.1/audio"}}}))
                    await ws.send(json.dumps({"msg_type": 6,
                        "event": {"event_type": 1, "timestamp": 1000}}))
                    async for raw in ws:
                        msg = json.loads(raw)
                        control.append(msg)
                        if msg["msg_type"] == 7:
                            ready.set()
                        if msg["msg_type"] == 21:
                            await ws.send(json.dumps({"msg_type": 22, "status_code": 0}))
                else:
                    assert first["msg_type"] == 3
                    await ws.send(json.dumps({"msg_type": 4, "status_code": 0,
                        "payload_encrypted": False, "media_params": {"audio": AUDIO_PARAMS}}))
                    await ready.wait()
                    await ws.send(json.dumps({"msg_type": 12, "timestamp": 123}))
                    keepalive = json.loads(await ws.recv())
                    assert keepalive == {"msg_type": 13, "timestamp": 123}
                    await ws.send(json.dumps(audio()))
                    await ws.wait_closed()
            except Exception as exc:
                errors.append(exc)

        def on_audio(packet):
            packets.append(packet)
            pending_stop.append(asyncio.create_task(client.stop()))

        client = RTMSClient("id", "local-test-secret", on_audio,
                            on_origin=origins.append, allowed_hosts=["127.0.0.1"])
        async with serve(handler, "127.0.0.1", 443, ssl=server_tls):
            await asyncio.wait_for(client.run("test-uuid", "test-stream", "wss://127.0.0.1/signal"), 10)
            await asyncio.gather(*pending_stop)
        assert not errors
        assert origins == [1000] and len(packets) == 1
        assert packets[0].pcm == b"\x01\x00" * 320
        assert any(msg["msg_type"] == 21 for msg in control)
        assert trusted_context.verify_mode == ssl.CERT_REQUIRED
        assert trusted_context.check_hostname
    run(scenario())
