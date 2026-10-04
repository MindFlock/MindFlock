/** Map tab — the Code Tree: the session's worktree as a tree, with the agents
 * as birds on it (docs/web-ui.md "Map tab"; the approved prototype in
 * mindflock-prototypes/code-tree).
 *
 * Trunk = repo, branch = folder, leaf = file, roots underground = tests
 * (under the code they test), ground piles = docs / CI / root config. This
 * session is the bird in the accent colour; other live sessions on the repo
 * fly too. A bird nests in the folder it mostly works in and perches on the
 * leaf it touches (dot = read, its colour = edited, bud = planned); gold
 * leaves import what a bird edited, with per-folder "N depend on …" badges.
 * Keep out (red hatched band) and Only here (the rest dims to dusk) are the
 * server's zones — painted from the tree, the Rules card, + Zone or the
 * Zones dialog, never kept client-side. The Session panel keeps the plan
 * loop (Ask for plan, Go, Go — only the planned files), scope requests,
 * breaches and the full lists: the accessible twin of the picture.
 *
 * Cost discipline, because up to nine panes can mount one: nothing mounts
 * until the tab is first opened; nothing polls or draws unless the tab is
 * active and the document visible; the layout search runs in a Web Worker and
 * is cached per repo + data (IndexedDB), warm-started from the previous layout
 * so a small change doesn't reshuffle the tree; snapshot, live state, model
 * and camera live in module-level caches so re-activating paints instantly.
 *
 * No window.prompt/confirm/alert anywhere: they are silent no-ops in the
 * Electron app. Every input is an inline DOM row. */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ApiError } from "../../api/client";
import type { CodeMapLive, CodeMapSnapshot, FeedRecord, PlanItem, RedZone, RedZonePreview, SearchItem } from "../../api/types";
import { refreshInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { errMsg } from "../../lib/format";
import { instances } from "../../lib/sessionActions";
import {
  autoMode,
  blastFrom,
  blastRows,
  classifyPath,
  currentBreaches,
  effectiveTests,
  exemptSet,
  feedState,
  findPattern,
  guardPill,
  isGreen,
  isGreenDeny,
  isTestPath,
  liveAllowState,
  planProgress,
  reverseIndex,
  scopeRequests,
  sessionsOnWorktree,
  snapStale,
  zoneAddTell,
  zoneDoc,
  type RevIndex,
  type ZoneClass,
} from "../../lib/codemap";
import {
  addZone,
  allowPath,
  askPlan as apiAskPlan,
  fetchFileView,
  fetchLive,
  fetchSnapshot,
  goPlan,
  previewZone,
  removeZone as apiRemoveZone,
  setExempt,
  waiveZone as apiWaiveZone,
  type GoResult,
} from "../../lib/codemapApi";
import { markCodemapSeen, serverNow } from "../../lib/codemapSeen";
import { cachedModel, layoutModel, LAST_INFO } from "../../lib/codetree/engine";
import { rawFromSnapshot } from "../../lib/codetree/input";
import { LiveBirds } from "../../lib/codetree/live";
import { isUnder, type Model, type RawRepo, type TFile, type TNode } from "../../lib/codetree/model";
import { patternFor, treeZones } from "../../lib/codetree/zones";
import { tailPath } from "../../lib/codetree/draw";
import type { TZone } from "../../lib/codetree/types";
import { AddZoneRow, newAdd, type AddState } from "./codemap/AddZoneRow";
import { CodeTree } from "./codemap/CodeTree";
import { SearchBox } from "./codemap/SearchBox";
import { SessionPanel, recKey } from "./codemap/SessionPanel";
import type { TreeCtl } from "./codemap/treeCtl";

// --- Module-level state (survives tab switches and pane remounts) ----------

interface Entry {
  snap: CodeMapSnapshot | null;
  live: CodeMapLive | null;
  feed: FeedRecord[];
  since: number;
  skew: number;
  err: string;
  snapAt: number;
}

const ENTRIES = new Map<string, Entry>();
const ENTRY_MAX = 12;
const FEED_KEEP = 400;
const POLL_MS = 2000;
/** A null fingerprint can't be compared, so the snapshot is simply re-read
 * this often instead. */
const NULL_FP_REFRESH_MS = 20000;
/** While the held import graph is partial (its build hit the server's time
 * budget), re-ask this often: each ask resumes the build from the server's
 * per-file memo, so a huge repo converges over a few polls. */
const PARTIAL_REFRESH_MS = 3000;

function entryFor(title: string): Entry {
  let e = ENTRIES.get(title);
  if (e) {
    ENTRIES.delete(title);
    ENTRIES.set(title, e); // LRU bump
    return e;
  }
  e = { snap: null, live: null, feed: [], since: 0, skew: 0, err: "", snapAt: 0 };
  ENTRIES.set(title, e);
  while (ENTRIES.size > ENTRY_MAX) ENTRIES.delete(ENTRIES.keys().next().value!);
  return e;
}

/** The tree per title: the last model on screen (instant repaint), the
 * birds (their flights span polls) and the snapshot → input conversion. */
const MODEL_OF = new Map<string, Model>();
const BIRDS = new Map<string, LiveBirds>();
const RAW = new WeakMap<CodeMapSnapshot, RawRepo>();
const GRAPHS = new WeakMap<CodeMapSnapshot, { index: Map<string, number>; rev: RevIndex; tests: Set<number> }>();

function birdsFor(title: string): LiveBirds {
  let b = BIRDS.get(title);
  if (!b) {
    b = new LiveBirds();
    BIRDS.set(title, b);
    if (BIRDS.size > ENTRY_MAX) BIRDS.delete(BIRDS.keys().next().value!);
  }
  return b;
}

/** Reuse `prev` when `next` is structurally identical — keeps object
 * identities stable across polls so every memo below stays warm. */
function share<T>(prev: T | undefined, next: T): T {
  if (prev === undefined) return next;
  try {
    return JSON.stringify(prev) === JSON.stringify(next) ? prev : next;
  } catch {
    return next;
  }
}

function tellText(told: unknown, reason?: string): { text: string; bad: boolean } {
  if (told === "sent") return { text: "Sent to the agent.", bad: false };
  if (told === "queued") return { text: "Queued — it goes to the agent when this turn ends.", bad: false };
  return { text: "Couldn't reach the agent" + (reason ? ": " + reason : "."), bad: true };
}

/** This session's bird colour: the accent, lifted a little off a dark sky
 * (pressed a little into a light one) so it reads on the tree. */
function birdAccent(): string {
  if (typeof document === "undefined") return "#a08cff";
  const cs = getComputedStyle(document.documentElement);
  const acc = cs.getPropertyValue("--accent").trim() || "#7d56f4";
  const light = document.documentElement.classList.contains("light");
  const m = /^#([0-9a-f]{6})$/i.exec(acc);
  if (!m) return acc;
  const x = parseInt(m[1], 16);
  const mix = (c: number, t: number, k: number) => Math.round(c + (t - c) * k);
  const k = light ? 0.12 : 0.3,
    t = light ? 0 : 255;
  const r = mix(x >> 16, t, k),
    g = mix((x >> 8) & 255, t, k),
    b = mix(x & 255, t, k);
  return "#" + ((r << 16) | (g << 8) | b).toString(16).padStart(6, "0");
}

/** Re-render when the theme / accent / surface flips (bird colours follow). */
function useThemeKey(): number {
  const [k, setK] = useState(0);
  useEffect(() => {
    const mo = new MutationObserver(() => setK((x) => x + 1));
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["class", "data-accent", "data-surface"] });
    return () => mo.disconnect();
  }, []);
  return k;
}

