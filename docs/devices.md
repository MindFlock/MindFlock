# Your devices, your phone, and people: where trust lives

MindFlock has **two ways to trust another machine**, and each control is on
exactly one Settings screen (the **Devices and people** group). This page is
the map; the API behind it is in [web-api.md](web-api.md) (Your devices,
Remote devices, Peer links, Auth), the invite/join flow in
[peer-link.md](peer-link.md).

| Screen (key) | What is there | What is NOT there |
|---|---|---|
| **Your devices** (`devices`) | Tailscale on this device · **Make reachable** (the one Tailscale-mode switch; **Restart server to apply** when needed) · the roster · **Add a device** (pair by code) / **Ask to join** → **Approve** · Paste a code · **Run here** (`github.automation_device`) · **Settings sync** · **One link for all devices** (the shared phone link + its setup checklist) · **Advanced**: Trusted Tailscale accounts, Allow remote control, Pair by pasted token | the access token |
| **Your phone** (`mobile`) | the three steps in order (Tailscale on this computer — with the doctor's diagnosis when it's missing; Tailscale on the phone; scan) · **one** QR · the `/m` links · a pointer to Your devices → Make reachable when the server is local-only | the token, the Tailscale-mode switch, the shared-link setup |
| **People** (`peer`) | **Invite someone** / **Join someone**, links, the shared folder, messages; Advanced: the peer-links switch, relay, listen address | anything about your own devices |
| **Security** (`security`) | **Require access token** (auto / on / off) · this device's token (Show / Copy / **Regenerate**) · "Trusting your own devices lives in Your devices" | trusted accounts, remote control, token pairing, settings sync |

## The two ways

1. **Your own computers → Your devices.** They join one group by code (or
   Ask → Approve) and share a fleet key: settings sync, sign-in, ticket
   claims, "Run here", the phone link. No token to paste. A computer the
   others can't reach (bound to 127.0.0.1) turns on **Make reachable** —
   Tailscale mode *with* the access gate, one save, MindFlock restarts once.
   Off again = this computer only.
2. **Other people → People.** A one-time invite (`mfp1:`/`mfp2:`), a
   safety number to compare, one shared folder per link, sandboxed. Nothing
   on your machine beyond that folder is exposed.

## Advanced (Your devices → Advanced): when you'd still use the older routes

- **Trusted Tailscale accounts** — a phone or laptop that doesn't run
  MindFlock at all: signed in to Tailscale as a ticked account, its browser
  opens this device without the access token. Tagged and shared-in devices
  still need it. (`general.tailnet_trusted_logins`; was on Security.)
- **Allow remote control** — a MindFlock too old to join your devices, or one
  you don't own: with it on, a MindFlock holding this device's access token
  lists and drives its sessions. (`general.remote_control`; was on Security.)
- **Pair by pasted token** — the other half: paste that device's token
  (Security there, or `mindflock token`) to see and drive its sessions. The
  sidebar's **Connect…** on such a device lands here with it preselected.
  (`POST /api/devices/{device}/connect`; was the sidebar's Connect dialog and
  Security's *Pair a device you don't own*.)

## Send a session to another device

