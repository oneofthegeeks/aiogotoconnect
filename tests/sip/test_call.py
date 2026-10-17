import asyncio
import io
import wave

import pytest

from gotoconnect import CallResult, GoToAuthError, GoToVoice
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
    server = FakeGoToServer("user", "pw", getattr(request, "param", "answer"))
    host, port = await server.start()
    yield server, GoToVoice("user", "pw", server=host, port=port)
    await server.stop()


async def test_happy_path(fake):
    server, voice = fake
    result = await voice.call_and_play("+15551234567", _wav(0.5))
    assert result.answered and result.reason == "completed" and result.duration > 0.4
    assert server.registered and server.invite_authenticated and server.got_ack
    await _eventually(lambda: len(server.rtp_packets) >= 20)
    await _eventually(lambda: server.got_bye)


@pytest.mark.parametrize("fake", ["busy"], indirect=True)
async def test_busy(fake):
    result = await fake[1].call_and_play("+15551234567", _wav(0.5))
    assert result == CallResult(answered=False, duration=0.0, reason="busy")


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
