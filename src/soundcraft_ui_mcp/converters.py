"""Value converters for the Soundcraft Ui protocol.

Ported from fmalcher/soundcraft-ui (packages/mixer-connection/src/lib/utils/value-converters).
The mixer stores almost every value as a float between 0 and 1.
"""

from __future__ import annotations

import math

NEG_INF = float("-inf")

FADER_MAX_DB = 10.0

EQ_FREQUENCY_MIN = 20.0
EQ_FREQUENCY_MAX = 22050.0
EQ_Q_MIN = 0.05
EQ_Q_MAX = 15.0
EQ_GAIN_MIN_DB = -20.0
EQ_GAIN_MAX_DB = 20.0
HPF_MIN_HZ = 20.0
HPF_MAX_HZ = 1000.0


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# --- fader -------------------------------------------------------------------


def _fader_to_linear_amplitude(v: float) -> float:
    return (
        (math.sin(28.559933214452666 * v) if v < 0.055 else 1.0)
        * math.exp(
            (23.90844819639692 + (-26.23877598214595 + (12.195249692570245 - 0.4878099877028098 * v) * v) * v) * v
        )
        * 2.676529517952372e-4
    )


def _fader_to_linear_amplitude_deriv(v: float) -> float:
    p = (23.90844819639692 + (-26.23877598214595 + (12.195249692570245 - 0.4878099877028098 * v) * v) * v) * v
    p_prime = 23.90844819639692 + (-52.4775519642919 + (36.58574907771074 - 1.9512399508112392 * v) * v) * v
    exp_p = math.exp(p)
    if v < 0.055:
        wv = 28.559933214452666 * v
        return 2.676529517952372e-4 * exp_p * (28.559933214452666 * math.cos(wv) + math.sin(wv) * p_prime)
    return 2.676529517952372e-4 * exp_p * p_prime


def db_to_fader(db: float) -> float:
    """Convert dB (-inf..+10) into the mixer's linear fader value (0..1)."""
    if db <= -200:
        return 0.0
    if db >= FADER_MAX_DB:
        return 1.0
    target = 10 ** (db / 20)
    v = 0.5
    for _ in range(20):
        delta = (_fader_to_linear_amplitude(v) - target) / _fader_to_linear_amplitude_deriv(v)
        v -= delta
        if v < 0:
            v = 1e-10
        if v > 1:
            v = 1.0
        if abs(delta) < 1e-15:
            break
    return round(v * 1e11) / 1e11


def fader_to_db(value: float) -> float:
    """Convert the mixer's fader value (0..1) into dB, rounded to 0.1 dB."""
    lin = _fader_to_linear_amplitude(clamp(value, 0.0, 1.0))
    if lin < 1e-10:
        return NEG_INF
    return round(20 * math.log10(lin), 1) + 0.0


# --- EQ ----------------------------------------------------------------------


def frequency_to_value(hz: float) -> float:
    hz = clamp(hz, EQ_FREQUENCY_MIN, EQ_FREQUENCY_MAX)
    return round(math.log(hz / EQ_FREQUENCY_MIN) / math.log(EQ_FREQUENCY_MAX / EQ_FREQUENCY_MIN), 10)


def value_to_frequency(value: float) -> float:
    v = clamp(value, 0.0, 1.0)
    return float(round(EQ_FREQUENCY_MIN * (EQ_FREQUENCY_MAX / EQ_FREQUENCY_MIN) ** v))


def q_to_value(q: float) -> float:
    q = clamp(q, EQ_Q_MIN, EQ_Q_MAX)
    return round(math.log(q / EQ_Q_MIN) / math.log(EQ_Q_MAX / EQ_Q_MIN), 10)


def value_to_q(value: float) -> float:
    v = clamp(value, 0.0, 1.0)
    return round(EQ_Q_MIN * (EQ_Q_MAX / EQ_Q_MIN) ** v, 2)


def eq_gain_db_to_value(db: float) -> float:
    return clamp((db - EQ_GAIN_MIN_DB) / (EQ_GAIN_MAX_DB - EQ_GAIN_MIN_DB), 0.0, 1.0)


def value_to_eq_gain_db(value: float) -> float:
    db = round(value * (EQ_GAIN_MAX_DB - EQ_GAIN_MIN_DB) + EQ_GAIN_MIN_DB, 1)
    return clamp(db, EQ_GAIN_MIN_DB, EQ_GAIN_MAX_DB) + 0.0


# --- pan ---------------------------------------------------------------------


def pan_to_value(pan: float) -> float:
    """-100 (L) .. 0 (C) .. +100 (R)  ->  0..1"""
    return round(clamp((pan + 100) / 200, 0.0, 1.0), 3)


def value_to_pan(value: float) -> float:
    return round(clamp(value, 0.0, 1.0) * 200 - 100, 1) + 0.0


def db_repr(db: float) -> float | str:
    """JSON-friendly dB value (-inf becomes the string "-inf")."""
    return "-inf" if db == NEG_INF else db
