from __future__ import annotations

import io
import json
import sys
import types
import urllib.error

import pytest

from copilot.student_core.models import HookEvent
from copilot.student_core.transport import (
    Accepted,
    PermanentTransportError,
    StudentTransport,
    TemporaryNetworkError,
)


def event() -> HookEvent:
    return HookEvent(
        event="Stop",
        student_id="student-1",
        session_id="session-1",
        cwd="/workspace",
        transcript_tail="hello",
        transcript_path="/tmp/transcript.jsonl",
    )


class Response:
    def __init__(self, status: int, body: bytes = b"{}") -> None:
        self.status = status
        self._body = io.BytesIO(body)

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return self._body.read()


def test_post_hook_sends_contract_and_auth_headers_without_logging_token() -> None:
    captured: dict[str, object] = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return Response(202, b'{"queued":true}')

    transport = StudentTransport(
        "https://copilot.example",
        student_id="student-1",
        token="secret-token",
        opener=opener,
        timeout=2.5,
    )
    result = transport.post_hook(event())

    assert result == Accepted(status_code=202, body={"queued": True})
    assert captured["url"] == "https://copilot.example/report"
    headers = {str(k).lower(): str(v) for k, v in captured["headers"].items()}
    assert headers["authorization"] == "Bearer secret-token"
    assert headers["x-copilot-token"] == "secret-token"
    assert captured["body"] == event().to_dict()
    assert captured["timeout"] == 2.5


def test_post_hook_includes_spool_event_id_when_supplied() -> None:
    captured: dict[str, object] = {}

    def opener(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return Response(202, b'{"status":"accepted"}')

    transport = StudentTransport(
        "https://copilot.example",
        student_id="student-1",
        opener=opener,
    )

    transport.post_hook(event(), event_id="spool-event-1")

    assert captured["body"] == {**event().to_dict(), "event_id": "spool-event-1"}


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_post_hook_classifies_retryable_http_failures(status: int) -> None:
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, status, "retry", {}, io.BytesIO(b"secret"))

    transport = StudentTransport("https://copilot.example", student_id="s", token="token", opener=opener)
    with pytest.raises(TemporaryNetworkError) as exc_info:
        transport.post_hook(event())
    assert "secret" not in str(exc_info.value)
    assert "token" not in str(exc_info.value)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_post_hook_classifies_permanent_http_failures(status: int) -> None:
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, status, "bad request", {}, io.BytesIO(b"secret"))

    transport = StudentTransport("https://copilot.example", student_id="s", token="token", opener=opener)
    with pytest.raises(PermanentTransportError):
        transport.post_hook(event())


def test_post_hook_classifies_network_failures() -> None:
    def opener(request, timeout):
        raise urllib.error.URLError("offline")

    transport = StudentTransport("https://copilot.example", student_id="s", token="token", opener=opener)
    with pytest.raises(TemporaryNetworkError):
        transport.post_hook(event())


def test_ack_message_posts_to_existing_student_ack_api_with_rest_auth() -> None:
    captured: dict[str, object] = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return Response(200, b'{"ok":true}')

    transport = StudentTransport(
        "https://copilot.example",
        student_id="student-1",
        token="secret-token",
        opener=opener,
        timeout=2.5,
    )
    result = transport.ack_message("message-1")

    assert result == Accepted(status_code=200, body={"ok": True})
    assert captured["url"] == "https://copilot.example/api/student/messages/ack"
    assert captured["body"] == {"student_id": "student-1", "message_id": "message-1"}
    headers = {str(k).lower(): str(v) for k, v in captured["headers"].items()}
    assert headers["authorization"] == "Bearer secret-token"
    assert headers["x-copilot-token"] == "secret-token"


