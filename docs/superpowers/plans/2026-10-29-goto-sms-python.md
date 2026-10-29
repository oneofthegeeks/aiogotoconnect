# goto-sms for Python (aiogotoconnect 0.4.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `create_goto_sms(...)` to aiogotoconnect. It is a Python port of `@oneofthegeeks/goto-sms`, at
exact parity: the same setup page, HTTP contract and C1–C25 behaviours, served as a framework-free
ASGI app.

**Architecture:** A new subpackage, `src/aiogotoconnect/sms/`, mirrors the Node package's modules
one-to-one: config, oauth, directory, send, inbound/*, store/*, server/*. Each Python module is a
port of the Node file with the same name, and that Node file is the behavioural reference. The
framework-free handlers take an `HttpRequest` and return an `HttpResponse`. A thin ASGI adapter
turns them into an ASGI 3 app, the way Node's `express.ts` wraps `handlers.ts`. The existing
`GoToOAuth`, `GoToMessaging`, `GoToVoice` and `exceptions` modules are not modified.

**Tech Stack:** Python >= 3.12, asyncio, aiohttp (HTTP client), cryptography (AES-GCM, optional
`[sms]` extra), stdlib sqlite3. Tests use pytest + pytest-asyncio (auto mode), an aiohttp fake
GoTo, httpx `ASGITransport` for handler tests, and uvicorn + pytest-playwright for e2e. Linting is
ruff and mypy --strict, scoped to the new code.

**Spec:** `docs/superpowers/specs/2026-10-29-goto-sms-python-design.md` (approved by Taylor,
2026-10-29).

**Reference implementation (read it, it is the spec for behaviour):** goto-sms repo, cloned at
`/home/taylor/dev/goto-sms-app`. `packages/node/src/<x>.ts` → `src/aiogotoconnect/sms/<x>.py`;
`packages/node/test/<x>.test.ts` → `tests/sms/test_<x>.py`. Also read `contract/http.md`,
`contract/conformance.md` and `e2e/setup.spec.ts`.

**Work location:** a worktree of `oneofthegeeks/aiogotoconnect` on branch `feat/sms-web`, off
`origin/main`. Do **not** use `~/dev/gotoconnect`, which has diverged and is Taylor's. main is
PR-only (required checks `test (3.12)`, `test (3.13)`).

## Global Constraints

- **Parity is the bar:** when this plan and the Node code disagree on behaviour, Node wins. Record a
  ledger ruling and follow Node. Only Python naming and the spec's listed Claude's-call deviations
  differ.
- Python API is snake_case. The JSON wire format (`/api/*` bodies, `status()` payload, stored
  values) keeps Node's camelCase **exactly**.
- Env var names are unchanged: `GOTO_CLIENT_ID`, `GOTO_CLIENT_SECRET`, `GOTO_ACCOUNT_KEY`,
  `GOTO_FROM_NUMBER`, `GOTO_REFRESH_TOKEN`, `GOTO_SMS_ENCRYPTION_KEY`, `GOTO_SMS_PUBLIC_URL`.
  Empty values and the literal `CHANGEME` count as unset.
- `GOTO_SMS_ENV=production` replaces Node's `NODE_ENV=production`. It refuses both `log_only` and
  `allow_unauthenticated_setup`.
- Messages keep Node's wording but name the Python API (`create_goto_sms`,
  `allow_unauthenticated_setup=True`, `data_dir`).
- `poll_interval` and `send_timeout` are **seconds** (float), with Node's defaults ÷ 1000. Internally,
  `now()` returns epoch **milliseconds** (float) and `GoToSmsStore.claim` takes `ttl_ms`, as in Node,
  so the store protocol matches.
- Runtime deps: `aiohttp>=3.9` (existing) plus the `[sms]` extra `cryptography>=41`. **No web
  framework at runtime.** `import aiogotoconnect` must work without `cryptography` installed.
- Secrets are sealed in Node's exact format: `v1.<iv>.<tag>.<ciphertext>`, base64, AES-256-GCM, a
  12-byte IV, a 16-byte tag. The key is `sha256(utf8(key string))`, or a raw 32-byte keyfile
  `<data_dir>/.goto-sms.key` created mode 0600 with exclusive create.
- Logger default is `logging.getLogger("aiogotoconnect.sms")`. Never log secrets, tokens, the webhook
  secret or message text at INFO or above (follow Node's log lines).
- Setup page: v0.2.1 from the goto-sms release, byte-for-byte.
- Version `0.4.0`. Existing modules and their tests are unchanged. `pytest -W error` stays green.
- Test names: a conformance test is named `test_cN_<what>` (e.g. `test_c4_single_flight_one_refresh`).
  Every other ported Node test keeps its meaning under a snake_case name.
- Commits: Conventional Commits, small, one per task minimum.

## Review Focus

1. **Forgotten lifespan / concurrent first requests:** two simultaneous first requests to the ASGI
   app run `start()` exactly once, and both get answers (Task 12 test).
2. **Root mount and trailing slashes:** `mount_path=""` or `"/"` gives `https://x.example/callback`,
   never `//callback`. A `public_url` with trailing slashes is trimmed (Task 11 test).
3. **Malformed requests:** a non-JSON body or a JSON array on `POST /api/*` gets a 400 JSON error. A
   body over 100 KB gets a 413. Neither ever returns a 500 or a traceback (Task 12 test).
4. **A misbehaving message handler:** a sync or async `on("message")` handler that raises is logged.
   The message stays processed, and the poller and other handlers keep running (Task 8 test).
5. **Missing `[sms]` extra:** `import aiogotoconnect` succeeds. `create_goto_sms()` raises
   `ImportError` whose text contains `pip install aiogotoconnect[sms]` (Task 1 test).

---

### Task 1: Scaffolding, types, errors, phone

**Files:**
- Modify: `pyproject.toml` (version 0.4.0. Extras: `sms = ["cryptography>=41"]`, and `dev` gains
  `ruff`, `mypy`, `httpx`, `uvicorn`, `fastapi`, `pytest-playwright`, `cryptography>=41`. Add
  `[tool.ruff]` with line-length 100 and `[tool.mypy]` `strict = true`, `files = ["src/aiogotoconnect/sms"]`.
  Add `"aiogotoconnect" = ["py.typed", "sms/setup/*"]` to package-data.)
- Modify: `.github/workflows/ci.yml`: install `.[dev,sms]`; add steps
  `ruff check src/aiogotoconnect/sms tests/sms`, `ruff format --check src/aiogotoconnect/sms tests/sms`,
  `mypy`. Modify `publish.yml` to install `.[dev,sms]`.
- Create: `src/aiogotoconnect/sms/__init__.py`, `types.py`, `errors.py`, `phone.py`
- Modify: `src/aiogotoconnect/__init__.py` (lazy re-exports, see below)
- Test: `tests/sms/__init__.py`, `tests/sms/test_phone.py`, `tests/sms/test_extra.py`

**Interfaces:**
- Produces in `sms/types.py`:
  - `SendStatus = Literal["sent","opted_out","invalid_number","uncertain","not_connected","rejected"]`
  - `Keyword = Literal["STOP","START","HELP"]`
  - frozen dataclasses:
    - `SendResult(status, reason=None, retryable=None, message_id=None, to=None, deduped=None)`
    - `InboundMessage(id, from_, to, text, received_at, raw, account_key=None, keyword=None)`
    - `LogEntry(id, direction, from_, to, status, at, reason=None, goto_id=None)`
  - `VERSION: str`, from `importlib.metadata.version("aiogotoconnect")`, falling back to `"dev"`
- Produces in `sms/errors.py`: `GoToSmsError(message, code)` and its subclasses `LoginExpiredError`,
  `TemporaryError`, `NotConfiguredError`, `GoToApiError`, `SendFailedError`, `DecryptError`. Fields
  and codes as in Node `errors.ts`.
- Produces in `sms/phone.py`: `normalize_phone(raw: str) -> str | None`.
- `aiogotoconnect.__init__` re-exports `create_goto_sms`, `GoToSms`, `MemoryStore`, `FileStore`,
  `SqliteStore`, `normalize_phone`, the result and message types, and the sms error classes. It uses
  a module `__getattr__` so nothing under `sms/` that needs `cryptography` is imported at package
  import. Existing exports are unchanged.

- [ ] **Step 1: Write failing tests.** In `test_phone.py`, port Node `test/phone.test.ts` (C8: NANP
  loose formats, area/exchange 2–9, no N11 area, junk → None). In `test_extra.py`:

```python
def test_import_without_cryptography(monkeypatch):
    # Simulate the extra being absent: block the module, re-import the package fresh.
    monkeypatch.setitem(sys.modules, "cryptography", None)
    for m in [m for m in sys.modules if m.startswith("aiogotoconnect")]:
        monkeypatch.delitem(sys.modules, m)
    import aiogotoconnect
    assert hasattr(aiogotoconnect, "GoToOAuth")
    with pytest.raises(ImportError, match=r"pip install aiogotoconnect\[sms\]"):
        aiogotoconnect.create_goto_sms()
```

- [ ] **Step 2:** `pytest tests/sms -v`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement the files above. `create_goto_sms` is a placeholder at this point: it
  raises the `ImportError` when `cryptography` is missing, else `NotImplementedError`. Task 11
  replaces it.
- [ ] **Step 4:** `pytest -W error && ruff check src/aiogotoconnect/sms tests/sms && mypy`. Expected:
  all pass, existing tests included.
- [ ] **Step 5:** Commit `feat(sms): scaffold sms subpackage, types, errors, phone`.

### Task 2: Stores

**Files:**
- Create: `src/aiogotoconnect/sms/store/{__init__,types,memory,file,sqlite,conformance}.py`,
  `src/aiogotoconnect/sms/testing.py`
- Test: `tests/sms/test_store.py`

**Interfaces:**
- `Json = None | bool | int | float | str | list["Json"] | dict[str, "Json"]`
- `class GoToSmsStore(Protocol)`: all methods async:
  - `get(tenant, key) -> Json | None` (None = absent)
  - `set(tenant, key, value) -> None`
  - `delete(tenant, key) -> None`
  - `list(tenant, prefix) -> list[tuple[str, Json]]`, sorted by key
  - `compare_and_set(tenant, key, expected: Json | None, next: Json) -> bool`
  - `claim(tenant, key, ttl_ms: float, owner: str) -> bool`
  - Stored JSON `null` is not a supported value. Document that, matching Node, where `undefined`
    means absent.
- `json_equal(a, b) -> bool` and `next_claim(...)`, ported from `store/types.ts`.
- `MemoryStore(now=None)`, `FileStore(path, now=None, logger=None)` (atomic write via temp file +
  `os.replace`), `SqliteStore(db: str | os.PathLike | sqlite3.Connection, table="goto_sms", now=None)`.
  It sets WAL when it opens the path itself and uses the same schema and SQL as `store/sqlite.ts`.
- `aiogotoconnect.sms.testing.run_store_conformance(make_store: Callable[[], Awaitable[GoToSmsStore]]) -> Awaitable[None]`
  is a port of `store/conformance.ts`, raising `AssertionError`. It's public, for custom-store
  authors.

- [ ] **Step 1:** Write `test_store.py`. It ports Node `test/store.test.ts`, running
  `run_store_conformance` parametrized over the three stores (FileStore and SqliteStore in
  `tmp_path`), plus the store-specific tests.
- [ ] **Step 2:** Run it. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `pytest tests/sms/test_store.py -W error -v`. Expected: PASS. Also run ruff and mypy.
- [ ] **Step 5:** Commit `feat(sms): memory, file and sqlite stores with conformance suite`.

### Task 3: Config resolution and secrets

**Files:**
- Create: `src/aiogotoconnect/sms/config.py`, `src/aiogotoconnect/sms/secrets.py`
- Test: `tests/sms/test_config.py`, `tests/sms/test_secrets.py`,
  `tests/sms/fixtures/node_sealed.json`

**Interfaces:**
- `ConfigField = Literal["clientId","clientSecret","accountKey","fromNumber"]` (wire names) and
  `Source = Literal["options","env","store","unset"]`.
- `config_value(v: str | None) -> str | None`. Blank and `CHANGEME` give None.
- `ConfigResolver(store, tenant, box, options: Mapping[ConfigField, str | None], env: Mapping[str,str])`:
  - `async resolve() -> ResolvedConfig`, a dataclass with `client_id, client_secret, account_key,
    from_number`, each with its source, as in `config.ts`
  - `async save(patch: Mapping[ConfigField, str]) -> None`
  - `seed_refresh_token() -> str | None`
- `SecretBox(key: bytes)` with `seal(plain: str) -> str` and `open(sealed: str) -> str`. `open`
  raises `DecryptError` for anything malformed, and uses strict base64 validation.
- `async resolve_encryption_key(option_key, env, data_dir, logger) -> tuple[bytes, Literal["option","env","keyfile"]]`
  is a port of `resolveEncryptionKey`, including the placeholder warning and the damaged-keyfile
  message.

- [ ] **Step 1:** Write the tests:
  - Port Node `test/config.test.ts` and `test/secrets.test.ts`, including C22 `seal/open round
    trip` and `tampered ciphertext throws DecryptError`.
  - Add `test_c22_opens_node_sealed_value`. The fixture holds `{"key": "test-key-not-secret",
    "sealed": "<made once with node>", "plain": "hello"}`. Make it with a one-off `node -e`
    script against `/home/taylor/dev/goto-sms-app/packages/node/dist`, using the key string
    `test-key-not-secret`. Python must open it.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement with `cryptography.hazmat.primitives.ciphers.aead.AESGCM`.
- [ ] **Step 4:** Tests, ruff, mypy pass.
- [ ] **Step 5:** Commit `feat(sms): config resolution and AES-GCM secret box (Node-compatible)`.

### Task 4: Fake GoTo, HTTP wrapper, directory

**Files:**
- Create: `tests/sms/fake_goto.py`, `tests/sms/conftest.py`,
  `src/aiogotoconnect/sms/http.py`, `src/aiogotoconnect/sms/directory.py`
- Test: `tests/sms/test_directory.py`

**Interfaces:**
- `FakeGoTo` is a port of `test/helpers/fake-goto.ts` onto an `aiohttp.web` server on 127.0.0.1 with
  an ephemeral port. It covers the same routes, state and fault-injection methods, in snake_case
  (e.g. `hang_next()`, recorded `requests` list). It exposes `auth_base_url` and `api_base_url`.
  The conftest fixture `fake_goto` starts and stops it per test.
- `GoToHttp`: `create_goto_http(session: aiohttp.ClientSession, api_base_url, get_token, force_refresh, timeout_s, logger) -> GoToHttp`,
  as `http.ts` does. It maps statuses to `GoToApiError` / `TemporaryError` the same way. Also
  `error_name(err) -> str`.
- `GoToAccount(key, name)`, `GoToNumber(number, ...)` (fields as `directory.ts`).
  `async list_accounts(http) -> list[GoToAccount]`, `async list_numbers(http, account_key) -> list[GoToNumber]`.

- [ ] **Step 1:** Port Node `test/directory.test.ts` (C6 and C7).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement the fake, http and directory.
- [ ] **Step 4:** Pass, plus ruff and mypy.
- [ ] **Step 5:** Commit `feat(sms): GoTo http wrapper, directory discovery, fake GoTo for tests`.

### Task 5: OAuth

**Files:** Create `src/aiogotoconnect/sms/oauth.py`. Test `tests/sms/test_oauth.py`.

**Interfaces:**
- `GoToAuth(store, tenant, box, config, session, auth_base_url, redirect_uri, logger, now)`, with
  these methods:
  - `async start_connect() -> str` (the authorize URL)
  - `async finish_connect(code, state) -> None`
  - `async get_access_token() -> str`
  - `async verify() -> None`
  - `async force_refresh() -> str`
  - `async disconnect() -> None`
  - `async status() -> AuthStatus` (fields as Node's return)
- Single-flight refresh uses one shared `asyncio.Task` (or a future) per refresh. State comparison
  uses `hmac.compare_digest`.

- [ ] **Step 1:** Port every test in Node `test/oauth.test.ts`: C1, C2, C3 (×3), C4 (×2), C5 (×3)
  and the rest.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Pass, plus ruff and mypy.
- [ ] **Step 5:** Commit `feat(sms): OAuth connect, single-flight refresh, login-expired handling`.

### Task 6: Message log, opt-outs, sender

**Files:**
- Create: `src/aiogotoconnect/sms/log.py`, `optouts.py`, `send.py`
- Test: `tests/sms/test_send.py`

**Interfaces:**
- `MessageLog(store, tenant, now, retention_days)`, with `append(entry_fields) -> LogEntry`,
  `list(limit=...) -> list[LogEntry]` and `prune()`. `PRUNE_EVERY = 100`.
- `OptOutSource = Literal["keyword","admin"]`. `OptOuts(store, tenant, now)`:
  - `has(e164)`
  - `add(e164, source)`
  - `remove(e164)`
  - `list() -> list[dict]` with keys `number`, `at`, `source`
- `Sender(**SenderOptions)`, fields as `send.ts`, with `send_timeout_s` in seconds. Methods:
  - `async send(to, text, dedupe_key=None) -> SendResult`
  - `async send_or_raise(...) -> SendResult`, which raises `SendFailedError` when not `sent`
  - `async prune_dedupe()`
  - Log-only behaviour as in Node.

- [ ] **Step 1:** Port every test in Node `test/send.test.ts`: C8 no-network, C9–C15 and the rest.
  The C15 production half (`log_only` with `GOTO_SMS_ENV=production`) is tested in Task 11.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Pass, plus ruff and mypy.
- [ ] **Step 5:** Commit `feat(sms): sender with dedupe, opt-outs, honest results; message log`.

### Task 7: Webhook enable/disable and envelope parsing

**Files:** Create `src/aiogotoconnect/sms/inbound/{__init__,webhook}.py`. Test `tests/sms/test_webhook.py`.

**Interfaces:** Ports of these `webhook.ts` functions:
- `async enable_webhook(...)`
- `async get_webhook_secret(store, tenant) -> str | None`
- `async ensure_webhook_secret(store, tenant) -> str`
- `async disable_webhook(...)`
- `async get_webhook_account(...)`
- `check_webhook_secret(given, expected) -> bool` (constant time)
- `parse_webhook_envelope(body: object) -> InboundMessage | None` (keyword unset, never raises)

- [ ] **Step 1:** Port every test in Node `test/webhook.test.ts`: C16 parse (×3), C17, C18 (×3).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Pass, plus ruff and mypy.
- [ ] **Step 5:** Commit `feat(sms): webhook channel/subscription setup and envelope parsing`.

### Task 8: Keywords and inbound processor

**Files:** Create `src/aiogotoconnect/sms/inbound/{keywords,processor}.py`. Test `tests/sms/test_inbound.py`.

**Interfaces:**
- `KeywordReplies` (TypedDict, keys as Node), `DEFAULT_REPLIES`.
  `detect_keyword(text, yes_is_opt_in: bool) -> Keyword | None`.
- `InboundProcessor(**InboundProcessorOptions)` with these methods:
  - `persist(m) -> bool`
  - `process(id) -> None`
  - `replay_pending() -> int`
  - `prune() -> None`
  - `emit` is a callable the facade supplies. It calls handlers that may be sync or async. An async
    handler is awaited inside the processor's task. Any exception is logged with
    `logger.exception` and never propagates.

- [ ] **Step 1:** Write the tests:
  - Port every test in Node `test/inbound.test.ts`: C20 (×3), C21, and the rest.
  - Add `test_c25_text_to_other_number_dropped`, ported from the Node handlers test `inbound: a
    text to another number in the same GoTo account is ignored`. If Node does the drop in
    `handlers.ts`, put this test in Task 10 instead and note it in the ledger.
  - Review Focus 4: `test_handler_exception_is_logged_and_processing_continues`, covering a sync
    raiser and an async raiser. A second handler still receives the message, `caplog` has the
    error, and the message is marked processed.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Pass, plus ruff and mypy.
- [ ] **Step 5:** Commit `feat(sms): STOP/START/HELP keywords and inbound processor`.

### Task 9: Poller

**Files:** Create `src/aiogotoconnect/sms/inbound/poller.py`. Test `tests/sms/test_poller.py`.

**Interfaces:** `Poller(**PollerOptions)` with `poll_interval_s`. Methods:
- `async tick() -> int`
- `start() -> None`, which creates one `asyncio.Task`. It must be called inside a running loop.
- `stop() -> None`
- `async reset_cursor()`
- `async idle()`

Timestamps are compared by parsed instant (`datetime.fromisoformat`, after normalizing `Z` and
fractional digits). This matches Node's `Date.parse` ordering for mixed µs/ms inputs.

- [ ] **Step 1:** Port every test in Node `test/poller.test.ts`: C19 (×2) and the rest.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Pass, plus ruff and mypy.
- [ ] **Step 5:** Commit `feat(sms): polling for incoming texts`.

### Task 10: Status payload and framework-free handlers

**Files:**
- Create: `src/aiogotoconnect/sms/server/{__init__,status,handlers}.py`
- Test: `tests/sms/test_handlers.py`

**Interfaces:**
- `HttpRequest(method, path, query: dict[str,str], headers: dict[str,str], cookies: dict[str,str], body: object | None)`.
  `path` is relative to the mount and header names are lower-case.
- `AuthorizeRequest(method, path, query, headers, cookies)` is the frozen view passed to
  `authorize`.
- `HttpResponse(status, json=None, html=None, redirect=None)`. `Handler = Callable[[HttpRequest], Awaitable[HttpResponse]]`.
- `REQUIRED_SCOPES`, `IncomingError`, `StatusPayload` (a TypedDict with camelCase keys),
  `startup_error_text(err) -> str`, `async build_status(core, include_webhook_url: bool) -> StatusPayload`.
- `assert_setup_auth(options)`, `create_handlers(core, setup_html: str) -> Handler`,
  `create_deferred_handlers(...) -> Handler`. These are ports of `handlers.ts`, including
  `OPTIONS /inbound/:secret`, the inbound 204-first behaviour (background work goes into
  `core.inflight`), the C25 drop, `login_url` 302 and HEAD rules.
- Consumes `Core` (Task 11 defines the dataclass). This task defines a minimal `Core` dataclass in
  `sms/core.py` with Node's `Core` fields in snake_case. Task 11 fills it.

- [ ] **Step 1:** Port every test in Node `test/handlers.test.ts`, including C22 `/api/status never
  contains clientSecret or tokens`, C23 and C24 (`OPTIONS /inbound/:secret answers 200 before
  webhook mode is on`). Drive the handlers directly with `HttpRequest` objects, using the Node
  test helper `test/helpers/sms.ts` ported to `tests/sms/helpers.py`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Pass, plus ruff and mypy.
- [ ] **Step 5:** Commit `feat(sms): status payload and framework-free contract handlers`.

### Task 11: The `create_goto_sms` facade and lifecycle

**Files:**
- Create: `src/aiogotoconnect/sms/client.py`
- Modify: `sms/core.py`, `sms/__init__.py`, `aiogotoconnect/__init__.py`
- Test: `tests/sms/test_client.py`, `tests/sms/test_smoke.py`

**Interfaces:**
- `create_goto_sms(*, public_url=None, mount_path="/goto-sms", data_dir=None, store=None, tenant="default", authorize=None, allow_unauthenticated_setup=False, back_url=None, back_label=None, login_url=None, client_id=None, client_secret=None, account_key=None, from_number=None, refresh_token=None, encryption_key=None, log_only=False, replies=None, yes_is_opt_in=False, poll_interval=<Node default/1000>, send_timeout=<Node default/1000>, inbound_per_hour=10, log_retention_days=<Node default>, logger=None, session=None, auth_base_url=<Node default>, api_base_url=<Node default>, env=None, now=None) -> GoToSms`.
  - Validation runs synchronously at construction, as Node's `createGoToSms` does: link options,
    the `log_only`/`allow_unauthenticated_setup` production guard, and `trim_trailing_slashes`.
  - `env` defaults to `os.environ`. `logger=False` disables logging. Any default not given here is
    copied from Node `client.ts`.
- `class GoToSms`:
  - `send`, `send_or_raise`
  - `on(event: Literal["message"], fn) -> GoToSms`, `off(...)`
  - `opt_outs` (has/add/remove/list)
  - `async status() -> StatusPayload` (no `webhookUrl`, never raises)
  - `async verify_connection() -> bool`
  - `asgi_app()`, `webhook_app()` (Task 12)
  - `async start()`: idempotent and safe under concurrent callers (one shared task). It does what
    Node's `ready` does.
  - `async close()`: stops the poller, awaits `core.inflight`, releases the lock, closes a session
    it created
  - `__aenter__` / `__aexit__`
  - `core` (raises until started)
- `trim_trailing_slashes(s: str) -> str`: loop, no regex.

- [ ] **Step 1:** Write the tests:
  - Port every test in Node `test/client.test.ts` and `test/smoke.test.ts`, including the C15
    production half, adapted: `create_goto_sms(log_only=True, env={"GOTO_SMS_ENV": "production"})`
    raises, and the same goes for `allow_unauthenticated_setup=True`.
  - Add `test_start_is_idempotent_under_concurrency`: `await asyncio.gather(sms.start(),
    sms.start())`, then the fake shows one lock claim and one poller.
  - Add `test_root_mount_urls`: mount paths `""` and `"/"` with `public_url="https://x.example/"`
    both give redirect URI `https://x.example/callback`. `mount_path="/admin/tools/sms/"` gives
    `https://x.example/admin/tools/sms/callback`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Remove the Task 1 placeholder.
- [ ] **Step 4:** Full `pytest -W error`, ruff and mypy pass.
- [ ] **Step 5:** Commit `feat(sms): create_goto_sms facade, start/close lifecycle`.

### Task 12: ASGI adapter and the vendored setup page

**Files:**
- Create: `src/aiogotoconnect/sms/server/asgi.py`, `src/aiogotoconnect/sms/setup/index.html`
  (from the zip), `src/aiogotoconnect/sms/setup/SETUP_UI_VERSION` (`v0.2.1`),
  `scripts/update-setup-page.sh`
- Modify: `sms/client.py` (`asgi_app`, `webhook_app`)
- Test: `tests/sms/test_asgi.py`

**Interfaces:**
- `build_asgi_app(source: RouterSource, inbound_only: bool = False) -> ASGIApp`, where `RouterSource`
  has `options`, `logger` and `handle`.
  - Port the behaviour of `express.ts`: `Cache-Control: no-store` on everything, and on HTML
    `X-Frame-Options: DENY` plus `Content-Security-Policy: frame-ancestors 'none'`.
  - JSON body limit 100 KB → 413. Unparseable or non-object JSON → 400 `{"error": ...}`, with the
    same shape Node gives.
  - Query and header values are flattened as `flatten()` does. Cookies are parsed from the
    `cookie` header with `http.cookies.SimpleCookie`.
  - Only the `http` scope is handled. `lifespan` messages get `lifespan.startup.complete` /
    `shutdown.complete` without starting anything.
  - The "mounted at X but mount_path is Y" warning is logged once, comparing `scope["root_path"]`
    with `mount_path`.
- `GoToSms.asgi_app()`: raises `GoToSmsError` without `authorize`, unless
  `allow_unauthenticated_setup`, with Node's message adapted. Each request awaits `self.start()`
  first. If start failed, the deferred handlers answer.
- `GoToSms.webhook_app()`: inbound only (`POST` and `OPTIONS` `/inbound/:secret`). Anything else is
  404.
- The setup page is loaded with `importlib.resources.files("aiogotoconnect.sms.setup")`.
  `update-setup-page.sh vX.Y.Z` runs
  `gh release download vX.Y.Z -R oneofthegeeks/goto-sms -p 'goto-sms-setup-ui-*.zip'`, unzips the
  file into `sms/setup/` and writes the version.

- [ ] **Step 1:** Write the tests:
  - Port Node `test/express.test.ts` onto an `httpx.AsyncClient(transport=ASGITransport(app), base_url=...)`.
    Mount under a tiny FastAPI app at `/api/goto` with `mount_path="/api/goto"`, so `root_path` is
    exercised.
  - Add `test_setup_page_relative_urls_only`, porting the Node check.
  - Add `test_setup_page_matches_pinned_release`: the file's sha256 equals the one recorded in the
    test, taken from the v0.2.1 zip.
  - Review Focus 3: `test_bad_json_400_and_oversized_413` (no 500).
  - Review Focus 1: `test_concurrent_first_requests_start_once`, two `gather`ed GETs of
    `/api/status` before `start()`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Run `scripts/update-setup-page.sh v0.2.1`, then implement the adapter.
- [ ] **Step 4:** Full suite, ruff and mypy pass. `python -m build`, then confirm the wheel contains
  `aiogotoconnect/sms/setup/index.html`.
- [ ] **Step 5:** Commit `feat(sms): ASGI app and vendored goto-sms setup page v0.2.1`.

### Task 13: FastAPI example and browser e2e

**Files:**
- Create: `examples/fastapi-app/{app.py,README.md}`, `e2e/test_setup.py`, `e2e/conftest.py`
- Modify: `.github/workflows/ci.yml` (new job `e2e`: install `.[dev,sms]`, run
  `playwright install --with-deps chromium`, then `pytest e2e`)

**Interfaces:** `examples/fastapi-app/app.py` is a FastAPI app:
- The lifespan uses `async with sms:`, and the app mounts at `/api/goto` with
  `mount_path="/api/goto"`.
- `authorize` checks a demo cookie, with a `/login` route that sets it. This mirrors
  `examples/express-app`.
- It reads `PORT` (default 8765), `GOTO_SMS_PUBLIC_URL`, and the `GOTO_*` env vars. Data goes in
  `./data`.
- An `on("message")` handler logs inbound texts. This is what the live check watches.

The e2e conftest starts `FakeGoTo` and the example under uvicorn, with env pointing the app's
`auth_base_url`/`api_base_url` at the fake. Read these from `GOTO_SMS_AUTH_BASE_URL` and
`GOTO_SMS_API_BASE_URL` in the example only, never in the package, the way the Node e2e wires its
example.

- [ ] **Step 1:** Port `e2e/setup.spec.ts` step for step into `e2e/test_setup.py`, using
  pytest-playwright's `page`.
- [ ] **Step 2:** `pytest e2e`. Expected: FAIL (no example yet).
- [ ] **Step 3:** Write the example.
- [ ] **Step 4:** `pytest e2e` passes. Drive the page at 375 px and 1280 px widths with a screenshot
  each, and check it's dark by default.
- [ ] **Step 5:** Commit `feat(sms): FastAPI example app and Playwright e2e`.

### Task 14: Docs, conformance map, cross-repo rows

**Files:**
- Modify: `README.md` (new "SMS for web apps" section):
  - Install with `pip install aiogotoconnect[sms]`.
  - FastAPI quick start (spec's lifespan snippet), and a one-paragraph Starlette/Django-ASGI recipe.
  - Options table and env vars, `GOTO_SMS_ENV`.
  - The account-wide webhook note and the polling-starts-after-`start()` note.
- Modify: `CHANGELOG.md` (0.4.0 row), `CLAUDE.md` (Stack, Testing, Key Files, Gotchas; keep it
  ≤ 300 lines; the Live line changes at release in Task 15)
- Create: `docs/conformance.md`, a table of C1–C25 to Python test names. Verify every name with
  `pytest --collect-only -q`.
- Commit the spec and this plan if they aren't committed yet.
- **goto-sms repo** (a separate worktree off `origin/main`, branch `docs/conformance-c24-c25`):
  - Add rows C24 and C25 to `contract/conformance.md`, with the Node test names from the spec, and
    change "23 behaviours" to "25".
  - Add an "Other languages" line to the README pointing at aiogotoconnect.
  - Open a PR.

- [ ] **Step 1:** Write the docs.
- [ ] **Step 2:** Run `pytest --collect-only -q` and check every `docs/conformance.md` name exists
  (a small script that greps collected ids).
- [ ] **Step 3:** Commit `docs(sms): README, conformance map, changelog, CLAUDE.md`. Open the goto-sms
  PR.

### Task 15: Integration gates and release (controller does this, not a subagent)

- [ ] **Step 1:** Push `feat/sms-web` and open a PR. CI shows `test (3.12)`, `test (3.13)` and `e2e`
  all green.
- [ ] **Step 2:** **ha-goto gate.** Clone `oneofthegeeks/ha-goto` into the scratchpad and install its
  `requirements_test.txt`. Then install the built 0.4.0 wheel **without** the `[sms]` extra and run
  its pytest. Expected: same pass count as against 0.3.1. Fail = stop.
- [ ] **Step 3:** Ask Taylor to merge both PRs (aiogotoconnect and goto-sms). After merge, add `e2e`
  to the aiogotoconnect main ruleset's required checks with `gh api`.
- [ ] **Step 4: Live check** (needs Taylor):
  - Confirm he has added `http://localhost:8765/api/goto/callback` to the ha-goto OAuth client.
  - Pipe `HA_GOTO_OAUTH_CLIENT_ID/SECRET` from Infisical (`gotoconnect` project) into the example's
    env via stdin. Never print them.
  - Run the example from main on port 8765 with `GOTO_SMS_PUBLIC_URL=http://localhost:8765`.
  - Taylor runs `ssh -L 8765:localhost:8765 <box>`, completes the setup page with **polling** mode,
    and sends a test to his mobile, then replies.
  - Pass: the reply is logged by the example's `on("message")` handler.
  - Stop the app and delete `examples/fastapi-app/data`.
- [ ] **Step 5:** Fetch and confirm main is current, then tag `v0.4.0` on main and push the tag. Tell
  Taylor to approve the `pypi` environment. Confirm `pip index versions aiogotoconnect` shows 0.4.0.
- [ ] **Step 6:** Docs PR: the `CLAUDE.md` Live line (`v0.4.0 (date): SMS for web apps; goto-sms
  parity...`). Update the memory handoff `goto-sms-handoff.md` and the goto-sms `CLAUDE.md` if it
  references Python.
