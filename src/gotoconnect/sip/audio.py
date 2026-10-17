"""WAV to raw G.711 conversion for the RTP sender.

Input is any PCM WAV (Home Assistant TTS output). Output is headerless a-law
(PCMA, payload type 8) or mu-law (PCMU) bytes at 8 kHz mono, one byte per sample.
"""

import io
import warnings
import wave

# ponytail: audioop is deprecated on 3.12 (PEP 594) and removed on 3.13, where
# the audioop-lts dependency provides the same module name. Silence only this
# import-time DeprecationWarning, so the rest of the suite still fails on warnings.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop

TARGET_RATE = 8000
CODECS = {"pcma": audioop.lin2alaw, "pcmu": audioop.lin2ulaw}


def wav_to_g711(wav_bytes: bytes, codec: str = "pcma") -> bytes:
    """Convert WAV bytes to 8 kHz mono G.711 (`pcma` or `pcmu`) payload bytes."""
    encode = CODECS.get(codec)
    if encode is None:
        raise ValueError(f"unknown codec {codec!r}; expected 'pcma' or 'pcmu'")
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"input is not a readable PCM WAV: {exc}") from exc

    if channels > 2:
        # ponytail: stereo downmix only, multichannel needs a per-channel mixer.
        raise ValueError(f"unsupported channel count: {channels}")
    if width == 1:
        # WAV 8-bit PCM is unsigned; audioop expects signed samples.
        frames = audioop.bias(frames, 1, 128)
    if width != 2:
        frames = audioop.lin2lin(frames, width, 2)
    if channels == 2:
        frames = audioop.tomono(frames, 2, 0.5, 0.5)
    if rate != TARGET_RATE:
        frames, _ = audioop.ratecv(frames, 2, 1, rate, TARGET_RATE, None)
    return encode(frames, 2)
