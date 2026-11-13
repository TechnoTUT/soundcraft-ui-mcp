from __future__ import annotations

import asyncio

import pytest

from soundcraft_ui_mcp.config import Settings
from soundcraft_ui_mcp.controller import MixerController
from soundcraft_ui_mcp.converters import db_to_fader
from soundcraft_ui_mcp.mixer import MixerClient
from soundcraft_ui_mcp.protocol import parse_line


def base_state() -> dict[str, float | str]:
    st: dict[str, float | str] = {
        "model": "ui12",
        "firmware": "1.0.0-ui12",
        "m.mix": db_to_fader(0),
        "m.pan": 0.5,
    }
    for code, n in (("i", 8), ("l", 2), ("p", 2), ("f", 4), ("s", 4), ("a", 4)):
        for k in range(n):
            cid = f"{code}.{k}"
            st.update({f"{cid}.mix": db_to_fader(-10), f"{cid}.mute": 0, f"{cid}.solo": 0, f"{cid}.pan": 0.5})
            st[f"{cid}.name"] = ""
            if code in "ilpa":
                st[f"{cid}.stereoIndex"] = -1
            if code in "ilpf":
                for b in range(4):
                    st.update({f"{cid}.aux.{b}.value": 0.0, f"{cid}.aux.{b}.mute": 0, f"{cid}.aux.{b}.post": 0})
            if code in "ilpfs":
                for b in range(1, 5):
                    st.update(
                        {
                            f"{cid}.eq.b{b}.freq": 0.5,
                            f"{cid}.eq.b{b}.q": 0.5252185347,
                            f"{cid}.eq.b{b}.gain": 0.5,
                        }
                    )
                st[f"{cid}.eq.hpf.freq"] = 0.0
                st[f"{cid}.eq.bypass"] = 0
            for p in ("threshold", "ratio", "attack", "release", "gain", "outgain", "softknee", "bypass"):
                st[f"{cid}.dyn.{p}"] = 0.3
    return st


class FakeMixer(MixerClient):
    """MixerClient that records outgoing messages instead of using a socket."""

    def __init__(self) -> None:
        super().__init__("test", tick_sec=0.01)
        self.sent: list[str] = []
        self.state.update(base_state())
        self.status = "open"
        self.synced.set()

    def _send(self, message: str) -> None:
        self.sent.append(message)

    def sent_paths(self) -> list[str]:
        return [m.split("^")[1] for m in self.sent]

    def inject(self, line: str) -> None:
        """Simulate an inbound message from the mixer (e.g. another client moved a fader)."""
        self._handle_frame("3:::" + line)

    def last_value(self, path: str) -> float:
        for m in reversed(self.sent):
            cmd = parse_line(m)
            if cmd and cmd.path == path:
                return float(cmd.value)
        raise KeyError(path)


@pytest.fixture
def settings() -> Settings:
    # fast ramps for tests, but same shape as production
    return Settings(raise_rate_db_per_sec=100.0, dyn_raise_rate_per_sec=10.0, ramp_tick_sec=0.01)


@pytest.fixture
def mixer() -> FakeMixer:
    return FakeMixer()


@pytest.fixture
def ctl(mixer: FakeMixer, settings: Settings) -> MixerController:
    return MixerController(mixer, settings)


async def wait_ramps(mixer: MixerClient, timeout: float = 3.0) -> None:
    async def _wait() -> None:
        while mixer.active_ramps():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait(), timeout)
