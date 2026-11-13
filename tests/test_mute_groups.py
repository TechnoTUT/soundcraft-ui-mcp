"""Mute groups / MUTE ALL / MUTE FX, following the semantics of the mixer's web app."""

import math

import pytest

from soundcraft_ui_mcp import converters as cv
from soundcraft_ui_mcp.mock import default_state
from soundcraft_ui_mcp.mock_audio import SignalModel
from soundcraft_ui_mcp.paths import Channel, mute_status

from .conftest import wait_ramps

GROUP1 = 1 << 0
MUTE_FX = 1 << 22
MUTE_ALL = 1 << 23


def test_mute_status():
    st = default_state()
    ch = Channel("input", 1)
    assert mute_status(ch, st) == (False, None)
    st["i.0.mgmask"] = GROUP1
    assert mute_status(ch, st) == (False, None)  # member, but group not active
    st["mgmask"] = GROUP1
    assert mute_status(ch, st) == (True, "mute group 1")
    st["i.0.forceunmute"] = 1
    assert mute_status(ch, st)[0] is False
    st["mgmask"] = MUTE_ALL
    st["i.0.forceunmute"] = 0
    assert mute_status(Channel("input", 5), st) == (True, "MUTE ALL")
    st["mgmask"] = MUTE_FX
    assert mute_status(Channel("fx", 1), st) == (True, "MUTE FX")
    assert mute_status(Channel("input", 5), st) == (False, None)


async def test_unmute_inside_active_group_uses_forceunmute(ctl, mixer):
    mixer.state.update({"mgmask": GROUP1, "i.0.mgmask": GROUP1})
    ov = ctl.overview()
    assert ov["active_mute_groups"] == ["group 1"]
    assert ov["input"][0]["mute"] is True and ov["input"][0]["mute_reason"] == "mute group 1"
    res = ctl.set_mute(ctl.channel("input", 1), False)
    assert "forceunmute" in res["note"]
    assert mixer.get("i.0.forceunmute") == 1 and mixer.get("i.0.mute") == 0
    assert mixer.sent[0] == "SETD^i.0.mix^0"  # level dropped before the unmute, then ramped back
    await wait_ramps(mixer)
    assert cv.fader_to_db(mixer.get_float("i.0.mix")) == pytest.approx(-10, abs=0.1)
    assert ctl.overview()["input"][0]["mute"] is False


async def test_mute_clears_forceunmute(ctl, mixer):
    mixer.state.update({"mgmask": GROUP1, "i.0.mgmask": GROUP1, "i.0.forceunmute": 1})
    ctl.set_mute(ctl.channel("input", 1), True)
    assert mixer.get("i.0.forceunmute") == 0
    assert mixer.get("i.0.mute") == 0  # muted by the group again; channel mute left alone like the web app
    assert ctl.overview()["input"][0]["mute"] is True


async def test_plain_mute_unchanged_without_groups(ctl, mixer):
    ctl.set_mute(ctl.channel("input", 2), True)
    assert mixer.sent == ["SETD^i.1.mute^1"]


def test_mock_signal_respects_group_mute():
    st = default_state()
    model = SignalModel.from_profile("demo")
    assert model.compute(st, 0)["input"][0]["post_fader_db"] > -60
    st.update({"mgmask": MUTE_ALL})
    assert model.compute(st, 0)["input"][0]["post_fader_db"] == -math.inf
    st["i.0.forceunmute"] = 1
    assert model.compute(st, 0)["input"][0]["post_fader_db"] > -60
