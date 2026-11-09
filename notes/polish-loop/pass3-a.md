# Pass 3 — area (a): install, doctor, first launch + onboarding

Walked as a new user on ux-lab sandboxes (ports 9410–9412, scratch
~/polish-scratch/a): `--fresh --no-agent` (no agent CLI on PATH) and `--fresh`
with the stub agent. CLI under the sandbox HOME/PATH: `mindflock doctor`,
`doctor --all`, `doctor --fix` (pty, answering n / a name+email), `mindflock
init`, the `serve` first-start banner (server.log). Web: headless Chrome on the
sandbox server: welcome card → Welcome tour → Get set up checklist (All
checks, Or run it yourself, git identity with an emptied sandbox gitconfig),
New session with no agent installed, first session start with the stub agent,
page reloads after onboarding. Fixed frontend checked by serving a scratch
`vite build` (outDir in scratch; the committed bundle is untouched) through
playwright routes. Desktop: read electron/offline.html's states (copy is in
good shape; nothing changed).

| # | Sev | Finding | Repro / where | Status |
|---|---|---|---|---|
| 1 | med | First launch: a newcomer who closes the welcome before the first `/api/doctor` answers (~5 s on a first probe) is read by What's new as an upgrader from before What's new; the next page load pops every release since 0.11 ("there's no hidden second name any more"…) over the Get set up checklist. | `--fresh` sandbox: Let's go within ~2 s, reload. `WhatsNew.tsx` `onboarded = welcomeSeen \|\| …` + `planWhatsNew` seen=null | FIXED f7e3d891 (a first launch's welcome records the installed version as seen on close; docs a88bccef) |
| 2 | med | Browser checklist: missing git identity shows only a shell command, with prose "(or Connect GitHub in the browser fills it in…)" inside the `<code>` — dead end without a terminal. | gitconfig without [user] → step ① | FIXED 1b9bebe7 (Name / Email / Save in the row via POST /api/github/identity; command folded under "Or in a terminal") |
| 3 | med | `doctor --fix` silently skips a missing git identity (no cmd), so the first commit fails later with "Please tell me who you are". The fix line wasn't pasteable either (trailing parenthetical). | sandbox HOME without .gitconfig, `doctor --fix` | FIXED 1f0bafe1 (asks name + email, Enter skips, writes global config, re-probes; fix is a bare command) |
| 4 | low | gh installed but signed out: row prints gh's own "You are not logged into any GitHub hosts. To log in, run: gh auth login" above "fix: run gh auth login", flagged ! under Needed to ship though gh is optional. | `mindflock doctor` in the sandbox | FIXED 4df85da5 (says optional + what uses it; info when MindFlock already has a GitHub token) |
| 5 | low | Web doctor rows say "agent CLI (claude)" / "agent auth (claude)" where the terminal says "agent (claude)" / "agent sign-in (claude)". | All checks (8) | FIXED 1b9bebe7 (`checkLabel`; wire labels unchanged — InstallTerminal parses them) |
| 6 | low | `serve` banner: the local "Phone: mindflock serve tailscale" line races the three-steps block (printed above "Three steps…" on one boot, after it on another) and repeats its "Optional, later: … phone". | server.log of two sandboxes; printed by server.py `_startup_warmups` ~927 vs run.py `_preflight` thread | DEFERRED — clean fix is in `backend/web/run.py` (outside fence): print the phone line inside the preflight buffer and have server.py skip the local one-liner |
| 7 | low | TROUBLESHOOTING.md heading "`installed but not authenticated` (gh)" no longer matches any printed text (now "installed but not signed in …"). | TROUBLESHOOTING.md:124 | DEFERRED — outside fence |
| 8 | low | New session with no agent installed: "Install it in Settings → Agents, or pick another agent." while the checklist right behind has a one-click Install button. | `frontend/src/components/dialogs/newSession.ts:983` | DEFERRED — outside fence |
| 9 | low | `doctor --fix` ends on "Next: mindflock doctor --fix installs the 1 missing tool" right after the person declined that install inside --fix. | `doctor --fix`, answer n | DEFERRED (nit; still the right next step) |
| 10 | low | First `/api/doctor` takes ~5 s; step ① is titled "Install Claude Code" over "Checking what's installed…" even when it is installed. | `--fresh` stub-agent sandbox, first load | DEFERRED (skeleton covers it) |
