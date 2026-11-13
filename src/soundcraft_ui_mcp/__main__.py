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
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def build_http_app(settings: Settings):  # noqa: ANN201 - Starlette/ASGI app
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import JSONResponse

    from .server import build_server

    mcp = build_server(settings)
    local = settings.http_host in LOCAL_HOSTS
    security = None
    if not local:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = mcp.streamable_http_app(host=settings.http_host, transport_security=security)

    token = settings.auth_token

    async def asgi(scope, receive, send):  # noqa: ANN001, ANN202
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == "/healthz":
                await JSONResponse({"ok": True, "mixer": mcp.mixer.status})(scope, receive, send)
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
        for k, v in {"mixer_ip": args.mixer_ip, "http_host": args.host, "http_port": args.port}.items()
        if v is not None
    }
    if overrides:
        settings = settings.model_copy(update=overrides)

    if args.transport == "stdio":
        from .server import build_server

        build_server(settings).run("stdio")
        return

    if settings.http_host not in LOCAL_HOSTS and not settings.auth_token:
        sys.exit("localhost 以外で待ち受ける場合は MCP_AUTH_TOKEN を設定してください")

    import uvicorn

    uvicorn.run(build_http_app(settings), host=settings.http_host, port=settings.http_port, log_level="info")


if __name__ == "__main__":
    main()
