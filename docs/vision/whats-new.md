# What's new — as built

2026-11-03. The owner: "whenever I am releasing a new big feature like Sky and
actually cutting a release there should be like a pop up or something that
explains all the new features that just came in."

## What a person sees

After an update to a release that has big features, the app opens **What's new
in MindFlock 0.11** once: the release's headline, then each feature as a quiet
row — a title, one or two plain sentences (what it is, why you'd care) and, for
most, a **Show me** button that takes you there. Smaller releases since the last
one you saw fold under **Also in this release**. One primary button, **Got it**.

Reopen it any time: the palette's **What's new**, **Settings → General → Help →
What's new**, or a click on the version (`v0.11.0`) beside the top bar's
wordmark.

## Files

| What | Where |
|---|---|
| The entries (data, newest first) | `frontend/src/whatsNew/releases.ts` |
| The rules (pure, vitest) | `frontend/src/lib/whatsNew.ts` · `frontend/src/__tests__/whatsNew.test.ts` |
| The window, its CSS | `frontend/src/components/whatsNew/WhatsNew.tsx`, `WhatsNew.css` |
| "Show me" ids → existing helpers | `frontend/src/components/whatsNew/actions.ts` |
| Seen state + the open/closed store | `frontend/src/components/whatsNew/seen.ts` |
| The release guard | `tests/unit/test_whats_new.py` |

Mounted with one line in `App.tsx` (beside `<WelcomeTour />`); reopened from
`CommandPalette.tsx`, `settings/screens/General.tsx` (Help) and `TopBar.tsx`
(the version label is a no-drag button).

## The rules (`lib/whatsNew.ts`)

- **Pop** only when an unseen entry with `big: true` sits at or below the
  running version. Unseen = newer than the last seen version.
- The window lists **all** unseen entries at or below the running version,
  newest first; the big ones in full, the small ones under "Also in this release".
- **Fresh install** (nothing seen AND onboarding not done — the welcome tour not
  seen and no session ever started, `config.onboarded`): never pops; the running
  version is marked seen. First-time users get onboarding, not a changelog.
- Someone who used MindFlock **before What's new existed** (nothing seen, but
  onboarded) gets it on the first release that has it.
- An entry **above the running version** is never shown (an entry written ahead
  of its release, a downgrade, a dev build behind the newest entry).
- An **unknown running version** (a source tree: `""`, `0+unknown`) pops
  nothing and records nothing; opened by hand it shows the newest entries.
- Closing by **any** way (Got it, ×, Esc, the backdrop, a Show me) records the
  running version as seen, never lower than what was already seen.
- Opened by hand: the latest release's entries — from the newest at or below the
  running version down to the newest big one.
- Versions compare numerically, part by part.

**Timing.** The decision waits for the running version (`/api/doctor`), the
config and the first prefs pull. Even when it should pop, it waits until
onboarding is done and nothing else is on screen (no welcome card, no
coach-mark tour, no dialog), so it never covers the welcome tour.

**Actions.** A feature's `action.run` names an id in `actions.ts`: `sky`,
`sky-tour`, `sky-guide`, `saved-routes`, `behaviors`, `intake`, `schedule`,
`settings`, `agents`. An unknown id draws no button — never a crash.

## Seen state

`prefs.whats_new_seen` (a version string) through the existing per-person prefs
route (`GET/POST /api/prefs`), so the browser tab, the desktop app and the phone
share it via settings sync. This browser's localStorage (`mf_whats_new_seen`) is
written first and is the fallback; the higher of the two wins.

**Open (as of this round):** the server half — the `whats_new_seen: str` field
on `PrefsSettings` (`backend/config/settings.py`) and its `SYNCED["prefs"]` entry
and label in `backend/web/core/settings_sync.py` — is not in yet: the worker's
fence did not include `backend/config/settings.py`. Until it lands,
`POST /api/prefs` ignores the unknown field (by design) and each browser keeps
its own seen version in localStorage — the pop-up works, once per browser
rather than once per person.

## Cutting a release (how the integrator uses it)

1. Write the entry in `releases.ts` for the tag — newest first, `version`
   exactly the tag's `major.minor.patch`, `date`, `big`, a `headline` and 4–6
   `features` from `git log --oneline v<previous>..HEAD` and the round notes.
   Plain words from `docs/vocabulary.md`; no internal names (the vitest pin
   refuses "chain", "flyway", "perch", "Fast-track", "checklist", "test plan").
2. `big: true` for a release with features worth stopping someone for: it pops
   for everyone who updates. `big: false` for the rest (bug fixes, small
   polish): they are only listed beside a big one, or when reopened.
3. `tests/unit/test_whats_new.py` fails when pyproject's `major.minor` is newer
   than the newest entry's: a minor or major release must ship an entry; patch
   releases may skip. Writing the entry ahead of the bump is fine — it shows
   only once the running version reaches it.
4. Rebuild the bundle with the rest of the release (`backend/web/static/`).
