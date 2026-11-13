"""End-to-end through the MCP protocol with an in-memory client."""

import json

from mcp.client import Client

from soundcraft_ui_mcp.config import Settings
from soundcraft_ui_mcp.server import build_server

from .conftest import FakeMixer


def payload(result):
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


async def test_tools_via_mcp():
    mixer = FakeMixer()
    server = build_server(Settings(raise_rate_db_per_sec=100, ramp_tick_sec=0.01), mixer=mixer)
    mixer.start = _noop  # type: ignore[method-assign]
    async with Client(server) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        assert {"get_mixer_overview", "set_fader", "set_aux_send", "set_eq_band", "set_compressor"} <= tools

        ov = payload(await client.call_tool("get_mixer_overview", {}))
        assert ov["model"] == "ui12"

        res = await client.call_tool("set_fader", {"channel_type": "input", "channel": 1, "db": -20})
        assert not res.is_error
        assert payload(res)["after_db"] == -20

        res = await client.call_tool("set_fader", {"channel_type": "master", "db": 6})
        assert res.is_error
        assert "confirm" in res.content[0].text

        res = await client.call_tool("set_fader", {"channel_type": "input", "channel": 12, "db": -20})
        assert res.is_error


async def _noop():
    return None
