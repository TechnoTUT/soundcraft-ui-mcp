"""Text protocol of the Soundcraft Ui WebSocket.

Outbound frames are prefixed with ``3:::``. Inbound frames with that prefix carry one or more
newline-separated commands such as ``SETD^i.0.mix^0.5`` (numeric) or ``SETS^i.0.name^Vox``.
"""

from __future__ import annotations

from dataclasses import dataclass

PREFIX = "3:::"


@dataclass(frozen=True)
class SetCommand:
    path: str
    value: float | str


def frame(message: str) -> str:
    return PREFIX + message


def build_setd(path: str, value: float) -> str:
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, float) and value.is_integer():
        text = str(int(value))
    else:
        text = repr(value) if isinstance(value, float) else str(value)
    return f"SETD^{path}^{text}"


def build_sets(path: str, value: str) -> str:
    return f"SETS^{path}^{value}"


def split_frame(raw: str) -> list[str]:
    """Return the command lines of an inbound frame (empty if not a ``3:::`` frame)."""
    if not raw.startswith(PREFIX):
        return []
    return [line for line in raw[len(PREFIX) :].split("\n") if line]


def parse_line(line: str) -> SetCommand | None:
    parts = line.split("^")
    if len(parts) < 3 or parts[0] not in ("SETD", "SETS"):
        return None
    path = parts[1]
    raw_value = "^".join(parts[2:])
    if parts[0] == "SETS":
        return SetCommand(path, raw_value)
    try:
        return SetCommand(path, float(raw_value))
    except ValueError:
        return SetCommand(path, raw_value)
