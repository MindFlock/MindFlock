/** UI sweep round 2, package Q4 — Intake.
 *
 * #7  The Auto-start tab says when a list can't be read ("Couldn't list PRs:
 *     …" with Retry), instead of "Not set up yet" or "Nothing waiting".
 * #8  A panel the server keeps answering `stale` backs its re-poll off from
 *     2 s to 15 s after three stale answers in a row, and says "Couldn't
 *     refresh" with Retry while it does.
 * #9  Refresh cancels a poll in flight before its ?fresh=1 sweep (fetchQuery
 *     would join the poll and never send it), and the button reads
 *     "Refreshing…", disabled, while the sweep runs.
 * #23 An epic's Start flips its row to "Open lead ›" at once (setQueryData),
 *     and a refused start uses the tickets' wording. */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { code, squash } from "./sourceText";

let queryResult: Record<string, unknown> = {};
const useQuery = vi.fn((opts: unknown) => ({ opts, ...queryResult }));
vi.mock("@tanstack/react-query", async (orig) => ({
  ...(await orig<typeof import("@tanstack/react-query")>()),
  useQuery: (opts: unknown) => useQuery(opts),
}));
let answer: () => unknown = () => ({});
const api = vi.fn(async (_path: string) => answer());
vi.mock("../api/client", async (orig) => ({
  ...(await orig<typeof import("../api/client")>()),
  api: (p: string) => api(p),
}));

const Q = await import("../state/queries");
const { usePanelQuery, panelRefetchMs, refreshPanel, queryClient, PANEL_STALE_STREAK, PANEL_BACKOFF_MS } = Q;
const kit = await import("../components/intake/kit");
const { markEpicStarted } = await import("../components/intake/EpicsTab");

type Opts = {
  queryFn: () => Promise<unknown>;
  refetchInterval: (q: { state: { data?: { stale?: boolean } } }) => number | false;
};
const optsOf = (key: keyof typeof Q.PANELS) => {
  const r = usePanelQuery(key) as unknown as { opts: Opts; stalled: boolean };
  return r;
};

beforeEach(() => {
  useQuery.mockClear();
  api.mockClear();
  queryResult = {};
  answer = () => ({});
});

describe("#8 the stale re-poll backs off", () => {
  it("2 s while stale, 15 s from the third stale answer in a row, none once fresh", () => {
    expect(PANEL_STALE_STREAK).toBe(3);
    expect(PANEL_BACKOFF_MS).toBe(15_000);
    expect(panelRefetchMs(true, 0)).toBe(2_000);
    expect(panelRefetchMs(true, 2)).toBe(2_000);
    expect(panelRefetchMs(true, 3)).toBe(15_000);
    expect(panelRefetchMs(true, 9)).toBe(15_000);
    expect(panelRefetchMs(false, 9)).toBe(false);
    expect(panelRefetchMs(undefined, 0)).toBe(false);
  });

  it("counts the answers its own fetches get, and a fresh one ends the run", async () => {
    const { opts } = optsOf("github-issues");
    const staleData = { state: { data: { stale: true } } };
    // A fresh answer first, so earlier tests' counts don't leak in.
    answer = () => ({ stale: false });
    await opts.queryFn();
    answer = () => ({ stale: true });
    await opts.queryFn();
    await opts.queryFn();
    expect(opts.refetchInterval(staleData)).toBe(2_000);
    await opts.queryFn(); // the third in a row
    expect(opts.refetchInterval(staleData)).toBe(15_000);

    // While it lasts, the hook says so — the "Couldn't refresh" line's flag.
    queryResult = { data: { stale: true } };
    expect(optsOf("github-issues").stalled).toBe(true);
    // Other panels keep their own count.
    expect(optsOf("github-prs").stalled).toBe(false);

    answer = () => ({ stale: false });
    await opts.queryFn();
    expect(opts.refetchInterval(staleData)).toBe(2_000);
    expect(optsOf("github-issues").stalled).toBe(false);
  });

  it("a failed answer counts toward the run too", async () => {
    const { opts } = optsOf("tickets");
    answer = () => ({ stale: false });
    await opts.queryFn();
    answer = () => {
      throw new Error("502");
    };
    for (let i = 0; i < 3; i += 1) await expect(opts.queryFn()).rejects.toThrow("502");
    expect(opts.refetchInterval({ state: { data: { stale: true } } })).toBe(15_000);
    answer = () => ({});
    await opts.queryFn();
  });

  it("the list panel draws the line with a Retry, under the list", () => {
    const html = renderToString(
      createElement(kit.WorkListPanel, { label: "Open issues", onRefresh: () => {}, hint: "h", stalled: true, children: "rows" })
    );
    expect(html).toContain("ik-stalled-refresh");
    expect(html).toContain("Couldn&#x27;t refresh");
    expect(html).toContain(">Retry<");
    expect(html.indexOf("rows")).toBeLessThan(html.indexOf("ik-stalled-refresh"));
    const quiet = renderToString(createElement(kit.WorkListPanel, { label: "x", onRefresh: () => {}, hint: "h", children: "rows" }));
    expect(quiet).not.toContain("ik-stalled-refresh");
  });
});

