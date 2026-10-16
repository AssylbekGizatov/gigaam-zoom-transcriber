"""Credential setup regressions without a Tk window, DPAPI, disk or network."""
from copy import deepcopy
from dataclasses import dataclass
import io
import json
from types import SimpleNamespace
from typing import ClassVar

import pytest

from gigaam_zoom import gui
from scripts import import_credentials as importer


PRIVATE = "SETUP_TEST_PRIVATE_VALUE_NEVER_PRINT"
OLD_TOKENS = {"access_token": "old-test-access", "refresh_token": "old-test-refresh"}


@dataclass
class SettingsDouble:
    client_id: str = "old-client"
    public_client_id: str = "old-public-client"
    model_dir: str = "local-model"
    event_endpoint: str = ""
    event_mode: str = "webhook"
    webhook_port: int = 8765
    export_dir: str = "local-output"

    loaded: ClassVar[object] = None
    saved: ClassVar[list] = []
    validated: ClassVar[list] = []
    validation_error: ClassVar[Exception | None] = None
    load_count: ClassVar[int] = 0

    @classmethod
    def load(cls):
        cls.load_count += 1
        return deepcopy(cls.loaded)

    def validate(self):
        type(self).validated.append(deepcopy(self))
        if type(self).validation_error is not None:
            raise type(self).validation_error

    def save(self):
        type(self).saved.append(deepcopy(self))


class VaultDouble:
    def __init__(self):
        self.doc = {"client_secret": "old-client-secret", "webhook_secret": "old-webhook-secret",
                    "tokens": deepcopy(OLD_TOKENS)}
        self.calls = []
        self.error = None

    def update(self, **updates):
        self.calls.append(deepcopy(updates))
        if self.error:
            raise self.error
        # Each invocation represents one encrypted credential-file transaction.
        self.doc = {**self.doc, **deepcopy(updates)}


class Var:
    def __init__(self, value=""):
        self.value = value
    def get(self):
        return self.value
    def set(self, value):
        self.value = value


@pytest.fixture
def doubles(monkeypatch):
    SettingsDouble.loaded = SettingsDouble()
    SettingsDouble.saved = []
    SettingsDouble.validated = []
    SettingsDouble.validation_error = None
    SettingsDouble.load_count = 0
    vault = VaultDouble()
    messages = []
    monkeypatch.setattr(importer, "Settings", SettingsDouble)
    monkeypatch.setattr(importer, "SecretStore", lambda: vault)
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *args: messages.append(args))
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *args: messages.append(args))
    return vault, messages


def app_without_tk(vault):
    app = gui.Application.__new__(gui.Application)
    app.settings = SettingsDouble()
    app.vault = vault
    app.busy = False
    app.controller = SimpleNamespace(running=False, settings=app.settings,
                                     api=SimpleNamespace(settings=app.settings))
    app.variables = {key: Var(str(getattr(app.settings, key))) for key in (
        "client_id", "public_client_id", "model_dir", "event_endpoint", "event_mode", "webhook_port")}
    app.variables.update(client_secret=Var(""), webhook_secret=Var(""))
    app.folder = Var(app.settings.export_dir)
    app.account = Var("Old connected account")
    app.status = Var()
    return app


@pytest.mark.parametrize("identity_field", ["client_id", "public_client_id"])
def test_gui_changed_oauth_identity_clears_tokens_with_secret_in_one_update(doubles, identity_field):
    vault, messages = doubles
    app = app_without_tk(vault)
    previous = app.settings
    app.variables[identity_field].set("different-app")
    app.variables["client_secret"].set(PRIVATE)
    assert app.save() is True
    assert vault.calls == [{"client_secret": PRIVATE, "tokens": {}}]
    assert vault.doc["tokens"] == {}
    assert vault.doc["client_secret"] == PRIVATE
    assert app.settings is not previous
    assert app.controller.settings is app.settings
    assert app.controller.api.settings is app.settings
    assert getattr(previous, identity_field) != "different-app"
    assert SettingsDouble.saved == [app.settings]
    assert app.variables["client_secret"].get() == ""
    assert app.account.get() != "Old connected account"
    assert not messages


def test_gui_secret_rotation_for_same_identity_preserves_oauth_tokens(doubles):
    vault, messages = doubles
    app = app_without_tk(vault)
    app.variables["client_secret"].set(PRIVATE)
    app.variables["webhook_secret"].set("rotated-webhook-test-secret")
    assert app.save() is True
    assert vault.calls == [{"client_secret": PRIVATE, "webhook_secret": "rotated-webhook-test-secret"}]
    assert vault.doc["tokens"] == OLD_TOKENS
    assert app.account.get() == "Old connected account"
    assert app.variables["webhook_secret"].get() == ""
    assert not messages


