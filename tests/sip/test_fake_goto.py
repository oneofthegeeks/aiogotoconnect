"""Self-check for the fake GoTo server, driven by a raw-socket client.

Deliberately independent of gotoconnect.sip: the client here builds SIP text by hand
and computes the digest with hashlib.
"""
import asyncio
import hashlib
import re

import pytest

from fake_goto import FakeGoToServer

USER, PASSWORD = "alice", "s3cret"


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


class RawClient(asyncio.DatagramProtocol):
    def __init__(self):
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.cseq = 0

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.queue.put_nowait(data.decode())

    async def recv(self, timeout=2.0) -> str:
        return await asyncio.wait_for(self.queue.get(), timeout)

    async def recv_final(self) -> tuple[int, str]:
        """Skip provisionals; return (status, raw) of the first final response."""
        while True:
            raw = await self.recv()
            code = int(raw.split()[1])
            if code >= 200:
                return code, raw

    def send(self, addr, method, call_id, cseq, extra="", body="", branch=None, uri="sip:goto.test", ctype=""):
        branch = branch or f"z9hG4bK{call_id}{cseq}"
        lines = [
            f"{method} {uri} SIP/2.0",
            f"Via: SIP/2.0/UDP 127.0.0.1:5999;branch={branch}",
            f"From: <sip:{USER}@goto.test>;tag=ft",
            "To: <sip:svc@goto.test>",
            f"Call-ID: {call_id}",
            f"CSeq: {cseq} {method}",
            "Max-Forwards: 70",
        ]
        if extra:
            lines.append(extra)
        if body:
            lines += ["Content-Type: application/sdp"]
        lines += [f"Content-Length: {len(body)}", "", body]
        self.transport.sendto("\r\n".join(lines).encode(), addr)


def auth_header(challenge: str, method: str, uri: str, password=PASSWORD) -> str:
    hdr = re.search(r"Proxy-Authenticate: Digest (.*)", challenge).group(1)
    p = dict(re.findall(r'(\w+)="([^"]*)"', hdr))
    cnonce, nc = "abc123", "00000001"
    ha1 = md5(f"{USER}:{p['realm']}:{password}")
    resp = md5(f"{ha1}:{p['nonce']}:{nc}:{cnonce}:auth:{md5(f'{method}:{uri}')}")
    return (
        f'Proxy-Authorization: Digest username="{USER}", realm="{p["realm"]}", nonce="{p["nonce"]}", '
        f'uri="{uri}", response="{resp}", qop=auth, nc={nc}, cnonce="{cnonce}", algorithm=MD5'
    )


@pytest.fixture
async def client():
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(RawClient, local_addr=("127.0.0.1", 0))
    yield proto
    transport.close()


async def start(behavior="answer"):
    srv = FakeGoToServer(USER, PASSWORD, behavior)
    return srv, await srv.start()


async def register(client, addr, password=PASSWORD):
    client.send(addr, "REGISTER", "reg1", 1)
    code, challenge = await client.recv_final()
    assert code == 407 and 'qop="auth"' in challenge
    client.send(addr, "REGISTER", "reg1", 2, extra=auth_header(challenge, "REGISTER", "sip:goto.test", password))
    return await client.recv_final()


SDP = (
    "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=t\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
    "m=audio 40000 RTP/AVP 8 101\r\na=rtpmap:8 PCMA/8000\r\na=rtpmap:101 telephone-event/8000\r\n"
)


async def invite(client, addr, call_id="inv1"):
    """INVITE -> 407 -> authenticated INVITE. Returns the 407 so callers can check it."""
    client.send(addr, "INVITE", call_id, 1, body=SDP)
    code, challenge = await client.recv_final()
    assert code == 407
    client.send(addr, "INVITE", call_id, 2, extra=auth_header(challenge, "INVITE", "sip:goto.test"), body=SDP)
    return challenge


async def test_register_correct_digest(client):
    srv, addr = await start()
    try:
        code, raw = await register(client, addr)
        assert code == 200 and srv.registered
        # echoes Call-ID/CSeq/Via branch, adds a to-tag on the final response
        assert "Call-ID: reg1" in raw and "CSeq: 2 REGISTER" in raw
        assert "branch=z9hG4bKreg12" in raw and re.search(r"To: .*;tag=\w+", raw)
    finally:
        await srv.stop()


async def test_register_wrong_digest_stays_407(client):
    srv, addr = await start()
    try:
        code, _ = await register(client, addr, password="wrong")
        assert code == 407 and not srv.registered
    finally:
        await srv.stop()


async def test_reject_auth_always_407(client):
    srv, addr = await start("reject_auth")
    try:
        code, _ = await register(client, addr)
        assert code == 407 and not srv.registered
    finally:
        await srv.stop()


async def test_answer_flow(client):
    srv, addr = await start("answer")
    try:
        await invite(client, addr)
        assert [int((await client.recv()).split()[1]) for _ in range(2)] == [100, 180]
        code, ok = await client.recv_final()
        assert code == 200 and srv.invite_authenticated
        m = re.search(r"m=audio (\d+) RTP/AVP", ok)
        assert m and "a=rtpmap:8 PCMA/8000" in ok
        client.send(addr, "ACK", "inv1", 2)
        for i in range(3):
            client.transport.sendto(b"\x80\x08" + bytes([i]) * 10, ("127.0.0.1", int(m.group(1))))
        client.send(addr, "BYE", "inv1", 3)
        code, _ = await client.recv_final()
        assert code == 200 and srv.got_ack and srv.got_bye and len(srv.rtp_packets) == 3
    finally:
        await srv.stop()


async def test_busy(client):
    srv, addr = await start("busy")
    try:
        await invite(client, addr)
        code, _ = await client.recv_final()
        assert code == 486 and srv.invite_authenticated
    finally:
        await srv.stop()


async def test_no_answer_then_cancel(client):
    srv, addr = await start("no_answer")
    try:
        await invite(client, addr)
        await client.recv(); await client.recv()  # 100, 180
        with pytest.raises(asyncio.TimeoutError):
            await client.recv(0.3)  # silence
        client.send(addr, "CANCEL", "inv1", 2, branch="z9hG4bKinv12")
        codes = sorted([(await client.recv_final())[0], (await client.recv_final())[0]])
        assert codes == [200, 487] and srv.got_cancel
    finally:
        await srv.stop()


async def test_hangup_during_audio(client):
    srv, addr = await start("hangup_during_audio")
    try:
        await invite(client, addr)
        _, ok = await client.recv_final()
        port = int(re.search(r"m=audio (\d+)", ok).group(1))
        client.send(addr, "ACK", "inv1", 2)
        for _ in range(10):
            client.transport.sendto(b"\x80\x08" + b"\0" * 10, ("127.0.0.1", port))
        bye = await client.recv()
        assert bye.startswith("BYE ") and "Call-ID: inv1" in bye
        client.transport.sendto(b"SIP/2.0 200 OK\r\nCall-ID: inv1\r\nCSeq: 1 BYE\r\n\r\n", addr)
        await asyncio.sleep(0.1)
        assert srv.sent_bye and srv.got_ack
    finally:
        await srv.stop()
