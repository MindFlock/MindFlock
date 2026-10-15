/** Intake → Tickets: connect a tracker, and the tickets in the states you pick
 * start as sessions — on their own while auto-start is on, or by hand.
 *
 * The tab the other two were modelled on (see ./kit.tsx): master switch, then
 * the tickets the connected sources yielded — each source's heading being where
 * that source is edited (Edit / Fix opens `TicketSourceEditor` under it) — and
 * "+ Add source" (the dialog head) opening the tracker add flow at the top.
 * (Internally a source being edited is still called a "card" in places: the
 * save bookkeeping below predates the move and is unchanged.)
 *
 * Those tickets are grouped **by source, then by workflow state**. Two levels
 * because the state names are the provider's, not ours: a Jira site and a
 * Linear workspace can both have an "In Progress", and flattening them into one
 * bucket (which is what a single level did) merged two different queues under
 * one heading and gave no way to tell which ticket came from where without
 * reading each row's meta line. The bucket *names* stay a global choice —
 * "I don't care about Completed" means it everywhere — while a bucket's
 * open/closed state is per source.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "../../api/client";
import {
  putTicketingSources,
  refreshConfig,
  refreshInstances,
  refreshPanel,
  useAgentChoices,
  usePanelQuery,
  useTicketingCatalog,
  useTicketingSources,
} from "../../state/queries";
import { toast } from "../../lib/toast";
import { errorPop } from "../../lib/errorPop";
import { useUi } from "../../state/store";
import { startTogetherText } from "../../lib/runStart";
import {
  AddFlowShell,
  ArmButton,
  HeadAction,
  SourcePill,
  TestButton,
  ViewMenu,
  useEditing,
  useListFilter,
  WorkGroup,
  WorkItemRow,
  WorkListPanel,
  ageText,
  panelNote,
  reopenIntakeItem,
  repoSlugOf,
  trackerKinds,
  useToggleSet,
  ONLY_REPO,
  REPO_KIND,
  type ItemRoute,
  type ItemWorkspace,
  type TestResult,
} from "./kit";
import {
  TOKEN_URLS,
  filterSummary,
  friendlySourceError,
  friendlyStartError,
  friendlyTestError,
  listNames,
  savedFilter,
  savedLabels,
  stateName,
} from "./vocab";
import { useAutosave } from "./autosave";
import {
  TEST_STATE_LABELS,
  advancedFields,
  advancedHasValues,
  basicFields,
  clearSecretPatch,
  fieldErrorsFrom,
  hasSecret,
  isTestSensitive,
  lastTestOf,
  missingAutoFields,
  putBody,
  requiredMarker,
  savedStates,
  shouldAutoTest,
  sourceTestState,
  splitForSave,
  withLastTest,
  workspaceChoices,
  type FieldErrors,
} from "./sourceForm";
import {
  NO_STATE_BUCKET,
  loadMineOnly,
  loadShownBuckets,
  saveMineOnly,
  saveShownBuckets,
  visibleBuckets,
} from "./buckets";
import { LANE_LABEL, SOURCE_DEPTHS } from "../../lib/autopilot";
import {
  EFFORTS,
  effortOptionLabel,
  effortTitle,
  supportsEffort,
} from "../../lib/effort";
import { useMindflockStatus, useProviderEfforts } from "../../state/queries";
import type { IntakeTabKey, TabProps } from "./IntakeDialog";
import { ticketMatches } from "./search";
import { MergeButton, MergeDrawer } from "./MergeTicket";
import { RepoListField } from "./RepoListField";
import { AddRepoFlow } from "./RepoSources";
import { sourceRepos } from "./repoList";
import { AutoStartLink } from "./AutoStartSwitches";
import type {
  TicketClaimedBy,
  TicketingCatalogEntry,
  TicketingCatalogField,
  TicketingLastTest,
  TicketingSource,
  TicketingWorkflowState,
} from "../../api/types";

// The catalog and source shapes live in api/types, shared with the query cache
// that holds them (state/queries) — the form below renders straight off the
// catalog, so the two must not drift.
type CatalogField = TicketingCatalogField;
type CatalogEntry = TicketingCatalogEntry;
type Source = TicketingSource;

/** Coding-CLI names a source's Agent picker offers, plus the app-wide default
 * shown as the "unset" option's label (so the empty choice is never a mystery). */
interface AgentChoices {
  names: string[];
  fallback: string;
}

/** Stable "nothing yet" identities, so a tab rendering before its caches land
 * doesn't hand its children a new array on every frame. */
const EMPTY_CATALOG: CatalogEntry[] = [];
const EMPTY_AGENTS: AgentChoices = { names: [], fallback: "" };

/** What a person calls a source: its label, else the provider plus whatever
 * tells two of them apart — "Shortcut", "Jira — payments". Never the internal
 * id ("sc"), which is a branch prefix, not a name anyone gave it. */
export function sourceDisplayName(source: Source, catalog: CatalogEntry[]): string {
  const meta = catalog.find((p) => p.id === source.provider) || null;
  const provName = meta?.label || source.provider;
  const detail = (source.label || "").trim();
  // Said once. A label that already names its provider ("Jira — payments") is
  // the whole title; prefixing it again read "jira — Jira — payments".
  // A whole word only: "Linearity board" does not name Linear.
  const namesProvider = [provName, source.provider].some(
    (n) =>
      !!n &&
      detail.toLowerCase().startsWith(n.toLowerCase()) &&
      !/[a-z0-9]/i.test(detail.charAt(n.length))
  );
  return !detail ? provName : namesProvider ? detail : provName + " — " + detail;
}

/** A card's save line: what its last edit did. */
/** "pending" = a field holds typed text not sent yet (it goes on a short
 * pause, blur or Enter) — the card says "Saving…" then, never "Saved ✓". */
type SaveState = { state: "pending" | "saving" | "saved" | "failed"; at: number };

/** The add flow's footer lines for fields still to fill / that a save refused. */
const FILL_FIELDS = "Fill in the marked fields first.";
const FIX_FIELDS = "Fix the marked fields first.";

