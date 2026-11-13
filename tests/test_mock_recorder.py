"""2-track USB recorder and the no-USB mode of the mock."""

import asyncio

import pytest
import websockets
from soundcraft_ui_mock import MockMixer

from soundcraft_ui_mcp.mixer import MixerClient


async def serve(m):
    srv = await websockets.serve(m.handler, "127.0.0.1", 0)
    c = MixerClient(f"127.0.0.1:{srv.sockets[0].getsockname()[1]}")
    await c.start()
    assert await c.wait_ready(3)
    return srv, c


@pytest.fixture
async def with_usb():
    m = MockMixer(vu_rate=0, rta_rate=0)
    srv, c = await serve(m)
    yield m, c
    await c.stop()
    srv.close()


@pytest.fixture
async def no_usb():
    m = MockMixer(vu_rate=0, rta_rate=0, usb=False)
    srv, c = await serve(m)
    yield m, c
    await c.stop()
    srv.close()


async def test_record_toggle_adds_recording(with_usb):
    m, c = with_usb
    assert c.get("var.isRecording") == 0 and c.get("var.recBusy") == 0
    c.send_command("RECTOGGLE")
    await asyncio.sleep(0.1)
    assert c.get("var.isRecording") == 1
    c.send_command("RECTOGGLE")
    await asyncio.sleep(0.1)
    assert c.get("var.isRecording") == 0
    assert (await c.playlists())["Recordings"] == ["REC0001.wav"]


async def test_no_usb_mode(no_usb):
    m, c = no_usb
    # values seen on a real Ui12 without a USB stick
    assert c.get("var.currentState") == 1 and c.get("var.recBusy") == 1 and c.get("var.usbfill") == 0
    assert await c.playlists() == {}
    c.send_command("RECTOGGLE")
    c.send_command("MEDIA_PLAY")
    await asyncio.sleep(0.1)
    assert c.get("var.isRecording") == 0 and c.get("var.currentState") == 1
