"""Mock Soundcraft Ui mixer: a WebSocket server that speaks the mixer's text protocol.

Behaves like the real device closely enough for development without hardware:

* on connect it sends ``1::`` and then the full state as ``SETD``/``SETS`` lines
  (split into multi-line frames like the real mixer),
* commands received from a client update the state and are broadcast to every *other* client,
* ``ALIVE`` keepalives are accepted, and ``2::`` heartbeats are sent periodically,
* ``VU2`` meter frames and ``RTA`` spectrum frames are generated from a small signal model
  (``--signal demo|silent``) that reacts to gain, EQ, gate, compressor, faders, mutes and sends,
* AUX / master graphic EQs, master stereo dynamics and AFS keys are part of the state.

Usage::

    uv run soundcraft-ui-mock                         # ws://127.0.0.1:8080, synthetic Ui12 state
    uv run soundcraft-ui-mock --state state.json      # start from a saved state
    uv run soundcraft-ui-mock dump --from 192.168.12.22 -o state.json   # snapshot a real mixer

Then point the MCP server at it: ``MIXER_IP=127.0.0.1:8080 uv run soundcraft-ui-mcp``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
import time
from pathlib import Path

import websockets
from websockets.asyncio.server import ServerConnection

from .converters import db_to_fader
from .meters import encode_rta, encode_vu2
from .mock_audio import PROFILES, SignalModel
from .paths import CAPABILITIES, TYPE_CODES
from .protocol import build_setd, build_sets, frame, parse_line, split_frame

log = logging.getLogger("soundcraft_ui_mcp.mock")

State = dict[str, float | str]

FRAME_LINES = 200
HEARTBEAT_SEC = 2.0

# Default values as observed on a Ui12 after initialisation
EQ_DEFAULTS = {1: 0.3286901902, 2: 0.5584347738, 3: 0.7563259869, 4: 0.887124964, 5: 0.9542171999}
EQ_Q_DEFAULT = 0.5252185347
DYN_DEFAULTS = {
    "threshold": 0.875,
    "ratio": 0.0,
    "attack": 0.34375,
    "release": 0.4887695312,
    "gain": 0.75,
    "outgain": 0.3334960938,
    "softknee": 0.0,
    "bypass": 0.0,
}
GEQ_BANDS = 31
# FX processors: Ui default order is reverb, delay, chorus, room (soundcraft-ui FxType 0..3)
FX_DEFAULT_TYPES = (0.0, 1.0, 2.0, 3.0)
FX_PARAM_DEFAULTS = {"par1": 0.5, "par2": 0.4, "par3": 0.5, "par4": 1.0, "par5": 0.0, "par6": 0.0}
DEESSER_DEFAULTS = {"enabled": 0.0, "freq": 0.3617576874, "ratio": 0.4, "threshold": 0.7250676895}
DIGITECH_DEFAULTS = {
    "enabled": 0.0,
    "amp": 0.0,
    "cab": 0.0,
    "gain": 0.75,
    "level": 0.5,
    "bass": 0.5,
    "mid": 0.5,
    "treble": 0.5,
}
MASTER_DYN_DEFAULTS = {
    "threshold": 0.9375,
    "ratio": 0.0,
    "attack": 0.0,
    "release": 0.2490234375,
    "hold": 0.158,
    "gain": 0.75,
    "outgain": 0.3334960938,
    "softknee": 0.0,
}
GATE_DEFAULTS = {
    "thresh": 0.0,
    "depth": 0.0,
    "attack": 0.0,
    "hold": 0.3042441765,
    "release": 0.3150737134,
    "enabled": 1.0,
    "bypass": 0.0,
}


def default_state(model: str = "ui12") -> State:
    """Synthetic but realistic initial state (master 0 dB, every channel at -10 dB, unmuted)."""
    caps = CAPABILITIES[model]
    st: State = {
        "model": model,
        "firmware": f"1.0.0000-{model}",
        "m.mix": db_to_fader(0),
        "m.pan": 0.5,
        "m.dim": 0.0,
        "m.name": "MASTER",
        "settings.multiplesolo": 0.0,
        "settings.solotype": 1.0,
        "mgmask": 0.0,
        "flavour": "1",
        "schema": "6",
        "type": f"{caps['input']}ch",
    }
    n_aux = caps["aux"]
    n_fx = caps["fx"]
    for type_, code in TYPE_CODES.items():
        for k in range(caps[type_]):
            cid = f"{code}.{k}"
            st.update(
                {
                    f"{cid}.mix": db_to_fader(-10),
                    f"{cid}.mute": 0.0,
                    f"{cid}.solo": 0.0,
                    f"{cid}.pan": 0.5,
                    f"{cid}.name": "",
                }
            )
            if code in "ilpa":
                st[f"{cid}.stereoIndex"] = -1.0
            if code in "ilpf":
                for b in range(n_aux):
                    st.update(
                        {
                            f"{cid}.aux.{b}.value": 0.0,
                            f"{cid}.aux.{b}.mute": 0.0,
                            f"{cid}.aux.{b}.post": 0.0,
                            f"{cid}.aux.{b}.pan": 0.5,
                        }
                    )
            if code in "ilps":
                for b in range(n_fx):
                    st.update({f"{cid}.fx.{b}.value": 0.0, f"{cid}.fx.{b}.mute": 0.0})
            if code in "ilpfs":
                for b, freq in EQ_DEFAULTS.items():
                    st.update(
                        {
                            f"{cid}.eq.b{b}.freq": freq,
                            f"{cid}.eq.b{b}.q": EQ_Q_DEFAULT,
                            f"{cid}.eq.b{b}.gain": 0.5,
                        }
                    )
                st[f"{cid}.eq.hpf.freq"] = 0.0
                st[f"{cid}.eq.bypass"] = 0.0
            if code in "ilpfsa":
                st.update({f"{cid}.mgmask": 0.0, f"{cid}.forceunmute": 0.0})
                st.update({f"{cid}.dyn.{p}": v for p, v in DYN_DEFAULTS.items()})
                st.update({f"{cid}.gate.{p}": v for p, v in GATE_DEFAULTS.items()})
            st[f"{cid}.safe"] = 0.0
            if code == "i":
                st.update({f"{cid}.gain": 0.5, f"{cid}.phantom": 0.0})
                st.update({f"{cid}.hiz": 0.0, f"{cid}.invert": 0.0, f"{cid}.color": 0.0, f"{cid}.instrument": ""})
                st.update({f"{cid}.deesser.{p}": v for p, v in DEESSER_DEFAULTS.items()})
                st.update({f"{cid}.digitech.{p}": v for p, v in DIGITECH_DEFAULTS.items()})
            if code == "l":
                st.update({f"{cid}.gain": 0.4, f"{cid}.phantom": 0.0})
            if code == "f":
                st[f"{cid}.fxtype"] = FX_DEFAULT_TYPES[k % len(FX_DEFAULT_TYPES)]
                st[f"{cid}.bpm"] = 120.0
                st.update({f"{cid}.{p}": v for p, v in FX_PARAM_DEFAULTS.items()})
            if code in "ilps":
                st[f"{cid}.subgroup"] = -1.0
            if code == "a":
                # AUX outputs: 31-band graphic EQ + feedback suppression (AFS)
                st.update({f"{cid}.eq.peak.{b}": 0.5 for b in range(GEQ_BANDS)})
                st.update({f"{cid}.eq.bypass": 0.0, f"{cid}.eq.linked": 0.0})
                st.update(afs_defaults(cid))
    # master: stereo graphic EQ, stereo compressor, gate and AFS
    for side in ("l", "r"):
        st.update({f"m.eq.peak.{side}.{b}": 0.5 for b in range(GEQ_BANDS)})
        st.update({f"m.dyn.{side}.{p}": v for p, v in MASTER_DYN_DEFAULTS.items()})
    st.update({"m.eq.bypass": 0.0, "m.eq.linked": 1.0, "m.dyn.bypass": 0.0, "m.dyn.linked": 1.0})
    st.update({f"m.gate.{p}": v for p, v in GATE_DEFAULTS.items()})
    st.update({"m.gate.enabled": 0.0, "m.gate.bypass": 1.0})
    st.update(afs_defaults("m"))
    st["var.rta"] = ""
    return st


def afs_defaults(prefix: str) -> State:
    st: State = {
        f"{prefix}.afs.enabled": 0.0,
        f"{prefix}.afs.sensitivity": 0.5,
        f"{prefix}.afs.numfixed": 6.0,
        f"{prefix}.afs.numtotal": 12.0,
        f"{prefix}.afs.logic": 1.0,
        f"{prefix}.afs.livelift": 0.0,
    }
    st.update({f"{prefix}.afs.eq.{i}": "1000.0000000000,116.0000000000,0.0000000000,0" for i in range(12)})
    return st


def state_lines(state: State) -> list[str]:
    return [
        build_sets(path, value) if isinstance(value, str) else build_setd(path, value) for path, value in state.items()
    ]


class MockMixer:
    def __init__(
        self,
        state: State | None = None,
        *,
        echo: bool = False,
        signal: SignalModel | None = None,
        vu_rate: float = 10.0,
        rta_rate: float = 10.0,
    ) -> None:
        self.state: State = dict(state) if state is not None else default_state()
        self.echo = echo
        self.clients: set[ServerConnection] = set()
        self.received: list[str] = []
        model = str(self.state.get("model", "ui12"))
        self.signal = signal or SignalModel(model=model if model in CAPABILITIES else "ui12")
        self.vu_rate = vu_rate
        self.rta_rate = rta_rate
        self._last_vu: str | None = None
        self._levels: dict[str, list[dict[str, float]]] | None = None
        self._meter_task: asyncio.Task[None] | None = None
        self._t0 = time.monotonic()

    # --- meters ---------------------------------------------------------------

    def vu_line(self) -> str:
        """Compute the current meter frame and return it as a ``VU2^`` line."""
        self._levels = self.signal.compute(self.state, time.monotonic() - self._t0)
        return "VU2^" + encode_vu2(self._levels)

    def rta_line(self) -> str:
        if self._levels is None:
            self.vu_line()
        assert self._levels is not None
        target = self.state.get("var.rta", "")
        bands = self.signal.spectrum(self.state, self._levels, target if isinstance(target, str) else "")
        return "RTA^" + encode_rta(bands)

    async def start_meters(self) -> None:
        if self._meter_task is None or self._meter_task.done():
            self._meter_task = asyncio.create_task(self._meter_loop())

    async def stop_meters(self) -> None:
        if self._meter_task:
            self._meter_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._meter_task

    async def _meter_loop(self) -> None:
        tick = 1 / max(self.vu_rate, self.rta_rate, 0.1)
        next_vu = next_rta = 0.0
        while True:
            now = time.monotonic()
            if self.clients and self.vu_rate > 0 and now >= next_vu:
                next_vu = now + 1 / self.vu_rate
                line = self.vu_line()
                if line != self._last_vu:  # like the real mixer: only when the meters change
                    self._last_vu = line
                    await self.broadcast(line)
            if self.clients and self.rta_rate > 0 and now >= next_rta:
                next_rta = now + 1 / self.rta_rate
                await self.broadcast(self.rta_line())
            await asyncio.sleep(tick)

    async def handler(self, ws: ServerConnection) -> None:
        self.clients.add(ws)
        log.info("client connected: %s (%d total)", ws.remote_address, len(self.clients))
        heartbeat = asyncio.create_task(self._heartbeat(ws))
        try:
            await ws.send("1::")
            lines = state_lines(self.state)
            for i in range(0, len(lines), FRAME_LINES):
                await ws.send(frame("\n".join(lines[i : i + FRAME_LINES])))
            await ws.send(frame(self.vu_line()))
            async for raw in ws:
                if isinstance(raw, str):
                    await self._handle(ws, raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            heartbeat.cancel()
            self.clients.discard(ws)
            log.info("client disconnected (%d left)", len(self.clients))

    async def _heartbeat(self, ws: ServerConnection) -> None:
        with contextlib.suppress(websockets.ConnectionClosed):
            while True:
                await asyncio.sleep(HEARTBEAT_SEC)
                await ws.send("2::")

    async def _handle(self, sender: ServerConnection, raw: str) -> None:
        for line in split_frame(raw):
            self.received.append(line)
            cmd = parse_line(line)
            if cmd is None:
                continue  # ALIVE and other non-state commands
            self.state[cmd.path] = cmd.value
            log.info("%s = %s", cmd.path, cmd.value)
            await self.broadcast(line, exclude=None if self.echo else sender)

    async def broadcast(self, line: str, exclude: ServerConnection | None = None) -> None:
        for client in list(self.clients):
            if client is not exclude:
                with contextlib.suppress(websockets.ConnectionClosed):
                    await client.send(frame(line))

    async def set_external(self, path: str, value: float | str) -> None:
        """Change a value as if someone used the mixer's own web UI."""
        self.state[path] = value
        line = build_sets(path, value) if isinstance(value, str) else build_setd(path, value)
        await self.broadcast(line)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.state, indent=1, ensure_ascii=False))


