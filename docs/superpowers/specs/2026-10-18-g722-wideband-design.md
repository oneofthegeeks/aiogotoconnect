# G.722 Wideband Codec + SDP Negotiation — Design

Approved by Taylor in chat, 2026-10-18.

## Intent

TTS calls sound like an 8 kHz phone line because the library offers exactly one codec
(G.711 PCMA) in its SDP and downsamples Home Assistant's 16–24 kHz TTS output to 8 kHz.
GoTo Connect supports G.722 ("HD voice"). Add a G.722 encoder and real offer/answer
negotiation so calls use wideband whenever GoTo accepts it.

## Goals

- TTS calls negotiate G.722 (16 kHz audio) when GoTo's answer accepts it.
- Safe fallback by construction: if GoTo picks PCMA/PCMU or rejects G.722, behavior is
  exactly today's.
- No new runtime dependency (must stay installable inside the Home Assistant container).
- ha-goto upgrades automatically via a version-pin bump — no new HA config.

## Non-goals / anti-goals

- No Opus (needs libopus native bindings — rejected).
- No receive path / decode-for-playback; calls remain send-only TTS. (A G.722 decoder may
  exist for tests.)
- No persistent registration or other SIP behavior changes.

## Decided how

1. **Encoder**: pure-Python port of the ITU-T G.722 reference encoder, 64 kbit/s mode,
   in a new `sip/g722.py`. Input 16-bit mono 16 kHz PCM, output one byte per two samples.
2. **Audio pipeline** (`sip/audio.py`): resample TTS WAV to the codec's rate
   (16 kHz for g722, 8 kHz for g711) before encoding.
3. **SDP offer** (`sip/messages.py`): offer multiple codecs in one m= line, default
   `g722 pcma` → `m=audio PORT RTP/AVP 9 8 101`. The G.722 rtpmap is `G722/8000` —
   the 8000 is the RFC 3551 historical quirk, not a bug.
4. **Answer parsing** (`sip/call.py`): read the payload types in GoTo's 200 OK answer,
   encode with the first codec GoTo accepted. Encoding moves from before the INVITE to
   after the answer; WAV validation stays upfront (fail before touching the network).
5. **RTP** (`sip/rtp.py`): unchanged. G.722's RTP clock is 8 kHz by spec, so it is the
   same 160 bytes / 160 timestamp ticks per 20 ms frame as G.711.
6. Lib `0.2.0 → 0.3.0` (breaking: `codec: str` param becomes `codecs` sequence), PyPI
   release via the usual tag flow, then ha-goto bumps its pin.

## Left to the builder

- Internal structure of the G.722 port (band state classes, table layout).
- Test strategy for the encoder (no official vectors in-repo; round-trip SNR against a
  ported decoder is acceptable).

## Known unknowns

- Whether GoTo answers G.722 for these softphone registrations — settled by the live
  smoke call (`examples/make_call.py`) before release.
- If the callee is a PSTN cell phone, the carrier leg may cap quality at 8 kHz anyway.
