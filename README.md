# soundcraft-ui-mcp

An MCP server that lets MCP clients such as Claude Code and Claude Desktop control a Soundcraft Ui12 digital mixer. It is likely to work with the Ui16 and Ui24R as well, but has only been tested on a Ui12.
It is built for use during live DJ events, so any operation that raises a level always fades in slowly.

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
uv run soundcraft-ui-mcp --transport http                     # http://127.0.0.1:8000/mcp
MCP_AUTH_TOKEN=xxxx uv run soundcraft-ui-mcp --transport http --host 0.0.0.0
```

- To listen on any address other than localhost, you must set `MCP_AUTH_TOKEN`. Clients then connect with an `Authorization: Bearer xxxx` header.
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

- Channel numbers start at 1. An operation on a stereo-linked channel also applies to its paired channel.
- Compressor and gate settings use the mixer's internal values (0 to 1), because the formula for converting them to dB is not published.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MIXER_IP` | `192.168.12.22` | Mixer IP address |
| `MCP_HTTP_HOST` / `MCP_HTTP_PORT` | `127.0.0.1` / `8000` | HTTP listen address and port |
| `MCP_AUTH_TOKEN` | (none) | Bearer token for HTTP |
| `MAX_FADER_DB` | `0` | Fader limit |
| `MAX_AUX_SEND_DB` | `0` | AUX send limit |
| `MAX_EQ_BOOST_DB` | `6` | EQ boost limit |
| `RAISE_RATE_DB_PER_SEC` | `1` | Fade-up speed (dB/s) |
| `RAMP_FLOOR_DB` | `-50` | Level a fade up from -inf starts at |
| `QUIET_ZONE_DB` / `QUIET_ZONE_MULTIPLIER` | `-30` / `3` | Below this level, fade up faster by this factor |
| `DYN_RAISE_RATE_PER_SEC` | `0.01` | Fade-up speed for compressor gain and outgain (internal units per second) |

## Mock mixer

For development without hardware, `soundcraft-ui-mock` runs a WebSocket server that speaks the mixer protocol. When a client connects, it sends the full state. It applies incoming commands to its state, broadcasts them to the other connected clients, and logs every change.

```bash
uv run soundcraft-ui-mock                                   # ws://127.0.0.1:8080, synthetic Ui12 state
MIXER_IP=127.0.0.1:8080 uv run soundcraft-ui-mcp            # point the MCP server at the mock
```

| Option | Description |
|---|---|
| `--port` / `--host` | Listen address (default `127.0.0.1:8080`) |
| `--model ui12\|ui16\|ui24` | Channel layout of the synthetic state |
| `--state FILE` | Start from a saved JSON state |
| `--save-on-exit FILE` | Write the final state to JSON on exit |
| `--echo` | Also echo commands back to the client that sent them |

To snapshot a real mixer's state (read-only) and replay it in the mock:

```bash
uv run soundcraft-ui-mock dump --from 192.168.12.22 -o state.json
uv run soundcraft-ui-mock --state state.json
```

In tests, `MockMixer.set_external(path, value)` simulates someone changing a value on the mixer's own web UI.

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
npx @modelcontextprotocol/inspector uv run soundcraft-ui-mcp
```
