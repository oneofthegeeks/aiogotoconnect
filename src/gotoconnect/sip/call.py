"""Outbound SIP call: REGISTER -> INVITE -> play G.711 audio over RTP -> BYE.

# ponytail: per-call REGISTER; persistent registration if call setup latency ever matters
# ponytail: no retransmits (UDP) and no Record-Route handling; GoTo answers promptly and the
#           contact is directly reachable. Add timers/route sets if lossy links show up.
"""

import asyncio
import re
import time
from dataclasses import dataclass

from gotoconnect.exceptions import GoToAuthError, GoToCallError, GoToSipError
from gotoconnect.sip.audio import wav_to_g711
from gotoconnect.sip.messages import (
    SipDialog,
    SipResponse,
    _check_number,
    build_ack,
    build_bye,
    build_cancel,
    build_invite,
    build_register,
    digest_response,
)
from gotoconnect.sip.rtp import RtpSender
from gotoconnect.sip.transport import SipTransport, make_transport

_REPLY_TIMEOUT = 5.0  # non-INVITE transactions and post-CANCEL/BYE waits
_GAP_SECONDS = 0.5  # pause between repeats of the audio
_REFUSALS = {486: "busy", 603: "rejected"}


@dataclass
class CallResult:
    answered: bool
    duration: float
    reason: str  # "completed" | "remote-hangup" | "no-answer" | "busy" | "rejected"


def _branch_of(request: bytes) -> str:
    return re.search(rb"branch=([^;,\s]+)", request).group(1).decode()


def _tag_of(response: SipResponse) -> str:
    m = re.search(r";\s*tag=([^;>\s]+)", response.get("to", ""))
    return m.group(1) if m else ""


def _ok_for(request: bytes) -> bytes:
    """A 200 OK for an incoming request: echo Via/From/To/Call-ID/CSeq."""
    echo = ("via", "from", "to", "call-id", "cseq")
    lines = request.decode(errors="replace").split("\r\n\r\n")[0].split("\r\n")[1:]
    kept = [ln for ln in lines if ln.partition(":")[0].strip().lower() in echo]
    return "\r\n".join(["SIP/2.0 200 OK", *kept, "Content-Length: 0", "", ""]).encode()


def _remote_media(sdp: str) -> tuple[str, int]:
    c = re.search(r"^c=IN IP4 (\S+)", sdp, re.M)
    m = re.search(r"^m=audio (\d+) ", sdp, re.M)
    if not (c and m):
        raise GoToCallError("200 OK carried no usable audio SDP")
    return c.group(1), int(m.group(1))


