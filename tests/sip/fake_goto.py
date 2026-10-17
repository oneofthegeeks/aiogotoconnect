"""Fake GoTo SIP server for tests.

Deliberately an independent reimplementation: it imports nothing from gotoconnect.sip,
so a bug in the library's message/digest code can't be mirrored here and hidden.
Single-call, UDP only. Mimics the proven GoTo behavior: 407 (not 401) challenges on
REGISTER and INVITE, then per ``behavior`` answer / busy / no_answer /
hangup_during_audio / reject_auth.
"""
import asyncio
import hashlib
import re
import secrets

REALM = "goto.test"
BEHAVIORS = {"answer", "busy", "no_answer", "hangup_during_audio", "reject_auth"}
REASONS = {
    100: "Trying", 180: "Ringing", 200: "OK", 407: "Proxy Authentication Required",
    486: "Busy Here", 487: "Request Terminated",
}


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


class _Msg:
    """Minimal SIP message: start line, ordered (name, value) headers, body."""

    def __init__(self, data: bytes):
        head, _, self.body = data.decode(errors="replace").partition("\r\n\r\n")
        lines = head.split("\r\n")
        self.start = lines[0]
        self.headers = [tuple(h.split(":", 1)) for h in lines[1:] if ":" in h]
        self.headers = [(n.strip(), v.strip()) for n, v in self.headers]
        self.is_response = self.start.startswith("SIP/2.0")
        parts = self.start.split()
        self.method = "" if self.is_response else parts[0]
        self.uri = "" if self.is_response else parts[1]

    def all(self, name: str) -> list[str]:
        return [v for n, v in self.headers if n.lower() == name.lower()]

    def get(self, name: str) -> str:
        vals = self.all(name)
        return vals[0] if vals else ""

    @property
    def cseq(self) -> tuple[int, str]:
        num, method = self.get("CSeq").split()
        return int(num), method


class _Sip(asyncio.DatagramProtocol):
    def __init__(self, server):
        self.server = server

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            self.server._on_sip(_Msg(data), addr)
        except (ValueError, IndexError):
            pass  # malformed datagram: ignore, like a real server would


class _Rtp(asyncio.DatagramProtocol):
    def __init__(self, server):
        self.server = server

    def datagram_received(self, data, addr):
        self.server._on_rtp(data)


