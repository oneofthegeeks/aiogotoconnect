# gotoconnect

Live: v0.1.0 (2026-10-17) — first PyPI release; live-smoked against GoTo (UDP; registrar reg.jiveip.net + per-account proxy).

## What This Is
Async Python client for GoTo Connect: SMS via Messaging v2 and outbound TTS voice calls via SIP. Consumed by the ha-goto Home Assistant integration.

## Stack
Python >=3.12, aiohttp, audioop-lts on 3.13+, pure-asyncio SIP + RTP, pytest (pytest-asyncio, auto mode).

## Local Development
```bash
git clone git@github.com:oneofthegeeks/gotoconnect.git && cd gotoconnect
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -v -W error
```

## Testing
pytest: `.venv/bin/python -m pytest -W error`. All protocol tests run against a fake GoTo SIP server in `tests/sip/fake_goto.py` (UDP, or TLS with an ssl_context) - no network. Live check: `examples/make_call.py`.

## Deployment
Tag `vX.Y.Z` -> GitHub Actions `publish.yml` -> PyPI trusted publishing. No Docker - it's a library.

## Key Files
`src/gotoconnect/`: `auth.py` (GoToOAuth, TokenProvider), `messaging.py` (GoToMessaging), `exceptions.py`, `sip/messages.py` (builders, digest), `sip/transport.py` (UDP/TLS), `sip/audio.py` (WAV -> G.711), `sip/rtp.py` (RtpSender), `sip/call.py` (GoToVoice, CallResult).

## Gotchas
- GoTo challenges with 407 (Proxy-Authenticate), not 401; 401 is handled too.
- The From user must be the SIP username, never the local IP.
- REGISTER and INVITE use distinct Call-IDs; the digest nonce is bound per method.
- PCMA/8000 is the default codec (PCMU opt-in); SDP also offers telephone-event/101.
- `audioop` deprecation warning is suppressed at import on 3.12 (audioop-lts on 3.13+).
- REGISTER happens per call (no persistent registration).
- TLS defaults to verified system CAs; tests inject a self-signed context via `ssl_context`.
