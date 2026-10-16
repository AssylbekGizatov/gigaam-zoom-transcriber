import hashlib
import hmac
import json
import os
import threading
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest
from gigaam_zoom.config import Settings, SecretStore, meeting_id, subscription_url, meeting_origin
from gigaam_zoom.auth import ZoomAPI, ZoomError, pkce_pair
from gigaam_zoom.events import WebhookReceiver, verify_webhook


class Vault:
    def __init__(self):
        self.doc = {"client_secret": "test-secret"}
    def load(self):
        return self.doc.copy()
    def update(self, **values):
        self.doc.update(values)


def test_settings_roundtrip_ignores_unknown_secret_fields(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(client_id="example", model_dir=str(tmp_path), webhook_port=9900)
    settings.save(path)
    assert Settings.load(path) == settings
    assert "secret" not in path.read_text()
    doc = json.loads(path.read_text())
    doc["secret"] = "do-not-load"
    path.write_text(json.dumps(doc))
    assert not hasattr(Settings.load(path), "secret")


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_dpapi_roundtrip_without_plaintext(tmp_path):
    vault = SecretStore(tmp_path / "credentials.dpapi")
    vault.update(client_secret="sensitive-test-value", tokens={"refresh_token": "refresh-example"})
    assert b"sensitive-test-value" not in vault.path.read_bytes()
    assert vault.load()["client_secret"] == "sensitive-test-value"
    vault.clear_tokens()
    assert vault.load()["tokens"] == {}
    assert vault.load()["client_secret"] == "sensitive-test-value"


def test_meeting_urls_never_store_passcode():
    assert meeting_id("https://company.zoom.us/j/12345678901?pwd=private") == "12345678901"
    assert meeting_id("123 456 789") == "123456789"
    for bad in ("https://zoom.us.evil.test/j/123456789", "123", "१२३४५६७८९", "../x", "https://zoom.us/j/123456789/extra"):
        with pytest.raises(ValueError):
            meeting_id(bad)


def test_event_endpoint_rejects_token_and_arbitrary_hosts():
    assert subscription_url("wss://ws.zoom.us/ws?subscriptionId=example")
    for bad in ("ws://ws.zoom.us/ws?subscriptionId=x", "wss://evil.test/ws?subscriptionId=x", "wss://ws.zoom.us/ws?subscriptionId=x&access_token=private"):
        with pytest.raises(ValueError):
            subscription_url(bad)


def test_pkce_s256():
    verifier, challenge = pkce_pair()
    import base64
    assert 43 <= len(verifier) <= 128
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def test_optional_actual_meeting_start():
    from datetime import datetime, timezone
    assert meeting_origin("") is None
    assert meeting_origin("16.10.2026 20:00:00") == int(datetime(2026, 10, 16, 15, tzinfo=timezone.utc).timestamp() * 1000)
    with pytest.raises(ValueError):
        meeting_origin("bad time")


def test_login_rejects_wrong_state_then_accepts_valid_callback():
    vault, calls = Vault(), []
    def request(url, **kwargs):
        calls.append(kwargs)
        return {"access_token": "access-example", "refresh_token": "refresh-example", "expires_in": 3600}
    api = ZoomAPI(Settings(public_client_id="public-example"), vault, request)
    def browser(url):
        args = parse_qs(urlsplit(url).query)
        assert args["code_challenge_method"] == ["S256"]
        callback = args["redirect_uri"][0]
        def redirect():
            with pytest.raises(HTTPError) as error:
                urlopen(callback + "?code=bad&state=wrong", timeout=5)
            assert error.value.code == 400
            with urlopen(callback + "?code=good&state=" + args["state"][0], timeout=5) as response:
                assert response.status == 200
        threading.Thread(target=redirect).start()
    api.login(open_browser=browser)
    body = parse_qs(calls[0]["data"].decode())
    assert body["code"] == ["good"]
    assert "code_verifier" in body and "Authorization" not in calls[0]["headers"]
    assert vault.doc["tokens"]["refresh_token"] == "refresh-example"


def test_refresh_rotates_and_billing_guard():
    vault, calls = Vault(), []
    vault.update(tokens={"access_token": "old", "refresh_token": "old-refresh", "expires_at": 0})
    def request(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("oauth/token"):
            return {"access_token": "new", "refresh_token": "new-refresh", "expires_in": 3600}
        return {}
    api = ZoomAPI(Settings(public_client_id="public-example", client_id="app-example"), vault, request)
    assert api.access_token() == "new"
    assert vault.doc["tokens"]["refresh_token"] == "new-refresh"
    with pytest.raises(ZoomError):
        api.rtms("123456789", "start")
    assert len(calls) == 1
    api.rtms("123456789", "start", billing_consent=True)
    assert json.loads(calls[-1][1]["data"])["action"] == "start"
    api.rtms("123456789", "stop")
    assert json.loads(calls[-1][1]["data"])["action"] == "stop"


def sign(body, timestamp, secret="example"):
    return "v0=" + hmac.new(secret.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256).hexdigest()


def test_webhook_timestamp_signature_and_real_http_crc_dedup():
    seen = []
    receiver = WebhookReceiver("example", seen.append, port=0)
    receiver.start()
    def post(doc, valid=True):
        body = json.dumps(doc).encode()
        ts = str(int(time.time()))
        request = Request(f"http://127.0.0.1:{receiver.port}/webhook", data=body,
            headers={"x-zm-request-timestamp": ts, "x-zm-signature": sign(body, ts) if valid else "wrong"})
        return urlopen(request, timeout=5)
    try:
        with post({"event": "endpoint.url_validation", "payload": {"plainToken": "challenge"}}) as response:
            assert json.load(response)["encryptedToken"] == hmac.new(b"example", b"challenge", hashlib.sha256).hexdigest()
        event = {"event": "meeting.rtms_started", "payload": {"meeting_id": "123456789", "rtms_stream_id": "fixture"}}
        with post(event):
            pass
        with post(event):
            pass
        assert seen == [event]
        with pytest.raises(HTTPError) as error:
            post(event, valid=False)
        assert error.value.code == 401
        assert not verify_webhook(b"{}", "1", sign(b"{}", "1"), "example")
    finally:
        receiver.stop()


def test_failed_webhook_callback_can_be_redelivered():
    attempts = []
    def callback(event):
        attempts.append(event)
        if len(attempts) == 1:
            raise RuntimeError("Simulated transient failure")
    receiver = WebhookReceiver("example", callback, port=0)
    receiver.start()
    body = json.dumps({"event": "meeting.rtms_started", "payload": {"meeting_id": "123456789"}}).encode()
    stamp = str(int(time.time()))
    def request():
        return urlopen(Request(f"http://127.0.0.1:{receiver.port}/webhook", data=body,
            headers={"x-zm-request-timestamp": stamp, "x-zm-signature": sign(body, stamp)}), timeout=3)
    try:
        with pytest.raises(HTTPError) as error:
            request()
        assert error.value.code == 503
        with request() as response:
            assert response.status == 200
        assert len(attempts) == 2
    finally:
        receiver.stop()
