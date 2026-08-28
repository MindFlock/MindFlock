# Extensions & hooks

MindFlock has four extension seams, from zero-code to full in-process:

1. **Shell hooks** — drop an executable in `~/.mindflock/hooks/<event>/`; it runs
   on every matching session event. Any language, no MindFlock imports.
2. **The `/api/events` WebSocket** — an external tool (script, dashboard, bot)
   subscribes to the same event stream over a socket, with replay on reconnect.
3. **In-process addons** — a Python `Addon` (routes + lifecycle + manifest) plus
   an optional ES module the frontend loads generically. The **notify** addon
   (`backend/web/addons/notify.py` + `static/addons/notify.js`) is the
   worked example for sections 1–3.
4. **Extensions (Addon API v3)** — an addon that also contributes UI the way a
   VSCode extension does: one sidebar bar, commands in the palette, and
   dialog/pane surfaces it renders into host-owned containers, all declared in
   a manifest. Discovered from `~/.mindflock/extensions/`, toggled on
   Settings → Extensions. The built-in **Database Client** is the worked
   example for section 4.

All four are fed by the same server-side event bus
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
| `session.turn_ended` | A session's work really is over — corroborated work, idle ever since, nothing queued | `data: {idle_for}` |

Addon-originated events (see `AppContext.emit`) live under the `addon.`
namespace, e.g. `addon.notify.ping`. Notable transitions:

- **agent needs you**: `session.activity_changed` with `new == "clarify"`
- **agent has finished**: `session.turn_ended` — NOT `session.activity_changed`
  with `new == "idle"`. The activity flip is a chip colour: the CLI's Stop hook
  fires at the end of *every* assistant turn, so a ten-turn conversation flips
  it ten times, and a window that has merely been re-opened flips it once
  without having run anything at all. `session.turn_ended` is the fact worth
  acting on — it asserts that the agent's work was corroborated (its CLI's own
  hook report, or the live-turn status line on its pane — never a CPU spike on a
  parked session) in its current tmux
  incarnation, has been idle continuously for the evidence-tiered dwell
  (`server._TURN_END_DWELLS`: 12s hook-armed, 25s status-line-armed, 45s for
  the CPU backstop — see [web-api.md](web-api.md)),
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

## 4. Extensions (Addon API v3)

An **extension** is an addon that also contributes UI the way a VSCode
extension does: one left-sidebar bar with buttons, commands, and dialog/pane
surfaces whose bodies it renders — all declared in a static manifest, so the
host draws every piece of chrome without running extension code. The manifest
is a new `extension` key on the addon's `/api/addons` row; v2 addons
(`FrontendDescriptor` + `slots.js`) keep working untouched because `slots.js`
never reads that key. The host is `frontend/src/extensions/` (`host.ts` is the
React-free runtime; `types.ts` mirrors the backend dataclasses and *is* the
contract). The built-in **Database Client** (`backend/web/addons/dbclient/` +
`backend/web/static/extensions/dbclient/`) is the worked example throughout,
and the last subsection walks through a minimal third-party extension.

### Principles

1. **Declare, don't inject.** Everything an extension adds — bar, buttons,
   commands, dialog/pane surfaces — is declared in a static manifest served by
   `GET /api/addons`. The host renders all chrome without executing extension
   code, which is what lets a bar button and a palette entry exist before the
   module has ever loaded.
2. **Fixed contribution vocabulary.** Four slots only: one sidebar `bar`,
   `dialog` surfaces, `pane` surfaces, and a row on Settings → Extensions. No
   DOM outside the host-provided containers.
3. **Commands are the universal verb.** Every action is a command id
   (`<ext-id>.<verb>`). Bar buttons reference commands; the command palette
   lists every command of every enabled extension; a command either opens a
   declared surface (listable *and runnable* without loading the extension) or
   runs code the extension registers at activation.
4. **Lazy activation, uniform lifecycle.** The ES module loads on first use.
   `activate(api)` runs once; every registration returns `{dispose()}`; the
   host drains all disposables when the extension is disabled or errors.
5. **Failure containment.** Every extension callback runs in a host
   `try/catch` attributed to the extension id (`[extension <id>] …` in the
   console, one toast, and the Settings row shows the error). Frontend
   containment is complete; a *discovered* extension's backend (router, bus
   subscriptions) stays loaded until the server restarts — stated in the UI
   and here, not hidden.
6. **One small versioned API.** `activate(api)` receives one deep-frozen
   object per extension (fresh sub-objects; the manifest a frozen
   `structuredClone`). `api_version` in the manifest is the **minimum host API
   level** required; the host refuses activation when its own level is lower,
   and its level is bumped on every addition to the API.
7. **Namespaced everything.** Events `addon.<id>.*`; REST under `/api/<id>/`;
   browser storage `mfx:<id>:`; static assets `/extensions/<id>/`; CSS class
   prefix `.mfx-<id>`. Server-side persistence is the extension's own affair
   (own endpoints + own file under `~/.mindflock/`, mode 0600) — there are NO
   per-extension groups in settings.json.
8. **Native look, mostly automatic.** The host renders bar/dialog/pane chrome.
   Extension CSS loads via a host-injected
   `<style>@import url("…/style.css") layer(components);</style>`, so a sloppy
   selector loses to the theme layer instead of beating the whole app, and
   every selector is namespaced under `.mfx-<id>`.
