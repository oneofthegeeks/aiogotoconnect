import asyncio
import io
import ssl
import subprocess
import wave

import pytest

from gotoconnect import CallResult, GoToAuthError, GoToSipError, GoToVoice
from gotoconnect.sip import call as call_module
from fake_goto import FakeGoToServer


def _wav(seconds: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x10\x00" * int(8000 * seconds))
    return buf.getvalue()


async def _eventually(cond, timeout=2.0):
    # RTP and SIP arrive on separate sockets, so cross-socket state settles slightly later.
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < deadline, "condition not met in time"
        await asyncio.sleep(0.02)


@pytest.fixture
async def fake(request):
    """param = behavior, or (behavior, FakeGoToServer kwargs)."""
    behavior, opts = getattr(request, "param", "answer"), {}
    if isinstance(behavior, tuple):
        behavior, opts = behavior
    server = FakeGoToServer("user", "pw", behavior, **opts)
    host, port = await server.start()
    yield server, GoToVoice("user", "pw", server=host, port=port)
    await server.stop()


def _errors(server):
    return [t for d, t in server.log if d == "error" and not t.startswith("dropped")]


async def test_happy_path(fake):
    server, voice = fake
    result = await voice.call_and_play("+15551234567", _wav(0.5))
    assert result.answered and result.reason == "completed" and result.duration > 0.4
    assert server.registered and server.invite_authenticated and server.got_ack
    assert result.bye_confirmed
    await _eventually(lambda: len(server.rtp_packets) >= 20)
    await _eventually(lambda: server.got_bye)


@pytest.mark.parametrize("fake", ["busy"], indirect=True)
async def test_busy(fake):
    result = await fake[1].call_and_play("+15551234567", _wav(0.5))
    assert result == CallResult(answered=False, duration=0.0, reason="busy")
    await _eventually(lambda: fake[0].got_ack)  # the ACK to the 486


@pytest.mark.parametrize("fake", ["no_answer"], indirect=True)
async def test_no_answer(fake):
    server, voice = fake
    result = await voice.call_and_play("+15551234567", _wav(0.5), ring_timeout=1.0)
    assert not result.answered and result.reason == "no-answer"
    assert server.got_cancel


@pytest.mark.parametrize("fake", ["hangup_during_audio"], indirect=True)
async def test_remote_hangup(fake):
    server, voice = fake
    result = await voice.call_and_play("+15551234567", _wav(5.0))
    assert result.answered and result.reason == "remote-hangup"
    assert server.sent_bye and result.duration < 4.0


@pytest.mark.parametrize("fake", ["reject_auth"], indirect=True)
async def test_bad_password(fake):
    server, voice = fake
    with pytest.raises(GoToAuthError):
        await voice.call_and_play("+15551234567", _wav(0.5))
    assert not server.registered
    assert sum(1 for d, t in server.log if d == "in" and t.startswith("REGISTER")) == 2


async def test_cancellation_mid_stream_hangs_up_and_leaves_no_tasks(fake):
    server, voice = fake
    before = asyncio.all_tasks()
    task = asyncio.create_task(voice.call_and_play("+15551234567", _wav(5.0)))
    await _eventually(lambda: len(server.rtp_packets) >= 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _eventually(lambda: server.got_bye)
    assert asyncio.all_tasks() - before == set()


async def test_pcmu_call_sends_payload_type_0(fake):
    server, voice = fake
    result = await voice.call_and_play("+15551234567", _wav(0.5), codec="pcmu")
    assert result.reason == "completed"
    assert server.client_sdp["codecs"][:1] == [0] and server.client_sdp["rtpmap"][0] == "PCMU/8000"
    await _eventually(lambda: len(server.rtp_packets) >= 20)
    assert all(p[1] & 0x7F == 0 for p in server.rtp_packets)


async def test_unknown_codec_rejected(fake):
    with pytest.raises(ValueError):
        await fake[1].call_and_play("+15551234567", _wav(0.5), codec="opus")


def _tls_contexts(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
         "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True,
    )
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)
    return server_ctx, ssl.create_default_context(cafile=str(cert))


async def test_happy_path_tls(tmp_path):
    server_ctx, client_ctx = _tls_contexts(tmp_path)
    server = FakeGoToServer("user", "pw", ssl_context=server_ctx)
    host, port = await server.start()
    try:
        voice = GoToVoice("user", "pw", server=host, port=port, transport="tls", ssl_context=client_ctx)
        result = await voice.call_and_play("+15551234567", _wav(0.5))
        assert result.answered and result.reason == "completed" and result.duration > 0.4
        assert server.registered and server.invite_authenticated and server.got_ack
        assert "Via: SIP/2.0/TLS" in server.log[0][1]
        await _eventually(lambda: len(server.rtp_packets) >= 20)
        await _eventually(lambda: server.got_bye)
    finally:
        await server.stop()


async def test_remote_hangup_tls(tmp_path):
    server_ctx, client_ctx = _tls_contexts(tmp_path)
    server = FakeGoToServer("user", "pw", "hangup_during_audio", ssl_context=server_ctx)
    host, port = await server.start()
    try:
        voice = GoToVoice("user", "pw", server=host, port=port, transport="tls", ssl_context=client_ctx)
        result = await voice.call_and_play("+15551234567", _wav(5.0))
        assert result.reason == "remote-hangup" and server.sent_bye and result.duration < 4.0
        # Contact advertises TLS so GoTo sends its BYE down our connection, not over UDP
        assert any(t.startswith("INVITE") and ";transport=tls>" in t for d, t in server.log if d == "in")
    finally:
        await server.stop()



# ---- review fix wave --------------------------------------------------------------------------

@pytest.mark.parametrize("fake", ["no_answer"], indirect=True)
async def test_cancel_during_ring_sends_cancel(fake):
    server, voice = fake
    task = asyncio.create_task(voice.call_and_play("+15551234567", _wav(0.5)))
    await _eventually(lambda: server.invite_authenticated)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _eventually(lambda: server.got_cancel)


@pytest.mark.parametrize("fake", ["answer_after_cancel"], indirect=True)
async def test_200_racing_the_cancel_is_acked_and_hung_up(fake):
    server, voice = fake
    result = await voice.call_and_play("+15551234567", _wav(0.5), ring_timeout=0.5)
    assert (result.answered, result.reason) == (False, "no-answer")
    assert server.got_cancel and server.got_ack and server.got_bye and result.bye_confirmed
    assert _errors(server) == []


@pytest.mark.parametrize("fake", [("answer", {"record_route": ["<sip:p1.test;lr>", "<sip:p2.test;lr>"]})],
                         indirect=True)
async def test_route_set_on_ack_and_bye(fake):
    server, voice = fake
    result = await voice.call_and_play("+15551234567", _wav(0.3))
    assert result.reason == "completed" and result.bye_confirmed
    assert server.got_ack and server.got_bye  # the fake demands Route: p2, p1 on both
    assert _errors(server) == []


@pytest.mark.parametrize("fake", [("answer", {"bye_reply": 481})], indirect=True)
async def test_refused_bye_is_reported(fake, caplog):
    caplog.set_level("WARNING", logger="gotoconnect")
    result = await fake[1].call_and_play("+15551234567", _wav(0.3))
    assert result.reason == "completed" and not result.bye_confirmed
    assert "BYE refused" in caplog.text


@pytest.mark.parametrize("fake", [("answer", {"bye_reply": None})], indirect=True)
async def test_unanswered_bye_is_reported(fake, monkeypatch):
    monkeypatch.setattr(call_module, "_REPLY_TIMEOUT", 0.3)
    result = await fake[1].call_and_play("+15551234567", _wav(0.3))
    assert result.reason == "completed" and not result.bye_confirmed


@pytest.mark.parametrize("fake", [("answer", {"drop_first": 1})], indirect=True)
async def test_dropped_first_register_is_retransmitted(fake):
    server, voice = fake
    result = await voice.call_and_play("+15551234567", _wav(0.3))
    assert result.reason == "completed" and server.registered


@pytest.mark.parametrize("fake", [("answer", {"resend_200": True})], indirect=True)
async def test_retransmitted_200_is_acked_again(fake):
    server, voice = fake
    result = await voice.call_and_play("+15551234567", _wav(1.0))
    assert result.reason == "completed"
    assert server.ack_count >= 2


@pytest.mark.parametrize("fake", [("answer", {"drop_first": 1000})], indirect=True)
async def test_silent_server_raises_sip_error(fake, monkeypatch):
    monkeypatch.setattr(call_module, "_REPLY_TIMEOUT", 0.3)
    with pytest.raises(GoToSipError):
        await fake[1].call_and_play("+15551234567", _wav(0.3))


async def test_unresolvable_host_raises_sip_error():
    voice = GoToVoice("user", "pw", server="no-such-host.invalid")
    with pytest.raises(GoToSipError):
        await voice.call_and_play("+15551234567", _wav(0.3))


@pytest.mark.parametrize("fake", [("answer", {"challenge_401": True})], indirect=True)
async def test_401_challenge_uses_authorization_header(fake):
    server, voice = fake
    result = await voice.call_and_play("+15551234567", _wav(0.3))
    assert result.reason == "completed" and server.registered
    assert any(t.startswith("INVITE") and "\r\nAuthorization: Digest" in t for d, t in server.log if d == "in")


@pytest.mark.parametrize("fake", ["invite_unauth"], indirect=True)
async def test_invite_challenged_forever_raises_auth_error(fake):
    server, voice = fake
    with pytest.raises(GoToAuthError):
        await voice.call_and_play("+15551234567", _wav(0.3))
    assert server.registered
    await _eventually(lambda: server.challenge_acks == 2)  # both challenges were ACKed


async def test_debug_logs_show_start_lines_only(fake, caplog):
    caplog.set_level("DEBUG", logger="gotoconnect")
    await fake[1].call_and_play("+15551234567", _wav(0.3))
    assert "SIP -> REGISTER" in caplog.text and "SIP <- SIP/2.0 407" in caplog.text
    assert "registered" in caplog.text and "answered" in caplog.text
    assert not any(w in caplog.text for w in ("Digest", "nonce", "response=", "Authorization"))
