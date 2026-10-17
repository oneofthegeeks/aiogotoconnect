"""Paced RTP sender for G.711 payloads (160-byte frames, 20 ms apart)."""

import asyncio
import random
import socket
import struct

FRAME_BYTES = 160
FRAME_SECONDS = 0.02


class RtpSender:
    def __init__(self, local_port: int = 0) -> None:
        # Bound synchronously so local_port is known before the INVITE/SDP is built.
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self._sock.bind(("0.0.0.0", local_port))

    @property
    def local_port(self) -> int:
        return self._sock.getsockname()[1]

    async def stream(
        self,
        payload: bytes,
        dest: tuple[str, int],
        payload_type: int = 8,
        stop: asyncio.Event | None = None,
    ) -> None:
        loop = asyncio.get_running_loop()
        seq = random.randrange(0x10000)
        timestamp = random.randrange(0x100000000)
        ssrc = random.randrange(0x100000000)
        start = loop.time()
        # A trailing partial frame (< 160 bytes) is dropped, not padded: the
        # right silence byte depends on the codec, and it is under 20 ms of audio.
        for n in range(len(payload) // FRAME_BYTES):
            if stop is not None and stop.is_set():
                return
            # Marker bit on the first packet only (talkspurt start).
            pt_byte = payload_type | (0x80 if n == 0 else 0)
            header = struct.pack("!BBHII", 0x80, pt_byte, seq, timestamp, ssrc)
            frame = payload[n * FRAME_BYTES : (n + 1) * FRAME_BYTES]
            await loop.sock_sendto(self._sock, header + frame, dest)
            seq = (seq + 1) & 0xFFFF
            timestamp = (timestamp + FRAME_BYTES) & 0xFFFFFFFF
            # Drift-free pacing: sleep to an absolute target, not a fixed interval.
            await asyncio.sleep(max(0, start + (n + 1) * FRAME_SECONDS - loop.time()))

    async def close(self) -> None:
        self._sock.close()