9. **Trusted, same-origin code (v1 trust model).** Extensions are local code
   with the same trust level as `~/.mindflock/hooks/`. No iframe sandbox in
   v1; documented as the escape hatch if a marketplace ever exists.
10. **Legacy never breaks.** v2 addons work untouched; v3 is a new `extension`
    manifest key that `slots.js` ignores.

### The manifest (`Addon.extension() -> ExtensionSpec`)

`Addon` gained one method, `extension()`, returning an `ExtensionSpec` — or
`None`, the default, so every existing addon is unchanged. The dataclasses
live in `backend/web/addons/base.py` and are serialized verbatim (`to_dict()`)
into the addon's `/api/addons` row under `extension`. The Database Client's,
as built:

```python
ExtensionSpec(
    module="/extensions/dbclient/index.js",   # the ES module the host imports
    bar_label="Database",
    buttons=[
        ExtensionButton(command="dbclient.explorer", label="Explorer",
                        title="Browse connections, tables and data"),
        ExtensionButton(command="dbclient.sql", label="SQL",
                        title="Open the SQL query pad"),
    ],
    commands=[
        # Declarative: the host opens the surface before the module has loaded.
        ExtensionCommand(id="dbclient.explorer", title="Database: Explorer", surface="main"),
        ExtensionCommand(id="dbclient.add-connection", title="Database: Add connection",
                         surface="main", ref="new"),
        # Code-backed: the module registers the handlers in activate().
        ExtensionCommand(id="dbclient.sql", title="Database: SQL query pad"),
        ExtensionCommand(id="dbclient.new-query", title="Database: New query"),
    ],
    surfaces=[
        ExtensionSurface(id="main", kind="dialog", title="Database Client"),
        ExtensionSurface(id="query", kind="pane", title="SQL", multi=True,
                         back_command="dbclient.explorer"),
        ExtensionSurface(id="table", kind="pane", title="Table", multi=True,
                         back_command="dbclient.explorer"),
    ],
    stylesheet=True,   # host loads <module dir>/style.css into layer(components)
    api_version=1,     # MINIMUM host API level this module needs
)
```

#### `ExtensionSpec`

| Field | Meaning |
|---|---|
| `module` | URL of the ES module the host `import()`s on first use. A built-in's lives under `backend/web/static/extensions/<id>/` and rides the main static mount; a discovered extension's `frontend/` dir is mounted at `/extensions/<id>/` — so the URL is `/extensions/<id>/index.js` either way. |
| `bar_label` | Label of the sidebar bar. |
| `buttons` | `ExtensionButton` list, rendered left to right on the bar. May be empty. |
| `commands` | `ExtensionCommand` list — the palette entries, and the only things a button may reference. |
| `surfaces` | `ExtensionSurface` list — the dialogs and panes the extension may open. |
| `stylesheet` | `True` → at activation the host injects `<module dir>/style.css` into `layer(components)`. Default `False`. |
| `api_version` | The **minimum** host API level the module was written against (default `1`). See `HOST_API_VERSION` below. |

#### `ExtensionButton`

| Field | Meaning |
|---|---|
| `command` | The command id the click runs. Must be one of this extension's `commands` — a button carries no code. |
| `label` | Button text. |
| `title` | Tooltip (optional, default `""`). |

#### `ExtensionCommand`

| Field | Meaning |
|---|---|
| `id` | `<ext-id>.<verb>`, the verb matching `[a-z0-9][a-z0-9-]*`. The bar, the palette and `api.commands.*` all speak this id. |
| `title` | Palette text, in the `"Database: Explorer"` style — the extension's noun first, so its entries read as a group. |
| `surface` | Optional. Set → the command is **declarative**: the host opens that surface itself, from the manifest, without loading the module. Unset → the command runs the handler the module registers at activation. |
| `ref` | Optional instance ref for a declarative open (meaningful only with `surface`). The Database Client uses `ref="new"` to open its dialog straight onto the connection form. |

#### `ExtensionSurface`

| Field | Meaning |
|---|---|
| `id` | Surface id — same slug rule as an extension id, unique within the extension. |
| `kind` | `dialog` (the modal popup) or `pane` (a grid window). House vocabulary — never "popup"/"window". |
| `title` | Default chrome title; `SurfaceHost.setTitle` overrides it per instance. |
| `multi` | Pane only. `True` → many live instances at once; the host mints an opaque ref per instance when the opener passes none. |
| `back_command` | Pane only. The host draws a **Back** button in the pane head that runs this command (the verify-pane-back precedent — both Database Client panes go back to the explorer). |

**Id rules.** Extension ids and surface ids match `^[a-z0-9][a-z0-9-]*$`
(`EXTENSION_ID_RE`) — deliberately narrow, because an id doubles as a URL
segment, an event namespace and a CSS class. Command ids match
`^<ext-id>\.[a-z0-9][a-z0-9-]*$`. `validate_extension_spec(addon_id, spec)`
returns every problem in one list: a bad id; a duplicate surface or command
id; an unknown `kind`; `multi` or `back_command` on a dialog; a `back_command`
naming no command; a button referencing no command; a `ref` without a
`surface`; a declarative command opening an unknown surface; and a
declarative command opening a `multi` surface without an explicit `ref` — the
manifest defines no ref-minting, so such a command has no single instance it
could mean (pin one, or make the command code-backed and call `openPane`). It
returns rather than raises because its two callers want opposite severities:
a built-in's bad spec is developer error in this repo and `register_addons`
raises at import, while a discovered extension's bad spec must only skip that
extension.

