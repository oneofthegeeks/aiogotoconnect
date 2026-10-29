# aiogotoconnect

Async Python client for GoTo Connect: send SMS via the Messaging v2 API and place outbound text-to-speech voice calls over SIP. Pure Python, asyncio only.

Used by [ha-goto](https://github.com/oneofthegeeks/ha-goto), the Home Assistant integration.

Formerly published as `gotoconnect` (v0.1.0); renamed so the unofficial client does not carry the bare product name. The old PyPI name stays claimed but dormant.

## Install

```bash
pip install aiogotoconnect
```

Python 3.12+. For the web-app SMS package see [SMS for web apps](#sms-for-web-apps).

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

## SMS for web apps

`aiogotoconnect.sms` is a Python port of [`@oneofthegeeks/goto-sms`](https://github.com/oneofthegeeks/goto-sms): a guided setup page where an admin connects GoTo, picks the sending number and enables inbound texts, plus `send()` and an `on("message")` hook. It is a plain ASGI app with no web-framework dependency, and it follows the same HTTP contract and conformance checklist as the Node package ([C1-C25 map](docs/conformance.md)).

```bash
pip install 'aiogotoconnect[sms]'
```

The `[sms]` extra adds `cryptography`, which seals the stored secrets (AES-256-GCM, the same format as Node). `import aiogotoconnect` works without it; `create_goto_sms()` raises an `ImportError` naming the extra if it is missing.

### FastAPI quick start

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from aiogotoconnect.sms import create_goto_sms

def is_admin(req) -> bool:  # req has method, path, headers, cookies, query
    return req.cookies.get("session") == "..."  # your own check

sms = create_goto_sms(
    public_url="https://app.example.com",
    mount_path="/api/goto",
    authorize=is_admin,
    data_dir="./data",
)

async def on_text(message):
    print(message.from_, message.text)

sms.on("message", on_text)

@asynccontextmanager
async def lifespan(app):
    async with sms:  # start() ... close()
        yield

app = FastAPI(lifespan=lifespan)
app.mount("/api/goto", sms.asgi_app())
```

Open `/api/goto/` as an admin and follow the steps. Send from your own code with `await sms.send("+15551234567", "Hello", dedupe_key="order-42")` (returns a `SendResult`; `send_or_raise` raises `SendFailedError` instead).

`asgi_app()` works the same under Starlette, Django ASGI, Quart or anything else that can mount an ASGI 3 app at `mount_path`: mount `sms.asgi_app()` there and wrap the host's lifespan in `async with sms:`. `sms.webhook_app()` serves only `/inbound/:secret`, for hosts that keep the setup page elsewhere.

Things to know:

- **Authorization is required.** `asgi_app()` raises `RuntimeError` without `authorize`, unless you pass `allow_unauthenticated_setup=True` (refused when `GOTO_SMS_ENV=production`). The OAuth callback and the GoTo webhook are exempt from `authorize`.
- **Polling starts after `start()`.** Mounted sub-apps do not get ASGI lifespan events, so use the host's lifespan as above. If you forget, the ASGI app calls `start()` on its first request, but nothing polls (and nothing is replayed) until then. `async with sms` logs a startup failure and lets the app boot so the setup page can show the reason; `await sms.start()` raises it.
- **Handlers run one at a time with no timeout.** Sync and async handlers are called in order, per message; an exception is logged and the message stays processed. An async handler that hangs blocks processing of later texts, so do slow work in a background task.
- **One process per store.** The store holds a lock that each process re-claims every 30 seconds. A second process that cannot get it logs a warning (`Another process is using this goto-sms store`) and keeps going: nothing is gated on the lock, so it still polls, replays pending texts and sends. Two processes can therefore both deliver the same inbound text to their handlers. Run one process per store.
- **Webhooks are account-wide.** GoTo `INBOUND_MESSAGE` subscriptions cover the whole account: every app on it is sent texts addressed to every number. The package drops a text whose `to` is not the configured `from_number`. When GoTo creates the webhook channel it sends `OPTIONS` to the URL and needs a 2xx; `OPTIONS /inbound/:secret` answers 200 whenever the secret matches.
- **One event loop per instance.** An instance is bound to the event loop that first starts it. In a sync host, run one long-lived loop for it; don't wrap each call in its own `asyncio.run()`.
- **The mount warning compares ASGI `root_path`.** Behind a proxy that sets `--root-path`, the prefix differs from `mount_path`, so the "mounted at" warning may be logged once; it is harmless there.
- **Closed means closed.** After `close()`, `send()` no longer sends: it returns a result whose status is not `sent` (and before any start it raises `GoToSmsError` with code `closed`). Create a new instance instead of reusing a closed one.
- **Use `dedupe_key` for exactly-once.** A send without one that is cancelled mid-request may still go out, and sending again then texts twice.
- `GoToApiError` here is not the top-level `aiogotoconnect.GoToApiError`; import the SMS errors from `aiogotoconnect.sms`.

### Options

All keyword-only. Credentials fall back to the environment variables below; empty values and the literal `CHANGEME` count as unset.

| Option | Default | Notes |
|---|---|---|
| `public_url` | `GOTO_SMS_PUBLIC_URL` | Public origin; the OAuth redirect and webhook URL are built from it. Trailing slashes are trimmed. |
| `mount_path` | `"/goto-sms"` | Where you mount the app. `""` or `"/"` is the root. |
| `data_dir` | `"./data"` when no `store` is passed | Holds the default file store and the key file. With a custom `store` and no `data_dir` there is no key file, so set `encryption_key` or `GOTO_SMS_ENCRYPTION_KEY`. |
| `store` | file store in `data_dir` | `FileStore`, `SqliteStore` (table `goto_sms_kv`), `MemoryStore` or your own `GoToSmsStore`. |
| `tenant` | `"default"` | Namespaces keys in a shared store. |
| `authorize` | none | `(request) -> bool` or awaitable. |
| `allow_unauthenticated_setup` | `False` | Dev only. |
| `back_url`, `back_label` | none, `"the app"` | "Back" link on the setup page; absolute http(s) or a relative path. |
| `login_url` | none | Signed-out visitors to the page are redirected here. |
| `client_id`, `client_secret`, `account_key`, `from_number`, `refresh_token` | env | Preset credentials; otherwise saved by the setup page. |
| `encryption_key` | `GOTO_SMS_ENCRYPTION_KEY` | Else a random key file `<data_dir>/.goto-sms.key` (mode 0600). |
| `log_only` | `False` | Log texts instead of sending. Refused when `GOTO_SMS_ENV=production`. |
| `replies` | built-in | Dict with `stop`, `start`, `help` reply texts. |
| `yes_is_opt_in` | `True` | A `YES` text counts as `START`. |
| `poll_interval` | `30.0` | Seconds, polling mode only. |
| `send_timeout` | `10.0` | Seconds; a timeout after sending is `uncertain` and never retried. |
| `inbound_per_hour` | `10` | Per sender; extra texts are logged, not delivered. |
| `log_retention_days` | `90` | Message log retention. |
| `logger` | `logging.getLogger("aiogotoconnect.sms")` | Pass `False` to silence. Secrets and text bodies never appear at INFO or above. |
| `session`, `auth_base_url`, `api_base_url`, `env`, `now` | | Injection points for hosts and tests. |

### Environment variables

`GOTO_CLIENT_ID`, `GOTO_CLIENT_SECRET`, `GOTO_ACCOUNT_KEY`, `GOTO_FROM_NUMBER`, `GOTO_REFRESH_TOKEN`, `GOTO_SMS_ENCRYPTION_KEY`, `GOTO_SMS_PUBLIC_URL` (same names as the Node package), and `GOTO_SMS_ENV`. Setting `GOTO_SMS_ENV=production` refuses `log_only` at construction and `allow_unauthenticated_setup` without `authorize`.

A runnable demo with a browser e2e is in [`examples/fastapi-app`](examples/fastapi-app/README.md). The setup page is vendored from a goto-sms release (`scripts/update-setup-page.sh`).

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

Audio is sent as G.722 wideband when GoTo accepts it, else G.711 PCMA; pass `codecs=("pcmu",)` to force PCMU.

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

Bad arguments raise plain `ValueError` before any network traffic: an invalid destination number, a WAV that is unreadable, empty or has more than two channels, or an empty/unknown `codecs` entry (default `("g722", "pcma")`; the codec is picked from the server's answer).

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
