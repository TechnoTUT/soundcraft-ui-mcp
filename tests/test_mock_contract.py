"""Contract tests: everything the MCP server may send to a Ui12 is understood by the mock.

State writes are taken from soundcraft-ui's facade (paths and value ranges); commands are the raw
strings the library sends. A second client checks that the mock broadcasts like the real mixer.
"""

import asyncio

import pytest
import websockets

from soundcraft_ui_mcp.mixer import MixerClient
from soundcraft_ui_mcp.mock import MockMixer, default_state

# (feature, path, value) - every path must already exist in a Ui12 state
STATE_WRITES = [
    ("mute group 2 on", "mgmask", 2.0),
    ("MUTE ALL", "mgmask", float(1 << 23)),
    ("channel in mute groups 1+3", "i.0.mgmask", 5.0),
    ("force unmute", "i.0.forceunmute", 1.0),
    ("FX type", "f.1.fxtype", 3.0),
    ("FX param", "f.0.par3", 0.25),
    ("FX BPM", "f.1.bpm", 128.0),
    ("FX bypass", "f.0.bypass", 1.0),
    ("FX send", "i.0.fx.2.value", 0.5),
    ("FX send mute", "l.1.fx.0.mute", 1.0),
    ("input gain", "i.3.gain", 0.6),
    ("phantom power", "i.3.phantom", 1.0),
    ("Hi-Z", "i.0.hiz", 1.0),
    ("polarity", "i.1.invert", 1.0),
    ("line gain", "l.0.gain", 0.5),
    ("de-esser", "i.2.deesser.threshold", 0.6),
    ("DigiTech amp", "i.0.digitech.enabled", 1.0),
    ("subgroup assign", "i.4.subgroup", 1.0),
    ("stereo link", "i.2.stereoIndex", 0.0),
    ("channel safe", "i.5.safe", 1.0),
    ("solo mode", "settings.multiplesolo", 1.0),
    ("solo type", "settings.solotype", 0.0),
    ("player mode", "settings.playMode", 3.0),
    ("shuffle", "settings.shuffle", 1.0),
    ("record mode", "settings.recordMode", 1.0),
    ("access lock", "settings.block.mixlvl", 1.0),
    ("RTA source", "var.rta", "i.0"),
    ("channel name", "i.0.name", "DJ ミキサー L"),
    ("AUX name", "a.2.name", "Monitor"),
    ("AFS", "a.0.afs.enabled", 1.0),
    ("master graphic EQ", "m.eq.peak.l.17", 0.4),
    ("master gate", "m.gate.enabled", 1.0),
    ("master comp", "m.dyn.l.threshold", 0.8),
]

# raw commands soundcraft-ui sends (Ui12 relevant)
COMMANDS = [
    "MEDIA_PLAY", "MEDIA_PAUSE", "MEDIA_STOP", "MEDIA_NEXT", "MEDIA_PREV",
    "MEDIA_SWITCH_PLIST^BGM", "MEDIA_SWITCH_TRACK^BGM^Lounge A.mp3",
    "MEDIA_GET_PLISTS", "MEDIA_GET_PLIST_TRACKS^BGM",
    "RECTOGGLE",
    "SHOWLIST", "SNAPSHOTLIST^Default", "CUELIST^Default",
    "LOADSHOW^Default", "LOADSNAPSHOT^Default^* Init *", "LOADCUE^Default^x",
    "SAVESNAPSHOT^Default^new", "SAVECUE^Default^new",
]  # fmt: skip


@pytest.fixture
async def clients():
    m = MockMixer(vu_rate=0, rta_rate=0)
    async with websockets.serve(m.handler, "127.0.0.1", 0) as srv:
        addr = f"127.0.0.1:{srv.sockets[0].getsockname()[1]}"
        a, b = MixerClient(addr), MixerClient(addr)
        for c in (a, b):
            await c.start()
            assert await c.wait_ready(3)
        yield m, a, b
        await a.stop()
        await b.stop()


@pytest.mark.parametrize(("feature", "path", "value"), STATE_WRITES, ids=[w[0] for w in STATE_WRITES])
def test_paths_exist_in_ui12_state(feature, path, value):
    assert path in default_state("ui12"), f"{feature}: {path} is not part of the Ui12 state"


async def test_state_writes_reach_other_clients(clients):
    m, a, b = clients
    for _feature, path, value in STATE_WRITES:
        (a.sets if isinstance(value, str) else a.setd)(path, value)
    await asyncio.sleep(0.2)
    for feature, path, value in STATE_WRITES:
        if path == "mgmask":
            continue  # written twice above; checked separately
        assert m.state[path] == value, feature
        assert b.get(path) == value, feature
    assert b.get("mgmask") == float(1 << 23)


@pytest.mark.parametrize("command", COMMANDS)
def test_every_library_command_has_a_handler(command):
    name = command.split("^", 1)[0]
    assert hasattr(MockMixer, f"_cmd_{name.lower()}"), f"mock does not handle {name}"


async def test_commands_do_not_break_the_connection(clients):
    m, a, b = clients
    for command in COMMANDS:
        a.send_command(command)
    await asyncio.sleep(0.3)
    assert a.ready and b.ready
    assert {line.split("^", 1)[0] for line in m.received} >= {c.split("^", 1)[0] for c in COMMANDS}
