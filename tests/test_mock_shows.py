"""Shows, snapshots and cues on the mock, driven through MixerClient with the library's commands."""

import asyncio

import pytest
import websockets
from soundcraft_ui_mock import MockMixer

from soundcraft_ui_mcp import converters as cv
from soundcraft_ui_mcp.mixer import MixerClient


@pytest.fixture
async def mock():
    m = MockMixer()
    async with websockets.serve(m.handler, "127.0.0.1", 0) as srv:
        yield m, f"127.0.0.1:{srv.sockets[0].getsockname()[1]}"


async def connect(addr):
    c = MixerClient(addr)
    await c.start()
    assert await c.wait_ready(3)
    return c


async def test_show_lists(mock):
    m, addr = mock
    c = await connect(addr)
    assert await c.shows() == {"Default": {"snapshots": ["* Init *"], "cues": []}}
    await c.stop()


async def test_save_and_load_snapshot(mock):
    m, addr = mock
    a, b = await connect(addr), await connect(addr)
    a.setd("i.0.mix", cv.db_to_fader(-20))
    await asyncio.sleep(0.05)
    a.send_command("SAVESNAPSHOT^Default^verse")
    await asyncio.sleep(0.05)
    assert "verse" in (await a.shows())["Default"]["snapshots"]
    assert b.get("var.currentSnapshot") == "verse"
    a.setd("i.0.mix", cv.db_to_fader(0))
    a.setd("i.1.mute", 1)
    await asyncio.sleep(0.05)
    a.send_command("LOADSNAPSHOT^Default^verse")
    await asyncio.sleep(0.1)
    for client in (a, b):  # the mixer broadcasts the recalled values to every client
        assert cv.fader_to_db(client.get_float("i.0.mix")) == pytest.approx(-20, abs=0.1)
        assert client.get("i.1.mute") == 0
    await a.stop()
    await b.stop()


async def test_safe_channels_are_not_recalled(mock):
    m, addr = mock
    c = await connect(addr)
    c.send_command("SAVESNAPSHOT^Default^base")
    await asyncio.sleep(0.05)
    c.setd("i.2.safe", 1)
    c.setd("i.2.mix", 0.0)
    c.setd("i.3.mix", 0.0)
    await asyncio.sleep(0.05)
    c.send_command("LOADSNAPSHOT^Default^base")
    await asyncio.sleep(0.1)
    assert c.get_float("i.2.mix") == 0.0  # safe: kept
    assert c.get_float("i.3.mix") > 0  # recalled
    await c.stop()


async def test_cues_and_show_switch(mock):
    m, addr = mock
    c = await connect(addr)
    c.send_command("SAVECUE^Default^intro")
    c.send_command("SAVESNAPSHOT^Night^start")
    await asyncio.sleep(0.1)
    shows = await c.shows()
    assert shows["Default"]["cues"] == ["intro"] and shows["Night"]["snapshots"] == ["start"]
    c.send_command("LOADSHOW^Night")
    await asyncio.sleep(0.05)
    assert c.get("var.currentShow") == "Night" and c.get("var.currentSnapshot") == ""
    c.send_command("LOADCUE^Default^intro")
    await asyncio.sleep(0.05)
    assert c.get("var.currentShow") == "Default" and c.get("var.currentCue") == "intro"
    c.send_command("LOADSNAPSHOT^Default^does-not-exist")  # ignored like an unknown name
    await asyncio.sleep(0.05)
    assert c.get("var.currentCue") == "intro"
    await c.stop()