def load_state(path: Path) -> State:
    data = json.loads(path.read_text())
    return {k: (v if isinstance(v, str) else float(v)) for k, v in data.items()}


async def serve(host: str, port: int, mock: MockMixer, save_path: Path | None) -> None:
    async with websockets.serve(mock.handler, host, port, ping_interval=None, max_size=None) as server:
        log.info("mock %s mixer listening on ws://%s:%d", mock.state.get("model"), host, port)
        await mock.start_meters()
        try:
            await server.serve_forever()
        finally:
            await mock.stop_meters()
            if save_path:
                mock.save(save_path)
                log.info("state saved to %s", save_path)


async def dump(source: str, timeout: float = 8.0) -> State:
    """Read the full state of a real mixer (read-only)."""
    from .mixer import MixerClient

    client = MixerClient(source)
    await client.start()
    try:
        if not await client.wait_ready(timeout):
            raise SystemExit(f"could not sync with mixer {source}: {client.last_error}")
        return dict(client.state)
    finally:
        await client.stop()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="soundcraft-ui-mock", description="Mock Soundcraft Ui mixer")
    sub = p.add_subparsers(dest="command")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--state", type=Path, help="JSON state file to start from")
    p.add_argument("--model", choices=sorted(CAPABILITIES), default="ui12")
    p.add_argument("--save-on-exit", type=Path, help="write the final state to this JSON file")
    p.add_argument("--echo", action="store_true", help="also echo commands back to the sender")
    p.add_argument("--signal", choices=sorted(PROFILES), default="demo", help="simulated input signals for meters")
    p.add_argument("--vu-rate", type=float, default=10.0, help="VU2 frames per second (0 = off)")
    p.add_argument("--rta-rate", type=float, default=10.0, help="RTA frames per second (0 = off)")
    p.add_argument("--log-level", default="INFO")
    d = sub.add_parser("dump", help="snapshot a real mixer's state to JSON (read-only)")
    d.add_argument("--from", dest="source", required=True, help="mixer address, e.g. 192.168.12.22")
    d.add_argument("-o", "--output", type=Path, required=True)
    args = p.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr, format="%(asctime)s %(message)s")

    if args.command == "dump":
        state = asyncio.run(dump(args.source))
        args.output.write_text(json.dumps(state, indent=1, ensure_ascii=False))
        print(f"saved {len(state)} keys to {args.output}", file=sys.stderr)
        return

    state = load_state(args.state) if args.state else default_state(args.model)
    model = str(state.get("model", args.model))
    signal = SignalModel.from_profile(args.signal, model if model in CAPABILITIES else "ui12")
    mock = MockMixer(state, echo=args.echo, signal=signal, vu_rate=args.vu_rate, rta_rate=args.rta_rate)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(args.host, args.port, mock, args.save_on_exit))


if __name__ == "__main__":
    main()
