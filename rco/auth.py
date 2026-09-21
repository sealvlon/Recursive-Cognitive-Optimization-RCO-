"""Outermost pure-ASGI layer (design section 3): the bearer token alone decides who is calling.

Order: (a) the lifespan scope passes through untouched, so the SDK session manager starts; a websocket is
closed (1008) and any other scope dropped, never passed through unauthenticated;
(b) any path but /mcp and /mcp/ gets a plain 404, with no auth check and no WWW-Authenticate, so no
OAuth discovery is invited; (c) the raw Authorization bytes are compared with every client's
"Bearer <token>" bytes (hmac.compare_digest, no early exit; bytes because a non-ASCII str would make
compare_digest raise and turn a hostile header into a 500); (d) the inner app.
"""

import hmac
from collections.abc import Mapping
from typing import Any, Protocol

MCP_PATHS = ("/mcp", "/mcp/")


class AuthObserver(Protocol):
    def request_started(self, client_id: str, protocol_version: str | None) -> None: ...
    def request_finished(self, client_id: str) -> None: ...
    def auth_rejected(self, reason: str, env_var: str | None) -> None: ...


def _header(scope: dict[str, Any], name: bytes) -> bytes | None:
    for k, v in scope.get("headers") or ():
        if k == name:
            return v
    return None


async def _respond(send: Any, status: int, body: bytes, content_type: bytes, *extra: tuple[bytes, bytes]) -> None:
    headers = [(b"content-type", content_type), (b"content-length", str(len(body)).encode()), *extra]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class BearerAuth:
    def __init__(
        self, app: Any, tokens: Mapping[str, str], auth_env: Mapping[str, str], observer: AuthObserver
    ) -> None:
        """tokens: client id -> token value; auth_env: client id -> env-var name (for placeholder detection)."""
        self.app = app
        self.observer = observer
        self._expected = [(cid, b"Bearer " + tok.encode("utf-8")) for cid, tok in tokens.items()]
        self._placeholders = {("${%s}" % var).encode("ascii"): var for var in auth_env.values()}

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            if scope["type"] == "websocket":  # only if a websocket library is ever installed
                await send({"type": "websocket.close", "code": 1008})
            return
        if scope["path"] not in MCP_PATHS:
            await _respond(send, 404, b"Not Found", b"text/plain; charset=utf-8")
            return
        header = _header(scope, b"authorization")
        client_id = self._match(header)
        if client_id is None:
            self.observer.auth_rejected(*self._classify(header))
            await _respond(
                send, 401, b'{"error":"unauthorized"}', b"application/json",
                (b"www-authenticate", b'Bearer realm="rco"'),
            )
            return
        scope.setdefault("state", {})["rco_client"] = client_id
        pv = _header(scope, b"mcp-protocol-version")
        self.observer.request_started(client_id, pv.decode("latin-1") if pv else None)
        try:
            await self.app(scope, receive, send)
        finally:
            self.observer.request_finished(client_id)

    def _match(self, header: bytes | None) -> str | None:
        presented = header or b""
        found = None
        for cid, expected in self._expected:  # every token is compared: no early exit
            if hmac.compare_digest(presented, expected) and found is None:
                found = cid
        return found

    def _classify(self, header: bytes | None) -> tuple[str, str | None]:
        if header is None:
            return "missing", None
        scheme, _, credential = header.partition(b" ")
        var = self._placeholders.get(credential.strip())
        if scheme.lower() == b"bearer" and var is not None:
            return "placeholder", var
        return "unknown", None
