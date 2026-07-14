"""Testable HTTP/WebSocket boundary for the student client."""
from __future__ import annotations

import json
import inspect
import ipaddress
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .models import HookEvent


class TemporaryNetworkError(RuntimeError):
    """The request may succeed if retried later."""


class PermanentTransportError(RuntimeError):
    """The request was rejected and should not be retried unchanged."""


DEFAULT_PENDING_MESSAGE_LIMIT = 64


@dataclass(frozen=True)
class Accepted:
    status_code: int
    body: dict[str, Any] = field(default_factory=dict)


def _auth_headers(token: str) -> dict[str, str]:
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}", "X-Copilot-Token": token}


def _default_ws_connect(url: str, headers: dict[str, str]):
    import websockets

    connect = websockets.connect
    try:
        parameters = inspect.signature(connect).parameters
    except (TypeError, ValueError):
        parameters = {}
    version = str(getattr(websockets, "__version__", ""))
    try:
        major = int(version.split(".", 1)[0])
    except (TypeError, ValueError):
        major = 0
    if "additional_headers" in parameters:
        header_keyword = "additional_headers"
    elif "extra_headers" in parameters:
        header_keyword = "extra_headers"
    else:
        # Older websockets versions accept arbitrary kwargs and only fail when
        # the async context is entered. Prefer their legacy spelling when
        # introspection cannot distinguish the two APIs.
        header_keyword = "additional_headers" if major >= 14 else "extra_headers"

    kwargs: dict[str, Any] = {header_keyword: headers}
    hostname = urllib.parse.urlsplit(url).hostname or ""
    try:
        is_loopback = hostname.lower() == "localhost" or ipaddress.ip_address(
            hostname,
        ).is_loopback
    except ValueError:
        is_loopback = hostname.lower() == "localhost"
    # websockets 15+ discovers system proxies automatically. A local Student
    # Core connection must never require the optional SOCKS dependency or
    # leave the host through a proxy.
    if is_loopback and ("proxy" in parameters or major >= 15):
        kwargs["proxy"] = None
    return connect(url, **kwargs)


class StudentTransport:
    """Small injectable transport used by the agent and easy to test offline."""

    def __init__(
        self,
        base_url: str,
        *,
        student_id: str,
        token: str = "",
        timeout: float = 5.0,
        opener: Callable[..., Any] | None = None,
        ws_connect: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.student_id = str(student_id)
        self.token = str(token)
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen
        self._ws_connect = ws_connect or _default_ws_connect

    @property
    def auth_headers(self) -> dict[str, str]:
        return _auth_headers(self.token)

    @property
    def ws_url(self) -> str:
        scheme = "wss" if self.base_url.startswith("https://") else "ws"
        host = self.base_url.split("://", 1)[-1]
        query = urllib.parse.urlencode({"student_id": self.student_id})
        return f"{scheme}://{host}/ws?{query}"

    def post_hook(self, event: HookEvent, *, event_id: str = "") -> Accepted:
        payload = event.to_dict()
        configured_student_id = str(self.student_id or "")
        if not configured_student_id:
            raise PermanentTransportError("student identity is required")
        # The configured transport identity is authoritative. Old spool rows
        # may contain a stale id after account rotation; overwrite it instead
        # of either forwarding an impersonation or poisoning the queue.
        payload["student_id"] = configured_student_id
        if event_id:
            payload["event_id"] = str(event_id)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/report",
            data=body,
            headers={"Content-Type": "application/json", **self.auth_headers},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                status_value = getattr(response, "status", None)
                if status_value is None:
                    status_value = response.getcode()
                status = int(status_value)
                raw = response.read()
        except urllib.error.HTTPError as exc:
            self._raise_http_error(exc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TemporaryNetworkError("hook request failed") from exc
        parsed = self._parse_body(raw)
        if 200 <= status < 300:
            return Accepted(status_code=status, body=parsed)
        if 400 <= status < 500:
            raise PermanentTransportError("hook request rejected")
        raise TemporaryNetworkError("hook request unavailable")

    def ack_message(self, message_id: str, *, student_id: str | None = None) -> Accepted:
        """Acknowledge a rendered/received mentor message through the REST API."""
        resolved_student_id = str(self.student_id or "")
        supplied_student_id = str(student_id or "")
        resolved_message_id = str(message_id or "")
        if supplied_student_id and supplied_student_id != resolved_student_id:
            raise PermanentTransportError("student identity mismatch")
        if not resolved_student_id or not resolved_message_id:
            raise PermanentTransportError("message receipt rejected")
        body = json.dumps(
            {"student_id": resolved_student_id, "message_id": resolved_message_id},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/student/messages/ack",
            data=body,
            headers={"Content-Type": "application/json", **self.auth_headers},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                status_value = getattr(response, "status", None)
                if status_value is None:
                    status_value = response.getcode()
                status = int(status_value)
                raw = response.read()
        except urllib.error.HTTPError as exc:
            self._raise_http_error(exc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TemporaryNetworkError("message receipt unavailable") from exc
        parsed = self._parse_body(raw)
        if 200 <= status < 300:
            return Accepted(status_code=status, body=parsed)
        if 400 <= status < 500:
            raise PermanentTransportError("message receipt rejected")
        raise TemporaryNetworkError("message receipt unavailable")

    def get_pending_messages(
        self,
        *,
        limit: int = DEFAULT_PENDING_MESSAGE_LIMIT,
        after_id: int = 0,
    ) -> list[dict[str, Any]]:
        """Fetch the authenticated mentor-message backlog for receipt recovery."""
        student_id = str(self.student_id or "").strip()
        if not student_id:
            raise PermanentTransportError("message backlog rejected")
        query = urllib.parse.urlencode({
            "student_id": student_id,
            "limit": max(1, min(int(limit), DEFAULT_PENDING_MESSAGE_LIMIT)),
            "after_id": max(0, int(after_id)),
        })
        request = urllib.request.Request(
            f"{self.base_url}/api/student/messages/pending-receipts?{query}",
            headers=self.auth_headers,
            method="GET",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                status_value = getattr(response, "status", None)
                if status_value is None:
                    status_value = response.getcode()
                status = int(status_value)
                raw = response.read()
        except urllib.error.HTTPError as exc:
            self._raise_http_error(exc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TemporaryNetworkError("message backlog unavailable") from exc
        if 200 <= status < 300:
            items = self._parse_body(raw).get("items", [])
            return [dict(item) for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []
        if 400 <= status < 500:
            raise PermanentTransportError("message backlog rejected")
        raise TemporaryNetworkError("message backlog unavailable")

    def open_ws(self) -> Any:
        """Return a long-lived authenticated WebSocket context manager."""
        return self._ws_connect(self.ws_url, self.auth_headers)

    async def send_ws(self, payload: Mapping[str, Any]) -> Accepted:
        try:
            async with self.open_ws() as socket:
                await socket.send(json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")))
        except (PermanentTransportError, TemporaryNetworkError):
            raise
        except Exception as exc:
            raise TemporaryNetworkError("student websocket unavailable") from exc
        return Accepted(status_code=200)

    @staticmethod
    def _parse_body(raw: bytes) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _raise_http_error(exc: urllib.error.HTTPError) -> None:
        status = int(exc.code)
        if status == 408 or status == 429 or 500 <= status < 600:
            raise TemporaryNetworkError("hook request unavailable") from exc
        raise PermanentTransportError("hook request rejected") from exc
