"""Channel addressing for the Soundcraft Ui state tree.

Channel numbers in the public API are 1-based; the mixer state uses 0-based indices:
``i.0.mix`` is the fader of input 1 on the master bus, ``i.0.aux.2.value`` its send to AUX 3.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

ChannelType = Literal["master", "input", "line", "player", "fx", "sub", "aux"]

TYPE_CODES: dict[str, str] = {
    "input": "i",
    "line": "l",
    "player": "p",
    "fx": "f",
    "sub": "s",
    "aux": "a",
}

# Ported from DEVICE_CAPABILITIES in soundcraft-ui
CAPABILITIES: dict[str, dict[str, int]] = {
    "ui12": {"input": 8, "line": 2, "player": 2, "fx": 4, "sub": 4, "aux": 4},
    "ui16": {"input": 12, "line": 2, "player": 2, "fx": 4, "sub": 4, "aux": 6},
    "ui24": {"input": 24, "line": 2, "player": 2, "fx": 4, "sub": 6, "aux": 10},
}

EQ_TYPES = {"input", "line", "player", "fx", "sub"}
DYN_TYPES = {"input", "line", "player", "fx", "sub", "aux"}
STEREO_LINKABLE = {"input", "line", "player", "aux"}
AUX_SEND_SOURCES = {"input", "line", "player", "fx"}
EQ_BANDS = 4


class AddressError(ValueError):
    pass


def capabilities(model: str | None) -> dict[str, int]:
    return CAPABILITIES.get(model or "ui12", CAPABILITIES["ui12"])


@dataclass(frozen=True)
class Channel:
    type: ChannelType
    number: int | None = None

    @property
    def is_master(self) -> bool:
        return self.type == "master"

    @property
    def code(self) -> str:
        return "m" if self.is_master else TYPE_CODES[self.type]

    @property
    def id(self) -> str:
        """State path prefix of the channel on the master bus (e.g. ``i.2`` or ``m``)."""
        if self.is_master:
            return "m"
        assert self.number is not None
        return f"{self.code}.{self.number - 1}"

    def label(self) -> str:
        return "master" if self.is_master else f"{self.type} {self.number}"


def make_channel(type_: str, number: int | None, model: str | None) -> Channel:
    if type_ == "master":
        return Channel("master")
    if type_ not in TYPE_CODES:
        raise AddressError(f"未知のチャンネル種別: {type_}")
    caps = capabilities(model)
    if number is None:
        raise AddressError(f"{type_} にはチャンネル番号 (1〜{caps[type_]}) が必要です")
    if not 1 <= number <= caps[type_]:
        raise AddressError(f"{type_} の番号は 1〜{caps[type_]} です (指定: {number})")
    return Channel(type_, number)  # type: ignore[arg-type]


def check_aux_bus(bus: int, model: str | None) -> None:
    n = capabilities(model)["aux"]
    if not 1 <= bus <= n:
        raise AddressError(f"AUX バス番号は 1〜{n} です (指定: {bus})")


def linked_number(number: int, stereo_index: Any) -> int | None:
    """Port of getLinkedChannelNumber(): stereoIndex 0 = left of pair, 1 = right of pair."""
    if stereo_index == 0:
        return number + 1
    if stereo_index == 1:
        return number - 1
    return None


def stereo_partner(ch: Channel, state: Mapping[str, Any]) -> Channel | None:
    if ch.is_master or ch.type not in STEREO_LINKABLE or ch.number is None:
        return None
    idx = state.get(f"{ch.id}.stereoIndex", -1)
    partner = linked_number(ch.number, int(idx) if isinstance(idx, (int, float)) else -1)
    if partner is None:
        return None
    return Channel(ch.type, partner)


def master_ids(ch: Channel, state: Mapping[str, Any]) -> list[str]:
    """Channel ids that receive a master-bus command (the channel plus its stereo partner)."""
    ids = [ch.id]
    partner = stereo_partner(ch, state)
    if partner is not None:
        ids.append(partner.id)
    return ids


def send_id(source: Channel, bus: int) -> str:
    return f"{source.id}.aux.{bus - 1}"


def aux_send_ids(source: Channel, bus: int, state: Mapping[str, Any]) -> list[str]:
    """Port of AuxChannel linking: source stereo pair x AUX bus stereo pair."""
    ids = [send_id(source, bus)]
    src_partner = stereo_partner(source, state)
    aux_partner = stereo_partner(Channel("aux", bus), state)
    if src_partner is not None:
        ids.append(send_id(src_partner, bus))
    if aux_partner is not None:
        assert aux_partner.number is not None
        ids.append(send_id(source, aux_partner.number))
    if src_partner is not None and aux_partner is not None:
        assert aux_partner.number is not None
        ids.append(send_id(src_partner, aux_partner.number))
    return ids


def aux_pan_ids(source: Channel, bus: int, state: Mapping[str, Any]) -> list[str]:
    """Pan on an AUX send is only mirrored to the linked AUX bus (not the linked source)."""
    ids = [send_id(source, bus)]
    aux_partner = stereo_partner(Channel("aux", bus), state)
    if aux_partner is not None:
        assert aux_partner.number is not None
        ids.append(send_id(source, aux_partner.number))
    return ids


def _lr(n: int) -> str:
    return {1: "L", 2: "R"}.get(n, str(n))


def default_name(ch: Channel) -> str:
    n = ch.number or 0
    return {
        "master": "MASTER",
        "input": f"CH {n}",
        "line": f"LINE IN {_lr(n)}",
        "player": f"PLAYER {_lr(n)}",
        "fx": f"FX {n}",
        "sub": f"SUB {n}",
        "aux": f"AUX {n}",
    }[ch.type]


def channel_name(ch: Channel, state: Mapping[str, Any]) -> str:
    name = state.get(f"{ch.id}.name")
    return name if isinstance(name, str) and name else default_name(ch)


# --- mute groups ---------------------------------------------------------------
# From the mixer's web app: the global "mgmask" holds the active mute groups (bits 0..5),
# MUTE FX (bit 22) and MUTE ALL (bit 23). "<ch>.mgmask" holds the groups a channel belongs to.
# Unmuting a channel that is muted by a group sets "<ch>.forceunmute" instead of clearing "mute".

MUTE_GROUPS = 6
MUTE_FX_BIT = 22
MUTE_ALL_BIT = 23


def _int(v: Any) -> int:
    return int(v) if isinstance(v, (int, float)) else 0


def group_mute_reason(ch: Channel, state: Mapping[str, Any]) -> str | None:
    """Why a mute group currently mutes this channel (None if no group does)."""
    if ch.is_master:
        return None
    active = _int(state.get("mgmask", 0))
    if active >> MUTE_ALL_BIT & 1:
        return "MUTE ALL"
    if ch.type == "fx" and active >> MUTE_FX_BIT & 1:
        return "MUTE FX"
    member = _int(state.get(f"{ch.id}.mgmask", 0)) & active
    groups = [str(i + 1) for i in range(MUTE_GROUPS) if member >> i & 1]
    return f"mute group {','.join(groups)}" if groups else None


def mute_status(ch: Channel, state: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Effective mute state of a channel on the master bus and the reason for it."""
    direct = bool(_int(state.get(f"{ch.id}.mute", 0)))
    group = group_mute_reason(ch, state)
    forced = bool(_int(state.get(f"{ch.id}.forceunmute", 0)))
    if group and forced:
        return False, f"{group} を forceunmute で個別に解除中"
    if direct:
        return True, "channel"
    if group:
        return True, group
    return False, None
