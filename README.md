# aiogotoconnect

Async Python client for GoTo Connect: send SMS via the Messaging v2 API and place outbound text-to-speech voice calls over SIP. Pure Python, asyncio only.

Used by [ha-goto](https://github.com/oneofthegeeks/ha-goto), the Home Assistant integration.

Formerly published as `gotoconnect` (v0.1.0); renamed so the unofficial client does not carry the bare product name. The old PyPI name stays claimed but dormant.

## Install

```bash
pip install aiogotoconnect
```

Python 3.12+.

## SMS

```python
import asyncio
from aiogotoconnect import GoToOAuth, GoToMessaging

async def main():
    auth = GoToOAuth(client_id="...", client_secret="...", refresh_token="...")
    sms = GoToMessaging(auth.async_get_access_token)
    await sms.async_send_sms("+15551230000", ["+15551234567"], "Hello from aiogotoconnect")
    # GoTo may rotate the refresh token: persist auth.refresh_token after calls.

asyncio.run(main())
```

`GoToMessaging` takes any async callable returning a bearer token, so a host app (e.g. Home Assistant) can supply its own.

## Voice calls

```python
import asyncio
from aiogotoconnect import GoToVoice

async def main():
    voice = GoToVoice("sip-username", "sip-password")  # server="reg.jiveip.net"
    with open("message.wav", "rb") as f:  # 16-bit PCM WAV
        result = await voice.call_and_play("+15551234567", f.read(), ring_timeout=25, repeat=1)
    print(result)  # CallResult(answered=True, duration=4.2, reason='completed')

asyncio.run(main())
```

`reason` is one of `completed`, `remote-hangup`, `no-answer`, `busy`, `rejected`. `bye_confirmed` is `False` only when we hung up but the server never answered (or refused) our BYE, so the far side may still think the call is up; it is `True` when no BYE was needed or it was confirmed. Bad SIP credentials raise `GoToAuthError`.

UDP requests are retransmitted (0.5 s, 1 s, 2 s, ...) until the server responds, and the Record-Route set from the answer is honoured on the ACK and BYE. Cancelling the `call_and_play` task hangs up (or CANCELs a ringing call) before the cancellation propagates.

### Logging

Everything logs to `logging.getLogger("aiogotoconnect.sip.call")` at DEBUG: SIP start-lines and status codes plus state changes, never headers or credentials. `examples/make_call.py --verbose` turns it on.

## Exceptions

All library errors derive from `GoToError`:

- `GoToAuthError`: credentials rejected (OAuth refresh failed, SIP digest refused, SMS 401).
- `GoToApiError`: a REST call returned a non-success status (`.status`).
- `GoToSipError`: SIP registration/signalling failed, including network failures (timeouts, DNS, TLS errors are wrapped, never raised raw).
  - `GoToCallError`: the call itself failed (e.g. 4xx/5xx to the INVITE, no usable SDP).

Bad arguments raise plain `ValueError` before any network traffic: an invalid destination number, a WAV that is unreadable, empty or has more than two channels, or an unknown `codec`.

### Create the SIP device in GoTo

1. In GoTo Connect Admin, go to **Devices** -> **Add Device** -> **SIP Device**.
2. Copy the **SIP Username**, **Password**, and the device's **SIP Server** and
   **Outbound Proxy**. The registrar is typically `reg.jiveip.net` (the library's
   default); the outbound proxy is account-specific (e.g.
   `<account>.jive.rtcfront.net`) — pass it as `proxy=`. Calls proven live against
   GoTo used both.

### Transport

UDP (port 5060) is the default. TLS (port 5061) is supported: `GoToVoice(..., transport="tls")`. TLS verifies the server certificate with the system CAs; pass `ssl_context=` to override. An outbound `proxy=` host, if set, is the connection target while `server` stays in the SIP URIs.

### Live smoke test

```bash
GOTO_SIP_USERNAME=... GOTO_SIP_PASSWORD=... python examples/make_call.py --to +15551234567 --wav message.wav --transport tls
```

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```
