import asyncio
import os
import socket
from urllib.parse import urlparse

import asyncpg

_pool: asyncpg.Pool | None = None


async def _resolve_ipv4(hostname: str, port: int) -> str | None:
    """Return the first IPv4 address for hostname, or None if unavailable."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(
            hostname, port,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        )
        return infos[0][4][0] if infos else None
    except OSError:
        return None


async def init_pool() -> None:
    global _pool
    dsn = os.environ["DATABASE_URL"]
    parsed = urlparse(dsn)
    port = parsed.port or 5432

    # Force IPv4 to avoid routing failures in Docker on Windows/WSL2 where
    # IPv6 addresses are returned by DNS but have no usable route.
    ipv4 = await _resolve_ipv4(parsed.hostname, port)

    _pool = await asyncpg.create_pool(
        dsn=dsn,
        host=ipv4 if ipv4 else parsed.hostname,
        port=port,
        ssl="require",  # encrypt without hostname-vs-IP cert mismatch
        min_size=2,
        max_size=10,
    )


async def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialised — call init_pool() first")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None