"""Runtime configuration with environment-variable defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = "AF Agent Framework"
    database_url: str = "sqlite+pysqlite:///storage/af.sqlite3"
    host: str = "127.0.0.1"
    port: int = 8000
    mcp_transport: str = "stdio"
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    allow_remote_inference: bool = False
    selector_confidence_threshold: float = 0.8

    @classmethod
    def from_environment(cls) -> Settings:
        """Read AF-prefixed settings without coupling the core to a settings library."""
        defaults = cls()
        return cls(
            app_name=os.getenv("AF_APP_NAME", defaults.app_name),
            database_url=os.getenv("AF_DATABASE_URL", defaults.database_url),
            host=os.getenv("AF_HOST", defaults.host),
            port=int(os.getenv("AF_PORT", str(defaults.port))),
            mcp_transport=os.getenv("AF_MCP_TRANSPORT", defaults.mcp_transport),
            allowed_hosts=_split_csv_env("AF_ALLOWED_HOSTS"),
            allowed_origins=_split_csv_env("AF_ALLOWED_ORIGINS"),
            allow_remote_inference=os.getenv(
                "AF_ALLOW_REMOTE_INFERENCE",
                str(defaults.allow_remote_inference),
            ).lower()
            in {"1", "true", "yes", "on"},
            selector_confidence_threshold=float(
                os.getenv(
                    "AF_SELECTOR_CONFIDENCE_THRESHOLD",
                    str(defaults.selector_confidence_threshold),
                )
            ),
        )


def _split_csv_env(name: str) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())
