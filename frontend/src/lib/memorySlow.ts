/** Why the app is slow when memory is short, in the words the memory notice
 * and the connection card use. When the machine runs low on memory every
 * process waits on the disk to page its memory back in: the agents, tmux and
 * the MindFlock server itself. Typing shows up late, a pane's output lags and
 * the session list stops refreshing. Without a word about it a person reads
 * that as the app being broken; these lines name the cause and the fix. */

/** The memory notice's line about responsiveness. */
export const SLOW_SESSIONS_LINE =
  "Sessions may be slow to show what you type and their new output until memory recovers.";

/** What frees memory fastest (both cards say it the same way). */
export const FREE_MEMORY_HINT = "Putting idle agents to sleep or closing sessions frees memory fastest.";

/** Whether a memory level (GET /api/system/memory's `level`) reads short. */
export function memoryIsShort(level: string | undefined | null): boolean {
  return level === "low" || level === "critical";
}

/** The connection card's header and text while memory reads short, or null
 * when memory is fine (the card keeps its own wording). `pollFailing`: the
 * session list's poll failed twice in a row; otherwise the live update stream
 * dropped. A server stalled on memory is slow, not gone: on the owner's laptop
 * the event loop waited 10 s to 5 min at a time during the 2026-11-10 memory
 * crunch (logs/stacks.log), long enough for both to trip. */
export function memoryConnText(
  level: string | undefined | null,
  pollFailing: boolean
): { head: string; text: string } | null {
  if (!memoryIsShort(level)) return null;
  const head = "MindFlock is slow to respond: memory is " + (level === "critical" ? "critically low" : "low");
  const text = pollFailing
    ? "The server is slow to answer because this computer is short of memory. It usually catches up on its own. " +
      FREE_MEMORY_HINT
    : "Live updates are lagging because this computer is short of memory, and sessions may be slow to respond. " +
      FREE_MEMORY_HINT;
  return { head, text };
}