A session's work can follow you to another of your devices: "send tripwire
to my Mac" (the MCP's `send_session`, `mindflock send TITLE DEVICE`, `POST
/api/instances/{title}/send-to-device` — [web-api.md](web-api.md#send-a-session-to-another-device)).
MindFlock pushes the session's branch (never a force), starts a session on
that device ON the same branch in its own checkout of the repository, and
hands it a short brief as its first prompt: what the session was for, its
commits and changed files, its last reply, and an optional note ("install it
here as a launchd service").

- **Only your devices.** The target must be one of **Your devices** (or a
  device paired by pasted token) and reachable with remote control on — the
  same trust the sidebar's `<device>::<title>` rows ride. No new trust path:
  a gate-off MindFlock that merely shares your tailnet is refused.
- **Only git and text travel.** No agent login, credential file or
  conversation transcript is copied — a copied Claude refresh token would
  log the real install out. The other device's agent starts on its own
  login.
- **Committed work only.** Uncommitted or untracked files are refused, by
  name: commit them first. Nothing is committed or dropped for you.
- **Its own checkout when it has one.** The device finds its checkout by
  the remote URL (`GET /api/repos/find`); then the branch is pushed and the
  session works ON it there. Without one, see below.
- The new session shows here as `<device>::<title>`. It is not the sender's
  child; read it with `read_output` / the sidebar. The answer says which way
  the work went: `via: "git"` or `via: "bundle"`, and `how` in one sentence.

### When the other device has no checkout

A device without a clone of the repository — or without access to its
remote (its fetch of the pushed branch fails), or when the repository has no
remote at all — gets the branch as a **git bundle** instead
(`backend/web/core/session_bundle.py`). `via: "auto"` (the default) picks
this by itself; `via: "git"` refuses instead (`repo_missing`, with the `git
clone` to run there), `via: "bundle"` always bundles.

- **Small, and standalone.** A bundle of a large repository's whole history
  is too much, and a thin one would need history the device lacks. So the
  branch is re-rooted: ONE snapshot commit holding the tree where the branch
  left its base, then each of the session's own commits (first-parent, at
  most 200; same changes, authors, dates and messages, new hashes). It
  verifies on its own.
- **Just the folder.** `paths: ["tripwire"]` (MCP) / `--path tripwire` (CLI)
  sends only that folder; one folder becomes the new repository's root, its
  history the commits that touched it. When the whole bundle is over
  `transfers.max_mb`, the refusal suggests the folders the branch's changes
  are in (`suggest_paths`).
- **Never secrets.** Files that look like secrets or agent config (the same
  rules as Send files, below) are left out of every tree, and named in the
  brief and the answer (`bundle.left_out`).
- **Unpacked safely there** into `<mindflock home>/inbox/<from-device>/<id>/
  <name>/`: a NEW repository (`git init --template=`, so no hook templates),
  with the system and global git config off, `core.hooksPath=/dev/null`,
  `git bundle verify`, exactly one head (`refs/heads/<branch>`), fetched with
  `fsckObjects`, checked out with `core.symlinks=false` (a symlink in the
  tree becomes a small text file) and `core.fileMode=false`; every file then
  loses its executable/set-id bits (listed in the brief). Never fetched into
  an existing repository; nothing in it is run by MindFlock.
- The session starts **in place** in that repository, on the branch, with
  the usual brief plus what came across. It has no remote: its commits stay
  there until someone carries them on.

Not yet: a Send button in Sky or the sidebar (the API, MCP and CLI only).

## Send files to another device

`send_files(device, paths, note, to_session)` (MCP), `mindflock send-files
DEVICE PATH… [--note] [--to-session]` (CLI) or `POST
/api/instances/{title}/send-files` sends files or folders from a session's
worktree to another of your devices. The sending half is
`backend/web/core/send_files.py`, the receiving half
`backend/web/core/inbox.py` (`/api/transfers`).

"Send tripwire to MLRIG2023 and have an agent spin it up", from the session
that built it:

```
send_session(device="MLRIG2023", note="install tripwire as a service and start it")
```

— the branch goes by git when MLRIG2023 has the repository, else as a bundle
(above); `paths=["tripwire"]` sends only that folder. For plain files that
are not a branch's work: `send_files(device="MLRIG2023", paths=["tripwire"],
note="spin it up")`.

**Where they land.** In a NEW folder per transfer,
`<mindflock home>/inbox/<this device>/<id>/…` (`~/.mindflock/inbox/…`),
keeping their paths relative to the worktree, with a record beside it
(`<id>.json`: files, sizes, SHA-256s, who sent them, the note). Never
anywhere else, never over anything, never run.

**Then what.** With `to_session` that session on the device gets a short
message saying what arrived and where (an unknown name is refused before a
byte moves, with a "did you mean"). With a `note` and no `to_session`, a
session starts there **in that folder** with the note as its task. With
neither, the files just land and the answer says where.

**Safety** — a transfer is untrusted input even between your own devices,
because the receiving machine runs agents:

- **What may leave.** Only paths inside the session's own worktree (symlinks
  resolved; an escape is refused). In folders, symlinks, special files and
  everything git ignores (`node_modules`, `.venv`, build output) are skipped
  and listed. **Never** sent: `.git` internals, agent login/config/transcript
  folders (`.claude`, `.codex`, `.gemini`, `.aider*`, …), `.ssh`/`.gnupg`/
  cloud credential folders, `.netrc`, `.git-credentials`. **Left out unless
  allowed**: `.env*` (not `.env.example`), keys and certificates (`*.pem`,
  `*.key`, `id_rsa*`, …), credential files, `*.tfstate`/`*.tfvars`, and every
  file the repo's `.secrets.baseline` names — `include_secrets: true` sends
  them only when the owner turned on `transfers.allow_secrets` on the sending
  device (off by default). A path in a red zone or outside the session's
  fence is refused. Uncommitted files may go (that is the point); the answer
  lists them.
- **The wire.** The same authenticated device link everything else rides
  (this device's fleet key or the token paired for that device) — no new
  trust path, no third-party service. A manifest (paths, sizes, a SHA-256
  each and one over the lot) opens the transfer; each file follows in 4 MB
  chunks at offsets the receiver confirms (a lost answer resumes from what
  arrived; anything else cancels the transfer there). Limits per transfer:
  `transfers.max_mb` (512) and `max_files` (5000), checked on both sides.
- **The receiving device** accepts a transfer only from another MindFlock
  presenting a valid credential (checked even with its token gate off) and
  only while **Allow remote control** is on there; guest links never. It
  refuses absolute paths, `..`/`.`/empty segments, backslashes, drive
  letters, control characters, `.git` anywhere, duplicates (case-
  insensitively) and a file that is also a folder; it only ever creates the
  files itself (`O_NOFOLLOW`; a symlink, hard link or anything else found in
  the staging area drops the transfer). Every SHA-256 is checked before the
  files move into the inbox; a mismatch drops them. Files land `0644`: no
  executable, setuid or setgid bit (the ones that had one are listed, so an
  agent can `chmod +x` one on purpose). The inbox has a quota
  (`transfers.inbox_mb`, 4096) and a transfer no live session works in is
  removed after `transfers.keep_days` (14) — on the next receive; an
  unfinished one after 6 h.
- **Who may send.** As `send_session`: a session sends from itself or one it
  manages; guest links, relayed requests and shared-folder (peer) sessions
  are refused. Every refusal is a sentence with a `code` and the next step.

## Keys and deep links

The screen keys never changed: `devices`, `mobile`, `peer`, `security`. The
new names and the moved controls' names route too
(`frontend/src/components/settings/screenKeys.ts`): `your-devices`,
`your-phone` / `phone`, `people` / `peer-links`, and `remote-control`,
`tailnet-trust`, `trusted-accounts`, `pair-by-token`, `settings-sync`,
`make-reachable`, `shared-link` → Your devices. Palette: **Your devices**,
**Your phone**, **People…**, **Paste a code…**.

`tests/settingsScreens.test.ts` (vitest) pins the once-only rule: the token
display, the Tailscale-mode switch and the trusted-accounts list are each
rendered by one screen, found by their `data-control` attribute.
