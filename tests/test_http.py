"""HTTP transport: authentication rules and the auth wrapper."""

import json

import pytest

from soundcraft_ui_mcp.__main__ import HttpAuthError, check_http_auth, with_auth
from soundcraft_ui_mcp.config import Settings


def test_localhost_needs_no_auth():
    assert check_http_auth(Settings()) is None


def test_lan_requires_token_or_explicit_no_auth():
    with pytest.raises(HttpAuthError):
        check_http_auth(Settings(http_host="0.0.0.0"))
    assert check_http_auth(Settings(http_host="0.0.0.0", auth_token="t")) is None
    warning = check_http_auth(Settings(http_host="0.0.0.0", http_no_auth=True))
    assert warning and "認証なし" in warning


def test_token_and_no_auth_conflict():
    with pytest.raises(HttpAuthError):
        check_http_auth(Settings(http_host="0.0.0.0", auth_token="t", http_no_auth=True))


def test_no_auth_from_env(monkeypatch):
    monkeypatch.setenv("MCP_HTTP_NO_AUTH", "1")
    assert Settings.from_env().http_no_auth is True


async def call(app, path="/mcp", headers=()):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": path, "headers": list(headers), "query_string": b""}
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], body


async def inner(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"inner"})


async def test_without_token_requests_pass_through():
    assert await call(with_auth(inner, None, health=dict)) == (200, b"inner")


async def test_with_token():
    app = with_auth(inner, "secret", health=lambda: {"ok": True})
    assert (await call(app))[0] == 401
    assert (await call(app, headers=[(b"authorization", b"Bearer wrong")]))[0] == 401
    assert await call(app, headers=[(b"authorization", b"Bearer secret")]) == (200, b"inner")
    status, body = await call(app, path="/healthz")
    assert status == 200 and json.loads(body) == {"ok": True}