export function TicketsTab({ gotoTab, focus, adding, startAdd, endAdd }: TabProps) {
  // Catalog, saved sources and agent names all come from the shared query cache
  // (see state/queries), warmed at app startup. They used to be three requests
  // fired in series from this component's mount, behind a full-tab "Loading…" —
  // which is what made opening Intake feel like loading a page rather than
  // switching to one. On a warm cache this renders on the first frame.
  const catalog = useTicketingCatalog(true).data || EMPTY_CATALOG;
  const savedSources = useTicketingSources(true);
  const agents = useAgentChoices().data || EMPTY_AGENTS;
  // The card list is edited in place, so it is local state seeded from the
  // query rather than read straight off it — a background refetch must not
  // revert an edit in progress, or re-collapse cards the user has opened.
  // Seeded in the initialiser, not only in the effect below: on a warm cache
  // (the normal case) that is the difference between the cards being there on
  // the first frame and a "Loading…" flash on every reopen.
  const [sources, setSources] = useState<Source[] | null>(savedSources.data ?? null);
  const seeded = useRef(savedSources.data !== undefined);
  // The ids the server has stored, as of the last load or save: a card outside
  // it that is still missing a typed required field is not sent (splitForSave).
  const storedIds = useRef<Set<string>>(
    new Set((savedSources.data || []).map((s) => s.id))
  );
  const seq = useRef(0);
  // What the last save refused, per source — read straight after the save by
  // the add flow's Finish (the state copy below lands a render later).
  const lastRefused = useRef<Record<string, FieldErrors>>({});
  // The add flow's draft while it is not stored yet: every other save leaves
  // it out, so editing another source mid-add can't store it early.
  const pendingDraft = useRef("");
  // Per source: what the server refused on the last save (shown inline, and
  // the card reads "not saved" until it passes), and what its Test is doing
  // or last said — one line for a click's test and a save's test alike.
  const [errors, setErrors] = useState<Record<string, FieldErrors>>({});
  const [tests, setTests] = useState<Record<string, TestResult>>({});
  // Per card: "Saving…" / "Saved ✓" / "Not saved" — on the card itself, every
  // time, instead of a toast that only some edits raised.
  const [saves, setSaves] = useState<Record<string, SaveState>>({});
  // Per card: the fields holding typed text not sent yet. While any is, the
  // card reads "Saving…" — a save of ANOTHER field must not flip it to
  // "Saved ✓" over text that is still on its way.
  const [dirty, setDirty] = useState<Record<string, string[]>>({});
  const markDirty = (id: string, key: string, on: boolean) =>
    setDirty((prev) => {
      const cur = new Set(prev[id] || []);
      if (on) cur.add(key);
      else cur.delete(key);
      if (cur.size === (prev[id] || []).length && [...cur].every((k) => (prev[id] || []).includes(k)))
        return prev;
      return { ...prev, [id]: [...cur] };
    });
  // The list as last edited, ahead of React's next render: a field that saves
  // as it unmounts (the dialog closing mid-typing) edits this, not a closure
  // over a render that is already gone.
  const latest = useRef<Source[] | null>(null);
  latest.current = sources;
  const markSave = (ids: string[], state: SaveState["state"]) =>
    setSaves((prev) => {
      const next = { ...prev };
      for (const id of ids) next[id] = { state, at: Date.now() };
      return next;
    });
  // The listing's per-source failures, for the switch's "can't connect"
  // warning — the same cached query the work list below reads.
  const ticketsPanel = usePanelQuery<{
    errors?: Array<{ source: string; error: string }>;
    stale?: boolean;
  }>(
    "tickets"
  );

  // Which source's editor is open under its heading (one at a time), seeded
  // from a deep link (Settings → Connections → "Open in Intake").
  const edit = useEditing(focus);
  // The tracker draft "+ Add source" is building, and which step it is on.
  const [draft, setDraft] = useState<{ id: string; step: 2 | 3 } | null>(null);
  pendingDraft.current = draft && !storedIds.current.has(draft.id) ? draft.id : "";
  // Step 1's pick: "" until the person presses a tile — nothing is chosen for
  // them (a preselected tracker read as "you are configuring GitHub Issues"
  // to someone who hadn't picked one). Continue makes the draft.
  const [choice, setChoice] = useState("");
  const [flowError, setFlowError] = useState("");
  const [busy, setBusy] = useState(false);
  // With no source yet the add flow leads — until you cancel it.
  const [dismissed, setDismissed] = useState(false);
  // A tracker tile pressed on another tab hands its provider over once.
  const handedOver = useRef("");
  const beginDraftRef = useRef<((provider: string) => void) | null>(null);
  const wanted = adding?.kind === "tracker" ? adding.provider || "" : "";
  useEffect(() => {
    if (!wanted || handedOver.current === wanted) return;
    if (!beginDraftRef.current) return;
    handedOver.current = wanted;
    beginDraftRef.current(wanted);
  });
  useEffect(() => {
    if (!adding) handedOver.current = "";
  }, [adding]);

  useEffect(() => {
    if (seeded.current) return;
    if (savedSources.data === undefined) {
      // Errored with nothing cached: an empty list is the same "connect your
      // first source" state the old catch produced, and stops the tab sitting
      // on "Loading…" forever.
      if (savedSources.isError) {
        seeded.current = true;
        setSources([]);
      }
      return;
    }
    seeded.current = true;
    storedIds.current = new Set(savedSources.data.map((s) => s.id));
    setSources(savedSources.data);
  }, [savedSources.data, savedSources.isError]);

  /** Run Test on a SAVED source and record the verdict on it (the server
   * stamps `last_test`, fills the member id / Asana workspace, and hands the
   * source back). The card's pill is derived from that stored verdict, so a
   * test that passes here is what turns it green. */
  const runTest = useCallback(async (src: Source, meta: CatalogEntry | null) => {
    const payload: Record<string, unknown> = {
      id: src.id,
      provider: src.provider,
      record: true,
    };
    for (const f of meta?.fields || []) {
      const v = src[f.key];
      if (f.secret && (!v || v === "•••set")) continue; // stored secret stays server-side
      if (v != null) payload[f.key] = v;
    }
    if (src.repo_url) payload.repo_url = src.repo_url;
    setTests((prev) => ({ ...prev, [src.id]: { testing: true } }));
    try {
      const r = await api<{
        ok?: boolean;
        error?: string;
        message?: string;
        source?: Source;
      }>("/api/settings/test/ticketing", { json: payload });
      const verdict = r?.source ? lastTestOf(r.source) : null;
      if (r?.source) {
        const srv = r.source;
        setSources((prev) =>
          (prev || []).map((s) =>
            s.id === srv.id
              ? withLastTest(
                  {
                    ...s,
                    member_id: srv.member_id || s.member_id || "",
                    project: srv.project || s.project || "",
                  },
                  verdict
                )
              : s
          )
        );
      }
      const raw = String(r?.message || r?.error || (r?.ok ? "Connected" : "test failed"));
      const msg = r?.ok ? raw : friendlyTestError(src.provider, raw, src.base_url);
      setTests((prev) => ({ ...prev, [src.id]: { testing: false, ok: !!r?.ok, msg } }));
      if (!r?.ok) throw new Error(msg);
      refreshConfig();
      return msg;
    } catch (err) {
      const msg = friendlyTestError(src.provider, (err as Error).message || "test failed", src.base_url);
      setTests((prev) => ({ ...prev, [src.id]: { testing: false, ok: false, msg } }));
      throw new Error(msg);
    }
  }, []);

  /** Test a DRAFT's typed values without saving or recording anything (no
   * `record`: the server only looks). The verdict, the states it listed and
   * the member id it resolved land on the local draft, so step 3's pickers
   * fill in — the source itself is stored only by Finish, which then tests
   * again with `record: true` so its stored verdict is an honest one. */
  const testDraftSource = useCallback(async (src: Source, meta: CatalogEntry | null) => {
    const payload: Record<string, unknown> = { id: src.id, provider: src.provider };
    for (const f of meta?.fields || []) {
      const v = src[f.key];
      if (f.secret && (!v || v === "•••set")) continue;
      if (v != null && v !== "") payload[f.key] = v;
    }
    if (src.repo_url) payload.repo_url = src.repo_url;
    setTests((prev) => ({ ...prev, [src.id]: { testing: true } }));
    try {
      const r = await api<{
        ok?: boolean;
        error?: string;
        message?: string;
        member_id?: string;
        project?: string;
        states?: TicketingWorkflowState[];
        workspaces?: { gid: string; name: string }[];
      }>("/api/settings/test/ticketing", { json: payload });
      const raw = String(r?.message || r?.error || (r?.ok ? "Connected" : "test failed"));
      const msg = r?.ok ? raw : friendlyTestError(src.provider, raw, src.base_url);
      const verdict: TicketingLastTest = {
        ok: !!r?.ok,
        at: new Date().toISOString(),
        message: msg,
        ...(r?.member_id ? { member_id: r.member_id } : {}),
        ...(r?.states?.length ? { states: r.states } : {}),
        ...(r?.workspaces?.length ? { workspaces: r.workspaces } : {}),
      };
      const next = (latest.current || []).map((s) =>
        s.id === src.id
          ? withLastTest(
              {
                ...s,
                member_id: (r?.ok && r?.member_id) || s.member_id || "",
                project: s.project || (r?.ok && r?.project) || "",
              } as Source,
              verdict
            )
          : s
      );
      latest.current = next;
      setSources(next);
      setTests((prev) => ({ ...prev, [src.id]: { testing: false, ok: !!r?.ok, msg } }));
      if (!r?.ok) throw new Error(msg);
      return msg;
    } catch (err) {
      const msg = friendlyTestError(src.provider, (err as Error).message || "test failed", src.base_url);
      setTests((prev) => ({ ...prev, [src.id]: { testing: false, ok: false, msg } }));
      throw new Error(msg);
    }
  }, []);

  const persist = useCallback(
    async (
      list: Source[],
      opts: {
        nulls?: Record<string, string[]>;
        autoTest?: string;
        changed?: string;
        /** Finish: the draft goes too (the add flow's one save). */
        withDraft?: boolean;
      } = {}
    ) => {
      const mySeq = ++seq.current;
      lastRefused.current = {};
      const pending = opts.withDraft ? "" : pendingDraft.current;
      // A new card still missing a typed required field stays local with its
      // inline errors; the rest of the list saves without it.
      const { send, held } = splitForSave(
        pending ? list.filter((s) => s.id !== pending) : list,
        catalog,
        storedIds.current
      );
      const changed = opts.changed ? [opts.changed] : [];
      if (!send.length && !storedIds.current.size) {
        // Nothing stored and nothing ready: no request to make.
        setErrors(held);
        return;
      }
      const sending = changed.filter((id) => !held[id]);
      markSave(sending, "saving");
      try {
        const r = await api<{ sources?: Source[] }>("/api/settings/ticketing/sources", {
          method: "PUT",
          json: putBody(send, opts.nulls),
        });
        if (mySeq !== seq.current) return;
        storedIds.current = new Set((r?.sources || send).map((s) => s.id));
        setErrors(held);
        // The server decides whether a source's stored test still vouches for
        // it (unchanged tested fields keep it; a changed token drops it), so
        // the verdict on each card comes back from the save, not from memory.
        const byId = new Map((r?.sources || []).map((s) => [s.id, s]));
        const stamp = (s: Source) =>
          byId.has(s.id) ? withLastTest(s, lastTestOf(byId.get(s.id))) : s;
        const saved = list.map(stamp);
        setSources((prev) => (prev || []).map(stamp));
        // Keep the shared cache in step, so the next open of this tab shows what
        // was saved instead of refetching to be told the same thing.
        putTicketingSources(r?.sources || send);
        // The card says "Saved ✓" itself; a held card says what it still needs
        // (its pill and its marked fields), so no toast restates either.
        markSave(sending, "saved");
        // Connecting/removing a source flips the ticketing capability.
        refreshConfig();
        if (opts.autoTest) {
          const src = saved.find((s) => s.id === opts.autoTest);
          if (src) {
            const meta = catalog.find((p) => p.id === src.provider) || null;
            runTest(src, meta).catch(() => {
              /* the verdict is on the card already */
            });
          }
        }
      } catch (err) {
        const fieldErrors = err instanceof ApiError && err.status === 400 ? fieldErrorsFrom(err.body) : {};
        if (Object.keys(fieldErrors).length) {
          lastRefused.current = fieldErrors;
          // Not saved: the fields are marked on their cards, which stay as
          // typed — the next blur retries the whole list.
          if (mySeq === seq.current) setErrors({ ...held, ...fieldErrors });
          markSave(sending, "failed");
          return;
        }
        markSave(sending, "failed");
        errorPop(
          "Ticket sources not saved",
          (err as Error).message || "the server rejected the ticket sources"
        );
      }
    },
    [catalog, runTest]
  );

  if (sources === null) return <p className="set-hint">Loading…</p>;

  // A source's id is its branch prefix (feature/<id>-<ticket>/…), so it is
  // seeded from the provider's slug prefix — `sc`, not `shortcut` — never the
  // provider name. The backend falls back the same way for an id-less source.
  const prefixFor = (provider: string) =>
    catalog.find((p) => p.id === provider)?.slug_prefix || provider;

  const uniqueId = (base: string, list: Source[] = sources, except = "") => {
    const taken = new Set(list.map((s) => s.id).filter((x) => x !== except));
    let cand = base,
      n = 1;
    while (taken.has(cand)) {
      n += 1;
      cand = `${base}-${n}`;
    }
    return cand;
  };

  // Whether `id` is still the one `add` seeded for `provider` (`sc`, `sc-2`, …)
  // rather than something the user or a hand-edited config chose.
  const isSeededId = (id: string, provider: string) => {
    const base = prefixFor(provider);
    return id === base || new RegExp(`^${base.replace(/[^\w]/g, "\\$&")}-\\d+$`).test(id);
  };

  /** An edit to the DRAFT: local only. Nothing is stored until Finish — a
   * Test, a Back or a Cancel before it leaves no trace on the server (round 5:
   * Test used to save a half-built source, and Cancel left it behind). */
  const updateDraft = (id: string, patch: Record<string, string | null>) => {
    const strings: Record<string, string> = {};
    for (const [k, v] of Object.entries(patch)) strings[k] = v ?? "";
    const next = (latest.current || []).map((s) => (s.id === id ? ({ ...s, ...strings } as Source) : s));
    latest.current = next;
    setSources(next);
    const merged = next.find((s) => s.id === id);
    if (!merged) return;
    const meta = catalog.find((p) => p.id === merged.provider) || null;
    // A field filled in loses its "Required"; once none is missing, the
    // footer's "Fill in the marked fields first." goes too.
    const { held } = splitForSave([merged], catalog, new Set());
    setErrors((prev) => {
      if (!prev[id]) return prev;
      const out = { ...prev };
      if (held[id]) out[id] = held[id];
      else delete out[id];
      return out;
    });
    if (!held[id]) setFlowError((f) => (f === FILL_FIELDS ? "" : f));
    // A changed credential makes the last Test's verdict about something else.
    if (isTestSensitive(meta, patch)) {
      setTests((prev) => {
        if (!prev[id]) return prev;
        const out = { ...prev };
        delete out[id];
        return out;
      });
      setFlowError((f) => (f.startsWith("Test failed") ? "" : f));
    }
  };

  const update = (id: string, patch: Record<string, string | null>) => {
    if (draft && id === draft.id && !storedIds.current.has(id)) {
      updateDraft(id, patch);
      return;
    }
    {
      const list = latest.current || [];
      // A null clears the field (a secret's Clear control): locally it reads
      // as blank, and the PUT sends the null so the server clears it rather
      // than keeping the stored value, which is what a blank secret means.
      const nulls: Record<string, string[]> = {};
      const strings: Record<string, string> = {};
      for (const [k, v] of Object.entries(patch)) {
        if (v === null) (nulls[id] ||= []).push(k);
        strings[k] = v ?? "";
      }
      let autoTest: string | undefined;
      // The card's id after this edit (a provider switch can re-seed it).
      let changedId = id;
      const next = list.map((s) => {
        if (s.id !== id) return s;
        const merged = { ...s, ...strings } as Source;
        // Switching the provider of a card whose id is still the seeded one
        // re-seeds it, or "Add source → pick Jira" branches Jira tickets under
        // whatever provider the card happened to open on.
        if (
          patch.provider &&
          patch.provider !== s.provider &&
          (!s.provider || isSeededId(s.id, s.provider))
        ) {
          merged.id = uniqueId(prefixFor(patch.provider), list, s.id);
          // Still the stored source under a new id: it is sent (and judged by
          // the server) rather than held, or holding it would drop the stored one.
          if (storedIds.current.has(s.id)) storedIds.current.add(merged.id);
          if (nulls[id]) {
            nulls[merged.id] = nulls[id];
            delete nulls[id];
          }
        }
        // A change to something a test looks at, on a card complete enough to
        // try: the save is followed by a Test, so "saved" and "works" arrive
        // together instead of the card sitting on a token nobody checked.
        const meta = catalog.find((p) => p.id === merged.provider) || null;
        if (shouldAutoTest(meta, merged, patch)) autoTest = merged.id;
        changedId = merged.id;
        return merged;
      });
      // Outside a setState updater on purpose: an edit flushed while the tab
      // unmounts must still reach the server, and an updater never runs then.
      latest.current = next;
      setSources(next);
      // Picking the tracker on a card nothing has been typed into yet is not
      // a save: there is nothing to store, and "Required" in red under fields
      // the user hasn't reached yet reads as a telling-off.
      const firstPick =
        !storedIds.current.has(id) &&
        Object.keys(patch).length === 1 &&
        "provider" in patch &&
        !next.find((s) => s.id === changedId)?.api_token;
      if (firstPick) {
        setErrors((prev) => {
          const out = { ...prev };
          delete out[id];
          delete out[changedId];
          return out;
        });
        return;
      }
      persist(next, { nulls, autoTest, changed: changedId });
    }
  };

  const remove = (id: string, quiet = false) => {
    const list = latest.current || [];
    const gone = list.find((s) => s.id === id);
    const next = list.filter((s) => s.id !== id);
    latest.current = next;
    setSources(next);
    if (storedIds.current.has(id)) persist(next);
    if (gone && storedIds.current.has(id) && !quiet) toast("Removed " + sourceDisplayName(gone, catalog));
    if (edit.editing === id) edit.close();
  };

  // ---- + Add source: the tracker half -----------------------------------
  // A draft is a real entry in the list (it saves as its fields fill, like any
  // card did, so Test has a SAVED source to record its verdict on) that is not
  // yet a heading: only stored sources are listed below.

  /** Start a draft for `provider`. Its id is the provider's slug prefix, so
   * nothing re-seeds it later. `needs_filter: "on"`: it auto-starts NOTHING
   * until a state (or label) is picked — the safe default. */
  const beginDraft = (provider: string) => {
    let list = latest.current || [];
    // Picking another tracker over a draft that was never saved replaces it;
    // one that was saved is removed (it is still mid-add, nobody chose it).
    if (draft) {
      if (storedIds.current.has(draft.id)) remove(draft.id, true);
      list = (latest.current || []).filter((x) => x.id !== draft.id);
    }
    const id = uniqueId(prefixFor(provider), list);
    const fresh = { id, provider, needs_filter: "on" } as Source;
    latest.current = [...list, fresh];
    setSources(latest.current);
    setDraft({ id, step: 2 });
    // ‹ Back to step 1 keeps this tile picked.
    setChoice(provider);
    setFlowError("");
  };
  const dropDraft = () => {
    if (draft) {
      const gone = draft.id;
      for (const set of [setErrors, setTests] as Array<
        React.Dispatch<React.SetStateAction<Record<string, unknown>>>
      >)
        set((prev) => {
          if (!(gone in prev)) return prev;
          const out = { ...prev };
          delete out[gone];
          return out;
        });
      if (storedIds.current.has(draft.id)) remove(draft.id, true);
      else {
        latest.current = (latest.current || []).filter((x) => x.id !== draft.id);
        setSources(latest.current);
      }
    }
    setDraft(null);
    setFlowError("");
  };
  const closeAdd = () => {
    dropDraft();
    setChoice("");
    endAdd();
    setDismissed(true);
  };
  beginDraftRef.current = beginDraft;

  const draftSource = draft ? sources.find((x) => x.id === draft.id) || null : null;
  const draftMeta = draftSource ? catalog.find((p) => p.id === draftSource.provider) || null : null;

  /** The draft as typed, once a field's pending autosave has landed (a
   * field flushes on blur, which a click on a footer button fires first). */
  const settledDraft = async (): Promise<Source | null> => {
    await new Promise((r) => setTimeout(r, 0));
    if (!draft) return null;
    return (latest.current || []).find((x) => x.id === draft.id) || null;
  };
  /** Whether the draft has every field the user must type; marks the ones it
   * doesn't. Local — nothing is sent. */
  const draftComplete = (src: Source): boolean => {
    const { held } = splitForSave([src], catalog, new Set());
    if (!held[src.id]) return true;
    setErrors((prev) => ({ ...prev, [src.id]: held[src.id] }));
    setFlowError(FILL_FIELDS);
    return false;
  };
  const testAndContinue = async (skip = false) => {
    if (!draft || busy) return;
    setBusy(true);
    try {
      const src = await settledDraft();
      if (!src || !draftComplete(src)) return;
      if (!skip) {
        try {
          // A look, not a save: the typed values are tested in memory and
          // the states it lists fill the next step's pickers.
          await testDraftSource(src, draftMeta);
        } catch (err) {
          setFlowError(
            "Test failed — " + ((err as Error).message || "no answer") + ". Fix it, or continue anyway."
          );
          return;
        }
      }
      setFlowError("");
      setDraft({ id: draft.id, step: 3 });
    } finally {
      setBusy(false);
    }
  };
  const finish = async () => {
    if (!draft || busy) return;
    setBusy(true);
    try {
      const src = await settledDraft();
      if (!src || !draftComplete(src)) {
        if (src) setDraft({ id: draft.id, step: 2 });
        return;
      }
      const anyone = src.assignee_scope === "anyone";
      const hasFilter = savedFilter(src).length > 0 || savedLabels(src).length > 0;
      if (anyone && !hasFilter) {
        // The server refuses this save too (Anyone's without a state would
        // quietly fall back to assigned-to-me); said here, where the fix is.
        setFlowError(
          "Anyone's needs at least one state to take tickets from — pick one, or set Whose tickets back to Assigned to me."
        );
        return;
      }
      if (!sourceRepos(src).length) {
        // The pipeline skips a source with nowhere to start its tickets.
        setFlowError("Add the repository its tickets are worked on (Repo URL).");
        return;
      }
      // The ONE save of the add flow.
      await persist(latest.current || [], { changed: draft.id, withDraft: true });
      if (!storedIds.current.has(draft.id)) {
        // Refused: the fields are marked. A refused credential sits on step 2.
        const refused = Object.keys(lastRefused.current[draft.id] || {});
        const credKeys = new Set(basicFields(draftMeta).filter((f) => f.type !== "state").map((f) => f.key));
        if (refused.some((k) => credKeys.has(k))) setDraft({ id: draft.id, step: 2 });
        setFlowError(FIX_FIELDS);
        return;
      }
      // Now that it is stored, test it again WITH record, so its stored
      // verdict (and the "Connect your tracker" step, which the server ticks
      // only on a saved source whose test passed) is the honest one.
      const stored = (latest.current || []).find((x) => x.id === draft.id) || src;
      let connectNote = "";
      try {
        await runTest(stored, draftMeta);
      } catch (err) {
        connectNote = " — can't connect yet: " + ((err as Error).message || "the test failed");
      }
      const name = sourceDisplayName(src, catalog);
      setDraft(null);
      setChoice("");
      setFlowError("");
      endAdd();
      setDismissed(true);
      void refreshPanel("tickets");
      // Never switches Auto-start tickets on: adding a source is not consent
      // to start sessions from it.
      toast(
        "Added " +
          name +
          (connectNote || (hasFilter ? "" : " — nothing auto-starts until you pick a state"))
      );
    } finally {
      setBusy(false);
    }
  };

  // Only SAVED sources have tickets to list or a switch to gate: a draft still
  // being filled in must not conjure a work list — that is how a half-typed
  // card once sat under someone else's error.
  const stored = sources.filter((s) => storedIds.current.has(s.id) && s.id !== draft?.id);
  const panelErrors = new Map(
    (ticketsPanel.data?.errors || []).map((e) => [e.source, e.error])
  );

  /** A source heading's state: whether it needs fixing, and the sentence. */
  const headOf = (id: string, listError?: string): { broken: boolean; message: string } => {
    const src = sources.find((x) => x.id === id);
    if (!src) return { broken: false, message: "" };
    const meta = catalog.find((p) => p.id === src.provider) || null;
    const name = sourceDisplayName(src, catalog);
    if (Object.keys(errors[id] || {}).length) return { broken: true, message: "not saved — fix the marked fields" };
    const t = tests[id];
    if (t && !t.testing && t.msg && !t.ok) return { broken: true, message: "can't connect — " + t.msg };
    if (listError)
      return {
        broken: true,
        message: "can't connect — " + friendlySourceError(name, listError, true).replace(name + " ", "").replace(/\.$/, ""),
      };
    const verdict = sourceTestState(src, meta);
    const last = lastTestOf(src);
    // A stale-but-passing test is not broken (the listing just worked); a
    // failed one, no credentials, or a Test-filled field still empty is.
    if (verdict.state === "off") return { broken: true, message: "no credentials yet" };
    if (last && !last.ok)
      return {
        broken: true,
        message:
          "can't connect — " +
          (friendlyTestError(src.provider, last.message || "", src.base_url) || "the last test failed"),
      };
    if (last && missingAutoFields(meta, src).length) return { broken: true, message: verdict.message };
    return { broken: false, message: "" };
  };

  const editorFor = (id: string) => {
    const src = sources.find((x) => x.id === id);
    if (!src) return null;
    return (
      <TicketSourceEditor
        section="edit"
        source={src}
        catalog={catalog}
        agents={agents}
        fieldErrors={errors[src.id]}
        save={saves[src.id]}
        dirty={(dirty[src.id] || []).length > 0}
        onDirty={(key, on) => markDirty(src.id, key, on)}
        gotoTab={gotoTab}
        saved={storedIds.current.has(src.id)}
        listError={panelErrors.get(src.id)}
        test={tests[src.id]}
        onTest={() => runTest(src, catalog.find((p) => p.id === src.provider) || null)}
        onChange={(patch) => update(src.id, patch)}
        onRemove={() => remove(src.id)}
      />
    );
  };

  const showAdd = !!adding || !!draft || (!stored.length && !dismissed);
  const trackers = trackerKinds(catalog);
  // Step 1 → step 2: a tracker becomes a draft; Repository hands over to the
  // repository flow. Only ever from a tile the person pressed.
  const continueWith = (key: string) => {
    if (!key) return;
    if (key === "repo") {
      dropDraft();
      setChoice("");
      startAdd({ kind: "repo", uses: ["pr"] });
    } else beginDraft(key);
  };
  // The title names a tracker only once one is chosen — before that it is the
  // neutral "Add a source", never the first tile's name.
  const addTitle = draftMeta ? "Add " + draftMeta.label : "Add a source";
  const addFlow = !showAdd ? null : adding?.kind === "repo" && !draft ? (
    <AddRepoFlow
      key="add-repo"
      uses={adding.uses?.length ? adding.uses : ["pr"]}
      trackers={trackers}
      onPickTracker={(provider) => {
        endAdd();
        beginDraft(provider);
      }}
      onCancel={closeAdd}
      onDone={() => closeAdd()}
    />
  ) : (
    <AddFlowShell
      key="add-tracker"
      title={addTitle}
      step={draft ? draft.step : 1}
      kinds={[...trackers, REPO_KIND]}
      picked={draftSource?.provider || (draft ? "" : choice)}
      onPick={(key) => {
        // Step 1 only marks the tile (Continue goes on); once past it, a
        // different tile switches the draft straight over.
        if (!draft) {
          setChoice(key);
          setFlowError("");
        } else if (key !== draftSource?.provider) continueWith(key);
      }}
      onCancel={closeAdd}
      footer={
        !draft ? (
          <>
            <span className="ik-add-hint" id="ik-add-pick-hint">
              {!choice ? (
                <>
                  <strong>Pick your tracker</strong> — or <strong>Repository</strong> to watch a
                  GitHub repository's PRs, issues or what ships.
                </>
              ) : choice === "repo" ? (
                "Next: the repository to watch, and what to use it for."
              ) : (
                "Next: connect " + (catalog.find((p) => p.id === choice)?.label || "it") + "."
              )}
            </span>
            <button
              type="button"
              className="btn-primary"
              id="ik-add-continue"
              disabled={!choice}
              title={choice ? undefined : "Pick your tracker first"}
              onClick={() => continueWith(choice)}
            >
              Continue ›
            </button>
          </>
        ) : draft.step === 2 ? (
          <>
            <span className="ik-add-error" role="alert">
              {flowError}
              {flowError.startsWith("Test failed") ? (
                <>
                  {" "}
                  <button type="button" className="linklike" onClick={() => void testAndContinue(true)}>
                    Continue anyway
                  </button>
                </>
              ) : null}
            </span>
            <button type="button" className="ik-quiet-btn" onClick={dropDraft}>
              ‹ Back
            </button>
            <button
              type="button"
              className="btn-primary"
              id="ik-add-next"
              disabled={busy}
              onClick={() => void testAndContinue()}
            >
              {busy ? "Testing…" : "Test & continue ›"}
            </button>
          </>
        ) : (
          <>
            {flowError || !tests[draft.id]?.msg ? (
              <span className="ik-add-error" role="alert">
                {flowError}
              </span>
            ) : (
              // What step 2's Test found, carried over — or, after "Continue
              // anyway", that Finish saves it and tests it once more.
              <span
                className={"ik-add-note" + (tests[draft.id]?.ok ? " ok" : "")}
                id="ik-add-test-note"
              >
                {tests[draft.id]?.ok
                  ? "✓ " + tests[draft.id]?.msg
                  : "Not connected yet — Finish saves it and tests it again."}
              </span>
            )}
            <button
              type="button"
              className="ik-quiet-btn"
              onClick={() => setDraft({ id: draft.id, step: 2 })}
            >
              ‹ Back
            </button>
            <button
              type="button"
              className="btn-primary"
              id="ik-add-finish"
              disabled={busy}
              onClick={() => void finish()}
            >
              Finish
            </button>
          </>
        )
      }
    >
      {draftSource ? (
        <div className="ik-add-body">
          <TicketSourceEditor
            section={draft!.step === 2 ? "connect" : "setup"}
            source={draftSource}
            catalog={catalog}
            agents={agents}
            fieldErrors={errors[draftSource.id]}
            save={saves[draftSource.id]}
            dirty={(dirty[draftSource.id] || []).length > 0}
            onDirty={(key, on) => markDirty(draftSource.id, key, on)}
            gotoTab={gotoTab}
            onAddRepo={() => {
              dropDraft();
              startAdd({ kind: "repo", uses: ["issue"] });
            }}
            saved={storedIds.current.has(draftSource.id)}
            test={tests[draftSource.id]}
            onTest={() => testDraftSource(draftSource, draftMeta)}
            onChange={(patch) => update(draftSource.id, patch)}
            onRemove={dropDraft}
          />
        </div>
      ) : null}
    </AddFlowShell>
  );

  return (
    <>
      {addFlow}
      <p className="set-hint set-block-hint">
        Connect a tracker. Tickets in the states you pick start as sessions — on their own
        while auto-start is on, or when you press <strong>Start</strong>.
      </p>
      {/* The switch itself lives on the Auto-start tab, beside the other two. */}
      <AutoStartLink kind="tickets" gotoTab={gotoTab} />
      {/* WORK, with each source's heading as its handle: Edit (or Fix) opens
          the source's settings under its heading. There is no card list
          below any more — "+ Add source" in the dialog head adds one. */}
      {stored.length > 0 ? (
        <AssignedTickets
          sources={stored}
          catalog={catalog}
          agents={agents.names}
          // Per source, what "Default" resolves to on a row's Agent picker —
          // the source's own Agent CLI, else the app default.
          sourceAgents={Object.fromEntries(
            sources.map((s) => [s.id, s.agent || agents.fallback || ""])
          )}
          defaultAgent={agents.fallback}
          // Same idea for the depth picker: what "Default" means on this
          // source's rows is the source's own automation depth.
          sourceDepths={Object.fromEntries(
            sources.map((s) => [s.id, s.depth || ""])
          )}
          // ...and for the effort picker, so a row's empty choice names the
          // queue's own rung instead of the CLI's default.
          sourceEfforts={Object.fromEntries(
            sources.map((s) => [s.id, s.effort || ""])
          )}
          editing={edit.editing}
          onEdit={edit.toggle}
          onOpenEditor={edit.open}
          headOf={headOf}
          editorFor={editorFor}
        />
      ) : !showAdd ? (
        <div className="set-row" id="ticketing-sources">
          <div className="repo-empty ik-empty-add">
            <span>No ticket source yet — connect Shortcut, Jira, Linear, Asana or GitHub Issues.</span>
            <button
              type="button"
              id="ticketing-add"
              className="test-btn"
              onClick={() => startAdd({ kind: null })}
            >
              + Add a ticket source
            </button>
          </div>
        </div>
      ) : null}
    </>
  );
}

