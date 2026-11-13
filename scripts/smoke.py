"""Read-only smoke test against a real mixer: connect, sync and print an overview.

Usage: uv run scripts/smoke.py [MIXER_IP]
Nothing is written to the mixer.
"""

import asyncio
import json
import sys

from soundcraft_ui_mcp.config import Settings
from soundcraft_ui_mcp.controller import MixerController
from soundcraft_ui_mcp.mixer import MixerClient


class ReadOnlyMixer(MixerClient):
    def _send(self, message: str) -> None:
        if message.startswith(("SETD", "SETS")):
            raise RuntimeError(f"smoke test must not write: {message}")
        super()._send(message)


async def main() -> int:
    settings = Settings.from_env()
    ip = sys.argv[1] if len(sys.argv) > 1 else settings.mixer_ip
    mixer = ReadOnlyMixer(ip)
    await mixer.start()
    ok = await mixer.wait_ready(8)
    ctl = MixerController(mixer, settings)
    status = ctl.status()
    print(
        json.dumps(
            {k: status[k] for k in ("mixer_ip", "connection", "synced", "model", "firmware", "last_error")},
            ensure_ascii=False,
            indent=2,
        )
    )
    if ok:
        ov = ctl.overview()
        print("master:", ov["master"])
        for t in ("input", "line", "player", "fx", "sub", "aux"):
            for c in ov[t]:
                print(
                    f"  {t:6} {c['number']:>2} {c['name']:<12} {c['fader_db']!s:>6} dB"
                    f"{'  MUTE' if c['mute'] else ''}{'  SOLO' if c['solo'] else ''}"
                    f"{'  link→' + str(c['stereo_linked_with']) if 'stereo_linked_with' in c else ''}"
                )
        print("AUX1:", json.dumps(ctl.aux_mix(1)["aux_master"], ensure_ascii=False))
        print("input1 EQ:", json.dumps(ctl.get_eq(ctl.channel("input", 1)), ensure_ascii=False))
        print("state keys:", len(mixer.state))
    await mixer.stop()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
