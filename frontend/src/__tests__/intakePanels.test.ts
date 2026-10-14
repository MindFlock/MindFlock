/** The Intake panels (tickets / open PRs / open issues) held in the shared
 * query cache — the client half of the stale-while-revalidate contract the
 * server's `_cached_fanout` implements.
 *
 * The dialog unmounts on close and each tab unmounts on switch, so per-screen
 * useState meant every visit started from an empty panel plus a slow upstream
 * fan-out. Pinned: one key→path map, the cache options (keep rows while
 * refetching, re-poll only while the server says stale, outlive the default
 * gcTime), the Refresh escape hatch (`?fresh=1`, client cache bypassed), the
 * warm-up on open, and the one shared `panelNote` that words the banner.
 *
 * Moved from tests/unit/test_frontend_settings_revamp.py, which sliced the
 * built app.js; here the hook and the prefetch are run. The server-side TTL
 * this window must match is pinned in Python against this module's source. */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { ALL_CODE, squash, src } from "./sourceText";

const useQuery = vi.fn((opts: unknown) => ({ opts }));
vi.mock("@tanstack/react-query", async (orig) => ({
  ...(await orig<typeof import("@tanstack/react-query")>()),
  useQuery: (opts: unknown) => useQuery(opts),
}));
const api = vi.fn(async (_path: string) => ({}));
vi.mock("../api/client", () => ({ api: (p: string) => api(p) }));

const { PANELS, usePanelQuery, prefetchIntakePanels, queryClient } = await import("../state/queries");
const { panelNote } = await import("../components/intake/kit");

const count = (hay: string, needle: string) => hay.split(needle).length - 1;
const TICKETS = src("components/intake/TicketsTab.tsx");
const PRS = src("components/intake/PullRequestsTab.tsx");
const ISSUES = src("components/intake/IssuesTab.tsx");

type Opts = {
  queryKey: string[];
  queryFn: () => unknown;
  staleTime: number;
  gcTime: number;
  placeholderData: (prev: unknown) => unknown;
  refetchInterval: (q: { state: { data?: { stale?: boolean } } }) => number | false;
};

beforeEach(() => {
  useQuery.mockClear();
  api.mockClear();
});

describe("one key → path map", () => {
  it("maps each panel to its endpoint", () => {
    expect(PANELS).toEqual({
      tickets: "/api/tickets",
      "github-prs": "/api/github/prs",
      "github-issues": "/api/github/issues",
    });
  });

  it("all three tabs read through usePanelQuery, none keeps the list in state", () => {
    // Type arguments are erased in the bundle; in source they may span lines.
    const reads = (text: string, key: string) =>
      new RegExp('usePanelQuery(?:<[^()]*>)?\\("' + key + '"\\)').test(text);
    expect(reads(TICKETS, "tickets")).toBe(true);
    expect(reads(PRS, "github-prs")).toBe(true);
    expect(reads(ISSUES, "github-issues")).toBe(true);
    // That component state is exactly what the dialog's unmount used to throw away.
    for (const gone of ["setPrs(", "setIssues(", "setTickets(", "setPrsNote(", "setIssuesNote("]) {
      expect(ALL_CODE, gone).not.toContain(gone);
    }
  });
});

