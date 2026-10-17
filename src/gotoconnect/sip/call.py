"""Outbound SIP call: REGISTER -> INVITE -> play G.711 audio over RTP -> BYE.

# ponytail: per-call REGISTER; persistent registration if call setup latency ever matters
# ponytail: UDP retransmits are a simple 0.5s-doubling resend until the first response; no full
#           RFC 3261 timer set (Timer E/G/H) and no re-INVITE/refresh handling.
"""

import asyncio
import logging
import re
import ssl
import time
from dataclasses import dataclass

from gotoconnect.exceptions import GoToAuthError, GoToCallError, GoToSipError
from gotoconnect.sip.audio import wav_to_g711
from gotoconnect.sip.messages import (
    CODEC_PAYLOADS,
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

log = logging.getLogger(__name__)  # start-lines/status codes only: never headers (auth values)

_REPLY_TIMEOUT = 5.0  # non-INVITE transactions and post-CANCEL/BYE waits
_GAP_SECONDS = 0.5  # pause between repeats of the audio
_REFUSALS = {486: "busy", 603: "rejected"}


@dataclass
class CallResult:
    answered: bool
    duration: float
    reason: str  # "completed" | "remote-hangup" | "no-answer" | "busy" | "rejected"
    # False only when we sent a BYE and the server did not answer it with a 200 (the call may
    # still be up on the far side). True when no BYE was needed or the BYE was confirmed.
    bye_confirmed: bool = True


def _branch_of(request: bytes) -> str:
    return re.search(rb"branch=([^;,\s]+)", request).group(1).decode()


@dataclass
class _Leg:
    """An established dialog (after a 200 OK): what in-dialog requests need."""

    dialog: SipDialog
    to_tag: str
    target: str | None  # the 200's Contact URI = request-URI of ACK/BYE
    routes: list[str]  # Record-Route reversed (RFC 3261 12.1.2)
    ack: bytes = b""
    invite_cseq: int = 0


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
        self.invite_branch = ""
        self.pending: tuple[SipDialog, str] | None = None  # (dialog, branch) of an INVITE with no final response yet

    async def send(self, data: bytes) -> None:
        log.debug("SIP -> %s", data.split(b"\r\n", 1)[0][:100].decode(errors="replace"))
        await self.tp.send(data)

    async def recv(self, timeout: float) -> bytes:
        raw = await self.tp.receive(timeout)
        log.debug("SIP <- %s", raw.split(b"\r\n", 1)[0][:100].decode(errors="replace"))
        return raw

    async def _final(self, method: str, cseq: int, timeout: float,
                     resend: bytes | None = None) -> SipResponse:
        """Next final (>=200) response to CSeq `cseq method`; provisionals, strays and requests are skipped.

        `resend` (UDP only): retransmit it at 0.5s, 1s, 2s... until the first response to this
        transaction arrives. Matching the whole CSeq ignores a stale retransmitted 407.
        """
        loop = asyncio.get_running_loop()
        deadline, delay = loop.time() + timeout, 0.5
        if self.via != "UDP":
            resend = None
        next_resend = loop.time() + delay
        while True:
            remaining = max(0.0, deadline - loop.time())
            wait = min(remaining, max(0.0, next_resend - loop.time())) if resend else remaining
            try:
                raw = await self.recv(wait)
            except asyncio.TimeoutError:
                if not resend or wait >= remaining:
                    raise
                await self.send(resend)
                delay *= 2
                next_resend = loop.time() + delay
                continue
            if not raw.startswith(b"SIP/2.0"):
                continue
            resp = SipResponse.parse(raw)
            if resp.get("cseq", "").split() != [str(cseq), method]:
                continue
            resend = None  # the server has the request
            if resp.status >= 200:
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
            await self.send(req)
            resp = await self._final(method, dialog.cseq, timeout, resend=req)
            if method == "INVITE":
                self.pending = None  # a final response ended it: nothing left to CANCEL
            if resp.status not in (401, 407):
                return resp
            if method == "INVITE":
                await self.ack_refusal(dialog, resp, _branch_of(req))
            if attempt:
                raise GoToAuthError("SIP credentials rejected by server")
            dialog.cseq += 1
            req = build(self._auth_line(resp, method, uri))
            if method == "INVITE":
                log.debug("INVITE challenged; retrying with credentials")

    async def ack_refusal(self, dialog, resp, branch: str) -> None:
        """ACK a non-2xx final: same transaction (branch) as the INVITE."""
        await self.send(build_ack(to_number=self.number, dialog=dialog, to_tag=_tag_of(resp),
                                  branch=branch, **self.common))

    def leg_from(self, dialog: SipDialog, resp: SipResponse) -> _Leg:
        contact = re.search(r"<([^>]+)>|(sip:\S+)", resp.get("contact", ""))
        target = (contact.group(1) or contact.group(2)) if contact else None
        routes = re.findall(r"<[^>]*>", resp.get("record-route", ""))[::-1]
        return _Leg(dialog, _tag_of(resp), target, routes)

    async def ack_2xx(self, leg: _Leg) -> None:
        """2xx ACK: a new transaction (fresh branch) to the Contact, via the route set."""
        leg.invite_cseq = leg.dialog.cseq
        leg.ack = build_ack(to_number=self.number, dialog=leg.dialog, to_tag=leg.to_tag,
                            request_uri=leg.target, routes=leg.routes, **self.common)
        await self.send(leg.ack)

    def bye(self, leg: _Leg) -> bytes:
        leg.dialog.cseq += 1
        return build_bye(to_number=self.number, dialog=leg.dialog, to_tag=leg.to_tag,
                         request_uri=leg.target, routes=leg.routes, **self.common)

    async def hangup(self, leg: _Leg) -> bool:
        """BYE and wait for its 200. False (and a warning) if it is refused or never answered."""
        req = self.bye(leg)
        await self.send(req)
        try:
            resp = await self._final("BYE", leg.dialog.cseq, _REPLY_TIMEOUT, resend=req)
        except asyncio.TimeoutError:
            log.warning("BYE got no reply; the far side may still think the call is up")
            return False
        if resp.status != 200:
            log.warning("BYE refused: %s %s", resp.status, resp.reason)
        return resp.status == 200

    async def cancel_pending(self) -> bool:
        """CANCEL the INVITE that has no final response yet and settle whatever answers it.

        Returns bye_confirmed. If the 200 raced the CANCEL, the call is up: ACK it, then hang up.
        """
        dialog, branch = self.pending
        self.pending = None
        await self.send(build_cancel(to_number=self.number, dialog=dialog, branch=branch, **self.common))
        try:  # the 487 for the INVITE (the 200 for the CANCEL is skipped by CSeq)
            resp = await self._final("INVITE", dialog.cseq, _REPLY_TIMEOUT)
        except asyncio.TimeoutError:
            return True
        if resp.status == 200:
            leg = self.leg_from(dialog, resp)
            await self.ack_2xx(leg)
            return await self.hangup(leg)
        await self.ack_refusal(dialog, resp, branch)
        return True

    async def register(self) -> None:
        dialog, uri = SipDialog(), f"sip:{self.domain}"
        resp = await self.transact("REGISTER", uri, dialog, lambda auth: build_register(
            username=self.username, domain=self.domain, local_ip=self.ip, local_port=self.port,
            dialog=dialog, auth_header=auth, transport=self.via), _REPLY_TIMEOUT)
        if resp.status != 200:
            raise GoToSipError(f"REGISTER failed: {resp.status} {resp.reason}")
        log.debug("registered")

    async def invite(self, ring_timeout: float, codec: str) -> tuple[SipDialog, SipResponse | None, str, bool]:
        """Returns (dialog, final response or None on ring timeout, INVITE branch, bye_confirmed)."""
        dialog = SipDialog()

        def build(auth):
            req = build_invite(to_number=self.number, media_port=self.rtp.local_port,
                               dialog=dialog, auth_header=auth, codec=codec, **self.common)
            self.invite_branch = _branch_of(req)
            self.pending = (dialog, self.invite_branch)
            return req

        uri = f"sip:{self.number}@{self.domain}"
        try:
            resp = await self.transact("INVITE", uri, dialog, build, ring_timeout)
        except asyncio.TimeoutError:
            log.debug("ring timeout; cancelling")
            return dialog, None, self.invite_branch, await self.cancel_pending()
        return dialog, resp, self.invite_branch, True

    async def play(self, payload: bytes, dest, repeat: int, stop: asyncio.Event, pt: int) -> None:
        for n in range(repeat):
            if n:
                try:  # the inter-repeat gap, cut short by a remote hangup
                    await asyncio.wait_for(stop.wait(), _GAP_SECONDS)
                except asyncio.TimeoutError:
                    pass
            if stop.is_set():
                return
            await self.rtp.stream(payload, dest, payload_type=pt, stop=stop)

    async def stream_until_done(self, payload: bytes, dest, repeat: int, pt: int, leg: _Leg) -> bool:
        """Stream audio while watching for the server's BYE. True if the remote hung up.

        A retransmitted 200 to our INVITE means the server missed our ACK: send it again.
        """
        stop = asyncio.Event()
        play = asyncio.create_task(self.play(payload, dest, repeat, stop, pt))
        hung_up, rx = False, None
        try:
            while not play.done():
                rx = asyncio.create_task(self.recv(3600))
                await asyncio.wait({rx, play}, return_when=asyncio.FIRST_COMPLETED)
                if not rx.done():
                    break  # audio finished; rx is reaped in finally
                raw = rx.result()
                if raw.startswith(b"BYE "):
                    await self.send(_ok_for(raw))
                    hung_up = True
                    stop.set()
                    break
                if raw.startswith(b"SIP/2.0 200"):
                    resp = SipResponse.parse(raw)
                    if resp.get("cseq", "").split() == [str(leg.invite_cseq), "INVITE"]:
                        await self.send(leg.ack)
            await play
        finally:
            stop.set()
            for task in (rx, play):  # also runs on outer cancellation: leave no orphan tasks
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in (rx, play) if t is not None), return_exceptions=True)
        return hung_up


