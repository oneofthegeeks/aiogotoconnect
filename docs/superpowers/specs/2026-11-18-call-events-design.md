# Calls to the sending number as events (`sms.on("call")`)

Status: draft for the maintainer's review, 2026-11-18. Target release: 0.5.0, as a pull request stacked on #7 (`feat/sms-web`).

## Intent and why

the maintainer does not answer his GoTo line. His to-do board turns each call to that number into a "call back" item and a
text. The GoTo half of that lives in the board (`~/dev/todo-board/calls.py`), written by hand against the call-reports
API. His standing rule is that GoTo work lives in his library and apps never hand-roll it, so the next app that wants
"someone called" should get it the way it gets "someone texted": one handler, no GoTo knowledge.

This design moves the GoTo half into `aiogotoconnect.sms`. Python first. Node (`@oneofthegeeks/goto-sms`) and the shared
setup page follow as a second piece of work, sketched at the end.

## Goals

- An app using `create_goto_sms` can opt in and receive one event per inbound call to its sending number.
- The app needs no knowledge of GoTo's call-reports API, its quirks, or its token.
- A missing scope or a refused request is visible in `status()` in words an admin can act on.
- The to-do board ends up with no GoTo HTTP code of its own.

## Non-goals

- What to do about a call. Creating an item, texting the owner, capping how often, quiet hours: all the app's.
- Calls to other numbers on the account, outbound calls, voicemail audio or transcripts, live (ringing) notification.
- Any change to the setup page or the Node package in this release.
- Recording calls in the message log (`sms.message_log` stays texts only).

## Anti-goals (a failure even if it works)

- Importing old calls the first time it is switched on. The first look starts from now.
- Treating caller ID as trusted. The name is whatever the caller's carrier sent and can be crafted.
- Calls to a colleague's line on the same account reaching the handler.
- Breaking or slowing texting when the call-reports API fails, is refused, or changes shape.
- A cost for apps that do not opt in: no extra requests, no new required scope.

## Constraints

- Lands on top of pull request #7; nothing in #7 changes behaviour. Passes ruff, `mypy --strict` and
  `pytest -W error` as the texting module does.
- Python and Node share `contract/http.md` in goto-sms, and Node wins where they disagree. The only shared surface this
  touches is one new optional key in the status payload (below).
- The setup page is vendored byte for byte and is not edited here.
- The call-reports API is partly documented. What is known was observed on one account on 2026-11-18 and is recorded in
  "What GoTo gives us". Anything outside that is unknown and must fail soft.
- The token comes from the existing login. GoTo scopes are chosen on the OAuth client, not at sign-in, so the admin
  must enable `cr.v1.read` and reconnect.

## Approaches considered

1. **Event only (chosen by the maintainer, 2026-11-18).** The library polls call reports and emits `on("call")`. The app
   decides everything else. Mirrors `on("message")`, smallest surface, straightforward to port to Node.
2. **Event plus a built-in text.** The library would also text a configured number on a missed call, with wording, an
   hourly cap and quiet hours as options. Rejected: that is app policy, the board already has its own, and it would
   override most of it.
3. **A separate `aiogotoconnect.calls` module.** Cleaner name, usable without the texting setup. Rejected: it repeats
   the store, the polling loop, the token handling and the status that `sms` already has, and Node has no such split.

## What the app sees

```python
sms = create_goto_sms(..., watch_calls=True)          # off by default

async def on_call(call):                              # sync handlers work too
    print(call.from_, call.caller_name, call.outcome)

sms.on("call", on_call)
```

New options on `create_goto_sms` (keyword-only):

| Option | Default | Notes |
|---|---|---|
| `watch_calls` | `False` | Poll call reports and emit `"call"`. Needs scope `cr.v1.read` on the OAuth client. |
| `call_poll_interval` | `120.0` | Seconds. Call reports appear a little after a call ends, so faster buys nothing. |

New event. `on` and `off` accept `"message"` or `"call"`; any other name raises as today. Registering a `"call"`
handler without `watch_calls=True` is allowed and logs one warning, since nothing will ever arrive.

```python
@dataclass(frozen=True)
class InboundCall:
    id: str                 # GoTo's conversationSpaceId
    from_: str              # caller, E.164 when it normalises, else GoTo's text cut to 20 characters; "" if withheld
    to: str                 # the number dialled: always the configured sending number
    at: str                 # when the call started (GoTo's callCreated, ISO)
    outcome: str            # GoTo's callerOutcome as sent, e.g. "MISSED"; "" if absent. Not an enum: the set is undocumented
    raw: Any                # the full report, for hosts that want more
    caller_name: str | None = None   # carrier caller ID, whitespace collapsed, cut to 40 characters. Untrusted data
    ended_at: str | None = None
```

Every inbound call to the sending number is delivered, answered or not; the app filters on `outcome`. (The board
records all of them today and notes the outcome when it is not a miss.)