**What the `/api/addons` row carries.** Alongside the v2 keys, every addon row
now has `extension` (the spec, or `null`), `enabled` (the absence of an
opt-out — see *Enabling and disabling*) and `origin` (`"builtin"` | `"user"`,
stamped by the registrar, never declared by the extension). `useExtensions()`
(`frontend/src/state/queries.ts`) selects the rows with a non-null
`extension`; the sidebar, the palette, Settings → Extensions and the host all
read that one cache, so none of them can disagree about which extensions
exist.

### Discovery and layout

User extensions live in `$MINDFLOCK_EXTENSIONS_DIR` (default
`~/.mindflock/extensions/`), one directory each:

```
~/.mindflock/extensions/<dir>/
├── extension.py     # def build(ctx) -> Addon
└── frontend/        # optional; mounted at /extensions/<id>/
    ├── index.js     # the ES module named in ExtensionSpec.module
    └── style.css    # loaded when stylesheet=True
```

`discover_extensions(ctx)` (`backend/web/addons/__init__.py`) scans the
subdirectories in name order and, for each one containing `extension.py`,
imports it under the module name `mindflock_ext_<dir>` and calls its
`build(ctx) -> Addon` with the same `AppContext` the built-ins get. The
addon is then checked before it is accepted: its `id` must match the id
regex, must not be in `RESERVED_EXTENSION_IDS` (the core `/api` segments and
namespaces — `addons`, `instances`, `settings`, `events`, `config`,
`providers`, `devices`, `logs`, `aliases`, `extensions`, `assistant`,
`server`, `auth`, `core`, `vendor`, `static`, `api`, `m` — plus every
built-in addon id), and must not collide with an already-registered addon or
an earlier user dir; its `ExtensionSpec`, if it has one, must validate.
Containment is per directory: any failure — an import error, a `build` that
raises or returns something that is not an `Addon`, a `router` that raises
when touched, an unsafe id, an invalid spec — logs `extension <dir> failed to
load: <err>` and skips that extension, never the server. An accepted addon is
stamped `origin = "user"`.

`register_addons` includes the built-ins first (raising on a built-in's
invalid spec), then the discovered extensions in dir-name order, and mounts
each discovered extension's `frontend/` at `/extensions/<id>` — all before
the main static catch-all, which is what lets those mounts win. Discovery
runs **once at server import**: a new extension, or an edit to
`extension.py`, needs a restart (the Settings screen says so). The frontend
is different — the module is fetched when it is first activated, so editing
`index.js` and reloading the page is enough.

**Test isolation.** `tests/conftest.py` pins `MINDFLOCK_EXTENSIONS_DIR` to an
empty temp dir at import (with `setdefault`, so a run that deliberately
targets a fixture tree can still export its own), so pytest never executes the
developer's real `~/.mindflock/extensions/`. Any hand-run `TestClient` or
server MUST do the same, alongside `MINDFLOCK_SETTINGS_FILE` (the settings
store) and — whenever the Database Client is involved —
`MINDFLOCK_DBCLIENT_FILE` (its profile file); otherwise it reads and writes
the owner's real `~/.mindflock/`. The Vite dev server proxies `/extensions`
to the backend (`vite.config.ts`), so extension modules load in development
too.

### Enabling and disabling

Extensions are on by default. The only per-extension state settings.json
carries is the opt-out list, `extensions.disabled` (`ExtensionsSettings` in
`backend/config/settings.py`): `POST /api/settings {"extensions":
{"disabled": ["dbclient"]}}` replaces the list, an empty list clears it, and
`Addon.manifest()` reads the store fresh on every `/api/addons` fetch (through
a local `load_settings` import — the house pattern for settings readers), so
a flip lands without a restart. Settings → Extensions shows one row per
extension — label, id, origin, bar label and command count, an enable switch,
and the last activation error the host recorded. Its switch writes the list
and refetches the manifests; it never talks to the host directly.

The host takes its enabled set from the manifest query, not from the toggle:
an `App.tsx` effect feeds `syncExtensions(list)` every successful
`useExtensions()` result, and that call deactivates every record whose
extension is now disabled or gone from the manifest. This is the one path a
disable takes whether it was made in this tab, in another tab, or by hand in
the settings file, so the host can never disagree with what the server says
is enabled. It is guarded on a *successful* query on purpose: an in-flight
first fetch or a failed refetch must not tear every extension down — typed
SQL and dirty grid cells included — over a network hiccup.

Disabling drains the extension's registrations, disposes every pane and
dialog body, closes its grid windows and its dialog if that is what is open,
removes its stylesheet and deletes its record. Re-enabling starts from
scratch, which is also how a failed activation is retried: a record in the
`error` state stays failed for the life of the page (so a failing module is
not re-imported and re-toasted on every click), and the Settings row says to
turn it off and on again. The caveat, printed on the row of every discovered
extension: this tears down the **frontend** only. A discovered extension's
backend — its router, its bus subscriptions, its `on_startup` work — stays
loaded until MindFlock restarts, because Python modules do not unload and a
half-removed router would be worse than a present one.

