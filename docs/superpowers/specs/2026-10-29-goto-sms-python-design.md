# goto-sms for Python (aiogotoconnect 0.4.0): design

Date: 2026-10-29. Status: draft for Taylor's approval.

## Intent

`@oneofthegeeks/goto-sms` (npm, repo `oneofthegeeks/goto-sms`) lets a Node developer add GoTo
Connect SMS to an app in minutes. Their end user pastes a client ID and secret into a guided setup
page, clicks Connect, picks an account and number, and sends. Nobody has to learn GoTo's OAuth or
webhook quirks. The family design (`goto-sms` repo, `docs/superpowers/specs/2026-10-27-goto-sms-design.md`)
always planned Python next, by extending this package.

This project is that Python version, for **the same people as the Node version, who prefer Python**
(Taylor). **Match Node exactly** (Taylor): the same features, the same setup page, the same HTTP
contract and the same behaviours. Python naming conventions apply, but the behaviour does not change.
A Python developer and a Node developer who each follow their README should end up with apps whose
end users can't tell them apart.

## Goals

1. A high-level API in aiogotoconnect, `create_goto_sms(...)`, that mirrors `createGoToSms` option
   for option and method for method (mapping below).
2. The setup page, its JSON API, the OAuth callback and the inbound webhook are served as one
   **ASGI app** that the host mounts. It implements `contract/http.md` from the goto-sms repo
   exactly.
3. The **same setup page file**, taken byte-for-byte from a goto-sms GitHub release
   (`goto-sms-setup-ui-vX.Y.Z.zip`), not rebuilt or forked.
4. Every row of the conformance checklist (`contract/conformance.md` in goto-sms, C1–C23, plus the
   two rows added below) has a pytest test against a Python fake GoTo. Nothing touches real GoTo.
5. A runnable `examples/fastapi-app`, with a browser (Playwright) test that matches the Node e2e.
6. Released as aiogotoconnect **0.4.0** on PyPI through the existing tag → approval → trusted
   publishing workflow.

## Non-goals (v1)

- Sync/WSGI apps (Flask, classic Django views) and a blocking `send()`. They come later if someone
  asks, through an adapter plus a sync wrapper.
- Framework-specific integrations beyond mounting the ASGI app. FastAPI gets the docs and the
  example; Starlette, Litestar, Quart and Django-under-ASGI get a short README recipe each, or just
  the FastAPI one where it reads the same.
- Anything Node v1 also excludes: multi-tenant beyond the `tenant` key, several processes sharing
  one store, MMS, group texts.
- Sharing one store or database file between a Node app and a Python app.
- Changes to voice/SIP.

## Anti-goals (failure even if everything new works)

- **Breaking ha-goto.** `GoToOAuth`, `GoToMessaging`, `GoToVoice`, the exception classes and their
  behaviour stay as they are. Their existing tests pass unchanged. Before tagging, run ha-goto's test
  suite against the 0.4.0 build.
- Python behaving differently from Node anywhere the contract or checklist covers. When in doubt,
  read the Node code (`packages/node/src` in goto-sms); it is the reference implementation.
- Reporting a text as sent when it wasn't, or replying to a text that was sent to a different number
  in the same GoTo account (see "Lessons from production").
- A secret in a log line, an API response, a test fixture, or a committed file.

## Lessons from production that must carry over

Both behaviours shipped in goto-sms after the checklist was written. They become checklist rows:

- **C24: OPTIONS check.** When GoTo creates a webhook channel, it sends `OPTIONS` to the webhook URL
  and requires a 2xx. `OPTIONS /inbound/:secret` answers 200 when the secret matches, even before
  webhook mode is on. Node test: `OPTIONS /inbound/:secret answers 200 before webhook mode is on`.
- **C25: other-number drop.** GoTo INBOUND_MESSAGE subscriptions are **account-wide**: every app on
  the account receives texts sent to every number. The inbound handler drops a message when its
  `to` is known and differs from the configured `from_number` (204, debug log). Polling is already
  scoped by owner number. Node test: `inbound: a text to another number in the same GoTo account is
  ignored`.

Part of this project is a small goto-sms PR that adds C24 and C25 to `contract/conformance.md`.
`contract/http.md` already documents both.

## Decisions (Taylor's call: "match it exactly, go with your picks")

Taylor decided the scope (Node parity, for Python developers, inside aiogotoconnect) and handed
every how-it-gets-made choice to Claude. The choices below are therefore **Claude's calls**,
reviewed by Taylor in chat:

- **Framework-free ASGI core.** The package implements the contract as a plain ASGI 3 app, with no
  Starlette, FastAPI or other web-framework dependency. This mirrors Node's framework-free
  `(req, res)` handlers. FastAPI is the documented main path:
  `app.mount("/api/goto", sms.asgi_app())`.
- **Async only.** `await sms.send(...)`, async message handlers. This matches the existing package.
- **Runtime dependencies:** `aiohttp` (existing; HTTP client). **`cryptography`** (AES-GCM for
  secrets at rest, since the standard library has no AES) is an **optional extra**:
  `pip install aiogotoconnect[sms]`. ha-goto runs inside Home Assistant, which pins its own
  `cryptography`, and it never uses this feature, so it must not inherit a new hard requirement.
  `create_goto_sms` raises a clear "install aiogotoconnect[sms]" error when the extra is missing;
  `import aiogotoconnect` never needs it.
