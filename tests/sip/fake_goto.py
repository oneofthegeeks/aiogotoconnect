"""Fake GoTo SIP server for tests.

Deliberately an independent reimplementation: it imports nothing from aiogotoconnect.sip,
so a bug in the library's message/digest code can't be mirrored here and hidden.
Single-call; UDP by default, SIP over TLS when given an ``ssl_context`` (RTP is always UDP). Mimics the proven GoTo behavior: 407 (not 401) challenges on
REGISTER and INVITE, then per ``behavior`` answer / busy / no_answer /
hangup_during_audio / reject_auth.

It is intentionally strict (nonce bound to method+Call-ID, ACK/BYE/CANCEL must match the
dialog, RTP must be well-formed) so a subtly wrong client fails here. Everything it sees
and sends is in ``log`` as ("in" | "out" | "error", text) for debugging failures.
"""
import asyncio
import hashlib
import re
import secrets

REALM = "goto.test"
BEHAVIORS = {"answer", "busy", "no_answer", "hangup_during_audio", "reject_auth",
             "answer_after_cancel",  # the 200 races the CANCEL: sent right after the CANCEL's 200
             "invite_unauth"}  # REGISTER works but the INVITE is challenged forever
REASONS = {
    100: "Trying", 401: "Unauthorized", 180: "Ringing", 200: "OK", 407: "Proxy Authentication Required",
    400: "Bad Request", 481: "Call/Transaction Does Not Exist",
    486: "Busy Here", 487: "Request Terminated",
}


_NAMES = {0: "PCMU", 8: "PCMA", 9: "G722"}


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _tag(header: str) -> str:
    m = re.search(r";\s*tag=([^;>\s]+)", header)
    return m.group(1) if m else ""


def _branch(msg: "_Msg") -> str:
    m = re.search(r"branch=([^;,\s]+)", msg.get("Via"))
    return m.group(1) if m else ""


class _Msg:
    """Minimal SIP message: start line, ordered (name, value) headers, body."""

    def __init__(self, data: bytes):
        head, _, self.body = data.decode(errors="replace").partition("\r\n\r\n")
        lines = head.split("\r\n")
        self.start = lines[0]
        self.headers = [(n.strip(), v.strip()) for n, v in (h.split(":", 1) for h in lines[1:] if ":" in h)]
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
        raw = data.decode(errors="replace")
        self.server.log.append(("in", raw))
        try:
            self.server._on_sip(_Msg(data), addr)
        except (ValueError, IndexError) as e:
            # Malformed datagram: don't act on it, but make it visible to the test.
            self.server.log.append(("error", f"unparseable SIP datagram ({e!r}): {raw!r}"))


class _Rtp(asyncio.DatagramProtocol):
    def __init__(self, server):
        self.server = server

    def datagram_received(self, data, addr):
        self.server._on_rtp(data)


