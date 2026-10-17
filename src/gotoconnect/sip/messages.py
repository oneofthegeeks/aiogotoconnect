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
from dataclasses import dataclass, field

from gotoconnect.exceptions import GoToSipError

CRLF = "\r\n"


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


def _request(method, request_uri, dialog, username, domain, local_ip, local_port, transport,
             to_uri, to_tag="", branch=None, cseq_method=None, extra=(), routes=(), body=""):
    lines = [
        f"{method} {request_uri} SIP/2.0",
        f"Via: SIP/2.0/{transport} {local_ip}:{local_port};branch={branch or dialog.branch()}",
        f"From: <sip:{username}@{domain}>;tag={dialog.from_tag}",
        f"To: <{to_uri}>" + (f";tag={to_tag}" if to_tag else ""),
        f"Call-ID: {dialog.call_id}",
        f"CSeq: {dialog.cseq} {cseq_method or method}",
        *(f"Route: {r}" for r in routes),
        f"Contact: <sip:{username}@{local_ip}:{local_port}>",
        "Max-Forwards: 70",
        *extra,
        f"Content-Length: {len(body.encode())}",
    ]
    return (CRLF.join(lines) + CRLF + CRLF + body).encode()


def build_register(username, domain, local_ip, local_port, dialog, expires=60,
                   auth_header=None, transport="UDP") -> bytes:
    return _request("REGISTER", f"sip:{domain}", dialog, username, domain, local_ip, local_port,
                    transport, f"sip:{username}@{domain}",
                    extra=[f"Expires: {expires}", *([auth_header] if auth_header else [])])


def _sdp(local_ip: str, media_port: int) -> str:
    return CRLF.join([
        "v=0",
        f"o=- 53655765 2353687637 IN IP4 {local_ip}",
        "s=GoTo",
        f"c=IN IP4 {local_ip}",
        "t=0 0",
        f"m=audio {media_port} RTP/AVP 8 101",
        "a=rtpmap:8 PCMA/8000",
        "a=ptime:20",
        "a=sendrecv",
        "a=rtpmap:101 telephone-event/8000",
        "a=fmtp:101 0-15",
        "",
    ])


def build_invite(username, domain, to_number, local_ip, local_port, media_port, dialog,
                 auth_header=None, transport="UDP") -> bytes:
    uri = f"sip:{to_number}@{domain}"
    return _request("INVITE", uri, dialog, username, domain, local_ip, local_port, transport, uri,
                    extra=[*([auth_header] if auth_header else []), "Content-Type: application/sdp"],
                    body=_sdp(local_ip, media_port))


def build_ack(username, domain, to_number, local_ip, local_port, dialog, to_tag="",
              routes=(), branch=None, transport="UDP") -> bytes:
    """Pass the INVITE's `branch` when ACKing a non-2xx; omit it for a 2xx ACK (new transaction)."""
    uri = f"sip:{to_number}@{domain}"
    return _request("ACK", uri, dialog, username, domain, local_ip, local_port, transport, uri,
                    to_tag=to_tag, branch=branch, routes=routes)


def build_bye(username, domain, to_number, local_ip, local_port, dialog, to_tag="",
              routes=(), transport="UDP") -> bytes:
    uri = f"sip:{to_number}@{domain}"
    return _request("BYE", uri, dialog, username, domain, local_ip, local_port, transport, uri,
                    to_tag=to_tag, routes=routes)


def build_cancel(username, domain, to_number, local_ip, local_port, dialog, branch,
                 transport="UDP") -> bytes:
    """`branch` and dialog.cseq must match the INVITE being cancelled (RFC 3261 9.1)."""
    uri = f"sip:{to_number}@{domain}"
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