describe("#9 Refresh really sweeps", () => {
  it("cancels the poll in flight, then fetches ?fresh=1", async () => {
    const order: string[] = [];
    const cancel = vi.spyOn(queryClient, "cancelQueries").mockImplementation(async (f) => {
      order.push("cancel:" + JSON.stringify((f as { queryKey: unknown }).queryKey));
    });
    const fetchQuery = vi.spyOn(queryClient, "fetchQuery").mockImplementation(async (o) => {
      order.push("fetch:" + JSON.stringify(o.queryKey));
      return (o.queryFn as () => Promise<unknown>)();
    });
    const { refresh } = usePanelQuery("github-prs") as unknown as { refresh: () => Promise<unknown> };
    await refresh();
    expect(order).toEqual(['cancel:["github-prs"]', 'fetch:["github-prs"]']);
    expect(api).toHaveBeenCalledWith("/api/github/prs?fresh=1");
    // The add flow's re-sweep goes the same way.
    order.length = 0;
    await refreshPanel("github-issues");
    expect(order).toEqual(['cancel:["github-issues"]', 'fetch:["github-issues"]']);
    cancel.mockRestore();
    fetchQuery.mockRestore();
  });

  it("the Epics refresh cancels its poll first too", () => {
    const text = squash(code("state/queries.ts"));
    expect(text).toContain(
      'await queryClient.cancelQueries({ queryKey: ["epics"] }); return queryClient .fetchQuery({ queryKey: ["epics"], queryFn: () => api<EpicsResponse>("/api/epics?refresh=1")'
    );
  });

  it("the button reads Refreshing…, disabled, while the sweep runs", () => {
    const busy = renderToString(createElement(kit.RefreshButton, { id: "r", onClick: () => {}, busy: true }));
    expect(busy).toContain("Refreshing…");
    expect(busy).toContain("disabled");
    const idle = renderToString(createElement(kit.RefreshButton, { id: "r", onClick: () => {}, busy: false }));
    expect(idle).toContain(">Refresh<");
    expect(idle).not.toContain("disabled");
    // Every list's Refresh is that button, driven by the sweep's promise.
    const KIT = squash(code("components/intake/kit.tsx"));
    expect(KIT).toContain("const [refresh, refreshing] = useBusyAction(onRefresh);");
    expect(KIT).toContain("<RefreshButton id={refreshId} onClick={refresh} busy={refreshing} />");
    const QUEUE = squash(code("components/intake/QueueTab.tsx"));
    expect(QUEUE).toContain("Promise.all([ticketsQ.refresh(), prsQ.refresh(), issuesQ.refresh()])");
    expect(QUEUE).toContain('<RefreshButton id="ik-queue-refresh" onClick={refreshAll} busy={refreshing} />');
    // Epics hand over the promise, not `void` it.
    expect(code("components/intake/EpicsTab.tsx")).toContain("onRefresh={q.refresh}");
  });
});

describe("#7 the Auto-start tab says when a list can't be read", () => {
  const QUEUE = squash(code("components/intake/QueueTab.tsx"));

  it("each section carries its list's error, its staleness and its Refresh", () => {
    expect(QUEUE).toContain('q.error ? "Couldn\'t list " + what + ": " + (q.error.message || "error") : ""');
    for (const [q, what] of [
      ["ticketsQ", "tickets"],
      ["prsQ", "PRs"],
      ["issuesQ", "issues"],
    ]) {
      expect(QUEUE).toContain(`error: errorFor(${q}, "${what}"), stalled: ${q}.stalled, onRetry: ${q}.refresh,`);
    }
  });

  it("an error replaces 'Not set up yet' / 'Nothing waiting', with a Retry", () => {
    expect(QUEUE).toContain(
      ') : s.error ? ( <div className="pr-open-note ik-queue-error"> {s.error} <button type="button" className="linklike" onClick={retry} disabled={retrying}>'
    );
    expect(QUEUE).toContain('{s.broken || s.error ? "" : s.state === "unset"');
    // …and the chip doesn't say "not set up" over it.
    expect(QUEUE).toContain('<StateChip state={s.broken || s.error ? "broken" : s.state}');
    // The backoff line, unless the error already speaks for the section.
    expect(QUEUE).toContain("{s.stalled && !s.error ? <StalledRefresh onRetry={retry} busy={retrying} /> : null}");
  });
});

describe("#23 an epic's Start flips its row at once", () => {
  const data = {
    epics: [
      { source: "sc", id: "1", title: "epic-1", has_session: false },
      { source: "sc", id: "2", title: "epic-2", has_session: false },
    ],
  } as unknown as Parameters<typeof markEpicStarted>[0];

  it("marks only that epic as having its lead, under the lead's title", () => {
    const next = markEpicStarted(data, "sc", "2", "lead-2")!;
    expect(next.epics!.map((e) => [e.id, e.has_session, e.title])).toEqual([
      ["1", false, "epic-1"],
      ["2", true, "lead-2"],
    ]);
    expect(data!.epics![1].has_session).toBe(false); // the cache entry is replaced, not mutated
    expect(markEpicStarted(data, "sc", "1")!.epics![0].title).toBe("epic-1");
    expect(markEpicStarted(undefined, "sc", "1")).toBeUndefined();
  });

  it("the start writes it into the cache and fails in the tickets' words", () => {
    const EPICS = squash(code("components/intake/EpicsTab.tsx"));
    expect(EPICS).toContain(
      'queryClient.setQueryData<EpicsResponse>(["epics"], (d) => markEpicStarted(d, e.source, e.id, r.title));'
    );
    expect(EPICS).toContain('errorPop("Start failed — " + e.name,');
    expect(EPICS).not.toContain("Couldn't start the epic");
  });
});
