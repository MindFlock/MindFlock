# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.10.3] - 2026-10-28

### Added

- **Say it as.** Settings → Voice gains your own pronunciation rows ("k8s"
  → "kubernetes", your project's name the way you say it), synced across
  your devices, with a Try button. They win over the built-in table.
- **The words engineers say.** Every spoken line — a reply, a confirmation,
  the phone's and the desktop's alike — is shaped once, at the moment it is
  said and never in a caption: "everything goes live" says *live* the right
  way, "I read it" says *red*, SQL, JSON, nginx, tmux, uv, pytest, k8s,
  "PR #275", "v0.10.2" and "1m23s" are said as a person says them,
  identifiers and paths are read as words, and markup and URLs are left out.

- **Ask MindFlock how it works, by voice.** "How do I schedule a
  template?", "what does autopilot do?", "where do I turn on the phone
  page?" — a question about MindFlock itself is no longer typed into the
  session as a prompt: the phone and the desktop mic say *Looking that up…*
  and answer out loud in a sentence or two from MindFlock's own docs, naming
  the section ("From Fast-track: …"). A quick model call words the answer;
  without one, the section's own first sentences are read. "Say that again"
  repeats it. A question about the agent's work ("how do I fix the login
  bug?") is still a prompt; start with "MindFlock, …" or "help: …" to ask
  MindFlock anyway (`POST /api/voice/help`).

### Changed

- **"Over" sends sooner.** The wait for the accurate recognition pass after
  "over" is no longer a fixed 2.5 seconds: the phone and the desktop wait
  about one and a half times the pass's measured round trip, between 0.3
  and 1.5 seconds, and a final that matches the words already heard sends at
  once. The server starts the accurate pass on the first quiet chunk and
  gives it the command words and your session names as vocabulary hints:
  on the bench the wait halved and dictation errors fell from one word in
  ten to one in twenty-five.
- **Harder to mishear.** Each command has a table of what the recognizer
  actually writes for it ("ova", "over an out", "dis regard", "stop
  listing", "mind lock"), the same on the server, the desktop and the
  phone. A command word heard a little off counts only at the start of a
  phrase or alone, so "…in this regard" mid-sentence never wipes a draft.
  Session names match across camelCase, digits and number words, and
  sound-alikes ("sight check"), with *Which one?* when two fit.
- **Speech that keeps up.** Short sentences are packed into one request for
  a voice with a fixed cost per request (Voice Lab), spoken clips are
  cached, the mic ring reacts at once with a slow release, *Listening…*
  lands in the frame of the press, the start chime no longer clips, and
  speaking over a reply on the desktop cuts it and keeps the word you said.
- **"Stop listening" says nothing back.** A stop phrase was answered with a
  spoken *Stopped.*, heard as the mic saying stop back, every time. The
  falling chime alone answers it now, on the phone and the desktop; *Sent.*,
  *Over and out.* and *Cleared.* are unchanged, and one still owed finishes
  before the chime.
- **Phone page: one-row top bar, room to type.** The top bar never wraps
  (the session's name gives way, and in portrait the state chip steps aside
  for it — the dot still shows the colour); tapping the text box hides the
  voice strip, *Tap to hear …* included, until you leave it; and the
  compose placeholder fits on one line.
- **Voice says only what is news.** Switching sessions, turning voice on,
  resuming it, coming back to the page, the poll and *Everything* no longer
  read a reply from a session that has been done for a while. A reply is read
  by itself only when it ended after you could have heard it — after voice
  went on or the page came back, after you last switched to, selected or sent
  to that session — and within the last ten minutes; a reply to a prompt you
  sent is always read. Asking still reads it: the notification's *Tap to
  hear*, and the phone's `mfVoice.sayLast` for "what did it say". The same
  rule holds on the phone and on the desktop mic.
- **Spoken replies give their context.** The `Say:` line a session is asked
  for now opens with what it was working on, then what it did, then the
  question — up to three short sentences (*We were working on MindFlock's
  settings screen, and the voice install button now sits in the Try-it row.
  Should I add it to the phone page too?*). And a reply from a session you
  are not on, or have not heard from in ten minutes, says where it is from
  first: *In Invoice parser, on parse the March invoices: …*. Claude Code
  sessions pick the new instruction up when their hook is re-pinned (turning
  voice on does it).
- **Switch sessions by voice the way you'd say it.** "Switch to the
  sitecheck session", "take me to login fix", "jump over to the settings
  one", "open sitecheck" — the words around a name are ignored and a name
  matches across hyphens and digits ("bot seven" is bot7), with a plural or
  a wrong letter forgiven. When two sessions match, it asks *Which one:
  sitecheck-bot7 or sitecheck-bot7-copy-2?* and listens: "the second one",
  "copy two" or "the one that's working" picks. Phone and desktop.
- **Start a session by talking.** "Start a new session", "spin one up" or
  "create a session to fix the login bug in mindflock": it asks *In which
  project?* and *What should it do?* when you haven't said, then starts it
  and moves you there. "This one" means the project you're on; "never mind"
  drops it.
- **"What did it say?"** reads the session's last reply on demand ("read
  it", "what did login fix say").


- **Switching sessions no longer replays an old reply.** Moving to a
  session by voice says where you are — *Now on login-fix, idle: Fix login
  redirect.* — and reads its last reply only if it finished in the last two
  minutes. "What did it say" is now that read-back; "summarize that" still
  condenses.
- **A new session by voice asks for its project** when more than one is
  known, instead of taking the current session's (say "this one" for it),
  and an unknown project is asked for again rather than refused.

- **Only a real prompt raises a Question.** A reply that merely ends with a
  question in plain words ("Should I also add them?") no longer puts a
  Question on the session's row, in the bell, the toast, the push or the
  voice router — only a permission, a plan approval or Claude's question
  menu does. The old behaviour is a switch, off by default: Settings →
  Notifications → *Count a question asked in plain words at the end of a
  reply* (`notifications.prose_questions`).

### Fixed

- **Phone page: the band between the keys and the keyboard.** Two causes,
  both gone: the key bar kept its home-bar inset under the keyboard (iOS
  keeps reporting it), and a fixed 60-pixel allowance for a Safari
  address strip above the keyboard that today's Safari no longer draws.
  The esc / ctrl keys now sit on the keyboard.
- **iPhone: "Speech recognition did not start (start-failed: InvalidStateError:
  audio session category is not compatible with audio capture)".** The
  first tap set iOS's audio session to playback-only (so the silent switch
  does not mute replies), and that category refuses the microphone the
  server's recognizer then asks for — in Safari and in DuckDuckGo alike. A
  page that may open the microphone (recognition on the server, the
  fallback to it, the wake word) now takes `play-and-record`, which the
  silent switch does not mute either, and the server recognizer sets it
  again right before it asks for the microphone.
- **A question put off with Later came back after every restart.** The
  dismissal and the "someone typed into it" mark lived in the server's
  memory, and `mindflock restart` (an update too) re-execs the server in
  place — so a reply that ended with a question at 5 PM wore its Question
  chip again after the 8 PM update. Both are now kept in
  `~/.mindflock/asking.json`.

## [0.10.2] - 2026-10-27

### Fixed

- **DuckDuckGo (and Firefox, and in-app browsers) on the phone page said
  "Microphone permission denied" with the mic allowed.** Their Web Speech
  API has no recognition service behind it, so every start fails with the
  same code a refused microphone gives. The page now tells them apart — a
  granted microphone, the *service-not-allowed* code or a known browser
  reads as *This browser has no speech recognition service … use recognition
  on this computer, or open this page in Chrome* — and when the server has
  recognition ready it switches this page to it on the spot and keeps
  listening. The ⚙ sheet's diagnostics and the Voice screen's privacy note
  name the browsers.
- **"Speech recognition did not start (start-failed)" now says why.** The
  phone page's microphone failures were one code with no reason. A page
  opened over plain http (the Local URL on a phone has no microphone at
  all) reads *The microphone needs an https page: open the phone page by its
  https link*, before either recognizer is tried; a microphone another app
  or tab holds reads *busy or unavailable*; anything else carries the
  browser's own error name and message. The ⚙ sheet's diagnostics start with
  the page's protocol.

## [0.10.1] - 2026-10-27

Local speech models install from the row that says they are missing and
stay installed across releases, a session's question pops up only when
there is a real prompt to answer, and a template can be scheduled once.

### Added

- **Schedule a template once.** Beside every day, weekdays and cron, a
  template takes `schedule: {"once": "YYYY-MM-DDTHH:MM"}` (local time): one
  slot, spent after it fires (or is skipped or paused through), then *Ran
  once on Tue 28 Oct at 21:00* and no next run. The editor's cadence select
  gains **Once** with a date field; a new one-off already behind the clock
  is refused. The command palette, the Schedule tour, `mindflock templates`
  and the docs follow.

### Changed

- **Install local models where recognition says they are missing.** Settings
  → Voice's *Try it → Recognition* row showed a disabled *Try recognition*
  button with a terminal command as its hint while the Install button sat in
  another row. The row now carries the install itself — the step, a progress
  bar and the last log line while it runs, *Try again* after a failure — and
  the server's verdict reads *local models not installed: Settings → Voice
  installs them*.
- **Local models survive a release.** Every engine rebuild (an update, a
  reinstall) rebuilt the server's venv from `mindflock[web]` alone and
  dropped `faster-whisper` and `piper-tts`. A good install now records them
  in `~/.mindflock/voice/packages.json`; the server's updater and
  `mindflock update` carry them through the rebuild the way the Database
  explorer's drivers ride (`--with` on the uv line, `pipx inject` after
  pipx); a rebuild that dropped them anyway is healed at boot or on the next
  `GET /api/voice` as a running install the Settings screen follows (the
  packages only — the models on disk are untouched). A package gone from the
  index drops the record. `mindflock doctor` lists the record as *Local
  speech models*.
- **The ask pop-up opens only for a prompt it can answer.** A dialog the
  server could not parse — no options to answer on the card, and a misread
  frame of a busy screen is the usual reason there is one — never opens the
  pop-up by itself, on the desktop or the phone; it keeps its place in the
  bell, the rail and the toasts.

### Fixed

- A `?` quoted as a character in a reply (*the "?" in its options*) no
  longer reads as a question the session is asking.

## [0.10.0] - 2026-10-27

Talk to your sessions. The phone page and the desktop app both have a mic:
your words are typed into the session as you say them, "over" sends, the
agent's reply is read back in your own voice, a session that asks you
something pops up in the middle of the screen, and "MindFlock" said on its
own wakes it up. Every agent CLI's dialogs can be answered by voice, and a
failed pre-commit hands the agent its own failure and the steps left.

### Added

- **Voice, on the phone and the desktop.** A mic in the phone page and in
  the desktop top bar (next to the bell; the Talk hotkey is `Ctrl+T`, hold or
  click per the mic's push-to-talk mode). Words are typed into the focused
  session as you speak, corrected in place as the recognizer refines them,
  and the first pass's guess shows within half a second. Listening stays on
  once you start: **"over"** sends the message and keeps listening,
  **"over and out"** sends what is pending and stops, **"disregard"** takes
  the phrase back, and the stop phrases ("stop recording", "that's all",
  "mic off") stop without sending. A control word acts a quarter second
  after you say it. Spoken confirmations in your voice ("Sent.", "Cleared.",
  "Stopped."), a rising chime when listening starts and a falling one when
  it stops, a still mic ring that brightens as it hears you, and a small
  "Listening…" / "Speaking…" pill — nothing else pops up.
- **A voice router.** "Session three", "next session", "switch to <name>",
  "open a new session to fix the login bug", "what needs me", "what is it
  asking", "yes" / "no" / "always" / "option two" to answer a dialog, and
  "stop talking" to cut a reply short. The default is that your words stay
  with the session you are in. Spoken replies lead with the window's name
  when it is not the one you are in, and never talk over each other.
- **Read replies aloud: Off / Replies to what I said by voice / Everything.**
  In *Everything* every session's finished turn is spoken; the agent is
  asked for a one-line `Say:` on every prompt (a standing marker the prompt
  hook reads — typed-in-the-terminal prompts included), and only when a reply
  will be spoken, so typed prompts cost no extra tokens otherwise.
- **Speech engines that just work.** The OS's own engines on Windows and
  macOS out of the box (Dictation on a Mac), a one-click install of local
  models (faster-whisper + piper), or your own model server: any host that
  speaks the OpenAI shape plugs in with just its URL — Voice Lab's cloned
  voices, Kokoro-FastAPI, openedai-speech, a local whisper server — and a
  server on this computer needs no API key. Recognition streams with a fast
  model for the live words and re-transcribes each phrase once with the
  accurate one; both stay resident, and the voice engine warms at boot.
  **Settings → Voice** holds all of it, with a "Try it" row.
- **The wake word.** "Listen for “MindFlock”" keeps the mic in a standby
  that sends only a short lone utterance to be judged; the word on its own
  chimes and starts listening. A sentence containing it never wakes it.
- **When a session asks you something.** A permission or choice prompt —
  Claude Code's multi-question select prompts included, with their tabs,
  multi-select checkboxes, "Type something" and the review step — pops up
  in the middle of the screen (desktop and phone), with the prompt's own
  buttons, 1–9 / Y / N keys, a mic to answer by voice, and "Later". Several
  asking at once step through one card. A question asked in plain words at
  the end of a reply reads **Question** on the rail, the bell, the toasts and
  the phone push, with a Reply button. The desktop app raises an OS
  notification (and flashes the taskbar) when the window is in the
  background; the phone page can notify through a service worker. Dialogs
  are parsed for **every** agent CLI now — aider, opencode, cline, goose and
  antigravity join Claude Code and Codex — so each gets answer buttons,
  one-tap push buttons and voice answers. Claude Code's own menus (`/model`,
  `/effort`, `/config`, `/permissions`, `/usage`, the slash autocomplete) are
  never mistaken for a question.
- **A voice tutorial and your own keywords.** A guided tour opens the first
  time you use the mic (and from "Show me how" in Settings → Voice); the
  phone shows a "How to talk" card. **Settings → Voice → Voice words** lets
  you change the send / end / start-over / stop / stop-talking phrases and
  the wake word, ours by default, synced to your devices, read by the
  desktop, the phone, the router and the wake-word judge.
- **Fast-track hands a failed pre-commit to the agent.** The hook's own
  output and the exact steps left to the run's target are typed into the
  session at once; a repeat failure gets a fresh note, never a duplicate.

### Changed

- The mic opens in a quarter of a second: the microphone stream stays warm
  for 90 s after a phrase, the server stream opens on the press, and the
  local recognition models never idle-unload while local recognition is on.
