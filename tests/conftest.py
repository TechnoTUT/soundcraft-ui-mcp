from __future__ import annotations

import asyncio

import pytest
from soundcraft_ui_mock import default_state

from soundcraft_ui_mcp.config import Settings
from soundcraft_ui_mcp.controller import MixerController
from soundcraft_ui_mcp.mixer import MixerClient
from soundcraft_ui_mcp.protocol import parse_line


def base_state() -> dict[str, float | str]:
    return default_state("ui12")


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
