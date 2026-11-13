"""MixerClient against a local fake mixer WebSocket server."""

import asyncio

import pytest
import websockets

from soundcraft_ui_mcp import mixer as mixer_mod
from soundcraft_ui_mcp.mixer import MixerClient


class FakeMixerServer:
    def __init__(self) -> None:
        self.received: list[str] = []
        self.clients: set = set()

    async def handler(self, ws) -> None:
        self.clients.add(ws)
        await ws.send("1::")
        await ws.send("3:::SETS^model^ui12\nSETS^firmware^1.0-ui12\nSETD^i.0.mix^0.5")
        await ws.send("3:::SETD^m.mix^0.7")
        try:
            async for msg in ws:
                self.received.append(msg)
        finally:
            self.clients.discard(ws)


@pytest.fixture
async def server():
    fake = FakeMixerServer()
    async with websockets.serve(fake.handler, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        yield fake, f"127.0.0.1:{port}"


async def test_connect_sync_and_send(server, monkeypatch):
    fake, addr = server
    monkeypatch.setattr(mixer_mod, "KEEPALIVE_SEC", 0.05)
    client = MixerClient(addr)
    await client.start()
    assert await client.wait_ready(3)
    assert client.get("model") == "ui12"
    assert client.get_float("m.mix") == 0.7
    client.setd("i.0.mute", 1)
    await asyncio.sleep(0.2)
    assert "3:::SETD^i.0.mute^1" in fake.received
    assert "3:::ALIVE" in fake.received
    await client.stop()
    assert client.status == "closed"


async def test_reconnect_after_drop(server, monkeypatch):
    fake, addr = server
    monkeypatch.setattr(mixer_mod, "RECONNECT_SEC", 0.1)
    client = MixerClient(addr)
    await client.start()
    assert await client.wait_ready(3)
    for ws in list(fake.clients):
        await ws.close()
    await asyncio.sleep(0.05)
    assert not client.ready
    assert await client.wait_ready(3)
    assert client.ready
    await client.stop()


async def test_unreachable_reports_error():
    client = MixerClient("127.0.0.1:1")
    await client.start()
    assert not await client.wait_ready(0.5)
    assert client.last_error
    await client.stop()
