# Extensions & hooks

MindFlock has three extension seams, from zero-code to full in-process:

1. **Shell hooks** — drop an executable in `~/.mindflock/hooks/<event>/`; it runs
   on every matching session event. Any language, no MindFlock imports.
2. **The `/api/events` WebSocket** — an external tool (script, dashboard, bot)
   subscribes to the same event stream over a socket, with replay on reconnect.
3. **In-process addons** — a Python `Addon` (routes + lifecycle + manifest) plus
   an optional ES module the frontend loads generically. The **notify** addon
   (`backend/web/addons/notify.py` + `static/addons/notify.js`) is the
   worked example throughout this doc.

All three are fed by the same server-side event bus
(`backend/web/core/events.py`).

## The event envelope

Every event — on the bus, over the WebSocket, and on a shell hook's stdin — is
one JSON envelope:

```jsonc
{
  "seq": 42,                          // monotonically increasing per server process
  "event": "session.status_changed",  // see vocabulary below
  "session": "sc-19815",              // session title ("" for non-session events)
  "old": "loading",                   // previous value (null when n/a)
  "new": "running",                   // new value (null when n/a)
  "ts": 1719900000.0,                 // unix seconds
  "data": {}                          // event-specific extras
}
```

Core vocabulary (emitted by the server):

| Event | When | `old` → `new` |
|---|---|---|
| `session.created` | A session is created | — → initial status |
| `session.deleted` | Killed / closed / cleaned up | `data` may carry `{"closed": true}` or `{"cleaned": true}` |
| `session.paused` / `session.resumed` | Pause / resume lifecycle | — |
| `session.status_changed` | `running·ready·loading·paused` flips | statuses |
| `session.activity_changed` | `working·clarify·idle·offline` flips | activities |
| `session.stage_changed` | `provisioning·agent·precommit·interrupt·committed·pushed·pr` flips | stages |
| `session.budget_exceeded` | Estimated cost first crosses `general.session_budget_usd` | `data: {cost, budget}` |
| `session.prompt_sent` | The queue drain loop auto-sends a prompt | `data: {text, remaining, loop}` |
| `session.queue_changed` | Any prompt-queue edit | `data: {pending, enabled, loop}` |
| `session.usage_restored` | A provider window reopened for a session that had run out | `data: {resumed}` |
| `session.turn_ended` | A session's work really is over — observed working, idle ever since, nothing queued | `data: {idle_for}` |
| `session.profile_changed` | A session's auth profile (the identity its agent runs as) was hot-swapped | `data: {profile_id}` — `""` inherit app default, `"default"` the CLI's own login |

Addon-originated events (see `AppContext.emit`) live under the `addon.`
namespace, e.g. `addon.notify.ping`. Notable transitions:

- **agent needs you**: `session.activity_changed` with `new == "clarify"`
- **agent has finished**: `session.turn_ended` — NOT `session.activity_changed`
  with `new == "idle"`. The activity flip is a chip colour: the CLI's Stop hook
  fires at the end of *every* assistant turn, so a ten-turn conversation flips
  it ten times, and a window that has merely been re-opened flips it once
  without having run anything at all. `session.turn_ended` is the fact worth
  acting on — it asserts that the agent was observed working in its current tmux
  incarnation, has been idle continuously for `server._TURN_END_DWELL_S` (45s),
  and has no queued prompt waiting to wake it. Emitted once per cycle of
  observed work, so a session that then sits idle all night says it once
- **PR merged/closed**: `session.stage_changed` with `old == "pr"` (an open PR
  is stage `pr`; merging or closing it moves the stage off `pr`)
- **out of usage**: `session.activity_changed` with `new == "limit"` — the pane
  is showing the CLI's usage-limit screen. There is no separate "ran out" event;
  the counterpart `session.usage_restored` is emitted once per reopening (not
  once per session) by the watcher that resumes such sessions, so it can only
  fire after a real outage — never on a window that merely rolled over

