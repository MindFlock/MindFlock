# Pass 5 — leftovers backlog (deferred low/med items from passes 3–4)

Worked from the deferred rows of pass3-a/c/d and pass4-agents (fenced to the
files below; no walk — each item was already reproduced by the pass that
found it). Branch forks from emandel2630/polish-pass4 (ff-merged first).

| # | Sev | Finding | Repro/where | Status |
|---|---|---|---|---|
| 1 | low | The rail's held-offline notice (and the plain "N agents offline" bar) still counted a missing-CLI session and offered Start all, which skips it (pass4-agents #6). | `sidebar/OfflineBar.tsx`, `lib/heldOffline.ts` heldRows | FIXED 67bb39f3 (heldRows takes a `missing` predicate; OfflineBar passes Setup's `agentMissing` read to heldRows and offlineRows) |
| 2 | low | A session's FIRST launch (at create) wrote no exit marker, so its first exit-127 wasn't read as "agent not installed" (pass4-agents #8). | `session/tmux/tmux.py` `start` vs `web/core/terminal._wrap_launch_cmd` | FIXED 458f6ec1 (create path runs through the relaunch wrapper: `rm -f <marker>; …; echo $? > <marker>`; also clears a stale same-name marker. Lazy import; an install without the web package launches unwrapped — nothing reads markers there). Real-shell test: missing CLI → marker 127 |
| 3 | low | Intake: Repo URL box red on arrival (pass3-d #8). | Add Shortcut → step 3 | FIXED ac252c42 (red once blurred empty or the server refused `repo_url`/`repos`; aria-invalid) |
| 4 | low | Intake: two "auto-start is off" lines on the Tickets tab; "Turn on ›" only switched tabs (pass3-d #9). | After Finish, all switches off | FIXED ac252c42 (header link always reads "Auto-start switches ›"; the per-tab "Auto-start: off" line is hidden while all three are off — the header says "off everywhere"; docs/web-ui.md) |
| 5 | low | Intake: "Connect a tracker." intro stayed once one was connected (pass3-d #10). | Tickets tab with a source | FIXED ac252c42 |
| 6 | low | Intake: "Not saved" pill on the Connect step chip while the refused field is on What it takes (pass3-d #11). | Edit → Anyone's → remove last state | FIXED ac252c42 (`fieldPart`: pill on the step of the first refused field; editor opens there). The "said three times" half is left as is |
| 7 | low | Local model intro used raw ids "codex, aider and goose"; Test line listed raw ids (pass3-c #11). | Settings → Local model | FIXED eda47e81 (agentName: Codex, Aider, Goose; "Installed agents that can use it") |
| 8 | low | Shared link: the operator refusal had Markdown backticks in plain text. | `web/core/shared_link.py` `_explain` | FIXED 526fabcd (ends on the bare pasteable command) |
| 9 | low | `doctor --fix` ends on "Next: mindflock doctor --fix installs…" after you declined (pass3-a #9). | `doctor --fix`, answer n | DEFERRED (outside fence: the line is `backend/cli.py:1184-1186` → `backend/init_wizard.py:311-330` `next_action(st, "doctor")`; fix = a `"doctor-fix"` door that says "install it yourself — the ✗ line above says how — then run mindflock doctor again") |

Follow-ups outside the fence:
- `backend/cli.py:1186` / `backend/init_wizard.py:318` — row 9.
- `backend/web/core/agent_state.py:1025` `_agent_exited` docstring says the
  engine's first launch doesn't clear a stale marker; it now does (the mtime
  guard stays correct, only the comment is stale).
- Rebuild `backend/web/static/app.js` (not done here, per the pass rules).