def test_get_pending_messages_uses_authenticated_student_backlog_contract() -> None:
    captured: dict[str, object] = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["method"] = request.get_method()
        captured["timeout"] = timeout
        return Response(200, b'{"items":[{"type":"mentor_message","message_id":"m-1"}]}')

    transport = StudentTransport(
        "https://copilot.example",
        student_id="student-1",
        token="secret-token",
        opener=opener,
        timeout=2.5,
    )

    assert transport.get_pending_messages(after_id=64) == [{"type": "mentor_message", "message_id": "m-1"}]
    assert captured["url"] == (
        "https://copilot.example/api/student/messages/pending-receipts?"
        "student_id=student-1&limit=64&after_id=64"
    )
    assert captured["method"] == "GET"
    assert captured["timeout"] == 2.5
    headers = {str(k).lower(): str(v) for k, v in captured["headers"].items()}
    assert headers["authorization"] == "Bearer secret-token"
    assert headers["x-copilot-token"] == "secret-token"


def test_ws_send_uses_auth_headers_and_classifies_success() -> None:
    captured: dict[str, object] = {}

    class Socket:
        async def __aenter__(self):
            captured["entered"] = True
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, payload: str) -> None:
            captured["payload"] = json.loads(payload)

    def ws_connect(url: str, headers: dict[str, str]):
        captured["url"] = url
        captured["headers"] = headers
        return Socket()

    transport = StudentTransport(
        "https://copilot.example",
        student_id="student-1",
        token="secret-token",
        ws_connect=ws_connect,
    )
    result = __import__("asyncio").run(transport.send_ws({"type": "heartbeat"}))

    assert result == Accepted(status_code=200, body={})
    assert captured["url"] == "wss://copilot.example/ws?student_id=student-1"
    assert captured["headers"] == {
        "Authorization": "Bearer secret-token",
        "X-Copilot-Token": "secret-token",
    }
    assert captured["payload"] == {"type": "heartbeat"}


def test_ws_send_classifies_disconnect_as_temporary() -> None:
    class OfflineSocket:
        async def __aenter__(self):
            raise OSError("offline")

        async def __aexit__(self, *args):
            return None

    transport = StudentTransport(
        "https://copilot.example",
        student_id="s",
        token="token",
        ws_connect=lambda url, headers: OfflineSocket(),
    )
    with pytest.raises(TemporaryNetworkError):
        __import__("asyncio").run(transport.send_ws({"type": "heartbeat"}))


def test_default_ws_connector_selects_legacy_extra_headers_before_async_enter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Connector:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    # websockets 12 accepts unknown keyword arguments and only raises when the
    # connection is entered. Signature inspection must choose extra_headers
    # before that delayed failure can occur.
    def connect(uri: str, **kwargs):
        if "additional_headers" in kwargs:
            raise AssertionError("new header spelling reached legacy connector")
        captured["uri"] = uri
        return Connector(**kwargs)

    legacy = types.ModuleType("websockets")
    legacy.connect = connect
    monkeypatch.setitem(sys.modules, "websockets", legacy)

    from copilot.student_core.transport import _default_ws_connect

    connector = _default_ws_connect("ws://example/ws", {"Authorization": "Bearer t"})
    assert isinstance(connector, Connector)
    assert captured == {
        "uri": "ws://example/ws",
        "extra_headers": {"Authorization": "Bearer t"},
    }


def test_default_ws_connector_uses_installed_websockets_api_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import websockets
    from copilot.student_core.transport import _default_ws_connect

    real_connect = websockets.connect
    captured: dict[str, object] = {}

    def capture(uri: str, **kwargs):
        captured["uri"] = uri
        captured.update(kwargs)
        return real_connect(uri, **kwargs)

    monkeypatch.setattr(websockets, "connect", capture)
    _default_ws_connect("ws://127.0.0.1:1/ws", {"Authorization": "Bearer t"})

    assert captured["uri"] == "ws://127.0.0.1:1/ws"
    assert captured["additional_headers"] == {"Authorization": "Bearer t"}
    assert captured["proxy"] is None

    captured.clear()
    _default_ws_connect("wss://copilot.example/ws", {"Authorization": "Bearer t"})
    assert captured["uri"] == "wss://copilot.example/ws"
    assert captured["additional_headers"] == {"Authorization": "Bearer t"}
    assert "proxy" not in captured
