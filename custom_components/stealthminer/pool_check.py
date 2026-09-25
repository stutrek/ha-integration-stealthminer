"""Check whether a mining pool can be reached, without involving the miner."""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

DEFAULT_STRATUM_PORT = 3333


def pool_address(url: str) -> tuple[str, int] | None:
    """Return (host, port) from a pool URL like stratum+tcp://pool.example:3333."""
    url = url.strip()
    if not url:
        return None
    if "://" not in url:
        url = f"stratum+tcp://{url}"
    try:
        parts = urlsplit(url)
        port = parts.port or DEFAULT_STRATUM_PORT
    except ValueError:
        return None
    if not parts.hostname:
        return None
    return parts.hostname, port


async def pool_reachable(addresses: list[tuple[str, int]], timeout: float = 5.0) -> bool:
    """True if a TCP connection to any of the pools is accepted.

    Only opens and closes a connection; nothing is sent.
    """
    for host, port in addresses:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except (OSError, asyncio.TimeoutError):
            continue
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True
    return False