Handlers follow the rules texts already have: called one at a time in registration order, an exception is logged and
the call stays delivered, no timeout.

`status()` and `GET /api/status` gain one optional key, present only when `watch_calls` is on:

```json
"calls": { "watching": true, "error": { "error": "calls_scope", "message": "…", "gotoStatus": 403 } }
```

`watching` is true once a poll has succeeded and no error is set. `error` has the same shape as `incoming.error` and is
cleared by the next good poll. Codes: `calls_scope` (GoTo answered 401 or 403: "GoTo refused the call reports. Enable
the scope cr.v1.read on the OAuth client, then reconnect."), `goto_api` (any other status), `goto_unreachable`.

Exported from `aiogotoconnect.sms`: `InboundCall`. README gains a short "Calls" section; CHANGELOG a 0.5.0 row.

## What GoTo gives us (observed 2026-11-18)

- `GET /call-events-report/v1/report-summaries?accountKey&startTime&endTime` → `{"items": [...]}`. Each item:
  `conversationSpaceId`, `callCreated`, `callEnded`, `direction` (`INBOUND` / `OUTBOUND`), `callerOutcome`,
  `caller {name, number}`. It does **not** say which of the account's numbers was dialled.
- `GET /call-events-report/v1/reports/{conversationSpaceId}` → `participants[]`. For an outside call in, one participant
  has `type.value == "PHONE_NUMBER"`, `type.number` = the number **dialled**, `type.caller {name, number}` = who called.
- One ring can be reported as two conversations.
- A report appears some time after the call ends, not when it rings.
- Without `cr.v1.read` the request is refused with 401 or 403.
- Timestamps mix precisions (as the messaging API does); compare parsed instants.

**Checked live on 2026-11-19 (UTC) through the board service, 13 inbound calls, key names and true/false only:**

- `report-summaries` pages: the answer carries `pageSize` (default 100) and `nextPageMarker`; the next page is asked
  for with `pageMarker`. Follow it as the text poller does.
- The summary **does** say which number was dialled after all: for an inbound call `caller.type.value` is
  `PHONE_NUMBER` and `caller.type.number` is the dialled number (equal to the full report's in 13 of 13).
  `caller.name` and `caller.number` equal the report's caller. So the watcher reads the summary alone and fetches the
  full report only when `caller.type.number` is missing. `raw` on the event is the summary (the report when fetched).
- For an outbound call `caller.type.value` is `LINE`.
- `callerOutcome` values seen: `MISSED`, `NORMAL`, `LEFT_DIAL_PLAN` (answered by a dial-plan step, here voicemail or
  an AI receptionist: `callAnswered` is set). The set is not documented, so it stays a plain string.
- GoTo's guide says a report "becomes available as soon as a call completes" and that a `REPORT_SUMMARY` push
  subscription exists; polling is kept because the push needs a reachable webhook or an open socket.

Still not known: the rate limit, whether a missing scope answers 401 or 403, and what a summary looks like when the
caller withholds their number. These are handled defensively: both statuses are treated alike, and a call dialled to
the sending number is delivered whether or not `caller.number` is present. (The board drops such calls today; that is
a bug this fixes.) Never start a second library instance on the board's store to look: it would contest the lock and
could rotate the live refresh token.

## How it works inside

One new module, `sms/inbound/calls.py`, holding a `CallWatcher` shaped like `Poller` (start, stop, idle, a shared
in-flight tick, `_safe_tick` so nothing kills the loop). `client.py` builds it when `watch_calls` is on, starts it in
`start()` and stops it in `close()`. It runs in both incoming modes (webhook and polling): calls have no webhook path
here. It uses the existing `GoToHttp`, so token refresh and error mapping are shared with texting.

Each tick:

1. Resolve config. No account key or sending number: skip quietly.
2. No cursor in the store: save now as both the cursor and `calls:since`, and stop. Old calls are never imported.
3. Ask for summaries from (cursor − 15 minutes) to now. The reach-back covers reports that arrive late. Clamp the
   reach-back to 24 hours, as the text poller does, and never deliver a call that started before `calls:since`. A call
   already ringing at the moment watching is first switched on can therefore be missed; that is accepted.
4. For each summary, oldest first, that is `INBOUND` and whose id is not already seen: read the dialled number and the
   caller from the summary (fetching the full report only when the summary lacks the dialled number). Not an outside
   call, or dialled to another number: mark seen, move on. If a needed report fetch fails, leave it unseen so the next
   tick tries again.
5. Double ring: if the same caller number already produced an event within 120 seconds (across ticks, not only within
   one), mark seen and skip. A withheld number (`from_ == ""`) is never treated as a repeat: two different hidden
   callers must both arrive.
6. Emit the event, then mark the id seen and save, one call at a time.
7. Save the cursor (now) and clear any error. On a failed summaries request, save the error and leave the cursor.

**A refusal must never touch the login.** `GoToHttp.json` answers a 401 by forcing a token refresh, retrying, and
raising `LoginExpiredError` on a second 401. If GoTo reports a missing scope as 401, a naive watcher would force a
refresh every tick and could mark the login expired, which stops texting. So: call-report requests may use the one
refresh-and-retry, but a failure on these paths never writes the login-expired state, is always reported as
`calls_scope`, and while `calls_scope` is set the watcher looks only once an hour. If that needs a small no-refresh
option on `GoToHttp` (a file from #7), add it there with its own test.

Store keys (tenant-namespaced, like `poll:cursor`): `calls:cursor` (ISO), `calls:since` (ISO, written once), `calls:seen` (the last 400 entries of
`[id, caller, at_ms]`, which serves both the seen check and the double-ring check), `calls:error`.

Delivery promise: **at least once, and in practice once.** Seen is saved after the handlers run, so a crash between a
handler and the save can deliver that one call again on restart. Texts have a stronger pending-row mechanism because a
lost STOP matters; a repeated "call back" does not, and the simpler rule is the one to port. Hosts that must not act
twice key on `call.id`. This is stated in the README.

Logging: failures at warning with the error name only; the scope refusal once per change, not every tick. Caller
numbers and names never appear at INFO or above, matching the rule for text bodies.

One process per store still applies; two processes would both deliver.

## Changed during the build (2026-11-18, after the live check and the final review)

- The reach-back is **2 hours**, not 15 minutes: the list is searched by when a call started and a report appears only
  when it ends, so 15 minutes would have lost every call longer than that.
- The double-ring rule needs the **same outcome** as well as the same caller within 120 seconds, and applies only to a
  caller number that normalises to E.164. A voicemail followed by a missed call is two events.
- The hourly back-off while refused ends as soon as the stored login changes (a reconnect or a renewed token).
- The first look makes one request over an empty window, so a missing scope shows at once.
- A fourth error code, `login_expired`, for a login that is really dead.
- A summary whose caller has no `type` at all is settled by the full report.

## Tests

`tests/sms/fake_goto.py` learns the two endpoints. `tests/sms/test_calls.py` covers: first tick imports nothing; an
inbound call to the sending number is delivered once across repeated ticks; a call to another number on the account is
not; outbound is not; the double ring within one tick and across two ticks gives one event; a failed report fetch is
retried next tick; 403 sets `calls.error` with `calls_scope` and a later good tick clears it; caller name is collapsed
and cut; a withheld number still delivers, and two withheld callers a minute apart both deliver; a call that started before `calls:since` is not delivered; repeated 401s never set login-expired, never refresh more than once per look, and slow the watcher to hourly; a handler exception does not stop later calls; `watch_calls=False` makes no
call-report requests; `close()` waits for a tick in flight; status has no `calls` key when off. No conformance rows
yet: those are added to the shared checklist when Node lands.

## The board afterwards (separate small change in todo-board, after 0.5.0 is installed)

- `sms_app.py`: `create_goto_sms(..., watch_calls=True)`, `goto.on("call", …)` calling the board's `record` and text
  logic; the `watch_calls` loop and `calls.poll` go.
- `calls.py` shrinks to the board's half: `record`, `pretty`, the text with its six-an-hour cap, `last_call`. Its
  `get`, `dialled_and_caller`, `poll`, cursor and seen list are deleted along with `calls.json`'s cursor fields.
- `sms.py` reads the state line for the page from `status()["calls"]` instead of `calls.state()`.
- The guide and `CLAUDE.md` lose the "one piece still outside the library" lines; board item S5 is removed.

## Later: Node and the setup page (not this release)

Port `calls.py` to `calls.ts` with the same options, event, store keys and status key; add the `calls` key to
`contract/http.md` and rows to the conformance checklist; give the setup page a "Calls" line that shows whether
watching is on and, when refused, the scope to enable. Whether the page gets a switch (so an admin can turn watching on
without a code change) is a decision for that design.

## Calls made here that the maintainer has not explicitly decided

- Off by default, behind `watch_calls=True`, so `cr.v1.read` does not become a required scope for every app.
- All inbound calls are delivered with the raw `outcome`, not missed calls only.
- Double-ring suppression works across ticks (the board's works only within one poll).
- At-least-once delivery with no pending-row machinery.
- Calls that began before watching was first switched on are never delivered, at the cost of possibly missing one that
  was ringing at that moment. This also means the board's cutover creates no duplicate "Called again" lines.
- Calls from a withheld number are delivered (with an empty `from_`); the board ignores them today.
- While GoTo refuses the call reports, the watcher slows to one look an hour and never marks the login expired.
- The status key `calls` is added in Python now and written into the shared contract when Node is done, not before.
- The module lives under `sms/` although calls are not SMS, because that is where the login, store and status are.

## Left to the builder

Internal names, the exact reach-back and clamp constants, how `seen` is encoded, the wording of log lines and of the
README section, and the paging code once the live check has shown whether the summaries list pages.
