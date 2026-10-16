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


def test_stop_while_waiting_event_authorization_is_prompt_and_never_starts_rtms(monkeypatch, tmp_path):
    c, _ = make(monkeypatch, tmp_path)
    c.settings.event_mode = "websocket"
    calls = []
    c.api.rtms = lambda mid, action, **kwargs: calls.append(action)

    async def run():
        waiting = asyncio.Event()
        async def events(endpoint, token, event, status, stop, ready):
            waiting.set()
            await stop.wait()
        monkeypatch.setattr(module, "websocket_events", events)
        task = asyncio.create_task(c._run())
        await asyncio.wait_for(waiting.wait(), 1)
        c.stop()
        await asyncio.wait_for(task, .5)
    asyncio.run(run())
    assert calls == [] and c.pipeline.pending_seconds == 0
    c.close()


def test_event_authorization_failure_is_reported_without_waiting_full_timeout(monkeypatch, tmp_path):
    c, _ = make(monkeypatch, tmp_path)
    c.settings.event_mode = "websocket"
    calls = []
    c.api.rtms = lambda mid, action, **kwargs: calls.append(action)
    async def events(*_):
        raise RuntimeError("private-token-must-not-escape")
    monkeypatch.setattr(module, "websocket_events", events)
    with pytest.raises(module.ZoomError) as error:
        asyncio.run(asyncio.wait_for(c._run(), .5))
    assert "private-token" not in str(error.value)
    assert calls == []
    c.close()


def test_event_gap_after_ready_before_paid_start_aborts_without_start(monkeypatch, tmp_path):
    c, _ = make(monkeypatch, tmp_path)
    c.settings.event_mode = "websocket"
    calls = []
    c.api.rtms = lambda mid, action, **kwargs: calls.append(action)
    async def events(endpoint, token, event, status, stop, ready):
        ready.set()
        status("events_connected", {})
        ready.clear()
        status("events_reconnecting", {})
        await stop.wait()
    monkeypatch.setattr(module, "websocket_events", events)
    with pytest.raises(module.ZoomError, match="повторите запуск"):
        asyncio.run(asyncio.wait_for(c._run(), .5))
    assert calls == []
    c.close()


def test_event_gap_during_start_waits_for_rest_response_then_sends_stop(monkeypatch, tmp_path):
    c, _ = make(monkeypatch, tmp_path)
    c.settings.event_mode = "websocket"
    entered, release = threading.Event(), threading.Event()
    calls = []
    def rtms(mid, action, **kwargs):
        calls.append(action)
        if action == "start":
            entered.set()
            assert release.wait(2), "test did not release REST Start"
            calls.append("start_response")
    c.api.rtms = rtms

    async def run():
        gapped = asyncio.Event()
        async def events(endpoint, token, event, status, stop, ready):
            ready.set()
            status("events_connected", {})
            assert await asyncio.to_thread(entered.wait, 1)
            ready.clear()
            status("events_reconnecting", {})
            gapped.set()
            await stop.wait()
        monkeypatch.setattr(module, "websocket_events", events)
        task = asyncio.create_task(c._run())
        try:
            await asyncio.wait_for(gapped.wait(), 1)
            await asyncio.sleep(.03)
            assert calls == ["start"]  # Stop must not race an unfinished Start.
            release.set()
            with pytest.raises(module.ZoomError, match="повторите запуск"):
                await asyncio.wait_for(task, 1)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
    assert calls == ["start", "start_response", "stop"]
    c.close()


def test_event_gap_while_waiting_rtms_started_fails_fast_and_stops_paid_stream(monkeypatch, tmp_path):
    c, _ = make(monkeypatch, tmp_path)
    c.settings.event_mode = "websocket"
    entered = threading.Event()
    calls = []
    def rtms(mid, action, **kwargs):
        calls.append(action)
        if action == "start":
            entered.set()
    c.api.rtms = rtms
    async def events(endpoint, token, event, status, stop, ready):
        ready.set()
        status("events_connected", {})
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.sleep(.02)
        ready.clear()
        status("events_reconnecting", {})
        await stop.wait()
    monkeypatch.setattr(module, "websocket_events", events)
    with pytest.raises(module.ZoomError, match="повторите запуск"):
        asyncio.run(asyncio.wait_for(c._run(), 1))
    assert calls == ["start", "stop"]
    c.close()


def test_event_gap_after_media_connection_keeps_media_and_journals_warning(monkeypatch, tmp_path):
    c, events_out = make(monkeypatch, tmp_path)
    c.settings.event_mode = "websocket"
    calls = []
    def rtms(mid, action, **kwargs):
        calls.append(action)
        if action == "start":
            c._lifecycle_event(started())
    c.api.rtms = rtms

    async def run():
        gapped = asyncio.Event()
        async def events(endpoint, token, event, status, stop, ready):
            ready.set()
            status("events_connected", {})
            while not c._media_established:
                await asyncio.sleep(.001)
            ready.clear()
            status("events_reconnecting", {"message": "Lifecycle connection interrupted"})
            gapped.set()
            await asyncio.sleep(.03)
            ready.set()
            status("events_connected", {})
            await stop.wait()
        monkeypatch.setattr(module, "websocket_events", events)
        task = asyncio.create_task(c._run())
        await asyncio.wait_for(gapped.wait(), 1)
        assert not c._cancel.is_set() and not c.stop_event.is_set()
        assert calls == ["start"]
        c.stop()
        await asyncio.wait_for(task, 1)
    asyncio.run(run())
    assert calls == ["start", "stop"]
    assert list(c.store.iter_segments())
    assert any(g.reason == "event_delivery_gap_unknown_duration" for g in c.store.iter_gaps())
    assert any(kind == "events_reconnecting" for kind, _ in events_out)
    c.close()


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