class FakeGoToServer:
    def __init__(self, username: str, password: str, behavior: str = "answer"):
        assert behavior in BEHAVIORS, behavior
        self.username, self.password, self.behavior = username, password, behavior
        self.registered = False
        self.invite_authenticated = False
        self.rtp_packets: list[bytes] = []
        self.got_ack = False
        self.got_bye = False
        self.got_cancel = False
        self.sent_bye = False  # hangup_during_audio: we sent BYE to the client
        self._nonces: set[str] = set()
        self._invite: _Msg | None = None  # the authenticated INVITE, while call is live
        self._invite_addr = None
        self._to_tag = secrets.token_hex(4)
        self._sip = self._rtp = None
        self._host = "127.0.0.1"

    async def start(self) -> tuple[str, int]:
        loop = asyncio.get_running_loop()
        self._sip, _ = await loop.create_datagram_endpoint(lambda: _Sip(self), local_addr=(self._host, 0))
        return self._sip.get_extra_info("sockname")[:2]

    async def stop(self):
        for t in (self._sip, self._rtp):
            if t:
                t.close()

    # ---- building / sending -------------------------------------------------

    def _respond(self, req: _Msg, code: int, extra: list[str] = (), body: str = "", addr=None):
        to = req.get("To")
        if code >= 200 and ";tag=" not in to:
            to += f";tag={self._to_tag}"
        lines = [f"SIP/2.0 {code} {REASONS[code]}"]
        lines += [f"Via: {v}" for v in req.all("Via")]
        lines += [f"From: {req.get('From')}", f"To: {to}", f"Call-ID: {req.get('Call-ID')}", f"CSeq: {req.get('CSeq')}"]
        lines += list(extra)
        if body:
            lines.append("Content-Type: application/sdp")
        lines += [f"Content-Length: {len(body.encode())}", "", body]
        self._sip.sendto("\r\n".join(lines).encode(), addr or self._reply_addr)

    def _challenge(self, req: _Msg):
        nonce = secrets.token_hex(8)
        self._nonces.add(nonce)
        self._respond(req, 407, [f'Proxy-Authenticate: Digest realm="{REALM}", nonce="{nonce}", qop="auth"'])

    # ---- digest (own implementation) ------------------------------------------

    def _auth_ok(self, req: _Msg) -> bool:
        hdr = req.get("Proxy-Authorization")
        if not hdr.lower().startswith("digest ") or self.behavior == "reject_auth":
            return False
        p = {k.lower(): v1 or v2 for k, v1, v2 in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', hdr[7:])}
        needed = ("username", "realm", "nonce", "uri", "response", "qop", "nc", "cnonce")
        if any(k not in p for k in needed):
            return False
        if p["username"] != self.username or p["realm"] != REALM or p["nonce"] not in self._nonces:
            return False
        if p["qop"] != "auth" or p["uri"] != req.uri:
            return False
        ha1 = _md5(f"{self.username}:{REALM}:{self.password}")
        ha2 = _md5(f"{req.method}:{p['uri']}")
        want = _md5(f"{ha1}:{p['nonce']}:{p['nc']}:{p['cnonce']}:auth:{ha2}")
        return secrets.compare_digest(want, p["response"].lower())

    # ---- SIP dispatch ------------------------------------------------------------

    def _on_sip(self, msg: _Msg, addr):
        self._reply_addr = addr
        if msg.is_response:
            return  # only a 200 to our BYE is expected; nothing to do
        handler = getattr(self, f"_on_{msg.method.lower()}", None)
        if handler:
            handler(msg, addr)

    def _on_register(self, msg, addr):
        if self._auth_ok(msg):
            self.registered = True
            self._respond(msg, 200, ["Expires: 60"])
        else:
            self._challenge(msg)

    def _on_invite(self, msg, addr):
        if self._invite is not None and msg.get("Call-ID") == self._invite.get("Call-ID"):
            return  # retransmission of the live INVITE
        if not self._auth_ok(msg):
            self._challenge(msg)
            return
        self.invite_authenticated = True
        self._invite, self._invite_addr = msg, addr
        self._respond(msg, 100)
        self._respond(msg, 180)
        if self.behavior == "busy":
            self._respond(msg, 486)
        elif self.behavior in ("answer", "hangup_during_audio"):
            asyncio.ensure_future(self._answer(msg, addr))
        # no_answer: silence until CANCEL

    async def _answer(self, msg, addr):
        loop = asyncio.get_running_loop()
        self._rtp, _ = await loop.create_datagram_endpoint(lambda: _Rtp(self), local_addr=(self._host, 0))
        port = self._rtp.get_extra_info("sockname")[1]
        sdp = (
            f"v=0\r\no=- 1 1 IN IP4 {self._host}\r\ns=fake-goto\r\nc=IN IP4 {self._host}\r\nt=0 0\r\n"
            f"m=audio {port} RTP/AVP 8 101\r\na=rtpmap:8 PCMA/8000\r\na=rtpmap:101 telephone-event/8000\r\n"
            "a=sendrecv\r\n"
        )
        sip_port = self._sip.get_extra_info("sockname")[1]
        self._respond(msg, 200, [f"Contact: <sip:svc@{self._host}:{sip_port}>"], sdp, addr)

    def _on_ack(self, msg, addr):
        # Only an ACK for the authenticated INVITE counts, not the ACK to a 407.
        if self._invite is not None and msg.cseq[0] == self._invite.cseq[0]:
            self.got_ack = True

    def _on_cancel(self, msg, addr):
        self.got_cancel = True
        self._respond(msg, 200)
        if self._invite is not None:
            self._respond(self._invite, 487, addr=self._invite_addr)

    def _on_bye(self, msg, addr):
        self.got_bye = True
        self._respond(msg, 200)

    # ---- RTP ---------------------------------------------------------------------------

    def _on_rtp(self, data: bytes):
        self.rtp_packets.append(data)
        if self.behavior == "hangup_during_audio" and len(self.rtp_packets) == 10:
            self._send_bye()

    def _send_bye(self):
        inv = self._invite
        self.sent_bye = True
        sip_port = self._sip.get_extra_info("sockname")[1]
        contact = re.search(r"<([^>]+)>", inv.get("Contact"))
        lines = [
            f"BYE {contact.group(1) if contact else inv.get('From')} SIP/2.0",
            f"Via: SIP/2.0/UDP {self._host}:{sip_port};branch=z9hG4bK{secrets.token_hex(4)}",
            f"From: {inv.get('To')};tag={self._to_tag}",
            f"To: {inv.get('From')}",
            f"Call-ID: {inv.get('Call-ID')}",
            "CSeq: 1 BYE",
            "Max-Forwards: 70",
            "Content-Length: 0",
            "", "",
        ]
        self._sip.sendto("\r\n".join(lines).encode(), self._invite_addr)
