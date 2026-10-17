import asyncio
import ssl
import subprocess

import pytest

from gotoconnect.sip.transport import TlsTransport, UdpTransport, make_transport


class _Echo(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.transport.sendto(data, addr)


async def test_udp_round_trip_and_local_addr():
    loop = asyncio.get_running_loop()
    server, _ = await loop.create_datagram_endpoint(_Echo, local_addr=("127.0.0.1", 0))
    port = server.get_extra_info("sockname")[1]
    t = UdpTransport("127.0.0.1", port)
    await t.connect()
    try:
        assert t.local_addr[0] == "127.0.0.1" and t.local_addr[1] > 0
        await t.send(b"INVITE sip:x SIP/2.0\r\n\r\n")
        assert await t.receive(2) == b"INVITE sip:x SIP/2.0\r\n\r\n"
    finally:
        await t.close()
        server.close()


async def test_udp_receive_timeout():
    loop = asyncio.get_running_loop()
    server, _ = await loop.create_datagram_endpoint(
        asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0)
    )
    t = UdpTransport("127.0.0.1", server.get_extra_info("sockname")[1])
    await t.connect()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await t.receive(0.05)
    finally:
        await t.close()
        server.close()


@pytest.fixture
def tls_certs(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
         "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True,
    )
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)
    client_ctx = ssl.create_default_context(cafile=str(cert))
    return server_ctx, client_ctx


async def test_tls_splits_back_to_back_messages(tls_certs):
    server_ctx, client_ctx = tls_certs
    one = b"SIP/2.0 100 Trying\r\nContent-Length: 0\r\n\r\n"
    two = b"SIP/2.0 200 OK\r\nContent-Length: 5\r\n\r\nhello"
    three = b"SIP/2.0 180 Ringing\r\nl: 2\r\n\r\nhi"

    async def handle(reader, writer):
        # All three in one write: framing must come from Content-Length.
        writer.write(one + two + three)
        await writer.drain()
        await reader.read()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_ctx)
    port = server.sockets[0].getsockname()[1]
    t = TlsTransport("localhost", port, ssl_context=client_ctx)
    await t.connect()
    try:
        assert t.local_addr[1] > 0
        assert await t.receive(2) == one
        assert await t.receive(2) == two
        assert await t.receive(2) == three
        with pytest.raises(asyncio.TimeoutError):
            await t.receive(0.05)
    finally:
        await t.close()
        server.close()
        await server.wait_closed()


def test_make_transport_defaults():
    u = make_transport("h", "udp")
    t = make_transport("h", "tls")
    assert isinstance(u, UdpTransport) and u.port == 5060
    assert isinstance(t, TlsTransport) and t.port == 5061
    assert make_transport("h", "udp", 5099).port == 5099
    with pytest.raises(ValueError):
        make_transport("h", "tcp")


@pytest.mark.parametrize("split_at", [10, 45], ids=["mid-header", "mid-body"])
async def test_tls_timeout_mid_message_preserves_partial_data(tls_certs, split_at):
    server_ctx, client_ctx = tls_certs
    msg = b"SIP/2.0 200 OK\r\nContent-Length: 11\r\n\r\nhello world"
    assert 0 < split_at < len(msg)
    release = asyncio.Event()

    async def handle(reader, writer):
        writer.write(msg[:split_at])
        await writer.drain()
        await release.wait()
        writer.write(msg[split_at:])
        await writer.drain()
        await reader.read()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_ctx)
    t = TlsTransport("localhost", server.sockets[0].getsockname()[1], ssl_context=client_ctx)
    await t.connect()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await t.receive(0.1)  # only a fragment has arrived
        release.set()
        assert await t.receive(2) == msg
    finally:
        await t.close()
        server.close()
        await server.wait_closed()
