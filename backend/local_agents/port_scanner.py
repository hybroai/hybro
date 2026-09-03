from __future__ import annotations

import asyncio
import contextlib
import socket

_SCAN_CONCURRENCY = 128
_NO_EXCLUDED_PORTS: frozenset[int] = frozenset()


class HostPortScanner:
    def __init__(
        self,
        *,
        host: str,
        port_start: int,
        port_end: int,
        connect_timeout_seconds: float,
        excluded_ports: frozenset[int] = _NO_EXCLUDED_PORTS,
    ) -> None:
        self._host = host
        self._port_start = port_start
        self._port_end = port_end
        self._connect_timeout = connect_timeout_seconds
        self._excluded_ports = excluded_ports

    async def scan_open_ports(self) -> list[int]:
        target_host = await self._resolve_host()
        queue: asyncio.Queue[int] = asyncio.Queue()
        for port in range(self._port_start, self._port_end + 1):
            if port not in self._excluded_ports:
                queue.put_nowait(port)

        open_ports: list[int] = []

        async def worker() -> None:
            while True:
                try:
                    port = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    if await self._is_open(target_host, port):
                        open_ports.append(port)
                finally:
                    queue.task_done()

        worker_count = min(_SCAN_CONCURRENCY, queue.qsize())
        workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
        await asyncio.gather(*workers)
        return sorted(open_ports)

    async def _resolve_host(self) -> str:
        loop = asyncio.get_running_loop()
        addresses = await loop.getaddrinfo(
            self._host,
            None,
            type=socket.SOCK_STREAM,
        )
        if not addresses:
            raise OSError(f"Could not resolve local agent discovery host: {self._host}")
        return addresses[0][4][0]

    async def _is_open(self, host: str, port: int) -> bool:
        writer: asyncio.StreamWriter | None = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=self._connect_timeout,
            )
            return True
        except (TimeoutError, OSError):
            return False
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()


__all__ = ["HostPortScanner"]
