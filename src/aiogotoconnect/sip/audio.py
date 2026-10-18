"""WAV to raw RTP payload conversion (G.711 or G.722) for the RTP sender.

Input is any PCM WAV (Home Assistant TTS output). Output is headerless mono
a-law (PCMA), mu-law (PCMU) at 8 kHz, or G.722 at 16 kHz.
"""

import io
import warnings
import wave

from aiogotoconnect.sip import g722

# ponytail: audioop is deprecated on 3.12 (PEP 594) and removed on 3.13, where
# the audioop-lts dependency provides the same module name. Silence only this
# import-time DeprecationWarning, so the rest of the suite still fails on warnings.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop

# codec -> (target sample rate, PCM encoder)
CODECS = {
    "pcma": (8000, lambda pcm: audioop.lin2alaw(pcm, 2)),
    "pcmu": (8000, lambda pcm: audioop.lin2ulaw(pcm, 2)),
    "g722": (16000, g722.encode),
}


def _codec(codec: str):
    try:
        return CODECS[codec]
    except KeyError:
        raise ValueError(f"unknown codec {codec!r}; expected one of {', '.join(CODECS)}") from None


def wav_pcm(wav_bytes: bytes) -> tuple[bytes, int]:
    """Decode WAV bytes to 16-bit mono PCM; returns (pcm, native sample rate)."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"input is not a readable PCM WAV: {exc}") from exc

    if not frames:
        raise ValueError("WAV contains no audio frames")  # would otherwise 'play' a silent call
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
    return frames, rate


def pcm_to_payload(pcm: bytes, rate: int, codec: str) -> bytes:
    """Resample 16-bit mono PCM to the codec's rate and encode it."""
    target, encode = _codec(codec)
    if rate != target:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, target, None)
    return encode(pcm)


def wav_to_payload(wav_bytes: bytes, codec: str = "pcma") -> bytes:
    """Convert WAV bytes to mono payload bytes for `codec` (pcma, pcmu or g722)."""
    _codec(codec)  # fail on the codec before parsing, as before
    pcm, rate = wav_pcm(wav_bytes)
    return pcm_to_payload(pcm, rate, codec)
