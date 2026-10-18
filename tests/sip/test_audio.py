import io
import math
import struct
import wave

import pytest

from aiogotoconnect.sip.audio import wav_pcm, wav_to_payload


def make_wav(rate: int, channels: int, width: int, seconds: float = 1.0) -> bytes:
    """Build a 440 Hz tone WAV in memory. 8-bit WAV is unsigned, 16-bit is signed."""
    frame_count = int(rate * seconds)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        frames = bytearray()
        for n in range(frame_count):
            tone = math.sin(2 * math.pi * 440 * n / rate)
            for _ in range(channels):
                if width == 1:
                    frames += struct.pack("<B", 128 + int(100 * tone))
                elif width == 3:
                    frames += int(12000 * 256 * tone).to_bytes(3, "little", signed=True)
                else:
                    frames += struct.pack("<h", int(12000 * tone))
        wf.writeframes(bytes(frames))
    return buf.getvalue()


@pytest.mark.parametrize(
    "rate, channels, width",
    [(44100, 2, 2), (8000, 1, 2), (22050, 1, 1), (16000, 1, 3)],
)
def test_output_length_is_one_byte_per_8khz_sample(rate, channels, width):
    out = wav_to_payload(make_wav(rate, channels, width, seconds=1.0), "pcma")
    assert abs(len(out) - 8000) <= 80  # within 1% of 1 s at 8 kHz mono


def test_pcma_and_pcmu_encode_differently():
    wav = make_wav(8000, 1, 2)
    assert wav_to_payload(wav, "pcma") != wav_to_payload(wav, "pcmu")


def test_silence_encodes_to_codec_zero_codes():
    # Standard codes: A-law zero is 0xD5, mu-law zero is 0xFF.
    silent_frames = b"\x00\x00" * 8000
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(silent_frames)
    silent_wav = buf.getvalue()
    assert set(wav_to_payload(silent_wav, "pcma")) == {0xD5}
    assert set(wav_to_payload(silent_wav, "pcmu")) == {0xFF}


def test_default_codec_is_pcma():
    wav = make_wav(8000, 1, 2)
    assert wav_to_payload(wav) == wav_to_payload(wav, "pcma")


def test_non_wav_bytes_raise_value_error():
    with pytest.raises(ValueError):
        wav_to_payload(b"this is not a wav file", "pcma")


def test_unknown_codec_raises_value_error():
    with pytest.raises(ValueError):
        wav_to_payload(make_wav(8000, 1, 2), "opus")


def test_8bit_silence_is_unsigned_midpoint_so_encodes_to_alaw_zero():
    # 8-bit WAV silence is 0x80 (unsigned); a missing bias would encode loud DC instead of 0xD5.
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(8000)
        wf.writeframes(b"\x80" * 8000)
    assert set(wav_to_payload(buf.getvalue(), "pcma")) == {0xD5}


def test_zero_frame_wav_raises_value_error():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
    with pytest.raises(ValueError):
        wav_to_payload(buf.getvalue())


def test_g722_payload_rate():
    out = wav_to_payload(make_wav(16000, 1, 2), "g722")
    assert abs(len(out) - 8000) <= 80  # one byte per 2 samples @ 16 kHz


def test_g722_resamples_hifi():
    out = wav_to_payload(make_wav(44100, 2, 2), "g722")
    assert abs(len(out) - 8000) <= 80


def test_unknown_codec_lists_g722():
    with pytest.raises(ValueError, match="g722"):
        wav_to_payload(make_wav(8000, 1, 2), "opus")


def test_wav_pcm_validates():
    with pytest.raises(ValueError):
        wav_pcm(b"garbage")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
    with pytest.raises(ValueError):
        wav_pcm(buf.getvalue())
