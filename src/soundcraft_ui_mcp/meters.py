"""Meter messages of the Soundcraft Ui protocol (``VU2^`` and ``RTA^``).

Format (from the mixer's own web app and fmalcher/soundcraft-ui):

``VU2^<base64>`` decodes to bytes::

    header (8 bytes): n_input, n_player, n_sub, n_fx, n_aux, n_master, n_line, 0
    input / player / line blocks (6 bytes): pre, post, post_fader, dyn_in, dyn_out, comp
    sub / fx blocks (7 bytes):              post_l, post_r, post_fader_l, post_fader_r, dyn_in, dyn_out, comp
    aux / master blocks (5 bytes):          post, post_fader, dyn_in, dyn_out, comp   (master: one block per side)

Level bytes are linear meter values ``byte * 0.004167508166392142`` (0..1), i.e. -80..0 dB.
The ``comp`` byte holds the compressor gain reduction in its low 7 bits and a flag in bit 7.

``RTA^<base64>`` decodes to one byte per analyser band (120 bands), same scaling.
"""

from __future__ import annotations

import base64
import math
from dataclasses import dataclass, field
from typing import Any

VU_FACTOR = 0.004167508166392142
VU_MAX_BYTE = 240
METER_FLOOR_DB = -80.0
COMP_ZOOM = 2.0
RTA_BANDS = 120

# header order of the channel groups
GROUPS = ("input", "player", "sub", "fx", "aux", "master", "line")
BLOCK_SIZE = {"input": 6, "player": 6, "line": 6, "sub": 7, "fx": 7, "aux": 5, "master": 5}


def vu_to_db(value: float) -> float:
    return round(max(0.0, min(1.0, value)) * 80 - 80, 1)


def db_to_vu_byte(db: float) -> int:
    if db == -math.inf or db <= METER_FLOOR_DB:
        return 0
    v = min(1.0, (db - METER_FLOOR_DB) / -METER_FLOOR_DB)
    return min(VU_MAX_BYTE, round(v / VU_FACTOR))


def byte_to_db(b: int) -> float:
    return vu_to_db(b * VU_FACTOR)


def comp_byte(reduction_db: float, flag: bool = True) -> int:
    """Encode compressor gain reduction (dB, >= 0) the way the web app decodes it."""
    r = max(0.0, min(1.0, reduction_db / (-METER_FLOOR_DB / COMP_ZOOM)))  # 0..40 dB full scale
    x = (1 - r / COMP_ZOOM) / VU_FACTOR
    low7 = max(0, min(127, int(x) >> 1))
    return (0x80 if flag else 0) | low7


def comp_reduction_db(b: int) -> float:
    x = ((b & 0x7F) << 1) | 1
    r = (1 - VU_FACTOR * x) * COMP_ZOOM
    r = 0.0 if r < 0.008 else min(1.0, r)
    return round(r * -METER_FLOOR_DB / COMP_ZOOM, 1)


@dataclass
class VuFrame:
    """Decoded VU data. Every level is in dB (-80 = silence)."""

    input: list[dict[str, float]] = field(default_factory=list)
    player: list[dict[str, float]] = field(default_factory=list)
    line: list[dict[str, float]] = field(default_factory=list)
    sub: list[dict[str, float]] = field(default_factory=list)
    fx: list[dict[str, float]] = field(default_factory=list)
    aux: list[dict[str, float]] = field(default_factory=list)
    master: list[dict[str, float]] = field(default_factory=list)  # [left, right]

    def as_dict(self) -> dict[str, Any]:
        return {g: getattr(self, g) for g in GROUPS}


def _mono_block(b: bytes, with_pre: bool) -> dict[str, float]:
    if with_pre:
        return {
            "pre_db": byte_to_db(b[0]),
            "post_db": byte_to_db(b[1]),
            "post_fader_db": byte_to_db(b[2]),
            "dyn_in_db": byte_to_db(b[3]),
            "dyn_out_db": byte_to_db(b[4]),
            "comp_reduction_db": comp_reduction_db(b[5]),
        }
    return {
        "post_db": byte_to_db(b[0]),
        "post_fader_db": byte_to_db(b[1]),
        "dyn_in_db": byte_to_db(b[2]),
        "dyn_out_db": byte_to_db(b[3]),
        "comp_reduction_db": comp_reduction_db(b[4]),
    }


def _stereo_block(b: bytes) -> dict[str, float]:
    return {
        "post_l_db": byte_to_db(b[0]),
        "post_r_db": byte_to_db(b[1]),
        "post_fader_l_db": byte_to_db(b[2]),
        "post_fader_r_db": byte_to_db(b[3]),
        "dyn_in_db": byte_to_db(b[4]),
        "dyn_out_db": byte_to_db(b[5]),
        "comp_reduction_db": comp_reduction_db(b[6]),
    }


def decode_vu2(payload: str) -> VuFrame:
    raw = base64.b64decode(payload)
    counts = dict(zip(GROUPS, raw[:7], strict=True))
    frame = VuFrame()
    pos = 8
    for group in GROUPS:
        size = BLOCK_SIZE[group]
        for _ in range(counts[group]):
            block = raw[pos : pos + size]
            if len(block) < size:
                return frame
            if group in ("input", "player", "line"):
                entry = _mono_block(block, with_pre=True)
            elif group in ("sub", "fx"):
                entry = _stereo_block(block)
            else:
                entry = _mono_block(block, with_pre=False)
            getattr(frame, group).append(entry)
            pos += size
    return frame


def encode_vu2(levels: dict[str, list[dict[str, float]]]) -> str:
    """Encode levels (same keys as ``VuFrame``, values in dB) into a ``VU2`` payload."""
    out = bytearray(len(levels.get(g, [])) for g in GROUPS)
    out.append(0)
    for group in GROUPS:
        for ch in levels.get(group, []):
            comp = comp_byte(ch.get("comp_reduction_db", 0.0))
            if group in ("input", "player", "line"):
                keys = ("pre_db", "post_db", "post_fader_db", "dyn_in_db", "dyn_out_db")
            elif group in ("sub", "fx"):
                keys = ("post_l_db", "post_r_db", "post_fader_l_db", "post_fader_r_db", "dyn_in_db", "dyn_out_db")
            else:
                keys = ("post_db", "post_fader_db", "dyn_in_db", "dyn_out_db")
            out.extend(db_to_vu_byte(ch.get(k, -math.inf)) for k in keys)
            out.append(comp)
    return base64.b64encode(bytes(out)).decode()


def rta_frequencies(n: int = RTA_BANDS) -> list[float]:
    """Assumed analyser band centres: log-spaced 20 Hz .. 20 kHz."""
    return [20 * (1000 ** (i / (n - 1))) for i in range(n)]


def decode_rta(payload: str) -> list[float]:
    return [byte_to_db(b) for b in base64.b64decode(payload)]


def encode_rta(levels_db: list[float]) -> str:
    return base64.b64encode(bytes(db_to_vu_byte(d) for d in levels_db)).decode()
