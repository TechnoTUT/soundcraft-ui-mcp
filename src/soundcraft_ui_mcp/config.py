"""Runtime configuration from environment variables."""

from __future__ import annotations

import os

from pydantic import BaseModel, Field


class Settings(BaseModel):
    mixer_ip: str = "192.168.12.22"
    http_host: str = "127.0.0.1"
    http_port: int = 8000
    auth_token: str | None = None
    # explicitly allow Streamable HTTP without authentication on a non-localhost address
    http_no_auth: bool = False

    # Upper limits; going above requires confirm=true
    max_fader_db: float = Field(0.0, le=10.0)
    max_aux_send_db: float = Field(0.0, le=10.0)
    max_eq_boost_db: float = Field(6.0, le=20.0)

    # Every level increase is ramped slowly
    raise_rate_db_per_sec: float = Field(1.0, gt=0)
    # Ramps from -inf start here
    ramp_floor_db: float = -50.0
    # Below this level the ramp may move faster (practically inaudible region)
    quiet_zone_db: float = -30.0
    quiet_zone_multiplier: float = Field(3.0, ge=1.0)
    # Ramp speed for normalized dynamics gains (units of 0..1 per second)
    dyn_raise_rate_per_sec: float = Field(0.01, gt=0)
    ramp_tick_sec: float = Field(0.1, gt=0)

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        mapping = {
            "mixer_ip": "MIXER_IP",
            "http_host": "MCP_HTTP_HOST",
            "http_port": "MCP_HTTP_PORT",
            "auth_token": "MCP_AUTH_TOKEN",
            "http_no_auth": "MCP_HTTP_NO_AUTH",
            "max_fader_db": "MAX_FADER_DB",
            "max_aux_send_db": "MAX_AUX_SEND_DB",
            "max_eq_boost_db": "MAX_EQ_BOOST_DB",
            "raise_rate_db_per_sec": "RAISE_RATE_DB_PER_SEC",
            "ramp_floor_db": "RAMP_FLOOR_DB",
            "quiet_zone_db": "QUIET_ZONE_DB",
            "quiet_zone_multiplier": "QUIET_ZONE_MULTIPLIER",
            "dyn_raise_rate_per_sec": "DYN_RAISE_RATE_PER_SEC",
        }
        values = {field: env[var] for field, var in mapping.items() if env.get(var)}
        return cls(**values)
