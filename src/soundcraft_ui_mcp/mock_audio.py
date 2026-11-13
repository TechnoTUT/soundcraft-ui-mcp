"""Very small signal-flow model that turns mixer state into meter readings for the mock.

It is not an audio engine. It produces plausible meter movement that responds to the controls:
source -> input gain -> EQ -> gate -> compressor -> fader/mute -> buses (master, AUX, FX, sub).
Levels are in dB on the mixer's meter scale (-80 .. 0).
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from . import converters as cv
from .meters import RTA_BANDS, rta_frequencies
from .paths import CAPABILITIES, Channel, mute_status

SILENT = -math.inf
# Input gain range in dB per model (soundcraft-ui HwChannel): Ui12/Ui16 -40..+50, Ui24R -6..+57
HW_GAIN_RANGE_DB = {"ui12": (-40.0, 50.0), "ui16": (-40.0, 50.0), "ui24": (-6.0, 57.0)}


def hw_gain_db(model: str, value: float) -> float:
    lo, hi = HW_GAIN_RANGE_DB.get(model, HW_GAIN_RANGE_DB["ui12"])
    return lo + (hi - lo) * value


def db_sum(levels: list[float]) -> float:
    power = sum(10 ** (lv / 10) for lv in levels if lv != SILENT)
    return 10 * math.log10(power) if power > 0 else SILENT


def _f(state: Mapping[str, Any], path: str, default: float = 0.0) -> float:
    v = state.get(path, default)
    return float(v) if isinstance(v, (int, float)) else default


@dataclass
class Source:
    """A simulated input signal."""

    level_db: float  # average level at the input meter with default gain
    kind: str = "music"  # music | voice | tone
    bpm: float = 124.0


PROFILES: dict[str, dict[tuple[str, int], Source]] = {
    "silent": {},
    # a DJ setup: stereo DJ mixer on input 1/2, a mic on input 3, background music on the player
    "demo": {
        ("input", 1): Source(-14, "music"),
        ("input", 2): Source(-14, "music"),
        ("input", 3): Source(-24, "voice"),
        ("player", 1): Source(-20, "music", bpm=100),
        ("player", 2): Source(-20, "music", bpm=100),
    },
}


@dataclass
class SignalModel:
    model: str = "ui12"
    sources: dict[tuple[str, int], Source] = field(default_factory=dict)
    seed: int = 1

    def __post_init__(self) -> None:
        self.caps = CAPABILITIES[self.model]
        self.rng = random.Random(self.seed)
        self._voice_on: dict[tuple[str, int], bool] = {}

    @classmethod
    def from_profile(cls, name: str, model: str = "ui12") -> SignalModel:
        return cls(model=model, sources=dict(PROFILES[name]))

    def set_source(self, type_: str, number: int, level_db: float | None, kind: str = "music") -> None:
        if level_db is None:
            self.sources.pop((type_, number), None)
        else:
            self.sources[(type_, number)] = Source(level_db, kind)

    # --- source signal ------------------------------------------------------

    def _source_level(self, key: tuple[str, int], t: float) -> float:
        src = self.sources.get(key)
        if src is None:
            return SILENT
        jitter = self.rng.uniform(-1.5, 1.5)
        if src.kind == "tone":
            return src.level_db
        if src.kind == "voice":
            # talk in bursts: toggles on/off every few seconds
            on = self._voice_on.get(key, True)
            if self.rng.random() < 0.02:
                on = not on
            self._voice_on[key] = on
            return src.level_db + jitter * 3 if on else SILENT
        beat = (t * src.bpm / 60) % 1.0
        kick = 4.0 * math.exp(-beat * 8)  # transient on each beat
        swell = 1.5 * math.sin(2 * math.pi * t / 16)  # slow phrase-level movement
        return src.level_db + kick + swell + jitter - 2

    # --- channel processing -------------------------------------------------

    @staticmethod
    def _eq_effect(state: Mapping[str, Any], cid: str) -> float:
        if _f(state, f"{cid}.eq.bypass"):
            return 0.0
        gains = [cv.value_to_eq_gain_db(_f(state, f"{cid}.eq.b{b}.gain", 0.5)) for b in range(1, 5)]
        effect = sum(gains) / 8  # rough broadband estimate
        hpf = cv.value_to_frequency(_f(state, f"{cid}.eq.hpf.freq"))
        if hpf > 20:
            effect -= min(6.0, 2.0 * math.log2(hpf / 20) / 5.6)
        return effect

    @staticmethod
    def _dynamics(state: Mapping[str, Any], cid: str, level: float) -> tuple[float, float, float]:
        """Return (dyn_in, dyn_out, gain_reduction)."""
        if level == SILENT:
            return SILENT, SILENT, 0.0
        x = level
        if _f(state, f"{cid}.gate.enabled", 1) and not _f(state, f"{cid}.gate.bypass"):
            if x < cv.gate_threshold_db(_f(state, f"{cid}.gate.thresh")):
                x += cv.gate_depth_db(_f(state, f"{cid}.gate.depth"))
        dyn_in = x
        reduction = 0.0
        if not _f(state, f"{cid}.dyn.bypass"):
            x += cv.comp_gain_db(_f(state, f"{cid}.dyn.gain", 0.75))
            threshold = cv.comp_threshold_db(_f(state, f"{cid}.dyn.threshold", 1.0))
            ratio = cv.comp_ratio(_f(state, f"{cid}.dyn.ratio", 1.0))
            if x > threshold and ratio > 1:
                reduction = (x - threshold) * (1 - 1 / ratio)
            x = x - reduction + cv.comp_outgain_db(_f(state, f"{cid}.dyn.outgain", 0.3334960938))
        return dyn_in, x, reduction

    def _fader_db(self, state: Mapping[str, Any], path: str) -> float:
        return cv.fader_to_db(_f(state, path))

    @staticmethod
    def _pan_gains(pan: float) -> tuple[float, float]:
        """Constant-power pan, 0 dB in the centre."""
        angle = max(0.0, min(1.0, pan)) * math.pi / 2
        to_db = lambda g: 20 * math.log10(g * math.sqrt(2)) if g > 1e-6 else SILENT  # noqa: E731
        return to_db(math.cos(angle)), to_db(math.sin(angle))

    def compute(self, state: Mapping[str, Any], t: float) -> dict[str, list[dict[str, float]]]:
        caps = self.caps
        out: dict[str, list[dict[str, float]]] = {g: [] for g in ("input", "player", "line", "sub", "fx", "aux")}
        master_l: list[float] = []
        master_r: list[float] = []
        sub_in: dict[int, list[float]] = {}
        fx_in: dict[int, list[float]] = {b: [] for b in range(caps["fx"])}
        aux_in: dict[int, list[float]] = {b: [] for b in range(caps["aux"])}

        def add(level: float, pan: float, dest_l: list[float], dest_r: list[float]) -> None:
            if level == SILENT:
                return
            gl, gr = self._pan_gains(pan)
            dest_l.append(level + gl if gl != SILENT else SILENT)
            dest_r.append(level + gr if gr != SILENT else SILENT)

        for type_, code in (("input", "i"), ("line", "l"), ("player", "p")):
            for k in range(caps[type_]):
                cid = f"{code}.{k}"
                ch = Channel(type_, k + 1)  # type: ignore[arg-type]
                pre = self._source_level((type_, k + 1), t)
                if type_ == "player" and _f(state, "var.currentState") != 2:
                    pre = SILENT  # the USB player only produces signal while playing
                if type_ == "input" and pre != SILENT:
                    # sources are calibrated at the default gain (0.5)
                    pre += hw_gain_db(self.model, _f(state, f"{cid}.gain", 0.5)) - hw_gain_db(self.model, 0.5)
                processed = pre + self._eq_effect(state, cid) if pre != SILENT else SILENT
                dyn_in, post, reduction = self._dynamics(state, cid, processed)
                muted = mute_status(ch, state)[0]
                pf = SILENT if muted or post == SILENT else post + self._fader_db(state, f"{cid}.mix")
                out[type_].append(
                    {
                        "pre_db": pre,
                        "post_db": post,
                        "post_fader_db": pf,
                        "dyn_in_db": dyn_in,
                        "dyn_out_db": post,
                        "comp_reduction_db": reduction,
                    }
                )
                group = int(_f(state, f"{cid}.subgroup", -1))
                if group >= 0:
                    sub_in.setdefault(group, []).append(pf)
                else:
                    add(pf, _f(state, f"{cid}.pan", 0.5), master_l, master_r)
                for b in range(caps["fx"]):
                    if not _f(state, f"{cid}.fx.{b}.mute"):
                        send = self._fader_db(state, f"{cid}.fx.{b}.value")
                        if send != SILENT and pf != SILENT:
                            fx_in[b].append(pf + send)
                for b in range(caps["aux"]):
                    sid = f"{cid}.aux.{b}"
                    if muted or _f(state, f"{sid}.mute"):
                        continue
                    send = self._fader_db(state, f"{sid}.value")
                    tap = pf if _f(state, f"{sid}.post") else post
                    if send != SILENT and tap != SILENT:
                        aux_in[b].append(tap + send)

        for k in range(caps["fx"]):
            cid = f"f.{k}"
            wet = db_sum(fx_in[k])
            post = wet - 6 if wet != SILENT else SILENT  # effect return
            muted = mute_status(Channel("fx", k + 1), state)[0]
            pf = SILENT if muted or post == SILENT else post + self._fader_db(state, f"{cid}.mix")
            out["fx"].append(self._stereo_entry(post, pf))
            add(pf, 0.5, master_l, master_r)

        for k in range(caps["sub"]):
            cid = f"s.{k}"
            post = db_sum(sub_in.get(k, []))
            muted = mute_status(Channel("sub", k + 1), state)[0]
            pf = SILENT if muted or post == SILENT else post + self._fader_db(state, f"{cid}.mix")
            out["sub"].append(self._stereo_entry(post, pf))
            add(pf, _f(state, f"{cid}.pan", 0.5), master_l, master_r)

        for k in range(caps["aux"]):
            cid = f"a.{k}"
            post = db_sum(aux_in[k])
            if post != SILENT:
                post += self._geq_effect(state, f"{cid}.eq.peak.", f"{cid}.eq.bypass")
            muted = mute_status(Channel("aux", k + 1), state)[0]
            pf = SILENT if muted or post == SILENT else post + self._fader_db(state, f"{cid}.mix")
            out["aux"].append(
                {"post_db": post, "post_fader_db": pf, "dyn_in_db": post, "dyn_out_db": post, "comp_reduction_db": 0}
            )

        master_fader = self._fader_db(state, "m.mix")
        master = []
        for side, levels in (("l", master_l), ("r", master_r)):
            post = db_sum(levels)
            if post != SILENT:
                post += self._geq_effect(state, f"m.eq.peak.{side}.", "m.eq.bypass")
            pf = post + master_fader if post != SILENT else SILENT
            master.append(
                {"post_db": post, "post_fader_db": pf, "dyn_in_db": post, "dyn_out_db": post, "comp_reduction_db": 0}
            )
        out["master"] = master
        return out

    @staticmethod
    def _stereo_entry(post: float, pf: float) -> dict[str, float]:
        return {
            "post_l_db": post,
            "post_r_db": post,
            "post_fader_l_db": pf,
            "post_fader_r_db": pf,
            "dyn_in_db": post,
            "dyn_out_db": post,
            "comp_reduction_db": 0,
        }

    @staticmethod
    def _geq_effect(state: Mapping[str, Any], prefix: str, bypass_path: str) -> float:
        if _f(state, bypass_path):
            return 0.0
        gains = [cv.value_to_geq_db(_f(state, f"{prefix}{i}", 0.5)) for i in range(len(cv.GEQ_FREQUENCIES))]
        return sum(gains) / len(gains)

    # --- RTA ----------------------------------------------------------------

    def spectrum(self, state: Mapping[str, Any], levels: dict[str, list[dict[str, float]]], target: str) -> list[float]:
        """Analyser bands for ``target`` (e.g. ``i.0``, ``a.1``; ``""`` = master)."""
        code, _, idx = target.partition(".")
        level: float = SILENT
        if not target:
            level = db_sum([levels["master"][0]["post_db"], levels["master"][1]["post_db"]])
        else:
            group = {"i": "input", "l": "line", "p": "player", "f": "fx", "s": "sub", "a": "aux"}.get(code)
            if group and idx.isdigit() and int(idx) < len(levels[group]):
                entry = levels[group][int(idx)]
                level = entry.get("post_db", entry.get("post_l_db", SILENT))
        if level == SILENT:
            return [SILENT] * RTA_BANDS
        freqs = rta_frequencies()
        bands = []
        for f in freqs:
            shape = -3.0 * math.log2(f / 1000) * 0.8  # pink-ish tilt
            if f < 40:
                shape -= (40 - f) / 4
            shape += self._eq_curve(state, target, f)
            bands.append(level + shape - 8 + self.rng.uniform(-2, 2))
        return bands

    @staticmethod
    def _eq_curve(state: Mapping[str, Any], target: str, f: float) -> float:
        if not target:
            prefix, bypass = "m.eq.peak.l.", "m.eq.bypass"
        elif target.startswith("a."):
            prefix, bypass = f"{target}.eq.peak.", f"{target}.eq.bypass"
        else:
            if _f(state, f"{target}.eq.bypass"):
                return 0.0
            total = 0.0
            for b in range(1, 5):
                f0 = cv.value_to_frequency(_f(state, f"{target}.eq.b{b}.freq", 0.5))
                q = cv.value_to_q(_f(state, f"{target}.eq.b{b}.q", 0.5252185347))
                g = cv.value_to_eq_gain_db(_f(state, f"{target}.eq.b{b}.gain", 0.5))
                total += g * math.exp(-((math.log2(f / f0) * q) ** 2) * 2)
            hpf = cv.value_to_frequency(_f(state, f"{target}.eq.hpf.freq"))
            if hpf > 20 and f < hpf:
                total -= 12 * math.log2(hpf / f)
            return total
        if _f(state, bypass):
            return 0.0
        total = 0.0
        for i, f0 in enumerate(cv.GEQ_FREQUENCIES):
            g = cv.value_to_geq_db(_f(state, f"{prefix}{i}", 0.5))
            total += g * math.exp(-((math.log2(f / f0) * 4.3) ** 2))
        return total
