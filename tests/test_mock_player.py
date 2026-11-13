"""Media player on the mock, driven with soundcraft-ui's MEDIA_* commands."""

import asyncio

import pytest
import websockets

from soundcraft_ui_mcp.mixer import MixerClient
from soundcraft_ui_mcp.mock import DEMO_MEDIA, MockMixer, default_state
from soundcraft_ui_mcp.mock_audio import SignalModel

PLAYING, PAUSED, STOPPED = 2, 3, 0


@pytest.fixture
async def mock():
    media = {"Short": [("a.mp3", 2.0), ("b.mp3", 2.0)], **DEMO_MEDIA}
    m = MockMixer(media=media, player_speed=10.0, vu_rate=0, rta_rate=0)
    async with websockets.serve(m.handler, "127.0.0.1", 0) as srv:
        await m.start()
        c = MixerClient(f"127.0.0.1:{srv.sockets[0].getsockname()[1]}")
        await c.start()
        assert await c.wait_ready(3)
        yield m, c
        await c.stop()
        await m.stop()


async def until(cond, timeout=3.0):
    async def _wait():
        while not cond():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_wait(), timeout)


async def test_playlists(mock):
    m, c = mock
    lists = await c.playlists()
    assert list(lists) == ["Short", "DJ Set", "BGM"]
    assert lists["DJ Set"] == ["01 Warmup.mp3", "02 Peak Time.mp3", "03 Closing.mp3"]


async def test_transport(mock):
    m, c = mock
    c.send_command("MEDIA_SWITCH_PLIST^DJ Set")
    await until(lambda: c.get("var.currentTrack") == "01 Warmup.mp3")
    assert c.get("var.currentLength") == 262
    c.send_command("MEDIA_PLAY")
    await until(lambda: c.get("var.currentState") == PLAYING)
    await until(lambda: c.get_float("var.currentTrackPos") > 0)  # position advances
    c.send_command("MEDIA_PAUSE")
    await until(lambda: c.get("var.currentState") == PAUSED)
    c.send_command("MEDIA_NEXT")
    await until(lambda: c.get("var.currentTrack") == "02 Peak Time.mp3")
    c.send_command("MEDIA_PREV")
    await until(lambda: c.get("var.currentTrack") == "01 Warmup.mp3")
    c.send_command("MEDIA_SWITCH_TRACK^BGM^Lounge B.mp3")
    await until(lambda: c.get("var.currentTrack") == "Lounge B.mp3")
    assert c.get("var.currentPlaylist") == "BGM"
    c.send_command("MEDIA_STOP")
    await until(lambda: c.get("var.currentState") == STOPPED)
    assert c.get_float("var.currentTrackPos") == 0


async def test_end_of_track_manual_and_auto(mock):
    m, c = mock
    c.send_command("MEDIA_SWITCH_PLIST^Short")
    c.send_command("MEDIA_PLAY")
    await until(lambda: c.get("var.currentState") == PLAYING)
    await until(lambda: c.get("var.currentState") == STOPPED, 5)
    assert c.get("var.currentTrack") == "a.mp3"  # manual mode stops at the end of the track
    assert c.get_float("var.currentTrackPos") == 0
    c.setd("settings.playMode", 3)  # auto
    await asyncio.sleep(0.05)
    c.send_command("MEDIA_PLAY")
    await until(lambda: c.get("var.currentTrack") == "b.mp3", 5)
    assert c.get("var.currentState") == PLAYING


async def test_shuffle_is_reported_as_var(mock):
    m, c = mock
    c.setd("settings.shuffle", 1)
    await until(lambda: c.get("var.shuffle") == 1)


def test_player_signal_only_while_playing():
    st = default_state()
    model = SignalModel.from_profile("demo")
    assert model.compute(st, 0)["player"][0]["pre_db"] == float("-inf")
    st["var.currentState"] = PLAYING
    assert model.compute(st, 0)["player"][0]["pre_db"] > -40