interface Toast {
  /** one line */
  text: string;
  /** the whole story, on hover */
  detail?: string;
  bad?: boolean;
  undo?: () => void;
  ms?: number;
}

// --- The tab --------------------------------------------------------------

export function CodeMapTab({ title, active }: { title: string; active: boolean }) {
  // Nothing mounts (no fetch, no poll) until the tab is first opened: most
  // panes never show their Map, and nine of them may exist.
  const [ever, setEver] = useState(active);
  useEffect(() => {
    if (active) setEver(true);
  }, [active]);
  if (!ever) return null;
  return <CodeMap title={title} active={active} />;
}

function CodeMap({ title, active }: { title: string; active: boolean }) {
  const e0 = entryFor(title);
  const [snap, setSnap] = useState<CodeMapSnapshot | null>(e0.snap);
  const [live, setLive] = useState<CodeMapLive | null>(e0.live);
  const [feed, setFeed] = useState<FeedRecord[]>(e0.feed);
  const [err, setErr] = useState(e0.err);
  const [model, setModel] = useState<Model | null>(MODEL_OF.get(title) || null);
  const [growing, setGrowing] = useState<number | null>(null);
  const [growErr, setGrowErr] = useState("");
  const [drawer, setDrawer] = useState(false);
  const [hiZone, setHiZone] = useState<RedZone | null>(null);
  const [preview, setPreview] = useState<RedZonePreview | null>(null);
  const [previewErr, setPreviewErr] = useState("");
  const [add, setAdd] = useState<AddState | null>(null);
  const [announce, setAnnounce] = useState("");
  const [busy, setBusy] = useState("");
  const [actionMsg, setActionMsg] = useState<{ text: string; bad: boolean } | null>(null);
  const [zoneBusy, setZoneBusy] = useState("");
  const [reqBusy, setReqBusy] = useState<Record<string, string>>({});
  /** "Go — only the planned files" exempted these already-changed files. */
  const [goExempt, setGoExempt] = useState<{ paths: string[]; state: string } | null>(null);
  const [toast, setToast] = useState<Toast | null>(null);
  const themeKey = useThemeKey();
  const reducedMotion = useUi((s) => s.reduceMotion) || prefersReducedMotion();

  const searchRef = useRef<HTMLInputElement | null>(null);
  const addInputRef = useRef<HTMLInputElement | null>(null);
  const ctlRef = useRef<TreeCtl | null>(null);
  const polling = useRef(false);
  const snapLoading = useRef(false);
  const [, setTick] = useState(0);
  const toolbarSig = useRef("");

  // --- Data: snapshot + live poll ------------------------------------------

  const loadSnap = useCallback(async () => {
    if (snapLoading.current) return;
    snapLoading.current = true;
    const en = entryFor(title);
    try {
      const d = await fetchSnapshot(title, en.snap?.fingerprint);
      en.snapAt = Date.now();
      if (d && !("unchanged" in d && d.unchanged)) {
        en.snap = d as CodeMapSnapshot;
        setSnap(en.snap);
      }
      en.err = "";
      setErr("");
    } catch (x) {
      en.snapAt = Date.now();
      if (!en.snap) {
        en.err = errMsg(x);
        setErr(en.err);
      }
    } finally {
      snapLoading.current = false;
    }
  }, [title]);

  const poll = useCallback(async () => {
    if (polling.current) return;
    polling.current = true;
    const en = entryFor(title);
    try {
      const first = en.since === 0 && !en.feed.length;
      const d = await fetchLive(title, en.since);
      const prev = en.live;
      const next: CodeMapLive = {
        ...d,
        changed: share(prev?.changed, d.changed || []),
        zones: share(prev?.zones, d.zones || []),
        plan: share(prev?.plan ?? undefined, d.plan ?? null),
        off_plan: share(prev?.off_plan, d.off_plan || []),
        breaches: share(prev?.breaches, d.breaches || []),
        others: share(prev?.others, d.others || []),
        guard: share(prev?.guard ?? undefined, d.guard ?? null),
        repo: share(prev?.repo ?? undefined, d.repo ?? null),
        exempt: share(prev?.exempt ?? undefined, d.exempt ?? null),
        companions: share(prev?.companions ?? undefined, d.companions ?? null),
        companion_files: share(prev?.companion_files ?? undefined, d.companion_files ?? null),
        feed: [],
      };
      en.skew = (d.now || Date.now() / 1000) - Date.now() / 1000;
      const incoming = d.feed || [];
      if (incoming.length) {
        const seen = new Set(en.feed.map(recKey));
        const fresh = incoming.filter((r) => !seen.has(recKey(r)));
        if (fresh.length) {
          en.feed = en.feed.concat(fresh).sort((a, b) => a.ts - b.ts).slice(-FEED_KEEP);
          en.since = Math.max(en.since, ...fresh.map((r) => r.ts || 0));
          if (!first) {
            // One polite announcement per poll: blocks and breaches are the
            // events a screen-reader user must not miss.
            const green = fresh.filter(isGreenDeny);
            const deny = fresh.filter((r) => r.deny && !r.deny.push && !isGreenDeny(r));
            const pushDeny = fresh.filter((r) => r.deny?.push);
            const br = fresh.filter((r) => r.breach && r.breach.length);
            if (br.length)
              setAnnounce("Zone breached: " + br.flatMap((r) => (r.breach || []).map((b) => b.path)).slice(0, 3).join(", "));
            else if (green.length)
              setAnnounce(
                "Scope guard stopped an edit outside the green zone: " + green.map((r) => r.deny!.path).slice(0, 3).join(", ")
              );
            else if (deny.length)
              setAnnounce(
                `Red zone blocked ${deny.length === 1 ? "an edit" : deny.length + " edits"} to ` +
                  deny.map((r) => r.deny!.path).slice(0, 3).join(", ")
              );
            else if (pushDeny.length) setAnnounce("A zone blocked a push: this branch changes a protected file.");
          }
          setFeed(en.feed);
        }
      }
      en.live = next;
      en.err = "";
      setLive(next);
      setErr("");
      // Server clock: the rail chip compares this with the hook-stamped
      // last_block_ts, and the browser may be another machine.
      markCodemapSeen(title, serverNow(en.skew));
      if (
        snapStale({
          snap: en.snap,
          liveFp: next.fingerprint,
          sinceSnapMs: Date.now() - en.snapAt,
          nullFpMs: NULL_FP_REFRESH_MS,
          partialMs: PARTIAL_REFRESH_MS,
        })
      )
        void loadSnap();
    } catch (x) {
      if (!en.live) {
        en.err = errMsg(x);
        setErr(en.err);
      }
      if (x instanceof ApiError && (x.status === 404 || x.status === 409)) {
        en.err = errMsg(x);
        setErr(en.err);
      }
    } finally {
      polling.current = false;
    }
  }, [title, loadSnap]);

  useEffect(() => {
    if (!active) return;
    // Only once a poll has measured the skew; otherwise the first poll stamps
    // it (a browser-clock stamp could run ahead and, the marker being
    // monotonic, hide the next real block).
    const en0 = entryFor(title);
    if (en0.live) markCodemapSeen(title, serverNow(en0.skew));
    void poll();
    if (!entryFor(title).snap) void loadSnap();
    const t = setInterval(() => {
      if (!document.hidden) void poll();
    }, POLL_MS);
    const onVis = () => {
      if (!document.hidden) void poll();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      clearInterval(t);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [active, title, poll, loadSnap]);

  // --- The tree: snapshot → input → layout (worker, cached) -------------------

  const repoLabel = live?.repo?.label || snap?.repo?.label || "";
  const repoKey = snap?.repo?.id || live?.repo?.id || "title:" + title;
  const raw = useMemo(() => {
    if (!snap) return null;
    let r = RAW.get(snap);
    if (!r) {
      r = rawFromSnapshot(snap, repoLabel || title);
      RAW.set(snap, r);
    }
    return r;
  }, [snap, repoLabel, title]);
  useEffect(() => {
    if (!raw || !active) return;
    const hit = cachedModel(repoKey, raw);
    if (hit) {
      MODEL_OF.set(title, hit);
      setModel(hit);
      setGrowing(null);
      return;
    }
    let dead = false;
    setGrowing(0);
    setGrowErr("");
    layoutModel(repoKey, raw, (k) => !dead && setGrowing(k))
      .then((M) => {
        if (dead) return;
        MODEL_OF.set(title, M);
        setModel(M);
        setGrowing(null);
      })
      .catch((x) => {
        if (dead) return;
        setGrowing(null);
        setGrowErr(errMsg(x));
      });
    return () => {
      dead = true;
    };
  }, [raw, repoKey, title, active]);

  // --- Derived model ---------------------------------------------------------

  const now = (live?.now || 0) || Date.now() / 1000;
  const activity = live?.activity || "";
  const fs = useMemo(() => feedState(feed, now, activity), [feed, now, activity]);
  const changed = live?.changed || EMPTY_CHANGED;
  const zones = live?.zones || EMPTY_ZONES;
  // The guard matches ignoring case on a case-insensitive filesystem; the
  // dimming, the zone counts and the find preview must agree with it.
  const ci = !!live?.ci;
  const zdoc = useMemo(
    () => zoneDoc(zones, live?.companions, live?.companion_files, ci),
    [zones, live?.companions, live?.companion_files, ci]
  );
  const classify = useCallback((p: string): ZoneClass => classifyPath(p, zdoc), [zdoc]);
  const greenZones = zdoc.greenZones;
  const green = greenZones.length > 0;
  // Short names for the pill: a zone's name, else its path's last segment.
  const greenNames = useMemo(
    () =>
      Array.from(
        new Set(
          greenZones.map((z) => {
            if (z.name) return z.name;
            const p = z.pattern.replace(/^\/+/, "").replace(/\/(\*\*)?$/, "");
            return /[*?[\]]/.test(p) ? p : tailPath(p); // "web/core", never just "core"
          })
        )
      ),
    [greenZones]
  );
  const exempt = useMemo(() => exemptSet(live?.exempt), [live?.exempt]);
  const plan = live?.plan && live.plan.items && live.plan.items.length ? live.plan : null;
  const planSupported = live ? live.plan_supported !== false : true;
  const clarify = activity === "clarify";
  const mode = autoMode(plan, fs.lastEditTs);
  const skew = entryFor(title).skew;

  const tzones = useMemo<TZone[]>(() => (model ? treeZones(model, zones, ci) : EMPTY_TZ), [model, zones, ci]);
  const agents = useMemo(
    () =>
      model
        ? birdsFor(title).update(model, {
            feed,
            live,
            title,
            accent: birdAccent(),
            serverNow: now,
            viewNow: performance.now() / 1000,
            zones: tzones,
            light: typeof document !== "undefined" && document.documentElement.classList.contains("light"),
          })
        : [],
    // themeKey: the accent colour moved
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [model, feed, live, title, tzones, themeKey]
  );

  const graph = useMemo(() => {
    if (!snap) return null;
    let g = GRAPHS.get(snap);
    if (!g) {
      const files = snap.files || [];
      const index = new Map<string, number>();
      files.forEach((f, i) => index.set(String(f[0] || ""), i));
      g = { index, rev: reverseIndex(snap.edges || [], files.length), tests: effectiveTests(snap) };
      GRAPHS.set(snap, g);
    }
    return g;
  }, [snap]);
  const isTest = useCallback(
    (p: string) => {
      const i = graph?.index.get(p);
      if (i !== undefined && graph) return graph.tests.has(i);
      return isTestPath(p);
    },
    [graph]
  );

  const planItems = plan?.items || EMPTY_PLAN;
  const offPlan = live?.off_plan || EMPTY_STR;
  const othersList = live?.others || EMPTY_OTHERS;
  const breachList = live?.breaches || EMPTY_BREACHES;
  const breachS = useMemo(() => currentBreaches(breachList), [breachList]);
  const changedSet = useMemo(() => new Set(changed.map((c) => c.path)), [changed]);

  // The Session panel's blast list: the same set the tree paints gold — the
  // DIRECT importers of this session's edits (the branch's changes + the feed's).
  const seeds = useMemo(() => {
    if (!graph) return [] as string[];
    const s = new Set<string>();
    if (mode === "plan" && plan) for (const i of plan.items) s.add(i.path);
    else {
      for (const c of changed) s.add(c.path);
      for (const [p, f] of fs.files) if (f.kind === "edit" && f.lastTs) s.add(p);
    }
    return Array.from(s).filter((p) => graph.index.has(p)).sort();
  }, [graph, mode, plan, changed, fs]);
  const dependents = useMemo(() => {
    const out = new Map<string, number>();
    if (!graph || !snap || !seeds.length) return out;
    const idx = seeds.map((p) => graph.index.get(p)!);
    const files = snap.files;
    for (const [i, hops] of blastFrom(idx, graph.rev, 1)) {
      const f = files[i];
      if (f) out.set(f[0], hops);
    }
    return out;
  }, [graph, snap, seeds]);
  const blast = useMemo(() => blastRows(dependents, null, isTest), [dependents, isTest]);
  const blastTests = blast.reduce((s, b) => s + b.tests, 0);

  // The add row / a clicked zone previews its own pattern (server-compiled).
  const previewPattern = add ? add.pattern.trim() : hiZone ? findPattern(hiZone.pattern, hiZone) : "";
  const previewKind: "red" | "green" = add ? add.kind : isGreen(hiZone) ? "green" : "red";
  useEffect(() => {
    if (!active) return;
    if (!previewPattern) {
      setPreview(null);
      setPreviewErr("");
      return;
    }
    let dead = false;
    const t = setTimeout(async () => {
      try {
        const d = await previewZone(title, previewPattern, previewKind);
        if (!dead) {
          setPreview(d);
          setPreviewErr("");
        }
      } catch (x) {
        if (!dead) {
          setPreview(null);
          setPreviewErr(errMsg(x));
        }
      }
    }, 250);
    return () => {
      dead = true;
      clearTimeout(t);
    };
  }, [previewPattern, previewKind, title, active]);

  // Enforced-zone per-zone match counts (the Zones list's "N files").
  const zoneCounts = useMemo(() => {
    const counts = new Map<string, number>();
    if (!snap) return counts;
    const ms: Array<[string, RegExp]> = [];
    for (const z of zones) {
      if (!z.re) continue;
      try {
        ms.push([z.id, new RegExp(z.re, ci ? "i" : "")]);
        counts.set(z.id, 0);
      } catch {
        /* a source this engine rejects — no count */
      }
    }
    for (const f of snap.files || []) for (const [id, r] of ms) if (r.test(f[0])) counts.set(id, (counts.get(id) || 0) + 1);
    return counts;
  }, [snap, zones, ci]);

  const requests = useMemo(
    () => (green ? scopeRequests(feed, (p) => classify(p) === "outside") : []),
    [green, feed, classify]
  );
  // "Allowed ✓" only while the path IS allowed: once its zone is removed, a
  // repeat scope request gets a working Allow button again.
  const shownReqBusy = useMemo(() => liveAllowState(reqBusy, (p) => classify(p) === "outside"), [reqBusy, classify]);

  // One row per tool call: a post/fail supersedes its pre (same id), so a
  // Bash command shows once, as its outcome.
  const recent = useMemo(() => {
    const out: FeedRecord[] = [];
    const seenIds = new Set<string>();
    for (let i = feed.length - 1; i >= 0 && out.length < 12; i--) {
      const r = feed[i];
      if (r.id) {
        if (seenIds.has(r.id)) continue;
        seenIds.add(r.id);
      }
      out.push(r);
    }
    return out;
  }, [feed]);

  const planTs = plan?.ts || 0;
  const goZones = zones.filter((z) => !z.waived && (z.created || 0) > planTs);
  const midFlight = changed.length > 0 || fs.lastEditTs > 0;
  const progress = useMemo(() => {
    const ch = new Set(changed.map((c) => c.path));
    const ed = new Set<string>();
    for (const [p, f] of fs.files) if (f.kind === "edit" && f.lastTs > planTs) ed.add(p);
    return planProgress(planItems, ch, ed);
  }, [changed, fs, planItems, planTs]);

  const inst = instances().find((i) => i.title === title);
  const provider = inst?.program || inst?.provider || "";
  // Sessions sharing this WORKTREE (`folder`), not the repo (`path`).
  const sessionsHere = sessionsOnWorktree(instances(), title);
  const zonesEnforced = zones.filter((z) => !z.waived).length;
  const guard = guardPill(live?.guard, zonesEnforced, provider.split(/\s+/)[0] || "", greenNames);

  // --- Actions ---------------------------------------------------------------

  const showToast = useCallback((t: Toast | null) => setToast(t), []);
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), toast.ms || (toast.undo ? 10000 : 4200));
    return () => clearTimeout(t);
  }, [toast]);

  const applyZones = (zs: RedZone[] | undefined) => {
    if (!zs) return;
    const en = entryFor(title);
    if (en.live) {
      en.live = { ...en.live, zones: zs };
      setLive(en.live);
    }
    refreshInstances();
  };

  const openAdd = (kind: "red" | "green", pattern: string, name = "") => {
    setHiZone(null);
    setAdd(newAdd(kind, pattern, name));
    setTimeout(() => addInputRef.current?.focus(), 0);
  };

  const submitAdd = async (tellOverride?: boolean) => {
    if (!add) return;
    const pattern = (add.done && tellOverride ? add.done.pattern : add.pattern).trim();
    if (!pattern) {
      setAdd({ ...add, err: "Enter a path or pattern." });
      return;
    }
    const kind = add.done && tellOverride ? add.done.kind : add.kind;
    setAdd({ ...add, busy: true, err: "" });
    // The checkbox shows unchecked (and disabled) while the agent waits on a
    // prompt; the request says the same.
    const tell = zoneAddTell(add.tell, tellOverride, clarify);
    try {
      const r = await addZone(title, {
        pattern,
        name: add.name.trim(),
        note: "",
        scope: kind === "green" ? "worktree" : add.scope,
        tell_agent: tell,
        kind,
      });
      applyZones(r.zones);
      setAdd({
        ...add,
        pattern: "",
        name: "",
        busy: false,
        err: "",
        done: {
          kind,
          pattern: r.zone?.pattern || pattern,
          already: r.already_changed || [],
          told: tell ? tellText(r.told, r.reason).text : "",
          exempt: r.exempt || [],
          committedOutside: r.committed_outside || 0,
          exemptState: "",
        },
      });
    } catch (x) {
      setAdd({ ...add, busy: false, err: errMsg(x) });
    }
  };

  const treatAsBreaches = async () => {
    if (!add?.done) return;
    const d = add.done;
    setAdd({ ...add, done: { ...d, exemptState: "busy" } });
    try {
      const r = await setExempt(title, d.exempt, false);
      applyZones(r.zones);
      setAdd({ ...add, done: { ...d, exemptState: "breaches" } });
    } catch (x) {
      setAdd({ ...add, err: errMsg(x), done: { ...d, exemptState: "" } });
    }
  };

  /** Re-add a removed zone exactly as it was (Undo). */
  const readd = async (z: RedZone) => {
    try {
      const r = await addZone(title, {
        pattern: z.pattern,
        name: z.name || "",
        note: z.note || "",
        scope: isGreen(z) ? "worktree" : z.scope === "worktree" ? "worktree" : "repo",
        tell_agent: false,
        kind: isGreen(z) ? "green" : "red",
      });
      applyZones(r.zones);
      showToast({ text: "Undone", ms: 1800 });
    } catch (x) {
      showToast({ text: "Couldn't undo: " + errMsg(x), bad: true });
    }
  };

  const removeZone = async (z: RedZone, undoable = true) => {
    setZoneBusy(z.id);
    try {
      const r = await apiRemoveZone(title, z.id);
      applyZones(r.zones);
      if (undoable)
        showToast({ text: `Removed ${isGreen(z) ? "✓ only here" : "⛔ keep out"} · ${z.name || z.pattern}`, undo: () => void readd(z) });
    } catch (x) {
      showToast({ text: "Couldn't remove that zone: " + errMsg(x), bad: true });
    } finally {
      setZoneBusy("");
    }
  };

  const waiveZone = async (z: RedZone, waived: boolean) => {
    setZoneBusy(z.id);
    try {
      const r = await apiWaiveZone(title, z.id, waived);
      applyZones(r.zones);
    } catch (x) {
      setActionMsg({ text: "Couldn't change that zone: " + errMsg(x), bad: true });
    } finally {
      setZoneBusy("");
    }
  };

  /** Paint a rule from the tree (a tool click or a card button): one POST; the
   * tree draws it from the server's answer. Undo removes it again. */
  const paint = async (tool: "keep" | "only", target: { node?: TNode | null; file?: TFile | null }) => {
    const M = model;
    if (!M) return;
    const node = target.file ? null : target.node || null;
    const file = target.file || null;
    // clicking a branch that already carries this rule takes it off (a toggle)
    const same = tzones.find((t) => t.type === tool && !t.waived && (node ? t.node === node : file ? t.file === file.id : false));
    if (same) {
      void removeZone(same.z);
      return;
    }
    if (tool === "keep") {
      const inside = tzones.find((t) => t.type === "keep" && !t.waived && t.node && (node ? node !== t.node && isUnder(node, t.node) : file ? isUnder(file.node, t.node) : false));
      if (inside) {
        showToast({ text: `${node ? node.disp || node.label || node.name : file?.name} is already inside ⛔ keep out · ${inside.label}` });
        return;
      }
    }
    const pat = patternFor(M, { node, file });
    if ("error" in pat) {
      showToast({ text: pat.error, bad: true });
      return;
    }
    const kind = tool === "only" ? "green" : "red";
    const tell = zoneAddTell(kind === "green", undefined, clarify);
    try {
      const r = await addZone(title, { pattern: pat.pattern, name: "", note: "", scope: kind === "green" ? "worktree" : "repo", tell_agent: tell, kind });
      applyZones(r.zones);
      const what = node ? `${node.nFiles} file${node.nFiles === 1 ? "" : "s"}` : "this file";
      // say at once which birds it concerns: a rule only bites at the NEXT edit
      const hit: string[] = [];
      const primary = agents.find((A) => A.ag.primary);
      for (const A of agents) {
        if (A.done || !A.cur) continue;
        // only-here is this worktree's rule: another session (its own worktree) is not bound by it
        if (tool === "only" && !A.ag.primary && !(primary && A.ag.parent === primary.ag.key)) continue;
        const inZ = (n: TNode | null | undefined) => !!n && !!node && isUnder(n, node);
        const at = A.file ? A.file.node : A.nest;
        if (tool === "keep" && (inZ(at) || (file && A.file === file))) hit.push(`${A.ag.name} is working in there — its next edit there is blocked`);
        if (tool === "only" && !(inZ(at) || (file && A.file === file))) hit.push(`${A.ag.name} is outside — its next edit there is blocked`);
      }
      const told = tell ? " · " + tellText(r.told, r.reason).text : "";
      const nEx = r.exempt ? r.exempt.length : 0;
      const ex = nEx ? ` · ${nEx} already-changed file${nEx === 1 ? "" : "s"} kept exempt` : "";
      const zid = r.zone?.id;
      // one line (what was set, who it bites now); the whole story on hover
      const short =
        `${tool === "keep" ? "⛔ Keep out" : "✓ Only here"}: ${pat.label}` +
        (hit.length ? ` · ${hit.length} agent${hit.length === 1 ? "" : "s"} ${tool === "keep" ? "in there" : "outside"} — next edit there blocked` : "") +
        (nEx ? ` · ${nEx} exempt` : "");
      showToast({
        text: short,
        detail: `${tool === "keep" ? "⛔ Keep out" : "✓ Only here"} set on ${pat.label} (${what})${hit.length ? " · " + hit.slice(0, 2).join("; ") : ""}${ex}${told}`,
        undo: zid ? () => void removeZone({ ...(r.zone as RedZone) }, false).then(() => showToast({ text: "Undone", ms: 1800 })) : undefined,
        ms: 12000,
      });
      ctlRef.current?.showRule(node, file);
    } catch (x) {
      showToast({ text: "Couldn't set that rule: " + errMsg(x), bad: true });
    }
  };

  const allow = async (path: string) => {
    setReqBusy((s) => ({ ...s, [path]: "busy" }));
    try {
      const r = await allowPath(title, path);
      applyZones(r.zones);
      setReqBusy((s) => ({ ...s, [path]: "done" }));
    } catch (x) {
      setReqBusy((s) => ({ ...s, [path]: "" }));
      setActionMsg({ text: "Couldn't allow " + path + ": " + errMsg(x), bad: true });
    }
  };

  const goExemptAsBreaches = async () => {
    if (!goExempt) return;
    const paths = goExempt.paths;
    setGoExempt({ paths, state: "busy" });
    try {
      const r = await setExempt(title, paths, false);
      applyZones(r.zones);
      setGoExempt({ paths, state: "breaches" });
    } catch (x) {
      setGoExempt({ paths, state: "" });
      setActionMsg({ text: "Couldn't treat those as breaches: " + errMsg(x), bad: true });
    }
  };

  const askPlan = async () => {
    setBusy("ask");
    setActionMsg(null);
    try {
      const r = await apiAskPlan(title, midFlight ? "remaining" : "plan");
      setActionMsg(tellText(r.told, r.reason));
    } catch (x) {
      setActionMsg({ text: errMsg(x), bad: true });
    } finally {
      setBusy("");
    }
  };

  const go = async (scopeToPlan: boolean) => {
    setBusy(scopeToPlan ? "go-scope" : "go");
    setActionMsg(null);
    setGoExempt(null);
    try {
      const r: GoResult = await goPlan(
        title,
        goZones.map((z) => z.id),
        scopeToPlan
      );
      // scope_to_plan answers with only the zones it CREATED: re-poll for the
      // full effective set rather than replace the list with a subset.
      if (scopeToPlan) {
        void poll();
        refreshInstances();
        // Already-changed files now outside the scope were exempted: say so,
        // with the same one-click "Treat as breaches" as the + Zone row.
        if (r.exempt && r.exempt.length) setGoExempt({ paths: r.exempt, state: "" });
      }
      setActionMsg(tellText(r.told, r.reason));
    } catch (x) {
      setActionMsg({ text: errMsg(x), bad: true });
    } finally {
      setBusy("");
    }
  };

  // --- Navigation ------------------------------------------------------------

  const openDiff = () => useUi.getState().setLastTab(title, "diff");
  const selectPath = (p: string) => {
    if (!ctlRef.current?.revealPath(p)) showToast({ text: `${p} isn't on the tree (yet) — it appears once the map re-reads the worktree.` });
  };
  const onPick = (it: SearchItem) => {
    const c = ctlRef.current;
    if (!c) return;
    const tool = c.v?.tool || "explore";
    if (it.kind === "dir") {
      if (tool !== "explore" && model) {
        const n = model.nodeOf.get(it.path);
        if (n) {
          void paint(tool, { node: n });
          c.setTool("explore");
          return;
        }
      }
      c.selectFolder(it.path);
      return;
    }
    if (tool !== "explore" && model) {
      const f = model.byPath.get(it.path);
      if (f) {
        void paint(tool, { file: f });
        c.setTool("explore");
        return;
      }
    }
    if (c.selectFile(it.path) && it.kind !== "file") showToast({ text: `Flew to ${it.path.split("/").pop()} — it holds the ${it.kind} ${it.name}`, ms: 3500 });
    else if (!c.v || !model?.byPath.has(it.path)) selectPath(it.path);
  };

  // "/" focuses the search from anywhere in the tab (not while typing).
  const onRootKey = (ev: React.KeyboardEvent<HTMLDivElement>) => {
    if (ev.defaultPrevented) return;
    const t = ev.target as HTMLElement;
    const typing = t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT";
    if (ev.key === "/" && !typing) {
      ev.preventDefault();
      searchRef.current?.focus();
    } else if (ev.key === "Escape" && !typing) {
      if (add) {
        setAdd(null);
        ev.preventDefault();
      } else if (drawer) {
        setDrawer(false);
        ev.preventDefault();
      } else if (ctlRef.current?.key(ev.nativeEvent)) ev.preventDefault();
    }
  };

  // --- Render ------------------------------------------------------------------

  // Nothing to show yet and the first reads failed (a session still
  // provisioning answers 409, a restarting server 5xx).
  const failed = !!err && !snap && !live && !model;
  const ctl = ctlRef.current;
  const tool = ctl?.v?.tool || "explore";
  const histN = ctl ? ctl.hist.length : 0;
  const sessionCount = requests.length + breachList.length;
  const primaryExtra: ReactNode =
    plan || requests.length || breachList.length ? (
      <div className="ct-agent-extra">
        {plan && (
          <button type="button" className="ct-link" onClick={() => setDrawer(true)} title="Open the plan: Go, or Go — only the planned files">
            Plan · {plan.items.length} file{plan.items.length === 1 ? "" : "s"} — review
          </button>
        )}
        {requests.length > 0 && (
          <button type="button" className="ct-link warn" onClick={() => setDrawer(true)}>
            {requests.length} ask{requests.length === 1 ? "s" : ""} to edit outside scope — review
          </button>
        )}
        {breachList.length > 0 && (
          <button type="button" className="ct-link bad" onClick={() => setDrawer(true)}>
            {breachList.length} breach{breachList.length === 1 ? "" : "es"} — pushing is blocked
          </button>
        )}
      </div>
    ) : null;
  const info = LAST_INFO.get(repoKey);
  const growNote = model && info && info.how !== "memory" ? `${info.how === "replay" ? "cached" : info.how} layout · ${info.ms} ms` : "";

  const toastNode = toast ? (
    <div className={"ct-toast" + (toast.bad ? " bad" : "")} role="status" aria-live="polite" title={toast.detail || toast.text} onPointerEnter={() => setToast({ ...toast, ms: 60000 })}>
      <span className="tx">{toast.text}</span>
      {toast.detail && <span className="cm-sr">{toast.detail}</span>}
      {toast.undo && (
        <button
          type="button"
          className="cm-btn on"
          onClick={() => {
            const u = toast.undo!;
            setToast(null);
            u();
          }}
        >
          Undo
        </button>
      )}
      <button type="button" className="ct-x" aria-label="Dismiss" onClick={() => setToast(null)}>
        ×
      </button>
    </div>
  ) : null;

  return (
    <div className="cm-root ct-root" onKeyDown={onRootKey}>
      {!failed && (
        <div className="cm-toolbar ct-toolbar">
          <button type="button" className="cm-btn" disabled={!histN} title="Back to where you were (Backspace)" onClick={() => ctl?.goBack()}>
            ←<span className="lbl"> Back</span>
          </button>
          <button type="button" className="cm-btn" disabled={!model} title="Show the whole tree (0 or Home)" onClick={() => ctl?.wholeTree()}>
            ⌂<span className="lbl"> Whole tree</span>
          </button>
          <span className="cm-seg ct-tools" role="group" aria-label="Paint a rule">
            <button
              type="button"
              className={"red" + (tool === "keep" ? " on" : "")}
              aria-pressed={tool === "keep"}
              disabled={!model}
              title="Keep out: fence a folder or file off — agents may read there, every edit is blocked"
              onClick={() => {
                ctl?.setTool("keep");
                setTick((x) => x + 1);
              }}
            >
              ⛔<span className="lbl"> Keep out</span>
            </button>
            <button
              type="button"
              className={"green" + (tool === "only" ? " on" : "")}
              aria-pressed={tool === "only"}
              disabled={!model}
              title="Only here: the one folder agents may edit in; everything else dims"
              onClick={() => {
                ctl?.setTool("only");
                setTick((x) => x + 1);
              }}
            >
              ✓<span className="lbl"> Only here</span>
            </button>
          </span>
          <span className="cm-grow" />
          <SearchBox ref={searchRef} title={title} onPick={onPick} />
          <span className={"cm-guard " + guard.cls} title={guard.title}>
            <span className="cm-guard-dot" aria-hidden="true" />
            <span className="lbl">{guard.label}</span>
          </span>
          <button
            type="button"
            className="cm-btn"
            title="Keep agents out of a path — or scope them to only some paths — by typing a pattern"
            onClick={() => (add ? setAdd(null) : openAdd("red", ""))}
          >
            {add ? "Close" : "+ Zone"}
          </button>
          <button
            type="button"
            className={"cm-btn" + (drawer ? " on" : "")}
            aria-pressed={drawer}
            aria-controls={"ct-session-" + title}
            title="The session as lists: plan (Go), scope requests, breaches, blast radius, zones, changes"
            onClick={() => setDrawer(!drawer)}
          >
            Session{sessionCount ? <span className="cm-count bad">{sessionCount}</span> : plan ? <span className="cm-count">plan</span> : null}
          </button>
        </div>
      )}

      {add && !failed && (
        <AddZoneRow
          add={add}
          setAdd={setAdd}
          inputRef={addInputRef}
          preview={preview}
          previewErr={previewErr}
          clarify={clarify}
          repoLabel={repoLabel}
          sessionsHere={sessionsHere}
          quick={[]}
          onSubmit={(t) => void submitAdd(t)}
          onTreatAsBreaches={() => void treatAsBreaches()}
        />
      )}

      <div className="cm-body ct-body">
        {failed ? (
          <div className="cm-note cm-note-err" role="alert">
            <b>Code map unavailable.</b>
            <span className="muted">{err}</span>
          </div>
        ) : (
          <CodeTree
            title={title}
            active={active}
            model={model}
            growing={growing ?? (snap ? null : 0)}
            growErr={growErr}
            files={raw ? raw.files.length : snap?.files?.length || 0}
            agents={agents}
            tzones={tzones}
            zones={zones}
            feed={feed}
            skew={skew}
            changed={changedSet}
            classify={classify}
            reqBusy={shownReqBusy}
            zoneBusy={zoneBusy}
            reducedMotion={reducedMotion}
            ctlRef={ctlRef}
            primaryExtra={primaryExtra}
            primaryTask={inst?.branch ? "branch " + inst.branch : undefined}
            toast={toastNode}
            fileView={(p) => fetchFileView(title, p)}
            onPaint={(t, target) => void paint(t, target)}
            onRemoveZone={(z) => void removeZone(z)}
            onAllow={(p) => void allow(p)}
            onOpenDiff={openDiff}
            onNote={(m) => showToast({ text: m })}
            onChange={() => {
              // the toolbar only shows the armed tool and whether Back has somewhere to go
              const c = ctlRef.current;
              const sig = (c?.v?.tool || "") + "|" + (c ? c.hist.length > 0 : false);
              if (sig !== toolbarSig.current) {
                toolbarSig.current = sig;
                setTick((x) => (x + 1) & 0xffff);
              }
            }}
          />
        )}
        {drawer && !failed && (
          <aside className="cm-side ct-drawer" id={"ct-session-" + title} aria-label="Session details">
            <div className="ct-drawer-head">
              <h3>Session</h3>
              {growNote && <span className="ct-muted" title="How the tree's layout was obtained">{growNote}</span>}
              <button type="button" className="ct-x" aria-label="Close the Session panel" onClick={() => setDrawer(false)}>
                ×
              </button>
            </div>
            <SessionPanel
              m={{
                live,
                mode,
                green,
                greenZones,
                zones,
                zoneCounts,
                breaches: breachList,
                requests,
                recent,
                fs,
                guard,
                provider,
                skew,
                plan,
                planSupported,
                progress,
                offPlan,
                goZones,
                midFlight,
                clarify,
                busy,
                actionMsg,
                goExempt,
                blast,
                blastTests,
                depth: 1,
                seeds: seeds.length,
                graphPartial: !!snap?.graph_partial,
                changed,
                exempt,
                others: othersList,
                zoneBusy,
                reqBusy: shownReqBusy,
                levelName: (p) => p || repoLabel || "repo root",
                classify,
              }}
              a={{
                selectPath,
                openDiff,
                askPlan: () => void askPlan(),
                go: (s) => void go(s),
                removeZone: (z) => void removeZone(z),
                waiveZone: (z, w) => void waiveZone(z, w),
                previewZone: (z) => setHiZone(hiZone?.id === z.id ? null : z),
                openAdd,
                allow: (p) => void allow(p),
                keepGoExempt: () => setGoExempt(goExempt ? { ...goExempt, state: "kept" } : null),
                goExemptAsBreaches: () => void goExemptAsBreaches(),
              }}
            />
            {breachS.size > 0 && <p className="cm-hint">{breachS.size} breached now.</p>}
          </aside>
        )}
      </div>
      <div className="cm-sr" aria-live="polite" role="status">
        {announce}
      </div>
    </div>
  );
}

function prefersReducedMotion(): boolean {
  try {
    return !!window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

const EMPTY_CHANGED: CodeMapLive["changed"] = [];
const EMPTY_ZONES: RedZone[] = [];
const EMPTY_PLAN: PlanItem[] = [];
const EMPTY_STR: string[] = [];
const EMPTY_OTHERS: CodeMapLive["others"] = [];
const EMPTY_BREACHES: CodeMapLive["breaches"] = [];
const EMPTY_TZ: TZone[] = [];
