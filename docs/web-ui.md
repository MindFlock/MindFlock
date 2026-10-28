# Web UI guide

The frontend is a React + TypeScript app. Source lives in `frontend/` (Vite;
`npm run dev` proxies to the FastAPI server, `npm run build` emits committed,
unminified `app.js` / `style.css` / `index.html` into
`backend/web/static/`). The addon runtime (`core/events.js`,
`core/slots.js`, `core/ws-xterm.js`), the `/m` mobile page (`mobile.*`), and
`theme.css` stay outside the bundle as plain scripts. See
`frontend/README.md` and `backend/web/static/README.md` for the layout
and the contracts to preserve.

## Vocabulary

One word per thing, everywhere a person reads it — the web UI, the phone page,
CLI output, MCP tool descriptions and these docs. API paths, JSON and settings
keys, event names and MCP tool names keep their older names (`/api/instances`,
`autopilot`, `lane`, `set_autopilot`).

| Thing | Word |
|---|---|
| An agent working on a worktree | **session** |
| Assistant, log tail, Verify and extension rows on the rail | **window** |
| The ladder that ships a session when its turn ends (off / Commit / Push / Open PR / Merge) | **Fast-track** |
| Several sessions started together and shipped by MindFlock | **team run** |
| The git steps | **Commit**, **Push**, **Open PR**, **Merge** (an existing PR's link is **View PR ↗**) |
| Leaving a session | **Close** (keeps the worktree), **Delete** (removes it) |
| Waiting on the person | **Needs you**, with its reason: Question, Pre-commit failed, Checks failing, Setup failed |

## Layout

**Top bar** — on the left: the brand logo, the sidebar toggle (`Ctrl+B` / `⌘B`),
the theme toggle 🌙, the notifications 🔔 bell, whose one amber badge counts
everything that needs you (see [Notifications](#notifications-)), and the
mic right after it (below: talk to the focused session). Then the menu,
which carries only the daily loop — **New**, **Intake** (`Alt+I`; see
[Intake](#intake)), **Verify** (`Alt+V`), **Schedule** (`Alt+S`; see
[Schedule](#schedule)) and **Settings** — and ends in a
monochrome magnifier icon (`#palette-btn`, tooltip *Command palette — Ctrl+P /
⌘P*) that opens the command palette (below). **Intake** sits beside **New**
because both are about starting sessions — one from scratch, one from what came
in. The **MindFlock** wordmark (carrying the *running engine's* version, plus a
red `-DEV` badge under a dev shell) sits centered, and the empty strip beside it
is the drag region that moves the desktop shell's window.

The bar never wraps and never clips (any width from 360 px up): it measures
itself, before paint and on every resize, and gives things up in a fixed order
(`fitTopBar` in `TopBar.tsx`). The wordmark goes first, then Settings, Schedule
and Verify move, from the right, into one **⋯** (`#more-btn`) that sits where
they stood, just before the magnifier. **New**, **Intake** and the magnifier
never fold. The ⋯ carries the folded items' counts (Verify's red count rides on
it while Verify is folded), and its menu lists the folded items with their own
counts and tooltips (↑/↓ move, Esc closes). A folded item keeps its id
(`#verify-btn` &c., hidden), its shortcut and its palette entry; the calm rule
below still decides which items are on the bar at all.

**Verify** is always on the bar once the [calm first hour](#layout) is over
(or its first checklist exists — before that it waits, and so does
**Schedule** until its first template; Alt+V / Alt+S and the palette work
throughout). Its badge counts the checklists under **Needs
you** and **Failed** (the dialog's first two groups) and says nothing at zero;
it turns **red** when any of them failed, and the tooltip's second line says
what it counts (*3 checklists need you or failed — 1 failed*). Everywhere Verify is described in passing
(this tooltip, the palette, the shortcut sheet) it is one sentence,
`lib/vocab.ts`'s *check that pushed changes work*.

**Schedule** is always on the bar too (`#schedule-btn`, tooltip *Schedule —
templates that start sessions on their own (Alt+S)*). Its only mark is a red
count of the scheduled templates whose **last run failed** — exactly the
Schedule cards that say *Last run failed*, so it clears when the next run
starts fine — with the tooltip's second line saying so (*1 scheduled
template's last run failed*). A schedule that is merely set, a paused one, or
a run skipped because the previous one was still going gets no mark: those
are the feature working. It refreshes on `template.run_started` /
`template.run_failed` (`lib/useScheduleStatus.ts`). Under 520 px the worded
items close up so the palette icon stays on the bar.

**The bar's tooltips read their shortcuts off the live keymap**
(`shortcutLabels`), rebinds included — and in a browser tab they leave out
keys the browser keeps for itself, so **New** says *Alt+N* there (Ctrl+N opens
a browser window) and *Ctrl+N / Alt+N* in the desktop app. Under 560 px the
wordmark steps aside rather than being clipped.

**Each surface has a URL.** Opening **New**, **Intake**, **Verify**,
**Schedule** or **Settings** puts it in the hash (`#new`, `#intake/tickets`,
`#verify`, `#schedule`, `#settings/accounts`; an old `#templates` link opens
Schedule) with one history entry, so the
browser's **Back** closes it instead of leaving the app, and a link (or a
reload) opens it (`lib/surfaceHash.ts`). Moving from one surface to another
replaces that entry, so Back still lands on the grid; closing a surface in the
app steps back over its entry. Commit, the palette and other moments don't
touch the URL.

Four entries left the bar:

- **Outbox** is gone as a place. What waited on you is in the bell; a finished
  team run's summary is **Copy summary** in its header's ⋯ menu; a team run's queued
  lines are in that same menu — or, for a split or one-for-all team run (which has
  no header), on its lead's Thread tab (see
  [Team runs on the rail](#team-runs-on-the-rail)).
- **Prompts** is a sidebar bar, switched on in Customize like the Assistant
  (see [Saved prompts](#saved-prompts)).
- **Recent** is the **Recently closed (n)** link under the session list (see
  [Recently closed](#recently-closed)).
- **Command** is the magnifier icon.

In the desktop shell **on macOS** the bar mirrors. The window keeps the OS
traffic lights top-left (`titleBarStyle: 'hidden'` — the shell draws no – □ ✕
there), so the bar reserves ~78px in that corner and moves the logo / theme /
bell cluster (mic, bell, theme, logo — the mic now *before* the bell, reading
inward from the corner) to the right, where a Mac has nothing else; the sidebar toggle and
the menu stay where they are. It is the same elements with the same behaviour,
mirrored — the one difference is DOM order: the mirrored cluster sits after the
drag region, so `Tab` reaches the theme toggle and bell last there instead of
first. The reserved gap collapses in fullscreen, because macOS hides the lights then:
the shell pushes transitions over a `fullscreen-changed` IPC event, and the bar
also *pulls* the current state (`win:is-fullscreen`) when it mounts — a window
that is already fullscreen when the UI loads or reloads never sees a transition,
so without the pull it would hold the gap open until you toggled fullscreen.

The **same** UI in Safari or Chrome on macOS keeps the standard left-hand
layout: the decision reads the preload bridge's `nativeTitleBar` capability flag
(`frontend/src/lib/shell.ts`), never the user agent, so the layout only shifts
where something really draws native controls. Two DOM hooks come with it, for
theme and addon authors styling against the bar: `#topbar[data-mac]` (native
title bar) and `#topbar[data-mac-lights]` (…and the lights are visible right
now).

**The mic** (`#mic-btn`, a monochrome glyph like the bell's) talks to the
**focused session** — the tooltip names it (*Talk to Login fix — click to talk,
right-click for options*). The words are **typed into the session's input
field as you speak**: each phrase, the moment it is recognised, goes down the
session's agent terminal as keys (with a trailing space), exactly as if you had
typed it. Nothing pops up while you speak or while a reply is read: the words
are in the terminal, and a small pill under the mic says only *Listening…*,
*Speaking…* or, for a second after "stop talking", *Quiet.* (see [Voice
input](#voice-input)). When the server's
recogniser streams (see below), or Web Speech has interim words, the words go
in **while you speak**, as soon as the recogniser first hears them, and are
corrected (backspaces, then the new end) as it changes its mind — only a
trailing word that is, or could still become, a control phrase (“over”,
“disregard that”, “stop recording”, “that is all”) waits for the next ones or the end of
the phrase, so a closing radio word or stop phrase is never typed; at the
phrase's end the server's accurate model hears the whole phrase again and,
when it heard it otherwise, the whole phrase is retyped to that (see [Voice
input](#voice-input)). Nothing is sent until you say “over”. With no session focused, nothing is typed (*Click
a session first*).

- **Radio words**, the last word of a phrase (any case or punctuation; the
  idle caption says *Say “over” to send, “over and out” to stop, “disregard”
  to start over*): **“over”** (or “over it”) types the phrase before it and
  sends the draft — `POST /api/instances/{title}/send` with `{"enter": true,
  "say": …}`, which presses Enter after the same dialog checks as a send (a
  session that turned out to be on a dialog answers 409, the draft stays and
  listening stops: *Login fix is on a dialog — press Enter yourself when it is
  answered*) — and **listening goes on**: the next phrase starts the next
  message, offered to the router again (“switch to billing”, then the words
  go there). **“over and out”** (or “over, out”, “over and out, that's all”)
  sends what is pending — nothing, when nothing was said since the last
  “over” — and stops. “Over” and “over and” are held back while “out” may
  still follow. **“disregard”** (or “disregard that”) takes back everything
  this message typed — one backspace per character, never Escape, which in
  Claude Code would also interrupt a running turn — and keeps listening.
  **Esc** is “disregard” and stop.
  “Submit” is no radio word: said at the end of a phrase it is typed like any
  other (the router still takes a bare “submit” on Claude's question prompt's
  review step, see [When a session asks you something](#when-a-session-asks-you-something)). To stop and leave the words
  unsent, use a stop phrase, Esc, the mic or the Talk hotkey again, or let go
  in *Hold to talk*.
- **Spoken confirmations** (Settings → Voice → *Spoken confirmations*, on by
  default; per browser, `confirm` in `cs_voice_desktop`): your own actions are
  said back in the reply voice — *Sent.* after “over”, *Sent. Over and out.*
  (or *Over and out.* with nothing to send), *Cleared.* after “disregard”,
  and the router's own line after a switch (*Now talking to Billing.*) — with
  *Read replies aloud* off too. A stop phrase says nothing back: the falling
  chime is its answer. They go to
  the front of the one speech queue (after other confirmations, never behind
  a reply), are said even while the person is mid-sentence, are
  dropped when more than 5 s late, and the recorder drops its audio while one
  plays and 150 ms after (`Recognizer.mute`; Web Speech ignores its results
  then) without ending the phrase. The server voice's audio for them is kept
  in memory per voice (engine + detail), and the five fixed lines are made
  ahead once — on the first press, when `/api/voice` says the voice is local
  (`tts_backend` `local` / `system`, or a host at a local address in
  `tts_detail`), else only after a line was first said without an error.
- **Replies while listening.** In continuous listening the speech queue is
  not held: a reply's `Say:` line, *Everything*'s lines, ask read-outs and
  the router's lines play as soon as they are ready, in the person's pause —
  the queue's gate (`SpeechQueue.setGate`) holds a line only while the phrase
  under way has words from the last 1.5 s and no 250 ms pause yet
  (`LiveDictation.midSentence`; also for 1.5 s after the mic is keyed). While
  any line plays the recorder drops its audio, and 150 ms after
  (`Recognizer.mute`) — the phrase under way is not ended — and listening
  goes on after it. A click (or the Talk hotkey) while a line plays cuts that
  line and keeps listening; the next one stops. **Barge-in by voice:** while
  a line plays, the server's recogniser keeps reading the microphone's level
  (an analyser, every 40 ms; the recorder's muted blocks are unmetered) and
  three loud reads in a row — a voice over the line, ~120 ms after it starts
  — cut a reply or a read-out (`RecognizerHandlers.onBarge` →
  `SpeechQueue.skip`; a confirmation such as *Sent.* plays on) and hold the
  next line for 1.5 s and the person's words (`LiveDictation.bargedIn`); the
  mute lifted for it skips no tail, so the recorder goes on from the voice
  itself (the first ~120 ms, before the detector was sure, are lost — the
  recorder drops muted audio). "Loud" is over 0.04 RMS and three times what
  the microphone hears of the app's own voice — the speakers' bleed the
  browser's echo cancellation leaves — learnt as a floor from the muted reads
  (`BargeDetector.bleed`; the first read seeds it, so a machine with no
  cancellation never barges on its own voice, and only a voice well over the
  bleed barges). Web Speech has no level while muted: no barge-in by voice
  there. A hands-free answer still holds the queue for its 12 s listen.
- **Control words act fast.** A control phrase at the end of the **guess** —
  “over”, “over and out”, “disregard”, a stop phrase, “stop talking”, or a
  switch command as a message's first phrase (“switch to billing”, “focus 3”,
  “next session”) — acts once the level meter has heard nothing for 250 ms
  (`CONTROL_TAIL_MS`), not after the recogniser's 1.1 s pause: the phrase is
  ended at the recogniser at once (`Recognizer.endPhrase`, its `/end`), and
  “disregard”, a stop, “stop talking” and a switch act on the guess right
  away (that phrase's late words and end are ignored). “Over” waits for
  `/end` — its accurate final retyped first — only as long as finals have
  been taking: half again the typical round trip plus 100 ms, within
  300 ms..1.5 s (`overWaitMs`; `OVER_FINAL_WAIT_MS` is the most, for the
  first "over" of a page before anything is known — `/end`'s `final_ms`,
  the server's own measure of its accurate pass, plus 150 ms stands in
  until a round trip was measured, from the phrase's end at the recogniser
  to its `/end` back, `FinalClock`), then sends the fast words and ignores a
  later final — never the other way round: a slow final never holds the
  words. A final that matches the fast words types nothing, so Enter goes
  at once (no trailing space is typed before Enter, and nothing settles);
  only a correction waits 250 ms for its keys to land. The time from the
  word's end to Enter is the Diagnostics line's *over → sent 0.9 s* (and
  `window.__mfVoiceSent`, the debug trace's line).
  A control word the next word rules out (“move it over here”) goes in as
  before.
- **Sounds** (Settings → Voice → *Sounds*, on by default, per browser):
  every start of listening — a click, the hotkey, the wake word, the ask
  card's mic — plays a rising two-tone chime (skipped from the recording),
  and every end of it — “over and out”, a stop phrase, Esc, the button or
  hotkey again, letting go, the idle stop, a refused send — a falling, softer
  one (880 → 660 Hz, 140 ms; `chime(ctx, "stop")` in `lib/wakeWord.ts`).
  “Over” (sent, still listening) plays none — *Sent.* is the cue. A chime
  never plays over words: it goes through the speech queue
  (`SpeechQueue.cue`) after the line being said and the confirmations
  waiting, ~120 ms after their end, before any reply — so a pending
  *Over and out.* / *Sent.* plays whole, then the chime. Nothing on the
  stop path cancels or skips the queue; a per-session cancel never drops a
  confirmation.
- **Stop at any moment**: **“stop recording”**, “stop listening”, “mic off”
  or “that's all” at the end of any committed words — mid-phrase too, not only
  as the last word of a phrase — stops listening at once; the phrase is never
  typed and what was typed before it stays in the field (*Stopped listening*
  when nothing was).
- **Push to talk.** *Click to talk* (the default): a click starts listening
  (the mic turns red inside a thin, still red ring) and it **stays on** —
  message after message, no idle timeout — until “over and out”, a stop
  phrase, Esc or a second click, which stops leaving the words in the field
  unsent (*In the field — press Enter to send*). A wake word (“MindFlock”)
  starts it the same way. *Hold to talk*: hold the button (or Space / Enter
  on it) while you speak — “over” sends and listening goes on while you
  hold; releasing stops, after waiting up to 1.5 s for the phrase still being
  recognised (it is typed like the rest — and if it ended in “over”, sent).
  There, and for a hands-free answer (one message: “over” sends and stops),
  two minutes without a word stops listening; the words stay where they are.
- **The Talk hotkey** (the keymap's Talk action, default Ctrl+T — Alt+Space
  stays as the browser-safe alias, since Chrome keeps Ctrl+T for a new tab —
  rebindable in Keyboard shortcuts) does the same from anywhere — a terminal
  pane included: the keymap dispatches `mindflock:voice-talk` on `window` with
  `{phase: "down" | "up"}`. In *Hold to talk* a press starts and the release
  stops (a window blur's `up` ends only a press the key began); in *Click
  to talk* each press starts or stops and releases do nothing. Stopping leaves
  the words in the field; say “over” first to send them. The
  keydown counts as the gesture that starts the audio engine, as a click does.
- **A dialog on screen.** Before a dictation's first phrase is typed, the
  session's live activity is checked (the rows the app already polls): on a
  question, permission prompt or usage-limit menu (`clarify` / `limit`) nothing
  is typed — words would answer it — the mic's tooltip says *Login fix is
  waiting on a dialog — answer it first* and listening ends.
- **A text box focused** (Send a message, a dialog field…): the words go into
  the box at the cursor, “disregard” removes them while they still sit before
  the cursor, and “over” only starts the next message (“over and out”
  stops) — Enter is never pressed in a text box (the ask card's answer field
  is the exception: it sends itself, and once the card closes or moves on the
  next message goes to the focused session).
- **Routing.** A dictation's first phrase is first offered to the voice
  router: “switch to the login fix”, “take me to the sitecheck session”,
  “open settings” selects that session (and the conversation stays there —
  later words go to it); when two match it asks *Which one: a or b?* and
  the next phrase picks (“the second one”, “copy two”). “Start a new
  session” asks *In which project?* and *What should it do?*, then starts
  it and selects it; “what needs me” / “status” / “what is it asking” /
  “what did it say” answer out loud — none of them is typed. What the
  router asked back rides with the next phrase for 30 s
  (`routePending`); a continuous dictation keeps listening for the answer,
  a single one needs another press. Everything else — and everything when the router can't be
  reached, or answers with a kind this page doesn't know — is typed into the
  focused session.
- **Numbered sessions.** Sessions are numbered as the rail's badges number
  them (`railOrder`, the same rows Alt+N reads — window rows count): “focus
  three” / “session 3” selects row 3 (*There is no session 7* past the end;
  *Row 2 is a window, not a session*), “next session” / “previous session”
  move from the focused one, skipping windows and wrapping.
- **Answering by voice.** While the focused session shows a dialog (its row
  reads `clarify`) or its last `Say:` line asked a question (`GET
  /api/instances/{title}/voice` `has_question`, until it works again), a lone
  “yes”, “no”, “always” or a number goes to the router the moment it is
  committed. When the router resolved the dialog, its `keys` are pressed with
  `POST /api/instances/{title}/answer {keys, dialog_id, by: "user"}` — only
  while **Approve dialogs by voice** is on in Settings → Voice (`GET
  /api/voice` `approve_dialogs`; off: *Approving by voice is off: Settings →
  Voice*). “Always” is pressed only when the router says so, never inferred
  here. A question (no dialog) gets the answer as a prompt: typed and
  sent like “over”, with `say` per *Read replies aloud*. “Session three
  yes” answers that session without moving focus (a row number alone is mapped
  to its title and asked again). No dialog and no question: the router's
  sentence is shown (and read, with replies on).
- **Replies read aloud** are **off** by default here (the phone is the
  hands-free surface). Turned on, a draft the mic submits asks the session for
  a closing `Say:` line, and when that turn ends the line is read once —
  bare for the session you are on and talking to, and opening with where it
  is from for any other, or one you have not heard from in ten minutes
  (*“In Login fix, on fix the login redirect: We were working on the login
  page, and the redirect loop is fixed. Should I push it?”*) — only for drafts
  this mic submitted, never for prompts typed or sent from the phone. Only
  **news** is read by itself (see [Voice input](#voice-input)). Nothing is read while the mic listens; queued lines
  wait. **Hands-free**, with the same switch on (no other setting): a dialog
  that comes up on the focused session is read out name first in the phone's
  words (*“Login fix: Allow git push? Say yes, no, or always.”*), and a reply
  that asks a question opens the mic once it has been read; the mic then
  listens 12 s for the answer. A miss — silence, or words the router doesn't
  take as an answer — gets one retry (*I didn't catch that: say yes, no, or
  always.* / *Still there? …*), then it goes quiet (*Login fix is waiting on
  you*). With approving by voice off the dialog is read and *Answer it on the
  screen.* follows; the mic doesn't open.
- **Options**: right-click or Shift+click the mic (only then — it never opens
  by itself) for a small popover of controls and nothing else: *Read replies
  aloud* as one segmented row (Off / Mine / Everything — the full wording is on
  hover: Replies to what I said by voice, Everything, every session; another
  window's line starts with its name, the focused one's does not; everything
  spoken goes through one queue, one line at a time; *Everything* also turns on
  the server's `say-all` marker, `POST /api/voice/say-all`, so every prompt in
  every session — typed into a terminal included — ends with a `Say:` line
  instead of a digest), *Push to talk* (Click / Hold, the Talk hotkey beside
  it), the *Listen for “MindFlock”* switch (why standby stopped is its
  tooltip) and, while a line is read, **Stop talking**. It hangs under the mic,
  ends at the mic's right edge instead when it would cross the window's, keeps
  8 px from every edge and scrolls inside a short window
  (`placeUnder`, `lib/menuPlace.ts`). The explainers and the diagnostics line
  are in **Settings → Voice → This computer's mic**: the engines in use
  (*Recognition: server, built in (Windows via WSL) — one clip per phrase:
  16 kHz, browser resampler, a phrase ends after 1.1 s of quiet · Voice: server
  (piper)*, *Recognition: server (local) — streaming (words as you speak):
  16 kHz, windowed-sinc resampler, a phrase ends after 1.1 s of quiet*,
  *Recognition: this browser · Voice: this browser*, and why the server's
  recogniser isn't ready when it would be used), then the last pass, final and
  wake times. The choices are kept per browser (`cs_voice_desktop`), and
  Settings shows the same ones, so either place sets them (they follow each
  other through the `mindflock:voice-prefs` window event).

**Which engine.** In the **desktop app** (Electron — its Chromium has no
Google speech key, so Web Speech fails there on every start) recognition and
the voice both use the **server's** engines whenever `GET /api/voice` says that
kind is ready (`server_ready.stt` / `.tts`: the built-in Windows or macOS
engine, whisper, piper or a host), whatever `voice.stt` / `voice.tts` say —
those are the phone page's preference. With no server recogniser ready the mic
never tries Web Speech: it says *Speech needs the server's engine here:
Settings → Voice*. With no server voice, replies use the app's own
speechSynthesis. In a **browser tab** it is the phone's rule: Web Speech and
speechSynthesis, unless Settings → Voice says *server* for that kind and it is
ready (Chromium builds without Google's speech service, such as Arc, need that:
their Web Speech fails with *Speech recognition failed (network)*). The mic
re-reads `GET /api/voice` when the window regains focus and after any settings
save.

**The server's recogniser** records each phrase as **16 kHz 16-bit mono WAV**
(`lib/wavRecorder.ts`, through the page's one AudioContext, made inside the
click) — the built-in recognisers take nothing but PCM. The microphone's rate
(usually 48 kHz) is brought down by the browser's own band-limited resampler
(an `OfflineAudioContext` at 16 kHz rendering the clip), or, where there is
none, a Blackman-windowed sinc — never plain interpolation, whose aliasing
folds everything above 8 kHz back into the speech band. A clip ends after
1.1 s of quiet following speech (short, so typing keeps up with you) or at
12 s, and is uploaded as `audio/wav` to `POST /api/voice/transcribe`; a clip
the level meter heard no speech in is not sent.

**Streaming.** When `GET /api/voice` says `server_ready.stt_stream` (only the
local whisper engine streams) the mic streams instead of sending clips. At a
phrase's first speech it opens `POST /api/voice/stream {lang}` → `{id}`; then,
whenever the previous request is back and at least 300 ms of new audio exists,
the recorder's `take()` (the WAV since the last take, 16 kHz through the
windowed sinc, recording on) goes to `POST /api/voice/stream/{id}/chunk` —
never two at once. Each answer's `committed` words are typed as above; its
`partial` is the muted tail. The quiet before the first word is not sent. The
same 1.1 s of quiet ends the phrase: `…/end` flushes its last words (the
phrase's radio word, if any, is read off the whole phrase then — "over" and
"disregard" act exactly as in clip mode), and the next phrase opens a
new stream; there is no 12 s cap. Letting go (or the second click) sends what
was said since the last chunk, flushes, and stops — waiting for the final
only the finals' usual round trip (`finishWaitMs`, as for "over"; a clip
waits for its transcript, up to 45 s; Web Speech 1.5 s), then stops with the
words already typed. A dictation's **first
phrase** still goes to the router whole: when its first words could start a
command ("switch to …", "what needs me", "stop" …) it waits for the end of the
phrase; when they can't — almost every prompt — it is typed as it comes. A
`409` on the open means the server can't stream right now: that phrase and the
rest go as clips. Any failed chunk or end (the network, a stream expired after
60 s, a 5xx) drops the stream (`DELETE`), types the words held back, says
*Streaming stopped (…) — one clip per phrase from here* once, and the rest of
that dictation goes as clips. How the `Say:` line is produced and which
engines exist: [Voice: talking to a session](#voice-talking-to-a-session).

**Sidebar** — session list with drag-to-reorder, stage chips, and a
close ✕ per row (closes the session, keeps the worktree). The chip is the
row's state; a status dot is drawn only where it adds something — a dropped
terminal socket (red), or a node of a family tree, whose connector lines meet
it. Long titles ellipsize **in the middle** (`add-readm…-setup-2`), so the
suffix that tells two sessions apart is never the part cut (full name on
hover; the pane title does the same); a session cut from a different repo than the one the
server manages shows a compact `⇄` glyph whose tooltip carries the repo name and
full path. Under each row title a muted **context line** shows the session's
total diff stat at a glance — `+120 −8 · 6 files`: everything the session has
produced vs its base branch, committed + uncommitted, untracked files included
(green/red tints; hidden when there are no changes or the backend doesn't
report `diff_stat`; the tooltip splits out the uncommitted slice); the same
line appears in the pane header while the pane is wide enough. A session
another session's agent spawned or adopted (see
[Agent teams](#agent-teams-mindflock-mcp)) nests under its parent and carries
a status line under its name; the parent carries a roll-up of its workers. Above the list:
**+ New** and a stack of **customizable bars** (see below) — the token/cost
**Usage** readout (hidden while there are no sessions), the **Assistant** bar
(**Chat** · **Todo** · **Instructions** — **Instructions** opens the
assistant's editable agent file, its standing instructions; the chat window's
head has the same editor under the same word, and so does the palette's
**Assistant instructions (agent file)…** — "Agent" on its own means the coding
agent only), the **Prompts** bar (your saved prompts, one click from any
session — see [Saved prompts](#saved-prompts)), the **Tickets** bar (pipeline on/off switch and state dot;
it appears once a ticket source is connected), the **Pull requests** and
**Issues** bars, the opt-in **Verify** bar, and one bar per extension. Under the
list, a **Recently closed (n)** link shows once anything is closed (see
[Recently closed](#recently-closed)). Below: **View** **Auto** · **1** · **2** ·
**4** · **9** (one button per grid size, the active one filled), the session
count, **⚙ Customize** (see [Customize](#customize)) and **⌨ Shortcuts**. The command
palette, Intake, Verify and Settings live in the top bar, not down here, and
the desktop-notification on/off toggle lives in Settings → Notifications.
Disk management is not a screen of its own any more: it is the same page as
[Recently closed](#recently-closed).

**The calm first hour** (`lib/calm.ts`). A newcomer — no session yet, or a
first session under a week old, read from the server's setup record (the
`repo` step's date), so every device agrees — with fewer than three sessions
sees only what the first hour needs: the sessions, **+ New** and **Setup**.
The usage strip, the **Assistant** bar and every extension's bar (Database,
Usage optimizer…) join the *default* hidden set; the **View** presets, the
filter box (`/` still summons it), the "more bars" 💡 hint and the rows'
Alt+N numbers wait too; and pane headers are calm unless you customized them (see
[The pane header](#the-pane-header)). Three sessions, or a week, brings the
full screen back by itself. A choice the person made always wins: bars set in
Customize (here or synced from another device) are never touched, and a View
preset other than Auto keeps the picker shown.

The top bar is calm too (`topBarShows` in `lib/calm.ts`): **Verify** appears
with the first checklist and **Schedule** with the first template — until
then each has only an empty state to offer, and both compete with *③ Start
your first session*. Hidden is not removed: **Alt+V** / **Alt+S**, the
palette and the `#verify` / `#schedule` links open them all along, and once
the first hour ends both are always on the bar. An item simply appears; there
is no fanfare. The first time a top-bar dialog opens in the calm first hour,
its [tour](#tours) runs once by itself.

The decision is made **before first paint**: `/api/setup` carries the doctor
and can take seconds, so until it (and the session list) answer, the screen
uses this browser's last calm decision (`mf_calm_hint` in localStorage — a
render hint only, never a first-run flag) and the server's answer replaces it
the moment it lands. No dense layout flashes and then collapses on reload.
With no hint either (a new browser) the first paint is **calm** until the
server answers — never dense-then-calm: a veteran on a new browser sees a few
bars arrive a moment later, which reads as loading, where the full chrome
flashing for seconds and then vanishing read as something going wrong.

The footer's **Setup** chip is amber only while a *required* step is left;
once the three are done it turns neutral (*Setup 3 of 3 · 2 optional* is an
offer, not a warning).

The footer's doctor chip (*⚠ setup issues — open Setup*) stays down while the
grid's **Get set up** card or the **Setup** chip is showing — they open the
same checklist, which lists the issue. Setup has **one count** everywhere
(`setupCount`): the chip, the card and the modal all say **Setup 1 of 3** for
the three required steps, then **Setup 3 of 3 · 2 optional** while optional
steps are left (see [First-run onboarding](#first-run-onboarding)).

**Customizable bars** — the sidebar bars are movable and hideable, driven by a
shared registry (`sidebar/barDefs.ts`: Usage, Tickets, Pull requests, Issues,
Verify, Assistant, Prompts). A **fresh install shows three** (`DEFAULT_VISIBLE_BARS`) —
**Usage**, **Tickets** and **Assistant** — so the flagship ticket → session flow
is reachable out of the box without being overwhelming; **Pull requests**,
**Issues**, **Verify** and **Prompts** start hidden. **Customize** toggles each bar
on/off, and bars drag-to-reorder by their ⠿ grip (the session list is a fixed
anchor bars can sit above or below, but which never itself moves). Order and the
hidden set are synced prefs (`hidden_bars`, `bar_order`). A bar added after you saved an
order (Prompts — `LATE_BARS`) appears just above the session list rather than
below it, and a hidden set saved before it existed leaves it visible.

The three automation bars carry the **names of the Intake tabs they mirror**, so
one feature has one name: **Tickets**, **Pull requests**, **Issues**. Each bar's
label is its door — a button (`.dc-open`) that opens that Intake tab — so a bar
is a status light, a switch and a door rather than the only way in; Intake is in
the top bar whether or not its bar is showing. Each switch's tooltip names the
Intake switch it mirrors (**Tickets**, **PR review comments**, **New issues** on
Intake's Auto-start tab), and a switch that fails says why in a bottom-right
card rather than a browser `alert()` (which the desktop app does not
implement). The **Verify** bar's label opens Verify the same way. Turning a
feature's bar on is still how you get an at-a-glance dot for PR review and
issue handling once you've connected them. The bars' labels read
**GitHub PRs** / **GitHub issues**, like the Intake tabs. There is no
"pause everything" control: each bar's switch is the one that turns its
automation off.

**Window rows** — every open non-session window (MindFlock logs, System
logs, the Assistant chat, a verify watch window, an extension pane such as a
database table) gets a row IN the session list (`sidebar/WindowList.tsx`),
a first-class member of the rail: it carries the same `Ctrl/Alt+1…9` number
badge, drags to reorder among the sessions (one saved order holds both — a
never-dragged window still starts below the sessions), is toured by
`Ctrl+Tab`, and narrows under the sidebar filter by its title. Clicking a row
selects the window (top of the MRU, so a capped view brings it on screen) and
its ✕ closes the window — the same ✕ every non-session pane head carries at its
top right. A **session** pane's head carries **−** there instead: it hides the
window and the session keeps running, so ✕ always means close (see
[the pane header](#the-pane-header)). Closing a verify row closes only the
watch window; the run keeps going and the Verify dialog reopens it. Verify
*sessions* stay off the session rail (they are not work); their open windows
list here like any other window. What keeps one off the rail is the row's
`hidden_by: "verify"` flag the server records when Verify creates it (see
[web-api.md](web-api.md#get-apiinstances)), not its name — a session you name
`verify-anything` yourself is listed, numbered and promptable like any other.
The Assistant — the one window here that takes input — also accepts a dropped
file the way a session terminal does; the read-only tails do not — see "Hand a
file to the agent" below.

The **Assistant row wears the same activity chip a session row does** —
`running` / `question` / `limit` / `idle` / `offline`, painted from the same five
words by the same helper (`lib/stage.ts` `activityChip`, split out of
`chipState` so a window that has an agent but is not a session shares the
vocabulary rather than growing a second copy of it). It is fed by
`GET /api/assistant/state`, polled because the session event bus only speaks for
sessions the engine owns. The pane **head** deliberately carries none:
`.pane-head .stagechip` is `display:none` for every window, so status belongs on
the rail row and a chip added to a header would render invisibly.

A drop **merges into** the saved order rather than replacing it: the browser
only ever sees the rows it currently has, so replacing would drop everything it
cannot see — the sessions of a sleeping remote device, a filtered-out row —
and reordering two rows would silently shuffle rows that were not on screen.
Merging keeps the absent ones where they were.

**Command palette** — `Ctrl+P` or `Ctrl+Shift+P` (`Cmd` on Mac, or the top bar's
magnifier icon) opens a fuzzy-filtered palette over everything. Commands come
first and the jump rows last: **New session…**, then the dialog openers — Open
Intake (plus **Intake: Tickets** / **Intake: GitHub PRs** / **Intake:
GitHub issues** — the tabs' own names — so typing "issues" lands on that queue instead of on a dialog you then
have to navigate), **Verify — check that pushed changes work**, **Customize…**, **Prompts…** (*manage saved prompts*),
**Schedule…** (`Alt+S`; also on the short menu), **Templates…** (*your templates — in
Schedule*, the name it had, kept so the word still finds it), **New scheduled template…**
(*daily, weekdays, once or cron* — opens Schedule on a new template with **Run on a schedule**
ticked; *schedule a session* still finds it), one **Run template:
<name>** (*fresh session, task sent*) per template that has a folder,
**Assistant instructions (agent file)…**,
**Recently closed…**, Open Settings / Doctor / **Open Setup checklist**,
**Settings → …** rows for what you typed (`palette/settingsSearch.ts`: one
entry per screen plus a few controls, matched on the words people use —
"github", "notification", "worktree", "parallel", "phone", "token", "theme"
— each opening Settings on that screen, scrolled to the control, with one
small line under it saying what is behind it: *Phone alerts — Push
notifications to your phone (ntfy)* vs *Your phone — Use MindFlock itself
from your phone*; a title match ranks above a subtitle match; listed right
after the commands whose names contain the text),
**Replay <X> tour** (one per [tour](#tours): welcome, New, Intake, Verify,
Schedule, Settings, People — found by *tour*, *help* or *how*), Toggle sidebar, Keyboard
shortcuts — then the focused session's verbs (Commit / Push / **Open PR** /
**Merge PR** once there is a PR / Open / focus IDE, Rename…, Queue prompt… *runs when idle*, Split…
only where a split can actually start, **Pause session** (*stops the agent;
keeps it in the sidebar to resume* — the one place in the desktop UI that
pauses; the menus and the pane header only resume) / **Resume session**
(whichever applies),
**Close session** (*Ctrl+W · stops the agent and moves it to Recently closed*) and
**Delete session…** (*removes the worktree and branch*, asked on a card),
**Find file on map…** (opens its Map; `/` there is the Map's find box)) — each
named `<verb> — <session>` with the session's rail name, never its raw title —
then one **Focus:** row per session, named exactly as its rail row and wearing
its state's word (*Working*, *Question*, *Idle 25m*) in its tone, then extension
commands. Type to filter (subsequence match), `↑`/`↓` to select (the list
scrolls with you; it is a listbox the input points into, for screen readers),
`Enter` to run, `Esc` to close. Both bindings work from anywhere, including while a
terminal has keyboard focus — the same VSCode trade-off as its quick-open/palette
keys.

The palette opens on a **short menu**, not the whole index: first the sessions
that need you (up to five, in the bell's Needs-you order — the list `Ctrl+K N`
walks, `lib/needsYou.ts`), then New session, the
sessions you last looked at, Intake, Verify, Schedule, Setup, Settings, Replay welcome tour,
Keyboard shortcuts (the first nine), and **All commands…** (`palette/paletteRank.ts`). Typing matches
labels and a few plain synonyms (*help* finds the shortcuts and the tours,
*install* finds Setup and the Doctor). An **outward verb** — Commit, Push,
Open PR, Merge, Pause, Close, Delete — is never the `Enter` default: typing a
session's name highlights **Focus: <name>**. **Join with code …** is offered
only for a real code shape (a dashed `XXXX-XXXX` device code or an
`mfp1:`/`mfp2:` invite) — an eight-letter word like *worktree* is not a code.

**Shortcut sheet** (`?`, or **⌨ Shortcuts**) — in a browser tab, each row
leads with the combo that works there (Alt+N before Ctrl+N, Delete before
Ctrl+W), keys the browser keeps for itself (Ctrl+N/W/T, Ctrl+Tab, Ctrl+PgUp/Dn,
Ctrl+1…9) are drawn faded, and rows with nothing usable in a tab sort last,
tagged **desktop app**. In the desktop app nothing is marked.

**Narrow window** (≤ 760 px) — the sidebar becomes an overlay that starts
closed (the top bar's ▯ opens it; picking a session closes it), and **Auto**
view shows one pane. Nothing is saved: widen the window and your own sidebar
choice is back.

**Grid** — terminal panes in a draggable grid (grip `⠿` to rearrange; layout is
persisted). The view mode caps how many panes are visible; the most recently used
sessions win a slot and the rest keep running hidden. A session still
provisioning shows a placeholder pane ("the first provisioned run clones the
base repo…") until its terminal is live.

**Missing workspace** — a session whose workspace directory vanished (deleted
outside MindFlock; the backend flags it `workspace_missing`) renders as a
muted/hollow sidebar row with a **missing** chip (the pane placeholder tooltip
still spells out "workspace directory no longer exists") and a single **Delete**
action (also on its pane placeholder), which removes the dead session via
the existing DELETE. Diff and context line are suppressed. Delete is
**optimistic**: the row and pane drop the instant you click (single row or the
bulk **Delete missing**), while the DELETE — tmux kill + worktree GC — runs
behind them; the 4 s poll suppresses still-listed pending sessions so they
aren't resurrected mid-delete, and a failed DELETE restores the row with a
toast.

**No origin remote** — when a `committed` session's repo has no `origin`
(backend `has_origin: false`), the pane's next-step button becomes a
non-destructive "No remote — add origin…" hint: the tooltip shows the
`git remote add origin <url>` command and clicking copies it. Push errors from
the API (including the friendly 400 in this case) surface as toasts.

**Per-pane tabs**

- **Agent** — the coding agent's terminal (WebSocket → tmux).
- **Terminal** — a real shell in the workspace (separate tmux, lazy-created).
  Commit runs here so you watch pre-commit hooks live.
- **Diff** — the session's changes, grouped per file with collapsible
  sections; clicking a file loads its whole-file diff. Two baselines (toggle
  persisted): **All changes** (default) diffs against the fork point from the
  base branch — committed + uncommitted, the same total as the header badge —
  and **Uncommitted** shows only working-tree changes since the last commit.
  Split/unified view toggle is persisted too; until you press one, a pane
  under 700px reads Unified (Split's four columns crush the code there). The
  open tab reloads when the session's diff stats move, and on **↻**. Each
  file header has a **Map** button (*Show <file> on the map*); the Map's file
  card **Open diff** opens this tab on that file.

Terminals keep tmux mouse scrolling (speed configurable in settings);
**Shift+drag** (Alt+drag on macOS) selects text and auto-copies it.

**Hand a file to the agent** — drag a file onto a terminal (or `Ctrl+V` an
image) and its bytes are uploaded, then the saved **absolute path** is typed
into the PTY followed by a space. The agent CLI runs on this machine and can
see neither the browser's clipboard nor the filesystem you dragged from, so
"look at this screenshot" has to become a path it can open. The pane highlights
(`.file-drop`) while a file-carrying drag is over it; dropping N files types all
N paths, space-separated and quoted if a path contains spaces; anything over
20 MB is refused by the API.

The gesture belongs to windows that **take input**: a session's Agent and
Terminal panes and the **Assistant** chat. Windows you only watch — MindFlock
logs, System logs, a verify watch — have no prompt to paste into, so a drag
passes straight through them (and is caught by the page-level guard that stops
the browser from navigating away). An addon pane with its own terminal is built
by `core/ws-xterm.js` rather than by either of the two callers above, so it does
not have the gesture either.

Where the file lands depends on the window, and only on that: a session
terminal stores it inside that session's workspace under `.mindflock_pastes/`
(git-excluded, so the agent needs no out-of-tree read and the file never joins
a commit), while the Assistant — which has no worktree of its own — stores it
under `~/.mindflock/pastes`, the same bucket the phone UI uses. Either way the
agent receives an absolute path, so the experience on the CLI side is
identical. Both directories are transient: each new upload prunes its own
directory back to the newest few files.

## Workflow stages and the guided next step

Each session shows a stage badge (sidebar + pane) computed from git, plus a
GitHub lookup (`gh` or a token) for the two PR stages:

```
Provisioning → Agent → Pre-commit → Committed → Pushed → PR open → Complete ✓
                          └─ Pre-commit ✗ (hooks blocked the commit)
```

The chip words are sentence case, like the state words they share the chip
with (*Working*, *Question*), and a verdict mark trails the word: *Pre-commit
✗*, and the checks chip's *Checks…* / *Checks ✗* / *Checks ✓* / *Checks
stale* (`lib/stage.ts`).

The pane shows a single **guided next-step button**:

| Stage | Button | What it does |
|---|---|---|
| agent | **Commit…** | Prompts for a message; runs `git commit` in the Terminal tab (pre-commit hooks visible; auto-fix retries up to 5×) |
| pre-commit ✗ | **Re-commit** | Restages the hook auto-fixes and retries with the same message |
| committed | **Push** | `git push --no-verify -u origin HEAD` (hooks already ran) — plain git over your own remote, SSH or HTTPS |
| pushed | **Open PR** | `gh pr create --base <base> --fill` when `gh` is authenticated; else the GitHub REST API with a token; else opens a prefilled compare URL in your browser |
| PR open | **Merge** | Confirms in the app (*Merge this PR?* — a bottom-right card, since the desktop app has no browser `confirm()`), then `gh pr merge --merge`; else the REST API with a token; else opens the PR page |
| merged | **View PR ↗** | Opens the PR page |

Stages are detected best-effort from git state, so a commit made in Cursor also
advances the badge within a few seconds. Everything up to and including
**pushed** is pure git and needs no GitHub credential at all; the `PR open` and
`merged` stages do (an authenticated `gh`, or a token from Intake → Pull requests
→ Advanced options), so with neither the chip parks on `pushed` while the buttons
keep working through the browser. While the agent is doing anything but sitting
idle, the chip shows the session's **state** instead (next section); the ship
stage shows beside an idle agent.

### One session state

Every surface that says how a session is doing reads **one** derived state
(`frontend/src/lib/sessionState.ts`, `deriveSessionState`): the rail row's dot
and its chip are painted from the same object, the bell's *Needs you*
list is exactly the states marked "in bell", the toasts and the tab badge use
the same word. Before this, the dot read the server's `status` (green for any
running session), the chip read the live activity and then the git stage, and
the bell and the toasts each had their own phrasing — so a crashed agent showed
a green dot beside an "offline" chip, and a usage limit that auto-resumes was
painted red.

| State | Tone | Who acts | In the bell | Label |
|---|---|---|---|---|
| Starting | grey, pulsing | nobody | no | **Starting** (provisioning, or the worktree setup running) |
| Working | green | the agent | no | **Working** |
| Needs you | gold | you | yes | the reason: **Question**, **Pre-commit failed**, **Checks failing**, **Setup failed** — on the rail the chip is a button to where you act: the bell at its row (Question), its **Terminal** tab (Pre-commit failed; Checks failing with no PR yet), a worktree-setup re-run (Setup failed), the PR (Checks failing) |
| Idle | neutral | nobody | no | **Idle** — the ship-stage chip (`Committed`, `Pushed`, `PR open`) shows beside it |
| Idle, stuck | gold | you | yes — 20 min idle on uncommitted work | **Idle 25m** — how long, in the gold the bell's rows wear |
| Waiting on limit | blue | nobody | no | **Waiting on limit, resumes HH:MM** when the window is known |
| Offline | red | you | yes | **Offline** — the chip is a **↻ Restart** button |

Precedence: a missing folder (**Missing**) and a paused session (**Paused**)
first, then a provisioning one (**Starting**), then a sleeping one
(**Sleeping**, grey: its agent was stopped on purpose to free memory, so its
`offline` reading is not Offline) — none waits on anyone; then a
failed setup (prompts are held whatever the agent is doing); then the live
activity (`clarify` → Question, `working`, `limit`, `offline`); then the git
facts a resting agent leaves behind (a blocked commit, failing checks); then
idle. So an agent that is *working* on its failed pre-commit is **Working**,
not in the bell — the bell lists what waits on *you*.

**Background jobs.** Claude Code calls a session busy while any job the agent
backgrounded is still alive, even after its turn ended. The server classifies
those jobs (the row's `background` field, see
[providers.md](providers.md#background-jobs)), and the row shows a second chip
beside the state:

- **job waiting** / **N jobs waiting**: muted. The session reads **Idle**,
  and the tooltip names the command and how long it has been quiet (or, when
  the job has come back from long quiets before, the server's reason: *quiet
  22m — it has resumed after 41m before*). The agent resumes when the job
  finishes.
- **waiting on job · 2h 10m of ~6h**: the same muted chip for a waiting job
  that has an expected duration and is inside it; the tooltip gives the ETA
  as a clock time (*done about 15:50*) and who set it.
- **job past ~6h**: muted with an amber dot — past its expected duration,
  not yet stuck. The tooltip says how long it has run, how long it has been
  quiet, and when it would be called stuck.
- **job stuck ✕** / **N jobs stuck ✕**: red, for a job quiet past its
  threshold (15 min, or twice its longest earlier quiet, or 1.5× its expected
  duration — whichever is longest) or provably unable to finish. The tooltip
  is the server's reason.

On this device's rows the chip is a button that opens the job's menu (a
popover, like the row's ⋮): **Stop the job** (stuck only —
`POST /api/instances/{title}/background/{pid}/stop` on the *first* stuck
job, toasting *Stopped the stuck background job in <name>* or *Couldn't stop
the background job in <name> — <why>*); **Expected to take…** → 30 min / 1 h
/ 6 h / 24 h / **Custom…** (one inline field taking `6h`, `90m`, `1h30m`;
1 minute to 14 days), which calls
`POST /api/instances/{title}/jobs/{job_id}/expect {duration: seconds}`; and
**Clear expectation** (`DELETE …/expect`) once one is set. A job can also
get its expectation from the agent (the MCP tool) or from its command line
(`MINDFLOCK_EXPECT=6h python train.py`); the menu says who set it. On a
remote `device::title` row the chip is plain text, with no menu.

A job that is still running gets no chip, because the row already reads
**Working**. The state chip's tooltip — on the row and in the pane header —
names the job instead, with its progress when it has an expectation (*Turn
ended; a background job is still running (2h 10m of ~6h, done about 15:50):
…*). A pending row draws no job chip.

When a job first passes its expected duration, `session.job_past_expected`
toasts *Job on <name> is past its expected ~6h — still quiet* and adds a
bell row (*job is past its expected ~6h — still quiet*), once per job.

**Restart.** The Offline chip calls `POST /api/instances/{title}/revive`
([web-api.md](web-api.md)), the same lazy relaunch a terminal attach or a sent
prompt does: the conversation resumes after a crash, a clean quit starts
fresh, and an agent that died mid-turn is queued a *continue where you left
off*. A click toasts *Restarting <name>…* (*… — it will continue where it left
off…* when it was mid-turn, with the memory reason appended when memory is low;
or *Couldn't restart <name> — <why>*). Rows proxied from another device are
read-only and offer no Restart.

**Restart all.** From two Offline rows up, a strip above the list reads *N
agents offline · ↻ Restart all*: `POST /api/server/revive-all` with exactly
those titles — two at a time, waiting for memory between launches — and a toast
of what happened (*Restarted 4 agents — 2 told to continue where they left off;
1 held back: memory is low*). The toast counts only the rows the strip offered,
not the paused or already-running sessions the sweep also reports. The button
is disabled while the sweep runs, and the strip hides below two offline rows.

**Memory notice.** While `GET /api/system/memory` reads low or critical, a card
in the bottom-right corner gives the numbers (free memory, swap in use, the
share of the last minute spent stalled) and what MindFlock is holding back
(agent spawns, team-run lines, restarts). While it is up it also polls
`GET /api/system/slots` and, when heavy commands are using `mindflock slot`,
adds a line for them (*Heavy jobs: fix-auth running · 2 waiting for a slot*).
**Hide** lasts until the level worsens or clears and returns.

**After a server restart, Offline fixes itself.** Once the server has booted
(past its 30 s quiet window) it relaunches in the background every session
whose agent was running when it went down — two at a time, oldest first,
through the same path as Restart — so the rail comes back to Working / Idle
without a click each. Paused, provisioning, peer-shared, workspace-missing and
usage-limited sessions are left alone, and so are sleeping ones (below). Settings → General → **Revive sessions
after a restart** (`general.revive_on_start`, on by default) turns it off;
`POST /api/server/revive-all` runs it on demand. The same switch brings back
an agent the system killed for memory, once memory recovers (at most 3 times
an hour per session — see
[session-engine.md](session-engine.md#when-the-oom-killer-fires-anyway-agents-go-first-and-come-back)):
while it waits its Offline chip says so in its hint and stays out of the bell,
and no Offline toast or push fires; given up on, the hint says why and the row
is back in the bell.

**Sleeping.** An agent whose turn has ended and that has sat idle, unwatched,
with nothing queued or waiting for it for `general.sleep_idle_minutes` (45 by
default; 5 while memory is low) is put to sleep: its CLI and MCP servers stop,
giving back the ~0.8 GB an open window holds, and the session, worktree and
conversation stay (see [session-engine.md](session-engine.md#sleeping-agents)).
The row reads **Sleeping**: a muted dot and a quiet chip whose **Wake** action
appears on hover (`POST /api/instances/{title}/wake`; it toasts *Waking
<name>…*). Its tooltip says since when, why (idle, or memory was low) and
roughly how much memory it gave back. A sleeping row is not Offline: it is
never counted by *N agents offline · Restart all*, never in the bell, and its
sleep pops no "offline" toast or push. Opening the session (its terminal
attaches) wakes it, back in the same conversation; so does sending it anything
— the send box, the prompt queue, Send now, another agent's message. A prompt
sent to a sleeper goes to the front of its queue and is typed once the CLI has
drawn its prompt (the response carries `woke: true`). Rows from another device
show Sleeping without the Wake action (they are read-only here; opening one
wakes it on its own device). The phone page shows the same word with a
**Wake** action in the session's sheet.

**Tones are tokens.** The six colours live once in `styles/tokens.css`
(`--tone-muted`, `-working`, `-attention`, `-neutral`, `-waiting`, `-broken`,
each with an `-ink` for text over it), re-declared for the light theme; the
dot and the chips name a tone, never a colour. The activity is detected from
the CLI's own hooks where available, with CPU/pane-hash fallback (see
[providers.md](providers.md)), and debounced across two polls (events push
instantly).

A push whose checks have not passed for this commit stops with a bottom-right
card — *Checks haven't passed for this commit* — and a **Push anyway** button,
and **Delete** on a session whose folder is gone asks the same way. These used
to be browser `confirm()`s, which the desktop app does not implement, so the
buttons did nothing there.

### The pane header

Left to right: the ⠿ grip, the session's name, the tabs, then the buttons,
then **▶ Resume** (only on a paused session) and **⋯**, always the last thing
in the row. Which tabs and buttons show, and in what order, is yours to
choose in [Customize → Pane header](#customize) (`lib/paneHeader.ts`). Out of
the box: **Agent · Terminal · Diff · Queue · Map**, then ⏩
[Fast-track](#fast-track-) and **Copy all** (copy entire history). Switched
off until you want them: the **Thread** tab, the guided
**Commit…** / next-step button, **History**, the cost chip, the diff stats and
**−** (hide). (Map joined the default in 0.10 — tucked under ⋯ nobody found
it; a saved header that hid it keeps it hidden.) Agent can't be switched off. There is no Pause button:
pausing is the palette's *Pause session*, `mindflock pause` or
`POST …/pause` (see *Pause keeps your work* under
[Session row actions](#session-row-actions-the-rows--or-a-right-click)).
Under the header, a one-line **Task:** bar
names what the session was asked to do — its newest prompt from the agent's
transcript, or, until there is one (or for an agent MindFlock can't read a
transcript of), the task it was created with (`task` on `/api/instances`:
`Instance.Task`, the create prompt, set once and persisted in `state.json`);
the ❯ arrow drops the whole prompt over the terminal. The sidebar row's
muted sub-line names the same task, condensed to a short verb phrase — the
**task headline** (`task_headline` on `/api/instances`): "ok so can you please
fix the bug where the pin bar draws below the terminal" reads **Fix pin bar
drawing below terminal**, "why is the server using so much memory" reads
**Investigate server memory use**. It is made by fixed rules, not a model;
when no rule reads the prompt cleanly the line shows your own words instead —
its first sentence, cleaned and cut to 8 words with "…". A reply that carries
no task ("continue", "yes", "commit and push", the revive prompt, a status
ping) leaves the previous headline in place. Claude Code's context-compaction
hand-off ("This session is being continued from a previous conversation…") is
read through: the headline comes from the request its summary records (the
user's quoted words, latest first), and still never replaces a remembered one.
Hover the line for the prompt
that headline came from, as typed. This bar does not condense: it shows the
newest thing you actually asked.

- **⋯ is the session's one menu, and nothing switched off is lost** — the
  same model as the sidebar row's ⋮ (see
  [Session row actions](#session-row-actions-the-rows--or-a-right-click)),
  less the tabs this header already shows. Whatever the header leaves out is
  in it: a hidden tab under **More** (Terminal, Diff, Code map, Thread),
  **Browse history** and **Copy entire history** under **More**, the hidden
  next step first under **Ship** (*Commit…*, *Push*, *Open PR*, *Re-commit*… —
  once, if Ship already lists it), **Fast-track…** and **Hide window** always.
  Read-outs the header leaves out — the cost (*Claude · ~$0.42 · 79k/200k*)
  and the diff stats (*+19 −4 · 2 files*) — head the popover. It opens over
  the whole window — a 2×2 grid's lower pane used to paint over its lower
  half.
- **The Map tab's badge** is the rail row's zone chip when it needs you —
  **⛔n** red-zone files changed, **⛔** a block you haven't seen, **!** the
  guard off — else a quiet count of changed files (`mapTabBadge`,
  `lib/mapEntry.ts`); its screen-reader name says which.
- **Every Open map lands on the spot.** The push / PR / merge refusal card,
  the commit warning, a red-zone toast, the rail's zone chip, a Thread worker's
  **Map** and a Diff file's **Map** all go through `openMap` (state/store):
  a breach opens the Map's Breaches on the first breached file, a block
  outside an *only here* zone opens Scope on that file, a worker's Map opens
  its changed files. A toast for one file blocked outside an *only here*
  zone has **Allow <file>** right on it (then *Allowed …*); the rest of the
  toast opens the Map. The tabs are a `tablist` (←/→ move between them).
- **What a session can't have never shows**, whatever the setting: Terminal
  for a peer-shared folder (its shell would run outside the sandbox), Diff
  and Map outside a git repository, Thread until the session has workers or
  messages. **A hidden tab you are on still shows** while you are on it
  (opened from ⋯, a shortcut, the commit dialog switching to the shell), so
  the active tab always has its place.
- **Copy the whole history.** **Copy all** (always its clipboard icon, never
  the words) copies the open terminal tab's entire scrollback (Agent or Terminal) — the same text as
  the History view's **Copy all**, through the same loader
  (`lib/paneHistory.ts`) — and says *Copied full agent history (N chars)*,
  *No history to copy* or *Copy failed*. On Diff, Map, Queue and Thread it is
  greyed (not hidden, so the controls don't shift as you change tabs).
- **Calm header.** In the [calm first hour](#layout) an *untouched* header
  also leaves **Queue**, **Map** and ⏩ to **⋯** (Queue prompt…, Code map and
  Fast-track… are in it always) — a newcomer's header is name · Agent ·
  Terminal · Diff · Copy all. A tab you are on, a Queue with something in it,
  a Map with something to show (a changed file, or any zone state) and a
  fast-track someone set stay in the row. Once you change anything in Customize → Pane
  header, calm leaves the header alone: what you switched on is what you
  see. A pane's width no longer folds anything — words turn to icons
  instead (below).
- **Commit…** shows only once there is something to commit (uncommitted
  files, untracked included); until then the header has no lead button and
  the state chip says what the agent is doing. On hover: *Commit this
  session's changes — you check the message first. Next: Push, then Open PR.*
- **⋯ → Verify checklist…** (any header, when the session has a branch) opens
  Verify on that branch's checklist — also on the row's ⋮ menu (still
  *Checklist…* there). In the pane's ⋯ the checklist's verdict rides under
  **Open PR** / **Merge PR** (*Checklist ✗ Failed*), and on a failed checklist
  they ask first: *Checklist failed — open the PR anyway?* with **Show the
  checklist** and **Open PR anyway** (`withVerifyVerdict`, lib/stage.ts). When a checklist
  exists, its verdict chip (*✓ Verified*, *✗ Failed*, *3/5 checked*) sits beside
  the PR chip and opens it in one click.
- **An agent that isn't installed, or isn't signed in.** When the session's
  agent CLI is missing on this computer (`/api/setup`'s `logins`), a line over
  the terminal says so with **Install <agent>…** (the Setup checklist for the
  default agent; Settings → Agents on that agent's card for any other, since
  Setup only checks the default), and the
  rail row reads *Failed: claude isn't installed* with an **Install** chip —
  never Idle or Offline/Restart. Once it is installed, the row's usual Restart
  takes over. When the CLI is installed but its login probe found nothing
  (`logged_in: false`; "can't tell" never counts, nor a session on an account
  profile of its own), the row reads *Not signed in* with a **Sign in** chip
  instead of Idle or Question, and the pane says what its terminal is showing
  (*Claude Code isn't signed in yet. Answer its questions in the terminal
  below — a text style, then how to log in — and open the link it shows.*);
  the chip focuses that session. An agent that quit at launch instead gets
  **Sign in…** (the sign-in window), then MindFlock restarts it. Both clear by
  themselves once the login lands (the pane re-reads `/api/setup` every 8 s).
  The pieces live in `components/setup/AgentBlocked.tsx`
  (`agentBlocked` in `lib/sessionState.ts`).

- **One name.** The pane is titled with exactly what its rail row shows — the
  rename if there is one, else the `(tix) feature/slug` label — and the same
  name heads its palette **Focus:** row. The raw title and branch stay in the
  tooltip.
- **Quiet until it is your move.** The next-step button is outlined, not
  filled, while the agent is working, asking a question or at its usage limit,
  and while it offers **Commit…** with nothing uncommitted. It turns accent
  once the agent is idle with changes to commit.
- **⏩ alone while Off.** The Fast-track button shows just the glyph while it
  is Off (its screen-reader name still says *Fast-track: off*) and
  names the target once one is set.
- **− hides, ✕ closes.** The session pane's last button is **−** (*Hide window —
  the session keeps running*); ✕ is kept for closing a session (the rail row's
  ✕) and closing a non-session window.
- **Words while they fit, icons once they don't.** The header measures
  itself (`headNeed` in `grid/Pane.tsx`: every item at its drawn width, the
  name at a reserved 110px — or its own width when shorter — plus gaps and
  padding) before paint, on every resize (a ResizeObserver) and whenever what
  it holds changes. When the words would overflow, the tabs become icons — a
  sparkle, `>_`, `±`, a folded map, a list, two bubbles — and **History**
  its glyph (**Copy all** is a glyph at every width), each with a tooltip naming it and its word kept
  for screen readers, the active tab still filled; ▶ Resume becomes ▶. The
  words come back only once there is room for them **plus 24px**
  (`fitDecision`'s hysteresis), so a header at the edge never flickers. With
  the default set, two panes side by side at 1440px keep their words (heads
  ~573px; the switch is at ~466px, back at ~490px); a 2×2 at 1044×904 (heads
  ~375px) draws icons, and the same 2×2 with the sidebar hidden (heads
  ~505px) keeps words. Each pane decides for itself, so a pane with a long
  name or a Queue badge can be in icons beside one in words. The container
  queries still trim the rest: below 1000px the cost chip drops its provider
  and context parts (the cost stays); below 700px the diff-stat line goes;
  below 640px and 440px the gaps and pads tighten; below 300px an Off ⏩
  leaves (Fast-track… is in ⋯). A squeezed name gives way in its middle first, then — only past
  its tail — with an ellipsis at the end, never sliced mid-letter; a
  `(tix)` tag stays on the name's line.
- **The live step doesn't repeat the target.** While fast-track is working,
  the step chip shows the run's own note (what it is doing or waiting on) and
  the target stays on ⏩ beside it; a halted run shows ✗ on ⏩ with the reason
  in its tooltip.

**Ctrl+C on a running hook gives the button back.** The commit chain holds a lock
file while the hooks run, which is what keeps the pill on `pre-commit`; the whole
chain runs in a subshell trapping `INT`/`HUP`/`TERM`, so cancelling it drops that
lock and the pill is back to `commit` within one poll. Cancelling used to leave
the lock behind — an interactive shell abandons the rest of a `;` list, so the
chain's own cleanup never ran — and the window looked wedged for most of a minute
while the **stale-lock self-heal** (a liveness grace plus a persistence debounce)
noticed. That self-heal stays, as the net for what a trap cannot catch: a
`SIGKILL`, or the pane being killed out from under the hooks.

**↺ — back to idle, when the cycle is finished and you are not.** The stage is
re-derived from git on every pass and self-heals, so "let me keep working" needs
no help while the tree is dirty: the instant it is, the stage is `agent` again.
The gap is the **clean-tree** window — `committed`, `pushed`, `pr` — where the
branch is finished as far as git can tell and the header therefore insists on
Push / Open PR / Merge. Someone who just opened a PR and wants to carry on writing
code on that same branch has no control on the session that isn't about advancing a
cycle they consider done. ↺, beside the guided button, says so.

It is a **one-shot action, not a toggle** (nor a setting like ⏩ next to it): "back to idle" is
something you do to a session, not a mode the session is in, and a control lingering
in an ON state would assert otherwise. It appears only where the ladder has
somewhere to be put back *from* (a clean branch at `committed` / `pushed` / `pr`,
not one already pinned), and its tooltip names the ask it is silencing — *"the
header just stops asking you to open a PR so you can keep working here"*.

**Nothing git-facing happens.** No reset, no revert, no PR close: the commits stay
committed and an open PR stays open. The pin (`backend/web/core/stage_reset.py`,
`POST /api/instances/{title}/reset-stage`, surfaced as `stage_reset` on the row)
is a display note that only the guided ladder honours — chip, primary button, live
step. It deliberately does **not** touch the published `stage`, so Fast-track
driver, the verification-check kicker and every `*_changed` event keep reading the
same git-derived truth they always did; a display pin that lied to Fast-track
could make an armed chain try to commit a clean tree.

**It releases itself against the worktree, never against the stage label.** The
pin records the HEAD sha it was set on and dies the moment the tree goes dirty
(new work exists, so reality already agrees) or HEAD moves (a new commit landed,
and its ladder is a real one to climb). Keying release off the label instead cannot
work: filing a PR moves the stage `pushed → pr` a beat after the pin is set, which
would release it immediately. The server is the authority — it has to be, so the
pin survives a reload and so the sidebar, the pane header and `/m` cannot
disagree — though only about a pin that already exists: `/m` reads `stage_reset`
and puts its ladder back to **Commit**, but carries no ↺ of its own and no echo,
so a pin is always *set* from the desktop UI. There the browser keeps a
20-second optimistic echo so the press feels immediate, and that echo expires on
its own, degrading a lost request to "the button did nothing" rather than to a
pin no server agrees with.

The pin lives in **server process memory** and is deliberately not persisted: it
is a statement about this moment on this branch and costs one click to re-make,
so losing pins to a restart is a shrug, while carrying a stale one across one —
onto a worktree that may have moved well past the recorded sha — is worse. It is
also pruned the moment its session stops existing, because titles are reused
after a delete and a namesake session sitting on the same sha would otherwise
inherit someone else's pin.

Two leftovers from the finished cycle go down with it, because a "back to idle"
that leaves the last run's badges up is not back to idle: a **halted** fast-track
record (a live one is left strictly alone — stopping someone's running chain is
not what this button says it does) and a **stale** verification result, whose sha
is not HEAD (a current failure is never touched; the push gate reads it). The
toast names them — *"Back to idle — also cleared fast-track + checks"*. A
successful **Open PR** in this UI runs the same reset quietly, so the button
reads **Commit…** again and the commit → push → PR loop can repeat in the same
session. That follow-up is the browser's (`submitMakePr` in
`frontend/src/lib/sessionActions.ts` POSTs the same route), **not** the
`/make-pr` route's, so a PR opened from `/m` or by Fast-track at `pr` depth
leaves the ladder still asking for **Merge** until someone presses ↺.

## Session row actions (the row's ⋮ or a right-click)

A session has **one menu**, opened from its sidebar row or the pane header's
**⋯** — one model (`lib/sessionMenu.ts`) drawn by one component
(`sidebar/SessionMenu.tsx`). On the row:

- **⋮** at the row's right end, beside the **✕**. It is drawn on hover, on
  keyboard focus anywhere in the row, on the selected row and while its menu
  is open, and always on a touch screen (no hover); otherwise it is invisible
  but keeps its 16px slot, so no title reflows when it appears. A window row
  holds the same empty slot, so its chip lines up. Pending (force-started)
  rows have no menu.
- **Right-click** anywhere on the row opens the same menu at the pointer — except
  in the inline rename box and on links, which keep the browser's own menu.
- **Shift+F10** or the **Menu** key, with focus on anything in the row (its ⋮,
  a chip, the answer strip), opens it under the ⋮ and focuses its first item;
  **Enter**/**Space** on the ⋮ does the same. **↑/↓**, **Home**/**End** move
  through the items; **Esc** closes it and returns focus to the ⋮.

Both hosts draw it as a popover over the whole window (`sidebar/RowMenu.tsx`
for the row, `grid/PaneMoreMenu.tsx` for the pane, both portalled to
`<body>`), measured hidden for one layout pass and then placed by
`lib/menuPlace.ts` so all of it is on screen: under the ⋮ / ⋯ with right
edges aligned, or at the pointer; flipped above when there is no room below
(and to the pointer's left when there is none to its right), nudged — never
flipped — when **More ▸** or an armed **Delete…** grows it, and scrolling
only when the window is shorter than the menu. While it scrolls, **✕ Close**
and **Delete…** stay pinned at its foot, and unfolding **More ▸** scrolls its
items into sight above them (a 1280×720 window shows every item of the
unfolded fold at once). A click outside, scrolling the list or resizing the
window closes it. The items, in groups:

- **Work** — **Message…** (`Ctrl+K S`) · **Queue prompt…** *runs when idle*
- **Ship** — **Commit…** (only once there is something to commit) · **Push** ·
  **Open PR** · **Merge PR** once there is a PR · **View PR ↗** (while the
  session's diff-stat reads zero, **Push** and **Open PR** stay in place
  greyed, noted *Nothing to push yet*; without GitHub connected **Open PR**
  and **Merge PR** are greyed *Connect GitHub to open PRs* — the pane's guided
  button and the `Ctrl+K` chords still answer) ·
  **Fast-track…** (opens the pane's [⏩ picker](#fast-track-), with the current
  choice under it) · **Checklist…** (when the session has a branch: Verify on
  that branch's checklist, `openDialogFor("verify", branch)`) · **Run checks
  now** · **Bring work home…** on a shared folder
- **Session** — **Rename…** · **Resume session** (*starts the agent again in
  the same worktree*; only on a paused session — neither menu offers Pause)
- **More ▸** (folded) — **Terminal** · **Code map** (row menu only: the pane
  header always has those tabs) · **Thread** · **Browse history** (pane only,
  calm header) · **Copy entire history** (pane only, on a terminal tab) ·
  **Split into parallel pieces…** · **Move out of {team
  run}** for a team run member · **Duplicate session** (`Ctrl+K D`; a second
  in-place session on the same worktree) · **Branch session** (`Ctrl+K B`;
  see [Branch session](#branch-session)) · **Open in <IDE>** *your code editor*
  (`Ctrl+K O`; a double-click on the row — not its name, which renames — does
  the same; left out once `GET /api/ides`
  says the configured editor isn't installed here) · **Open preview ↗** `:port`
  when the session serves one · **Select** (`Ctrl+click`) · **Hide/Show
  window** (`Ctrl+K H`; the session keeps running) · **Re-run worktree setup**
  after a failed one · **Copy folder path**
- then the two ways to leave a session: **✕ Close** and **Delete…**.

Every verb's label, shortcut and when-it-applies rule comes from one table
(`lib/sessionActionTable.ts`) that this menu, the command palette and the
`Ctrl+K` chords all read. A verb that doesn't apply is left out — unless the
table gives a reason worth saying (`why`, today only Branch outside git): then
the menu shows it greyed with the reason as its note, the palette lists it
with the reason as its hint, and its chord raises the reason as an error card.
A row whose workspace folder is gone offers only **Delete — remove session**.

### Branch session

**Branch session** (`Ctrl+K B`, the row ⋮ / pane ⋯ menu's **More ▸**, or the
palette) is Duplicate's sibling for work that must not collide. Duplicate opens
a second agent in the *same* worktree on the *same* branch; Branch starts a new
session `<title>-branch` (numbered `-branch-2`, `-branch-3`… past names or
branches already taken) in its **own worktree, on a new branch**
(`<branch_prefix><title>-branch`) cut from the source's current `HEAD` — its
branch tip, not the repo's default branch. The source's **uncommitted changes
come along as a copy**: edits, deletions and untracked files (`.gitignore`d
ones stay behind) appear in the new worktree as uncommitted changes, nothing
staged; the source's files, index and branch are not touched. The new session
runs the same agent CLI with the source's own launch flags, auth profile and
model; like Duplicate it starts a fresh conversation, with no parent. Its
recorded base is the source's branch, so its diff shows only what happens on
the branch and **Open PR** targets the source's branch. It lands right under
its source and takes focus; the toast says whether uncommitted changes came
along (`POST /api/instances/{title}/branch`).

Git only. On a session whose folder isn't in a git repository (an in-place
session in a plain folder — the row's `git_repo: false`) the entry is greyed
with *Branch needs a git repository — <folder> isn't one*, and `Ctrl+K B`
raises that as an error card. Shared (peer) folders, another device's rows and
sessions still starting don't offer it; the server refuses them too, plus a
repo with no commit yet and a clone-strategy provisioned workspace (its
repository is the session's own folder, deleted with it).

**Pause keeps your work, and keeps the session here.** Pause only stops the
agent: the session stays in the sidebar (status *Paused*), and the worktree and
branch stay exactly as they are — no commit under your name, no removed
checkout — and **Resume session** relaunches the agent in the same folder
(`POST /api/instances/{title}/pause` / `/resume`). Pausing is the command
palette's *Pause session*, `mindflock pause` or the API — the pane header and
the ⋯ menus have no Pause (it was judged pointless there); a paused session
shows **▶ Resume** in its pane header and **Resume session** in both menus. A session paused by an
older build (checkout removed) still resumes the old way. While paused, the
pane's **Agent** tab is a calm placeholder — *Paused · The agent is stopped;
your worktree and conversation are kept.* with a **▶ Resume** button
(`grid/PausedBody.tsx`) — and opens no agent socket (the server would refuse
it with 4423 rather than relaunch the agent; `lib/terminals.ts` never
reconnects on that code). Pausing with the pane open drops its agent terminal;
Resume mounts a fresh one. The **Shell** tab keeps working. The phone page
(`/m`) shows the same line and attaches once the session is resumed.

**One way to leave a session, in two strengths.** **Close** (the ✕ on the row,
`Ctrl+W` / `Delete`, the palette's *Close session*) stops the agent and moves
the session to Recently closed (its worktree is kept) — the item says so under
the verb (*stops the agent and moves it to Recently closed*). The row's ✕ is
labelled *Close session*, and on a session with uncommitted changes it asks
first, on a strip under the row: *Close session? Its N uncommitted changes
stay in its folder — reopen it from Recently closed.* **Close session** /
**Cancel** (focus on Cancel). The Close toast carries an **Undo** button:
*Closed “<name>”* **Undo** (its tooltip names the shortcut, Ctrl+Shift+T) —
pressing it reopens exactly that session, and **Recently closed (n)** refreshes
on the close and on the undo. Only the button reopens; a click on the rest of
the toast does nothing, and the toast stays up while the pointer is on it or
the button has focus. (The toast helper takes an optional
`action: { label, onClick }` for a button like this; `lib/toast.ts`.) A plain
message that arrives meanwhile (*Copied*, *Renamed*) never takes the Undo
away: the Undo steps up one slot and keeps its own timer — two toasts at most,
and a newer Undo replaces an older one. A toast stays up as long as its text
takes to read (at least 1.4 s, 2.5 s for a failure, 6 s with a button) and is a
polite live region; a failure (*… failed*, *Couldn't …*) is an alert.

**Open PR** and **Merge** say what they did: *Opened PR for <name>* (the PR
opens in a new tab — or, if the browser blocked that, the toast is the link),
and *Merged <name> into <base>*.
After a ✕ the next row slides up under the pointer, so the row's ✕ ignores a
second click within 400 ms. **Delete…** removes the worktree and its branch; the
first click turns the item into an inline strip — *Deletes the folder and
branch “<branch>”, including uncommitted changes. This can't be undone — use
Close to keep it.* (an in-place session: *Removes this session. Its folder and
branch stay as they are…*) with **Delete** and **Cancel** (focus lands on
Cancel, so a second Enter never deletes). The pane's ⋯ is named *Actions for
this session* and the row's ⋮ *Session menu for <name>*, so a screen reader
can tell them apart. Both land in [Recently closed](#recently-closed); only a
closed one can be reopened. The words *End* and *Delete + wipe* are gone from
the app: a session is closed or deleted, nothing else.

When another live session shares the folder (a **Duplicate session** runs in
place), Delete is refused and the row grows a small card — *Folder in use by
&lt;names&gt;. Close those first.* — until you dismiss it; from the palette the
same sentence shows on a bottom-right card. When a closed or deleted session
had workers that stay up, the confirmation toast adds *N worker sessions kept
running*.

**Merge PR** asks first in the app (*Merge this PR?*, with a **Merge PR**
button), because a merge cannot be undone from MindFlock. Questions like these
(Merge, Delete from the palette, Delete on a gone workspace) are drawn on a
**neutral** bottom-right card — no red rule — because nothing has gone wrong
yet; a *failure* (push refused, delete failed, pause did not take) is a red
card that stays until dismissed, and a *success* is a one-line toast. None of
them is a browser `confirm()` / `alert()`, which do nothing in the desktop app.

**A status line that needs you is a door.** When a fast-tracked row's line
ends **— open the bell** (an escalation or a failure) or **· ready — approve in
the bell** (an ask-first ship), clicking it — or Tab to it and Enter — opens
the bell scrolled to that session's row, without selecting, dragging or
renaming the row.

A copy is filed **directly beneath the one it was copied from**, not at the
bottom of the rail — the provisioning row lands there too, so it never appears
at the end and then jumps. It stays put after that: the sidebar order is yours,
and nothing but a drag moves a row again.

## Send a message / prompt queue (✉ per pane)

Each session pane header has an **✉** button (a badge shows the pending-queue
count). It opens a popover with two ways to drive the agent:

- **Send now** types a message into the agent window and submits it — booting or
  resuming the session first if it isn't running, so one line kicks a fresh
  session into motion (maximizing token use). Independent of the Shortcut
  pipeline.
- **Add to queue** appends to a FIFO the server drains into the agent whenever
  it's idle, so a run keeps going unattended and **resumes on its own the moment
  usage returns** after an outage (the drain reboots a session whose agent CLI
  exited on a usage limit). **Loop** re-queues each sent prompt so a single
  self-improving prompt ("keep improving the repo") cycles forever; **Auto-run**
  toggles draining. Items reorder by dragging the **⋮⋮** handle, edit (✎),
  insert a new prompt above/below any item (**+↑** / **+↓** open an inline
  composer at that slot), delete (✕ — the toast's **Undo** puts it back in
  its place), or **send now (▶)**. In the box, `Ctrl+Enter` (`⌘+Enter`) is
  Send now and `Alt+Enter` Add to queue; a plain `Enter` is a new line. Dropping a `.csv` file
  anywhere on the tab appends one prompt per row to the end of the queue
  (quoted fields respected, an obvious header row skipped; any other text
  file queues one prompt per line) —
  ▶ delivers that one queued item immediately, skipping the idle wait and any
  usage-limit hold; **Clear all** empties it.

While the agent is usage-limited, **Wait out usage limits** holds the queue and
auto-resumes when the window reopens. The hold is anchored to the provider's own
usage meter when available (for Claude, the same data as the CLI's `/usage`
screen — 5-hour *and* weekly windows), falling back to the reset time parsed
from the CLI's banner; if the meter shows the window reopened early, the queue
resumes immediately. The meter is consulted **even when no limit banner is on
the pane** — so a session that runs out mid-turn, exits, and reboots to a fresh
idle prompt (no banner) still arms a hold straight from the meter rather than
firing the next queued prompt into the wall. A window that reads *spent* but
carries **no usable reset time** (a null/absent/past reset) holds on a bounded
fallback instead of sending. A meter that reads *open* — or is *unavailable* —
leaves the queue free to send, so healthy sessions are never over-held.

**A session that ran out with nothing queued** used to fall outside all of that:
an empty queue meant nothing was watching, so it sat on the CLI's limit screen
until a human came back, often hours after the window reopened. The same drain
pass now also walks the sessions whose activity is `limit` (a free read of the
state snapshot — no probes when nothing has run out), waits out the window with
the same meter/banner logic, then sends **Esc + `continue`** so the agent picks
its task back up. Settings → General's **Resume sessions when usage comes back**
turns the nudge off (default on); either way the reopening emits
`session.usage_restored` once, which is what the "Usage comes back" notification
rule rides on. Sessions with a queued prompt are left to the drain — its prompt
is the better thing to send — and the nudge obeys the drain's own
send-once-then-wait rule, so a menu that doesn't clear gets a bounded retry
rather than one every five seconds.

**Running out looks exactly like finishing**, which is the trap under all of the
above. A turn the weekly cap cuts short ends the way a completed one does — the
CLI fires its Stop hook, the pane goes quiet — so the activity badge read `idle`,
the watcher above (which selects on `limit`) never saw the session, and
fast-track's usage-limit gate was never armed: it counted its 30-second settle
over a half-finished session and committed, hooks and all. An `idle` reported by
the CLI's own hook is now re-checked against the pane for a limit screen
(throttled per session, since that path exists to avoid a capture on every poll),
and before fast-track commits anything it confirms an idle agent against the
usage meter — the pane may carry no banner at all. While a limit holds, the
settle is dropped and has to be re-earned once the window reopens, and the step
deadline stops running: a weekly window can be shut for days, and a run that
waits it out correctly must not then be halted for "no progress".

The drain never types into a pane where only a shell is left (a provisioned
agent you quit drops to `bash -i`; the prompt and its Enter would run as
commands there): the queue waits until an agent is back. A multi-line prompt
goes in as **one bracketed paste**, so CLIs that submit on every newline
(aider and other prompt_toolkit CLIs) still get it as one turn. A plain session
whose CLI takes no start prompt (aider, goose, opencode, cline, a custom
script) gets its initial prompt through this queue too, typed once the agent
is idle.

Messages from other agents ([Agent teams](#agent-teams-mindflock-mcp)) are
typed by a separate lane that never shows in this tab. Your queued prompts go
first, and the two never interleave inside one turn.

The command palette has **Message…** and **Queue prompt…** for the focused
session. Message… opens the session's Thread tab with its composer
addressed to that session (`Ctrl+K S` does the same). Queue prompt… opens the
Queue tab with the caret in its box. Neither asks through a browser prompt:
the desktop app has none, so those entries used to do nothing there.

## Agent teams (MindFlock MCP)

Every Claude Code and Codex session MindFlock launches gets the **MindFlock
MCP**: tools that let its agent see the other sessions, read their replies
and diffs, message them, and run a team of worker sessions of its own. There
is nothing to install or register. Ask the agent in plain words, for example
*"split this into one worker per service, wait for their reports, then merge
their branches"*, and it spawns the workers, waits, reviews and merges. The
full tool reference is [mcp.md](mcp.md).

What it looks like in the app:

- **Workers are ordinary sessions.** Each gets its own row, worktree and
  branch, forked from the orchestrator's last commit (a ticket orchestrator's
  workers start from the repository's base branch). You can open, steer,
  pause or close a worker like any other session. A row whose parent is gone
  keeps a muted **`↳ agent`** line.
- **Workers nest under their orchestrator on the rail.** A new worker lands
  directly below its orchestrator's family, indented (from the status dot
  onwards) with a thin accent connector. The rail always keeps a family
  together: whatever the saved order says (a duplicated window slotted in
  under its source, a drag), a worker draws directly under its parent's
  family, and the row numbers, Alt+N, Ctrl+Tab and drag-and-drop follow that
  same order. Drag the orchestrator and its workers move with it; drag a
  worker and it only reorders among its siblings.
- **Fold a family away.** The orchestrator's roll-up line ends in a small
  **hide**; click it and every session below it (workers, their workers)
  leaves the rail, and it reads **show 3**. The roll-up stays, so a folded
  family still says **1 needs you**. Folded rows leave the numbering too
  (Alt+N skips them, as with a folded team run), a search shows every match
  regardless, and the session you are focused on is never hidden. Remembered
  per browser (`mf_familycollapse`). It is a word, not a chevron: a chevron
  reads as "expand", and the row's actions are its ⋮.
- **The orchestrator's Thread shows progress and finished workers.** A bar
  under its header counts every worker, live and finished — **3 of 5 done**,
  in runs of done, failed or blocked, needs you, working and idle — and each
  live worker shows how far its branch has got (agent, committed, pushed,
  PR open). A worker that is closed or deleted moves to **Finished (n)**,
  newest first, with its final report, its last stage, its diff size and
  its PR (**View PR ↗**), or **Recently closed…** when it can be reopened.
  Its spawn record and report stay in the log below.
- **The orchestrator's Thread shows the order its workers run in** when it
  set one (`set_order`, `spawn_session(after=…)`, or a fence that overlaps
  another worker's): an **Order** section, one column per step, left to
  right — a column's workers run together, the next column starts once
  every one of them is done. The line beside the heading says the rule
  (**One at a time**, **Up to 2 at a time, in 3 steps**, **All at once**).
  Each card: the worker (click to go to it), its state (**running**,
  **waiting** — *after w1*, *for a free slot*, **done**, **stopped**), why
  it waits when its fence overlaps another's (*same files as w1
  (src/api.py)*), and its fence as chips (**only src/api/\*\*** green,
  **⛔ db/** red). A waiting card has **Start now**. A title a step names
  that has no worker yet is a dashed card, **not started**. No connector
  lines: the columns are the order. A split's lead shows its team run's
  stages the same way under its header — **Plan → Pieces → Merge back**
  (**Commit each** in its folder) **→ Check → One PR**, the running stage
  lit. A held worker's own Thread says **Waiting its turn** and shows its
  fence.
- **Each worker's status line** replaces the `↳` line: **✓ reported**
  (green), **✗ blocked** / **✗ failed** (red), **? needs you · question**
  (gold), **working · 6m**, **waiting · after w1** (MindFlock holds its task
  until its turn), or **idle — no report**. A worker that is not
  directly under its family names it: **`↳ api · ✓ reported`**.
- **The orchestrator's roll-up**, most urgent first: **1 needs you ·
  3 workers**, **2 of 3 reported**, **all 3 reported**, **3 working**. Hover
  it for every worker's status; click it (or Tab to it and press Enter) to
  open the orchestrator's Thread. A worker still being created, or one on
  another device, isn't counted — the menu, the palette, the roll-up and
  the Thread all count workers the same way the server does.
- **Its chip**, while it sits idle over its workers: **waiting** (dashed)
  until every worker has reported, then **wrap up** (filled). Clicking
  **wrap up** types the Wrap up prompt into the orchestrator's input box
  and selects it. Nothing runs until you press Enter. The agent then merges
  each worker, runs the tests, and asks you before deleting anything. When
  the orchestrator can't take a paste (this launch has no MindFlock tools),
  **wrap up** is a plain label with the reason in its tooltip.
- **Answer a blocked worker from the rail.** When an orchestrator or a
  worker stops on a permission prompt, its row shows the prompt and that
  prompt's own buttons (**1 Yes**, **2 Always**, **3 No…**). A session
  created with **Split across workers** counts as an orchestrator from its
  first prompt (the row's `playbook`), so its first `spawn_session` prompt
  gets the strip before any worker exists. A prompt raised by one of Claude's
  background sub-agents counts too: the server reads a dialog on screen as
  "needs you · question" whatever the agent's hooks last said, and answers a
  click on a visible dialog whatever the session's status reads. The strip
  leads with what the prompt is about — the command for a shell prompt (its
  description is the question), the worker's title for a `spawn_session` —
  and its tooltip names a sub-agent that asked. "Always" is
  never the highlighted button. Click one, or Tab to the row and press 1–9:
  the keys only work while that row has focus. The strip then reads
  **answered** until the agent moves on, and if the prompt changed since it
  was shown, the new one appears instead of being answered blind. (Resizing
  the pane does not count as a change: a prompt keeps its identity at any
  width, even where Claude cuts an option short with "…". The strip also
  reads the prompt again when its row resizes and right before a click on a
  read more than 3 s old, and if the server still says "the prompt changed"
  but the fresh read has the same command, keys and labels, it answers that
  read once instead of showing the note.) The strip reads nothing while the
  row's status isn't "needs you · question", and a prompt that has just gone
  away is simply no prompt — not an error in the console. One
  answer per prompt: a second click on it (the bell's copy, a double click,
  or the orchestrator answering it too) is refused by the server. If the
  agent asks the very same prompt again, the buttons come back after a few
  seconds with **It's asking again**. **↗**
  opens the pane. A prompt MindFlock can't parse shows its question and ↗
  only. **No…** also opens the orchestrator's Thread, addressed to the
  worker, so you can say what to do instead.
- **Messages are typed into the recipient's terminal** as one line that starts
  `[MindFlock message … from session "orch" — another agent, not your user.`
  They arrive only when that agent is idle, never into a permission prompt or
  over your own typing, and they never count as your input. "Never into a
  prompt" is checked on the screen itself right before typing — a dialog on
  screen holds the message even when the session's status reads idle. A worker's
  report arrives as `[MindFlock result … (status: done) …]`.
- **A toast** for each message (`✉ orch → api-billing: …`, one per sender per
  30 s) and for each report (`✓ worker api-billing reported: done — …`, `⚠`
  for blocked or failed). Clicking one selects the recipient.
- **The bell** keeps the reports (not the chatter between agents), filed
  under the orchestrator. A worker's **needs you · question** item adds
  **· worker of api** and the same answer buttons as its rail row, so a
  blocked worker is two clicks away from any screen.

Permissions: only the read, wait and report tools are pre-approved. An
orchestrator running **without** skip-permissions asks you before every
message, spawn, answer or close/delete, so give it skip-permissions (Settings → Agent
CLI's default launch flags, or the New-session dialog's **Launch flags**)
when it should run unattended. You can message any agent yourself from a
terminal with `mindflock msg TITLE "…"`, and use the same tools from your own
Claude Code outside MindFlock: `mindflock mcp --print-config` prints the line
to register it.

**Settings → Agent orchestration** (under Agents) has the on/off switch and
the scope, which apply from each session's next launch, and the **spawn
limits**: sub-sessions per orchestrator (default 8, also the most pieces a
split can make), agent-spawned sessions in total (24) and nesting depth (3).
A limit applies to the next spawn, no relaunch; an empty box means the
default, and each row says what applies now, including when a
`MINDFLOCK_MAX_*` env var on the server overrides it. An agent that hits a
limit is told which one and where to raise it.

### Fast-track (⏩)

**Fast-track** is the one name for "how far MindFlock carries this session
once its agent is done". Every control that sets it shows the same five
choices and the same toggle, and every one **acts right away through the
server — nothing is pasted into the agent**:

| Choice | What MindFlock does once the agent stops and your hooks pass |
|---|---|
| **Off** | Nothing — you take it from there |
| **Commit** | Commits with a message written from the diff |
| **Push** | …and pushes the branch |
| **Open PR** | …and opens the PR |
| **Merge when green** | …and merges it once its checks pass |

**Ask me before it ships** stops one step short of the first outward step
(a Commit target stops before committing; Push / PR / Merge stop once
committed) and waits for your go in the bell, which shows the commit message
and PR title before anything is pushed. It means nothing while fast-track is
Off.

"Once the agent stops" includes the jobs it backgrounded. While the session
reads Idle but a job it started is still **waiting**
([Background jobs](#one-session-state)), fast-track holds with *a background
job the agent started is still waiting*, because that job's completion will
wake the agent. The hold is bounded: after 15 quiet minutes the job counts as
**stuck** and stops holding, so a wedged job can't block shipping forever.

**The ⏩ button** in each pane head, beside the guided **Commit… / Push /
Open PR** button, is THE per-session control. It names the target —
**⏩ Commit**, **⏩ Push**, **⏩ PR**, **⏩ Merge**, with a small circled **?**
when it asks first, and just **⏩** while it is Off — and a click opens the
picker:
Off (`O`), Commit (`C`), Push (`U`), Open PR (`P`), Merge when green (`M`),
then **Ask me before it ships** (`A`). The current choice is ticked and
highlighted; arrows, Enter, Esc (back to the terminal) and the letters work,
and a screen reader follows the highlighted item. The button is a control,
not a status chip: it is **filled while a run is working** toward the target,
turns red with **✗** (and the reason in its tooltip) when a run halts, and
is never hidden while a run is armed — even while the workspace is still
provisioning. To do *one* step yourself, use the guided button beside it;
fast-track never takes that slot over.

**When a run stops on something the agent can fix** — a pre-commit step or
the repo's verification check failed, CI went red on the PR, or the PR
conflicts with (or is behind) its base — MindFlock hands it to that agent
with a **`[MindFlock note …]`**: what failed, where to look (`pre-commit run
<hook>`, `.mindflock_check.log`), and to fix it and commit — or, when the
failure is really a conflict with someone else's change, to stop and tell
you. A failed **pre-commit hook's own output** goes in with it: the hook's
section of the Terminal tab (its `- hook id:` block up to the next hook),
ANSI and shell-special characters stripped, at most 25 lines (a huge one,
like a test run, keeps its first 5 and last 20 with *… N lines …* between)
and 1500 characters. The note then says **which steps are left and that
fast-track takes them by itself** — *"Fast-track will then push the branch,
open the PR and merge it once CI is green by itself, so fixing and
committing is all that is left for you."* (a Commit target: *"That commit is
the last step of this Fast-track run."*). The run stays armed, waits for the
fix turn and carries on from the agent's commit (twice per run; a third
failure stops it, and that note says so). The same hook failing again after
the fix gets a fresh note with the fresh output, marked *failed again
(second time)*; the same failure seen twice is told once.

The note is typed **immediately** — the commit ran in the Terminal tab, so the
agent is idle — through the agent-message lane's own safety checks: only into
an idle agent, never into a dialog, never into a bare shell and never while
you are typing in the pane. When any of those holds it, the lane types it at
the agent's next settled idle instead. Typed, it is one line (the output's
lines are separated by `│`); the inbox keeps it as written. The ⏩ row keeps
the output too (`fix_output`), until the fix turn is over. The halt reason is
unchanged; the ✗ tooltip and the *fast-track stopped* toast add that the
agent was told. A team run's members are left to their run, which sends its
own fix prompt. **Settings → Workspace → Advanced → Tell the agent when
fast-track stops** turns the note off (on by default); the run then stops on
the failure.

A session that shares its branch with another (a copy session), a team run's
lead (its team run ships it once) and a one-for-all member (its work merges
into the team run's one PR) show the picker greyed, with the reason.

**Other ways in**, all to the same picker or the same choices:

- `Ctrl+K F`, the row's › menu (**Fast-track…**, with the current choice)
  and the palette (**Fast-track… — name**) open the ⏩ picker on that pane.
- The **New** dialog: **Fast-track to** (one line, and on the "Set it up
  myself" form) or **Fast-track each to** (a list — Intake's **Start
  together…** opens New this way). **Off unless you pick**: a single session
  (one line, or the form) always starts Off; a list starts on the Settings
  default below.
  Fast-track commits for *this* session, so it gets its own worktree:
  choosing **This folder directly** turns fast-track Off (the radio
  says so).
- The **Commit** dialog: **Then fast-track to** Off / Push / Open PR /
  Merge when green, plus ask-first — the commit happens now, the rest
  follows.
- **Settings → Workspace → Fast-track goes as far as**: *"New sessions
  start Off; batches and ticket runs start at this default."* A batch is a
  list in New or Intake's Start together; a team run the MCP's
  `start_team_run` starts without a fast-track takes it too. Unset, it is **Off**.
  A ticket source keeps its own **Fast-track tickets to** in Intake, which
  never offers Merge and never falls back to this setting.

Under the hood a fast-track target is what the API calls a `lane`:
`POST /api/instances/{title}/lane` (`{lane: "leave"|"commit"|"push"|"pr"|
"merge", ask_first}`; `leave` is Off), the row's `lane` field and the MCP's
`lane` parameters. See [web-api.md](web-api.md) and
[team-runs.md](team-runs.md).

**Split into parallel pieces…** is its own action in the row's › menu and the
palette: it makes this session the lead of a split run (`POST /api/runs`,
`split: true`). The lead proposes the pieces, each fenced to its own paths;
you approve the plan in its Thread tab; the server starts the workers and
merges them back into one PR, which goes as far as the session's fast-track
(a PR when it was Off). In the row's › menu it is disabled, with the reason
(the palette lists it only where a split can start), on a server that
doesn't take splits, for a CLI that doesn't get the MindFlock tools, an agent
launched without them ("Restart this agent…"), or one on a prompt or at its
usage limit. A team run member's row › menu also has **Move out of {team run}**
(the team run stops driving it; the session and its fast-track stay), and
**Message…** (`Ctrl+K S`) opens the Thread composer so the words go in as
yours. Sessions on another device get no Split or Move out. If you had
already rebound another `Ctrl+K` chord to `S`, `F` or `T`, your binding wins
and the shortcuts sheet (`?`) marks the newer chord **(taken)** — click it to
give it a free key.

The Thread tab's worker buttons and the rail's **wrap up** chip still paste a
prompt for you to send (the server renders it and types it without pressing
Enter, after re-checking the agent, so it can't land in a permission dialog).

In the **Thread** tab's composer, **Send now** (`Ctrl+Enter`) asks the server
to type it in: when the agent turns out to be on a prompt or at the usage
limit, it is queued for when the agent is free instead (the toast says
"queued"), never typed into the dialog. **Let name decide** queues a prompt
naming the worker by its session title, not its alias. Delete and `Ctrl+W`
never close the session while the keyboard is anywhere in the Thread tab.

## Code map (Map tab): the Code Tree, zones and the plan loop

Every git session pane has a **Map** tab (after Diff; hidden with Diff when git
isn't available). It answers "what is this code, where is this agent working,
what depends on what it changed, and what may it touch at all" — as one picture
of the worktree, the **Code Tree**.

### The Map tour

The first time a Map is shown (on any of your devices: it is the synced
`prefs.map_tour`), a short tour opens over the live map once the tree is laid out (never while
"Growing the tree…" shows).
It is five small cards at most, each ringing the part it explains, in the order
a newcomer needs them: the canopy (trunk = repo, branch = folder, leaf = file;
zoom all the way in and every leaf is named at its tip), the roots (they hold
what keeps the tree standing: tests under the branch they test, docs, CI and
config under the trunk; each root tip is one file), the agents column (birds =
agents, nests, leaves in a bird's colour and lit wood = changes, gold = could
break), the toolbar's **Rules** group (⛔ Keep out / ✓ Only here) — then it ends on this
session's next step ("5 changed — start with ⚠ Could break, then open the
diff"), or, with none, on getting around (`/`, drag, double-click, ⌂ Whole
tree, **?** for the legend). The steps are picked from what is on screen when
it opens: with nothing changed the birds card says every leaf is grey for now,
a repo with no roots (no tests, docs or config) skips the roots card, and a
toolbar card whose target isn't visible (a narrow toolbar) is skipped. The
card sits beside the ring, on the side with the most free canvas and off the
tree and the HUD panels; a card whose part isn't on screen (the agents column
folded to a rail, the roots scrolled away) docks in a corner of the canvas. It
never covers the middle of what it explains. It is non-modal, so the map keeps
moving underneath. `→` / `Enter` steps forward, `←` back, and `Esc`, **Skip
tour** or **Done** end it and mark it seen. **? Guide** at the right of the
toolbar replays it. Only one pane runs it at a time. Switching tabs mid-tour
hides it *without* marking it seen, so it comes back the next time that Map
shows; a device that has never pulled the synced value may show it once more.

### Reading the tree

- **Trunk = the repo, branch = a folder, leaf = a code file.** A branch is as thick
  as the square root of the code beneath it (sizes are the files' bytes, ~32
  bytes a line). A folder with no sub-folders is a round clump of leaves on its
  own twig; chains of single-child folders are one branch; boilerplate source
  roots (`src/main/java/com/acme/…`) are not labelled, the first telling folder
  below them is.
- **The roots hold what keeps the tree standing: tests under the branch they
  test, docs, CI and config under the trunk.** Underground, every file that is
  not code is the **tip of a root strand** (a small round bead, bigger for a
  longer file), never a leaf; a folder of them is a fan of strands growing from
  its root limb.
  - **Tests.** A test named after a code file (`test_foo.py`, `foo.test.ts`,
    `FooTest.java`) roots under that file's folder, preferring a candidate it
    imports; otherwise under the deepest folder holding most of what it imports
    (`tests for backend/web`). Tests that say nothing are `other tests ·
    tests/`. A changed test lights its strand from the limb down to its tip,
    the way lit wood leads to a changed leaf.
  - **Docs, CI and config** (and any other non-code file: data, images, a
    folder that is mostly non-code) are the same tips and fans in a cooler,
    greyer soil tone. A code folder's non-code files (its README, its
    `package.json`) hang on their own limb beside that folder's tests,
    `frontend · non-code`; a folder's test data (fixtures, golden files) on
    `test data` beside the tests it serves. Everything with no branch of its
    own hangs as a clump of the **tap root** under the trunk, `docs, CI and
    config`: `docs`, `.github`, `repo root files`, `test data · tests/`. A
    group of more than 24 files splits into clumps by its next folder
    (`docs › api`). A changed doc is a lit tip, as a changed test is.
- **Birds = agents.** This session is the bird in the accent colour (●); other
  live sessions on the same repo fly too, in colour-blind-safe secondary
  colours with their own glyph (▲ ■ ◆). A bird **nests** in the deepest folder
  holding most of its recent edits (its eggs are in the bird's colour; the
  nest's folder is named by the same pill as any changed folder) and
  **perches on the leaf** it is touching; a leaf it edited takes its colour, a
  **dot** = read, a **bud** = planned (a planned new file buds on the folder
  that will hold it). A refused edit is a red ✕ where the bird hit the rule.
- **Helpers = subagents.** Every subagent the session sends out (Claude's
  `Agent` / `Task` tool) is a smaller bird of its own, in a tint of its
  parent's colour with the parent's glyph HOLLOW and an index (○1, ○2 — the
  glyph, not the tint, is what tells them apart for colour-blind eyes, and a
  solid `●4` after a folder name is always a change count, never a helper), named
  `<agent_type> · <description>` on its card and `explore#1` on the tree. It
  flies out from its parent when its first call arrives, perches on its own
  leaves (its own dots, coloured leaves, ✕ and gold badges — "4 depend on
  explore#1's edits"), and when its Agent call returns (or after 120 s
  without a call) flies home into the parent's nest and fades. Finished
  helpers stay listed for 45 s, then drop off; their work stays on the tree
  as the parent's. While a helper is listed, files it edited are its own, not
  also the parent's branch changes.
- **Changes are the loudest thing; the rest of the tree is a calm silhouette.**
  The unchanged canopy is a muted, low-contrast grey-green; the wood from the
  trunk to every change is **lit** in the changing bird's colour, so the eye
  follows lit branches to the edited leaves. A changed leaf is drawn large and
  flat in its bird's colour at every zoom; only one changed in the last few
  minutes glows, the glow fading as the edit ages (so a glow always means
  "now"); seen from far out, a folder's changed leaves are nudged a few
  pixels apart so each change stays a separate, countable mark, and no label,
  tag or badge is ever placed over one. Labels are quiet lozenges with a
  neutral hairline (only the counts inside carry the birds' colours), so they
  never compete with the leaves. Each changed folder — the nest's folder too —
  is one clickable pill that names its limb —
  `backend › web/core ●4`, `frontend › lib ●2` (a nested folder always carries
  its top-level folder), with a count per bird in the bird's colour and glyph;
  a click opens its folder card (Keep out / Only here are there), and the
  folder gets a thin solid ring in the selection colour. One pill per chain:
  `frontend ●121`, never also `frontend › src ●121`, and a `src` that is its
  folder's only sub-folder is never named on its own.
- **Every name is attached to the one thing it names — never placed by
  nearness.** A folder's name sits on its own wood: just outside its clump's
  rim where its twig comes in (or flat across the twig), else along its branch
  like a river's name, else beside its fork, touching a small dot drawn on the
  fork. A leaf's name starts a few px past the leaf's tip, tied to the tip by a
  1 px hairline in the leaf's colour, and reads outward (to the right of a leaf
  pointing right, to the left of one pointing left, above or below an upright
  one; or hung off the tip by its corner when that clears a neighbour), or
  else on its base side, tied to the base the same way; a root tip's name (a
  test's, a doc's) sits past its tip. A name with no attached spot
  is shortened — `backend › web/core ●4`, then `web/core ●4` (only a nest's, a
  rule's or the open card's folder goes on to its bare `●4`) — or not drawn
  at all; it is never parked in
  the sky, on a leader or across the canvas edge, and a grey name never covers
  another leaf. So two `README.md` are fine: each sits at its own tip. Labels
  appear only on top-level folders and folders that hold changes, a nest, a
  rule or the selection; file names wait until you zoom close, and then the
  hovered file, the selected one, each bird's current file and the changed
  files come first (a changed name may lie over grey leaves, never over
  another loud one). No file is named at the whole-tree zoom (a leaf there is a
  few px, too small for a name to point at): the perched bird's tag names its
  file. A bird's tag names its file only when the leaf itself couldn't be
  named (usually the bird's body covers the tip): one name per leaf.
- **What could break is on demand, never the default layer.** Hover or select
  a changed leaf and the files that import it light up **gold** (no per-folder
  pills — the file card carries the count); **Show the N that depend on it**
  pins the full picture with per-folder badges (`web/core · 10 depend on …`,
  click one for the list). The bird index's **⚠ Could break** opens a ranked
  bar list of the changed files by importer count AND draws the same answer on
  the map: a soft gold wash over each clump holding files that import a
  change, with one gold count per folder (`⚠ web 15`, `⚠ backend · other 8`
  for a limb's smaller folders; the counts sum to the dependents, never a count
  inside a count; click one for the list). **Most at risk** (or a ranked row)
  shows that one file's dependents the same way without moving the camera; a
  second click or Esc clears it.
- **Off-screen birds** get a compact edge chip — glyph and arrow (`○2 →`) — on
  the edge of the visible canvas in the bird's direction (the details card
  counts as an edge); it slides along that edge, never into the tree, and never
  over a nest or the selection ring. Esc (or the followed row again) stops
  following and flies back to the view you had before.
- **⛔ Keep out** is a red territory, filled and hatched with a solid border:
  the hull of the folder's whole branch (from the fork it leaves) and every
  clump on it, grown by ~16 px on screen, so even a small folder seen from far
  out is an area, not a dot. It never covers what is not the zone's: another
  folder's clumps are cut out of it (the border runs round the cut), and so
  are a bird working outside it and the lit wood of a change outside it — a
  lit branch that only passes by is never read as working inside. Its sign
  sits on the territory's rim or inside it — `⛔ providers`, `⛔ Keep out ·
  backend/providers` once you zoom close, the bare `⛔` when there is no room —
  and once the rule has stopped an edit it says so (`⛔ providers · 1 edit
  blocked`). It prefers a spot well clear of every other folder's nest and
  glowing leaves, never sits on one, and no bird's tag is laid across a
  territory the bird is not working in. **✓ Only here** is the same territory
  in green (a spotlight: no hatching) and dims
  everything else to dusk — the names, pills, bird tags and lit wood outside
  it too; changed leaves outside stay visible but unlit.

**Semantic zoom:** at the whole-tree zoom folders are leafy clumps with ~25
names on a big repo (more on a small one) and root fans read as fibrous root
balls (brown for tests, grey-blue for docs, CI and config). Zoom in and the leaves open: every
leaf is its own shape on its own stalk, fanned out along its twig with its tip
facing out, and no two leaves of a folder overlap at any zoom. A leaf grows
with the zoom until it is ~24 px long; past that it barely grows (never past
~44 px) and the gaps between leaves open instead, so at the closest zoom even
the densest clump has room for every leaf's name at its tip. Then file names
appear; hovering a clump when zoomed in names ALL of its leaves — at their
tips where they fit, the rest in a column beside the clump (in tip order, each
tied to its tip by a hairline ending on a dot; a clump wider than the view
gets a column at each edge, on a light plate). Each root tip likewise becomes
its own mark. A breadcrumb (`repo › backend › web`) says where you are
once zoomed in.

**Moving around:** scroll to zoom (gently — one notch ≈ ×1.15, a flick never
jumps from a leaf to the whole tree), drag or arrow keys to pan, double-click a
folder to zoom to it, `0` / **⌂ Whole tree** (it fits the whole picture —
canopy and roots — into the canvas left of
any panel and right of the agents column, centred, as large as fits up to a
cap, so a small tree is drawn large; the soil is as deep as the roots need. A panel or
card that opens, docks or grows keeps a whole-tree view whole), **← Back** (`Backspace`) undoes
the last move, the minimap (bottom-left, shown only once you zoom in past the
whole-tree view — there it would only repeat the canvas) jumps. **Find file or symbol** (`/`
from anywhere in the pane, or with nothing focused while the pointer is over
it; never from a text field) flies to a file — or to the file holding a symbol
— and opens its card.

**Cards:** click a leaf (or a root tip) for its **file card** (lines, kind, used by N, imports,
what each bird did to it, its zone, then Classes / Functions — public first —
/ Routes / Tested by, from `GET …/code-map/file`) with **Show the N that depend
on it**, Centre, Folder, Open diff, **⛔ Keep out** and **✓ Only here** (or
**Allow this file** when it is outside the green scope). Click a branch, a
clump or a folder name for its **folder card** (files, lines, sub-folders,
which birds work there) with **Zoom to it / Fold / ⛔ Keep agents out / ✓ Only
here** (or Remove); a root fan opens its root card the same way (`tests ·
backend/web`, or what a support root holds: `the non-code files of frontend/
(README, config)`). The trunk is the repo card. Click a bird to follow it.

**Following a bird:** click a bird, its row in the bird index (or a helper's
row, or a rail row in a small pane). The camera glides to it (a jump under reduced motion) at a zoom
where its folder's and leaves' names show, then keeps it centred as it flies
from leaf to leaf, until you pan, zoom, drag, press `Esc`, click the card
again or click another bird's row (which switches). The followed row is marked
in its bird's colour (`aria-pressed` on the row's button).

**The bird index** is a compact column on the **left** of the Map at every pane
size — never a strip along the top. **Agents:** one row per bird (glyph,
colour, status, what it is doing now, `7 changed (2 by helpers) · web/core 4,
lib 2` — the session's count includes its helpers', so it agrees with the
pane header and with Changes' `this session 7`), its
helpers nested under it ("editing x.py" / "reading …" / "done"); a click
follows the bird, `▸` expands the row (task, nest, read / planned / could
break, its changed files most-depended-on first, and past eight **+N more —
all changed files** opens **Plan & lists** at Changed). A session that isn't
working says so in the sidebar's words (`idle`, `question`), never "done" — this
session's row reads `idle — waiting for your next message`, another session's
`idle · on its own branch`. This session's row always carries one **Next:**
line, by state: **N breaches — review** (Plan & lists at Breaches), a question
waiting in the terminal, **Review plan · Go** (at Plan), **Review: ⚠ Could
break (N)** + **Open diff** once files changed, or **Ask for plan** before
anything did; any **scope requests** get a line of their own.
**Changes:** where
the changes are (`Changes 9 in all`, then `this session 7 + other sessions 2`
— this session's 7 is the pane header's `7 files`; the split is left out while
no other session has changed anything, since it would only repeat the total —
and `backend 5 · frontend 4`; a quiet note says when the counts can't see
everything: *Showing the first 40,000 files* on a truncated listing, *Imports
still indexing — counts may be low* while the import graph is partial), **Most at risk · 73 import it — config/settings.py** (click to show its
importers on the map), **⚠ Could break** (the ranked list + the map layer), a
caption saying what the row buttons do (`⛔ keep out ✓ only here`), and a
row per changed folder (the five with the most changes, plus any a rule covers,
then **Show all N folders**) (`backend › web/core ✎4`, the agents' colour dots, its
first files) with **⛔** / **✓** buttons: blocking or allowing an area is one
click from where you read about it (a second click on the same button removes
the rule; a folder already inside a rule says so, and with an only-here zone a
folder of this session's outside it reads `already changed · exempt from ✓`,
and another session's folder reads `other worktree · not affected` — only
here binds this session's worktree, so another session's marks on the map are
never dimmed as if restricted; the row height never
changes, so nothing reflows under the pointer). The row buttons stay quiet
(half opacity) until the row is pointed at or focused, or the rule is on; on a
touch screen they are always full. **Rules** come last, once there is one (a rule
added from a Changes row lands below it): every rule, a click zooms there, ×
removes it; a rule goes by its short name (`providers`, `web/core`, the full
path on hover) and says what it stopped in one wording everywhere
(`1 edit blocked`). Helper rows say where the helper is on their own line (`in
backend/config · 2 changed`). A bird working inside a keep-out (or outside an only-here) says so
on its own "now" line (`⚠ editing Sidebar.tsx · in ⛔ keep out`), never on a
new one. The **Legend** is behind `?`.
**Activity** (each bird's reads, edits and refusals and each rule, newest
first; a row flies to its file) starts folded to its latest line — in a small
pane a one-line ticker, never a block over the roots; changes the feed never saw
(made before the map started watching) are one honest line, `3 files changed
before the map started watching`, never "Nothing yet". Labels and
badges keep off the HUD. Below 760 × 560 px the column becomes a slim left
**rail** (glyph + short name + changed count per bird, then `✎9 changed` /
`⛔1 rule` tiles — the rule tile only once there is one); `»` (or a tile) expands the full column over the canvas
(`Esc` or a click on the canvas folds it back) and cards open as a bottom
sheet that scrolls on its own, its actions pinned at its top; the paint-tool
hint sits at the bottom, so arming a tool never moves what you aim at. Toasts
keep right of the column, and the toolbar's buttons shrink to their icons (the
words stay their accessible names).

**Painting rules:** the toolbar's **Rules** group holds **⛔ Keep out**,
**✓ Only here** and **+ Rule…**. The first two arm a
tool; click a folder name, a branch or a leaf (a leaf paints its folder unless
you are zoomed in on single files) and it disarms — `Shift`-click to paint
several, `Esc` cancels. Each paint is one `POST …/red-zones` (keep-out: repo
scope; only-here: worktree scope); the tree draws what the server answers, and
a toast offers **Undo** (it says "Undone" only once the rule is really gone).
Clicking a branch that already carries the rule takes it off — a folder or
file card with the rule on offers **Remove ⛔ keep out** / **Remove ✓ only
here**. **+ Rule…** types a pattern instead. A **⛔ Keep out** over files this
session already changed asks first, wherever it is added from (the tree, a
card, a Changes row, + Rule…): "4 changed files are inside — pushing will be
blocked until they're reverted or you remove the rule" with **Add anyway** /
**Cancel** (only-here has its own exemption flow, below).

**Plan & lists** (toolbar) is the same session as lists — the accessible
twin of the picture: **Breaches**, **Scope requests** (green mode, each with
**Allow this file**), **Activity**, **Plan** (Ask for plan / Go / Go — only the
planned files), **Blast radius** (direct importers of what changed, per
folder), **Rules**, **Changed**, **Workers' fences** and **Other agents
here**. Blocks and breaches are also announced through a polite live region.
In a pane at least 1100 px wide it docks as a column beside the tree (the
canvas shrinks and a whole-tree view re-centres in what is left); in a smaller
one it lies over the tree's right side, and a card its rows open stacks above
it.

`openMap(title, {path, section})` (every "Open map" in the app) lands on the
Map at a file (flown to, its card open — once the tree is laid out, if it
arrives earlier) and/or a part of it: a Plan & lists section (the panel opens,
scrolled to it) or the ⚠ Could break layer.

**When a read fails** the Map says so instead of waiting forever: no snapshot
→ *Couldn't read the worktree* with **Retry** (even while the live poll
works); nothing at all → *Code map unavailable* with **Retry**; a later error
with data already on screen → a quiet *map may be out of date · Retry*.

**Fences an orchestrator set.** A worker its orchestrator fenced
(`fence_session`, `spawn_session(only=…, keep_out=…)`) sees the fence on its
own Map like rules — painted on the tree, listed under **Rules** as *this
session · set by orch* — but without **×** or **Allow here**: only the
orchestrator lifts it. The orchestrator's Map lists **Workers' fences**: each
worker with its **✓ only** and **⛔ keep-out** paths (click one to find it on
the tree) and the reason it gave. An edit the fence refused shows in
**Activity** as *outside fence* — never as a scope request with **Allow this
file**: the worker reports blocked and its orchestrator decides. A change to
the fence after it was set is an **Activity** line of its own — *fence
widened by orch: follow-up needs b*, *fence lifted by …*, or *fence ended —
session closed* / *fence done — its piece finished* when it simply ran its
course (hover for the paths that were in force).

**Cost:** nothing mounts until the tab is first opened, and nothing polls or
draws unless it is showing and the window is visible (`GET …/code-map/live`
every 2 s).

**Preload.** Opening the Map used to pay for everything at once: the server
listed the files and read every source file for the import graph (about 3 s for
a 3k-file repo after a restart), then the browser laid the tree out. Now three
things happen ahead of the click, so the first open is a cache hit:

- **The focused pane prefetches** its Map 1.5 s after it gets focus (git
  sessions, not already on the Map): `GET …/code-map` once, then the layout, into
  the same caches the tab reads. Nothing polls until the tab is opened, and
  only the focused pane does this, never all nine.
- **The server warms every session at boot**, one worktree at a time, starting
  45 s after boot (past the 30 s quiet window and the background revive's
  start, so it never competes with terminals and agents coming up), within 90 s
  in all (`MINDFLOCK_CODE_MAP_WARM=0` turns it off; it never runs under
  pytest). A Map opened before then still gets the prefetch below.
- **The per-file scan memo is kept on disk**
  (`~/.mindflock-assistant/.code-map-cache/`), so a restart re-reads only the
  files that changed since.

Measured with a server made to take 3 s for the snapshot: the Map became usable
3.2 s after the click without the prefetch and 0.4 s after it with the prefetch. The layout search runs in a Web Worker (`/codetree-worker.js`)
behind a "Growing the tree…" state with progress (a failed layout says
"Couldn't grow the tree" with **Retry**); its placements are cached
per repo and data in IndexedDB, so the next open replays them in milliseconds,
and a changed worktree warm-starts from the previous layout — unchanged
subtrees keep their place, a changed folder is pushed clear of its neighbours —
so a small change doesn't reshuffle the tree. Measured (Node, same engine):
cold 0.2 s for ~730 files, 0.8 s for ~2.9k, 2.6 s for ~4.8k; replay 10–60 ms;
warm 10–200 ms. The picture is re-drawn only when the camera, the data or the
hover changes (plus a 12 fps bob for perched birds; nothing animates under
reduced motion), into a static layer at ≤ 1.5× DPR plus an overlay.

### Zones: keep out (red) and only here (green)

A zone is a path glob. **⛔ Keep out** (red): the agent may **read but not
change** it (create, modify, delete or move). **✓ Only here** (green): while a
worktree has a green zone, the agent may change **only** what is inside a
green zone — everything else is read-only. Red always wins over green.
Patterns are gitignore-flavoured, over worktree-relative paths: a bare name
(`config.toml`, `*.pem`) matches at any depth, a path with a slash
(`backend/athena`) is anchored at the repo root, a leading `/` anchors a bare
name, `*`/`?`/`[…]` stay within one folder and `**` crosses folders (a `**/`
segment spans zero or more, so `backend/**/secret.py` also covers
`backend/secret.py`); a zone covers the path **and everything under it**.
Git-ignored files a red zone matches (the usual home of "my configuration
file") are guarded too. On a case-insensitive filesystem (macOS, Windows)
zones match ignoring case — on the map as in the guard; the Map classifies
every path with the same predicate the guard, the monitor and the push gate
use.

Make one from wherever you are looking: paint it on the tree (the toolbar's
**⛔ Keep out** / **✓ Only here** tools, or a folder or file card's buttons),
click **⛔** on a blast-radius row, or **+ Rule…**. The inline **+ Rule…** row
starts with the kind — **⛔ Keep out | ✓ Only here** — then the pattern (a
map-picked root-level path is entered as `/name`, so it means that one path),
an optional name, the scope and **Tell the agent**. A keep-out zone's scope is
**Whole repo** (the default: every worktree and future session on the repo,
keyed by its origin URL) or **This worktree**. A green zone is always **this
worktree** — it scopes a *task*, and as repo policy it would lock every other
session (verify runs, intake sessions) out of their own files — and the row
says how many sessions share the worktree. The preview underneath says what
the pattern covers: for green, how many files become writable, under which
roots, and how many already-changed files would fall outside; an unanchored
green pattern (it matches at any depth) offers **anchor to /…**, and one that
matches nothing says the agent may only create new files there.

After a green add, files **already changed outside the new scope** are exempt
by default (they were changed before the scope existed, and stay exempt while
their content is unchanged): the row says "N files already changed outside
this scope" with **Keep exempt** and **Treat as breaches**.

**Green mode** dims everything outside the green zone(s) to dusk on the tree
(folder names stay readable; the minimap dims too), and the guard pill reads
**✓ Only here: providers**. Some
files outside a green zone are still writable as **companions**, flagged amber
and never a breach: lockfiles, snapshots, tests that import a file inside the
zone, and the repo's declared **derived outputs** (Zones… → Derived outputs —
e.g. a committed build bundle). Reads are never blocked — shell reads can't be,
and blinding the agent to the callers of what it edits breaks things — so a
read outside the scope is only noted, as **peeked outside scope** in Activity.
Every edit the guard refuses for being outside the scope is a **scope
request** with **Allow this file**, which adds an anchored green zone for
exactly that path and tells the agent.

The **guard pill** (in the toolbar once a rule or a breach exists; before that it
could only say "No rules") says how much the rules hold right now: **Guarded**
(Claude Code: the edit is refused before it happens, subagents included),
**Arming…** (installed, proven on the next tool call — also what a rule just
added reads until the guard's next check, never a flash of "No rules"),
**Detect-only (codex)** (no hook guard for this CLI: breaches are detected,
flagged and block pushes, but not prevented), **Guard off — re-arming** (the
hooks went missing; MindFlock is reinstalling them), **No rules**, or in green
mode **✓ Only here: …** (tinted amber/red by the same states). While any breach
exists it reads red **N breaches** instead, whatever the guard state. Hovering
it gives the server's one-line explanation.

A branch carrying a breach can't leave: **Push**, **Open PR** and **Merge**
refuse with the offending paths. The refusal is an error card (bottom right)
with **Open map** and **Push / Open PR / Merge anyway** — the override is one
explicit click for that one action, never a browser `confirm()` (which the
desktop app doesn't implement).

**Zones…** in the command palette manages keep-out zones per repo outside any
session (every repo you have a session on is listed, zones or not; each zone
shows its kind), holds each repo's **Plan first** switch, and its **Derived
outputs** (companions for green-scoped agents; the built-in lockfile/snapshot
set is shown beside them). It is a modal like Intake: `Esc` closes it from
anywhere, and `Delete` / `Ctrl+W` pressed inside it never close the session
behind.

### The plan loop

**Ask for plan** (mid-flight: **Ask what's left**) asks the agent to list every
file it intends to create, modify or delete, one `path — intent` per line in a
`mindflock-plan` block, and wait. While a plan exists and nothing has been
edited since, the Map is in plan mode: the blast radius grows from the plan
instead of from the diff, so you can see "this reaches ticket_ingestion"
before a line is written, add zones, and press **Go — with N zones** (carry
on; the zones added since the plan are named in the message). **Go — only the
planned files** goes further: it turns the plan into anchored green zones for
this worktree (each planned file exactly; a new file's parent folder) and tells
the agent its scope — "if you need a file that isn't in your plan, say so".
Once work starts, plan rows tick off as files change, anything edited that
wasn't in the plan is marked off-plan, and planned files outside the green
zones are flagged. Ask/Go/Tell are disabled while the agent is waiting on a
question in the terminal. Plan-first can be set at launch (the New-session
dialog's **Plan first** checkbox) or per repo (Zones…) for sessions started
from Intake. A Plan-first start opens on the Map's Plan section, to watch the
plan arrive.

A plan waiting for Go doesn't need the Map open to be seen: the server's
red-zone loop counts it (`redzone.plan_pending`, see web-api.md), so the rail
chip reads **plan · n**, the 🔔 lists **Plan ready — review & Go** (both open
the Map's Plan section), and the phone shows a plan card with **Go** and **Go —
only the planned files**. It clears once Go is delivered or the agent starts a
tool call.

### The rail chip

A session's sidebar row carries a zone chip when there is something to
say: red **⛔n** for breached files on the branch (or **⛔** for a block you
haven't looked at since you last opened its Map), amber **!** while the guard is
arming or off, amber **plan · n** while a plan waits for Go, and a quiet **🛡**
while it is guarded (**✓** when the worktree is scoped by green zones). "Since you last
looked" is kept on the server's clock (the block time is the server's), so a
browser on another machine, or a WSL2 clock that drifted after sleep, neither
hides a block nor keeps a seen one red. Clicking it selects the
session and opens its Map on the part the chip is about (`redZoneChip`'s
`section`: Breaches for ⛔, Plan for **plan · n**, Zones otherwise). Blocks, breaches and tampering also arrive as toasts
(clickable, straight to the Map) and in the 🔔 feed; the "Red zone blocked an
edit" notification rule is off by default, the other two are on.

## Notifications (🔔)

**The bell is the one place for everything that needs you.** Its panel (*Notifications —
what needs you, and what happened while you were away*, about 400px wide) has
two lists: **Needs you** on top, then the history feed.

**Needs you** merges two sources into one list, one row per session: the
sessions whose [one state](#one-session-state) is "in the bell" — **Question**,
**Pre-commit failed** (with the hook), **Setup failed**, **Offline**, **Checks
failing**, and an **Idle** session 20 minutes into uncommitted work — plus a
branch ready for its PR, and the `waiting` group of `GET /api/outbox`
(ask-first approvals, stuck team run lines, budget pauses, a lead's plan or PR),
and a session whose agent waits on Go for its plan (**Plan ready — review &
Go**, ranked like an approval; it opens the Map's Plan section).
The words are the state's own labels, the same ones the row's chip and the
toast use. It is sorted by urgency: answers first, then approvals and team run
escalations, then broken (pre-commit, setup, offline, stuck), failing and
ready. The list scrolls and shows six rows before **+N more**. What each kind
of row offers:

- **An agent's question.** Every session on a dialog gets the same answer
  strip its rail row has — the prompt's own buttons, **↗** to open the pane —
  not only an orchestrator's workers. A question the agent asked **in plain
  words** at the end of its reply (the row's `asking`, or an outbox `question`
  row) shows the question and **Reply**, which opens the
  [ask pop-up](#when-a-session-asks-you-something) — there is no key to press.
- **An ask-first approval.** The exact commit message and PR title, plus where
  its fast-track stops, *before* anything leaves the machine: **Diff** /
  **Commit** (or **Open the PR**), via `POST /api/instances/{title}/ship-now`. A
  commit-step approval can **edit** the message inline first; an approval held
  before its *push* has already committed, so it offers no edit. With no
  message previewed the row reads "written from the diff when it commits".
- **A stuck team run line** (stalled twice, hooks failed twice, a conflict, a
  create that failed after its retries) carries its reason and the server's
  actions for it: **Retry**, **Retry fresh**, **Open ↗**, **Skip**.
- **A team run budget pause** offers **Raise to $N** (an inline amount;
  `POST /api/runs/{id}/resume {budget_usd}`, raise and resume in one call) and
  **Stop** (`/cancel`).
- **A team run's plan or release** keeps its one-click quick action (**Approve
  the plan** / **Start N workers**, which approves into separate worktrees, the
  same default the Thread card preselects; the release's own button, such as
  open the PR) next to **Open the Thread ↗**, which opens the lead's Thread
  with the full card. A failed check offers **Run the check again**, a team run
  whose lead is gone offers **Cancel the team run**, and changes no piece owns
  (a `stray` item) offer **Open ↗**.

**One badge.** `#notif-badge` counts that merged list, so nothing needs you
that the badge does not count. The top bar has no second count, and the
desktop-notification on/off toggle lives only in Settings → Notifications.

**The switches govern every in-app channel.** The per-rule switches in
Settings → Notifications (*needs input*, *pre-commit failed*, and the
*setup failed* / *checks failing* / *offline* ids the client names for the
same states) gate the **toast** and the **tab badge** for that state; the bell
keeps showing it regardless — the bell is the source of truth, a switch only
quietens the interruptions. A rule the server does not list counts as on.

**Opening it from elsewhere.** Anything can open the panel by dispatching a
`mf-open-bell` DOM event (`document.dispatchEvent(new CustomEvent("mf-open-bell",
{detail: {title}}))`); with a `title` it scrolls that session's row into view
and flashes it. The rail's clickable **— open the bell** lines and a team run
`run.needs_you` toast without a lead use it.

**History** keeps a running feed of notable session events — finished,
needs-input, stage changes, cost-over-budget, auto-sent queued prompts — fed by
the events bus, **including the backlog replayed on connect** so it answers
"what happened while I was away." Events since you last opened it are marked
unread (keyed on timestamp so it survives server restarts); while nothing
needs attention, unread history shows as a small neutral dot on the bell, never
a number. Clicking an entry focuses that session. Verify adds three rows,
named **Verify · <checklist>** and opening that checklist in Verify: *ready to
check* (`session.test_plan_due`), *couldn't write the checklist*
(`session.test_plan_failed`) and *checklist failed — N steps*
(`session.test_plan_checked` with `new: "failed"`) — or *checklist failed — step
1 GET /health* when a **person's** Fail turned it red
(`session.test_plan_step_failed`) — each gated by its own rule
(`verify_plan_due`, `verify_plan_failed`, `verify_run_failed` for both failed
rows). A session that ended reads
**closed** when it was closed (✕, `Ctrl+W`, the palette — the worktree kept)
and **deleted** only when it was deleted (worktree and branch removed), never
the other way round. The rows read as sentences: a stage change says
*committed*, *pushed*, *opened a PR*, *merged*, *pre-commit failed* or *running
pre-commit hooks* rather than the raw stage key, and an auto-sent prompt says
*sent the next queued prompt (n left)*. A team run's rows are labelled with the
team run's name; clicking one opens the lead's Thread when the team run has a lead,
scrolls to its **Needs you** row when it needs you, and otherwise
closes the bell and reveals the team run's header on the rail (scrolled into view,
one pulse) — or, for a team run with no header, opens its lead's Thread (see
[Team runs on the rail](#team-runs-on-the-rail)). *No
notifications yet.* shows only when
both lists are empty.

**Agent messages stay out of the feed; worker reports don't.** A
`session.message` ([Agent teams](#agent-teams-mindflock-mcp)) becomes a row
only when it is a worker's report (`kind: "result"`): *worker api-billing
reported done — …*, filed under the parent and styled as a warning for
`blocked` or `failed`. Messages between agents get a toast and no row: the
feed has no dedupe, and an orchestrator's chatter would bury everything else.
The toasts are throttled (one per sender per 30 s for messages, one per worker
and parent per 30 s for reports) and never fire for a replayed backlog.

**Every notification leads with the session's rail slot** — *[3] sitecheck-bot7
has finished* — the same 1–9 number `Ctrl/Alt+1…9` jump to, resolved
client-side at render time so drag order and the live sidebar filter are
honored (the order is per-browser state the server never sees). Desktop
popups, bell rows and in-app toasts all carry it; rows past the ninth,
sessions the filter is hiding, and phone (ntfy) pushes — which have no rail to
agree with — carry no number rather than a wrong one. Extension pages get the
same resolver as `mindflock.slotNumber(title)`
(see [extensions.md](extensions.md)).

A raw idle transition no longer produces a row at all. The feed used to log
*finished — now idle* on every activity flip to idle, with no rule gate and no
dedupe — so it fired at the end of every assistant turn, between two prompts of
a draining queue, and for a re-opened window whose agent had run nothing. The
finished row now comes from `session.turn_ended` and says how long the quiet has
lasted — **finished — idle 25s**, **finished — idle 2m** — because the dwell is
the part that makes the claim trustworthy. `working`/`offline` flips remain
chip colours and are still filtered out.

**One rule list, two delivery channels.** Settings → Notifications is split that
way on purpose: *What you're alerted about* (needs-input, PR approved, PR
merged/closed, budget exceeded, pre-commit failed, ran out of usage / usage came
back — plus the noisy opt-ins, among them "changes requested") governs **both**
channels, and each channel decides only where an alert lands.

**"Your PR was approved" comes from the reviews, not from the merge button.**
The verdict is read off the pull request's own review list (the latest review
per reviewer; a standing "changes requested" outranks an approval, a comment is
not a decision) rather than from GitHub's `mergeable_state`, which reports
`blocked` both for a missing review and for a failing required check — and says
nothing at all once the approval lands. Like every PR signal it is seeded
silently on first sight, so a restart never re-announces an approval that was
already there, and a failed lookup is never a withdrawal.

**Verify has six rules of its own.** They ride the same narrow events the
Verify dialog refetches on — folding the halves together would silence the case
most worth hearing about. The three that are bad news or "your turn" are **on by
default**; the ambient ones are opt-in:

| Rule | Event | Default | Means |
|---|---|---|---|
| `verify_plan_failed` | `session.test_plan_failed` | on | The generation or rewrite produced no checklist at all |
| `verify_plan_due` | `session.test_plan_due` | on | The work shipped and its checklist is ready to check |
| `verify_run_failed` | `session.test_plan_checked` with `new: "failed"` | on | An agent's check found a failed step |
| `verify_plan_ready` | `session.test_plan_ready` | off | A checklist got written — there are steps worth showing |
| `verify_run_finished` | `session.test_plan_checked` | off | An agent finished working a checklist |
| `verify_run_gave_up` | `session.test_plan_gave_up` | off | A run was released without ever writing its answers |

`verify_plan_failed` was opt-in before round 4 (`was_opt_in`, so a saved choice
is kept). `verify_run_failed` matches because the emitter stamps `new: "failed"`
on a check with any failed step — a rule sees `old`/`new`, never `data`.
`verify_plan_due` is `ntfy: False`: its phone push is the driver's own
at-most-once announcement (`_notify_test_plan_due`), which honours this rule's
switch, so one toggle governs both channels. The ambient rules stay opt-in
because a flock that pushes all day writes one checklist per session per branch.
The bell's history feed does not list these events yet (see
`NotificationsBell.notifFromEvent`); the app toasts "… shipped to main — its
checklist is ready in Verify" when one comes due. A **push cannot carry the
numbers**: the templates fill `{session}` and nothing else, so where the in-app
toast says "8 passed, 3 need your eyes" the push says to open Verify — the
envelope's `failed` / `needs_you` are for the browser, and a body that invented
counts would be worse than one that points at them.

**"A session finishes its work" is a claim, and it is checked.** The opt-in rule
fires on `session.turn_ended`, not on the activity chip going grey. The chip
goes grey at the end of every assistant turn — the CLI's Stop hook cannot tell
"the work is done" from "this reply is done" — and also for a session you merely
re-opened, since attaching its pane relaunches a dead agent that then parks at an
empty prompt. Before the event fires, three things have to be true: MindFlock
*watched this agent work* in the tmux session that is running now — corroborated
by the CLI's own hook report or its live-turn status line, never a bare CPU
spike on a parked session — it has been idle since for a dwell sized to that
evidence (12 s when the CLI reports through hooks, 25 s for a status-line read,
45 s for the CPU backstop), and there is no queued prompt about to wake it. Each
cycle of work announces itself once, however long the session then sits there.

The two usage rules are a pair: **"A session runs out of usage"** fires on the
activity flip to `limit` (the agent is parked on its CLI's usage-limit screen),
and **"Usage comes back after running out"** fires on `session.usage_restored`,
which is emitted once per reopening by the watcher that resumes those
sessions — so it cannot fire for a window that merely rolled over while nothing
was blocked, and several sessions unblocking together still make one
notification. Whether those sessions are actually nudged to carry on is
Settings → General's **Resume sessions when usage comes back**; the
notification fires either way.

- **Browser / desktop** — the notify addon's `Notification` popup. Needs a tab
  open on a secure origin (HTTPS or localhost), so it is silent on a plain-http
  tailnet URL and says so in the toggle's status line.
- **Phone push (ntfy)** — optional, off until you turn it on, in the
  screen's **Phone alerts (ntfy)** fold. The *server*
  publishes to an [ntfy](https://ntfy.sh) topic your phone subscribes to, so an
  alert reaches you with MindFlock closed and no tab anywhere. **Set up** (the
  Phone alerts row) generates a long random topic AND saves it when none is
  saved, so the topic and its QR stay the same on every visit (**Generate**
  picks a new one; the empty field's grey text is just "press Generate",
  never an unsaved name). Scan the QR into the ntfy app, and
  **Send a test** confirms the round trip (with no topic it is disabled and
  says *Needs a topic first: press Generate.*); the row afterwards reports the last
  push (or why it failed). Point **Server** at your own ntfy instance to keep
  everything in-house, and give **Access token** a value only if the topic is
  protected — **Clear** beside it removes a saved one, since blanking the field
  means "keep" and a *wrong* token is worse than none (ntfy 401s a bad
  credential on a topic that needed no credential at all).
  *Tapping opens* is an optional URL the notification opens. You usually do not
  need it: when Tailscale is up every push already carries this machine's
  tailnet `/m` URL — in the message text and as the tap target — deep-linked to
  the session the alert is about (`/m?s=<title>`), so a tap lands on that
  session's terminal. Set the field only to send taps somewhere else; MindFlock
  strips an `?token=` from whatever you paste, since that URL is stored on the
  ntfy server. (The automatic URL is bare for the same reason.)

  **Include the agent's words in pushes** (`notifications.ntfy_agent_words`,
  on by default; hint *The spoken summary of a finished turn and the question
  of a dialog travel through your ntfy server*, plus *Yours is the public
  ntfy.sh, so a public server stores them* while that is the server) is
  pocket mode: the *has finished* push's body is the session's own spoken
  `Say:` line and the *needs you: Question* push's body is the dialog's
  question and the command it asks about, so a phone that reads notifications
  aloud says something useful while it stays in a pocket. Off keeps every
  body generic. Those two pushes open the phone page on the session with
  `&voice=1` (one tap speaks the line and listens), and a dialog push also
  carries one-tap **Yes** / **No** / **Open** buttons — single-use links
  good for ten minutes, never "always"; from the lock screen on Android,
  after unlock on iOS. The line under the toggle says the one thing that
  makes it work on an iPhone: *turn on ntfy under Settings → Notifications →
  Announce Notifications to hear pushes through AirPods.* See
  [Voice](#voice-talking-to-a-session) on the mobile page.

  Priority is per rule: "needs you (Question)", "budget exceeded", "pre-commit
  failed" and "PR approved" go out at ntfy priority 4 (buzzes through most
  do-not-disturb setups), PR merged/closed, changes requested, the two usage
  rules and the three verify rules that report an *outcome* at 3, and the
  ambient opt-ins (idle, pre-commit running, "verification plan ready") at 2 so
  they arrive quietly.

  You also get one push carrying the phone URL whenever it becomes newly
  reachable — at server start, when you switch this channel on, and when you
  turn on tailscale mode — so a phone that has never heard from this machine
  learns where it is without a QR scan at the desk. It is deduplicated (turning
  on push and tailscale mode back to back is one intent), and a URL that is not
  live until the server restarts says so.

  On the **public ntfy.sh server the topic name is the credential** — anyone who
  knows it can read your session titles or send you fakes — so keep the
  generated random name, or self-host. The screen shows that warning while the
  channel is on and pointed at ntfy.sh.

**Diagnosing a channel you can't see.** A push channel is invisible when it works
and baffling when it doesn't, so the row beside **Send a test** is the whole
diagnostic surface — no log reading required:

- Straight after a test it shows that test's verdict: *"Sent — check your phone."*
  or the failure reason. The reason is ntfy's **own** sentence when the server
  sent one (`{"error": …}` from its JSON body — "topic is reserved", an auth
  refusal), otherwise the transport error (DNS, TLS, timeout).
- Otherwise it shows the last *real* push, either **"Last push sent 4 min ago"**
  (relative: "just now" under a minute, then minutes, hours, days) or
  **"Last push failed: …"** with the same surfaced reason. Blank means nothing has
  been attempted yet this server run — the record lives in memory, so it resets on
  restart, and a fresh reload of a long-running server shows the real history.
- A missing QR is not a failure: it means the optional `segno` package isn't
  installed, and the subscribe URL printed beside it is the intended fallback.
- One failure mode this row will *not* show you: the 60-pushes/hour runaway cap.
  Throttled pushes are dropped before any HTTP attempt and deliberately don't
  overwrite the last result, so the symptom is a cheerful "Last push sent …"
  next to a silent phone. **Send a test** is exempt from the cap and will still
  succeed; the evidence is a single line in Settings → System logs
  (`ntfy: over 60 pushes/hour — dropping further pushes this window`).

Delivery is best-effort in one direction only: **a failed push never touches the
session it was reporting on.** The event that triggered it has already been
emitted, the websocket clients and shell hooks have already seen it, and the
browser channel fires independently. A wrong token or an unreachable ntfy server
costs you the phone alert and nothing else.

### When a session asks you something

A session asks in one of two ways, and both read as the same **Question** —
its chip, its toast, its bell row and the tab badge: a **dialog** on its
screen (a permission prompt, a plan approval, a choice — activity `clarify`),
or a reply that **ended with a question in plain words** that nobody has
answered yet (the row's `asking.kind: "question"`, `session.question`). A
question in words keeps the session in the bell instead of Idle; the 20-minute
*Idle* rule is for sessions that are truly idle.

**The pop-up.** When a session shows a **dialog** — a permission or choice
prompt, Claude's own question prompt included — a small card opens in the
**middle of the screen** by itself (`AskDialog`): the session's rail slot and
name, the kind (*Permission*, *Plan approval* or *Question*), the question and
the command it wants to run in a monospace box. A question in plain words at
the end of a reply never opens it by itself (a reply that merely says
"question" is no prompt): it keeps its bell row, rail strip, toast and OS
notification, and their **Reply** opens the card on it. Then:

- a dialog MindFlock could read: **its own options**, the answer strip's
  buttons with full labels — **1–9** pick by number, **Y / N / A** pick yes /
  no / always, **Enter** presses the highlighted one, and *always* is never the
  highlighted one. The answer goes through the same checks as the strip (the
  dialog's id; a changed prompt is shown instead of answered; *Already
  answered* when someone beat you to it). **No…** puts the keyboard in the
  session's pane, where the agent waits to hear what to do instead;
- **Claude's question prompt** (its AskUserQuestion tool; `GET /dialog` kind
  `select`): a row of **tabs** like Claude's — each question's label, ✓ once
  answered, the shown one lit, *Submit* last — then the question and its
  options, each with its description underneath. A **single-select**
  question: a press (or its digit) picks it, and the card reads the prompt
  again and shows the **next question in place** — it stays open while the
  dialog id changes step by step (`lib/askSelect.ts` `selectFlows`), and a
  409 `dialog_changed` there is just the next step (*The prompt changed* only
  when a different kind of prompt came up). A **multi-select**: checkboxes —
  a click or a digit ticks one on the card — and **Next ›** (or the
  prompt's own *Submit*, `button`) sends the keys of the boxes ticked
  differently from the server, in order, in one post, then Right
  (`nav.next`). **‹ Back** is Left. **Type something** opens a text field:
  Enter posts its digit, then `{text, keys: ["Enter"]}`; **Chat about this**
  opens one whose words go to the session as a prompt (its digit, then
  `/send {text, dialog_safe: true}`). The **review** step reads as a
  confirmation: the answers listed, then **one** primary **Submit answers**
  beside a plain **Cancel** in the dialog-button row — no digit badges there
  (1 / 2 still press them; Enter is Submit answers, Esc is Later). The step
  buttons sit in that same row: **‹ Back** on the left, the step's one
  primary on the right — **Next ›** on a multi-select, labelled from the
  prompt's own `button` (**Submit**, no arrow, on the last question). Every post carries the
  option's own `keys` (with Claude's cursor on the text row they read
  `["Up", "1"]`, never a bare digit) and is pinned to the dialog id you saw
  (`dialog_id` — it holds across ticks and while the text row is typed into);
  a later post of the same answer refused as changed or just answered is sent
  once more without the pin. A small **Cancel the
  question** link presses Escape (the whole tool). Keys: *1–9 picks*, *1–9
  ticks · Enter next*, *Enter submits*. The rail and bell strips send a
  multi-select or a review to the card (**Reply**) instead of pressing a box;
- a dialog it couldn't read: the question and **Open the pane ↗** — the prompt
  needs the terminal;
- a question in words (opened with Reply): an **answer box** — **Enter** sends it
  (`POST /api/instances/{title}/send {text, dialog_safe: true}`, which types it
  and submits it), **Shift+Enter** is a
  new line — and **Open the pane ↗**. If a dialog came up meanwhile the send is
  refused and the pop-up shows the dialog.

**Later** (**Esc**) puts it off: a question is dismissed on the server
(`POST …/asking/dismiss`), a dialog is snoozed until its prompt changes; the
bell keeps either. After an answer the card steps to the next session still
asking, or closes. Several asking at once share **one** card with a **1 of N**
stepper (‹ ›), the longest-waiting first — the dialogs, and a question in
words only once you opened it with Reply; `Ctrl+K N` still walks everything
that needs you.

It does **not** open by itself for a question in words; for a dialog the
server could not parse (no options to answer on the card — it could only say
*open the pane* — and a misread frame of a busy screen is the usual reason
there is one: that ask stays in the bell, the rail and the toasts); for an ask you already
answered, put off or opened in its pane; for the focused session when you typed into it in the last
10 seconds (you are already answering it there); or over another dialog (it
waits for that one to close). It waits for the keyboard to be quiet for a
moment first, so a word you are typing elsewhere isn't cut in half. The bell's
**Reply**, a Question toast and the OS notification open it on demand — even
for one you put off.

**Answering by voice.** The card has a mic button with the Talk hotkey beside
it (*Hotkey: Ctrl+T*). While the card is up, that button, the hotkey and the
top-bar mic all listen for **this** session's answer: for a dialog, say "yes",
"no", "always" or a number (pressed only with *Approve dialogs by voice* on in
Settings → Voice — otherwise it says "Approving by voice is off"); on
Claude's question prompt, a number or an option's words answer the shown step,
"next" and "submit" move on (the voice router answers it with the option's
keys), and "type something" opens that field — the next words are dictated
into it and **over** sends them; for a question, the words are typed into the
answer box as you speak (*Answer Login fix — say “over” to send, “over and
out” to stop, “disregard” to start over*), **over** sends them (*Sent.*) and
listening goes on — on the focused session once the card has closed or moved
on — **over and out** sends them and stops, and **disregard** clears what was
said; a stop phrase ("stop recording") stops and leaves the text. The card reads its step again every
2 s, so an answer said (or typed in the pane) shows at once. With *Read
replies aloud* on (either choice), the card reads the ask out as it opens —
*"sitecheck-bot7: Allow git push? Say yes, no, or always."*, each step of a
question prompt once — and, once nothing else is waiting to be said, listens
for 12 seconds, with one retry; nothing is ever spoken with it off.
**Speech stops when you act**: any hand on the card — a click on an option, a
tick, Back / Next / Submit, typing in its field, a digit or Enter — cancels
the read-out of **that** ask, the line being said and everything queued for
it (other sessions' lines play on), and ends its 12-second listen. When the
card steps to the next question the previous step's read-out is cancelled
first, and the new step is read only if nothing on the card was touched by
hand in the last 3 s — clicking through by hand is never narrated at; a
voice-driven flow is. You can also answer **before it finishes speaking**:
the card's mic, the Talk hotkey or the top-bar mic cut the speech and listen
at once (see [Voice input](#voice-input)). The card is
the one hands-free entry for a dialog: the top-bar mic no longer reads dialogs
out by itself while the pop-up is on, so nothing is read twice.

**When MindFlock is in the background.** The desktop app raises an OS
notification — *[3] sitecheck-bot7 needs you: Question*, with the question as
the body — at most one per session per 30 seconds, and flashes the taskbar
button (bounces the dock icon on macOS). Clicking it brings the window
forward, selects the session and opens the pop-up on it. The browser
notification addon leaves these two to the desktop app, so nothing pops twice.
In the window, the **Question** toast does the same on click. Both follow the
*needs input* rule.

**The switch.** Settings → Notifications → *Pop up a session's permission and
choice prompts in the middle of the screen* (`notifications.ask_popup`, on by
default, saved through the notification settings view). Off, asks stay in the
bell, the rail and the toasts (a Reply still opens the card). Under it, *Count
a question asked in plain words at the end of a reply*
(`notifications.prose_questions`, **off** by default) decides whether such a
question counts at all: off, only a real prompt — a permission, a plan
approval, Claude's question menu — raises a Question; on, a reply ending
with a question also puts a Question on the row, in the bell, the toast and
the push, answered with Reply.

## Diff → instruction

Select any text in a pane's **Diff** tab and a floating **✦ Ask agent** button
appears. It sends a one-line message (the file + a short excerpt + your note)
into that session's agent and flips the pane to the Agent tab — acting on a
review finding without leaving the diff.

## Bulk session actions

Select sessions right on the rail: **Ctrl/⌘-click** a row to pick it,
**Shift-click** to pick the range from the last pick, or row menu → **Select**
(also *Select sessions…* in the palette). Picked rows are tinted with a ✓ where
their slot number was, and while select mode is on a plain click toggles a row
instead of focusing it. A strip pinned to the top of the list counts the picks
(**All** / **None**) and offers **Close** (keeps their worktrees), **Hide**,
**Delete missing** (rows whose workspace vanished), or **Delete…** (removes
their worktrees and branches). Close and Delete ask first on an inline strip
(*Close 3 session(s)? Keeps their worktrees — reopen from Recently closed.* /
*Delete 3 session(s)? Removes their worktrees and branches.*, with the verb and
**Cancel**) — never a browser `confirm()`, which does nothing in the desktop
app. Actions fan out over the per-session endpoints and tolerate individual
failures: the toast counts what landed (*Closed 2/3 session(s)*, plus *N worker
sessions kept running* when workers stayed up), failures are named on a
bottom-right card, and a folder another live session holds is reported in the
strip (*Folder in use by …. Close those first.*). **✕** or **Esc** leaves select
mode and drops the picks.

## Signing in

When the server is exposed beyond localhost (tailscale mode) it requires the
access token (Settings → Security, `mindflock token`, or the startup banner): a
browser hits a small login page, or scan the banner QR (`/m?token=…`) to land
signed in. The desktop app signs itself in: when its window lands on the
sign-in page it reads the token in the engine's environment (inside WSL on
Windows) and reloads with `?token=`, so turning on Tailscale mode never locks
it out of its own server. See the API doc's
Authentication section. If the token leaks, regenerate it from Settings →
**Security** (below).

## Finding, renaming & attention

- **Filter** — a filter box above the sidebar list (`/` focuses it, Esc
  clears). It matches title, alias, branch, and repo, and only narrows the
  sidebar — the grid is untouched. Customize → *Session list* → **Filter box**
  shows or hides it (shown by default); hidden, `/` still opens it for one
  search.
- **Rename** — row menu → *Rename…*, the palette, double-click the row's
  name, or `F2` on a focused row. (A click on the row that is already
  selected selects it again — back to its terminal, and a narrow window's
  drawer closes — it never starts a rename.) The new name is the session's name everywhere: the rail, the phone page
  (`/m`, via the `name` field on `GET /api/instances`), and notifications — the
  old one is not kept around in tooltips. Names are unique: a rename to a name
  another session already has (its title, its rename, or its rail label) is
  refused inline, and the server refuses it too (`POST /api/aliases` → 409),
  as it refuses creating a session under a name another session goes by. The
  underlying session/tmux/worktree keep their original key, so nothing can
  break. **Notifications use the name you see**:
  a desktop notification and an ntfy push both call a session by its rename, or
  — for the ones nobody renamed — by the label the rail shows (`(tix)
  add-dark-mode/sc-12345`), never by the machine slug the event carries. A push
  about a session you cannot find in the rail is worse than no push.
- **Attention** — sessions that need you (every "in the bell" state of the
  [one session state](#one-session-state) whose notification switch is on,
  not only questions) badge the tab **title** (`● (n)`) *and* the **favicon**
  (a red dot), so a backgrounded tab is noticeable; focusing the session, or
  the tab, clears it. The 🔔 bell keeps the durable list, prioritized: waiting
  on your answer, then approvals and team run escalations, then broken
  (pre-commit / worktree setup / offline / stuck), then checks failing, then
  ready to move.
- **Wedged-session watchdog** — the lowest-noise entry on that list: a session
  idle for more than **20 minutes** while still holding **uncommitted** changes
  reads *idle 25m with unfinished work — possibly stuck*, i.e. an agent that
  stopped in the middle of something with nobody typing. A `committed` branch
  does **not** count as unfinished work — git considers it done and the header
  is merely asking you to push — so committing and walking away is not flagged.
  This rule only started rendering at all once `activity_since` was populated
  (see [web-api.md](web-api.md#get-apiinstances)); before that it read a field
  that was always `0` and never fired.
- **Undo** — reversible actions (hide, bulk hide) show a *click to undo* toast
  instead of a modal; only Delete (worktree and branch gone) still asks first,
  inline.
- **Hidden sessions stick** — a session you hide stays hidden across reloads
  (persisted per browser); showing it again clears that. Expanded action menus
  are transient (a reload starts them collapsed).

## Keyboard shortcuts

Bindings follow VSCode wherever one exists. The desktop (Electron) app can
claim combos browsers reserve (`Ctrl+W`, `Ctrl+N`, `Ctrl+Tab`, `Ctrl+1…9`,
`Ctrl+Shift+T`); in a plain browser use the `Alt+…` / `Delete` aliases
instead. `Ctrl` means `Cmd` on Mac throughout. Press `?` in the app for the
live cheat-sheet — it is generated from the same table that drives the
bindings.

`Esc` closes the sheet. It is also the editor, once you ask for it: **Change
keys** in its header turns the rows rebindable (they highlight on hover) —
click one, press the new combo (`Esc` cancels the recording), and it takes
effect immediately; **Done** leaves the mode. The
row's `+` button adds an *extra* combo instead of replacing (starting from
the defaults, so an action can answer to several combos). Chord rows rebind
their second key (the `Ctrl+K` prefix is fixed). A `↺` next to a customized
shortcut restores that one default; **Restore defaults** in the sheet header
clears every customization. Overrides are saved per browser/device (in
localStorage), since a Mac laptop and a Windows desktop usually want
different combos. Combos already in use are refused, bare letter keys are
refused (they'd fire while you type), and customizing a shortcut retires its
built-in alias.

| Keys | Action |
|---|---|
| `Ctrl+P` / `Ctrl+Shift+P` | Command palette (fuzzy: focus session, rename, send/queue, commit/push/PR/IDE, work, settings, doctor, shortcuts…) |
| `Ctrl+B` | Toggle sidebar |
| `Ctrl+N` / `Alt+N` | New-session dialog |
| `Alt+I` | Intake — tickets, PRs and issues waiting to become sessions |
| `Alt+V` | Verify — pushed changes nobody has checked |
| `Alt+S` | Schedule — templates that start sessions on their own |
| `Ctrl+T` (`Alt+Space` is the browser-safe alias: Chrome reserves Ctrl+T for a new tab) | Talk to the focused session through the top-bar mic — hold it (hold-to-talk) or press once to start and again to stop (click-to-talk), following the mic's push-to-talk mode. Works while a terminal has focus; the keystroke never reaches the terminal (no readline transpose; Option+Space on a Mac types no non-breaking space either). Key-repeat while held is ignored, and letting go of either key — or switching away from the window — ends a hold. The keymap only reports the key: it fires a `mindflock:voice-talk` window event with `detail.phase` `"down"` / `"up"`, and the mic decides what that means. Rebindable like the rest; a rebind applies to the release as well as the press |
| `Ctrl+Tab` / `Ctrl+Shift+Tab` (also `Ctrl+PgDn` / `Ctrl+PgUp`) | Next / previous window — sessions and open windows (assistant, logs, extension panes), one list |
| `Ctrl+1…9` / `Alt+1…9` | Focus the Nth sidebar row — a session or a window, matching the number badges |
| `/` | Focus the sidebar session filter (when it's showing) |
| `?` | Keyboard-shortcut cheat-sheet |
| `Ctrl+R` | Reload the app — only while no terminal is focused; in a terminal it stays shell reverse-i-search |
| `Ctrl+K C` | Commit… (chord: `Ctrl+K`, then `C`) |
| `Ctrl+K P` | Push |
| `Ctrl+K R` | Open PR |
| `Ctrl+K O` | Open/focus IDE |
| `Ctrl+K D` | Duplicate session |
| `Ctrl+K B` | Branch session — a new worktree on a new branch from the focused session, with its uncommitted changes ([more](#branch-session)) |
| `Ctrl+K H` | Hide/show window |
| `Ctrl+K M` | Code map — open the focused session's Map tab |
| `Ctrl+K S` | Message… — the Thread composer, addressed to the focused session |
| `Ctrl+K F` | Fast-track — the focused pane's ⏩ picker |
| `Ctrl+K T` | Thread — the focused session's workers and messages |
| `Ctrl+K N` | Next session that needs you — in the bell's Needs-you order (an answer first), wrapping; needs no focused session |
| `↑` / `↓` / `Enter` / `F2` (on a sidebar row) | Move between rows / select / rename. The list is one `Tab` stop (the selected row), plus any row carrying answer buttons |
| `Ctrl+W` / `Delete` | Close the focused session — moves it to Recently closed (reopen with `Ctrl+Shift+T`). Neither fires while **any** dialog or overlay is open (detected generically: the store's dialog slot, or any visible `[role="dialog"]`, `.modal`, `.dialog`, `.palette` or `[data-modal]` element, plus the bell, ⏩ picker, Prompts menu and break screen), nor while the keyboard is in an input, textarea, select, contenteditable or a terminal — `Ctrl+W` at a shell is delete-word, not "close this session". Never fires for a selected non-session window — those close from their ✕ |
| `Ctrl+V` (in a terminal) | Paste into the PTY: an image or file on the clipboard uploads and pastes its path, plain text pastes with a "Pasted N chars" toast, and an empty clipboard event falls back to reading `navigator.clipboard`. Same in session panes and the Assistant |
| `Ctrl+F` (in a terminal) | Find across the pane's **whole** scrollback, prefilled with a one-line terminal selection — **in the live pane**: a find bar floats top-right with an `n / total` count, the pane itself jumps to each hit, every visible hit yellow, the current one orange. It searches whatever scrollback the pane's mouse wheel moves, decided per pane, so it holds for every agent CLI. Where the app leaves the mouse to tmux (shells, codex, aider, gemini, cursor-agent, goose) it's tmux's copy-mode search. Where the app scrolls itself (Claude Code, opencode, cline) the server **indexes** everything the app would show the moment the bar opens — the window is briefly made tall at the same width, read top to bottom (PageUp/PageDown where the app pages with keys, measured wheel bursts where it doesn't), and put back — while the pane shows a frozen snapshot, so none of it is seen; the count is then instant, each step is one jump, and reopening find on an unchanged pane reuses the index. The server also keeps indexes current as the agent writes (one screen read every 1.5 s, nothing moves), and builds a missing one ahead of time for a pane nobody is using — agent idle (never while it waits on a question), no input from you for 30 s, screen still for 20 s, at most one screen attached — with that pane frozen meanwhile; any input into it cancels and snaps back. Claude Code's own pinned `❯ prompt` row is hidden the frame it's drawn (the prompt bar above mirrors it), so jumping never flashes it. Only rows that move with the content are indexed — never the input box, a status bar, a pinned prompt row or a floating hint. Only a pane that scrolls from neither (a pager on the alternate screen) opens the full-history view's find instead. Options, as in an editor: **Aa** match case (`Alt+C`), **ab** whole word (`Alt+W`), **.\*** regular expression (`Alt+R`), and **near** — a second term that must be on the same line or within N lines (1–50) of the first; both terms are highlighted and the options apply to both (matching is server-side, `core/find_query`, and painted with the same rules in the browser; the tmux path reads tmux's history rather than using tmux's own search, which can't do these). You start at the newest hit at or above what you were looking at: `Enter` / `F3` / `Ctrl+G` step **up** to older hits, add `Shift` to step back down, wrapping at the ends; `Esc` returns to live. Literal and case-insensitive unless the options say otherwise |
| `Ctrl+Shift+T` | Reopen the last-closed session |
| `Ctrl+Enter` | Submit the commit dialog |

Outward-facing git verbs (push, PR) sit behind `Ctrl+K` chords on purpose: a
chord takes two deliberate keystrokes, so no single mistyped combo can push a
branch — the old `Ctrl+Alt` single-stroke family collided with AltGr on
Windows international layouts. Like VSCode (whose
`terminal.integrated.allowChords` defaults to true), the `Ctrl+K` prefix is
claimed even while a terminal is focused; readline's kill-line is the accepted
cost (`Ctrl+U` still kills the line, `Esc` cancels a pending chord). **Merge
PR** is deliberately unbound — palette/menu only, behind an in-app confirm.
`Alt+O` is unbound: it opened the Outbox, which is gone. A rebind saved for it
(id `outbox` in `mf_keymap`) is dropped the next time the app loads, so it
neither counts as a customization nor holds its old combo.

## New-session dialog

Once a ticket tracker is connected (`caps.ticketing`), the dialog has two
tabs — **Session** and **Ticket**. Before that there is no tab strip at all:
filing a ticket needs a tracker, so a newcomer sees only the Session content.
Session is the landing tab on every open (including after a refused create),
because it is the hot path. Ticket is described further down.

The Session tab is two pages, as in v0.7: a compact **Describe** card it opens
on (*What do you want to work on?* — see
[describe it in a sentence](#new-session-dialog-describe-it-in-a-sentence)),
and the full form behind it (**More options**, which carries what you typed
into the Task, or **Let the agent pick the folder…** to have the model fill it
in). The dialog is a `role="dialog"` with `aria-modal`. The form has **← Back** to the sentence. The caret starts in the sentence
box; after a refused create the dialog reopens on the form with the caret in
**Name**, since the form is what needs fixing.

**What you type is never thrown away.** Closing New — or a trip to Intake,
Settings or anywhere else — keeps the sentence, the form and the page you were
on; the next opening shows them as you left them. Only a **Start** (or
Intake's **Start together…**, which opens New on its own list) starts fresh,
and **Clear** — beside the box on page 1 (the form's **← Back** leads there),
shown while something typed is in it — empties it on purpose. A Task the
opening's own last-used template wrote doesn't count as typed.

**Dialog placement.** The four top-bar dialogs — **New**, **Intake**,
**Verify** and **Schedule** — share ONE fixed top edge (`.modal.top-dialog`,
`components/Modal.css`), so they open at exactly the same y, hopping between
them never jumps, and a tab, fold or editor inside one never moves its top (a
centred card shifts by half of any height change). The edge is placed so a
typical card, New's full form with More options shut (`--dlg-anchor-h`,
620 px), reads as centred (~142 px down on a 904 px window); shorter cards sit
a little high, like a launcher. It is floored 16 px below the top bar (which
draws over every modal), so on a short window — the desktop app at 125 % zoom
is ~723 px — a card never starts under or against it; on a phone (≤480 px) it
sits straight under the bar. A card grows downward to 24 px above the window's
bottom (`--dlg-max-h`) and then scrolls inside. Every pane of New — Session
page 1, the full form and the Ticket tab — hangs from that edge at its own
natural height. Settings and the smaller dialogs stay centred. With More options shut the form fits
a 723 px window without a scroll; at 420 px the fields stack and Fast-track's
choices wrap.

**The form, in order:** the **Templates** strip (only once a template exists),
with one muted line under it — *Templates save this whole form (folder, agent,
task, fast-track) to run again or schedule.* — then **Folder** and **Agent**,
**Task**, **Works in**, then **More options** (shut). The footer has
**← Back** and **Start ↵** (the button names what Enter does).

- **Folder** — with one quiet line of suggestion chips under it: recent
  folders first, each run headed by a muted **Recent** / **Started in** (the
  folder MindFlock was started in) / **Home**, 📦 for a git repo and 📁 for a
  plain folder. Whole chips that don't fit are hidden and the last chip says
  how many (*8 more…*); it unfolds the line into all of them, one run per
  line (**Fewer** folds it back; picking a chip folds it too). The line is
  left out when its only suggestion is the folder already in the field.
  "Recent" is the server's list: only folders a session was actually started
  in. While the field isn't being edited it shows the END of the path (the
  repo's name), the whole path in its tooltip. **A folder is never made by
  itself:** a path that doesn't exist reads *No folder at <path>.* in red with
  **Create this folder**, and both Starts stay off until it exists — the
  button makes exactly that folder (`POST /api/mkdir`, the parent must exist)
  and the form then reads it as an empty folder with no repo. A plan from
  **Let the agent pick the folder…** that proposes a new folder asks the same way.
- **Agent** — the agents this device has; the ones that aren't installed fold
  behind a last row, *More agents… (6 not installed)*, which lists them as
  *codex (not installed)* (`GET /api/providers/status`, this device only — an
  unknown answer never blocks; the chosen agent always shows). The default never starts on a missing agent
  when another is installed. Picking a missing one shows *<agent> isn't
  installed on this device. Install it in Settings → Agents, or pick another
  agent.* under the field, and **Start** (on either page)
  refuses — a session would otherwise sit "Idle" on *command not found*. An
  installed agent `GET /api/setup` knows is NOT signed in gets *<agent> isn't
  signed in yet — a session would stop at its sign-in screen.* **Sign in…**
  (Settings → Agents) — under the field and on page 1. A note, not a block.
- **Task** — what the agent starts on (blank starts it idle), always on the
  form — two rows to start, growing with its text up to 220 px, then
  scrolling — with **Insert saved text…** (built-ins first, then yours; it
  goes in at the caret, or after what is there with a blank line between —
  never over the Task) and
  **Manage…**, which opens the Prompts dialog and comes back to this form as
  it was — there, clicking a prompt puts it in this Task box (no *Paste
  into*: you are making a new session, not talking to a running one).
- **Works in** — two radios, on the form, each an answer in two lines beside
  its radio (title, then one muted line, both on the same hung edge — never
  wrapping back under the radio): **Separate worktree** `recommended` / *Its
  own branch, so several sessions can run at once* — **the default** — and
  **This folder directly** / *Edits your checkout as it is, on its current
  branch*. Under the worktree, for a git folder, the branch it will cut, on ONE
  line hung on the same edge (ellipsised when long, the whole of it in its
  tooltip): *New branch `me/add-readme-section-about-setup` · cut from `main`* (the configured
  prefix from `GET /api/repos/check`'s `branch_prefix`, plus the Name or the
  name made from the Task). It is the **real** name: a name made from the task
  that another session already has is numbered here exactly as the server
  numbers it (`session_create.free_title` — titles and renames), *New branch
  `me/add-readme-section-about-setup-2` · cut from `main` · “add-readme-section-about-setup”
  is taken, so this one is numbered*. A typed Name another session has says so
  (the server refuses it). With no Task yet it reads *New branch `me/…` from
  your task · cut from `main`*. When other sessions on this device already work in this
  repo (any way; Verify's runs aside), one line says who: *“<session>” is
  already working in this repo — a separate worktree keeps you apart.* For a
  folder with no repo it says the session will run in the folder itself
  instead.
  Picking **This folder directly** while another session on this device
  already edits that folder directly warns *“<session>” is already editing
  this folder directly. Pick “Separate worktree” so they don't overwrite each
  other.*
  For a real folder with **no git repo**, Works in asks that one question
  instead: **Make it a git repo first** `recommended` / *Turns on diff, commit
  and PR — git init and a first commit here* or **Just run in the folder** /
  *The agent edits the files as they are; no diff, commit or PR* (the default —
  nothing is git-inited unasked). Fast-track's rungs past Off are greyed until
  it is a repo.
- **More options** — only what a first session may want: **Name** (*optional;
  empty names it from the task* — the placeholder is the name it will get),
  **Fast-track** (*Off / Commit / Push / Open PR / Merge when green*; without
  GitHub connected **Open PR** and **Merge when green** are greyed with the
  session menu's own sentence, *MindFlock can’t open or merge the PR for you
  yet — …*) and **Save as template…**. Its summary names anything in it that
  isn't the default (*named x · Fast-track: Commit · from main · flags*), else
  *name, Fast-track, save as template*. Inside it, one more fold,
  **Advanced**, holds what a first-hour session can't use yet, its summary
  naming only what is in it: **Ask me before it ships** (*(needs Fast-track)*
  while it is Off), **Auto-split** (*Lets the agent break a big task into up to
  3 parallel sessions — you approve the split first.*), **Provision the
  worktree** (*also run the repo's setup and warm test caches* — with the
  workspace strategy), **Base branch** (only while there is a branch to pick;
  *Loading branches…* until the list lands), **Plan first** (*— the agent
  lists the files it will change and waits for your OK (Map tab)*) and the
  **Launch flags** fold. A flag that turns the agent's permission prompts off
  (`--dangerously-skip-permissions` and its kin) adds a line under the chips:
  *⚠ Skip permission prompts — the agent runs commands and edits files without
  asking you.* (the label and tooltip are `SKIP_PERMS_LABEL` /
  `SKIP_PERMS_TITLE` in `lib/vocab.ts`, for the chip itself too).

**Names come from the task.** With the Name empty, a session is named from
its Task: the first line's first few words, filler dropped, lowercased and
hyphenated, at most 32 characters (*Add a README section about setup* →
`add-readme-section-about-setup`; `-2`, `-3`… when taken). With no Task either
it is the `untitled` quick launch. The server does this
(`session_create.title_from_prompt`), so a create from anywhere — the form,
page 1, the API — gets the same name; the Name placeholder previews it.

**The by-hand path costs nothing.** The form is pre-filled with the template,
agent and account the last session in that folder was started with — so
Create (or Name → Enter) starts the session with no model turn. The memory is
per folder and per browser (`localStorage` `mf_new_session_last`, the last 32
folders), written on a successful create, and applied only while that
template still exists and that agent is still installed; it never carries a
name, a prompt, launch flags or a base branch.

For a new worktree in a git folder, **Base branch** (*— the worktree is cut
from it and diffed against it*) lists the branch checked out there first, then
**Local** and **Remote** branches (`GET /api/repos/branches?path=`, 400 ms
after the Folder settles; the first 200). Leaving it on the first row keeps
the server's own choice; any other row is sent as `base_ref` + `base_branch`
on create. **Ctrl/Cmd+Enter** submits the dialog from anywhere in it.

**Save as template…** (under More options) opens an inline name row in place (Enter saves,
Esc cancels without closing the dialog; never a `prompt()`) and saves the
**whole form**: folder, agent, where it works, provisioning, git init, Task and
fast-track — stored as the template's autopilot (*Commit*, *Commit + push* or
*Open a PR*; a template never merges unattended, so *Merge when green* saves as
*Open a PR*) — not Plan first or Auto-split.
A name another template already has (ignoring case) is refused on the row —
*A template named “nightly” already exists — pick another name.* — since the
server upserts by name and would replace it. The strip appears (or updates)
at once — no reopen — and the toast (6 s, clickable) says where: *▶ on its
chip at the top of New runs it, ⏰ schedules it. Click to open Schedule.*

**One Templates list.** The **Templates** strip (*Templates — ▶ runs one now ·
⏰ schedules it · its name fills the form*) is on **both pages**: under the
sentence box on page 1 (not for an Intake batch) and at the top of the form.
Each template is one pill: its **name** fills the form, its autopilot as the
fast-track,
and opens More options when the template set something there (on page 1 it
also moves to the form; a template with no task takes the sentence); the Name
is left empty so the session is named from its task. **▶** starts a new
session from it now (`POST /api/templates/{name}/run`, greyed with the reason
when it has no folder) and New steps aside, keeping your draft; **⏰** opens
[Schedule](#schedule) on that template's editor with its schedule on. A
scheduled template's ⏰ says its time on its face (*⏰ 07:30*, *⏰ paused*) and
its tooltip is the same one schedule line the Schedule card shows (*Weekdays
at 07:30 EDT · next tomorrow 07:30 — change or stop it*).
**Open Schedule…** opens **Schedule** (also the top bar's **Schedule**, `Alt+S`,
and the palette's **Schedule…** / **Templates…**). ▶ and ⏰ are this device's: they are not drawn when the
session would run on another device.
Closing Schedule brings New back **as it was left**, with the list
reloaded.

<a id="schedule"></a>**Schedule** (top bar, `Alt+S`, `#schedule`; `TemplatesDialog.tsx`, store key
`schedule` — `openDialogFor("templates")` still opens it) is the home of
templates and of what starts by itself. It opens on two lines: *Templates
start a **new** session — when you run one, or by themselves on a schedule.*
and *To send text to a session that's already running, use Prompts.*, with
**+ New template** and **Close** in the header. Top to bottom:

- **Next** — one band: *NEXT nightly-deps in 2h — today 21:00*, the soonest
  run of any schedule that isn't paused (or *Schedules start on mac-mini —
  the computer that runs schedules.* when another device fires them).
- **Scheduled (n)** — the scheduled templates, soonest next run first, then
  any with no next run, paused ones last. Each card's **strip** holds THE
  one schedule line — `templateScheduleLine()` in `lib/templates.ts`, the
  same words New's ⏰ chip and Intake's Auto-start show: *Every day at 07:30
  EDT · next Fri 07:30 (tomorrow skipped)*, *(mac-mini's time)* after the
  zone when another device fires schedules — then how far off that is (*in
  2h*), and its three switches: **Change schedule** (the compact schedule
  row, below), **Skip next** (*Let today 21:00 pass without a session — the
  one after runs as usual*; the line then says *(today skipped)* and the
  strip offers **Undo skip**) and **Pause** / **Resume** (keeps the cadence,
  starts nothing; the strip says *Paused*). Under it: the last run (*Last run
  yesterday 21:00 · scheduled → nightly-deps-oct13*, the session name a link
  that closes Schedule and shows that session, or *nightly-deps-oct13
  (closed)* once it's gone — the same in **Recent runs**), or *Last run
  failed: …* in red (the card's border turns red, and the top bar counts a
  scheduled one), or *Skipped …: the previous run is still working*, and the
  **Recent runs (n)** fold. A one-off's card reads *Once on Wed 28 Oct at
  21:00 EDT · next Oct 28 21:00* until its minute, then *Ran once on Wed 28
  Oct at 21:00 EDT* (or *Skipped once on …* / *Failed once on …*) with no
  next run and no **Pause** — the template keeps its schedule, spent, and
  **Change schedule** picks a new date; the top bar counts it only when that
  one run failed. With nothing scheduled the section says *Nothing
  runs by itself yet — Schedule… on a template below starts it every day,
  weekday, once or on any cron.*
- **Not scheduled (n)** *run now or add a schedule* — the templates without a
  schedule, each with **▶ Run now**, **Schedule…**, **Edit** and **Delete**.

Every card has **▶ Run now**, **Edit** and **Delete** at its top right.
With no templates at all the surface is one dashed panel: *Save a session as
a template once, then start it with one click — or every night by itself.* and a
**Create a template** button (the new-template editor). An editor with unsaved
changes is never dropped by the next click: another card's **Edit** /
**Schedule…** / **Delete**, **+ New template**, `Esc` or **Close** first show an
inline *Discard changes to “name”?* row (**Discard** / **Keep editing**; `Esc`
keeps editing), and a press on the backdrop doesn't close it. Saving under
another template's name is refused (*A template named “x” already exists —
pick another name.*). **Edit** opens the
editor inline in the card — the New form's own parts, laid out the way New
lays them out (`TemplateEditor.tsx`; `scope="tpl"` ids): **Name**; then the
New form's Folder row — **Folder** (placeholder *The repo it works in*; a
NEW template starts on the most recent repo, so it can run as soon as it's
saved; the field also takes a folder name to look up, as in New) with
**📁 Browse…** (the same floating folder picker; Escape closes the picker and
puts the old folder back before it closes the editor) beside **Agent** (New's picker — this device's
agents with *Your default agent* first, one this device lacks tagged *(not
installed)* as in New, and its **Manage** link), the two on one baseline and
one height; under them one line of one-click folders — *Use: 📦 repo* (repos
and recent folders, up to three) — and New's *No git repo in this folder…
Create one* line for a plain folder; a missing agent says *codex isn't
installed — **Install** or pick another agent* under them (red, and Save
refuses, once the template is scheduled: it would fail every night with
nobody watching); **Task** (*— what each session from it
starts on*), New's box that starts at two rows and grows. Below a rule, New's
label column: **Works in** — New's own row: **Separate worktree**
`recommended` with **Provision the worktree** (*Also runs the repo's setup and
warms test caches*, and its workspace strategy once ticked) hung under it,
**This folder directly**, and **Create a git repo in this folder** (*git init +
first commit — turns on diff, commit and PR*; not offered for a folder that is
already a repo); **Schedule** — **Run on a schedule** (*Starts a new session
from it, by itself*); and **Fast-track** (*When the agent stops. Applies to
Run now and to scheduled runs.* — *Off* / *Commit* / *Push* / *Open PR*, the
ladder's own words; never *Merge when green*: a template's runs are
unattended), the template's autopilot — picking one moves *This folder
directly* (whose line then reads *Can't commit or open a PR for you — needs
its own worktree*) to the worktree and the other way round, as in New. *Open
PR* with GitHub not connected says *Needs GitHub — **Connect GitHub*** under
it (the server refuses that save with `github_required`). **Cancel** /
**Save** sit in a footer that sticks to the bottom of the list while the form
scrolls, with a refused save's reason (the server's own sentence) on the
left of that row. **Delete** asks on an inline row — *Delete “<name>”?* plus
*This also removes it from your other linked devices.* only when there are
other linked devices — **Delete** / **Keep** — and checks the delete
happened (*“<name>” was already gone*). **+ New template** adds one.
Templates live on the server (`/api/templates`, synced between devices).

**Run now and schedules.** **▶ Run now** (*Run now — starts a new session from
this template*) starts a fresh session — the template's agent, folder and
workspace — with its Task already sent, titled after the template and the date
(`rivian-update-oct13`, `-2` for a second run that day, so a nightly
template's sessions sort together), and focuses it (Schedule steps aside). The
palette offers the same as **Run template: <name>**. When the server refuses
(its agent isn't installed, no folder — a 409 recorded as a failed run) the
toast is its own sentence — *Couldn't start pr-bot: codex isn't installed* —
never *Started…*, nothing is focused, and the card turns red with *Last run
failed: codex isn't installed*. A template with no folder can't start a
session on its own: saving one says *Saved — add a folder to run it*, Run now
is visibly disabled and the card says *Run now and schedules need a folder.*
**Add one** (opens Edit with the caret in Folder and the field ringed); the
palette leaves it out. **Schedule…** (or a scheduled card's **Change
schedule**) opens a compact row inside the card — just the schedule, **Save**
and **Cancel** (and **Stop schedule** on a scheduled one, which saves it
unscheduled and offers **Undo** in its toast); **Edit** keeps the whole form.
Saving a schedule toasts its schedule line (*Saved “nightly” — Weekdays at
07:30 EDT · next tomorrow 07:30*). Either way the schedule is
**Every day** / **Weekdays** **at** a 24-hour time (a text field, *07:30* —
one format everywhere; *7:30* and *730* are read as 07:30), **Once** — a
date (the browser's date picker, starting on the first day the time on
screen is still ahead) **at** that time: one run, then the schedule is
spent — or **Custom (cron)**: a 5-field cron expression (`*/30 * * * *`,
`0 21 * * 1-5`, `@weekly`) that starts from the days and time on screen
(*Every day 07:30* → `30 7 * * *`, *Weekdays* → `30 7 * * 1-5`; switching
back keeps a simple cron's time). Every mode shows the server's reading
under it as you type, with the zone of the computer that runs schedules:
*Every day at 07:30 EDT — next tomorrow 07:30, Fri 07:30, Sat 07:30*, *Once
on Wed 28 Oct at 21:00 EDT — next Oct 28 21:00*, *Every 30 minutes — next
today 10:30, today 11:00, today 11:30*, *Every Feb 29 at 00:00 EDT — next
Feb 29 2028, 2032, 2036* (dates in another year name it), or why it can't
be saved — nothing more often than every 15 minutes, and a one-off's minute
must still be ahead (*Pick a date and time that are still ahead.*; a spent
one re-saves as it is, so editing its task never trips this, but a new date
must). A scheduled template needs a
folder and a Task. **Fast-track** arms each Run now and scheduled run with
that autopilot depth, so a nightly chore can end in a PR. The card then reads *Weekdays at 21:00 EDT ·
next today 21:00 · last yesterday 21:00 (scheduled) → rivian-update-oct12*
(*by hand* for a Run now; *starts on <device>* when another device in the
group runs automation and fires schedules), *Last run failed: …* when the
create was refused, *Skipped …: the previous run is still working* when a
scheduled run didn't start because the last one hadn't finished (or is
waiting on you), and a **Recent runs (n)** fold lists the last 20 starts,
failures and skips. A scheduled run that fails or is skipped is also a bell
row (a failure is a default-on push, *A scheduled template run can't
start*). A schedule fires once per slot, starts counting from when it was
saved, and after the server was down fires only the most recent missed slot —
see [web-api.md](web-api.md) (Templates → Schedules). Scheduled templates are
also listed last on **Intake → Auto-start** (*Scheduled templates*: name and
the same schedule line the card says, with **Run now** — which closes Intake on
the new session — and **Open Schedule ›**), since they answer the same "what
starts on its own?" question as the ticket, PR and issue queues there. From a
shell, `mindflock run <template>` is Run now and `mindflock templates` lists
schedules and last runs.

### The folder picker window (📁 Browse…)

Every editable path field — New's **Folder** and its *Runs in* line, a
template's **Folder**, **Settings → Workspace → Workspace folder** — has a
**📁 Browse…** button that opens ONE floating, draggable window, **Pick a
folder** (`FolderPickerWindow.tsx`, store in `lib/folderPicker.ts`; the field
itself is `PathField.tsx` outside New). It is non-modal — the form under it
stays usable — and sits above every dialog and under the toast. Drag it by its
title bar (it stays wholly on screen, and at ~420 px it is as wide as the
screen less a margin); its corner resizes it; it reopens where it was left and
at the size it was left, per browser (`localStorage` `mf_folder_picker_rect`,
never a synced pref). Inside is the folder browser New always had (📦 repos /
📁 folders, *Click selects · double-click or → opens · ← up*, **+ Folder**),
the folder you are in on the top line and the chosen one in the footer beside
**Use this folder** (Enter; Enter on a row takes that row; ↑/↓ walk the rows).
**✕** or Escape closes it without a pick — Escape goes to the picker before the
dialog under it, except inside the + Folder name box. It opens on the field's
value, or the nearest folder above it that exists, or home. One window at a
time: another field's Browse… re-targets it (the first field's browse is
cancelled). A pick fills the field through its normal change path, so saving
works as before (the Workspace folder saves on pick, as it does on blur). In
New and the template editor a click in the picker already fills the Folder
field (the git nudge follows it) and Escape / ✕ put the old folder back.
Read-only path rows (Workspace's **Copy** lines) have no button.

**Browse… → + Folder** creates a folder on disk, inside whatever directory the
browser is currently showing — the path line directly above the name box — and
picks it. The parent is never in question, which matters: MindFlock has always
created folders as a side effect of **Create** (a Folder path that does not exist
yet is `mkdir`'d, and *create new repo* `git init`s it), and a bare name typed
into that field once produced a directory beside the server's own checkout.

**Plan first** (under More options) appends a request to the prompt: list
every file you intend to touch with a one-line intent, then wait. The session's
Map tab shows that list, and what depends on it, to review and fence off with
zones before you press Go (see [the plan loop](#the-plan-loop)). It needs a prompt to
ride on; with none it does nothing. It is only offered for a CLI that can
declare a plan the Map shows (Claude): for any other agent the checkbox is
disabled with "needs an agent with plan support — Claude", since that agent would
wait on a Go the UI can't send.

**Auto-split into up to N sessions if it's worth it** sits under More
options, below **Fast-track to** (`#new-split-row`), off by default, with a
− / + stepper for N (2 up to the server's `caps.team_runs.max_pieces`,
`MINDFLOCK_MAX_CHILDREN`; default 3). On a server that doesn't take splits
(`caps.team_runs.split` false) it is not shown at all, rather than shown
disabled. The form's **Prompt** is the task, however many lines it holds —
line breaks never split anything — and an empty Prompt is refused with
"Auto-split needs a task". Ticked, a sentence under the box says what will
happen, **Create** starts an optional split run (`POST /api/runs`, `split: true,
split_optional: true, max_pieces: N`, the Name as the team run's name), and the
account, Plan first, launch flags and git options do not apply
(choosing *This folder directly* unticks it). Off on Fast-track reads
Commit, as for any one-PR team run. MindFlock creates one session in a new
worktree of the form's **Folder**, named for the work, and its
agent reads the code and decides. Not worth splitting: it says so, the team run
dissolves (a 7-second toast says "*<session>* didn't split it — it's doing the
task itself: *<why>*", the reason cut at 120 characters) and that session does the task, fast-tracked as chosen. Worth it: it
proposes up to N pieces with separate paths (while it decides, the team run's
lead line reads "deciding whether to split…" and its subline "Waiting for
*<lead>* to decide whether to split — …"), you approve them in its Thread
tab, and the server starts the workers and merges them back. When the sentence
lists separate pieces ("billing, search and upload") a **suggested · billing ·
search · upload** pill appears, but the box is never ticked for you. It is
disabled, with the reason, for an agent that doesn't get the MindFlock tools;
if the server hasn't said which agents get them, the box stays usable and the
server refuses the start when it can't attach.

**Launch flags** are extra CLI flags appended to the agent on every start/resume
of the session. The field is pre-filled from the global per-provider default
(`coding_cli.default_launch_args`, Settings → Agents) and its value is
*always sent* with the create request — so clearing it for one session creates
that session with no flags rather than re-inheriting the default.

## New → Ticket: file a ticket by describing it

The second tab files a work item on one of your configured ticketing sources
and hands you the link to it. One box, one picker, one button.

**There is no ticket form, and there will not be one.** You cannot hand-write a
title and a description here — the tracker already has a form for that, it is
one click away, and a worse copy of it living inside a session dialog would earn
nothing. What MindFlock can do that the tracker cannot is turn *“the login page
hangs for SSO users on slow connections”* into a ticket somebody else on the team
could pick up. So the input is a brief, and the output is a filed ticket.

What happens when you press **Create ticket**:

1. A model turn writes a title, a description and acceptance criteria. This is
   the slow part (~10–25 s) and the button says so once it runs long.
2. The description is normalized to paragraphs, one `## Acceptance Criteria`
   heading and `-` bullets — nothing else. That narrow grammar is load-bearing
   in two places that are not defensive about it: the pipeline's
   acceptance-criteria miner reads the criteria back out of that exact heading
   on ingestion, and Jira's ADF translator understands those four shapes alone.
3. The ticket is filed on the chosen source, **in the workflow state that source
   already ingests from** and assigned to its configured member — so it lands
   where the board is looking rather than in a default backlog, and where the
   poller can still find it.
4. You get the link.

Filing does **not** start a session. The ticket shows up in Intake → Tickets
like any other, and **Begin work** is where you start one. If ticket ingestion
is on for the flock, the pane says so before you press the button: a ticket that
quietly turns into a running agent is a surprise worth one sentence.

**Ctrl/Cmd+Enter** files from inside the Ticket tab — distinct from the
dialog-level shortcut that submits the Session form, so the same keys do the
right thing on either tab. The brief box takes focus when you switch to the tab,
so switching and typing works without a click. When exactly **one** configured
source can accept a ticket it is preselected; with two it is not, because which
board work lands on is a decision and a silent default is how it lands on the
wrong one. Once the filing runs past **~8 s** the button
changes from *Filing…* to *Still writing…* rather than sitting unchanged, which
reads as a hang long before it is one.

Sources that cannot accept a ticket stay in the picker, disabled, with the
reason — usually one field short (a Jira source needs its **Project** set to a
project key; a Linear source in a multi-team workspace needs its **Project** set
to a team key). All five providers can file: GitHub Issues, Shortcut, Jira,
Linear and Asana.

If the drafting succeeds and the *filing* fails — an expired token, an archived
project — the drafted ticket is shown rather than thrown away. It is the
expensive half and, at that moment, the only copy of that text anywhere.

One failure sentence is worth reading before you retry: *“the ticket was filed
on X but the tracker did not return a link to it — check the board before filing
it again.”* That one means the ticket **exists** and only its link is missing, so
pressing the button again files a second copy. It is reported as a failure
anyway, because a ticket you cannot open is one you cannot act on.

## New-session dialog: describe it in a sentence

The dialog opens on a **Describe** page — *What do you want to work on?*, one
text box — and the full form is page 2. The box is the widest of the three ways
to fill the form in (a sentence, a template chip, or by hand), and **nothing in
it is submitted**: it wears the same card as the Templates and folder-suggestion
strips rather than looking like a second form stacked on the first. Under it,
ONE line says where **Start** runs it: *Runs in [📦 repo ▾] · on a new
branch*. Nothing on it follows what you type: the branch's name (made from the
task) is only in the tooltip, because shown live it rewrote itself on every
keystroke and the line read as reacting to the prompt. Once you start typing,
the folder is yours: a folder suggestion that lands late no longer replaces
it. The folder is a real picker (`#new-describe-folder`,
the New tour's `new-folder` hook): **Recent folders** (only ones a session was
started in), the other suggestions, then **Browse…**, which opens the folder
picker window. A folder with no repo reads *· in the folder
itself (no git, so no diff or PR)*; a path that doesn't exist reads *No folder
at …* with **Create this folder**, and Start stays off. Then *Starting from a ticket, pull request or issue? **Open Intake***. A
path typed into the sentence is not read as the folder — the line is the
folder.

Three actions sit under it — ONE primary:

- **More options** — quiet, far left: straight to page 2, with what you typed
  as the Task (a Task you edited since an earlier trip is kept). Nothing is
  read and no model runs; it is the only route to the form for someone with no
  coding CLI installed.
- **Let the agent pick the folder…** — secondary (hidden for an Intake batch),
  and the other way to choose the line's folder. Sends
  the sentence to [`POST /api/session-plan`](web-api.md#post-apisession-plan--200),
  which walks the filesystem, asks one headless model turn, and fills in page 2
  for you to change before anything starts — Folder, Name (when the plan has
  one), Task, and the Works in choice — then moves the caret to Folder. It
  leaves the agent, account, launch flags and Plan first alone. The button is
  **`type="button"`**: the box lives inside the form.
- **Start ↵** — the primary (accent), and what **Enter** in the box (and
  **Ctrl/Cmd+Enter** on page 1) does: starts the session **now**, in the
  folder the line names, with the sentence as typed as its Task, the form's
  other choices as they stand, and the name made from the task — no model
  turn (pressed during a read, it drops the read). A refusal (no folder, a
  missing agent) opens the form with the sentence already in Task. After
  **Let the agent pick the folder…** and **← Back**, it acts on that plan instead of
  reading again. Shift+Enter starts another line.

While a turn is in flight the button shows a ring and **Asking…** (**Still
asking…** after 8 s), and a line under the box says who and for how long at
most: *Asking Claude Code to fill in the details… 0:04 (up to 30 s)*. The read
gives up at **30 s** (the server's own budget, `session_plan.TIMEOUT_PLAN`, is
the same): *The agent didn't answer within 30 seconds.* with ONE recovery,
**Start it in <folder> instead** (with no folder: **Pick a folder…**, the
form). Any other failure says why and offers the same button — a CLI known not
to be signed in says *claude isn't signed in yet — sign in under Settings →
Agents* rather than *claude exited 1*. A **Cancel** link appears beside the
box, which goes read-only rather than disabled so the caret and the selection
survive the wait. Start on the form is held while a turn is reading.

**One prompt, however many lines; batches come from Intake.** The box is a
textarea and stays ONE prompt however many lines it holds — **Fast-track to**
(Off / Commit / Push / Open PR / Merge when green, starting **Off** unless
you pick), **Ask me before it ships** and **Auto-split** sit on the form with
the rest of the details, and anything but Off is armed on the new session as
soon as its worktree exists. Only Intake's **Start together…** opens New as a
**batch** — its ticked tickets in the box, one
session per line or ticket — and only a batch shows choices on page 1: the
folded **Options** row (`#new-options`, e.g. **Options · Fast-track: Off**,
remembered in `mf_new_options_open`) for one row, unfolded for several (it has
no auto-split box):

- Rows under the box, read by the server (`POST /api/runs/preview`, 300 ms
  after you stop typing): a `TICKET`/`TASK` chip, the ID, the title, where it
  comes from (the ticket's source, or the folder a task starts in) and **✕**,
  which takes it out of the box. A ticket that can't be found stays a red row
  with the reason — never quietly a task — and Start refuses until it is
  removed. Plain Enter adds a line in a list.
- **Fast-track each to** starts on the batch default (Settings →
  Workspace; Off when unset), **PRs** One per line / One for all ("one-for-all merges them
  into one branch first"; until `caps.team_runs.together` is true One for all
  is disabled with "one PR for the whole team run is coming next" and every team run
  is sent as one PR per line), **At a time** 1–8 (default 3; "the rest wait in the
  team run, not in your grid"), **Team run name** (the server's suggested name) and,
  when there are typed tasks, the folder they start in.
- A sentence under the choices says what will happen, e.g. *"6 sessions, 3 at a
  time. Each one is committed with a message written from its diff, pushed and
  opened as its own PR once its agent stops and your hooks pass. Nothing
  merges; each PR shows on its row."* With ask-first on it adds *"Before the
  first commit it stops and asks you first, in the bell."*
- **Start N sessions** posts `POST /api/runs`; the dialog closes once the server
  has taken it, and a refusal ("one-for-all needs a single repository") lands
  beside the choices. MindFlock owns those sessions from then on: queueing,
  retries and shipping are the server's.

Intake's Tickets tab feeds the same list: tick tickets (the box before each
ID) and the bar at the bottom — **N tickets · Start together…** — opens New
with their IDs already in the box.

**Refusals say why instead of greying out.** Nothing is disabled for a sentence
that is too short; the refusal shows under the box. An empty box answers
*"Type what you want to work on first."*; a one-word sentence answers *"Say a bit
more — “scan” doesn't say which project or what to do."* The floor is about
ambiguity, not length: a bare name resolves to exactly one folder on the machine
and it is the wrong one, and there is no repair for a sentence that never said
which project.

When a plan lands, the strip shows the **server-written note** (which folder,
which mode, why it was clamped) — the model never writes it, because a
model-written note is a sentence that can disagree with the form under it — and
**Create is held for ~0.5 s**, saying *"Just filled the form in — check the
folder, then press Create."* The answer arrives 10-25 s after the keystroke that
asked for it and moves the caret on its way in; without the hold, an Enter aimed
at something else creates a session in a folder a model picked and nobody read,
and the create closes the dialog before the POST, so there is nothing left on
screen to cancel.

**The new-folder gate.** When the plan answers `folder_exists: false`, the foot
of the strip asks *There is no folder at `~/code/invoice-parser` yet. Make it?*
with an unticked **Yes, create …** box, and **Create refuses until it is
ticked** — with a sentence naming the folder and quoting the tick word for word,
because by then the row can be several screens up. The gate is derived from the
Folder field, so editing that field away from the proposed path clears it and
typing the path back re-arms it. This is **not** the *Create a git repo in this
folder* checkbox (`init_repo`, a `git init`) under More options — a new
project usually wants both, and the tick's own copy says so. A folder is the one
thing a plan proposes that outlives the session: a worktree goes when the session
does, but nobody comes back for the directory.

## Saved prompts

Saved prompts are text you reuse — mostly to paste straight into a session.
They are not a list of their own any more: a saved prompt is a
[template](#new-session-dialog) that holds a prompt, so they live on the
server and follow you between devices. They have three doors, one job each:

- **The Prompts bar** (sidebar; switch it on in [Customize](#customize), like
  the Assistant) is the daily one: one short row, **Prompts · Paste ▾**, at
  any number of prompts. **Paste ▾** opens a menu (`#prompts-menu`): yours
  first, then the built-ins, each name over a one-line preview of its text
  (hover for all of it), and **Manage prompts…** last (arrows, Enter, Esc).
  A pick is pasted into the **focused** session (`POST
  /api/instances/{title}/send` with `submit: false` and `dialog_safe: true`)
  and the keyboard moves to that session's terminal: *nothing is sent until
  you press Enter*, and a session sitting on a permission or limit prompt is
  skipped rather than typed into. There is no target picker on the bar; with
  no session selected, a pick says to select one first.
- **The Prompts dialog** (`#prompts-dialog`, from the bar menu's **Manage
  prompts…**, the palette's **Prompts…** or New's **Manage…** — which comes
  back to New as it was on Close) opens on *Saved text you send to a running
  session. To start a new session from a saved setup, use Templates.* It adds and
  deletes them, previews a long one (⋮), and pastes the same way into a
  session you choose: the selected one by default, any other running session
  by the name its rail row shows, or **All running sessions (n)** once two or
  more are running ("running" = status `running` or started, but not paused;
  verify sessions are left out; *All* sends once per session). Both surfaces
  use one paste path, `lib/promptPaste.ts`.
- **New's Task box** can start from one: **Insert saved text…** sits
  beside the **Task** label. Built-ins ship for the common loops — *Fix
  failing tests*, *Address PR review comments*, *Write tests for recent
  changes*, *Refactor for clarity — no behavior change*. Picking one fills the
  Task; edit freely before creating. Adding and deleting saved prompts happens
  in the Prompts dialog (**Manage…**, where — opened from New — a click puts
  the prompt in the Task box and there is no *Paste into*); the form's one
  save is **Save as template…**, which saves the whole setup.

All three read the same list: the templates that hold a prompt
(`/api/templates`, through `lib/presets.ts` over `lib/templates.ts`, with a
paint-only `localStorage` mirror). Saving a prompt under a template's name
keeps that template's other fields. The four built-ins ship with the app and
are not stored. A browser's old per-browser list (`localStorage`
`mindflock.prompt_presets`) is migrated to the server once
(`mf_presets_migrated`): a prompt already there by name and text is skipped,
a same-named different one is saved as *<name> (saved prompt)*.

## First-run onboarding

One checklist, shown in one place at a time. On a genuinely first launch the
welcome card opens once over the grid's first-run card (below); its **Let's
go** starts the Welcome [tour](#tours) over the checklist. It counts as seen
(the synced `welcome_tour` pref) by **every** exit — **Let's go**, **Skip**,
`Esc` or the backdrop — so it never comes back by itself. UI store state
(`state/store.ts`: `hintsEnabled`, `dismissedHints`, persisted in
`localStorage`) covers the hints; the checklist's state is the server's.

- **Get set up** — with no sessions, and while a required step is still to
  do, the grid shows a card headed *Get set up*, *Three steps to your first
  agent. The rest can wait.* It holds the setup checklist
  (`setup/SetupChecklist.tsx` — the same component the Setup modal renders).
  The steps, their names, order and groups come from the server
  (`GET /api/setup`, `backend/web/core/setup_steps.py` — the same list
  `mindflock init` prints, in the same state), drawn as three groups so the
  few things that matter come first:
  - **Get started** (required): ① **Install Claude Code** — the server
    names the default agent by its product name (`tools_title`); it reads
    *Install the tools* only when the agent is already here and something
    else is missing. The **Install …** button names the agent too (*Install
    Claude Code*); beside it only problems the button does NOT fix (a row the
    install plan covers shows inside *All checks* only). *All checks (N)*
    lists only checks that can block ①–③ (each doctor row's `required`);
    phone, Tailscale, MTU and sudo fixes never show in Setup — they belong to
    Settings → Your phone and Settings → Doctor. Before any payload arrives
    the step shows a skeleton of the install row, then the setup payload's own
    doctor result, never an empty card. **Re-check**. ② **Sign in to your
    agent** (the default agent's **agent card** — the same card Settings →
    Connections, Agents and Custom agents show, with its one next action:
    **Install** → **Sign in** → **Test**; *Using Claude Code · change*; *Using
    an API key instead? Add an API key* — opens Settings → Accounts with its
    add form on **API key** for the default agent, via the one-shot intent in
    `setup/accountsIntent.ts`), ③ **Start your first agent** (*Pick a
    repo folder and say what to do.* **+ New session**, whose dialog has the
    folder browser). ③ ticks only once a session's agent has actually started
    on its task (the activity classifier's first corroborated *working*
    reading), not when a session row appears; until then a session that exists
    says *Your first session is open. This ticks once its agent starts on your
    task.* (or, while ② is not done, that it can't start until you sign in),
    and the step's primary is **Show my session →** (closes Setup and selects
    that session's pane — the focused one, else the first) with a quiet *or
    start another* link instead of a second **+ New session**.
  - **Connect your work** (optional): ④ **Connect GitHub** (*Optional —
    pushing works without it; needed to open and merge pull requests.* — the
    Connect GitHub dialog below) and ⑤ **Connect your tracker** (**Add a
    ticket source** opens Intake → Tickets). Each has a **Skip** (and **Show
    again**).
  - **Optional, later** — folded, unnumbered, not counted: one line, *Use it
    from your phone — Set up in Settings → Your phone*. Tailscale, its fixes
    and the QR live on that screen, not in Setup.
  Tour hooks (stable; pinned by `setupChecklist.test.ts`):
  `data-tour="setup-step-<id>"` on each counted step's root (`tools`,
  `agent_login`, `repo`, `github`, `tracker`) and `data-tour="setup-next"` on
  the next step's primary button only (the install button, the agent card's
  **Sign in**, **Show my session →** / **+ New session**, **Connect GitHub**,
  **Add a ticket source**).
  One obvious next action: the server's `summary.next` step is outlined and
  its button is the only accent-coloured one; a done step shrinks to its
  title. The progress line uses the chip's one count (`setupCount`): the three
  required steps (*1 of 3 done · next: Sign in to your agent*), then what
  optional is left (*3 of 3 done · 2 optional left · next: Connect GitHub*).
  **Install …** never leaves you watching a cursor: the window's line says
  what it fetches and a running clock (*Downloading Claude Code… 0:12 — can
  take a few minutes*, from the run's own start, `started_at` on
  `GET /api/doctor/install-state`); on success the window STAYS, with *✓
  Claude Code installed — Next: Sign in* and a **Sign in to Claude Code**
  button that hands over to the agent's sign-in window (the checklist keeps
  step ①'s body mounted while either window is up, so ticking ① no longer
  takes the window with it); after 90 s, or when the run fails,
  shows the commands to run in your own terminal with **Copy** — the same
  commands are always under *Or run it yourself* by the button. A run still
  going after a reload (or in another tab) shows as *Installing… 1:23*
  **Show** in the button's place.
  Steps are marked by the server from the thing itself, never by a button:
  ① and ② by every doctor run (nothing failing but optional extras; the
  default agent CLI installed and its login found), ③ by the first session
  create, ④ when a GitHub credential resolves (a Settings / env token, or a
  signed-in gh), ⑤ by saving the first ticket source, the phone by the owner
  fetching the QR or a phone loading `/m` from off this machine. Connecting
  GitHub or signing an agent in re-reads the checklist at once. The only
  writes from the UI are `POST /api/setup/<step>/skip` (and `/unskip`). A
  step once done stays done. The state is `general.setup`;
  `general.onboarded` is derived from ③ (an older store is migrated on load;
  one that settled the old combined "Connect a tracker and GitHub" step reads
  ④ as skipped). The text a first `mindflock serve` prints at boot (a CLI
  transcript — `report` on `GET /api/setup`) is NOT in Setup; it is the
  folded *First-start report* under Settings → System logs.
- **Setup chip** — once the three required steps are done (or a session
  exists) the card gives way to a compact chip in the sidebar footer reading
  **Setup 1 of 3** (then **Setup 3 of 3 · 2 optional**), until every
  step of the first two groups is done or skipped (the phone, under
  *Optional, later*, never keeps it up). The chip, the doctor chip's **open Setup**, the
  palette's **Open Setup checklist** and the desktop app's `?setup=1` (its
  older spelling `?setup=install`; once, the parameter is stripped) open the
  **Setup** modal, which renders the same checklist (its **Close** sits
  top-right, like every dialog's). The modal never opens
  by itself — not on a failing doctor probe, not on a first load. In a
  phone-narrow window the sidebar is a drawer over the page; any dialog that
  opens (the chip's Setup, Shortcuts, …) closes the drawer first
  (`Modal`'s `mf:modal-open` event → `closeDrawerForModal`), so it is never
  drawn under it. The
  doctor chip stays down while the grid card or this chip is showing,
  because both open the checklist that already lists the same checks.
- **Welcome card** (`onboarding/WelcomeTour.tsx`) — ONE card: **Welcome to
  MindFlock**, *MindFlock runs coding agents side by side on this computer,
  each session on its own branch. Three steps and your first agent is
  running.* and one button, **Let's go**, which closes it and starts the
  Welcome tour over the Get set up checklist (on the empty grid; a replay
  with sessions open brings up the Setup modal first). **Skip**, `Esc` and
  the backdrop just close it. It replaced a five-slide tour whose slides
  described a grid, shipping and Intake that weren't on screen yet, covered
  the checklist, re-armed itself after two of its exits (*Set up now →*,
  *Open Setup*) and said *You're all set* at 2 of 3. It **opens by itself
  once**, on a first launch — no session ever started (the setup record's
  `repo` step) and never seen on any of your devices (the synced
  `prefs.welcome_tour`; the page waits for its first prefs pull before
  deciding). After that it is the palette's **Replay welcome tour**, the
  **Tour** link on the **?** shortcuts sheet, and **Welcome tour → Replay** in
  Settings → General → Help.
- **Connect GitHub** — the dialog; see [Connect GitHub](#connect-github)
  below.
- **Push failed** — the Push button types `GIT_TERMINAL_PROMPT=0 git push …`
  into the session's shell (whose wrapper exports the connected GitHub
  sign-in as git's helper for github.com), so a missing credential fails at
  once instead of waiting on a Username prompt. When the push's output (kept
  in the worktree's private git dir) shows a missing sign-in, SSH key or git
  identity, a toast says so — *Push failed on [1] api: git has no GitHub
  sign-in on this computer for an HTTPS remote — Connect GitHub in Setup* —
  and clicking it opens Setup (`session.push_failed`).
- **Install …** — above the dependency checklist (the card, the Setup modal and
  Settings → Doctor) whenever something this machine needs is missing. The
  button names what it installs, an agent by its product name, when that's
  short (*Install tmux + Claude Code*, else *Install everything missing*,
  with the full list beside it), and says *asks for your password once* when
  a package-manager or Homebrew step is in it.
  One click opens a terminal running a single script: Homebrew first on a Mac
  that lacks it, one package-manager run for every system package (one sudo
  prompt), then each tool's own installer — including whichever agent CLI you
  chose, not just claude. It works without tmux (a plain PTY — tmux is
  usually what's being installed). Closing the window mid-install doesn't stop
  it (the next click reattaches); when it finishes the checklist re-checks
  itself.
- **Sign in to <agent>** — on the doctor's *agent auth* rows (Setup and
  Settings → Doctor) and on every agent card (Connections, Agents, Custom
  agents, Setup ②), whenever no login was found for a CLI that declares a
  login flow. It opens the CLI's own login in a terminal window
  (`AgentSignIn.tsx`) that lists what the terminal will really ask — for
  Claude Code: *Pick a text style … Choose how to log in … Open the link it
  shows* — and watches the login probe, so it says *✓ Signed in. You can close
  this window.* the moment the login lands. Closing it re-runs the check.
  The login, gh and install terminals are always dark (`.login-term-dark`
  in `dialogs/ConnectGitHub.css`), whatever the app's theme: a CLI's login
  screen is drawn for a dark terminal, and Claude Code's highlighted option
  is light cyan.
  Neither the login nor the install terminal draws tmux's status line.
- **The agent card** (`components/setup/AgentCard.tsx`) — one card per agent,
  the same on Connections (*What you use*), Settings → Agents, Settings →
  Custom agents and Setup ②: the status pill, one plain line (*Signed in as
  you@example.com*, never a `.claude.json` path) and ONE next action —
  **Install** (not on this computer: the Setup checklist's own one-click
  install, or the copyable command for an agent that isn't in its plan) →
  **Sign in** (installed, no login found) → **Test** (everything else; it
  answers in words: *Found at /usr/bin/claude · signed in as … · answered in
  0.4 s*, or *… · installed, not signed in yet*). The
  default agent's card on Connections adds a quiet **Change agent** link to
  Settings → Agents; nothing points at another screen for the same thing. A
  finished install says *✓ Claude Code installed — Next: Sign in*.
- **Hints** (`onboarding/Hint.tsx`) — small dismissible 💡 inline callouts that
  nudge toward a feature. Each needs a **stable `id`**; dismissing one remembers
  that id (`dismissHint` → `dismissedHints`). A master switch (`hintsEnabled`,
  Settings → General) hides all of them at once; turning hints **back on
  re-arms** every previously dismissed hint (clears `dismissedHints`).

The tours and hints are replayed from the **Help** block at the bottom of
**Settings → General**. There is no sidebar welcome hint any more; the one
first-run hint left in the sidebar points at Customize.

### Tours

A **tour** is a short coach-mark walkthrough of one surface: a ring around a
live control and a small card beside it (`onboarding/CoachTour.tsx`) — one
plain sentence, **Back** / **Next** / **Done**, *2 of 4*, **Skip tour**. It
is non-modal (`role="dialog" aria-modal="false"`): what it points at stays
clickable, the card sits beside, below or above its target (re-measured on
resize, on any scroll, and as the dialog fills in), and a step whose target
isn't on screen centres the card and still shows. `Esc` ends it; the arrow
keys page.

Every step is **brought into view by the tour** (`revealTarget` in
`CoachTour.tsx`): a target inside a closed `<details>` has its fold opened,
and one not wholly inside its scrolling pane (a Settings screen, a dialog
body) or the window is scrolled to the pane's centre (`behavior: "smooth"`,
or instant under reduce motion) — nobody scrolls to find a ring. A target
that renders late is looked for again for a few seconds. A step can also name
a `screen` of its surface to switch to first (`TourStep.screen`); Settings is
the one surface that reads it as a screen, and the mic reads `"mic-pop"` (the
Voice tour) as "open my options".

There are eight (`lib/tours/`, one file per tour, `TOURS` in `index.ts`):
**Welcome** (over the Get set up checklist: the *next:* line, ① Install your
agent (the checklist names which), ② Sign in to your agent, ③ Start your first session, then the
first sidebar row), **New** (the task box, the *Runs in* folder picker,
**Start**, then a session pane's Agent / Diff tabs and its ⋯ — hooks
`new-task`, `new-folder`, `new-start`, `pane-tabs`, `pane-menu`; the last
step centres when no session is open), **Intake**, **Verify**, **Schedule** (six
steps: the top-bar **Schedule** — or **Create a template** while that item is
still hidden — then Create a template, Name + Folder, Task, Run on a schedule
and its next-runs line, **▶ Run now**; hooks `schedule-new`, `schedule-name`,
`schedule-folder`, `schedule-task`, `schedule-when`, `schedule-run`) and
**Settings** (seven steps across four screens: Connections' status header,
GitHub's **Connect** — the whole GitHub card once connected — and the search
box; Agents' default-agent picker; Workspace's *Where your worktrees live*;
**Show advanced settings · N more screens** at the nav's foot; General's Help
block. Hooks `settings-status`, `settings-github`, `settings-default-agent`,
`settings-worktrees`, `settings-advanced`, `settings-tours`. Its first step's
screen is Connections, so the **?** always restarts it there) and **People**
(five steps on Settings → People: **Invite someone**, **Join someone**, the
safety number, the share-a-folder row, the joint task — hooks
`people-invite`, `people-join`, `people-safety`, `people-share`,
`people-joint`; the link-card steps centre until someone is linked. Its **?**
sits in the People screen's header; replaying it from General or the palette
opens Settings on People first, `settings/screens/peer/replayPeopleTour.ts`.
It never opens by itself) and **Voice** (eight steps over the top-bar mic,
its last two in the mic's options; see [The Voice tour](#the-voice-tour)). A step's target is a CSS selector, usually a stable id or a
`data-tour="<surface>-<thing>"` hook (with today's id as a fallback in the
same selector list — a list matches in *document* order, so a fallback must
be the hooked element or inside it, never earlier in the page).

- **Replay** lives in three places: the **?** beside Close in each top-bar
  dialog's header (`onboarding/TourButton.tsx` — *Show me around*, titled
  *Replay the Intake tour*), **Settings → General → Help** (one **Replay**
  per tour), and the palette (**Replay Intake tour** …).
- **By itself, once** (`onboarding/useAutoTour.ts`): a dialog's tour opens
  the first time that dialog opens in the [calm first hour](#layout) — decided
  from the server's setup record, never the render hint — and only while it
  isn't in the synced `prefs.tours_seen` list (localStorage `mf_tours_seen`;
  the page waits for its first prefs pull, so a tour seen on another device
  never opens here). Never outside the first hour, never with hints off,
  never over the welcome card or another tour. The Voice tour is the one
  that opens on a control's first use instead — the mic's — in any hour.
- **Seen by every exit**: **Done**, **Skip tour**, `Esc`, or closing the
  dialog under it (`closeCoachTour` in the store) adds it to `tours_seen`.

The Map tab's own tour ([The Map tour](#the-map-tour)) is the same coach mark,
contained in the Map's pane.

### Connect GitHub

`dialogs/ConnectGitHub.tsx`, `/api/github/*`. One sign-in for opening and
merging PRs, stored as `github.token` (which settings sync shares with your
other devices) — and, once connected, git's credential for github.com too:
every clone, fetch and push MindFlock runs, and git inside every session,
carry it as a per-process credential helper (no keychain prompt, no gh, no
global git config written; see *GitHub credential* in
[configuration.md](configuration.md)). Pushing stays plain git over your own
remote. The dialog has ONE primary path (`connectLayout`):

- **Device flow configured** (an OAuth App client id: the build's constant,
  `github.oauth_client_id` or `$MINDFLOCK_GITHUB_CLIENT_ID`): one **Sign in
  with GitHub** button (a code to type at github.com/login/device); the token
  steps are folded under *Or paste a token*.
- **Not configured**: the three numbered token steps ARE the path — **Make a
  token on GitHub** (a pre-filled classic-token page: `repo`, `read:org`) →
  paste it → **Save & test** (GitHub checks it before it is saved). When gh
  is installed, *Other ways to connect ▸* folds **Sign in with the GitHub
  CLI**: a terminal running `gh auth login --hostname github.com
  --git-protocol https --web` then `gh auth setup-git`, whose window lists
  what the terminal asks, in order (*Authenticate Git with your GitHub
  credentials?* → Enter; the one-time code → Enter opens GitHub; approve), and
  says *✓ Connected as @…* + **Done** once GitHub reports the sign-in (gh's
  token is copied in when the window closes).

After *✓ Connected as @…* a **Done** button closes the dialog, back to the
Setup checklist with ④ ticked. *Your name and email for commits* (in the
dialog's step-heading style) shows when git has none — pre-filled from the
account with its private `@users.noreply.github.com` address; **Use these**
writes the global git config, nothing writes it on its own. **Check I can
push** says what it will ask before the click (*Checks repo → <origin URL>*,
from `push_target` on `GET /api/github/status`), asks that origin for real (a
`push --dry-run` with prompts off), and on a failure names the repo and the
remote with one fix — a command in a **Copy** box (*repo has no remote called
origin yet, so there's nowhere to push* → `git remote add origin <url>`), or a
sentence for anything else. When it fails for want of an HTTPS credential,
**Let git push with this sign-in** runs `gh auth setup-git`, or — only when
git has no credential helper for GitHub — registers `mindflock
git-credential` for https://github.com in the global git config (a 409 when
git already has one). That button is for git run **outside** MindFlock, in a
terminal of your own. Inside it nothing needs clicking: once connected,
every clone and push MindFlock runs, and git in every session, already use
the sign-in as a per-process helper — so macOS's *git-credential-osxkeychain
wants to use your confidential information* prompt no longer appears for
github.com at each clone or push, and **Check I can push** exercises the
same helper a session's push will. Every one of these routes is for the
person at this computer (`privileged()`), never a relayed or anonymous caller.
Settings → Connections' GitHub card links its **Docs** here.

## Make-PR dialog

Clicking **Open PR** opens a branch-picker dialog (`dialogs/MakePrDialog.tsx`)
before the PR is created: a combobox whose dropdown filters the repo's real
branches (from `GET /api/instances/{title}/branches`) as you type, and you can
also just type a name. The session's own branch is flagged and can't be the
target. The **last base chosen per repo is remembered** (`prBaseByRepo`,
persisted) and pre-selected next time (falling back to the server-computed
default); submitting calls `submitMakePr` → `POST /api/instances/{title}/make-pr`.
Under the base field one line says what the PR could break — **⚠ Could break:
9 files in 3 folders (2 tests) — See on map** (the Map's blast radius:
direct importers of the changes, from `lib/mapDigest`; See on map opens the
⚠ Could break layer). It stays hidden while loading and when nothing imports
the changes.

## Recently closed

One page for closed sessions **and** what they left on disk. "Workspaces on
disk" used to be a second, near-identical list of the same directories seen from
the other end, so the two were folded together: *can I have this work back* and
*can I have this disk back* are the same question about the same directory.
Opened from the **Recently closed (n)** link under the session list, which
shows only while there is something to list (it shares the `recently-closed`
query Verify uses, and refreshes when a session is deleted) or from the
palette's **Recently closed…**; `Ctrl+Shift+T` / `Ctrl+Z` reopen the last one
without it. Rows come from `GET /api/recent`. Forget, Delete, **Delete unused worktrees** and its
follow-up confirm in a bar under the header, and failures show in a
bottom-right card — never a browser `confirm()` or `alert()`, which the desktop
app does not implement.

Each row carries both identities — the saved name / branch / session title, then
badges for what the directory is (`worktree`, `workspace`, `pr`, `in-place`,
`provisioned`, `worktree gone`, `in use: <session>`), when it was last used
("closed 3d ago" for a session, "used 5d ago" for a leftover directory), and its
size once the second `?sizes=1` pass lands. `Ctrl+F` filters, the sort picker
orders by last used / name / size, and the checkboxes drive the same bulk bar the
other list dialogs use (**Forget selected** · **Delete from disk**).

Two things are deliberately NOT rows. Protected shared infrastructure (base
clones, cache refreshers): not yours to delete, and a row whose only ever action
is "protected" is noise. And any workspace a **live** session is working in: that
one is in the sidebar, and killing a running agent is not something a page about
closed work should offer. The header says how many of each it withheld — and
names the protected ones on hover, with their size — so the disk total is never
quietly under-reported.

A session the server dropped at startup because both its folder and its tmux
session were gone is listed too, never silently lost (`reason: "workspace
missing"`): its row reads *Workspace missing — removed at startup.* and then
*Branch <name> is still in the repo — check it out to recover the work.*, *Its
branch is still in the repo.* or *Its branch is gone too.* (`branch_exists`).

Per row: **Reopen** (a closed session whose directory is still there; a
closed shared-folder session — `peer-<name>-<id>` — shows **Reopen from
People** instead, which opens its card there, since the generic reopen is
refused for it), **Delete**
(the directory — absent for an in-place session, whose folder is your own repo,
and for a directory a running session shares) and **Forget** (drop the closed
session from the list; the directory stays, and comes back as an on-disk row).

**Delete unused worktrees** is the page's one bulk action and its only git-gated
control (`POST /api/workspaces/prune-worktrees`). It removes every worktree no
session is using and nothing has touched for over a week. What it may take is
decided by the server, twice — once to build the list, again immediately before
each delete — and the rule is structural rather than a matter of naming: the
directory's `.git` has to be a gitdir **file**, which is what `git worktree add`
writes. A repository, a clone, a `_base_*` mirror, a `pr-*` review clone and any
folder git did not make all have a `.git` directory (or none) and are excluded by
that one test; nothing outside MindFlock's own worktrees root is looked at at
all.

It asks twice when it has to. The first confirmation names every directory it
will remove, states what can never be removed, and says what a removal costs: a
worktree's branch and commits live in the repository it came from and survive, so
only what was never committed goes with the directory. Anything holding
uncommitted work — or a detached HEAD whose commit no ref contains — is reported
separately and held back unless a second confirmation says to include it, so
"remove all unused worktrees" never silently does the destructive half of itself.
Rows the sweep would take are badged **unused Nd**, and the button carries the
count, so the number is checkable against what is on screen. When it finds
nothing it says why: how many are in use, how many were used this week, how many
are repositories it will never touch.

## Intake

**Where your sessions come from.** Ticketing, PR review and issue handling used
to be three screens in Settings, and they never belonged there: Settings is
set-and-forget, while these three are somewhere you *visit* — to see what came
in, start something by hand, or pause a queue. They now have their own top-bar
entry (**Intake**, `Alt+I`, or the palette's Open Intake), dialog `#intake-dialog` /
panel `#intake-panel`, built from
`frontend/src/components/intake/{IntakeDialog,TicketsTab,PullRequestsTab,IssuesTab,QueueTab,RepoSources,kit}.tsx`,
with the shared rules beside them in the same folder (`search.ts`, `buckets.ts`,
`queue.ts` — referred to below as `intake/…`).

**Every work list has the same Ctrl+F filter.** Tickets, Pull requests, Issues
and the auto-start roll-up all grow without bound — every open PR on every
watched repo, every ticket in every workflow state — so each toolbar carries the
box Recently closed and Verify already use, with the same rule: every
whitespace-separated token has to appear somewhere in the row, so `sitecheck
4217` and `feature coupon` narrow without anyone remembering the separator.
Ctrl+F focuses it; Escape clears it before it closes the dialog. What each list
searches is the row's own words plus the two things the row shows as *shape*
rather than text — the state/branch it is grouped under, and whether auto
ingestion has queued it, so `queued` narrows a repo to what is about to start on
its own and `already handled` finds what it skipped. **Filtering happens before
grouping**, so every heading's count describes what is under it and a workflow
state with no match drops out instead of leaving an empty group behind. A
**source** heading never drops out: it is where that source is edited (below), so
a source with no match stays, reading **0** and *no match*, under one line saying
*No ticket matches "…"*. The one number that ignores the filter is the **View**
menu's per-state count, because that menu is a standing choice about which
states this panel shows at all (`intake/search.ts` holds the four rules, one per
list).

**Tab strip with live counts** — **Tickets**, **GitHub PRs**, **GitHub issues**,
**Auto-start** (keys `tickets` | `prs` | `issues` | `autostart`, unchanged — the
GitHub tabs say "GitHub" so they no longer read as a second home for the GitHub
Issues ticket provider), a horizontal
strip rather than Settings' long left nav because they are peers you flip
between while reading. Each badge counts what needs you: the rows on that tab
that are **ready to start** (what auto-start would take), not every row listed —
and the **Auto-start** badge turns grey and says *N waiting* when nothing is
switched on, since those rows are waiting rather than about to go. It is still never a total. That distinction is
the whole of it: counting every ticket the provider ever assigned you read `1221`
over a list of 52, because done states are parked (hidden under **View**), so
the badge and the panel go through one shared bucket filter (`intake/buckets.ts`)
and cannot disagree. A tab with nothing (or an unconfigured integration, which
502s) shows no badge at all rather than a `0` that turns into `12` a second
later. The
two GitHub tabs are gated on the `git` cap only and explain themselves in place
when it is missing: PR comments and issues run on their own switches and GitHub
sign-in (the pipeline boots for them with no ticket source), so they are no
longer locked behind connecting a tracker. The strip is a real `role="tablist"`
(arrow keys move along it), and the dialog hangs from the top-bar dialogs'
shared top edge ([dialog placement](#new-session-dialog)) rather than being
centred, so switching to a taller tab grows it downward instead of moving the
tab strip under the cursor.

**One home for auto-start** (round 5). The three switches — **Tickets**,
**PR review comments**, **New issues** — sit together at the top of the
**Auto-start** tab (`intake/AutoStartSwitches.tsx`, the same ids as before:
`#tk-ingestion-enabled`, `#gh-pr-enabled`, `#gh-issues-enabled`), each saying how
often it checks (*checks every 20 s* / *every minute*, from the source's
`poll_interval_seconds` or `github.poll_interval_seconds` /
`issue_poll_interval_seconds`, else the pipeline's defaults) or why it can't be
on yet (*Add a repository first — on the GitHub PRs tab*; *Jira can't connect —
press Fix on the Tickets tab*). Beside them, **Pause all auto-start**
(`#ik-pause-all`) turns every switch that is on, off, in one press. It is not
the old pause — there is no **Pause intake** any more: Pause all records nothing
and has no Resume, and turning one back on is that switch (an older build's
`general.intake_paused` is still never read). The toasts keep the switches'
long names (*Auto-start tickets on*, *Auto-address review comments off*,
*Auto-start new issues on*).
The Tickets / GitHub PRs / GitHub issues tabs show one line where their switch
used to be — *Auto-start: on — checks every 20 s* · **Change ›** (or *off* ·
**Turn on ›**) — which goes to the switches.

**What auto-starts, in one line.** A line above the tabs says what starts on its
own right now across the three switches (*Auto-start is on for tickets and PR
comments.*, *Auto-start is off everywhere.*) and the next scheduled template
(*Next to start on its own: nightly tests, Fri 07:30 — tomorrow's run
skipped.*); its link (**Turn on ›** / **Auto-start switches ›**) goes to the
switches (`intake/switches.ts` reads the three together, for this line, the
row chips and New → Ticket alike). It is hidden until something is set up, so a
first open shows only the add flow. **New → Ticket** reads the same ticket switch, so it never says
auto-start is on while Intake says off.

**Chips follow the switches.** A row that passes every filter says
**will auto-start** only while its kind's switch is on; otherwise it reads
*auto-start off* in a neutral chip. Skip reasons from
the server are written in the UI's words — *not in an auto-start state*, *missing
an auto-start label (…)*, *already started before*, *already queued to start*,
*already handled before*, *too new — waits 12 more min* — never "ingest" or
"ledger", so the rows show them as they come.

**Where a ticket goes.** On a source with several repos every ticket row (and its
Auto-start row) carries a line saying which repo it will start in and why —
*→ billing-service · label 'billing'*, or *→ repo picked by the agent when it
starts* when nothing on the ticket names one. The row's **Options** add a
**Repo** picker (`.ik-item-repo`, only on such a source): *Default (<routed
repo>)* — or *Default (agent picks)* — then every repo of the source. A pick is
sent as `repo` on `POST /api/tickets/start`, skips routing for that start, and is
recorded as *picked on the row*, so the row keeps showing it and a later start
of the same ticket keeps it. For a standing rule, give the ticket a label
matching a repo's name or hint. The three retired Settings screen keys
(`ticketing`, `repo`, `issues`) still route — `LEGACY_SCREEN_TABS` in
`IntakeDialog.tsx` maps them onto tabs, and the Settings dialog hands off to Intake
when it is deep-linked one — so every old link keeps working, including the
**Configure** button on a Connections card (the server's `settings_screen`
values) and the welcome tour's setup slides. A target may also name a source
after a colon — `tickets:jira`, `prs:acme/api` (`parseIntakeTarget`) — and the
tab then opens that source's heading with its editor open, scrolled into view:
this is what a ticket source's **Open in Intake** on Settings → Connections does
(the row's `source_id`), instead of landing on the top of the tab.

**Running on another of your devices.** With [Your devices](#settings-)
joined, every start path asks the other devices first (`GET
/api/tickets/claims`, [web-api.md](web-api.md#assigned-tickets-pr-auto-review--issue-handling)):
a ticket that one of them already holds — a live session, a launch under
way, an in-flight ingestion marker, or a team run's reserved item — shows a
*running on <device>* reason first and is not eligible here; **Begin work**
answers 409 and names it. PR review and issue handling likewise run on
exactly one of your devices (`github.automation_device`), so on the others
the **Auto-start** tab's chip reads *runs on <device>* and the note under a
stalled section says that device starts these on its own and this computer
doesn't — the switch here isn't the reason — with **Run here** in Settings → Your
devices to move them.

**Auto-start — what happens next without me.** The other three tabs are
per-source workbenches: every ticket, every open PR, every open issue, grouped by
where it lives, with the ones that will actually be taken marked by a chip. That
leaves the roll-up question unanswered — the chips are scattered down three lists
in three groupings, so "what is about to start" meant opening three tabs and
reading past everything that *isn't* queued. This tab shows only those rows,
across all three sources, **oldest first**, which is the order the pipeline draws
in (`intake/queue.ts` holds the rule, and the tab strip's badge shares it, so the
count and the list cannot disagree).

Two things are deliberately **not** here. Work the pipeline has already accepted:
*"already queued to start"* is a skip reason, so those items are no longer
eligible — they have become sessions, which is where you watch them. (One
exception: a pending ticket since moved out of the ingest state still shows this
reason, but the pipeline re-checks before launch and drops it instead — see
[ingestion-pipeline.md](ingestion-pipeline.md#story-flow), step 2.) And anything
that already has a session: ticket eligibility rules those out through the ledger,
but PR and issue eligibility only consult the processed ledger, so an item started
by hand seconds ago can still read as eligible, and calling work that is being
done "waiting to start" would be wrong.

**One vocabulary, whichever switch is really involved.** Each of the three
sections is headed with its count and one of three chips — **auto-start on**,
**auto-start off**, **not set up** — regardless of the plumbing underneath.
Tickets are switched by the ingestion engine; PR review and issue handling have
their own toggles *inside* it. An earlier cut let that leak into the labels, so
tickets read "ingestion paused" beside a PR reading "switched off", which invites
you to think two different things are wrong. The difference is real, and it
belongs in exactly one place: the sentence under the heading, which names the
switch to flip — *"They won't start on their own: Tickets is off (above). You can
still start any of them here, with **Start**."* — naming the switch as the
switches above name it. The sentence comes from the same state as the chip
(`stalledText` in `QueueTab.tsx`), so a *not set up* chip is never followed by
"auto-start is off" and a section that can't connect says so once, by source
name. Ticket rows are grouped under the source's name (*Jira — payments*), never
its id. The PR and issue sections follow their own switches
only; they used to be read as stalled whenever the ticket switch was off, which
was wrong — the pipeline runs whichever halves are on. A ticket section whose
sources are all saved but can't be reached says *can't connect* and names the
source and why (*Shortcut rejected the token*) rather than *not set up*.

A row carries the **will auto-start** chip only while its section is **auto-start
on**. When the section is off or not set up the chip would be a promise the
pipeline won't keep, and the sentence under the heading already says why.

Each heading also carries an **Open Tickets ›** / **Pull requests ›** /
**Issues ›** button through to the tab that owns the setup, and a section with
nothing in it says *"Nothing
waiting."* on one plain line — or *"Nothing configured yet"* when it is `unset` —
rather than in the boxed empty state the other tabs use, because three empty boxes
make a quiet queue look like a broken app.

**It doesn't only watch.** Every row is the same `WorkItemRow` the other tabs
use, with the same per-launch agent, depth and effort pickers and a **Start**
button, so waiting for the sweep is the default rather than the only option — and
when auto-start is off, starting from here is the entire point of the tab.

**One anatomy, three tabs** (`components/intake/kit.tsx`): an intro line → the
**auto-start line** (where the switch stands, and the way to it) → (on the GitHub
tabs) the **GitHub line** → a **work list grouped by source**, each row carrying
why auto-pickup did or didn't take it → **Advanced options** (the tab-wide
defaults, on the GitHub tabs, once a repository is watched). An empty GitHub PRs
/ GitHub issues tab is one card instead (`GithubSetupCard`): **1 Connect GitHub**
(✓ *connected as @you* once it is) → **2 Pick a repo…** (opens the add flow's
repository step) → **3 Turn on** (a link to the Auto-start tab). The add flow no
longer opens there by itself; **+ Add source** still opens it.

**Add once, edit where it shows.** There are no source cards. A configured
source — a tracker, a watched repository — is its **heading in the work list**,
and that heading is where it is edited:

- Every configured source has a heading, always — empty, unreachable, still
  loading (no count yet) or filtered to nothing (**0**, *no match*). A
  repository a stale listing still names but that is no longer watched gets a
  heading without an Edit (*no longer watched*).
- The heading's right edge is **Edit** — or a red **Fix** when the source is
  broken (it can't be listed, its last Test failed, it has no credentials, a
  Test-filled field is still empty, or a save was refused), or **Done** while
  its editor is open. One editor is open at a time per list.
- The editor opens **attached under the heading** (`.ik-editor`), where the
  problem was read — the heading and its editor read as one object.
- The heading's note is the source's setting, and pressing it opens the
  editor: a ticket source reads *auto-starts Product Development · Ready for Dev*
  (prefixed *anyone's ·* on an Anyone's source; *auto-starts nothing yet — pick a
  state* in amber when none is picked; *can't connect — rejected the token* in
  red with Fix), a repository names the CLI its sessions run (*codex*, or
  *claude (default)*).
- A source the listing could not reach says so once, under its heading, in
  words — *Jira — payments rejected the token — press Fix on its heading* — never
  raw JSON or an internal id.

The editor has the same sections for every kind: **Connection** (the
credentials, and the Test that proves them — a ticket source's pill sits beside
the section title) → **What it takes** (whose tickets, which states) → **Where it
goes, and who works it** (repo, agent, fast-track) → **Advanced options**. A
ticket source's editor shows them one at a time, as the add flow's steps
(**1 Connect** · **2 What it takes** · **3 Where it goes**, `.ik-edit-steps`):
it opens on Connect when the source can't connect, else on What it takes. The
closed **Advanced options** fold says what it holds on its summary line (*—
Name: Jira – EU · Effort: high*, or *— nothing changed*). A Test-filled field
is labelled *Member ID (filled in by Test)*.

**Rows.** A startable row's line says where it goes first: *→ quickpay/quickpay ·
claude · Fast-track: Off* (`owner/repo` for a GitHub remote), with **Options**
holding a **Repo** picker when the source has several. A row with a session —
on any tab, just started or listed with `has_session` — has one control,
**Open session ›**: it closes Intake and brings that session forward
(`openSessionFromIntake`); Intake remembers its tab, so `Alt+I` returns to the
same place. Tab badges say what they count in their tooltip (*2 ready to start —
the ones auto-start would take (7 listed)*). A ticket source that can't connect
shows the server's `source_status[id].connect_error` in place of its list —
*Can't connect — No Jira site at acme.atlassian.net — check the Site URL — press
Fix.* — never "No tickets are assigned to you"; the footer names the heading's
real button (**Fix** or **Edit**). Its
footer is the save line and **Remove**, which **arms** rather than fires: the
first press turns it into a red *Confirm — remove Jira — payments* (it disarms
on blur or after five seconds), the second removes it — inline, because these
live inside an `aria-modal` dialog and the desktop app has no native confirm.

**+ Add source** — the dialog head's one door (`#intake-add-source`), replacing
the four "+ Add" buttons that sat under four card lists in two dialogs. It opens
an inline three-step flow at the top of the tab that owns the kind
(`AddFlowShell`): **What** (tiles: every tracker in the catalog, and
**Repository**) → **Connect** → **What it does**. Each tile carries one line
saying whose work it brings in (`trackerKinds` / `REPO_KIND` in `kit.tsx`) —
above all so **GitHub Issues** the tracker (*Issues assigned to you, as
tickets*) never reads as **Repository** (*Every PR or new issue in a repo*,
the GitHub issues / PRs tabs). On Tickets (and Auto-start) the tiles start with
**nothing picked** and **Continue** disabled under a one-line *Pick your
tracker* hint: a tile only marks the choice, Continue makes the draft, and the
title stays *Add a source* until then — it becomes *Add Jira* once Jira is
chosen. (v0.8.6 drafted the catalog's first entry, so a new user opened a card
already titled *GitHub Issues* over a Provider select set to it, and read it as
"I'm configuring GitHub Issues" before choosing anything.) On GitHub PRs /
GitHub issues the Repository tile is picked — the tab's own kind, never a
tracker — with *PR comments* / *New issues* ticked. A tracker tile pressed on a GitHub tab switches to Tickets with that
tracker's draft open. With nothing configured on a tab, the flow **leads** the
tab on its own until you Cancel it (then an empty line offers **+ Add a ticket
source** / **+ Add a repository**). The tracker and repository halves:

- **A tracker** — picking its tile and pressing **Continue** (‹ Back keeps the
  tile picked AND the draft as typed, a pasted token included — **Continue** on
  the same tile resumes it; past step 1 another tile switches the draft
  straight over; switching Intake tabs keeps it too — only **Cancel** drops it)
  starts a draft (`needs_filter: "on"`: it
  auto-starts nothing until a state or label is picked). **Connect** is the
  credentials, each tracker's **API token** with a **Create a token ↗** link to
  the tracker's own token page; **Test & continue** tests the typed values
  WITHOUT saving anything (`POST /api/settings/test/ticketing` with no
  `record`) — the draft stays local, and the verdict, member id and state list
  land on it so step 3's pickers fill in. A failed Test stays on the step with a
  sentence that names the field (Jira: *No Jira site at acme.atlassian.net —
  check the Site URL*, *Jira rejected the email + token pair*, *Can't reach
  acme.atlassian.net from this machine*) and offers *Continue anyway*; the
  *Fill in the marked fields first.* line clears as soon as they are. **What it does** is whose tickets, the states
  and labels, the repo(s), agent and fast-track, and the Advanced fold.
  **Finish** refuses *Anyone's* without a state or label (the server refuses
  that save too) and a source with no repo, and **never switches Auto-start
  tickets on** — adding a source is not consent to start sessions from it.
  **Finish is the one save**: it PUTs the source, then tests it again with
  `record: true` so the stored verdict is honest (a source that still can't
  connect is added with *can't connect yet: …* in the toast; the server, not the
  page, decides whether *Connect your tracker* is done; a save that fails with
  nothing to mark says so — *Not saved — …. Try again.*). Cancel leaves nothing
  behind — nothing was stored. Picking **GitHub Issues** offers *Add it
  as a Repository instead* for "every new issue in a repo".
- **A repository** — the slug once (`owner/name`; the hint names repositories
  already known — on any list, or a ticket source's GitHub repo), **Test
  access** beside it, the GitHub line, and **Use it for** ticks: *Address review
  comments on my PRs* · *Start on new issues* · *Verify what ships*. Lists the
  repository is already on show ticked and locked (*already*). **Add & continue**
  saves every ticked list in **one** `POST /api/settings` (`saveGroups`), then
  shows each ticked use's own fields; **Finish with defaults** saves and closes.
  Nothing here turns a switch on: the first PR repository of a setup that never
  chose is saved with `github.enabled: false`, issue handling stays opt-in.

**View, not settings.** On Tickets, *which states this panel shows* and *whose
tickets it lists* are one **View** menu on the toolbar (`#tk-view`, reading *View:
everyone's · 2 states hidden*): **Whose tickets** (*Everyone's* / *Only mine*,
only when a source ingests anyone's) and **States shown** (one checkbox per
workflow state, with its count). They used to be two selects (*Everyone's
tickets*, *+ Show state…*) and a ✕ on each state heading, and they read as the
source's own **Whose tickets** and **Auto-start states** — which they are not:
the view is per browser, what auto-starts is on each source's heading.

What legitimately differs stays in the tabs: only tickets have workflow-state
buckets inside a source, and only the two GitHub tabs share one credential.

- **Tickets** — its switch is **Tickets** on the Auto-start tab (the same
  `/api/mindflock/status` + `start`/`stop` contract, query key and 4 s interval as
  the sidebar's **Tickets** bar, so the two never disagree). Turning it on while a
  saved source can't connect (its last Test failed, or the listing couldn't reach
  it) asks first — *"Shortcut can't connect. Shortcut rejected the token…
  **Turn on anyway** / **Not now**"* — and while it is on, the switch row says
  *Shortcut can't connect — nothing starts from it until fixed*. A ticket
  source's editor (`TicketSourceEditor`) runs in the order a first-timer fills
  it: **Connection** — the credentials (**API token** …) → **Test** (which also
  loads the workflow states); **What it takes** — **Whose tickets** beside
  **Auto-start states**, then **Auto-start labels**; **Where it goes, and who
  works it** — **Repo URL** / **Repos**, then **Agent** beside **Fast-track to**.
  A collapsed **Advanced options** fold holds the source's **Name**, **Thinking
  effort** and the catalog's optional extras (**Move to state on start**,
  **Comment when moved**, **Claim label (on start)** — whose hint says it assigns
  the ticket to you, except on an *Anyone's* source where it only labels it — the
  optional Test-filled account ids, Jira's **Project key**). The footer is its
  save line (*Saving…* / *Saved ✓* / *Not saved*) and **Remove**; its pill reads
  *Can't connect* with the reason when the last Test failed or the listing
  couldn't reach it. Text fields **save as you type** (on a short pause, on blur,
  on Enter, and when the dialog closes — `intake/autosave.ts`), and the save line
  reads *Saving…* while any field holds text not sent yet, never *Saved ✓*.
  **The safe default:** a source added here auto-starts **nothing** until at
  least one auto-start state (or label) is picked (`needs_filter` on the source;
  the picker says *None picked — nothing auto-starts from this source yet*, rows say *no
  auto-start state picked yet*, and the pipeline's scan skips the source —
  `providers.base.auto_start_waiting`). **Any state — start every ticket assigned
  to me** stays in the state menu as an explicit choice; sources saved before
  this keep their old "empty = every state" rule. Until a Test has passed a
  state can be **typed** (*Type a state name*); it is swapped for the tracker's
  real state once a Test lists one by that name, a typed name the tracker lacks
  shows as a red chip, and the server starts nothing on a name it can't match
  (Shortcut would otherwise drop it and search every state). A saved state whose
  name isn't known yet shows as *Saved state*, and a chip's ✕ reads *Stop
  auto-starting "Ready for MindFlock"*, never the state's id. **Whose tickets**
  shows the hint for the option that is selected. The work list reads each source's
  auto-start filter from its **saved** settings, so a source the tracker can't
  reach still says *Auto-start only takes tickets in 1 saved state* — never "no
  filter is set". A source that can't be listed is said once, in its own group,
  by its name, in words (*Shortcut rejected the token — press Fix on its
  heading*) — no raw API JSON, no internal id like `sc`, and no group at all for
  a draft that hasn't been saved yet. Rows say **Start** — the quiet weight on a row that
  will auto-start anyway, so the *will auto-start* chip carries it — and every
  row names its repo (*→ payments-api*; on a several-repo source, which one and
  why, with a **Repo** pick under **Options**). **Combine with another ticket
  (deletes this one)…** lives at the bottom of the row's **Options**, under *On
  the tracker*, never beside Start; Escape closes that drawer, then the row's
  Options, before the dialog. A Start the tracker refuses says so in one
  sentence (*Shortcut rejected the API token. Fix it in Intake → Tickets…*),
  never its raw JSON. Then
  **Assigned tickets** — the slowest of the three fan-outs (~3 s: a provider
  search per source plus a `git ls-remote` per repo). A **Shortcut** source
  lists what Shortcut's own boards show: archived stories, and stories under an
  archived epic, no longer appear at all. Bucket counts can therefore drop
  without anything changing in the tracker, and a source whose open work is all
  archived now renders an empty heading. No other provider hides archived work,
  and no setting turns this one off
  ([ingestion-pipeline.md](ingestion-pipeline.md#providers)). A source's **Agent CLI**
  lists `GET /api/providers` (so a provider you defined yourself is selectable
  too), its unset option names the app default rather than showing a blank, and
  the source's heading always states which CLI the queue runs — the difference
  between a queue on a hosted CLI and one on a local model is worth seeing
  without opening anything. **Thinking effort** opens the Advanced fold, first thing
  in it, because it qualifies that CLI: a rung means different things on different CLIs (each spells
  it its own way and clamps its own ceiling), so the picker names *that* CLI's
  ceiling and is disabled outright for a CLI with no effort control. It applies
  to every ticket from the queue — ingested automatically or started by hand —
  and an individual ticket can still override it on its row, where the empty
  choice reads **Default (…)** naming the queue's rung. Unset means
  whatever the CLI does on its own; there is deliberately no flock-wide default,
  because how hard to think is a property of the work rather than of the
  installation. Unset **Agent CLI** falls back to `[mindflock].agent`, then the app
  default (see
  [ingestion-pipeline.md](ingestion-pipeline.md#which-agent-cli-a-ticket-runs)).
  **Move to state on start** (Shortcut/Jira/Linear, and Asana as **Move to section on start**; opt-in per source) is
  the ingest-state picker's mirror image: that one filters on where a ticket
  *is*, this one names where it *goes* once a session starts for it — so a board
  stops showing work an agent is already doing as untouched. It reads the same
  live state list (populated by **Test connection**), takes one state, and its
  empty choice — **Leave it where it is** — is the default and the pre-existing
  behavior. It applies to both ways a session starts: the pipeline picking the
  ticket up and **Run ticket** on a row. The move happens after the session is
  live and never fails a launch — a tracker that refuses it (most often a Jira
  status with no transition into it from where the issue sits) logs a warning
  and the session keeps running.
  **Comment when moved** (Shortcut/Jira/Linear/Asana) sits directly under the
  start-state picker: **Off — move it silently** (the default) or **On —
  "MindFlock (you) is taking this on."**, signed with the first name of the
  connected tracker account. It does nothing without a start state, and posts
  only when the move actually changed the ticket's state — so **Run ticket** on
  a ticket already in that state doesn't comment twice. A refused comment is a
  warning; the session keeps running.
- **Pull requests** — **Auto-address review comments**: when a reviewer comments
  on one of *your own* open PRs, a session addresses the comments (it never
  reviews other people's PRs, and the tab never says "review" for it). Off until
  switched on, like Issues: a setup that never chose saves its first repository
  with `github.enabled: false`; saved settings from before keep their meaning
  (unset with repos = on). **Your open pull requests** with **Start**, one
  heading per watched repository; **Skip authors** is now **Ignore comments
  from** (saved as you type, like a repository's own fields). Under the switch, the **GitHub line** says the credential's real state
  — *GitHub — connected as @you — one sign-in for PRs, issues, Verify and Open
  PR · Manage…*, or, amber, *GitHub — not connected — your PRs can't be listed
  or started · Connect GitHub* (`#gh-connect-btn`, from `GET /api/github/status`,
  re-read when the Connect GitHub dialog closes) — opening the [Connect
  GitHub](#first-run-onboarding) dialog: one sign-in, shared with your other
  devices; its **Test** checks the token and your first repository, a
  repository's **Test access** checks one repo. It used to be a stateless
  *Connect GitHub* button inside the Advanced fold that read *Connect* while
  connected. There is no raw token field and no *Test GitHub* button.
- **Issues** — **Auto-start new issues** (opt-in: absent = off), **Open issues**
  with **Start**, one heading per watched repository (`github.issue_repos`,
  independent of review's — the *Use it for* ticks put one repository on both).
  The same **GitHub line** (`#gh-issue-connect-btn`): one credential for both
  tabs.

**Basic vs Advanced, and one rhythm.** Which provider fields are basic is the
catalog's call, not the form's: a field marked `advanced` in
`backend/ticket_ingestion/providers/__init__.py` (never a `required` one — a
hidden field can't be filled) renders inside the editor's **Advanced options**
`<details>`, with the source's name and thinking effort. Fast-track left the
fold: it sits beside **Agent**, where a repository's editor puts it too. The
fold starts closed on a fresh source and **opens on its own** when anything
inside already holds a value someone chose, or when a save refused a field
inside it (`advancedHasValues` in `intake/sourceForm.ts`; a Test-filled id
doesn't count, or every connected source would open it). Inside an editor a
field's own parts sit tight — label, control, hint 5px apart — and the next
field starts 16px below, so a long hint can never read as the next field's.
Fields that pair (Whose tickets / Auto-start states, Agent / Fast-track, a
repository's Agent / Fast-track / Min age) sit side by side in an even grid —
never an auto-fit one, which re-wraps a different number of columns at every
width — and fold to one column below 700px. The work lists keep their own
density.

**A ticket source is connected only after a live test.** A source's editor
carries a three-state pill beside **Connection** — **Connected** (the last Test passed within
a day and nothing Test fills in is missing), **Needs attention** (with why:
*not tested yet — run Test*, the failed test's own message, *<fields> still to
fill in (run Test)*, *last tested over a day ago — retest*) or **Off** (*no
credentials yet*) — read from the verdict stored on the source
(`last_test`), never from whether a token happens to be saved; Settings →
Connections reads the same rule. Required fields wear a red **\***, and a
field Test fills in says *filled by Test*. A save is checked against the
provider catalog (required fields, URL / email / key / gid shapes, *Anyone's*
needing an ingest state); what it refuses shows inline under each field, the
pill reads **Not saved** — *fix the marked fields* — and the typed values stay
for the next try. A save that passes runs Test on its own (only when it
touched the provider or a credential field), and **Test** — **Test again**
once a verdict exists — stores its verdict, member
id and state list on the source, so the state pickers open filled and refresh
from the tracker on focus. A stored secret reads *•••set (saved) — type to
replace*; blank keeps it, and **Clear** forgets it (an explicit `null` on the
PUT). Jira has a *Project key* and Linear a *Team key* (both optional — where
New → Ticket files, and Linear's bound on *Anyone's*). Asana's **Workspace**
is looked up by Test from the token — one workspace fills it in, several offer
**Pick a workspace…** — and it gains **Ingest sections**, *Anyone's — by
section*, **Move to section on start** and **Comment when moved**. A GitHub
Issues source has no token field; one saved before the single GitHub credential
still carries its own and says *This source still has its own token — move it
to Settings → Connections → GitHub → Connect (read as a fallback this release,
ignored from the next)*.

A ticket source's and a repository's **Fast-track to** offer **Off** plus the
same ladder minus Merge — *How far each ticket
goes after its agent finishes. Merge is per-ticket only — a source default runs
with nobody watching.* A source that stored the old `agent` depth shows Off.

**Per-repo settings, with inherited defaults** — a watched repository's editor
(`RepoEditor` in `intake/RepoSources.tsx`, attached under its heading) carries
its **Agent CLI**, **Fast-track to**, **min age**, **base branch** (PR only —
issue work always branches off the repo's own default), **ignore … from**,
**Test access** and **Remove**; the heading always names the CLI that repo's
sessions run. A per-repo value is what a multi-repo setup needs — "watch this
one too, but give it half an hour of grace and run it on codex". The tab-wide
fields are in the tab's **Advanced options** fold and are the **defaults a
blank field inherits** (the placeholder shows the inherited value, so empty
reads as "inherit 15" rather than "no grace period"), alongside the genuinely
one-per-app setting, the poll interval. The GitHub token is not here: it is the
Connect GitHub dialog's.

**One repository, three uses.** The same repository can be watched for PR
comments (`github.repos`), new issues (`github.issue_repos`) and by Verify
(`repository.verify_repos`) — still three lists in settings.json (settings
sync, the pipeline's restart signature and older devices read those keys), but
on screen it is added once and its uses are ticks. Every editor starts with
**Use it for: PR comments · New issues · Verify** — its own surface's tick shown
on and locked (taking it off the list you are looking at is Remove's job, which
asks first) — and ticking another puts it on that list. `intake/repoUses.ts`
turns ticks into **one** `POST /api/settings` spanning both groups
(`useSettings().saveGroups`: one echo, one settings-hook pass, one pipeline
restart), and owns the rules each list carries: the first PR repository of a
setup that never chose is saved with `github.enabled: false`; leaving an
Intake list drops that repository's block (a couple of menu picks); leaving
Verify **keeps** its block (the user's own words — live branch, where it runs,
notes — and the backend reads an orphan block as nothing). It replaced a
hint that admitted *Separate from the other GitHub tab's list*.
Storage is `github.repo_settings` / `github.issue_repo_settings` — maps keyed by
`owner/name` whose block may carry `agent`, `depth`, `base_branch` (PR only),
`min_age_minutes`, `skip_authors`; an absent key or blank value inherits. A
field's save is built on the editor's own copy of the map, so two quick edits
compose (the map is written wholesale). See
`backend/config/settings.py` (`REPO_OVERRIDE_KEYS`, `_repo_overrides`,
`GithubSettings`) for the shape and `backend/ticket_ingestion/config.py`
(`GithubConfig.min_age_for` / `base_branch_for` / `skip_authors_for` /
`agent_for_repo` and the `issue_*` twins, plus `pr_agent(repo)` /
`issue_agent(repo)`) for how the monitors resolve them — every filter goes
through one of those rather than reading the flat field.

**Test access** (`POST /api/settings/test/github-repo`, body `{repo}` →
`{ok, name, private, default_branch, can_push}`, always HTTP 200) is the
per-repo twin of the Connect GitHub dialog's **Test** (`/api/settings/test/github`,
who the token is and whether it reaches your first repository): this one
answers "does it reach *this* repo", which is the failure people
actually hit — a typo'd slug, or a private repo the PAT has no scope for (GitHub
404s both, so the message covers both readings). On the Issues tab a read-only
token is called out, since issue work has to push a branch.

**Work lists group by source — always, on every tab.** "Which provider is this
from?" is the question a queue has to answer before any other, and a heading
answers it once for however many rows sit under it. A per-row label was the
alternative and it does not scale: the same name repeated down a column of 500.

**Tickets add a third level, because the providers have one.** A source with
several workflows/boards has to qualify its state names to keep them unique —
Shortcut returns `Product Development · Deferred`, `DevOps · Unscheduled` — so a
flat list wrote `PRODUCT DEVELOPMENT ·` onto seven consecutive headings. The
workflow becomes a level of its own and the qualifier is written once:

```
▾ Allure Security          52   auto-ingests Product Development · Will do
   ▾ Product Development   48
      ▸ Deferred           20  ✕
      ▸ Unscheduled        13  ✕
      ▸ Will do             4  ✕
   ▾ DevOps                 3
      ▸ Unscheduled         1  ✕
   ▾ Creative               1
      ▸ Unstarted           1  ✕
```

Every heading in that tree is a press-target and is styled as one: a hit area, a
hover fill, a caret that *rotates* rather than swapping ▸ for ▾, and — on the
source, which is a section header as well as a control — a filled band. As plain
text beside a small caret, the top-level heading read as a stray line of copy and
people did not find the tickets under it. The source band also carries the
source's **Edit** / **Fix** and its pressable note (see *Add once, edit where it
shows* above).

`GET /api/tickets` carries a `bucket_meta` map (`{bucket: {group, label}}`) so the
UI nests without splitting strings: the provider reports the workflow (`group`)
and the unqualified state (`label`) alongside the composite `name`, which stays
the unique bucket key that the **View** menu's *States shown* and `done_buckets` are indexed by
(see `TicketProvider.list_states`). A provider with one workflow — or none, like
Jira and Linear — reports an empty `group`, the level doesn't render, and the
bucket keeps its own name. The two GitHub tabs have no analogous middle level:
their hierarchy is repository → rows.

The
source heading carries a count and what that source auto-starts ("auto-starts
In Progress, Ready" / "auto-starts every state" / "can't connect — …"), which
is also why `GET /api/tickets` gained a top-level `source_labels` map covering
**every** configured source — one that returned nothing, or errored, still needs
a heading to say so under. Bucket **show/hide** is app-wide by name (choosing
"I don't care about Completed" means it everywhere; done-type buckets start
unticked under **View**), while a bucket's **open/closed** state is per
source, keyed `source::bucket`. Both live in `localStorage`
(`mf_ticket_buckets`, `mf_ticket_buckets_open`, `mf_work_ticket_sources`;
`mf_work_pr_groups` / `mf_work_issue_groups` for the repo groups) rather than
settings.json — "I collapsed Backlog on this laptop" is not a property of the
flock. Source groups default open (the rows are why you came), state buckets
default closed (the headings with their counts are the overview), and both are
stored as the set of exceptions so a fresh install writes nothing. The panel
still lists work you are about to move *into* an ingest state, not only what
already matches the source's filters — Jira, Linear and Shortcut annotate each
ticket with its workflow state, while GitHub Issues and Asana expose no
workflow-state model and land in `No state`.

**Work that is already on this machine leads with Reopen.** A row for an item
that has been worked once already said so in chips — *already started before*, *a branch
for it exists on the remote* — and then offered exactly one button, **Begin work**,
which starts it over. But the workspace is usually still right there: ending a
session keeps its worktree (see Recently closed), and a run whose session was lost
to a restart leaves its worktree behind too. Starting fresh on top of that either
collides with it or silently duplicates it.

So all three panels ask the other question — *is the work still here?* — in one
place (`backend/web/core/reopen.py`), because the three share their row rendering
and must not disagree about what a row can do. Resolution is most-informative
first: a **recently-closed session** for the item, whose stashed data restores the
branch, program, prompt and provisioning flags rather than approximating them; a
**provisioned clone directory** (`pr-<slug>`, or a clone-strategy ticket/issue
workspace — both deterministic paths, so an `isdir` is the whole probe); or a
**worktree still holding the item's branch**, found by asking the base clone (one
`git worktree list` per repo per request, indexed). A directory with no `.git` in
it does not count: a half-deleted workspace would otherwise offer a Reopen that
lands the agent somewhere every git-backed panel then fails against.

When something is found the row leads with **Reopen window** — the primary
button, with the start demoted to the quiet one beneath it, since reopening is the
action and starting over is the alternative. Its tooltip names which thing is
being reopened (*session ended 2d ago* / *workspace left on this machine*), its
branch and its path. Nothing is hidden: a leftover workspace can be one you mean
to abandon, and **Begin work** still reclaims it when it holds no work.

`POST /api/intake/reopen` takes the item's identity in the same shape its start
route takes, never a path from the client, so a panel left open for an hour cannot
name a directory that has since been deleted — the server re-resolves and answers
**410** (*"no workspace for this item is left on this machine — use Start to
start it fresh"*) when it is gone, **409** when the session is already open, and
restores a closed entry in full or, for a workspace whose session is gone, opens a
fresh **in-place** session on the directory. In-place because the directory is not
MindFlock's to delete: ending that session must leave the work exactly where it
was. Every part of the probe is read-only and best-effort, and any failure answers
"nothing found", which is precisely how the panels behaved before.

**Duplicate tickets are merged away, in the tracker.** Two people file the same
task constantly, and until this existed the queue's only answers were to start
two sessions on it or to remember forever that sc-41 is "really" sc-38 — neither
of which is visible to the person who filed the second one. So a ticket row's
**Options** end with **Combine with another ticket (deletes this one)…**, under
an *On the tracker* heading (tracker housekeeping, kept away from **Start**):
pick the ticket to keep from the *same* source, confirm (*Confirm — delete
sc-41, keep sc-38*), and
everything the duplicate has — description, acceptance criteria, comments and
attached files — is appended to the survivor's description, a comment on the
survivor records where it came from, and the duplicate is **deleted from the
tracker**. In Shortcut, Jira, Linear and GitHub, not just here; Asana's adapter
implements no merge writes, so its rows never show it (the server stamps
`merge_ready` per row from the adapter's own `can_merge`, rather than the UI
keeping a second list of which providers can write). *Merging* is the capability
Asana lacks — it can still **file** a new ticket, which every provider can; see
[New → Ticket](#new--ticket-file-a-ticket-by-describing-it) for the other
direction, filling this panel rather than tidying it. A ticket filed there shows
up in this list because filing drops the panel's cached fan-out, not because the
panel went back to the tracker — so it arrives on the next poll rather than
instantly.

The four steps run in an order chosen so that every partial failure is a
*recoverable* one, and the surface is built to report which one happened rather
than to claim success: the append runs first (a failure there leaves both
tickets exactly as they were), and the delete runs **last** (a failure there
leaves a duplicated ticket rather than an erased one). So the result is not a
tick — it is a sentence, and only "the content is on the survivor AND the
original is gone" is green. *"…was copied into sc-38, but it could NOT be
deleted — your Jira account needs the project's Delete Issues permission"* goes
to the bottom-right card instead of a 1.4 s toast, because it is an instruction
and somebody still has to act on it.

The trigger sits in the **action column**, at the quiet weight the demoted
**Begin work** already uses. It shipped for one afternoon as a chip in the meta
line instead — on the theory that a rare action should not cost a common row any
height — and that was wrong twice: the meta line wraps, so on any row with a real
skip reason it took a line anyway, and a control sitting among `6d old` and `not
in an ingest state` inherits their grey passive reading and stops looking
pressable. It was reported missing by someone looking straight at it. The meta
line is where a row states facts about itself; actions belong in the column that
holds actions.

The confirmation is a **drawer under the row**, not a dialog and not a native
`confirm()`. These rows already live inside an `aria-modal` dialog, and a second
window opened from inside one is painted by the wrong layer and steals focus
from a dialog that still thinks it owns it — the lesson Verify wrote down at
length. The primary button **arms** rather than firing: the first press turns
*Merge and delete sc-41* into a red *Confirm — delete sc-41*. Everything else on
this panel is reversible, and this one reaches into a system MindFlock does not
own and destroys something other people can see; two presses is the cheapest
honest guard, and it costs nothing because nobody merges tickets in a hurry.
A session open on the merged-away ticket is named in the drawer and deliberately
left running — the work in it is real and possibly uncommitted, and closing it
is not this button's decision.

**Every row can be started on a different coding CLI, for that one launch** — a
small picker beside **Begin work** / **Begin review** / **Start work**, whose
empty choice names what the row's source or repo card would use ("Default
(codex)", or "Default (app default)"), so it is never a mystery. You notice mid-review that this one wants a
different model; re-configuring the whole queue to run one item is the wrong
shape of action, so the override is not persisted. All three start routes
(`POST /api/tickets/start`, `/api/github/prs/review`, `/api/github/issues/start`)
accept an optional `agent`; it outranks the source/repo card's own agent, and an
unknown name is a **400** rather than a silent fall back to the default
(`_start_agent_override` in `server.py` — a typo that quietly ran the wrong CLI
is worse than a rejected request).

Beside it sit the other two per-launch pickers, on the same line and with the
same "just this item" lifetime: **how far** to carry it and **how hard to
think** about it. **How far** is the same Fast-track ladder as everywhere else
— **Off** / **Commit** / **Push** / **Open PR** / **Merge when green** — led by
**Default (X)**, naming what the source would do; an explicit **Off** is
offered only when that default isn't already Off, and an individual item may
choose *Merge*, which a source default may not. There is no *Agent only* rung:
a stored `agent` depth reads as Off, which is what it always did.

**The pickers fold behind Options.** A row shows a muted one-liner of what
**Start** will use — e.g. *claude · Off · default effort* — and a quiet
**Options** toggle (`.ik-item-opts`, `aria-expanded`); the three selects appear
when it is open, or as soon as any of them holds a choice, so a pick is never
hidden. The effort ladder is neutral — Low, Medium, High, Extra
high, Max, Ultra — and the server translates the rung into whichever CLI the row
launches, so the picker labels what will actually happen: a rung above that CLI's
ceiling reads "Max (→ Extra high)", the top rung is named the way the CLI names
it ("Ultra (ultracode)" on Claude Code, which starts the session with `--effort
ultracode` rather than at Max), and a CLI with no effort setting disables the control
outright ("No effort control (aider)") rather than leaving one that quietly does
nothing. The rungs come from `GET /api/providers` (`effort.levels`); the
translation table is in [providers.md](providers.md). All three pickers share one
line-and-a-half above a button that hugs its label: they used to stretch the
primary button to their own width, which turned **Begin work** into a bar.

**An intake-armed run no longer commits under the item's name.** A run started
from a ticket, PR or issue used to carry that item's name as its commit message,
and a run armed with ⏩ carried a generated *"Work on `<session>`"* — both
describe the *request*, not the change that was made. Those are now marked as
**placeholders**, and at commit time — the one moment the diff is final — the
placeholder is replaced by a message written from that diff (the same generator
the ✨ button uses), with the item's id and title passed in as context and kept
as the fallback if the generator fails or times out. A message **you typed** is
never touched, and neither is the on-disk one a blocked commit left behind for
its retry. Re-arming with ⏩ inherits the previously armed message *along with
its placeholder flag*, so pressing it again cannot freeze a placeholder into the
commit.

**Failures pop up bottom-right**, as a card with a red rule — the corner the
connection-lost card already owns (`lib/errorPop.ts`), not the 1.4s bottom-centre
confirmation strip `toast()` uses. A refused start answers with a paragraph of git
output whose remedy is its last sentence ("… is already checked out at `<path>`.
Kill that session first"), and it has to survive long enough to be read: the cards
stack, newest nearest the corner, and stay until dismissed. The same card is where
a *recorded* failure reason goes — the chip shows the front of the sentence and
opens the rest on click, instead of wrapping three lines of git output into the
row and reformatting the list around it.

Two related fixes live behind this surface: a ticketing source's **Agent CLI is
re-read from disk when a ticket is stamped** (`source_agent_now`), so switching
it in the UI applies to the next ticket rather than the next pipeline restart —
and *clearing* it now clears it, instead of the boot snapshot's value living on;
and a **forced** start resolves the per-repo / per-source agent rather than
jumping straight to the app default.

`GET /api/github/prs` also lists PRs into **every** base branch now and explains
a non-matching one as a skip reason (`"targets X, not the watched base (Y)"`)
instead of filtering the row out server-side — a PR sitting there with no chips
read as "queued" when the monitor would never see it. **Begin review** works on
those too.

### Panel lists are cached, not refetched per visit

The three list panels — **Assigned tickets**, **Pull requests**' open PRs,
**Issues**' open issues — each fan out to a slow upstream. They no longer live in
per-tab React state (which the dialog threw away on close, and a tab switch
unmounts anyway); they're held in the query client
(`frontend/src/state/queries.ts`), so reopening the dialog or switching away and
back shows the last list **immediately** while a refresh runs behind it. The
panel's note area says `Loading…` on a cold panel and `Refreshing…` over rows
already on screen. A cached list can therefore still hold a row for a story
archived since it was fetched — and **starting that row bypasses the archived
filter**, because a force-start goes through `fetch(ticket_id)`, which does not
apply it. **Refresh** re-lists and the row disappears.
Opening the Intake dialog **prefetches all three**
(`prefetchIntakePanels()`), so clicking through to one finds it loaded — and it is
what fills the tab strip's counts, which read the same cached query the tab does
and therefore can never disagree with the list underneath.
**Refresh** sends `?fresh=1`, which skips the server's cache and waits for a real
sweep, so the click means what it says. A force-start row action deliberately
does *not*: it re-lists from the cache a few seconds later, because `has_session`
is annotated live on every response — even a cached one — so the cheap re-list
already shows the session it just created.

Two consequences worth knowing:

- A failed load now **keeps the previous rows** and adds an error banner instead
  of emptying the panel, and the server keeps serving its last known list for up
  to 5 minutes through an upstream blip (see [web-api.md](web-api.md)). A
  per-source failure on the Tickets tab is reported on that source's own heading
  and in the note line, not by failing the whole panel.
- Because opening Intake warms all three, it costs up to three ticket/GitHub
  fan-outs even if you only glance at the counts — and an integration that isn't
  configured yet will have its `Could not list …` error ready the moment you
  first open that tab.

## Customize

The sidebar footer's **⚙ Customize** button (title *Choose which bars the
sidebar shows — Prompts, Assistant, Tickets…*) opens `#customize-dialog`
(`components/customize/CustomizeDialog.tsx`): **two pages**, switched by the
**Sidebar · Pane header** tabs at its top. It opens on the page you last
looked at (this browser's `mf_customize_page` — a convenience, not a pref).
Each page has one line saying what it changes and a **Reset to default** at
the end of that line (greyed while the page is already the default). Every
row on both pages is the same shape (`CustomizeRow.tsx`): a ⠿ grip to drag
it, a switch, the item's name over one line saying what it is or when it
shows, and **↑ ↓** to move it from the keyboard. Long explanations sit behind
an ⓘ (InfoTip), not in the rows.

**Sidebar** — *Choose which bars sit around your session list, and in what
order.* *Show in the sidebar*: one row per bar, built-in and extension, in
sidebar order (the same `hidden_bars` / `bar_order` prefs the bars always
used), with the session list drawn as a fixed dashed divider — moving a bar
past it moves it below (or above) your sessions, as dragging its grip in the
sidebar does. Each built-in bar says when it has something to show —
*What your sessions cost* (Usage); *Appears once a ticket source is connected*
with an **Intake → Tickets** link (Tickets); *Appears once a repository is
added* with **Intake → GitHub PRs** / **Intake → GitHub issues** (Pull requests,
Issues); *Appears once Verify tracks a repository or has a checklist* with
**Open Verify** (the sidebar's Verify bar — the top bar's **Verify** button is
always there); *Chat, a todo list, and its editable agent file* (Assistant);
*Your saved prompts — pick one to paste it into the session you are in* with **Manage
prompts** (Prompts); *From an extension* for an extension's bar. *Session list*
→ **Filter box**. Reset brings back the starter bars (Usage, Tickets,
Assistant), the default order and the filter box.

**Pane header** — *Choose the tabs and buttons along the top of every session
window. The rest stay in its ⋯ menu.* On top, a **live preview** (sticky, so
it stays in view as you switch rows below it): a header drawn with the real
header's classes, twice — *Room to spare* (words) and *Too narrow for words*
(icons) — that changes as you switch or move a row. Then **Tabs** (Agent,
Terminal, Diff, Queue, Map, Thread) and **Buttons** (Fast-track ⏩, Copy
entire history, Next step (Commit…), History, Cost, Diff stats, Hide −), each
ordered among its own kind. Agent's switch is locked on. ⋯ is not a row: it
is always last, and holds everything switched off here (see
[The pane header](#the-pane-header)). The choice is the synced pref
`pane_header` (`{order, hidden}`, localStorage `mf_paneheader`), so it
follows you to your other devices; `null` (Reset) is the default set. An item
a newer build adds, which an older saved choice never mentioned, takes its
own default rather than vanishing or appearing uninvited.

The palette's **Customize…** opens it too. `Esc` or a click on the backdrop
closes it.

Customize holds what is optional. Set-and-forget configuration stays in
[Settings](#settings-), and anything that needs you is in the bell.

There is no Outbox. It used to be a third tab, a read-only log of outbound
work; each thing it held now lives elsewhere:

- what needs you (answers, ask-first approvals, escalations, a lead's plan
  or PR) — the bell's **Needs you** (see [Notifications](#notifications-));
- a team run's queued lines (**Start now** / **Take out of run**) — the team run header's ⋯
  menu (below); for a split or one-for-all team run, which has no header, the
  same buttons sit on each queued piece in its lead's Thread tab;
- a finished team run's summary — **Copy summary** in that same menu, or at the
  top of the lead's Thread tab for a split or one-for-all team run;
- what is shipping — each row's own status line (`⇡ opening PR`, below).

The *Shipped today* log is gone; a session's PR and checks stay on its row.

## Team runs on the rail

Sessions started together sit under a team run header — the same header family
as device groups: `▾ Q4 PAYMENTS → PR (1) 1/6 shipped`. The gold badge is how
many need you; a click folds the team run, and **⋯** on hover has Pause / Resume,
Add lines…, the queued lines (**Start now** / **Take out of run**) and Cancel… (an
inline confirm; sessions and branches are kept). Once the team run has finished,
the menu has **Copy summary** instead: it copies the team run's summary as
Markdown (*Copied the summary as Markdown*). The bell's cached copy covers a
week of finished team runs; an older one is read from the team run's own record
(`GET /api/runs/{id}`), and the item is disabled — *MindFlock has no summary
for this team run* — only when neither has one. The header is
`li.run-group-head` with `data-run` set to the team run's id.
A header appears only for a team run of two or more, and **On their own** only
under at least one header. A split or one-for-all team run never gets one: it is
a family under its lead, and its lead's Thread tab holds what the header's ⋯
would — **Start now** / **Take out of run** on a queued piece and, once finished,
**Copy summary**. While the team run waits, the header shows a badge: *usage*
for a usage limit, or else *memory* while the [memory
guard](session-engine.md#memory-guard) holds its queued lines (tooltip *Memory
is low — queued lines start once it recovers*; the header's tooltip says
*waiting for memory — queued lines start once it recovers*). Queued lines are
dim, with a hollow dot and no number. Headers and queued lines are never rail keys: Alt+N numbering skips
them, and a folded team run's rows leave the numbering like a folded device's.

Every session with fast-track on leads its status line with the target, so a
narrow rail cuts the detail and never the target: `→ PR · working 12m`, `⇡ opening PR`,
`✓ PR #318 · checks ✓`, `? needs you · question`, `→ commit, asks first · idle`,
`! hooks failed twice — open the bell`,
`→ commit, asks first · ready — approve in the bell`. A team run member (or any
session with fast-track on) on a prompt gets the answer strip under its row,
like a family worker — and so does any asking session whose pane is not on
screen, since nothing else there shows its question. A line that ends **open the bell** or **approve in the
bell** is clickable and opens the bell on that session's row.

**Notifications.** The bell's history lists a team run's escalations, its finish,
and each line that shipped, under the team run's name. Clicking one opens the
lead's Thread when the team run has a lead; otherwise an escalation scrolls to its
**Needs you** row, and a finish or shipped row (or an escalation already
answered) closes the bell and shows the team run where it lives: its rail header
scrolls into view and pulses once. A team run with no header — a split or
one-for-all team run, a team run of one, or one whose header was pruned or filtered
off the rail — opens its lead's Thread instead, else selects a member row
still open; with nothing of the team run left it just closes the bell. A
`run.needs_you` toast opens the lead's Thread, else the bell, and a
`run.finished` toast shows the team run the same way. Rows
obey the `run_needs_you` / `run_finished` switches in Settings → Notifications,
and a prompt is never announced twice (the session's own "Needs you" row
covers it). At most one team run toast per 30 seconds.

## Verify

**What shipped, and does it work?** Intake is the front of the pipeline; Verify
is the back of it. The pipeline's last honest checkpoint is "the PR merged", and
merged is not verified — nobody has opened the thing and looked at it. So work
that reaches the branch you actually ship from comes back here as a **checklist**,
an agent works the steps it can from a shell, and whatever needs a pair of eyes
is handed to you as a short list. Top-bar entry **Verify** (`Alt+V`, or the
palette's **Verify — check what was pushed**) — the button is always on the bar,
with its due badge. Dialog `#verify-dialog` / panel
`#verify-panel`, from
`frontend/src/components/dialogs/{VerifyDialog.tsx,verify.ts}`; the sidebar bar
is `frontend/src/components/sidebar/VerifyBar.tsx` and the store is
`backend/web/core/test_plans.py`.

**One noun, one verb.** The feature is **Verify**, the artifact is a
**checklist**, the act is to **check**. "Test plan" is the name in the API paths,
the store file and the settings keys — it never appears on screen, because to a
developer it reads as pytest.

**Two moments, one line each** — the dialog's intro says exactly this, and moves
it into the ⓘ beside the title (*How Verify works*, an InfoTip) once there are
checklists to work:

- **Before merge** — press **Check now** to check the branch. Every step passed
  reads **Verified on branch**.
- **After merge** — it comes back here to be checked again on the live branch.

Checking before merge never costs the check after it: a branch whose every step
passed sits under **Verified** as *Verified on branch*, and when the work ships
it comes back to **Needs you** like any other. The branch's answers are kept as history — the expanded checklist
says *"Checked before merge: every step passed, 2d ago. This is the check after
merge."* — but they are not the current answer, and answers a person gave on the
branch are not carried into the after-merge run. (Server side: a run made before
merge is stamped `prelive`; `test_plans._current_run` ignores such a run once the
plan has shipped.)

**The chain, in order.** Nothing appears here until three things have happened,
and every "why is nothing showing up?" is a question about which one hasn't:

1. **Track a repository** (Verify → **+ Add repository**, a repository's *Use it
   for → Verify* tick in Intake, or the repo's own committed `.mindflock.toml`). Membership *is* the opt-in; there is
   no second on/off per repo. Writing a checklist costs a real model call, so
   nothing happens in a repo nobody named.
2. **Push a session branch in it.** The **first** push of each branch writes a
   checklist — from that branch's diff, and from what the session itself was
   doing. One per branch, not per session: two sessions on the same branch of the
   same repo (a duplicated session, a repo adopted twice) share the checklist
   written under whichever pushed first, and a push from the other one refreshes
   *that* plan rather than writing a second. It sits in **In progress** while
   the branch is reviewed and merged — with **Check now** on it, for checking
   the branch before you merge it. A later push always records the newest
   commit, and — *only while
   nobody has answered anything yet* — rewrites the checklist from the branch's
   whole diff at that commit, at most **three** times and never within five
   minutes of the last one. The moment you answer one step the checklist is yours
   and nothing rewrites it under you; **Rewrite the checklist** is the button for
   that, and it always works from the newest pushed commit.

   It rewrites rather than appending, deliberately: a later commit that reverses
   or renames what an earlier one added would otherwise leave a step that *must*
   fail against correct shipped code, and nothing could retract it.
3. **That commit reaches the live branch, and the deploy lands.** A background
   pass asks `origin` whether each waiting checklist's sha is an ancestor of the
   live branch. **Merged is not deployed**, so that starts a clock rather than
   handing you the checklist: the row reads *"Merged 4m ago — waiting for it to
   deploy"* (verdict **Waiting to ship**) until the repo's deploy window has
   passed, and only then does it move into **Needs you**, raise the badge and (if ntfy is configured) push to
   your phone.

   The window is **Wait after merge (minutes)** in the repository's editor, inheriting
   the flock-wide `repository.deploy_delay_minutes`, which defaults to **5**. Set
   it to `0` where merging *is* shipping. It exists because checking too early is
   not merely early — you see the behaviour the change replaced and record a
   **failure against correct code**, which is the one outcome this surface cannot
   survive, so the wait deliberately errs long. When a deploy lands sooner than
   usual, **⋯ → It's deployed — check it now** skips the rest of it.

**When the wait cannot end, the row says so.** Two things used to make a
checklist wait for ever in silence, and both looked identical to "not merged
yet":

- **origin has no such branch.** `git fetch origin <branch>` only writes
  `refs/remotes/origin/<branch>` when the remote's refspec covers it — and
  MindFlock's own provisioned base clones are cloned narrow, so `origin/main`
  never existed locally and the ancestry test failed against a ref that could not
  be resolved. The fetch now names its destination explicitly, and a branch
  origin genuinely does not have is reported on the row instead of waited on.
- **the PR merged somewhere else.** A repo that PRs into `staging` but ships from
  `main` has work that is genuinely merged and genuinely not live. The row now
  reads *"Its pull request merged into staging, not main"* and points at the
  repo's card.

Both clear themselves the moment the branch shows up. Neither is an error — the
row shows them where you are already looking, under the waiting sentence, because
they are yours to fix rather than something that went wrong.

**Every row says where the work actually is.** Beside the branch and the sha, a
card carries a chip reading **merged into `staging`** — the branch on `origin`
this work most recently reached, tinted when that branch is the one the repo
ships from. It is the fact neither of the other two names: `branch` is where the
work was *pushed* and the live branch is what the checklist is *waiting for*, so
in a repo that ships through a `staging` or `develop` step a change spends most
of its life merged somewhere the row could not say. Hovering gives when it landed
and the trail it took (`staging` on the day it merged, `main` on the day somebody
promoted it). No chip means it has reached nothing but the branch it was pushed
to, which is the ordinary state of a checklist whose PR is still open.

The answer comes from ancestry — every `origin` branch that contains the commit,
ranked by which one it reached most recently, with branches that arrived in the
*same merge* folded together (every branch cut from `main` afterwards contains
that merge, and listing them all would answer with four names when one thing
happened). Where a **squash merge** left no ancestry to find, the branch its pull
request merged into is used instead. It refreshes every few minutes per plan on
one shared fetch per repository, and stops being asked a week after the work last
landed anywhere — the branch a change sits on keeps climbing (merged into
`staging`, promoted into `main` days later) and no branch name marks the top of
that climb, so the stop is a clock rather than the live branch. In a repo that
promotes `staging` into `main` the chip therefore moves on from the tinted
`staging` to a plain `main` once the promotion happens; the tint marks the live
branch, not the end of the trail.

**A checklist nobody has answered follows its repo's live branch**, so
re-pointing a repo from `staging` to `main` re-aims every unanswered checklist
within a minute — including forgetting a merge it had already seen into the old
branch, and pulling one that had gone **due** against the old branch back to
waiting (its due-ness was a fact about a branch you just stopped shipping
from). Once you have answered a step, or a run is in flight or finished, it
keeps the branch its answers were measured against, and the row says so.
Answers you clicked and clicked straight back off don't count as answers.

**What "live" means is per repo**, resolved first-non-empty:
`repository.verify_repo_settings[owner/name].live_branch` → `repository.live_branch`
→ `pr_base_branch` → `base_branch` → `main`. The dialog header names the flock-wide
answer and adds *· some repos differ* when any tracked repo overrides it; each row
carries the branch it was actually measured against. A checkout with **no GitHub
origin** cannot be named on the list (there is no slug to type) — its only opt-in
is its own committed `.mindflock.toml`, which the **Repository without a GitHub
remote?** fold in the add flow hands over as a copyable snippet:

```toml
[workspace]
verify_on_push = true
```

That is a team-wide, committed decision, it is OR'd with the tracked-repo list,
and neither half can switch the other off.

**The panel is Intake's anatomy, in Intake's order**: the intro (the two
moments), the master switch, the watched repositories on **one line**, the
**Checklists** work list, then the **New checklist** fold — using Intake's
classes, because it is the fourth surface of that kind.

**Repositories: one line, edited from their own headings.** Checklists group by
verdict, not by repository, so there is no repository heading in the work to
hang an Edit on (as Intake's tabs do). The repositories get one quiet line
instead — *Watching acme/api Live branch: main · acme/web Live branch: staging* (one line,
truncated with an ellipsis, the full list on hover) — and **Edit repositories**
opens one heading per repository (*Live branch: staging · https://staging…*), each with
the same editor Intake uses on the Verify use: **Use it for** ticks (putting the
same repository on Intake's PR / issue lists in one save), **Branch that ships**,
**Wait after merge (minutes)**, **Where it runs**, **Notes for the checklist
writer**, **Test access** and an arm-then-confirm **Remove** (which keeps the
repository's verify block — your own words — for a re-add). A line under it says
when the repositories need GitHub connected; the editor's toggle reads **Close
editor**. **+ Add repository** (`#vf-repo-add-btn`) sits in a fold at the bottom,
**Watch a GitHub repository for every push**, and opens the add flow with only the
Verify use: the slug, Test access, the GitHub line and the no-remote snippet, then
the Verify fields. It **never opens on its own** (round 5): a fresh Verify shows
the empty state — *Verify writes a checklist for each branch your sessions
change, then checks it. Nothing to check yet — it fills in once a session
commits.* with a **New session** button — and once a repository is watched the
second line reads *Watching 1 repository — its next pushed branch appears here.*
The master switch, **Write a checklist on every push**
(`repository.verify_enabled`), shows only once a repository is watched, and
pauses the **automatic** half only: no checklist
written on a push, and nothing new moved into the list. Repositories, checklists
and every recorded answer are kept, and asking for one, running one and
answering a step all keep working — exactly as a forced PR review still runs with
automated review switched off.

**New checklist** has two rows, each doing what it says (the first also takes
a branch no session has been on):

- **Ask the agent for a checklist** — pick a branch (listed as `branch — session`)
  and the agent reads the branch's changes and writes the steps, about three
  minutes, with nothing configured. **Every** session with a branch is listed,
  open or closed: one that can't be picked stays in the list, greyed, saying why
  — *has a checklist*, *on main — nothing to compare* for a session sitting on
  the branch its repo ships from, or *no commits yet* for a branch with nothing on
  it (its `diff_stat` counts no files; the server refuses it too, 409
  `reason: "no_commits"`) — and the default is always one that can be.
  Pressing it for a session that turns out to be covered does not error — the
  toast names the checklist that covers it. The picker's last entry,
  **Another branch…** (the default when there is no session to pick), swaps in
  a repo picker (every repo a checklist, an open session or a closed one has been
  in) and a branch box: for a teammate's branch, say. That checklist is keyed
  `<repo>@<branch>`, read from the repo's own checkout (fetching the branch from
  origin when it isn't local), and written from the diff alone. Sent with
  `empty: true` (`POST /api/test-plans/branch`) the checklist is created
  **empty** instead — no model call, `origin: "manual"`, 201 — and its row
  reads **Empty — add a step** under **Needs you** with **Add a step** as its
  button, never *Couldn't be written* (a rewrite asked of it that fails reads as
  the failure again).
- **Write steps yourself** — pick a branch with no checklist yet and **Start an
  empty checklist** makes one (`{"empty": true}` on the session's write route —
  no agent, and it works on a branch with no commits), or pick an existing
  checklist and **Add a step**; either opens it with its **Add a step** form open
  and the caret in it.

**Closed sessions are listed too**, labelled `(closed)`, alongside
the open ones. A checklist outlives its session everywhere else in this feature —
the plan stores the main repo rather than the worktree, precisely so it can be
run and rewritten months later — and creation was the one half that still
demanded a live session, which made the button useless at the moment people
actually reach for it: after the work is finished and the session has been put
away. The branch has to still exist somewhere in the repo (locally or on
`origin`); merged-and-deleted says so rather than writing a checklist about
nothing. The one thing a closed session cannot supply is the **intent** — the
ticket text lives in the session's seed prompt and the transcript went with the
reclaimed worktree — so a checklist written this way is diff-only, and the
rewrite box is how you aim it.

**Each row is headed by what shipped, not by a branch name.** The model writes a
one-sentence summary alongside the steps — *"Search results can be filtered by
owner"* — and it sits under the session's title. A checklist coming due three
weeks later used to be `sc-1234-fix-filters` over a list of imperatives, leaving
the reader to reconstruct the change from the steps.

**Six groups, one axis, loudest first** (round 4; round 5 split the writer's
failures out). **Failed** (a step failed — the change doesn't work), **Needs
you** (shipped and unchecked, steps of yours still open, or a wait only a
setting can end — *Origin has no branch called production…* with **Edit
repositories** as a link), **In progress** (an agent is writing or checking it,
or the branch hasn't merged and nobody has checked it), **Not written yet**
(grey: the writer failed, timed out, came back empty or stalled — the reason is
the row's sentence, *Not written yet — the agent didn't answer in 3 minutes.*
with **Rewrite the checklist**; a branch with no commits reads *Comes back
automatically when the session commits* with no button, and the due loop
re-writes it once the session has a new commit), **Not verified** (a run
finished with gaps nobody owes — steps never reached, or answered *Can't
check*) and **Verified**. The badge counts the first two, by construction —
never *Not written yet*. Merged-or-not
is never a heading; it is on the row.

**Every row says its verdict first** — a pill before the sentence: **Verified on
branch · by you** / **by the agent** (who recorded the passes), **Verified
live · by …**, **Failed**, **Not written yet** (grey; *Not written* on a pane
chip), **Empty — add a step** (made empty on purpose), **Agent
checking**, **Being written**, **Waiting to ship** (merged, waiting out the
deploy) or **n of m checked** (`planVerdict` in `verify.ts`). Then one
plain-English sentence saying whose turn it is — a failure names its step:
*"Failed: step 2 — POST /webhooks returns 200 (+1 more)."* — and at most one
filled button: only a row asking something of **you** gets the accent button;
every other row's button is a plain one. A failed row also has **Fix in a new
session** beside it (normal weight; a failed step has its own), and while that
session (`fix-<checklist>`) is open the sentence ends *Being fixed in
fix-<checklist>.* with its name as a link. Any row with an agent step nobody
has answered shows **Check now** as a visible secondary button. A partly
checked row carries one counter (the tally), not a pill as well. The landing
chip reads **not merged** or **merged → main · 4m ago** (never "local" — the
branch was pushed). Everything rare is in the row's ⋯ menu, which starts with
**Open the work session**, **View diff**, **What could break** (the session's
Map on its ⚠ Could break layer) and **Open PR ↗** while the session is open; the verify run is **Open the checking run** / **Show the checking run
here**. A finished row says *Every step passed.* (never "it works").

**"Check now" means one thing** — start the agent's steps — on the row, in the ⋯
and in the intro. A checklist with no agent step says **Show steps** instead
(the button only opens the steps), and one whose every step has an answer but
some were *Can't check* reads *"Couldn't check 2 steps — not verified."* with
**Show steps**, never "Answer 2 steps".

**The legend lives in the ⓘ** next to *Does the change work?*, with the two
moments (before / after merge): `you` = a person checks it · `agent` = the agent
does · Pass, Fail and Can't check are all answers, only Pass says it works ·
keys 1 2 3, u undo, n note. Its last line is **Replay the Verify tour**. The
tour (`lib/tours/verify.ts`, five steps) rings the row chip
(`data-tour="verify-chip"`), a step's YOU/AGENT badge (`verify-badges`), its
Pass · Fail · Can't check (`verify-answer`), Check now or Fix (`verify-check`)
and the merge pill (`verify-merge`), scrolling each into view inside the list.
At 420 px a step's receipt, Undo and Fix stack under its answers.

**The verdict outside Verify.** A session's sidebar row and pane head carry the
same verdict as a small chip (`VerdictChip`, class `.vchip` — not `.stagechip`,
which a pane head hides): *✓ Verified*, *✗ Failed*, *3/5 checked*, … Clicking it
opens Verify on that branch. The chip matches a checklist by session title, then
by repo + branch, so a duplicated window shows its sibling's checklist.

**Finding one, and acting on several.** The list grows by one checklist per
session branch per repo, so it has Recently-closed's two controls, reused rather
than re-cut: a **Ctrl+F filter** (every whitespace-separated token has to appear
somewhere in the row — `sitecheck grafana` narrows without anyone remembering
the separator) and a **checkbox per row** with a tri-state select-all that
applies to what the filter is *showing*. The filter searches what the row shows
— ticket, summary, branch, sha — plus the repo path and **what the steps say**,
because a checklist is remembered as "the one about the Grafana collage board"
long after its number has stopped meaning anything.

Selecting rows raises the bulk bar: **Check N now · M can't** (only the ones a run would
actually take — a checklist that is still generating, one an agent is already
working, or one with no agent step in it is counted out in the label rather than
discovered as a wall of 409s) and **Delete selected**, which asks first and lists
what it is about to destroy. Each acts through the same per-plan route the row
does, so every refusal a single press would have earned still applies, and the
bar reports the tally: *"Started 3 of 5"*, with the other two's reasons in an
error card. Shift-click extends a range; a selection the filter is hiding is
counted in the bar so a bulk delete can never quietly reach past the screen.

**Where each checklist has got to, before you open it.** A row's sentence says
whose turn it is; the **tally** beside it says how far along it is, glyph and
words — `✓ 5 passed ✗ 1 failed ● 2 for you ○ 1 not checked` — the same glyphs
and colours the step rows and the roll-up inside the plan use, so the three
renderings cannot drift (and the whole thing is announced as a sentence: *"9
steps: 5 passed, 1 failed, 2 need you"*). The
two groups that are an ask carry the same arithmetic on their **heading** —
*"Needs you · 7 · 3 steps need you"* — because a collapsed group was a
number of checklists and said nothing about how much work was in them, or
whether any of it was red. Triage used to mean expanding all seven.

**A run that died says so on the row.** Starting a run answers before anything
has been provisioned — the worktree, the branch and tmux all happen afterwards —
so a failure arrives minutes later, on a background task. It lands on the plan,
and the row now carries the first sentence of it (*"The verify session couldn't
start."*) after whatever it says next, with the raw git or tmux line kept for
the expanded body where there is room for it. Before that, `fail_run` put the
plan back to unchecked and the row read exactly like one nobody had
pressed Run on. The failure is retracted when a later run reports, when the run
is cancelled, and when the next one starts.

**The sidebar bar says when something shipped broken.** The **Verify** dot goes
red with a `✗N` count when checklists are under **Failed** (a failed step, or one
that couldn't be written). The top bar's badge counts them too, alongside Needs
you; the red pill says how many of those are failures. A failure is an answer,
so it stays until the thing is fixed and re-checked (or the checklist deleted).

**Answering steps.** Expand a row and each step is labelled **you** or **agent**:
`agent` is anything settleable from a shell or the agent's own tools — log
searches, dashboard panels and metric queries included, via its Grafana MCP
tooling — `you` is visual judgement, a real browser, or a service the agent has
no tool for. Three answers, and all three count as *your* answer:

- **Pass** — it did what's expected.
- **Fail** — it didn't.
- **Can't check** — you went and looked and could not get to it (staging is down,
  you have no account on it). Recorded with a reason, and **never** counted as a
  pass: the checklist stops asking you, and it files under **Not verified** with
  the verdict *n of m checked*.

Pressing an answer is idempotent; **Undo** beside it is the way back. An answer
the *agent* recorded shows as `blocked · agent`, which is it saying "a person has
to do this" — that one keeps the row open. You can answer your own steps while an
agent is still running the rest: your answers win, and the plan closes when the
agent's run lands rather than stranding it.

**A checklist reads like a checks panel.** Above the steps is one roll-up line —
`8 steps · 5 passed · 1 failed · 2 need you` — and every step carries its state
as a mark in a fixed column on the left (`✓` passed, `✗` failed, `●` waiting on
you, `–` you couldn't check, `○` not checked yet). The roll-up is *counted from*
the marks rather than computed beside them, so the summary and the column cannot
disagree.

**Answering is a keyboard job.** **Answer N steps** scrolls to the first step
that is yours *and focuses it*; on a focused step, `1`/`p` pass, `2`/`f` fail,
`3`/`b` can't check, `u` undo, `n` note. The legend under each checklist says so,
because none of it is discoverable. Keys typed inside the note box are left
alone.

**Running a checklist costs a real session.** **Check now** (and **Check again**
once one has run) provisions a worktree, checks out the live branch — or, before
merge, the branch's own newest commit — and works every step it can, minutes,
not seconds. The row says the split before you press (`An agent can check 8 of 11;
the rest are yours`), and a checklist whose every step is yours offers **Answer**
instead, because an agent is forbidden from settling one. **The run is watched
inside the checklist**: its terminal streams into the expanded row, read-only, so
the steps it hands back are answerable on the same screen you are watching. ⋯ →
**Open the session in its own window** puts it on the grid instead, for keeping
it while you work elsewhere; closing either does not stop the run.

**A run that cannot start, or cannot finish, ends in words.** Everything about
this step happens after the request has been answered, which is why each of
these used to be silent:

- **A leftover from the last run of the same checklist.** The session's name is
  derived from the plan and the commit, so its tmux session and its branch have
  the *same* name every time — and one orphan (the app killed mid-run, a session
  deleted while detached, a lost record) made every later run die with `tmux
  session already exists` or `already used by worktree`, permanently, for that
  checklist. Both are now cleared before the session is created. **Fix what
  failed** got the same treatment with one deliberate difference: a fix
  session's whole job is to change its tree, so its leftover is reclaimed only
  when it is provably pristine — and when it is not, the press is **refused in
  words naming the directory** ("reopen it from Recently closed to finish or discard
  that work") rather than dying minutes later with a raw git line in the
  notifications bell.
- **Two presses at once.** Run and a per-step Re-check start the same request, so
  two of them for one plan raced through a single session title: one create won,
  the loser's failure was recorded against the winner's plan, and the winner's
  agent was left running with nothing owning it — which is where the orphan
  above comes from. The title is now claimed under the engine's lock, a
  background start only ever clears *its own* record, and the buttons withhold
  themselves while a start is in flight.
- **A repo that has moved.** A checklist stores the main repo so it outlives its
  session; when that path is gone the run — and **Fix what failed**, which had
  it worse, since it would have started an agent to repair code that was not
  there — is refused in words, rather than creating an empty folder and starting
  a real agent in it.
- **An open verify session whose workspace was cleared.** The record outlives the
  worktree, so "re-check one step" answered *workspace no longer exists* for ever;
  the husk is closed and a fresh session takes its place. (Clearing worktrees is
  [Recently closed](#recently-closed)'s **Delete unused worktrees**, which is why
  a checklist has to survive one.)
- **A run that never comes back.** The agent's window dying (a usage limit, a
  killed pane) is noticed within a couple of minutes instead of waiting out the
  two-hour deadline, and either way the plan is released **with the reason on
  it** and a `session.test_plan_gave_up` event — a billed session that ran for
  two hours and reported nothing used to be indistinguishable from a button
  nobody pressed. That event carries the opt-in `verify_run_gave_up`
  notification rule (see [Notifications](#notifications-)), so a run that gives
  up can reach you rather than only the dialog. The abandoned session is then
  closed by the sweep like any other stray.

**What the agent is told about the environment** is now true, which it was not
before. A fresh worktree has nothing running in it, so the run prompt says so and
tells the agent to start the product (this session's own port block, or whatever
the repo's standing instructions name). And *"I could not reach the product"* is
**blocked**, never **fail** — a fail recorded against working code is the one
thing that makes a checklist stop being believed.

**Where "it works" is checked** is the repo's **Where it runs** field in its
editor (Verify → **Edit repositories**). Set it to the deployment your users reach and both the checklist
and the agent working it are aimed at that running system. Leave it blank — the
right answer for a library, a CLI, or anything with no environment to point at —
and the run exercises a fresh checkout of the live branch on this machine, which
is what a green checklist then means.

**Checking before merge is the row's button**, not a menu item. A checklist in
**In progress** that hasn't merged reads *"Not merged yet — check the branch now, or it comes
back here after it reaches main"* with **Check now** beside it (it used to be
⋯ → **Check it early…**, behind a confirm warning that it closed the checklist
for good — and, on a collapsed card, a press that did nothing at all). Running
it, or answering every step by hand, keeps the checklist waiting; it comes back
after merge (see *Two moments* above). A branch that passed is **Verified on
branch** and reads *"Works on the branch — it comes back to be checked again
after merge."*

**A failed step has its next move beside it.** **Fix this…** on the step starts a
session to fix it (the same route as ⋯ → **Fix what failed…**, scoped to that
step); the session is told to reproduce the failure first.

**Finishing a checklist doesn't make it vanish.** Answering the last step re-files
it — usually under **Verified**, far down the list — so a notice stays where it
was for a few seconds: *"✓ add-csv-export moved to Verified."* with **Show**,
which scrolls to it.

**A checklist that couldn't be written can still be used**: add a step to it and
it becomes an ordinary checklist (it leaves **Failed**, and the
generation error goes with it — it no longer describes the checklist).

**Deep link for other surfaces.** `useUi.getState().openDialogFor("verify",
<branch>)` opens Verify filtered to that branch with its checklist expanded and
scrolled into view (a session title works too). When no checklist covers the
branch, a line at the top says so with **Ask the agent for one** (for the session
on that branch), and **New checklist** opens with that session picked.
`verifyDeepLinkPlan` in `verify.ts` is the lookup.

**The checklist is written for what the work was ASKED to do.** The ticket — its
title, description and acceptance criteria — is snapshotted onto the checklist at
push time and is the thing the steps are judged against; the diff supplies the
mechanism (the exact route, flag, button label, field). That ordering is the
whole point: the earlier version treated the diff as the only authority, which
made checklists that described a patch rather than testing a feature. The
snapshot matters as much as the ordering — read live off the session, the ticket
was gone by the time anyone pressed **Rewrite**, so the second draft ran on less
evidence than the first.

**Only the part of the change worth reading reaches the model.** The diff is
selected rather than truncated: files are ranked by how much they actually
changed, generated bundles and lockfiles are dropped by name, and about ten files
are shown properly with the rest listed as omitted. Before this it was the first
24,000 characters of `git diff` — which git emits in *path order*, so on a wide
branch the model read the alphabetically-first handful of files and never saw the
feature at all.

**The session's own conversation is used too** — filtered hard: the most recent
turns only, whole turns dropped if they are too big to be speech, if they carry an
output contract (a previous MindFlock one-shot looks exactly like one), or if they
match a credential shape. Your own turns are read as the sharpest statement of
what mattered; none of it is treated as evidence that anything exists, so a
feature discussed and abandoned cannot become a step. It is snapshotted with the
checklist, so a rewrite sees what the first draft saw. Turn it off flock-wide with
`repository.verify_use_conversation`.

**Every step is an input and an output.** A step names the exact input — the
request and its body, the values typed in, the message sent — and the exact
observable result: a status code and body, a number on screen, a row, a log line.
It never spends itself getting the product running: whoever works the checklist
starts it first, and that is not a check. Where a step needs the system in a
particular state it says so as a *condition* (“on an order that already has
SAVE10 applied”), never as a command to run first. Before this rule, real
checklists opened with `docker build …`, `uv run pytest …` and
“start the service on port 8080” — environment plumbing dressed up as
verification, and a list that asked a person to launch processes rather than to
compare a result.

**Every step is written to stand alone** — it names the endpoint, the payload,
the URL or the account it needs, rather than saying "repeat the above". That is a
rule in the generator's prompt, and it exists because nothing reads a checklist
top to bottom: **Answer N steps** scrolls straight to the first step that is
yours, **Re-check this step** runs one of them on its own months later, and a
step that leaned on a value the agent held while it ran is not recoverable from
the checklist at all. Checklists written before this rule keep their old steps —
**Rewrite the checklist…** re-asks the model about the same diff.

**You can add steps of your own, and fix the ones the model wrote.** ⋯ → **Edit
this step** changes the wording in place; ⋯ → **Let the agent check this** / **I'll
check this one** moves it between lanes. Editing a step makes it yours, so the
next rewrite keeps your wording — and changing what a step *asks* drops any answer
already recorded against it, while changing only who answers keeps every one.
Before this, correcting a single wrong sentence meant rewriting the whole
checklist: a model call, three minutes, and every answer against every step that
changed. The actor toggle is also the only way out of a checklist an agent cannot
run at all — an unrecognised actor is read as `human`, and a run is refused when
every step is a person's.

**Rewriting asks for a second draft, and lets you say what the first got wrong.**
⋯ → **Rewrite the checklist** opens a box in the row — *"What should it check
instead? — e.g. focus on the coupon flow at checkout, ignore the settings
refactor"*. That note is stored on the checklist, so a later push keeps honouring
it, and it outranks the model's own judgement about which part of the change
matters; it can never change the format the model has to answer in. Steps you
wrote or edited are kept. A rewrite **cannot un-ship a checklist** (one that has
already gone live comes back where it was, and its "it shipped" push is never
re-sent), **cannot destroy one** (a rewrite that fails leaves the steps you had,
in the queue, with the reason shown above them), and is **refused while an agent
is running it**.

## Settings (⚙)

**It reopens where you were.** Settings comes back on the screen you last had open — people usually return to the setting they were on — for as long as the page lives (`lib/settingsLastScreen.ts`); a deep link to a screen still wins, a screen that no longer exists (the other mode, a retired key) falls back to General, and a reload or restart forgets it on purpose.

**Set-and-forget configuration and diagnostics**, from the top bar's
**Settings**. With no screen named (the top bar, the palette's **Open
Settings**) it opens on **Connections** — the status of everything MindFlock
talks to, and the first-hour screen (`DEFAULT_SETTINGS_SCREEN` in
`lib/openSettings.ts`).

**Basic first, advanced on request.** Out of the box the nav lists only the
seven screens a new user needs to point MindFlock at a repo and a tracker,
run agents and talk to them — **General**, **Connections**, **Notifications**,
**Agents**, **Workspace**, **Appearance** and **Voice** — as one flat list
with no headings. **Show
advanced settings** at the foot of the nav (`#settings-advanced-toggle`, with
"*N* more screens" under it) reveals every other screen; **Hide advanced
settings** puts them away again. The choice is the synced pref
`prefs.settings_advanced` (cached in localStorage `mf_settings_advanced`), so it
follows the person to all their devices, like the theme. There is ONE
advanced mechanism: the power-user controls that live on a basic screen sit
in an **Advanced** section (`AdvancedFold`) that basic mode does not render
at all, and advanced mode shows as an open fold — General's plan window
budget and terminal scroll speed, Agents' keep-usage-windows-warm schedule,
Workspace's live branch, fast-track depth and
retryable pre-commit hooks, and Connections' rows beyond
what you use under **More connections** (devices and people, orchestration,
databases, phone push, extensions, system — so basic mode never calls a merely
enabled extension "Connected"; folded even in advanced mode,
`defaultOpen={false}`). Only Notifications' two optional folds — **Phone alerts
(ntfy)** and **More alerts** — opt back into basic mode (`basic`), as closed
folds. A control's long explanation sits behind a small circled-i at the end
of its one-line hint (`InfoTip`, components/InfoTip.tsx): hovering or
focusing it shows the text in a bubble in the app's popover style (a 100 ms
grace lets the pointer cross onto it), Escape or leaving closes it, and on a
phone a tap toggles it and a tap elsewhere closes it. The bubble is drawn on
`<body>` with fixed positioning — flipping above the icon near the bottom of
the window and shifting sideways near an edge, so a scrolling screen never
clips it — and the text also stays in place, visually hidden, as the
button's description. A deep link to an
advanced screen — the palette, a Connections card, an in-screen link,
`openSettings(...)`, a legacy key — still opens it in basic mode: the open
screen is added at the END of the nav under an **Advanced** heading (so the
heading never sits over Workspace or Appearance), and the saved mode does not
change. The rules live in `components/settings/settingsMode.tsx`.

Labels follow the shared glossary; keys never change: **Agents** is key
`coding` (was "Agent CLI"), **Custom agents** is `providers` (was "Agent
providers"), **Engine & updates** is `advanced` (was "Advanced").

In advanced mode the nav is grouped under non-clickable headings
(`.set-nav-group`) so it scans: **General**, **Connections** and
**Notifications** first, then **Agents** (Agents, Custom agents, Accounts,
Local model, Agent orchestration), **Code** (Workspace, IDE), **This device**
(Security, Appearance), **Devices and people** (**Your devices**, **Your
phone**, **Voice**, **People** — the two ways to trust another machine:
yours, and someone else's, with Voice, the only basic screen in the group,
next to the phone it used to live under), **Extensions** (its own group: loaded extensions and any
that failed to load) and **Troubleshooting** (Doctor, System logs, Engine &
updates, and Site traffic in a `--mindflock-dev` shell only). The list is
`SCREENS` in `components/settings/SettingsDialog.tsx`. Every screen key is
stable — Your devices is still `devices`, Your phone `mobile`, People `peer` —
and the new names and the moved controls' names (`your-devices`,
`your-phone`, `people`, `remote-control`, `tailnet-trust`, `settings-sync`,
`mobile-voice`, `speech`, `microphone`, …) route to the right screen too, so deep links, the palette (**Your devices**,
**Your phone**, **People…**), the legacy Intake hand-off and the Connections
cards' **Configure** buttons land where they always did.

A few screens got quieter:

- **General** opens with **Tours** (a **Replay** per tour and the **Show
  hints** switch); the per-session budget, resume when usage comes back,
  revive after a restart and idle sleep wait in its Advanced fold. The
  MindFlock MCP rows (the on/off switch, the scope and the spawn limits) are
  their own screen, **Agents → Agent orchestration** (see
  [Agent teams](#agent-teams-mindflock-mcp)).
- **Advanced → Platform (Windows / WSL only)** shows only on Windows/WSL, or
  where a WSL distro or Windows Terminal command is already set.
- **Extensions** folds *Create an extension* closed.
- **Security**'s turn-off and **Regenerate**, and **Accounts**' **Remove
  anyway**, confirm in an inline row with **Cancel**. They used to be browser
  `confirm()`s, which do nothing in the desktop app.
- **Every save says "✓ Saved" right by the control** — text fields, numbers,
  selects and toggles alike (`savedCue.ts`: the screens pane notes the control
  last changed or left, and the save marks its label for a moment; a toast
  only when no such control is on screen). A number field refuses a value it
  can't take (below 0) with the reason under it — *Enter a number, 0 or more,
  or leave it blank for no limit.* — instead of saving `-2` as "no limit".
- **Search settings…** heads the nav (`#settings-search`, on the command
  palette's own index, `palette/settingsSearch.ts`, synonyms included): the
  matches list under the box as you type, each with the same one-line
  subtitle as the palette's rows ("tour" → **General → Tours** *Replay the
  welcome tour and the menu tours*, "dark" → **Appearance → Mode** *Light or
  dark*); ↑/↓ and Enter (or a click) open the
  screen, scroll to the control (opening a fold around it) and outline it for
  a moment — a control that only exists in advanced mode (General's *Budgets,
  resume and sleep*) turns **Show advanced settings** on first; Escape clears
  the box before it closes Settings.
- **Notifications** opens on **Where alerts go**: In MindFlock (the bell,
  always on), **Desktop notifications** (the only desktop on/off toggle; the
  bell carries no copy) and **Phone alerts** (**Set up** opens the ntfy fold),
  with one plain line when only the bell is on — the reason the switches
  below seem to do nothing. Then **What you're alerted about** in three
  groups — **Needs you** (a question, a session finishing, a session
  stopping, a computer asking to join), **Pull requests**, **Usage & cost** —
  and **More alerts** (team runs, hooks, Verify, zones) folded with an "*N* of
  *M* on" count. Each row's words come from `screens/notifyCopy.ts`, keyed by
  rule id; the server's `title`/`body` are the alert's own templates
  (`{session}`, `{detail}`) and are never shown as a description.
- **General** keeps one plain line per control (no event ids); **Reduce
  motion**, **Take a break** and **Idle flock** moved to **Appearance →
  Comfort**.
- **Agents** shows the default agent's own Connections row (status, Test,
  Sign in) so the two screens give one answer. The picker greys out every
  agent that isn't installed, and right under it **Not installed — how to
  install ▸** (*Set up another agent* once everything is installed) unfolds
  the chosen agent's card with its install command — no longer inside the
  Advanced fold. Turning on a skip-permissions flag shows *⚠ Skip permission
  prompts: the agent runs commands and edits files without asking you*
  (`SKIP_PERMISSIONS_LABEL` / `SKIP_PERMISSIONS_TIP`, exported for the flag
  chips). Its flags field suggests a
  harmless per-agent example (never a permission-skipping flag), and
  **Sessions running at once** says it plainly: no limit on sessions you
  start, **Ticket sessions at once** (`engine.max_sessions`, the same field
  as Intake → Queue) for auto-started ticket sessions, and a link to Agent
  orchestration's spawn limits for sessions an agent starts.
- **Connections** leads with **What you use** — the default agent, GitHub
  and the ticket tracker — and its header count covers exactly those rows in
  both modes. Every other agent CLI on the machine is folded under **Other
  agents on this computer** with neutral pills (**Not set up**, **Not
  installed**, **Installed** when its sign-in can't be checked), never the
  warning gold. Agent rows read "Agent · Claude Code" with no file paths;
  GitHub's row is a plain status ("Not connected. Agents can't open pull
  requests yet." / "Connected as @you"), with no `gh` output or Copy chip and
  no Test until something is connected. A failing Test outranks an agent
  row's stored Connected.
- **Connect GitHub** never shows "device flow is not configured": without a
  device flow the token path leads (the `repo` and `read:org` scopes named,
  the link opening GitHub's token page with both ticked), with the gh CLI
  sign-in as a second button when gh is installed. The token path is three
  numbered steps — **1** make a token (**Open GitHub ↗**), **2** paste it,
  **3** **Save & test** (disabled with *Paste a token first.* beside it) — and
  the answer appears right under step 3: *✓ Connected as @you* (then the Test
  result) or *✗ Token rejected — GitHub says it is mistyped, expired or
  revoked*. **Test** appears once connected (before that, step 3 is the test);
  **Check I can push** is always available — it asks the repository's remote
  with git and needs no token. Connected, the steps fold under **Use a
  different token**.
- **Workspace** answers *where do my worktrees live?* in one place (**Where
  your worktrees live**, `GET /api/settings/workspace-paths`): the absolute
  folder for **Sessions you start** (`<config dir>/worktrees`) and for
  **Ticket and PR sessions** (the **Workspace folder** field — a relative one
  anchored where the ticket pipeline runs, `~` expanded and stored expanded;
  **📁 Browse…** beside it opens the folder picker window and saves the pick),
  each with **Copy**. The folder is checked as you type: a missing one *will
  be created*, an unwritable one or a file is flagged in red under the field,
  and `MINDFLOCK_WORKSPACE_DIR` is named when it overrides the field. **How
  ticket sessions copy the repo** (worktree or full clone, `engine.mode`)
  moved here from Engine & updates.

**Spacing.** Every screen runs on one rhythm (`--set-rhythm`, 24px, on
`#settings-panel`): a field's label, control and hint sit 6px apart, the next
field starts 24px below, so a hint always reads as part of the field above it.
Section titles get 40px above and 16px below; cards and inline button rows
use the same 4/8/12/16/24 scale. In a window narrower than 600px the nav
becomes a short stack above the screen — the search box, one `<select>` of
the screens (`#settings-nav-select`, the same list as the buttons) and the
advanced switch on its own line — instead of a wrap of tabs that re-flowed
whenever the bold active tab changed width; the ntfy fields stack their
labels above the inputs.

- **Cursor auto-adopt** — adopt Cursor-opened workspaces as sessions.
- **Per-session budget (USD, 0 = off)** (General) — cost guardrail: when a session's
  estimated cost crosses this figure the server emits a one-shot
  `session.budget_exceeded` event → warning toast (click focuses the session),
  a notification on every enabled channel (desktop and/or ntfy — see
  [Notifications](#notifications-)), and shell hooks. An over-budget session's
  pane shows a lock overlay with a **Raise budget** action
  (`POST /api/instances/{title}/budget/raise`); sends are refused (409) until
  raised.
- **Connections** — the dashboard of every outside thing MindFlock talks to
  (`GET /api/connections`): one row each, grouped by kind — agent CLIs (one
  per installed or referenced provider, the default marked), GitHub, each
  ticket source, Your devices, Your phone, People, MCP, databases, ntfy and
  extensions (a failed load is its own row). Every row is **Connected**,
  **Needs attention** or **Off**, with one action button named by the server
  (**Connect**, **Sign in**, **Manage**, **Copy** the install command…) and a
  **Test** whose result shows inline on the row. The header reads *N connected
  · M need attention · K off*, rows that need you come first, and the screen
  refreshes every 30 s and the moment a dialog reports a change (Connect
  GitHub, Sign in). A ticket source is **Connected** only when its last test
  passed within a day with nothing left to fill in; **Needs attention** when it
  is untested, failed, stale or incomplete; **Off** with no credentials yet —
  the same rule (`connection`, `message`, `tested_at` on the row) as Intake's
  source-card pill. A GitHub Issues source still holding its own token is
  **Needs attention** until it is moved to **Connect**.
- **Accounts** (screen key `accounts`) — the identities sessions can run as
  (`GET/PUT /api/settings/auth-profiles`; full guide in
  [accounts.md](accounts.md)). One card per profile: an `account` kind is a
  second login of the CLI itself, isolated in its own config dir, and its card
  has a **Sign in** button that runs the CLI's own login in an in-app terminal
  scoped to that config dir, with **Copy login command** and the line to type
  yourself (*In a terminal: <command> — or `mindflock accounts login <id>`*)
  as the fallback; `api_key` and `openrouter`
  kinds inject a key at launch, and an OpenRouter card's **Test key**
  (`POST /api/settings/test/openrouter`) reports the key's real spend and lists
  the models it can reach, turning its model field into a picker. Keys read back
  masked, and a save that resends the mask keeps the stored value. One profile
  can be made the app-wide default for new sessions.

  Two surfaces elsewhere depend on this screen, and both stay invisible until a
  profile exists: the **New session** dialog grows an **Account** select (plus a
  **Model** select for key/gateway accounts) and steers the Agent picker to a
  CLI the chosen account can actually route; and each pane header grows an
  **`@account` chip** that hot-swaps a *live* session
  (`POST /api/instances/{title}/profile`) — the agent restarts under the new
  identity while the worktree, diff and shell pane survive. The chip rides the
  same right-hand chip group as the cost counter, and sheds with it below a
  560px pane. A swap starts a fresh conversation; the thread does not follow the
  session across identities.
- **Local model** (screen key `localmodel`) — run sessions against a model served
  on this machine: an on/off switch, the server (**Ollama** / **LM Studio** /
  any OpenAI-compatible), its base URL (blank = that runtime's default) and the
  model. **Test connection** (`POST /api/settings/test/local-model`) does three
  things at once: confirms the server answers, lists the models it actually serves
  — which turns the model field from "type the exact tag" into a dropdown — and
  names which installed CLIs can be pointed at it. It also states plainly that
  Claude Code has no local route, because a session silently using its hosted API
  is the one outcome the privacy claim cannot afford to be quiet about.
- **Advanced → Version & updates** — one section with two transports, picked by
  which one can actually do the work. Inside the Electron shell `window.mfengine`
  owns it (`engine:update-info` / `engine:install`): the shell can install while
  nothing of *its* own is being replaced. There the section also carries
  **Install updates automatically** (on by default): releases install
  themselves once you've been away 10 minutes (or locked the screen) and no
  agent is mid-turn, honouring "Skip this version" — see
  [electron/README.md](../electron/README.md#automatic-updates). A shell
  **running from source** (the dev shell, or any unpackaged run) shows no
  shell updater at all: a one-line *running from source — update the checkout
  with git* note under the engine's own view. In any other browser — one on the
  tailnet, a second machine — the shell object is absent and the **server
  updates itself** over `/api/update/*` (see
  [web-api.md](web-api.md#engine-updates)). The version line reads *This engine
  is **vX** · newest release **vY** (release notes)*, or *· couldn't reach
  GitHub to check for a newer one* when the lookup failed — never "you're on the
  newest release", which is a different claim. A **dev/editable checkout** shows
  the exact git commands instead of a button (fetch and check out the release,
  then restart), with the install method named on the version line:
  that is a **refusal, not a failure** — reinstalling over an editable install
  would swap a contributor's working tree for a release build. Otherwise
  **Update to vY** starts the install, **Installer output** folds open a live log
  tail, and the screen polls `/api/update/state` until the install is done. The
  server restarts itself onto it (no tab needed — closing this one doesn't stop
  it), so the screen shows *Installed — restarting…* — also on a later visit
  that finds an install the server hasn't picked up yet, never a second
  **Update** offer — and reloads onto the new bundle once the new build
  answers. Your sessions are tmux sessions, so nothing running is lost. When
  the last update didn't end well the screen says how: *interrupted — try
  again* (its installer died), *failed* (System logs has the output), or *vY
  didn't start, so vX was put back* (the installer's health check rolled it
  back). If the server doesn't come back within 3 minutes the screen says so
  and points at System logs / `mindflock restart` rather than spinning.
- **Update notices.** When a newer release is out and this device — or
  another of "Your devices" — runs an older one, the bell gets one **Updates**
  row per release ("MindFlock vY is out — 2 of your devices are behind"; the
  click opens Settings → Your devices when other devices are behind, else
  Advanced), a browser tab gets one toast (the desktop app has its own), and
  `/m` shows a one-line banner with **Update** / **Update all**. Settings → Your
  devices offers **Update all my devices to vY** and follows the rollout one
  line per device.
- **Advanced → Engine → Ticket sessions in MindFlock** (`engine.enabled`,
  **default on**) — where ingested tickets land. On: each one becomes a MindFlock
  session with its own worktree, branch, seeded agent, stage badge and guided git
  bar. Off: a detached tmux session plus an OS terminal tab, with no session in
  the app. Takes effect the next time the ingestion pipeline starts; it is the
  same switch as `[mindflock].enabled` in `config.toml` and overrides that file
  (see [configuration.md](configuration.md)).
- **Agents → Agent orchestration → Give agents the MindFlock
  MCP** (`general.agent_mcp`, **default on**) — launches each Claude Code and Codex session with the MindFlock MCP
  attached (see [Agent teams](#agent-teams-mindflock-mcp)). Applies on each
  session's next launch; a running agent keeps what it started with. When the
  server runs with `MINDFLOCK_AGENT_MCP=0` the row says the switch is
  overridden. Beneath it, **Agent MCP scope** (`general.agent_mcp_scope`):
  *Default (children)*, *Children* (manage only the sessions it spawned),
  *Read-only* (look and check its inbox, no messaging) or *All* (manage any
  session). It limits answering, killing and re-parenting other sessions;
  reading and messaging are open in every scope but read-only. A guard-rail,
  not a security boundary.
- **General → Resume sessions when usage comes back**
  (`general.resume_on_usage_reset`, **default on**) — whether a session parked
  on its CLI's usage-limit screen with an empty queue is told to `continue` once
  the provider's window reopens (see
  [Send a message / prompt queue](#send-a-message--prompt-queue--per-pane) for
  the gate it shares with `wait_for_limit`). Off leaves it parked; the "Usage comes back"
  notification fires either way.
- **General → Revive sessions after a restart** (`general.revive_on_start`,
  **default on**) — whether the server relaunches, in the background after it
  boots, every session whose agent was running when it went down (see
  [One session state](#one-session-state)), and an agent the system killed
  for memory once memory recovers. Off leaves them **Offline** until you press
  their **↻ Restart**.
- **General → Put idle agents to sleep after** (`general.sleep_idle_minutes`,
  **45** when empty, **0 = never**) — how long an agent must have sat idle,
  its turn over, unwatched and with nothing waiting for it, before its CLI and
  MCP servers are stopped to free their memory (the row then reads
  **Sleeping**; opening it or sending it anything wakes it). While memory is
  low, 5 minutes (never longer than the value set). Per device: never synced.
- **General → Take a break** (`mf_break_on` / `mf_break_every`, **off by
  default**) — one switch whose whole description is the sentence it configures:
  *Reminder to take a break every `[N]` minutes*, with N editable in place
  (5–480, default 60). When it fires, a card asks you to get up, with the
  murmuration from mindflock.ai flying over your grid.
  **Snooze 5 min** pushes it back five minutes; **Resume Working** (or
  Escape) restarts the whole interval. The clock is wall-clock and survives a
  reload — a refresh is not a way to dodge a break — but changing the interval
  re-arms it from now.

  **It only runs while the app does.** A deadline on its own keeps counting
  through a closed window, a shut-down machine and a slept laptop, so whoever
  came back in the morning was met by a card claiming they had been at the desk
  all night, its away-clock still climbing. They had not been: time away from
  the app is time away from the desk, which is the break itself. So the app
  stamps a heartbeat while it runs (`mf_break_seen`, every 15s), and a gap in it
  of more than **five minutes** starts the interval over instead of resuming it
  — on opening, and also mid-run, which is the only thing that notices a machine
  that slept with the window open (and takes the stale card down with it).

  Opening the app is itself a fresh start: the countdown begins when you sit
  down, whatever yesterday left behind. That is not a hole in the rule above,
  because a *refresh* is told apart from an *open* by navigation type rather
  than by guesswork — F5, the palette's Reload, the reload every tab does when
  the server restarts, and the API client's reload on a lost session are all
  `reload` and all keep the deadline; a shell launch or a new tab is `navigate`.
  The five-minute tolerance is wide on purpose: a hidden tab's timer is
  throttled to about one tick a minute, and that must not read as an absence.

  **There is no scrim**: your grid, sidebar and panes stay
  exactly as you left them and the flock flies over them, the same treatment the
  idle overlay gets; the card is the only new thing on screen. The overlay still
  swallows the pointer and the keymap stands down with it, so you can watch your
  agents work but can't join in until you answer — a break rather than a
  suggestion. Sessions keep running throughout, and the card hands the keyboard
  back to whatever had it. The flock is a round trip through the logo, and a
  lopsided one. It streams OUT of the MindFlock mark in the top bar over
  **twenty seconds** — birds leaving one at a time, each flying its own short
  arc and joining the live flock the moment it lands, so the window keeps
  filling for as long as you stay away from it — and folds back INTO the mark in
  under a second when you dismiss it. Dismissing mid-stream takes over from the
  emergence rather than queueing behind it. The idle overlay arrives and leaves
  the same way; birds that simply materialise everywhere read as a bug, birds
  that come out of the logo read as the app's own.
- **General → Idle flock** (`mf_idle_flock` / `mf_idle_after`, **on by
  default**) — **The idle flock** is what MindFlock does when nobody is
  looking. The row is the same sentence-with-a-field shape as the break row
  above: *Fly the flock over your grid after `[N]` minutes with no click,
  keystroke or scroll in this window*, with N editable in place (1–480,
  **default 10**). Unlike the break card this one is on out of the box,
  because it can only appear once you have already walked away from the
  window. The flock flies across the whole
  screen, ignoring the sidebar and pane boundaries entirely. It is
  `pointer-events: none`, so nothing is covered or blocked, and the first click
  or keystroke sends it home. Two things stand it down: the switch, and the
  break card, which brings its own flock. Turning the switch off mid-flight
  sends the birds home through the logo rather than cutting them off. Three
  things deliberately do not count as you being present:
  - **Moving the mouse.** `pointermove` is not in the activity list at all, so a
    cursor drifting over the window — or parked on it while you read something
    else — neither delays the flock nor dismisses it.
  - **Agent output.** That is the machine working, not you.
  - **Anything you do in another app.** Neither `focus` nor `visibilitychange`
    resets the clock, so MindFlock idles behind you while you work elsewhere — a
    second monitor fills with birds, which is the whole point — and the click or
    keystroke that brings you back is what wakes it.

  Under `prefers-reduced-motion` both surfaces show a settled flock rather than a
  moving one.
- **General → Help** (the bottom of the screen, after the Advanced fold;
  still `#general-tours`) — under *Tours*, one **Replay** per tour
  (Welcome · New · Intake · Verify · Schedule · Settings) and the master
  **Show hints** switch, which also stops tours opening by themselves (see
  [Tours](#tours)). The per-session budget, **Resume sessions when usage comes
  back**, **Revive sessions after a restart** and **Put idle agents to sleep
  after** sit in General's Advanced fold (advanced mode) with the plan window
  budget and scroll speed; their keys are unchanged.
- **Agent CLI → scheduled window refresh** — a keepalive that periodically
  pokes each provider's CLI so usage windows stay warm
  (`GET/POST /api/window-refresh`). Its **Default provider** picker reads
  `GET /api/providers/status` and lists **every** provider, the absent ones
  disabled as *<name> — not installed* (*A CLI that isn't installed can't be
  picked — install or sign in to it under Agent providers.*) — you can't make
  a CLI that isn't there the launch default; if the stored default is missing it
  falls back to the first installed CLI and **persists the correction** (the
  backend rejects the save otherwise — see [web-api.md](web-api.md)). Under
  the picker is the default agent's card; the Advanced fold's **Set up another
  agent** picker (product names) shows any other agent's card.
- **Custom agents** — one row per CLI, built-in or custom
  (`GET /api/providers/status`), named by its product name: its agent card
  (status + Install / Sign in / Test — the sign-in runs the CLI's own login
  in an in-app terminal, so MindFlock still never sees your credentials; see
  [providers.md](providers.md)) and *Runs <path>* where it was found. Every
  row has a **Binary path** field (*This device
  only — never synced to your other devices. Leave empty to use PATH.*;
  `PUT /api/providers/<name>/binary-path`) for a claude or codex that lives
  off PATH; one pinned by the server's environment says so and wins. A custom
  TOML that fails to load is a red row with the file name, *failed to load*
  and the parse error. Below the rows is the custom-provider **manager**
  (**+ Add a custom agent**, Edit, Delete): add/edit/delete user-TOML
  providers via the `/api/providers` CRUD, including each provider's saved
  **launch flags** (`[launch] args`). The per-provider **default launch flags**
  (`coding_cli.default_launch_args`) that pre-fill the New-session dialog are
  also edited here.
- **Your devices** (screen key `devices`; was Devices) is the one place for
  your own machines. The **You** card (*You — the same on every device you
  pair.*: name, git author name and email for MindFlock's own commits, the
  person id with Copy — `GET/POST /api/person`, see [people.md](people.md))
  comes LAST, after **Add a device** and Settings sync — you come here to add a
  device, not to fill in a form. It opens with **Tailscale on this device** (`GET
  /api/tailscale/health`; the same list the doctor's tailscale row reads):
  signed in as whom (a tagged device shows its tag), the tailnet, this
  device's name and IP, then each problem with its fix, worst first. Signed
  out or turned off, **Sign in to Tailscale** / **Turn on Tailscale** (`POST
  /api/tailscale/login`) shows Tailscale's sign-in link and a QR and keeps
  checking until the device is connected. A key expiring within 30 days gets
  a warning with the admin-console link and "Disable key expiry" (tagging in
  the console after sign-in keeps the expiry). On a Mac the Tailscale app's
  built-in CLI counts as installed; on WSL with Tailscale only on Windows the
  card explains why that isn't reachable and gives the tested path (a second
  Tailscale inside WSL) and the alternative (mirrored networking). Your phone
  shows the same card when Tailscale is missing.

  Below it, **This device** says where MindFlock listens ("listens on your
  tailnet (100.x:8765) · access gate on"). A device bound to 127.0.0.1 can
  still make a code nobody can use and still join, then sit "offline" on
  every other screen — so then the line turns into a warning ("Your other
  devices can't reach this one") with **Make reachable**: one save that turns
  Tailscale mode AND the access gate on together (never a tailnet bind with
  the gate off — that would open the machine to the LAN too), after trading
  this browser's token for its sign-in cookie so it stays signed in, and then
  waits out the server's own restart. The same fix appears where it matters:
  **Add a device** is blocked inline on a local-only device (no code to type
  into a computer that can't reach this one), the toast after joining says
  "Joined X — but your other devices can't reach this one yet" and its click
  opens the confirm, and on the device that let someone in, a joiner it can't
  reach after the join reads "Joined, but rig isn't reachable from here —
  connection refused on :8765 … On rig: Settings → Your devices → Make reachable".
  The **Make reachable** switch here is the one Tailscale-mode control in the
  app (Your phone and Security point at it).

  When the other members do something this one doesn't, **Match my other
  devices** offers one confirm ("reachable on Tailscale + phone link
  “mindflock”"): it saves the same reachable settings plus
  `general.shared_link`, then lists any phone-link checklist step still
  failing here (tag, operator — see One link for all devices below). The **phone link** row shows
  who answers the shared link across your devices ("hosted by mac-mini ✓,
  rig ✓ · laptop ⚠ awaiting approval") with **Host here** for this one.

  "No other MindFlock found" is gone: each tailnet device that isn't a
  member or a joinable MindFlock gets its own row with what discovery found —
  "connection refused on :8765 → MindFlock isn't running there, or it's
  local-only", "timed out on :8765 → your Tailscale policy may block
  tcp:8765" (with **Copy grant**: the policy lines that open it to you
  only — the same owner-scoped grants as One link for all devices' policy block),
  "asleep —
  Tailscale last saw it 2 h ago" — tagged devices first; member rows use the
  same words instead of "offline".

  A join request shows here whichever of your devices it asked: the asked
  device copies it to every member, so **Approve** works from any of them
  (the answer goes back to the asked one under the devices' key, carrying the
  6-digit code shown — "It asked rig; approving here answers there"), from the
  bell's own **Approve** on the request's row, and from the phone's approve
  card on `/m` (an ntfy tap opens `/m#approve=<id>`). The desktop app also
  raises an OS notification for a join request, someone arriving on a peer
  link, a joint task proposed to you, a mirror that failed, someone unlinking
  you (*your shared folder is kept at …*) and an update while its window isn't
  focused; clicking one brings the
  window forward on the right screen.

  **Paste a code** is ONE box for every code: one of your devices' codes
  (bare, `<device> <code>`, or the whole `mindflock devices join` command)
  joins here; someone's `mfp1:`/`mfp2:` invite is joined as a peer link and
  People opens on it, with the safety number to compare. The
  palette's **Paste a code…** focuses it, and a code typed straight into the
  palette is offered as "Join with code …" at the top (below the commands
  instead when a command's own name contains what was typed — "run template"
  also reads as a device name and a code).

  Under each of your devices, a read-only line says whether it is **ready to
  work** or what's missing there (*missing tmux · codex not signed in · can't
  push · Tailscale key expires in 4d*) — each member's own summary of itself
  (`GET /api/fleet/readiness`); hover for the fixes, which always run on that
  device. Beside a live **Add a device** code, **New computer with nothing
  installed yet?** shows one copyable line for a brand-new Mac, Linux box or
  WSL distro: it installs MindFlock at **this** device's version
  (`MINDFLOCK_INSTALL_REF=v<version>`), signs in to Tailscale if needed, and
  makes itself reachable (Tailscale mode + the access gate, as **Make reachable** does), and joins with the code — asking for approval instead if setting up took
  longer than the code lives. Desktop users get the app's download link and
  the `<device> <code>` to paste into **Paste a code**. When settings
  sync is holding a synced default agent back because its CLI isn't installed
  here (*Not applied here*), an **Install …** button runs the doctor's install
  terminal for it, then syncs so the setting applies.
- **Your devices → Advanced — other ways to trust a machine** — a fold at
  the bottom holding the older routes, each with one line on when you'd still
  use it: **Trusted Tailscale accounts** (moved from Security — for a phone or
  laptop that doesn't run MindFlock), **Allow remote control** (moved from
  Security — for a MindFlock too old to join your devices, or one you don't
  own) and **Pair by pasted token** (the sidebar's old Connect… dialog, now an
  inline form — for someone else's MindFlock on your tailnet, or one too old
  to join). The sidebar's **Connect…** opens Your devices with this fold open
  and that device preselected.
- **Your devices → Settings sync** (`GET/POST /api/settings/sync`,
  `backend/web/core/settings_sync.py`; it used to sit under Security) —
  keeps the settings that belong to *you* the same on every one of your
  devices: ticket sources, GitHub repos and options, notifications, agent
  limits, budgets, trusted Tailscale accounts, the accent, your keymap,
  prompt presets, theme and other UI preferences, session templates, red
  zones and your own agent CLIs. Turn it on **from this device** (its values
  are stamped now and lead) or **by adopting** another member's values first;
  change one anywhere and the others follow within seconds, latest change
  wins per field (per ticket source or preset), and deletes travel too. One
  row per member shows when it last synced and anything it holds back; a
  synced default agent whose CLI isn't installed here reads *Not applied
  here* with an **Install …** button. A setting whose value must differ per
  device (a ticket source whose id means something else on the device you
  joined) is **pinned** and kept separate. When one scan would clear many
  settings at once — a replaced or restored `settings.json`, not a change
  made through MindFlock — sync **pauses** here and asks *theirs or mine?*
  rather than spreading a reset (`settings.sync_paused` fires once). It only
  ever talks to your devices; another MindFlock on the tailnet is never a
  source. Full field list: the `/api/settings/sync*` rows in
  [web-api.md → Authentication](web-api.md#authentication).
- **Shared with you** (rail, `sidebar/PeerSharedRows.tsx`): above the session
  list, one row per connected peer whose end reports a shared session
  (`their_share` on `GET /api/peer`, polled every 10 s). It opens the
  **shared-session viewer** (`PeerSessionViewer`, dialog `peer-session`):
  **Session** (their terminal, re-asked every 1.5 s, colours only —
  `lib/ansiSpans` renders spans, never HTML), **Files** (their folder; click to
  read), **Changes** (their diff), and a line that messages them. Until the
  safety number is confirmed (`sas_verified`) the three tabs are off and one
  line says why (*Confirm the safety number with <name> in People first — then
  their screen, files and changes show here*, with **Open People**); a refusal
  by their side is the server's own sentence (*<name> hasn't confirmed the
  safety number yet…*, or the switch they turned off: *<name> turned off “see
  my changes” on this link…*).
- **People** (screen key `peer`; was People; the Peer links screen, `/api/peer*`,
  `backend/peer`) — pair-coding with another MindFlock user. It leads with
  **Invite someone** (**Copy invite** copies a ready-to-send message with the
  `mfp1:`/`mfp2:` code) and **Join someone** (takes that whole message or just
  the code; Your devices' **Paste a code** box routes a peer code here too); doing
  either turns peer links on, no toggle first. The screen opens with one line
  (*one of you invites, the other joins*) and **Create invite** as the primary
  button; **Join someone** takes the paste (a cut-off code is caught before it
  is sent; a just-joined card scrolls into view and lights up). Each
  connection's card is three groups, the next step first — **Trust** leads
  until the safety number is confirmed, then **Share · Talk · Trust**:
  **Trust** is the **safety number** (*the same number on both screens means
  nobody is in between*) with **It matches** and **It doesn't match** (removes
  the connection and says to make a new invite and send it another way); a
  new number after a reconnect is called out (*New safety number after
  reconnecting — confirm it with <name> again*: an unconfirmed number the
  server announced with `peer.state` `sas_changed`, or one different from the
  number this browser last saw confirmed); *In the folder you share, they may* (✓ Saved on each change);
  and **Remove connection…** in red. **Share** holds the labelled share form
  (Folder · Branch · Agent; *Confirm the safety number before you share* and
  Share stays off until then), **Bring work home** (Your repo · As branch ·
  Export, its result next to the button: *Brought home as peer/Bea in api (3
  commits)*, *Nothing new to bring home yet*, or the server's one sentence why
  not — never *export failed: export failed:*), the joint task and the Mirror
  row. Share, Export, Mirror's **Enable** and **Reopen** stay off until **It
  matches**, saying *Confirm the safety number with <name> first* (the
  server's 409 says the same). A shared folder whose session was closed —
  peer links turned off, or Close — keeps its folder (`shared` and not
  `share_open`): the card says *Your shared session is closed — its folder is
  kept* with **Reopen** (`POST …/share/reopen`, the agent picker beside it; a
  refusal shows next to it). **Talk** is the conversation by person (*Bea*,
  *you*, *your agent* — never a session id) with its send box, **Watch their
  session** and **Their changes**; the shared-session viewer shows the same
  thread above its send box, and every message from them is a bell row (filed
  under **People**, its first line from `text_head`). The bell also says when
  they unlink you (*<name> unlinked — your shared folder is kept at <path>*,
  or just *<name> unlinked*) and when a re-pair brings a new number (*New
  safety number after reconnecting — confirm it with <name> again*). **Remove
  connection…** says *Their shared session is closed, not deleted; yours too
  unless you tick delete*. Turning **Connections to people** off in Advanced
  asks first when anyone would be disconnected (*Disconnect from Bea and Cy
  and pause 1 shared session?*, counting open shared sessions), then calls
  `POST /api/peer/disable`; the cards stay listed, greyed and inert, under
  *Connections to people are off* with **Turn on**. Advanced's status line
  says where the listener really is (`listen.bound`). In detail: **Share a folder** (any folder — typed or picked
  with **Browse…**, the New dialog's folder browser; a plain folder is shared
  as a snapshot — optional branch, the agent CLI — any sandbox-capable one;
  not aider) which starts the shared
  session `peer-<name>-<id>` in a sandboxed shallow clone under
  `~/.mindflock/peer/shares/`, **Export** (brings the shared work into your
  own repo as a `peer/…` branch; **Browse…** picks the repo), **Watch their
  session** (the shared-session viewer above), **Reconnect** (offline joiner
  only: *it usually comes back by itself; if not, paste a fresh invite*) and
  **Remove connection**.

  **Joint task** (one per connection; a done or cancelled one stays listed
  until the next proposal replaces it; `settings/screens/peer/JointTask.tsx`,
  `/api/peer/links/{id}/joint*`): two people, two repos, one feature. Once the
  safety number is checked and they're online, **Start a joint task** opens a
  form — what you're building together, your half (folder via **Browse…**,
  optional branch, agent) and a **starter contract** prefilled with four
  headings (*What we're building / Interfaces between the halves / Who owns
  what / Done means*) — and **Propose to <name>**. Your card then reads
  *Proposed to <name> — waiting for them to accept* (**Withdraw**); theirs
  shows the brief and the contract with **Accept with…** (their folder,
  branch, agent — their half starts at once) or **Decline**. When the other
  side is offline, a change says it *will sync when they reconnect*; if your
  half's session fails to start, its place says why. An active task
  shows the brief, both halves (**Open session** for yours, **Watch their
  session** for theirs), the **Contract** panel (rendered Markdown — React
  elements, never HTML — with its rev and who changed it; **Edit** saves a new
  rev to both sides and both folders' `CONTRACT.md`; each open proposal from
  an agent shows as the lines it adds and removes, the whole text folded, with
  **Accept** / **Reject** — agents never accept), the **Mirror** row
  (*Mirror their work into [repo] as peer/<name>* → **Enable**; on, it says
  when it last updated and **Disable**; a failed fetch shows *Last mirror
  failed: …*), the joint checklist when there is one (its verdict and each
  side's steps), **Done** and **Cancel…**. A done or cancelled task says so
  and offers **Start another joint task**. The Mirror row also shows on a link
  without an active task once they share or a mirror exists. The bell and a
  toast report a proposal (*<name> proposes a joint task — accept in People*),
  their accept / cancel, their agent's open contract proposal (your own
  agent's is a toast only: *review it in People*), and each mirror update or
  failure; a click opens People. *Advanced* holds every setting
  (`peer.*` in [configuration.md](configuration.md#peer-links-peer-settings-store-only):
  relay `auto` / `off` / `cloudflare` / `url`, listen and advertise
  addresses, display name, egress allow-list) and the off switch. The
  doctor checks the sandbox (bubblewrap on Linux, the built-in
  `sandbox-exec` on macOS) and can install `cloudflared`. Shared folders run
  on Linux and macOS. Design and protocol in [peer-link.md](peer-link.md).
- **Your phone** (screen key `mobile`; was Mobile) — getting this MindFlock
  onto your phone, as four rows, each with its own live ✓/✗: **1 Tailscale on
  this computer** (`GET /api/tailscale/health`: ✓ when running; installed but
  stopped says *Tailscale is installed but not running. Start it:* with the
  one command in a Copy box; not installed or signed out shows the Tailscale
  card with its install guidance and **Sign in**), **2 Tailscale on your
  phone** (*as <login>* when known, with *This QR gets the app*), **3 Make
  MindFlock reachable from your phone** (✗ while it only listens on this
  computer, with **Turn on Make reachable…**), and **4 Scan to open
  MindFlock** — the QR and the `/m` URLs (`GET /api/mobile`; under a list
  with a plain `http://` link: *Voice needs an https link: a phone browser
  gives an http:// page no microphone* — neither recognizer can hear on
  it, and the phone page says so itself), drawn ONLY once
  1 and 3 are ✓, so there is never a QR that opens a URL that hangs. No
  backticked shell in the prose: Tailscale issue lines drop the clause that
  quotes their own fix (`plainIssueMessage`). Nothing here is a knob. **Turn
  on Make reachable…** opens Your devices, where **Make reachable**
  is the one Tailscale-mode switch (it takes the restart the bind needs
  itself — `POST /api/settings` answers `{"restarting": true}`, the screen
  waits for the server to come back — and gives up after three attempts,
  leaving **Restart server to apply**, rather than restart-looping a server
  that isn't going to come up on the tailnet; the same check runs at every
  boot, so a server started in local mode while the setting says tailscale
  corrects itself). It shows no token: scanning signs the phone in, and
  typing the link by hand asks for the access token from Settings →
  Security. The tailnet URL here is also the natural paste target for
  *Tapping opens* on the [Notifications](#notifications-) screen — though pushes
  now carry it automatically, so that field is only needed to send taps
  somewhere else. One wrinkle is worth knowing before you blame the link: the
  URL is offered
  with `?token=…` baked in so a scan lands signed in, and MindFlock **strips that
  token** when saving it as an ntfy click URL (it would otherwise be stored on the
  ntfy server). A tap therefore only lands signed in if that device already holds
  the `mf_auth` cookie — i.e. you scanned the QR there once before. Otherwise it
  lands on the login prompt, which is the intended trade: one extra tap on a new
  device instead of this machine's token sitting on a third party's server.

  Below the four rows one line, `#mobile-voice`, says *Voice settings moved
  to Settings → Voice* and links there (the old anchor still lands on it).

- **Voice** (screen key `voice`; also `mobile-voice`, `speech`,
  `microphone`; `components/settings/screens/Voice.tsx`) — talking to
  sessions out loud, from the desktop app's mic in the top bar and from the
  phone page. A **basic** screen, right after Your phone, so a fresh user
  finds it. Its settings are `voice.*`
  ([configuration.md](configuration.md#web-exposed-settings)); the screen
  re-reads `GET /api/voice` after every save. It is laid out like every
  other screen, from the shared Settings pieces only: one uppercase
  `.set-section-title` per concern, each field a stacked `.set-row` (label,
  control, hint under it — the same selects and inputs as General), the
  switch a standard `.set-switch-row`, and the installer and the Try-it
  buttons General's action rows (label and hint on the left, the button on
  the right, progress and results in the hint). Status lines carry Your
  phone's ✓ / ✗ glyph. `voice.css` keeps only the installer's progress bar
  and its cut-to-one-line log. In order:
  - The intro says what runs where: the desktop app's mic always uses this
    computer's engines, the phone page uses the phone's own unless told
    otherwise, and out of the box this computer uses its **built-in engine**
    (Windows' System.Speech — also from WSL — or macOS' Speech framework),
    so a fresh install talks with nothing to download.
  - **Recognition** and **Voice** (`#voice-engines`, where the mic and the
    phone page deep-link; two titled sections): each an **Engine** picker — *This computer (built
    in)* · *Your own model server (OpenAI-compatible)* · *Local models
    (faster-whisper / piper)*, built in first; a **Bring your own models**
    note above the pickers says that any server speaking the OpenAI shape
    (Voice Lab's cloned voices, Kokoro-FastAPI, openedai-speech, a local
    whisper server, or a hosted service) plugs in with just its URL, that a
    server on this computer or the network needs no API key, and that the
    Model / Voice name fields swap models; a saved `""` shows whichever engine the server
    resolved it to, the built-in one wherever it exists. The built-in engine
    needs nothing filled in except, for the voice, **Voice name** — a select
    of the system's voices (`system.voices`, the system default first; free
    text when it lists none). A host adds **URL** and **API key** (masked
    once saved, blank keeps it, **Clear** forgets it through
    `POST /api/voice/clear-key`) and **Model**; local models add a whisper
    size for recognition and a piper voice name. Under each block a
    readiness line from `GET /api/voice` (*Server recognition ready
    (system): Windows via WSL — recognizer en-US, voices David, Zira.* /
    *Server voice not ready: …*): with the built-in engine picked its own
    sentence is the detail (the Linux one, macOS' permission one); a server
    too old to report `system` says it has no built-in engine yet. A saved
    key's **Clear** sits in the key's hint line.
  - **The local-models installer** (`#voice-install`, under Recognition: a
    *Local models* action row, its status in the hint).
    Windows' built-in recognizer is the old desktop (SAPI) one —
    `system.stt_quality: "basic"` (macOS' is `"good"`), or a built-in
    readiness detail ending *basic accuracy — install local models for
    better recognition* — so while it is the engine picked, the block
    recommends *Windows' built-in recognizer is basic. Local models
    (faster-whisper) recognise far better and stay on this computer.* with
    an **Install local models (about 300 MB)** button. The same button
    sits by the readiness line whenever *Local models* is picked but not
    ready, on every platform. It `POST`s `/api/voice/install-local`
    (faster-whisper and piper into the server's own environment, then the
    whisper model — `voice.stt_model`, default `small` — and the piper
    voice), then polls `GET /api/voice/install-local` every 2 s while it is
    `running`: the button reads *Installing…*, and the hint shows the step,
    a `<progress>` (the server reports no percentage) and the install's
    last log line. *done* → *Installed — recognition now uses local models.* (the
    server switched `voice.stt_backend` to `local` itself; the screen
    re-reads the settings and `GET /api/voice`); *failed* → the error and
    **Try again**; a 409 → *Already installing* and that install is
    followed. Reopening the screen mid-install resumes following it. The
    button is gone once local models recognise, and on a server without the
    installer (its `GET` fails). A viewer who may not change the server
    (`configure_allowed: false` in `GET /api/voice`, or the POST's 403)
    gets it disabled with the reason.
  - **On the phone page** (`#voice-phone`): *The desktop app always uses
    this computer.* under the title, then where recognition and the voice
    run for the phone (`voice.stt` / `voice.tts`: *The phone* · *This
    computer*), two rows.
  - **This computer's mic** (the top-bar mic's own choices, per browser,
    `cs_voice_desktop` — the same as its popover, each following the other
    through `mindflock:voice-prefs`): **Read replies aloud** as three radios —
    *Off* (the default), *Replies to what I said by voice*, *Everything,
    every session* (an older saved switch loads as *Off* / *Replies to what
    I said by voice*) — with the name-first and one-at-a-time rules under it;
    **Push to talk** (*Click to talk* · *Hold to talk*); and the **Listen
    for “MindFlock”** switch (the current wake word) (`#voice-wake`, pref `wake`, off by default;
    see [the wake word](#voice-input)), with its hint that the mic stays
    open and the browser's mic light stays on, and — when standby has
    stopped — why (*server recognition isn't ready*, *the microphone was
    refused*). Its hint line ends with **Show me how** (`#voice-tour-link`),
    which replays [the Voice tour](#the-voice-tour).
  - **Voice words** (`#voice-words`): the words that send, stop and start
    over, and the wake word — [Voice words](#voice-words).
  - **Say it as** (`#voice-pronounce`, a sub-block right after the words):
    words the voice gets wrong, respelled — [Say it as](#say-it-as).
  - **Permission dialogs**: the **Approve dialogs by voice** switch (*A
    spoken yes or no answers a permission dialog after its command is read
    aloud. 'Always' is only accepted when you say the word.*).
  - **Try it** (`#voice-try`), one action row per button: **Say a test sentence** (`POST /api/voice/speak` *This is
    MindFlock speaking.*, played through an AudioContext made in the click;
    then *Spoke with system in 240 ms.* or the error) and **Try recognition**
    (asks for the microphone, records three seconds as WAV with a countdown
    on the button — `lib/wavRecorder.ts` — and sends it to
    `POST /api/voice/transcribe`; then *Heard: “…” (system, 412 ms)*, *Heard
    nothing…* or the error; a refused microphone gets a plain sentence).
    Each row's hint says what its button does, its last result, or — with
    the button disabled — why, until the server has that engine ready (or
    while the browser cannot record).
  - **Privacy**: the built-in engines (Windows' and Apple's on-device
    speech) and local models keep audio on this computer, a host receives
    what you send it; the phone's own recognition sends audio to Google on
    Android and to Apple on iOS when no on-device model exists; DuckDuckGo,
    Firefox and in-app (WebView) browsers have no speech recognition service
    of their own — their recognizer fails every start with *not-allowed*
    whatever the microphone permission says, which the page used to report
    as a refused microphone; it now says so, and when the server has
    recognition ready it switches this page to it on the spot; on iPhone,
    voice needs a Safari tab (the home-screen icon cannot hear you), which
    recognition on this computer fixes.

  The server routes and backends are described in
  [web-api.md](web-api.md#voice-talking-to-a-session-from-the-phone). See
  [Voice: talking to a session](#voice-talking-to-a-session).

- **Your devices → One link for all devices** (`general.shared_link`; moved
  here from Mobile) gives the phone a URL
  that names no machine: `https://<name>.<tailnet>.ts.net/m`, answered by
  whichever of your machines is running MindFlock. It is a
  [Tailscale Service](https://tailscale.com/kb/1552/tailscale-services): every
  device that turns the switch on with the same **Name** (default
  `mindflock`) runs `tailscale serve --service=svc:<name> --https=443
  http://127.0.0.1:<port>`, and Tailscale routes the phone to an available
  host. Saving applies it at once (no restart), the server re-advertises at
  every boot, and a clean shutdown runs `tailscale serve clear` so a machine
  that is on but not running MindFlock stops drawing the traffic. A server
  that crashes stays advertised until it next starts. Once it is up, the QR,
  the startup banner and the ntfy push all hand out the shared URL instead of
  this device's own, which stays listed as **This device**. It works in local
  mode too, because `tailscale serve` fronts 127.0.0.1 and the auth
  middleware accepts the service hostname as one of this server's names.

  **Setup checklist.** While the switch is on, the screen shows a numbered
  checklist, one step per thing a host needs. Each step is marked ✓ (passes),
  ✗ (needs fixing) or **?** (this device can't tell), with a one-line reason
  and the exact fix. A step that passes folds its fix under *Show how*.
  Everything is read from `tailscale status --json` and `tailscale serve
  status --json`:

  1. **Tailscale serve access**: whether `tailscale serve` accepted the
     config. A refusal shows `sudo tailscale set --operator=$USER` with a
     Copy button.
  2. **Tag this device**: names the exact machine to tag in the admin
     console's Machines page, by MagicDNS name and Tailscale IP, then says
     to **Disable key expiry** there too: tagging a device after it signed
     in keeps its key expiry, and when it expires the device silently drops
     off the link. When
     Tailscale de-duplicated the name (`box` → `box-1`, because another device
     already has `box`), the step says so, since that is the entry people
     look for and don't find.
  3. **Define the service**: `svc:<name>` with port `tcp:443` on the
     Services page. ✓ once this device can see it (its `services/<name>`
     capability or the service's MagicDNS record). Otherwise **?**, because
     a device that hasn't advertised isn't told.
  4. **Approve hosts automatically**: one policy block with one Copy
     button, prefilled with this device's own tag, the service name and this
     server's port: `tagOwners`, `autoApprovers.services`, `grants` and a
     `tests` stanza. The grants open MindFlock to its owner only, never to
     everyone on a shared tailnet: `autogroup:member` → `autogroup:self` on
     the port (your untagged devices to each other), the tag ↔ the tag on
     the port and 443, your Tailscale login ↔ the tag (port and 443 in,
     the port out), and your login → `svc:<name>` on 443 — what a custom
     policy such as `autogroup:self` otherwise blocks silently. The login is
     this device's, or on a tagged device the tailnet's one untagged-device
     owner; when it can't tell (or the caller may not see it) the block
     names `YOUR-TAILSCALE-LOGIN@example.com` under a `// REPLACE` comment. A
     policy file can't hold a key twice, so the block says, in comments,
     to paste it whole only when none of those keys exist yet and otherwise
     move the lines inside each into the existing key. The policy itself
     can't be read from a node, so this step is **?** until approval shows
     it worked.
  5. **Approved as a host**: only the node's `service-host` capability
     counts, together with the service's VIPs appearing in its `AllowedIPs`.
     `services/<name>` is present while the advertisement is still
     *pending*, so it is **not** approval. An older build read it as
     approval and showed ✓ while the phone timed out. When the capability
     map can't be read, the step says unknown and never shows ✓.
  6. **Test from your phone**: the QR and the shared URL. Only the phone can
     confirm this step.

  **Re-check** (`POST /api/mobile/shared/recheck`) re-reads all of this and
  re-applies what drifted. While the link is on, the server also re-checks
  every 60 seconds, and the screen refreshes at the same pace. A re-apply
  happens when the live serve config no longer carries the service (cleared
  behind MindFlock's back), when the port changed, when this device's tags
  changed since the last apply, or when the last attempt failed. Tags matter
  because Tailscale's auto-approver only looks at an advertisement when it is
  made: one made before the device was tagged stays pending until it is made
  again. Re-check and re-saving the same name also re-advertise a tagged host
  that still isn't approved. An approved, live host is left alone. The other
  devices that report the same link in their discovery hello are listed
  under the checklist. **Sign-in carries across:** each
  device keeps its own token, so the shared QR carries this device's token
  plus every paired device's (`?token=a&token=b`). The browser keeps a
  cookie per token (`mf_auth_<hash>` beside `mf_auth`), and each server
  accepts the request if any of them is its own token. One scan therefore
  signs the phone in on whichever device answers. A device you have not
  paired with shows its login page once. Notification deep links name their
  session `<device>::<title>`, the way every other device lists it. `/m`
  strips its own device prefix back off, which it learns from the public
  `/api/remote/hello`.
- **Security** — only the access token and the gate now, and a pointer
  (*Trusting your own devices lives in Your devices*). **Require access
  token**: *Auto — only when exposed beyond localhost (default)*, *Always on*
  or *Always off* (asked first on an inline row). **This device's access
  token**: **Show** / **Hide**, **Copy** and a **Regenerate**
  button (`POST /api/settings/auth-token/rotate`) for compromise recovery: it
  mints a new token and invalidates every issued cookie, QR code, and paired
  device at once. The browser you regenerate from stays signed in (its cookie
  is re-issued). Unavailable (409) when `MINDFLOCK_AUTH_TOKEN` pins the token —
  change the env var instead.
  **Trusted Tailscale accounts** (now under Your devices → Advanced) lists one checkbox per Tailscale login
  that owns an untagged device on the tailnet
  (`GET /api/settings/tailnet-trust`). A ticked login's own phones and
  laptops open this device with no token (`general.tailnet_trusted_logins`,
  empty = off). Tagged devices and devices shared in from other accounts
  still sign in. Shared-link requests (through `tailscale serve`) are trusted
  on Linux and macOS hosts, where the server can prove the proxy is Tailscale's
  own; on other systems the hint notes they still ask for the token. A ticked login that no longer
  owns a device stays listed so it can be unticked. **(this device's
  owner)** marks this node's own login (shown only when this node is
  untagged). With Tailscale stopped and nothing trusted, the row reads
  *Tailscale isn't running on this device.* Regenerating the token does not
  sign out trusted accounts: untick the login to revoke them.
- **Remote devices** — when `general.remote_control` is on (Your devices →
  Advanced → **Allow remote control**), other MindFlock
  servers on your tailnet appear as sidebar device groups
  (sessions namespaced `<device>::<title>`); pair/unpair via
  `/api/devices/{device}/connect|disconnect`. A MindFlock that can join your
  devices reads "Not one of your devices yet" under its group with **Add to
  my devices…** (Settings → Your devices, its row highlighted — joining turns
  remote control on, no token to paste); pasting an access token
  (**Connect…**) is left for one too old to join, and lives in Your devices →
  Advanced → **Pair by pasted token**, which **Connect…** opens with that
  device preselected. A remote session's pane head
  carries its device's name as a small tag. New Session gains a **Runs on**
  picker once another device is connected: the folder suggestions, Browse…,
  the agent list and the create all answer from the chosen device, and the
  session lands in that device's group. Accounts and Auto-split stay with this
  device (each device signs in with its own logins); a batch from Intake
  always starts here. With a single device there is no picker and no tag.
- **Session templates** — save a New-session dialog configuration under a name
  and refill it later (templates addon, `/api/templates`, synced between
  devices). A template only ever *starts* a session — CLI, repo, workspace
  and an opening prompt; New's **Templates** strip lists them on both pages
  (▶ runs one, ⏰ schedules it) and its **Open Schedule…** (or the top bar's
  **Schedule**, `Alt+S`) opens [Schedule](#schedule).
  A template's **Run now** (its card, its ▶ in New, or the palette's **Run template: …**)
  starts a fresh session from it with the prompt already sent, and an
  optional schedule (every day / weekdays at a time, once on a date, or a
  cron) starts one on its own.
  A template can carry a fast-track, so a scheduled one can end in a PR.
  Saved prompts are the templates that hold only a prompt — one list. To
  paste text into a session that is already running, use the
  [Prompts bar](#saved-prompts).
- **System logs** — under the log, the folded **First-start report**: what
  the first `mindflock serve` printed (`report` on `GET /api/setup`; moved
  here from the Setup checklist).
- **System logs** — a grid pane tailing the server log (`GET /api/logs`,
  3 s poll).
- **Terminal scroll speed** — 1–20 lines per wheel notch, applied live.
- **Appearance** — **Mode** (*Dark* / *Light*: the same value as the top
  bar's 🌙, held in the UI store and set through `lib/theme.ts`, synced as the
  `theme` pref; until anyone chooses, the first paint follows the OS colour
  scheme), then theme sets (surface: backgrounds/panels/borders/text) and
  accent presets, mixable freely; every set ships its own light-mode palette so
  Mode works inside any theme. One bird per set, painted the way the bird
  is: most are **multi-color**, giving the top bar, the sidebar and the window a
  hue each — Scarlet Macaw is a cobalt bar over an emerald sidebar over a scarlet
  window; Goldfinch is brilliant lemon over a black cap. Swallow (the default),
  Raven and Heron are the quiet single-neutral ones. Multi-color sets steer the
  `--topbar-*` / `--sidebar-*` region tokens, which
  `frontend/src/styles/regions.css` rebinds onto each region so the controls
  inside it follow along with no per-component CSS; top-bar icons are monochrome
  `currentColor` SVGs so they survive a bright bar (an emoji 🌙/🔔 would not).
  Accent keys are shared with the surface of the same name where one exists.
  `tests/unit/test_appearance_accent.py` asserts a contrast floor for every
  region of every set, so a louder repaint can't strand the text.
  The palettes live in `theme.css`
  (shared with the mobile page, which renders their window palette only); the
  choice persists server-side
  (`ui.surface` / `ui.accent` via `/api/settings`) so desktop and `/m` match,
  with `localStorage` (`cs_surface` / `cs_accent`) as a pre-paint cache.

Theme (dark/light), sidebar visibility, view mode, pane order/layout, diff mode,
last-used tab, and saved prompts are persisted in `localStorage` (`cs_*` /
`mindflock.*` keys).

## Token / cost usage

Each pane's usage trigger opens a per-session popup (input/output/cache tokens,
context-window fill, estimated cost, model); the sidebar readout aggregates
across sessions with a period selector (session / day / week / month / year,
backed by `/api/usage`). Figures come from Claude Code transcripts and a daily
pricing feed — rough estimates for awareness, not billing.

The **Usage** bar (the built-in Usage Optimizer extension) goes a step
further for subscription plans: a capacity score (how much of the plan
windows you could have filled you actually did), the live window and weekly
cap with a pace reading and the time the limit would land, advice (cheaper
model while you are draining hot at the keyboard; queue work when a window
is about to reset half-empty or unattended hours sat idle), and charts of
window fill, uptime (working / idle / locked out) and attended vs.
unattended time. See [extensions.md](extensions.md#worked-example-2-the-usage-optimizer).

The **Workflow** bar (the built-in Workflow Optimizer extension, button
**Bottlenecks**) breaks every ticket's time down from first work to the PR:
the median (and active) time to PR and to merge, a stacked bar of where that
time went — agent working, waiting on your answer, your turn, idle while you
were away, setup, pre-commit hooks, recovering from failed hooks, committed
not pushed, pushed no PR, PR open, usage limit — grouped by who held the
work, the bottlenecks ranked with what to change, a per-phase table (median
and p90 per item, until or after the PR), a weekly trend, and every work item
with its own timeline. It records from the moment it is installed. Your own
time (waiting on your answer, your turn, idle while you were away) is left
out by default — every number is computed as if you answered instantly, so
the ranking is about the agent, tooling, handoffs, limits and review (PR
open, waiting on reviewers, still counts); the **Include my time** switch in
its toolbar counts it again. See
[extensions.md](extensions.md#worked-example-3-the-workflow-optimizer).

## Voice input

Every voice word named in this section — "over", "over and out",
"disregard", "stop recording", "stop talking", "MindFlock" — is the
**default**: each can be changed in [Settings → Voice → Voice
words](#voice-words), and the mic, its hints and the Voice tour then use the
new ones. Each is also heard the way the recogniser tends to write it (*ova*,
*over an out*, *dis regard*, *mind lock*), and a word you add a letter off
(*rodger* for "roger") — at the end of a phrase only, never mid-sentence
([Voice words](#voice-words), *Heard a little off*).

The mic in the top bar, beside the bell, talks to the **focused session** and
types as you speak: click (or hold) to talk, and each recognised phrase lands
in the session's input field at once — word by word while you speak when the
server's recogniser streams. A phrase ending in "over" stops and sends the
words (Enter, after the server's dialog checks), one ending in "disregard"
takes back what was typed and keeps listening, Esc takes it back and stops;
"stop recording" (or "that's all", "mic off") stops at any point, never typed,
and leaves the words in the field unsent — as letting go of push to talk does.
"Submit" is an ordinary word here (only Claude's question prompt's review step
takes it, through the router). A click keeps listening across messages until
"over and out"; each action is confirmed out loud (*Sent.*, *Cleared.*). The Talk hotkey (default
Ctrl+T, Alt+Space in a browser tab) works like the button from anywhere. Nothing is typed into a
session showing a dialog — a lone "yes", "no", "always" or a number answers it
instead (pressed only with *Approve dialogs by voice* on), and answers a
question the session asked as a prompt. A dictation's first phrase is offered
to the voice router first ("switch to …", "focus three", "next session", "new
session …", "what needs me", "what sessions do I have", "session three yes",
and questions about MindFlock itself — "how does the fence work?" — which are
answered out loud from the docs, *Read replies aloud* or not, and kept for
"say that again"),
and the reply is read aloud only while *Read replies aloud* says so (right-click
the mic, or Settings → Voice → This computer's mic): **Off** (the default),
**Replies to what I said by voice** (a draft this mic submitted asks the
session for a `Say:` line, read once when that turn ends), or **Everything,
every session** — every finished turn that carries a `Say:` line or a digest
(`session.turn_ended` / `activity_changed new="idle"` `data.say`) is read
once, deduplicated across the two events, and this app's typed sends (the ask
card's answer box, the Thread composer) carry `say: true` too. A reply says
where it is from first — *"In vc-frontend, on fix the build: Pushed the
fix."* (`In <name>, on <its task headline>:`, or `In <name>:` when the row
has no headline) — when its session is not the focused one when it is read,
or is one you have not heard from (been read a reply, sent a draft to, or
focused) in the last ten minutes; the session you are on and talking to is
spoken bare. **Only news is read by itself**: a turn is read only when it
ended (the event's stamp, less `turn_ended`'s `idle_for`) after the listener
could have heard it — after this page opened or *Read replies aloud* was
turned on, after they last focused that session — and no more than ten
minutes ago; a reply to a draft this mic sent is always news.
`isNews` / `contextLead` / `HeardClock` in `lib/voiceDesktop.ts` hold the
rules (the twin of the phone's). Everything
the desktop says — replies, ask read-outs, the router's answers — goes
through **one queue, one line at a time** (`SpeechQueue`): never two over each
other, never the browser's voice and the server's at once; a burst from
several sessions plays in arrival order; nothing new starts while the mic
listens (it waits, then goes on); a line more than 60 s old when its turn
comes is dropped (logged to the console, not said); "repeat" says the last
line again. **Speech by sentence**: with the server's voice each line is
split into sentences (at `.` `?` `!` `;` — never inside "3.5", after "e.g.",
"U.S." or "Dr."; one under 12 characters joins the next, one over 180 is cut
at a comma; `speechSentences`), and `POST /api/voice/speak` is asked for the
first one at once: it plays the moment it is back while the next request is
being made — one request in flight, never more — and each is scheduled to
start the instant the one before ends, so a slow voice starts speaking after
one sentence's synthesis, not the whole line's. The requests after the first
pack short sentences together (`speechRequests`: to at least ~2.7 s of
speech, ~40 characters, and at most 1.5× the request before, so each is
back before the one before has finished playing; a long sentence stays a
request of its own) — measured against Voice Lab, a request costs
max(1.4 s, 0.52 × its audio), so a short sentence alone pays the floor for a
fraction of the sound and a gap before each. The line counts as being said (*Speaking…*) from the first request to
the last sentence's end; Stop talking, Esc or the mic opening stop the
sentence playing, abort the one being made and drop the rest. A sentence the
server can't say hands the rest of the line to the browser's voice in one
go. **Barge in** any time: pressing the mic (or the Talk hotkey, or
the ask card's mic) while a line is being read cuts it **before** the mic
opens — that line is over, never resumed, and when you are answering the ask
being read the rest of its read-out is dropped too — and the server's
recogniser skips the first 150 ms of audio (the app's own voice dying away;
`BARGE_SKIP_MS`), so it never hears itself. **"Stop talking"** — or "be
quiet", "quiet", "shut up", "stop reading", "enough", "okay stop", said as a
whole phrase (the router's kind `hush`, matched locally too) — silences
everything queued, every session, never typed; the mic keeps listening if it
was, and the pill says *Quiet.* for a second. **Esc** while something is
being read (and no dialog or popover is taking Esc) does the same, and the
mic's options show a **Stop talking** button while it speaks. Switching
*Read replies aloud* to **Everything** also turns on the server's say-all
marker (`POST /api/voice/say-all {on: true}`, off again when switched away;
`GET /api/voice` `say_all`), so a prompt typed straight into a terminal asks
for a `Say:` line too; on load the pref wins once if the two disagree, and a
device that may not change it (403) says so once and keeps the pref. With either choice a dialog or a question in a reply to what you
said is read out and, once the queue is quiet, the mic listens 12 s for the
answer. With the
[ask pop-up](#when-a-session-asks-you-something) on (the default), the pop-up
does that for any session's dialog, focused or not, and while it is up the
mic, the Talk hotkey and the pop-up's own mic button answer **that** session —
a dialog by "yes" / "no" / "always" / a number, a question by dictating into
its answer box ("over" sends it); with the pop-up off, only the focused
session's dialog is read out — see [The mic](#layout) in the top bar section. Words said while a text box has focus are typed there instead and
never sent ("over" just stops there). In the desktop app the words and the voice go through the
server's engines whenever one is ready (Settings → Voice) — the app's own Web
Speech can't recognise — and in a browser tab through the browser's unless
Settings say *server*; the server's recogniser is sent each phrase as a 16 kHz
WAV clip, resampled without aliasing — or, when it streams (the local whisper
engine), short chunks while you speak, so the words appear as you say them
and may be corrected (see *Words as you speak* below). The phone page's mic is the hands-free
version of the same conversation, with spoken replies on by default — see
[Voice: talking to a session](#voice-talking-to-a-session) under the Mobile UI.

The mic starts without a wait: the microphone is opened as soon as talking is
likely — the mic's options opening, the button going down, the ask pop-up's
mic appearing (only if the permission is already given) — and kept open
90 s after a dictation (`KEEP_STREAM_MS` in `lib/voiceMic.ts`), so the next
press records at once; the browser's "mic in use" light stays on for that
grace, and it is given back at once when the page has been hidden 10 s or you
say "mic off". The server's stream opens while the mic gets ready, so the
first chunk goes the moment you speak (the quiet before it is never sent).
While it listens the mic is **still**: a red icon inside a thin red ring (the
ask pop-up's mic too), and the only thing your voice changes is that ring's
opacity, in three steps — quiet, hearing you, loud — read off the level
smoothed with a fast attack and a slow release (120 ms up, 400 ms down:
a word's first syllable moves it, its gaps don't) and eased over 150 ms
(`levelStep` in `lib/voiceMic.ts`, written as `--mic-step` only when the
step changes): no pulse, no growing halo, no size change. The caption says
*Listening…* from the press (nothing waits on the microphone to say so) and
*Hearing you…* from the first loud block. A reply being read shows a small
still accent dot on the mic, standby a small grey one.

**The pill.** The only voice words on the top bar are a small pill under the
mic (`micPill`, `#mic-pill`, an `aria-live` status): *Listening…* while the
mic is open, *Speaking…* while a line is read, *Quiet.* for a second after
"stop talking", and nothing otherwise. It never shows what you said, what is
being typed or what is being read — those are in the terminal — and no box
appears on its own while the app listens or speaks. Errors and notices (*Click
a session first*, *Sent to Login fix.*) are the mic's tooltip; the ask pop-up
keeps its own words.

**Words as you speak.** Like a phone keyboard, the words go into the field
the moment the recogniser first hears them, and are corrected as it changes
its mind: the phrase under way — the stream's committed words plus its
unsure tail, or Web Speech's interim words (both engines behave alike) — is
typed as a guess, and each new answer is compared with what is typed from
the phrase's start: the end that differs is taken back with backspaces (DEL,
`\x7f`, one per character — what the terminal's Backspace key sends; a text
box has its value set once instead) and the new end typed. At most one
correction per answer, never reaching before the phrase's start — earlier
phrases and whatever was in the field before you spoke are untouched. A
phrase being said carries no trailing space; its end (the pause, or letting
go) settles it with one. Never typed, not even as a guess: a trailing radio
word ("over", "disregard" and their tails like "disregard that"), a stop
phrase or "stop talking" — or the start of one ("stop" might become "stop
recording", "that is" might become "that is all"; such a word goes in once
the next word rules that out) — and a dictation's first phrase while it may
still be a router command ("switch to…" waits whole for the router; a guess
that turns into one is taken back). Nothing is guessed into a session
showing a dialog or with no session focused; the phrase's end decides then.
"Disregard" still takes the whole dictation out, guess and all.

**The accurate final.** The fast pass that types as you speak trades some
accuracy for speed, so at the phrase's end `POST /api/voice/stream/{id}/end`
also answers `final` — the whole phrase heard again by the accurate model —
with `final_ms`, `final_wait_ms` (how long `/end` itself waited: the
accurate pass usually started on the quiet chunk that ended the speech, so
often little), `final_early` and `same` (true when it matches the fast
words). How long to wait for `/end` is seeded from `GET /api/voice`'s
`server_ready.final_ms` (`final.expect_ms`: the server's measured p95 over
its last 20 finishes, 1.4 s before it has three), not a fixed worst case;
`final_early: true` means the pass started on the quiet chunk that ended the
speech, so the correction usually lands within a second of your stopping. When
`final` differs, the whole phrase as typed (its committed words and the guess)
is retyped to it, the same backspace-then-type correction as above and never
before the phrase's start (a text box gets its value set once), before the
phrase's closing space — and the radio words, a stop phrase, the router and a
dialog answer are all read off `final`, not the fast guess (a fast
"…overall" that was really "…over all" sends). `same`, or no `final` (the
accurate pass timed out or is off): the fast words stand.

**Pacing** (`chunkMinS` in `lib/voiceDesktop.ts`): a phrase's first chunk
goes as soon as 150 ms of speech is recorded (`FIRST_CHUNK_S`; the short
lead-in before it rides along), so the first words show after one pass. One
chunk is in flight at a time; audio that arrives while a pass runs is sent
together the moment it returns, and each later chunk waits for at least the
last pass's server time (`ms` in the chunk's answer) × 0.8, held to
150–600 ms (300 ms until the server has reported one) — a fast server gets
small chunks often, a slow one no slivers. The diagnostics line (Settings →
Voice → This computer's mic) ends with the last pass time, the last accurate
final's and the last wake's, e.g. *whisper ~0.4 s/pass · final 1.2 s · wake
0.6 s*. With localStorage `cs_voice_debug` set to `1` the console logs the
start's timings (also in `window.__mfVoiceTiming`): press, mic ready, stream
open, first speech, first unsure words, first characters typed (`typed`),
first committed words, the first accurate final (`final`) — each pass's
server time (`[voice] pass 412 ms`; the last 20 in `window.__mfVoicePasses`)
and each wake's latency (`[voice] wake 612 ms (sound end → listening)`;
`window.__mfVoiceWake`).

**The wake word.** With **Listen for “MindFlock”** on (the mic's options or
Settings → Voice → This computer's mic; per browser, off by default), saying
"MindFlock" **on its own** starts listening as a click on the mic would — at
once, the microphone being open already, with a short two-tone chime in the
same moment (its sound skipped from the recording) — then one dictation like
any other
(the radio words, the router; a text box with the keyboard gets the words;
nothing said within 8 s ends it quietly). A sentence never wakes it —
"MindFlock is great", or the word in the middle of talking: the mic stays in
**standby** (`lib/wakeWord.ts`), watching only the level of what it hears,
and a candidate is speech that starts after at least 350 ms of quiet, lasts
250 ms–1.6 s and is followed by 200 ms of quiet. Only that clip (with 100 ms
of lead-in) is sent, in the very audio block that tail is reached, to `POST
/api/voice/wake`, which recognises it with the word as the hint and answers
whether it was the word alone — so the word's end to listening is the 200 ms
tail, the server's check and the round trip (the diagnostics' *wake 0.6 s*). At most one check
every 1.5 s, one at a time; none while a dictation records, while the app is
talking or for 500 ms after it stops (its own voice never wakes it), and none
while the page is hidden — standby pauses there and the microphone is given
back after 10 s, reopened on return. While standby is on the microphone stays
open, so **the browser's mic light stays on**; the mic button shows it with a
small still grey dot. Standby stops — with the reason on the switch's tooltip
in the options and in Settings — when the server has no recogniser
ready (409), when the microphone is refused, and on "mic off" (which also
turns the switch off). After a reload it starts again by itself when the mic
permission is already granted, else at your next click in the app.

### Voice words

The words the mic listens for are the person's to change — **Settings →
Voice → Voice words** (`#voice-words`, `screens/VoiceWords.tsx`, right after
*This computer's mic*), saved through the ordinary settings model as
`voice.words` (owner-only; synced across the person's devices; `{}` = every
default). `GET /api/voice` reports them as `words` (the effective ones, the
defaults overlaid) and `words_default`; the page keeps them in
`lib/voiceWords.ts` — the built-in defaults until that answers (the mic reads
it when it mounts, with its engine config; Settings → Voice re-reads it after
every save), then the server's.

- **The rows**: **Send** ("over" — sends, listening goes on), **End (send and
  stop)** ("over and out", "over, out"), **Start over** ("disregard"),
  **Stop listening** ("stop", "stop listening", "mic off", "pause
  listening", "that's all", "done talking"), **Stop talking** ("stop
  talking", "be quiet", "quiet", "hush", "shut up", "enough") — each a chips editor: type a phrase,
  **Enter** adds it, **×** (or Backspace in the empty field) removes the
  last; a row with none of its own shows the defaults faded, and phrases of
  its own **replace** them — and the **Wake word**, one text field with the
  default as its placeholder (empty = the default). **Reset to defaults**
  (only once something is set) saves `{}`. The hint says they work
  everywhere: this computer's mic, the phone page and the wake word.
- **Limits** (the server's, refused before a save with the reason under the
  field): at most 6 phrases per row, 1–40 characters each (English letters,
  digits, spaces, apostrophes, commas), no repeats; the wake word one or two
  words of letters, 3–20 characters, shown capitalised as the server hints
  it to the recogniser ("Hey Jarvis"). Phrases are lowercased and their spaces
  collapsed; a save the server kept differently shows *Saved as: …* under the
  row. A viewer who may not change the server (`configure_allowed: false`)
  sees the rows disabled with *Only the owner of this computer can change
  them.*
- **The matchers** (`lib/voiceDesktop.ts` `buildMatchers`, rebuilt the moment
  the words change): `parseRadio` reads the send / end / clear phrases at the
  END of a phrase, **longest first** (so "over and out" beats "over"),
  case- and punctuation-blind, an apostrophe optional ("thats all"), each
  phrase's words escaped, the send and clear phrases taking today's tails
  ("disregard that", "over it"); the hold-back keeps any of them (and their
  starts, and an end phrase followed by a stop phrase) whole; the older
  wordings the router still hears ride along with their main phrase while it
  is in the list (`PHRASE_KIN`: "stop recording" with "stop listening", "turn
  off the mic" with "mic off", "that is all" with "that's all", "stop
  speaking" / "okay stop" with "stop talking", "that's enough" with
  "enough") and go when it goes; a stop phrase of
  **two words or more** ends any committed words at once, while a one-word one
  ("stop", "halt") counts only said on its own (the router's), so "make it
  stop" stays a prompt; a stop-talking phrase counts only as the whole
  phrase, with an optional "ok" / "okay" / "hey" / "please" before and
  "please" / "now" after; and the stop and stop-talking phrases wait whole for
  the router like its own commands. The wake word is checked by the server
  (`POST /api/voice/wake` hears for the saved one).
- **Heard a little off.** The recogniser does not always write the word
  said, so two more hearings ride along, both the server's
  ([Voice words](web-api.md#voice-words) in the API doc has the rule) and
  pinned against the phone and the server by one fixture
  (`tests/unit/data/voice_sounds.json`, run by `voiceEars.test.ts`): **what
  whisper writes** for each default phrase comes with the words as
  `words.heard` (`lib/voiceWords` `heardOf`) and is matched exactly like
  the phrase — *ova*, *oh ver* send; *over an out*, *over and now* send and
  stop; *dis regard*, *just regard that* start over (a clear word heard a
  little off counts from its first letter on — *dis regards* — or said on
  its own: "…in this regard" is dictation, and a false clear loses the
  draft); *stop
  listing*, *mike off* stop; *be quite*, *quite*, *enuf* hush — and is held
  back while it may still be said, like the words; and a phrase **a letter
  off** (`soundsLike`: *rodger* for "roger", *pipe town* for "pipe down" —
  a phonetic key plus an edit margin that shrinks with the phrase, so a
  four-letter word is only ever itself) counts for the send / end / clear
  words at the end of a phrase and for a stop-talking phrase said whole,
  never mid-sentence (*over the bridge*, *the summit meeting*, *this regard
  is important* type through). `parseRadio` and `isHushPhrase` still read
  one RegExp: the matchers' `radio` and `hush` are a `RegExp` whose `exec`
  tries the pattern (the words and their heard forms) and then the sound.
  The stop tail (`stopTail`) stays word for word; a one-word stop or hush
  word a letter off ("hault") is the router's, said alone.
- **The copy follows**: the caption (*Say “over” to send, “over and out” to
  stop, “disregard” to start over*), the ask card's hint, the mic's tooltip
  and *Listen for “MindFlock”* switch, Settings → Voice's hints and the Voice
  tour all name the current words (`dictHint`, `answerHint`,
  `wakeSwitchLabel`, `wakeSwitchHint`).

### Say it as

Everything MindFlock says out loud goes through one shaping step right
before it reaches a voice — and never what a caption shows. The engines
(piper's espeak-ng, the OS voices, a neural host such as F5-TTS, the
browsers' `speechSynthesis`) take plain text and no SSML, so the portable fix
for a word they get wrong is a plain-ASCII **respelling**: *everything goes
live* is said as *lyve* (the adjective, as in "we go live in 3, 2, 1"), while
*I live in Boston* keeps the verb; *I read the file* becomes *red*; `SQL` is
*sequel*, `JSON` *jayson*, `nginx` *engine x*, `tmux` *tee mux*, `k8s`
*kubernetes*, `npm` / `API` / `PR` letters; `askMicTap` is *ask mic tap*,
`backend/web/core/speech.py:404` *backend, web, core, speech dot pie line
404*, `v0.10.2` *version 0 point 10 point 2*, `1m23s` *1 minute 23 seconds*;
Markdown, emoji and URLs ("a link") are dropped. The rule table lives once —
`backend/web/core/spoken_text.py` (`shape(text, overrides)`), its TypeScript
twin `lib/spokenText.ts` (`shapeForSpeech(text, overrides)`) and the phone
page's build-free `static/spoken-text.js` (`window.MindFlockSpokenText.
shape`) carry the same JSON literal — and `tests/unit/data/spoken_text.json`
runs every case through all three. The server shapes inside `POST
/api/voice/speak` (which also caches repeats: `X-Voice-Cache`); a client
shapes just before `speechSynthesis.speak`.

The person's own rows come first. **Settings → Voice → Say it as**
(`#voice-pronounce`, `screens/Pronounce.tsx`, the sub-block right after
*Voice words*): one line per pronunciation — the word, an arrow, how to say
it, **Try** (hears the respelling through the current server voice: `POST
/api/voice/speak` with `shape: false`), **×** — and a draft line (*Word or
phrase* → *Say it as*, Enter or **Add**; its own **Try** plays a draft before
it is saved). Adding a word that already has a row replaces that row; at
most 40 rows of 40 characters a side; the editor refuses what the server
would (`pronounceError`), inline, with no `prompt()` or `alert()`. Saved as
`voice.pronounce` (owner-only; `GET /api/voice` serves it as `pronounce`,
normalised); **Remove all** clears it. *Only the owner of this computer can
change them* for a viewer the server won't let configure.

### The Voice tour

A [tour](#tours) of the mic (`lib/tours/voice.ts`, id `voice`), eight steps
on real elements, in the current voice words: ① the mic button (`#mic-btn`,
hook `voice-mic`) — *Click the mic or press Ctrl+T* (the Talk hotkey as the
keymap shows it), hold it in Hold to talk; ② the focused session's terminal —
the words go in as you speak; ③ the mic's pill — *say “over” to send*,
listening stays on; ④ the mic — “over and out” sends and stops, “disregard”
starts the message over; ⑤ the sidebar's rows — “session three”, “next
session”, “switch to” a name; ⑥ the ask card's mic (`#ask-mic`) when a card
is up, else the bell (`#notif-btn`) — answering a question; ⑦ the mic's
options' **Listen for “MindFlock”** row; ⑧ their **Read replies aloud** row
(Off / Mine / Everything, and “stop talking” cuts it short). Steps ⑦ and ⑧
carry `screen: "mic-pop"`: the mic opens its options for them (under the
tour's layer, so the card and ring sit on top; a click on the card never
closes them) and closes them again when the tour moves off — the only time
the options open without a right-click.

It **opens by itself the first time the mic is used** — a click, a press or
the Talk hotkey that starts listening (not the wake word or a hands-free
listen; `openTourOnFirstUse` in `onboarding/useAutoTour.ts`) — in any hour,
once the first prefs pull is in, with hints on, nothing else touring, and
`voice` not yet in `tours_seen`. **Show me how** replays it: the link in
Settings → Voice → This computer's mic (closing Settings first), the **?**
beside the title of the mic's options (the only thing added to them), and
Settings → General → Help / the palette like every tour. Any exit marks it
seen.

## Mobile UI (`/m`)

A single full-screen terminal for phones: session picker, state dot and chip,
Agent/Terminal/Diff tabs, a `+` that starts a session, and a soft-key bar (esc,
sticky ctrl, tab, arrows, enter — the sticky ctrl folds the next key into a
control code). The top bar is one row, always: the picker's name is what gives
(it ellipsizes), and under ~200 px of picker — a phone in portrait — the state
chip hides and the dot alone carries the tone. While the compose box has focus
the voice strip (its *Tap to hear …* block included) leaves the screen to the
terminal and the keyboard, and the key bar drops its home-bar inset while the
keyboard is up (iOS keeps reporting the inset; it left a band between the keys
and the keyboard). It uses the same WebSockets
and session list as the desktop UI, remembers your last session, and accepts
`?s=<title>` to deep-link one. The server prints the `/m` URL (and a QR code in
tailscale mode) shortly after startup — the banner probe (like the other
non-critical warmups: paste cleanup, scroll-speed apply) runs as a background
task so the server answers its first request immediately instead of blocking
on shell-outs that can hang; the banner may therefore print after the server
is already serving. The copy of that banner written to the server log
(the Settings → System logs pane, `GET /api/logs`) is redacted — no token, no
QR; the full banner appears only on the operator's console.

**Sessions and their actions** — the picker is a button that opens a
**Sessions** sheet, attention first; each row reads like the rail's: a tone dot,
the name, the state word, the rail's zone chip (⛔ / ! / **plan · n** / 🛡 / ✓,
the same rules; a block counts as seen once the session is opened on the
phone) and the repo. When the current session's agent waits on Go for its
plan, a card under the header lists the plan and offers **Go** and **Go — only
the planned files** (the Map's own `code-map/live` and `code-map/go`). The words and tones are the rail's one
state table ([One session state](#one-session-state)) copied into
`mobile.js` `SESSION_STATES`: *Starting*, *Working*, *Question* / *Pre-commit
failed* / *Checks failing* / *Setup failed*, *Idle*, *Waiting on limit*
(*, resumes HH:MM*), *Offline*, plus *Paused*, *Missing* and *Sleeping* (with
a **Wake** action in its sheet) outside the table;
CSS classes `tone-muted|working|attention|neutral|waiting|broken`. A row's
**⋯** (or a long press on it), or the header's **⋯** for the current session,
opens its actions: **Restart** (only when offline — `POST /revive`),
**Resume session** (only on a paused session — the phone has no Pause, as the
desktop's menus don't; the palette, `mindflock pause` and the API still
pause), **Close**
(stops the agent and moves it to Recently closed; toast *Closed — reopen from
Recently closed*) and **Delete…**, which
asks on an inline strip (*Removes the worktree and branch*, focus on Cancel) and
answers a 409 with *Folder in use by &lt;names&gt;. Close those first.*
**Recently closed** (from the actions sheet or the Sessions sheet) lists
`GET /api/recently-closed` with **Reopen**. No `confirm()`/`prompt()`/`alert()`
anywhere on the page — every question is a sheet.

**Scrolling** — swipe up/down on the terminal to scroll history, natural
direction (content follows the finger). The agent TUI lives on tmux's alt
screen, so there is no local xterm scrollback to swipe through; instead a
vertical one-finger drag is translated into SGR mouse-wheel ticks sent down the
PTY and tmux (`mouse on`) scrolls its copy-mode history — the same mechanism
the desktop grid uses for the mouse wheel (and the desktop grid now accepts
touch drags the same way, for tablets). Taps, pinches, and horizontal gestures
pass through untouched, so tapping the terminal still raises the keyboard for
raw keystroke mode.

**Compose box** — a normal phone text field between the terminal and the
soft-key bar: type or paste with full native autocorrect/clipboard support,
then **Send** (or Enter) delivers the whole draft to the PTY followed by Enter.
Shift+Enter inserts a newline in the draft, an empty Send just presses Enter
(confirming TUI prompts), and the field keeps focus after sending so the
keyboard stays up. The soft-key bar still acts on the PTY directly while
composing — arrows and sticky ctrl work mid-draft.

**Diff tab** — a third tab beside Agent/Terminal showing the session's diff, so a
phone can *approve* work and not just unblock it. It reads the same
`GET /api/instances/<title>/diff` the desktop Diff tab does, with the same two
baselines behind one button: **All changes** (`base=fork` — everything vs the
branch's fork point, matching the header badge) and **Uncommitted**
(`base=head`), persisted per browser under the same `mf_diffbase` key. Unified
only — a split view needs width a phone hasn't got — colorized like the desktop
(`@@` cyan, `+` green, `-` red), with each file's name lifted out of its
`diff --git` line into a header that sticks while its hunks scroll past. Long
lines scroll sideways inside the panel rather than wrapping (a wrapped diff line
loses its shape) and the page itself never scrolls horizontally. Very large
diffs are truncated with a note pointing at the desktop. The Diff panel is a
*view*, not another terminal: the PTY stays attached behind it, so switching
back is instant, and the git action bar stays visible (with its status toast
following you into the tab) because reading the diff is exactly when you decide
to push.

**Git workflow action bar** — a **Commit / Push / PR / Merge** button row that
brings the desktop pane header's guided flow to a phone, so the whole git
loop is drivable from mobile. The buttons hit the same
`/api/instances/<title>/{commit,push-branch,make-pr,merge-pr}` endpoints the
desktop uses, and `nextAct()` mirrors the desktop's stage logic to **highlight
the one recommended next step** for the session's current stage (agent →
Commit, committed → Push, pushed → PR, PR open → Merge). **Commit** opens a
bottom-sheet commit-message dialog; **Merge** asks first on a sheet (*Merge this
PR?*). **Push** honors the O3 soft gate like the desktop `pushSession` flow: a
`409` for checks that haven't passed opens a sheet whose **Push anyway**
re-pushes the same session with `{force: true}`. A red-zone `409`
(`red_zone_breaches`) on Push / PR / Merge is explained on the same sheet in the
desktop's words; the override itself is desktop-only for now. After a PR opens
the phone resets the stage (`POST /reset-stage`), as the desktop does.

**New session (`+`)** — the `+` in the top bar (and the "No sessions yet" line,
which is itself the button) opens a bottom sheet that starts a session from the
phone. Two screens, one input each:

1. **What do you want to work on?** — one sentence. It goes to
   `POST /api/session-plan`, which reads the filesystem, asks the flock's own
   coding CLI one headless question and answers with the New Session form's own
   fields. The button reads *Reading…*, then *Still reading…* after 8s (the same
   relabel the desktop's Describe box does), because that is a real model turn
   and takes ~10-25s.
2. **Review** — an editable **Name**, the **Folder**, one line saying the mode
   ("work happens in the folder directly" / "in a new worktree" / "a new
   folder"), the server's own note, and the typed sentence as the session's
   editable **first prompt**. **Start session** posts it to the unchanged
   `POST /api/instances`.

Almost none of the desktop dialog comes to the phone — no templates, no folder
combobox, no Browse tree, no provisioning, no workspace strategy, no launch
flags, no account picker, no model pin. Every one of those has a working
default, and the way to inherit a default is to *send no key at all*, so the
create posts exactly five keys (`title`, `repo_path`, `prompt`, `in_place`,
`init_repo`) and the session that comes up is the one the desktop would have
made with nobody touching Advanced.

**Which device.** When another MindFlock device on the tailnet is connected
(paired, remote control on, answering — the same `connected` rows of
`GET /api/devices` the desktop dialog's device select offers), screen 1 grows a
**Run on** select: this device, then each connected device by hostname (the
MagicDNS key when two share a hostname). With no second device it stays hidden.
It sits on screen 1, before Continue, because the plan is read *on* the chosen
device: the plan, the folder list and the create all go through
`/api/devices/<device>/fwd/…`, so the folders on offer are that device's and the
session is born there. The review screen then labels the folder *Folder on* the device's name, the
new-folder tick names the device the folder is left on, and the session is
selected under its namespaced `<device>::<title>` once it lands. The pick is
kept across openings of the sheet, and falls back to this device if the picked
one stops being connected.

**Confirming a new folder.** When the plan proposes a folder that does not exist
yet (`folder_exists: false`, which happens only for the plan route's
`new:<name>` answer), the review screen shows a checkbox naming that folder,
defaulted **off**, and **Start session refuses while it is unticked** — with a
sentence saying which folder it would create, not a greyed-out button. Creating
a directory is the one thing a plan proposes that outlives the session and that
closing it never takes back: a worktree goes when the session does, but nobody
comes back for the folder. This is *not* the same question as the desktop's
"Create a git repo in this folder" (`init_repo`, a `git init`) — both can be
true at once, and the tick's own copy says so.

**No model? Not a dead end.** `POST /api/session-plan` answers **502** with one
human sentence when the CLI is missing, times out, or answers unreadably. The
sheet shows that sentence and falls back, in place, to a tap-list of folders from
`GET /api/repos/suggest` — the same rows the desktop dialog's suggestion chips
show. The typed sentence still becomes the prompt. That list is also reachable
deliberately from the review screen (tap the folder row), so a plan that picked
the wrong repo is correctable without walking to a desktop.

**No folder name ever reaches `POST /api/instances`.** There is no free-text
folder field on this page, on purpose: `_prepare_plain_repo` realpaths whatever
it is handed against the *server's* cwd and then `makedirs` it, which is how
typing `api` once created a `MindFlock/api` directory. Every folder here is
either a plan's `repo_path` (the model answers with the **number** of a row in a
menu the server built by walking the filesystem — it never writes a path) or a
row of `/api/repos/suggest`, and Start refuses anything that isn't absolute
anyway. The create answers **202**, so the new session is selected from the next
poll tick, when its row actually appears in the list.

### A session is asking you something

When a session waits on you — a permission prompt or other dialog, or a reply
that ended with a question in plain prose that nobody has typed to since (the
server's `asking` on each `GET /api/instances` row) — the phone has an **ask
sheet** in the middle of the screen, over whatever else is open. It opens
**by itself only for a real dialog** (a prompt drawn on the screen that the
server parsed: a permission, a plan approval, Claude's select questions — not
one it could not parse, which keeps its chip and notification). A question in prose
never pops up on its own — a reply that merely says "question" is not a
prompt: it keeps its **Question** chip on the row, its notification and its
ntfy push, and its sheet opens when you tap the row or the notification. It
shows the session's number (the Sessions sheet's, so "session two" by
voice is the same one) and name, the kind of ask (**Permission**, **Question**,
**Plan approval**, plus *from &lt;agent&gt;* for a background sub-agent's
prompt), the question, and the command in monospace. Then the way to answer it
right there:

- **A dialog the server parsed** — its options as buttons. A tap posts
  `POST /answer {keys, dialog_id, by: "user"}` pinned to the dialog it read;
  a dialog that changed meanwhile (409 `dialog_changed`) is re-read and the new
  prompt shown with a note; one answered elsewhere (409 `dialog_answered`)
  says *Already answered*.
- **Claude's select questions** (AskUserQuestion; `GET /dialog` says
  `kind: "select"`) — answered step by step in the same sheet, which never
  closes mid-way. A row of steps like Claude's tabs (done ✓, the active one
  outlined, Submit last), the question, then:
  - *single-select*: the options with their descriptions as buttons; a tap
    posts the option's digit (`POST /answer {keys, dialog_id, by: "user"}`)
    and the next step is read into the sheet (the same id still on screen a
    moment later is waited out; a lone question is submitted at once);
  - *multi-select*: checkboxes ticked on the phone, then the step's primary
    button — labelled from the dialog's `button`, **Next ›** or **Submit ›**
    — posts the digits whose state differs from the server's `checked`
    followed by `nav.next`, in one `/answer`;
  - **‹ Back** (`nav.prev`) from the second step on, on the left of the
    step's row;
  - the *review* step: your answers listed, then one row — **‹** (Back), a
    plain **Cancel** and one primary **Submit answers** (their digits; no
    numbered rows, no digit badges);
  - *Type something*: a tap opens the field; **Send** posts its keys, then
    `{text, keys: ["Enter"]}`, both pinned to the same `dialog_id` (it holds
    while the row is typed into). Options are always posted with their own
    `keys` (with Claude's cursor on the text row they read `["Up", "1"]`);
  - *Chat about this*: the field too; **Send** posts its digit, then the words
    go as a prompt (`POST /send {text, dialog_safe: true}`, a dialog still up
    for a moment waited out).

  A 409 `dialog_changed` inside the flow is just the next step, never an
  error; no dialog any more (204) is the question answered, and the sheet
  moves on. By voice (the sheet's 🎙, or voice reading the question — which
  shows it here even with the pop-up off): a number or an option's own words
  answer the shown step through the router (`POST /api/voice/route`; the
  number, *yes* / *no*, or the label when it cannot say; a free-text answer
  the router makes carries its `text`), on a multi-select they tick the box
  and say so; *next* and *back* are the buttons; *submit* / *done* before the
  review run **Next** up to it; *submit* / *yes* on the review submits,
  *cancel* / *no* cancels; *type something* /
  *chat about this* open the field and dictate into it — *over* sends it.
  After each spoken step the next one is read and listened for; the end is
  *Answered.*

  **Speech stops when you act.** Any tap on the sheet — an option, a box,
  **Back** / **Next** / **Submit answers**, **Send**, **Later**, the ‹ ›
  stepper — or a keystroke in its field cuts what is being read about the
  ask shown and drops its queued lines (another session's lines keep their
  place), and voice stops listening for its answer (words in the field
  stay). For 3 s after a tap nothing new is read from the sheet: the step
  it moves to is on the screen under your hand, and the poll never reads
  that step later either. While the sheet reads, its 🎙 cuts the reading
  and listens at once (barge-in, below).
- **A dialog it could not parse** — **Open the session**, and a line saying the
  prompt needs the terminal.
- **A question in prose** (opened by its row or notification) — a field and
  **Send** (`POST /send {text, dialog_safe: true}`, plus `say: true` with
  Spoken replies on *Everything*), plus **Open the session**. If a dialog came up in the
  meantime (409 `in_dialog`) the sheet shows that dialog instead and keeps your
  words in the field.

**Later** puts the ask off: a prose question is dismissed on the server
(`POST /asking/dismiss`, so it stops being "asking" everywhere), a dialog is
snoozed on this phone by its id (a new dialog from the same session still pops
up). A tap outside the sheet, the phone's Back and Esc do the same. Several
sessions on a dialog → one sheet with **1 of N** and ‹ › — the oldest first;
answering one moves on to the next (a prose question is in that count only
while it is the one shown). The sheet does not open for a dialog you put off
or already answered, nor while you are typing into that session's compose box
(it opens once you stop), and not by itself at all with the ask pop-up
setting off (`notifications.ask_popup`, read from `GET /api/notify/ntfy`; on
when absent) — that setting is the switch for dialogs. Whatever that setting says, an asking
row in the Sessions sheet reads **Question** with the question under it (a
prose question no longer reads *Idle*), tapping it opens the session with its
ask sheet on top, and the tab's title counts the asking sessions
(`(2) MindFlock — Mobile`). Asks arrive with the 4 s session poll; while voice
is on, the events socket's `session.question` / clarify frames bring them at
once.

**Answering by voice.** The sheet's 🎙 listens for the answer to the ask shown
(it selects that session first — voice follows the selection). A dialog goes
through the voice dialog mode: *yes* / *no* / *always* (said, never implied) /
an option's number when the options are not a permission, under **Approve
dialogs by voice** (off → *tap an answer*). A prose question is dictated into
the sheet's field with the radio words — *over* sends it (a dictated answer
asks for a spoken reply, like any dictated prompt with Spoken replies on)
and, the sheet closed, listening goes on into the compose box (the sheet's
🎙 is a continuous listen, below); *over and out* sends it and stops;
*disregard* clears it; *submit* is just a word. Inside the sheet every phrase
is the answer's words, never a command. The strip's state
is mirrored on the sheet while it is open. A push's tap (`/m?s=<title>&voice=1`)
opens that session's sheet — even with the pop-up off — with **Hear it and
answer by voice**: one tap reads the question name first and listens (12 s, one
*Still there?* retry), replacing the strip's *Tap to hear*. With voice already
on and Spoken replies on (or on *Everything*, voice on or off), another
session's new ask is read aloud once, name first, in the one speech queue,
without listening (that would change what "yes" means mid-conversation); the
voice session's own ask is read by its own loop, never twice, and on
*Everything* a prose question is its turn's spoken line, read already.

**Notifications while the page is open.** The Sessions sheet has **Notify me
when a session asks**: it asks the browser for permission, then a new ask on a
hidden page raises a notification (once per ask, tagged per session), and
tapping it focuses the page with that ask's sheet open; once granted the same
row turns them off and on for this browser. A visible page buzzes instead
(`navigator.vibrate`, not while the mic is listening). What the phone can do
is said under the button:

- **Android Chrome** shows page notifications only through a service worker;
  the page registers `/mobile-sw.js` (scope `/m`) when notifications are
  allowed and shows them through it, falling back to `new Notification` (which
  desktop browsers and Firefox accept). A backgrounded tab is polled every 10 s
  while notifications are on, and Android freezes a long-hidden tab — a
  closed or frozen page notifies nobody.
- **iPhone / iPad**: only a web app added to the Home Screen (Share → Add to
  Home Screen, iOS 16.4+) can notify at all, and even then only through a
  service worker; a Safari tab cannot. The page says so instead of offering
  the button.
- Everywhere, the server's **ntfy** pushes (rule *needs input*, now also for
  prose questions with the question as the body, tap → the deep link above)
  are what reach a phone whose page is closed; when ntfy is active the row
  notes that both would arrive.

### Voice: talking to a session

The phone page can hold a spoken conversation with a session: you dictate a
prompt, hear what the session is doing, and when it finishes or asks a
question you hear that too and answer by voice. Speech runs on the phone's
own engines (Web Speech recognition and `speechSynthesis` — no key, no
subscription, no server compute); the *words* come from the session you
already pay for, which writes its own spoken line (below). Nothing here
spends a model call unless you ask for a summary.

**Starting.** Tap the mic (🎙, right of **Send**). The tap primes speech
output (iOS and Android speak nothing until the first `speak()` runs inside
a tap; the same tap sets `navigator.audioSession.type` to `"playback"` so the
silent switch does not mute the page — or to `"play-and-record"` when this
page may open the microphone itself: recognition on the server, the
fallback to it, the wake word; iOS refuses capture under `"playback"` with
*InvalidStateError: audio session category is not compatible with audio
capture*, and the server recognizer sets `"play-and-record"` again right
before it asks for the microphone), takes a screen wake lock, turns voice
mode on for the selected session (`POST /api/instances/<title>/voice
{on: true}`), opens the events socket and listens — 15 s for a first word,
then for as long as you keep talking (Android buzzes 30 ms). With **Hold to
talk** (the ⚙ sheet's *Push to talk*) that first tap only turns voice on;
you then press and hold the mic to talk. A **voice
strip** appears above the composer. It says **one word and nothing else**:
*Listening…* while the mic listens, *Speaking…* while a line is said (the
whole line, every sentence of it), *Sending…* for the moment a prompt is on
its way (the radio word was heard), *Quiet.* for a moment after a hush,
*Heard nothing.* for a moment when a listening window closed without a word
(a cue, never a silent blank), and
otherwise nothing — never the words it hears (they are in the box already,
as you speak), never the line being said, no hints; one line, so nothing
on the strip pops up or grows as words are typed or a line plays. Beside
it, **Cancel** and **⚙** (and **Stop talking** while a line plays). The
hints and the words so far still reach the ask sheet's voice line, which is
unchanged. **The mic is still:** while it records, a constant thin red ring
round the 🎙 (and the ask sheet's) whose opacity alone eases (150 ms) between
three steps — quiet, hearing you, loud — from the voice's level, up in
~80 ms (alive on the first syllable) and down over ~300 ms (no flicker
between words); no scale, no pulse, no halo that bounces with the voice. The
wake word's standby is a small grey dot, a line being said a still accent
dot. The level meter (a thin bar along the strip's top edge, never a row of
its own) shows only while the ⚙ sheet is open. Voice follows the
**selected** session: picking another session in the Sessions sheet (or
saying "switch to <name>", "session two", "next session") moves the
conversation there; the flag of the session you left stays on. The Sessions
sheet numbers its rows **1, 2, 3…** in the order shown (attention first),
and that order holds while the sheet is open — a row that changes state
does not jump, a new session is added at the end — so the number you read
is the number you say. With voice on the sheet says so under the list
(*Say "session 2" or "next session" to switch…*). **On iPhone, open `/m` in a Safari tab.** iOS has speech
recognition only there (WebKit bug 225298): the home-screen icon, and every
other iOS browser, can speak but cannot hear you, and the page says so on the
first mic tap — a **Voice needs Safari: open this page in a Safari tab**
banner with this page's URL and **Copy**. Without a recognizer the mic still
turns voice on as a read-aloud mode (*No speech recognition here — replies
are still read aloud*): replies and dialogs are spoken, nothing listens.
Server recognition (Settings → Voice, `voice.stt = "server"`)
is what lets the home-screen icon hear you: the page then records a clip
and sends it to the server's recognizer instead, and the
Safari banner goes away. Diagnostics (below) names the adapter in use.
The clip is **16-bit PCM mono WAV at 16 kHz** (`Content-Type: audio/wav`) in
every browser: the server's built-in recognizers — Windows' System.Speech and
macOS' Speech framework, which work on a fresh install with no model
download — need plain PCM, and faster-whisper and OpenAI take WAV too, while
`MediaRecorder` only makes webm/opus (Chrome) or mp4/AAC (Safari). So the
page pulls the samples off its one `AudioContext` (made inside the tap): the
mic stream → a `ScriptProcessorNode(4096, 1, 1)` → a muted gain → the
destination (Chrome does not run a processor that reaches nothing). Each
block's first channel is kept; when the clip ends they are joined, resampled
to 16 kHz, clamped and written behind the 44-byte RIFF header — the same
clip as the desktop app's recorder (`lib/wavRecorder.ts`). The resampling is
anti-aliased: the browser's own resampler first (an
`OfflineAudioContext(1, n × 16000 / rate, 16000)` rendering the clip through
a buffer source), and where that is missing, refuses the rate, fails or
takes over 5 s, a low-pass decimator in the page — each output sample is a
windowed-sinc (Blackman) weighted average of the input samples around it,
cut off at 0.45 of 16 kHz — so sound above the clip's 8 kHz Nyquist is
filtered out instead of folding back into the speech band as noise (plain
interpolation, the old way, let it fold back at full level). Speech up to
about 6 kHz keeps its level. The RMS of those blocks drives the level meter
and the silence detector: 1.1 s of quiet after speech ends the clip, so a
phrase arrives sooner (a tap ends it at once, 12 s or the listening window
is the cap, and a clip that heard no speech is dropped).
When the server streams (`GET /api/voice` → `server_ready.stt_stream: true`,
the local whisper backend) the words appear **while you speak**: the tap
opens a stream (`POST /api/voice/stream {lang}` → `{id}`), and the phrase
goes out in chunks of 16 kHz WAV (`POST /api/voice/stream/{id}/chunk` →
`{committed, partial, ms}`) — one request at a time, so a slow server gets
fewer, longer chunks rather than a queue.

**Words as you speak, corrected as you go.** Like the phone keyboard's
dictation, the phrase so far — the stream's `committed` words, then its
`partial` (the unstable tail) — is **typed into the box after every chunk**:
the first pass's guess shows within one pass of your first word, and each
new answer **corrects it in place** from where it differs (*fix the bu* →
*fix the bag* takes back *u* and adds *ag*; the words before the phrase are
never touched, the caret stays at the end). A space joins the phrase to the
words already in the box; none is added after it while you speak. The
phrase is decided only at its **end**: the guess is taken back and the
final words go in by the usual rules — *over*, *over and out*, *disregard* (which
takes back the whole box, the guess included), a command, an answer — so a
correction can never send, stop or answer anything. **Never typed, not even
as a guess:** a trailing control phrase or its start — *over* (…*and out*),
*disregard (that)*, *stop* (it may become *stop recording*), *that is*
(…*all*), *quiet* / *stop talking*, and while the session is asking *yes*,
*no*, *option* (…*two*) — until the next word rules it out (*stop the
server* goes in once *the* is heard); a **first phrase into an empty box
that may still be a command** (*switch to…*, *what's…*, *new session…*,
*yes*: it waits whole for the router — one that can't be one, most prompts,
is typed as it comes and stays in the box while the router reads its final
words); anything while a **dialog** is being answered by voice (its answer
is the final words, never a guess); and anything while an earlier phrase is
with the router. A stop phrase still acts the moment its words settle,
mid-phrase. Listening that stops before a phrase ends (Cancel aside, which
empties the box) leaves its guess in the box as text.

**Control words act from the guess.** A guess that **ends** in a control
phrase (`voiceGuessControl`: *over and out* before *over*, *disregard*, a
stop phrase, *stop talking*) acts as soon as the voice has been quiet
**250 ms** after it (`VOICE_CONTROL_TAIL_MS`) — never while it is still
loud, and not at all if the next guess rules it out (*go over the bridge*)
— instead of after the 1.1 s silence stop. *Disregard*, a stop phrase and
*stop talking* need no accurate words: they act on the guess at once, the
stream DELETEd, never ended. *Over* / *over and out* end the phrase there
(`POST …/end`) and send with `/end`'s accurate `final` (the retype first);
when `/end` takes longer than its **budget** the fast words go and a later
final is ignored. The budget follows how long `/end` takes on this server
(`voiceEndBudgetMs`): **1.5 s** (`VOICE_CONTROL_END_MS`) only while nothing
is known; once `/end` has been timed here (a running mean of its round
trips, seeded by the advisory `final_ms` when `GET /api/voice` carries one)
**1.5× that plus 100 ms**, held to **0.3–1.5 s** (the desktop app's rule)
— so a whisper that answers in 300 ms is waited for 550 ms at most, and a
late `/end` grows the next budget (it took at least that). The Enter itself waits for no round
trip: the ack's baseline read goes out alongside `POST /send`, not before
it. The trace and Diagnostics time it: *Last control: over → sent 0.9 s
(waited for /end up to 1.2 s; /end takes ~0.7 s here)* (the word's last
loud block → the Enter).

**Pacing.** A phrase's first chunk goes once **150 ms of speech** is in
(with the 300 ms lead-in before it); after that each chunk waits for the
last pass's time (the server's `ms`) × 0.8 of new audio, held to
**150–600 ms** (300 ms until a pass has reported), and audio that arrives
while a pass runs goes together the moment it is back — a fast server is
fed small chunks often, a slow one no slivers. Only a first chunk is padded
(with silence before it, where the quiet was). The phrase ends as a clip
does (silence, the tap, the cap — 30 s here); the unsent rest goes out,
`POST …/end` → `{committed}` flushes the last words, and the next phrase
opens a new stream; Cancel or voice off `DELETE`s it. **The accurate
final:** a server with a slower, more accurate model besides the fast
streaming one also answers `/end` with `final` (the whole phrase
transcribed again by it), `final_ms`, `final_wait_ms`, `final_early` and
`same` (true: no different). The page sizes its wait for `/end` from `GET
/api/voice`'s `server_ready.final_ms` (`final.expect_ms`, the server's
measured p95; 1.4 s before it has three finishes) rather than a fixed worst
case, and `final_early: true` says the accurate pass started on the quiet
chunk that ended the speech, so the correction usually lands within a second
of the person stopping. When
`final` is there and `same` is not true, the **whole phrase typed so far**
(its committed words and the guess — never the words before it) is
replaced in the box by `final`, the caret at the end, **before** the phrase
is decided: *over*, *over and out*, *disregard*, a command, a dialog's answer all
read the accurate words. Without `final` (an older server, one model) the
fast words stand. Diagnostics' *Last transcribe* line says *final 1.2 s*
(and *(corrected)* when it changed the words). A 409 on the open
(the server does not stream after all: a clip per phrase until `GET
/api/voice` says otherwise) or any failed request turns the phrase into a
clip — the words so far stay, the audio the stream had not answered is
transcribed in one piece.
**Starting fast, and seeing it hear you** (server recognition). The stream
opens *with* the tap, alongside the microphone's start-up, and nothing is
uploaded before your first word: the page keeps only the last 300 ms before
it (the word's soft start) and sends that with the first 150 ms of speech,
so the first words show — and are typed — about 0.25 s after you start
talking (measured in the headless harness with a 120 ms recognizer; a real
whisper pass adds its own time, ~0.3–0.6 s with the streaming model). The
**microphone stays warm** for 90 s after a phrase (`VOICE_KEEP_STREAM_MS`),
for as long as a reply is on its
way back (the reply opens listening by itself), and 15 s after the page is
hidden: a tap in that time starts listening at once, with no
`getUserMedia`. The browser's mic-in-use indicator stays lit for that long;
voice off gives the mic back at once. While you speak, a **ring round the
mic** (and the ask sheet's mic) grows and firms with your voice — its level
relative to the room's noise, so a quiet room sits at zero, rising in
~80 ms and falling over ~300 ms — and the ⚙ sheet's meter follows it. The
start chime never delays the first word: under it (and under a barge-in's
or a confirmation's tail) the recorder keeps the audio, so a word that
starts right away loses nothing, but no word *starts* in it — the chime
is never heard as speech. The
strip says *Listening…* throughout — no *Hearing you…*, no greyed partial:
the streamed words are typed into the box as they come. Android ticks (12 ms) at
each phrase's first word — but not when the 30 ms listening buzz has just
gone. Diagnostics shows *Microphone: warm / not held* and *Last start: mic
… ms, stream open … ms, speech … ms, words … ms, typed … ms, settled … ms
after the start* (*typed*: the first characters in the box); with
`localStorage.cs_voice_debug = "1"` the console logs every mark and every
pass's time too.
A Chromium browser that does not ship Google's speech key — Arc, Brave,
Vivaldi, Opera — has the recognizer but every start fails with a `network`
error and nothing is heard: the first is retried, the second in a listening
window stops the loop with *Speech recognition failed (network): this browser
cannot reach its speech service…* on the strip (Diagnostics keeps it as *Last
error*). Use Chrome, Edge or Safari there, or server recognition, whose clip
records in any browser.

**The words come from Settings → Voice.** Every word below — *over* (send),
*over and out* (send and stop), *disregard* (clear), the stop phrases, the
*stop talking* phrases and the wake word *MindFlock* — is the default; the
owner changes them on the desktop in **Settings → Voice** (synced to every
device), and the phone only reads them: `GET /api/voice` → `words` (`send`,
`end`, `clear`, `stop`, `hush` lists and `wake`), on load, when voice turns
on, when the ⚙ sheet opens and when the page comes back to the front while
voice is on. Until the first answer — and from an older server that sends
none, or a kind it leaves empty — the page uses ours. Every client-side
check is built from them (`voiceWordsApply`: the radio parser, the stop
phrase at a phrase's end, the guess's held-back words, the commands' stop
and hush, the fast control on a streamed guess), longest first, each phrase
escaped, an apostrophe optional, a comma optional (*over, out* is also
*over out*), the clear words keeping their tails (*… that / it / all /
everything*). A stop phrase of two words or more ends any phrase (*fix it,
that's all*); a one-word one (*stop*) stops only said on its own. A default
phrase keeps the spoken variants the router keeps while it is in the list
(*stop listening* → *stop recording*, *mic off* → *microphone off*, *done
talking* → *I'm done talking*, *stop talking* → *stop speaking*, *okay
stop*…) and what the recogniser writes for it (`words.heard`, the server's
table — *ova*, *dis regard*, *over an out*, *be quite* — folded in by
`voiceWordsWide`, held back on a guess like the words); a phrase the owner
adds is matched whole, or a letter off (`voiceSoundsLike`, the server's
`sounds_like` to the letter — *rodger*, *pipe town*): `voiceDictParse`
falls back to the last words that sound like a send / end / clear phrase
(`voiceSoundTail`), `voiceIsHush` to the whole phrase sounding like a hush
one; `voiceTail` (a stop phrase at a phrase's end) stays word for word. The
shared fixture `tests/unit/data/voice_sounds.json` pins the page against
the desktop and the server. Every hint and caption
that names a word — *Say “over” to send…*, *In the box — say “over”…*, the
⚙ sheet's *Listen for “MindFlock”*, the wake word's notes — names the
current one, refreshed the moment a refetch brings new words.

**How to talk** is a short card in the page's sheet style: tap the mic (or
hold it, with Hold to talk); the words appear in the box as you speak; say
*over* to send — listening stays on; *over and out* sends and stops,
*disregard* starts over; *stop* / *stop listening* stops with the words
kept, *stop talking* cuts a spoken reply; switch with *session three* or
*next session*; answer a session's question from its sheet (*yes*, *no*,
*option two*); the wake word; Spoken replies Off / Mine / Everything — each
line built from the owner's words. It opens by itself the **first time
voice is turned on in this browser** (the `cs_voice_tutorial` localStorage
flag; storage that throws: once per page load) — the listen that tap would
have started waits for **Got it**, whose tap starts it — never over the
asking sheet, and any time from the ⚙ sheet's **How to talk** row, right
under voice mode. **Got it** closes it and remembers.

**The loop.** Push to talk, with radio rules. Say the prompt phrase by
phrase: interim words show in the strip's caption, each finished phrase
lands in the compose box, and the mic stays open across pauses. How it ends
is the ⚙ sheet's **Push to talk** (a per-phone setting):

- **Tap to talk** (the default) — a tap on the mic starts a **continuous**
  listen (so do the first tap that turns voice on, the wake word and the
  ask sheet's 🎙): it runs until **"over and out"**, a stop phrase, the next
  tap on the mic (or on the strip), Cancel — never on silence. The tap
  stops with the words in the box, unsent.
- **Hold to talk** — press and hold the mic to talk; letting go (or the
  phone cancelling the touch) stops, the words kept. A hold is never
  continuous: its *over* sends and ends it. The first tap of a session
  still just turns voice on, and a plain click (a keyboard, a screen
  reader) starts a tap-style listen.

Either way the words still on their way are waited for before listening
stops: the phone's last phrase (at most 1.5 s), a server clip being
transcribed, a route answer (below). The radio words work in both modes:
end a phrase with **"over"** and the box (with that phrase's words) is
**sent** — and a continuous listen goes on listening: the box empties at
once, the next phrase starts on the same session, no tap needed (the send
is the same `POST /send`; a failure puts the words back in front of what
was said since). End it with **"over and out"** (or *over, out*) and the
box is sent and listening **stops** (nothing in the box: nothing is sent,
it just stops). End it with **"disregard"** (or *disregard that*) and the
box is emptied and listening goes on. **"over" and the Send button are the
only ways to send** — the push-to-talk gesture never does; **"submit" is an
ordinary word** (typed, never sent — a select dialog's review still takes a
spoken *submit*, below). An "over" with nothing in the box says *Nothing
to send yet* and listens on. **Cancel** stops and drops the words, **Send**
sends whatever is in the box at once, and a listen that is not continuous
(a reply's question, hands-free) stops after two minutes without a word,
the words kept, unsent (*Kept, not sent — Send sends it, or tap the mic to
go on*). The last words of a phrase are what count — an "over" in the
middle of a sentence is a word — and case and punctuation do not matter
(*Over.*, *Over and out!*, *Disregard that.*). Commands ("switch to…", "new
session…", "what needs me", "voice off"…) are commands only while the box
is empty; mid-message they are words (see **Voice commands** below) — except
the **control words**, which act at the end of *any* phrase: a stop phrase
always, an answer while the session is asking (below). Then
*Sent to <name>.* (or *Queued for <name> — it is on a prompt.*: while voice
is on, Send and "over" go through `POST /send {dialog_safe:
true, say}`, so a prompt is queued
rather than typed into a dialog; a waking session says *<name> is waking up,
try again in a moment.*, a paused one *<name> is paused. Resume it first.*,
anything else *Couldn't send.* with the text kept). While the agent works the
page polls its newest reply every 3 s and speaks the **first sentence** of
the first assistant text once — the "what it is doing" confirmation (Claude
opens with "I'll …"); nothing else is narrated unless you ask. A Codex
session gets no such sentence (its screen fallback is skipped), only the line
at the end. When the turn ends — `session.activity_changed` → `idle` over
`WS /api/events`, with the page's 4 s session poll as the fallback — the
page speaks the session's **`Say:` line**. The line itself opens with its
context (see *Where the line comes from*): *We were working on the login
page, and the redirect loop is fixed. Should I push it?* A reply from the
session the phone is on, and talked to lately, is spoken bare; a reply from
any other session, or from one not heard from in ten minutes (or ever —
read a reply, sent a prompt, switched to or selected), says where it is from
first — *In Invoice parser, on parse the March invoices: Parsed all twelve
files.* (`In <name>, on <its task headline>:`, or `In <name>:` with no
headline) — so a voice from elsewhere is never a surprise (decided when the
line is said, so after a switch the new session is the bare one). Other
lines (confirmations, dialogs, asks) keep the plain name first —
*Invoice parser: …*. If that line asks a question the
mic reopens — 12 s for a first word, then the same radio rules (one *Still
there? <line>* re-prompt when nothing was said, then idle); otherwise it goes
quiet — or, with **Hands-free** on, listens 8 s for a first word after every
reply. A turn that ends with no line at all says *<name> finished.*

**Spoken replies** (the ⚙ sheet, per phone) has three settings:

- **Off** — nothing is read out for a prompt: *Sent to <name>.* shows on the
  strip and the phone stays quiet.
- **Replies to what I said by voice** (the default; an older page's "on"
  loads as this, its "off" as Off) — *Sent to <name>.*, the first-sentence
  confirmation and the `Say:` line are spoken for a **dictated** prompt. A
  **typed** prompt asks for no `Say:` line, so its reply is not read out
  either: the strip just says *Your turn* when the turn ends.
- **Everything** — every session's finished turn is read: the spoken line
  the `session.activity_changed` (`new: "idle"`) / `session.turn_ended`
  frame carries, from any session, once per reply (the two frames carry the
  same line; a new turn clears it), and only when it is **news** (below), with
  where it is from first unless the phone is on that session. It works with voice **off** too: the events socket stays open
  while the page is visible (one connection, push — cheaper than polling
  every session's `GET /voice`; a turn replayed after a reconnect is as old
  as its frame, so the one-minute rule below drops the stale ones), and the
  first tap anywhere on the page unlocks speech. Every prompt the phone
  sends asks for a `Say:` line — typed ones too: with voice off a typed
  prompt goes through `POST /send {text, dialog_safe: true, say: true}`
  (a dialog on screen, or a failed send, types it into the terminal as
  before), and so do the ask sheet's answers. Prompts typed anywhere else —
  straight into a terminal, on the desktop — ask for it through the
  server's **say-all** marker: switching *to* Everything posts
  `POST /api/voice/say-all {on: true}`, switching away from it
  `{on: false}` (Off ↔ Replies posts nothing); on load, a
  `GET /api/voice` `say_all` that disagrees with this phone's choice is set
  to it with one POST (an older server without `say_all` is left alone). A
  403 (a device that is not the owner) is said once on the status line and
  the choice stays.

**In continuous listening** a line does not wait for the listen to end: the
Say line of the turn you sent, another session's line, an ask read-out, a
router line all play **as soon as they are ready**, over the listen — the
recorder deaf for the line (its blocks skipped, the phrase under way not
ended) and no word starting in the 150 ms after it (the audio kept — a word
that starts right away loses nothing; the phone's own recognizer, which cannot be
deafened, has what it reports meanwhile dropped), and listening goes on
after it. Only **mid-sentence** — words in the last 1.5 s and a voice in
the last 250 ms — does the queue hold, until the quiet. A tap on the mic
while such a line plays cuts it (barge-in) and the listen goes on; the
reply's own answer-listen and hands-free do not apply (it never stopped
listening). A listen that is not continuous still holds the queue.

**Spoken confirmations** (the ⚙ sheet, per phone, under Spoken replies; on
by default): short lines in the configured voice for what *you* did —
*Sent.* after *over* (as the Enter is pressed; listening goes on), *Sent.
Over and out.* after *over and out* (*Over and out.* when nothing was in the
box), *Cleared.* after *disregard*; a stop phrase says nothing back (the
falling chime is its answer — *Stopped.* was heard as the phone saying stop
back, every time); a session switch says the router's own line (*Now on …*).
They speak even
with Spoken replies **Off** (they are about your action, not the agent's
reply). One voice still: they go through the queue but at its **front**
(after the confirmations already there), are said even while the mic
listens (the recorder deaf for them, as above), and are dropped when 5 s
old by their turn. With the server's voice each confirmation's audio is
kept in memory per voice (backend + its detail) after its first synthesis,
so a slow voice says the second *Sent.* at once; the five lines are made
ahead once — right after the tap that unlocks audio for a voice made on
this machine (`server_ready.tts_backend` `local` / `system`), and for any
other host (it may cost per line) only once a line it made has played. The
phone's own voice says them directly. A phrase heard right after a
confirmation that starts with its words has them taken off (the phone
recognizer's echo). A stop never cuts a confirmation: *Over and out.* and
*Sent. (Over and out.)* are queued before listening
ends, so the order is the line, whole → the falling chime → whatever
waited for the mic (a reply); nothing queued is dropped by a stop (only
*stop talking* drops lines). After *disregard* the phone's own recognizer,
if it ended with the phrase (Android), starts again only once *Cleared.*
is over — its start would take the audio and cut the line.

**Sounds** (the ⚙ sheet, per phone; on by default): a **rising** two-tone
chime (~120 ms) whenever the mic starts listening — a tap, the wake word,
the ask sheet's 🎙, a listen after a question — and a **falling**, softer one
(880 → 660 Hz, ~140 ms) whenever it stops: *over and out*, a stop phrase,
Cancel, the tap that ends it, a hold's release, the window running out, a
refused or failed microphone. *Over* (send, listen on) plays none — *Sent.*
is the cue. Both are made from the page's AudioContext (no asset); the
recorder skips them; a chime due while a reply is being said waits for it
to end — the falling one for a confirmation too, after the last one in a
row — and the next line waits for the chime; a line cut short (a tap, Cancel)
takes its waiting chime with it.

**Wake word: say "MindFlock".** **Listen for "MindFlock"** in the ⚙ sheet
(under Spoken replies; off by default, stored with the phone's other voice
choices) lets you start listening without touching the phone: say
*MindFlock* **on its own** and it chimes, the strip says *Yes?*, Android
gives a short tick, and it listens exactly as a tap on the mic does (Tap to
talk, whatever Push to talk says). What wakes it: the word alone, with a
moment of quiet before and after — a lone utterance of roughly ¼ s to
1.6 s. What does not: the word inside a sentence (*"MindFlock is great"*,
*"ask MindFlock to…"*), anything longer than 1.6 s, a click or a cough, and
anything while the phone is listening, recording, sending a clip, or
speaking (and for half a second after it stops). How it works: while voice
mode is on and the page is in front, the phone keeps the microphone open
and watches the sound level itself — **nothing goes to the server** until a
short sound bounded by quiet ends — 350 ms of quiet before it, and only
**200 ms after** — and then, in that very audio block (the page's own
synchronous resampler, no render to wait for), just that clip (16 kHz WAV,
with ~100 ms before it) goes to `POST /api/voice/wake`, which recognises it
with *MindFlock* as the hint and answers whether it was the word alone
(`backend/web/core/wake.py`). A sound less than 350 ms after a clip is
never a new wake (the first was the start of something longer). On
`wake: true` listening starts **at once** — the mic is already open, so the
dictation's stream opens right away — with the chime in the same moment;
the recorder skips the chime's ~230 ms (its length and the audio engine's
latency), so it is never transcribed. The word's end → listening is about
the 200 ms tail + the server's check + the round trip; Diagnostics'
*Wake word* line says *wake 0.6 s*, and so does *Last start* (and the
console with `cs_voice_debug`). At most one check is on its way and one goes
per 1.5 s; a "no" does nothing at all. **The mic indicator:** because the
mic stays open, the phone shows its mic-in-use indicator the whole time,
and the 🎙 button shows a small grey dot (still); the warm-mic grace never gives the mic back while the wake word
is on. It needs a server recognizer (Settings → Voice): if the server has
none ready (409), or is too old for the wake word, or the microphone is
refused, the switch turns itself off and the strip says why. With the
phone's own recognizer (not server recognition) the mic is handed back
while it listens, since that recognizer needs it to itself. **iPhone
limits:** it works only while the page is open in Safari and on screen —
never in the background or with the phone locked (iOS cuts a hidden page's
microphone). Hidden, it stops; back on screen it starts again by itself,
asking for the mic again if iOS cut it. If iOS keeps the page's audio
paused until you touch the screen, the strip says *Tap anywhere to listen
for "MindFlock" again*, and one tap anywhere resumes it. The chime is two
short rising tones played through the page's own audio (made by the tap
that turned voice or the switch on), not a sound file. Diagnostics shows a
**Wake word** line: standby / paused / waiting for a tap, how many checks
and wakes, and what the last check heard.

**One voice at a time.** Everything the phone says — replies, the
first-sentence confirmation, dialog read-outs, *what is it asking*, the
router's lines, another session's ask or turn — goes through **one speech
queue**: strictly one line at a time, in the order they arrived, so two
sessions never talk over each other. The queue holds while the mic is
listening and goes on when it stops; a listen that follows a read-out (an
answer to a question, a dialog, hands-free) starts only once the queue has
drained. **Barge-in:** you can answer before it has finished speaking — a
tap on the mic (or a hold, or the ask sheet's 🎙) while a line is being said
cuts the speaker first, drops that session's remaining lines (another
session's wait for their turn), and keeps the phone's own voice out of the
transcript: the server's recorder starts at once (its stream opens and the
mic comes up under the speaker's tail) but no word may start for 150 ms;
the phone's own recognizer, which cannot be deafened, starts 150 ms later.
The cut line is not said again.
**Stop talking:** say *stop talking* (*be quiet*, *quiet*, *hush*, *shut
up*, *stop reading*, *enough*, *okay stop*…; the router's `{kind: "hush"}`,
the page's own table when it cannot answer) — or tap **Stop talking** in the
voice strip, or the line on the strip, while it speaks (the strip shows for
it even with voice off on *Everything*): what is being said stops, every
queued line goes, nothing is said instead, the strip says *Quiet.* for a
moment, and a mic that was listening goes on listening — never typed into the
box, never a dialog's "no". Plain *stop* still means stop listening. Cancel,
voice off and hiding the page drop every line too. A line still waiting a minute after it arrived is dropped rather than
said late (logged to the console). *Repeat* / *say that again* re-speaks the
last line said. A line
that arrives mid-dictation waits until the page is idle and is dropped once a
new prompt is sent; a line you have already heard is never repeated (the
events compare by text, `GET /voice` by its stamp, and the line on screen
when voice was turned on counts as heard).

**News only.** Nothing is read by itself unless it is news: a reply is news
only when it **ended after the listener could have heard it** — after voice
was turned on or the page came back (loaded, or visible again), after they
last switched to, selected, sent to or heard that session — and **no more
than ten minutes ago**. A prompt sent from the phone moves that line back to
its send: its reply is news even across a hidden page or a switch away. So
turning voice on, *Tap to resume voice*, switching sessions, the 4 s poll,
the events socket (and its replay after a reconnect), coming back to the
page and *Everything* never read a session that has been done for a while;
it is still on the screen, and asking reads it. When a turn ended is the
frame's stamp (less `turn_ended`'s `idle_for`) or the reply file's
(`GET /voice` `ts`), on the phone's clock (the socket's `hello` gives the
skew); a reply with no stamp is news only while a prompt from here is being
worked on. The deep link's *Tap to hear <name>* is asking, so it reads the
reply whatever its age, and so does `mfVoice.sayLast(title)` (with where it
is from) for the router's "what did it say". The rule is one pure function,
`voiceIsNews(endedAt, heardAt, now)` in `mobile.js`, with
`voiceContextLead` for the *In <name>, on …:* lead; the desktop's twins are
`isNews` / `contextLead` in `lib/voiceDesktop.ts`.

**Where the line comes from.** The `Say:` line is asked for **per
prompt**: the page sends `say: true` on `POST /send` only for a prompt that
was dictated (a phrase you said landed in the box) while Spoken replies is
on, so a typed prompt costs no Say tokens. That prompt carries one
instruction asking the session to end its reply with one final line
starting `Say:` — up to three short plain sentences (under about 50 words)
a person can hear, no code, paths or markdown: **first the context** — what
it was working on, the feature or area in plain words, and the project when
more than one could be meant — then what it did or will do, then any
question (*We were working on MindFlock's settings screen, and the voice
install button now sits in the Try-it row. Should I add it to the phone page
too?*), so a listener who was away hears where they are. Claude Code
receives it invisibly from the hook MindFlock installs in the worktree
(nothing shows in the terminal; turning voice on re-pins the hook on a
running session and Claude Code hot-reloads it); Codex and the other CLIs
get the same sentence typed after the prompt. When the turn ends the Stop
hook files the reply and the server lifts the `Say:` line out; with no
`Say:` line it speaks a short digest of the reply instead (code, paths and
URLs dropped, the first sentence and any question kept). The line is written
by the model that did the work and costs no extra call. See
[web-api.md](web-api.md#voice-talking-to-a-session-from-the-phone) for the
routes, the row fields and the event data.

**Voice commands.** The first phrase said into an empty box goes to the
server's router first (`POST /api/voice/route {text, session}`), which
knows every session and can act on what it hears: *switch to <name>* — said
any natural way, *switch to the sitecheck session*, *put me on the login
fix*, *jump over to the settings one* — moves voice there and says so (two
matches: *Which one: a or b?*, and the mic opens for the pick); *start a new
session* asks *In which project?* and *What should it do?* (the mic opens
for each answer), then creates the session, selects it and says so; *what
did it say* reads the session's last reply; *what sessions do I
have* reads them; *what needs me* and *status* read the attention list and
the current session; the bare commands below run on the page. **Anything
else is a prompt and stays with the current session** — the words land in
the box as usual. When the router cannot answer (an older server, no
network, 2 s without a reply) the page's own table decides — the whole
phrase, case and punctuation ignored, with an optional "ok", "okay", "hey"
or "please" in front:

| Say | Does |
|---|---|
| *cancel* · *never mind* · *scratch that* · *forget it* | drops the dictated text |
| *session two* · *go to session 3* · *focus (on) session number four* | moves voice to that row of the Sessions sheet, numbered as shown (the router: `{kind: "switch_index", index}`); past the last row, *There is no session N.* |
| *next session* · *previous session* · *switch to the next one* | one row down / up from the current session's, round the ends (`{kind: "switch_rel", delta: ±1}`) |
| *switch to <name>* · *go to <name>* · *change to* · *jump (over) to* · *take me to* · *put me on* · *can you switch to …* | moves voice to that session — by name or title, the words around it (*the*, *my*, *session*, *one*, *please*; *bot* when it matches nothing) ignored: every word said must name a word of the name, across hyphens and digits (*sitecheck* ↔ sitecheck-bot7, *bot seven* ↔ bot7, *login fix* ↔ login-fix), a plural or one wrong letter allowed; the whole name said wins. A tie asks *Which one: a or b?* (below); a miss says *No session called X. Sessions: …* (after a polite lead — *can you switch to …* — a miss is a prompt); a hit says *Now on <name>, <state>: <task>.* — and its last reply only when its turn ended in the last two minutes — and reads its dialog when it is on one |
| *focus (on) <name>* · *open <name>* · *show (me) <name>* · *select* · *pull up* · *bring up* · *talk to* · *go back to* | the same, but only for a session whose whole name (or title) is said or its start in whole words — *open the readme*, *show me the logs*, *go back to the old approach* stay prompts; with *session* said (*pull up the settings session*) any match counts |
| *the second one* · *number two* · *the last one* · *copy two* · *the one that's working* · *the idle one* | **right after *Which one: a or b?*** (within 30 s): picks one of the sessions read out — by its place, a fragment of its name, or its state. A phrase that picks none is routed as usual and the question is dropped |
| *start a new session* · *I want a new session* · *spin one up* · *create a session to fix the login bug in mindflock* · *new session in sitecheck* | a short conversation: no project named (and more than one known) → *In which project? a, b or c?* — answer *parser*, *the sitecheck repo*, *this one* (the current session's), *the second one*, or the project and the task (*parser, add tests*); no task → *What should it do in <project>?* — the next phrase is the task, as said (*nothing* / *just start it*: none). Then it starts and says *Started <title> in <project>.* *Never mind* drops it (*Okay, no new session.*) |
| *what did it say* · *read it* · *read me the last reply* · *what did <name> say* | the session's last reply (its spoken line), whatever its age: *<name> said: …* (`{kind: "read", say}`) |
| *what is it asking* · *what's it asking (me)* · *what does it want* · *what's the question* | the current session's question: its dialog read out again (and listened for), or the question its last line asked; the router's `{kind: "question", say}` is said name first |
| *what needs me* · *who needs me* · *what needs (my) attention* | the sessions in the bell, attention first, at most three: *<name> is asking: <its line>.* or *<name> is <state word>.* — else *Nothing needs you.* |
| *what's it doing* · *what is it doing* · *status* · *what's happening* · *what's going on* | the state word (*for N minutes* while working) and the session's latest turn snippet |
| *summarize that* · *summarize it* · *summarise* · *summary* | one model call on the server (`POST /voice/condense`, 10–45 s): the newest reply in two sentences — *That took too long.* on a timeout, the route's own sentence on any other refusal |
| *how do I schedule a template?* · *what does autopilot do?* · *where do I turn on the phone page?* · *mindflock, …* · *help: …* | a question about **MindFlock itself** is answered from its docs: *Looking that up…*, then at most two sentences naming the section they come from (*From Fast-track: …*) — the router's `{kind: "help", text}`, answered by `POST /api/voice/help` (a quick model call words the best sections; without one, the best section's first sentences). It is help only when the question names MindFlock or one of its features (sessions, templates, schedules, fences, zones, Fast-track, devices, the phone page, voice, tickets, PRs, worktrees, settings, team runs, peers, extensions, …) and says nothing of the agent's work (*fix*, *bug*, *test*, *file*, *code*, *this*, *my project*…), the session's state (*doing*, *said*, *status*…) or *you* — *how do I fix the login bug?* is a prompt. Said after *mindflock* or *help* it is always help. Not in the offline table: a question is a prompt there |
| *say that again* · *repeat (that)* | the last line again |
| *voice off* · *turn voice off* · *turn off voice* | voice mode off (`POST /voice {on: false}`) |
| *stop talking* · *stop speaking* · *stop reading* · *be quiet* · *quiet* · *hush* · *shut up* · *(that's) enough* · *okay stop* | stops talking: the queue goes, listening goes on (below) — matched in **any** mode, before everything else |
| *stop listening* · *stop* | back to idle |
| *session three yes* · *session 2, option one* · *tell <name> no* · *answer <name> always* | answers **that** session (below) — voice stays where it is |

Anything else is a prompt — and so is any `kind` the page does not know (a
newer server's): the words stay words.

**A switch is not a replay.** Moving to a session by voice says where you
are — *Now on login-fix, idle: Fix login redirect.* (the state word and the
row's `task_headline`) — and reads its last reply only when that is news:
the session is idle and its turn ended (`activity_since`) at most two
minutes ago. A session that finished a while back, or is working on
something new, is not read out again; *what did it say* reads it on demand.
A tap on another session while voice is on moves voice there silently. The
offline table follows the same rule (`voiceSwitchLine`).

**What the router asks back.** *Which one: a or b?* and the new session's
*In which project?* / *What should it do?* carry a `pending` the page keeps
for 30 s (`VOICE_PENDING_MS`) and sends with the next phrase; after saying
the question the mic opens for the answer, as after a tap. Any answer
without a `pending` drops it.

**Control words, anywhere.** At the end of every phrase — and, for a stop
phrase, the moment a streaming recognizer settles it — two kinds of words
act whatever is in the box, and never land in it:

| Say (at the end) | Does |
|---|---|
| *stop recording* · *stop listening* · *stop dictating* · *mic off* · *that's all* · *that is all* · *done talking* · *I'm done talking* | stops listening; the words said before it stay in the box, unsent (*In the box — say "over", or tap Send*), answered by the falling chime alone (no *Stopped.*) — after an *over* (*fix it over and out, that's all*) it sends as *over and out* does. In a dialog too — before *stop* could be read as a *no* |
| *yes* · *no (thanks)* · *always* · *yes and don't ask again* · *option two* · *the second one* · *answer yes* | **only while the session is asking** — a dialog is up, or its last `Say:` line asked a question: answers it. A bare number counts only as the whole phrase (*add two* is words) |

An answer to a **dialog** is the option the dialog's own words pick (the
table under *Dialogs by voice*: *always* only when said, a number never on a
permission prompt), POSTed as below — only with **Approve dialogs by voice**
on, else *Answer it on the screen.* An answer to a **question** is a prompt:
it goes to the session through `POST /send` like any other (with the words
already in the box in front of it), no *over* needed. Said into an empty
box the router decides first: `{kind: "answer", choice, number?, dialog_id,
keys, say?}` is POSTed as is (`POST /answer {keys, dialog_id, by: "user"}` —
*always* is the router's call), `{kind: "answer", index}` (only the row
number) is turned into that row's title and asked again with `session` set
to it, `no_dialog` is said (*<name> isn't asking anything.*), and an answer
with no dialog is the question's prompt. Its `say` is spoken name first.

**Answering another session.** *session three yes*, *session 2, option one*,
*tell parser no thanks*, *answer the login fix always* — the whole phrase,
a row number or a name, then an answer — answer **that** session's dialog
(or question) without moving voice: *Invoice parser: answered yes.* (or
*Sent to Invoice parser.* for a question's answer, the box left as it was),
and voice stays on the session you were talking to. Offline the page reads
that session's dialog itself (`GET /dialog?quiet=1`) and picks the option
with the same rules.

**Dialogs by voice.** When the session lands on a permission dialog
(`clarify`), the page fetches it (`GET /dialog?quiet=1`) and reads it out:
*<name>: <question> It wants to run: <command>. Say yes, no, or always.*
(*Say yes or no.* when the dialog offers no always; a dialog of other choices
lists them — *one: main. two: develop. three: release. Say one, two, or
three.*). It then listens for 12 s. Only option words are accepted, and free
text is **never** typed into a dialog (a stop phrase stops listening, the
dialog left waiting; *what is it asking* reads it again; *session two yes*
answers that other session instead):

| Means | Words |
|---|---|
| yes | yes · yeah · yep · yup · allow · approve · okay · ok · go ahead · do it · sure |
| no | no · nope · deny · reject · don't · cancel · skip · stop |
| always | **only when you say the word** — "always", "don't ask again" or "do not ask again"; never inferred from an enthusiastic yes, and a dialog without an always option takes it as yes |
| a numbered choice | one … nine, first … ninth, the digit, "option two" |

Checked in the order always → number → no → yes. The answer is pinned to the
dialog's id (`POST /answer {keys, dialog_id, by: "user"}`) — *Answered yes.*
— so a prompt that changed underneath is read again rather than mis-answered,
and one that was just answered says *Already answered.* Not understood, or
silence: *I didn't catch that: say yes, no, or always.* — twice, then
*<name> is waiting on you.* A dialog the provider can't parse (a free-text
question) is announced as *<name> is waiting on you: open the screen.*
**Approve dialogs by voice** (Settings → Voice,
`voice.approve_dialogs`, on by default) turns the answering off: the dialog
is still read out, followed by *Answer it on the screen.*, and nothing
listens. The ⚙ sheet shows that setting read-only.

**The ⚙ sheet** — *Voice mode for <session>* on/off, **Push to talk**
(*Tap to talk* / *Hold to talk*), **Spoken replies** (*Off* / *Replies to
what I said by voice*, the default / *Everything*), **Listen for
"MindFlock"** (the wake word, above; off by default),
**Hands-free** (listen
after every reply, not only after questions; off by default), **Speech rate**
0.7–1.6×, a **Voice** picker (the phone's `speechSynthesis` voices, this
language first — iOS exposes only its compact built-in voices), the read-only
approval line, and **Diagnostics**: what this phone can do, stated plainly,
so a silent failure has a name — *Recognition* (available, or *NOT available
— iOS has it only in a Safari tab*), *Speech* and how many voices loaded,
*Mode* (browser tab or home-screen app; iOS / Android), *Wake lock*, *Audio
session*, the *Events socket* state and sequence number, *Backends*
(recognition and voice: which adapter is active — the phone's, or the
server's when Settings say server and `GET /api/voice` reports it ready),
*Built-in engine* (*<platform> — <detail>*, from the `system` object of
`GET /api/voice`; left out when an older server sends none), *Recorder*
(*WAV 16 kHz (ScriptProcessor)*, or *not supported* without Web Audio or a
microphone), *Resampler* (which one made the last clip — *OfflineAudioContext
(the browser's)*, or *low-pass decimator* and why — or which one will),
*Silence stop* (*1100 ms after speech*, and the clip cap), *Recognition
stream* with server recognition (*words as you speak* with the pass time —
*~0.4 s/pass* — or *one clip per phrase*, and the last chunk's time or why
it failed), *AudioContext*
(its state), *Last transcribe* / *Last speak*
(how long the server took and which backend, or why it failed), *Primed*
(whether a tap has unlocked speech output yet), *Push to talk* (tap or
hold), *Replies* (on or off), *Last prompt* (dictated or typed, and whether
it asked for a `Say:` line), *Router* (what `/api/voice/route` last answered
— its kind, with the row for *switch_index* / an answer by number, the step
for *switch_rel*, the choice and whether a dialog was named for *answer* — or
why the local table decided), *Session numbers* (*1 Login fix, 2 Invoice
parser, …* — the Sessions sheet's order, held while it is open), *State*,
and the last error. The choices
are this phone's own (with the rate and the voice, in `localStorage`).
Refreshed every second while the sheet is open.

**Pocket mode.** Nothing on the web can speak while the phone is locked or
the page is in the background (iOS suspends the page within seconds and the
events socket dies in about 30 s), so the push carries the words. With
**Include the agent's words in pushes** on (Settings → Notifications → Phone
alerts; the default) the *has finished* push's body is the session's `Say:`
line and the *needs you: Question* push's body is the dialog's question and
command, so a phone that reads notifications aloud says something useful.
On iPhone: Settings → Notifications → **Announce Notifications**, turn it on
for **ntfy**, and Siri reads the push through AirPods; Android has read-aloud
apps that do the same. Both pushes open the page on the session with
`/m?s=<title>&voice=1`, which offers **Tap to hear <name>** — one tap speaks
the line (or reads the dialog it is on) and listens. A dialog push also
carries one-tap **Yes** / **No** buttons (and **Open**): on Android they fire
from the lock screen, on iOS after unlock; each is a single-use link good for
ten minutes that answers that one dialog and nothing else, and "always" is
never a button. There is **no voice reply from an announcement** — the OS
reserves that for native messaging apps; pocket mode is "Siri reads, you
tap to reply". See [Notifications](#notifications-).

**What is not promised.** A conversation lives while the screen is on: when
the page is hidden it stops listening and speaking, closes its socket and
releases the wake lock; on return it re-locks, reconnects and asks for a
line that arrived meanwhile. Nothing speaks while the phone is locked except
the OS reading a push. Dictation is for intent, not code — the words show
in the box as you speak, nothing goes before an explicit "over", and a stop
phrase or a tap keeps them in the editable box to fix before Send: that is
the mitigation for misheard identifiers. iOS speaks
only through its compact voices and cuts long utterances, so lines are
chunked by sentence with a timeout behind each chunk. Phone recognition
sends audio to Google on Android and to Apple on iOS when no on-device model
exists (the Settings screen says so). With Settings → Voice
set to the server for recognition or speech, the page uses the server's
engines (`POST /api/voice/transcribe`, `POST /api/voice/speak`, audio played
through one `AudioContext`) whenever `GET /api/voice` reports that backend
ready, and the phone's own otherwise; a server that is asked for but not
ready is said once in the strip. **The server's voice speaks sentence by
sentence**, so a slow voice (the owner's own Voice Lab voices take ~8 s to
make a 5 s line on a CPU) starts after its first sentence, not the whole
line: a line is split at `.` `?` `!` `;` (never inside *3.5* or
*note.txt*, never after *e.g.*, *i.e.*, *U.S.*, *Dr.*, *etc.*; a sentence
under 20 characters joins the next, the last one the one before — every
request costs the voice its fixed time, ~1.1 s on Voice Lab, however short
the words); `/api/voice/speak` is asked for sentences 1 and 2 at once and 1
plays when it arrives; from then the clips are asked for **two ahead** of
the one playing (`VOICE_SPEAK_AHEAD`), so a voice that makes one at a time
is never idle between sentences — each plays the moment the one before
ends, back to back. *Speaking…* spans the whole line. Cancel, a hush or a
barge-in stops the sentence playing, drops the rest and aborts the requests
on their way (at most two). A sentence the server cannot say is said by the
phone's voice. The phone's own voice (speechSynthesis) is unchanged.
Diagnostics' *Last speak* line says *first sound 0.9 s (3 sentences)*. The
spoken confirmations (*Sent.*, *Cleared.*, *Over and out.*…) are made ahead and
cached at the first tap for a voice made on this machine or **nearby** — not
only `local` / `system` but any host whose detail names a loopback or
private address, such as Voice Lab at 127.0.0.1:8770 — so the first *Sent.*
is instant; a remote host (it may cost per line) gets them once a line it
made has played.

The page remembers, per browser (`localStorage`): the voice session and
whether voice was on (the next visit offers **Tap to resume voice**), the
speech rate, the chosen voice and Hands-free. The events socket is open only
while voice is on (`?since=<seq>` on reconnect, 1→30 s backoff; a fresh
connect ignores the replayed backlog).

## Addons in the frontend

`GET /api/addons` describes each addon's frontend mounts (`FrontendDescriptor`:
where to render, which JS module, which WebSocket, poll interval).
`core/slots.js` auto-renders a sidebar bar for any addon **not** marked
`builtin_ui` (shown or hidden, like every bar, from Customize → Sidebar) and, for descriptors carrying a `module` URL, dynamically imports
that ES module (served from `static/addons/`) and calls
`window.mindflockAddons[<id>].init(ctx)` — handing it the descriptor plus the
client event bus (`window.mindflock.events`, fed by `WS /api/events`), the
`window.mindflock.sessions()` snapshot accessor, and a toast helper when the
SPA provides one. A module that fails to load is skipped with a console warning.
The hand-wired built-ins (MindFlock, Assistant, Settings, Doctor) are
`builtin_ui: true` with `module: null`; the **notify** addon
(`static/addons/notify.js`, desktop notifications on clarify / PR close /
budget exceeded — rules are data-driven from `GET /api/notify/config`, and the
same rules drive the server-side ntfy push in `web/core/ntfy.py`) is the
reference for this generic path — the full contract lives in
[docs/extensions.md](extensions.md). `slots.js` also feeds the provider list
into the New-session dialog's Program field.

`core/ws-xterm.js` exports `WsXterm`, the shared xterm↔WebSocket bridge used by
addon panes (binary PTY frames in, resize JSON out, auto-reconnect except on
4404/4409/4423).
