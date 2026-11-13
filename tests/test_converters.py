import math

import pytest

from soundcraft_ui_mcp import converters as cv


@pytest.mark.parametrize("db", [-60, -30, -10, -6, -1.5, 0, 3, 9.5])
def test_fader_roundtrip(db):
    assert cv.fader_to_db(cv.db_to_fader(db)) == pytest.approx(db, abs=0.1)


def test_fader_extremes():
    assert cv.db_to_fader(10) == 1
    assert cv.db_to_fader(-math.inf) == 0
    assert cv.fader_to_db(0) == cv.NEG_INF
    assert cv.fader_to_db(1) == pytest.approx(10, abs=0.1)


def test_fader_monotonic():
    values = [cv.db_to_fader(d) for d in range(-80, 11)]
    assert values == sorted(values)


def test_eq_defaults_match_web_app():
    # default bands of the Ui mixers (raw values observed on a Ui12)
    assert cv.value_to_frequency(0.3286901902) == 200
    assert cv.value_to_frequency(0.5584347738) == 1000
    assert cv.value_to_frequency(0.887124964) == 10000
    assert cv.value_to_q(0.5252185347) == 1.0
    assert cv.value_to_eq_gain_db(0.5) == 0


def test_eq_roundtrip():
    assert cv.value_to_frequency(cv.frequency_to_value(2500)) == 2500
    assert cv.value_to_q(cv.q_to_value(1.4)) == 1.4
    assert cv.value_to_eq_gain_db(cv.eq_gain_db_to_value(-3)) == -3
    assert cv.eq_gain_db_to_value(50) == 1


def test_pan():
    assert cv.pan_to_value(-100) == 0
    assert cv.pan_to_value(0) == 0.5
    assert cv.value_to_pan(1) == 100
