import { describe, it, expect } from "vitest";
// The To-do extension ships as plain ES modules under the Python package's
// static dir (no build step), so its pure model is imported by relative path
// from outside the TS root. vitest resolves it fine; tsc has no declaration
// for a .js file without allowJs, hence the ignore — the casts below type it.
// @ts-ignore -- untyped ES module outside src/
import * as model from "../../../backend/web/static/extensions/todo/model.js";

interface Item {
  id: string;
  text: string;
  done?: boolean;
  priority?: "high" | "low";
  kind?: "manual_test" | "needs_you";
  session?: string;
  details?: string;
  due?: string;
  closed_at?: number;
  done_at?: number;
  created_at?: number;
  outcome?: "done" | "removed";
}
type Part = { type: "text"; text: string } | { type: "link"; href: string; text: string };
type Block = { type: "text"; parts: Part[] } | { type: "code"; lang: string; code: string; open?: boolean };
interface Move {
  ids: string[];
  priority: string | null;
  changed: boolean;
}
interface Sections {
  needsYou: { session: string; items: Item[] }[];
  rest: { tier: string; items: Item[] }[];
  showTiers: boolean;
  open: number;
  needs: number;
  shown: number;
}
interface Due {
  label: string;
  overdue: boolean;
  soon: boolean;
  title: string;
}

const sortByTier = model.sortByTier as (items: Item[]) => Item[];
const buildSections = model.buildSections as (todos: Item[], q?: string) => Sections;
const orderAfterDrop = model.orderAfterDrop as (order: Item[], dragId: string, anchorId: string, place: "before" | "after") => Move;
const orderAfterStep = model.orderAfterStep as (order: Item[], visible: Item[], id: string, dir: number) => Move | null;
const applyOrder = model.applyOrder as (todos: Item[], ids: string[]) => Item[];
const applyTier = model.applyTier as (todos: Item[], id: string, tier: string) => Item[];
const parseDetails = model.parseDetails as (details: string) => Block[];
const linkify = model.linkify as (text: string) => Part[];
const groupHistoryByDay = model.groupHistoryByDay as (h: Item[], now: Date) => { key: string; label: string; items: Item[] }[];
const dayLabel = model.dayLabel as (epoch: number, now: Date) => string;
const matchesFilter = model.matchesFilter as (item: Item, q: string) => boolean;
const formatDue = model.formatDue as (due: string | undefined, now: Date) => Due | null;
const parseDue = model.parseDue as (due: string) => Date | null;
const toDueString = model.toDueString as (d: Date) => string;
const paneTitle = model.paneTitle as (n: number) => string;
const tierOf = model.tierOf as (item: Item) => string;

const ids = (items: Item[]) => items.map((t) => t.id);
const it_ = (id: string, extra: Partial<Item> = {}): Item => ({ id, text: id, ...extra });
const epoch = (d: Date) => d.getTime() / 1000;

describe("tiers", () => {
  it("reads anything but high/low as normal", () => {
    expect(tierOf(it_("a", { priority: "high" }))).toBe("high");
    expect(tierOf(it_("a"))).toBe("normal");
    expect(tierOf({ id: "a", text: "a", priority: "urgent" as unknown as "high" })).toBe("normal");
  });

  it("sorts by tier and keeps the order inside a tier (stable)", () => {
    const list = [
      it_("n1"),
      it_("l1", { priority: "low" }),
      it_("h1", { priority: "high" }),
      it_("n2"),
      it_("h2", { priority: "high" }),
      it_("l2", { priority: "low" }),
      it_("n3"),
    ];
    expect(ids(sortByTier(list))).toEqual(["h1", "h2", "n1", "n2", "n3", "l1", "l2"]);
    // A large run stays stable too (the engine's sort must not reorder ties).
    const many = Array.from({ length: 40 }, (_, i) => it_("x" + i, i % 3 === 0 ? { priority: "low" } : {}));
    const sorted = sortByTier(many);
    const normals = ids(sorted.filter((t) => !t.priority));
    expect(normals).toEqual(ids(many.filter((t) => !t.priority)));
  });

  it("does not mutate its input", () => {
    const list = [it_("a", { priority: "low" }), it_("b", { priority: "high" })];
    sortByTier(list);
    expect(ids(list)).toEqual(["a", "b"]);
  });
});

