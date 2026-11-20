/** A poll that rests while the page is hidden (a minimized window, another
 * browser tab) and catches up once as soon as it is shown again — the rule
 * the Queue, Map and Thread tabs already follow. Returns the stop function. */
type VisDoc = Pick<Document, "hidden" | "addEventListener" | "removeEventListener">;

export function pollWhileVisible(
  run: () => void,
  ms: number,
  doc: VisDoc | undefined = typeof document === "undefined" ? undefined : document
): () => void {
  const tick = () => {
    if (doc?.hidden) return;
    run();
  };
  const t = setInterval(tick, ms);
  const onVis = () => {
    if (!doc?.hidden) run();
  };
  doc?.addEventListener("visibilitychange", onVis);
  return () => {
    clearInterval(t);
    doc?.removeEventListener("visibilitychange", onVis);
  };
}