class GoToVoice:
    def __init__(self, username: str, password: str, server: str = "sip.goto.com",
                 proxy: str | None = None, transport: str = "udp", port: int | None = None,
                 ssl_context: ssl.SSLContext | None = None) -> None:
        self.username, self.password, self.server = username, password, server
        self.proxy, self.transport, self.port = proxy, transport, port
        self.ssl_context = ssl_context  # TLS only; None = verified system CAs

    async def call_and_play(self, number: str, wav_bytes: bytes, *, ring_timeout: float = 25.0,
                            repeat: int = 1, codec: str = "pcma") -> CallResult:
        _check_number(number)  # fail before touching the network
        payload = wav_to_g711(wav_bytes, codec)  # also rejects unknown codecs (ValueError)
        pt = CODEC_PAYLOADS[codec][0]
        tp = make_transport(self.proxy or self.server, self.transport, self.port, self.ssl_context)
        rtp = call = leg = None
        acked = bye_sent = False
        try:
            await tp.connect()
            rtp = RtpSender()  # bound now so the INVITE's SDP carries the real port
            call = _Call(tp, self.transport, self.username, self.password, self.server, number, rtp)
            await call.register()
            dialog, resp, branch, bye_ok = await call.invite(ring_timeout, codec)
            if resp is None:
                return CallResult(False, 0.0, "no-answer", bye_ok)
            if resp.status in _REFUSALS:
                await call.ack_refusal(dialog, resp, branch)
                return CallResult(False, 0.0, _REFUSALS[resp.status])
            if resp.status != 200:
                await call.ack_refusal(dialog, resp, branch)
                raise GoToCallError(f"call failed: {resp.status} {resp.reason}")

            log.debug("answered")
            leg = call.leg_from(dialog, resp)
            started = time.monotonic()
            await call.ack_2xx(leg)
            acked = True  # dialog is up: from here the finally must hang up on any failure
            dest = _remote_media(resp.body)
            log.debug("streaming audio to %s:%s", *dest)
            hung_up = await call.stream_until_done(payload, dest, repeat, pt, leg)
            duration = time.monotonic() - started
            if hung_up:
                acked = False  # the server already ended the dialog
                log.debug("remote hangup")
                return CallResult(True, duration, "remote-hangup")
            bye_sent = True
            confirmed = await call.hangup(leg)
            return CallResult(True, time.monotonic() - started, "completed", confirmed)
        except OSError as exc:  # TimeoutError, gaierror, ConnectionError, ssl.SSLError ...
            raise GoToSipError(f"SIP I/O failure: {type(exc).__name__}: {exc}") from exc
        finally:
            if acked and not bye_sent:
                try:  # best-effort hangup so the callee isn't left off-hook; no wait for its 200
                    await call.send(call.bye(leg))
                except Exception:
                    pass
            elif call is not None and call.pending is not None:
                try:  # still ringing (e.g. task cancelled): best-effort CANCEL so the phone stops
                    await call.cancel_pending()
                except Exception:
                    pass
            if rtp:
                await rtp.close()
            await tp.close()
