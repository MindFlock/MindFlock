/** The Assistant's state pill.
 *
 * The Assistant is a Claude window like any other, and the question you have
 * about a Claude window you aren't currently looking at is always the same
 * one: is it still going, is it done, or is it waiting on me? Every session
 * row answers that with a chip; this window used to answer it with nothing at
 * all — its rail row carried its name and a ✕, and the only state anywhere near
 * it was the websocket's (hidden, and about the pipe rather than what is on the
 * other end of it).
 *
 * Same five words, same classes and the same tooltips as a session's pill
 * (`activityChip`), from the same server-side ladder — so "running" here and
 * "running" three rows up mean exactly the same thing.
 *
 * It lives on the rail row and nowhere else, for the same reason a session's
 * chip does: the pane header deliberately carries no status label for any
 * window (`.pane-head .stagechip` is display:none), so a chip added there would
 * be written and never drawn. Mounting it starts the poll and unmounting it
 * stops it (see `useAssistantActivity`) — the row exists exactly while the
 * window is open, so nothing is polled while it is closed.
 */

import { useAssistantActivity } from "../state/queries";
import { activityChip } from "../lib/stage";

export function AssistantChip() {
  const { data } = useAssistantActivity();
  // No reading yet (first poll in flight): show nothing rather than guess.
  // "offline" is a claim — that the agent isn't running — and the one moment
  // it is most likely to be wrong is the moment the window opens, which is
  // exactly when the session is being started.
  if (!data) return null;
  const chip = activityChip(data.activity);
  return (
    <span className={"stagechip " + chip.cls} title={chip.title}>
      {chip.label}
    </span>
  );
}
