# The prompt queue waits on an orchestrator's workers

Owner report (2026-11-03): *"queue does not seem to work well when we spawn
agents. it is mistaking the waiting state or something for idling."*

## What was going wrong

The queue drain (`backend/web/core/queue_driver.py`, `_drain_one_queue`) types
the next queued prompt once the session's activity reads `idle` and has
settled (4 s for an idle the CLI reported itself, 12 s otherwise). An
orchestrator waiting on its workers reads `idle` for two reasons:

1. **Claude Code backgrounds a long MCP wait, and the turn ends.**
   `wait_for_session` blocks for up to 25 min. After ~2 min Claude Code
   answers the tool call with *"MCP tool "mindflock/wait_for_session" is
   still running after 123s. It was moved to the background as task …"*,
   and the agent usually ends its turn there. The Stop hook writes `idle`.
   The orchestrator's own transcript shows the whole sequence:

   | time (UTC) | event |
   |---|---|
   | 15:37:13 | `wait_for_session(no-bar-views)` |
   | 15:40:38 | the wait is moved to the background |
   | 15:40:40 | Stop hook: the turn ends → `idle` |
   | 15:40:46 | **the queue types the owner's queued prompt** (worker still running) |

   A sitecheck orchestrator's transcript from 2026-11-02/03 shows the same
   background move 42 times.
2. **The instructions tell orchestrators to end their turn.** The MCP
   instructions say *"wait_for_session(titles=[...]) or end your turn
   (reports are typed in)"*. Worker reports are typed in by the mailbox
   lane, but that lane always let a ready queue go first
   (`_queue_wants_turn`). So the queue's prompt jumped ahead of the reports
   it should have followed.

A third case turned up while this was being fixed (reported by the
orchestrator): **a nested `claude -p` writes `idle` into the session that
runs it.** The pre-commit documentarian hook runs `claude -p` inside the
worker's `git commit`. That process inherits `$TMUX_PANE` and loads the
worktree's `.claude/settings.local.json`. Its Stop hook therefore writes the
*outer* session's activity marker (and its thread marker). The worker read
`idle` for minutes while its screen showed `✽ Harmonizing… (3m 54s · ↓ 7.7k
tokens · esc to interrupt)`, and `wait_for_session` returned `reason:
"idle"` early.

## The rule now

### The queue holds while the session waits on its workers

In the drain's idle path (`_track_hold` → `_hold_reason`), the queue holds,
and its idle dwell restarts once the hold lifts, while any of these is true:

- **A live child is unfinished.** Children are the sessions whose `Parent`
  is this session (`lineage.children_of`). A child holds when it is
  - `working` (the activity memo the 4 s state tick keeps);
  - `starting`: its task is still queued in its own prompt queue, or it has
    no reading yet / has not started (bounded by the idle escape below);
  - `idle` with no report, for at most **`_QUEUE_WORKER_IDLE_ESCAPE` = 5 min**;
  - `queued` (held by its worker order), but only while another child is
    still out. A worker held behind a *stopped* (blocked/failed)
    predecessor waits on a person, not on time, so it never holds the queue
    by itself.
- **A child reported less than `_QUEUE_WORKER_GRACE` = 30 s ago.** This lets
  the orchestrator's reaction start first, e.g. the notification from a
  backgrounded `wait_for_session`, which starts a turn without firing a
  UserPromptSubmit hook.
- **A worker's message is still pending in its mailbox.** That means any
  pending `result` (a report) or a pending message from a live child.
  `_queue_wants_turn` answers False while the hold stands, so the mailbox
  lane types the reports first. After that the orchestrator works on them,
  ends its turn, settles, and only then does the queue send.
- **The session is long-polling its own inbox** (`wait_for_message`, i.e.
  `mailbox.waiter_active`).

A child is **finished** when:
- it reported (done, blocked or failed) after the last message its parent
  sent it, so a follow-up ask makes it "out" again; or
- it is gone, paused, offline (closed, stopped or asleep), usage-limited, or
  on a dialog (`clarify`). A dialog needs a person or the orchestrator to
  act, and holding the queue behind it helps no one.

### Escapes, so a stuck child cannot hold the queue forever

