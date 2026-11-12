"""Batas ukuran body request untuk endpoint non-upload (perlindungan RAM Pi 3).

FastAPI membaca body JSON utuh ke memori. Tanpa batas, satu request raksasa ke
endpoint publik seperti /auth/login bisa menghabiskan RAM 1 GB. Endpoint upload
(yang men-stream body ke disk dengan batasnya sendiri) dikecualikan.
"""

from __future__ import annotations

import json
import re

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_STREAMING_ROUTES = (
    ("POST", re.compile(r"^/api/v1/files/?$")),
    ("PUT", re.compile(r"^/api/v1/uploads/[^/]+/chunks/[^/]+/?$")),
    ("POST", re.compile(r"^/api/v1/uploads/[^/]+/chunks/[^/]+/?$")),
)


def _is_streaming_upload(scope: Scope) -> bool:
    method = scope.get("method", "")
    path = scope.get("path", "")
    return any(method == m and pattern.match(path) for m, pattern in _STREAMING_ROUTES)


async def _send_413(send: Send, limit: int) -> None:
    body = json.dumps(
        {"detail": f"Body request terlalu besar (maksimal {limit} byte)", "code": "request_too_large"}
    ).encode()
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close")],
    })
    await send({"type": "http.response.body", "body": body})


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _is_streaming_upload(scope):
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                if declared > self.max_bytes:
                    await _send_413(send, self.max_bytes)
                    return
                break

        received = 0
        exceeded = False
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    exceeded = True
                    # Hentikan pembacaan: aplikasi melihat klien terputus.
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if exceeded:
                return  # respons aplikasi diganti 413
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded and not response_started:
            await _send_413(send, self.max_bytes)
