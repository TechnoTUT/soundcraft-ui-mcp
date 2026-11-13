import asyncio

import pytest

from soundcraft_ui_mcp import converters as cv
from soundcraft_ui_mcp.config import Settings
from soundcraft_ui_mcp.controller import MixerController
from soundcraft_ui_mcp.safety import SafetyError, db_rate_fn

from .conftest import wait_ramps


def db(mixer, path):
    return cv.fader_to_db(mixer.get_float(path))


async def test_lowering_is_immediate(ctl, mixer):
    res = ctl.set_fader(ctl.channel("input", 1), db=-20)
    assert "即時" in res["mode"]
    assert mixer.active_ramps() == []
    assert db(mixer, "i.0.mix") == -20
    assert mixer.sent == [f"SETD^i.0.mix^{cv.db_to_fader(-20)!r}"]


async def test_raising_ramps_slowly(ctl, mixer):
    res = ctl.set_fader(ctl.channel("input", 1), db=-4)
    assert res["estimated_sec"] > 0
    assert mixer.active_ramps(), "raise must run as a background ramp"
    assert db(mixer, "i.0.mix") < -4  # not jumped yet
    await wait_ramps(mixer)
    assert db(mixer, "i.0.mix") == pytest.approx(-4, abs=0.1)
    values = [float(m.split("^")[2]) for m in mixer.sent]
    assert len(values) > 3
    assert values == sorted(values)


def test_raise_rate_in_production_settings():
    s = Settings()
    rate = db_rate_fn(s)
    assert rate(-10) == 1.0  # 1 dB/s in the audible region
    assert rate(-40) == 3.0  # faster below the quiet zone
    assert db_rate_fn(s, cap=0.5)(-10) == 0.5  # fade_ms may only slow it down
    assert db_rate_fn(s, cap=50)(-10) == 1.0


async def test_upper_limit_requires_confirm(ctl, mixer):
    with pytest.raises(SafetyError):
        ctl.set_fader(ctl.channel("master"), db=3)
    assert mixer.sent == []
    ctl.set_fader(ctl.channel("master"), db=3, confirm=True)
    await wait_ramps(mixer)
    assert db(mixer, "m.mix") == pytest.approx(3, abs=0.1)


async def test_delta(ctl, mixer):
    ctl.set_fader(ctl.channel("input", 2), delta_db=-6)
    assert db(mixer, "i.1.mix") == pytest.approx(-16, abs=0.1)
    with pytest.raises(ValueError):
        ctl.set_fader(ctl.channel("input", 2))


async def test_lower_with_fade(ctl, mixer):
    ctl.set_fader(ctl.channel("input", 1), db=float("-inf"), fade_ms=100)
    assert mixer.active_ramps()
    await wait_ramps(mixer)
    assert mixer.get_float("i.0.mix") == 0.0


async def test_new_command_cancels_ramp(ctl, mixer, settings):
    slow = MixerController(mixer, settings.model_copy(update={"raise_rate_db_per_sec": 1.0}))
    slow.set_fader(slow.channel("input", 1), db=0)
    await asyncio.sleep(0.05)
    slow.set_fader(slow.channel("input", 1), db=-30)  # emergency down
    assert mixer.active_ramps() == []
    await asyncio.sleep(0.05)
    assert db(mixer, "i.0.mix") == -30


async def test_external_change_cancels_ramp(ctl, mixer, settings):
    slow = MixerController(mixer, settings.model_copy(update={"raise_rate_db_per_sec": 1.0}))
    slow.set_fader(slow.channel("input", 1), db=0)
    await asyncio.sleep(0.05)
    mixer.inject("SETD^i.0.mix^0.2")  # someone moved the fader on the web UI
    assert mixer.active_ramps() == []
    assert "外部" in mixer.finished_ramps()[-1]["ended"]
    n = len(mixer.sent)
    await asyncio.sleep(0.05)
    assert len(mixer.sent) == n


async def test_external_change_is_reasserted_after_crossing_write(ctl, mixer, settings):
    """A ramp write in flight can land after a person's change; their value must win in the end."""
    slow = MixerController(mixer, settings.model_copy(update={"raise_rate_db_per_sec": 1.0}))
    slow.set_fader(slow.channel("input", 1), db=0)
    await asyncio.sleep(0.05)
    mixer.inject("SETD^i.0.mix^0.2")  # person pulls the fader down while our write is in flight
    await asyncio.sleep(0.3)
    assert mixer.sent[-1] == "SETD^i.0.mix^0.2"  # their value re-sent, so the mixer ends on it
    assert mixer.get_float("i.0.mix") == 0.2


async def test_no_reassert_when_no_recent_write(ctl, mixer, settings, monkeypatch):
    from soundcraft_ui_mcp import mixer as mixer_mod

    monkeypatch.setattr(mixer_mod, "CROSSING_WINDOW_SEC", 0.0)
    slow = MixerController(mixer, settings.model_copy(update={"raise_rate_db_per_sec": 1.0}))
    slow.set_fader(slow.channel("input", 1), db=0)
    await asyncio.sleep(0.05)
    mixer.inject("SETD^i.0.mix^0.2")
    n = len(mixer.sent)
    await asyncio.sleep(0.3)
    assert len(mixer.sent) == n