The last ~100 envelopes are kept in a ring buffer for replay; `seq` survives the
buffer rolling over (it keeps counting), but not a server restart.

## 1. Shell hooks (`~/.mindflock/hooks/`)

Layout — one directory per event name, plus `all/` which runs for every event:

```
~/.mindflock/hooks/
├── session.activity_changed/
│   └── 10-notify.sh          # executable; runs in name order
├── session.stage_changed/
│   └── 20-slack.sh
└── all/
    └── 99-log-everything.sh
```

Each executable file (`chmod +x`) runs on every matching event with:

- **env vars**: `MINDFLOCK_EVENT`, `MINDFLOCK_SESSION`, `MINDFLOCK_OLD`,
  `MINDFLOCK_NEW` (`old`/`new` of `null` become empty strings)
- **stdin**: the full JSON envelope
- a **10-second budget** — a hook still running after 10s is killed
- fire-and-forget: hook failures are logged, never surfaced, never block the server

Override the root with `MINDFLOCK_HOOKS_DIR` (used by the tests).

### Example: desktop notification when an agent needs input

`~/.mindflock/hooks/session.activity_changed/10-notify.sh`:

```sh
#!/bin/sh
# Notify when a session flips to "clarify" (the agent is waiting on you).
[ "$MINDFLOCK_NEW" = "clarify" ] || exit 0
notify-send "MindFlock: $MINDFLOCK_SESSION" "The agent needs your input"
```

### Example: Slack ping when a PR opens

`~/.mindflock/hooks/session.stage_changed/20-slack.sh`:

```sh
#!/bin/sh
# Ping Slack when a session reaches the PR stage (a PR just opened for it).
[ "$MINDFLOCK_NEW" = "pr" ] || exit 0
cat > /dev/null   # drain the JSON envelope from stdin
curl -s -X POST -H 'Content-Type: application/json' \
  -d "{\"text\": \"PR open for *$MINDFLOCK_SESSION* ($MINDFLOCK_OLD -> $MINDFLOCK_NEW)\"}" \
  "https://hooks.slack.com/services/T000/B000/XXXX" > /dev/null # pragma: allowlist secret
```

(For merged-or-closed instead, test `[ "$MINDFLOCK_OLD" = "pr" ]`.)

Remember: `chmod +x` both files.

## 2. `/api/events` WebSocket (external tools)

`WS /api/events` streams every envelope as a JSON text frame. On connect the
ring-buffer backlog is replayed first; pass `?since=<seq>` with the last `seq`
you processed to skip what you already saw, then live events follow. Clients
only listen (frames you send are ignored); a slow client silently loses events
(bounded queue) rather than ever blocking the server.

### When `*_changed` events fire

`session.status/activity/stage_changed` events are **computed by diffing**, not
watched: whenever session state is refreshed, the server compares each
session's freshly computed state against its previous snapshot and emits one
event per changed field. Historically that refresh only happened while a
browser was polling `GET /api/instances` — a headless WS consumer with no UI
open got lifecycle events (`created` / `deleted` / `paused` / `resumed`) but no
`*_changed` transitions. The server now also ticks session state itself while
`/api/events` has connected clients, so the guarantee is simply: **keep a WS
connection open and you'll receive `*_changed` events — no browser needed.**

One seeding note: the first time a session is sighted after a server (re)start,
its state only *seeds* the diff snapshot — no `*_changed` event is emitted for
that first observation; the stream picks up from the session's next actual
transition.

```python
# pip install websockets
import asyncio, json, websockets

async def main():
    last_seq = 0
    while True:  # reconnect loop; ?since= makes reconnects lossless (within ~100 events)
        try:
            async with websockets.connect(f"ws://127.0.0.1:8765/api/events?since={last_seq}") as ws:
                async for frame in ws:
                    env = json.loads(frame)
                    last_seq = env["seq"]
                    print(env["event"], env["session"], env["old"], "->", env["new"])
        except OSError:
            await asyncio.sleep(2.5)

asyncio.run(main())
```

## 3. In-process Python addons