- An unreported idle child stops holding after 5 min.
- Offline, limited, paused and clarify children never hold.
- One hold episode is capped at **`_QUEUE_WORKER_HOLD_MAX` = 3 h**. Past it
  the queue drains as before until the hold clears and a new one starts.
- **Send now** in the Queue tab still sends straight away.

### Sessions with no children

These are unchanged. The one new hold that can apply to them is an active
inbox long-poll, and an agent in a long-poll is waiting by definition.

### Cost

Every input is a cached read: the activity memo, the mailbox's cached file,
and the queue store. There are no tmux or CLI probes. The check runs only
for sessions with a queued prompt that are otherwise ready to send. Each
such pass for a session with children still does one `mailbox.fetch` per
child, plus one fetch of up to 200 messages from its own box.
`_queue_wants_turn`, which the mailbox lane calls, repeats that work.

### Known limitations

- **The 3 h cap starts when the drain first sees the hold.** An episode
  begins in `_track_hold`. `queue_hold(title)` on its own treats a hold the
  drain has not tracked yet as starting now.
- **Any pending `result` counts toward the mail hold, whoever sent it.**
  Plain messages count only when they come from a live child.
- **Team-run nudges and Flights wait too.** They go through the prompt
  queue, so a nudge or Flight step queued into a session with live children
  waits on the hold.
- **The hold fails open.** If `_hold_reason` raises, the queue does not
  hold.

### Visible state

The start, change and end of a hold are announced on the existing
`session.queue_changed` event:
- `data.waiting_on`: the child titles;
- `data.waiting`: one line, e.g. "waiting on 2 workers (a, b); 1 worker
  message to type first".

A `[MONITORING] queue for X holds: …` / `… released` line goes to the error
log. `queue_driver.queue_hold(title)` returns the same answer read-only, for
the queue route.

**Follow-up outside this change's fence:** add `"waiting":
_queue_driver.queue_hold(title)` to `server._queue_state_json` /
`_queue_summary` so the Queue tab can show "waiting on N workers". The UI
itself is also left for a follow-up.

### A live turn on screen outranks an idle marker

`agent_state._agent_activity` already captures the visible screen for a CLI
with a dialog parser (Claude). When the marker says `idle` but the bottom 8
non-blank lines carry the provider's live-turn status line
(`working_pane_patterns`: Claude's `esc to interrupt`), the reading is
`working`, with source `status`.

Only the bottom lines are read, because the transcript above can quote the
phrase. This covers:
- the nested `claude -p` case;
- a turn Claude starts on its own after a background task finishes.

The rule is skipped on a limit screen. A `status` reading arms work, like
the CLI's own report: `worked_at` and `last_worked_at` are stamped, and
`worked_evidence` is `"status"`. Team-run nudges and autopilot gating key
on `worked_at`, so they now see this screen-corroborated work. `status` is
not an authoritative source (`_AUTHORITATIVE_SOURCES`), so the settle skip
and the drain's 4 s fast tier do not treat it as a hook report. The rule
runs only where a screen is already captured, i.e. for providers with a
dialog parser. Any TOML provider whose `working_patterns` are used this way
needs patterns that cannot match its input box or footer.

**Root fix, outside this change's fence:**
`backend/providers/activity_markers.py` `hook_command` should not let a
nested CLI write the pane's markers at all. Possible approaches:
- skip the marker/thread write when the payload's `session_id` differs from
  the window's current thread marker and the event is not
  `SessionStart`/`UserPromptSubmit`; or
- have the launcher export the agent's pid and have the hook compare it with
  its parent CLI's pid.

Until then, the nested run also overwrites the thread marker for the length
of the commit, so `claude agents --json` is looked up under the wrong
conversation. The screen rule above covers that window.

## Tests

`tests/unit/test_queue_waits_on_workers.py`:
- an idle orchestrator with running children is not typed into, and the hold
  is announced;
- reports are typed first, then the queue sends after the orchestrator's
  turn;
- `_queue_wants_turn` yields to pending reports;
- the stuck-child escape and the 3 h cap;
- clarify, limit and offline children don't hold;
- a childless session is unchanged;
- order-held children hold only while a sibling runs;
- a follow-up after a report counts the child as out again;
- a worker whose task is still queued is "starting";
- an inbox long-poll holds;
- the live-status-line rule, including that the phrase in the transcript
  doesn't count.