### The module and `activate(api)`

The module default-exports — or, the house idiom shared with v2 addons,
registers on `window.mindflockExtensions[<id>]` — an object with
`activate(api)`, which may be async. The host imports it on first use (a bar
button, a palette entry, a surface being mounted) and awaits `activate` once;
every caller of the same extension awaits the same promise. Before importing,
the host compares the manifest's `api_version` against its own
`HOST_API_VERSION` (`frontend/src/extensions/host.ts`, currently `1`) and
refuses with `needs host API level N (this app provides M)` when the
extension asks for more than the host has. The rule behind the number:
**`api_version` is the minimum host level the module was written against**,
and the host bumps its level on every *addition* to the API object — so an
extension that uses a member added at level 2 declares `api_version=2` and
degrades to one clear error on an older app instead of a `TypeError`
mid-click. Names are never renamed or removed: `ExtensionApi` and
`SurfaceHost` below are the forever contract, and `types.ts` is the file
that pins it.

```ts
interface ExtensionApi {
  readonly id: string;
  readonly apiVersion: number;          // the HOST's level (HOST_API_VERSION)
  readonly manifest: ExtensionSpec;     // structuredClone of the manifest, deep-frozen
  ui: {
    registerSurface(surfaceId: string, renderer: SurfaceRenderer): Disposable;
    openDialog(surfaceId?: string, ref?: string, ctx?: unknown): void;   // default: first dialog surface
    closeDialog(): void;                // GUARDED: no-op unless the open dialog is this extension's
    openPane(surfaceId: string, opts?: {ref?: string; title?: string; ctx?: unknown}): string; // pane key, "" on error
    closePane(surfaceId: string, ref?: string): void;
    toast(msg: string, opts?: {duration?: number}): void;
  };
  commands: {
    register(commandId: string, handler: (...args: unknown[]) => void): Disposable; // own prefix enforced
    run(commandId: string, ...args: unknown[]): Promise<void>;  // own commands only (v1)
  };
  events?: MindflockEvents;   // window.mindflock.events — shared, feature-detect
  sessions?: () => unknown[]; // window.mindflock.sessions — shared under the v1 trust model
  request(path: string, opts?: RequestInit & {json?: unknown}): Promise<unknown>; // the app's api() wrapper
  storage: { get<T>(key: string, fallback: T): T; set(key: string, value: unknown): void };
  log: { error(msg: string, err?: unknown): void };
}

interface SurfaceHost {
  el: HTMLElement;        // host-owned keep-alive container (class "ext-surface mfx-<id>")
  surfaceId: string;
  ref?: string;           // the instance ref (host-minted for multi panes)
  ctx?: unknown;          // the opts.ctx passed at open — in-memory only
  setTitle(title: string): void;
  close(): void;
}
type SurfaceRenderer = (host: SurfaceHost) => Disposable | void;
interface Disposable { dispose(): void; }
```

What each member does, and the rules the host enforces:

- **Frozen, per extension.** The api object and its `ui`, `commands`,
  `storage` and `log` sub-objects are fresh per extension and frozen; the
  manifest is a deep-frozen `structuredClone`. `events` and `sessions` are the
  *shared* live objects (`window.mindflock.events` mutates `lastSeq` and
  `connected` for the whole app) and are handed over unfrozen, and only when
  present — feature-detect them. Under the v1 trust model an extension sees
  the same session snapshot the SPA does.
- **`ui.registerSurface`** binds a renderer to a declared surface id. An
  instance already adopted into a pane and waiting for its renderer starts
  the moment it is registered, so registration order inside `activate` does
  not matter. A surface opened without a renderer ever being registered
  renders the error `surface "<id>" was never registered`.
- **`ui.openPane`** returns the full pane key (`"<ext>:<surface>[:<ref>]"`)
  or `""` after an attributed error — the surface is not a `pane`, or a `ref`
  was passed for a single-instance surface. For a `multi` surface with no
  `ref` the host mints one (`#1`, `#2`, … per surface — opaque; never parse
  it). The same surface + ref opened again *reveals* the existing pane and
  applies the new `title`; the body is never re-rendered. Opening a pane also
  closes this extension's own dialog if that is what is on top: the dialog →
  pane flow must not strand the new window behind a modal (the VerifyDialog
  precedent). `ui.closePane` on a `multi` surface requires the ref.
- **`ui.openDialog`** with no `surfaceId` opens the extension's first
  `dialog` surface. There is one shared modal (`ExtensionDialog`, store
  `DialogName` `"extension"`) and the target string rides in `dialogTarget`,
  so opening a different surface or ref of the same extension replaces the
  body. `ui.closeDialog` is a no-op unless the dialog on screen belongs to
  the caller — no extension can swat someone else's modal.
- **`commands.register`** refuses (attributed error, no-op disposable) an id
  without the `<ext-id>.` prefix. **`commands.run`** runs the extension's
  *own* commands only in v1 and resolves immediately otherwise. A registered
  handler always wins the routing (next subsection), which is what makes
  `run` safe from inside your own `activate()` — provided the command is
  registered *before* it is run; running a not-yet-registered command during
  `activate` is reported instead of deadlocking on the activation promise.
