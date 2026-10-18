# aiogotoconnect

Live: v0.2.0 (2026-10-18) — renamed from gotoconnect (unofficial-client naming); same code as the live-smoked v0.1.0.

## What This Is
Async Python client for GoTo Connect: SMS via Messaging v2 and outbound TTS voice calls via SIP. Consumed by the ha-goto Home Assistant integration.

## Stack
Python >=3.12, aiohttp, audioop-lts on 3.13+, pure-asyncio SIP + RTP, pytest (pytest-asyncio, auto mode).

## Local Development
```bash
git clone git@github.com:oneofthegeeks/aiogotoconnect.git && cd aiogotoconnect
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -v -W error
```

## Testing
pytest: `.venv/bin/python -m pytest -W error`. All protocol tests run against a fake GoTo SIP server in `tests/sip/fake_goto.py` (UDP, or TLS with an ssl_context) - no network. Live check: `examples/make_call.py`.

## Deployment
Tag `vX.Y.Z` (admin-only via tag ruleset) -> GitHub Actions `publish.yml` -> the `pypi` environment PAUSES for Taylor's manual approval in the Actions UI -> PyPI trusted publishing. Main is PR-only (required checks: test 3.12 + 3.13); work lands via PRs Taylor merges. No Docker - it's a library.

## Key Files
`src/aiogotoconnect/`: `auth.py` (GoToOAuth, TokenProvider), `messaging.py` (GoToMessaging), `exceptions.py`, `sip/messages.py` (builders, digest), `sip/transport.py` (UDP/TLS), `sip/audio.py` (WAV -> G.711/G.722 payload), `sip/g722.py` (pure-Python G.722 encoder; decoder is test-only), `sip/rtp.py` (RtpSender), `sip/call.py` (GoToVoice, CallResult).

## Gotchas
- GoTo challenges with 407 (Proxy-Authenticate), not 401; 401 is handled too.
- The From user must be the SIP username, never the local IP.
- REGISTER and INVITE use distinct Call-IDs; the digest nonce is bound per method.
- `call_and_play(codecs=("g722", "pcma"))` is the default offer (one m= line, plus telephone-event/101); PCMU is opt-in via `codecs=("pcmu",)`. The payload is encoded after the 200 OK with the codec GoTo answered.
- G.722 rtpmap is `G722/8000` (RFC 3551 quirk: RTP clock is 8 kHz though sampling is 16 kHz), so `sip/rtp.py` is unchanged: 160 bytes / 160 ticks per 20 ms frame.
- `audioop` deprecation warning is suppressed at import on 3.12 (audioop-lts on 3.13+).
- REGISTER happens per call (no persistent registration).
- TLS defaults to verified system CAs; tests inject a self-signed context via `ssl_context`.