describe("buildSections", () => {
  const todos: Item[] = [
    it_("t1"),
    it_("m1", { kind: "manual_test", session: "api" }),
    it_("n1", { kind: "needs_you", session: "web" }),
    it_("m2", { kind: "manual_test", session: "api", priority: "high" }),
    it_("x1", { kind: "needs_you" }),
    it_("t2", { priority: "low" }),
    it_("d1", { done: true, kind: "manual_test", session: "api" }),
  ];

  it("puts open needs-you items first, grouped by session, no-session last", () => {
    const s = buildSections(todos);
    expect(s.needsYou.map((g) => g.session)).toEqual(["api", "web", ""]);
    // Tier order inside a group: the high one first.
    expect(ids(s.needsYou[0].items)).toEqual(["m2", "m1"]);
    expect(s.needs).toBe(4);
    expect(s.open).toBe(6);
  });

  it("splits the rest by tier and shows tier headings only when >1 tier is used", () => {
    const s = buildSections(todos);
    expect(s.rest.map((g) => g.tier)).toEqual(["normal", "low"]);
    expect(s.showTiers).toBe(true);
    const one = buildSections([it_("a"), it_("b")]);
    expect(one.rest.map((g) => g.tier)).toEqual(["normal"]);
    expect(one.showTiers).toBe(false);
  });

  it("filters rows but keeps the unfiltered counts", () => {
    const s = buildSections(todos, "web");
    expect(s.shown).toBe(1);
    expect(s.needsYou.map((g) => g.session)).toEqual(["web"]);
    expect(s.rest).toEqual([]);
    expect(s.needs).toBe(4);
  });

  it("titles the pane with what needs the person", () => {
    expect(paneTitle(0)).toBe("To-do");
    expect(paneTitle(1)).toBe("To-do · 1 needs you");
    expect(paneTitle(2)).toBe("To-do · 2 need you");
  });
});

describe("orderAfterDrop", () => {
  const order = [it_("h1", { priority: "high" }), it_("h2", { priority: "high" }), it_("a"), it_("b"), it_("c"), it_("l1", { priority: "low" })];

  it("moves within a tier without touching priority", () => {
    const r = orderAfterDrop(order, "c", "a", "before");
    expect(r).toEqual({ ids: ["h1", "h2", "c", "a", "b", "l1"], priority: null, changed: true });
    expect(orderAfterDrop(order, "a", "c", "after").ids).toEqual(["h1", "h2", "b", "c", "a", "l1"]);
  });

  it("a drop into another tier's run takes that tier", () => {
    const up = orderAfterDrop(order, "b", "h1", "after");
    expect(up.priority).toBe("high");
    expect(up.ids).toEqual(["h1", "b", "h2", "a", "c", "l1"]);
    const down = orderAfterDrop(order, "h1", "l1", "after");
    expect(down.priority).toBe("low");
    expect(down.ids).toEqual(["h2", "a", "b", "c", "l1", "h1"]);
    const toNormal = orderAfterDrop(order, "l1", "a", "before");
    expect(toNormal.priority).toBe("normal");
    expect(toNormal.ids).toEqual(["h1", "h2", "l1", "a", "b", "c"]);
  });

  it("reports a no-op drop as unchanged", () => {
    expect(orderAfterDrop(order, "a", "b", "before").changed).toBe(false);
    expect(orderAfterDrop(order, "a", "a", "after").changed).toBe(false);
    expect(orderAfterDrop(order, "zz", "a", "after").changed).toBe(false);
  });

  it("Alt+arrow steps through the visible run, crossing a tier boundary", () => {
    const up = orderAfterStep(order, order, "a", -1);
    expect(up && up.priority).toBe("high");
    expect(up && up.ids).toEqual(["h1", "a", "h2", "b", "c", "l1"]);
    expect(orderAfterStep(order, order, "h1", -1)).toBeNull();
    expect(orderAfterStep(order, order, "l1", 1)).toBeNull();
    const down = orderAfterStep(order, order, "a", 1);
    expect(down && down.ids).toEqual(["h1", "h2", "b", "a", "c", "l1"]);
  });
});

describe("local mirrors of the order / priority routes", () => {
  const todos = [it_("a"), it_("done", { done: true }), it_("b"), it_("h", { priority: "high" }), it_("c")];

  it("applyOrder: named first, unnamed keep relative order, tiers win, done trails", () => {
    expect(ids(applyOrder(todos, ["c", "a", "nope"]))).toEqual(["h", "c", "a", "b", "done"]);
  });

  it("applyTier moves the item to the END of its new tier", () => {
    const r = applyTier(todos, "c", "high");
    expect(ids(r)).toEqual(["h", "c", "a", "b", "done"]);
    expect(r.find((t) => t.id === "c")?.priority).toBe("high");
    const back = applyTier(r, "h", "normal");
    expect(ids(back)).toEqual(["c", "a", "b", "h", "done"]);
    expect(back.find((t) => t.id === "h")?.priority).toBeUndefined();
  });
});

