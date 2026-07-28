# Security Policy

MindFlock runs coding agents with **write access to your repositories**. We
treat the security of that boundary as a product feature, not an afterthought.
This document describes the threat model, the defaults, and how to report a
vulnerability.

## Reporting a vulnerability

Email **security@mindflock.ai**.

- Please include reproduction steps, the version (`mindflock --version`), and
  your platform.
- You'll get an acknowledgement within **72 hours** and a substantive reply
  (assessment + planned fix or rationale) within **14 days**.
- Please give us a reasonable window to ship a fix before public disclosure;
  we'll credit you in the release notes unless you prefer otherwise.
- There is currently no bug bounty. We still deeply appreciate reports.

Please do **not** open a public GitHub issue for anything you believe is
exploitable.

## Threat model & defaults

### Network exposure

- **Localhost by default.** `mindflock serve` binds `127.0.0.1`. Nothing off
  your machine can reach the server unless you opt in.
- **Exposure is explicit.** `mindflock serve tailscale` binds `0.0.0.0` for
  phone/tailnet access — and turns the access-token gate on automatically
  (auth Auto/On). The port is then reachable from your whole LAN, not only the
  tailnet; unauthenticated clients get 401. If you have explicitly forced auth
  *Off*, a non-loopback bind instead prints a loud startup **SECURITY WARNING**
  — the server never silently exposes an unauthenticated control API. Nothing
  reaches the public internet unless you forward the port yourself.

### The access token

- A single bearer token — 256 bits of entropy (`secrets.token_urlsafe(32)`),
  not guessable — gates every HTTP route **and** every WebSocket when the gate
  is on. Comparisons use `hmac.compare_digest` (constant-time), so the check
  leaks no timing oracle.
- The token is stored in `~/.mindflock/settings.json` with `0600` permissions
  (directory `0700`).
- It is printed to the operator's console and encoded into the phone QR; it is
  **never written to `mindflock.log`** (the logged copy of the startup banner
  is redacted, QR included) and query-string tokens are redacted from the
  request log. The `?token=` sign-in URL immediately redirects to strip the
  token from browser history and set an `HttpOnly` cookie instead.
- **Compromised token?** Settings → Security → *Regenerate* (or
  `POST /api/settings/auth-token/rotate`). Every issued cookie, QR code, and
  paired device is invalidated at once.

### Browser-borne attacks (enforced even with the gate off)

- **Cross-origin WebSockets are refused.** WebSocket handshakes ignore CORS,
  so without this a malicious webpage could connect to
  `ws://127.0.0.1:8765/...` and drive your agent terminals. Requests carrying
  a foreign `Origin` header are rejected (HTTP 403 / WS close 4403).
- **DNS rebinding is refused in local mode.** A server bound to `127.0.0.1`
  only answers loopback `Host` headers.

### Agent-session sockets

There are two WebSocket classes, and only one can *drive* a session:

- **`/api/events`** is *read-only* telemetry (status / stage / cost envelopes).
  It cannot start, stop, or type into a session — the most a reader gains is
  visibility of session metadata.
- **`/api/instances/<title>/{terminal,shell}`** are read-write PTYs: whoever
  holds one drives the agent, which has write access to your repo.

Both sit behind the same gate and the same Origin/Host guards as the HTTP
routes (a cross-origin handshake is closed with WS `4403`, an unauthenticated
one with `4401`), enforced by one ASGI middleware *before* any socket handler
runs. So a browser page cannot hijack a session, and neither can another
machine. Another **local process** is bounded exactly as described next: a
same-user process is already inside the trust boundary; a *different* local
user is stopped only when the gate is on.

### What the server trusts

On a single-user machine — a personal laptop, the common case — the default
posture (loopback bind, gate off) is safe: nothing off the machine can reach
the server, and the browser guards above stop web pages from driving it.

Be precise about what the *default* excludes, because `127.0.0.1` is shared by
every account on a host — a loopback bind is a *machine* boundary, not a *user*
one:

- **Other machines** — always excluded (loopback bind).
- **Web pages in your browser** — always excluded (the Origin/Host guards
  above), even with the gate off.
- **Processes running as _your own_ user** — inside the trust boundary and not
  excludable at the HTTP layer: they can already `tmux attach` to the sessions
  or read your files directly. A token cannot change this.
- **Other _local users_ on a shared machine** — excluded **only when the token
  gate is on.** With the gate off, any local account can reach
  `127.0.0.1:8765`; with no token to stop it, it can drive the agent terminals.

So on any multi-user or shared host, turn the gate on: Settings → Security →
*Always on* (or `MINDFLOCK_AUTH=1`). This is the lesson Jupyter learned the
hard way — loopback alone is not a per-user boundary. (A future release may
verify the connecting peer's UID on the loopback socket to close this without
a token; today, the gate is that boundary.)

### Remote control between devices

Another MindFlock device can only drive this one when (a) you've enabled
*Allow remote control* on this device **and** (b) it presents this device's
access token. Both default to off/required.

### Untrusted ticket / issue content

The ingestion pipeline feeds ticket, issue, and PR-review text straight to a
coding agent. That text is **untrusted** — a Shortcut/Jira/Linear ticket or a
GitHub issue can be authored by anyone with access to your tracker, so treat it
as a prompt-injection vector. By default provisioned agents launch with
`--dangerously-skip-permissions` (`[mindflock].skip_permissions`, default on) so
a fresh worktree doesn't re-prompt the per-folder trust gate — which also means
the agent acts on instructions embedded in that text without asking.

What limits it, and what you own:

- **Nothing is pushed automatically.** Ingestion provisions a workspace and
  runs the agent; commit / push / PR / merge are all explicit actions. Review
  every diff before pushing — the agent's output is as untrusted as its input.
- **Worktree isolation** and per-session cost budgets bound the blast radius of
  a single run, but they do not sandbox filesystem or network access within
  your own user account.
- Only enable ingestion for repos and trackers you control, and set
  `[mindflock].skip_permissions = false` if you want the agent to re-prompt on
  sensitive actions.

### Supply chain

- `install.sh` pins the `uv` installer version and **verifies its sha256**
  before executing it, and resolves the requested branch/tag to a **full
  commit SHA** before installing — the printed SHA is your audit trail.
- Dependencies are locked (`uv.lock`).

## Hardening checklist for exposed setups

- Prefer `tailscale serve --bg 8765` + `mindflock serve local`: the server
  stays on loopback and Tailscale terminates HTTPS for the phone UI.
- Keep the auth mode on *Auto* (or *Always on*); never *Off* on a machine
  with other users or an untrusted LAN.
- Rotate the access token if a banner/QR may have been seen (screen shares,
  streams, photos).
- Agents run with your permissions. Per-session cost budgets and worktree
  isolation limit blast radius, but review diffs before pushing.

## Supported versions

Security fixes land on `main` and the latest release. Older releases are not
patched — upgrade with `curl -LsSf https://raw.githubusercontent.com/MindFlock/MindFlock/main/install.sh | sh`
(re-running upgrades in place).
