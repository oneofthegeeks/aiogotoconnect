import asyncio
import socket
import struct
import time

from aiogotoconnect.sip.rtp import RtpSender


def _listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    return sock


async def _collect(sock, packets, stop_when):
    loop = asyncio.get_running_loop()
    while not stop_when.is_set():
        try:
            data = await asyncio.wait_for(loop.sock_recv(sock, 2048), 0.5)
        except asyncio.TimeoutError:
            continue
        packets.append(data)


async def test_streams_paced_rtp_packets():
    sock = _listener()
    sender = RtpSender()
    assert sender.local_port > 0
    packets, done = [], asyncio.Event()
    collector = asyncio.create_task(_collect(sock, packets, done))
    try:
        start = time.monotonic()
        await sender.stream(b"\xd5" * 8000, ("127.0.0.1", sock.getsockname()[1]))
        elapsed = time.monotonic() - start
        await asyncio.sleep(0.1)
    finally:
        done.set()
        await collector
        await sender.close()
        sock.close()

    assert 0.9 <= elapsed <= 1.5
    assert len(packets) == 50
    parsed = [struct.unpack("!BBHII", p[:12]) for p in packets]
    assert all(p[0] == 0x80 and len(packets[0]) == 172 for p in parsed)
    assert [p[1] for p in parsed] == [0x88] + [8] * 49  # marker on first only
    for prev, cur in zip(parsed, parsed[1:]):
        assert cur[2] == (prev[2] + 1) & 0xFFFF
        assert cur[3] == (prev[3] + 160) & 0xFFFFFFFF
        assert cur[4] == prev[4]


async def test_stop_halts_stream_promptly():
    sock = _listener()
    sender = RtpSender()
    stop = asyncio.Event()
    task = asyncio.create_task(
        sender.stream(b"\xd5" * 160 * 500, ("127.0.0.1", sock.getsockname()[1]), stop=stop)
    )
    try:
        await asyncio.sleep(0.2)
        stop.set()
        t0 = time.monotonic()
        await asyncio.wait_for(task, 1)
        assert time.monotonic() - t0 < 0.1
    finally:
        await sender.close()
        sock.close()
