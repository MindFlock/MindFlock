# People

A MindFlock server has always had a credential — one bearer token, plus the
fleet key your devices share — but nobody behind it. The **person** is that
somebody: one identity that all of one person's devices share, stamped on what
MindFlock does. It is an *identifier*, never a credential. Nothing is ever
allowed because a request names a person; the bearer token and device pairing
keep gating everything exactly as before.

This is the identity piece. There are no per-person permissions and no hosted
team directory. Three things build on the id described here: ownership,
presence and the driver lock (see [Owner, driver, viewers](#owner-driver-viewers)),
and, for working with **other people**, shared ticket claims (the tracker says
who is on a ticket) and shared zones (the repo carries the zones a team agreed
on), both below.

## The record

Settings `person` (`backend/config/settings.py` `PersonSettings`):

| Field | What | Synced |
|---|---|---|
| `id` | A UUID (uuid4 on the device that made it), lower-case | agreed, see below |
| `name` | How MindFlock names you (`""` = unnamed) | yes |
| `git_name` | The author name of MindFlock's **own** commits | yes |
| `git_email` | …and their email (one `@`, no spaces or `<>`) | yes |

The server makes the id at startup if the store has none, so an existing
`settings.json` gets one on its first load after upgrading (an unreadable file
is never saved over). Read and edit it with `GET` / `POST /api/person`
([web-api.md](web-api.md#people)); the id can't be set through either route or
through `POST /api/settings`. Settings → Your devices shows it as the **You**
card (name, git identity, the id small with Copy).

## One id across your devices

Ids are agreed, never edited:

1. **Pairing.** A device that joins another's group — by code, by Ask/Approve,
   or the one-click add of a device you pasted a token for — adopts the
   **inviter's** id. The pairing payloads carry the inviter's `person` beside
   the bundle (`bundle_for(...)["person"]`, and `"person"` in the
   `/api/fleet/adopt` body); the joiner takes the id before settings sync
   starts, and fills its own blank name / git fields from it. So the device
   that owns the fleet key the others paired to wins. An inviter from before
   people sends none, and nothing changes.
2. **Settings sync.** Two devices already in one group with different ids
   (both paired before people existed) settle on one the first time they
   sync: the id of the `github.automation_device` when it is one of the two,
   else the lexicographically smaller id. Each export carries
   `person: {id}`; both sides compute the same answer from the same facts, so
   they converge in one pass without negotiating. The switch is logged once.

`name`, `git_name` and `git_email` sync like any shared field (last edit wins,
see [configuration.md](configuration.md)).

## Where the person shows up

- **Requests between devices.** Every request this device proxies to another
  of yours carries `X-MindFlock-Person: <id>` beside the device key
  (`X-MindFlock-Remote`). The receiving server records it on the request state
  (`backend.web.core.auth.request_person(request)` — only when the remote
  marker is present; a browser can't name someone else) and falls back to its
  own person for its own browser / CLI.
- **Sessions.** A session created from now on records the person the create
  request was for, in web metadata beside `hidden_by`
  (`~/.mindflock/session_person.json`, never the engine's Instance JSON). It is
  the `person` field of every `/api/instances` row: `""` for sessions created
  before people existed and for peer-link sessions (the peer is not you). It
  survives close / reopen and is dropped on delete.
- **Fleet events.** `device.joined` / `device.removed` events carry `person` beside `device` (not `device.join_requested`: the asking
  device isn't one of yours yet).
- **Fleet ticket claims.** `GET /api/tickets/claims` answers
  `{device, person, claims: {<slug>: {kind, since, person}}}`, and each claim
  `fleet_claims.fleet_claims()` returns carries `person`. A claim without one
  takes the answering device's; a device from before people reads as `""`.
- **MCP.** `whoami` returns `person: {id, name}`; `list_sessions` rows carry
  `person` when the session has one, `get_session` returns the full row.

## Working with other people

Pairing joins one person's own devices. A teammate is somebody else, with
their own MindFlock, and the two are never paired — so what they share goes
through the things both of you already use.

- **Shared claims — the tracker.** Starting a ticket assigns it to your
  tracker identity (when nobody has it) and labels it `mindflock:working`
  (per source: `claim_label`, default `mindflock`); closing or shipping the
  session takes the label off and never un-assigns. Before any start —
  Begin work, the pipeline, team runs, agents — a ticket assigned to someone
  else that carries the label, or sits in a started state, reads as
  **"<name> is on this"**: refused like "running on <device>", with an
  **Override** only the owner can press. Paired-device claims are still asked
  first. See [ingestion-pipeline.md](ingestion-pipeline.md#shared-claims-a-ticket-someone-else-is-on).
- **Shared zones — the repo.** The zones you add in the Zones dialog or on
  the Map are stored on this machine and stay local. The ones a team shares
  live in a committed `.mindflock/zones.toml` at the repo root:

  ```toml
  [[zone]]
  kind = "red"            # red = agents may read, never change; green = only here
  pattern = "config.toml"
  note = "prod settings"
  author = "Ethan"        # your person name (git name when unnamed)
  added = 2026-10-10
  ```

  It is read from each session's own worktree (so it follows the branch), by
  the agents' guard, the push/PR gate and the Code Map, on top of the local
  zones. The Zones dialog's **Shared with the repo (committed)** section lists
  it with each zone's author, and **Add to repo** on a local zone appends it
  to the file in a session's worktree — not committed, so it rides that
  branch. Agents can't edit the file. A file that isn't valid TOML is ignored
  (logged once).
- **Shared branches — the remote.** In the New dialog's **Base branch**
  picker, a remote pick (`origin/x`) offers **Start a new branch from
  origin/x** (the default) or **Work on x itself (shared with whoever pushed
  it)**: the session's worktree is ON `x`, tracking `origin/x`, refused with
  a 409 naming the worktree when `x` is already checked out there. Its rail
  row carries a small ⑂ marker, and its pane head says **↓N behind** when the
  remote has commits it lacks (from the last fetch; a shared session
  re-fetches every 5 minutes in the background). Every push — shared or not
  — first fetches its branch; when somebody pushed in the meantime the push
  stops with "x moved on origin: N new commits by Ada" and a card offering
  **Rebase onto origin/x and push** (`git pull --rebase`; a conflict is
  aborted, the tree left as it was, and the files named) or **Cancel**.
  Nothing here ever force-pushes. The MCP's `ship_session` reports the same
  stop as a `diverged` step; `get_session` rows carry `shared_branch` and
  `behind`. See [web-api.md](web-api.md) (`continue_branch`,
  `/rebase-remote`).
- **Your own sessions.** Zones are a hard stop for agents only. A session YOU
  started (not agent-spawned) that commits files inside a red zone, or
  outside a green one, gets the breach card at **Commit** with **Commit
  anyway** — a warning, never a block on your keyboard.

### Guest links — someone else in a few of your sessions

The simplest way to work with someone: no second MindFlock, no sandbox. They
open a link in a browser — **from anywhere**: the link is a public address, a
Cloudflare quick tunnel to this server (`backend/web/core/guest_tunnel.py`,
the same `cloudflared` peer links use, free, no account) — and type into the
sessions you picked, on **your** device, at your agent, in your worktree;
every other session on the rail is crossed out for them.

**This is access to your computer, from the internet.** In those sessions
the guest types at your agent and your shell: they can read and change the
files in that worktree, run commands as you, and spend your agent's credits.
The block says so in red and won't make a link until you tick *I
understand*; send one only to someone you would hand your keyboard to.

**Kill the connection.** The red ⏻ button — on the People block, and on the
rail (a "N guest links · public link up" bar) whenever a link or the tunnel
is live — revokes every link and closes the public address at once
(`DELETE /api/guests/tunnel`). A guest's open terminal closes on its next
frame, not at their next page load; the same happens to one link's sockets
when you **Revoke** it alone, or narrow it to fewer sessions.

Settings → **People** → *Let someone into a few of your sessions*: tick the
sessions, name who it's for, **Make a link**. The public address takes a few
seconds (Cloudflare mints a random `https://<label>.trycloudflare.com` name;
the block polls and copies the link when it's there), and needs
`cloudflared` — missing, the block offers the doctor's one-click install.
Send them the URL (`https://<label>.trycloudflare.com/?token=…`; the same
link opens the phone page at `/m`). The name is minted per server process:
after a restart the URL changes, so copy it again from the block (the token
inside is the credential; the hostname is only where it is presented). A
*Copy on-your-network link* beside it is the tailnet/LAN address, for
someone already on your network. The block lists every link with **Copy**
and **Revoke**; the last revoke takes the tunnel down. The access gate must
be on (Settings → Security) — with it off a tunnel would be this whole
machine on the internet, so links and the tunnel are refused until it is,
and the block offers to turn it on first.

What the link holds, exactly (`backend/web/core/guests.py`):

- **Their sessions, as yours.** Everything under `/api/instances/<title>`
  for a title the link reaches — the terminal and shell, prompts, answers,
  the queue, voice, find, the family thread, waking, the diff and history —
  with the owner's calls held back: close/delete, copy, commit, push, PR,
  merge, fast-track, branch, fences, zones, owner/parent/order/profile answer
  403. A link **reaches its named sessions and their sub-sessions**: anything
  whose parent chain leads to a named title, spawned before the link or
  after it (`guests.reach`, per request). The picker lists root sessions
  only — a sub-session is never picked on its own, it comes with its parent.
- **Under your names.** A session the owner renamed is called that for the
  guest too, now and after any later rename: `GET /api/aliases` is on the
  guest's read-only list (filtered to the link's reach) and a rename is
  `session.renamed` on the bus, which every open page — the owner's other
  devices, the guest's tab — answers by re-reading the map
  (`frontend/src/lib/aliasSync.ts`). A guest cannot rename (the POST is
  403; the UI reverts).
- **Every other session: absent.** `GET /api/instances` lists only what the
  link reaches — nothing crossed out, nothing to wonder about — and every
  call on another session is `403 {"error": "not part of your invite"}`.
  The event stream carries only their sessions' events.
- **Their own name.** The owner does not name the link. The guest's first
  screen asks *What should we call you?* (`POST /api/guest/name`, required
  before their pill shows; unique on this rail — another guest's name or the
  owner's own is refused) and the name is kept on the link, so a reload
  doesn't ask again.
- **Where they are.** The guest's page reports the session it has selected
  (`POST /api/instances/<title>/focus`, on every change and every 30 s) and
  the owner's rail shows a **pill with the guest's name** on that row; it
  moves as they click (`guest.focus_changed`; `GET /api/guests` rows carry
  `focus` for a fresh page) and a toast says when they arrive. Press the
  pill to **follow** them — your selection then moves with theirs — press
  again to stop. A page silent for 90 s has left (`guests.FOCUS_TTL_S`).
- **Nothing else.** No creating sessions, no settings, no token, no devices,
  no links — a few read-only boot calls (`/api/config` says `guest:
  {label, sessions}`, which is what crosses the rail out) and the speech
  endpoints their own typing needs.
- **Not a device, not a person.** A guest token is never the access token
  (rotating yours leaves the links alone) and never the devices' key; it
  signs in with its own keyed cookie, never the plain `mf_auth`. Links name
  sessions by title, so a session closed and recreated under the same title
  is open to the link again.
- **The tunnel fronts the whole server** — the gate is the only thing
  between the internet and this machine, which is why links need it on.
  Anyone who finds the random hostname without a token gets the login page
  or a 401; the owner's token works there like anywhere. `host_ok` lets the
  name through only while the tunnel is up (`auth.allow_fronted_host`).

`GET/POST /api/guests`, `POST/DELETE /api/guests/<id>` — [web-api.md](web-api.md#guest-links).

### A small chat

When someone is on one of your sessions, a little chat window pops up
bottom-right for that session — a full-width sheet on a phone. It is for the
**people** on the session: you, a guest who came in through a guest link,
and a peer who has the session open from their own MindFlock. **Agents are
not in it** — nothing typed there reaches the terminal.

- **It opens by itself** when someone arrives on one of your sessions (the
  guest's pill appears) and, minimized with an unread count, when someone
  writes on a session whose chat isn't open. A guest's own page opens it
  when you write to them. Close it and it stays closed for that person on
  that session for the rest of the page load; someone else arriving brings
  it back as a pill, not a window. **💬 Chat** in the corner opens it by
  hand for the session in focus while someone else is on it.
- **One per session**, the session's name in the header, **▁** to minimize
  (the pill carries the unread count), **×** to close. Enter sends,
  Shift+Enter is a new line; messages are up to 2000 characters.
- **Nothing is kept.** The server holds the last 200 messages per session
  in memory (`backend/web/core/chat.py`); a restart or deleting the session
  forgets them. A chat lives where its session lives — another device's row
  has none here.
- **Across a peer link, both ways.** When you share a session with a peer
  (People → Share a session), they and you talk in the same little window:
  on their rail your session is a `<you>::<title>` row whose chat card says
  "with <you>", and it pops open when you write (a pill once they closed
  it); **💬 Chat** opens it by hand while that row is in focus. What they
  type arrives in your chat for the session under their name, like a
  guest's line. The chat itself lives on the sharer's machine: the peer's
  copy holds what was said since the row appeared and is forgotten when the
  link drops or you stop sharing. Agents are never in it, on either side —
  the chat never reaches either terminal. (`chat_msg` and `chat_out` over
  the link: `backend/peer/chat_bridge.py`.)

`GET/POST /api/instances/<title>/chat` (a guest may use it on a session
their link reaches; on a peer's row the same routes go over the link);
every message is `session.chat` on the bus.

## Git identity

`backend.web.core.person.git_identity_args()` returns
`["-c", "user.name=…", "-c", "user.email=…"]` when both `git_name` and
`git_email` are set, else `[]`, and every commit MindFlock makes on your
behalf puts it in front of the git subcommand:

- the Fast-track / ship commit (`git -c … commit -F …` typed into
  the session's shell — the same command otherwise),
- a team run's integration merge and its same-folder piece commits
  (`backend/web/core/git_merge.py`),
- the initial commit of a folder MindFlock `git init`s for you,
- the peer-link export (`peer/<branch>`): authored as you instead of the
  anonymous `MindFlock peer <peer@mindflock.invalid>`. Checkpoints the peer's
  agent asks for stay anonymous.

Unset, nothing changes: the repo's own git config decides. Your agents' own
commits run in their shells and are never touched.

## Owner, driver, viewers

A session has an **owner**, any number of **viewers**, and at most one
**driver** — the person whose keystrokes reach it. It is attribution and a
soft lock that keeps two people from typing into one agent at once, never an
access control: the token and pairing still gate every request, and anyone
who can reach a session can take its keyboard. All of one person's devices
share an id, so a person alone with their sessions never meets any of this —
their laptop and their mac mini are the same driver. A guest link's browser
is its own person, `guest:<link id>`, named what the guest picked (also when
they pick it after their terminal attached), and a peer typing into a session
you share is `peer:<link id>` under the peer's name — never you, so you and
they never both hold the keyboard.

`backend/web/core/ownership.py` keeps it, as web metadata keyed by title (never
the engine's Instance JSON):

| Fact | What | Changes when |
|---|---|---|
| `owner` | The person the session belongs to: the `person` stamp it was created with, unless overridden | only `POST /api/instances/{title}/owner {"person"}` — this device's owner (`may_configure`); persisted in `~/.mindflock/session_owner.json`, dropped on delete |
| `driver` | The person holding the keyboard, `""` = nobody | see below; in memory (every terminal closes with the server, so a restart starts with nobody driving) |
| `viewers` | `{person id: {name, device, devices, since, typing}}` — who has a terminal open | a terminal opens / closes, someone starts typing |

All three are on every `/api/instances` row (with `driver_name`, the driver in
a sentence), refreshed on every poll. A remote row (`<device>::<title>`)
carries what its own device says, unchanged by the gateway.

**Who drives.**

- The first person to attach a terminal (agent or shell) to a session nobody
  drives takes the keyboard; a keystroke while nobody drives takes it too.
- `POST /api/instances/{title}/driver {"take": true}` takes it from whoever
  has it. The previous driver is told: a `session.driver_changed` event, and
  each of their open terminals gets a `{"type": "driver", "driving": false}`
  frame. `{"release": true}` lets go.
- The lock lapses after **10 minutes** without input from its holder, and
  when their **last** terminal on the session closes.

**What the keyboard gates.** A terminal websocket whose person doesn't drive
is read-only: its input is dropped and answered with one line over the
terminal — "Alice is driving — take control to type" (the emulator's replies
to tmux's probes still pass). `/send`, the prompt queue's add and Send now,
and `/answer` (the UI's answer buttons and the MCP's `answer_prompt`) are
**409** `{"error": "driven by <name>", "driver"}` for anyone but the driver
while someone drives, unless the body says `"take": true`. The MCP mailbox
(`send_message`) is not keyboard input — an agent's messages to its workers
are never gated — and neither are the server's own typers (the queue drain,
the limit watcher).

**Presence.** The per-title "a human is at this session" stamp that quiets
notifications is kept per (title, person, device) underneath; a viewer is
`typing` for 5 s after their last input. Every change emits
`session.presence_changed` (data: the row fields). The rail draws up to three
initials dots per session (the driver's ringed; tooltip "Alice (laptop) ·
driving") and the pane header one control — **Driving** (click to let go) or
**Watching — take control** — both only once someone besides you is on the
session. Another person's name doesn't travel between servers yet: they show
as their device.

**Agents.** MCP `list_sessions` rows carry `owner`, `driver` and `viewers` when
set, `get_session` the whole row, and `whoami` says `you_drive` — whether this
server's person drives the agent's own session.