An addon is a self-contained feature: one backend module, an optional ES module
for its UI, and one registry line — zero edits to the core server.

### The `Addon` ABC (`backend/web/addons/base.py`)

```python
class Addon(abc.ABC):
    id: str = ""      # stable addon id (manifest key + window.mindflockAddons key)
    label: str = ""   # human label

    @property
    @abc.abstractmethod
    def router(self) -> APIRouter: ...          # routes + websockets; mounted
                                                # BEFORE the static catch-all
    def frontend(self) -> List[FrontendDescriptor]: ...  # UI contributions (0..n)
    async def on_startup(self, ctx: AppContext) -> None: ...
    async def on_shutdown(self, ctx: AppContext) -> None: ...
```

`on_startup` / `on_shutdown` are the lifecycle pair, awaited on the server loop
around the app's lifespan, and they are where an **in-process subscription**
belongs — a constructor runs too early to have a loop to hand work to.
The contract:

- `ctx.subscribe(...)` returns an **unsubscribe callable**. The addon must retain
  it and call it from `on_shutdown`; nothing reclaims it otherwise, so a
  re-subscribing addon accumulates live callbacks (the notify addon guards with
  `if self._unsubscribe is None`, making startup idempotent).
- Subscribe **unconditionally** and let the *callback* decide whether the feature
  is switched on. The alternative — subscribing only when configured — means a
  toggle in Settings does nothing until the next restart.
- `on_startup` runs on the server's event loop, so it is the place to capture that
  loop for later cross-thread work (`ntfy.set_loop(asyncio.get_running_loop())`).
- Both hooks are isolated: the server calls each in a `try/except` and logs a
  failure as `addon <id> startup failed`, so one broken addon neither takes the
  server down nor stops its siblings' hooks — but it *does* fail silently apart
  from that log line, so don't rely on a raise to surface misconfiguration.
  Shutdown hooks run in reverse registration order.

Optionally implement the structural `ManagedProcess` protocol
(`start/stop/status/is_running`) to get generic start/stop/logs treatment.

Register it with one line in `build_addons()`
(`backend/web/addons/__init__.py`). `GET /api/addons` then serves its
manifest: `{"id", "label", "managed", "frontend": [descriptor…]}`.

### `AppContext` v2 (the event-bus seam)

Every lifecycle hook (and the constructor) receives the shared `AppContext`:

| Member | What it is |
|---|---|
| `ctx.engine` | The process-wide session `Engine` singleton |
| `ctx.register_task(coro)` | Track a background asyncio task (cancelled on shutdown) |
| `ctx.log` | The `backend.log` module (`ErrorLog`/`InfoLog`), best-effort |
| `ctx.subscribe(event_name, cb) -> unsubscribe` | Register `cb(envelope)` for bus events named `event_name`; `"*"` matches everything. The callback runs synchronously on whatever thread emits — keep it tiny. |
| `ctx.emit(event, session="", old=None, new=None, data=None) -> envelope` | Publish an addon-originated event. The `session.*` namespace is **reserved** (raises `ValueError`); any other name is auto-prefixed with `addon.` — `ctx.emit("notify.ping")` publishes `addon.notify.ping`. Convention: `<addon_id>.<what>`. |
| `ctx.sessions() -> list[dict]` | Read-only snapshot of the sessions as last computed by the `/api/instances` poll (same dicts the SPA sees: `title`, `status`, `activity`, `stage`, `tokens`, …). Empty until the first poll; the dicts are copies. |

```python
class MyAddon(Addon):
    id, label = "mine", "Mine"

    async def on_startup(self, ctx):
        self._unsub = ctx.subscribe("session.stage_changed", self._on_stage)

    def _on_stage(self, env):
        if env["new"] == "pr":
            ...  # react; or ctx.emit("mine.pr_seen", session=env["session"])

    async def on_shutdown(self, ctx):
        self._unsub()
```

### `FrontendDescriptor`

Each `frontend()` entry is serialized verbatim into the manifest:

