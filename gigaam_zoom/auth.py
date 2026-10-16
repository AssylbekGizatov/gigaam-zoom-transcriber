"""Zoom PKCE login, token rotation and narrowly scoped API operations.

Official references: https://developers.zoom.us/docs/integrations/oauth/
https://developers.zoom.us/docs/api/websockets/
"""
from __future__ import annotations
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib.request import Request, build_opener, HTTPRedirectHandler
import webbrowser


class ZoomError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, *, method="GET", data=None, headers=None):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"zoom.us", "api.zoom.us"}:
        raise ZoomError("Недопустимый адрес Zoom API.")
    request = Request(url, data=data, method=method, headers=headers or {})
    try:
        with build_opener(_NoRedirect).open(request, timeout=20) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ZoomError("Ответ Zoom превысил допустимый размер.")
            return json.loads(raw) if raw else {}
    except HTTPError as error:
        # Never expose response bodies, URLs with tokens or credential-bearing request objects.
        messages = {400: "Zoom отклонил запрос. Проверьте настройки приложения и ID встречи.",
                    401: "Авторизация Zoom истекла или ключи неверны. Подключите аккаунт снова.",
                    403: "Zoom запретил действие. Проверьте scopes, разрешение хоста и Developer Pack.",
                    404: "Встреча или приложение Zoom не найдены.",
                    429: "Лимит запросов Zoom. Повторите действие позже."}
        raise ZoomError(messages.get(error.code, f"Ошибка Zoom HTTP {error.code}.")) from None
    except (URLError, TimeoutError, OSError):
        raise ZoomError("Нет защищённого соединения с Zoom. Проверьте сеть и прокси.") from None
    except (ValueError, UnicodeError):
        raise ZoomError("Zoom вернул неподдерживаемый ответ.") from None


def pkce_pair():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class ZoomAPI:
    def __init__(self, settings, vault, request=request_json):
        self.settings, self.vault, self.request = settings, vault, request
        self.lock = threading.RLock()

    def _token(self, fields, confidential=False):
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if confidential:
            secret = self.vault.load().get("client_secret", "")
            if not secret:
                raise ZoomError("Сохраните Client Secret вашего General App.")
            basic = base64.b64encode(f"{self.settings.client_id}:{secret}".encode()).decode()
            headers["Authorization"] = "Basic " + basic
        return self.request("https://zoom.us/oauth/token", method="POST",
                            data=urlencode(fields).encode(), headers=headers)

    def _save_tokens(self, doc):
        if not doc.get("access_token") or not doc.get("refresh_token"):
            raise ZoomError("Zoom не выдал пользовательские OAuth-токены.")
        doc = {k: doc[k] for k in ("access_token", "refresh_token", "expires_in", "scope") if k in doc}
        doc["expires_at"] = time.time() + int(doc.get("expires_in", 3600))
        self.vault.update(tokens=doc)

    def login(self, cancel=None, open_browser=webbrowser.open):
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(32)
        outcome = {}

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urlsplit(self.path)
                args = parse_qs(url.query)
                good = (url.path == "/callback" and len(args.get("state", [])) == 1
                        and secrets.compare_digest(args["state"][0], state))
                if good and args.get("code") and len(args["code"]) == 1:
                    outcome["code"] = args["code"][0]
                elif good and args.get("error"):
                    outcome["error"] = True
                self.send_response(200 if good else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(("Ответ принят. Вернитесь в GigaAM Zoom." if good else "Неверный OAuth state.").encode())

        with HTTPServer(("127.0.0.1", 0), Callback) as server:
            server.timeout = 0.3
            redirect = f"http://127.0.0.1:{server.server_port}/callback"
            url = "https://zoom.us/oauth/authorize?" + urlencode({"response_type": "code",
                "client_id": self.settings.public_client_id, "redirect_uri": redirect,
                "code_challenge": challenge, "code_challenge_method": "S256", "state": state})
            open_browser(url)
            deadline = time.monotonic() + 180
            while not outcome and time.monotonic() < deadline and not (cancel and cancel.is_set()):
                server.handle_request()
        if "code" not in outcome:
            raise ZoomError("Вход отменён или истекло время ожидания. Повторите подключение.")
        with self.lock:
            self._save_tokens(self._token({"grant_type": "authorization_code", "client_id": self.settings.public_client_id,
                "code": outcome["code"], "redirect_uri": redirect, "code_verifier": verifier}))

    def access_token(self):
        with self.lock:
            doc = self.vault.load().get("tokens", {})
            if not doc.get("refresh_token"):
                raise ZoomError("Сначала подключите аккаунт Zoom.")
            if float(doc.get("expires_at", 0)) <= time.time() + 60:
                # Public-client refresh: body client_id, no confidential Authorization header.
                self._save_tokens(self._token({"grant_type": "refresh_token", "refresh_token": doc["refresh_token"],
                                              "client_id": self.settings.public_client_id}))
                doc = self.vault.load()["tokens"]
            return doc["access_token"]

    def event_token(self):
        return self._token({"grant_type": "client_credentials"}, confidential=True)["access_token"]

    def api(self, path, *, method="GET", body=None):
        headers = {"Authorization": "Bearer " + self.access_token()}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        return self.request("https://api.zoom.us/v2/" + path, method=method, data=data, headers=headers)

    def profile(self):
        doc = self.api("users/me")
        return {key: doc.get(key) for key in ("id", "display_name", "email")}

    def meetings(self):
        return self.api("users/me/meetings?type=upcoming&page_size=100").get("meetings", [])

    def rtms(self, meeting_id, action, *, billing_consent=False):
        if action not in {"start", "stop"}:
            raise ValueError("Недопустимое действие RTMS")
        if action == "start" and not billing_consent:
            raise ZoomError("Запуск RTMS требует явного подтверждения расхода Developer Pack кредитов.")
        from .config import meeting_id as normalize
        return self.api(f"live_meetings/{normalize(meeting_id)}/rtms_app/status", method="PATCH",
                        body={"action": action, "settings": {"client_id": self.settings.client_id}})