- **`request`** is the app's own `api()` wrapper: same-origin `fetch` with
  the session cookie; `{json: …}` turns the call into a JSON `POST`; the
  parsed body is returned; a non-2xx throws `ApiError`; a 401 reloads the
  page. Talk to your own `/api/<id>/…` routes through it.
- **`storage`** is `localStorage` under `mfx:<id>:<key>`, JSON-encoded,
  every read and write in a `try/catch` (a private window simply returns the
  fallback). Per-browser convenience state only — server-side persistence is
  your own file under `~/.mindflock/`.
- **`log.error`** prefixes `[extension <id>]`, so a broken extension reads as
  itself in the console rather than as a host bug.
- **`SurfaceHost.setTitle`** retitles the pane's grid slot, or the dialog's
  heading. **`SurfaceHost.close`** closes this instance (a pane by its key; a
  dialog through the guarded close), which disposes it.

### Commands, the bar and the palette

The bar is 100% host-rendered (`ExtensionBar.tsx`: `.ext-bar#ext-bar-<id>`,
a label, and one `.ext-btn` per manifest button with its `title` as tooltip),
so no extension code runs until a button is clicked. Its sidebar key is
`"ext:" + id`; it drags and hides exactly like the built-in bars because it
lives in the same `BarSlot` machinery and the footer Customize menu, and it is
visible by default. Ordering (`orderedSections`, `barDefs.ts`): a saved order
is honored for the keys it knows; a missing *built-in* key is appended at the
tail, as before; a missing *extension* key is inserted immediately **above
the session list**, so a freshly installed extension lands where the eye is
even for a user with a years-old saved order.

The command palette adds one action per command of every enabled extension —
labelled with the command's `title`, hinted with the extension's label — from
the same `useExtensions()` cache. Listing needs no extension code.

Every entry point — a bar button, a palette action, the pane-head Back
button, `api.commands.run` — goes through `runCommand(extId, commandId)`,
whose routing decision is a pure function (`routeCommand`, unit-tested) with
four outcomes, tried in order:

1. **A registered handler** → invoke it (try/catch; a throw is attributed and
   toasts `Extension <label>: <command> failed`).
2. **Else a declarative manifest command** (one with `surface`) → open that
   surface from the manifest alone — `dialog` → the shared dialog, `pane` →
   a grid pane — and start activation in the background, because the body
   will need the module. This is why the Database Client's Explorer opens
   instantly on a cold page.
3. **Else the module is idle or loading** → await activation, then invoke the
   handler if it is registered now; if the module activated without
   registering it, an attributed error and an `unknown command` toast.
4. **Else** (active or failed, with neither a handler nor a surface) → an
   attributed error.

A command of a disabled or unknown extension is refused with an attributed
error. Activation failure itself is contained the same way: status `error`,
the message recorded (the Settings row shows it), one toast `Extension
<label> failed: <msg>`, and every registration from the partial `activate`
drained, so a half-activated extension cannot keep half-working.

### Surfaces and the keep-alive contract

Dialog targets and pane keys share one format, `"<ext>:<surface>[:<ref>]"`
(`buildTarget` / `parseTarget`). Parse with `indexOf` slices, never
`split(":")`: extension and surface ids cannot contain colons, but a ref is
an opaque token that may.

**The host owns every surface instance's root element.** `SurfaceHost.el` is
a detached `<div class="ext-surface mfx-<id>">` created in the host's module
registry — the same idiom as the terminals registry. The React shell
(`ExtPaneBody` for panes, `ExtensionDialog` for the dialog) *adopts* it with
`appendChild` on mount and *detaches* it — without disposing — on unmount,
and renders nothing of its own inside the mount point, so React never
reconciles around nodes it does not own. A grid drag, a row reflow, the grid
re-keying its panes: each unmounts and remounts the React shell, and the
extension's DOM, with everything closed over it, is untouched. That is the
whole reason typed SQL and dirty grid cells survive a drag with no
save/restore code in the extension.

The lifecycle that follows from it:

- **The renderer runs once per instance**, at first mount, after activation
  has resolved. Its return value (a `Disposable`, or nothing) is kept with
  the instance.
- **Dispose happens only on an explicit close or on deactivation**: the pane's
  Close button, `SurfaceHost.close`, `api.ui.closePane`, the dialog closing,
  or the extension being disabled or leaving the manifest. Never on unmount.
  `dispose()` runs in a host try/catch and the element is then removed.
- **Dialog bodies are transient.** The one dialog body is kept for the *same
  target* while the dialog stays open (a re-render of the modal does not
  restart it) and disposed when the dialog closes or another target of the
  same extension replaces it — closing a dialog *is* the explicit close. An
  extension that wants a dialog to reopen where it left off keeps that state
  in module variables; the Database Client's explorer caches its expanded
  tree, fetched levels and selection per connection this way and repaints
  from them.
- **Every post-await mount checks an epoch token.** Each open takes a fresh
  token; when activation resolves, the mount compares its token against the
  live registry, and a stale instance — Escape pressed during a slow module
  load, or the extension disabled meanwhile — disposes itself and renders
  nothing, rather than rendering into limbo.
