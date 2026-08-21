"""One structured line per HTTP request, for the things a tool-call log cannot see.

``server/toollog.py`` wraps tool handlers and answers "what did this call do". This wraps
the ASGI app and answers "who asked, over what, and did they get in" — which is a
different question and, at step 5, an unanswered one.

It exists for a specific reason rather than as general-purpose logging. **Which MCP
protocol revision does Cowork speak** is one of the build-time verifications the plan
requires an answer to, and the answer only exists in the ``Mcp-Protocol-Version`` header
of a real request from the real client. The obvious place to capture that is the API
Gateway access log, and an HTTP API will not do it: ``$context.request.header.*`` is a
REST API feature, and asking for it fails at ``CreateStage`` with

    BadRequestException: The following context variables are not supported

Moving the whole API to REST to read one header would be a large change to answer a small
question, so the header is read here instead, where it was always available.

Two things it deliberately also records. ``user_agent``, because the connector identifies
itself and that is worth having in the write-up alongside the protocol version. And
``authenticated``, a bare boolean saying whether an ``Authorization`` header was present —
never the token, never a prefix of it. During the Cognito work the question "is Cowork
even sending a bearer token" is the first fork in every debugging session, and inferring
it from a 401 is guessing.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Callable, TextIO

# Set by configure(). stdout in Lambda is CloudWatch Logs, which is the whole delivery
# mechanism — the same arrangement server/toollog.py uses, for the same reason.
_stream: TextIO = sys.stdout


def configure(stream: TextIO) -> None:
    global _stream
    _stream = stream


def _header(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    wanted = name.lower().encode()
    for key, value in headers:
        if key.lower() == wanted:
            return value.decode("latin-1")
    return None


class RequestLogMiddleware:
    """Pure ASGI rather than Starlette's BaseHTTPMiddleware.

    BaseHTTPMiddleware wraps the response in a streaming layer, and this app is served by
    the Lambda Web Adapter in buffered mode with ``json_response=True``. Adding a stream
    where there is deliberately none is a way to find out about it later, in production,
    on the one request being recorded.
    """

    def __init__(self, app: Callable[..., Any]) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers: list[tuple[bytes, bytes]] = scope.get("headers") or []
        started = time.perf_counter()
        status = {"code": 0}

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            line = {
                "event": "http_request",
                "method": scope.get("method"),
                "path": scope.get("path"),
                "status": status["code"],
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "mcp_protocol_version": _header(headers, "mcp-protocol-version"),
                "user_agent": _header(headers, "user-agent"),
                # Presence only. The value is a credential.
                "authenticated": _header(headers, "authorization") is not None,
            }
            print(json.dumps(line), file=_stream, flush=True)
