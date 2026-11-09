# The voice assistant

Talk to **MindFlock itself** — not to a coding session — in plain words, and
it does what you could do by hand: open a dialog or a Settings screen, switch
between the grid and the sky, go to a session, start a new one, tell a session
something, change a setting, run a session action, or answer a question about
what your sessions are doing or how MindFlock works. It answers out loud, in a
built-in voice, and only when there is something worth hearing.

It follows the way phone and desktop assistants drive apps (Android
AppFunctions, Apple App Intents): MindFlock exposes a **typed catalog of
actions**, and a small fast model picks from it. The model never scripts the
app freely, every action it names is checked before anything happens, and
anything destructive is asked about first.

## How to talk to it

- **Address it**: start a phrase with **"MindFlock"**, **"Hey MindFlock"** or
  **"Flock"** — "MindFlock, open intake", "Hey MindFlock, what needs me?",
  "Flock, turn on the question pop-up". The rest of the phrase goes to the
  assistant. "Flock" alone, the name in the middle of a sentence, "Flock mode
  …" and "MindFlock's …" are ordinary dictation.
- **Assistant mode**: the mic menu's *Talk to MindFlock* (and its hotkey)
  sends every phrase to the assistant until you say "that's all" / "thanks" /
  a stop word, or a minute passes with no speech.
- **No session open**: a phrase that would have been typed into a session
  goes to the assistant when no session is focused.

Your usual voice commands — switching sessions by name or number, scrolling,
interrupting, "stop listening", "scratch that" — keep working exactly as
before and stay instant. Even addressed ("MindFlock, stop listening"), the
mic's own words and the pane actions stay those commands.

## What it can do

| Say | It does |
|---|---|
| "open intake", "show the schedule", "voice settings", "open the command palette" | Opens that dialog or Settings screen — instantly, no model |
| "switch to the sky", "back to the grid", "close this" | Switches the view / closes the open dialog — instantly |
| "go to the parser session" | Focuses that session |
| "start a session on sitecheck to add a README" | Creates the session in that project with that task, and focuses it |
| "tell docs to also update the changelog" | Types and submits that prompt in the session (the voice send path: never into a dialog, never into a paused session) |
| "turn on plain-words questions", "make the assistant chattier" | Changes the setting |
| "commit this session", "open the PR for parser" | Runs that session action |
| "delete the parser session" | Asks "Delete parser?" first — say yes or no |
| "what's my sitecheck session doing?", "what needs me?" | Tells you, from the sessions' live state |
| "how do red zones work?" | Answers from MindFlock's own docs |

When a request is ambiguous ("start a session to write tests" with several
projects, "go to parser" with two parsers) it asks one short question and
keeps listening for the answer.

**Always confirmed first**: deleting, closing or merging a session, and
clicking anything labelled Delete, Remove, Merge, Discard or Reset. Say *yes /
yeah / sure / do it / go ahead* or *no / nope / cancel / never mind*; anything
else drops the question. A question waits 60 seconds.

**Never by voice**: secrets (tokens, API keys) and the settings that open your
machine or weaken a gate — sign-in and access mode, binding and the phone
link, Tailscale trust, peer links, skipping permission prompts, agent
binaries and endpoints. Those stay in Settings. From a device that isn't
allowed to change settings (an anonymous tailnet caller of a device without
the access gate), the assistant can't change them either.

## How much it talks

Settings → Voice → *Assistant talk* (`voice.assistant_talk`):

| Level | Spoken | Shown as a caption with a chime |
|---|---|---|
| **Quiet** | questions, confirmations, errors | everything else |
| **Brief** (default) | also answers ("parser is asking about an edit") | what it did |
| **Chatty** | also what it did on the server ("Started fix-login in sitecheck") | plain navigation |

It never says back what you just asked it to do — no "Opening intake.", no
"Sent." A plain navigation is a chime and a caption, never words.

Its voice is the built-in Kokoro voice (`voice.assistant_voice`, default
*Heart*), the same whatever engine reads your sessions aloud; without Kokoro
installed it uses your normal voice engine.

## Speed

- **Instant path** — "open/show/go to <a dialog or Settings screen>",
  "settings", "the grid / the sky", "close this", a command-palette label:
  matched strictly on the server without a model, in about a millisecond.
- **Everything else** goes to one warm model process the server keeps for the
  assistant (Claude Code's `claude -p --input-format stream-json`, model
  `voice.assistant_model`, default `claude-haiku-5-5`): measured here at about
  0.7 s per turn within a conversation, 1–2 s for the first turn of a new
  one, and each action runs as soon as its line arrives.
- **Docs questions** ("how do red zones work?", "what is a flyway?") are
  answered by the same warm process from the best excerpts of MindFlock's
  docs (the same search the phone's help uses). A plain question about
  MindFlock carries those excerpts in its own turn, so one model turn
  answers it: measured here at 0.9–1.3 s. Anything else the model decides is
  a docs question gets the excerpts as a second turn in the same process:
  about 1.7–1.9 s. (Before, a separate cold model call took 6–7 s.) Only a
  CLI without a streaming chat mode still makes that separate call.
- The process starts when the mic opens or assistant mode turns on
  (`POST /api/voice/assistant/warm`), starts afresh for a new conversation
  (after two minutes without a turn), restarts if it dies, and stops after ten
  idle minutes.

## Privacy

What you say to the assistant, with a short description of your screen and
sessions (session names, states, projects, task headlines, the visible
buttons and commands, your settings' values minus secrets), goes to the
**model of the CLI configured for the assistant**: Settings → Agents →
*Assistant agent* (`coding_cli.assistant_provider`), else your default agent —
the same place your coding sessions' words go. The process has no tools, no
MCP servers and no settings files: it can only answer in text, and MindFlock
decides what that text may do. It runs in MindFlock's neutral one-shot folder,
never in a repository. Words to a session (dictation) never reach the
assistant, and the instant path never reaches any model.

A CLI without a streaming chat mode is asked once per turn instead (slower);
a CLI with no headless mode at all leaves the instant path working and says
the assistant isn't available.

## The protocol (for developers)

`POST /api/voice/assistant` with `{text, conversation, snapshot?, confirm?}`
answers an NDJSON stream of events — `act` (the client does it), `did` (the
server did it), `say`, `ask`, `confirm`, `error`, and always last `done {ms,
model}`; see [web-api.md](web-api.md#the-voice-assistant).
`GET /api/voice/assistant/catalog` lists what it may do.

Inside (`backend/web/core/voice_assistant.py`), the model answers one item
per line, parsed as the text streams so each event goes out the moment its
line is complete:

```
ACT {"do": "open", "target": "intake"}
SAY Two sessions need you: docs and parser.
ASK Which project: sitecheck or MindFlock?
CONFIRM {"do": "session_action", "session": "parser", "action": "delete"} Delete parser?
```

Each action is validated against the catalog
(`backend/web/core/voice_assistant_catalog.py`: settings from the settings
dataclasses, dialogs / Settings screens / session actions mirrored from the
web UI — a test fails when they drift) and the client's snapshot (a command,
a click or a preference must be one the client listed). An action it can't
validate is dropped with an `error`, never guessed at; prose around the lines
is ignored, and a reply that is only prose is said as is. An `ACT help` is
held until the model's turn ends; then the docs excerpts for its question go
back to the same process as a follow-up turn (`build_help_message`), whose
SAY (or prose) is the answer: never asked twice, and the best excerpt's first
sentences are said if it answers nothing. The CLI's flags
belong to its provider (`CodingProvider.stream_chat_argv`).
