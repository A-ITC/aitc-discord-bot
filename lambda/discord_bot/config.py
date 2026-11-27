"""Validate non-secret configuration at cold start."""

import os
import re
from dataclasses import dataclass
from collections.abc import Mapping

SNOWFLAKE = re.compile(r"[0-9]{17,20}\Z")


@dataclass(frozen=True)
class Config:
    application_id: str
    public_key: bytes
    guild_ids: frozenset[str]

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        application_id = env.get("DISCORD_APPLICATION_ID", "")
        public_key = env.get("DISCORD_PUBLIC_KEY", "")
        guild_ids = env.get("ALLOWED_GUILD_IDS", "").split(",")
        if not SNOWFLAKE.fullmatch(application_id):
            raise ValueError("Invalid DISCORD_APPLICATION_ID")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", public_key):
            raise ValueError("Invalid DISCORD_PUBLIC_KEY")
        guild_ids = [item.strip() for item in guild_ids]
        if not all(SNOWFLAKE.fullmatch(item) for item in guild_ids):
            raise ValueError("Invalid ALLOWED_GUILD_IDS")
        return cls(application_id, bytes.fromhex(public_key), frozenset(guild_ids))
