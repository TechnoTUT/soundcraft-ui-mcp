"""Command line entry point: ``soundcraft-ui-mcp [--transport stdio|http]``."""

from __future__ import annotations

import argparse
import logging
import secrets
import sys

from .config import Settings

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="soundcraft-ui-mcp", description="Soundcraft Ui MCP server")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--mixer-ip", help="mixer IP address (env MIXER_IP)")
    p.add_argument("--host", help="HTTP bind address (env MCP_HTTP_HOST)")
    p.add_argument("--port", type=int, help="HTTP port (env MCP_HTTP_PORT)")
    p.add_argument(
        "--no-auth",
        action="store_true",
        help="serve HTTP without authentication even on a non-localhost address (env MCP_HTTP_NO_AUTH=1)",
    )
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


class HttpAuthError(ValueError):
    pass


def check_http_auth(settings: Settings) -> str | None:
    """Validate the HTTP authentication setup. Returns a warning to log, or raises HttpAuthError."""
    local = settings.http_host in LOCAL_HOSTS
    if settings.http_no_auth and settings.auth_token:
        raise HttpAuthError(
            "MCP_AUTH_TOKEN と --no-auth (MCP_HTTP_NO_AUTH) が両方指定されています。どちらか一方にしてください"
        )
    if settings.auth_token or local:
        return None
    if settings.http_no_auth:
        return (
            f"認証なしで {settings.http_host}:{settings.http_port} を公開しています。"
            "同じネットワークの誰でもミキサーを操作できます"
        )
    raise HttpAuthError(
        "localhost 以外で待ち受ける場合は MCP_AUTH_TOKEN を設定するか、"
        "認証なしで公開することを --no-auth (MCP_HTTP_NO_AUTH=1) で明示してください"
    )


def build_http_app(settings: Settings):  # noqa: ANN201 - Starlette/ASGI app
    from mcp.server.transport_security import TransportSecuritySettings

    from .server import build_server

    mcp = build_server(settings)
    local = settings.http_host in LOCAL_HOSTS
    security = None
    if not local:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = mcp.streamable_http_app(host=settings.http_host, transport_security=security)

    return with_auth(app, settings.auth_token, health=lambda: {"ok": True, "mixer": mcp.mixer.status})


def with_auth(app, token: str | None, health):  # noqa: ANN001, ANN201 - ASGI app
    """Wrap an ASGI app: ``/healthz`` is always open; with a token, every other request needs it."""
    from starlette.responses import JSONResponse

    async def asgi(scope, receive, send):  # noqa: ANN001, ANN202
        if scope["type"] == "http":
            if scope.get("path", "") == "/healthz":
                await JSONResponse(health())(scope, receive, send)
                return
            if token:
                headers = dict(scope.get("headers") or [])
                auth = headers.get(b"authorization", b"").decode()
                if not secrets.compare_digest(auth, f"Bearer {token}"):
                    await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                    return
        await app(scope, receive, send)

    return asgi


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        stream=sys.stderr,  # stdout is reserved for the stdio transport
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    overrides = {
        k: v
        for k, v in {
            "mixer_ip": args.mixer_ip,
            "http_host": args.host,
            "http_port": args.port,
            "http_no_auth": True if args.no_auth else None,
        }.items()
        if v is not None
    }
    if overrides:
        settings = settings.model_copy(update=overrides)

    if args.transport == "stdio":
        from .server import build_server

        build_server(settings).run("stdio")
        return

    try:
        warning = check_http_auth(settings)
    except HttpAuthError as exc:
        sys.exit(str(exc))
    if warning:
        logging.getLogger("soundcraft_ui_mcp").warning(warning)

    import uvicorn

    uvicorn.run(build_http_app(settings), host=settings.http_host, port=settings.http_port, log_level="info")


if __name__ == "__main__":
    main()
