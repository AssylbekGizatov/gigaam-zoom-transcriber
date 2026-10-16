"""Audio-only Zoom RTMS transport using the documented WebSocket protocol.

No REST start call is made here: the application must obtain explicit billing
approval before starting RTMS. TLS verification stays enabled on every socket.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import inspect
import json
import ssl
import time
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .models import AudioPacket

MAX_MESSAGE_BYTES = 262_144
MAX_AUDIO_BYTES = 32_000  # one second, PCM16 mono 16 kHz
AUDIO_PARAMS = {
    "content_type": 2, "sample_rate": 1, "channel": 1,
    "codec": 1, "data_opt": 2, "send_rate": 20,
}


class RTMSError(RuntimeError):
    """Safe user-facing transport failure; never includes a credential/payload."""


class RTMSProtocolError(RTMSError):
    """The remote response is invalid or refuses the negotiated format."""


def validate_server_url(url: str, allowed_hosts: Iterable[str] | None = None) -> str:
    """Reject non-Zoom endpoints before credentials are sent (including redirects).

    ``allowed_hosts`` is an exact-host override for controlled TLS test servers.
    The default accepts zoom.us and its subdomains, port 443 only.
    """
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "wss" or parts.username or parts.password or parts.fragment:
            raise ValueError
        if parts.port not in (None, 443) or not host or host.endswith("."):
            raise ValueError
        if allowed_hosts is None:
            allowed = host == "zoom.us" or host.endswith(".zoom.us")
        else:
            allowed = host in {item.lower() for item in allowed_hosts}
        if not allowed:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise RTMSProtocolError("RTMS endpoint must be an approved Zoom WSS host on port 443") from exc
    return url


def generate_signature(client_id: str, client_secret: str, meeting_uuid: str, stream_id: str) -> str:
    message = f"{client_id},{meeting_uuid},{stream_id}".encode("utf-8")
    return hmac.new(client_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def parse_message(raw: str | bytes) -> dict:
    if not isinstance(raw, (str, bytes)) or len(raw) > MAX_MESSAGE_BYTES:
        raise RTMSProtocolError("RTMS message exceeds the local size limit")
    try:
        message = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise RTMSProtocolError("Invalid RTMS JSON message") from exc
    if not isinstance(message, dict) or type(message.get("msg_type")) is not int:
        raise RTMSProtocolError("Invalid RTMS message envelope")
    return message


class _ZoomConnect(connect):
    """Apply the endpoint policy again to HTTP redirect targets."""

    def __init__(self, uri, *, allowed_hosts=None, **kwargs):
        self._allowed_hosts = allowed_hosts
        super().__init__(uri, **kwargs)

    def process_redirect(self, exc):
        target = super().process_redirect(exc)
        if isinstance(target, str):
            try:
                validate_server_url(target, self._allowed_hosts)
            except RTMSProtocolError as rejected:
                return rejected
        return target


class RTMSClient:
    def __init__(
        self, client_id: str, client_secret: str, on_audio: Callable,
        on_event: Callable | None = None, on_origin: Callable | None = None,
        allowed_hosts: Iterable[str] | None = None, *,
        max_reconnect_attempts: int = 5, retry_base: float = 3.0,
        receive_timeout: float = 65.0,
    ):
        self.client_id = client_id
        self._secret = client_secret
        self.on_audio = on_audio
        self.on_event = on_event or (lambda *_: None)
        self.on_origin = on_origin or (lambda *_: None)
        self.allowed_hosts = tuple(allowed_hosts) if allowed_hosts is not None else None
        self.max_reconnect_attempts = max_reconnect_attempts
        self.retry_base = retry_base
        self.receive_timeout = receive_timeout
        self.origin_ms: int | None = None
        self._stopping = asyncio.Event()
        self._close_ack = asyncio.Event()
        self._signaling = None
        self._media = None
        self._stream_id = ""
        self._meeting_uuid = ""
        self._server_url = ""
        self._remote_terminated = False
        self._stop_warning_sent = False
        self._last_end: dict[str, float] = {}
        self._generation = 0
        self._participant_generations: dict[str, int] = {}
        self._left: set[str] = set()
        self._origin_ready = asyncio.Event()
        self._running = False

    async def _callback(self, callback: Callable, *args):
        result = callback(*args)
        if inspect.isawaitable(result):
            await result

    async def _event(self, kind: str, **payload):
        await self._callback(self.on_event, kind, payload)

    def _connect(self, url: str):
        validate_server_url(url, self.allowed_hosts)
        # Disable proxy discovery: media must connect directly to the validated
        # Zoom host. The library validates certificates and hostname normally.
        # Race the next address family after 250 ms on Windows dual-stack networks.
        return _ZoomConnect(
            url, allowed_hosts=self.allowed_hosts,
            ssl=ssl.create_default_context(), proxy=None,
            open_timeout=10, close_timeout=2, max_size=MAX_MESSAGE_BYTES,
            max_queue=16, ping_interval=20, ping_timeout=20,
            happy_eyeballs_delay=.25,
        )

    async def _send(self, socket, message: dict):
        await socket.send(json.dumps(message, separators=(",", ":")))

    async def _receive(self, socket, timeout: float | None = None) -> dict:
        raw = await asyncio.wait_for(socket.recv(), timeout or self.receive_timeout)
        return parse_message(raw)

    async def _keepalive(self, socket, msg: dict) -> bool:
        if msg["msg_type"] != 12:
            return False
        if type(msg.get("timestamp")) is not int:
            raise RTMSProtocolError("Invalid RTMS keepalive timestamp")
        await self._send(socket, {"msg_type": 13, "timestamp": msg["timestamp"]})
        return True

    async def _handshake_response(self, socket, expected_type: int) -> dict:
        deadline = time.monotonic() + 15
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RTMSProtocolError("RTMS handshake timed out")
            msg = await self._receive(socket, remaining)
            if await self._keepalive(socket, msg):
                continue
            if msg["msg_type"] == expected_type:
                if msg.get("status_code") != 0:
                    # Server reason can echo credentials; expose code only.
                    code = msg.get("status_code")
                    safe_code = code if type(code) is int else "unknown"
                    raise RTMSProtocolError(f"RTMS handshake rejected (code {safe_code})")
                return msg
            if msg["msg_type"] == 6:
                await self._signal_event(msg)

    async def _signal_event(self, msg: dict):
        event = msg.get("event")
        if not isinstance(event, dict):
            raise RTMSProtocolError("Invalid RTMS session event")
        event_type = event.get("event_type")
        if event_type == 1:
            timestamp = event.get("timestamp")
            if type(timestamp) is not int or timestamp < 0:
                raise RTMSProtocolError("Invalid RTMS session origin")
            if self.origin_ms is None:
                self.origin_ms = timestamp
                self._origin_ready.set()
                await self._callback(self.on_origin, timestamp)
            elif timestamp != self.origin_ms:
                # A reconnect's first timestamp must never reset meeting time.
                await self._event("origin_preserved", timestamp_ms=self.origin_ms)
        elif event_type in (3, 4):
            participants = event.get("participants", [])
            if not isinstance(participants, list) or len(participants) > 10_000:
                raise RTMSProtocolError("Invalid RTMS participant list")
            for participant in participants:
                if not isinstance(participant, dict) or "user_id" not in participant:
                    raise RTMSProtocolError("Invalid RTMS participant")
                uid = str(participant["user_id"])
                if event_type == 3 and uid in self._left:
                    self._participant_generations[uid] = self._participant_generations.get(uid, 0) + 1
                    self._left.discard(uid)
                    self._last_end.pop(uid, None)
                await self._event(
                    "participant_joined" if event_type == 3 else "participant_left",
                    user_id=uid,
                    user_name=participant.get("user_name"),
                    timestamp_ms=event.get("timestamp"), epoch=self._epoch(uid),
                )
                if event_type == 4:
                    self._left.add(uid)
                if len(self._participant_generations) + len(self._left) > 10_000:
                    raise RTMSProtocolError("RTMS participant lifecycle limit reached; save and restart")
        elif event_type == 7:
            await self._event("reconnecting", connection="audio", possible_gap=True)
            if self._media:
                await self._media.close()
        else:
            await self._event("session_event", event_type=event_type)

    async def _signaling_loop(self, socket):
        while not self._stopping.is_set():
            msg = await self._receive(socket)
            if await self._keepalive(socket, msg):
                continue
            if msg["msg_type"] == 6:
                await self._signal_event(msg)
            elif msg["msg_type"] == 22:
                self._close_ack.set()
            elif msg["msg_type"] in (8, 9):
                state, reason = msg.get("state"), msg.get("reason")
                await self._event("stream_state", state=state, reason=reason)
                if reason == 8:
                    await self._event("revoked", delete_assets=True)
                if (msg["msg_type"] == 8 and state in (3, 4)) or (msg["msg_type"] == 9 and state == 5):
                    self._remote_terminated = True
                    self._stopping.set()
                    return
                if msg["msg_type"] == 8 and state == 2 and reason == 14 and self._media:
                    await self._media.close()

    async def _audio_message(self, msg: dict):
        content = msg.get("content")
        if not isinstance(content, dict):
            raise RTMSProtocolError("Invalid RTMS audio content")
        data, timestamp, user_id = content.get("data"), content.get("timestamp"), content.get("user_id")
        if not isinstance(data, str) or len(data) > 44_000 or type(timestamp) is not int or timestamp < 0:
            raise RTMSProtocolError("Invalid RTMS audio encoding or timestamp")
        if type(user_id) not in (int, str) or not str(user_id) or len(str(user_id)) > 128:
            raise RTMSProtocolError("Missing RTMS audio source ID")
        try:
            pcm = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RTMSProtocolError("Invalid RTMS audio base64") from exc
        if not pcm or len(pcm) > MAX_AUDIO_BYTES or len(pcm) % 2:
            raise RTMSProtocolError("Invalid RTMS PCM16 frame size")
        if content.get("length", len(pcm)) != len(pcm):
            raise RTMSProtocolError("RTMS audio length does not match payload")
        uid = str(user_id)
        name = content.get("user_name")
        if name is not None and (not isinstance(name, str) or len(name) > 1024):
            raise RTMSProtocolError("Invalid RTMS participant name")
        if uid == "0":
            await self._event("mixed_audio", user_id="0", attribution="unknown")
            name = None
        end = timestamp + len(pcm) / 32.0
        previous_end = self._last_end.get(uid)
        if previous_end is not None:
            if end <= previous_end:
                await self._event("duplicate_audio", user_id=uid, timestamp_ms=timestamp)
                return
            if timestamp < previous_end:
                # Replayed chunks can partly overlap after reconnection.
                trim = min(len(pcm), int(round((previous_end - timestamp) * 16)) * 2)
                pcm = pcm[trim:]
                timestamp += trim // 32
            elif timestamp - previous_end > 100:
                await self._event("audio_gap", user_id=uid,
                                  start_ms=previous_end, end_ms=timestamp,
                                  reason="mute_silence_or_network")
        self._last_end[uid] = end
        # Bound metadata memory even when a long meeting cycles through IDs.
        if len(self._last_end) > 10_000:
            del self._last_end[next(iter(self._last_end))]
        if self.origin_ms is None:
            # Signal/media delivery order isn't guaranteed. Wait without blocking
            # the signaling reader. A missing origin must be visible to the UI.
            try:
                await asyncio.wait_for(self._origin_ready.wait(), 3)
            except TimeoutError:
                await self._event("origin_unavailable", time_basis="first_audio")
        if pcm:
            await self._callback(self.on_audio, AudioPacket(
                user_id=uid, user_name=name, timestamp_ms=timestamp,
                pcm=pcm, sample_rate=16000, epoch=self._epoch(uid),
            ))

    def _epoch(self, uid: str) -> str:
        return f"{self._stream_id}:{self._generation}.{self._participant_generations.get(uid, 0)}"

    async def _media_loop(self, media_url: str, signaling):
        for attempt in range(self.max_reconnect_attempts + 1):
            if self._stopping.is_set():
                return
            try:
                async with self._connect(media_url) as socket:
                    self._media = socket
                    await self._send(socket, {
                        "msg_type": 3, "protocol_version": 1, "sequence": 1,
                        "meeting_uuid": self._meeting_uuid, "rtms_stream_id": self._stream_id,
                        "signature": generate_signature(self.client_id, self._secret, self._meeting_uuid, self._stream_id),
                        "media_type": 1, "payload_encryption": False,
                        "media_params": {"audio": dict(AUDIO_PARAMS)},
                    })
                    response = await self._handshake_response(socket, 4)
                    confirmed = response.get("media_params", {}).get("audio", {})
                    if not isinstance(confirmed, dict) or any(
                        key in confirmed and confirmed[key] != value
                        for key, value in AUDIO_PARAMS.items() if key != "send_rate"
                    ):
                        raise RTMSProtocolError("Zoom did not accept separate PCM16 mono 16 kHz audio")
                    if response.get("payload_encrypted", False):
                        raise RTMSProtocolError("Additional RTMS payload encryption is unsupported; TLS remains required")
                    await self._send(signaling, {"msg_type": 7, "rtms_stream_id": self._stream_id})
                    await self._event("connected", connection="audio", attempt=attempt)
                    while not self._stopping.is_set():
                        msg = await self._receive(socket)
                        if await self._keepalive(socket, msg):
                            continue
                        if msg["msg_type"] == 14:
                            await self._audio_message(msg)
            except RTMSProtocolError:
                raise
            except (OSError, TimeoutError, ConnectionClosed) as exc:
                if self._stopping.is_set():
                    return
                if attempt >= self.max_reconnect_attempts:
                    raise RTMSError("RTMS audio reconnection attempts exhausted") from exc
                await self._event("reconnecting", connection="audio", attempt=attempt + 1, possible_gap=True)
                await self._retry_wait(attempt)
            finally:
                self._media = None

    async def _retry_wait(self, attempt: int):
        try:
            await asyncio.wait_for(self._stopping.wait(), min(12.0, self.retry_base * 2**attempt))
        except TimeoutError:
            pass

    async def run(self, meeting_uuid: str, stream_id: str, server_url: str):
        if self._running:
            raise RTMSError("RTMS client is already running")
        if not meeting_uuid or not stream_id or len(meeting_uuid) > 256 or len(stream_id) > 256:
            raise RTMSProtocolError("Invalid RTMS meeting or stream identifier")
        validate_server_url(server_url, self.allowed_hosts)
        self._meeting_uuid, self._stream_id = meeting_uuid, stream_id
        self._server_url = server_url
        self._running = True
        self._stopping.clear()
        self._close_ack.clear()
        cancelled = False
        try:
            for attempt in range(self.max_reconnect_attempts + 1):
                if self._stopping.is_set():
                    break
                try:
                    async with self._connect(self._server_url) as signaling:
                        self._signaling = signaling
                        await self._send(signaling, {
                            "msg_type": 1, "protocol_version": 1, "sequence": 1,
                            "meeting_uuid": meeting_uuid, "rtms_stream_id": stream_id,
                            "signature": generate_signature(self.client_id, self._secret, meeting_uuid, stream_id),
                        })
                        response = await self._handshake_response(signaling, 2)
                        self._generation += 1
                        self._participant_generations.clear()
                        self._left.clear()
                        try:
                            media_url = response["media_server"]["server_urls"]["audio"]
                            validate_server_url(media_url, self.allowed_hosts)
                        except (KeyError, TypeError) as exc:
                            raise RTMSProtocolError("Zoom did not provide an audio endpoint; check audio scope") from exc
                        await self._send(signaling, {"msg_type": 5, "events": [
                            {"event_type": 3, "subscribe": True},
                            {"event_type": 4, "subscribe": True},
                        ]})
                        tasks = [asyncio.create_task(self._signaling_loop(signaling)),
                                 asyncio.create_task(self._media_loop(media_url, signaling))]
                        try:
                            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                            for task in done:
                                task.result()
                        finally:
                            for task in tasks:
                                task.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
                        if self._stopping.is_set():
                            break
                        raise RTMSError("RTMS connection ended unexpectedly")
                except RTMSProtocolError:
                    raise
                except Exception as exc:
                    if self._stopping.is_set():
                        break
                    if attempt >= self.max_reconnect_attempts:
                        raise RTMSError("RTMS signaling reconnection attempts exhausted; restart from Zoom") from exc
                    await self._event("reconnecting", connection="signaling", attempt=attempt + 1, possible_gap=True)
                    await self._retry_wait(attempt)
                finally:
                    self._signaling = None
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if cancelled:
                # Cancellation is used to replace an obsolete transport during
                # failover. It is NOT a command to stop the server's RTMS stream.
                self._stopping.set()
                for socket in (self._media, self._signaling):
                    if socket:
                        await socket.close()
            else:
                await self.stop()
            self._running = False
            await self._event("disconnected" if cancelled else "stopped", origin_ms=self.origin_ms)

    async def stop(self):
        """Request stream termination before closing sockets; safe to call repeatedly."""
        self._stopping.set()
        signaling, media = self._signaling, self._media
        if signaling:
            try:
                await self._send(signaling, {"msg_type": 21, "rtms_stream_id": self._stream_id})
                await asyncio.wait_for(self._close_ack.wait(), 2)
            except Exception:
                pass
        if not self._close_ack.is_set() and not self._remote_terminated and self._stream_id and not self._stop_warning_sent:
            self._stop_warning_sent = True
            await self._event("stop_unconfirmed", action="Stop app in Zoom Active Apps or via REST")
        for socket in (media, signaling):
            if socket:
                await socket.close()

    async def reconnect(self, server_url: str):
        """Switch a running stream to failover URLs without terminating it.

        Use for a duplicate ``meeting.rtms_started`` or ``rtms_interrupted``
        lifecycle event. A different stream ID requires a new client. Cancelling
        ``run`` only disconnects its sockets; ``stop`` explicitly ends a stream.
        """
        validate_server_url(server_url, self.allowed_hosts)
        if not self._running or self._stopping.is_set():
            raise RTMSError("RTMS reconnect requires a running stream")
        self._server_url = server_url
        await self._event("reconnecting", connection="signaling", possible_gap=True, cause="server_failover")
        for socket in (self._media, self._signaling):
            if socket:
                await socket.close()
