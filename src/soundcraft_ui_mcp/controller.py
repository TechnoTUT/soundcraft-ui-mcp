"""High-level mixer operations with live-safety rules.

Rules
-----
* Any operation that makes something louder is ramped slowly (``raise_rate_db_per_sec``).
  This includes fader / AUX send increases, unmuting, EQ boosts and dynamics gain increases.
* Lowering and muting happen immediately (or over ``fade_ms`` if requested).
* Targets above the configured limits need ``confirm=True``.
"""

from __future__ import annotations

import math
import time
from typing import Any

from . import converters as cv
from .config import Settings
from .converters import NEG_INF, db_repr
from .mixer import MixerClient
from .paths import (
    AUX_SEND_SOURCES,
    DYN_TYPES,
    EQ_BANDS,
    EQ_TYPES,
    AddressError,
    Channel,
    aux_pan_ids,
    aux_send_ids,
    capabilities,
    channel_name,
    check_aux_bus,
    make_channel,
    master_ids,
    send_id,
    stereo_partner,
)
from .safety import SafetyError, check_upper_limit, constant_rate_fn, db_rate_fn

COMPRESSOR_PARAMS = ("threshold", "ratio", "attack", "release", "gain", "outgain", "softknee")
COMPRESSOR_LOUDER = ("gain", "outgain")
GATE_PARAMS = ("thresh", "depth", "attack", "hold", "release")


def _bool(v: Any) -> bool:
    return bool(v) if isinstance(v, (int, float)) else False


