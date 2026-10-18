"""Pure SIP message layer: build requests, parse responses, compute digest auth.

No sockets, no state machine. Header structure mirrors the SIPp scenarios
proven against GoTo production. `auth_header` args are complete header lines
(e.g. "Proxy-Authorization: Digest ..."), so the caller picks the header name
from the challenge type (407 -> Proxy-Authorization, 401 -> Authorization).
"""

import hashlib
import re
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from aiogotoconnect.exceptions import GoToSipError

CRLF = "\r\n"
# codec -> (RTP payload type, rtpmap name)
CODEC_PAYLOADS = {"pcma": (8, "PCMA"), "pcmu": (0, "PCMU")}


@dataclass
class SipResponse:
    status: int
    reason: str
    headers: dict[str, str]  # keys lower-cased; repeated headers joined with ", "
    body: str

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)

    @classmethod
    def parse(cls, raw: bytes) -> "SipResponse":
        head, _, body = raw.decode("utf-8", errors="replace").partition(CRLF + CRLF)
        lines = head.split(CRLF)
        m = re.fullmatch(r"SIP/2\.0 (\d{3})(?: (.*))?", lines[0])
        if not m:
            raise GoToSipError(f"unparseable SIP response: {lines[0][:60]!r}")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if not sep:
                continue  # ponytail: header line folding not supported; GoTo doesn't fold
            key = name.strip().lower()
            value = value.strip()
            headers[key] = f"{headers[key]}, {value}" if key in headers else value
        return cls(int(m.group(1)), m.group(2) or "", headers, body)


@dataclass
class SipDialog:
    """Identifiers for one call. Callers bump `cseq` between transactions."""

    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    from_tag: str = field(default_factory=lambda: secrets.token_hex(4))
    cseq: int = 1

    def branch(self) -> str:
        return "z9hG4bK" + secrets.token_hex(8)


def _request(method: str, request_uri: str, dialog: SipDialog, username: str, domain: str,
             local_ip: str, local_port: int, transport: str, to_uri: str, to_tag: str = "",
             branch: str | None = None, cseq_method: str | None = None, extra: Sequence[str] = (),
             routes: Sequence[str] = (), body: str = "") -> bytes:
    # Over TLS the Contact must say so, or the server sends in-dialog requests (its BYE) over UDP.
    contact_params = ";transport=tls" if transport.upper() == "TLS" else ""
    lines = [
        f"{method} {request_uri} SIP/2.0",
        f"Via: SIP/2.0/{transport} {local_ip}:{local_port};branch={branch or dialog.branch()}",
        f"From: <sip:{username}@{domain}>;tag={dialog.from_tag}",
        f"To: <{to_uri}>" + (f";tag={to_tag}" if to_tag else ""),
        f"Call-ID: {dialog.call_id}",
        f"CSeq: {dialog.cseq} {cseq_method or method}",
        *(f"Route: {r}" for r in routes),
        f"Contact: <sip:{username}@{local_ip}:{local_port}{contact_params}>",
        "Max-Forwards: 70",
        *extra,
        f"Content-Length: {len(body.encode())}",
    ]
    # Header-injection guard: no CR/LF/control chars in any header line or URI.
    if any(ord(c) < 0x20 or ord(c) == 0x7F for line in lines for c in line):
        raise GoToSipError("control character in SIP header value")
    return (CRLF.join(lines) + CRLF + CRLF + body).encode()


def _check_number(to_number: str) -> str:
    if not re.fullmatch(r"\+?[0-9*#]{1,32}", to_number):
        raise ValueError(f"invalid destination number: {to_number!r}")
    return to_number


def build_register(username: str, domain: str, local_ip: str, local_port: int, dialog: SipDialog,
                   expires: int = 60, auth_header: str | None = None, transport: str = "UDP") -> bytes:
    return _request("REGISTER", f"sip:{domain}", dialog, username, domain, local_ip, local_port,
                    transport, f"sip:{username}@{domain}",
                    extra=[f"Expires: {expires}", *([auth_header] if auth_header else [])])


