"""SIP transports: bytes in, bytes out. One datagram (UDP) or one framed message (TLS) per receive."""

from __future__ import annotations

import asyncio
import re
import ssl
from typing import Protocol

from aiogotoconnect.exceptions import GoToSipError

_CONTENT_LENGTH = re.compile(rb"^(?:content-length|l)[ \t]*:[ \t]*(\d+)", re.I | re.M)


class SipTransport(Protocol):
    async def connect(self) -> None: ...
    async def send(self, data: bytes) -> None: ...
    async def receive(self, timeout: float) -> bytes: ...
    async def close(self) -> None: ...

    @property
    def local_addr(self) -> tuple[str, int]: ...


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()

    def datagram_received(self, data: bytes, addr) -> None:
        self.queue.put_nowait(data)


class UdpTransport:
    def __init__(self, host: str, port: int = 5060):
        self.host, self.port = host, port
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _UdpProtocol | None = None

    async def connect(self) -> None:
        self._transport, self._protocol = await asyncio.get_running_loop().create_datagram_endpoint(
            _UdpProtocol, remote_addr=(self.host, self.port)
        )

    async def send(self, data: bytes) -> None:
        if self._transport is None:
            raise GoToSipError("UDP transport not connected")
        self._transport.sendto(data)

    async def receive(self, timeout: float) -> bytes:
        if self._protocol is None:
            raise GoToSipError("UDP transport not connected")
        return await asyncio.wait_for(self._protocol.queue.get(), timeout)

    async def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    @property
    def local_addr(self) -> tuple[str, int]:
        if self._transport is None:
            raise GoToSipError("UDP transport not connected")
        return self._transport.get_extra_info("sockname")[:2]


class TlsTransport:
    def __init__(self, host: str, port: int = 5061, ssl_context: ssl.SSLContext | None = None):
        self.host, self.port = host, port
        self._ssl = ssl_context or ssl.create_default_context()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        # Bytes read but not yet returned; kept across calls so a receive timeout
        # mid-message never loses data.
        self._buf = b""

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(
            self.host, self.port, ssl=self._ssl, server_hostname=self.host
        )

    async def send(self, data: bytes) -> None:
        if self._writer is None:
            raise GoToSipError("TLS transport not connected")
        self._writer.write(data)
        await self._writer.drain()

    def _pop_message(self) -> bytes | None:
        """Return one complete message from the buffer, or None if more bytes are needed."""
        end = self._buf.find(b"\r\n\r\n")
        if end < 0:
            return None
        head_end = end + 4
        m = _CONTENT_LENGTH.search(self._buf[:head_end])
        total = head_end + (int(m.group(1)) if m else 0)
        if len(self._buf) < total:
            return None
        msg, self._buf = self._buf[:total], self._buf[total:]
        return msg

    async def _read_message(self) -> bytes:
        while (msg := self._pop_message()) is None:
            chunk = await self._reader.read(4096)
            if not chunk:
                raise GoToSipError("TLS connection closed by peer")
            self._buf += chunk
        return msg

    async def receive(self, timeout: float) -> bytes:
        if self._reader is None:
            raise GoToSipError("TLS transport not connected")
        return await asyncio.wait_for(self._read_message(), timeout)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (ssl.SSLError, OSError):
                pass
            self._writer = self._reader = None

    @property
    def local_addr(self) -> tuple[str, int]:
        if self._writer is None:
            raise GoToSipError("TLS transport not connected")
        return self._writer.get_extra_info("sockname")[:2]


def make_transport(host: str, mode: str, port: int | None = None,
                   ssl_context: ssl.SSLContext | None = None) -> SipTransport:
    if mode == "udp":
        return UdpTransport(host, port or 5060)
    if mode == "tls":
        return TlsTransport(host, port or 5061, ssl_context)
    raise ValueError(f"unknown SIP transport mode: {mode!r}")