class MixerController:
    def __init__(self, mixer: MixerClient, settings: Settings) -> None:
        self.mixer = mixer
        self.s = settings

    # --- helpers -------------------------------------------------------------

    @property
    def model(self) -> str:
        m = self.mixer.get("model")
        return m if isinstance(m, str) and m else "ui12"

    def channel(self, type_: str, number: int | None = None) -> Channel:
        return make_channel(type_, number, self.model)

    def _fader_db(self, path: str) -> float:
        return cv.fader_to_db(self.mixer.get_float(path))

    def _pan(self, path: str) -> float:
        return cv.value_to_pan(self.mixer.get_float(path, 0.5))

    def _fader_path(self, ch: Channel) -> str:
        return "m.mix" if ch.is_master else f"{ch.id}.mix"

    def _fader_paths(self, ch: Channel) -> list[str]:
        if ch.is_master:
            return ["m.mix"]
        return [f"{cid}.mix" for cid in master_ids(ch, self.mixer.state)]

    def _change_level(
        self,
        *,
        key: str,
        label: str,
        paths: list[str],
        current_db: float,
        target_db: float,
        limit_db: float,
        confirm: bool,
        fade_ms: int | None,
    ) -> dict[str, Any]:
        """Shared fader-like level change (dB domain, fader taper)."""
        target_db = min(target_db, cv.FADER_MAX_DB)
        check_upper_limit(target_db, limit_db, confirm, label)
        fade_s = (fade_ms or 0) / 1000
        result: dict[str, Any] = {
            "target": label,
            "paths": paths,
            "before_db": db_repr(current_db),
            "target_db": db_repr(target_db),
        }

        if target_db <= current_db:
            # Lowering: immediate unless a fade was requested
            if fade_s <= 0 or current_db == NEG_INF:
                self.mixer.cancel_ramps_for(paths)
                value = cv.db_to_fader(target_db)
                for p in paths:
                    self.mixer.setd(p, value)
                result.update(mode="即時に下げました", after_db=db_repr(self._fader_db(paths[0])))
                return result
            floor = self.s.ramp_floor_db
            ramp_target = target_db if target_db != NEG_INF else floor - 0.1
            rate = (current_db - ramp_target) / fade_s
            ramp = self.mixer.start_ramp(
                key=key,
                label=label,
                paths=paths,
                start=current_db,
                target=ramp_target,
                unit="dB",
                rate_fn=constant_rate_fn(rate),
                to_value=lambda d: 0.0 if d < floor else cv.db_to_fader(d),
            )
            result.update(mode="フェードで下げています", estimated_sec=round(ramp.estimated_sec, 1))
            return result

        # Raising: always a slow ramp
        start = max(current_db, self.s.ramp_floor_db)
        if target_db <= start:
            start = target_db  # target is below the ramp floor; nothing audible to ramp
        cap = (target_db - start) / fade_s if fade_s > 0 else None
        ramp = self.mixer.start_ramp(
            key=key,
            label=label,
            paths=paths,
            start=start,
            target=target_db,
            unit="dB",
            rate_fn=db_rate_fn(self.s, cap),
            to_value=cv.db_to_fader,
        )
        result.update(
            mode="ゆっくり上げています (完了を待たずに返しています)",
            rate_db_per_sec=self.s.raise_rate_db_per_sec,
            estimated_sec=round(ramp.estimated_sec, 1),
        )
        return result

    @staticmethod
    def _resolve_target_db(current_db: float, db: float | None, delta_db: float | None) -> float:
        if (db is None) == (delta_db is None):
            raise ValueError("db と delta_db のどちらか一方だけを指定してください")
        if db is not None:
            return db
        assert delta_db is not None
        base = max(current_db, -100.0)
        target = base + delta_db
        return NEG_INF if target <= -100 else target

    # --- status / overview ---------------------------------------------------

    def status(self) -> dict[str, Any]:
        m = self.mixer
        return {
            "mixer_ip": m.ip,
            "connection": m.status,
            "synced": m.synced.is_set(),
            "last_error": m.last_error,
            "model": m.get("model"),
            "firmware": m.get("firmware"),
            "capabilities": capabilities(self.model),
            "active_fades": m.active_ramps(),
            "recent_fades": m.finished_ramps(),
            "safety": {
                "max_fader_db": self.s.max_fader_db,
                "max_aux_send_db": self.s.max_aux_send_db,
                "max_eq_boost_db": self.s.max_eq_boost_db,
                "raise_rate_db_per_sec": self.s.raise_rate_db_per_sec,
            },
        }

    def _channel_summary(self, ch: Channel) -> dict[str, Any]:
        st = self.mixer.state
        info: dict[str, Any] = {
            "number": ch.number,
            "name": channel_name(ch, st),
            "fader_db": db_repr(self._fader_db(self._fader_path(ch))),
            "mute": _bool(st.get(f"{ch.id}.mute")),
            "solo": _bool(st.get(f"{ch.id}.solo")),
            "pan": self._pan(f"{ch.id}.pan"),
        }
        partner = stereo_partner(ch, st)
        if partner is not None:
            info["stereo_linked_with"] = partner.number
        return info

    def overview(self) -> dict[str, Any]:
        caps = capabilities(self.model)
        out: dict[str, Any] = {
            "model": self.model,
            "master": {
                "fader_db": db_repr(self._fader_db("m.mix")),
                "pan": self._pan("m.pan"),
            },
        }
        for type_ in ("input", "line", "player", "fx", "sub", "aux"):
            out[type_] = [
                self._channel_summary(Channel(type_, n))  # type: ignore[arg-type]
                for n in range(1, caps[type_] + 1)
            ]
        out["active_fades"] = self.mixer.active_ramps()
        return out

    def channel_detail(self, ch: Channel) -> dict[str, Any]:
        if ch.is_master:
            return {"master": self.overview()["master"], "active_fades": self.mixer.active_ramps()}
        info = self._channel_summary(ch)
        info["type"] = ch.type
        if ch.type in AUX_SEND_SOURCES:
            info["aux_sends"] = [
                self._aux_send_summary(ch, bus) for bus in range(1, capabilities(self.model)["aux"] + 1)
            ]
        if ch.type in EQ_TYPES:
            info["eq"] = self.get_eq(ch)
        if ch.type in DYN_TYPES:
            info["dynamics"] = self.get_dynamics(ch)
        return info

    # --- mixing --------------------------------------------------------------

    def set_fader(
        self,
        ch: Channel,
        db: float | None = None,
        delta_db: float | None = None,
        fade_ms: int | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        paths = self._fader_paths(ch)
        current = self._fader_db(paths[0])
        target = self._resolve_target_db(current, db, delta_db)
        return self._change_level(
            key=f"fader:{ch.id}",
            label=f"{ch.label()} フェーダー",
            paths=paths,
            current_db=current,
            target_db=target,
            limit_db=self.s.max_fader_db,
            confirm=confirm,
            fade_ms=fade_ms,
        )

    def _soft_unmute(self, *, key: str, label: str, mute_paths: list[str], level_paths: list[str]) -> dict[str, Any]:
        """Unmute without a jump: drop the level to -inf, unmute, then ramp back slowly."""
        st = self.mixer.state
        if not any(_bool(st.get(p)) for p in mute_paths):
            return {"target": label, "mode": "既にミュート解除されています"}
        restore_db = self._fader_db(level_paths[0])
        if restore_db <= self.s.ramp_floor_db:
            for p in mute_paths:
                self.mixer.setd(p, 0)
            return {"target": label, "mode": "ミュート解除 (レベルが低いため即時)"}
        self.mixer.cancel_ramps_for(level_paths)
        for p in level_paths:
            self.mixer.setd(p, 0.0)
        for p in mute_paths:
            self.mixer.setd(p, 0)
        ramp = self.mixer.start_ramp(
            key=key,
            label=f"{label} (ミュート解除後の復帰)",
            paths=level_paths,
            start=self.s.ramp_floor_db,
            target=restore_db,
            unit="dB",
            rate_fn=db_rate_fn(self.s),
            to_value=cv.db_to_fader,
        )
        return {
            "target": label,
            "mode": "ミュート解除し、元のレベルまでゆっくり戻しています",
            "restore_to_db": restore_db,
            "estimated_sec": round(ramp.estimated_sec, 1),
        }

    def set_mute(self, ch: Channel, muted: bool) -> dict[str, Any]:
        if ch.is_master:
            raise AddressError("master にはミュートがありません。フェーダーで下げてください")
        ids = master_ids(ch, self.mixer.state)
        mute_paths = [f"{cid}.mute" for cid in ids]
        if muted:
            self.mixer.cancel_ramps_for([f"{cid}.mix" for cid in ids])
            for p in mute_paths:
                self.mixer.setd(p, 1)
            return {"target": ch.label(), "mode": "即時にミュートしました", "paths": mute_paths}
        return self._soft_unmute(
            key=f"fader:{ch.id}",
            label=ch.label(),
            mute_paths=mute_paths,
            level_paths=[f"{cid}.mix" for cid in ids],
        )

    def set_solo(self, ch: Channel, solo: bool) -> dict[str, Any]:
        if ch.is_master:
            raise AddressError("master はソロできません")
        st = self.mixer.state
        paths = [f"{cid}.solo" for cid in master_ids(ch, st)]
        cleared: list[str] = []
        if solo and not _bool(st.get("settings.multiplesolo", 0)):
            # Single-solo mode is enforced by the client (as the mixer's web app does), not by the mixer
            cleared = sorted(k for k, v in st.items() if k.endswith(".solo") and _bool(v) and k not in paths)
            for p in cleared:
                self.mixer.setd(p, 0)
        for p in paths:
            self.mixer.setd(p, 1 if solo else 0)
        result: dict[str, Any] = {"target": ch.label(), "solo": solo, "paths": paths}
        if cleared:
            result["cleared_other_solos"] = cleared
        return result

    def set_pan(self, ch: Channel, pan: float) -> dict[str, Any]:
        path = f"{ch.id}.pan"
        before = self._pan(path)
        self.mixer.setd(path, cv.pan_to_value(pan))
        return {"target": ch.label(), "before": before, "after": self._pan(path)}

    # --- AUX sends -----------------------------------------------------------

    def _check_send_source(self, source: Channel, bus: int) -> None:
        check_aux_bus(bus, self.model)
        if source.type not in AUX_SEND_SOURCES:
            raise AddressError("AUX に送れるのは input / line / player / fx です")

    def _aux_send_summary(self, source: Channel, bus: int) -> dict[str, Any]:
        sid = send_id(source, bus)
        st = self.mixer.state
        return {
            "bus": bus,
            "send_db": db_repr(self._fader_db(f"{sid}.value")),
            "mute": _bool(st.get(f"{sid}.mute")),
            "post": _bool(st.get(f"{sid}.post")),
            "pan": self._pan(f"{sid}.pan"),
        }

    def aux_mix(self, bus: int) -> dict[str, Any]:
        check_aux_bus(bus, self.model)
        caps = capabilities(self.model)
        aux = Channel("aux", bus)
        sources = []
        for type_ in ("input", "line", "player", "fx"):
            for n in range(1, caps[type_] + 1):
                src = Channel(type_, n)  # type: ignore[arg-type]
                entry = self._aux_send_summary(src, bus)
                entry.pop("bus")
                sources.append({"source": src.label(), "name": channel_name(src, self.mixer.state), **entry})
        return {"aux_master": self._channel_summary(aux), "sends": sources}

    def set_aux_send(
        self,
        bus: int,
        source: Channel,
        db: float | None = None,
        delta_db: float | None = None,
        fade_ms: int | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        self._check_send_source(source, bus)
        paths = [f"{sid}.value" for sid in aux_send_ids(source, bus, self.mixer.state)]
        current = self._fader_db(paths[0])
        target = self._resolve_target_db(current, db, delta_db)
        return self._change_level(
            key=f"aux:{send_id(source, bus)}",
            label=f"{source.label()} → AUX {bus} センド",
            paths=paths,
            current_db=current,
            target_db=target,
            limit_db=self.s.max_aux_send_db,
            confirm=confirm,
            fade_ms=fade_ms,
        )

    def set_aux_send_mute(self, bus: int, source: Channel, muted: bool) -> dict[str, Any]:
        self._check_send_source(source, bus)
        ids = aux_send_ids(source, bus, self.mixer.state)
        mute_paths = [f"{sid}.mute" for sid in ids]
        level_paths = [f"{sid}.value" for sid in ids]
        label = f"{source.label()} → AUX {bus} センド"
        if muted:
            self.mixer.cancel_ramps_for(level_paths)
            for p in mute_paths:
                self.mixer.setd(p, 1)
            return {"target": label, "mode": "即時にミュートしました"}
        return self._soft_unmute(
            key=f"aux:{send_id(source, bus)}", label=label, mute_paths=mute_paths, level_paths=level_paths
        )

    def set_aux_send_pan(self, bus: int, source: Channel, pan: float) -> dict[str, Any]:
        self._check_send_source(source, bus)
        if stereo_partner(Channel("aux", bus), self.mixer.state) is None:
            raise AddressError(f"AUX {bus} はモノラルなのでパンは設定できません")
        paths = [f"{sid}.pan" for sid in aux_pan_ids(source, bus, self.mixer.state)]
        for p in paths:
            self.mixer.setd(p, cv.pan_to_value(pan))
        return {"target": f"{source.label()} → AUX {bus}", "pan": self._pan(paths[0]), "paths": paths}

    def set_aux_send_tap(self, bus: int, source: Channel, mode: str) -> dict[str, Any]:
        self._check_send_source(source, bus)
        ids = aux_send_ids(source, bus, self.mixer.state)
        if mode in ("pre", "post"):
            for sid in ids:
                self.mixer.setd(f"{sid}.post", 1 if mode == "post" else 0)
        elif mode in ("pre_proc", "post_proc"):
            for sid in ids:
                self.mixer.setd(f"{sid}.postproc", 1 if mode == "post_proc" else 0)
        else:
            raise ValueError("mode は pre / post / pre_proc / post_proc のいずれかです")
        return {"target": f"{source.label()} → AUX {bus}", "mode": mode}

    # --- EQ ------------------------------------------------------------------

    def _check_eq(self, ch: Channel) -> None:
        if ch.type not in EQ_TYPES:
            raise AddressError("パラメトリック EQ があるのは input / line / player / fx / sub です")

    def get_eq(self, ch: Channel) -> dict[str, Any]:
        self._check_eq(ch)
        g = self.mixer.get_float
        bands = []
        for b in range(1, EQ_BANDS + 1):
            base = f"{ch.id}.eq.b{b}"
            bands.append(
                {
                    "band": b,
                    "frequency_hz": cv.value_to_frequency(g(f"{base}.freq")),
                    "q": cv.value_to_q(g(f"{base}.q")),
                    "gain_db": cv.value_to_eq_gain_db(g(f"{base}.gain", 0.5)),
                }
            )
        hpf = cv.value_to_frequency(g(f"{ch.id}.eq.hpf.freq"))
        return {
            "bypass": _bool(self.mixer.get(f"{ch.id}.eq.bypass")),
            "bands": bands,
            "hpf_hz": "off" if hpf <= cv.HPF_MIN_HZ else hpf,
        }

    def set_eq_band(
        self,
        ch: Channel,
        band: int,
        frequency_hz: float | None = None,
        q: float | None = None,
        gain_db: float | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        self._check_eq(ch)
        if not 1 <= band <= EQ_BANDS:
            raise AddressError(f"EQ バンドは 1〜{EQ_BANDS} です")
        if frequency_hz is None and q is None and gain_db is None:
            raise ValueError("frequency_hz / q / gain_db のいずれかを指定してください")
        ids = master_ids(ch, self.mixer.state)
        result: dict[str, Any] = {"target": f"{ch.label()} EQ band {band}"}
        if gain_db is not None:
            gain_db = cv.clamp(gain_db, cv.EQ_GAIN_MIN_DB, cv.EQ_GAIN_MAX_DB)
            check_upper_limit(gain_db, self.s.max_eq_boost_db, confirm, f"{ch.label()} EQ band {band}")
        if frequency_hz is not None:
            for cid in ids:
                self.mixer.setd(f"{cid}.eq.b{band}.freq", cv.frequency_to_value(frequency_hz))
        if q is not None:
            for cid in ids:
                self.mixer.setd(f"{cid}.eq.b{band}.q", cv.q_to_value(q))
        if gain_db is not None:
            gain_paths = [f"{cid}.eq.b{band}.gain" for cid in ids]
            current = cv.value_to_eq_gain_db(self.mixer.get_float(gain_paths[0], 0.5))
            if gain_db <= current:
                self.mixer.cancel_ramps_for(gain_paths)
                for p in gain_paths:
                    self.mixer.setd(p, cv.eq_gain_db_to_value(gain_db))
                result["gain"] = "即時に変更しました"
            else:
                ramp = self.mixer.start_ramp(
                    key=f"eq:{ch.id}.b{band}",
                    label=f"{ch.label()} EQ band {band} ゲイン",
                    paths=gain_paths,
                    start=current,
                    target=gain_db,
                    unit="dB",
                    rate_fn=constant_rate_fn(self.s.raise_rate_db_per_sec),
                    to_value=cv.eq_gain_db_to_value,
                )
                result["gain"] = f"ゆっくりブーストしています (約 {ramp.estimated_sec:.0f} 秒)"
        result["eq"] = self.get_eq(ch)["bands"][band - 1]
        result["eq"]["gain_target_db"] = gain_db
        return result

    def set_eq_bypass(self, ch: Channel, bypass: bool) -> dict[str, Any]:
        self._check_eq(ch)
        for cid in master_ids(ch, self.mixer.state):
            self.mixer.setd(f"{cid}.eq.bypass", 1 if bypass else 0)
        return {"target": ch.label(), "eq_bypass": bypass}

    def set_hpf(self, ch: Channel, frequency_hz: float | None) -> dict[str, Any]:
        """frequency_hz=None switches the high-pass filter off (20 Hz)."""
        self._check_eq(ch)
        hz = cv.HPF_MIN_HZ if frequency_hz is None else cv.clamp(frequency_hz, cv.HPF_MIN_HZ, cv.HPF_MAX_HZ)
        for cid in master_ids(ch, self.mixer.state):
            self.mixer.setd(f"{cid}.eq.hpf.freq", cv.frequency_to_value(hz))
        return {"target": ch.label(), "hpf_hz": "off" if hz <= cv.HPF_MIN_HZ else hz}

    # --- dynamics ------------------------------------------------------------

    def _check_dyn(self, ch: Channel) -> None:
        if ch.type not in DYN_TYPES:
            raise AddressError("ダイナミクスがあるのは input / line / player / fx / sub / aux です")

    def get_dynamics(self, ch: Channel) -> dict[str, Any]:
        self._check_dyn(ch)
        g = self.mixer.get_float
        comp = {p: round(g(f"{ch.id}.dyn.{p}"), 4) for p in COMPRESSOR_PARAMS}
        comp["bypass"] = _bool(self.mixer.get(f"{ch.id}.dyn.bypass"))
        gate = {p: round(g(f"{ch.id}.gate.{p}"), 4) for p in GATE_PARAMS}
        gate["enabled"] = _bool(self.mixer.get(f"{ch.id}.gate.enabled"))
        gate["bypass"] = _bool(self.mixer.get(f"{ch.id}.gate.bypass"))
        ratio = cv.comp_ratio(comp["ratio"])
        readable = {
            "compressor_threshold_db": cv.comp_threshold_db(comp["threshold"]),
            "compressor_ratio": "inf" if ratio == math.inf else round(ratio, 2),
            "compressor_gain_db": round(cv.comp_gain_db(comp["gain"]), 1),
            "compressor_outgain_db": cv.comp_outgain_db(comp["outgain"]),
            "gate_threshold_db": cv.gate_threshold_db(gate["thresh"]),
            "gate_depth_db": cv.gate_depth_db(gate["depth"]),
        }
        return {
            "note": "compressor/gate の値はミキサー内部の正規化値 (0〜1)。readable は Web UI の換算式による目安",
            "compressor": comp,
            "gate": gate,
            "readable": readable,
        }

    def set_compressor(self, ch: Channel, params: dict[str, float]) -> dict[str, Any]:
        self._check_dyn(ch)
        ids = master_ids(ch, self.mixer.state)
        done: dict[str, str] = {}
        for name, value in params.items():
            if name not in COMPRESSOR_PARAMS:
                raise ValueError(f"未知のコンプレッサーパラメータ: {name}")
            value = cv.clamp(float(value), 0.0, 1.0)
            paths = [f"{cid}.dyn.{name}" for cid in ids]
            current = self.mixer.get_float(paths[0])
            if name in COMPRESSOR_LOUDER and value > current:
                ramp = self.mixer.start_ramp(
                    key=f"dyn:{ch.id}.{name}",
                    label=f"{ch.label()} comp {name}",
                    paths=paths,
                    start=current,
                    target=value,
                    unit="(0-1)",
                    rate_fn=constant_rate_fn(self.s.dyn_raise_rate_per_sec),
                    to_value=lambda v: round(v, 6),
                )
                done[name] = f"ゆっくり上げています (約 {ramp.estimated_sec:.0f} 秒)"
            else:
                self.mixer.cancel_ramps_for(paths)
                for p in paths:
                    self.mixer.setd(p, value)
                done[name] = "設定しました"
        return {"target": ch.label(), "changes": done, "dynamics": self.get_dynamics(ch)}

    def set_gate(self, ch: Channel, params: dict[str, float], enabled: bool | None = None) -> dict[str, Any]:
        self._check_dyn(ch)
        ids = master_ids(ch, self.mixer.state)
        for name, value in params.items():
            if name not in GATE_PARAMS:
                raise ValueError(f"未知のゲートパラメータ: {name}")
            for cid in ids:
                self.mixer.setd(f"{cid}.gate.{name}", cv.clamp(float(value), 0.0, 1.0))
        if enabled is not None:
            for cid in ids:
                self.mixer.setd(f"{cid}.gate.enabled", 1 if enabled else 0)
        return {"target": ch.label(), "dynamics": self.get_dynamics(ch)}

    def set_dynamics_bypass(self, ch: Channel, section: str, bypass: bool) -> dict[str, Any]:
        self._check_dyn(ch)
        if section not in ("compressor", "gate"):
            raise ValueError("section は compressor か gate です")
        key = "dyn" if section == "compressor" else "gate"
        for cid in master_ids(ch, self.mixer.state):
            self.mixer.setd(f"{cid}.{key}.bypass", 1 if bypass else 0)
        return {"target": ch.label(), "section": section, "bypass": bypass}

    # --- meters ---------------------------------------------------------------

    def get_meters(self) -> dict[str, Any]:
        """Latest VU meter frame, labelled with channel names (dB, -80 = silence)."""
        vu = self.mixer.vu
        if vu is None or self.mixer.vu_at is None:
            return {
                "available": False,
                "note": "まだメーター情報を受信していません (実機は無音時、メーターが変化した時だけ送信します)",
            }
        age = time.monotonic() - self.mixer.vu_at
        st = self.mixer.state
        out: dict[str, Any] = {
            "available": True,
            "age_sec": round(age, 1),
            "note": "dB はメーター上の値 (-80 = 無音, 0 = フルスケール)。age_sec が大きい場合は"
            "メーターが変化していない (無音など) 可能性があります",
        }
        if len(vu.master) >= 2:
            out["master"] = {
                "l_db": vu.master[0]["post_fader_db"],
                "r_db": vu.master[1]["post_fader_db"],
                "pre_fader_l_db": vu.master[0]["post_db"],
                "pre_fader_r_db": vu.master[1]["post_db"],
            }
        caps = capabilities(self.model)
        for type_ in ("input", "line", "player", "fx", "sub", "aux"):
            entries = []
            for i, m in enumerate(getattr(vu, type_)[: caps[type_]]):
                ch = Channel(type_, i + 1)  # type: ignore[arg-type]
                entries.append({"number": i + 1, "name": channel_name(ch, st), **m})
            out[type_] = entries
        return out

    def get_spectrum(self) -> dict[str, Any]:
        rta = self.mixer.rta
        if rta is None or self.mixer.rta_at is None:
            return {"available": False}
        from .meters import rta_frequencies

        target = self.mixer.get("var.rta", "")
        # condense the 120 analyser bands into the 31 graphic-EQ bands for readability
        freqs = rta_frequencies(len(rta))
        bands = []
        for f0 in cv.GEQ_FREQUENCIES:
            near = [lv for f, lv in zip(freqs, rta, strict=True) if abs(math.log2(f / f0)) <= 1 / 6]
            bands.append({"hz": f0, "db": max(near) if near else None})
        return {
            "available": True,
            "source": target or "master",
            "age_sec": round(time.monotonic() - self.mixer.rta_at, 1),
            "bands": bands,
        }

    # --- graphic EQ (AUX / master) ---------------------------------------------

    def _geq_prefixes(self, target: Channel, side: str = "both") -> list[str]:
        """State prefixes (ending with '.') of the graphic EQ bands to write."""
        st = self.mixer.state
        if target.is_master:
            if side not in ("both", "left", "right"):
                raise ValueError("side は both / left / right のいずれかです")
            linked = _bool(st.get("m.eq.linked", 1))
            if linked or side == "both":
                return ["m.eq.peak.l.", "m.eq.peak.r."]
            return ["m.eq.peak.l." if side == "left" else "m.eq.peak.r."]
        if target.type != "aux":
            raise AddressError("グラフィック EQ があるのは aux と master だけです")
        return [f"{cid}.eq.peak." for cid in master_ids(target, st)]

    def get_graphic_eq(self, target: Channel) -> dict[str, Any]:
        st = self.mixer.state
        if target.is_master:
            sides = {"left": "m.eq.peak.l.", "right": "m.eq.peak.r."}
            bypass = _bool(st.get("m.eq.bypass"))
            linked = _bool(st.get("m.eq.linked", 1))
        elif target.type == "aux":
            sides = {"mono": f"{target.id}.eq.peak."}
            bypass = _bool(st.get(f"{target.id}.eq.bypass"))
            linked = None
        else:
            raise AddressError("グラフィック EQ があるのは aux と master だけです")
        result: dict[str, Any] = {"target": target.label(), "bypass": bypass}
        if linked is not None:
            result["linked"] = linked
        for name, prefix in sides.items():
            if name == "right" and linked:
                continue
            gains = [
                cv.value_to_geq_db(self.mixer.get_float(f"{prefix}{i}", 0.5)) for i in range(len(cv.GEQ_FREQUENCIES))
            ]
            result["bands" if name in ("mono", "left") else "bands_right"] = [
                {"band": i + 1, "hz": f, "gain_db": g}
                for i, (f, g) in enumerate(zip(cv.GEQ_FREQUENCIES, gains, strict=True))
            ]
        result["non_flat"] = [b for b in result["bands"] if b["gain_db"] != 0]
        return result

    def set_graphic_eq_band(
        self,
        target: Channel,
        gain_db: float,
        band: int | None = None,
        frequency_hz: float | None = None,
        side: str = "both",
        confirm: bool = False,
    ) -> dict[str, Any]:
        if (band is None) == (frequency_hz is None):
            raise ValueError("band (1〜31) と frequency_hz のどちらか一方を指定してください")
        if band is None:
            assert frequency_hz is not None
            band = cv.nearest_geq_band(frequency_hz)
        if not 1 <= band <= len(cv.GEQ_FREQUENCIES):
            raise AddressError("グラフィック EQ のバンドは 1〜31 です")
        gain_db = cv.clamp(gain_db, cv.GEQ_MIN_DB, cv.GEQ_MAX_DB)
        hz = cv.GEQ_FREQUENCIES[band - 1]
        label = f"{target.label()} GEQ {hz}Hz"
        check_upper_limit(gain_db, self.s.max_eq_boost_db, confirm, label)
        paths = [f"{p}{band - 1}" for p in self._geq_prefixes(target, side)]
        current = cv.value_to_geq_db(self.mixer.get_float(paths[0], 0.5))
        result: dict[str, Any] = {"target": label, "band": band, "before_db": current, "target_db": gain_db}
        if gain_db <= current:
            self.mixer.cancel_ramps_for(paths)
            for p in paths:
                self.mixer.setd(p, cv.geq_db_to_value(gain_db))
            result["mode"] = "即時に変更しました"
        else:
            ramp = self.mixer.start_ramp(
                key=f"geq:{paths[0]}",
                label=label,
                paths=paths,
                start=current,
                target=gain_db,
                unit="dB",
                rate_fn=constant_rate_fn(self.s.raise_rate_db_per_sec),
                to_value=cv.geq_db_to_value,
            )
            result["mode"] = f"ゆっくりブーストしています (約 {ramp.estimated_sec:.0f} 秒)"
        return result

    def reset_graphic_eq(self, target: Channel, side: str = "both", confirm: bool = False) -> dict[str, Any]:
        """Flatten the graphic EQ: boosts are cut immediately, cuts are raised slowly."""
        if not confirm:
            non_flat = len(self.get_graphic_eq(target)["non_flat"])
            raise SafetyError(
                f"{target.label()} のグラフィック EQ には調整済みのバンドが {non_flat} 個あります (会場の音響補正の"
                "可能性があります)。本当にフラットに戻すなら confirm=true を付けて再実行してください。"
            )
        raised, lowered = [], []
        for prefix in self._geq_prefixes(target, side):
            for i in range(len(cv.GEQ_FREQUENCIES)):
                path = f"{prefix}{i}"
                current = cv.value_to_geq_db(self.mixer.get_float(path, 0.5))
                if current > 0:
                    self.mixer.cancel_ramps_for([path])
                    self.mixer.setd(path, 0.5)
                    lowered.append(cv.GEQ_FREQUENCIES[i])
                elif current < 0:
                    self.mixer.start_ramp(
                        key=f"geq:{path}",
                        label=f"{target.label()} GEQ {cv.GEQ_FREQUENCIES[i]}Hz",
                        paths=[path],
                        start=current,
                        target=0.0,
                        unit="dB",
                        rate_fn=constant_rate_fn(self.s.raise_rate_db_per_sec),
                        to_value=cv.geq_db_to_value,
                    )
                    raised.append(cv.GEQ_FREQUENCIES[i])
        return {
            "target": target.label(),
            "cut_immediately_hz": sorted(set(lowered)),
            "raising_slowly_hz": sorted(set(raised)),
        }

    def set_graphic_eq_bypass(self, target: Channel, bypass: bool) -> dict[str, Any]:
        if target.is_master:
            paths = ["m.eq.bypass"]
        elif target.type == "aux":
            paths = [f"{cid}.eq.bypass" for cid in master_ids(target, self.mixer.state)]
        else:
            raise AddressError("グラフィック EQ があるのは aux と master だけです")
        for p in paths:
            self.mixer.setd(p, 1 if bypass else 0)
        return {"target": target.label(), "graphic_eq_bypass": bypass}

    # --- fades ---------------------------------------------------------------

    def stop_fades(self) -> dict[str, Any]:
        return {"stopped": self.mixer.cancel_all_ramps("stop_fades で停止")}


__all__ = ["MixerController", "SafetyError", "AddressError"]