class _Call:
    """One call's signalling; owns the transport and RTP socket for its lifetime."""

    def __init__(self, tp: SipTransport, mode: str, username, password, domain, number, rtp):
        self.tp, self.via, self.username, self.password = tp, mode.upper(), username, password
        self.domain, self.number, self.rtp = domain, number, rtp
        self.ip, self.port = tp.local_addr
        self.common = dict(username=username, domain=domain, local_ip=self.ip,
                           local_port=self.port, transport=self.via)

    async def _final(self, method: str, timeout: float) -> SipResponse:
        """Next final (>=200) response to `method`; provisionals, strays and requests are skipped."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            raw = await self.tp.receive(max(0.0, deadline - asyncio.get_running_loop().time()))
            if not raw.startswith(b"SIP/2.0"):
                continue
            resp = SipResponse.parse(raw)
            if resp.status >= 200 and resp.get("cseq", "").endswith(method):
                return resp

    def _auth_line(self, resp: SipResponse, method: str, uri: str) -> str:
        name, header = (("Authorization", "www-authenticate") if resp.status == 401
                        else ("Proxy-Authorization", "proxy-authenticate"))
        challenge = resp.get(header)
        if not challenge:
            raise GoToSipError(f"{resp.status} response without a digest challenge")
        return f"{name}: " + digest_response(challenge, method, uri, self.username, self.password)

    async def transact(self, method, uri, dialog, build, timeout) -> SipResponse:
        """Send, answer one 401/407 challenge, return the final response.

        `build(auth_line)` makes the request bytes. A second challenge means bad credentials:
        raise rather than loop. Non-2xx finals to INVITE (the 407) are ACKed here.
        """
        req = build(None)
        for attempt in (0, 1):
            await self.tp.send(req)
            resp = await self._final(method, timeout)
            if resp.status not in (401, 407):
                return resp
            if method == "INVITE":
                await self.ack_refusal(dialog, resp, req)
            if attempt:
                raise GoToAuthError("SIP credentials rejected by server")
            dialog.cseq += 1
            req = build(self._auth_line(resp, method, uri))

    async def ack_refusal(self, dialog, resp, invite: bytes) -> None:
        await self.tp.send(build_ack(to_number=self.number, dialog=dialog, to_tag=_tag_of(resp),
                                     branch=_branch_of(invite), **self.common))

    async def register(self) -> None:
        dialog, uri = SipDialog(), f"sip:{self.domain}"
        resp = await self.transact("REGISTER", uri, dialog, lambda auth: build_register(
            username=self.username, domain=self.domain, local_ip=self.ip, local_port=self.port,
            dialog=dialog, auth_header=auth, transport=self.via), _REPLY_TIMEOUT)
        if resp.status != 200:
            raise GoToSipError(f"REGISTER failed: {resp.status} {resp.reason}")

    async def invite(self, ring_timeout: float) -> tuple[SipDialog, SipResponse | None, bytes]:
        """Returns (dialog, final response or None on ring timeout, last INVITE sent)."""
        dialog, sent = SipDialog(), []

        def build(auth):
            sent.append(build_invite(to_number=self.number, media_port=self.rtp.local_port,
                                     dialog=dialog, auth_header=auth, **self.common))
            return sent[-1]

        uri = f"sip:{self.number}@{self.domain}"
        try:
            resp = await self.transact("INVITE", uri, dialog, build, ring_timeout)
        except asyncio.TimeoutError:
            await self.tp.send(build_cancel(to_number=self.number, dialog=dialog,
                                            branch=_branch_of(sent[-1]), **self.common))
            try:  # the 487 for the INVITE (the 200 for the CANCEL is skipped by _final)
                resp = await self._final("INVITE", _REPLY_TIMEOUT)
                await self.ack_refusal(dialog, resp, sent[-1])
            except asyncio.TimeoutError:
                pass
            return dialog, None, sent[-1]
        return dialog, resp, sent[-1]

    async def play(self, payload: bytes, dest, repeat: int, stop: asyncio.Event) -> None:
        for n in range(repeat):
            if n:
                try:  # the inter-repeat gap, cut short by a remote hangup
                    await asyncio.wait_for(stop.wait(), _GAP_SECONDS)
                except asyncio.TimeoutError:
                    pass
            if stop.is_set():
                return
            await self.rtp.stream(payload, dest, stop=stop)

    async def stream_until_done(self, payload: bytes, dest, repeat: int) -> bool:
        """Stream audio while watching for the server's BYE. True if the remote hung up."""
        stop = asyncio.Event()
        play = asyncio.create_task(self.play(payload, dest, repeat, stop))
        hung_up = False
        try:
            while not play.done():
                rx = asyncio.create_task(self.tp.receive(3600))
                await asyncio.wait({rx, play}, return_when=asyncio.FIRST_COMPLETED)
                if not rx.done():
                    rx.cancel()
                    await asyncio.gather(rx, return_exceptions=True)
                    break
                raw = rx.result()
                if raw.startswith(b"BYE "):
                    await self.tp.send(_ok_for(raw))
                    hung_up = True
                    stop.set()
                    break
            await play
        finally:
            stop.set()
            if not play.done():
                play.cancel()
                await asyncio.gather(play, return_exceptions=True)
        return hung_up


class GoToVoice:
    def __init__(self, username: str, password: str, server: str = "sip.goto.com",
                 proxy: str | None = None, transport: str = "udp", port: int | None = None):
        self.username, self.password, self.server = username, password, server
        self.proxy, self.transport, self.port = proxy, transport, port

    async def call_and_play(self, number: str, wav_bytes: bytes, *, ring_timeout: float = 25.0,
                            repeat: int = 1, codec: str = "pcma") -> CallResult:
        _check_number(number)  # fail before touching the network
        payload = wav_to_g711(wav_bytes, codec)
        tp = make_transport(self.proxy or self.server, self.transport, self.port)
        rtp = None
        await tp.connect()
        try:
            rtp = RtpSender()  # bound now so the INVITE's SDP carries the real port
            call = _Call(tp, self.transport, self.username, self.password, self.server, number, rtp)
            await call.register()
            dialog, resp, invite = await call.invite(ring_timeout)
            if resp is None:
                return CallResult(False, 0.0, "no-answer")
            if resp.status in _REFUSALS:
                await call.ack_refusal(dialog, resp, invite)
                return CallResult(False, 0.0, _REFUSALS[resp.status])
            if resp.status != 200:
                await call.ack_refusal(dialog, resp, invite)
                raise GoToCallError(f"call failed: {resp.status} {resp.reason}")

            dest = _remote_media(resp.body)
            to_tag = _tag_of(resp)
            contact = re.search(r"<([^>]+)>|(sip:\S+)", resp.get("contact", ""))
            target = (contact.group(1) or contact.group(2)) if contact else None
            started = time.monotonic()
            await tp.send(build_ack(to_number=number, dialog=dialog, to_tag=to_tag,
                                    request_uri=target, **call.common))
            hung_up = await call.stream_until_done(payload, dest, repeat)
            duration = time.monotonic() - started
            if hung_up:
                return CallResult(True, duration, "remote-hangup")
            dialog.cseq += 1
            await tp.send(build_bye(to_number=number, dialog=dialog, to_tag=to_tag,
                                    request_uri=target, **call.common))
            try:
                await call._final("BYE", _REPLY_TIMEOUT)
            except asyncio.TimeoutError:
                pass  # the call is over either way
            return CallResult(True, time.monotonic() - started, "completed")
        finally:
            if rtp:
                await rtp.close()
            await tp.close()