| Field | Meaning |
|---|---|
| `id`, `label` | Slot id + display label |
| `where` | `sidebar-bar` · `grid-pane` · `dialog` · `pane-tab` · `settings` |
| `module` | URL of the ES module that renders it (e.g. `"/addons/notify.js"` — static files mount at `/`, so that is `static/addons/notify.js`). `None` when there is nothing to load. |
| `ws_path` | WebSocket the slot's terminal pane attaches to (optional) |
| `api_base` | The addon's REST prefix (optional) |
| `poll_ms`, `read_only`, `order`, `available_flag` | Poll interval, read-only pane, sort order, status-flag gating |
| `builtin_ui` | `True` = hand-wired UI in `app.js`/`index.html`; the generic slot renderer skips it. New addons leave it `False`. |

### The module-loading contract (what `core/slots.js` does)

For every descriptor with a `module` URL and `builtin_ui: false`:

1. `sidebar-bar` descriptors get a sidebar bar rendered first
   (`#addon-bars .addon-bar[data-addon="<id>"]`), so your module can extend it.
2. The module is dynamically `import()`ed. A load failure is a `console.warn` +
   skip — it can never break the SPA.
3. If the module registered `window.mindflockAddons[<addon id>] = { init(ctx) }`
   (or default-exports such an object), `init` is called **once** with:

```js
{
  descriptor,   // this FrontendDescriptor, verbatim from the manifest
  addon,        // { id, label } of the owning addon
  events,       // window.mindflock.events (may be undefined — feature-detect)
  sessions,     // window.mindflock.sessions (function; may be undefined)
  toast,        // window.mindflock.toast (function; may be undefined)
}
```

Minimal module skeleton:

```js
// static/addons/mine.js
window.mindflockAddons = window.mindflockAddons || {};
window.mindflockAddons.mine = {
  init(ctx) {
    if (!ctx.events) return;                       // bus not available: degrade
    ctx.events.subscribe("session.stage_changed", (env) => {
      if (env.new === "pr" && typeof ctx.toast === "function")
        ctx.toast(`${env.session}: PR open`);
    });
  },
};
```

### The worked example: notify

- **Backend** `backend/web/addons/notify.py`: serves the event →
  notification rules at `GET /api/notify/config` and declares a `sidebar-bar`
  descriptor with `module: "/addons/notify.js"`.
- **Frontend** `backend/web/static/addons/notify.js`: registers
  `window.mindflockAddons.notify`; `init` adds an On/Off toggle to its sidebar
  bar (persisted in `localStorage`), requests `Notification` permission lazily
  on first enable, subscribes to `"*"` and applies the served rules — a desktop
  notification (with the session title; click focuses the tab) when a session
  hits `clarify` or its PR leaves the open stage.
- **Registry**: one `NotifyAddon(ctx)` line in `build_addons()`.

It also demonstrates the *server-side* half of the seam. `on_startup` takes
`ctx.subscribe("*", …)` and runs the same rules in-process, pushing matches to
the user's [ntfy](https://ntfy.sh) topic (`web/core/ntfy.py`) — which is how a
notification arrives with no browser open at all. Three things generalize to any
addon that reacts to events in-process:

- The subscription is registered unconditionally and the **callback** checks
  whether the feature is configured, so turning it on in Settings takes effect
  immediately instead of at the next restart.
- Bus callbacks run synchronously on whichever thread emitted, so the handler
  stays tiny and hands its HTTP call to the loop (`ntfy.publish_soon`, the same
  trampoline `EventBus._dispatch_hooks` uses for shell hooks). It also swallows
  its own exceptions — one subscriber must not break an `emit()` for everyone.
- There is no replay to guard against server-side (the bus replays only to
  reconnecting websockets) and no first-poll burst after a restart, because
  `server._emit_state_changes` seeds its snapshot on first sighting without
  emitting. The browser channel, which *does* see a replayed backlog, filters it
  with `ctx.events.isReplay(env)`.

