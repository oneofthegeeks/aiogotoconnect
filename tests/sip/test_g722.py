import math
import struct

from aiogotoconnect.sip import g722


def _sine(freq=1000, rate=16000, seconds=0.5, amp=10000) -> bytes:
    """16-bit signed little-endian mono sine tone."""
    count = int(rate * seconds)
    return struct.pack(
        f"<{count}h", *(round(amp * math.sin(2 * math.pi * freq * n / rate)) for n in range(count))
    )


def _samples(pcm: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(pcm) // 2}h", pcm))


def test_encode_length():  # 0.5 s @ 16 kHz -> 4000 bytes (one per 2 samples)
    assert len(g722.encode(_sine())) == 4000


def test_odd_sample_dropped():  # 3 samples (6 bytes) -> 1 byte, no exception
    assert len(g722.encode(b"\x00\x01" * 3)) == 1


def test_roundtrip_snr():
    # decode(encode(sine)) must track the input: align by best lag within +/-128
    # samples (QMF delay), skip the first 40 ms (adaptation warmup), SNR > 20 dB.
    src = _samples(_sine())
    out = _samples(g722.decode(g722.encode(_sine())))
    assert len(out) == len(src)
    start, stop = 640, len(src) - 128  # 40 ms warmup; leave room for the lag window

    def snr(lag: int) -> float:
        sig = sum(src[n] ** 2 for n in range(start, stop))
        err = sum((src[n] - out[n + lag]) ** 2 for n in range(start, stop))
        return 10 * math.log10(sig / err) if err else math.inf

    assert max(snr(lag) for lag in range(-128, 129)) > 20


def test_silence_roundtrip():  # all-zero input decodes to near-silence
    out = _samples(g722.decode(g722.encode(b"\x00\x00" * 8000)))
    assert max(abs(s) for s in out) < 100


def test_deterministic():  # encode(x) == encode(x): no hidden global state
    pcm = _sine(freq=440)
    assert g722.encode(pcm) == g722.encode(pcm)
