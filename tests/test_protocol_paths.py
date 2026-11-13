import pytest

from soundcraft_ui_mcp.paths import (
    AddressError,
    Channel,
    aux_pan_ids,
    aux_send_ids,
    make_channel,
    master_ids,
)
from soundcraft_ui_mcp.protocol import build_setd, build_sets, parse_line, split_frame


def test_build_and_parse():
    assert build_setd("i.0.mix", 0.5) == "SETD^i.0.mix^0.5"
    assert build_setd("i.0.mute", 1.0) == "SETD^i.0.mute^1"
    assert build_sets("i.0.name", "Vox") == "SETS^i.0.name^Vox"
    assert parse_line("SETD^i.2.mix^0.25").value == 0.25
    assert parse_line("SETS^i.2.name^DJ^L").value == "DJ^L"
    assert parse_line("VU2^abc") is None


def test_split_frame():
    assert split_frame("3:::SETD^a^1\nSETD^b^2") == ["SETD^a^1", "SETD^b^2"]
    assert split_frame("2::") == []


def test_make_channel_bounds():
    assert make_channel("input", 8, "ui12").id == "i.7"
    assert make_channel("master", None, "ui12").id == "m"
    with pytest.raises(AddressError):
        make_channel("input", 9, "ui12")
    with pytest.raises(AddressError):
        make_channel("aux", None, "ui12")


def test_stereo_link():
    st = {"l.0.stereoIndex": 0, "l.1.stereoIndex": 1, "i.0.stereoIndex": -1}
    assert master_ids(Channel("line", 1), st) == ["l.0", "l.1"]
    assert master_ids(Channel("line", 2), st) == ["l.1", "l.0"]
    assert master_ids(Channel("input", 1), st) == ["i.0"]
    assert master_ids(Channel("fx", 1), st) == ["f.0"]


def test_aux_send_links():
    st = {"l.0.stereoIndex": 0, "a.0.stereoIndex": 0}
    assert aux_send_ids(Channel("line", 1), 1, st) == ["l.0.aux.0", "l.1.aux.0", "l.0.aux.1", "l.1.aux.1"]
    assert aux_pan_ids(Channel("line", 1), 1, st) == ["l.0.aux.0", "l.0.aux.1"]
    assert aux_send_ids(Channel("input", 3), 3, {}) == ["i.2.aux.2"]
