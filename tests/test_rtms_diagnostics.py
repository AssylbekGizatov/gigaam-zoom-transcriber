import io
from urllib.error import HTTPError

import pytest

from gigaam_zoom import auth


@pytest.mark.parametrize(("body", "expected"), [
    (b'{"code":13267,"message":"private meeting details"}', "13267"),
    (b'{"code":2310,"message":"private meeting details"}', "2310"),
    (b'{"code":"13267","message":"private meeting details"}', "запретил"),
    (b'not JSON: private meeting details', "запретил"),
])
def test_rtms_errors_expose_only_allowlisted_codes(monkeypatch, body, expected):
    class Opener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 403, "forbidden", {}, io.BytesIO(body))

    monkeypatch.setattr(auth, "build_opener", lambda *_: Opener())
    with pytest.raises(auth.ZoomError, match=expected) as error:
        auth.request_json("https://api.zoom.us/v2/live_meetings/123456789/rtms_app/status",
                          method="PATCH")
    assert "private meeting details" not in str(error.value)
    assert "123456789" not in str(error.value)