describe("usePanelQuery", () => {
  const opts = (key: keyof typeof PANELS) => {
    usePanelQuery(key);
    return useQuery.mock.calls.at(-1)![0] as Opts;
  };

  it("reads its own endpoint, keyed by the panel", async () => {
    const o = opts("github-prs");
    expect(o.queryKey).toEqual(["github-prs"]);
    await o.queryFn();
    expect(api).toHaveBeenCalledWith("/api/github/prs");
  });

  it("keeps the last rows on screen while it refetches", () => {
    const prev = { prs: [1] };
    expect(opts("tickets").placeholderData(prev)).toBe(prev);
  });

  it("re-polls only while the server reports stale", () => {
    const o = opts("github-issues");
    expect(o.refetchInterval({ state: { data: { stale: true } } })).toBe(2_000);
    expect(o.refetchInterval({ state: { data: { stale: false } } })).toBe(false);
    expect(o.refetchInterval({ state: {} })).toBe(false);
  });

  it("matches the server's 20s fresh window and outlives the default gcTime", () => {
    const o = opts("tickets");
    expect(o.staleTime).toBe(20_000);
    // An hour: "cached across opens" has to survive a short break.
    expect(o.gcTime).toBe(60 * 60_000);
  });

  it("Refresh asks the server to skip its cache and bypasses the client one", async () => {
    const fetchQuery = vi.spyOn(queryClient, "fetchQuery").mockResolvedValue({});
    const { refresh } = usePanelQuery("github-prs") as unknown as { refresh: () => Promise<unknown> };
    await refresh();
    const call = fetchQuery.mock.calls[0][0] as unknown as Opts;
    expect(call.queryKey).toEqual(["github-prs"]);
    expect(call.staleTime).toBe(0); // …and not answered from the query cache
    await call.queryFn();
    expect(api).toHaveBeenCalledWith("/api/github/prs?fresh=1");
    // A failed sweep is swallowed: the error is already in query state.
    fetchQuery.mockRejectedValueOnce(new Error("502"));
    await expect(refresh()).resolves.toBeUndefined();
    fetchQuery.mockRestore();
  });

  it("Refresh is the only sweep; a force start just re-lists", () => {
    expect(PRS).toContain('refreshId="gh-prs-refresh"'); // the Refresh button's id
    expect(PRS).toContain("onRefresh={loadOpenPrs}");
    expect(ISSUES).toContain("onRefresh={loadOpenIssues}");
    expect(PRS).toContain("relistPrs");
    expect(ISSUES).toContain("relistIssues");
    expect(TICKETS).toContain("relistTickets");
    // Referenced exactly twice — declared, then handed to the Refresh button.
    for (const sweep of ["loadOpenPrs", "loadOpenIssues"]) expect(count(ALL_CODE, sweep), sweep).toBe(2);
  });
});

describe("warming", () => {
  it("prefetches every panel, fire-and-forget, skipping fresh ones", () => {
    const prefetch = vi.spyOn(queryClient, "prefetchQuery").mockResolvedValue(undefined);
    expect(prefetchIntakePanels()).toBeUndefined(); // never awaited or returned
    const calls = prefetch.mock.calls.map((c) => c[0] as unknown as Opts);
    expect(calls.map((c) => c.queryKey[0])).toEqual(Object.keys(PANELS));
    for (const c of calls) {
      expect(c.staleTime).toBe(20_000); // a no-op while still fresh
      expect(c.gcTime).toBe(60 * 60_000);
    }
    prefetch.mockRestore();
    // Every panel, no hand-kept list.
    expect(squash(src("state/queries.ts"))).toContain(
      "export function prefetchIntakePanels() { for (const key of Object.keys(PANELS)"
    );
  });

  it("the Intake dialog warms panels and their chrome once per open", () => {
    // Keyed on `open` alone — the shell's warm loop (useIntakeWarm) runs on a timer.
    expect(squash(src("components/intake/IntakeDialog.tsx"))).toContain(
      "prefetchIntakePanels(); prefetchIntakeMeta(); }, [open]);"
    );
  });
});

describe("the panel note", () => {
  it("distinguishes a first load from reloading what you can see", () => {
    expect(panelNote({ fetching: true, loaded: false })).toBe("Loading…");
    expect(panelNote({ fetching: true, loaded: true })).toBe("Refreshing…");
    expect(panelNote({ fetching: false, loaded: true })).toBe("");
    // Exactly one place decides "Refreshing…" vs "Loading…"…
    expect(count(ALL_CODE, '? "Refreshing…" : "Loading…"')).toBe(1);
    // …and the three original panels (plus any later adopter) call it.
    expect(count(ALL_CODE, "panelNote({")).toBeGreaterThanOrEqual(3);
  });

  it("a per-source failure outranks the refresh note, even on a stale payload", () => {
    expect(panelNote({ fetching: true, loaded: true, detail: "jira: 401" })).toBe("jira: 401");
    expect(panelNote({ fetching: true, loaded: true, error: "boom" })).toBe("");
    expect(panelNote({ fetching: true, loaded: true, error: "boom", detail: "jira: 401" })).toBe("jira: 401");
    // The tickets panel no longer passes per-source errors in as detail: a
    // source's failure is said ONCE, in its own group, in words, by name.
    expect(TICKETS).not.toContain('(sourceLabels[s] || s) + ": " + e');
    expect(TICKETS).toContain("friendlySourceError(label, srcError)");
  });

  it("a failed refetch is a banner, never an emptied panel", () => {
    expect(PRS).toContain("Could not list PRs: ");
    expect(ISSUES).toContain("Could not list issues: ");
    expect(TICKETS).toContain("Could not list tickets: ");
  });
});
