# Pass 4 — agent-launch follow-ups (from pass 3 c/a)

Verified in a ux-lab sandbox (`sandbox.py`, fresh HOME, private tmux, stub
`claude`, no `codex` on PATH): created a session on `codex`, let it die (exit
127), then POST /revive → 409 *Codex isn't installed on this computer. Install
it in Settings → Agents, then press Start.* (nothing launched);
`/api/providers/status` → `providers_dir`; `/api/connections/test/agent`
(Check again) → *Codex isn't installed*. UI pieces checked by SSR vitest (no
bundle build, per the rules).

| # | Sev | Finding | Repro / where | Status |
|---|---|---|---|---|
| 1 | med | A session whose agent CLI isn't installed (exit 127) was held as "Offline since the agent crashed"; Start / Start all relaunched it into the same failure. | session on a missing CLI; `core/revive.py` `relaunch`/`skip_reason`, `grid/HeldBody.tsx` | FIXED 5ac12846 (Start refuses with the install sentence while the last exit was 127/126 AND the binary is still not on PATH, hold kept; Start all skips it as `agent not installed`; HeldBody says "<Agent> isn't installed…" with **Install <Agent>…** instead of Start until Setup reads it installed) |
| 2 | low | Not-installed agent card: no way to re-check after installing in a terminal; "Not installed" printed twice (line + pill). | Settings → Agents / Custom agents, Codex card; `Connections.tsx` ConnectionRowView | FIXED 50ecd2dd (**Check again** = the card's Test on the install stage; line says *Install it, then press Check again.*) |
| 3 | low | The custom-agent TOML note named the default dir, not the one the server reads. | `/api/providers/status`; `Providers.tsx` note | FIXED 8a72bfdc (`providers_dir`, `~`-shortened, shown in the note) |
| 4 | low | New session's missing-agent note sent people to Settings → Agents (copy a command) while Get set up installs in one click; raw CLI id. | `dialogs/newSession.ts` agentMissingReason | FIXED 214278aa |
| 5 | low | TROUBLESHOOTING: gh heading "installed but not authenticated" (doctor prints "installed but not signed in (optional — …)"); Claude Code install said npm (doctor uses the native installer); "Settings → Agent CLI". | TROUBLESHOOTING.md Doctor failures | FIXED c7f53ba3 (+ a test tying the gh heading to the printed line) |
| 6 | low | The rail's OfflineBar still counts a missing-CLI held session in "N stayed offline after a crash … Start all" (Start all now skips it, but the banner offers it). | `components/sidebar/OfflineBar.tsx` (+ `lib/heldOffline.ts` heldRows) | DEFERRED (outside fence: exclude rows `agentMissing` reads as missing) |
| 7 | low | doctor's own fix lines still say "Settings → Agent CLI" (screen is now Agents / Custom agents → Binary path). | `backend/doctor.py:727,734` | DEFERRED (outside fence) |
| 8 | low | A session's FIRST launch (at create) writes no exit marker, so its first 127 death isn't detectable (not held, Start not refused); only after a pane/Start relaunch does the marker exist. | create a session on a missing CLI; `.exit-markers/mindflock_<t>.code` absent until a relaunch | DEFERRED (outside fence: create-path launcher vs `launch_script` exit wrapper) |
