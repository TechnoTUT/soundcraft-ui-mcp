import asyncio
import math

import pytest
import websockets
from soundcraft_ui_mock import MockMixer, SignalModel

from soundcraft_ui_mcp.config import Settings
from soundcraft_ui_mcp.controller import MixerController
from soundcraft_ui_mcp.meters import (
    comp_byte,
    comp_reduction_db,
    db_to_vu_byte,
    decode_rta,
    decode_vu2,
    encode_rta,
    encode_vu2,
)
from soundcraft_ui_mcp.mixer import MixerClient

# captured from a silent Ui12 (and identical to vuMsg12_1 in fmalcher/soundcraft-ui)
UI12_SILENT = (
    "CAIEBAQCAgAAAAAAAPcAAAAAAPcAAAAAAPcAAAAAAPcAAAAAAPcAAAAAAPcAAAAAAPcAAAAAAPcAAAAAAPcAAAAAAPcAAAAAAAD3AAAAAAAA9w"
    "AAAAAAAPcAAAAAAAD3AAAAAAAA9wAAAAAAAPcAAAAAAAD3AAAAAAAA9wAAAAD3AAAAAPcAAAAA9wAAAAD3AAAAAPcAAAAA9wAAAAAA9wAAAAAA9w=="
)
# vuMsg24_2 from fmalcher/soundcraft-ui (Ui24R with signal)
UI24_SIGNAL = (
    "GAIGBAoCAgCin6eioPein6eiofcAAAAAAPcAAAAAAPcAAAAAAPcAAAAAAPcAAAAAAPcAAAAAAPcAAAAAAPcAAAAAAPcmJAAAIfcAAAAAAPcAAAAAAPc"
    "AAAAAAPcAAAAAAPcAAAAAAPcAAAAAAPcAAAAAAPcAAAAAAHcMAAAHAHcAAAAAAPcAAAAAAPcAAAAAAPcAAAAAAPcAAAAAAPcAAAAAAPeop6emqKj3AA"
    "AAAAAA9wAAAAAAAPcAAAAAAAD3AAAAAAAA9wAAAAAAAPcAAAAAAAD3oZ6npKGe9wAAAAAAAPcAAAAAAAD3d3h3d/d2d3Z29x4gHh33HiAeHfcAAAAA9w"
    "AAAAD3AAAAAPcAAAAA925Ub232bFJubfewrLCt966qAK33AAAAAAD3AAAAAAD3"
)


def test_decode_real_ui12_silent_frame():
    f = decode_vu2(UI12_SILENT)
    assert (len(f.input), len(f.player), len(f.sub), len(f.fx), len(f.aux), len(f.master), len(f.line)) == (
        8, 2, 4, 4, 4, 2, 2,
    )  # fmt: skip
    assert f.input[0]["pre_db"] == -80
    assert f.input[0]["comp_reduction_db"] == 0  # 0xF7 = no gain reduction


def test_decode_ui24_with_signal():
    f = decode_vu2(UI24_SIGNAL)
    assert len(f.input) == 24 and len(f.aux) == 10
    assert f.input[0]["pre_db"] == pytest.approx(-26, abs=0.1)  # 0xa2 = 162 -> -26 dB on the -80..0 scale
    assert f.master[0]["post_fader_db"] > -40


def test_vu_roundtrip():
    levels = {
        "input": [{"pre_db": -12, "post_db": -14, "post_fader_db": -20, "dyn_in_db": -14, "dyn_out_db": -18,
                   "comp_reduction_db": 6}],
        "player": [], "sub": [], "fx": [], "line": [],
        "aux": [{"post_db": -30, "post_fader_db": -36}],
        "master": [{"post_db": -10, "post_fader_db": -10}, {"post_db": -11, "post_fader_db": -11}],
    }  # fmt: skip
    f = decode_vu2(encode_vu2(levels))
    assert f.input[0]["pre_db"] == pytest.approx(-12, abs=0.4)
    assert f.input[0]["post_fader_db"] == pytest.approx(-20, abs=0.4)
    assert f.input[0]["comp_reduction_db"] == pytest.approx(6, abs=0.7)
    assert f.aux[0]["post_fader_db"] == pytest.approx(-36, abs=0.4)
    assert f.master[1]["post_fader_db"] == pytest.approx(-11, abs=0.4)


def test_byte_helpers():
    assert comp_byte(0) == 0xF7
    assert comp_reduction_db(0xF7) == 0
    assert db_to_vu_byte(0) == 240
    assert db_to_vu_byte(-math.inf) == 0
    assert decode_rta(encode_rta([-80, -40, 0])) == [-80, pytest.approx(-40, abs=0.4), pytest.approx(0, abs=0.4)]


# --- mock + client -------------------------------------------------------------


@pytest.fixture
async def running_mock():
    m = MockMixer(signal=SignalModel.from_profile("demo"), vu_rate=50, rta_rate=50)
    async with websockets.serve(m.handler, "127.0.0.1", 0) as srv:
        await m.start_meters()
        yield m, f"127.0.0.1:{srv.sockets[0].getsockname()[1]}"
        await m.stop_meters()


async def test_client_receives_meters_and_rta(running_mock):
    m, addr = running_mock
    c = MixerClient(addr)
    await c.start()
    assert await c.wait_ready(3)
    await asyncio.sleep(0.3)
    assert c.vu is not None and c.rta is not None and len(c.rta) == 120
    ctl = MixerController(c, Settings())
    meters = ctl.get_meters()
    assert meters["available"] and meters["input"][0]["name"] == "CH 1"
    assert meters["input"][0]["pre_db"] > -30
    assert meters["input"][4]["pre_db"] == -80  # no source on input 5
    spectrum = ctl.get_spectrum()
    assert spectrum["available"] and len(spectrum["bands"]) == 31
    # muting input 1 shows up in the meters
    ctl.set_mute(ctl.channel("input", 1), True)
    await asyncio.sleep(0.3)
    assert ctl.get_meters()["input"][0]["post_fader_db"] == -80
    await c.stop()


async def test_silent_profile_sends_vu_only_on_change(running_mock):
    m = MockMixer(signal=SignalModel.from_profile("silent"), vu_rate=50, rta_rate=0)
    sent: list[str] = []

    async def fake_broadcast(line, exclude=None):
        sent.append(line)

    m.broadcast = fake_broadcast  # type: ignore[method-assign]
    m.clients.add(object())  # type: ignore[arg-type]
    await m.start_meters()
    await asyncio.sleep(0.2)
    await m.stop_meters()
    assert len([s for s in sent if s.startswith("VU2^")]) == 1