- **While loading or failed, the shell speaks.** The pane body and the dialog
  body show `Loading <label>…` until the renderer has run, and an activation
  or render failure inline as `<label> failed: <why>` with a pointer to
  Settings → Extensions.

Pane chrome (`SpecialPane.tsx`, kind `"ext"`): the standard `.pane-head` with
grip, live title, an optional host-rendered **Back** button when the surface
declares `back_command` (tooltip: that command's title), and Close. Pane
slots live in the UI store (`extPanes`, keyed by pane key; `openExtPane` is
idempotent by key) and are deliberately **not persisted**, like the verify
panes: the keep-alive DOM behind them lives only in this page, so a reload
could only restore an empty shell — the extension reopens them on demand.
While an extension dialog is open, `"extension"` sits in
`MODAL_DIALOG_NAMES`, so the app's global shortcuts stand down: a stray
Delete or Ctrl+W meant for an editable grid must never reach the session
behind it.

Styling: the host puts `.mfx-<id>` on every surface root, so an extension's
selectors have a guaranteed ancestor (`.mfx-<id> .toolbar`), and its
`style.css` is imported into `layer(components)` when `stylesheet=True`. Use
the app's tokens (`--panel`, `--panel-2`, `--border`, `--text`, `--muted`,
`--accent`, …) rather than colours and the surface follows the theme and
every accent for free. Icons are inline monochrome SVG with `currentColor`
strokes; no emoji in chrome.

### Worked example: the Database Client

`dbclient` (label "Database Client") is a DBeaver-style client inside
MindFlock and the reference for every piece above. Its manifest is the code
block at the top of this section: a **Database** bar with Explorer and SQL,
two declarative commands and two code-backed ones, one dialog, and two
`multi` panes whose Back button runs the explorer command.

**Surfaces.**

- **`main` — the Explorer dialog.** Left, the connection tree: connections →
  [databases → [schemas →]] tables and views, one lazy level per `/tree`
  call, an approximate row-count badge only when the engine had a cheap
  estimate, a filter box and a Reload. Right, a context panel that follows
  the selection: the connection list with New connection; the connection
  form (an engine strip SQLite | PostgreSQL | MySQL, per-engine fields, a
  read-only toggle, Test and Save, and a driver-missing notice carrying the
  install hint) — `ref="new"` opens the dialog straight onto it; or a table's
  column summary with View Data / New Query / DDL. Expanded nodes, fetched
  levels and the selection live in module state keyed by connection, so a
  reopened dialog repaints instantly even though dialog bodies are disposed
  on close. Opening a pane from here goes through `api.ui.openPane` with
  `ctx = {connId, database, schema, table}`, and the host closes the dialog.
- **`table` — the table pane** (`multi`, Back → explorer): DATA | STRUCTURE |
  DDL. DATA is the editable page grid — header-click sort, a per-column
  filter row, checkbox selection, double-click editing with a Set-NULL
  control, Insert / Delete selected / Save, CSV/JSON export through a plain
  anchor GET, and a footer with prev/next and "rows X–Y" (" of ~N" only when
  the server had an estimate). Save is a two-step conversation with `/rows`:
  `preview: true` returns the generated SQL, shown in a confirm bar; the
  confirmation resends for real. Views and tables without a primary key
  render read-only with a badge saying why. Bytes cells are chips; strings
  past 8 KB get a magnifier to a read-only overlay; the first 200 columns
  render, with a notice (`RENDER_CAP` in `grid.js`). Identity comes from
  `host.ctx`; a pane opened with none (from the palette) asks with a picker
  first.
- **`query` — the SQL pane** (`multi`, Back → explorer): connection, database
  (and schema, where the engine has them) selectors; a monospace textarea;
  Run (Ctrl/Cmd+Enter — the statement under the caret, found by the splitter
  in `sql.js`); Run All (sequential, stops on the first error); a history of
  the last 50 statements in `api.storage`; CSV/JSON export of the statement
  at the caret; results in a read-only grid with an info line (`n rows · x
  ms` / `affected n` / a truncation note) or the error inline. The typed SQL
  and the last result live in the keep-alive DOM. `dbclient.sql` reveals the
  newest open query pane — the module tracks the host-minted refs in a `Map`,
  since only it knows which panes exist — and opens one only when none is
  open; `dbclient.new-query` always opens a fresh one.

**Endpoints** (`APIRouter(prefix="/api/dbclient")`; every handler is `async
def` and pushes adapter work through `asyncio.to_thread`, so a slow database
never stalls the event loop):