- Settings reopens on the screen you last had open (for the page's life).
- The mic's right-click menu is four controls; explainers live in Settings.

### Fixed

- A spoken "Stopped." is no longer cut off by its own stop chime.
- Local recognition works with PyAV 15+; `en-US` is accepted as a language.

## [0.9.6] - 2026-10-22

Connect GitHub once and git uses the sign-in too, the Map tab's Code Tree
reads at every zoom, and a worker's fence is a contract it cannot talk its
way out of.

### Changed

- **Leaves open as you zoom in.** A folder's leaves are packed so no two
  outlines ever cross, each leaf points away from its twig, and past ~24 px a
  leaf stops growing on screen so the gaps open instead (`lib/codetree/leafgeom.ts`).
  Every file is its own shape with free air at its tip.
- **Every name is attached to the thing it names.** A file's name sits at its
  leaf's tip reading outward (or on the base side with a hairline), a folder's
  name sits on its own clump rim where the twig enters or runs along its branch
  (else beside its fork on a dot), and nothing is placed by "nearest free spot":
  a name that would float, cross the canvas edge or touch another leaf is not
  drawn. Hovering a clump when zoomed in shows that clump's names. A perched
  bird's tag names its file when the bird's body covers the tip
  (`lib/codetree/labels.ts`).
- **Tests are root strands with tips, never leaves.** Each test file is the
  tip of a strand in a fan under the code it tests; a changed test lights its
  strand (`lib/codetree/roots.ts`).
- **Non-code files are roots.** The roots hold what keeps the tree standing:
  tests under the branch they test, docs, CI and config under the trunk. Every
  file that is not code is a root tip like a test, in a cooler soil tone: a
  code folder's README and config on its own limb beside its tests
  (`frontend · non-code`), test data beside the tests it serves, and docs/,
  .github/ and the repo root's files as clumps of one tap root
  (`docs, CI and config`). Leaves are code files only. The leaf piles and the
  scaffold are gone; a root limb never says `tests: tests` (`other tests ·
  tests/`).
- **The Map tour teaches the tree in a newcomer's order:** the tree, roots,
  birds and changes, rules, then your next step. The legend,
  cards, screen-reader summary and `docs/web-ui.md` say the same words.
- **Fences hold.** Widening, retargeting or lifting a started worker's fence
  needs a reason and is logged per worktree and shown in the session's
  Activity ("fence widened by <orchestrator>: why"); narrowing is free; a
  worker can never change its own fence and its children inherit it;
  `whoami` returns the fence; the orchestrator guidance says to fence at
  spawn and, when a worker is blocked outside, do it yourself or spawn
  another rather than widen by reflex.

### Fixed

- **Connect GitHub once and git uses the sign-in too.** The token from
  Connect GitHub (device flow or paste) served only the REST API; every clone,
  fetch and push went through git's own credential helpers, which on a Mac
  meant the `osxkeychain` prompt ("enter the login keychain password") at
  every provision, and Setup's "Let git push with this sign-in" refused
  whenever any helper existed. While a sign-in is connected (Settings token or
  `$GH_TOKEN`/`$GITHUB_TOKEN`), MindFlock's own git calls, every session's
  environment and the Push shell run with per-process git config that makes
  `mindflock git-credential` the one helper for `https://github.com`: no git
  config file is written, other hosts keep your helpers and SSH remotes are
  untouched. Covers provisioning, the pipeline's clones, merge and divergence
  fetches, Check I can push, and the agent's own push.
- **The tool hook no longer invents targets.** `2>/dev/null` is not a file
  named `2`, a `chmod` mode is not a path, and `cp X dir/` writes `dir/X`,
  not the directory, so a fenced worker is denied only for a real landing
  outside its fence (about 50 of 129 October denies were phantoms). The
  fence deny names the fence, who set it and the allowed paths, and points at
  `report_result(blocked)` instead of "ask the user" (hook rev 5).

## [0.9.5] - 2026-10-19

The ticket pipeline runs the same code as the server, so 0.9.4's provisioning
fixes reach ticket sessions.

### Fixed

- **The ticket pipeline runs the server's code.** It was started with the
  repo root's own `.venv` and imported `backend` from that folder, so with
  `MINDFLOCK_REPO_ROOT` on a dev checkout it ran whatever branch that
  checkout had (a 0.9.4 server's pipeline reported 0.8.7), and fixes to how
  ticket sessions are provisioned never reached them. It now runs the
  server's interpreter with `-P` and the server's own package first on
  `PYTHONPATH`.

## [0.9.4] - 2026-10-19

Ticket sessions on your own checkout push to GitHub and start from GitHub's
base branch, and fast-track no longer pushes while the agent is mid-turn.

### Fixed

- **Ticket sessions on a local checkout push to GitHub.** A ticket routed to
  your own clone (e.g. `~/code/app`) got `origin` = that folder, so fast-track
  and Push sent the branch into your laptop and Open PR failed with "none of
  the git remotes … point to a known GitHub host". `origin` is now the
  checkout's own GitHub remote; the checkout stays the fast clone source.
- **Ticket sessions fork from GitHub's base branch, not your checkout's stale
  copy.** When your local `staging` is only behind GitHub's, the base
  fast-forwards to GitHub's before the session starts; local commits of your
  own are kept.
- **Fast-track never pushes mid-turn.** An agent merging its base branch made
  the branch read "committed" with a clean tree, and fast-track pushed it a
  second later with none of the agent's work. Until a run has done something
  itself, the push / PR rungs wait for the agent's turn to end, like a commit.

## [0.9.3] - 2026-10-19

Fast-track no longer falls off the path after a failure it hands to the agent:
once the agent fixes it, the run carries on to the push and the PR.

### Fixed

- **Fast-track carries on after the agent fixes a failure.** A failed
  pre-commit hook (or check, red CI, a conflict) told the agent what failed
  and then stopped the run, so the agent's fix, even its own commit, never
  reached the push and PR. The run now waits for the fix and continues from
  it: the agent's commit is pushed and its PR opened, an uncommitted fix is
  re-committed. Twice per run at most; a fix turn that changes nothing stops
  with the original reason.
- A failed check result from an older commit no longer stops fast-track; the
  check is run again on the new commit.

## [0.9.2] - 2026-10-19

Per-repo base branches, so a repo that only has `main` can fork from and PR
into `main` while the rest use `staging`; and a New dialog whose *Runs in*
line stays put while you type.

### Added

- **Per-repo base branches.** Settings → Workspace → **Per-repo branches**
  gives a repo its own base branch (where its sessions fork from) and PR base
  branch (where its PRs point), overriding the Workspace defaults — for a repo
  that only has `main` while the rest fork from `staging`. A row's blank PR
  base means that repo's own base branch. Ticket sessions, Open PR,
  fast-track and the multi-repo ticket instructions all honour it.

### Changed

- **New is easier to read.** The line under the box says *Runs in [folder ▾]
  · on a new branch* and no longer rewrites a branch name as you type, which
  made it look like the folder followed your sentence. Its duplicate **More
  options** link is gone (the footer's button is the one way to the form), and
  **Let the agent set it up…** is now **Let the agent pick the folder…**, the
  other way to choose that same folder (on the phone too).

### Fixed

- New could swap the *Runs in* folder a few seconds after opening, while you
  were already typing, when the folder suggestions finished loading late.

## [0.9.1] - 2026-10-19

The map in the flow: the Map is where you already are — a tab on every git
pane, a chip on the row, a line in Make PR, a row in the bell — and every way
into it lands on the file or section you came for. Plus a sweep of the
day-to-day loop and of starting and finishing work, from five audits and a
hands-on walk of 0.9.0.

### Added

- **The Map tab is on by default** for git sessions (a header you customized
  keeps your choice), with a badge: the rail's zone chip (⛔ breaches, an
  unseen block, *plan · n*) or the changed-files count. In the calm first hour
  it waits in ⋯ until the session has changed something.
- **Every "Open map" lands on the spot.** Toasts, the push/PR refusal card,
  the commit warning, the rail chip, the bell, Thread worker rows (**Map**),
  Verify (**What could break**) and the palette (**Find file on map…**, straight
  into the find box) open the Map flown to the file and/or opened at the right
  part: breaches, scope requests, the plan, ⚠ Could break.
- **Plan ready reaches you.** While a declared plan waits for Go, the session's
  `redzone` summary carries `plan_pending`; the row and the Map tab show
  **plan · n**, the bell has a *Plan ready — review & Go* row, a Plan-first
  start opens the Map on its plan, and the phone shows the plan with **Go** /
  **Go — only the planned files**.
- **What could break, before you ship.** Make PR says "⚠ Could break: 9 files
  in 3 folders (2 tests) — See on map" under the base branch.
- **The Map says what to do next.** This session's row in the agents column
  ends on one next step — *Ask for plan*, *Review plan · Go*, *Review: ⚠ Could
  break (N) · Open diff*, or *N breaches — review*. The tour is built from what
  is on screen (no changes → no change steps; hidden targets skipped), adds
  *Plan first*, and ends on that next step.
- **Diff ⇄ Map per file.** Each Diff file header has a Map button; the Map's
  Open diff (file card, breach rows) opens that file. The Diff follows the
  session as it changes, has ↻, and is Unified in a narrow pane unless you
  picked Split.
- **Allow a file from the toast** when an only-here rule refuses one edit.
- **Ctrl+K N** jumps to the next session that needs you, in the bell's order;
  the palette lists those sessions first, with their state.

### Changed

- **The Map fits the pane.** The whole-tree view fits the tree itself between
  the agents column and any open panel, the ground is a thin band when there
  are no tests, **Plan & lists** (was *Session*) docks beside the tree in a wide
  pane instead of covering the card its rows open, tour cards sit beside what
  they explain, changed files are always named and names sit on a backing.
  In a small pane the card sheet scrolls with its actions on top, Esc or a
  canvas click folds the agents column, and `/` finds from anywhere in the pane.
- **Adding a keep-out over your own changes asks first** ("4 changed files
  are inside — pushing will be blocked…"), and the guard pill reads red
  **N breaches** whenever there are any.
- **One word per thing on the Map:** rules (⛔ Keep out / ✓ Only here,
  **+ Rule…**), fences only for an orchestrator's limits; other sessions read
  idle / waiting instead of "editing on its own branch".
- **Toasts last and are heard.** An Undo is no longer replaced by the next
  message, time on screen follows the text, and toasts are a live region.
- **Clicking the selected row takes you back to it** (terminal focus, drawer
  closed) instead of renaming it; rename is a double-click on the name, F2 or
  the menu. "Needs you" chips are buttons (Question → the bell, Pre-commit
  failed → Terminal, Setup failed → re-run, Checks failing → the PR), a stuck
  idle session reads **Idle 25m**, and chips are sentence case ("Committed",
  "Checks ✗").
- **Queue:** Ctrl/⌘+Enter sends now, Alt+Enter queues, ✕ offers Undo.

### Fixed

- Open PR succeeded silently (a popup-blocked `window.open`); it now offers
  the link, and Merge says "Merged <name> into <base>".
- The Map spun on "Growing the tree…" forever when the snapshot failed but
  the live poll worked; failures now say so with **Retry**, and a stale map
  says "may be out of date". Undo after a rule could say "Undone" when it
  failed. The file card's rule buttons now show an existing rule.
- Settings and Intake saves that failed still toasted success (and Intake
  re-swept); typed Intake fields saved only on blur.
- Saving a template under an existing name silently replaced it, schedule and
  all; the template editor dropped unsaved edits on a backdrop click or Esc.
- "Insert saved text" replaced the Task instead of inserting at the caret.
- Intake's ‹ Back and tab switches dropped a new ticket source, pasted token
  included; a failed save said "Fix the marked fields" with nothing marked.
- New kept an "agent not installed / not signed in" warning after you fixed
  it, kept the last opening's "work on the branch itself", and a missing
  non-default agent sent you to Setup, which only checks the default one.
- Verify closed a step editor and cleared the retry banner after a failed
  save; settings search could land on a hidden advanced fold.
- Device ✕ in the sidebar disconnected in one click (now asks inline).
- Accessibility: pane tabs are a tablist, sidebar rows take ↑/↓, the bell
  says how many need you, the palette is a listbox, and New / Settings /
  Setup / Intake controls are labelled and announced.

## [0.9.0] - 2026-10-15

### Added

- **Long jobs are not "stuck".** A background job the agent left running is
  judged against its own rhythm (the longest quiet gap it has already resumed
  from) and, when known, its expected duration: tell MindFlock how long it
  should take from the job chip's **Expected to take…** (30 min / 1 h / 6 h /
  24 h / custom), from the agent (`expect_background_job`), or by prefixing
  the command with `MINDFLOCK_EXPECT=6h`. Inside that window the chip reads
  "waiting on job · 2h 10m of ~6h"; passing it raises one bell row; "stuck" only
  after 1.5× the expectation (or when the job provably waits on itself).
- **Joint tasks: two people, two repos, one feature.** On a People connection,
  **Start a joint task** writes one brief; the other person accepts with their
  own folder, and both shared agents start with the brief and a shared
  **contract** (`CONTRACT.md` in each shared folder: what we're building,
  the interfaces between the halves, who owns what, done means). Either agent
  can read it and propose a change (`peer_contract`); a person accepts or
  rejects the proposal in People. **Mirror** keeps the other person's
  checkpoints flowing into a repo you choose as a `peer/…` branch (their
  history crosses the link as an incremental bundle; nothing is written to
  your working tree). The **joint check** is one checklist with steps tagged
  per side: each side's agent runs its own (`peer_check`), people answer the
  human steps, results sync over the link, and one verdict shows on both
  shared-session rows. A People tour (the "?" in its header, Help → Tours).
- **People, safer and plainer.** Unlinking never deletes the other side's
  work (the shared session is closed, its folder kept). A re-pair un-confirms
  the safety number and says so. Nothing from a shared folder crosses the
  link until both sides press *It matches*; Share, Mirror and Export wait
  for it too. Turning peer links off asks first and keeps every connection
  and share (Reopen brings a share back). The connection card is three
  groups — Trust, Share, Talk — with labels, a message thread where you are
  (and in the bell), an *It doesn't match* button, and export results in
  words ("Brought home as peer/ana in widgets (3 commits)"). The invite
  message says where to paste it. Settings search finds People under pair,
  join and link.
- **Sign in with GitHub, out of the box.** MindFlock ships the client id of
  its registered GitHub OAuth App, so Connect GitHub leads with one **Sign in
  with GitHub** button (a code typed at github.com/login/device) on every
  install. You authorize the app on your own account and can revoke it from
  GitHub → Settings → Applications. The token path and gh are still there,
  folded under *Or paste a token*; `MINDFLOCK_GITHUB_CLIENT_ID` or
  `github.oauth_client_id` still override the shipped app.

### Changed

- **Workflow Optimizer: time to commit, not time to PR.** A commit finishes a
  unit of work, so the headline is now the median time from the first real
  work to the commit, and pushing, the PR and review move to their own
  *After the commit* table. The recorder now notes each branch's commits
  (reading HEAD from the worktree's `.git` files; only commits authored just
  now count, so pulls, rebases, amends and merge commits don't), and
  history recorded before that gets estimated commit times, marked "est.".
  Idle time between turns on a branch that already had a commit no longer
  reads as a "Committed, not pushed" handoff — it's your time, left out by
  default like any other turn of yours.
- **Workflow Optimizer: show only the phases you care about.** Every chip in
  the legend is now a switch: click one (say, everything but *Agent working*
  and *Pre-commit hooks*) to hide it, and its time leaves every number, the
  same way your own time does — click the struck-out chip to bring it back.
  The choice is remembered.
- **Workflow Optimizer: count pushing, the PR and review when you want.**
  The legend under the bar ends with *After the commit:* chips (Committed,
  not pushed · Pushed, no PR yet · PR open — waiting), off by default;
  switch one on and its waits join the headline time. Clicking any chip,
  view button or unit no longer scrolls the panel back to the top.

Round 5 of the fresh-eyes UX lab: seven judges who could only use the UI walked
0.8.7 as first-time users, and every surface was then simplified first and
given a short, replayable tour second.

### Added

- **Tours — one per top-bar menu, replayable.** The five-slide welcome is now
  one welcome card; "Let's go" starts a coach-mark tour over the Get set up
  checklist. New, Intake, Verify, Schedule and Settings each have a 3–6 step
  tour drawn on the live screen (a ring on the real control, one sentence
  each), which opens once by itself during the calm first hour and never
  again on any of your devices. Replay any of them from the **?** in the
  dialog's header, **Settings → General → Tours**, or the command palette
  ("Replay Intake tour").
- **A terminal first run that tells the truth.** `mindflock init`, `doctor`,
  `serve` and install.sh print one "Three steps to your first agent" block
  with ✓/✗ and "← you are here", and end on exactly one `Next:` line.
  "You're set" shows only when tools, the agent sign-in and a project folder
  are all done. `doctor` is grouped — Needed to run an agent / Needed to ship /
  Optional — with a legend; optional items (Tailscale, MTU, gh…) never enter
  the first-run fix loop and are never installed by pressing Enter. The
  `serve` banner boxes the browser URL ("it walks you through the rest") and
  prints no phone-view box in local mode. `--help` is grouped and ends with
  "New here? Run mindflock init". README opens on a 3-step "Get started".
- **Verify: empty checklists, honest states.** Start an empty checklist and
  write the steps yourself; branches with no commits are marked and refused
  ("Nothing to check yet — this branch has no commits") and the checklist is
  re-written by itself once the session commits. "Not written yet" is its own
  grey state, off the Failed badge. **Check now** is visible on every card
  with unchecked agent steps; the pane's SHIP menu shows the verdict beside
  Open PR and asks before opening a PR on a failed branch; a failed step
  reaches the bell. "Verified on branch · by you / by the agent".
- **Intake → Auto-start is one home**: three labelled switches (Tickets / PR
  review comments / New issues), each saying how often it checks, plus
  "Pause all auto-start"; the other tabs show one status line and a Change
  link. Empty PRs / Issues tabs show one card (Connect GitHub → pick a repo →
  turn on). Each ticket row says which repo it goes to; "Open session ›"
  closes Intake and shows the session. "Create a token ↗" for every tracker.
- **New: a folder you name but that doesn't exist** shows "No folder at …" and
  a **Create this folder** button — nothing is created, git-inited or
  remembered on its own. A non-git folder asks one question: make it a git
  repo first, or just run in it.
- **A draggable folder picker.** Every editable folder field (Settings →
  Workspace folder, New's Folder, "Runs in", the template editor, peer links)
  has a 📁 Browse… button that opens one floating "Pick a folder" window you
  can drag and resize; Esc closes it, Enter picks, and it remembers where you
  left it.
- **Every tour scrolls to its step** (and opens a closed fold first), so no
  step hides below the fold. The Settings tour now walks seven stops across
  Connections, search, Agents, Workspace, "Show advanced settings" and Help.
  Tours and the hints switch live under **Help** at the bottom of Settings →
  General.
- **Settings search says what is behind each result** ("Phone alerts — push
  notifications to your phone (ntfy)" vs "Your phone — use MindFlock itself
  from your phone"), in Settings and in the palette; title matches rank first.
- **The top bar fits any width ≥ 360 px**: Settings, Schedule and Verify fold
  into one ⋯ (with their badges) as the window narrows; New, Intake and the
  palette never fold. Session menus flip and scroll so Delete… is always on
  screen. The Close toast has a real **Undo** button that holds the toast
  while you hover or focus it.
- **Schedule shows the same one-line schedule everywhere** (card, New's
  template chip, Intake → Auto-start): "Every day at 07:30 EDT · next Fri
  07:30 (tomorrow skipped)". A compact in-card editor changes just the
  schedule; Edit keeps the full form. Run-history names are links.

### Changed

- **On rails in the first hour.** Verify is hidden from the top bar until a
  checklist exists and Schedule until a template exists (Alt+V / Alt+S, the
  palette and the URL still open them); the first paint is calm until the
  server answers, so the full chrome never flashes and collapses. Setup shows
  steps ①–③ only: "All checks" lists just what blocks getting started,
  Tailscale/MTU/sudo fixes moved to Settings → Your phone, the first-start
  CLI transcript moved to Settings → System logs, step ① says "Install Claude
  Code". Once a session exists, Setup's primary is **Show my session →** (not
  "+ New session"). The welcome card no longer sends a 0-session user into
  the tracker wizard, and every exit marks it seen.
- **New: one primary button, Start.** The folder is a real picker under the
  task box ("Runs in 📦 repo · own branch … · change"); "Let the agent set it
  up…" is secondary with a bounded wait and one recovery; "More options"
  holds Name, Fast-track and Save as template, with everything a first-hour
  user can't use yet under a nested Advanced fold. Uninstalled agents sit
  behind "More agents…". Push / Open PR are greyed with a reason while
  there's nothing to push or no GitHub. The Close toast is the Undo; Delete
  says uncommitted work is lost; a second ✕ within 400 ms is ignored. Fast-track
  levels read the same everywhere; "⚠ Skip permission prompts" wears its
  warning on every chip.
- **Settings opens on Connections** (the status page); budget / resume /
  revive / sleep moved under General's Advanced fold; "More connections"
  folded; Agents shows "Not installed — how to install" beside the picker and
  Test answers in words; Your phone is live ✓/✗ rows with the QR only once it
  will work; Your devices puts "Add a device" first. Connect GitHub: the token
  path is the one numbered path (the gh CLI route folds under "Other ways"),
  a **Done** after "✓ Connected", and "Check I can push" names the repo and
  remote and gives one fix.
- **Intake: Test saves nothing.** Testing a new ticket source persists
  nothing; Finish is the one save and the "Connect your tracker" step ticks
  only from a saved source whose test passed. Jira errors are sentences
  ("No Jira site at <host> — check the Site URL"). A source that can't
  connect says so instead of "No tickets are assigned to you".
- **Schedule never reports a failed run as a success.** Agents are tagged
  "(not installed)" and a schedule with one won't save; Run now and scheduled
  fires with a missing agent record a failed run ("Last run failed: …"), red
  card, badge. Custom (cron) is seeded from the days and time on screen;
  every mode previews its next runs; one time format and the zone of the
  computer that runs schedules. One word: template.
- The mobile page's New sheet uses the desktop's words (Start / Let the agent
  set it up… / More options) and the same Delete warning. A template named
  "pr-…" is no longer shown as a PR session. At 420 px a modal opened from
  the sidebar drawer closes the drawer first and the Setup chip stays reachable.

### Fixed

- The terminal scroll speed reset on every restart and every reload: the
  server re-applied a legacy file instead of the synced setting at boot, and
  the browser applied the sub-line part only once Settings → General had been
  opened. The setting is the source of truth now and the app applies it at
  boot. Settings → General leads with the scroll speed; budgets, resume and
  sleep sit in a named fold that starts closed, and Help comes last.
- Auto-split refused a folder that wasn't a git repo even with "Make it a
  git repo first" ticked; the split path now runs the same `git init` + first
  commit as a plain start.
- **Database explorer drivers survive updates.** A driver installed with
  **Install driver** (psycopg, pymysql) vanished after every engine update or
  reinstall. It is now recorded in `~/.mindflock/dbclient/drivers.json`;
  updates carry it (`uv tool install --with …`, `pipx inject`), and one an
  update still dropped (install.sh, a hand-run reinstall) is put back in the
  background when the server starts ("Reinstalling psycopg after the
  update…"). `mindflock doctor` lists them.
- Verify's **Fix…** on a failed checklist returned a 500 (`create_instance()`
  missing `request`), as did Run; errors are plain sentences and never print
  an API path.
- Testing a ticket source saved it (and its token) and ticked the setup step;
  the Install window closed before showing success; the sign-in terminal's
  highlighted option was unreadable; Reopen left "Recently closed (n)" stale.

## [0.8.7] - 2026-10-14

A pass over New, Intake, Verify, Settings and the first hour, judged by
fresh-eyes testers who could only use the UI (never the code or docs).

### Added

- **Templates are easy to reach and can run on a schedule from anywhere.**
  The template strip shows on the New dialog's first page; each template has
  ▶ Run now and ⏰ Schedule… right on it. "Schedule a session…" is in the
  command palette (also found by "recurring", "cron", "nightly"), and
  scheduled templates are listed under Intake → Auto-start.
- **Verify says yes or no.** Every checklist leads with a verdict (Verified
  on branch, Verified live, Failed, 2 of 3 checked, Agent checking…) and the
  list is grouped Failed · Needs you · In progress · Verified. The same
  verdict shows as a chip on the session's sidebar row and pane header.
  **Check now** runs a checklist before merge without losing the after-merge
  check. You can ask for a checklist for any branch (even one no session was
  on), or start an empty one and write the steps yourself. Failures are red,
  turn the top-bar badge red, and reach the bell.
- **Start a ticket in the repo you choose.** On a source with several repos,
  each ticket row shows where it will go and why, and its Options have a
  Repo picker.
- **Search settings**, from a box at the top of Settings or the command
  palette ("github", "worktree", "parallel", "phone" all land on the right
  control).
- **Each surface has a URL**, so the browser's Back closes New, Intake,
  Verify, Settings or Templates instead of leaving the app.
- **Light/Dark in Settings → Appearance**, and a first launch follows the
  system's colour scheme.


- **Cron sessions: templates run on any cron schedule.** Besides every day /
  weekdays at a time, a template's schedule can be a cron expression
  (`*/30 * * * *`, `0 21 * * 1-5`, `@weekly`), previewed in words with its next
  runs as you type. Each fire starts a fresh session with the prompt sent; a
  scheduled run is skipped while the previous one is still working or waiting
  on you, so a slow job never stacks sessions. A template can also carry an
  autopilot depth (commit, push or open a PR) that every run is armed with.
  Cards show the last 20 runs; a scheduled run that can't start is a bell row
  and a push. `mindflock run <template>` starts one from a shell and
  `mindflock templates` lists them.
- **Fast-track tells the agent what failed.** When a run stops on something
  the agent can fix (a pre-commit step or the verification check failed, CI
  went red, the PR conflicts with or is behind its base), the agent gets a
  one-line `[MindFlock note …]`: what failed, where to look, and to fix it
  and commit, or to stop and tell you when it clashes with someone else's
  work. It is typed only once the agent is idle and never while you type. The
  ✗ tooltip and the *fast-track stopped* toast say the agent was told. Team
  run members are left to their run's own fix prompt. On by default; turn it
  off in Settings → Workspace → Advanced.
- **Workflow Optimizer: where each ticket's time goes, start to PR.** A new
  built-in extension (sidebar bar **Workflow**, palette "Workflow:
  Optimizer") records every session's phase — setup, agent working, waiting
  on your answer, your turn, idle while you're away, pre-commit hooks,
  recovering from failed hooks, committed-not-pushed, pushed-no-PR, PR open,
  usage limit — and breaks each work item (one branch's trip to a merged PR)
  down by them. It shows the median time to PR (and active time, without idle
  hours), time to merge, which phase is the bottleneck with what to change
  about it, a weekly trend, and every item's own timeline. Recording starts
  when you install it; server downtime shows as untracked, never as work.
  Your own time — answering the agent, your turns, idle while you're away —
  is left out by default, as if you answered instantly, so the bottlenecks
  are the agent, tooling, handoffs, limits and review; the **Include my
  time** switch in the dashboard counts it again.

- **Memory guard.** MindFlock reads memory pressure (available memory plus
  PSI) and, while it runs low, holds new agents that agents or the ticket
  queue would start (a person's New and Restart are never refused), starts no
  new team-run lines, and shows a notice. An agent that died mid-turn comes
  back with "continue where you left off" queued, and **Restart all** above
  the rail brings back every offline agent at once.
- **Sleeping agents.** An agent idle for 45 minutes (5 while memory is low),
  unwatched and with nothing waiting for it, is put to sleep: its CLI and MCP
  servers stop and their memory is freed, the row says **Sleeping**, and
  opening it or sending it anything (including another agent's message)
  wakes it in the same conversation. Settings → General.
- **Agents, not MindFlock, are what the OOM killer takes.** Every agent
  launch raises its own `oom_score_adj`, so under memory pressure the kernel
  kills a test run or an agent before the server; an agent killed that way
  comes back by itself once memory recovers (at most 3 times an hour).
- **`mindflock slot -- <command>`**: a machine-wide gate for heavy commands
  (test suites in commit hooks) so several sessions take turns instead of
  running out of memory together. One slot below ~32 GB of RAM, else two
  (`general.heavy_slots`).
- **Shared MCP servers** (opt-in, Settings → Agent orchestration): a stdio
  MCP server you mark as shared runs once per machine and every Claude window
  connects to that copy, instead of one copy per window.
- **Schedule** on the top bar (Alt+S): your templates and what starts by
  itself, in one place — what runs next, scheduled templates with Change
  schedule, Skip next and Pause, and saved setups. Its template editor uses
  New's folder explorer, agent picker and Works in.
- **Task headlines.** The line under each session in the sidebar is a short
  verb phrase of what it's working on ("Fix pin bar drawing below
  terminal"), made by fixed rules (no model) — or your own words when the
  rules aren't sure. Hover it for the prompt it came from.
- **Branch session (Ctrl+K B).** Like Duplicate, but the copy gets its own
  worktree on a new branch cut from the session's current commit, with its
  uncommitted changes copied across (the original is untouched). Git
  repositories only.
- **Customize the pane header.** Customize now has two pages, Sidebar and
  Pane header: choose and order the tabs and buttons along the top of every
  session (default Agent · Terminal · Diff · Queue · ⏩ · Copy all), with a
  live preview. Words until they don't fit, then icons; ⋯ is always on the
  right and holds everything switched off.
- **ⓘ tips** replace the "Learn more" folds: hover or tab to the icon for the
  explanation, tap it on a phone.

### Changed

- **Intake sources: add once, edit where it shows.** No more card lists:
  each source's heading in the work list has **Edit** (red **Fix** when it's
  broken) that opens its settings right there; one **+ Add source** adds a
  tracker or a repository, and a repository is added once with "Use it for"
  ticks (PR comments, new issues, Verify). The ticket view filters are one
  **View** menu, and Settings → Connections → Open in Intake lands on that
  source.
- **Adding a source starts with nothing picked**, and nothing names a tracker
  until you choose one; GitHub Issues as a tracker and a GitHub repository's
  issues are told apart.
- **The New dialog** sits centred with room above and below, its folder
  suggestions are one compact line, and Works in reads as clean aligned
  options. **New, Intake, Verify and Schedule open at the same top edge** and
  don't move as their content changes.
- **Pane header:** Terminal and Map are tabs again in every pane (calm mode
  had hidden them), a **Copy all** button copies the open terminal's whole
  history, and there is no Pause button (the palette, `mindflock pause` and
  the API still pause; ▶ Resume shows only on a paused session). The ⋯ menu
  opens fully on screen.
- **Sidebar rows:** the actions menu is a ⋮ at the right end of the row (on
  hover and the selected row), and right-clicking a row opens it.
- **The server uses far less memory** — about 230 MB instead of growing past
  1 GB — by capping glibc's per-thread arenas and reading transcript tails
  instead of whole files.

- **One agent card everywhere** (Connections, Settings → Agents, Custom
  agents, Setup): the agent's status and its one next step — Install, Sign
  in, or Test — with Test answering "Signed in as you@example.com". The
  sign-in window describes what the agent's terminal really asks. A session
  whose agent isn't signed in says **Not signed in — Sign in**, not "Idle" or
  "claude exited 1"; Setup's "Start your first agent" ticks only once the
  agent has actually started working.
- **A calm first hour.** Until you have a few sessions the sidebar shows
  sessions, New and Setup, nothing else; it no longer flashes the full
  layout on load. The welcome tour opens once (per person, synced), and
  setup shows one count everywhere.
- **One menu per session** (the row's ⋮ and the pane's ⋯ are the same, in
  groups), **Commit…** only when there is something to commit, the session's
  task in the pane header ("Task: …", kept on the server). Names are
  shortened in the middle, so "-2" stays visible.
- **New:** a git folder defaults to a separate worktree and shows the branch
  it will create (the real "-2" name when the repo is busy, and who else is
  in it); Folder → Agent → Task → Create, the rest under More options; what
  you typed is never thrown away; "Create session" starts straight away;
  agents that aren't installed are marked and refused; a blank name is taken
  from the task; "Save as template…" saves the whole form.
- **Intake:** a new ticket source auto-starts nothing until you pick a state
  or label (existing sources are unchanged); text saves as you type and only
  then says "Saved ✓"; tabs named **GitHub PRs** and **GitHub issues**; the PR feature is "Auto-
  address review comments" and starts off for new setups; one **Start** verb
  on every row; plain reasons when a ticket is skipped.
- **No more Pause intake.** The sidebar Tickets bar's ⏸ and Intake's
  **Pause intake / Resume intake** are gone: each tab's own switch (Auto-start
  tickets, Auto-address review comments, Auto-start new issues) already turns
  its automation off and on. Anything an earlier pause turned off simply
  shows as off — turn it back on with its switch.
- **Settings:** Notifications opens with "Where alerts go", alerts are in a
  few plain groups with one-line descriptions, and "a session finishes its
  work" is on for anyone who never changed their alerts; Connections leads
  with your agent, GitHub and tracker; Connect GitHub is three numbered steps
  with the result shown in the dialog; phone alerts keep the same topic;
  Workspace shows where worktrees live (copyable, checked as you type); basic
  mode shows no power-user controls; every save shows "✓ Saved".
- **Words:** one vocabulary across the app — *agent* (never "Agent CLI" or
  "provider"), *auto-start* (never "ingestion"), *checklist* and *step*
  (never "plan").


- **Workers always stay with their orchestrator on the rail.** A worker now
  draws directly under its parent's family whatever the saved order says.
  Dragging a worker elsewhere no longer un-nests it (it only reorders among
  its siblings), dragging an orchestrator carries its workers, and the row
  numbers, Alt+N and Ctrl+Tab follow the family order.

### Fixed

- **Pause sticks.** Opening (or having open) a paused session's pane no
  longer relaunches its agent; the pane shows "Paused · Resume" instead.
- An OOM-killed agent is no longer relaunched by its own launcher three
  seconds later (and, on a second kill, into a fresh conversation); a killed
  provisioned agent reads Offline, not Idle.
- Claude Code's "This session is being continued…" compaction message no
  longer becomes a session's task line.

- Aider (and other API-key agents) no longer read "Connected" without a key.
- Notification rules no longer show raw `{detail}` placeholders.
- The command palette no longer offers "Join with code W0RK-TREE" for
  ordinary words, and never makes Push/Merge/Delete the Enter default.
- Esc closes the Shortcuts sheet; top-bar tooltips show the shortcut that
  actually works (Alt+N in a browser tab).


## [0.8.6] - 2026-10-13

### Added

- **An intake source can span several repos.** Add more than one repo to a
  ticket source (each with optional hint words) and every ticket is routed to
  the repo(s) it needs: by its labels, then its title, then a one-shot ask to
  the agent CLI when those can't decide, else the first repo. The ticket row
  shows the choice and why ("→ vnext-frontend: label 'frontend'"). A ticket
  that needs two repos starts in the first, and its prompt lists the other
  repo's path with the steps to branch and open a separate PR there. A source
  with one repo works exactly as before.
- **Templates can run on their own.** Each template card, and a "Run template:
  …" row in the command palette, has **Run now**: it starts a fresh session in
  the template's folder with its prompt already sent. A template can also run
  on a schedule (every day or weekdays, at a local time). Only the automation
  device fires schedules, a slot never fires twice, and after downtime only the
  most recent missed run fires.

### Changed

- **Settings opens on the basics.** Six screens (General, Connections,
  Notifications, Agent CLI, Workspace, Appearance) by default; the other
  thirteen — other devices, your phone, people, providers, accounts, security,
  troubleshooting — sit behind "Show advanced settings", a preference that
  syncs across your devices. Advanced controls on the basic screens (phone
  push, usage-window warming, fast-track depth, …) moved into collapsed
  "Advanced" folds. Deep links to an advanced screen still open it.
- **Settings and Intake are easier to scan.** A field's hint sits close under
  its own control and the next field starts clearly below it, so a hint can no
  longer read as the label of the field after it. Sections, cards and button
  rows got room to breathe, and on a narrow window the Settings nav becomes a
  row of tabs.
- **Ticket source cards show the essentials first.** The claim label, "move to
  state on start", "comment when moved", Test-filled IDs, thinking effort and
  fast-track depth moved into an "Advanced options" fold that opens by itself
  when one of them holds a value you set.
- **First-run setup is three steps.** Install the tools, sign in to your agent,
  start your first agent; connecting GitHub and a tracker are two optional
  steps, and the phone (Tailscale) moved to a closed "Optional, later" group
  that no longer counts toward the setup chip. Optional tools (Tailscale,
  bubblewrap, cloudflared, …) no longer hold the install step open.

## [0.8.5] - 2026-10-13

### Fixed

- **A session whose turn has ended no longer reads "Working" just because the
  agent left a background job running.** Claude Code reports a session busy
  while any of its background shells is alive, so one leftover job (a wait
  loop that could never end, in the case that found this) kept a finished
  session "Working" for over half an hour. MindFlock now looks at each such
  job: one still using CPU or writing output keeps the session Working; a
  quiet one reads Idle with a "job waiting" chip; one quiet for 15 minutes, or
  one that provably waits on itself (a `pgrep -f` loop that only matches its
  own command line), reads Idle with a red "job stuck ✕" chip that stops it.
  Autopilot and the MCP `wait_for_session` tool hold while a job is waiting,
  never for a stuck one. Linux and WSL only; macOS is unchanged.

## [0.8.4] - 2026-10-12

### Fixed

- **The Usage figures (day, week, month, year, and each session's cost) are
  now right.** Every API reply was counted several times, because Claude Code
  writes one reply as several transcript lines, and copies them again when a
  conversation is resumed: totals came out about twice too high. At the same
  time the work done by subagents (the Agent tool) was never counted at all,
  and that is often most of it. Both are fixed: each reply counts once, and
  subagent work counts, priced with the model it actually ran on.
- **The year total no longer shrinks when Claude Code deletes old
  transcripts.** A day's total is remembered and never drops because its files
  disappeared. Totals saved by older versions were inflated, so they are
  recomputed where the transcripts still exist; older days are kept as an
  estimate, and the Usage popover says "Includes estimates before" that date
  under the year. The old file is kept as `usage-history.v1.json`.
- **A session's cost no longer picks up its sibling's conversations.** When a
  window and its copies share a folder, each window now remembers every
  conversation it has had, so a conversation started with `/clear` after a copy
  was made, or windows working at the same time, are counted for the right
  window.

## [0.8.3] - 2026-10-12

### Fixed

- **Ctrl/Cmd+Enter in the New dialog's sentence box no longer creates an
  empty "untitled" session.** On the first card the full form isn't shown, but
  the shortcut submitted it anyway: no name, last time's folder, and the
  sentence thrown away. It now reads the sentence, like Enter and **Review
  details first**. A list still starts its rows, and the full form still
  submits as before.

## [0.8.2] - 2026-10-12

### Changed

- **The New dialog looks and works as it did in v0.7 again.** It opens on the
  compact *What do you want to work on?* card with **Set it up myself
  instead**, **Review details first ↵** (what Enter does) and **Create
  session**; the full form is page 2, with **← Back** to the sentence and no
  describe box of its own. The form keeps Templates, last-used pre-fill
  (Name → Enter creates), Runs on, Fast-track and the folds. Page 1 and the
  Ticket tab share one top edge at their natural heights; the form is
  centred.

- **One wording across the UI, CLI and MCP.** The CLI gains `close`,
  `delete`, `pause` and `resume` (`rm` and `rm --wipe` stay as aliases). The
  MCP `kill_session` tool keeps its name but is titled "Close or delete a
  session". Pause and Close now say how they differ: Pause keeps the session
  in the sidebar to resume, Close moves it to Recently closed. Fast-track has
  one set of levels; both `off` and `leave` are still accepted, so no saved
  state needs migrating. "shipped" is replaced by the actual step (committed,
  pushed, PR opened, merged); Verify says "pushed".
- **The row menu, the command palette and the keyboard chords read one
  action table.** Where they disagreed, one rule now applies everywhere. Merge
  PR is offered only once a PR is open. A chord for an action that isn't
  available says so instead of firing a request the server refuses.
- Relative ages read "5m ago" everywhere (was "5 min ago" in some places).

### Fixed

- Five small stores (aliases, hidden windows, session owners, people, shared
  branches) were rewritten in place, so a crash mid-save could truncate them.
  They now write atomically, and their files are private to you (0600).
- The "is there a GitHub token" checks disagreed with the token actually
  used: one checked config.toml first, the real chain checks it last. A token
  kept only in config.toml showed "none" in Connect GitHub and Test. All of
  them now use one chain.
- About a dozen dialogs (Rename among them) closed when a click or a text
  selection *ended* on the backdrop. Every dialog now shares one shell that
  closes only on a press that starts on the backdrop.

### Internal

- `backend/web/server.py` shrank from 16,931 to about 11,400 lines. Route
  groups moved to `backend/web/routes/`. The autopilot, Verify, prompt-queue,
  state-event, session-removal and red-zone code moved to their own
  `web/core/*_driver` / `state_events` / `session_removal` modules. One
  subprocess helper (`core/proc.py`), one atomic-write helper
  (`backend/fsutil.py`) and one git helper (`backend/gitutil.py`) replace
  dozens of copies. NewSessionDialog and VerifyDialog are split into modules.
  About 300 pytest checks that grepped the built bundle are now vitest tests
  against the source. Five routes with no caller, their code, and unused
  functions and aliases are gone.

## [0.8.1] - 2026-10-11

### Added

- **The desktop app updates itself.** Automatic updates are on by default
  (Settings → Advanced → **Install updates automatically** turns them off):
  a downloaded app release — engine first, then the app — or an engine-only
  release installs on its own once you've been away for 10 minutes (system
  idle, or the screen locked; a minimized window also counts for an
  engine-only update) and no session on this machine is mid-turn, waiting at
  most 2 hours for that. "Skip this version" is honoured, each version is
  tried at most once per run, an update the app can't apply here (e.g. macOS
  refusing the swap) is left to the toast's button, and nothing installs on
  quit. Afterwards the app says once what happened ("Updated MindFlock to
  0.8.1 while you were away"). The decision is a pure function
  (`autoUpdateVerdict` in `electron/update.js`) with its own tests.

- **Shared folders on macOS.** A peer-link shared session now runs on a Mac,
  under a deny-by-default Seatbelt profile (`sandbox-exec`, built into
  macOS). Linux keeps bubblewrap. The profile gives the agent the same set
  the Linux sandbox does, at the same paths:
  - read-only: system code, the agent's own runtime and the repo's git dir;
  - read-write: `work/` (but not its `.git` file) and its own home (with a
    private `TMPDIR`);
  - network: the in-sandbox bridge to the egress proxy, and nothing else.
    That means no DNS, no other local port (the MindFlock server included)
    and no other host.

  It also takes back what Apple's baseline profile would leak: other
  processes' argv and environment (`kern.procargs2`) and process info. The
  keychain stays unreachable. The agent runs in its own session, so
  keystroke injection into the pane (`TIOCSTI`) fails, while a small
  supervisor outside relays resize and signals. The doctor reports the
  built-in sandbox on macOS, and Settings → People no longer says a Mac can't
  share a folder. On a Mac, claude's login is read from the keychain. New
  provider profile field `tmpdir_env` (TOML `[peer] tmpdir_env`): claude
  sets `CLAUDE_CODE_TMPDIR`, which otherwise uses a fixed `/tmp/claude-<uid>`.

### Fixed

- **A desktop app run from source no longer offers updates it can't do.**
  The dev shell (and any unpackaged `electron .` run) used to show the
  "Update available" toast and an OS notification for every release, and its
  Update button fell through to "This app can't update itself here — install
  the download that just opened". A source run now skips release checks
  entirely (logged in `main.log`), refuses the update run, and Settings →
  Advanced says "running from source — update the checkout with git" instead
  of a button. A packaged app pointed at a developer checkout
  (`MINDFLOCK_REPO`, or an editable engine) no longer shows the engine-update
  toast either.

- **A shared session no longer logs Claude Code out on the host computer.**
  The sandbox used to get a copy of claude's whole login, refresh token
  included. Refreshing rotates that token, so once the sandboxed claude
  refreshed, the host's stored copy was dead and every other claude on that
  computer had to log in again. Only the access token is seeded now (Linux
  and macOS). `CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`) and
  `ANTHROPIC_API_KEY` pass through when the server has them. A launch with
  no login, or one about to expire, is refused with the fix ("run `claude`
  here once") instead of starting an agent that can't log in.

## [0.8.0] - 2026-10-11

### Added

- **Usage Optimizer** (a built-in Addon API v3 extension, id `optimizer`,
  bar **Usage**). The thesis: a plan window is use-it-or-lose-it, so the
  dashboard scores **capacity used** — the share of the windows your plan
  could have filled that your windows actually reached (a window that reset
  at 40 % counts 0.4, one you never opened counts 0) — per provider (Claude,
  Codex, Antigravity) and overall, over 24 h / 7 d / 30 d. Live cards show
  the active window and the weekly cap with a pace reading (used share vs.
  elapsed share, the projection at reset, the time the limit lands at the
  recent burn). Advice follows from it: draining faster than the window
  while you are at the keyboard says to move routine work to a cheaper model
  (`/model sonnet`, lower effort) so the lockout does not fall in your
  working hours; a window about to reset half-empty, or idle unattended
  hours, say to queue work. Charts: window fill (one bar per reset, tinted
  by whether you were there), an uptime strip (working / idle / locked out /
  no sessions, with a "you were here" row) and a time split that separates
  **attended** time (someone typed into MindFlock in the last 10 minutes)
  from runs on their own (overnight, after hours, looping queues), plus
  tokens per hour for Claude. A sampler records one reading per provider per
  minute (change-driven, 5-minute heartbeat) into
  `~/.mindflock/usage_optimizer.jsonl` (35-day retention); routes under
  `/api/optimizer` (`summary`, `sample`, `samples`). Opens as the dialog or
  as a grid window.
- **The Map opens fast and explains itself.** The focused pane prefetches its
  Map, the server warms every session's snapshot at boot, and the import-scan
  memo is kept on disk (`~/.mindflock-assistant/.code-map-cache/`), so the first click is a cache hit instead of "Growing
  the tree…" for seconds (3.2 s → 0.4 s with a 3 s snapshot). A six-step tour
  opens over the first Map you see (synced: `prefs.map_tour`) and replays from
  **? Guide**. The default surface is calmer: the rule tools sit in one
  **Rules** group, the guard pill waits for a zone, the bird index lists five
  changed folders (then **Show all N**), the session split shows only when
  another session changed something, and an empty Rules list is gone.
  `MINDFLOCK_CODE_MAP_WARM=0` skips the boot warm-up.

- **Two people on one pushed branch.** The New dialog's Base branch picker
  offers, for a remote pick `origin/x`, **Work on x itself (shared with
  whoever pushed it)** next to the default new branch: `POST /api/instances`
  `continue_branch: "x"` (with `base_ref: "origin/x"`) checks the worktree out
  ON `x` tracking `origin/x` (fetched first; 409 naming the worktree when `x`
  is checked out elsewhere; 400 for a non-remote ref). Rows carry
  `shared_branch` (a ⑂ on the rail row) and `behind` (↓N on the pane head).
  Every push now fetches its branch first and answers 409 `diverged` ("x
  moved on origin: N new commits by …") instead of a rejected shell push; the
  card offers **Rebase onto origin/x and push** (`POST
  /api/instances/{t}/rebase-remote`; a conflict aborts and names the files)
  or Cancel — never a force-push. The MCP's `ship_session` reports it as a
  `diverged` step; `get_session` carries `shared_branch` and `behind`.
- **Connections is the dashboard.** Settings → Connections lists every
  outside thing MindFlock talks to, one row each, in three states
  (connected / needs attention / off) with one action and one Test per row and
  the fix command with Copy: every agent CLI (the default marked; Sign in
  opens the in-app login terminal, Off shows the install command), GitHub
  (needs attention when the token's last Test failed), each ticket source,
  Your devices, Your phone (a `/m` load in the last 7 days), People, the MCP
  auto-attach (needs attention when the last launch got no tools, naming the
  CLI), each saved database connection with its last Test, ntfy (the last
  push), and extensions — including ones that failed to load. A header line
  counts "N connected · M need attention · K off"; it refreshes every 30 s
  and on `mf:connections-changed`. Row contract in
  [docs/web-api.md](docs/web-api.md); the old `status` vocabulary stays one
  release as `legacy_status`.
- Extensions that fail to load at startup are recorded (file + error) and
  listed on Settings → Extensions — now its own nav group — and in
  `GET /api/addons` `failed`, instead of only being logged.

- **A person behind your devices.** Settings gain `person` — `{id, name,
  git_name, git_email}` — one identity all of one person's devices share
  ([docs/people.md](docs/people.md)). The id is made once (an existing store
  gets it on its first start), a device that pairs adopts the inviter's, and
  devices paired before this settle on one over settings sync (the automation
  device's, else the smaller). Every request proxied to another device carries
  `X-MindFlock-Person`; new sessions record their person (`person` on
  `/api/instances` rows), fleet events and ticket claims carry it, and MCP
  `whoami` returns `person: {id, name}`. With a git name and email set,
  MindFlock's own commits — fast-track/ship, team-run merges, the peer-link
  export (no longer the anonymous "MindFlock peer") — are authored as you.
  `GET/POST /api/person`; a **You** card for Settings → Your devices. The
  person is an identifier, never a credential: the token and pairing still
  gate everything.
- **One keyboard per session; see who else is there.** A session has an
  owner (the person it was created for; `POST /api/instances/{title}/owner`
  reassigns it), viewers, and one driver
  ([docs/people.md](docs/people.md#owner-driver-viewers)). The first person to
  open a terminal on a session nobody drives holds its keyboard; everyone
  else's terminal is read-only — a keystroke shows "Alice is driving — take
  control to type" over the pane — and `/send`, the prompt queue, Send now
  and `/answer` (so the MCP's `answer_prompt`) answer **409** `driven by
  <name>` unless `"take": true`. `POST /api/instances/{title}/driver`
  `{"take"|"release": true}` hands it over (the previous driver is told by a
  `session.driver_changed` event); it lapses after 10 idle minutes and when
  the driver's last terminal closes. `/api/instances` rows gain `owner`,
  `driver`, `driver_name` and `viewers` (`session.presence_changed` on every
  change; remote rows pass through unchanged, and taking a remote session's
  keyboard rides the gateway). The rail shows up to three presence dots per
  session, the driver's ringed, and the pane header a **Driving** /
  **Watching — take control** button — only once someone besides you is on
  the session, so one person across their own devices sees no change. MCP
  rows carry owner/driver/viewers and `whoami` says `you_drive`.

- **Shared ticket claims, through the tracker.** Starting a ticket now says so
  on the ticket: it is assigned to you (when unassigned; never on an
  "anyone's tickets" source) and labelled `mindflock:working` (per source
  `claim_label`, default `mindflock`), and the label comes off when the
  session closes or its PR merges — the assignment stays. Every start path
  (Begin work, the pipeline, team runs, agents) treats a ticket assigned to
  someone else with that label, or in a started state, as taken: Intake reads
  **"<name> is on this"** and Begin work answers 409 `claimed_by` with an
  owner-only **Override** (`override_claim`). Works with zero pairing; paired
  devices are still asked first. Every provider gained assign / label /
  unlabel calls ([docs/ingestion-pipeline.md](docs/ingestion-pipeline.md#shared-claims-a-ticket-someone-else-is-on)).
- **Shared zones, committed with the repo.** `.mindflock/zones.toml` at the
  repo root (`[[zone]]` with `kind`, `pattern`, `note`, `author`, `added`) is
  enforced by the agents' guard, the push/PR gate and the Code Map on top of
  the local zones, read from each worktree so it follows the branch; a bad
  file is ignored and logged. The Zones dialog has a **Shared with the repo
  (committed)** section with each zone's author and **Add to repo** on every
  local zone (writes the file into a session's worktree; owner only;
  `/api/instances/{title}/shared-zones`). Agents can't edit the file.
- **Zones warn your own windows.** A session you started (not agent-spawned)
  whose commit would take files inside a red zone or outside a green one gets
  the breach card at Commit with **Commit anyway** (`proceed_anyway`) — never
  a block.

### Changed

- Phone (`/m`): a Sessions sheet replaces the native picker, with the rail's state words and tones; a session actions sheet (row ⋯, long press, or header ⋯) offers Restart (offline only), Pause / Resume session, Close and Delete… (inline confirm, "Folder in use by …" on a 409), plus Recently closed with Reopen. Push/Merge questions and gates are inline sheets (no `confirm()`); the stage resets after a PR opens; "Terminal" replaces "Shell"; the New sheet uses the desktop's button words.

- **Rail: rename, select and filter.** A rename is the session's name
  everywhere (rail, phone page via the new `name` field on
  `GET /api/instances`, notifications) and must be unique: `POST /api/aliases`
  and session create answer 409 when another session already goes by that
  name; click the selected row's name to rename it. Select sessions straight on
  the rail — Ctrl/⌘-click a row, Shift-click a range, or row menu → Select —
  with a strip pinned to the top of the list (All / None, Close, Hide,
  Delete…; Esc or ✕ leaves select mode); the footer **Select…** button is gone.
  The filter box is always shown by default; Customize → Session list →
  **Filter box** hides it (`/` still opens it for one search).

- **One word per thing.** The web UI, phone page, CLI output, MCP tool
  descriptions and docs now say **session** (not instance, or window for a
  session), **Fast-track** (not autopilot, lane or auto-ship), **team run**
  (not group or ship lane), **Commit / Push / Open PR / Merge**, **Close**
  (keeps the worktree) and **Delete** (removes it), and **Needs you** with its
  reason. Assistant, log tail, Verify and extension rows stay **windows**. The
  table is at the top of [docs/web-ui.md](docs/web-ui.md#vocabulary); API
  paths, JSON and settings keys, event names and MCP tool names are unchanged.
- **Every install path lands on the same released version.** `install.sh`
  resolves the newest release tag (GitHub releases API, then the highest
  `vX.Y.Z` tag from `git ls-remote`) — the version the desktop app pins — and
  warns when `MINDFLOCK_INSTALL_REF` names a branch or commit ("installing an
  unreleased build; the desktop app pins releases"), so re-running it can no
  longer put the engine ahead of the app. After a successful install it
  records `~/.mindflock/install.json` (`method`, `ref`, `sha`, `python`, `at`)
  for `mindflock update` to follow the same path, and its next steps point to
  `mindflock init` and the desktop app. The README's uv and pipx commands pin
  the release tag instead of `main`, and CI's cold-install job asserts the
  record names the checkout under test.

### Changed

- **The ticket pipeline follows your settings.** A new or rotated tracker or
  GitHub token, a changed assignee scope, label filter, base URL, email, queue
  or repo — saved in Intake or Settings, or synced from another device — now
  restarts the running pipeline on its own, 2 s after the last change (a form
  that saves on every field restarts once), instead of only after a manual
  off/on. The restart is logged with the changed keys, never values.
- **PR review and issue handling run without a ticket source.** They used to
  need a ticketing source to exist just so the pipeline's config would load.
- **A source missing a required field no longer crashes the pipeline.** A
  Shortcut card without its member id yet is skipped — the status names the
  source and the field — while the other sources and PR review keep running.

### Added

- **`GET /api/mindflock/status` says what is wrong.** `last_error`
  (`auth` — a tracker or GitHub refused the token, naming the source;
  `config` — a skipped source and its field; `network`; `crash`),
  `exit_reason` once the server's pipeline exited (a configuration error
  names the missing fields), `config_errors`, and `env_override` while
  `MINDFLOCK_TICKET_PROVIDER` hides the sources saved in the UI. Auth errors
  were deliberately ignored by the connectivity check before, so a revoked
  token showed no indicator.

### Added

- **Agent providers you can see, sign in to, and point at a binary.**
  Settings → Agent providers shows one row per CLI, built-in or custom:
  installed (with the path it resolved to), signed in (with the evidence —
  or *no sign-in needed* / *sign-in unknown* when that is the honest
  answer), a **Sign in** button that runs the CLI's own login in an in-app
  terminal, the install command when it's missing, and a **Binary path**
  field for every provider that fixes a claude or codex off PATH on this
  device only (never synced). A custom TOML that fails to load is a red row
  with the file and the error, and is logged. Settings → Agent CLI lists
  every provider in the default picker (the absent ones disabled, saying
  why) and **Test agent CLI** can probe any of them. Settings → Accounts
  gets the same **Sign in** button per account, scoped to that account's
  config dir, with the command kept as a copyable fallback.
- **`mindflock doctor` checks the login of every agent you use**, not just
  the default: the Assistant's, each ticket source's, PR review's and issue
  handling's, and per-repo overrides, one row each; `doctor --fix` offers
  each sign-in in turn.

### Changed

- **One GitHub credential path.** The GitHub token has one home: **Connect
  GitHub** — Setup's step, and the same dialog from Settings → Connections →
  GitHub → **Connect** or the **Connect GitHub** button on Intake's Pull
  requests and Issues tabs. The raw, never-validated Token field under Intake
  → Pull requests → Advanced options and the per-source Token on GitHub
  Issues cards are gone, and the two "Test GitHub" buttons (which only said
  where a token *would* come from) are replaced by the dialog's **Test**,
  which really asks GitHub who the token is and whether it reaches your first
  configured repository. Every consumer now resolves the token through one
  chain in one order — Settings → `$GH_TOKEN` / `$GITHUB_TOKEN` → `gh auth
  token` → `[github].token` in `config.toml` — so Setup can no longer say
  "connected as @x" while PR review uses another token. **Deprecated:**
  `[github].token` in `config.toml` used to come first; it is last now, logged
  about once at startup, and ignored from the next release. A GitHub Issues
  source that still carries its own token keeps working this release (read as
  the last fallback, flagged *Action needed* in Settings → Connections) and is
  ignored from the next. The pipeline re-resolves the token on every poll
  (60 s memo) instead of caching it for the life of the process, so a pasted
  token reaches the loops within a minute without a restart. Every "no GitHub
  credential" remedy now says *Settings → Connections → GitHub → Connect*,
  and that row's **Configure** opens the dialog. The device flow's OAuth
  client id ships as a constant (`DEFAULT_GITHUB_OAUTH_CLIENT_ID`, empty
  until the owner registers the app; `mindflock serve --github-client-id` /
  `MINDFLOCK_GITHUB_CLIENT_ID` / `github.oauth_client_id` override it); while
  it is empty the dialog says "Device flow is not configured in this build;
  paste a token or use gh" instead of hiding the option.

### Changed

- **A ticket source is connected only after a live test, and the form cannot
  produce a broken one.** Intake → Tickets source cards now show a three-state
  pill — Connected, Needs attention (with why), Off — derived from the last
  test recorded on the source, not from whether a token happens to be stored;
  Settings → Connections reads the same rule (`connection`, `message`,
  `tested_at` on each ticketing row). Saving a source validates it against the
  provider catalog (required fields, URL / email / key / gid shapes, and
  "Anyone's needs an ingest state") with the errors shown inline; a passing
  save runs Test on its own, and Test's verdict, member id and state list are
  stored on the source (`last_test`), so the state pickers open filled and
  refresh on focus. Secrets get a **Clear** control (an explicit `null` on the
  PUT; blank still means keep). Jira gains a *Project key* field and Linear a
  *Team key* field — the names their adapters already read. Asana gains whose
  tickets, ingest sections, move-to-section on start and a start comment
  (sections are its states), and Test looks the workspace up from the token
  instead of asking for a gid (several workspaces offer a pick).

### Changed

- **Two ways to trust another machine, each control on one screen.** The
  Settings nav gains a **Devices and people** group: **Your devices** (was
  Devices) is now the only place for pairing by code / Ask-Approve, the fleet
  key, **Make reachable** (the one Tailscale-mode switch, with **Restart
  server to apply** when the server doesn't restart by itself), the shared
  phone link (**One link for all devices**, moved from Mobile), **Run here**,
  settings sync, and an **Advanced** fold holding the older routes with one
  line each on when you'd still use them: **Trusted Tailscale accounts**
  (moved from Security), **Allow remote control** (moved from Security) and
  **Pair by pasted token** (the sidebar's Connect… dialog, now an inline
  form; Connect… lands here with that device preselected). **Your phone**
  (was Mobile) is one QR, the `/m` link and the Tailscale steps in order — it
  never hides any more: without Tailscale it shows the install step with the
  doctor's diagnosis instead of a gate — and shows no token; when the server
  is local-only it points at Your devices → Make reachable. **People** (was
  Work with someone) is unchanged and leads with Invite someone / Join a
  peer. **Security** keeps only the access token, the gate and a pointer to
  Your devices. Screen keys did not change (`devices`, `mobile`, `peer`,
  `security`), and the new names plus the moved controls' names
  (`your-devices`, `your-phone`, `people`, `remote-control`,
  `tailnet-trust`, `settings-sync`, …) route to the right screen, so palette
  entries, docs links and deep links keep working. Palette: **Your
  devices**, **Your phone**, **People…**. No route or setting changed.

### Changed

- **One first-run checklist instead of three overlapping surfaces.** The
  welcome tour no longer opens by itself (it is a **Tour** entry in the
  command palette and the **?** shortcuts sheet, and Settings → General's
  Replay), the Setup dialog no longer pops up on a failing doctor probe, and
  the empty grid's "Get set up" card is now a five-step checklist — Engine
  and tools, Sign in to your agent (one button per installed agent CLI, green
  once signed in), Pick a repo, Connect a tracker and GitHub, Your phone —
  with the last two skippable. The phone step is always visible: it offers
  **Install Tailscale** with the doctor's hint until Tailscale is there, then
  the QR. Once the three required steps are done a compact **Setup 3/5** chip
  takes over until every step is done or skipped; it, the sidebar's doctor
  chip, the palette and the desktop app's `?setup=1` open the Setup dialog,
  which renders the same checklist. `mindflock init` prints the same five
  steps in the same state, and the text a first `mindflock serve` prints at
  boot is shown in the checklist too (the desktop app's server writes it to
  a log nobody reads). Steps are marked by the server from the thing itself
  — the doctor run, the first session, the first ticket source, a phone
  reaching `/m` — never by a button; the state is `general.setup`, and the
  old `general.onboarded` is derived from it (migrated on load). New
  `GET /api/setup` and `POST /api/setup/<step>/skip`. The doctor's fix text
  now says "Settings → Agent CLI", matching the screen.
### Fixed

- **Updating your devices skips one that's too old, instead of stopping.**
  A device on 0.7.4 or earlier can't be updated from another device: the
  route arrived in 0.7.5, and an older MindFlock answers HTTP 405 (not 404)
  to it. The rollout now marks that device "skipped — update it there" and
  carries on, where it used to stop with "answered HTTP 405". Other
  device-to-device requests to an older MindFlock now say "update MindFlock
  on … first" instead of the bare 405 too.

- **"Stopped updating your devices" clears once they caught up.** A stopped
  update used to stay on Settings → Devices even after every device it left
  behind was updated some other way (by hand there, say). It now reads as
  done, with those devices "up to date", once each one reports the version.

- **A queued prompt no longer waits five minutes behind a slash command.**
  After each send the queue waited for a "working" reading before sending the
  next item, so a command the CLI handles itself (`/clear`, `/model …`) left
  the queue stuck until the 300 s fallback. It now re-arms as soon as there is
  evidence the send was taken: the agent worked after it
  (`agent_state.last_worked_at`, which a "turn ended" notice does not clear),
  or the CLI's input box no longer holds the sent text (new provider method
  `input_holds`, implemented for Claude Code).

- **Your phone opens the shared link without the access token when the link
  is served from a Mac.** Trusted Tailscale accounts used to skip the token
  through the shared phone link only on Linux hosts; when Tailscale routed the
  phone to a macOS host it asked for the token. macOS hosts now prove the
  proxied request came from Tailscale itself (the connection's owning process,
  read from `netstat -anv`) the same way Linux reads `/proc`.

- **`spawn_session`'s `model` reaches the agent.** A model given without an
  account was recorded on the session but never passed to the CLI, so workers
  ran on the CLI's default. It now reaches Claude (`ANTHROPIC_MODEL`), Codex
  (`-m`), Aider and Opencode (`--model`) and Goose (`GOOSE_MODEL`); for a CLI
  with no known model flag `spawn_session` answers with a warning instead of
  dropping it.

- **A removed account no longer breaks Name → Enter.** The New dialog's
  last-used pre-fill restored the account without checking it still exists,
  so after removing an account every quick create in that folder posted its
  id and got 400 "unknown account". The remembered account is now applied
  (through the same path as a pick) only while the account list has it — or
  it is "CLI's own login" — and forgotten from that folder's memory otherwise.
- **Editing a custom provider no longer rewrites its TOML.** Saving the
  Edit form used to re-render the file from the six fields it knows,
  dropping `prompt_arg`, `oneshot_args`, the effort keys and the `[usage]`,
  `[activity]`, `[connect]` and `[peer]` tables — and settings sync then
  spread the loss to every device. It now patches only the edited keys and
  refuses (409, naming the error) to touch a file that doesn't parse. The
  providers module's docstring names the real default directory
  (`~/.mindflock-assistant/providers/`).

- **`mindflock rm` no longer deletes your work.** It now ends the session the
  way the web UI's Close does (agent stopped, worktree and branch kept, session
  in Recently closed) instead of silently removing the worktree while saying it
  was kept. The old destructive behaviour is `mindflock rm --wipe TITLE`, which
  asks `y/N` on a terminal, needs `--yes` in scripts, and names the sessions
  still using the folder when the server refuses with 409. `rm` also takes
  several titles at once.

### Docs

- **Docs and strings follow the new screens.** Every user-facing "Settings →
  Devices / Mobile / Work with someone" (the CLI's prints, toasts, desktop
  notifications, onboarding lines, README and docs) now says **Your devices**,
  **Your phone** and **People**; trusted Tailscale accounts point at Your
  devices → Advanced and a missing GitHub token at Settings → Connections →
  GitHub → Connect. `docs/web-ui.md` describes the five-step checklist (no
  auto tour, no auto Setup modal), the New-session fast path and one
  Templates list, the regrouped Settings nav and its screens, and Intake's
  source cards. Loose ends wired with it: the sidebar's **Connect…** opens
  Your devices directly (the redirect dialog is gone), Your devices leads
  with the **You** card, Settings → General gains **Revive sessions after a
  restart** (`general.revive_on_start`), the **Setup 3/5** chip moved into
  the sidebar footer beside the doctor chip (which now opens Setup), the
  checklist's leftover Test GitHub became **Connect GitHub**, a GitHub Issues
  source still holding its own token says to move it, and `doctor --fix` /
  `init` give each `agent-auth-<name>` row the same sign-in hint and re-probe
  as the default's.

- **The docs tell the truth about install, update and what exists.** README,
  `electron/README.md` and TROUBLESHOOTING now describe the Windows `.exe` as
  installing only the app (the engine installs on the app's first launch;
  the dead `MINDFLOCK_NO_WSL` / "Show details" advice is gone), say what
  macOS Gatekeeper and the Command Line Tools ask for, mark `git` optional
  per the doctor, and note that updates are one click since 0.7.2. The README
  stops claiming MindFlock never commits, pushes or merges (fast-track, team
  runs and autopilot do, at the depth you choose, never by default), lists
  Code Map + zones, Verify, your devices, settings sync and peer links, and
  counts the MCP's 27 tools (+ 7 peer tools). AGENTS.md and
  `docs/development.md` describe `backend/web/static/` as committed Vite
  output, add `frontend/`, `electron/`, `backend/mcp/` and `backend/peer/`
  to the maps, correct the frontend test facts (83 files / 1615 tests, run
  by CI) and re-verify the known issues (2026-10-10) with cross-references to
  the MCP and ingestion lists. `docs/configuration.md` has the peer relay's
  real default (`auto`) and the no-longer-a-gate `enabled`; `docs/web-ui.md`
  lists every Settings screen and gains Settings sync, Work with someone and
  Intake's *running on <device>* state; `docs/architecture.md` gains
  `backend/peer`, the full addon list and the sync / zones / peer state
  files; `docs/cli.md` documents `init --yes` and `serve --setup`;
  `docs/peer-link.md` is marked shipped (0.7.0 / 0.7.1). A TROUBLESHOOTING
  section for ticket sources, a feature-request issue template, and
  CODEOWNERS no longer names the non-existent `CLA.md`.


### Added

- **One update path everywhere.** The desktop app's **Update** toast, its
  **Update engine** toast and Settings → Advanced now run the same update
  (engine first, then the app if one is downloaded) and watch the same
  progress, so an engine-only update always stops the old server, waits until
  the port is released, starts the new one and reports the version it answers
  with — Settings → Advanced no longer sticks on "Updating…". When the engine
  has run ahead of the app (install.sh re-run by hand), the app says so:
  "Your engine is newer than the app — update the app" on the update toast, or
  a one-line notice when no app release is out; the engine is never downgraded
  to match.
- **`mindflock update` installs the way you installed.** It reads
  `~/.mindflock/install.json` (written by install.sh: method, ref, sha) and
  runs the matching install — the pinned `uv tool install --force` the
  in-app updater uses, or `pipx install --force` — then restarts a running
  server onto it and prints the version it now reports. A development
  checkout gets the exact git commands printed instead of an install.
  `--check` exits 0 when current and 1 when behind; `--ref` takes a tag or a
  commit.
- **pipx installs can update from the browser.** Settings → Advanced
  (`/api/update/check`) classifies a pipx engine as `pipx` (not `other`) and
  updates it; it shows how the engine was installed, and for a development
  checkout prints the git commands instead of a disabled button.

- **Sessions come back after a restart without being looked at.** After the
  server boots (past its 30 s quiet window) it relaunches, in the background,
  every session whose agent was running — two at a time, oldest first, through
  the same path as the Offline chip's Restart — instead of leaving them Offline
  until clicked. Paused, provisioning, peer-shared, workspace-missing and
  usage-limited sessions are skipped; one log line per revive and a summary
  line ("revived N, skipped M: …"). `general.revive_on_start` (default on)
  turns it off; `POST /api/server/revive-all` runs it on demand (owner only).
- **Nothing disappears silently at startup.** A session dropped at load because
  its folder and tmux are both gone is now recorded in Recently closed with
  `reason: "workspace missing"` and whether its branch still exists
  (`branch_exists`), and logged; the dialog shows "Workspace missing — removed
  at startup" and names a surviving branch so the work can be recovered.
- **Desktop first launch never looks hung.** "Starting…" shows the last lines
  of the server's own log after ten seconds, and a server that stops before
  it answers (Linux/macOS) gets its own screen: the log tail, **Try again**
  and **Copy diagnostics** (everything a bug report needs, on the clipboard).
- **Mac setup finishes by itself.** Before installing the engine, the desktop
  app checks for Homebrew (tmux comes from it) and offers **Install Homebrew**
  — it runs Homebrew's official installer in Terminal, which asks for your
  password there, and MindFlock carries on when it's done — or **Skip**. A
  Mac without Apple's Command Line Tools gets Apple's installer opened and
  MindFlock waits for it instead of asking you to press Install again. The
  one-more-step screen also says how to get past Gatekeeper (System Settings →
  Privacy & Security → Open Anyway): the app is self-signed.
- **The Windows installer checks WSL before copying anything.** No WSL, or
  WSL with no Linux distro, now stops the installer with one message
  ("MindFlock needs WSL 2 with a Linux distro. Install it with `wsl --install`,
  reboot, then run this installer again.") and a link to the setup guide,
  instead of installing an app that could only show an error.

### Changed

- After its first-run engine install the desktop app opens the web UI at
  `/?setup=1` (the setup checklist) exactly once; later launches open `/`.
  The install one-liner the app logs and shows is pinned to the app's own
  version in both places (the log line used to point at `main`).

### Changed

- **One way to end a session.** The row menu, the ✕, `Ctrl+W` / `Delete`, the
  command palette and the bulk bar all use the same two verbs: **Close** (stops
  the agent, keeps the worktree; reopen from Recently closed) and **Delete…**
  (removes the worktree and branch, asked first on an inline strip with
  Delete / Cancel). "End" and "Delete + wipe worktree" are gone. Pause's item
  now says what it does: *Pause (stops the agent, keeps the worktree)*.
- **Shortcuts that cannot fire by accident.** `Ctrl+W` and `Delete` no longer
  close the focused session while *any* dialog or overlay is open (detected
  generically — the palette, Settings, Make PR, Setup, To-do, the shortcut
  sheet, the templates modal and extension dialogs were holes in the old
  allow-list), nor while the keyboard is in a text field or a terminal.
- **Delete that cannot proceed says why.** When another live session shares the
  folder (a Duplicate session runs in place), the row shows *Folder in use by
  &lt;names&gt;. Close those first.* instead of a bare failure; when a closed or
  deleted session leaves workers running, the toast adds *N worker sessions
  kept running*.
- **Bulk session actions are reachable again.** **Select…** in the sidebar
  footer opens the checklist bar (it had no entry point since the per-row
  checkboxes left), with the same Close / Delete… verbs and inline
  confirmation instead of a browser `confirm()` that did nothing in the
  desktop app.
- **Command palette** gains *Close session*, *Delete session…* and *Pause
  session* / *Resume session* (whichever applies) for the focused session.
- **Questions no longer look like errors.** Merge this PR?, Delete …?, Clean
  up …? are drawn on a neutral bottom-right card; failures (push, make PR,
  merge, pause, resume, delete, reopen, check run, setup re-run) are all red
  cards that stay until dismissed instead of toasts that vanished mid-read.
- The bell logs a close as **closed**; **deleted** is reserved for a delete.

### Added

- **Journey tests.** `tests/e2e` drives the real UI in headless Chrome
  against a sandboxed server with a stub agent — first-run setup card, New
  session, close + reopen, Settings → Connections, the Ctrl+W guard — and
  asserts on what a user sees, so a UX rough edge cannot come back silently.
  `uv run pytest tests/e2e -q`; a new non-blocking `e2e` CI job runs them
  and uploads screenshots on failure.

### Changed

- **Verify's helper sessions are hidden by a flag, not by their name.** A
  checklist run used to stay off the session rail, the `Ctrl/Alt+1…9`
  numbers and the prompt-target pickers because its title began with
  `verify-`, so any session *you* named that way vanished too, with no row,
  no pane and nothing to say why. The server now records `hidden_by:
  "verify"` on the sessions Verify creates (`POST /api/instances` accepts the
  field; every `GET /api/instances` row carries it) and the UI hides on that
  alone. Name a session `verify-anything` yourself and it is listed, numbered
  and promptable like any other. Runs created before the flag are flagged at
  startup when a checklist still names them, so nothing old lands on the
  rail; a row from an older server, which sends no flag at all, still falls
  back to the name.

### Changed

- **One session state, the same words everywhere.** A session's rail dot, its
  chip, the bell's *Needs attention* list, the toasts and the tab badge now all
  read one state (`starting`, `working`, `needs you`, `idle`, `waiting on
  limit`, `offline`) instead of each deriving its own, so a crashed agent can no
  longer show a green dot beside an "offline" chip. One label per state:
  **Starting** (grey pulse), **Working** (green), **Question** / **Pre-commit
  failed** / **Checks failing** / **Setup failed** (gold — these need you and
  are in the bell), **Idle** (neutral; the ship-stage chip — committed, pushed,
  PR open — shows beside it, and it joins the bell only after 20 minutes idle
  on uncommitted work), **Waiting on limit, resumes HH:MM** (blue — it
  auto-resumes, so it is no longer painted red) and **Offline** (red, in the
  bell). The six tones are CSS tokens, defined once for both themes.
- **Offline has a remedy.** The Offline chip is a **↻ Restart** button
  (`POST /api/instances/{title}/revive`): the same lazy relaunch a terminal
  attach or a sent prompt does, resuming the conversation after a crash.
  Offline and a failed worktree setup now appear in the bell; the toast for an
  agent that went offline offers the restart on click.
- **Notification switches govern every in-app channel.** The *needs input*,
  *pre-commit failed*, *setup failed*, *checks failing* and *offline* toasts and
  the tab-title / favicon badge obey Settings → Notifications like the group
  toasts already did; the bell keeps showing them (it is the source of truth).
  The badge counts every session that needs you, not only questions.

### Changed

- **Pause keeps your work.** Pausing a session now only stops the agent;
  the worktree and branch stay exactly as they are — no automatic commit
  under your name, no removed checkout. Resume relaunches the agent in the
  same folder. Sessions paused before this change (checkout removed) still
  resume the old way.
- **Delete and Delete workspace refuse a folder another session is using.**
  Ending a session with its worktree removed (`DELETE /api/instances/{title}`,
  `/cleanup`, `/api/workspaces/delete`) answers **409** `folder in use` with
  the other sessions' titles — and changes nothing — while a copy made by
  Duplicate session (or any other live session) works in the same folder.
- **Every ended session lands in Recently closed**, including deleted ones
  (Delete used to skip the undo store).
- **Ending a session no longer cuts its child sessions loose.** They keep
  running under the ended session's own parent (or as roots), and the
  DELETE / `/close` / `/cleanup` responses list them as `children` so the
  UI can say so. One function, `backend/web/core/session_end.py`, is now
  behind all four end-of-session routes.

### Changed

- **New session: the fast path costs nothing again.** The dialog opens on
  the form with Name focused, pre-filled with the template, agent and account
  the last session in that folder was started with (remembered per folder,
  per browser), so Name → Enter creates the session with no model turn. The
  one-sentence box is now a secondary **Describe it instead…** row on the
  form: it expands the box, Enter there runs the model turn as before with a
  visible "Thinking…" state and a Cancel, and the fields it fills are the
  ones you are looking at. The old three-button first page survives only for
  Intake's "Start together…" batch.
- **Base branch picker.** The Git & workspace fold (now shut by default,
  with where the work happens and the base branch on its summary line) has a
  **Base branch** menu for a new worktree — local branches first, then
  remote-tracking ones, from the new `GET /api/repos/branches?path=` — sent
  as `base_ref` + `base_branch` on create. Leaving it on the first row keeps
  the server's own choice.
- **One Templates list.** Saved prompts and session templates were two lists
  with near-duplicate starters; they are one now: the server-side templates
  (`/api/templates`, synced between devices as before). A browser's old
  per-browser saved prompts are migrated into it once, deduped by name and
  body. The vanilla-JS templates modal is gone, replaced by a React Templates
  dialog (New → Templates → **Manage…**, always shown now, or the palette's
  **Templates…**) that lists, edits inline and deletes behind an inline
  confirm row — the old modal asked `window.confirm`, which does nothing in
  the desktop app, and never checked whether the delete happened.

### Fixed

- **`mindflock rm` no longer deletes your work.** It now ends the session the
  way the web UI's Close does (agent stopped, worktree and branch kept, session
  in Recently closed) instead of silently removing the worktree while saying it
  was kept. The old destructive behaviour is `mindflock rm --wipe TITLE`, which
  asks `y/N` on a terminal, needs `--yes` in scripts, and names the sessions
  still using the folder when the server refuses with 409. `rm` also takes
  several titles at once.
- **Ticket sources and Templates: review leftovers.** A recorded Test whose
  request overrides a tested field (a different token, state…) answers but no
  longer stamps its verdict onto the stored source ("— save first to record
  it"); a save that sends a Test-filled field blank (a blur racing the post-save
  Test) keeps the stored member id / Asana workspace and its verdict; a new
  "Add source" card still missing a required field stays local with its inline
  errors instead of failing every other card's save; a Shortcut/Asana source
  missing only its Test-filled field is pinned as skipped, not fatal, by the
  pipeline loader; and Templates shows a failed load at list level instead of
  "Loading templates…" forever.

## [0.7.5] - 2026-10-09

### Added

- **Tailscale on this device.** Settings → Devices opens with a card that says
  who this computer is signed in to Tailscale as, on which tailnet, and what's
  wrong if anything is: signed out (a **Sign in** button shows the sign-in link
  and a QR), MagicDNS or HTTPS off, or a device key about to expire (tagged
  devices included, with how to turn expiry off). MindFlock now finds the
  Tailscale app on macOS (the App Store and standalone builds keep their
  command inside the app) and recognises a Windows-side Tailscale under WSL,
  with steps for the setup that works there, instead of saying "install
  Tailscale" when it's already installed.

- **Joining makes a computer reachable.** A computer that only listens on
  itself can't be reached by your other devices, so Devices now says so and
  offers **Make reachable** (Tailscale mode with the access gate on, in one
  step), refuses to make a code nobody could use, and tells the device that
  let a new one in when it can't reach it back. When no other MindFlock shows
  up, each device on your tailnet gets a reason (not running there, only
  listening on itself, a Tailscale access rule blocking port 8765, asleep since
  …) with the fix next to it. **Match my other devices** copies the phone-link
  setup after a join.

- **Approve a new device from wherever you are.** A join request shows on every
  one of your devices, in the bell, on the phone page and, in the desktop app
  while it's in the background, as a system notification. The 6-digit code is
  still compared. **Paste a code** (Devices, the command palette, `mindflock
  join CODE`) takes either kind of code: one for your own devices or one from
  someone you work with.

- **Update all my devices.** Settings → Devices (and `mindflock devices
  update`) updates your other devices one at a time to the newest release,
  waits for each to come back on it, stops at the first that doesn't, and
  skips (with the reason) any it can't update, such as a development checkout.
  The bell, the browser and the phone page say when a release is out and which
  of your devices are behind. Also new: `mindflock update` and `mindflock
  restart`.

- **A new computer in one line.** **Add a device** also shows a single command
  for a computer with nothing installed yet. It installs your version of
  MindFlock, signs in to Tailscale, makes the computer reachable and asks to
  join (`mindflock devices bootstrap`, `install.sh --join`). The desktop app
  gets a "download, then paste this code" version. Devices also shows each
  device's readiness (version, tools, agent sign-in, push access, Tailscale key
  expiry), and offers **Install** when a device lacks the agent your devices
  share.

- **First run is one flow.** Setup follows one plan, also shown by `mindflock
  init`: tools → "first computer, or join one you already have?" → agent
  sign-in → Tailscale (only when you use more than one computer) → GitHub →
  first repo. **Sign in to <agent>** is back, in Setup and on the doctor's
  sign-in row. **Connect GitHub** signs in through GitHub's device flow when a
  client id is configured (`github.oauth_client_id`), else through `gh auth
  login --web`, else through a pre-filled token page. Setup also fills in git's
  name and email and checks that you can push. A push that fails for want of
  credentials now says so and how to fix it, instead of hanging on a password
  prompt.

- **Work with someone (peer links).** The inviter's screen updates as it
  happens ("B joined — read them your safety number", a countdown, Cancel), and
  the bell says when someone joins or leaves. A shared session is marked
  *Shared with <name>* in the sidebar. Pairing again with the same person
  reconnects the existing link and keeps its shared folder. You choose how
  they'll reach you before the first invite, and a failed tunnel falls back to
  a direct invite, labelled as such. Messages that arrive while no folder is
  shared wait in the link's inbox, and you can read and send messages and see
  their changes from Settings, so someone without a sandbox (a Mac) can take
  part.

### Changed

- **Updates finish themselves.** An engine update now restarts the server on
  its own (no open browser tab needed), keeps the mode it was started in,
  restarts the ticket pipeline onto the new build, and rolls back to the
  previous version if the new one never comes up. `install.sh` installs the
  newest release by default (`MINDFLOCK_INSTALL_REF=main` still works) and
  restarts a server that's already running.

- **Dependency installs actually install.** Agent CLIs install with their
  vendors' own installers (never pip or `sudo npm`). The install and sign-in
  terminals work without tmux. On a Mac without Homebrew, Homebrew is installed
  first. A first run waits for the Xcode command-line tools instead of failing.
  The desktop app goes straight from installing MindFlock into installing what
  it needs. The doctor shows each agent CLI's version.

- **Tailscale mode listens on Tailscale only.** It binds this machine plus its
  Tailscale addresses, not every network it's on (`MINDFLOCK_BIND_ALL=1` keeps
  the old behaviour), and answers only to its own names.

- **The suggested Tailscale access policy** is one block, safe to merge,
  covering device-to-device and the shared phone link. It names your own login
  instead of every user on the tailnet.

### Security

- On a computer whose access gate is off but which is reachable from the
  tailnet, a caller that isn't you can no longer:
  - change settings that reach your other devices (agent launch arguments,
    custom agents, templates, ticket sources, sync, peer links, red zones);
  - read this device's access token;
  - rotate the token;
  - start an update or choose its version (only a published release, never a
    downgrade or a branch);
  - drive your other devices through this one.
- A web page that re-resolves its name to 127.0.0.1 no longer counts as "this
  computer".
- `/api/github/push-check` is a POST and only runs in folders MindFlock
  already knows.

## [0.7.4] - 2026-10-08

### Added

- **One ticket, one device.** With several machines paired for remote
  control, a ticket another of your devices is already working (or starting,
  or holding in a team run) is no longer started again: Intake shows it as
  *running on <device>*, **Begin work** refuses, ticket ingestion backs off,
  team runs leave it out and the agents' `list_tickets` skips it. When two
  devices reach the same ticket at the same moment, the one that marked it
  first keeps it. A device that's off or unreachable never blocks the queue.

- **Your devices.** Settings → Devices (and `mindflock devices`) joins the
  computers you own into one group: type a code made on one into the other,
  ask from the new one and approve on the old one (both show the same 6-digit
  code), or add a computer you already pasted an access token for in one
  click. Your devices share one key, so they reach each other with no pasted
  tokens, and a phone signed in through the shared phone link reaches all of
  them (a QR from one device signs the phone in on that device). PR review and
  issue handling run on exactly one of your devices, a choice your devices
  share (`github.automation_device`): at first the one that was already
  running them when you joined. Choose **Run here** in Settings → Devices to
  move them, and Intake says where they run. When they move, the new device
  skips the PRs and issues open at that moment, which the old one already
  handled. Removing the device that runs them moves them to the one you
  removed it from; a device that leaves hands them on first. A device that
  starts PR review or issue handling for the first time while grouped with
  your others also skips what's open at that moment, once. Removing a device
  gives the rest a new key (one that's offline gets it when it's back) and, by
  default, a new access token on every device, so phones scan the QR again. If
  the device was lost or stolen, also remove it from your tailnet in the
  Tailscale admin console: that cuts it off everywhere at once, even from
  devices that are offline now. Settings → Devices shows which device removed
  which, so a removal you didn't make stands out; a device that left on its
  own is shown plainly and can join again from any device. A removed device
  that another of your devices lets back in stays out until you choose **Allow
  it here**. Security → Rotate token now also changes your devices' key and
  every device's own access token, so a lost phone is signed out of all of
  them; it names any device it couldn't reach, to rotate there. Your devices
  are only as locked as the least locked one: a device with the access gate
  off lets anyone who can reach it control your other devices through it, and
  Settings → Devices warns you when that's the case.

- **Settings sync across your devices.** Settings → Devices → **Settings
  sync** keeps ticket sources, GitHub repos and options, notifications, agent
  limits, budgets, trusted Tailscale accounts, the accent, your keymap, prompt
  presets, theme and other UI preferences, session templates, red zones and
  your own agent CLIs the same on every one of your devices. Change one
  anywhere and the others follow within seconds. The latest change wins,
  separately for each ticket source or preset, and deletes travel too. It
  only ever talks to your devices: another MindFlock on the same tailnet is
  never synced from. A device that joins starts from the device it joined.
  Pick **Keep different on this device** to hold one setting back. An agent
  that isn't installed on a machine waits there until it is, and a ticket
  source needs an id to sync. Paths, ports, the access token, the IDE and
  signed-in accounts stay per device. Sync protects what you have: a
  settings.json that won't open is never saved over (saves answer 409 and
  `mindflock accounts` says what to do), and a device whose settings suddenly
  look reset outside MindFlock pauses sync, tells you so (a toast and a bell
  row), and asks whether to take your other devices' settings back or keep
  its own; saving one setting afterwards doesn't wave the reset through.
  Nothing waits for a clock: a later edit wins even against a device whose
  clock runs fast, and only a clock more than a year off is ignored (Settings
  → Devices names that device). A device that ran a development build of
  settings sync from before this release drops its old sync state; if it is
  already grouped with your other devices it turns sync back on at once,
  taking their values where they have one, and its unset fields never clear
  theirs.

### Fixed

- **Copying from a Claude Code pane gives you the text, not its layout.** Drag-to-copy in a Claude Code agent window used to bring along the `●`/`⏺` bullet, the two-space gutter on every row and a line break wherever Claude Code wrapped a long line, so one sentence pasted as several indented lines. A row is now rejoined with the next one exactly when the next one's first word couldn't have fit on it, the gutter is dropped, and real line breaks are kept: short lines, list items, tables, code, diffs, tool output, a line Claude Code cut short with `…`. This applies to drag-to-copy, Cmd/Ctrl+C, right-click and Ctrl+Shift+C, and to selections and Copy all in the scrollback view. Shell tabs and other agents copy as before.

## [0.7.3] - 2026-10-08

### Added

- **Trusted Tailscale accounts: your own devices skip the access token.**
  Settings → Security lists the Tailscale accounts that own untagged devices on
  your tailnet; tick yours and your phone and laptops open MindFlock with no
  sign-in, checked per request with `tailscale whois`. Tagged devices, devices
  shared in from other accounts and requests relayed by another MindFlock never
  qualify. Through the shared phone link (`tailscale serve`) it works on Linux
  hosts, where the server confirms the connection really came from tailscaled;
  elsewhere those requests still ask for the token. Off until you tick an
  account. Regenerating the token does not sign trusted devices out — untick
  the account instead.

## [0.7.2] - 2026-10-08

### Changed

- **One-click update of the desktop app and its engine.** The app now downloads
  a new release in the background, and the **Update** toast installs it and
  restarts — updating the engine to the same release first (release installs
  only; a developer checkout is left alone). No more opening GitHub and
  re-running the installer. On macOS you approve the unverified developer once
  at first install; updates after that just work (same self-signed identity).
  Releases now carry `latest*.yml`, blockmaps and a macOS `.zip` for it.

## [0.7.1] - 2026-10-07

### Changed

- **Peer shared sessions run any agent CLI that can take the peer tools** —
  claude, codex, opencode, cline, goose and antigravity (agy), each verified
  starting inside the sandbox with the peer MCP attached. aider can't (it has
  no MCP client). What a CLI needs inside — API hosts, login files, config
  dir, how the MCP attaches — is now declared by its provider (or a TOML
  `[peer]` section for your own CLI) and validated by the sandbox, instead of
  a claude/codex table in the sandbox. The share form lists the installed ones,
  your default first.

- **Peer links: send a code, they paste it, you're connected.** Settings →
  Peer links now leads with **Invite someone** and **Join a peer**; every
  setting moved under *Advanced*. Inviting or joining turns peer links on (no
  toggle first). **Copy invite** copies a ready-to-send message, and Join
  accepts that whole message or just the code. The relay defaults to the new
  `auto`: invites go through the Cloudflare relay whenever `cloudflared` is
  installed, so the other person can be on any network, and the screen offers
  to install cloudflared/bubblewrap in one click when they're missing. Also in
  the command palette as *Peer links…*.

- **Install every missing dependency in one go.** The setup checklist (Setup,
  Settings → Doctor) has an **Install everything missing** button that runs one
  script in a terminal — one package-manager run for git/tmux/bubblewrap/…, one
  sudo prompt, then each tool's own installer — and re-checks when it finishes.
  `mindflock doctor --fix` (and the installer) asks once instead of once per
  tool, then offers the logins. Your own agent CLI gets installed, whichever it
  is (codex, aider, opencode, …) — not just claude; a separate Assistant CLI is
  checked too. The doctor now checks **bubblewrap** (the peer sandbox) and can
  install **cloudflared**, each only when peer links / the Cloudflare relay are
  on.

### Fixed

- **Starting a session on a Mac from the phone no longer hangs.** The plan's
  folder search looked inside `~/Desktop`/`~/Documents`/`~/Downloads`, and on a
  Mac that blocks until someone answers a privacy dialog on that Mac's screen —
  so "Reading…" never finished when the Mac was across the room. Automatic
  folder searches now skip macOS's privacy-protected folders (browsing to one
  still works), the plan's folder search gives up after 8 seconds instead of
  waiting forever, a plan forwarded to another device is no longer cut off
  before that device's own fallback answers, and a slow read offers **Pick the
  folder myself** on the phone.

## [0.7.0] - 2026-10-07

### Added

- **Mobile: choose which device a new session runs on.** When another
  MindFlock device on your tailnet is connected, the phone's New Session sheet
  shows a **Run on** picker. The plan, the folder list and the create are all
  answered by the chosen device, so the session starts there.
- **Peer links: pair-code with another MindFlock user.** Pair two instances
  with a one-time code (verify the safety number), then each side shares ONE
  folder: an agent runs in a shallow clone of it inside a bubblewrap sandbox
  and talks to the other person's agent through peer-only MCP tools
  (`peer_send`, `peer_inbox`, `peer_get_diff`, `peer_read_file`, …). Settings →
  Peer links, `mindflock peer …`, `/api/peer*`. Shared sessions refuse
  ship / push / PR / autopilot, spawning, the shell pane, the IDE, setup and
  check scripts, rename and team runs; no other session may run in the peer
  folder; export brings the work into your own repo as a `peer/…` branch.
  Linux + bubblewrap, `claude` or `codex`. See `docs/peer-link.md`.
- **One phone link for all your devices.** Settings → Mobile → **One link
  for all devices** gives the phone a URL that names no machine
  (`https://mindflock.<tailnet>.ts.net/m`). It opens on whichever of your
  machines is running MindFlock, so the link keeps working when one of them is
  off. Each device that turns it on advertises the same Tailscale Service. One
  QR scan signs the phone in on every paired device, because the QR carries
  their tokens and the browser keeps one cookie per token. It needs a one-time
  Tailscale setup (tagged devices, the service defined, an auto-approve rule),
  and the screen walks through it.
- **Peer links across networks.** Two MindFlock users with no shared network
  or tailnet can now pair. With `peer.relay = "cloudflare"` (off by default),
  the inviter runs a local `cloudflared` quick tunnel and the invite becomes
  an `mfp2:` code. The joiner needs nothing extra. The same pinned-key TLS 1.3
  handshake runs inside the WebSocket, so the relay can drop traffic but never
  read or alter it. `peer.relay = "url"` uses any WebSocket-capable HTTPS
  proxy instead. `mindflock peer address` changes a link's address. See
  `docs/peer-link.md`, "Connecting across networks".
- **Setup checklist for the shared phone link.** Settings → Mobile → One link
  for all devices walks through the Tailscale setup as six steps, each with
  its live state and a one-line reason. A **Re-check** button re-applies the
  serve config and re-advertises an unapproved host. The access token is
  masked, with Show and Copy buttons.

### Fixed

- **Workers stay nested under their orchestrator on a remote device's
  sessions.** Viewing another machine's sessions through remote control, its
  workers showed as separate top-level rows. A remote row's `parent` is now
  namespaced like its title (`<device>::<title>`), and a new worker is
  slotted under its parent in that device's section.
- **The shared phone link's status is accurate.** It showed approved while
  the advertisement was still pending, so the screen was green while the
  phone timed out. A signal this device can't read now shows as unknown.
  Applying the link is idempotent, and it re-serves every 60s when the live
  config has drifted.
- **Settings → Mobile layout.** The switch row's label no longer overlaps the
  description above it, and the QR, token and snippets fit a narrow pane.

## [0.6.1] - 2026-10-06

### Fixed

- **The desktop app offers engine updates when Tailscale mode is on.** Its
  update check asked the server for the engine version through an endpoint
  the access-token gate refuses, so with Tailscale mode on it never learned
  the version and Settings → Advanced said "The engine is up to date" on an
  outdated engine. It now asks the public identity endpoint, so the
  one-click **Update engine** offer appears, and a check that genuinely could
  not run says so instead of claiming the engine is current.

### Changed

- **Larger top-bar labels.** New, Intake, Verify and Settings read at 13px,
  the wordmark's size, instead of 11px.

## [0.6.0] - 2026-10-06

### Added

- **`mindflock token`** prints this machine's access token, the one another
  device's Connect dialog or the sign-in page asks for. It reads the settings
  store directly, so it works with no server running.

- **Settings → Agent orchestration.** The MindFlock MCP switch and scope moved
  out of General into their own screen under Agents, with the spawn limits
  beside them: sub-sessions per orchestrator, agent-spawned sessions in total,
  and nesting depth (were env vars only). A change applies to the next spawn;
  an env var on the server still overrides, and the row says so. An agent that
  hits a limit is told where to raise it.
- **Finished workers stay on the orchestrator's Thread.** A worker that is
  closed or deleted is listed under **Finished**, with its final report, how
  far its branch got, its diff size and its PR, and its report stays in the
  log. A progress bar over every worker, live and finished, reads **3 of 5
  done**, and each live worker shows its stage (agent, committed, pushed, PR).
- **Fold an orchestrator's sub-sessions out of the sidebar.** A **hide** at the
  end of its roll-up line tucks every session below it away (**show 3** brings
  them back). The roll-up still says what needs you, folded rows leave the
  Alt+N numbering, a search still finds them, and the focused session is never
  hidden.

- **Work on several things at once: team runs.** Give MindFlock a few ticket
  IDs and task lines — from your own Claude with the new `start_team_run`
  MCP tool, or `POST /api/runs` — and it starts one session per item, keeps
  at most N running with the rest queued, fast-tracks each one as far as you
  said (off, commit, push, a PR each, or merge when green), and surfaces only
  what needs you. The server does the plumbing deterministically: queueing,
  create retries with backoff, a fixed nudge for an agent that went quiet
  (twice, only after its work was corroborated), the failed hook handed back
  to the agent before anything escalates, a budget pause, holding new starts
  while a CLI is out of usage, and restart safety (it never adopts a stranger
  that reused a title and never double-spawns when the branch is still
  there). Tickets that are queued are reserved in the ingestion ledger so the
  pipeline never picks them up too. `get_run`, `list_runs`, `wait_for_run`
  and `control_run` (pause, resume, cancel, retry, start now, skip) round it
  out; the sessions belong to MindFlock, not to the agent that asked. See
  [docs/team-runs.md](docs/team-runs.md).

- **Split any session — and choose where the pieces run.** *Split into
  parallel pieces…* now works on a session that works directly in its folder
  or sits on `main`: it plans the split, and the plan card asks where the
  pieces run. **In separate worktrees (merge back)** stays the default — for
  such a session MindFlock starts a new lead (`<session>-split`, a fresh
  branch from its last commit) and the original is never merged into,
  switched or pushed (uncommitted changes there are not in the split, and it
  says so). **In this folder (no merge)** runs every piece as an extra agent
  in the lead's own folder, each fenced to its own paths (per session: the
  guard hook resolves which agent fired it), and MindFlock commits each
  piece's paths itself when it is done — one commit per piece, nothing else
  in it, never a commit hook in the middle of the others' edits. A piece
  that commits by itself, or a change no piece owns, is caught and said; a
  folder on `main` offers **Start a branch here first**. API:
  `POST /api/runs/{id}/plan/approve {mode}`, `POST /api/runs/{id}/lead/branch`.
  See [docs/team-runs.md](docs/team-runs.md#splitting-one-task).

- **Fast-track is one control under one name.** The ⏩ button in each pane
  head now says where the session is going (**⏩ Commit**, **⏩ PR**,
  **⏩ Merge**, with a small **?** when it asks first; just **⏩** while
  Off) and a click opens a picker: Off, Commit, Push, Open a PR, Merge when
  green, and **Ask me before it ships**, which parks the session one step
  short of its first outward step until you approve it in the bell. The same
  choices appear in the New dialog (**Fast-track to** / **Fast-track each to**) and
  the Commit dialog (**Then fast-track to**); `Ctrl+K F`, the row's › menu
  and the palette open the ⏩ picker. **Split into parallel pieces…** and
  **Move out of a group** are row › menu actions of their own. The rail's
  status line leads with the target: `→ PR · working 12m`, `⇡ opening PR`,
  `✓ PR #318 · checks ✓`. Over the API a fast-track target is a **lane**:
  `POST /api/instances/{title}/lane` (`leave` is Off), approval with
  `POST …/ship-now`; every row carries `lane` and `run`, a copy window shows
  the target of the window that drives its branch, and `/api/config` reports
  the default as `fasttrack_default`.

- **Off unless you pick.** A single new session starts with fast-track Off
  whatever Settings says. Batches and ticket runs (a list in New, Intake's
  Start together, a team run started over MCP without a lane) start at
  Settings → Workspace "Fast-track goes as far as" — which is now **Off**
  when unset instead of Open a PR. A value you stored keeps working as
  before. Ticket sources keep their own depth and never fall back to it.

- **`GET /api/outbox`**: what is waiting on you (a prompt, an approval, a
  group's escalation with its next action), what is shipping, what shipped
  today and what is queued — for every session, one row per branch.
  New events `run.changed`, `run.needs_you`, `run.task_shipped`,
  `run.finished`, and notification rules "A group needs you" and "A group
  finishes" (on) and "A group member ships" (off).

- **Agents can talk to each other, and an orchestrator can run a team of
  workers.** Every Claude Code and Codex session MindFlock starts now gets
  the MindFlock MCP server, which gives the agent 23 tools over the rest of
  the flock. An agent can list the other sessions, read a session's last
  reply or screen, look at its diff, and message it. An orchestrator can
  spawn worker sessions that fork from its own commit, wait for their
  reports, answer a permission prompt one of them is stuck on, merge their
  branches, and close or delete them when they are done. Workers finish by
  reporting back (done, blocked or failed, with a summary), and the report is
  typed into the orchestrator's terminal when it is idle. Workers commit on
  their own branch and don't merge, push or open a PR unless told to.
  It is on by default. Turn it off, or limit how far agents may steer each
  other, in Settings → General (`general.agent_mcp`,
  `general.agent_mcp_scope`); `MINDFLOCK_AGENT_MCP=0` is a server-wide kill
  switch. The attach happens per launch (Claude's `--mcp-config` plus an
  allow-list for the read, wait and report tools, one `-c` override for
  Codex) and never edits your own CLI config. Messaging, spawning, answering
  and killing still ask for permission unless the session skips
  permissions. See [docs/mcp.md](docs/mcp.md).

- **Agents can ship work, and start tickets as workers.** Four more MindFlock
  MCP tools. `ship_session` commits, pushes, opens a PR or merges a session's
  work right now, through the same Commit / Push / Make PR / Merge buttons you
  use: the commit message is written from the diff, the PR body is the
  worker's own report (what changed, the tests it ran, open issues), and the
  result carries the PR link. Finished steps are skipped, so calling it again
  picks up where it stopped, and a blocked pre-commit hook or a refused push
  comes back with the shell's own output. It won't ship a session whose agent
  is still working; `set_autopilot` arms the fast-track autopilot for that
  instead, so the session ships itself when its turn ends. `list_tickets`
  shows the Intake ticket list, and `spawn_ticket_session` starts a ticket
  exactly as **Begin work** does, as the agent's own worker that reports back.
  Merging always needs an explicit `confirm_merge`, a session can't merge its
  own PR under the default scope, and none of the shipping tools are
  pre-approved. See [docs/mcp.md](docs/mcp.md#list_tickets).

- **Make PR can take a title and body**, and **`GET
  /api/instances/{title}/ship-status`** reports whether a commit or push
  typed into a session's shell has finished (and why it failed). **Ticket
  starts can name a parent session** (`parent`, `spawned`, `report_back`,
  `note` on `POST /api/tickets/start`), with the same spawn limits as any
  other agent-spawned session. See [docs/web-api.md](docs/web-api.md).

- **Messages between sessions arrive once, at the right moment.** A message is
  typed into the recipient only when it is stably idle and its agent still
  holds the terminal. It is never typed into a permission prompt, a
  usage-limit screen or a shell the agent quit to, never over your own typing,
  it never wakes a stopped agent, and it waits behind your own queued prompts.
  If the recipient reads its inbox first, the message is never typed at all.
  Every message says it came from another agent and not from you. Reply-chain
  and rate limits hold a runaway back-and-forth in the inbox instead of
  letting two agents keep each other busy; a worker's first report always
  gets through. A `session.message` event fires for hooks and extensions.

- **See the team in the app.** A spawned or adopted session shows a muted
  `↳ <parent>` line under its name in the sidebar. Each message gets a short
  toast (one per sender per 30 s), and each worker report a toast that says
  done, blocked or failed. The bell keeps the reports and leaves the chatter
  out.

- **Sessions know who spawned them.** A session can have a parent: the
  orchestrator that spawned it, or one that adopted it later. Each row in
  `GET /api/instances` carries `parent`, `spawned` and `created_at`. Spawning
  and adopting have limits on children per parent and depth, and spawning on
  the total number of agent-spawned sessions (`MINDFLOCK_MAX_CHILDREN`,
  `MINDFLOCK_MAX_SPAWN_DEPTH`, `MINDFLOCK_MAX_SPAWNED`). When a parent goes
  away, its children become ordinary top-level sessions. Only sessions an
  agent created can ever be deleted by an agent, and then only once their
  work is merged (or with an explicit force).

- **`mindflock msg`, `mindflock inbox` and `mindflock mcp`.** Message a
  session's agent from your terminal, read a session's inbox without taking
  anything from it, or run the MCP server for your own Claude Code, Codex or
  other client (`mindflock mcp --print-config` prints the line to register
  it).

- **New API routes and fields for agent-to-agent work.**
  - `/api/instances/{title}/messages`, with long-poll.
  - `/output`: a session's last reply, transcript or screen.
  - `/answer`: answers a dialog the agent is blocked on.
  - `/parent`: re-parents a session.
  - `GET /api/create_failures`: why a session's background start failed.
  - `POST /api/instances` gains `parent`, `spawned`, `base_ref` and
    `base_branch` (cut a new worktree from any commit), and
    `extra_launch_args` (flags added to the defaults rather than replacing
    them). Its 202 body says how the initial prompt is delivered
    (`prompt_delivery`).

  See [docs/web-api.md](docs/web-api.md).

### Changed

- **A calmer first run, one place for what needs you.** The surface shows
  the daily loop and keeps everything else one click away, each feature
  under one name and in one home.
  - **The bell is the one list of what waits on you.** Its *Needs attention*
    list now holds what the Outbox's *Waiting on you* held: ask-first
    approvals with the commit message (editable) and PR title, a group's
    stuck lines (Retry / Retry fresh / Skip), budget pauses (Raise / Stop),
    and a group's plan and release with **Open the Thread ↗**. Every agent
    question gets its answer buttons there, one badge counts it all, and
    the rail's *— open the bell* lines open it on that row.
  - **The Outbox is gone as a place.** What waited on you is in the bell; a
    finished group's summary is **Copy summary** in its rail header's ⋯
    menu; a group's queued lines (Start now / Remove) are in that same
    menu. A split or one-for-all group has no header, so both live on its
    lead's Thread tab. The *Shipped today* log is dropped (a session's PR
    and checks stay on its row). `Alt+O` and the palette's Outbox entry are
    gone, and a rebind saved for `Alt+O` is dropped on load. A group's row in
    the bell, or its *finished* toast, scrolls to the group's header and
    pulses it, or opens the lead's Thread for a group with no header.
  - **Customize chooses which bars the sidebar shows**, and says when each
    one has something to show. The footer's **⚙ Customize** opens it.
  - **Saved prompts are a one-row sidebar bar**, switched on in Customize
    like the Assistant: open **Paste ▾** and pick a prompt to paste it into
    the session you are in, with the keyboard moved there; nothing is sent
    until you press Enter. The prompts live in that menu rather than under
    the bar, so it stays one row however many you save. (Pasting into
    another session, or all running ones, is the Prompts dialog's.) The menu's **Manage prompts…** opens the Prompts dialog to add,
    read or delete them (the templates manager's send row is gone: a template only
    starts a session).
  - **A shorter top bar**: New, Intake, Verify (always, with its due
    badge) and Settings, plus a magnifier for the command palette. Recently
    closed is a *Recently closed (n)* link under the session list.
  - **The sidebar bars carry the Intake tabs' names** (Tickets, Pull
    requests, Issues), and each label opens its tab. The first run has no
    welcome hint.
  - **A pane is named like its row**, and its header is quieter: the next
    step is outlined until it is your move, ⏩ is just the glyph while Off,
    **−** hides the window (✕ always ends or closes), Copy all moved into
    History, the `clarify` chip reads *question*, and narrow panes compact
    so a 2×2 grid fits on a laptop.
  - **One welcome at a time**: a five-slide tour over the grid's *Get set
    up* card, with GitHub and Shortcut tests folded as optional. The Setup
    modal opens by itself only once sessions exist.
  - **New opens on one box.** Fast-track and Split fold under *Options ·
    Fast-track: Off*, the Ticket tab appears once a tracker is connected,
    and *Saved prompt…* saves inline.
  - **Intake and Verify open on the work**, with sources below it. Intake's
    pickers use the one Fast-track ladder (*Agent only* is gone; a stored
    `agent` reads Off), say *Default (…)*, and fold behind a row's
    **Options**.
  - **The command palette lists commands first**, then a *Focus:* row per
    session named like its rail row. It gains *Customize…*, *Prompts…* and
    *Assistant agent file…* (the Assistant bar's **Agent** and the chat
    window's **Agent file** open the same editor), and says
    *Make PR* and *Merge PR*.
  - **Settings' nav is grouped** under Agents, Code, This device and
    Troubleshooting. Every screen key and label is unchanged.
  - **Confirms that did nothing in the desktop app now work**: Merge PR,
    Push anyway, Clean up, Delete + wipe, Recently closed's bulk actions, and
    Security's and Accounts' confirms are in-app, and failures that used to
    `alert()` show in a bottom-right card.

  The API is unchanged: `GET /api/outbox` still serves every group; the UI
  reads only `waiting` (the bell) and `summaries` (Copy summary; an older
  group's comes from `GET /api/runs/{id}`). See
  [docs/web-ui.md](docs/web-ui.md#customize).

- **Launch flags are merged as flag/value pairs.** When per-session flags
  meet the defaults (an intake effort level, an agent's added flags), a pair
  that repeats exactly appears once, and a flag set twice with different
  values keeps both pairs, so the last one wins. Before, `--model sonnet`
  plus `--model opus` became `--model sonnet opus`.

- **A new session that would collide with a closed session's branch is
  refused at once.** A create cut from a commit (`base_ref`) whose branch
  already exists, or a provisioned create whose branch or clone a closed
  session still keeps, answers 409 "already exists" instead of starting and
  failing in the background. A provisioned clone is no longer silently
  reused, old commits and all.

- **Multi-line prompts arrive as one turn.** Prompts typed into an agent
  (the queue, Send now) with more than one line go in as a single bracketed
  paste, so aider and other CLIs that submit on every newline no longer run
  each line as its own turn.

### Fixed

- **Turning on Tailscale mode no longer locks the desktop app out of its own
  server.** The access-token gate answered the app's window with the sign-in
  page, and the token it asked for lived in Settings, behind that page; a
  restart did not help. When the window lands on the sign-in page, the app now
  reads the token where the server runs (inside WSL on Windows) and signs
  itself in once; the cookie lasts a year. Connections that merely come from
  this machine are still not trusted, since `tailscale serve` and SSH tunnels
  arrive from 127.0.0.1 too.
- **The Connect to device dialog is a proper card.** It rendered unstyled over
  the page with a clipped placeholder. The sign-in page and the dialog now say
  where the token is (Settings → Security, or `mindflock token`) instead of
  pointing at a startup banner the desktop app never shows.

- **A deleted session no longer hands its fast-track run to the next session
  with the same name.** A run armed less than 30 minutes before the delete
  survived it, and a recreated `untitled-2` (or the same ticket started again)
  inherited its target and halt reason. Every way a session leaves — delete,
  close, a failed start — now disarms it.
- **Two ticket sources on the same provider each get their own fast-track
  rung.** The pipeline looked the rung up by provider, so a second Jira
  project ran at the first one's depth.

- **The CLI works when the server's access-token gate is on.** `mindflock ls`,
  `new`, `rm` and the other session commands now send the token
  (`MINDFLOCK_AUTH_TOKEN`, or the one the server saved in `settings.json`)
  instead of getting a 401. A server that refuses the token is reported as
  that, not as "no server found": `mindflock accounts` stops instead of
  editing the settings file behind it, and `mindflock uninstall` treats it as
  running. The settings-file token is only sent to this machine.

- **A plain session on a CLI without a prompt argument gets its initial
  prompt.** Outside ticket sessions (which already pasted it in), aider,
  goose, opencode, cline and custom scripts dropped the prompt silently; it
  now waits in the prompt queue and is typed in once the agent is idle.

- **The prompt queue never types into a bare shell.** After you quit a
  provisioned session's agent, its terminal drops to a shell that still
  reads idle; a queued prompt (and its Enter) would have run there as a
  command.

- **The fork diff no longer depends on your gitconfig.** `diff.noprefix`,
  `diff.mnemonicPrefix`, quoted non-ASCII paths, an external diff tool or
  forced colour each changed the shape of a session's diff.

## [0.5.0] - 2026-10-03

### Added

- **Map tab: see where your agents are changing code, as a tree.** Each
  session's Map draws its repo as a tree — the trunk is the repo, branches are
  folders, leaves are files, roots underground are the tests — and your agents
  are birds that nest in the folder they are working in. Unchanged code recedes
  to a calm silhouette; every changed file glows in its agent's colour and the
  branch from the trunk to it lights up, so "where are the changes?" is a
  glance, not a hunt. A left column lists the agents (and their subagents),
  sums the changes by area, and names the riskiest one ("config/settings.py —
  73 import it"). "⚠ Could break" shows, on demand, which code imports what
  changed. Click a bird's card to fly to it and follow it as it works. Scroll
  to zoom, drag to move, `/` to find a file, click a leaf for its functions.
  The layout runs off the main thread and is cached per repo, so a 5k-file
  repo grows its tree in about two seconds once and reopens instantly.

- **Red zones and green zones: fence agents in or out of parts of the
  code.** Mark a folder or file **Keep out** (red) and Claude Code is stopped
  before it edits it — through any tool, in subagents too, including shell
  writes caught right after they happen — with the reason fed back to the
  agent. Mark one **Only here** (green) and the agent may change nothing
  outside it (lockfiles and the tests of the code you scoped stay writable).
  Set either from the Map in one click on any changed folder, from the
  **Zones…** dialog for a whole repo, or mid-flight with "tell the agent".
  Pushes, pull requests and merges refuse to ship a red-zone change unless you
  override, and the guard re-arms itself if anything disables its hook. Other
  coding CLIs get the same zones as detection, alerts and the push gate.

- **Plan first, then let the agent go.** "Ask for plan" (or **Plan first** in
  New Session, or per repo for intake) has the agent list every file it
  intends to touch and why; the Map shows those files before any edit, you
  fence off what it should not touch, and **Go** sends one message with the
  rules — or **Go — only the planned files** turns the plan itself into the
  allowed area.

- **Ctrl+F in a live terminal pane.** Find text in a session's terminal —
  its tmux history and, for full-screen apps like Claude Code, the content you
  would scroll to — with a match count first, then next/previous that jumps
  straight to each hit.

- **Find inside the history overlay.** The overlay you get by scrolling past
  the screen edge now has its own search, with case, whole-word, regex and
  "near each other" options.

- **"Open the IDE when a ticket starts"** — a toggle in Settings → IDE.

### Changed

- **Shortcut ingestion can require a label.** A Shortcut source can name one
  or more labels (comma-separated); a ticket must carry one of them, on top of
  its workflow state, before it is ingested.

- **New ticket sources start with their provider's branch prefix** (`sc/`,
  `jira/`, `lin/`, `gh/`, `asana/`); a custom id still overrides it.

- **Each ticket queue can set its own effort level**, independent of which
  agent it uses.

### Fixed

- **Turning IDE auto-adopt off now survives a restart.** The toggle used to be
  remembered only in memory, so every restart quietly switched it back on.

## [0.4.0] - 2026-09-15

### Added

- **New → Ticket: describe work, get a filed ticket back.** The New dialog now
  has two tabs. The second one takes a sentence or two about what needs doing,
  writes a ticket from it — title, description, acceptance criteria — files it
  on one of your configured ticketing sources, and hands you the link. All five
  providers can file: GitHub Issues, Shortcut, Jira, Linear and Asana.

  There is deliberately **no ticket form**, and no route anywhere that accepts
  ticket fields. Every tracker already has a create form and it is one click
  away; the only thing MindFlock can do here that the tracker cannot is turn a
  half-formed sentence into something a teammate could pick up. The drafted
  description is normalized to paragraphs, one `## Acceptance Criteria` heading
  and `-` bullets — that narrow grammar is what the ingestion pipeline mines
  the criteria back out of, and the only grammar Jira's ADF translator
  understands. The ticket is filed into the state the source already ingests
  from and assigned to its configured member, so it lands where the board is
  looking instead of in a default backlog.

  Filing does not start a session — the ticket appears in Intake → Tickets like
  any other — but if ingestion is on, the pane says so before you press the
  button, because a ticket that quietly becomes a running agent is a surprise
  worth one sentence. A source that cannot accept a ticket stays in the picker
  with the reason (a Jira source needs its Project set to a project key; a
  Linear source in a multi-team workspace needs a team), and that refusal comes
  *before* the model runs rather than 25 seconds into it. If the drafting
  succeeds and the filing then fails, the draft comes back with the error: it
  is the expensive half, and at that moment the only copy of that text.

- **Merge a duplicate ticket into another one, and delete it.** Two people file
  the same task constantly, and the queue's only answers were to start two
  sessions on it or to remember forever that one is "really" the other —
  neither of which the second filer can see. A ticket row in Intake → Tickets
  now carries **Merge into…**: pick another ticket from the same source, and
  everything the duplicate has — description, acceptance criteria, comments and
  attached files — is appended to the survivor's description, a comment on the
  survivor records where it came from, and the duplicate is **deleted in the
  tracker**, not just hidden here. Shortcut, Jira, Linear and GitHub Issues;
  Asana's adapter is read-only and its rows never offer the control.

  The four writes run in the order that makes every partial failure survivable
  — the append first, so a refusal there leaves both tickets untouched, and the
  delete last, so a refusal there leaves a duplicated ticket rather than an
  erased one. The result is reported as a sentence rather than a tick, and only
  "the content is on the survivor and the original is gone" is green: a tracker
  that refused the delete (deleting a GitHub issue needs repo admin) says so,
  names the reason, and tells you the duplicate is still there. The
  confirmation is an inline drawer under the row rather than a dialog, and its
  button arms before it fires — this is the one thing in Intake that destroys
  something other people can see. A push goes out over ntfy too, since deleting
  a ticket is worth carrying past the tab that did it.

- **Drag a file onto the New Session dialog and get its path.** Anywhere on the
  card — the whole dialog lights up, not just the box — and the file is
  uploaded and its path dropped into whichever field is in front of you: the
  one-sentence describe box on the first screen, the launch prompt on the full
  form, or the ticket brief on the Ticket tab. The agent runs on this machine
  and cannot see your browser, so "look at this screenshot" has to become a
  path it can open; the terminals have answered this gesture for a while and
  now the dialog does too. Dropping onto the full form also opens the Prompt
  fold, so you can see where the path landed rather than trusting that it did.

### Changed

- **A new project no longer lands in a folder you have never heard of.** The
  dialog used to offer the first of `code`, `projects`, `src`, `dev`, `work`,
  `Development` that happened to exist under `$HOME`. One shallow clone dropped
  into `~/src` by some unrelated tool is enough to make that folder exist, and
  from then on every new project was proposed into it — past a `$HOME` holding
  a dozen real projects. It now picks whichever of those directories, `$HOME`
  included, actually holds the most repos; the list only breaks ties, and a
  directory you made and named still beats an empty `$HOME`.

- **A new project whose name is already a folder opens that folder.** Asking
  for `trawl` when `~/trawl` is right there gave you a second `trawl` somewhere
  else; the folder is the one thing a plan leaves behind that closing the
  session never cleans up. The name is matched ignoring case and punctuation,
  so `date-bot` finds `DateBot`, and the form says it is *adopting* an existing
  folder rather than creating one.

### Fixed

- **Frontend dependency updates can be merged again.** The committed bundle in
  `backend/web/static/` is what actually ships, and CI rebuilds it and demands
  an identical tree — which Dependabot can never satisfy, because it does not
  run Vite and React is bundled. Every frontend bump therefore arrived red and,
  since they all rewrite the same lockfile, merging any one conflicted the
  rest. Those updates are grouped into a single PR now, and both facts are
  written down where the next person will hit them. React 19.3, Vite 8.3,
  TypeScript 7 and xterm 6 came in this way.

- **A failing frontend build no longer reports itself as a blank error.**
  `check-bundle-fresh.sh` sent the build to `/dev/null`, and `tsc` writes its
  diagnostics to stdout — so a type error surfaced in CI as a bare
  `exit code 1` after a third of a second, indistinguishable from a stale
  bundle. It now replays the build log when the build fails.

- **CI no longer runs twice on every pull request.** An unfiltered `push:`
  trigger sat beside `pull_request:`, so each PR ran the whole matrix against
  byte-identical trees — two Linux suites, two macOS suites, two cold installs.
  The GitHub Actions in use also moved off the deprecated Node 20 runtime.

- **“+ Folder” in the folder browser now works in the desktop app.** It never
  had: it asked for the name with `window.prompt`, which Electron does not
  implement at all — it returns null and logs a refusal — so the button did
  nothing, silently, for everyone outside a browser tab. The name is now typed
  into a row inside the browser itself, directly under the path it will be
  created in.

## [0.3.2] - 2026-09-11

### Added

- **Drop a file on the assistant window.** Dropping an image or a file on the
  assistant chat did nothing, while the same drop on a session terminal
  uploaded it and typed back the path the agent can read — so the gesture
  looked broken rather than missing. The assistant takes it now (and an image
  pasted with `Ctrl+V` too): the file lands in `~/.mindflock/pastes`, since the
  assistant has no workspace of its own, and its absolute path goes into the
  prompt. Windows you only watch — a log tail, a verify run — still let a drag
  pass through, having no prompt to paste into. The assistant's `Ctrl+V` now
  goes through the same path as a session terminal's, so pasting text reports
  "Pasted N chars" where xterm's silent default used to run.

## [0.3.1] - 2026-09-09

### Added

- **Install a missing database driver from the tree.** A connection whose
  driver is not installed shows "Install driver" on the failing node itself,
  not only inside the connection form — the error the user can actually fix no
  longer dead-ends in a shell command to paste. The install's progress is keyed
  by engine rather than by the open form, so it survives the form being
  replaced (or never having been opened), and a successful install refreshes
  the driver cache and clears the stale "driver is not installed" rows so the
  tree retries instead of reporting a problem that is gone.

### Changed

- **A renamed session row reads like every other row.** Renaming a tab used to
  put its label in italics; the real title is already one hover away in the
  tooltip, and a row that is styled differently for a reason nobody can see
  just looks like a mistake.

### Fixed

- **One verification checklist per branch, not per window.** With the same
  branch open in two windows — a duplicated window, a repo adopted twice — both
  windows saw the same push and each wrote its own checklist, so one piece of
  work produced two cards from two model calls whose drafts did not even agree.
  A checklist describes a branch's diff in a repo, so that is what it is now
  keyed on: the second window writes nothing, a later push from either window
  refreshes the one checklist, and the write-a-checklist button reports which
  window's checklist already covers the branch instead of pointing at a row
  that does not exist.

- **A verify card no longer stops one rung short of where the work actually
  is.** In a repo that merges pull requests into `staging` and promotes
  `staging` into `main`, the "landed on" line froze at `staging` — the pass
  that asks where work has got to stopped as soon as the work reached the
  branch the repo ships from, which in that shape is the FIRST rung. It now
  keeps asking until a landing has stopped moving (a week without a promotion),
  which also bounds the question for a plan sitting on `develop` — the old rule
  re-probed those for ever.

## [0.3.0] - 2026-09-02

### Added

- **Window rows are first-class sidebar rows.** The Assistant chat, the log
  tails, verify watch windows and extension panes now behave exactly like
  session rows in the rail: they carry the same `Ctrl/Alt+1…9` number badge
  (and those shortcuts focus them), drag to reorder anywhere among the
  sessions — one saved order holds both, and the grid's slot order follows
  it — are toured by `Ctrl+Tab`, and narrow under the sidebar filter. A
  reordered assistant even reopens where you left it. En route, dragging a
  session row no longer silently erases the saved position of rows absent
  from the current snapshot (e.g. a sleeping remote device's sessions): the
  drop merges into the saved order instead of replacing it.

- **"Your PR was approved."** A notification when a reviewer approves the pull
  request of a session you have open — the moment that is actually waiting on a
  human, and the one the app could not see before. The verdict is read from the
  pull request's own review list (latest review per reviewer; a standing
  "changes requested" outranks an approval; a comment is not a decision), since
  GitHub's `mergeable_state` says `blocked` both for a missing review and for a
  failing required check, and says nothing once the approval lands. Off the
  same rule list as every other notification, so it reaches the browser and
  ntfy alike, and there is an opt-in twin for "changes requested". New event:
  `session.pr_review_changed`.
- **Resizable columns in the database client.** Drag a header's right edge in
  any result grid (the SQL results and the table view are the same grid);
  double-click that edge to fit the column to its widest value. Widths are
  keyed by column name, so they survive a sort, a page turn and a reload of the
  same query. Columns now also START at the width of their own NAME rather than
  their content: a header you cannot read is worse than a value you cannot, and
  the value is one double-click away.

- **One page for closed work and disk.** **Recently closed** now lists closed
  sessions *and* the workspace directories no closed session accounts for — the
  separate "Workspaces on disk" manager is gone, and the top bar's **Recent**
  dropdown is a single button. Rows carry both identities (branch/session name,
  what the directory is, when it was last used, its size) with the same filter,
  sort and bulk selection the list dialogs share. Protected base clones and cache
  refreshers are no longer rows at all — they are counted and named in the header
  instead, along with anything a live session is using, which the page leaves to
  the sidebar. New: `GET /api/recent`.
- **Remove unused worktrees**, in that page: one sweep for every worktree no
  session is using and nothing has touched for over a week
  (`POST /api/workspaces/prune-worktrees`). It can only ever remove a worktree
  *git generated* — the test is that the directory's `.git` is a gitdir FILE, as
  `git worktree add` writes it, so a repository, a clone, a `_base_*` mirror, a
  `pr-*` review clone and any folder git did not make are all excluded
  structurally, and nothing outside MindFlock's own worktrees root is looked at.
  The candidate list is resolved on the server (never a path the page sends) and
  re-verified immediately before each delete; the confirmation names every
  directory and says what a removal costs, since a worktree's branch and commits
  live in the repository it came from and survive. Anything holding uncommitted
  work, or a detached HEAD no ref contains, is held back unless a second
  confirmation includes it. Staleness is the newest of the directory's mtime and
  the worktree's own git index/HEAD/reflog — the directory's mtime alone runs days
  behind real use.

- **Extensions (Addon API v3).** An addon can now contribute UI the way a
  VSCode extension does: one draggable/hideable sidebar bar with buttons,
  commands in the command palette, and dialog and grid-window surfaces whose
  bodies it renders into host-owned keep-alive containers — so typed text and
  unsaved edits survive a grid drag, because the drag remounts the window's
  React shell and never touches the extension's DOM. Everything is declared
  in a static manifest served by `GET /api/addons`, which is what lets a bar
  button and a palette entry exist, and a declared dialog open, before the
  extension's ES module has loaded; the module is imported lazily on first
  use, `activate(api)` runs once against a small frozen API object, every
  registration returns a disposable, and every callback runs in a host
  `try/catch` attributed to the extension — a broken extension toasts once
  and shows its error on its Settings row instead of taking the app down.

  User extensions are discovered once at startup from
  `~/.mindflock/extensions/<id>/extension.py` (`build(ctx) -> Addon`; an
  optional `frontend/` is served at `/extensions/<id>/`), with ids that
  collide with the core API refused, and toggled on **Settings →
  Extensions** (`extensions.disabled` in settings.json — the only
  per-extension state that file ever carries; an extension's own
  configuration is its own file). A disable tears down the frontend
  completely; a discovered extension's backend stays loaded until the next
  restart, which the row says out loud. The manifest's `api_version` is the
  minimum host API level the module needs, so an extension written against a
  newer app fails with one clear message rather than a `TypeError` mid-click.
  Existing addons are untouched. Reference: docs/extensions.md § 4, including
  a two-file starter extension.
- **Database Client**, the first extension — a **Database** bar (Explorer,
  SQL) with SQLite, PostgreSQL (psycopg) and MySQL (pymysql) connection
  profiles kept in `~/.mindflock/dbclient.json` (0600, passwords never sent
  back; read-only profiles enforced at connect), a lazy schema tree, an
  editable table grid (sort, filter, paging, insert/update/delete previewed as
  SQL and applied in one transaction with stale-row detection; views and
  tables without a primary key read-only, and say so), a SQL query pad
  (statement at cursor, run all, history, a needs-confirmation guard for a
  no-WHERE `UPDATE`/`DELETE` judged after stripping string literals), and
  CSV/JSON export. Every statement goes through one server-side chokepoint:
  single statement only, 1–300 s timeouts, a 10 000-row cap, identifiers
  validated against the introspected schema, values always bound. Drivers are
  detected, not required — the connection form names a missing one and offers
  an **Install driver** button that puts it into the environment already
  running the app (`uv pip install --python <this interpreter>`, falling back
  to pip), re-checks importability in the live process so nothing restarts,
  and shows the installer's own output when it fails. Where an in-app install
  would mean breaking a system-managed Python it says so and keeps to the
  copyable command.
- **Database Client: an IDE-grade tree, the query unified with the table, and
  in-place table detail.** The explorer tree now groups a schema's objects
  under **Tables (n)** / **Views (n)** nodes, tables unfold into their columns
  (type badges, a key on the primary key), databases and schemas carry a size
  badge where the engine answers it from cheap statistics (`pg_database_size`,
  summed `pg_total_relation_size`, `information_schema` lengths — never a
  scan), and every scope row grows hover actions (New query here, Refresh).
  The table view and the query are one page: a **SQL bar** above the grid
  shows the SELECT behind it and is visibly rewritten by every sort click,
  filter, page turn and page-size change; edit it and Ctrl+Enter runs a
  custom query in the same grid (read-only, with a badge), and any managed
  action — or the Table button — returns to the page. Selecting a table in
  the explorer embeds that unified view right where the column summary used
  to be; the old **View data** button is now **Open as window**, which opens
  the same view as a grid window.
- **Windows in the sidebar.** Every open non-session window — MindFlock
  logs, System logs, the Assistant chat, a verify watch window, an extension
  pane such as a database table — now gets a row under a **Windows** header
  at the tail of the session list, closable with the same ✕ a session row
  has; clicking a row scrolls its window into view. The panes themselves no
  longer draw their own Close buttons — the rail is the one place windows
  are controlled, exactly like sessions.

### Changed

- **Every window closes from its own header.** Each grid window — session panes
  included — now carries a ✕ at its top right, next to the copy-all button. It
  is the same action as the window's sidebar row: session windows hide (the
  session keeps running), special windows close (a verify run keeps going).
- **Window headers scroll sideways instead of clipping.** In a narrow pane the
  tab bar and chips used to be cut off past the right edge — and the old
  narrow-pane rules hid the usage chip and live-step text entirely. Everything
  now stays rendered and reachable by horizontal scroll, while the history /
  copy-all / ✕ cluster stays pinned to the right edge.
- The Database bar no longer has a **SQL** button; the query pad is still one
  step away via the Explorer, the command palette (`Database: SQL query pad`),
  and each table view's SQL bar.
- Deleting a workspace now also has to name a directory the server *lists* as a
  workspace — a flat child of a provisioning root, or a worktree leaf. A deeper
  path (`<workspace_dir>/_base_repo/src`) used to pass every guard, because the
  containment check accepted any descendant while the base-clone protection
  looked only at the last path segment. This is the "direct child" rule the
  endpoint and its docs always claimed.
- Deleting a worktree now prunes the stale registration from the repository the
  worktree's own `gitdir:` pointer names. The old test ("is my parent directory
  called `worktrees`") is false for every branch name containing a slash, so
  nested worktrees left registrations behind, which then made a later
  `git worktree add` for the same branch fail. Empty branch-slug directories left
  under the worktrees root are collected too.
- Wiping a closed session's worktree is refused with **409** when a still-running
  session shares that directory (a session and its copy keep one worktree).
- Non-session windows (the assistant, log tails, verify watches, an extension's
  table) are rows in the sidebar's own list instead of a separate "Windows"
  group, and they take a grid slot like any session: they share one cap and one
  MRU, so at "view: 1" the window on screen is the one you picked, and opening
  one selects it. Previously they were appended after the cap, so "1" meant one
  session plus however many windows happened to be open.
- The database client's CSV and JSON downloads are one control instead of two
  buttons that broke onto separate lines in a narrow toolbar.
- Forgetting a closed session re-reads the store before writing it back, so
  deleting several rows at once no longer resurrects the entries whose
  directories just went (each would have offered a Reopen that could only 410).
- **An ingested run's commits get a written message, not the ticket's title.**
  A fast-track run started from a ticket, PR or issue used to commit under the
  item's own name — the work as *requested*, repeated verbatim on every commit
  it made. The item's name is now the fallback: at commit time, with the diff
  final, the run asks the same generator the ✨ **Write it** button uses, and
  the item's id and name go along as context. If no model answers (a CLI with
  no text-only mode, a logged-out one, a timeout), the commit still lands under
  the item's name exactly as before.
- Cancelling a commit with `Ctrl+C` clears the "pre-commit" chip within a poll
  instead of leaving the session looking wedged for most of a minute. The
  commit chain now drops its own lock from a trap, so an interrupted commit
  cleans up at the moment it is interrupted rather than waiting for the
  stale-lock self-heal (which stays, for what a trap cannot catch — a `SIGKILL`,
  a killed pane).
- The ✨ **Write it** buttons (Commit, and the verify-plan writer) show a
  spinner while a message is being written. "Writing…" alone read as a dead
  button on a cold CLI start.
- **Shift-click extends the selection in the database client's grid.** Tick one
  row's checkbox, shift-click another, and everything between takes the state
  the clicked box just took — so it unticks a run as readily as it ticks one.
  Deleting twenty rows was twenty clicks. The anchor is the last row you ticked
  plainly, and it survives the extend; a new page of rows clears it.
- **The web UI is built with Vite 8** (Rolldown + Oxc, replacing Rollup +
  esbuild), which also unblocks `@vitejs/plugin-react` 6 — the plugin major
  that 0.1.x had to pin away from because it requires vite 8. The shipped
  bundle is smaller for it: 2.00 MB to 1.77 MB, 458 kB to 371 kB gzipped,
  most of it source comments that the old toolchain dropped and the new one
  preserved until told not to. Nothing in the UI changes.

## [0.2.1] - 2026-08-28

### Added

- **Sessions can run as different identities — a second Claude subscription
  beside a work one, a metered API key, an OpenRouter key — without logging any
  CLI out.** An **auth profile** names one identity, and every session runs
  under exactly one of them or under none, which is each CLI's own ambient
  login and remains the default. Three kinds: an `account` is a second login of
  the CLI itself, kept in its own isolated config dir and reached through
  `CLAUDE_CONFIG_DIR` / `CODEX_HOME`; `api_key` and `openrouter` inject a key at
  launch, the latter through OpenRouter's Anthropic-compatible endpoint. Any
  profile can also carry raw `env` overrides, which apply to any CLI at all —
  the escape hatch for a user-defined provider the typed kinds have never heard
  of. A combination with no route (an OpenRouter profile on `cline`, say) is
  reported out loud and left on the CLI's own login rather than launched with
  invented env.

  Set them up in Settings → **Accounts** or with `mindflock accounts`, which is
  the same store and goes through the running server so the app picks a change
  up immediately. An `account` card shows the login command to paste into a
  terminal, because the CLI's OAuth flow is interactive and cannot run through
  an API; an OpenRouter card's **Test key** reports the key's real spend and
  turns its model field into a picker over the models that key can actually
  reach. Pick a session's identity in the **New session** dialog — which also
  steers the agent picker to a CLI the chosen account can route — or swap a
  *live* one from the `@account` chip in the pane header: the agent restarts
  under the new identity while the worktree, diff and shell pane survive. A
  swap starts a fresh conversation, since a transcript belongs to the account
  that created it, so each window keeps a thread per identity.

  Secrets live in `~/.mindflock/settings.json` at mode 0600 and read back
  masked, and a save that resends the mask keeps the stored value. A key
  reaches its CLI through a per-run file rather than argv, so it never lands in
  `/proc/<pid>/cmdline`. A local model still outranks a profile — a session
  pinned to this machine cannot be pulled off it by an account pin — and with
  no profiles configured every overlay is empty and every launch path is
  byte-identical to before the feature existed. Full guide:
  [docs/accounts.md](docs/accounts.md).

- **Notifications carry the session's rail slot: "[3] sitecheck-bot7 has
  finished."** The sidebar numbers its first nine rows, and those numbers are
  how people locate a window — so the desktop notification, the bell feed, and
  the in-app toasts now lead with the same number the rail shows at that
  moment (drag order and the live filter applied, resolved client-side at
  render time, because the order is per-browser state the server never sees).
  Rows past the ninth, filtered-out sessions, and phone pushes — which have no
  rail to agree with — carry no number rather than a wrong one. Extension
  pages get the same resolver as `window.mindflock.slotNumber(title)`.

### Changed

- **The whole window state flow got significantly tighter — where the evidence
  earns it.** "Your agent has finished" used to take ~55 seconds from the last
  keystroke of a turn, for every agent, in every situation: a flat 45-second
  dwell chosen as a blanket over three different hazards (a human typing a
  follow-up, a queued prompt in flight, a fast-track chain mid-step), plus the
  activity settle and a poll tick. Each of those hazards is now checked
  *exactly*, so the padding survives only where the evidence is weak.

  A hook CLI's finished run (Claude, Codex) now announces in **~15 seconds**;
  a pane CLI's in ~35; the CPU backstop keeps the full 45. The chip — and the
  default-on "needs your input" push — react within **one tick** when the
  CLI's own hook reported the state, because a hook marker cannot misread a
  frame the way a pane capture can (the 3s settle stays for pane-derived
  readings, and for "ran out of usage", whose banner detection is a pane fact
  whatever triggered it). Queued prompts go out one drain pass sooner on a
  hook-reported idle. The exact gates that replaced the padding: recent human
  input (the terminals, /send, send-now — and tmux's own client activity, so a
  conversation held in a raw tmux attach doesn't buzz per turn), a send-grace
  covering the window where the last queued prompt is typed but not yet picked
  up (previously covered only by the blanket — announcing there was possible
  before this release), and fast-track's own record, bounded by its driver
  lease so a wedged chain cannot mute a session forever.

  The turn's END also got a harder look at *why* it ended: the working→idle
  hook transition now forces a fresh usage-limit probe (a miss never refreshes
  the probe throttle), closing a window where a turn cut short by the cap
  could be announced as finished — including the variant where the banner left
  the screen before the throttled re-check ever saw it, which no amount of
  dwell used to fix. And the pane layer's proven-busy bookkeeping is cleared
  by every non-working reading, so a busy run can no longer straddle an agent
  death or a relaunch and re-arm off stale evidence.

### Fixed

- **Database Client: the tree indents for real.** Row depth was set as a CSS
  custom property through `Object.assign(el.style, …)`, which is a silent
  no-op for `--` properties (no named setter on `CSSStyleDeclaration`) — the
  entire tree had been rendering flat at one padding since it shipped. The
  element builder now routes `--` keys through `style.setProperty`.
- **Database Client: "Add connection" opens the form instead of failing.**
  Opening the explorer on the new-connection form (the palette command, or a
  saved link with `ref="new"`) called into the form before the dialog's root
  element existed — a temporal-dead-zone `ReferenceError` the host reported
  as "surface failed to render". The jump now happens after the dialog is
  built.
- **Database Client: hidden means hidden.** Elements toggled with the
  `hidden` attribute but styled with their own `display` rule (the grid's
  "No rows" note, the read-only badge) stayed visible — author display beats
  the UA's `[hidden]` rule — so an empty lock chip sat in the tabs row and
  "No rows" floated over full grids. One scoped `[hidden]{display:none
  !important}` guard restores the attribute for the whole extension.
- **Database Client: no more silent edit loss.** Typing a per-column filter,
  and (new) switching tables in the explorer while the embedded grid holds
  unsaved changes, now ask before discarding — the same confirm every other
  managed action already had; declining the page-size confirm also snaps the
  selector back. Saving with a cell editor still open now commits that cell
  into the batch instead of dropping it, and typing in a filter box no longer
  loses focus after every pause while the page reloads under it.
- **Database Client: stale-response and stale-cache holes.** A slow page
  response can no longer repaint over a newer one (or over a custom result) —
  loads carry a generation token; Refresh after a custom *write* statement
  reloads the table instead of silently executing the statement again;
  refreshing a scope in the tree also drops the cached table_info beneath it
  (stale column leaves); and refreshing schema "auth" no longer also forgets
  sibling "auth_archive" (prefix over-match). The Postgres/MySQL size
  queries run under a 5 s statement timeout and fall back to plain names.
- **Sessions no longer read "idle" while their agent is visibly working.** The
  activity hooks MindFlock installs into a repo's `.claude/settings.local.json`
  resolved their marker *directory* at install time — baked in as an absolute
  path — while resolving the session name at fire time. One sandboxed install
  poisoned the well: a Verify run executes with `HOME` redirected into a
  scratchpad, and when its MindFlock instance re-pinned the SHARED repo's
  hooks file, every cohabiting session's hooks began writing markers into a
  dead sandbox under /tmp. The chips froze on whatever the last pre-poison
  reading was — "idle", for a session that then worked for fifteen straight
  minutes — and the resume-thread binding drifted the same way, so the live
  `claude agents --json` signal (which knew the truth: `busy`) was consulted
  under a dead conversation id and answered nothing. Both directories are now
  resolved *inside the firing hook*, from the CLI's own environment: a
  sandboxed CLI writes to its sandbox, a real one to the real home, no matter
  who installed the hooks or where.

- **Archived Shortcut work no longer fills the Tickets panel.** A workflow state
  showing three stories in Shortcut listed ten in MindFlock, and the extra seven
  could not be found in Shortcut at all — because Shortcut hides them. Six were
  archived stories, which `/stories/search` returns and boards do not; the
  seventh was a live story under an archived epic, which disappears from the
  board with the epic it belongs to. Both are now filtered out of every Shortcut
  listing.

  The panel was the visible half. The same search feeds the ingestion pipeline,
  so a story archived while it sat in the configured ingest state could still be
  picked up and worked — archiving it being, presumably, how you meant to stop
  that. The epic lookup is one extra call per search, skipped entirely when no
  story carries an epic, and best-effort: if it fails you get the old, wider
  listing rather than an empty one.

- **The dev shell's notifications get their icon back.** A dev toast came up
  headed *MindFlock-dev* and badged with the blank white document tile — the one
  Windows draws when it cannot read the icon it was pointed at. The `.ico` was
  fine; the *path* was not. A shortcut's `IconLocation` is resolved by the
  Windows shell, later, in whatever process happens to be drawing a tile — and
  on the supported Windows shape the checkout lives on a WSL share, which the
  shell's icon extraction will not read. The window and taskbar icons were right
  all along, because Electron loads those itself with an ordinary file read,
  which is exactly why only the toast looked broken.

  The dev shortcut now keeps a copy of its icon on the local disk, in the dev
  profile beside the rest of the throwaway dev state, and points at that. It is
  byte-compared rather than rewritten, so changing `MINDFLOCK_DEV_ICON` is
  picked up and leaving it alone costs nothing; a checkout on a local drive
  keeps using its own file. The shortcut already on disk repairs itself on the
  next dev run.

- **"Your agent has finished" is now a claim MindFlock can back up.** The idle
  notification fired on the wrong fact. A session's chip goes grey when its CLI
  reports a turn ended, and a coding CLI reports that at the end of *every*
  assistant reply — so a ten-turn conversation announced "finished" ten times,
  a queue draining ten prompts announced it between each pair, and a window you
  merely clicked back open announced it for an agent that had not run anything
  at all.

  That last one is the one people saw most, and it was two bugs standing on each
  other. Opening a pane relaunches an agent whose tmux session has died — and
  the activity layer *threw away everything it knew about a session* on every
  poll while it was offline, so the relaunched CLI arrived as a stranger. Facing
  a pane it had never seen, the classifier assumed it was working, on the theory
  that a fresh agent usually is. It held the badge at **running** for eight
  seconds, decayed to **idle**, and rang the bell — a whole work cycle
  hallucinated out of a mouse click. The stale hook marker left behind by the
  *previous* run could do the same thing on its own: nothing deletes those, and
  they are trusted for six hours.

  Three changes, one per layer. The classifier **no longer guesses**: a pane it
  has never seen is read for the two things a single frame can actually prove —
  a usage-limit banner, and the interrupt hint that means a turn is live right
  now — and anything else is reported as idle, because nothing on that screen
  says otherwise. Its memory is now scoped to a tmux **incarnation** rather than
  wiped on every hiccup, so a session that briefly went offline comes back with
  its history intact, while a genuinely relaunched one starts clean — and a hook
  marker written before the current session existed no longer speaks for it.
  Finally, "the agent has finished" became its own event, `session.turn_ended`,
  which fires only once MindFlock has **watched that agent work**, seen it stay
  idle for 45 seconds, and confirmed nothing is queued to wake it. Once per
  cycle of real work, however long the session then sits there. A turn the
  usage cap cut short does not count — it did not finish, so the evidence is
  dropped rather than left armed to claim "done" the moment the banner scrolls
  off the pane.

  One more way to hallucinate a work cycle turned up after that, and it needed
  no mouse click: a session parked untouched for nineteen hours announced
  "finished". Its own auto-updater burned a single four-second poll's worth of
  CPU, which the classifier reads as **working** — a busy process tree is how it
  tells a thinking agent from a parked one — and twelve seconds later it was
  idle again with a full work cycle behind it. A `/clear`, a garbage collection
  pause, a stray compile: all of them look the same from outside. So "watched
  that agent work" now means **corroborated**, not merely *looked busy*. The
  CLI's own report counts, at any duration — a hook fires because a prompt was
  submitted or a tool ran. So does the interrupt hint on the pane, which is
  there *because* a turn is running, and which covers a long turn whose marker
  goes stale mid-thought. A busy process tree on its own no longer does: it
  still turns the chip green, and it no longer earns the right to interrupt you.
  This is not a Claude-only rule. Codex reports through hooks the same way;
  aider, antigravity, cline, goose and opencode have no hooks, so their status
  line is the signal — and because a regex against someone else's UI can simply
  be wrong, those keep a backstop: twenty unbroken seconds of CPU still counts,
  which no spike lasts. The two CLIs that report for themselves get no backstop,
  since a spike on a parked session of exactly that kind is the bug being fixed.
  A test pins which provider sits on which rung, so a CLI that quietly stops
  matching its own hint shows up as a failure rather than as silence.

  Everything that has to stay fast stayed fast. **Needs your input** and **ran
  out of usage** still fire on the instant signal — those are "come here now",
  and a dwell on them would be a bug of its own. The chip still flips the moment
  the state does. What changed is only which of those moments is worth
  interrupting you for.

  Two things fixed themselves on the way. The bell's own feed had no rule and no
  rate limit at all, so it logged "finished — now idle" even for people who had
  never turned the notification on; it now follows the same rule as every other
  channel. And the sidebar's **"idle 25m with unfinished work — possibly stuck"**
  warning turns out never to have appeared once: it reads a timestamp the app
  had never written under that name. It works now — and, on its first outing,
  it means what it says: a *committed* branch is finished work waiting for a
  push, not a stuck session, so only uncommitted output nobody is touching
  earns the row.

- **A "ran out of usage" push no longer swallows a "needs your input" push.**
  Both channels collapsed repeat notifications per *(session, event)* for five
  seconds, and three different rules ride the same activity event — so an agent
  that hit its usage cap and then asked a question inside that window sent one
  alert, not two, and it was the less actionable one. Worse in the browser: that
  same key is the notification's `tag`, which Windows and macOS treat as
  "replace the one already on screen", so a popup you were still reading was
  overwritten. Both are keyed by **rule** now, so each rule speaks for itself.

- **The break clock no longer counts the hours you were not here.** A deadline
  is wall-clock, so on its own it kept running through a closed window, a
  shut-down machine and a slept laptop — and whoever opened MindFlock in the
  morning was met immediately by the break card, its away-clock already at
  nine hours and still climbing. Time away from the app is time away from the
  desk, which is the break itself.

  The app now stamps a heartbeat while it is running, and a gap of more than
  five minutes starts the interval over rather than resuming it: on opening, and
  also mid-run, which is the only thing that notices a machine that slept with
  the window open — that case takes the stale card down with it. **Opening the
  app starts the countdown**, whatever yesterday left behind. A refresh still
  cannot dodge a break: the two are told apart by navigation type, so every
  reload path (F5, the palette, the reload every tab does when the server
  restarts, the API client's reload on a lost session) keeps its deadline, while
  a shell launch or a new tab is an open. The five-minute tolerance is wide
  because a hidden tab's timer is throttled to about one tick a minute, which
  must not read as an absence.

- **Windows dev builds head their notifications with a name.** A toast is
  labelled with the display name of the Start-menu shortcut registered for the
  app id that raised it. The prod app gets one from its installer; the isolated
  dev shell has its own app id and no installer, so Windows printed the raw
  `ai.mindflock.desktop.dev` across the top of every toast. A dev run now writes
  that shortcut itself — `MindFlock-dev.lnk`, rewritten only when missing or
  stale, best-effort — and the toasts read **MindFlock-dev**. It doubles as a
  working launcher, so it is also the thing to pin to the taskbar. Prod is
  untouched. Development-facing only; see `electron/README.md` and
  [docs/development.md](docs/development.md).

## [0.2.0] - 2026-08-25

### Added

- **Verify — "it merged" is not "it works".** The pipeline's last honest
  checkpoint was the merge, and merged is not verified: nobody has opened the
  product and looked at the thing. The agent's own green suite doesn't answer it
  either — that ran *in a worktree*, against *its* half of the repo, before
  anyone else's work landed on top. Nobody writes the check down, because at the
  moment you could write it the change isn't live yet, and at the moment you
  could run it the diff is a week old and the session has been deleted. MindFlock
  is the one process that sees both moments.

  So it writes the checklist at push time, holds it, and hands it back when the
  work is really live: top-bar **Verify** (`Alt+V`), with a badge for what is
  waiting on you. Track a repo (Verify → **Sources**, or its committed
  `.mindflock.toml`), and the first push of each session branch turns that
  branch's diff into steps. Each step carries an **actor**: `agent` for anything a
  shell can settle, `human` for visual judgement, a real browser, or an external
  service — and anything unrecognised becomes `human`, because an agent silently
  passing something it had no way to observe destroys the whole point. **Run**
  starts a real session that checks out the live branch and works its half, with
  the stream inline in the dialog, so watching the agent and answering the steps
  it leaves you are one screen instead of two windows and a round trip.

  **Merged is not deployed**, so ancestry starts a clock rather than handing you
  the list: the row reads *"Merged 4m ago — waiting for it to deploy"* until the
  repo's deploy window passes (**Deploy takes (minutes)**, default 5; set it to 0
  where merging is shipping, or skip the rest with **⋯ → It's deployed — check it
  now**). Checking too early is not merely early — you see the behaviour the
  change replaced and record a failure against correct code, which is the one
  outcome this surface cannot survive.

  Two model postures, deliberately opposite: writing a plan is an unattended
  read-only one-shot through the session's own CLI (`claude -p`, `codex exec`,
  no PTY, no skip-permissions flag — a question about the tree has no business
  editing it), while running one is an ordinary session you can watch, interrupt
  and take over. Plans live in their own `~/.mindflock/test_plans.json` because
  they **outlive their sessions**, and they are keyed per session branch: five
  pushes make one checklist, and a later push may rewrite it from the newest
  commit only while nobody has answered anything — an append-only checklist could
  never retract a step that a later commit made impossible to pass.

- **Every Verify card says where the work actually is.** Beside the branch and
  the sha, a checklist now carries a chip reading *merged into `staging`* — the
  branch on `origin` this work most recently reached — tinted when that branch is
  the one the repo ships from. It is the fact neither of the other two names:
  `branch` is where the work was pushed and the live branch is what the checklist
  is *waiting for*, so in a repo that ships through a `staging` or `develop` step
  a change spends most of its life merged somewhere the row could not say, and
  answering "is this in staging yet, or already in main?" meant leaving the app.

  Ancestry answers it — every `origin` branch that contains the commit, ranked by
  which it reached most recently, with branches that arrived in the *same merge*
  folded together, since every branch cut from `main` afterwards contains that
  merge and listing them all would answer with four names when one thing
  happened. "Most recently" is measured on each branch's own first-parent chain,
  which is what tells `staging` gaining the work at its merge from `main` gaining
  it at the promotion. Where a **squash merge** left no ancestry to find, the
  branch its pull request merged into is used instead. Refreshed every few
  minutes per checklist on one shared fetch per repository, budgeted like the
  liveness pass, and not asked again once the work has reached the branch its
  repo ships from.

- **Take a break.** Settings → General, right under Reduce motion: one switch
  whose description is the sentence it configures — *Reminder to take a break
  every `[N]` minutes*, with N editable in place (5–480, default 60). When it
  fires, a card asks you to get up, with the murmuration from mindflock.ai flying
  over your actual grid. **Snooze 5 min** pushes it back; **Resume Working** (or
  Escape) restarts the clock. Off by default. The countdown is wall-clock and
  survives a reload — a refresh is not a way to dodge a break — while changing
  the interval re-arms it from now.

  There is no scrim: the view stays exactly as you left it, sessions keep
  running, and you can watch them do it. What the screen does take is the
  pointer and the keyboard — every global shortcut stands down while it is up —
  so it is a break rather than a suggestion. The card hands the keyboard back to
  whatever had it.

  The flock is a round trip through the logo, and a lopsided one. It streams OUT
  of the MindFlock mark over **twenty seconds** — birds leaving the mark one at a
  time, each flying its own short arc and joining the live flock the moment it
  lands, so a few seconds in there is already a real murmuration by the edges and
  a stream still pouring out of the corner — and folds back INTO the mark in
  under a second when you dismiss it. A single twenty-second interpolation would
  have been twenty seconds with the flocking rules switched off, which reads as
  dead.

- **The idle flock.** Ten minutes with no click, keystroke, tap or scroll in
  the MindFlock window and the flock takes the whole screen — over the sidebar,
  the panes and the prompt boxes, with no regard for any of their edges. It is
  `pointer-events: none`, so nothing is covered or blocked, and the first click
  or keystroke sends it home to the logo the same way. It arrives out of the logo
  too, the same round trip the break card's flock makes.

  Its own switch sits under Take a break in Settings → General, with the same
  sentence-with-a-field shape — *Fly the flock over your grid after `[N]`
  minutes* (1–480, default 10). **On** by default, which the break reminder is
  not: this one can only ever appear in a room you have already left, so leaving
  it on costs you no interruptions. Switching it off mid-flight sends the birds
  home through the logo rather than cutting them off mid-air.

  Three things deliberately do not count as you being at the desk: moving the
  mouse, agent output, and anything you do in another app. So a cursor resting on
  the window doesn't hold the birds off, and MindFlock idles behind you while you
  work elsewhere — a second monitor fills with birds, which is the point — with
  the click that brings you back being what wakes it. Under
  `prefers-reduced-motion` both surfaces show a settled flock instead of a moving
  one.

- **Intake → Auto-start**, a fourth tab that answers the other question. The
  three per-source tabs are workbenches — every ticket, every open PR, every open
  issue, grouped by where it lives. This one is not "what is there" but *what
  happens next without me*: only the rows the automations will pick up on their
  own, across all three sources, oldest first, in the order the pipeline will
  draw them. One vocabulary throughout — every section reads *auto-start on* /
  *auto-start off* / *not set up*, whatever the underlying mechanism happens to
  be — and its badge counts the rows it will actually show, like the other three.

- **Reopen the work that is already on this machine.** A row for an item that has
  been worked once already said so in chips (*already ingested*, *a branch for it
  exists on the remote*) and then offered exactly one button: **Begin work**,
  which starts over. But the workspace is usually still right there — ending a
  session keeps its worktree, and a run lost to a restart leaves one behind — so
  starting fresh either collided with it or silently duplicated it. All three
  panels now lead with **Reopen** when the work is findable, resolving it
  most-informative-first: a recently-closed session (whose stashed data restores
  the branch, program, prompt and provisioning rather than approximating them), a
  provisioned clone directory, or a worktree still holding the item's branch.
  Everything about the probe is read-only and best-effort — any failure answers
  "nothing found", which is exactly how the panels behaved before.

- **Every intake row can be started three ways at once, for that one launch** —
  which CLI runs it, how far to carry it, and **how hard to think about it**. The
  effort ladder is neutral (Low, Medium, High, Extra high, Max, Ultra) because no
  two coding CLIs spell it the same way, and the server translates the rung into
  whatever the CLI that actually runs understands: `claude --effort xhigh`,
  `codex -c model_reasoning_effort=high`, `agy --effort high`. A rung above a
  CLI's ceiling **clamps** rather than being forwarded — claude warns and quietly
  uses its default for a level it doesn't know, and codex forwards the string to
  the API, which 400s — so the picker labels what will really happen ("Max (→
  Extra high)"), names the top rung the way that CLI names it ("Ultra
  (ultracode)"), and disables itself outright on a CLI with no effort control
  ("No effort control (aider)") instead of leaving a control that does nothing.
  None of the three is persisted: re-configuring a whole queue to run one item
  differently is the wrong shape of action.

- **↺ — put this window back to idle.** On a clean branch git considers finished
  (`committed`, `pushed`, `pr`) every control in the header is about advancing a
  cycle you may already consider done, so someone who just opened a PR and wants
  to keep writing code on the same branch had no way to say so. ↺ says it.
  Nothing git-facing happens — the commits stay committed, the PR stays open —
  and the pin deliberately does not touch the published stage either, so the
  autopilot, the check kicker and every `*_changed` event keep reading the same
  git-derived truth (a display pin that lied to the autopilot could make an armed
  chain try to commit a clean tree). It releases itself against the worktree
  rather than against the stage label: the moment the tree goes dirty or HEAD
  moves, it is gone.

- **The prompt queue takes a file, and reorders by hand.** Drop a `.csv` on it to
  queue one prompt per record (quoted fields, embedded commas and newlines, `""`
  escapes) or any other file to queue one per line, and drag **⋮⋮** to reorder
  what is waiting.

### Fixed

- **Notifications call a session what the rail calls it.** A desktop
  notification named its window by the machine slug the event carries —
  *"shortcut-21431 needs your input"* — while the sidebar showed either your
  rename or the pipeline label *"(tix) social-scan-noise/shortcut-21431"*. A
  push about a session that does not appear to exist under that name is worse
  than no push. The browser channel now asks the app itself
  (`window.mindflock.displayName`, the sidebar's own resolver), so the two cannot
  drift; the ntfy channel runs the same rule server-side, pinned against the
  TypeScript original's examples. Renames already won — the half that was missing
  is every session nobody renamed.

- **A duplicated window lands under the one it came from**, instead of at the
  bottom of a rail of twelve. The provisioning row is placed there too, so the
  copy never appears at the end and then jumps when the server answers.

## [0.1.17] - 2026-08-11

### Added

- **✨ writes the commit message.** The commit dialog has a button that reads the
  diff and writes the message, and a fast-track press that carried no message now
  gets one the same way instead of committing under "Work on `<session>`" — the
  subject that described the session rather than the change. It asks the session's
  own coding CLI headlessly (`claude -p`, `codex exec`, `agy --print`), so there
  is no API key to configure and no second provider to authenticate; a CLI with no
  text-only mode, a logged-out one, or a timeout all fall back to exactly the old
  behaviour, because a commit that can't be described still has to be committable.
  The dialog reports why it couldn't and leaves your message box untouched.

  Generation happens at COMMIT time, never when the fast-track is armed: arming is
  arm-and-wait, so a message written from the tree as it looked at the press would
  describe a diff that no longer exists. A message you typed, and an intake
  ticket's own title, are never replaced.

- **Intake loads before you open it.** The tickets, PR and issue panels are warmed
  from app startup and kept warm, so Alt+I lands on a list instead of a spinner
  and the tab counts are already filled in. Skipped when no ticketing source is
  connected, and paused while the window is hidden.

### Fixed

- **"PR merged or closed" stopped crying wolf.** Both PR notifications — the
  browser/ntfy alert and the in-app toast — were inferred from the stage pill
  leaving or re-entering the `pr` rung. But the stage ladder drops to `agent` the
  moment the working tree goes dirty, before it has even looked the PR up, so an
  agent iterating on review feedback walked `pr → agent → committed → pushed → pr`
  on every single edit. Each lap announced that the pull request had been merged
  or closed, then that it had opened again, for a PR that never moved.

  A new `session.pr_state_changed` event carries the PR's own state instead, and
  both channels key off that, so each transition is announced once when it really
  happens. A lookup that fails — no `gh`, a rate limit, a network blip — is never
  read as a close; a first sighting seeds silently, so a restart cannot
  re-announce an already-merged PR; and switching branches re-seeds rather than
  reporting the previous branch's PR as closed.

### Changed

- **Expanding a pinned prompt no longer shoves the terminal.** The full prompt
  used to open in flow, which shrank the terminal container, refit xterm and
  resized the real tmux pane — so reading what a session was asked to do reflowed
  the output you were reading it against, and cost a PTY resize on every toggle.
  It now draws over the terminal instead, anchored on the bar itself so the first
  line lands exactly where the collapsed summary was, with no blank row and no
  duplicated line. The bar stays one line tall in both states, so the terminal
  never moves.

## [0.1.16] - 2026-08-10

### Fixed

- **The full-history page was invisible.** Its stylesheet was never added to the
  ordered `@import` list that builds the bundle's CSS, so `position: absolute;
  inset: 0` never reached the page and the overlay opened as an unstyled,
  zero-size block behind the terminal. Every way in — the drag gesture, the
  header button, `Ctrl+↑` — looked broken for the same reason. A test now fails
  on any stylesheet that nothing imports, because "invisible component, green
  CI" should not be able to happen twice.

- **Dragging up out of a terminal reaches the history.** The gesture only armed
  once the pointer was 8px *outside* the top edge, but a drag up ends where the
  selection stops growing — on the top row — and nothing suggests you should
  keep dragging into the pane header. Both edges now arm from just inside, as
  the bottom one already did; the hold delay is still what separates the
  gesture from an ordinary overshoot.

- **A prompt typed while the agent is working counts as a prompt.** Claude Code
  files those as queued entries and never re-files them as messages, so the
  pinned line showed an older prompt, expanding it re-showed the same truncated
  text, and the history page dropped the message outright — exactly the
  follow-ups you go back to read.

### Changed

- **New Session, rearranged.** Name and Folder up top, then three folds: **Git &
  workspace** open — those are the settings the dialog exists to set, and hiding
  the workspace strategy behind a click had people launch with the wrong one —
  with **Prompt** and **Launch flags** shut, since most sessions start with
  neither. The card is sized to hold that without scrolling and no larger.
  Folder suggestions keep one row per source and show whole chips only, instead
  of wrapping to three lines or clipping the last name down the middle. The
  folder browser's parent button is `←` rather than `↑`, matching every other
  file browser. `Ctrl+Enter` still creates; it just no longer says so.

## [0.1.15] - 2026-08-10

### Added

- **The prompt you last typed is pinned above every pane.** Glancing at a grid of
  windows told you what each agent was *saying*, never what it had been *asked*;
  the one line that explains a window is now the one line above it, with an arrow
  that opens the whole prompt. Scroll the agent back and the bar follows the
  section you are reading — Claude Code pins its own contextual `❯ …` row up
  there, and the bar mirrors that instead of the latest turn.

- **A pane's full history is a page you can read.** The agent TUI redraws in
  place, so tmux keeps no usable scrollback and a drag-selection could never
  reach past the top of the screen. `Ctrl+↑`/`Ctrl+↓`, the header's history
  button, or simply dragging past a terminal's edge now opens the whole
  conversation as ordinary text: scroll it, select across screenfuls, release to
  copy. A drag that crosses the edge is continued by the page rather than
  restarted, and it opens parked where you were looking.

- **A maintainer's Site traffic dashboard** (Settings, `--mindflock-dev` shell
  builds only) — GitHub stars over time, forks, per-release download counts, and
  `mindflock.ai/go/*` click totals on one screen, each section degrading on its
  own if an upstream is down.

### Fixed

- **Two windows on one repo no longer read each other's conversation.** Sibling
  sessions share a transcript directory, and everything derived from it — the
  pinned prompt, the latest-turn line, the history page — picked whichever
  conversation had been written to last, so the pair flickered between each
  other's work. Each window now reads the conversation its own activity hooks
  recorded for it.

- **Site traffic finds the GitHub token you already have.** It resolved only a
  pasted token or `$GH_TOKEN`, never `gh auth token`, so a machine authenticated
  the ordinary way (`gh auth login`) called GitHub anonymously — and the star
  history is the one section GitHub refuses to serve anonymously, 401 even for a
  public repo. It now uses the same resolution chain as the PR buttons.

## [0.1.14] - 2026-08-07

### Fixed

- **Fast-track switches itself off once its task ends.** A run that has succeeded
  or failed no longer reads as armed, so the toggle tells you whether anything is
  actually going to happen. The record is kept, so the outcome — and a halt's
  reason — stays readable, and pressing the button starts another run.

  Doing only that would have brought back the problem the run had been made to
  survive: a branch which already has an open PR satisfies a "PR" target the
  *instant* you arm it, so the run finished having accomplished nothing and the
  toggle flipped straight back off. A run is now only **finished** once it has
  actually carried the work — a commit or a step recorded — and one that has not
  acted stays armed and waits. "Already at the target" and "the job is done" had
  been the same state; separating them is what lets both behaviours hold at once.

- **A booting window can be dragged.** Provisioning is the state a window spends
  the longest in — a cold clone runs for minutes — and it was the only one that
  rendered no grip and neither half of the drag contract, so it could be neither
  moved nor dropped onto. Arranging the grid meant waiting it out.

## [0.1.13] - 2026-08-07

### Added

- **Fast-track — carry a session as far down commit → push → PR → merge as you
  want, then stop.** A `⏩` toggle beside each window's guided button arms a
  standing instruction; the Commit dialog gained a "Then keep going" rung so one
  press can commit *and* continue; and **Settings → Workspace** sets where it stops
  by default (Open PR). Pressing it while the agent is still working is the normal
  case: it waits for the agent to genuinely finish rather than committing a
  half-written tree. The chain is driven server-side, so it survives a tab close,
  a page reload and a server restart — and because every step is re-derived from
  real git state rather than replayed from a script, resuming is the same code path
  as starting.

- **The same depth on intake, so a ticket can go from ingestion to a PR
  unattended.** Each source (tickets / PRs / issues) carries a default depth, and
  an individual row can override it before you start it. A whole source cannot
  default to *merge* — that default would apply to every future item with nobody
  watching, and merging is the one rung that cannot be undone — but an individual
  item can.

- **Retryable pre-commit hooks.** Some hooks fail without changing any files and
  will fail identically forever — an index-rebuilding hook with a corrupt index is
  the motivating case — which blocked the commit permanently. Listing a hook **ID**
  in **Settings → Workspace** lets fast-track retry it once and then re-run the
  commit with `SKIP=<id>`, saying which hook it skipped. Test and secret-scanning
  hooks are refused there and enforced in the driver, so a failing test still stops
  the run, which is the whole point of having one.

- **A live step indicator in each window's header.** It names the step that is
  actually happening — setting up, pre-commit, checks, pushing, opening a PR,
  merging — and, when a chain is armed, what it is waiting on in the server's own
  words and which rung it is heading to.

- **The Merge button refuses a merge GitHub would reject.** It reads real
  mergeability and goes unclickable with the blocker named — merge conflicts, a
  branch behind its base, a draft PR, a failing or pending required check, a
  missing review — instead of offering a button that just produces GitHub's error.
  When mergeability cannot be determined at all (no token, no `gh`, a network
  fault) the button is left exactly as it was: not knowing is not the same as no.

### Changed

- **Stage changes show up when they happen instead of at the next poll.** `GET
  /api/instances` serves the background tick's snapshot for up to ten seconds and
  never rebuilds probes inline, so the post-action refresh provably could not
  observe anything — "Push" appeared seconds after a commit finished. The publisher
  now computes its own stage rather than serving a memo another ticker filled, a
  new single-session read publishes through, and a bounded 250 ms watcher republishes
  the moment a commit or push actually lands.

- **Recently closed shows the whole branch name.** The row showed only the session
  title, which for an ingested ticket is the bare slug — so the list said nothing
  about what any of the work was.

- **A long window title truncates instead of clipping the header controls.** A
  60-character session name used to slice the Commit button, the fast-track toggle
  and the step indicator off the right edge. The title yields first now; it is a
  label whose full text is a hover away, and those are controls.

### Fixed

- **The guided button no longer sticks on "Commit…" for the rest of the session.**
  The pin set after a successful **Make PR** had no reconcile, so once a session
  had opened one PR its button never offered Push again for the life of the page.

- **A commit no longer inherits an unrelated message.** The saved message survived
  a *successful* commit, so anything committing without an explicit one adopted a
  stale subject — caught about to record one feature's files under a message
  describing a database migration. It is cleared once a commit lands, and only
  reused while a failure is genuinely pending.

- **Autopilot never pushes the base branch.** A run on `main` pushed straight to
  `origin/main`, which a bypass-silent ruleset accepts while merely noting that a
  PR was required. Committing there is still fine; going further needs a branch.

- **Two servers sharing one machine no longer fight over a run.** Each treated the
  other's presence as a restart and reset the "is the agent done" dwell every pass,
  so a chain could sit forever without acting — and both would otherwise have
  acted, double-committing and double-pushing the same worktree. One lease, one
  driver.

- **A PR no longer flaps in and out of its stage.** A failed lookup was cached as
  "there is no PR", so a single rate limit or network blip dropped the stage and the
  next poll restored it — re-announcing "PR merged or closed" every minute for a PR
  that was open the whole time.

- **The ticket-ingestion dot is a circle when it is red.** Its error state borrowed
  a class name from the app-wide rule for error *text*, whose `min-height` beat the
  dot's own height and rendered it as an oval — in exactly that one state.

## [0.1.12] - 2026-08-05

### Added

- **`mindflock init` — a guided first-run setup.** Getting to a first session
  meant discovering and sequencing four commands from the README. `init` runs the
  doctor, offers to run each fix for you (including logging your agent CLI in),
  then shows the git repos it found on this machine as a numbered list and
  remembers the one you pick, so the New Session dialog opens on it. `mindflock
  serve --setup` runs the same wizard before binding.

- **A first `serve` now prints what this machine actually needs.** It used to
  print the same three-step blurb to everyone; it now names the dependencies that
  are missing with their one-line fixes, lists the folders you could work in, and
  gives you the exact next command. It builds that off the main thread — a first
  run is the desktop app's very first launch, which is polling for the port.

- **The New Session dialog offers the repos you actually use.** A new
  `GET /api/repos/suggest` ranks the folder your last session used, the live
  sessions' repos, the server's own working directory, and a shallow scan for git
  repos under your home — and the dialog pre-selects the top one as a chip row
  instead of dropping you in `$HOME` to go hunting. `GET /api/repos/check` backs a
  quiet line that appears when the chosen folder is *not* a git repo, saying diff,
  commit and PR will be off, with a one-click "create one here" that drives the
  checkbox that was buried in **More options**.

### Changed

- **The folder browser selects like Finder.** A click used to navigate and only
  the per-row **select** button committed a folder, which read backwards. A single
  click now selects (and shows the folder in the field), a double click descends,
  the redundant button is gone, and Escape cancels the whole browse rather than
  leaving the field on the last directory you merely passed through.

- **The doctor's agent-auth check now covers every coding CLI, not just Claude.**
  It reported "no auth probe for `codex` — skipped" for anything else, so a user
  on Codex, opencode or aider learned their CLI was logged out when their first
  session failed. It reads each provider's own declared credential locations now.
  A provider that declares none stays quiet — absence of evidence is not evidence
  of absence — and it only offers to run a login command the provider actually
  declares, so `doctor --fix` can no longer drop you into an agent REPL that
  cannot resolve the check.

- **One first-run signal instead of three.** The welcome tour and the setup
  checklist each kept their own browser-local flag beside the server's
  `onboarded`, so clearing a profile or opening the desktop app on a second
  machine replayed the whole 12-slide tour at someone who plainly was not new. The
  server's flag is the only rule now, and it still means what it always meant: you
  have created a session.

### Fixed

- **A todo you clicked to edit closed again immediately.** The text box opened and
  reverted within a frame: the poll callback's dependencies changed the moment an
  edit began, which re-ran the effect that focuses the Add box, and that blur
  committed the edit before you could type. The initial focus is tied to the
  dialog opening now, not to unrelated re-renders.

- **The doctor told logged-in macOS users they were logged out.** Claude Code
  keeps its credentials in the login Keychain there rather than in
  `~/.claude/.credentials.json`, which the probe never looked at — so every Mac
  saw "CLI is installed but no sign of a login was found" on every run. It checks
  the Keychain now, after the cheap file reads, so it never shells out to
  `security` when a file already answered.

- **The sidebar's resize handle painted over every open dialog.** The `.modal`
  backdrop is fixed with no `z-index` of its own, so the handle's `z-index: 40`
  drew its accent line across the dimmed page and kept answering `:hover` — and
  the drag still worked through the backdrop, resizing a sidebar nobody could
  see. It hides while any modal is mounted, keyed off the overlay element so the
  command palette and the welcome tour are covered too.

## [0.1.11] - 2026-08-04

### Changed

- **The Intake tabs dropped the status line under their master switch, and
  tightened up.** "● Active — polling 1 source for tickets assigned to you" sat in
  a row of its own, under a switch that was already green, directly above the very
  sources it was counting — it restated what was on screen in a third colour and
  cost a full row in each of the three tabs. The switch is a banded row now, and
  says something only when the tab has nothing to switch on yet ("Add a repository
  below and this starts reviewing your PRs on it") — the one state a switch cannot
  show by itself.

  The dialog also sizes to its content instead of a flat `84vh`, which drew an
  empty box under any tab that ended early (Issues is 581px now, not a forced
  760). Each tab's sections — **Sources**, **Repositories**, **Assigned
  tickets**, **Open issues** — read as headings rather than another line of prose,
  and workflow-state rows print in the provider's own casing (`Ready for Review`,
  not `READY FOR REVIEW`), so on this surface uppercase means exactly one thing: a
  section heading.

### Fixed

- **The Intake master switch's label sat 10px above its own toggle.** It reused
  `.set-switch-row`, which nudges its label up to compensate for the desktop app's
  font; on a row whose label is already centered against a 20px switch, the nudge
  just pulled the two apart.

- **The Intake top-bar button announced itself as "Open work"** to a screen
  reader — the surface's name two renames ago, so the spoken label matched nothing
  on screen. The visible "Intake" is its accessible name now.

- **A commit message the pre-commit hooks rejected is offered back, not retyped.**
  It always survived on disk — `.mindflock_commit_msg` is the file
  `git commit -F` reads, and a re-commit with no message reuses it — but the
  dialog pre-filled from an in-memory map that a page reload empties, and a
  server restart prompts exactly that reload. New
  `GET /api/instances/{title}/commit-message` hands it back, gated on
  `.mindflock_commit_status` recording a failure so a *successful* commit's
  message never pre-fills the next one.

- **Clicking outside the Commit dialog closes it**, like Escape already did,
  instead of requiring the Cancel button. It closes on `mousedown` on the
  backdrop itself, so a text selection that starts in the message box and
  overshoots can't discard what you typed.

## [0.1.10] - 2026-08-04

### Added

- **Ticketing, PR review and issue handling moved out of Settings into a new
  top-bar surface: Intake.** They were never settings. Settings is where you
  configure the app once and forget it; these three are somewhere you *visit* —
  to see what came in, start something by hand, or pause a queue. Sitting in the
  same left nav as Appearance and System logs, they were both hard to find and
  mixed in with things that never change.

  **Intake** sits next to **New** in the top bar (`Alt+I`, plus four command-palette
  entries), and inside it a tab strip — **Tickets**, **Pull requests**, **Issues** —
  carries a live count per tab — the number of rows that tab will actually show
  you, not a lifetime total: counting every ticket the provider ever assigned you
  read `1221` over a list of 52, because done states like Completed are parked
  behind the **+ Add bucket…** menu. The badge and the panel now share one bucket
  filter so they cannot disagree. Every
  old deep link still works: the retired screen keys (`ticketing`, `repo`, `issues`)
  route to the tab that replaced them, so the sidebar bars' buttons, the welcome
  tour's "Set up now →", and the server's own `settings_screen` on a Connections
  card all land in the right place.

- **All three tabs are the same screen now.** They had drifted into three dialects
  of one shape, and Ticketing had the good one. The shared anatomy is: master
  switch → status line → a list of collapsible **source cards** with `+ Add` → the
  **work those sources yielded, grouped by source**, each row saying why automatic
  pickup did or didn't take it.

- **Each watched repository is its own card, with its own settings.** PR review and
  issue handling used to show a flat chip list of `owner/name` strings plus one
  screen-wide set of options, so "watch this one too, but give it half an hour of
  grace and run it on codex" was not expressible — there was nowhere to hang a
  per-repo value. Now each card carries its own **Agent CLI**, **base branch** (PR
  review), **min age**, **skip authors**, a **Test access** button and Remove. The
  tab-wide fields moved into *Advanced options* and are the defaults a blank card
  field inherits, shown as its placeholder so "empty" reads as "inherit 15" rather
  than "no grace period at all". Stored as `github.repo_settings` /
  `github.issue_repo_settings`, keyed by repo slug; an absent key inherits. The
  monitors resolve every filter through those (`GithubConfig.min_age_for`,
  `base_branch_for`, `skip_authors_for`, `agent_for_repo` and the issue twins), so a
  card never saves a value the pipeline ignores.

- **Assigned tickets group by ticketing source, then by workflow state.** One flat
  level of buckets merged same-named states from different sources: a Jira site and
  a Linear workspace both have an "In Progress", and under one heading there was no
  way to tell which ticket came from where without reading each row. Bucket
  show/hide stays an app-wide choice ("I don't care about Completed" means it
  everywhere) while open/closed is per source. `GET /api/tickets` gained a
  `source_labels` map covering *every* configured source, including ones that
  returned nothing or failed — deriving labels from the ticket rows alone made
  exactly those sources vanish.

  Grouping is unconditional, on every tab: "which provider is this from?" is the
  question a queue answers before any other, and a heading answers it once for
  however many rows sit under it. The per-row label that was tried instead does
  not scale — the same name repeated down a column of 500.

- **Ticket states nest under their workflow, instead of repeating it.** A source
  with several workflows has to qualify its state names to keep them unique, so
  Shortcut returns `Product Development · Deferred`, `Product Development ·
  Unscheduled`, and five more — and a flat list wrote `PRODUCT DEVELOPMENT ·`
  onto seven consecutive headings. `GET /api/tickets` now carries a `bucket_meta`
  map (`{bucket: {group, label}}`) and the panel renders **source → workflow →
  state**, each name written once. The composite name stays the bucket key, so
  `+ Add bucket…` / ✕ and the done-state filter are unaffected; a provider with
  one workflow (or none — Jira, Linear) reports an empty `group` and the level
  simply doesn't appear. See `TicketProvider.list_states` for the contract.

  Each heading in that tree is also styled as the control it is — hit area, hover
  fill, and a caret that rotates instead of swapping ▸ for ▾. As plain text
  beside a small caret, under a *bordered* source card, the top-level heading
  read as a stray line of copy rather than the way in to the tickets.

- **The tickets panel's hint describes *your* workflow, not an imagined one.** It
  said "done states like Completed start hidden", which is a guess — a Shortcut
  flock has `Product Development · Won't do`, a Jira one has `Closed`. It now
  names the states actually hidden, with matching singular/plural. And the
  auto-ingest sentence no longer merges sources: one source watching `Ready`
  alongside another watching `Todo` was reported as "watches Ready, Todo", which
  was false for both of them, since neither watched both. With several sources it
  points at the per-source headings, which each carry their own.

- **Any queued item can be started on a different coding CLI, just for that
  launch.** A picker beside **Begin work** / **Begin review** / **Start work**, next
  to the button rather than buried in configuration: you notice mid-review that
  this one wants a different model, and re-configuring the whole queue to run one
  item is the wrong shape of action. The three force-start routes take an optional
  `agent`; an unknown name is a 400 rather than a silent fall back to the default.

- **A per-repository GitHub access test** (`POST /api/settings/test/github-repo`),
  behind each repo card's **Test access**. The existing `/settings/test/github`
  answers "is there a credential"; this one answers "does it reach THIS repo",
  which is the failure people actually hit — a typo'd slug, or a private repo the
  token has no scope for. It also reports a read-only token, which matters for
  issue handling (that half has to push a branch).

### Fixed

- **Switching a ticketing source's Agent CLI kept launching the old one.** The
  pipeline reads its config once at boot and the scanners hold that snapshot, so
  stamping a ticket with `source.agent` from it meant a provider switched in the UI
  applied only after a restart — and *clearing* the field did nothing at all. The
  agent is now re-read from disk at stamp time (`source_agent_now`), for both freshly
  scanned tickets and ones replayed from a previous run's pending queue. An on-disk
  config with no opinion is treated as an answer ("use the app default"), which is
  what makes clearing the field work.

- **The same staleness one layer up: clearing PR review's or issue handling's
  Agent CLI did nothing on a running pipeline.** Those chains resolved through
  `fresh_agent`, which falls back to the config the runner was *constructed* with
  whenever the on-disk chain answers "nothing set" — so the boot-time provider
  came back every time. They now use `agent_now`, where an empty on-disk answer is
  an answer ("use the app default") and the snapshot is consulted only when the
  config cannot be read at all.

- **Forced starts ignored the agent their source was configured for.** A review or
  issue started by hand now resolves the same chain the automatic monitor does —
  this start's own pick, then the repo card, then the tab-wide value, then the app
  default — instead of jumping straight to the app default.

- **A newly pasted GitHub token did not take effect until a restart.** The
  resolved token is cached for the life of the process, and nothing invalidated
  it, so PR review, issue handling, **Make PR** and the new per-repo test all kept
  using the old credential — which reads exactly like the paste not having saved.
  Writing `github.token` now clears the cache, and the per-repo **Test access**
  resolves afresh, since answering about the previous token would be worse than
  not testing at all.

- **The open-PR list hid pull requests it should have explained.** With a base
  branch configured, the panel asked GitHub only for PRs into that branch, so a PR
  into any other one was simply absent — indistinguishable from "there are none",
  and impossible to force-review. It now lists every open PR and reports the
  mismatch as a skip-reason chip.

- **PR review's "Skip authors" field described itself wrongly.** It said "GitHub
  logins whose PRs are ignored", but review only ever takes your *own* PRs — the
  list drops review *comments*, which is how you stop a bot's feedback being acted
  on. The label now says what the code does.

- **One long failure reason dragged every Intake row sideways.** A recorded
  failure is a sentence of git output carrying a branch name and an absolute
  worktree path, and the chip meant to contain it capped itself at
  `max-width: 100%` — which is circular: a work row is a grid whose first track
  was `1fr`, whose automatic minimum is its content's min-content width, and a
  `nowrap` chip has no smaller width. The track had already grown, so 100% *was*
  the oversized width and the start buttons ended up off the right edge. The
  track and the meta line may now be narrower than their content, and a long
  reason wraps instead of ellipsizing on one line — the remedy in these messages
  ("Kill that session first") is at the end, so a one-line clip hid the half
  worth reading. The tooltip still carries the raw string.

## [0.1.9] - 2026-08-03

### Fixed

- **The default coding provider was ignored by everything that launches a
  session.** Settings → Coding provider writes `coding_cli.default_provider`
  into `settings.json`, but every launch path read `default_program` out of
  `config.json` — a different store, seeded once on first run by a helper that
  only ever hunts for `claude`, and which the UI never writes to. Nothing
  bridged the two, so choosing a default changed the Providers screen badge and
  `mindflock doctor` and nothing else: ingested tickets, PR-review sessions,
  issue sessions and the New Session dialog all still launched Claude Code.

  `backend.config.program.resolve_default_program` is now the single answer to
  "which CLI, when nobody asked for a specific one" — the chosen provider, then
  the engine config, then `claude` — and the engine bridge, the standalone tmux
  launcher and the web layer all resolve through it. It is also read fresh
  rather than from the config snapshot taken at server start, so changing the
  default no longer needs a restart. An explicitly chosen agent (a ticketing
  source's own, say) still outranks it.

- **Two welcome-tour slides scrolled.** "Welcome to MindFlock" needed 327px and
  "3. PR review" 394px inside a 320px content box, so both got a scrollbar and
  the PR-review slide hid its own *Set up now* button. The card is now 520×490
  with a 48ch body — widening did most of the work, taking that slide from 394px
  to 350px — which fits all twelve with room to spare.

- **The per-surface Agent CLI pickers chose a provider that nothing read.**
  0.1.9 added `github.agent` and `github.issue_agent` so PR review and issue
  handling could each run their own coding CLI, and the settings round-tripped
  correctly — but `_parse_github` built its `GithubConfig` without passing either
  field, so both sat at their `""` dataclass default however they were
  configured. The merged config computed the right value and then threw it away
  one line before anything could read it. `pr_agent()` and `issue_agent()` were
  correct and unreachable, so every review and every issue session fell through
  to the app-wide default provider.

  The gap survived the suite because the precedence logic was tested against a
  hand-built `GithubConfig` and the storage against `GithubSettings`, with
  nothing exercising the parse between them. That seam is now covered.

- **"Begin review" ignored the Agent CLI setting directly above it.** The forced
  PR-review route passed `ENGINE.default_program()` outright, so the dropdown in
  Settings → PR review governed only the auto monitor: clicking the button
  launched the app-wide default. It now resolves through `pr_review.review_agent()`,
  the same `pr_agent()` chain the monitor uses. The issue equivalent had the
  matching bug one level up — `prepare_start` read the ingestion-wide
  `agent_for()`, which skips straight past `github.issue_agent`.

- **Switching provider needed a pipeline restart to take effect.** The ingestion
  pipeline calls `load_config()` once at startup and holds that snapshot for the
  life of the process, which is right for poll intervals and tokens and wrong for
  "which CLI should this session run": a provider chosen in Settings was invisible
  until the pipeline was restarted. Agent choice is now re-read at the moment of
  launch — the rule `resolve_default_program` already followed for the app-wide
  default — via `config_for_launch` / `fresh_agent`, across tickets, issues and
  both PR runners. An injected config stays the fallback rather than being
  discarded, so a caller that builds one by hand still has it honoured.

- **A ticket that had run before could not be run again.** Ending a session
  deliberately keeps its worktree, so a second run of the same ticket met a
  leftover worktree still holding the feature branch and died in
  `git worktree add` with "already checked out at …". Nothing in the UI could see
  it: with no live session the panel enabled **Run ticket**, and the failure was
  then recorded with advice to delete the `state.json` ledger entry — which
  clears the *record* of the failure and leaves its cause in place, so the retry
  failed identically.

  Force-start now reclaims the leftover first, under two rules it never breaks:
  it never touches a worktree a live session owns, and it never removes one
  holding work (uncommitted changes, a stash, or unpushed commits). Anything it
  declines to take keeps the previous behaviour, so the worst case is the error
  message you already got.

- **A recorded failure now says what went wrong.** The ledger stored a
  `failure_reason` — "branch '…' is already checked out at `<path>`" — and the
  loader dropped it, leaving the panel to show a generic chip whose suggested
  remedy fixed nothing. The reason is carried through and shown in full (chip
  ellipsizes, tooltip has all of it; the actionable half of these messages is at
  the end, so clipping server-side removed exactly the part worth reading).

### Added

- **PR review, issue handling and the Assistant each pick their own agent CLI**
  (`github.agent`, `github.issue_agent`, `coding_cli.assistant_provider`), the
  way a ticketing source already could. Issue handling deliberately does *not*
  inherit PR review's choice and vice versa: they are separately configured
  features with separate repo lists and separate toggles, so inheriting would
  surprise anyone who set one and not the other. Both fall back to
  `[mindflock].agent`, then the resolved default.

  The Assistant needed this most — it was hardcoded to `claude` in two places,
  which made it unusable for anyone who had never set Claude up. Its picker
  lives in the Agent file dialog next to the standing instructions.

### Changed

- **PR review, Git issues and Ticketing are one screen with different nouns.**
  They had drifted into three dialects of the same layout, and every incidental
  difference cost a reader a moment working out whether it meant something. All
  three now read the same way top to bottom — switch, status line, what's
  watched, agent CLI, open-work panel, Advanced — from shared pieces in
  `settings/screens/automation.tsx`. Ticketing gains the status line it never
  had (its toggle had no feedback at all) and its per-source field is relabelled
  **Agent CLI** to match its neighbours.

- **The agent picker is a top-level field, not an Advanced one**, on every
  screen that has one. Which CLI does the work is not a tuning knob.

- A collapsed ticketing source now always names the CLI it runs
  (`Shortcut — Allure Security · claude (default)`). Naming it only when it was
  overridden made "on the app default" indistinguishable from "this source has
  no agent setting", which is what sent people hunting for the picker.

## [0.1.8] - 2026-08-03

### Added

- **Ingestion runs any agent CLI, not just Claude Code.** MindFlock has always
  let a hand-started session pick its CLI, but an *ingested* one couldn't:
  the generated workspace launcher hardcoded Claude Code's four spellings, so a
  provisioned session on anything else was started with flags that CLI rejects
  (`aider --dangerously-skip-permissions "<prompt>"`, resumed with
  `aider --continue`). That contradicted the provider-agnostic pitch and made a
  paid Claude subscription a hard dependency of the whole ticket pipeline.

  The launcher is now provider-neutral: the skip-permissions flag, how the seed
  prompt is passed, the resume flag, the entry subcommand and the clean-quit exit
  codes all come from the launching provider (`LauncherSpec`). codex resumes with
  `resume --last`, goose opens `goose session -r`, aider gets `--yes-always` and
  `--restore-chat-history` with no retry chain, and an unrecognized program has
  nothing invented for it. For `claude` the generated bytes are unchanged.

  Each ticketing source picks its own agent (Settings → Ticketing → **Agent**, or
  `[[ticketing.source]].agent`), falling back to a pipeline-wide
  `[mindflock].agent` and then the app default — so an existing install resolves
  exactly as before, and a flock can route one queue to a hosted CLI and another
  to a local model. A typo'd agent name is a config error at load time, listing
  the valid ones, instead of a session dying on a shell "command not found".

  The CLIs that take their first instruction interactively (aider, goose,
  opencode, cline) are seeded by pasting the prompt into the pane once its TUI has
  drawn — through a bracketed tmux paste, so a multi-line ticket arrives as one
  block instead of being submitted a line at a time. Only ever on a first launch;
  a resumed session is never re-seeded.

- **Run everything on a local model — no subscription, nothing leaves the
  machine.** Point sessions at a model you serve yourself (**Ollama**, **LM
  Studio**, or any OpenAI-compatible server such as llama.cpp, vLLM or a LiteLLM
  proxy) from Settings → **Local model**. It is a runtime overlay, not a new
  provider, so it applies to every launch path — a fresh session, an ingested
  ticket, a PR-review session and a post-reboot relaunch alike — and the registry
  and your own provider TOMLs are untouched.

  Per-CLI routing is verified rather than guessed: codex via its own
  `--oss --local-provider`, aider via `OLLAMA_API_BASE` / `LM_STUDIO_API_BASE`
  with the model prefixes its docs specify, goose via `GOOSE_PROVIDER` /
  `GOOSE_MODEL`. **Test connection** proves the server answers, lists the models
  it actually serves (so the model field becomes a dropdown instead of "type the
  exact tag"), and names which of your installed CLIs can be pointed at it.
  Claude Code speaks only the Anthropic API, so it has no local route — the
  screen and `mindflock doctor` both say so out loud, because a session quietly
  using its hosted API is the one outcome a privacy guarantee can't be quiet
  about. Off by default, and off is an exact no-op on every launch path.

### Changed

- **GitHub Issues is the zero-config on-ramp.** It now leads the provider catalog
  (so a newly added source starts there) and needs **no fields at all**: the token
  comes from your existing GitHub connection or `gh auth token`, and the
  repository resolves through the source's Repo URL, then `[repository].url`, then
  this checkout's `origin`. On a machine sitting in a GitHub clone, picking
  "GitHub Issues" and saving is the entire setup. **Test connection** reports the
  repo it resolved to, so "zero config" never means an empty field you have to
  trust; when nothing names a repo, the error says what to fill in instead of
  failing mid-poll.

- **The sidebar is the width you need it to be.** It was a fixed 260px, so long
  session labels ellipsized while the agent panes had width to spare — or the
  reverse on a wide screen. It has a drag handle now (arrow keys too), with the
  width persisted across reloads. Session labels stop being truncated in JS along
  with it: the old 20-character budget on the feature name was always a guess at
  how much fits, which is a CSS question, and one whose answer now changes as you
  drag.

- **The pre-commit stage pill stays a pill.** It badges a `failed_step` only when
  that value is actually pill-sized; the server's generic fallback can return a
  whole line of hook output (up to 80 chars), which either overflowed the row or
  forced the chip itself to be truncated. Long details ride in the tooltip
  instead. That fallback was also picking the wrong text — `capture-pane` without
  `-J` returns one line per pane *row*, so a wrapped error got split and the
  "last error-looking line" was its tail, leaving the badge reading as a mid-word
  fragment (`ffic-reduction'.` out of `…'traffic-reduction'.`).

- **The project says what it is: a _private_ flock, on your own machine.** New
  README section with the full network inventory — everything MindFlock itself
  talks to, when, and what it sends — and the honest boundary stated plainly:
  your agent CLI still calls its own vendor, and with a local model you can close
  even that.

## [0.1.7] - 2026-07-31

### Added

- **Your phone URL now travels with the notifications.** When Tailscale is up,
  every ntfy push carries this machine's tailnet `/m` address — in the message
  and as the tap target — deep-linked to the session it is about (`/m?s=<title>`),
  so a notification is one tap from the thing it's telling you about instead of
  the start of a hunt for the URL. On top of that, one push announces the URL
  whenever it becomes newly reachable: at server start, when the ntfy channel is
  switched on, and when tailscale mode is turned on. The access token is never
  included — the message is stored on a third-party server, so it goes bare and
  says that a new device will meet the sign-in page. Deduplicated (turning on
  push and tailscale mode back to back is one intent), and a URL that isn't live
  until a restart says so. *Tapping opens* stays available for sending taps
  somewhere else entirely.

- **A Diff tab in the phone UI.** `/m` gains a third tab beside Agent and Shell,
  reading the same `GET /api/instances/<title>/diff` as the desktop Diff tab
  with the same two baselines behind one button (**All changes** vs
  **Uncommitted**, persisted under the shared `mf_diffbase` key). Unified and
  colorized like the desktop, with each file's name lifted into a header that
  sticks while its hunks scroll past, sideways scrolling contained inside the
  panel, and a cap for very large diffs. The terminal stays attached behind it,
  so switching back is instant — and the git action bar (with its status toast)
  stays on screen, because reading the diff is exactly when you decide to push.
  A phone can now approve work, not just unblock it.

- **Tailscale mode applies itself.** Which interface uvicorn binds is fixed at
  process start, so the Settings → Mobile toggle only ever meant something after
  a restart. Turning it on now takes that restart: `POST /api/settings` answers
  `{"restarting": true}` and the screen waits for the server to come back and
  refreshes the URLs/QR in place. Bounded at three attempts (counted in the
  environment, since each attempt is a new process image) before it gives up and
  leaves the manual button, rather than restart-looping a server that isn't
  going to come up on the tailnet. The same check runs at every boot, so a
  server started in local mode while the setting says tailscale corrects itself.

- **Sessions that run out of usage get picked back up.** The prompt queue has
  always ridden out a usage limit for sessions with something queued; a session
  that simply ran out mid-task had an empty queue, so nothing was watching it —
  it sat on its CLI's limit screen until a human came back, often hours after
  the window reopened. The drain pass now also walks the sessions whose activity
  is `limit` (a free read of the state snapshot — no probes when nothing has run
  out), waits the window out with the same meter/banner logic, and sends
  Esc + `continue` so the agent resumes its task. New setting
  `general.resume_on_usage_reset` (Settings → General, default on).

  Two notification rules go with it, both default-on and mutable like the rest:
  **"A session runs out of usage"** and **"Usage comes back after running out"**.
  The second rides on a new `session.usage_restored` event, emitted once per
  reopening by that watcher — so it cannot fire for a window that merely rolled
  over while nothing was blocked, and several sessions unblocking together still
  make one notification.

## [0.1.6] - 2026-07-30

### Added

- **Optional ntfy push, so notifications reach your phone.** Until now the only
  notification channel was the browser's `Notification` popup — which needs a
  MindFlock tab open on a secure origin, exactly the situation you're not in when
  you'd most like to know that a session is waiting on you. Settings →
  Notifications gains a **Phone push (ntfy)** section: give it a topic
  (**Generate** offers a random one), scan the QR into the free
  [ntfy](https://ntfy.sh) app, and the **server** publishes matching events to
  it — no tab, no browser, no MindFlock window required. **Send a test**
  confirms the round trip and the row afterwards reports the last push or why it
  failed. Off until you turn it on, and pointable at your own ntfy instance
  (**Server**) with a token for a protected topic.

  The existing per-rule switches are now labelled as governing *both* channels —
  one "what notifies me" list, with channels deciding only where an alert lands.
  Per-rule ntfy priority means the actionable rules (needs-input, budget
  exceeded, pre-commit failed) buzz at priority 4 while the ambient opt-ins
  (idle, pre-commit running) arrive quietly at 2.

  New endpoints `GET|POST /api/notify/ntfy` and `POST /api/notify/ntfy/test`
  (see [docs/web-api.md](docs/web-api.md)); new settings
  `notifications.ntfy_enabled` / `_server` / `_topic` / `_token` / `_click_url`,
  resolved through env → `settings.json` → defaults so a headless box can just
  export `MINDFLOCK_NTFY_TOPIC` (an env topic is an implicit opt-in — there is no
  Settings screen there to flip a switch in).

  Care taken with the parts that could leak: the token is masked on read and
  kept on an empty write like every other secret, **and** is dropped when the
  server URL is retargeted at a different host without a fresh token, so one
  server's credential is never handed to another. A `token=` query parameter in
  the optional tap-to-open URL is stripped, because that URL is stored on the
  ntfy server. Nothing logs the topic name (`mindflock.log` is served back out
  over `GET /api/logs`, and on the public server the topic *is* the credential —
  hence the random default and the on-screen warning while pointed at
  `ntfy.sh`). Pushes are capped at 60/hour per process so a flapping session
  can't burn a quota, and delivery is best-effort throughout: a failed push is
  recorded for the settings screen and never touches the event that triggered it.

## [0.1.5] - 2026-07-30

### Changed

- **SSH remotes work, and the GitHub CLI is genuinely optional.** `gh` was
  declared optional in 0.1.1, but the code and the docs had not caught up: the
  engine's push ran `gh repo sync` with `git push` only as a fallback, the
  make-PR and merge endpoints refused with "GitHub CLI (gh) is not installed",
  and the README listed `gh` above the Optional row while doctor, CONTRIBUTING
  and the installer each said something different. A contributor whose git
  config uses SSH could not push at all. Now: **pushing is always plain
  `git push -u origin <branch>` over whatever remote your repo already has** —
  SSH or HTTPS, used verbatim, with your own git credentials, and MindFlock
  never rewrites a remote URL (so `url.<base>.insteadOf` still applies). Only
  **Make PR** and **Merge** need to reach the GitHub API, and each now resolves
  in three tiers: `gh` when it is installed *and* authenticated, then the GitHub
  REST API with a resolved token, then a prefilled compare/PR URL handed to your
  browser — `POST /api/instances/{title}/make-pr` returns `200 {ok: false,
  compare_url}` rather than a 400, and no response is ever just "gh is not
  installed". When a credential is genuinely needed the app prints one sentence:
  *add a GitHub token in Settings → PR review, or install the GitHub CLI*.
  Doctor's missing-`gh` line now reads
  `not found (optional — only PR create/merge and PR review need it; pushing uses plain git)`
  instead of "push/PR steps will fail"; `GET /api/config` gains
  `caps.github` (true when either credential exists). Remote URLs of every
  spelling — `https://`, `ssh://`, `ssh://host:22/…`, scp-style
  `git@host:owner/repo.git`, `git://`, and local paths — go through one parser
  (`backend/session/git/remote_url.py`), and a new `[repository].git_transport`
  setting (`auto` | `ssh` | `https`, default `auto`) picks the form used when
  MindFlock has to *build* a clone URL from an `owner/repo` slug: `auto` matches
  the transport of your own `[repository].url` for that repo, and an explicit
  value always wins. CI's cold-install job no longer installs `gh`, so a
  gh-forcing regression now fails the build instead of shipping.
- **The pitch, everywhere: MindFlock turns your ticket queue into a queue of
  pull requests.** The README, the website and the package description used to
  lead with parallel agent supervision — a crowded category — and buried ticket
  ingestion in bullet six. They now lead with the thing nothing else does: work
  assigned to you in Jira, Linear, GitHub Issues, Shortcut or Asana becomes an
  isolated session with nothing typed, and you review the diff and drive it
  home. Worktree isolation, tmux and the grid are described as *how* it works.
  The README also states plainly what is **not** automatic (no commit, push, PR
  or merge without your click; no writes to your tracker; polling, not
  webhooks).
- **The evidence is now visual, on both surfaces.** The README and the website's
  numbers section lead with the result — 6.3× more reviewed source per half-hour at
  the keyboard — instead of with the flat metric that makes the *method* interesting,
  and both now carry the charts: a 24-month trend showing the ticket rate holding
  while diff size and test coverage climb, and a second showing that the average day
  never changed while the ceiling on work in flight went from 16 branches to 31. That
  second chart is the clearest argument for the app rather than for agents in general,
  and it was previously nowhere.
- **The first published productivity figures, measured properly.** An earlier
  draft leaned on volume counts, which turned out to prove nothing: pull requests
  per month actually went *down*, and Shortcut's start→done clock got *longer*
  (it measures review, QA and deploy queues that no coding tool touches). Three
  eras of one repository — before agents, one agent at a time, and a flock —
  recomputed from the git graph and the Shortcut API instead: the ticket rate
  barely moved (43 → 55/month) while the median ticket went from **114 source
  lines across 4 files to 979 across 13**, pull requests touching tests went from
  **5% to 88%**, peak branches in flight went from 16 to 31, and reviewed source
  per half-hour-with-a-commit went up **6.3×**. Source files only (lockfiles,
  generated files, images and DB dumps excluded — under 1% of recent lines),
  medians not means, because a single 1.6 M-line bulk import would otherwise
  dominate every average. The method is documented in the README so the figures
  can be re-derived.
- **Ticket sessions land in the app by default.** `[mindflock].enabled` now
  defaults to `true`, and is exposed in Settings → Advanced instead of being
  file-only. Previously a fresh install that connected a tracker got detached
  tmux sessions and OS terminal tabs — no stage badge, no guided git bar — until
  it found an undocumented config flag. The engine bridge is in-process (it does
  not need a running server, and behaves the same headless); if it is ever
  unimportable the pipeline falls back to the standalone path with a warning
  naming what was lost.
- **Both ways in are stated as first-class.** Leading with ingestion is right —
  nothing else does it — but the README now says plainly that MindFlock is also a
  parallel-agent workspace you drive by hand (`+ New`, or `mindflock new`), that a
  tracker is a source of sessions rather than a requirement for them, and that a
  hand-started session runs whichever agent CLI you point it at. The website gets
  a section of its own for it.
- **The Ticket Ingestion bar is visible out of the box** (`DEFAULT_VISIBLE_BARS`),
  so the flagship feature is no longer hidden behind ⚙ Customize. The first-run
  footer hint now points at the bars that are still hidden.
- **The demo shows the pipeline, not the dashboard.** `docs/demo.gif` and the
  site's `demo.mp4` are re-cut from a new `pipeline` scene that opens on the
  ticket queue — a Jira issue assigned to you, then that issue becoming a
  worktree and a seeded agent with nothing typed — before it ever shows the
  grid, and then follows one session through diff → commit → push → PR → merge.
  Every session in it is titled by a tracker slug on a `feature/<slug>/<name>`
  branch, because that is what the pipeline actually produces.

### Fixed

- **Provisioned sessions pushed into your own laptop instead of the forge.**
  Provisioning clones from the repo you picked because a local clone is fast and
  works offline — but that left the workspace's `origin` pointing at a directory
  on your machine. `git push origin <branch>` then *succeeded* into your own
  checkout: the stage chip flipped to `pushed`, `git ls-remote` confirmed the
  branch, and yet nothing ever reached GitHub, so **Make PR** failed against a
  remote that is not a GitHub repo. The clone source and the push target are now
  separate: MindFlock still clones from the local path, then re-points `origin`
  at that repo's own forge URL (copied verbatim — an SSH remote stays SSH). A
  base clone created before this fix is healed on its next use rather than
  needing a manual reset — worktree *and* clone strategies, the latter on
  resume — and a repo with no upstream at all is left exactly as it was, so
  purely local work still provisions offline.

  Only the push destination changes. What a session's base branch *tracks* is
  deliberately untouched: the workspace keeps a `mindflock-source` remote
  pointing at your checkout and still refreshes from it, so committed-but-
  unpushed work reaches every session, not just the first one. Two smaller
  consequences: local clone sources are no longer cloned `--filter=blob:none`
  (a blobless clone defers objects to whatever `origin` points at, which would
  have made them network-only; cloning a local path in full costs nothing since
  git hardlinks the object store), and any leftover partial-clone config from a
  pre-fix base clone is cleared during healing for the same reason. A failed
  refresh fetch no longer resets the base to a stale tracking ref, which could
  freeze it at its first snapshot forever.
- **Jira acceptance criteria were being mined from the wrong bullets.** ADF
  headings were flattened without their `#` markers, so no Jira issue ever
  matched the `## Acceptance Criteria` section the miner looks for — every
  top-level bullet in the description was handed to the agent as a criterion
  instead, and an AC section written as prose yielded none at all (routing the
  ticket to clarification). Headings now keep their level.
- **Jira and Linear reached parity in the Assigned-tickets panel.** Both now
  implement `search_assigned_all()`, so the panel lists work you are about to
  move *into* an ingest state rather than only what already matches; both
  populate `Ticket.state`, so their tickets stop collecting in the `No state`
  bucket; and both emit a state `type`, so Done/Canceled states park correctly.
  (GitHub Issues and Asana expose no comparable state model — unchanged.)
- **String ticket ids no longer break the pipeline's logs.** Jira (`PROJ-42`),
  Linear (`ENG-9`) and Asana ids were formatted with `%d`, so every affected log
  line raised inside logging and was dropped — the provisioning narrative simply
  went missing for three of the five trackers.
- **Claims that were not true, on both surfaces.** Checking every sentence of the
  new copy against the code turned up several the old copy had been making for a
  while: that Gemini ships as a provider (Antigravity replaced that CLI; the real
  bundled set is Claude Code, Codex, Antigravity, aider, OpenCode, Cline, Goose);
  that the Windows installer runs the WSL2 setup for you (it probes for `wsl.exe`
  and tells you what to run); that the 15-minute grace period applies to reviews
  (it is on the pull request's own age, and tracker tickets have no age gate);
  that "every unresolved comment" reaches the review prompt (only unresolved
  *inline* review comments do — outdated threads and top-level PR conversation are
  skipped); and that dependencies are installed for any repo (auto-detected for
  Python/uv; other stacks declare `setup_commands`). All corrected, and the docs
  now say plainly that sessions the pipeline *provisions* launch Claude Code —
  any agent CLI drives sessions you start yourself.
- **Website:** a dead script block threw a `TypeError` on every page load and
  33% of the stylesheet was orphaned markup from a mock the video replaced; both
  are gone. The social card no longer bakes in `mindflock.ai/install` (which
  404s), the video poster is a real frame from the demo instead of that card, and
  the download note no longer claims the builds are unsigned (the macOS build is
  self-signed) or gives the Control-click → Open workaround that macOS Sequoia
  removed. `privacy.html` no longer claims the site makes no third-party
  requests or that the app never contacts GitHub — both were contradicted by the
  version check.

## [0.1.4] - 2026-07-29

### Added

- **Rename a session from the sidebar without a dialog.** Clicking the row that
  is *already* selected turns its name into an input with the text
  pre-selected — renaming no longer means finding "Rename…" in the row's
  actions menu (which still works, as does the command palette). The edit is
  held for the double-click window and cancelled by one, so double-click still
  opens the IDE and never flashes an editor. Enter or clicking away commits,
  Escape cancels, and typing the real title back clears the alias.

- **Ticket / PR / issue sessions read as what they are.** Those sessions are
  titled by their machine slug — `sc-12345`, `pr-app-42` — which says nothing
  about the work. The feature name is already in the branch, so the sidebar now
  shows `(tix) add-dark-mode/sc-12345`, `(pr) login-crash/app-42`,
  `(iss) cant-open/app-77`, with the full name, the real session title and the
  branch on hover. Hand-made session names are untouched, and the title itself
  is unchanged — every API path, tmux name and workspace dir is still keyed by
  it.

- **macOS windows use the OS's own controls.** The top bar drew its own
  – □ ✕ top-*right* on every platform, which is the Windows arrangement; on a
  Mac the red/yellow/green buttons belong top-left. The window now keeps the
  real traffic lights there (`titleBarStyle: 'hidden'`), draws none of its own,
  and the bar mirrors to match — the logo, theme toggle and notification bell
  move to the right, where a Mac has nothing else. The sidebar toggle stays
  left, pointing at the sidebar it controls. Every other platform is unchanged,
  as is a browser tab on macOS. The layout follows a shell *capability* flag
  rather than the platform, so an engine updated ahead of the desktop app keeps
  the layout the installed app actually draws instead of stacking its cluster
  on top of that app's own buttons.

### Fixed

- **A force-started PR review / issue / ticket showed nothing in the sidebar
  for as long as it took to clone.** The request was accepted and then spent
  tens of seconds provisioning before it could register a session, so the
  sidebar stayed empty and the start looked like it had failed. Every accepted
  start is now recorded on arrival — before the upstream lookup, not after it —
  and appears immediately as a provisioning row. It also greens the Ticket
  Ingestion / PR Review / Issue Handling dots: those read the *pipeline's*
  activity beacon, which knows nothing about work started from the UI, so a
  forced ticket used to provision for minutes with the light showing idle.
  Their status poll now matches the sessions poll (4s), since the green window
  can be a single provisioning.

- **A mystery `/opt/homebrew/bin/claude` entry above the agents in the New
  Session dialog** (macOS/Homebrew, and any first run where the CLI was found
  on `PATH`). Detection resolves the binary by shelling out to `which`, so it
  reports an absolute path, and that was stored verbatim as the default
  program; the dialog lists any program it doesn't recognise as an extra
  option, which is right for a custom agent and wrong for a path that *is*
  Claude. A resolved path to a known CLI is now folded back to the provider
  name — both when written and when served, so an existing config is fixed
  without editing it — while a genuinely custom program is left exactly as it
  is, because for those the string is the launch command.

- **No plan-usage percentage on macOS — only the reset countdown.** The live
  reader looked for the Claude OAuth token in
  `~/.claude/.credentials.json` only, but on macOS Claude Code keeps those
  credentials in the login Keychain. The token was never found, live usage was
  permanently dark, and the fallback estimate reports a reset time but no
  percentage unless a window budget is configured. The Keychain is now the
  fallback source (macOS only, timeout-bounded, and any failure means "no live
  data" exactly as before — expect a one-time Keychain permission prompt).
  Separately, a live reading that carried a reset time but *no* utilization was
  taken verbatim and blanked a percentage the estimate could still supply; it
  now falls back and says the number is an estimate.

## [0.1.3] - 2026-07-28

### Fixed

- **Settings → System logs: clicking the log path did nothing on Windows.**
  When the engine runs inside WSL while the desktop app runs on Windows, the
  path it reports (`/tmp/mindflock.log`) is a Linux path Explorer can't reach.
  `showItemInFolder` failed silently there (nothing opened, no error), so the
  click had no effect at all. The shell now only claims success when the file
  is actually reachable from the machine the app is on, and otherwise the UI
  falls back to copying the path to the clipboard.
- **Update toast was confusing when only the desktop app was behind.** The
  wordmark shows the *engine* version, which updates on its own, so a user
  whose engine already read the latest version saw "MindFlock X is available"
  and thought it was nagging about the version they were already running. The
  toast now spells out that it's the *desktop app* that's behind and which
  version it's on.

## [0.1.2] - 2026-07-28

## [0.1.1] - 2026-07-28

### Added

- **`mindflock uninstall`** — undoes what MindFlock wrote *outside* its own
  venv, which `uv tool uninstall mindflock` leaves entirely behind. Two of
  those leftovers were actively harmful: session worktrees under
  `~/.mindflock/worktrees` are live git worktrees **registered inside the
  user's repositories** (deleting the directory strands both the worktree and
  its branch, leaving `git worktree list` pointing at nothing), and the
  activity hooks merged into a repo's `.claude/settings.local.json` /
  `.codex/hooks.json` are self-contained inline `python3` with no dependency
  on the `mindflock` binary — so they kept firing after the engine was gone
  and **re-created `~/.mindflock-assistant` after the user deleted it**. The
  command removes worktrees through git (`worktree remove` → `branch -D` →
  `worktree prune`), strips only MindFlock-tagged hook entries, deletes the
  `.mindflock_*` scratch files and their `.git/info/exclude` lines, and sweeps
  orphaned worktree directories. `--dry-run` previews, `--purge` additionally
  deletes `~/.mindflock` + `~/.mindflock-assistant`, `--keep-worktrees` limits
  it to hooks/scratch. It refuses to run while a server is up, never deletes a
  user directory, never touches a worktree outside `~/.mindflock/worktrees`,
  never removes a user's own hooks or a pre-existing branch, and *prints*
  rather than runs the final `uv tool uninstall` (it executes from the venv
  that command deletes).

- **Engine/app version drift detection** — the desktop shell pins the engine
  to its own version at install time but only ran that install when the engine
  was **absent**, so updating the app alone left the old engine running
  indefinitely; `curl install.sh | sh` (which defaults to `main`) could
  likewise push the engine *ahead* of the app, which can trip the `state.json`
  downgrade path. Nothing checked either direction. The engine now reports its
  version in `GET /api/doctor`, the shell compares it on every successful app
  load, and a mismatch raises a toast that reinstalls the engine at the app's
  version with live installer output — over HTTP, so one code path covers
  macOS, Linux and Windows/WSL. Both toasts now share a stacking container so
  a release notice and a drift notice can't overlap.

- **Visible downgrade notice** — when a `state.json` written by a newer
  MindFlock is refused, `LoadState` preserves it as `state.json.newer-<ts>`
  and starts empty. That's non-destructive but looks exactly like data loss,
  and it was only ever reported to a log. The event is now recorded and
  surfaced two ways: a `state-schema` doctor check (`warn`, so it never makes
  `doctor` exit 1 — the installer runs it) and a UI banner naming the
  preserved file and how to recover it, dismissible via
  `POST /api/doctor/ack-state-notice`.

- **In-app update notifications** — the desktop app checks GitHub Releases
  shortly after launch and every 6 hours, and when a newer version exists it
  shows a small toast in the bottom-right with **Update** (opens the release
  page), **Later** (reappears on the next check/launch), and **Skip this
  version** (persisted, stops the nudges for that version). Entirely
  best-effort: any offline / rate-limited / 404 (private repo, no releases)
  response is swallowed, so a prompt only ever appears when a release is
  actually reachable. Override the source repo with `MINDFLOCK_UPDATE_REPO`.

- **One-click engine install on first launch** — the desktop app no longer
  asks anyone to paste a `curl` command. When the offline page finds the
  engine missing it offers an **Install the engine** button, runs `install.sh`,
  and streams the transcript into the window; the app connects on its own when
  it finishes, and a failure keeps the log, offers **Try again**, and falls
  back to showing the manual command. This closes the gap on macOS and Linux,
  where a `.dmg` (drag-copy, no post-install hook) and an AppImage (never
  "installed") have nowhere for the Windows NSIS hook's equivalent to run.
  The script is **bundled into the app** via `extraResources` rather than
  fetched at runtime, so it can't 404 or drift from the build, and the engine
  is pinned to the app's own version tag.
- macOS first launch detects missing **Xcode Command Line Tools** (which
  provide `git`) and opens Apple's installer, instead of failing several
  minutes later inside uv with an unrelated-looking git error. `install.sh`
  does the same check for terminal installs — `command -v git` had been
  passing on Macs where `/usr/bin/git` is only the stub that pops that dialog.

- **Settings → Advanced → Restart server & UI** — re-execs the server process,
  waits for it to answer again, then reloads the window so both halves come
  back fresh (the reload is deliberately *after* the wait; reloading into a
  dead port would land on the offline page). `POST /api/server/restart`
  already existed for the mobile serve-mode toggle; this exposes it on demand,
  for a config change that needs a fresh boot or a server that has gotten
  stuck. Nothing running is lost — sessions are tmux, ingestion is its own
  process.

### Changed

- **The GitHub CLI (`gh`) is now optional, not a requirement.** MindFlock runs
  fully without it — only the GitHub features (push/open PRs and the automated
  PR-review loop) need `gh`, and those simply stay off when it's absent.
  `mindflock doctor` now reports a missing `gh` as `info` ("optional") instead
  of a hard `fail`, so it no longer trips the "required dependency missing"
  exit, and the GitHub connection card shows a calm "off" state rather than an
  attention prompt.
- Settings → Advanced no longer suggests `Ubuntu` as the WSL distro: empty now
  means "your default distro", matching the app and the Windows installer.
- **The settings panels no longer reload from scratch every time you open
  them.** Assigned tickets, open PRs and open issues each fan out to a slow
  upstream (~3s for the ticket sources), and the dialog threw that data away
  on close, so every visit began with a spinner. The lists are now cached
  client-side and shown immediately while a refresh runs behind them, the
  server serves its cached copy rather than making the request wait on the
  sweep, and opening the dialog warms all three in the background. The Refresh
  button still forces a real sweep. For anyone calling the local API directly,
  `GET /api/tickets`, `/api/github/prs` and `/api/github/issues` now take
  `?fresh=1` (skip the cache, await a real sweep) and carry a `stale` boolean in
  the response body; a cached payload is served for up to 5 minutes past its
  20 s TTL, so those routes no longer 502 on an upstream blip once they have a
  list to show. See [docs/web-api.md](docs/web-api.md).

### Fixed

- The make-PR dialog's branch dropdown no longer opens by itself. The input is
  pre-filled with the remembered base branch and auto-focused, so on a repo
  with several branches sharing a name (`staging`, `staging-2`, …) the filtered
  list covered the buttons on every single PR. It now opens only when you type
  or press ↓.
- The frontend could not be built: `@vitejs/plugin-react` had been bumped to a
  major that imports `vite/internal` (vite 8 only) while the project pins
  vite 6, so `npm ci` failed to resolve and `vite build` crashed on config
  load. `vitest` was also missing from `devDependencies` even though the
  config and eight test files import it, which broke `npm run build`'s
  typecheck and left 118 frontend tests unrunnable.
- The offline page is no longer reloaded on every failed connection retry. A
  failed `loadURL` never commits, so the page was already the current one and
  reloading it just restarted its script — harmless flicker before, but it
  would have wiped a running install's transcript several times a minute.
- `install.sh` detected a usable terminal with `[ -r /dev/tty ]`, which passes
  even with no controlling terminal (`/dev/tty` is mode `crw-rw-rw-`), so the
  guided `mindflock doctor --fix` step failed its redirect and was swallowed
  by `|| true`. It now actually opens the device, and honours a new
  `MINDFLOCK_NONINTERACTIVE=1` override.

## [0.1.0] - 2026-07-27

First public release. MindFlock turns one repository into a fleet of parallel,
isolated AI coding sessions — each a git worktree plus a tmux session running a
coding agent, supervised from one desktop app.

### Added

- **Session engine** — instance lifecycle (start / pause / resume / kill), git
  worktree management, tmux/PTY plumbing, and state persisted in
  `~/.mindflock/`. In-place (workspace) sessions measure their stage and diff
  stats against the repo's live default branch.
- **`mindflock` CLI** — `serve`, `doctor` (with interactive `--fix`), and the
  thin-client session commands `new`, `ls`, `attach`, `rm`, `open`, `events`
  against a running server.
- **Desktop app (Electron)** — the one supported client: a frameless window
  with a draggable terminal grid, Agent / Terminal / Diff tabs per session,
  workflow-stage badges, and guided next-step buttons. It finds and
  auto-starts the server by itself (on Windows, inside WSL2 via `wsl.exe`).
- **Guided git workflow** — one-click commit → push → PR → merge driven by the
  `gh` CLI, plus "needs rebase" awareness (`↓N` on the stage pill when the
  branch is behind its base), an "Update from `<base>`" action that runs
  visibly in the session shell so conflicts stay resolvable, and a toolbar
  "⟳ Update all (N)".
- **Phone UI** — `mindflock serve tailscale` prints a QR code; the mobile UI
  at `/m` carries the same guided git action bar, gated by an access token.
- **Ticket ingestion** — polls Shortcut for assigned stories and GitHub for
  reviewed PRs, provisions a workspace, and launches a seeded agent session
  per story / per PR.
- **Provider framework** — Claude Code built in, with Codex, Antigravity,
  OpenCode, Cline, Goose and Aider bundled; add any coding-agent CLI via a
  TOML file. Shared hooks-based activity detection (working / idle /
  needs-input), model pricing, and rolling token/cost history.
- **Wedged-session watchdog** — a session that looks idle but has been sitting
  on unfinished work (uncommitted diff or unpushed commits) for 20+ minutes
  surfaces in the attention bell as "possibly stuck".
- **Extension points** — shell hooks on every session event, a
  `WS /api/events` stream, and in-process Python + ES-module addons.
- **Installers** — `install.sh` for the server/CLI (Linux, macOS, WSL2), and
  per-OS desktop builds attached to every tagged release: an NSIS `.exe` for
  Windows (which also bootstraps the CLI inside WSL), a universal `.dmg` for
  macOS, and an `AppImage` for Linux.

### Security

- The server binds `127.0.0.1` by default. Phone/tailnet access is an explicit
  opt-in via `mindflock serve tailscale`, which auto-enables the access-token
  gate.
- Browser-attack guards enforced even when the token gate is off: requests
  from a foreign `Origin` are refused (HTTP 403 / WS close 4403 — WebSocket
  handshakes ignore CORS, so this closes the cross-site terminal-hijack
  vector), and in local mode non-loopback `Host` headers are refused (DNS
  rebinding).
- The access token can be regenerated (Settings → Security → Regenerate,
  `POST /api/settings/auth-token/rotate`), invalidating every issued cookie,
  QR code, and paired device at once; the rotating browser stays signed in.
- The startup banner written to `mindflock.log` redacts the access token and
  omits the QR code (which encodes it) — `GET /api/logs` serves that file back
  out.
- `install.sh` pins the `uv` installer version and verifies its sha256 before
  running it, and resolves the requested branch/tag to a full commit SHA
  (printed) before installing.
- Threat model, hardening guidance, and the disclosure contact live in
  [SECURITY.md](SECURITY.md).

### Known limitations

- Desktop installers are **unsigned**: macOS Gatekeeper and Windows SmartScreen
  warn on first launch. The README lists the per-OS bypass.
- Native Windows is not a supported host for the engine (no tmux, no Unix
  PTYs) — WSL2 is required, and the Windows installer bootstraps it.

[Unreleased]: https://github.com/MindFlock/MindFlock/compare/v0.10.3...HEAD
[0.10.3]: https://github.com/MindFlock/MindFlock/releases/tag/v0.10.3
[0.10.2]: https://github.com/MindFlock/MindFlock/releases/tag/v0.10.2
[0.10.1]: https://github.com/MindFlock/MindFlock/releases/tag/v0.10.1
[0.10.0]: https://github.com/MindFlock/MindFlock/releases/tag/v0.10.0
[0.9.6]: https://github.com/MindFlock/MindFlock/releases/tag/v0.9.6
[0.9.5]: https://github.com/MindFlock/MindFlock/releases/tag/v0.9.5
[0.9.4]: https://github.com/MindFlock/MindFlock/releases/tag/v0.9.4
[0.9.3]: https://github.com/MindFlock/MindFlock/releases/tag/v0.9.3
[0.9.2]: https://github.com/MindFlock/MindFlock/releases/tag/v0.9.2
[0.9.1]: https://github.com/MindFlock/MindFlock/releases/tag/v0.9.1
[0.9.0]: https://github.com/MindFlock/MindFlock/releases/tag/v0.9.0
[0.8.7]: https://github.com/MindFlock/MindFlock/releases/tag/v0.8.7
[0.8.6]: https://github.com/MindFlock/MindFlock/releases/tag/v0.8.6
[0.8.5]: https://github.com/MindFlock/MindFlock/releases/tag/v0.8.5
[0.8.4]: https://github.com/MindFlock/MindFlock/releases/tag/v0.8.4
[0.8.3]: https://github.com/MindFlock/MindFlock/releases/tag/v0.8.3
[0.8.2]: https://github.com/MindFlock/MindFlock/releases/tag/v0.8.2
[0.8.1]: https://github.com/MindFlock/MindFlock/releases/tag/v0.8.1
[0.8.0]: https://github.com/MindFlock/MindFlock/releases/tag/v0.8.0
[0.7.5]: https://github.com/MindFlock/MindFlock/releases/tag/v0.7.5
[0.7.4]: https://github.com/MindFlock/MindFlock/releases/tag/v0.7.4
[0.7.3]: https://github.com/MindFlock/MindFlock/releases/tag/v0.7.3
[0.7.2]: https://github.com/MindFlock/MindFlock/releases/tag/v0.7.2
[0.7.1]: https://github.com/MindFlock/MindFlock/releases/tag/v0.7.1
[0.7.0]: https://github.com/MindFlock/MindFlock/releases/tag/v0.7.0
[0.6.1]: https://github.com/MindFlock/MindFlock/releases/tag/v0.6.1
[0.6.0]: https://github.com/MindFlock/MindFlock/releases/tag/v0.6.0
[0.5.0]: https://github.com/MindFlock/MindFlock/releases/tag/v0.5.0
[0.4.0]: https://github.com/MindFlock/MindFlock/releases/tag/v0.4.0
[0.3.2]: https://github.com/MindFlock/MindFlock/releases/tag/v0.3.2
[0.3.1]: https://github.com/MindFlock/MindFlock/releases/tag/v0.3.1
[0.3.0]: https://github.com/MindFlock/MindFlock/releases/tag/v0.3.0
[0.2.1]: https://github.com/MindFlock/MindFlock/releases/tag/v0.2.1
[0.2.0]: https://github.com/MindFlock/MindFlock/releases/tag/v0.2.0
[0.1.17]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.17
[0.1.16]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.16
[0.1.15]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.15
[0.1.14]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.14
[0.1.13]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.13
[0.1.12]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.12
[0.1.11]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.11
[0.1.10]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.10
[0.1.9]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.9
[0.1.8]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.8
[0.1.7]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.7
[0.1.6]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.6
[0.1.5]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.5
[0.1.4]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.4
[0.1.3]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.3
[0.1.2]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.2
[0.1.1]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.1
[0.1.0]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.0
