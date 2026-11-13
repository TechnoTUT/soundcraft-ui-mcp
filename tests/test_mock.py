"""The mock mixer, driven by the real MixerClient / MixerController."""

import asyncio

import pytest
import websockets

from soundcraft_ui_mcp import converters as cv
from soundcraft_ui_mcp.config import Settings
from soundcraft_ui_mcp.controller import MixerController
from soundcraft_ui_mcp.mixer import MixerClient
from soundcraft_ui_mcp.mock import MockMixer, default_state, load_state

from .conftest import wait_ramps


@pytest.fixture
async def mock():
    m = MockMixer()
    async with websockets.serve(m.handler, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        yield m, f"127.0.0.1:{port}"


async def connect(addr: str) -> MixerClient:
    c = MixerClient(addr, tick_sec=0.01)
    await c.start()
    assert await c.wait_ready(3)
    return c


def test_default_state_shape():
    st = default_state("ui12")
    assert st["model"] == "ui12"
    assert "i.7.mix" in st and "i.8.mix" not in st
    assert "a.3.mix" in st and "a.4.mix" not in st
    assert "i.0.aux.3.value" in st and "i.0.eq.b4.gain" in st and "a.0.dyn.outgain" in st
    assert len(default_state("ui24")) > len(st)


async def test_client_syncs_full_state(mock):
    m, addr = mock
    c = await connect(addr)
    assert c.get("model") == "ui12"
    assert len(c.state) == len(m.state)
    await c.stop()


async def test_commands_update_and_broadcast(mock):
    m, addr = mock
    a, b = await connect(addr), await connect(addr)
    a.setd("i.2.mute", 1)
    await asyncio.sleep(0.1)
    assert m.state["i.2.mute"] == 1
    assert b.get("i.2.mute") == 1  # broadcast to the other client
    assert "SETD^i.2.mute^1" in m.received
    await a.stop()
    await b.stop()


async def test_controller_against_mock(mock):
    m, addr = mock
    c = await connect(addr)
    ctl = MixerController(c, Settings(raise_rate_db_per_sec=200, ramp_tick_sec=0.01))
    ctl.set_fader(ctl.channel("input", 1), db=-3)
    await wait_ramps(c)
    await asyncio.sleep(0.05)
    assert cv.fader_to_db(float(m.state["i.0.mix"])) == pytest.approx(-3, abs=0.1)
    ctl.set_aux_send(2, ctl.channel("input", 1), db=-12)
    await wait_ramps(c)
    await asyncio.sleep(0.05)
    assert cv.fader_to_db(float(m.state["i.0.aux.1.value"])) == pytest.approx(-12, abs=0.1)
    await c.stop()


async def test_external_change_on_mock_cancels_fade(mock):
    m, addr = mock
    c = await connect(addr)
    ctl = MixerController(c, Settings(ramp_tick_sec=0.01))  # production speed: 1 dB/s
    ctl.set_fader(ctl.channel("input", 1), db=0)
    await asyncio.sleep(0.1)
    await m.set_external("i.0.mix", 0.3)  # someone grabs the fader on the web UI
    await asyncio.sleep(0.1)
    assert c.active_ramps() == []
    assert c.get_float("i.0.mix") == 0.3
    await c.stop()


def test_save_and_load(tmp_path):
    m = MockMixer()
    m.state["i.0.name"] = "DJ L"
    f = tmp_path / "state.json"
    m.save(f)
    assert load_state(f)["i.0.name"] == "DJ L"