def _sdp(local_ip: str, media_port: int, codec: str = "pcma") -> str:
    pt, name = CODEC_PAYLOADS[codec]
    return CRLF.join([
        "v=0",
        f"o=- 53655765 2353687637 IN IP4 {local_ip}",
        "s=GoTo",
        f"c=IN IP4 {local_ip}",
        "t=0 0",
        f"m=audio {media_port} RTP/AVP {pt} 101",
        f"a=rtpmap:{pt} {name}/8000",
        "a=ptime:20",
        "a=sendrecv",
        "a=rtpmap:101 telephone-event/8000",
        "a=fmtp:101 0-15",
        "",
    ])


def build_invite(username: str, domain: str, to_number: str, local_ip: str, local_port: int,
                 media_port: int, dialog: SipDialog, auth_header: str | None = None,
                 transport: str = "UDP", codec: str = "pcma") -> bytes:
    uri = f"sip:{_check_number(to_number)}@{domain}"
    return _request("INVITE", uri, dialog, username, domain, local_ip, local_port, transport, uri,
                    extra=[*([auth_header] if auth_header else []), "Content-Type: application/sdp"],
                    body=_sdp(local_ip, media_port, codec))


def build_ack(username: str, domain: str, to_number: str, local_ip: str, local_port: int,
              dialog: SipDialog, to_tag: str = "", routes: Sequence[str] = (),
              branch: str | None = None, transport: str = "UDP",
              request_uri: str | None = None) -> bytes:
    """Pass the INVITE's `branch` when ACKing a non-2xx; omit it for a 2xx ACK (new transaction).
    `request_uri` = the 200 OK's Contact URI for a 2xx ACK (RFC 3261 12.2.1.1); To stays the callee."""
    uri = f"sip:{_check_number(to_number)}@{domain}"
    return _request("ACK", request_uri or uri, dialog, username, domain, local_ip, local_port,
                    transport, uri, to_tag=to_tag, branch=branch, routes=routes)


def build_bye(username: str, domain: str, to_number: str, local_ip: str, local_port: int,
              dialog: SipDialog, to_tag: str = "", routes: Sequence[str] = (),
              transport: str = "UDP", request_uri: str | None = None) -> bytes:
    """`request_uri` = the 200 OK's Contact URI (in-dialog requests go to the remote target)."""
    uri = f"sip:{_check_number(to_number)}@{domain}"
    return _request("BYE", request_uri or uri, dialog, username, domain, local_ip, local_port,
                    transport, uri, to_tag=to_tag, routes=routes)


def build_cancel(username: str, domain: str, to_number: str, local_ip: str, local_port: int,
                 dialog: SipDialog, branch: str, transport: str = "UDP") -> bytes:
    """`branch` and dialog.cseq must match the INVITE being cancelled (RFC 3261 9.1)."""
    uri = f"sip:{_check_number(to_number)}@{domain}"
    return _request("CANCEL", uri, dialog, username, domain, local_ip, local_port, transport, uri,
                    branch=branch)


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def digest_response(challenge: str, method: str, uri: str, username: str, password: str,
                    cnonce: str | None = None) -> str:
    """Answer a WWW-/Proxy-Authenticate challenge; returns "Digest username=..., ..." (value only)."""
    params = {k.lower(): v1 or v2 for k, v1, v2 in
              re.findall(r'(\w+)=(?:"([^"]*)"|([^\s,]*))', challenge.partition(" ")[2])}
    algorithm = params.get("algorithm", "MD5")
    if algorithm.upper() != "MD5":
        raise GoToSipError(f"unsupported digest algorithm: {algorithm}")
    if "realm" not in params or "nonce" not in params:
        raise GoToSipError("malformed digest challenge: missing realm or nonce")
    realm, nonce = params["realm"], params["nonce"]
    ha1 = _md5(f"{username}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    fields = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"']
    if "auth" in [q.strip() for q in params.get("qop", "").split(",")]:
        cnonce = cnonce or secrets.token_hex(8)
        nc = "00000001"
        response = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        tail = ["qop=auth", f"nc={nc}", f'cnonce="{cnonce}"']
    else:
        response = _md5(f"{ha1}:{nonce}:{ha2}")
        tail = []
    fields += [f'response="{response}"', "algorithm=MD5", *tail]
    if "opaque" in params:
        fields.append(f'opaque="{params["opaque"]}"')
    return "Digest " + ", ".join(fields)
