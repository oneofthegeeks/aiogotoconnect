# gotoconnect

Async Python client for GoTo Connect: send SMS via the Messaging v2 API and place outbound text-to-speech voice calls over SIP. Pure Python, asyncio only.

Used by [ha-goto](https://github.com/oneofthegeeks/ha-goto), the Home Assistant integration.

## Install

```bash
pip install gotoconnect
```

Python 3.12+.

## SMS

```python
import asyncio
from gotoconnect import GoToOAuth, GoToMessaging

async def main():
    auth = GoToOAuth(client_id="...", client_secret="...", refresh_token="...")
    sms = GoToMessaging(auth.async_get_access_token)
    await sms.async_send_sms("+15551230000", ["+15551234567"], "Hello from gotoconnect")
    # GoTo may rotate the refresh token: persist auth.refresh_token after calls.

asyncio.run(main())
```

`GoToMessaging` takes any async callable returning a bearer token, so a host app (e.g. Home Assistant) can supply its own.

## Voice calls

```python
import asyncio
from gotoconnect import GoToVoice

async def main():
    voice = GoToVoice("sip-username", "sip-password")  # server="sip.goto.com"
    with open("message.wav", "rb") as f:  # 16-bit PCM WAV
        result = await voice.call_and_play("+15551234567", f.read(), ring_timeout=25, repeat=1)
    print(result)  # CallResult(answered=True, duration=4.2, reason='completed')

asyncio.run(main())
```

`reason` is one of `completed`, `remote-hangup`, `no-answer`, `busy`, `rejected`. Bad SIP credentials raise `GoToAuthError`.

### Create the SIP device in GoTo

1. In GoTo Connect Admin, go to **Devices** -> **Add Device** -> **SIP Device**.
2. Copy the **SIP Username**, **Password**, and **SIP Server** (e.g. `sip.goto.com`).

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