- **Python >= 3.12**, CI on 3.12 and 3.13, unchanged.
- **Stores:** `MemoryStore`, `FileStore` and `SqliteStore`, built on the standard library's
  `sqlite3`. `SqliteStore` takes a path or an existing `sqlite3.Connection`, plus `table=`. The
  custom-store protocol is the same as Node's.
- **Version 0.4.0** (new features; the existing API stays the same).

## API mapping (Node → Python)

Names are converted to snake_case. Behaviour, defaults and validation are identical unless noted.

| Node | Python |
|---|---|
| `createGoToSms(options)` | `create_goto_sms(**options)` → `GoToSms`, exported from `aiogotoconnect` |
| `publicUrl, mountPath ('/goto-sms'), dataDir, store, tenant` | `public_url, mount_path, data_dir, store, tenant` |
| `authorize, allowUnauthenticatedSetup` | `authorize, allow_unauthenticated_setup` |
| `backUrl, backLabel, loginUrl` | `back_url, back_label, login_url` (same validation: absolute http(s) or same-origin relative; scheme-relative and the literal `back` refused) |
| `clientId, clientSecret, accountKey, fromNumber, refreshToken, encryptionKey` | same in snake_case. Env fallbacks keep the **same env var names**: `GOTO_CLIENT_ID`, `GOTO_CLIENT_SECRET`, `GOTO_ACCOUNT_KEY`, `GOTO_FROM_NUMBER`, `GOTO_REFRESH_TOKEN`, `GOTO_SMS_ENCRYPTION_KEY`, `GOTO_SMS_PUBLIC_URL`. Empty values and the literal `CHANGEME` count as unset, as in Node. |
| `logOnly` and `allowUnauthenticatedSetup` (both refused when `NODE_ENV=production`) | `log_only` and `allow_unauthenticated_setup`, both refused when `GOTO_SMS_ENV=production` *(Claude's call: Python has no universal production flag)* |
| `replies, yesIsOptIn, inboundPerHour, logRetentionDays` | `replies, yes_is_opt_in, inbound_per_hour, log_retention_days` |
| `pollIntervalMs, sendTimeoutMs` | `poll_interval, send_timeout` in **seconds** (float) *(Claude's call: Python convention; same defaults)* |
| `logger` (Logger \| false) | `logger: logging.Logger \| False`, default `logging.getLogger("aiogotoconnect.sms")` |
| `fetch` | `session: aiohttp.ClientSession \| None` (tests inject one pointed at the fake) |
| `authBaseUrl, apiBaseUrl, env, now` | `auth_base_url, api_base_url, env (Mapping), now` |
| `send(to, text, {dedupeKey})` → `SendResult` | `await send(to, text, dedupe_key=None)` → `SendResult` dataclass (`status, reason, retryable, message_id, to, deduped`) |
| `sendOrThrow` | `send_or_raise` |
| `on('message', fn)` / `off` | `on("message", fn)` / `off`. `fn` may be sync or async. `InboundMessage` dataclass (Node's name). |
| `optOuts.has/add/remove/list` | `opt_outs.has/add/remove/list` (async) |
| `status()`, `verifyConnection()` | `await status()`, `await verify_connection()` (same never-throws rules) |
| `router()` / `webhookRouter()` | `asgi_app()` / `webhook_app()`. Same rule: `asgi_app()` raises without `authorize`, unless `allow_unauthenticated_setup`. |
| `ready` (starts in constructor), `close()` | `await start()` (idempotent) and `await close()`; `GoToSms` is also an async context manager. See lifecycle. |
| errors (`LoginExpiredError`, `TemporaryError`, `GoToApiError`, `DecryptError`, ...) | the same names as Python exceptions in a new `aiogotoconnect.sms` module. They must not collide with or change the existing `aiogotoconnect.exceptions`. |
| `normalizePhone` | `normalize_phone` |

The JSON wire format (`/api/*` bodies, `status()` payload keys) is **exactly** the contract's
camelCase. Only the Python API uses snake_case. Error and status messages keep Node's wording, but
name the Python API where Node's names its own: `create_goto_sms`, `allow_unauthenticated_setup=True`,
`data_dir` *(Claude's call)*. Tests assert the Python wording.

**`mount_path` works as it does in Node** *(Claude's call)*: the option decides. It is not read from
where the app is mounted, because the poller and `status()` need URLs before the first request.
The quick start and the example therefore pass `mount_path="/api/goto"` alongside
`app.mount("/api/goto", ...)`. Node's "mounted at X but mountPath is Y" warning carries over,
using ASGI `root_path`.

**`webhook_app()`** owns its mount like any ASGI app. Paths other than `/inbound/:secret` under it
answer 404 *(Claude's call)*.

### Lifecycle (Claude's call)

Python can't start async work in a plain constructor, so Node's `ready` becomes an explicit
`start()`. It does the same work: resolve the key, take the process lock, replay pending work,
start the poller. Recommended FastAPI wiring:

```python
sms = create_goto_sms(public_url=..., mount_path="/api/goto", authorize=is_admin)

@asynccontextmanager
async def lifespan(app):
    async with sms:          # start() ... close()
        yield

app = FastAPI(lifespan=lifespan)
app.mount("/api/goto", sms.asgi_app())
```

Mounted sub-apps don't receive ASGI lifespan events in Starlette/FastAPI. To keep a forgotten
lifespan from silently breaking things, the ASGI app calls `start()` on its first request. Like
Node, the page still loads and shows the reason if startup fails. Polling only begins once
`start()` has run; the README says so.

### authorize

`authorize(request) -> bool | Awaitable[bool]`, where `request` is a small read-only object with
`method`, `path`, `headers`, `cookies` and `query`. These are the fields Node's `AuthorizeRequest`
exposes, so a host can check a session cookie or JWT the way the Node apps do. The `/callback` and
`/inbound` exemptions and the `login_url` 302 behave exactly as `contract/http.md` says.

## The shared setup page

- Vendored at `src/aiogotoconnect/sms/setup/` from a pinned goto-sms release zip, with the pinned
  version recorded beside it (e.g. `SETUP_UI_VERSION`). Start with v0.2.1, which is guided and dark by
  default. Shipped as package data.
- `scripts/update-setup-page.sh vX.Y.Z` downloads the release zip with `gh release download`,
  replaces the files and updates the recorded version.
- A test checks that the page uses relative URLs only (as Node's does), and that it is served the
  way Node serves it (same headers, including any CSP and cache headers Node sets).
- The builder checks how Node injects per-request values into the page (back link, label) and does
  exactly the same.

## Testing

- **pytest** (asyncio auto mode, already configured), run with `-W error` as today.
- `tests/sms/fake_goto.py`: an aiohttp fake of GoTo's auth, SCIM, numbers, messages, notification
  channel and subscription endpoints, ported from goto-sms `packages/node/test/helpers/fake-goto.ts`
  (including `hangNext`-style fault injection).
- One test per checklist row (C1–C25). The test name starts with the row id, as the Node names do.
  The row-to-test map lives in `docs/conformance.md` in this repo.
- Contract tests run the ASGI app over real HTTP (uvicorn on an ephemeral port) or `httpx`'s ASGI
  transport. Either is a dev-only dependency. Builder's choice, but every `/api/*` shape in
  `contract/http.md` gets covered.
- Store conformance: the same shared suite runs against all three stores, as Node's
  `store/conformance.ts` does.
- **e2e:** `examples/fastapi-app` plus pytest-playwright drive the setup page against the fake
  GoTo, following the Node e2e flow. New CI job `e2e`.
- **Lint/typecheck:** add `ruff check`, `ruff format --check` and `mypy --strict` scoped to
  `aiogotoconnect/sms` (Claude's call; the repo has no Python lint today). Existing modules are only
  linted if that's free of churn.
- **Live check before tagging** (Taylor decided, 2026-10-29):
  - Run `examples/fastapi-app` on this box at port 8765, in **polling mode**. No webhook subscription
    is created, so nothing can reach Spring Ideas or party-invites.
  - Use the **existing ha-goto OAuth client**: `HA_GOTO_OAUTH_CLIENT_ID` and `_SECRET` in the
    Infisical `gotoconnect` project. Taylor adds the redirect URI
    `http://localhost:8765/api/goto/callback` to that client.
  - Taylor opens `ssh -L 8765:localhost:8765` from his Mac, completes the setup page in his browser,
    receives one test text on his mobile, and replies.
  - The check passes when the reply arrives through `on("message")`. This exercises the real OAuth
    callback, discovery, send and polling.

## Repo, CI and release

- New code lives in `src/aiogotoconnect/sms/`, mirroring Node's layout (config, oauth, directory,
  send, inbound/{webhook,poller,keywords,processor}, optouts, secrets, store/, server/). The public
  names are re-exported from `aiogotoconnect`.
- CI: the existing `test (3.12)` and `test (3.13)` jobs, plus lint/typecheck steps and an `e2e` job.
  Add `e2e` to the main ruleset's required checks.
- `publish.yml` stays as it is (tag = version check, `pypi` approval environment, trusted
  publishing). It also attaches nothing new.
- Docs: README "SMS for web apps" section (FastAPI quick start, Django/Starlette recipe, config and
  env vars, the account-wide webhook note), `CHANGELOG.md` row, `CLAUDE.md` Live line + Key Files.
  In goto-sms: README "Other languages" line pointing at aiogotoconnect, and the C24/C25 rows.

## Left to the builder

Internal module structure; HTTP test harness; how the fake is organized; whether low-level helpers
are shared with the existing `GoToOAuth`/`GoToMessaging` (allowed only if those classes' behaviour
and tests are unchanged); exact sealed-secret format (reusing Node's format is preferred but not
required; the key **format** must match so the same `GOTO_SMS_ENCRYPTION_KEY` value works in
either language).
