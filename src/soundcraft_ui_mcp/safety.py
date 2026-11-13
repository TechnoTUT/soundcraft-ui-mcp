"""Live-safety rules (pure functions)."""

from __future__ import annotations

from collections.abc import Callable

from .config import Settings


class SafetyError(ValueError):
    """Raised when an operation is refused by a safety guard."""


def check_upper_limit(target: float, limit: float, confirm: bool, what: str, unit: str = "dB") -> None:
    if target > limit and not confirm:
        raise SafetyError(
            f"{what} の目標 {target:+.1f}{unit} は安全上限 {limit:+.1f}{unit} を超えています。"
            "本当に必要なら confirm=true を付けて再実行してください (その場合もゆっくり上げます)。"
        )


def db_rate_fn(settings: Settings, cap: float | None = None) -> Callable[[float], float]:
    """Ramp speed in dB/s as a function of the current level.

    Below ``quiet_zone_db`` the level is practically inaudible, so the ramp may move faster there.
    ``cap`` (from a requested fade time) can only make the ramp slower, never faster.
    """

    def rate(current_db: float) -> float:
        r = settings.raise_rate_db_per_sec
        if current_db < settings.quiet_zone_db:
            r *= settings.quiet_zone_multiplier
        return min(r, cap) if cap else r

    return rate


def constant_rate_fn(rate: float) -> Callable[[float], float]:
    return lambda _current: rate