/** Multi-state auto-start picker: the field stores one or more state ids
 * comma-joined; each shows as a chip with a ✕, and the dropdown appends
 * another.
 *
 * Empty means "auto-start nothing" on every source Intake created
 * (`needs_filter`: the safe default — round 3 found "every ticket assigned to
 * you" sitting there as the default), and "every state" only on sources saved
 * before that, or where the user explicitly picked **Any state**.
 *
 * The names come from the tracker (what the last Test listed, refreshed on
 * focus). Before a Test has passed there is nothing to list, so the state can
 * be TYPED; a typed name is swapped for the real state once Test lists one
 * with that name, and the server starts nothing on a name it can't match
 * (backend: auto_start_waiting). */
function StatePicker({
  field,
  source,
  states,
  loadStates,
  onChange,
}: {
  field: CatalogField;
  source: Source;
  states: TicketingWorkflowState[];
  loadStates(): void;
  onChange(patch: Record<string, string>): void;
}) {
  const selected = savedFilter(source);
  const nameOf = (id: string) => stateName(id, states);
  const remaining = states.filter((s) => !selected.includes(String(s.id)));
  const commit = (list: string[]) => onChange({ [field.key]: list.join(",") });
  const [typed, setTyped] = useState("");
  // "Anyone's" is carried by this filter (or, on Shortcut, an auto-start
  // label) — with neither there is nothing to scope a whole-tracker search by,
  // so the source stays on assigned-to-me until one is set. Say that where the
  // gap is.
  const hasLabels = !!(source.ingest_labels || "").trim();
  const needsState = source.assignee_scope === "anyone" && !selected.length && !hasLabels;
  const safe = source.needs_filter === "on";
  const noList = !states.length;
  // The tracker's word: "states", "statuses" (Jira), "sections" (Asana).
  const plural = (field.label.replace(/^Auto-start /i, "") || "states").toLowerCase();
  const noun = /statuses$/.test(plural) ? plural.replace(/es$/, "") : plural.replace(/s$/, "");

  // A typed name becomes the real state once the tracker lists one by that
  // name (any Test or focus refresh), so what is stored is what the search uses.
  const selectedKey = selected.join(",");
  useEffect(() => {
    if (!states.length || !selected.length) return;
    const byName = new Map(states.map((st) => [String(st.name || "").trim().toLowerCase(), String(st.id)]));
    const ids = new Set(states.map((st) => String(st.id)));
    const next = selected.map((v) => (ids.has(v) ? v : byName.get(v.trim().toLowerCase()) || v));
    if (next.join(",") !== selectedKey) commit([...new Set(next)]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [states, selectedKey]);

  const addTyped = () => {
    const v = typed.trim().replace(/,/g, " ");
    if (!v || selected.some((x) => x.toLowerCase() === v.toLowerCase())) return setTyped("");
    commit([...selected, v]);
    setTyped("");
  };

  return (
    <div className="set-row">
      <span className="set-label">{field.label}</span>
      <div className="repo-list">
        {!selected.length ? (
          <div className={"repo-empty" + (safe && !hasLabels ? " tk-state-none" : "")} data-tk-state-empty={safe ? "safe" : "any"}>
            {needsState
              ? "Pick at least one " + noun + " — until you do, this source only takes tickets assigned to you."
              : hasLabels
                ? "Any " + noun + " — every ticket with an auto-start label can auto-start."
                : safe
                  ? "None picked — nothing auto-starts from this source yet. Pick the " +
                    noun +
                    "(s) that mean “ready for MindFlock”."
                  : "Any " + noun + " — every ticket assigned to you can auto-start."}
          </div>
        ) : (
          selected.map((id) => {
            const name = nameOf(id);
            // Not a listed state: a name typed before Test (or one the
            // tracker doesn't have).
            const unmatched = !name && !/^\d+$/.test(id) && !/^[0-9a-f-]{36}$/i.test(id);
            return (
              <span
                className={"repo-chip" + (unmatched && !noList ? " tk-state-unknown" : "")}
                key={id}
                data-tk-typed={unmatched ? "1" : undefined}
                title={
                  name
                    ? undefined
                    : unmatched
                      ? noList
                        ? "Typed — matched to your tracker's " + noun + " when Test passes"
                        : "Your tracker has no " + noun + " by this name — check the spelling"
                      : "Run Test to show this " + noun + "'s name"
                }
              >
                <span className="repo-chip-name">{name || (unmatched ? id : "Saved " + noun)}</span>
                <button
                  type="button"
                  className="repo-chip-x"
                  title={"Stop auto-starting “" + (name || id) + "”"}
                  aria-label={"Remove auto-start " + noun + " " + (name || id)}
                  onClick={() => commit(selected.filter((x) => x !== id))}
                >
                  ✕
                </button>
              </span>
            );
          })
        )}
      </div>
      {noList ? (
        // No list until Test passes — but the state can be typed now.
        <span className="tk-state-type">
          <input
            type="text"
            className="tk-state-typed"
            data-tk-field={field.key + "-typed"}
            autoComplete="off"
            placeholder={"Type a " + noun + " name, e.g. Ready for dev"}
            aria-label={"Add an auto-start " + noun + " by name"}
            value={typed}
            onFocus={loadStates}
            onChange={(e) => setTyped(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                addTyped();
              }
            }}
          />
          <button type="button" className="test-btn tk-state-add" disabled={!typed.trim()} onClick={addTyped}>
            Add
          </button>
        </span>
      ) : (
        <select
          className="tk-state"
          data-tk-field={field.key}
          value=""
          aria-label={"Add an auto-start " + noun}
          // Opens with what the last Test saw; every focus asks the tracker
          // again, so a column added this morning is there without a retest.
          onFocus={loadStates}
          onChange={(e) => {
            const v = e.target.value;
            if (v === ANY_STATE) onChange({ needs_filter: "" });
            else if (v) commit([...selected, v]);
          }}
        >
          <option value="">{"+ Add " + noun + "…"}</option>
          {remaining.map((st) => (
            <option key={String(st.id)} value={String(st.id)}>
              {st.name || String(st.id)}
            </option>
          ))}
          {/* The explicit opt-in to the old wide default — kept, never the default. */}
          {safe && !selected.length && !hasLabels ? (
            <option value={ANY_STATE}>{"Any " + noun + " — start every ticket assigned to me"}</option>
          ) : null}
        </select>
      )}
      <span className="set-hint">
        {noList
          ? "Only tickets in these " + plural + " auto-start. Typed names are checked when Test passes."
          : "Only tickets in these " + plural + " auto-start. Any other ticket starts with its row's Start."}
      </span>
    </div>
  );
}

/** The state select's "Any state" choice: clears `needs_filter` instead of
 * adding a state. Not a valid state id on any tracker. */
const ANY_STATE = "__any_state__";

/** Single-state picker: where a ticket is MOVED when a session starts for it.
 *
 * Its own component rather than a mode of `StatePicker` because it is the other
 * kind of thing: that one is a filter over several states, this one is one
 * destination, and empty means "leave it alone" rather than "any". It reads the
 * same live state list, so it is populated by the same Test connection. */
function StartStatePicker({
  field,
  source,
  states,
  loadStates,
  onChange,
}: {
  field: CatalogField;
  source: Source;
  states: TicketingWorkflowState[];
  loadStates(): void;
  onChange(patch: Record<string, string>): void;
}) {
  const current = (source[field.key] || "").trim();
  // A saved id whose state list hasn't loaded yet would otherwise fall off the
  // <select> and read as "Leave it where it is" — i.e. the setting silently
  // unset itself. Keep it as its own option until the real list arrives.
  const known = states.some((st) => String(st.id) === current);
  return (
    <label className="set-row">
      <span className="set-label">{field.label}</span>
      <select
        className="tk-state-one"
        data-tk-field={field.key}
        value={current}
        onFocus={loadStates}
        onChange={(e) => onChange({ [field.key]: e.target.value })}
      >
        <option value="">Leave it where it is</option>
        {current && !known ? <option value={current}>{current} (saved)</option> : null}
        {states.map((st) => (
          <option key={String(st.id)} value={String(st.id)}>
            {st.name || String(st.id)}
          </option>
        ))}
      </select>
      {field.hint ? <span className="set-hint">{field.hint}</span> : null}
    </label>
  );
}

/** A catalog field with a fixed set of values, rendered as a select. */
function ChoicePicker({
  field,
  source,
  error,
  onChange,
}: {
  field: CatalogField;
  source: Source;
  /** The server's objection (Anyone's without an ingest state lands here). */
  error?: string;
  onChange(patch: Record<string, string>): void;
}) {
  const value = source[field.key] || "";
  const option = (field.options || []).find((o) => o.value === value);
  const hint = option && option.hint !== undefined ? option.hint : field.hint;
  return (
    <label className="set-row">
      <FieldLabel field={field} />
      <select
        className={"tk-choice" + (error ? " field-missing" : "")}
        data-tk-field={field.key}
        aria-invalid={error ? true : undefined}
        value={value}
        onChange={(e) => onChange({ [field.key]: e.target.value })}
      >
        {(field.options || []).map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      {/* The hint stays under the error: the rule is worth reading either way.
          It explains the option that is SELECTED when the catalog has one for
          it — never "Anyone's takes…" under "Assigned to me". */}
      {error ? <span className="ik-field-error" data-tk-error={field.key}>{error}</span> : null}
      {hint ? <span className="set-hint">{hint}</span> : null}
    </label>
  );
}

/** The teammate a 409 from POST /api/tickets/start names (the tracker says
 * they are on the ticket — tracker_claims), or null for any other failure. */
export function claimedBy(err: unknown): TicketClaimedBy | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null;
  const body = err.body as { claimed_by?: TicketClaimedBy } | null;
  return body && body.claimed_by && body.claimed_by.name ? body.claimed_by : null;
}

interface AssignedTicket {
  source: string;
  source_label?: string;
  id: string;
  slug: string;
  name?: string;
  url?: string;
  created_at?: string;
  session?: string;
  bucket?: string;
  has_session?: boolean;
  /** Set when another of the user's devices already holds this ticket. */
  elsewhere?: { device: string; label: string; kind: string };
  /** Set when the TRACKER says a teammate is on it ("<name> is on this" leads
   * the reasons); Begin work then needs the owner's Override. */
  claimed_by?: TicketClaimedBy;
  eligible?: boolean;
  reasons?: string[];
  /** False only on a source that ingests anyone's tickets. Absent = yours. */
  mine?: boolean;
  /** Display name(s) of whoever it is assigned to, when the provider says. */
  assignee?: string;
  /** Present when a previous run of this ticket still has its workspace here. */
  workspace?: ItemWorkspace;
  /** Whether this ticket's SOURCE can merge one of its tickets into another and
   * delete the loser — i.e. whether its adapter implements the provider writes.
   * Stamped by the server (`TicketProvider.can_merge`) rather than decided from
   * the provider name here, so there is one answer rather than two. */
  merge_ready?: boolean;
  /** On a source with several repos: which one(s) this ticket goes to and why
   * (backend/ticket_ingestion/repo_routing.py::preview). */
  route?: ItemRoute & { summary?: string };
  /** The repo a start provisions from (the routed one, else the source's). */
  repo_url?: string;
}

/** Per-device: which `source::bucket` pairs are expanded. Keyed by the pair,
 * not the bucket name, so opening Jira's "In Progress" doesn't also open
 * Linear's — the whole point of grouping by source first. */
const BUCKETS_OPEN_LS_KEY = "mf_ticket_buckets_open";
/** Per-device: which source groups are COLLAPSED (membership = collapsed, so a
 * fresh install writes nothing and every source starts open). */
const SOURCES_CLOSED_LS_KEY = "mf_intake_ticket_sources";
/** Per-device: which `source::wf::<workflow>` groups are COLLAPSED. Open by
 * default — the level exists to show the states under it. */
const WORKFLOWS_CLOSED_LS_KEY = "mf_intake_ticket_workflows";

/** Assigned-tickets panel — the ticket twin of the PR and issue panels: EVERY
 * ticket assigned to you, grouped by source and then split into workflow-state
 * buckets. The Add bucket dropdown + per-bucket ✕ choose which buckets the
 * panel shows at all (persisted); Begin work force-starts a session, bypassing
 * the auto filters. */
function AssignedTickets({
  sources,
  catalog,
  agents,
  sourceAgents,
  defaultAgent,
  sourceDepths,
  sourceEfforts,
  editing,
  onEdit,
  onOpenEditor,
  headOf,
  editorFor,
}: {
  /** The SAVED sources, in their configured order: the panel groups only
   * these, names them as the user knows them, and reads each one's saved
   * auto-start filter from here rather than from the tracker. Every one of
   * them gets a heading — always, even empty, unreachable or filtered to
   * nothing — because the heading is where the source is edited. */
  sources: Source[];
  /** The source whose editor is open under its heading. */
  editing: string | null;
  /** Edit / Fix / Done on a heading. */
  onEdit(id: string): void;
  /** The heading's "auto-starts …" note: opens (never closes) the editor. */
  onOpenEditor(id: string): void;
  /** Whether the source needs fixing, and the sentence that says why. */
  headOf(id: string, listError?: string): { broken: boolean; message: string };
  editorFor(id: string): React.ReactNode;
  catalog: CatalogEntry[];
  agents: string[];
  sourceAgents: Record<string, string>;
  /** Per source, the thinking-effort rung its tickets run at — what "Configured"
   * resolves to on a row's effort picker. */
  sourceEfforts: Record<string, string>;
  /** Per source, the automation depth its rows inherit ("" = none set). */
  sourceDepths: Record<string, string>;
  /** What a row falls back to naming when its source isn't in the map — a
   * ticket from a source that was just removed, say. Never a bare "Configured". */
  defaultAgent: string;
}) {
  // The slowest of the work fan-outs (a provider search per source plus a
  // ls-remote per repo), so it is the one worth keeping across dialog opens.
  const ticketsQuery = usePanelQuery<{
    tickets?: AssignedTicket[];
    sources?: string[];
    source_labels?: Record<string, string>;
    buckets?: string[];
    done_buckets?: string[];
    ingest_states?: Record<string, string[]>;
    bucket_meta?: Record<string, { group?: string; label?: string }>;
    errors?: Array<{ source: string; error: string }>;
    /** Per source: "can't connect — <reason>", or null when it lists. */
    source_status?: Record<string, { connect_error?: string | null }>;
    stale?: boolean;
  }>("tickets");
  const load = ticketsQuery.refresh; // Refresh button: force a sweep
  const relistTickets = ticketsQuery.refetch; // after a force start
  const tickets = ticketsQuery.data
    ? (ticketsQuery.data.tickets || []).filter((t) => !sources.length || sources.some((s) => s.id === t.source))
    : null;
  const buckets = ticketsQuery.data?.buckets || [];
  const doneBuckets = ticketsQuery.data?.done_buckets || [];
  const ingestStates = ticketsQuery.data?.ingest_states || {};
  const sourceLabels = ticketsQuery.data?.source_labels || {};
  const configured = new Map(sources.map((s) => [s.id, s]));
  /** A source's name as the user knows it. The server's label falls back to
   * the raw id ("sc") when the tracker can't be reached — never show that. */
  const nameOf = (key: string) => {
    const src = configured.get(key);
    if (src) return sourceDisplayName(src, catalog);
    const server = sourceLabels[key];
    return server && server !== key ? server : key;
  };
  // Whether a ticket found here would actually start on its own: the ticket
  // switch. The row chips follow it, so none can say "will auto-start" beside
  // a switch that says it won't.
  const { data: status } = useMindflockStatus();
  const autoOn = !!(status?.desired ?? status?.running);
  const eligibleLabel = autoOn ? "will auto-start" : "auto-start off";
  const bucketMeta = ticketsQuery.data?.bucket_meta || {};
  /** The workflow/board a bucket belongs to ("" = the source has only one, so
   * there is nothing to nest under). */
  const groupOf = (b: string) => bucketMeta[b]?.group || "";
  /** A bucket's own name. Providers qualify the bucket KEY to keep it unique
   * across workflows ("Product Development · Deferred"); inside its workflow
   * group the qualifier is already written above it, so the heading uses this. */
  const labelOf = (b: string) => bucketMeta[b]?.label || b;
  // Only sources that are still configured: a cached listing from before a
  // source was removed (or re-seeded) must not show a group for it.
  const listedSources = (ticketsQuery.data?.sources || []).filter((s) => configured.has(s));
  const sourceErrors = new Map(
    (ticketsQuery.data?.errors || [])
      .filter((e) => configured.has(e.source))
      .map((e) => [e.source, e.error])
  );
  const error = ticketsQuery.error
    ? "Could not list tickets: " + (ticketsQuery.error.message || "error")
    : "";
  // A source's failure is said ONCE, in its own group (heading "can't
  // connect", body the sentence) — not again up here in the toolbar.
  const note = panelNote({
    error,
    fetching: ticketsQuery.isFetching,
    loaded: !!tickets,
  });

  const [shown, setShown] = useState<string[] | null>(loadShownBuckets);
  const [mineOnly, setMineOnly] = useState<boolean>(loadMineOnly);
  // Tickets ticked to start together, keyed source:id, in the order ticked —
  // the order they will be listed (and started) in. Not persisted: a pick is
  // a moment's intent, not a setting.
  const [picked, setPicked] = useState<Map<string, AssignedTicket>>(() => new Map());
  const pickKey = (t: AssignedTicket) => t.source + ":" + t.id;
  const setPick = (t: AssignedTicket, on: boolean) =>
    setPicked((m) => {
      const next = new Map(m);
      if (on) next.set(pickKey(t), t);
      else next.delete(pickKey(t));
      return next;
    });
  // The key predates the source level, when an entry was a bare state name.
  // Those can never match a `source::state` key again, so they are dropped on
  // load rather than accumulating forever; the cost is that a previously
  // expanded bucket starts collapsed once.
  const filter = useListFilter(
    "tk-tickets-filter",
    "Filter by ticket, title, source, state, or assignee…  ( Ctrl+F )",
  );
  const openBuckets = useToggleSet(BUCKETS_OPEN_LS_KEY, false, (v) => v.includes("::"));
  const openSources = useToggleSet(SOURCES_CLOSED_LS_KEY, true);
  const openWorkflows = useToggleSet(WORKFLOWS_CLOSED_LS_KEY, true);

  const visible = visibleBuckets(buckets, doneBuckets, shown);
  const hidden = buckets.filter((b) => !visible.includes(b));

  // Is there anyone else's work in here at all? Only a source set to ingest
  // anyone's tickets can produce that, and without one the Mine/Everyone
  // control would filter a list that is already entirely yours.
  const hasOthers = (tickets || []).some((t) => t.mine === false);
  const rows = mineOnly ? (tickets || []).filter((t) => t.mine !== false) : tickets || [];

  // source -> bucket -> rows. Both levels keep the server's ordering: sources
  // in configured order, buckets in the provider's own workflow order.
  const bySource = new Map<string, Map<string, AssignedTicket[]>>();
  const countAll = new Map<string, number>();
  // THE BUCKET COUNTS ARE OF THE WHOLE LIST, not of the filter. "+ Add
  // bucket…" is a standing choice about which workflow states this panel shows
  // at all; a bucket reading "(0)" because of a search two keystrokes old would
  // be the wrong answer to the question that menu asks.
  for (const t of rows) {
    const b = t.bucket || NO_STATE_BUCKET;
    countAll.set(b, (countAll.get(b) || 0) + 1);
  }
  // The GROUPING, though, is built from what matched — so every heading's count
  // describes what is under it, and a source or a state with no match drops out
  // instead of rendering an empty group.
  for (const t of filter.active
    ? rows.filter((t) => ticketMatches(t, filter.tokens))
    : rows) {
    const src = t.source || "unknown";
    const b = t.bucket || NO_STATE_BUCKET;
    if (!bySource.has(src)) bySource.set(src, new Map());
    const inner = bySource.get(src)!;
    if (!inner.has(b)) inner.set(b, []);
    inner.get(b)!.push(t);
  }
  // EVERY configured source, in configured order — filtered or not, listed or
  // not, reachable or not: its heading is its Edit. A source a stale listing
  // still names but that is no longer configured has no group.
  const sourceOrder = sources.map((s) => s.id);
  void listedSources;
  const matched = filter.active ? [...bySource.values()].reduce((n, m) => n + [...m.values()].flat().length, 0) : -1;

  const hideBucket = (b: string) => {
    // First customization starts from what's on screen (the default view),
    // so hiding one bucket never surfaces the parked done-type ones.
    const next = (shown ?? visible).filter((x) => x !== b);
    setShown(next);
    saveShownBuckets(next);
  };
  const addBucket = (b: string) => {
    if (!b) return;
    const next = [...(shown ?? visible), b];
    setShown(next);
    saveShownBuckets(next);
  };

  // Which done-type states are actually hidden right now. Named rather than
  // illustrated: "done states like Completed" was a guess about someone else's
  // workflow — these are Shortcut's "Product Development · Won't do", Jira's
  // "Closed", whatever this flock really has.
  // Unqualified, like the headings inside a workflow group: "Done, Completed and
  // Won't do" says the same thing as three "Product Development · …" mouthfuls,
  // and this is a hint, not a key.
  const parkedDone = doneBuckets
    .filter((b) => !visible.includes(b))
    .map((b) => labelOf(b));

  /** A source's auto-start filter, from its SAVED settings — never from the
   * tracker's answer. The tracker only supplies the state NAMES; when it can't
   * be reached the filter is still there, and the words say "1 saved state"
   * rather than the false "no filter is set" they used to. */
  const filterOf = (key: string) => {
    const src = configured.get(key);
    const ids = savedFilter(src);
    const live = ingestStates[key] || [];
    const names = ids
      .map((id) => stateName(id, lastTestOf(src)?.states) || null)
      .filter((n): n is string => !!n);
    return filterSummary({
      ids,
      names: names.length >= ids.length ? names : live.length >= ids.length ? live : names,
      labels: savedLabels(src),
      waiting: src?.needs_filter === "on",
    });
  };

  /** Which states auto-start takes — stated per source, never merged.
   *
   * The merged version ("watches A, B" from one source's A and another's B) was
   * false for both of them: no source watched both. With several sources each
   * group heading already carries its own, so the panel-level hint points there
   * instead of inventing a union. */
  const only = sourceOrder.length === 1 ? filterOf(sourceOrder[0]) : null;
  // The heading's button, named as it reads right now: Fix on a source that
  // can't connect, Edit otherwise.
  const onlyAction =
    sourceOrder.length === 1 && headOf(sourceOrder[0], sourceErrors.get(sourceOrder[0])).broken
      ? "Fix"
      : "Edit";
  /** The sentence a source that can't connect shows in place of its list —
   * the server's `connect_error` (listing failure or a failed stored test),
   * with Jira's bare HTTP codes put into words. */
  const connectErrorOf = (key: string): string => {
    const raw = ticketsQuery.data?.source_status?.[key]?.connect_error || "";
    if (!raw) return "";
    const src = configured.get(key);
    const reason = raw.replace(/^can't connect\s*[—-]\s*/i, "");
    const words = src ? friendlyTestError(src.provider, reason, src.base_url) : reason;
    return "Can't connect — " + words.replace(/\.$/, "") + " — press Fix.";
  };
  const ingestSummary = !only ? (
    <>
      Each source's heading says which tickets it auto-starts — press it to change them;{" "}
      <strong>Start</strong> starts any ticket now.
    </>
  ) : only.kind === "waiting" ? (
    <>
      This source auto-starts <strong>nothing</strong> until you pick a state — press{" "}
      <strong>{onlyAction}</strong> on its heading. <strong>Start</strong> starts any ticket now.
    </>
  ) : only.kind === "none" ? (
    <>
      Auto-start takes tickets in <strong>every state</strong> — press{" "}
      <strong>{onlyAction}</strong> on its heading to narrow it. <strong>Start</strong> starts
      any ticket now.
    </>
  ) : (
    <>
      Auto-start only takes tickets {only.kind === "labels" ? "with " : "in "}
      <strong>{only.text}</strong>
      {only.named ? "" : " (names show once the tracker answers)"}.{" "}
      <strong>Start</strong> starts any other ticket now.
    </>
  );

  const hiddenCount = hidden.length;
  const viewLabel =
    "View: " +
    (hasOthers ? (mineOnly ? "only mine" : "everyone's") + " · " : "") +
    (hiddenCount ? hiddenCount + (hiddenCount === 1 ? " state hidden" : " states hidden") : "all states");

  return (
    <WorkListPanel
      // "Assigned tickets" is a lie the moment a source ingests anyone's — the
      // QA queue's rows are assigned to whoever wrote the code.
      label={hasOthers ? "Ticket queue" : "Assigned tickets"}
      onRefresh={load}
      note={note}
      rowId="tk-assigned-row"
      refreshId="tk-tickets-refresh"
      noteId="tk-tickets-note"
      listId="tk-tickets-list"
      toolbarExtra={
        <>
          {tickets && tickets.length ? filter.control : null}
          {/* VIEW, kept apart from the sources' settings: whose tickets and
              which workflow states this panel shows (per browser). It used to
              be two selects — "Everyone's tickets" and "+ Show state…" — plus
              a ✕ on each state, and they read as the source's "Whose tickets"
              and "Auto-start states", which they are not. */}
          {buckets.length || hasOthers ? (
            <ViewMenu
              id="tk-view"
              label={viewLabel}
              title="Which tickets this panel shows — on this browser only; what auto-starts is on each source's heading"
            >
              {hasOthers ? (
                <div className="ik-view-sec" role="radiogroup" aria-label="Whose tickets this panel lists">
                  <span className="ik-view-h">Whose tickets</span>
                  {(["all", "mine"] as const).map((v) => (
                    <label key={v} className="ik-view-opt">
                      <input
                        type="radio"
                        name="tk-mine-filter"
                        id={v === "mine" ? "tk-mine-filter" : undefined}
                        checked={(v === "mine") === mineOnly}
                        onChange={() => {
                          setMineOnly(v === "mine");
                          saveMineOnly(v === "mine");
                        }}
                      />
                      {v === "mine" ? "Only mine" : "Everyone's"}
                    </label>
                  ))}
                </div>
              ) : null}
              {buckets.length ? (
                <div className="ik-view-sec" aria-label="Workflow states this panel shows">
                  <span className="ik-view-h">States shown</span>
                  {buckets.map((b) => {
                    const on = visible.includes(b);
                    return (
                      <label key={b} className="ik-view-opt" data-bucket={b}>
                        <input
                          type="checkbox"
                          checked={on}
                          onChange={() => (on ? hideBucket(b) : addBucket(b))}
                        />
                        <span className="ik-view-name">{b}</span>
                        <span className="ik-view-n">{countAll.get(b) || 0}</span>
                      </label>
                    );
                  })}
                </div>
              ) : null}
            </ViewMenu>
          ) : null}
        </>
      }
      hint={
        <>
          {ingestSummary}{" "}
          {parkedDone.length ? (
            <>
              <strong>{listNames(parkedDone)}</strong> {parkedDone.length === 1 ? "is" : "are"}{" "}
              hidden — show {parkedDone.length === 1 ? "it" : "them"} from <strong>View</strong>.
            </>
          ) : null}
        </>
      }
    >
      {error ? <div className="repo-empty">{error}</div> : null}
      {tickets !== null && !visible.length && buckets.length ? (
        <p className="ik-nomatch">All states are hidden — pick some under View.</p>
      ) : null}
      {matched === 0 ? <p className="ik-nomatch">No ticket matches “{filter.query}”.</p> : null}
      {sourceOrder.map((src) => {
          const inner = bySource.get(src) || new Map<string, AssignedTicket[]>();
          const shownBuckets = visible.filter((b) => (inner.get(b) || []).length);
          const total = shownBuckets.reduce((n, b) => n + (inner.get(b) || []).length, 0);
          const srcError = sourceErrors.get(src);
          const label = nameOf(src);
          const filt = filterOf(src);
          const head = headOf(src, srcError);
          const isEditing = editing === src;
          const scope = configured.get(src)?.assignee_scope === "anyone" ? "anyone's · " : "";
          // The workflows this source's shown buckets span, in the provider's
          // own order. More than one and they become a level of their own, so
          // "Product Development" is written once instead of onto each of its
          // seven state headings.
          const workflows: string[] = [];
          for (const b of shownBuckets) {
            const g = groupOf(b);
            if (!workflows.includes(g)) workflows.push(g);
          }
          const nestWorkflows = workflows.filter(Boolean).length > 1;

          /** The state buckets of one workflow (or all of them, ungrouped). */
          const bucketsOf = (wf: string | null) =>
            shownBuckets
              .filter((b) => wf === null || groupOf(b) === wf)
              .map((b) => {
                const rows = inner.get(b) || [];
                const key = src + "::" + b;
                return (
                  <WorkGroup
                    key={key}
                    indent
                    // Inside its workflow the qualifier is already overhead;
                    // without one the full key is the only name there is.
                    name={wf === null ? b : labelOf(b)}
                    count={rows.length}
                    open={openBuckets.isOpen(key)}
                    onToggle={() => openBuckets.toggle(key)}
                  >
                    {rows.map((t) => (
                      <AssignedTicketRow
                        key={t.source + ":" + t.id}
                        t={t}
                        // The MERGE picker offers tickets from this source, and
                        // they are not all in this bucket — the duplicate is
                        // usually sitting in a different state from the ticket
                        // it duplicates, which is half of why nobody spotted it.
                        // So the row gets the unfiltered list, not `rows`.
                        all={tickets || []}
                        agents={agents}
                        // Falls back to the app default so the picker never
                        // offers a bare "Configured" with nothing named — which
                        // is what a ticket from a just-removed source would show.
                        configuredAgent={sourceAgents[t.source] || defaultAgent}
                        configuredDepth={sourceDepths[t.source] || ""}
                        configuredEffort={sourceEfforts[t.source] || ""}
                        onStarted={relistTickets}
                        eligibleLabel={eligibleLabel}
                        autoOn={autoOn}
                        pick={{
                          checked: picked.has(pickKey(t)),
                          onChange: (on) => setPick(t, on),
                        }}
                      />
                    ))}
                  </WorkGroup>
                );
              });

          const cantConnect = connectErrorOf(src);
          const buckets =
            tickets === null || (filter.active && !total) ? null : cantConnect && !inner.size ? (
              <div className="repo-empty ik-source-error" data-connect-error={src}>
                {cantConnect}
              </div>
            ) : srcError ? (
              <div className="repo-empty ik-source-error">{friendlySourceError(label, srcError)}</div>
            ) : !inner.size ? (
              // Nothing at all, vs. nothing in the buckets you chose to show —
              // two different situations, and only one of them is your filter.
              <div className="repo-empty">
                {configured.get(src)?.assignee_scope === "anyone"
                  ? "No tickets in this source's states right now."
                  : "No tickets are assigned to you on this source."}
              </div>
            ) : !shownBuckets.length ? (
              <div className="repo-empty">
                No tickets from this source in the states you're showing.
              </div>
            ) : !nestWorkflows ? (
              bucketsOf(null)
            ) : (
              workflows.map((wf) => {
                const rows = shownBuckets.filter((b) => groupOf(b) === wf);
                const n = rows.reduce((acc, b) => acc + (inner.get(b) || []).length, 0);
                const key = src + "::wf::" + wf;
                return (
                  <WorkGroup
                    key={key}
                    indent
                    middle
                    name={wf || "Other states"}
                    count={n}
                    open={openWorkflows.isOpen(key)}
                    onToggle={() => openWorkflows.toggle(key)}
                  >
                    {bucketsOf(wf)}
                  </WorkGroup>
                );
              })
            );
          // The heading's note: the problem when there is one (red, with Fix),
          // else what the source auto-starts — which IS the setting, so
          // pressing it opens the editor at the state picker.
          const detail = filter.active && !total && !head.broken
            ? "no match"
            : head.broken
              ? head.message
              : filt.kind === "labels"
                ? scope + "auto-starts tickets with " + filt.text
                : filt.kind === "waiting"
                  ? scope + "auto-starts nothing yet — pick a state"
                  : scope + "auto-starts " + filt.text;
          return (
            <WorkGroup
              key={src}
              groupKey={src}
              heading
              name={label}
              count={tickets === null ? undefined : total}
              detail={detail}
              detailTone={head.broken ? "bad" : filt.kind === "waiting" ? "warn" : undefined}
              onDetail={() => onOpenEditor(src)}
              detailTitle={
                head.broken ? "Fix " + label : "Change which tickets " + label + " auto-starts"
              }
              open={openSources.isOpen(src)}
              onToggle={() => openSources.toggle(src)}
              action={
                <HeadAction
                  editing={isEditing}
                  broken={head.broken}
                  name={label}
                  onClick={() => onEdit(src)}
                />
              }
              editor={isEditing ? editorFor(src) : undefined}
            >
              {buckets}
            </WorkGroup>
          );
        })}
      {picked.size > 0 && (
        // Sticky to the bottom of the dialog's scroll, so it stays in reach
        // while more rows are ticked further down. Opens New in list mode with
        // the tickets already in its box — the lane, PR grouping and pace are
        // chosen there, in the one place a batch is described.
        <div className="ik-pickbar" role="region" aria-label="Picked tickets">
          <span>
            {picked.size} ticket{picked.size === 1 ? "" : "s"} ·{" "}
          </span>
          <button
            type="button"
            id="tk-start-together"
            className="ik-pick-go"
            onClick={() => {
              const text = startTogetherText([...picked.values()]);
              setPicked(new Map());
              useUi.getState().openNewWith(text);
            }}
          >
            Start together…
          </button>
          <button type="button" className="linklike" onClick={() => setPicked(new Map())}>
            Clear
          </button>
        </div>
      )}
    </WorkListPanel>
  );
}

function AssignedTicketRow({
  t,
  all,
  agents,
  configuredAgent,
  configuredDepth,
  configuredEffort,
  onStarted,
  eligibleLabel,
  autoOn,
  pick,
}: {
  t: AssignedTicket;
  /** "will auto-start" / "auto-start off". */
  eligibleLabel: string;
  autoOn: boolean;
  /** The "start together" tick box. */
  pick?: { checked: boolean; onChange(on: boolean): void };
  /** Every ticket the panel holds, for the Merge picker's candidate list. */
  all: AssignedTicket[];
  agents: string[];
  /** What this ticket's source is configured to run, for the picker's label. */
  configuredAgent: string;
  /** How far this ticket's source is configured to take its items. */
  configuredDepth: string;
  /** How hard this ticket's source is configured to think about its items. */
  configuredEffort: string;
  onStarted(): void;
}) {
  const [merging, setMerging] = useState(false);
  // No Merge control on a source whose adapter cannot write (Asana today), and
  // none on a ticket with nothing to merge into: a chip that opens a drawer
  // saying "there is nothing here" is worse than no chip.
  const canMerge =
    t.merge_ready !== false &&
    all.some((o) => o.source === t.source && String(o.id) !== String(t.id));
  return (
    <WorkItemRow
      pick={pick}
      agents={agents}
      configuredAgent={configuredAgent}
      configuredDepth={configuredDepth}
      configuredEffort={configuredEffort}
      reference={t.slug}
      url={t.url}
      title={t.name}
      linkTitle={"Open " + t.slug + " in " + (t.source_label || t.source)}
      tooltip={t.slug + " — " + (t.name || "") + "\nfrom " + (t.source_label || t.source)}
      // Whose it is, but only when it isn't yours — on an assigned-to-me queue
      // every row would say your own name, which tells you nothing.
      meta={
        t.mine === false
          ? ageText(t.created_at) + " · " + (t.assignee || "someone else")
          : ageText(t.created_at)
      }
      // Where it goes: a source spanning several repos says which and why; a
      // one-repo source just names its repo (cheap, and it answers "where
      // will this start?" on every row).
      route={
        t.route ||
        (t.repo_url
          ? { repos: [repoSlugOf(t.repo_url)], repo_url: t.repo_url, method: ONLY_REPO }
          : undefined)
      }
      hasSession={t.has_session}
      session={t.session}
      elsewhere={t.elsewhere?.label}
      eligible={t.eligible}
      eligibleLabel={eligibleLabel}
      eligibleOn={autoOn}
      reasons={t.reasons}
      housekeeping={
        // Tracker housekeeping, not intake: at the bottom of the row's Options
        // under "On the tracker", never beside Start.
        canMerge ? (
          <MergeButton
            reference={t.slug}
            open={merging}
            onToggle={() => setMerging((v) => !v)}
          />
        ) : null
      }
      onDrawerClose={() => setMerging(false)}
      drawer={
        merging ? (
          <MergeDrawer
            row={t}
            all={all}
            hasSession={t.has_session}
            onClose={() => setMerging(false)}
            // The merged-away ticket is still in the panel's cached listing and
            // the survivor's content just changed. Immediate, not the 5s delay
            // the start paths use: nothing is provisioning here, the tracker has
            // already answered, and the row that must disappear is the one the
            // user is looking at.
            onDone={onStarted}
          />
        ) : null
      }
      actionLabel="Start"
      failPrefix="Start failed"
      workspace={t.workspace}
      onReopen={async () => {
        const title = await reopenIntakeItem({
          kind: "tickets",
          source: t.source,
          id: t.id,
        });
        // The row's chips (and its Reopen button) are about to be wrong: it
        // has a session now. Same delay the start path uses.
        setTimeout(onStarted, 5000);
        return title;
      }}
      onStart={async ({ agent, depth, effort, repo }) => {
        const start = (override: boolean) =>
          api<{ title?: string }>("/api/tickets/start", {
            json: {
              source: t.source,
              id: t.id,
              ...(agent ? { agent } : {}),
              ...(depth ? { depth } : {}),
              ...(effort ? { effort } : {}),
              ...(repo ? { repo } : {}),
              ...(override ? { override_claim: true } : {}),
            },
          });
        let r: { title?: string };
        try {
          r = await start(false);
        } catch (err) {
          // The tracker says a teammate is on it: the same card the row's
          // failure raises (same title + text, so it is drawn once), with the
          // owner's Override on it.
          if (claimedBy(err))
            errorPop("Start failed — " + t.slug, (err as Error).message, [
              {
                label: "Override",
                primary: true,
                title: "Start it here anyway — the ticket stays assigned to them",
                run: () =>
                  void start(true).then(
                    (ok) => {
                      refreshInstances();
                      setTimeout(onStarted, 5000);
                      toast("Session " + (ok?.title || t.slug) + " — starting, see the sidebar");
                    },
                    (e) => errorPop("Start failed — " + t.slug, (e as Error).message || "error")
                  ),
              },
            ]);
          // Never the tracker's raw JSON ("Shortcut API returned 401 …
          // {"message":"Unauthorized"}"): one sentence saying what to fix.
          if (!claimedBy(err))
            throw new Error(
              friendlyStartError(t.source_label || t.source, (err as Error).message || "")
            );
          throw err;
        }
        // The server already has a provisioning row for it: pull it now
        // instead of leaving the sidebar blank until the next poll.
        refreshInstances();
        setTimeout(onStarted, 5000);
        return { message: "Session " + (r?.title || t.slug), title: r?.title };
      }}
    />
  );
}

/** The source editor's three parts — the add flow's steps, minus picking
 * the tracker (that is fixed once it exists). */
type EditPart = "connect" | "takes" | "goes";
const EDIT_PARTS: Array<[EditPart, string]> = [
  ["connect", "Connect"],
  ["takes", "What it takes"],
  ["goes", "Where it goes"],
];

/** One line naming what the Advanced fold holds that differs from the
 * defaults ("Name: Jira – EU · Effort: high · Claim label: mindflock") — so a
 * closed fold still says what it changes. "" when nothing in it is set. */
export function advancedSummaryOf(
  meta: CatalogEntry | null,
  source: Source,
  states: TicketingWorkflowState[] = [],
  effortUsable = true
): string {
  const parts: string[] = [];
  if ((source.label || "").trim()) parts.push("Name: " + source.label.trim());
  if (effortUsable && (source.effort || "").trim()) parts.push("Effort: " + source.effort);
  for (const f of advancedFields(meta)) {
    if (f.auto) continue;
    const v = (source[f.key] || "").trim();
    if (!v) continue;
    const shown =
      f.type === "state_one" || f.type === "state"
        ? v
            .split(",")
            .map((id) => stateName(id.trim(), states) || id.trim())
            .join(", ")
        : f.type === "choice"
          ? (f.options || []).find((o) => o.value === v)?.label || v
          : v;
    parts.push(f.label + ": " + shown);
  }
  if (parts.length <= 3) return parts.join(" · ");
  return parts.slice(0, 3).join(" · ") + " · +" + (parts.length - 3) + " more";
}

/** One ticket source's settings, in the order a first-timer fills them:
 * **Connection** (the credentials and the Test that proves them — it also
 * loads the workflow states the next fields pick from), **What it takes**
 * (whose tickets, which states/labels), **Where it goes, and who works it**
 * (repo(s), agent, fast-track), then **Advanced options**.
 *
 * Three ways in, one component: `edit` is the editor attached under a source's
 * heading (all sections, its save line and Remove); `connect` and `setup` are
 * steps 2 and 3 of "+ Add source", which owns its own footer. */
function TicketSourceEditor({
  section,
  source,
  catalog,
  agents,
  fieldErrors,
  save,
  dirty,
  onDirty,
  gotoTab,
  onAddRepo,
  saved,
  listError,
  test,
  onTest,
  onChange,
  onRemove,
}: {
  section: "edit" | "connect" | "setup";
  source: Source;
  catalog: CatalogEntry[];
  agents: AgentChoices;
  /** What the server refused on the last save, per field — the card is unsaved. */
  fieldErrors?: FieldErrors;
  /** What this card's last edit did ("Saving…" / "Saved ✓" / "Not saved"). */
  save?: SaveState;
  /** A field on this card holds typed text not sent yet. */
  dirty: boolean;
  /** A field started (`on`) or stopped holding unsent text. */
  onDirty(key: string, on: boolean): void;
  gotoTab(key: IntakeTabKey): void;
  /** In the add flow: switch to adding a Repository (GitHub Issues' "every new
   * issue in a repo" lives there, not on a tracker). */
  onAddRepo?: () => void;
  /** Whether the server holds this card at all (a new card may not, yet). */
  saved: boolean;
  /** The ticket listing's error for this source, when it couldn't be listed. */
  listError?: string;
  /** The Test in flight or its last word (a click's or a save's). */
  test?: TestResult;
  /** Test the SAVED source and record the verdict on it. */
  onTest(): Promise<string>;
  /** A null value clears the field (secrets); strings set it. */
  onChange(patch: Record<string, string | null>): void;
  onRemove(): void;
}) {
  const meta = catalog.find((p) => p.id === source.provider) || null;
  const errs = fieldErrors || {};
  const unsaved = Object.keys(errs).length > 0;
  // The pill: the server's rule over the stored verdict — except that a card
  // the server refused to save is "not saved", whatever its old verdict said.
  const verdict = sourceTestState(source, meta);
  // A test that just failed outranks a stored "not tested yet" (a card whose
  // test ran before it was saved has no stored verdict), and a listing that
  // can't reach the tracker outranks a day-old pass — the pill says what is
  // true NOW, in the same words the work list uses.
  const liveFail = test && !test.testing && test.msg && !test.ok ? test.msg : "";
  const cantList = !!listError && verdict.state !== "off";
  const pillState = unsaved ? "needs_attention" : liveFail || cantList ? "needs_attention" : verdict.state;
  const pillLabel = unsaved
    ? "Not saved"
    : liveFail || cantList
      ? "Can't connect"
      : TEST_STATE_LABELS[pillState];
  const pillMessage = unsaved
    ? "fill in the marked fields"
    : liveFail
      ? "Last test: " + liveFail
      : cantList
        ? friendlySourceError(sourceDisplayName(source, catalog), listError || "", true)
        : verdict.message;
  const stored: TicketingLastTest | null = lastTestOf(source);
  // Which CLI this source's tickets will actually run on, so the effort picker
  // below can name THAT CLI's ceiling rather than the ladder's. The empty choice
  // in the Agent picker means "app default", which is what `agents.fallback`
  // holds — so the two pickers agree about which CLI is under discussion.
  const effortProvider = source.agent || agents.fallback || "";
  // undefined = caps not fetched yet, or a custom program no provider claims.
  // That reads as "assume it works" rather than disabling a control that does.
  const effortCaps = useProviderEfforts().data;
  const effortCap = effortCaps ? effortCaps[effortProvider] : undefined;
  const effortUsable = supportsEffort(effortCap);
  // The pickers open on what the last Test listed and refresh on focus.
  const [states, setStates] = useState<TicketingWorkflowState[]>(() => savedStates(source));
  const savedStateKey = stored?.at || "";
  useEffect(() => {
    const fresh = savedStates(source);
    if (fresh.length) setStates(fresh);
    // Re-seed when a new verdict lands (its `at` changes), not on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [savedStateKey]);

  // A GitHub Issues source saved before the one GitHub credential path still
  // carries its own token (the server masks it as "•••set"): still read, as
  // the last fallback, this release only.
  const ownGithubToken = source.provider === "github_issues" && source.api_token === "•••set";

  const testPayload = () => {
    const payload: Record<string, string> = { id: source.id, provider: source.provider };
    for (const f of meta?.fields || []) {
      const v = source[f.key];
      if (f.secret && !v) continue; // blank secret => omit
      if (v != null) payload[f.key] = v;
    }
    if (source.repo_url) payload.repo_url = source.repo_url;
    return payload;
  };

  const loadStates = async () => {
    try {
      const r = await api<{ ok?: boolean; states?: TicketingWorkflowState[] }>(
        "/api/settings/ticketing/states",
        { json: testPayload() }
      );
      if (r?.ok && Array.isArray(r.states) && r.states.length) setStates(r.states);
    } catch {
      /* keep what the last Test listed */
    }
  };

  // A source tested before states were stored with the verdict has nothing to
  // open the picker on, and a disabled picker never gets the focus that would
  // load them: ask once, on mount, when the last Test passed.
  useEffect(() => {
    if (!states.length && stored?.ok) loadStates();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source.id]);

  // Open when something in the fold is set or refused; stays however the user
  // left it otherwise — and a NEW refusal inside it opens it again.
  const wantAdvanced = advancedHasValues(meta, source, errs);
  const [advancedOpen, setAdvancedOpen] = useState(wantAdvanced);
  useEffect(() => {
    if (wantAdvanced) setAdvancedOpen(true);
  }, [wantAdvanced]);

  // The Name field saves as you type, like every other text field here.
  const labelSave = useAutosave<string>(
    (v) => {
      onDirty("label", false);
      if (v !== (source.label || "")) onChange({ label: v });
    },
    () => onDirty("label", true)
  );

  const renderField = (f: CatalogField) =>
    f.type === "state" ? (
      <StatePicker
        key={f.key}
        field={f}
        source={source}
        states={states}
        loadStates={loadStates}
        onChange={onChange}
      />
    ) : f.type === "state_one" ? (
      <StartStatePicker
        key={f.key}
        field={f}
        source={source}
        states={states}
        loadStates={loadStates}
        onChange={onChange}
      />
    ) : f.type === "choice" ? (
      <ChoicePicker key={f.key} field={f} source={source} onChange={onChange} error={errs[f.key]} />
    ) : (
      <CatalogInput
        key={f.key}
        field={f}
        source={source}
        saved={saved}
        error={errs[f.key]}
        onChange={onChange}
        onDirty={(on) => onDirty(f.key, on)}
      />
    );

  // The form in the order a first-timer fills it: the key that unlocks the
  // tracker, a Test that proves it (and loads the workflow states the next
  // fields pick from), then whose tickets and which, then where they go and
  // who works them. Everything a first setup can leave alone is folded.
  const basics = basicFields(meta);
  const cut = basics.findIndex((f) => f.key === "assignee_scope" || f.type === "state");
  const credFields = cut < 0 ? basics : basics.slice(0, cut);
  const filterFields = cut < 0 ? [] : basics.slice(cut);
  // Whose tickets and the states it takes sit side by side: the rule that ties
  // them ("Anyone's needs a state") is then checked on one line.
  const paired =
    filterFields.length >= 2 && filterFields[0].key === "assignee_scope" && filterFields[1].type === "state";
  const saveLine = !saved
    ? unsaved
      ? "Not saved — fill in the marked fields"
      : dirty
        ? "Saving…"
        : "Not saved yet"
    : dirty || save?.state === "saving" || save?.state === "pending"
      ? "Saving…"
      : save?.state === "failed"
        ? "Not saved"
        : save?.state === "saved"
          ? "Saved ✓"
          : "";
  const name = sourceDisplayName(source, catalog);
  // The editor under a heading is the add flow's steps again, one at a time:
  // Connect, What it takes, Where it goes — a source that can't connect opens
  // on Connect, a working one on what it takes (the heading's note is a
  // shortcut there). Round 5: all of it on one scroll was ~17 controls.
  const [part, setPart] = useState<EditPart>(() =>
    pillState === "connected" && !unsaved ? "takes" : "connect"
  );
  const heads = section === "edit";
  const showConnect = section === "connect" || (heads && part === "connect");
  const showTakes = section === "setup" || (heads && part === "takes");
  const showGoes = section === "setup" || (heads && part === "goes");
  const advancedSummary = advancedSummaryOf(meta, source, states, effortUsable);

  return (
    <div
      className="ik-editor-body tk-source-editor"
      data-source-id={source.id}
      data-section={section}
      data-part={heads ? part : undefined}
    >
      {heads ? (
        <div className="ik-edit-steps" role="tablist" aria-label={"Settings for " + name}>
          {EDIT_PARTS.map(([key, label], i) => (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={part === key}
              className={"ik-edit-step" + (part === key ? " cur" : "")}
              data-part={key}
              onClick={() => setPart(key)}
            >
              <span className="ik-step-num">{i + 1}</span>
              {label}
              {key === "connect" ? (
                <SourcePill state={pillState} label={pillLabel} message={pillMessage} />
              ) : null}
            </button>
          ))}
        </div>
      ) : null}
      {showConnect ? (
        <>
          {heads ? null : meta?.blurb ? (
            <p className="set-hint tk-blurb">{meta.blurb}</p>
          ) : null}
          {source.provider === "github_issues" && section === "connect" ? (
            // The two GitHub-issue homes, told apart: this one is "issues
            // assigned to me, as tickets"; a watched Repository is "every NEW
            // issue in a repo".
            <p className="set-hint tk-gh-issues-note" role="note">
              Want every new issue in a repo to start on its own?{" "}
              <button
                type="button"
                className="linklike"
                onClick={() => (onAddRepo ? onAddRepo() : gotoTab("issues"))}
              >
                Add it as a Repository instead
              </button>
              .
            </p>
          ) : null}
          {ownGithubToken && (
            <p className="set-hint tk-own-token" role="note" data-tk-notice="own-token">
              This source still has its own token — move it to Settings → Connections → GitHub →
              Connect (read as a fallback this release, ignored from the next)
            </p>
          )}
          <div className="tk-fields">{credFields.map(renderField)}</div>
          {/* Test sits right under the credentials it checks — it is also what
              loads the workflow states the fields below pick from. In the add
              flow "Test & continue" is the test, so there is no second button. */}
          {section === "connect" ? null : (
          <div className="set-row tk-test-row">
            <span className="test-row">
              <TestButton label={stored ? "Test again" : "Test"} onTest={onTest} result={test} />
              {!test?.msg && !test?.testing ? (
                <span className="ik-source-message" title={pillMessage}>
                  {pillMessage}
                </span>
              ) : null}
            </span>
          </div>
          )}
        </>
      ) : null}
      {showTakes ? (
        <>
          {section === "setup" ? <h4 className="ik-editor-h">What it takes</h4> : null}
          {paired ? (
            <div className="ik-grid ik-grid-2">
              {renderField(filterFields[0])}
              {renderField(filterFields[1])}
            </div>
          ) : null}
          <div className="tk-fields">{(paired ? filterFields.slice(2) : filterFields).map(renderField)}</div>
        </>
      ) : null}
      {showGoes ? (
        <>
          {section === "setup" ? <h4 className="ik-editor-h">Where it goes, and who works it</h4> : null}
          <div data-tour="intake-repo">
          {/* One repo, or a set of them with per-repo hints (repoList.ts). */}
          <RepoListField
            source={source}
            onChange={(patch) => {
              onDirty("repos", false);
              onChange(patch);
            }}
            onPending={() => onDirty("repos", true)}
            onSettled={() => onDirty("repos", false)}
          />
          </div>
          <div className="ik-grid ik-grid-2">
            <label className="set-row">
              <span className="set-label">Agent</span>
              <select
                className="tk-agent"
                data-tk-field="agent"
                value={source.agent || ""}
                onChange={(e) => onChange({ agent: e.target.value })}
              >
                <option value="">
                  {agents.fallback ? `App default (${agents.fallback})` : "App default"}
                </option>
                {agents.names.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
              <span className="set-hint">Works the tickets from this source.</span>
            </label>
            <label className="set-row">
              <span className="set-label">Fast-track to</span>
              <select
                className="tk-depth"
                data-tk-field="depth"
                value={source.depth === "agent" ? "" : source.depth || ""}
                onChange={(e) => onChange({ depth: e.target.value })}
              >
                {/* One ladder, the same words as every other Fast-track control.
                    A stored "agent" (stop once the agent stops) IS Off, so it
                    reads as Off rather than as a sixth rung. */}
                <option value="">{LANE_LABEL.leave}</option>
                {SOURCE_DEPTHS.map((d) => (
                  <option key={d} value={d}>
                    {LANE_LABEL[d]}
                  </option>
                ))}
              </select>
              <span className="set-hint">After the agent finishes. Merge is per ticket only.</span>
            </label>
          </div>
          {/* BASIC vs ADVANCED. Everything above is what a first setup needs to
              get tickets flowing; the per-queue tuning and the catalog's
              `advanced` extras (claim label, move on start…) fold away here.
              The fold opens on its own when anything inside is already set or
              was refused by the server, so a stored value or an error is never
              hidden. */}
          <details
            className="tk-advanced"
            data-tk-advanced={source.id}
            open={advancedOpen}
            onToggle={(e) => setAdvancedOpen((e.currentTarget as HTMLDetailsElement).open)}
          >
            <summary>
              Advanced options
              <span className="tk-advanced-sum" data-tk-advanced-sum>
                {" — " + (advancedSummary || "nothing changed")}
              </span>
            </summary>
            <div className="tk-advanced-body">
              <label className="set-row">
                <span className="set-label">Name</span>
                <input
                  type="text"
                  className="tk-label"
                  placeholder="e.g. Jira – EU"
                  defaultValue={source.label || ""}
                  onChange={(e) => labelSave.schedule(e.target.value)}
                  onBlur={labelSave.flush}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      labelSave.flush();
                    }
                  }}
                />
                <span className="set-hint">
                  What this source is called in the ticket list — handy with two of one tracker.
                </span>
              </label>
              <label className="set-row">
                <span className="set-label">Thinking effort</span>
                <select
                  className="tk-effort"
                  data-tk-field="effort"
                  value={effortUsable ? source.effort || "" : ""}
                  disabled={!effortUsable}
                  title={effortTitle(effortProvider, effortCap)}
                  onChange={(e) => onChange({ effort: e.target.value })}
                >
                  <option value="">
                    {effortUsable
                      ? "Agent's default"
                      : "No effort setting (" + (effortProvider || "this agent") + ")"}
                  </option>
                  {effortUsable &&
                    EFFORTS.map((e) => (
                      // A rung above this CLI's ceiling still runs, clamped, and the top
                      // rung is named the way the CLI names it — so the pick says what it
                      // will actually do rather than what was asked for.
                      <option key={e} value={e}>
                        {effortOptionLabel(e, effortCap)}
                      </option>
                    ))}
                </select>
                <span className="set-hint">
                  How hard the agent thinks about every ticket from this source. Safe to set
                  higher than the agent supports — it uses its own top level. A row's Options
                  can pick another for one ticket.
                </span>
              </label>
              {advancedFields(meta).map(renderField)}
            </div>
          </details>
        </>
      ) : null}
      {section === "edit" ? (
        <div className="ik-editor-foot">
          <span
            className={"ik-save-line" + (save?.state === "saved" && saved && !dirty ? " ok" : "")}
            data-save={!saved ? "unsaved" : dirty ? "pending" : save?.state || ""}
            role="status"
            aria-live="polite"
          >
            {saveLine}
          </span>
          <ArmButton
            className="tk-remove"
            label="Remove"
            armedLabel={"Confirm — remove " + name}
            title={"Stop taking tickets from " + name + " — its sessions are kept"}
            onConfirm={onRemove}
          />
        </div>
      ) : null}
    </div>
  );
}

/** The label of a catalog field with its data-driven marker: the catalog's
 * `required` says which fields the server refuses a save without, and `auto`
 * which of those Test fills in rather than the user. */
function FieldLabel({ field }: { field: CatalogField }) {
  const marker = requiredMarker(field);
  return (
    <span className="set-label">
      {field.label}
      {marker ? (
        <span
          className={"ik-field-marker" + (marker === "required" ? " required" : "")}
          data-marker={marker}
        >
          {marker === "required" ? "*" : "(filled in by Test)"}
        </span>
      ) : null}
    </span>
  );
}

/** The hint under a field, or the server's objection to it in its place. */
function FieldNote({ field, error }: { field: CatalogField; error?: string }) {
  if (error) return <span className="ik-field-error" data-tk-error={field.key}>{error}</span>;
  return field.hint ? <span className="set-hint">{field.hint}</span> : null;
}

/** A plain catalog field: text, or a password with a Clear control once a
 * value is stored (sending `null` — a blank password means "keep"). An Asana
 * workspace the token could not pick alone is offered as a list instead. */
function CatalogInput({
  field: f,
  source,
  saved = true,
  error,
  onChange,
  onDirty,
}: {
  field: CatalogField;
  source: Source;
  /** Whether the server holds this source. An add-flow draft doesn't: its
   * typed secret stays in the box — never "•••set (saved)" over a token
   * nothing has saved. */
  saved?: boolean;
  error?: string;
  onChange(patch: Record<string, string | null>): void;
  /** The box holds typed text not sent yet (`true`), or no longer (`false`). */
  onDirty?(on: boolean): void;
}) {
  // Sent when typing pauses, on blur, on Enter and when the card goes away —
  // a typed value never sits unsaved under "Saved ✓".
  const save = useAutosave<string>(
    (v) => {
      onDirty?.(false);
      // Blank password => keep stored secret (omit from patch); Clear is
      // the one way to drop it.
      if (f.secret && v === "") return;
      if (v !== (source[f.key] || "")) onChange({ [f.key]: v });
    },
    () => onDirty?.(true)
  );
  const choices = f.key === "project" && f.auto ? workspaceChoices(source) : [];
  if (choices.length) {
    return (
      <label className="set-row">
        <FieldLabel field={f} />
        <select
          className="tk-choice"
          data-tk-field={f.key}
          data-tk-workspaces={choices.length}
          value=""
          onChange={(e) => {
            if (e.target.value) onChange({ [f.key]: e.target.value });
          }}
        >
          <option value="">Pick a workspace…</option>
          {choices.map((w) => (
            <option key={w.gid} value={w.gid}>
              {w.name}
            </option>
          ))}
        </select>
        <FieldNote field={f} error={error} />
      </label>
    );
  }
  const stored = saved && hasSecret(source, f);
  const input = (
    <input
      // A cleared secret remounts the (uncontrolled) input, so the old
      // characters don't linger in a field the server no longer holds.
      key={f.secret ? (stored ? "set" : "empty") : undefined}
      type={f.secret ? "password" : "text"}
      autoComplete="off"
      data-tk-field={f.key}
      className={error ? "field-missing" : ""}
      aria-invalid={error ? true : undefined}
      placeholder={f.secret && stored ? "•••set (saved) — type to replace" : f.placeholder || ""}
      defaultValue={f.secret && saved ? "" : source[f.key] || ""}
      onChange={(e) => save.schedule(e.target.value)}
      onBlur={save.flush}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          save.flush();
        }
      }}
    />
  );
  const tokenUrl = f.secret ? TOKEN_URLS[source.provider] : "";
  return (
    <label className="set-row" data-tour={f.secret ? "intake-token" : undefined}>
      <FieldLabel field={f} />
      {f.secret && stored ? (
        <span className="ik-secret-row">
          {input}
          <button
            type="button"
            className="ik-secret-clear"
            data-tk-clear={f.key}
            title={"Forget the stored " + f.label.toLowerCase()}
            onClick={(e) => {
              e.preventDefault();
              onChange(clearSecretPatch(f.key));
            }}
          >
            Clear
          </button>
        </span>
      ) : (
        input
      )}
      <FieldNote field={f} error={error} />
      {tokenUrl ? (
        <a
          className="ik-token-link"
          href={tokenUrl}
          target="_blank"
          rel="noopener noreferrer"
          data-tk-token-link={source.provider}
        >
          Create a token ↗
        </a>
      ) : null}
    </label>
  );
}
