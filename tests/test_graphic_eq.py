import pytest

from soundcraft_ui_mcp import converters as cv


async def test_graphic_eq_aux(ctl, mixer):
    from .conftest import wait_ramps

    aux1 = ctl.channel("aux", 1)
    res = ctl.set_graphic_eq_band(aux1, -6, frequency_hz=2600)
    assert res["band"] == 22 and "即時" in res["mode"]
    assert mixer.get("a.0.eq.peak.21") == cv.geq_db_to_value(-6)
    with pytest.raises(ValueError):
        ctl.set_graphic_eq_band(aux1, 9, band=5)
    ctl.set_graphic_eq_band(aux1, 3, band=5)
    assert mixer.active_ramps()
    await wait_ramps(mixer)
    geq = ctl.get_graphic_eq(aux1)
    assert {b["hz"]: b["gain_db"] for b in geq["non_flat"]} == {50: 3, 2500: -6}
    with pytest.raises(ValueError):
        ctl.reset_graphic_eq(aux1)  # needs confirm: may wipe the venue's tuning
    res = ctl.reset_graphic_eq(aux1, confirm=True)
    assert res["cut_immediately_hz"] == [50] and res["raising_slowly_hz"] == [2500]
    await wait_ramps(mixer)
    assert ctl.get_graphic_eq(aux1)["non_flat"] == []


async def test_graphic_eq_stereo_aux_and_master(ctl, mixer):
    mixer.state["a.0.stereoIndex"] = 0
    ctl.set_graphic_eq_band(ctl.channel("aux", 1), -3, band=10)
    assert mixer.get("a.1.eq.peak.9") == cv.geq_db_to_value(-3)  # mirrored to the linked AUX 2
    master = ctl.channel("master")
    ctl.set_graphic_eq_band(master, -4, band=1)
    assert mixer.get("m.eq.peak.l.0") == mixer.get("m.eq.peak.r.0") == cv.geq_db_to_value(-4)
    mixer.state["m.eq.linked"] = 0
    ctl.set_graphic_eq_band(master, -8, band=2, side="right")
    assert mixer.get("m.eq.peak.r.1") == cv.geq_db_to_value(-8) and mixer.get("m.eq.peak.l.1") == 0.5
    assert "bands_right" in ctl.get_graphic_eq(master)
    ctl.set_graphic_eq_bypass(master, True)
    assert mixer.get("m.eq.bypass") == 1
    with pytest.raises(ValueError):
        ctl.get_graphic_eq(ctl.channel("input", 1))
