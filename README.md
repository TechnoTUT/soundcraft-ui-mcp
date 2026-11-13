# soundcraft-ui-mcp

An MCP server that lets MCP clients such as Claude Code and Claude Desktop control a Soundcraft Ui12 digital mixer. It is likely to work with the Ui16 and Ui24R as well, but has only been tested on a Ui12.
It is built for use during live DJ events, so any operation that raises a level always fades in slowly.

## Motivation

- **Tune the venue by describing it.** Getting a room to sound right usually means someone who knows the mixer has to turn what people say ("the low end is boomy", "vocals get lost") into fader, EQ and dynamics moves. With this server, a person can describe what they hear and the AI adjusts the venue's sound for them.
- **Close the loop with real-time analysis.** The server reads the mixer's frequency spectrum in real time, so the AI can analyze the sound itself. It can run an analyze → adjust → listen cycle over and over, which makes automatic optimization of the venue's sound possible.

## Protocol

The server talks to the mixer over its WebSocket protocol (`SETD^path^value`). This is a Python reimplementation of the protocol as worked out by [fmalcher/soundcraft-ui](https://github.com/fmalcher/soundcraft-ui).

## Setup

```bash
uv sync
uv run scripts/smoke.py            # read-only connection check (default mixer: 192.168.12.22)
```

### Claude Code

```bash
claude mcp add soundcraft -- uv --directory /path/to/soundcraft-ui-mcp run soundcraft-ui-mcp
```

### Claude Desktop

```json
{
  "mcpServers": {
    "soundcraft": {
      "command": "uv",
      "args": ["--directory", "/path/to/soundcraft-ui-mcp", "run", "soundcraft-ui-mcp"],
      "env": { "MIXER_IP": "192.168.12.22" }
    }
  }
}
```

### Streamable HTTP

```bash
uv run soundcraft-ui-mcp --transport http                     # http://127.0.0.1:8000/mcp, no auth
MCP_AUTH_TOKEN=xxxx uv run soundcraft-ui-mcp --transport http --host 0.0.0.0
uv run soundcraft-ui-mcp --transport http --host 0.0.0.0 --no-auth
```

- On localhost, no authentication is needed.
- To listen on any other address, either set `MCP_AUTH_TOKEN` (clients then send an `Authorization: Bearer xxxx` header) or pass `--no-auth` (or set `MCP_HTTP_NO_AUTH=1`) to serve without authentication. With `--no-auth`, anyone on the same network can control the mixer, so the server logs a warning at startup. Setting both is refused.
- `GET /healthz` reports whether the server is connected to the mixer.

## Safety

| Operation | Behavior |
|---|---|
| Raising a fader or AUX send, unmuting, boosting an EQ band, raising compressor gain or outgain | **Fades up slowly** (1 dB/s by default). The tool returns without waiting for the fade to finish |
| Lowering, muting | Takes effect immediately |
| Target above the limit (0 dB for faders and AUX sends, +6 dB for EQ by default) | Refused unless the call includes `confirm=true` |

- Unmuting first drops the fader to -inf, then unmutes, then fades back up to the previous level.
- A fade up from -inf starts at `RAMP_FLOOR_DB` (-50 dB). Below -30 dB the level is practically inaudible, so that part of the fade runs 3x faster.
- If someone moves the same control on the web UI or the mixer during a fade, the fade stops and the person's change wins.
- `stop_fades` stops every running fade where it is.
- Fades also stop if the connection to the mixer drops. The server reconnects automatically.

Measured on a Ui12 with the default settings: -inf to -30 dB takes about 7 s, -inf to -20 dB about 17 s, and -inf to 0 dB about 37 s.

## Tools

| Group | Tools |
|---|---|
| Status | `mixer_status`, `mixer_reconnect`, `stop_fades`, `get_mixer_overview`, `get_channel` |
| Mixing | `set_fader`, `set_mute`, `set_solo`, `set_pan` |
| AUX | `get_aux_mix`, `set_aux_send`, `set_aux_send_mute`, `set_aux_send_pan`, `set_aux_send_tap` |
| EQ | `get_eq`, `set_eq_band`, `set_eq_bypass`, `set_hpf` |
| Dynamics | `get_dynamics`, `set_compressor`, `set_gate`, `set_dynamics_bypass` |
| Meters | `get_meters`, `get_spectrum` |
| Graphic EQ (AUX / master) | `get_graphic_eq`, `set_graphic_eq_band`, `reset_graphic_eq`, `set_graphic_eq_bypass` |

- Channel numbers start at 1. An operation on a stereo-linked channel also applies to its paired channel.
- Mute state takes mute groups, MUTE ALL and MUTE FX into account. To unmute a channel that a group is muting, the server sets `forceunmute`, the same way the mixer's web app does. Muting always clears `forceunmute`. `get_mixer_overview` reports `active_mute_groups`, and a `mute_reason` for each channel muted by a group.
- Compressor and gate settings are written as the mixer's internal values (0 to 1). `get_dynamics` also returns approximate dB values (`readable`), converted with the formulas from the mixer's web app.
- `get_meters` returns the latest meter frame in dB (-80 = silence): pre, post-processing and post-fader levels, plus compressor gain reduction. When there is no signal, the real mixer only sends meter data when a level changes, so `age_sec` can be large.
- The graphic EQ has 31 bands from 20 Hz to 20 kHz, ±15 dB. Cuts take effect immediately and boosts fade in slowly. `reset_graphic_eq` requires `confirm=true`, because it would wipe the venue's room tuning.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MIXER_IP` | `192.168.12.22` | Mixer IP address |
| `MCP_HTTP_HOST` / `MCP_HTTP_PORT` | `127.0.0.1` / `8000` | HTTP listen address and port |
| `MCP_AUTH_TOKEN` | (none) | Bearer token for HTTP |
| `MCP_HTTP_NO_AUTH` | (unset) | `1` serves HTTP without authentication on a non-localhost address (same as `--no-auth`) |
| `MAX_FADER_DB` | `0` | Fader limit |
| `MAX_AUX_SEND_DB` | `0` | AUX send limit |
| `MAX_EQ_BOOST_DB` | `6` | EQ boost limit |
| `RAISE_RATE_DB_PER_SEC` | `1` | Fade-up speed (dB/s) |
| `RAMP_FLOOR_DB` | `-50` | Level a fade up from -inf starts at |
| `QUIET_ZONE_DB` / `QUIET_ZONE_MULTIPLIER` | `-30` / `3` | Below this level, fade up faster by this factor |
| `DYN_RAISE_RATE_PER_SEC` | `0.01` | Fade-up speed for compressor gain and outgain (internal units per second) |

## Mock mixer

For development without hardware, use [soundcraft-ui-mock](https://github.com/TechnoTUT/soundcraft-ui-mock) (private). It is a WebSocket mock of the mixer with meters, shows/snapshots, the USB player and recorder. It is included as a git submodule at `mock/` and installed as a dev dependency:

```bash
git clone --recurse-submodules https://github.com/TechnoTUT/soundcraft-ui-mcp.git
# or, in an existing clone:
git submodule update --init

uv sync
uv run soundcraft-ui-mock                                   # ws://127.0.0.1:8080, synthetic Ui12 state
MIXER_IP=127.0.0.1:8080 uv run soundcraft-ui-mcp            # point the MCP server at the mock
```

See the mock's README for options (`--signal`, `--no-usb`, `--state`, `dump`, ...). `tests/test_mock_contract.py` checks that every state write and raw command this server may need is understood by the mock. To update the mock: `git -C mock pull`, then commit the new submodule revision.

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
npx @modelcontextprotocol/inspector uv run soundcraft-ui-mcp
```
