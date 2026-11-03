# Closing and reopening a session: its chain comes back

Closing a session cancels its chain at once (`flight_driver.on_session_removed`,
reason `flights.SESSION_REMOVED` = "its session was deleted"). The cancel has to
be immediate: titles come straight back, and a new session that only reuses the
title must never pick up the old chain's cursor.

Undoing the close (Recently closed → Reopen, Ctrl+Z, the close toast's Undo:
all `POST /api/recently-closed/<id>/reopen`) brings the chain back:

- **On close**, `recently_closed._record_closed` stores
  `flights.removal_snapshot(title)` with the entry as `entry["flight"]`:
  `{id, state, reason}` of the session's unfinished chain, taken *before* the
  removal hook cancels it. A landed (`done`) chain is not cancelled by a close,
  so there is nothing to snapshot.
- **On reopen**, `flight_driver.restore_on_reopen(entry["flight"], title)` runs
  `flights.op_restore`: the flight returns to the state it was in (running,
  waiting on you, paused, halted, ready or grounded), with the same cursor and
  step states, the way a pause and Carry on leave them, bound to the reopened
  title (which may be `title-2` when the name was taken meanwhile). History gets
  `resumed · restored when the session was reopened`, and old `finished:` keys
  in `announced` are dropped, so a real finish later is announced again. The
  usual `flight.changed` event makes every client refetch its chains. The reopen
  response carries `flight_restored: <flight id>`.
- **Not restored**: a chain cancelled any other way (by you, by a failure), one
  that had already ended, or one whose reopened title already has another live
  chain. That one is left alone, and the old one stays cancelled with a
  `halted · not restored: another chain is live on the reopened session` note.

Tests: `tests/unit/test_reopen_restores_chain.py`.

## The same round: two Sky dialog fixes

- **A stop's dialog Save works with nothing changed.** A stop that was just
  added (a Wait, say) opens in its dialog with its defaults. Save used to stay
  disabled until something changed. Now Save with nothing changed keeps the stop
  as it is and closes the dialog.
- **A finished chain's stop dialog offers Remove.** The loop pill on a landed
  chain opens its Loop's dialog. That dialog had no Remove, because Remove was
  wired only for a live chain. It now removes the same way the canvas does:
  removing a stop of a finished chain clears that finished chain, and the dialog
  says so ("Remove clears the finished chain.").
