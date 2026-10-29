# aiogotoconnect

Live: v0.3.1 (2026-10-18) — G.722 wideband + SDP answer negotiation (0.3.0, breaking `codec=` → `codecs=`) plus call-quality polish: pre-encode during ring, 0.3 s tail silence. GoTo answers G.722 (live-proven).

## What This Is
Async Python client for GoTo Connect: SMS via Messaging v2 and outbound TTS voice calls via SIP. Consumed by the ha-goto Home Assistant integration.

## Stack
Python >=3.12, aiohttp, audioop-lts on 3.13+, pure-asyncio SIP + RTP, pytest (pytest-asyncio, auto mode). `aiogotoconnect.sms` (0.4.0): framework-free ASGI app, optional `[sms]` extra (`cryptography>=41`), stdlib sqlite3; ruff + mypy --strict scoped to `src/aiogotoconnect/sms`; e2e uses uvicorn + pytest-playwright.

## Local Development
```bash
git clone git@github.com:oneofthegeeks/aiogotoconnect.git && cd aiogotoconnect
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -v -W error
```

## Testing
pytest: `.venv/bin/python -m pytest -W error`. All protocol tests run against a fake GoTo SIP server in `tests/sip/fake_goto.py` (UDP, or TLS with an ssl_context) - no network. Live check: `examples/make_call.py`.

SMS package: unit tests in `tests/sms/` (aiohttp fake GoTo in `tests/sms/fake_goto.py`, httpx ASGI transport); `docs/conformance.md` maps C1-C25 to test names, so keep it in sync when renaming tests. Browser e2e: `.venv/bin/python -m pytest e2e` (needs `playwright install chromium`). Lint/types: `.venv/bin/ruff check src/aiogotoconnect/sms tests/sms && .venv/bin/ruff format --check src/aiogotoconnect/sms tests/sms && .venv/bin/mypy (scoped; the older modules are not linted)`.

## Deployment
Tag `vX.Y.Z` (admin-only via tag ruleset) -> GitHub Actions `publish.yml` -> the `pypi` environment PAUSES for Taylor's manual approval in the Actions UI -> PyPI trusted publishing. Main is PR-only (required checks: test 3.12 + 3.13); work lands via PRs Taylor merges. No Docker - it's a library.

## Key Files
`src/aiogotoconnect/`: `auth.py` (GoToOAuth, TokenProvider), `messaging.py` (GoToMessaging), `exceptions.py`, `sip/messages.py` (builders, digest), `sip/transport.py` (UDP/TLS), `sip/audio.py` (WAV -> G.711/G.722 payload), `sip/g722.py` (pure-Python G.722 encoder; decoder is test-only), `sip/rtp.py` (RtpSender), `sip/call.py` (GoToVoice, CallResult).

`src/aiogotoconnect/sms/` (one module per goto-sms Node file): `client.py` (`create_goto_sms`, `GoToSms` lifecycle), `core.py` (resolved options, `AuthorizeRequest`), `oauth.py`, `directory.py`, `send.py`, `optouts.py`, `log.py`, `secrets.py` (AES-GCM), `inbound/` (`webhook.py`, `poller.py`, `processor.py`, `keywords.py`), `server/` (`handlers.py` framework-free, `asgi.py` adapter, `status.py`), `store/` (file, memory, sqlite, `conformance.py` shared store tests), `setup/` (vendored page + `SETUP_UI_VERSION`), `testing.py`. Also `docs/conformance.md`, `examples/fastapi-app/`, `e2e/`, `scripts/update-setup-page.sh`.

## Gotchas
- GoTo challenges with 407 (Proxy-Authenticate), not 401; 401 is handled too.
- The From user must be the SIP username, never the local IP.
- REGISTER and INVITE use distinct Call-IDs; the digest nonce is bound per method.
- `call_and_play(codecs=("g722", "pcma"))` is the default offer (one m= line, plus telephone-event/101); PCMU is opt-in via `codecs=("pcmu",)`. The preferred codec is pre-encoded while ringing (re-encoded only if the answer differs), and 0.3 s of trailing silence keeps the BYE from clipping the last word.
- G.722 rtpmap is `G722/8000` (RFC 3551 quirk: RTP clock is 8 kHz though sampling is 16 kHz), so `sip/rtp.py` is unchanged: 160 bytes / 160 ticks per 20 ms frame.
- `audioop` deprecation warning is suppressed at import on 3.12 (audioop-lts on 3.13+).
- REGISTER happens per call (no persistent registration).
- TLS defaults to verified system CAs; tests inject a self-signed context via `ssl_context`.
- SMS: `aiogotoconnect.sms` must not import `cryptography` at import time (optional extra); `create_goto_sms` checks for it. Its `GoToApiError` is not re-exported at the top level (name clash with `aiogotoconnect.GoToApiError`).
- SMS setup page is vendored (byte-for-byte) from a goto-sms release: update with `scripts/update-setup-page.sh vX.Y.Z`, never hand-edit `sms/setup/`.
- `GOTO_SMS_ENV=production` refuses `log_only` at construction and `allow_unauthenticated_setup` without `authorize` (checked at `asgi_app()`). Node behaviour wins when this port and Node disagree.
