# Pass 4: network/devices follow-ups (from pass3-e)

How: fixed the six listed items with tests, then checked them on the ux-lab
sandbox with pass 3's fake `tailscale` (`~/polish-scratch/e/tools/lab.py`,
Running status, 100.64.0.7 not bindable) on :9550/:9551. Never the real tailnet.
Checks: the console after a fresh `serve local`; `/m` with the gate on and no
cookie; POST/GET `/api/guests` in local mode and after Make reachable; Make
reachable (POST /api/settings serve_mode=tailscale) then `ss -ltn` +
`/api/fleet`. Results: 0.0.0.0:9551 with `bind: "unbindable"`; `local_live`
false in local mode and true after.

| # | Sev | Finding | Repro / where | Status |
|---|---|---|---|---|
| 1 | high | Your devices said "listens on your tailnet (100.64.0.7) · access gate on" while the server had fallen back to 0.0.0.0 (Tailscale's addresses can't be bound here: userspace networking or WSL), so the LAN could reach it too. The fallback was only printed to the console. | pass3-e #5; sandbox Make reachable → `ss` 0.0.0.0 | FIXED 0c178fa4 (run.py exports MINDFLOCK_BIND_WHY; `/api/fleet.bind`; thisDeviceLine: "listens on every interface (port N, <why>) — your LAN can reach it too, not only your tailnet") |
| 2 | med | Guest link "Copy on-your-network link" copied the tailnet URL while MindFlock only listened on 127.0.0.1, so the link hung. | pass3-e #6 | FIXED 0fc501a7 (`local_live` on each `/api/guests` row; when it's false the People screen shows "On-your-network link: Make reachable first", which opens Your devices) |
| 3 | med | The phone sign-in page only said to type the token or run `mindflock token`. It didn't mention that scanning the QR signs a phone in. | pass3-e #7; `/m` gate on | FIXED e7953617 |
| 4 | low | Raw backticks in the shared-link checklist reasons ("from \`tailscale status\`"). | pass3-e #8 | FIXED e7953617 |
| 5 | low | The You card's git-author hint said "(Goes as far as, team-run merges, peer exports)", an internal label. | pass3-e #9 | FIXED a8cbf968 |
| 6 | low | `serve local`: the "Phone: mindflock serve tailscale" line came from the server's startup banner, racing the preflight's three-steps block. | run.py `_preflight` / server.py banner | FIXED 37853e36 (the preflight writes it in its one write; `server._banner_for_stdout` leaves it out of stdout) |
| 7 | low | The Make reachable row's hover in Your devices still says "your LAN still can't", which is wrong when it falls back to 0.0.0.0. | `screens/Devices.tsx:446` | DEFERRED (outside fence) |
| 8 | low | `shared_link._explain`'s operator refusal still puts the fix command in backticks in prose. The operator step's Copy box already shows the command. | `core/shared_link.py:508` | DEFERRED (shown only on a refused serve, and the Copy box covers it) |