describe("parseDetails", () => {
  it("plain text with links", () => {
    const b = parseDetails("Open https://example.com/a?b=1. Then check (see http://x.io/p).");
    expect(b).toHaveLength(1);
    const parts = (b[0] as { parts: Part[] }).parts;
    expect(parts.filter((p) => p.type === "link").map((p) => (p as { href: string }).href)).toEqual([
      "https://example.com/a?b=1",
      "http://x.io/p",
    ]);
    expect(parts.map((p) => p.text).join("")).toBe("Open https://example.com/a?b=1. Then check (see http://x.io/p).");
  });

  it("keeps a balanced bracket inside a URL", () => {
    const parts = linkify("wiki https://en.wikipedia.org/wiki/Foo_(bar) end");
    expect(parts[1]).toEqual({ type: "link", href: "https://en.wikipedia.org/wiki/Foo_(bar)", text: "https://en.wikipedia.org/wiki/Foo_(bar)" });
  });

  it("splits fenced code blocks out, one per fence, with the language", () => {
    const b = parseDetails("Run these:\n```bash\nnpm test\n```\nthen\n```\ncurl -s localhost:9000/api/todo\n```\n");
    expect(b.map((x) => x.type)).toEqual(["text", "code", "text", "code"]);
    expect(b[1]).toEqual({ type: "code", lang: "bash", code: "npm test" });
    expect(b[3]).toEqual({ type: "code", lang: "", code: "curl -s localhost:9000/api/todo" });
  });

  it("keeps a code block's inner blank lines and indentation", () => {
    const b = parseDetails("```\n  a\n\n  b\n```");
    expect(b).toEqual([{ type: "code", lang: "", code: "  a\n\n  b" }]);
  });

  it("an unterminated fence runs to the end, marked open", () => {
    const b = parseDetails("Step 1\n```sh\nmake build\nmake test");
    expect(b[0].type).toBe("text");
    expect(b[1]).toEqual({ type: "code", lang: "sh", code: "make build\nmake test", open: true });
  });

  it("empty and CRLF details", () => {
    expect(parseDetails("")).toEqual([]);
    expect(parseDetails("\n\n")).toEqual([]);
    const b = parseDetails("a\r\n```\r\nx\r\n```");
    expect(b[1]).toEqual({ type: "code", lang: "", code: "x" });
  });
});

describe("history day grouping", () => {
  const now = new Date(2026, 10, 18, 15, 0);

  it("labels today, yesterday, then dates", () => {
    expect(dayLabel(epoch(new Date(2026, 10, 18, 0, 5)), now)).toBe("Today");
    expect(dayLabel(epoch(new Date(2026, 10, 17, 23, 59)), now)).toBe("Yesterday");
    expect(dayLabel(epoch(new Date(2026, 10, 14, 9, 0)), now)).toBe("Sat, Nov 14");
    expect(dayLabel(epoch(new Date(2025, 11, 31, 9, 0)), now)).toBe("Wed, Dec 31, 2025");
  });

  it("groups newest first and keeps the order inside a day", () => {
    const h: Item[] = [
      it_("a", { closed_at: epoch(new Date(2026, 10, 18, 14, 0)) }),
      it_("b", { closed_at: epoch(new Date(2026, 10, 18, 9, 0)) }),
      it_("c", { closed_at: epoch(new Date(2026, 10, 17, 20, 0)) }),
      it_("d", { done_at: epoch(new Date(2026, 10, 10, 8, 0)) }),
    ];
    const g = groupHistoryByDay(h, now);
    expect(g.map((d) => d.label)).toEqual(["Today", "Yesterday", "Tue, Nov 10"]);
    expect(g.map((d) => ids(d.items))).toEqual([["a", "b"], ["c"], ["d"]]);
  });
});

describe("matchesFilter", () => {
  const item = it_("x", { text: "Check the login page", details: "Use the staging URL", session: "auth-fix" });

  it("matches text, details and session, case-insensitively, every term", () => {
    expect(matchesFilter(item, "")).toBe(true);
    expect(matchesFilter(item, "LOGIN")).toBe(true);
    expect(matchesFilter(item, "staging")).toBe(true);
    expect(matchesFilter(item, "auth")).toBe(true);
    expect(matchesFilter(item, "login staging")).toBe(true);
    expect(matchesFilter(item, "login prod")).toBe(false);
  });
});

describe("due", () => {
  const now = new Date(2026, 10, 18, 15, 0); // Wed Nov 18 2026, 15:00

  it("round-trips the datetime-local format", () => {
    const d = parseDue("2026-11-20T09:05");
    expect(d && toDueString(d)).toBe("2026-11-20T09:05");
    expect(parseDue("")).toBeNull();
    expect(parseDue("tomorrow")).toBeNull();
    expect(parseDue("2026-11-20 09:05:00")?.getMinutes()).toBe(5);
  });

  it("formats relative to now and marks overdue", () => {
    expect(formatDue(undefined, now)).toBeNull();
    expect(formatDue("2026-11-18T14:30", now)).toMatchObject({ label: "overdue · 14:30", overdue: true });
    expect(formatDue("2026-11-16T09:00", now)).toMatchObject({ label: "overdue · Nov 16", overdue: true });
    expect(formatDue("2026-11-18T15:40", now)).toMatchObject({ label: "today 15:40", overdue: false, soon: true });
    expect(formatDue("2026-11-18T18:00", now)).toMatchObject({ label: "today 18:00", soon: false });
    expect(formatDue("2026-11-19T09:00", now)).toMatchObject({ label: "tomorrow 09:00" });
    expect(formatDue("2026-11-23T09:00", now)).toMatchObject({ label: "Mon 09:00" });
    expect(formatDue("2026-12-30T09:00", now)).toMatchObject({ label: "Dec 30" });
    expect(formatDue("2027-01-02T09:00", now)?.label).toBe("Jan 2, 2027");
    expect(formatDue("2026-11-19T09:00", now)?.title).toBe("Due Thu Nov 19 09:00");
  });
});
