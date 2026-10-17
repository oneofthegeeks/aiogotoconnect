import hashlib

import pytest

from gotoconnect.exceptions import GoToSipError
from gotoconnect.sip.messages import (
    SipDialog,
    SipResponse,
    build_ack,
    build_bye,
    build_cancel,
    build_invite,
    build_register,
    digest_response,
)

RAW_407 = (
    b"SIP/2.0 407 Proxy Authentication Required\r\n"
    b"Via: SIP/2.0/UDP 10.0.0.5:5060;branch=z9hG4bKabc\r\n"
    b"To: <sip:+15551230000@sip.goto.com>;tag=srv1\r\n"
    b"Call-ID: abc@10.0.0.5\r\n"
    b'Proxy-Authenticate: Digest realm="goto.com", nonce="n0nce", algorithm=MD5\r\n'
    b"Content-Length: 0\r\n\r\n"
)

ARGS = dict(username="+15551230000", domain="sip.goto.com", local_ip="10.0.0.5", local_port=5060)


def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


def test_parse_407():
    r = SipResponse.parse(RAW_407)
    assert (r.status, r.reason) == (407, "Proxy Authentication Required")
    assert r.get("proxy-authenticate").startswith("Digest realm=")
    assert r.get("PROXY-AUTHENTICATE") == r.get("Proxy-Authenticate")
    assert r.get("X-Missing") is None
    assert r.body == ""


def test_parse_body_and_multi_header():
    raw = b"SIP/2.0 200 OK\r\nRecord-Route: <sip:a>\r\nRecord-Route: <sip:b>\r\nContent-Length: 3\r\n\r\nabc"
    r = SipResponse.parse(raw)
    assert r.get("Record-Route") == "<sip:a>, <sip:b>"
    assert r.body == "abc"


def test_parse_garbage():
    with pytest.raises(GoToSipError):
        SipResponse.parse(b"INVITE sip:x SIP/2.0\r\n\r\n")


def test_dialog_branch_unique():
    d = SipDialog()
    assert d.branch().startswith("z9hG4bK") and d.branch() != d.branch()
    assert d.call_id and d.from_tag and d.cseq == 1


def test_register():
    d = SipDialog()
    msg = build_register(**ARGS, dialog=d, auth_header="Proxy-Authorization: Digest x=1").decode()
    assert msg.startswith("REGISTER sip:sip.goto.com SIP/2.0\r\n")
    assert f"From: <sip:+15551230000@sip.goto.com>;tag={d.from_tag}\r\n" in msg
    assert "To: <sip:+15551230000@sip.goto.com>\r\n" in msg
    assert f"Call-ID: {d.call_id}\r\n" in msg
    assert "CSeq: 1 REGISTER\r\n" in msg
    assert "Expires: 60\r\n" in msg
    assert "Proxy-Authorization: Digest x=1\r\n" in msg
    assert msg.endswith("Content-Length: 0\r\n\r\n")


def test_invite_sdp_and_length():
    d = SipDialog()
    raw = build_invite(**ARGS, to_number="+15559876543", media_port=40000, dialog=d)
    head, body = raw.decode().split("\r\n\r\n")
    assert head.startswith("INVITE sip:+15559876543@sip.goto.com SIP/2.0\r\n")
    assert "To: <sip:+15559876543@sip.goto.com>\r\n" in head
    assert f"From: <sip:+15551230000@sip.goto.com>;tag={d.from_tag}\r\n" in head
    assert "Content-Type: application/sdp\r\n" in head
    assert f"Content-Length: {len(body.encode())}" in head
    for line in ("m=audio 40000 RTP/AVP 8 101", "a=rtpmap:8 PCMA/8000", "a=rtpmap:101 telephone-event/8000",
                 "c=IN IP4 10.0.0.5"):
        assert line in body.split("\r\n")


def test_ack_bye_cancel():
    d = SipDialog()
    kw = dict(**ARGS, to_number="+15559876543", dialog=d)
    ack = build_ack(**kw, to_tag="peer1", routes=["<sip:proxy;lr>"]).decode()
    assert ack.startswith("ACK sip:+15559876543@sip.goto.com SIP/2.0\r\n")
    assert "To: <sip:+15559876543@sip.goto.com>;tag=peer1\r\n" in ack
    assert "Route: <sip:proxy;lr>\r\n" in ack and "CSeq: 1 ACK\r\n" in ack
    d.cseq = 2
    assert "CSeq: 2 BYE\r\n" in build_bye(**kw, to_tag="peer1").decode()
    cancel = build_cancel(**kw, branch="z9hG4bKinv").decode()
    assert "CSeq: 2 CANCEL\r\n" in cancel and "branch=z9hG4bKinv" in cancel
    assert "tag=" not in cancel.split("To:")[1].split("\r\n")[0]


def test_digest_rfc2617_vector():
    ch = ('Digest realm="testrealm@host.com", qop="auth,auth-int", '
          'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", opaque="5ccc069c403ebaf9f0171e9517f40e41"')
    h = digest_response(ch, "GET", "/dir/index.html", "Mufasa", "Circle Of Life", cnonce="0a4f113b")
    assert 'response="6629fae49393a05397450978507c4ef1"' in h
    assert "qop=auth" in h and "nc=00000001" in h and 'cnonce="0a4f113b"' in h
    assert 'opaque="5ccc069c403ebaf9f0171e9517f40e41"' in h and h.startswith("Digest ")


def test_digest_no_qop():
    ch = 'Digest realm="goto.com", nonce="n0nce", algorithm=MD5'
    h = digest_response(ch, "INVITE", "sip:+1555@goto.com", "u", "p")
    expected = md5(f"{md5('u:goto.com:p')}:n0nce:{md5('INVITE:sip:+1555@goto.com')}")
    assert f'response="{expected}"' in h
    assert "qop" not in h and "cnonce" not in h
    assert 'username="u"' in h and 'uri="sip:+1555@goto.com"' in h


def test_digest_unsupported_algorithm():
    with pytest.raises(GoToSipError):
        digest_response('Digest realm="r", nonce="n", algorithm=SHA-256', "GET", "/", "u", "p")