async def test_own_echo_does_not_cancel(ctl, mixer, settings):
    slow = MixerController(mixer, settings.model_copy(update={"raise_rate_db_per_sec": 1.0}))
    slow.set_fader(slow.channel("input", 1), db=0)
    await asyncio.sleep(0.03)
    mixer.inject(mixer.sent[-1])
    assert mixer.active_ramps()
    mixer.cancel_all_ramps()


async def test_mute_immediate_unmute_soft(ctl, mixer):
    ch = ctl.channel("input", 3)
    ctl.set_mute(ch, True)
    assert mixer.get("i.2.mute") == 1
    mixer.sent.clear()
    res = ctl.set_mute(ch, False)
    assert res["restore_to_db"] == pytest.approx(-10, abs=0.1)
    # fader dropped to -inf before unmuting
    assert mixer.sent[0] == "SETD^i.2.mix^0"
    assert mixer.sent[1] == "SETD^i.2.mute^0"
    await wait_ramps(mixer)
    assert db(mixer, "i.2.mix") == pytest.approx(-10, abs=0.1)


async def test_stereo_linked_fader(ctl, mixer):
    mixer.state["l.0.stereoIndex"] = 0
    mixer.state["l.1.stereoIndex"] = 1
    ctl.set_fader(ctl.channel("line", 1), db=-20)
    assert set(mixer.sent_paths()) == {"l.0.mix", "l.1.mix"}


async def test_aux_send_raise_and_limit(ctl, mixer):
    src = ctl.channel("input", 2)
    with pytest.raises(SafetyError):
        ctl.set_aux_send(2, src, db=2)
    ctl.set_aux_send(2, src, db=-5)
    await wait_ramps(mixer)
    assert db(mixer, "i.1.aux.1.value") == pytest.approx(-5, abs=0.1)
    mix = ctl.aux_mix(2)
    entry = next(s for s in mix["sends"] if s["source"] == "input 2")
    assert entry["send_db"] == pytest.approx(-5, abs=0.1)


async def test_aux_send_tap_and_pan(ctl, mixer):
    src = ctl.channel("input", 1)
    ctl.set_aux_send_tap(1, src, "post")
    assert mixer.get("i.0.aux.0.post") == 1
    with pytest.raises(ValueError):
        ctl.set_aux_send_pan(1, src, 50)  # mono AUX
    mixer.state["a.0.stereoIndex"] = 0
    ctl.set_aux_send_pan(1, src, 50)
    assert mixer.get("i.0.aux.1.pan") == 0.75


async def test_eq(ctl, mixer):
    ch = ctl.channel("input", 1)
    ctl.set_eq_band(ch, 2, frequency_hz=2500, q=1.4, gain_db=-3)
    eq = ctl.get_eq(ch)["bands"][1]
    assert eq == {"band": 2, "frequency_hz": 2500, "q": 1.4, "gain_db": -3}
    with pytest.raises(SafetyError):
        ctl.set_eq_band(ch, 1, gain_db=9)
    ctl.set_eq_band(ch, 1, gain_db=4)
    assert mixer.active_ramps()
    await wait_ramps(mixer)
    assert ctl.get_eq(ch)["bands"][0]["gain_db"] == 4
    ctl.set_hpf(ch, 100)
    assert ctl.get_eq(ch)["hpf_hz"] == 100
    ctl.set_hpf(ch, None)
    assert ctl.get_eq(ch)["hpf_hz"] == "off"
    with pytest.raises(ValueError):
        ctl.get_eq(ctl.channel("aux", 1))


async def test_dynamics(ctl, mixer):
    ch = ctl.channel("input", 1)
    ctl.set_compressor(ch, {"threshold": 0.6, "outgain": 0.5})
    assert mixer.get("i.0.dyn.threshold") == 0.6
    assert mixer.active_ramps()  # outgain increase is ramped
    await wait_ramps(mixer)
    assert mixer.get_float("i.0.dyn.outgain") == pytest.approx(0.5)
    ctl.set_dynamics_bypass(ch, "gate", True)
    assert mixer.get("i.0.gate.bypass") == 1
    with pytest.raises(ValueError):
        ctl.set_compressor(ch, {"bogus": 1})


async def test_overview(ctl):
    ov = ctl.overview()
    assert ov["model"] == "ui12"
    assert len(ov["input"]) == 8 and len(ov["aux"]) == 4
    assert ov["input"][0]["name"] == "CH 1"
    assert ov["line"][1]["name"] == "LINE IN R"
    assert ov["master"]["fader_db"] == 0


def test_readable_dynamics(ctl):
    d = ctl.get_dynamics(ctl.channel("input", 1))["readable"]
    assert d["compressor_outgain_db"] == 0
    assert d["compressor_gain_db"] == 0


async def test_single_solo_mode_clears_other_solos(ctl, mixer):
    ctl.set_solo(ctl.channel("input", 3), True)
    res = ctl.set_solo(ctl.channel("input", 4), True)
    assert res["cleared_other_solos"] == ["i.2.solo"]
    assert mixer.get("i.2.solo") == 0 and mixer.get("i.3.solo") == 1


async def test_multiple_solo_mode_keeps_other_solos(ctl, mixer):
    mixer.state["settings.multiplesolo"] = 1.0
    ctl.set_solo(ctl.channel("input", 3), True)
    res = ctl.set_solo(ctl.channel("input", 4), True)
    assert "cleared_other_solos" not in res
    assert mixer.get("i.2.solo") == 1 and mixer.get("i.3.solo") == 1
