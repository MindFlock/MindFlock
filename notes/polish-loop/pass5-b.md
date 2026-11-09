# Polish pass 5 — area (b): Settings → Voice

Walked as a new user on branch `emandel2630/polish5-voice` (stacked on
`emandel2630/stt-speed`, PR #320). Sandbox: `~/MindFlock/ux-lab/sandbox.py`
(fresh HOME, adopted nothing) on an auto-picked port, a Vite dev server on
:9590 proxying to it (a scratch config — the checked-in one proxies to the
live :8765), headless Chrome. A fake `uv` first on the sandbox's PATH made
every install fail with uv's real DNS-error shape; nothing was installed
anywhere. Walked: the fresh built-in engine (WSL), Install local models →
failure, Local models picked with nothing installed (both kinds), Try it,
Voice words, Say it as; docs/web-ui.md + docs/configuration.md against it.
The Live words / Moonshine row needs local recognition streaming, which the
sandbox can't reach without a real install: covered by SSR tests instead.

| # | Sev | Finding | Repro / where | Status |
|---|---|---|---|---|
| 1 | med | A failed Moonshine install said "if it fails again, mindflock doctor --all on the server says why" — doctor's *Fast recognition* row says only "not installed" and its fix is this same button: a loop with no way out. The local-models failure had no next step at all. | Voice.tsx `MOONSHINE_NEXT`; doctor.py `check_fast_recognition` | FIXED 832b455e (`installNextStep`: network → check online/proxy/VPN, full disk → free ~300/200 MB; Moonshine adds "Live words keep using whisper meanwhile.") |
| 2 | low | The failure reason was uv's last line verbatim: "installing packages failed: Caused by: dns error: …". | speech_install `_run_step` | FIXED 832b455e (`_reason` drops the lead-in) |
| 3 | med | With the built-in engine (the default), the Recognition line AND the Voice line both read "…ready (system): Recognition: Windows … (en-US). Voice: Windows … voices: David, Zira." — each repeated both kinds, and recognition's "basic accuracy" note was lost. | Voice.tsx `readinessLine` used `system.detail` | FIXED 832b455e (each kind's own detail; the system sentence only when there is none, or Linux's why) |
| 4 | med | Local models picked but missing: on Settings → Voice the lines said "Settings → Voice installs them"; under Voice (and with recognition on the built-in engine) there was no install button anywhere on the screen. | Voice.tsx `readinessLine` / `tryState` / `installOffer` | FIXED 832b455e ("install them below" / "under Recognition"; installer row offered when the voice's local models are missing) |
| 5 | low | The Moonshine install's progress bar is announced "Installing local models". | Voice.tsx `InstallStatus` | FIXED 832b455e |
| 6 | low | The local-models install switches `voice.stt_backend` to local even when only the voice needed it (now offered for that case, #4). | speech_install `_install` | DEFERRED (by design: "the person asked for accuracy"; worth a look if it surprises someone) |
| 7 | low | The checked-in `vite.config.ts` dev proxy hard-codes the live :8765 — a worker running `vite` to see a change drives the owner's real server. | frontend/vite.config.ts:78 | DEFERRED (outside fence; an env override such as `MINDFLOCK_DEV_TARGET` would do) |

Not walked: a real Moonshine success (needs the real package), macOS'
permission sentence, an OpenAI-compatible host.
