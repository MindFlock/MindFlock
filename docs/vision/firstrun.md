# First run: the vision on screen (as built, 2026-11-03)

Branch `emandel2630/vz-firstrun` (vision round). The goal: a first-time user
gets the picture in one breath from [`docs/vocabulary.md`](../vocabulary.md)
before anything else —

> **MindFlock runs your coding agents side by side.** Each **session** is one
> agent working on its own branch. Its **route** is the stops the work goes
> through until it is merged — the agent, its checks, your OK, the PR. The
> **Sky** shows every route at once, and what **needs you**.

— and never has to learn more than the vocabulary's ~8 nouns to act. A
fresh-eyes judge scored the Sky 3.5/10 for new users ("a 15-step tour and about
20 new nouns before you can do anything", "what is the Sky FOR?").

## One copy of the words

`frontend/src/lib/onboarding.ts` holds the picture once — `VISION_BREATH`
(sentence by sentence), `SKY_LINE`, `GRID_LINE`, `VIEWS_LINE`, `STOP_TEST`,
`VISION_PARTS` — and every surface below reads it, so they cannot drift.

`components/onboarding/VisionPicture.tsx` draws it: three tiny inline-SVG
sketches with captions, **Session** (one agent on its own branch) → **Route**
(the stops until it is merged, the bird on one) → **Needs you** (the bell with a
gold dot). `currentColor` + theme tokens only (`Vision.css`), so light and dark
both work; no image files.

## Screen by screen

### 1. Welcome card (`components/onboarding/WelcomeTour.tsx`)

| | Copy |
|---|---|
| Before | **Welcome to MindFlock** · "MindFlock runs coding agents side by side on this computer, each session on its own branch." · "Three steps and your first agent is running." · [Let's go] |
| After | **Welcome to MindFlock** · **MindFlock runs your coding agents side by side.** · *[picture: Session → Route → Needs you]* · "Each session is one agent working on its own branch. Its route is the stops the work goes through until it is merged — the agent, its checks, your OK, the PR. The Sky shows every route at once, and what needs you." · "Three steps and your first agent is running." · [Let's go] |

### 2. Welcome tour (`lib/tours/welcome.ts`) — 5 steps → 3

| Before | After |
|---|---|
| Three steps to your first agent — "This line always says what to do next; …" | *(gone: the card says it)* |
| ① Install your agent — "MindFlock drives a coding agent for you; one click installs it in about a minute." | ① Install your agent — "One click installs the coding agent MindFlock runs for you; each step ticks itself once it is really done." |
| ② Sign in to your agent | ② Sign in to your agent *(unchanged)* |
| ③ Start your first session — "Pick your repo and say what to do; the agent works on its own branch, so your checkout is untouched." | ③ Start your first agent *(the checklist row's own title)* — "Pick a repo and say what to do — that is a session: the agent works on its own branch, and the Sky shows its route." |
| Your sessions live here — "Each session is a row: Working means …" | *(gone: the empty Grid/Sky say where it shows up)* |

So: one card, then three coach marks, and the third is the action.

### 3. Get set up checklist (`components/setup/SetupChecklist.tsx`)

| Step | Before | After |
|---|---|---|
| ③ hint | "Pick a repo folder and say what to do." | "Pick a repo folder and say what to do. That is a session: one agent on its own branch, its route in the Sky." |
| ⑤ hint | "Turn tickets from Jira, Linear, Shortcut, Asana or GitHub Issues into sessions." | "Let work arrive by itself in Intake: tickets from Jira, Linear, Shortcut, Asana or GitHub Issues start as sessions." |

**"Setup 2 of 3 stays after my session is running"** (the judge) — fixed in
`setupModel.ts` + `SetupChip.tsx`. The server marks `repo` done on the first
corroborated *working* reading, but `agent_login` only on a doctor run — so a
user who signed in inside the session's own terminal kept "Setup 2 of 3" beside
a working agent, and the chip only re-read `/api/setup` when the grid went from
empty to not. Now `agentWorked(instances)` (any session `activity ===
"working"`) is sticky for the page: the chip counts the three required steps as
done (`setupCount(status, worked)` / `chipLabel(status, worked)`), hides itself
unless optional steps remain, and re-reads the checklist once with a doctor
re-probe (`refreshSetup(true)`) so the server catches up.

### 4. Sky tour (`lib/tours/sky.ts`) — 5 steps, first = the Sky one-liner

| # | Before | After |
|---|---|---|
| 1 | Each session is a chain — "A chain is a row of stops: …" | **What the Sky is for** (centred) — "Air traffic control for your agents — every piece of work in flight, the route it's cleared to fly, and the ones that need you." |
| 2 | Add a stop from the shelf — "Drag Agent, Wait for me, Ship, Verify, Wait or Loop onto a chain, …; saved chains drag on whole." | **Each session's route** (the session box) — "The agent comes first, then the stops its work goes through until it is merged; Ctrl+Y shows the same sessions in the Grid." |
| 3 | Wires set the order — "Drag from a stop's round port …" | **+ adds a stop** (`.sky-plus`, else the shelf) — "A stop moves the work, checks the work, or asks you — click + after the last stop, or drag one up from the shelf." |
| 4 | Run it, and answer it — "▶ Run … starts the chain and the bird shows where; …" | **The bird shows where the work is** — "It stands on the stop the work is at; ▶ Run sets the route going and the bird moves along it." |
| 5 | Help is here — "This ? replays the tour or opens the guide, …" | **Needs you** (the bell, `#notif-btn`) — "A stop that waits for your OK, or a route that halts, lands under Needs you on the bell; the sky's ? has the full guide." |

Wiring by port and the help menu moved to the guide's Start here.

### 5. The other tours (vocabulary pass; Verify's is another worker's)

| Tour · step | Before | After |
|---|---|---|
| Intake · 1 | "Intake turns tickets, pull requests and issues into sessions — start by picking your tracker, …" | "Intake is where work arrives by itself — tickets, pull requests, issues; pick your tracker, or Repository for a GitHub repo." |
| Intake · 4 | "Start opens a ticket as a session in the sidebar, …" | "Start opens a ticket as a session — its route shows in the Sky — and Open session › takes you to it." |
| Intake · 5 | "… and, folded below them, how ticket sessions are set up." | "One switch each for tickets, PR review comments and new issues; each source says which route its items take." |
| New · 1 | "Describe one task in plain words — the agent starts on exactly this." | "A session is one agent on its own branch — describe its task in plain words, and the agent starts on exactly this." |
| New · 3 | "Start begins the session now — watch, steer or stop it from its pane." | "Start begins the session now; talk to its agent in the Grid, or follow its route in the Sky." |
| Schedule · 1 | "Schedule keeps templates — a folder, an agent and a task — so a session starts with one click, or by itself." | "Schedule starts sessions and saved routes on a timer — a template is a folder, an agent and a task, ready in one click." |
| Settings · 7 | "Every tour lives here under Help, next to the switch for hints." | "How MindFlock works and every tour live here under Help, next to the switch for hints." |

People, Voice and the other steps already used only vocabulary words. Schedule
step 5 still says "once or on a cron" because the template dialog's own option
says so (pinned together in `scheduleRound5.test.ts`); it changes with the
dialog. A test (`firstRunVision.test.ts`) now fails any tour other than Verify
that teaches a retired word.

### 6. Empty Sky (`components/onboarding/SkyEmpty.tsx`, mounted in `SkyView.tsx`)

| | Copy |
|---|---|
| Before | a blank canvas with one grey line: "No sessions in the sky yet — double-click anywhere, or press the Session chip in the shelf below, to start one." |
| After | a centred card: **Nothing in the Sky yet** · "Air traffic control for your agents — every piece of work in flight, the route it's cleared to fly, and the ones that need you." · *[compact picture]* · "Each session is one agent working on its own branch. Start one, or let work arrive by itself from your tracker." · [**New session**] [Connect a source in Intake] |

Shown only when the sky is truly empty: no sessions, no route on its own, not
filtered (no search, not Needs you / This session), and nothing waiting in the
Intake lane (`useWaiting`, cache only). Otherwise the old plain line draws, as
before. `SkyView.tsx` diff: one import, one `blank` line, and the two
`{empty && <p…>}` renders became `<SkyEmpty blank={blank} className=…>{empty}</SkyEmpty>`.
The card carries `data-no-drag`, so the sky's marquee/pan/double-click leave its
buttons alone.

### 7. Empty Grid (`components/onboarding/GridEmpty.tsx`, mounted in `TerminalGrid.tsx`)

| | Copy |
|---|---|
| Before | "No sessions yet — click here to create one. (Ctrl+N / Alt+N)" |
| After | **No sessions yet** · "Inside the work — your sessions side by side, talk to the agents." · "Each session is one agent working on its own branch. Same sessions, two views: Ctrl+Y switches between the Grid and the Sky." · [**New session**] · `Ctrl+N / Alt+N` |

The whole `#empty` area still opens New session. The first-run card ("Get set
up — Three steps to your first agent") is unchanged.

### 8. Sky guide (`components/flights/SkyGuide.tsx`)

Start here now opens with the picture in one breath (an accent-ruled block),
then "The Sky is air traffic control for your agents — … The test for every
stop: **a stop moves the work, checks the work, or asks you.**", then the five
numbered essentials (Each session has a route · + adds a stop · Wire it · The
bird shows where the work is · Help).

Section titles: What the sky is → **What the Sky is for**; Chains, stops and
wires → **Routes, stops and wires**; Stops and saved chains → **Stops and saved
routes**; Chains on their own, and handoffs → **Routes on their own, and
handoffs**; The intake lane → **The Intake lane**. Every chain → route, frame →
outline, saved chain → saved route, "pauses the chain" → "halts the route",
Verify "works through the branch's checklist" → "checks the session's
behaviors", and the keyboard table's "chain"s. The handoff stop is named
**Start a route** (its backend label is still "Start a chain" until the
copy-rename worker changes `flight_kinds/handoff.py`).

### 9. Settings → General → Help (`components/settings/screens/General.tsx`)

New first row under Help: **How MindFlock works** — "Sessions, routes, the Sky
and what needs you, in one picture." [Show] → `onboarding/HowItWorks.tsx`: the
picture, the breath, "Sky: air traffic control …", "Grid: inside the work …",
"Same sessions, two views: Ctrl+Y …", the stop test, [Got it]. It does not
re-run the checklist tour (Replay on the Welcome tour row still does). Tour
hints: Sky "The sky: every session a line, the bird on its stop, and the Stops
shelf." → "What the Sky is for: each session's route, + to add a stop, the bird,
Needs you."; Intake "Turning tickets, pull requests and issues into sessions." →
"Where work arrives by itself: tickets, pull requests and issues."

### 10. Docs

- `README.md` — "What is MindFlock?" opens with the picture in one breath (+ the
  Grid line); the canonical tagline is untouched (it is deployed in several
  places and changes everywhere or nowhere). "fast-track sets a depth" → "Goes
  as far as sets how far".
- `docs/web-ui.md` — the intro says the picture, the Sky and Grid one-liners and
  what a first launch shows; its Vocabulary table now follows
  `docs/vocabulary.md` (route, saved route, Halt, Goes as far as, behaviors,
  Intake, Schedule) and points at it as the binding list.
- `docs/architecture.md` — intro: "git workspace" → "git worktree", plus the
  picture in the UI's words.

## Open

- The checklist's ③ title comes from the server
  (`backend/web/core/setup_steps.py`: "Start your first agent"), so the tour step
  uses that title; "Start your first session" would read better everywhere.
- `backend/web/core/flight_kinds/handoff.py` still labels its stop "Start a
  chain"; the guide already says "Start a route".