| Route | Purpose |
|---|---|
| `GET /drivers` | `{drivers: [{engine, available, driver, install_hint}]}`. Drivers are import-detected, never required; sqlite always works. |
| `GET /connections` · `POST /connections` · `DELETE /connections/{id}` | Profile CRUD. Reads mask `password` with `SECRET_MASK`; a write carrying the mask (or `""`) keeps the stored one; a *new* id with a masked password on a password engine is a 400 naming the field. |
| `POST /connections/{id}/test` | `{ok, error?, server?}` |
| `GET /connections/{id}/tree?database=&schema=` | One lazy level following the engine's hierarchy: `{level: "databases" \| "schemas" \| "tables", items}`. Table items are `{name, kind, approx_rows \| null}` — never a `COUNT(*)`. |
| `GET /connections/{id}/table?database=&schema=&table=` | `{columns: [{name, type, nullable, default, pk, autoinc}], indexes: [{name, columns, unique}], ddl, kind}` |
| `POST /connections/{id}/query` | `{sql, database?, schema?, max_rows?, timeout_s?, confirm?}` → `{ok, columns: [{name, type}], rows, row_count, affected, elapsed_ms, truncated, needs_confirm?, error?}` |
| `POST /connections/{id}/table-data` | `{database, schema?, table, page, page_size, sort: [{column, dir}], filters: [{column, op, value}]}` → `{columns, pk, kind, rows, page, page_size, has_more, total_approx \| null}`. `has_more` comes from fetching one row past the page; `total_approx` only from a cheap estimate, and never once a filter is set. |
| `POST /connections/{id}/rows` | `{database, schema?, table, operations: [{action: insert \| update \| delete, values?, where_pk?}], preview}` — one transaction. `preview: true` returns `{ok, statements: [{sql, params}]}` without executing. |
| `GET /connections/{id}/export?database=&schema=&table=&format=csv\|json` | Streams a whole table. A plain GET, so an anchor download carries the session cookie and nothing is buffered. |
| `POST /connections/{id}/export` | Ad-hoc SQL export (`{sql, database?, schema?, format?, timeout_s?}`) through the same chokepoint and the 10 000-row cap; `{ok: false, error}` on failure. |

Error shapes, chosen for the JS on the other side: a bad request is a 400
`{"error": …}` (plus `"field"` for profile validation); an unknown connection
a 404; a database failure while *introspecting* a 502; a database failure
while *running SQL* a 200 `{"ok": false, "error": …}`, so the query pad shows
it inline like any other result.

**Safety rules**, all in one place — `service.py`, the single execution
chokepoint behind the query pad, the table page, the row batch and export:

- **Concurrency.** Connections are cached per `(profile id, database,
  profile fingerprint)`, each guarded by a `threading.Lock` held for the
  whole duration of any use — the only reason
  `sqlite3.connect(check_same_thread=False)` is safe. The fingerprint means
  an out-of-band edit to `dbclient.json` misses the cache instead of reusing
  stale credentials; every profile write drops that profile's cached
  connections; a handler error rolls back and drops the dead connection so
  the next call reconnects. Connect timeout 5 s. `on_shutdown` closes every
  pool.
- **Read-only profiles** are enforced at connect, per engine: sqlite opens
  `mode=ro`, PostgreSQL gets `default_transaction_read_only=on`, MySQL runs
  `SET SESSION TRANSACTION READ ONLY`.
- **Guards on user SQL.** A quote/comment-aware scanner (the Python twin of
  the frontend's `sql.js` — the client's splitter is a convenience, never the
  guard) counts statements and rejects more than one server-side, and powers
  the no-WHERE guard: an `UPDATE`/`DELETE` without `WHERE`, judged **after**
  stripping strings and comments (so `SET note = 'where'` cannot fool it),
  answers `needs_confirm` until the client resends with `confirm: true`.
  Statement timeouts default to 30 s and clamp to 1–300 s; `max_rows`
  defaults to 500 and clamps to a hard ceiling of 10 000, with `truncated`
  saying when the cap bit.
- **Row batches** run in one transaction and roll back on the first failure.
  Update and delete require a primary key — no where-all-columns fallback, so
  tables without one are read-only in v1; NULL key values match with `IS
  NULL`; any update/delete touching ≠ 1 row is *stale* (the row changed
  elsewhere) and rolls the whole batch back, so nothing is ever half-saved.
- **Value codec.** Outbound cells are made JSON-safe: `Decimal` → str, dates →
  isoformat, bytes → `{"$type": "bytes", "len": n}` (not editable), strings
  past 8 KB → `{"$type": "truncated", "text": head, "len": n}`, NULL →
  `null`, which stays distinct from `""`. Inbound edits are coerced by the
  column's declared type, and `{"$null": true}` binds SQL NULL.
- **Identifier safety.** Every table and column a client names is checked
  against the introspected schema *before* any SQL is generated; sort
  directions and filter operators (`eq ne contains gt lt null notnull`) come
  from whitelists; every identifier goes through the adapter's `quote_ident`;
  values are always bound parameters — the service never interpolates a
  value into SQL text.
- **Persistence.** Profiles live in `~/.mindflock/dbclient.json`
  (`MINDFLOCK_DBCLIENT_FILE` overrides it), mode 0600, written atomically
  (tempfile + `os.replace`, the `save_settings` pattern) — never in
  settings.json, never through `/api/settings`.

It emits `addon.dbclient.query` with `{connection, elapsed_ms, ok}` — timing
and outcome only, never SQL text.

Files: `backend/web/addons/dbclient/{__init__,store,adapters,service}.py`
(manifest and router; profile store; one adapter per engine; the chokepoint)
and `backend/web/static/extensions/dbclient/{index,explorer,tableview,
querypad,grid,sql,ui}.js` + `style.css` (entry; the three surfaces; the
shared grid; pure SQL text utilities; DOM helpers and the monochrome SVG icon
set). Tests: `tests/unit/test_dbclient.py` (sqlite-backed, every guard above)
and `frontend/src/__tests__/dbclientSql.test.ts` (the splitter).

