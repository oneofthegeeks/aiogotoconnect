# Call events (`sms.on("call")`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An app that passes `watch_calls=True` to `create_goto_sms` receives one `"call"` event per inbound call to its sending number.

**Architecture:** One new module, `sms/inbound/calls.py`, holds a `CallWatcher` shaped like the existing `Poller` (own loop, shared in-flight tick, state in the store). `client.py` builds, starts and stops it and fans events out to `on("call")` handlers; `server/status.py` reports it under one optional `calls` key. Nothing in texting changes behaviour.

**Tech Stack:** Python 3.12+, asyncio, aiohttp (existing `GoToHttp`), pytest + pytest-asyncio (auto mode) against `tests/sms/fake_goto.py`, ruff, `mypy --strict`.

**Spec:** `docs/superpowers/specs/2026-11-18-call-events-design.md` (read it first; this plan argues from it).

## Global Constraints

- Branch `feat/call-events`, stacked on `feat/sms-web` (pull request #7). Do not change behaviour of anything #7 ships.
- Every task ends green on: `.venv/bin/python -m pytest tests/sms -W error`, `.venv/bin/ruff check src/aiogotoconnect/sms tests/sms`, `.venv/bin/ruff format --check src/aiogotoconnect/sms tests/sms`, `.venv/bin/mypy`.
- Never edit `src/aiogotoconnect/sms/setup/` (vendored).
- `watch_calls` off: zero call-report requests, no `calls` key in status, no new required scope (`REQUIRED_SCOPES` unchanged).
- Caller numbers and names never appear in log records at INFO or above. Failures log the error name only.
- Caller name is untrusted: whitespace collapsed, cut to 40 characters. A non-normalisable caller number is cut to 20.
- Constants (spec): interval default `120.0` s; reach-back `15` min; lookback clamp `24` h; double-ring window `120` s; refused back-off `3600` s; seen list capped at `400` entries.
- Store keys: `calls:cursor`, `calls:since`, `calls:seen`, `calls:error`.
- Scope message, exact: `GoTo refused the call reports. Enable the scope cr.v1.read on the OAuth client, then reconnect.`
- Live GoTo is touched only through the running board service (Task 1). Never start a second library instance on `~/.local/state/todo-board/goto-sms`.
- Conventional Commits; small commits; nothing is pushed until Task 6.

## Review Focus

1. **A report with several `PHONE_NUMBER` participants** (a transferred or forwarded call): the participant whose dialled number equals the sending number decides; the call is delivered once. (Task 3)
2. **Summaries or reports that are not the observed shape** (`items` missing, an item that is not a dict, no `conversationSpaceId`, `callCreated` unparseable): skipped or delivered with `at` = now, never an exception out of the tick, and the cursor still advances. (Task 3)
3. **A busy account**: more than 400 calls to other people's lines inside the 15-minute reach-back must not cause the sending number's calls to be re-delivered. Seen entries inside the reach-back window are never evicted; the 400 cap applies only to older ones. (Task 3)
4. **The sending number is changed on the setup page while watching**: the next tick uses the new number; no call to the old number is delivered after the change. (Task 3)
5. **`close()` while a handler is running**: `close()` waits for it, the call is marked seen, nothing writes to the store afterwards. (Task 5)

---

### Task 1: Find out what the live API returns

No library code. Settles paging and records field names, so Tasks 2 and 3 build on fact.

**Files:**
- Modify temporarily: `~/dev/todo-board/calls.py` (reverted in this task, never committed)
- Modify: `docs/superpowers/specs/2026-11-18-call-events-design.md` ("What GoTo gives us")

- [ ] **Step 1:** Read GoTo's published reference for `call-events-report/v1` (developer.goto.com). Note: paging parameters and marker name, page size limit, rate limit, the documented `callerOutcome` values, the status for a missing scope.
- [ ] **Step 2:** In `calls.py` `poll()`, right after the summaries request, add a temporary block that writes to `~/.local/state/todo-board/calls-shape.json` only: the top-level key names of the response, the key names of the first item, and for the first inbound report the key names of the report, of each participant and of each participant's `type`. **Key names and value types only: no numbers, names or ids.** Widen that one request's window to 7 days so there is something to look at. Wrap it in `try/except Exception: pass`.
- [ ] **Step 3:** `systemctl --user restart todo-sms`, wait for one poll (2 minutes), read the file.
- [ ] **Step 4:** `git -C ~/dev/todo-board checkout calls.py`, restart `todo-sms`, confirm `git status` is clean and `journalctl --user -u todo-sms -n 20` shows no error. Delete `calls-shape.json`.
- [ ] **Step 5:** Record what was learned in the spec's "What GoTo gives us" section (paging: yes/no and how; any field that differs). If summaries page, Task 3 follows the marker exactly as `Poller._all_pages` does; if not, one request per tick.
- [ ] **Step 6:** Commit the spec change: `docs(sms): record the live call-reports shape`.

### Task 2: The event type, the options and `on("call")`

**Files:**
- Modify: `src/aiogotoconnect/sms/types.py`, `src/aiogotoconnect/sms/__init__.py`, `src/aiogotoconnect/sms/core.py`, `src/aiogotoconnect/sms/client.py`, `src/aiogotoconnect/sms/inbound/processor.py`
- Test: `tests/sms/test_calls.py` (new), `tests/sms/test_smoke.py`

**Interfaces — Produces:**
- `types.InboundCall` (frozen dataclass), fields in this order: `id: str`, `from_: str`, `to: str`, `at: str`, `outcome: str`, `raw: Any`, `caller_name: str | None = None`, `ended_at: str | None = None`. Exported from `aiogotoconnect.sms`.
- `client.CallHandler = Callable[[InboundCall], Awaitable[None] | None]`.
- `ResolvedOptions.watch_calls: bool = False`, `ResolvedOptions.call_poll_interval_s: float = 120.0`; `create_goto_sms(..., watch_calls: bool = False, call_poll_interval: float = 120.0)`.
- `GoToSms.on(event: Literal["message", "call"], fn)`, `off` likewise; `GoToSms._emit_call(call: InboundCall) -> None` (async), which calls the `"call"` handlers.
- `processor.call_handlers` becomes generic: `call_handlers[T](handlers: Iterable[Callable[[T], Awaitable[None] | None]], item: T, logger)`. Behaviour unchanged.

- [ ] **Step 1: Failing tests** in `tests/sms/test_calls.py`:
  - `test_on_call_registers_and_emits_in_order`: two handlers (one sync, one async) registered with `on("call", …)` on a `create_goto_sms(watch_calls=True, …)` instance; `await sms._emit_call(call)` calls both, in registration order, with the same object.
  - `test_call_handler_exception_does_not_stop_the_next`: first handler raises; second still runs; one WARNING record whose message contains neither the caller number nor the name.
  - `test_message_handlers_do_not_get_calls`: an `on("message")` handler is not called by `_emit_call`.
  - `test_unknown_event_still_raises`: `on("ring", f)` raises `ValueError` naming `'message'` and `'call'`.
  - `test_on_call_without_watch_calls_warns_once`: `watch_calls=False`, two `on("call", …)` registrations → exactly one WARNING containing `watch_calls=True`.
  - `test_off_call_removes_the_handler`.
  - In `test_smoke.py`: `InboundCall` is in `aiogotoconnect.sms.__all__`.
- [ ] **Step 2:** Run `pytest tests/sms/test_calls.py -W error`; expect failures (no `InboundCall`).
- [ ] **Step 3:** Implement per Interfaces. Call handlers live in a second insertion-ordered dict, `self._call_listeners`, beside `self._listeners`.
- [ ] **Step 4:** Full check (Global Constraints commands); all green.
- [ ] **Step 5:** Commit `feat(sms): InboundCall, watch_calls option and on("call") registration`.

### Task 3: `CallWatcher`: one look at the call reports

**Files:**
- Create: `src/aiogotoconnect/sms/inbound/calls.py`
- Modify: `tests/sms/fake_goto.py`
- Test: `tests/sms/test_calls.py`

**Interfaces:**
- Consumes: `InboundCall`; `GoToHttp.json(method, path, body, *, query)`; `ConfigResolver.resolve()` (`.account_key`, `.from_number`); `GoToSmsStore`; `normalize_phone`; `log.iso_ms`; `poller._ms` (import it; do not copy).
- Produces:
  - `class CallWatcher.__init__(self, *, http: GoToHttp, config: ConfigResolver, store: GoToSmsStore, tenant: str, emit: Callable[[InboundCall], Awaitable[None]], interval_s: float, now: Callable[[], float], logger: logging.Logger)`.
  - `async tick() -> int` (calls delivered; concurrent callers share the in-flight look, as `Poller.tick`), `start() -> None`, `stop() -> None`, `async idle() -> None`, property `running: bool`.
  - Module constants `SUMMARIES = "/call-events-report/v1/report-summaries"`, `REPORTS = "/call-events-report/v1/reports/"`, `CURSOR_KEY`, `SINCE_KEY`, `SEEN_KEY`, `ERROR_KEY` with the Global Constraints key names.
  - `FakeGoTo.calls: list[dict[str, Any]]`, each `{"id", "direction", "created", "ended"?, "outcome"?, "dialled", "caller_number"?, "caller_name"?, "extra_participants"?}`, served by both routes in the observed GoTo shape (summaries filtered by `startTime`/`endTime`; summaries never include the dialled number). `FakeGoTo.call_report_hits: list[str]` records each request path.

**The tick, in order** (spec "How it works inside"; the only algorithm the tests do not fully determine):
1. Resolve config; without `account_key` or `from_number`, return 0.
2. No `calls:cursor`: write now to `calls:cursor` and `calls:since`, return 0.
3. Window = max(cursor − 15 min, now − 24 h) … now. Fetch summaries (paged or not per Task 1).
4. Oldest first by `callCreated`. Skip ids in `seen`. Not `INBOUND` → mark seen. Fetch the report; on failure leave it unseen and continue.
5. Pick the `PHONE_NUMBER` participant whose `type.number` normalises to the sending number. None → mark seen.
6. `callCreated` before `calls:since` → mark seen. Caller non-empty and same caller delivered within 120 s (checked against `seen`, so it holds across ticks) → mark seen.
7. Build `InboundCall`, `await emit(call)`, then append `[id, caller, at_ms]` to `seen` and save it. One call at a time.
8. After the loop save `calls:cursor` = now. Evict from `seen` only entries older than the reach-back window, oldest first, down to 400.

- [ ] **Step 1: Fake endpoints.** Add the two routes and `calls` / `call_report_hits` to `FakeGoTo`.
- [ ] **Step 2: Failing tests** (rig: `MemoryStore`, a `Clock`, `create_goto_http` against the fake, as `tests/sms/test_poller.py` builds its rig; `got: list[InboundCall]`):
  - `test_first_tick_imports_nothing`: a call 1 minute old exists; first `tick()` returns 0, `got == []`, `calls:cursor` and `calls:since` are set.
  - `test_inbound_call_to_the_sending_number_is_delivered_once`: after the first tick, add a call; `tick()` → 1; `got[0]` has `id`, `from_ == "+18015550123"`, `to == FROM`, `outcome == "MISSED"`, `caller_name`, `at`; three more ticks → still one.
  - `test_call_to_another_number_on_the_account_is_not_delivered`, `test_outbound_call_is_not_delivered` (and no report fetch for it).
  - `test_double_ring_in_one_tick_is_one_event`; `test_double_ring_across_two_ticks_is_one_event` (same caller, 60 s apart, different ids, a tick between).
  - `test_same_caller_three_minutes_later_is_a_second_event`.
  - `test_withheld_number_is_delivered_with_empty_from`; `test_two_withheld_callers_a_minute_apart_both_deliver`.
  - `test_call_started_before_since_is_not_delivered` (created 5 min before the first tick, reported after).
  - `test_failed_report_fetch_is_retried_next_tick` (`fake.fail_next("GET", REPORTS, 500)`).
  - `test_caller_name_is_collapsed_and_cut` (`"  Evil \n  Name" + "x"*60` → single spaces, length 40); `test_unnormalisable_caller_number_is_cut_to_20`.
  - `test_handler_error_in_emit_does_not_redeliver` (emit raises → call still marked seen, next call still delivered).
  - Review Focus 1: `test_transferred_call_with_two_phone_participants_is_delivered_once`.
  - Review Focus 2: `test_odd_shapes_never_raise_and_cursor_advances` (items missing; an item `None`; no id; `callCreated: "nope"` → delivered with `at` = now).
  - Review Focus 3: `test_many_calls_to_other_lines_do_not_evict_recent_seen` (450 calls to another line plus one to ours inside the window; three ticks → one event, and the second tick fetches no reports).
  - Review Focus 4: `test_changing_the_sending_number_takes_effect_next_tick`.
  - `test_no_account_key_or_number_makes_no_requests`.
- [ ] **Step 3:** Run; expect failures (no module).
- [ ] **Step 4:** Implement `CallWatcher` (lifecycle copied in shape from `Poller`: `_run`, `_safe_tick`, `_tick_done`, shielded shared tick). Errors from the summaries request propagate out of `tick()` for now (Task 4 handles them).
- [ ] **Step 5:** Full check; green.
- [ ] **Step 6:** Commit `feat(sms): CallWatcher polls call reports and emits one event per inbound call`.

### Task 4: Refusals, back-off and status

**Files:**
- Modify: `src/aiogotoconnect/sms/inbound/calls.py`, `src/aiogotoconnect/sms/server/status.py`, `src/aiogotoconnect/sms/__init__.py`
- Test: `tests/sms/test_calls.py`, `tests/sms/test_handlers.py` (status route)

**Interfaces — Produces:**
- `status.CallsStatus(TypedDict)`: `watching: bool`, `error: NotRequired[IncomingError]`; `StatusPayload.calls: NotRequired[CallsStatus]`. `CallsStatus` exported from `aiogotoconnect.sms`.
- `build_status` adds `calls` only when `options.watch_calls` and a core exists: `watching` = a cursor exists and no error is stored; `error` read from `calls:error` with the existing `_as_incoming_error`.
- `CallWatcher` error handling inside the tick, on the summaries request:
  - `GoToApiError` with status 403, or `LoginExpiredError` (what `GoToHttp` raises after a refresh and a second 401) → store `{"error": "calls_scope", "message": <exact scope message>, "gotoStatus": 403 or 401}`, do not move the cursor, and do not look again for 3600 s.
  - other `GoToApiError` → `{"error": "goto_api", "message": "GoTo API error <status>", "gotoStatus": <status>}`; `TemporaryError` → `{"error": "goto_unreachable", "message": "GoTo could not be reached"}`. Normal interval.
  - A good look deletes `calls:error`.
  - `tick()` returns 0 on these and does not raise. The scope WARNING is logged when the error is first stored, not on every look.

Note for the implementer: `GoToHttp` itself never writes the login-expired flag; only a refused token refresh does (`oauth.py`), and that is a genuinely dead login. So no change to `http.py` is needed. The risk the spec names is the refresh on every look, which the hourly back-off removes. The back-off is "skip the request while `now − last refused < 3600 s`", held in memory on the watcher, so a restart looks once.

- [ ] **Step 1: Failing tests:**
  - `test_403_sets_calls_scope_and_a_good_look_clears_it` (asserts the exact message and `gotoStatus == 403`; cursor unchanged while refused).
  - `test_repeated_401_never_marks_login_expired_and_backs_off`: summaries always answer 401; run 5 ticks 120 s apart → the token endpoint was hit at most once more than before, `store.get(T, "login_expired")` is not `True`, summaries were requested exactly once (two attempts inside `GoToHttp`), `calls:error.error == "calls_scope"`; advance the clock 3600 s → it looks again.
  - `test_500_sets_goto_api_and_keeps_the_normal_interval`; `test_network_failure_sets_goto_unreachable` (`fake.hang_next` with a short timeout, or a closed fake).
  - `test_scope_warning_is_logged_once`.
  - `test_texting_still_works_while_calls_are_refused`: with summaries answering 403, a `Sender.send` through the same `GoToHttp` returns `sent`.
  - Status: `test_status_has_no_calls_key_when_off`; `test_status_calls_watching_true_after_a_good_look`; `test_status_calls_carries_the_error` (both `sms.status()` and `GET /api/status`).
- [ ] **Step 2:** Run; expect failures.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Full check; green.
- [ ] **Step 5:** Commit `feat(sms): report call-report refusals in status and back off while refused`.

### Task 5: Wire it into the client

**Files:**
- Modify: `src/aiogotoconnect/sms/core.py`, `src/aiogotoconnect/sms/client.py`
- Test: `tests/sms/test_calls.py`, `tests/sms/helpers.py` (only if `connected_sms` needs to pass the new options through; it takes `**opts` already)

**Interfaces:**
- Consumes: `CallWatcher` (Task 3), `GoToSms._emit_call` (Task 2).
- Produces: `Core.calls: "CallWatcher | None" = None`. `_build` creates it when `options.watch_calls` (with `emit=self._emit_call`, `interval_s=o.call_poll_interval_s`) and starts it after the process lock is claimed, in every incoming mode. `close()` stops it and awaits `idle()` next to the poller.

- [ ] **Step 1: Failing tests** (through `connected_sms(fake, watch_calls=True, call_poll_interval=…)`):
  - `test_end_to_end_call_reaches_the_handler`: start, add a call to the fake, drive `sms.core.calls.tick()`, handler got it.
  - `test_watch_calls_off_makes_no_call_report_requests`: start, wait one text-poll interval, `fake.call_report_hits == []`, `sms.core.calls is None`.
  - `test_watcher_runs_in_webhook_mode_and_when_incoming_is_off`.
  - Review Focus 5: `test_close_waits_for_a_running_call_handler`: handler blocks on an event; `close()` does not finish until it is released; afterwards the call's id is in `calls:seen` and no store write happens after `close()` returns.
  - `test_start_twice_starts_one_watcher`.
- [ ] **Step 2:** Run; expect failures.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Full check, plus the whole suite once: `.venv/bin/python -m pytest -W error` (sip tests included) and `.venv/bin/python -m pytest e2e`.
- [ ] **Step 5:** Commit `feat(sms): start and stop the call watcher with the client`.

### Task 6: Docs, version, pull request

**Files:**
- Modify: `README.md`, `CHANGELOG.md`, `CLAUDE.md`, `pyproject.toml`, `examples/fastapi-app/app.py`

- [ ] **Step 1:** `pyproject.toml` version `0.5.0`. CHANGELOG row `v0.5.0` (dated the day of the commit). README: a "Calls" subsection after "Things to know" with the three-line example from the spec, the two options in the options table, the scope (`cr.v1.read`, enable and reconnect), "every inbound call, filter on `outcome`", at-least-once keyed on `call.id`, caller name is untrusted, the app must cap anything a stranger can trigger (a text per call), nothing before switch-on is delivered. CLAUDE.md: `inbound/calls.py` in Key Files, one Gotcha line (refused → hourly, never touches the login). Example app: an `on("call")` handler that prints, behind `watch_calls=True`.
- [ ] **Step 2:** Full check and `python -m build` (or the publish workflow's build step) succeeds.
- [ ] **Step 3:** Commit `docs(sms): calls section, 0.5.0`.
- [ ] **Step 4:** Push `feat/call-events`; open a pull request **against `feat/sms-web`** titled `feat(sms): calls to the sending number as events (0.5.0)`, body: what it adds, that it is stacked on #7, the list of calls from the spec's "Calls made here" section, test count. Confirm CI is green.

---

## After this plan (separate, in `~/dev/todo-board`)

Install the branch commit into the board's venv as #7's is installed today, then the board change in the spec's "The board afterwards" section, checked against a throwaway copy before the live service. Node and the setup page are a later design.