@pytest.mark.parametrize("failure", ["validation", "invalid_port"])
def test_gui_invalid_candidate_never_mutates_live_settings_or_credentials(doubles, failure):
    vault, messages = doubles
    app = app_without_tk(vault)
    previous = app.settings
    before = deepcopy(previous)
    app.variables["client_id"].set("new-app-must-not-become-active")
    app.variables["client_secret"].set(PRIVATE)
    app.folder.set("unvalidated-output-folder")
    if failure == "validation":
        SettingsDouble.validation_error = ValueError("Controlled invalid settings")
    else:
        app.variables["webhook_port"].set("not-an-integer")
    assert app.save() is False
    assert app.settings is previous
    assert previous == before
    assert app.controller.settings is previous
    assert app.controller.api.settings is previous
    assert vault.doc["tokens"] == OLD_TOKENS
    assert vault.calls == []
    assert SettingsDouble.saved == []
    assert messages
    assert PRIVATE not in str(messages)


def test_gui_vault_failure_does_not_activate_candidate_or_echo_secret(doubles):
    vault, messages = doubles
    app = app_without_tk(vault)
    previous = app.settings
    app.variables["public_client_id"].set("different-public-app")
    app.variables["client_secret"].set(PRIVATE)
    vault.error = RuntimeError("Underlying credential failure includes " + PRIVATE)
    assert app.save() is False
    assert app.settings is previous
    assert app.controller.settings is previous
    assert app.controller.api.settings is previous
    assert SettingsDouble.saved == []
    assert vault.doc["tokens"] == OLD_TOKENS
    assert PRIVATE not in str(messages)


class InputBuffer(io.BytesIO):
    def __init__(self, payload):
        super().__init__(payload)
        self.requested_sizes = []
    def read(self, size=-1):
        self.requested_sizes.append(size)
        return super().read(size)


def run_import(monkeypatch, payload):
    buffer = InputBuffer(payload)
    monkeypatch.setattr(importer.sys, "stdin", SimpleNamespace(buffer=buffer))
    result = importer.main()
    assert buffer.requested_sizes == [8193]  # Bounded read, even on hostile input.
    return result


@pytest.mark.parametrize("identity_field", ["client_id", "public_client_id"])
def test_import_changed_identity_clears_tokens_atomically_with_new_secret(doubles, monkeypatch, capsys, identity_field):
    vault, _ = doubles
    payload = json.dumps({identity_field: "different-app", "client_secret": PRIVATE}).encode()
    assert run_import(monkeypatch, payload) == 0
    assert vault.calls == [{"client_secret": PRIVATE, "tokens": {}}]
    assert vault.doc["tokens"] == {}
    assert getattr(SettingsDouble.saved[0], identity_field) == "different-app"
    assert PRIVATE not in capsys.readouterr().out


def test_import_same_identity_secret_rotation_preserves_tokens(doubles, monkeypatch, capsys):
    vault, _ = doubles
    payload = json.dumps({"client_id": "old-client", "public_client_id": "old-public-client",
                          "client_secret": PRIVATE}).encode()
    assert run_import(monkeypatch, payload) == 0
    assert vault.calls == [{"client_secret": PRIVATE}]
    assert vault.doc["tokens"] == OLD_TOKENS
    captured = capsys.readouterr()
    assert PRIVATE not in captured.out + captured.err


@pytest.mark.parametrize("payload", [
    json.dumps({"client_secret": PRIVATE, "unexpected_key": "unknown"}).encode(),
    json.dumps({"client_secret": PRIVATE, "client_id": 42}).encode(),
    json.dumps({"client_secret": PRIVATE, "event_endpoint": None}).encode(),
    json.dumps({"client_secret": [PRIVATE] * 8}).encode(),
    json.dumps([{"client_secret": PRIVATE}]).encode(),
    json.dumps({"client_secret": "short"}).encode(),
    json.dumps({"client_secret": PRIVATE + "x" * 4096}).encode(),
    ('{"client_secret":"' + PRIVATE).encode(),
    json.dumps({"client_secret": PRIVATE}).encode() + b" " * 8193,
], ids=["unknown-field", "integer-field", "null-field", "nonstring-secret", "not-an-object", "short-secret",
        "long-secret", "malformed-json", "oversized"])
def test_import_rejects_invalid_input_without_saving_or_printing_credentials(doubles, monkeypatch, capsys, payload):
    vault, _ = doubles
    assert run_import(monkeypatch, payload) == 1
    assert vault.calls == []
    assert vault.doc["tokens"] == OLD_TOKENS
    assert SettingsDouble.saved == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err
    assert PRIVATE not in captured.err


@pytest.mark.parametrize("failure", ["settings-validation", "vault-update"])
def test_import_failure_messages_never_echo_exception_credentials(doubles, monkeypatch, capsys, failure):
    vault, _ = doubles
    if failure == "settings-validation":
        SettingsDouble.validation_error = ValueError("Error includes " + PRIVATE)
    else:
        vault.error = RuntimeError("DPAPI error includes " + PRIVATE)
    payload = json.dumps({"client_id": "different-app", "client_secret": PRIVATE}).encode()
    assert run_import(monkeypatch, payload) == 1
    assert SettingsDouble.saved == []
    assert vault.doc["tokens"] == OLD_TOKENS
    captured = capsys.readouterr()
    assert captured.out == ""
    assert PRIVATE not in captured.err
    assert "Traceback" not in captured.err