**Invariant: the rule engine is implemented twice, by hand.** The rule *data*
(`NOTIFY_RULES`) is served from one place, but each channel evaluates it in its own
language, and those evaluators are hand-mirrored twins with no shared code and no
test that diffs them:

| `notify.py` (server / ntfy) | `notify.js` (browser / desktop) |
|---|---|
| `_matches(rule, envelope)` | `ruleMatches(rule, env)` |
| `_fill(template, envelope)` | `fill(template, env)` |
| `_DEDUPE_SECONDS = 5.0`, keyed `(session, rule.id)` | `DEDUPE_MS = 5000`, keyed `session + "|" + rule.id` |
| `aliases.display_name(title, branch)` | `window.mindflock.displayName(title)` |

`{session}` is the twin that bites hardest, because getting it wrong is silent:
the envelope carries the session's machine slug (`shortcut-21431`) and the user
reads the rail, which shows the rename if there is one and otherwise the
pipeline label (`(tix) social-scan-noise/shortcut-21431`). A push naming a window
nobody can find is worse than no push. The browser channel asks the SPA
(`window.mindflock.displayName`, the sidebar's own resolver); the server channel
runs a hand port of it (`core/aliases.session_label`, pinned against the
TypeScript original's examples in `tests/unit/test_aliases.py`).

The dedupe key is the **rule id**, not the event name — it was the event name,
and three rules ride `session.activity_changed`, so whichever matched first
swallowed the others for 5 s (`usage_limit` eating `needs_input`, both
default-on) and, because the same key doubles as the browser `Notification`
`tag`, the later popup also replaced a still-visible earlier one.

**The `session_idle` rule kept its id and changed its event.** It now matches
`session.turn_ended` with `old`/`new` both `null`, where it used to match
`session.activity_changed` with `new == "idle"`; its label, title and body
changed with it ("A session finishes its work and stays idle"). The id is
deliberately unchanged, so an existing `notifications.enabled_rules` opt-in
carries straight over and simply gets quieter — there is no settings migration.
An addon or filter that keyed on the *event name* rather than the rule id does
need updating.

Any change to rule *semantics* — a new constraint field, different `{…}`
placeholder handling, a different dedupe key or window — must land in **both**, or
the two channels quietly disagree and the same event notifies you on one channel
and not the other. (The `priority`/`tags` fields are the deliberate exception:
ntfy-only presentation, stripped from the client payload by
`_INTERNAL_RULE_FIELDS` along with `default_enabled`, since the browser gets the
already-resolved `enabled`.) A third channel should factor the evaluator out
rather than add a third twin.

## `window.mindflock` client API reference

Provided by `static/core/events.js` (loaded with the SPA); addon modules
receive the relevant pieces via `init(ctx)` but may also feature-detect the
global:

| Member | Contract |
|---|---|
| `mindflock.events.subscribe(eventNameOr"*", cb) -> unsubscribe` | `cb(envelope)` gets the full envelope for matching events (`"*"` = all) |
| `mindflock.events.lastSeq` | `seq` of the last envelope received (used for `?since=` resume) |
| `mindflock.events.connected` | Whether the `/api/events` socket is currently up |
| `mindflock.events.onStatus(cb)` | Called on connect/disconnect transitions |
| `mindflock.sessions()` | The latest instances snapshot array (same shape as `GET /api/instances`) |
| `mindflock.displayName(title)` | What a session is **called** — its rename, else the label the rail shows (`(tix) add-dark-mode/sc-12345`), else the raw title. Published by the SPA (`lib/windowName.ts`); feature-detect it, because a non-SPA page has no rail to agree with. Use it wherever a user reads a session's name: the event envelope carries the machine slug, and naming a window nobody can find under that name is worse than saying nothing. |
| `mindflock.toast(msg, opts?)` | Show a toast. Assigned by `app.js` (F3), so it's present whenever the SPA is loaded — still feature-detect in addon modules for non-SPA pages. `opts` is optional: `{onClick, duration}` makes the toast clickable and/or overrides its lifetime (ms). |