### A minimal third-party extension

Two files make a working extension: `extension.py` (the manifest and, if you
want one, a router) and `frontend/index.js` (the renderers and handlers).
This one adds a **Hello** bar whose button opens a dialog showing a greeting
fetched from the extension's own route, plus a palette-only command that
toasts.

`~/.mindflock/extensions/hello/extension.py`:

```python
"""hello — a minimal Addon API v3 extension: one bar, one dialog, two commands."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend.web.addons.base import (
    Addon,
    ExtensionButton,
    ExtensionCommand,
    ExtensionSpec,
    ExtensionSurface,
)


class HelloAddon(Addon):
    id = "hello"          # a slug: URL segment, event namespace, CSS prefix
    label = "Hello"

    def __init__(self, ctx=None):
        super().__init__(ctx)
        self._router = APIRouter(prefix="/api/hello")   # namespaced under the id

        @self._router.get("/greeting")
        async def greeting() -> JSONResponse:
            return JSONResponse({"text": "Hello from the server"})

    @property
    def router(self) -> APIRouter:
        return self._router

    def extension(self) -> ExtensionSpec:
        return ExtensionSpec(
            module="/extensions/hello/index.js",      # served from frontend/
            bar_label="Hello",
            buttons=[ExtensionButton(command="hello.open", label="Open", title="Say hello")],
            commands=[
                # Declarative: the host opens the dialog before index.js has loaded.
                ExtensionCommand(id="hello.open", title="Hello: Open", surface="main"),
                # Code-backed: index.js registers the handler in activate().
                ExtensionCommand(id="hello.toast", title="Hello: Toast"),
            ],
            surfaces=[ExtensionSurface(id="main", kind="dialog", title="Hello")],
            api_version=1,
        )


def build(ctx) -> Addon:
    return HelloAddon(ctx)
```

`~/.mindflock/extensions/hello/frontend/index.js`:

```js
/** hello — entry module. The host imports this lazily and calls activate(api)
 * once; everything registered here is disposed when the extension is disabled. */

const extension = {
  activate(api) {
    // The dialog body. host.el is the host-owned container: build plain DOM
    // inside it and return a dispose() for anything that outlives the element.
    api.ui.registerSurface("main", (host) => {
      const p = document.createElement("p");
      p.textContent = "Loading…";
      host.el.appendChild(p);
      api.request("/api/hello/greeting").then(
        (r) => {
          p.textContent = r.text;
        },
        (err) => {
          api.log.error("greeting failed", err);
          p.textContent = "Could not reach the server.";
        }
      );
      return {
        dispose() {
          p.remove();
        },
      };
    });

    // A code-backed command: the palette entry "Hello: Toast".
    api.commands.register("hello.toast", () => {
      api.ui.toast("Hello from " + api.id);
    });
  },
};

// The house idiom: default-export AND register on the window, so either
// loading path finds activate().
window.mindflockExtensions = window.mindflockExtensions || {};
window.mindflockExtensions.hello = extension;
export default extension;
```

Restart MindFlock. A **Hello** bar appears just above the session list (drag
or hide it from footer Customize); **Open** runs `hello.open` — the host
opens the dialog from the manifest, imports `index.js`, awaits `activate`,
then runs the `main` renderer into the body — and the palette lists "Hello:
Open" and "Hello: Toast". Settings → Extensions shows the row with its
`~/.mindflock/extensions` origin and its switch. If `extension.py` is wrong,
the server log says `extension hello failed to load: <why>` and nothing else
changes; if `index.js` throws in `activate`, the dialog shows the error inline
and the Settings row records it. To add a grid window, declare a
`kind="pane"` surface (`multi=True` for several at once) and call
`api.ui.openPane("<surface>", {title, ctx})` from a handler; to add styles,
ship `frontend/style.css` with every selector under `.mfx-hello` and set
`stylesheet=True`.

### Non-goals (v1)

No extension keybindings or `when` clauses; no marketplace or signing; no
iframe isolation (documented as the escape hatch if a marketplace ever
exists); no OS windows; extension panes are not persisted across a reload;
`commands.run` reaches an extension's own commands only; a discovered
extension's backend unloads only on restart. Database Client: no ER diagram,
mock data or structure diff; no SSH tunnels, Redis or Mongo (roadmap); tables
without a primary key are read-only; binary cells are read-only.

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
| `mindflock.slotNumber(title)` | The session's **slot number** in the rail — `"1"`…`"9"`, or `""` when the row shows none (tenth row on, filtered out, verify sessions). The rail's numbers are how people locate a window, so a notification carrying one must show the number the rail shows *right now* — drag order and the live filter applied. Same publish point and the same feature-detect rule as `displayName`; the notify addon renders it as a `[3] ` prefix on the name. |
| `mindflock.toast(msg, opts?)` | Show a toast. Assigned by `app.js` (F3), so it's present whenever the SPA is loaded — still feature-detect in addon modules for non-SPA pages. `opts` is optional: `{onClick, duration}` makes the toast clickable and/or overrides its lifetime (ms). |
