import asyncio
import threading

import pytest
from gigaam_zoom import controller as module
from gigaam_zoom.config import Settings
from gigaam_zoom.models import AudioPacket, Word


class Vault:
    def load(self):
        return {"client_secret": "test-only", "webhook_secret": "test-only"}


class Engine:
    def __init__(self, *_):
        pass
    def load(self):
        pass
    def transcribe_words(self, values, rate):
        return [Word(0, len(values) / rate, "Тест қазақ English")]


class Client:
    instances = []
    def __init__(self, client_id, secret, audio, event, origin):
        self.audio, self.event, self.origin = audio, event, origin
        self.closed = asyncio.Event()
        self._stream_id = ""
        self.reconnections = []
        self.__class__.instances.append(self)
    async def run(self, uuid, stream, server):
        self._stream_id = stream
        self.origin(1_000_000)
        self.audio(AudioPacket("42", "Same name", 1_000_000, b"\x10\x10" * 3200, epoch="1.0"))
        self.event("connected", {})
        await self.closed.wait()
    async def stop(self):
        self.closed.set()
    async def reconnect(self, url):
        self.reconnections.append(url)


def make(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "Transcriber", Engine)
    monkeypatch.setattr(module, "RTMSClient", Client)
    events = []
    c = module.Controller(Settings(client_id="example", public_client_id="public", model_dir=str(tmp_path),
        export_dir=str(tmp_path)), Vault(), lambda kind, payload: events.append((kind, payload)))
    c._active_id = "123456789"
    monkeypatch.setattr(c, "ensure_receiver", lambda: None)
    return c, events


def started():
    return {"event": "meeting.rtms_started", "payload": {"meeting_id": "123456789", "meeting_uuid": "test-uuid",
        "rtms_stream_id": "stream-example", "server_urls": "wss://example.zoom.us"}}


def test_start_audio_stop_drains_and_preserves_source(monkeypatch, tmp_path):
    c, events = make(monkeypatch, tmp_path)
    calls = []
    def rtms(mid, action, **kwargs):
        calls.append(action)
        if action == "start":
            c._lifecycle_event(started())
    c.api.rtms = rtms
    async def run():
        task = asyncio.create_task(c._run())
        while not any(kind == "connected" for kind, _ in events):
            await asyncio.sleep(.01)
        c.stop()
        await asyncio.wait_for(task, 3)
    asyncio.run(run())
    assert calls == ["start", "stop"]
    records = list(c.store.iter_segments())
    assert records and records[0].speaker_id == "42"
    assert records[0].source_epoch.startswith("stream-example:")
    assert c.store.get_metadata("time_basis") == "rtms_session"
    assert c.pipeline.pending_seconds == 0
    c.close()


def test_failed_start_still_attempts_stop(monkeypatch, tmp_path):
    c, _ = make(monkeypatch, tmp_path)
    calls = []
    def rtms(mid, action, **kwargs):
        calls.append(action)
        if action == "start":
            raise module.ZoomError("Simulated lost start response")
    c.api.rtms = rtms
    with pytest.raises(module.ZoomError):
        asyncio.run(c._run())
    assert calls == ["start", "stop"]
    assert c.pipeline.pending_seconds == 0
    c.close()


def test_lifecycle_stop_uuid_only_and_stop_failure_visible(monkeypatch, tmp_path):
    c, events = make(monkeypatch, tmp_path)
    def rtms(mid, action, **kwargs):
        if action == "start":
            c._lifecycle_event(started())
        else:
            raise module.ZoomError("Offline")
    c.api.rtms = rtms
    async def run():
        task = asyncio.create_task(c._run())
        while not any(kind == "connected" for kind, _ in events):
            await asyncio.sleep(.01)
        c._lifecycle_event({"event": "meeting.rtms_stopped", "payload": {"meeting_uuid": "test-uuid"}})
        await asyncio.wait_for(task, 3)
    asyncio.run(run())
    assert any(kind == "stop_unconfirmed" for kind, _ in events)
    assert any(kind == "zoom_stopped" for kind, _ in events)
    c.close()


def test_no_background_start_without_both_consents(monkeypatch, tmp_path):
    c, _ = make(monkeypatch, tmp_path)
    for consent, billing in ((False, False), (True, False), (False, True)):
        with pytest.raises(module.ZoomError):
            c.start("123456789", consent=consent, billing_consent=billing)
        assert not c.running


def test_receiver_recreated_when_secret_or_port_changes(monkeypatch, tmp_path):
    created = []
    class Receiver:
        def __init__(self, secret, event, port):
            self.secret, self.port, self.stopped = secret, port, False
            created.append(self)
        def start(self):
            pass
        def stop(self):
            self.stopped = True
    monkeypatch.setattr(module, "WebhookReceiver", Receiver)
    class MutableVault(Vault):
        secret = "one"
        def load(self):
            return {"webhook_secret": self.secret}
    vault = MutableVault()
    c = module.Controller(Settings(), vault, lambda *_: None)
    c.ensure_receiver()
    first = c.receiver
    c.ensure_receiver()
    assert c.receiver is first
    vault.secret = "two"
    c.ensure_receiver()
    assert first.stopped and c.receiver.secret == "two"
    c.close()
