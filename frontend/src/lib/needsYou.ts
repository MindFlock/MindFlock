/** "What needs me next?" — the sessions waiting on you, in the bell's own
 * Needs-you order (`needsAttention` over the rail's `attentionItems` and the
 * server's waiting rows: an answer first, then approvals / escalations, then
 * broken, then checks failing, then ready). Ctrl+K N walks it; the palette's
 * empty query lists it first. One list, so the three can never disagree. */

import type { Instance, OutboxResponse, OutboxWaiting } from "../api/types";
import { queryClient } from "../state/queries";
import { attentionItems } from "../components/sidebar/ordering";
import { needsAttention } from "../components/outbox/outbox";
import { selectSession } from "./sessionActions";
import { windowName } from "./windowName";
import { toast } from "./toast";

/** The sessions that need you, bell order, one title each. A group-level row
 * with no session (a budget pause, a ticket line) has nowhere to jump to and
 * is left out. */
export function needsYouTitles(instances: Instance[], waiting: readonly OutboxWaiting[] | undefined): string[] {
  const live = new Set(instances.map((i) => i.title));
  const out: string[] = [];
  for (const r of needsAttention(attentionItems(instances), waiting)) {
    if (r.title && live.has(r.title) && !out.includes(r.title)) out.push(r.title);
  }
  return out;
}

/** The one after `current` in `order`, wrapping; the first when `current`
 * isn't in it; null when nothing needs you. */
export function nextNeedsYou(order: string[], current: string | null | undefined): string | null {
  if (!order.length) return null;
  const at = current ? order.indexOf(current) : -1;
  return order[(at + 1) % order.length];
}

/** The bell's list right now, from the caches the bell itself reads. */
export function needsYouNow(): string[] {
  const rows = queryClient.getQueryData<Instance[]>(["instances"]) || [];
  const outbox = queryClient.getQueryData<OutboxResponse | null>(["outbox"]);
  return needsYouTitles(rows, outbox?.groups?.waiting);
}

/** Ctrl+K N: select the next session that needs you, and say which of how
 * many it is. */
export function jumpToNeedsYou(current: string | null | undefined) {
  const order = needsYouNow();
  const next = nextNeedsYou(order, current);
  if (!next) {
    toast("Nothing needs you right now");
    return;
  }
  selectSession(next);
  toast(windowName(next) + " — " + (order.indexOf(next) + 1) + " of " + order.length + " that need you");
}