class FakeGoToServer:
    def __init__(self, username: str, password: str, behavior: str = "answer", ssl_context=None, *,
                 drop_first: int = 0, challenge_401: bool = False, record_route=(),
                 resend_200: bool = False, bye_reply: int | None = 200,
                 answer_pt: int | None = None):
        """Options: drop_first = ignore the first N incoming requests (lossy link); challenge_401 =
        challenge with 401/WWW-Authenticate instead of 407; record_route = Record-Route values put
        on the 200 (ACK/BYE must then carry them as Route in reverse); resend_200 = send the 200
        twice (lost ACK); bye_reply = status for the client's BYE (None = stay silent);
        answer_pt = force the answered payload type (default: first offered of 0/8/9)."""
        assert behavior in BEHAVIORS, behavior
        self._drop_first, self._challenge_401, self._record_route = drop_first, challenge_401, list(record_route)
        self._resend_200, self._bye_reply, self._answer_pt = resend_200, bye_reply, answer_pt
        self.ack_count = 0  # valid ACKs for the answered INVITE
        self.challenge_acks = 0  # ACKs for our 407/401 challenges
        self._challenge_tags: set[str] = set()
        self._final_code = 0
        self._port = 0
        self._ssl_context = ssl_context  # server-side context -> SIP over TLS
        self._tls_server = None
        self.username, self.password, self.behavior = username, password, behavior
        self.registered = False
        self.invite_authenticated = False
        self.rtp_packets: list[bytes] = []  # valid RTP only (v2, agreed PT, >= 12 bytes)
        self.got_ack = False
        self.got_bye = False
        self.got_cancel = False  # a CANCEL that actually cancelled the pending INVITE
        self.sent_bye = False  # hangup_during_audio: we sent BYE to the client
        self.client_sdp: dict | None = None  # parsed offer: {"addr": (ip, port), "codecs": [pt], "rtpmap": {pt: str}}
        self.log: list[tuple[str, str]] = []
        self._nonces: dict[str, tuple[str, str]] = {}  # nonce -> (method, Call-ID) it was issued for
        self._invite: _Msg | None = None  # the authenticated INVITE
        self._invite_addr = None
        self._invite_final = False  # a final response was sent for the INVITE
        self._dialog_tag = ""  # our To-tag for the INVITE dialog (differs from any 407 tag)
        self._sip = self._rtp = None
        self._host = "127.0.0.1"
        self._contact = ""
        self._pt = 8  # payload type agreed in our answer (set in _answer)

    async def start(self) -> tuple[str, int]:
        loop = asyncio.get_running_loop()
        if self._ssl_context:
            self._tls_server = await asyncio.start_server(self._serve_tls, self._host, 0, ssl=self._ssl_context)
            port = self._tls_server.sockets[0].getsockname()[1]
        else:
            self._sip, _ = await loop.create_datagram_endpoint(lambda: _Sip(self), local_addr=(self._host, 0))
            port = self._sip.get_extra_info("sockname")[1]
        self._contact = f"sip:svc@{self._host}:{port}"
        self._port = port
        return self._host, port

    async def stop(self):
        for t in (self._sip, self._rtp, self._tls_server):
            if t:
                t.close()

    # ---- building / sending -------------------------------------------------

    async def _serve_tls(self, reader, writer):
        """One TLS connection: split the stream into messages by Content-Length; `addr` is the writer."""
        buf = b""
        try:
            while chunk := await reader.read(4096):
                buf += chunk
                while (end := buf.find(b"\r\n\r\n")) >= 0:
                    m = re.search(rb"^content-length:\s*(\d+)", buf[:end], re.I | re.M)
                    total = end + 4 + (int(m.group(1)) if m else 0)
                    if len(buf) < total:
                        break
                    data, buf = buf[:total], buf[total:]
                    self.log.append(("in", data.decode(errors="replace")))
                    try:
                        self._on_sip(_Msg(data), writer)
                    except (ValueError, IndexError) as e:
                        self.log.append(("error", f"unparseable SIP message ({e!r})"))
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    def _send(self, text: str, addr):
        self.log.append(("out", text))
        if self._ssl_context:
            addr.write(text.encode())
        else:
            self._sip.sendto(text.encode(), addr)

    def _respond(self, req: _Msg, code: int, addr, extra: list[str] = (), body: str = "", tag: str = ""):
        """Echo Via/From/To/Call-ID/CSeq. Finals get a To-tag: `tag` if given, else a fresh one."""
        to = req.get("To")
        if ";tag=" not in to and (code >= 200 or (tag and code == 180)):
            to += f";tag={tag or secrets.token_hex(4)}"
        lines = [f"SIP/2.0 {code} {REASONS[code]}"]
        lines += [f"Via: {v}" for v in req.all("Via")]
        lines += [f"From: {req.get('From')}", f"To: {to}", f"Call-ID: {req.get('Call-ID')}", f"CSeq: {req.get('CSeq')}"]
        lines += list(extra)
        if body:
            lines.append("Content-Type: application/sdp")
        lines += [f"Content-Length: {len(body.encode())}", "", body]
        self._send("\r\n".join(lines), addr)

    def _challenge(self, req: _Msg, addr):
        nonce = secrets.token_hex(8)
        self._nonces[nonce] = (req.method, req.get("Call-ID"))
        tag = secrets.token_hex(4)
        self._challenge_tags.add(tag)
        code, header = (401, "WWW-Authenticate") if self._challenge_401 else (407, "Proxy-Authenticate")
        self._respond(req, code, addr, [f'{header}: Digest realm="{REALM}", nonce="{nonce}", qop="auth"'], tag=tag)

    def _final_invite(self, code: int, extra=(), body="", addr=None):
        self._invite_final = True
        self._final_code = code
        self._respond(self._invite, code, addr or self._invite_addr, extra, body, tag=self._dialog_tag)

    # ---- digest (own implementation) ------------------------------------------

    def _auth_ok(self, req: _Msg) -> bool:
        hdr = req.get("Authorization" if self._challenge_401 else "Proxy-Authorization")
        if not hdr.lower().startswith("digest ") or self.behavior == "reject_auth":
            return False
        p = {k.lower(): v1 or v2 for k, v1, v2 in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', hdr[7:])}
        needed = ("username", "realm", "nonce", "uri", "response", "qop", "nc", "cnonce")
        if any(k not in p for k in needed):
            return False
        if p["username"] != self.username or p["realm"] != REALM:
            return False
        # The nonce must have been issued for this very method + Call-ID (no cross-request replay).
        if self._nonces.get(p["nonce"]) != (req.method, req.get("Call-ID")):
            return False
        if p["qop"] != "auth" or p["uri"] != req.uri:
            return False
        ha1 = _md5(f"{self.username}:{REALM}:{self.password}")
        ha2 = _md5(f"{req.method}:{p['uri']}")
        want = _md5(f"{ha1}:{p['nonce']}:{p['nc']}:{p['cnonce']}:auth:{ha2}")
        return secrets.compare_digest(want, p["response"].lower())

    # ---- SIP dispatch ------------------------------------------------------------

    def _on_sip(self, msg: _Msg, addr):
        if msg.is_response:
            return  # only a 200 to our BYE is expected; nothing to do
        if self._drop_first > 0:
            self._drop_first -= 1
            self.log.append(("error", f"dropped {msg.method} (drop_first)"))
            return
        handler = getattr(self, f"_on_{msg.method.lower()}", None)
        if handler:
            handler(msg, addr)

    def _on_register(self, msg, addr):
        if self._auth_ok(msg):
            self.registered = True
            self._respond(msg, 200, addr, ["Expires: 60"])
        else:
            self._challenge(msg, addr)

    def _on_invite(self, msg, addr):
        if self._invite is not None and msg.get("Call-ID") == self._invite.get("Call-ID"):
            return  # retransmission of the live INVITE
        if self.behavior == "invite_unauth" or not self._auth_ok(msg):
            self._challenge(msg, addr)
            return
        self.invite_authenticated = True
        self._invite, self._invite_addr, self._invite_final = msg, addr, False
        self._dialog_tag = secrets.token_hex(4)
        self.client_sdp = self._parse_offer(msg.body)
        self._respond(msg, 100, addr)
        self._respond(msg, 180, addr, tag=self._dialog_tag)
        if self.behavior == "busy":
            self._final_invite(486)
        elif self.behavior in ("answer", "hangup_during_audio"):
            asyncio.ensure_future(self._answer(msg, addr))
        # no_answer: silence until CANCEL

    @staticmethod
    def _parse_offer(body: str) -> dict | None:
        c = re.search(r"^c=IN IP4 (\S+)", body, re.M)
        m = re.search(r"^m=audio (\d+) RTP/AVP ([\d ]+)", body, re.M)
        if not (c and m):
            return None
        return {
            "addr": (c.group(1), int(m.group(1))),
            "codecs": [int(x) for x in m.group(2).split()],
            "rtpmap": {int(n): v for n, v in re.findall(r"^a=rtpmap:(\d+) (\S+)", body, re.M)},
        }

    async def _answer(self, msg, addr):
        loop = asyncio.get_running_loop()
        offered = (self.client_sdp or {}).get("codecs", [8])
        self._pt = self._answer_pt if self._answer_pt is not None else next((p for p in offered if p in _NAMES), 8)
        name = _NAMES[self._pt]
        self._rtp, _ = await loop.create_datagram_endpoint(lambda: _Rtp(self), local_addr=(self._host, 0))
        port = self._rtp.get_extra_info("sockname")[1]
        sdp = (
            f"v=0\r\no=- 1 1 IN IP4 {self._host}\r\ns=fake-goto\r\nc=IN IP4 {self._host}\r\nt=0 0\r\n"
            f"m=audio {port} RTP/AVP {self._pt} 101\r\na=rtpmap:{self._pt} {name}/8000\r\na=rtpmap:101 telephone-event/8000\r\n"
            "a=sendrecv\r\n"
        )
        extra = [f"Contact: <{self._contact}>", *(f"Record-Route: {r}" for r in self._record_route)]
        self._final_invite(200, extra, sdp, addr)
        if self._resend_200:  # as if the ACK were lost: the client must ACK again
            await asyncio.sleep(0.3)
            self._final_invite(200, extra, sdp, addr)

    def _in_dialog(self, msg) -> bool:
        return (
            self._invite is not None
            and msg.get("Call-ID") == self._invite.get("Call-ID")
            and _tag(msg.get("To")) == self._dialog_tag
        )

    def _on_ack(self, msg, addr):
        # Only an ACK for the authenticated INVITE's dialog counts: not the ACK to a 407,
        # and not one with a foreign Call-ID or the wrong To-tag.
        if self._in_dialog(msg) and msg.cseq[0] == self._invite.cseq[0]:
            if self._final_code == 200:  # 2xx ACK: new transaction, to the Contact, via the route set
                bad = (msg.uri != self._contact or _branch(msg) == _branch(self._invite)
                       or msg.all("Route") != self._expected_routes())
            else:  # non-2xx ACK: same transaction as the INVITE
                bad = _branch(msg) != _branch(self._invite)
            if bad:
                self.log.append(("error", "ACK has the wrong branch/request-URI/Route for the final response"))
                return
            self.got_ack = True
            self.ack_count += 1
        elif _tag(msg.get("To")) in self._challenge_tags:
            self.challenge_acks += 1
        else:
            self.log.append(("error", "ACK ignored: Call-ID/To-tag/CSeq do not match the dialog"))

    def _on_cancel(self, msg, addr):
        inv = self._invite
        if not (
            inv is not None
            and msg.get("Call-ID") == inv.get("Call-ID")
            and msg.cseq[0] == inv.cseq[0]
            and _branch(msg) == _branch(inv)
            and msg.uri == inv.uri
        ):
            self.log.append(("error", "CANCEL does not match the INVITE (Call-ID/CSeq/branch/URI)"))
            self._respond(msg, 481, addr)
            return
        self._respond(msg, 200, addr)
        if self._invite_final:
            self.log.append(("error", "CANCEL after final response: no effect"))
            return
        self.got_cancel = True
        if self.behavior == "answer_after_cancel":
            asyncio.ensure_future(self._answer(inv, self._invite_addr))
        else:
            self._final_invite(487)

    def _expected_routes(self) -> list[str]:
        return list(reversed(self._record_route))

    def _on_bye(self, msg, addr):
        if not self._in_dialog(msg):
            self.log.append(("error", "BYE does not match the dialog (Call-ID/To-tag)"))
            self._respond(msg, 481, addr)
        elif msg.all("Route") != self._expected_routes():
            self.log.append(("error", "BYE lacks the Route set from our Record-Route"))
            self._respond(msg, 400, addr)
        elif msg.uri != self._contact:
            self.log.append(("error", f"BYE request-URI {msg.uri!r} is not our Contact {self._contact!r}"))
            self._respond(msg, 400, addr)
        else:
            self.got_bye = True
            if self._bye_reply is not None:
                self._respond(msg, self._bye_reply, addr)

    # ---- RTP ---------------------------------------------------------------------------

    def _on_rtp(self, data: bytes):
        if len(data) < 12 or data[0] >> 6 != 2 or data[1] & 0x7F != self._pt:
            self.log.append(("error", f"invalid RTP packet (want v2, PT {self._pt}, >=12 bytes): {data[:12].hex()}"))
            return
        self.rtp_packets.append(data)
        if self.behavior == "hangup_during_audio" and len(self.rtp_packets) == 10:
            self._send_bye()

    def _send_bye(self):
        inv = self._invite
        contact = re.search(r"<([^>]+)>", inv.get("Contact"))
        if not contact:
            # Can't build a valid request-URI; fail loudly rather than send a malformed BYE.
            self.log.append(("error", "cannot send BYE: INVITE has no <Contact>"))
            return
        self.sent_bye = True
        via = "TLS" if self._ssl_context else "UDP"
        sip_port = self._port
        lines = [
            f"BYE {contact.group(1)} SIP/2.0",
            f"Via: SIP/2.0/{via} {self._host}:{sip_port};branch=z9hG4bK{secrets.token_hex(4)}",
            f"From: {inv.get('To')};tag={self._dialog_tag}",
            f"To: {inv.get('From')}",
            f"Call-ID: {inv.get('Call-ID')}",
            "CSeq: 1 BYE",
            "Max-Forwards: 70",
            "Content-Length: 0",
            "", "",
        ]
        self._send("\r\n".join(lines), self._invite_addr)
