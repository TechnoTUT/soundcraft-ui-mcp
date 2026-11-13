"""Asynchronous connection to a Soundcraft Ui mixer with state mirror and slow ramps."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import websockets

from .meters import VuFrame, decode_rta, decode_vu2
from .protocol import build_setd, build_sets, frame, parse_line, split_frame

log = logging.getLogger(__name__)

KEEPALIVE_SEC = 1.0
RECONNECT_SEC = 2.0
SYNC_QUIET_SEC = 0.3
ECHO_TOLERANCE = 2e-3
LIST_TIMEOUT_SEC = 1.5
# A ramp write may still be in flight when someone moves the control on the mixer and land after
# their change. If we wrote within this window, the person's value is re-sent after a short delay.
CROSSING_WINDOW_SEC = 0.5
REASSERT_DELAY_SEC = 0.15

# Resource lists the mixer sends on request (from soundcraft-ui). "keyed" lists carry the
# owner name as 2nd field, e.g. "SNAPSHOTLIST^<show>^snap1^snap2".
RESOURCE_LISTS = {"PLISTS": False, "PLIST_TRACKS": True, "SHOWLIST": False, "SNAPSHOTLIST": True, "CUELIST": True}


class MixerNotReady(RuntimeError):
    pass


@dataclass
class Ramp:
    key: str
    label: str
    paths: list[str]
    current: float
    target: float
    unit: str
    rate_fn: Callable[[float], float]
    to_value: Callable[[float], float]
    started_at: float = field(default_factory=time.monotonic)
    estimated_sec: float = 0.0
    task: asyncio.Task[None] | None = None
    sent: dict[str, deque[float]] = field(default_factory=dict)
    end_reason: str | None = None
    last_sent_at: float = 0.0

    def info(self) -> dict[str, Any]:
        elapsed = time.monotonic() - self.started_at
        return {
            "label": self.label,
            "current": round(self.current, 2),
            "target": round(self.target, 2),
            "unit": self.unit,
            "elapsed_sec": round(elapsed, 1),
            "remaining_sec_estimate": round(max(0.0, self.estimated_sec - elapsed), 1),
            **({"ended": self.end_reason} if self.end_reason else {}),
        }


def estimate_ramp_seconds(start: float, target: float, rate_fn: Callable[[float], float], tick: float) -> float:
    cur, t = start, 0.0
    direction = 1 if target >= start else -1
    for _ in range(1_000_000):
        if (target - cur) * direction <= 1e-9:
            break
        cur = _advance(cur, target, rate_fn(cur) * tick)
        t += tick
    return t


def _advance(cur: float, target: float, step: float) -> float:
    if target > cur:
        return min(target, cur + step)
    return max(target, cur - step)


class MixerClient:
    def __init__(self, ip: str, tick_sec: float = 0.1) -> None:
        self.ip = ip
        self.tick_sec = tick_sec
        self.state: dict[str, float | str] = {}
        self.status = "closed"
        self.last_error: str | None = None
        self.synced = asyncio.Event()
        self._ws: Any = None
        self._runner: asyncio.Task[None] | None = None
        self._stop = False
        self._ramps: dict[str, Ramp] = {}
        self._finished: deque[dict[str, Any]] = deque(maxlen=10)
        self._last_message_at = 0.0
        self.vu: VuFrame | None = None
        self.vu_at: float | None = None
        self.rta: list[float] | None = None
        self.rta_at: float | None = None
        self.lists: dict[str, list[str]] = {}
        self._list_waiters: dict[str, list[asyncio.Future[list[str]]]] = {}

    # --- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if self._runner and not self._runner.done():
            return
        self._stop = False
        self._runner = asyncio.create_task(self._run(), name="mixer-connection")

    async def stop(self) -> None:
        self._stop = True
        self.cancel_all_ramps("server stop")
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._runner:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._runner
        self.status = "closed"

    async def reconnect(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        await self.start()

    async def wait_ready(self, timeout: float = 5.0) -> bool:
        try:
            await asyncio.wait_for(self.synced.wait(), timeout)
        except TimeoutError:
            return False
        return True

    @property
    def ready(self) -> bool:
        return self.status == "open" and self.synced.is_set()

    async def require_ready(self, timeout: float = 5.0) -> None:
        if not self.ready:
            await self.start()
            if not await self.wait_ready(timeout) or not self.ready:
                raise MixerNotReady(
                    f"ミキサー ({self.ip}) に接続できていません。status={self.status}"
                    + (f", error={self.last_error}" if self.last_error else "")
                )

    async def _run(self) -> None:
        while not self._stop:
            self.status = "connecting"
            self.synced.clear()
            try:
                async with websockets.connect(
                    f"ws://{self.ip}", open_timeout=5, ping_interval=None, max_size=None
                ) as ws:
                    self._ws = ws
                    self.status = "open"
                    self.last_error = None
                    log.info("connected to mixer %s", self.ip)
                    keepalive = asyncio.create_task(self._keepalive(ws))
                    syncer = asyncio.create_task(self._wait_for_sync())
                    try:
                        async for raw in ws:
                            if isinstance(raw, str):
                                self._handle_frame(raw)
                    finally:
                        keepalive.cancel()
                        syncer.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - connection errors of any kind
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("mixer connection error: %s", self.last_error)
            finally:
                self._ws = None
                self.synced.clear()
                self.cancel_all_ramps("connection lost")
            if self._stop:
                break
            self.status = "reconnecting"
            await asyncio.sleep(RECONNECT_SEC)
        self.status = "closed"

    async def _keepalive(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_SEC)
            await ws.send(frame("ALIVE"))

    async def _wait_for_sync(self) -> None:
        """The mixer dumps its full state on connect; consider it synced once that settles."""
        while True:
            await asyncio.sleep(0.05)
            quiet = time.monotonic() - self._last_message_at
            if "m.mix" in self.state and quiet >= SYNC_QUIET_SEC:
                self.synced.set()
                log.info("mixer state synced (%d keys)", len(self.state))
                return

    # --- inbound -------------------------------------------------------------

    def _handle_frame(self, raw: str) -> None:
        lines = split_frame(raw)
        if not lines:
            return
        for line in lines:
            if line.startswith("VU2^"):
                self._handle_meter(line)
                continue
            if line.startswith("RTA^"):
                self._handle_rta(line)
                continue
            if line.split("^", 1)[0] in RESOURCE_LISTS:
                self._handle_list(line)
                continue
            cmd = parse_line(line)
            if cmd is None:
                continue
            self._last_message_at = time.monotonic()
            self.state[cmd.path] = cmd.value
            if isinstance(cmd.value, float):
                self._check_external_change(cmd.path, cmd.value)

    def _handle_meter(self, line: str) -> None:
        try:
            self.vu = decode_vu2(line[4:])
            self.vu_at = time.monotonic()
        except (ValueError, IndexError) as exc:
            log.debug("bad VU2 frame: %s", exc)

    def _handle_rta(self, line: str) -> None:
        try:
            self.rta = decode_rta(line[4:])
            self.rta_at = time.monotonic()
        except ValueError as exc:
            log.debug("bad RTA frame: %s", exc)

    def _handle_list(self, line: str) -> None:
        parts = line.split("^")
        n = 2 if RESOURCE_LISTS[parts[0]] else 1
        address = "^".join(parts[:n])
        items = [p for p in parts[n:] if p]
        self.lists[address] = items
        for fut in self._list_waiters.pop(address, []):
            if not fut.done():
                fut.set_result(items)

    def _check_external_change(self, path: str, value: float) -> None:
        for ramp in list(self._ramps.values()):
            if path in ramp.paths:
                sent = ramp.sent.get(path)
                if sent and not any(abs(value - v) <= ECHO_TOLERANCE for v in sent):
                    log.info("external change on %s; cancelling ramp %s", path, ramp.key)
                    self._end_ramp(ramp, "外部 (Web UI / 実機) で操作されたため中止")
                    if time.monotonic() - ramp.last_sent_at < CROSSING_WINDOW_SEC:
                        asyncio.get_running_loop().create_task(self._reassert_external(ramp))

    async def _reassert_external(self, ramp: Ramp) -> None:
        """Make the mixer end on the person's value, even if one of our ramp writes crossed theirs."""
        await asyncio.sleep(REASSERT_DELAY_SEC)
        for p in ramp.paths:
            value = self.state.get(p)
            ours = ramp.sent.get(p, ())
            if isinstance(value, float) and not any(abs(value - v) <= ECHO_TOLERANCE for v in ours):
                with contextlib.suppress(MixerNotReady):
                    log.info("re-sending external value %s=%s after a crossing ramp write", p, value)
                    self.setd(p, value)

    # --- outbound ------------------------------------------------------------

    def get(self, path: str, default: Any = None) -> Any:
        return self.state.get(path, default)

    def get_float(self, path: str, default: float = 0.0) -> float:
        v = self.state.get(path, default)
        return float(v) if isinstance(v, (int, float)) else default

    def _send(self, message: str) -> None:
        ws = self._ws
        if ws is None or self.status != "open":
            raise MixerNotReady("ミキサーに接続されていません")
        asyncio.get_running_loop().create_task(ws.send(frame(message)))

    def setd(self, path: str, value: float) -> None:
        value = float(value)
        self._send(build_setd(path, value))
        self.state[path] = value

    def sets(self, path: str, value: str) -> None:
        self._send(build_sets(path, value))
        self.state[path] = value

    def send_command(self, message: str) -> None:
        """Send a raw non-state command such as ``MEDIA_PLAY`` or ``LOADSNAPSHOT^show^snap``."""
        self._send(message)

    async def request_list(self, request: str, address: str, timeout: float = LIST_TIMEOUT_SEC) -> list[str]:
        """Send a list request and wait for the reply addressed ``address`` (e.g. ``SNAPSHOTLIST^Default``).

        The real mixer does not answer at all when some lists are empty (e.g. no cues), so a timeout
        means "empty".
        """
        fut: asyncio.Future[list[str]] = asyncio.get_running_loop().create_future()
        self._list_waiters.setdefault(address, []).append(fut)
        self._send(request)
        try:
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            self.lists.setdefault(address, [])
            return []
        finally:
            waiters = self._list_waiters.get(address, [])
            if fut in waiters:
                waiters.remove(fut)

    async def playlists(self) -> dict[str, list[str]]:
        names = await self.request_list("MEDIA_GET_PLISTS", "PLISTS")
        tracks = await asyncio.gather(
            *(self.request_list(f"MEDIA_GET_PLIST_TRACKS^{n}", f"PLIST_TRACKS^{n}") for n in names)
        )
        return dict(zip(names, tracks, strict=True))

    async def shows(self) -> dict[str, dict[str, list[str]]]:
        names = await self.request_list("SHOWLIST", "SHOWLIST")
        result: dict[str, dict[str, list[str]]] = {}
        for name in names:
            snaps, cues = await asyncio.gather(
                self.request_list(f"SNAPSHOTLIST^{name}", f"SNAPSHOTLIST^{name}"),
                self.request_list(f"CUELIST^{name}", f"CUELIST^{name}"),
            )
            result[name] = {"snapshots": snaps, "cues": cues}
        return result

    # --- ramps ---------------------------------------------------------------

    def cancel_ramps_for(self, paths: list[str], reason: str = "新しい操作で置き換え") -> None:
        targets = set(paths)
        for ramp in list(self._ramps.values()):
            if targets.intersection(ramp.paths):
                self._end_ramp(ramp, reason)

    def cancel_all_ramps(self, reason: str = "停止指示") -> list[dict[str, Any]]:
        stopped = []
        for ramp in list(self._ramps.values()):
            self._end_ramp(ramp, reason)
            stopped.append(ramp.info())
        return stopped

    def _end_ramp(self, ramp: Ramp, reason: str) -> None:
        if self._ramps.get(ramp.key) is ramp:
            del self._ramps[ramp.key]
        ramp.end_reason = reason
        if ramp.task and not ramp.task.done() and ramp.task is not asyncio.current_task():
            ramp.task.cancel()
        self._finished.append(ramp.info())

    def active_ramps(self) -> list[dict[str, Any]]:
        return [r.info() for r in self._ramps.values()]

    def finished_ramps(self) -> list[dict[str, Any]]:
        return list(self._finished)

    def start_ramp(
        self,
        *,
        key: str,
        label: str,
        paths: list[str],
        start: float,
        target: float,
        unit: str,
        rate_fn: Callable[[float], float],
        to_value: Callable[[float], float],
    ) -> Ramp:
        self.cancel_ramps_for(paths)
        ramp = Ramp(
            key=key,
            label=label,
            paths=list(paths),
            current=start,
            target=target,
            unit=unit,
            rate_fn=rate_fn,
            to_value=to_value,
            sent={p: deque(maxlen=30) for p in paths},
        )
        ramp.estimated_sec = estimate_ramp_seconds(start, target, rate_fn, self.tick_sec)
        self._ramps[key] = ramp
        ramp.task = asyncio.get_running_loop().create_task(self._run_ramp(ramp))
        return ramp

    def _ramp_send(self, ramp: Ramp, value_display: float) -> None:
        value = ramp.to_value(value_display)
        ramp.last_sent_at = time.monotonic()
        for p in ramp.paths:
            ramp.sent[p].append(value)
            self.setd(p, value)

    async def _run_ramp(self, ramp: Ramp) -> None:
        try:
            self._ramp_send(ramp, ramp.current)
            while abs(ramp.target - ramp.current) > 1e-9:
                await asyncio.sleep(self.tick_sec)
                ramp.current = _advance(ramp.current, ramp.target, ramp.rate_fn(ramp.current) * self.tick_sec)
                self._ramp_send(ramp, ramp.current)
            self._end_ramp(ramp, "完了")
        except asyncio.CancelledError:
            pass
        except MixerNotReady:
            self._end_ramp(ramp, "接続断のため中止")
