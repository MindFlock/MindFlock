/** Show a group of sessions started together where it lives: its header on
 * the rail. The bell's history rows and the run toasts land here for a group
 * event that isn't about one session or the lead's Thread.
 *
 * Scrolls the header (`li.run-group-head[data-run]`, RunGroupHeader.tsx) into
 * view and pulses it once. A group with no header any more (pruned, or the
 * rail filtered it away) is a quiet no-op — the caller has already closed
 * whatever it came from, and that is the whole answer. */

const FLASH = "rg-flash";
const FLASH_MS = 1600;

export function revealGroup(runId: string | null | undefined): boolean {
  if (!runId || typeof document === "undefined") return false;
  // Compared through the dataset rather than a selector, so no run id can
  // break the query.
  const head = Array.from(document.querySelectorAll<HTMLElement>("li.run-group-head[data-run]")).find(
    (el) => el.dataset.run === runId
  );
  if (!head) return false;
  if (typeof head.scrollIntoView === "function") head.scrollIntoView({ block: "nearest", behavior: "smooth" });
  // Restart the pulse when it is asked for twice in a row.
  head.classList.remove(FLASH);
  void head.offsetWidth;
  head.classList.add(FLASH);
  window.setTimeout(() => head.classList.remove(FLASH), FLASH_MS);
  return true;
}
