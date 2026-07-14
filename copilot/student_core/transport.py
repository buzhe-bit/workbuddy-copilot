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


class StudentAskNotFound(PermanentTransportError):
    """No server reservation exists, so the same keyed POST may be retried."""


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

    async def post_hook_async(self, event: HookEvent, *, event_id: str = "") -> Accepted:
        """Run the compatibility HTTP client away from the sole WS loop."""
        import asyncio

        return await asyncio.to_thread(self.post_hook, event, event_id=event_id)

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

    async def ack_message_async(
        self,
        message_id: str,
        *,
        student_id: str | None = None,
    ) -> Accepted:
        import asyncio

        return await asyncio.to_thread(
            self.ack_message,
            message_id,
            student_id=student_id,
        )

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

    async def get_pending_messages_async(
        self,
        *,
        limit: int = DEFAULT_PENDING_MESSAGE_LIMIT,
        after_id: int = 0,
    ) -> list[dict[str, Any]]:
        import asyncio

        return await asyncio.to_thread(
            self.get_pending_messages,
            limit=limit,
            after_id=after_id,
        )

    def get_recent_analyses(
        self,
        *,
        after_analysis_id: int = 0,
        limit: int = DEFAULT_PENDING_MESSAGE_LIMIT,
    ) -> dict[str, Any]:
        """Fetch one authenticated page in durable analysis-commit order."""
        student_id = str(self.student_id or "").strip()
        if not student_id:
            raise PermanentTransportError("analysis backlog rejected")
        query = urllib.parse.urlencode({
            "student_id": student_id,
            "after_analysis_id": max(0, int(after_analysis_id)),
            "limit": max(1, min(int(limit), DEFAULT_PENDING_MESSAGE_LIMIT)),
        })
        request = urllib.request.Request(
            f"{self.base_url}/api/student/analyses?{query}",
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
            raise TemporaryNetworkError("analysis backlog unavailable") from exc
        if 200 <= status < 300:
            body = self._parse_body(raw)
            items = body.get("items", [])
            return {
                "items": (
                    [dict(item) for item in items if isinstance(item, Mapping)]
                    if isinstance(items, list)
                    else []
                ),
                "next_cursor": max(0, int(body.get("next_cursor") or 0)),
                "has_more": bool(body.get("has_more")),
            }
        if 400 <= status < 500:
            raise PermanentTransportError("analysis backlog rejected")
        raise TemporaryNetworkError("analysis backlog unavailable")

    async def get_recent_analyses_async(
        self,
        *,
        after_analysis_id: int = 0,
        limit: int = DEFAULT_PENDING_MESSAGE_LIMIT,
    ) -> dict[str, Any]:
        import asyncio

        return await asyncio.to_thread(
            self.get_recent_analyses,
            after_analysis_id=after_analysis_id,
            limit=limit,
        )

    def ask(
        self,
        question: str,
        *,
        session_id: str | None = None,
        client_request_id: str | None = None,
    ) -> Accepted:
        """Submit one student question with an optional retry-safe client key."""
        student_id = str(self.student_id or "").strip()
        normalized_question = str(question or "").strip()
        if not student_id or not normalized_question:
            raise PermanentTransportError("student ask rejected")
        payload: dict[str, Any] = {
            "student_id": student_id,
            "question": normalized_question,
        }
        normalized_session_id = str(session_id or "").strip()
        if normalized_session_id:
            payload["session_id"] = normalized_session_id
        normalized_request_id = str(client_request_id or "")
        if normalized_request_id:
            payload["client_request_id"] = normalized_request_id
        request = urllib.request.Request(
            f"{self.base_url}/api/student/ask",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
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
            raise TemporaryNetworkError("student ask unavailable") from exc
        parsed = self._parse_body(raw)
        if 200 <= status < 300:
            return Accepted(status_code=status, body=parsed)
        if 400 <= status < 500:
            raise PermanentTransportError("student ask rejected")
        raise TemporaryNetworkError("student ask unavailable")

    async def ask_async(
        self,
        question: str,
        *,
        session_id: str | None = None,
        client_request_id: str | None = None,
    ) -> Accepted:
        import asyncio

        return await asyncio.to_thread(
            self.ask,
            question,
            session_id=session_id,
            client_request_id=client_request_id,
        )

    def get_ask_by_client_request(self, client_request_id: str) -> dict[str, Any]:
        """Recover a pending or terminal ask after an uncertain POST response."""
        student_id = str(self.student_id or "").strip()
        request_id = str(client_request_id or "").strip()
        if not student_id or not request_id:
            raise PermanentTransportError("student ask recovery rejected")
        encoded_request_id = urllib.parse.quote(request_id, safe="")
        query = urllib.parse.urlencode({"student_id": student_id})
        request = urllib.request.Request(
            f"{self.base_url}/api/student/asks/by-client-request/"
            f"{encoded_request_id}?{query}",
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
            if int(exc.code) == 404:
                raise StudentAskNotFound(
                    "student ask recovery row is missing"
                ) from exc
            self._raise_http_error(exc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TemporaryNetworkError("student ask recovery unavailable") from exc
        if 200 <= status < 300:
            return self._parse_body(raw)
        if 400 <= status < 500:
            raise PermanentTransportError("student ask recovery rejected")
        raise TemporaryNetworkError("student ask recovery unavailable")

    async def get_ask_by_client_request_async(
        self,
        client_request_id: str,
    ) -> dict[str, Any]:
        import asyncio

        return await asyncio.to_thread(
            self.get_ask_by_client_request,
            client_request_id,
        )

    def submit_ask_feedback(
        self,
        ask_id: int,
        feedback: str,
        *,
        note: str = "",
    ) -> Accepted:
        """Submit the student's explicit helpful/unresolved judgment."""
        student_id = str(self.student_id or "").strip()
        normalized_feedback = str(feedback or "").strip()
        if (
            not student_id
            or int(ask_id) <= 0
            or normalized_feedback not in {"helpful", "unresolved"}
        ):
            raise PermanentTransportError("student ask feedback rejected")
        payload = {
            "student_id": student_id,
            "feedback": normalized_feedback,
            "note": str(note or "").strip(),
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/student/asks/{int(ask_id)}/feedback",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
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
            raise TemporaryNetworkError("student ask feedback unavailable") from exc
        parsed = self._parse_body(raw)
        if 200 <= status < 300:
            return Accepted(status_code=status, body=parsed)
        if 400 <= status < 500:
            raise PermanentTransportError("student ask feedback rejected")
        raise TemporaryNetworkError("student ask feedback unavailable")

    async def submit_ask_feedback_async(
        self,
        ask_id: int,
        feedback: str,
        *,
        note: str = "",
    ) -> Accepted:
        import asyncio

        return await asyncio.to_thread(
            self.submit_ask_feedback,
            ask_id,
            feedback,
            note=note,
        )

    def post_sync(self, sessions: list[Mapping[str, Any]]) -> Accepted:
        student_id = str(self.student_id or "").strip()
        if not student_id:
            raise PermanentTransportError("session sync rejected")
        body = json.dumps(
            {"student_id": student_id, "sessions": [dict(item) for item in sessions]},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/sessions/sync",
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
            raise TemporaryNetworkError("session sync unavailable") from exc
        parsed = self._parse_body(raw)
        if 200 <= status < 300 and parsed.get("ok") is True:
            return Accepted(status, parsed)
        if 400 <= status < 500:
            raise PermanentTransportError("session sync rejected")
        raise TemporaryNetworkError("session sync unavailable")

    async def post_sync_async(self, sessions: list[Mapping[str, Any]]) -> Accepted:
        import asyncio

        return await asyncio.to_thread(self.post_sync, sessions)

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
