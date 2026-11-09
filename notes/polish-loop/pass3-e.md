# Pass 3 — area (e): Devices (phone pairing, Tailscale, guest links, Your devices)

How: ux-lab sandbox on :9470 (fresh HOME, private tmux, stub claude) with a
scriptable fake `tailscale` first on PATH and in `MINDFLOCK_TAILSCALE_BIN`
(state from a scratch `ts-status.json`: missing → "not answering",
NeedsLogin → sign-in, Running → signed in as a fake login; `login` prints a
fake auth URL; `serve` is a no-op). A stub `cloudflared` exits 1. Walked in
headless Chrome: Settings → Your devices (Tailscale card, Make reachable →
in-place restart, Add a device code, Join, Shared link checklist), Your phone
(all four rows across the three Tailscale states, before and after Make
reachable), People → guest links (gate on, the red warning, pick sessions,
Make a link, Revoke), and the phone page `/m` at 390×844 (first load with no
session, Send, the new-session sheet, the token sign-in page). Second boot
with `MINDFLOCK_AUTH` unset so the gate could turn on.

| # | Sev | Finding | Repro / where | Status |
|---|---|---|---|---|
| 1 | high | Phone page: typing a message and pressing Send with no session (or with the terminal reconnecting/paused) clears the box and sends nothing. The words just vanish. | `/m` with no sessions → type → Send; `mobile.js` `sendCompose` → `send()` drops on a closed socket | FIXED e3f19082 (no session → the new-session sheet opens holding the words; not connected → the draft stays and the status says so) |
| 2 | high | Guest links: when the public link fails to start, the People screen says "Starting the public link…" for ever and never shows the error or its Try again. Behind it, cloudflared is respawned on every 2 s poll. GET `/api/guests` called `ensure()` whenever the tunnel wasn't up. | gate on, stub cloudflared exits 1, Make a link → state `starting` with `error` set, forever | FIXED 163c2e60 (a failed start stays `failed` until Try again or a new link; a missing cloudflared or a tunnel that dropped still retries on a look) + docs 76d112ef |
| 3 | med | Your phone, step 4 "Scan to open MindFlock" lists `Local http://127.0.0.1:…/m` first among the links to open on the phone. On a phone that address points at the phone itself. | Your phone after Make reachable | FIXED ed627cbd (`phoneUrls` drops loopback) |
| 4 | low | Your devices: a lone "You — the same on every device you pair." hint sits under the folded Advanced block (reads as Advanced's content), right above the You card that says the same thing. | bottom of Your devices | FIXED 39d5b0fc |
| 5 | high | After Make reachable, Your devices says "listens on your tailnet (100.64.0.7:9470) · access gate on", but when the Tailscale addresses can't be bound here (userspace networking, or WSL reading the Windows side) the server actually binds 0.0.0.0, so the LAN can reach it too. The hover title even says "your LAN still can't". The fallback is only printed to the console. | sandbox: `ss -ltn` showed 0.0.0.0:9470; `run.py` WHY_UNBINDABLE / WHY_NO_TAILNET; `lib/fleet.ts:736 thisDeviceLine`; `core/fleet.py:3205` | DEFERRED (outside fence: run.py should export the bind reason, `/api/fleet` carry it, `thisDeviceLine` say "listens on every interface (Tailscale's addresses can't be bound here)") |
| 6 | med | Guest link "Copy on-your-network link" copies the tailnet URL even while MindFlock only listens on 127.0.0.1, so it hangs for whoever opens it. `_local_base` ignores the `live` half of `tailnet_url()`. | local mode + Tailscale running → make a link → local_url `http://<host>.ts.net:9470/?token=…` | DEFERRED (needs UI: `screens/peer/GuestLinks.tsx` ~409 should say "Make reachable first" when the server says not live; `guests.py` could add `local_live`) |
| 7 | med | The phone's sign-in page (gate on, no cookie) tells a phone user to type the access token or run `mindflock token`. The easy path is to scan the QR in Settings → Your phone (scanning signs in), and the page doesn't mention it. | `/m` without token | DEFERRED (outside fence: `backend/web/core/auth.py:759`) |
| 8 | low | Shared link checklist reasons show raw backticks: "…capabilities from \`tailscale status\` (an older Tailscale?)". | Your devices → Shared link on | DEFERRED (outside fence: `backend/web/core/shared_link.py:700,740`) |
| 9 | low | The You card's git-author hint says "(Goes as far as, team-run merges, peer exports)". "Goes as far as" is an internal label that a new user won't recognise. | Your devices → You | DEFERRED (outside fence: `screens/PersonCard.tsx:100`) |
| 10 | low | With `MINDFLOCK_AUTH=0` in the server's environment, "Turn the gate on" saves `auth_mode: on` but nothing changes and nothing says why (the env var wins). Dev/sandbox only. | ux-lab sandbox env | DEFERRED (rare outside dev) |

Notes for later passes
- `~/polish-scratch/e/tools/lab.py` = the ux-lab sandbox with a fixed port and
  a scriptable fake `tailscale` (write `<scratch>/ts-status.json` to switch
  states; `ts-calls.log` records every call). Pop `MINDFLOCK_AUTH` to walk anything
  gated (guest links, the phone sign-in page).
- Make reachable in the sandbox binds 0.0.0.0 (the fake 100.64.x address isn't
  bindable). The gate is on, but stop the sandbox when you're done.
