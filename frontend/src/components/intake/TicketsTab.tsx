/** Intake → Tickets: connect ticketing platforms and MindFlock turns each ticket
 * assigned to you into a coding session.
 *
 * The tab the other two were modelled on (see ./kit.tsx): master switch, one
 * collapsible card per connected source, then the assigned tickets those
 * sources yielded.
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
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import {
  putTicketingSources,
  refreshConfig,
  refreshInstances,
  useAgentChoices,
  usePanelQuery,
  useTicketingCatalog,
  useTicketingSources,
} from "../../state/queries";
import { toast } from "../../lib/toast";
import { errorPop } from "../../lib/errorPop";
import {
  AutomationSwitch,
  SourceCard,
  TestButton,
  useListFilter,
  WorkGroup,
  WorkItemRow,
  WorkListPanel,
  ageText,
  panelNote,
  reopenIntakeItem,
  useToggleSet,
  type ItemWorkspace,
} from "./kit";
import {
  NO_STATE_BUCKET,
  loadMineOnly,
  loadShownBuckets,
  saveMineOnly,
  saveShownBuckets,
  visibleBuckets,
} from "./buckets";
import { DEPTH_LABELS, SOURCE_DEPTHS } from "../../lib/autopilot";
import {
  EFFORTS,
  effortOptionLabel,
  effortTitle,
  supportsEffort,
} from "../../lib/effort";
import { useProviderEfforts } from "../../state/queries";
import type { TabProps } from "./IntakeDialog";
import { ticketMatches } from "./search";
import type {
  TicketingCatalogEntry,
  TicketingCatalogField,
  TicketingSource,
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

/** Master ticket-ingestion switch — the Intake-tab twin of the sidebar's
 * AutomationBar. Same server contract (GET /api/mindflock/status for the
 * desired state, POST /api/mindflock/{start,stop} to flip it), the same
 * ["mindflock-status"] query key AND the same 4s interval, so the two switches
 * stay in lock-step. Clicking the row text does nothing — only the switch
 * itself flips it. */
function IngestionToggle({ sourceCount }: { sourceCount: number }) {
  const [busy, setBusy] = useState(false);
  const [optimistic, setOptimistic] = useState<boolean | null>(null);
  const { data: status, refetch } = useQuery({
    queryKey: ["mindflock-status"],
    queryFn: () =>
      api<{ available: boolean; running: boolean; desired?: boolean }>("/api/mindflock/status"),
    // Matches the sidebar bars' interval exactly: a shared query key with two
    // different intervals makes which component mounted first decide the poll
    // rate, which is the kind of bug that only shows up as "the light feels
    // laggy sometimes".
    refetchInterval: 4_000,
    retry: false,
  });

  // No engine installed / reachable — nothing to toggle (matches the sidebar).
  if (!status || !status.available) return null;
  const running = !!status.running;
  const desired = optimistic ?? (status.desired ?? running);

  const toggle = async (start: boolean) => {
    if (busy) return;
    setBusy(true);
    setOptimistic(start);
    try {
      await api(`/api/mindflock/${start ? "start" : "stop"}`, { method: "POST" });
      toast(start ? "Ticket ingestion on" : "Ticket ingestion paused");
    } catch (err) {
      // Failures go to the bottom-right card (lib/errorPop.ts), not to the
      // confirmation strip: the switch has snapped back and the reason why is
      // the only thing left worth reading.
      errorPop(
        `Ticket ingestion ${start ? "start" : "stop"} failed`,
        (err as Error).message || "the server gave no reason"
      );
    } finally {
      setBusy(false);
      setOptimistic(null);
      refetch();
    }
  };

  return (
    <AutomationSwitch
      label="Automated ingestion"
      title="Run or stop ticket ingestion — polls your connected sources and auto-creates a coding session for each assigned ticket. Stays in this state across restarts."
      rowId="tk-ingestion-toggle-row"
      inputId="tk-ingestion-enabled"
      statusId="tk-ingestion-status"
      checked={desired}
      onChange={(next) => { if (!busy) toggle(next); }}
      note={sourceCount ? undefined : "Add a ticketing source below and this starts polling it"}
    />
  );
}

/** Stable "nothing yet" identities, so a tab rendering before its caches land
 * doesn't hand its children a new array on every frame. */
const EMPTY_CATALOG: CatalogEntry[] = [];
const EMPTY_AGENTS: AgentChoices = { names: [], fallback: "" };

export function TicketsTab(_: TabProps) {
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
  // Already-saved sources start collapsed (a summary chip).
  const [collapsed, setCollapsed] = useState<Set<string>>(
    () => new Set((savedSources.data || []).map((s) => s.id))
  );
  const seeded = useRef(savedSources.data !== undefined);
  const seq = useRef(0);

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
    setSources(savedSources.data);
    setCollapsed(new Set(savedSources.data.map((s) => s.id)));
  }, [savedSources.data, savedSources.isError]);

  const persist = useCallback(
    async (list: Source[]) => {
      const mySeq = ++seq.current;
      const missingRepo = list.filter((s) => !(s.repo_url || "").trim()).length;
      try {
        await api("/api/settings/ticketing/sources", { method: "PUT", json: { sources: list } });
        if (mySeq !== seq.current) return;
        // Keep the shared cache in step, so the next open of this tab shows what
        // was saved instead of refetching to be told the same thing.
        putTicketingSources(list);
        toast(
          missingRepo
            ? `Saved — but ${missingRepo} source(s) need a Repo URL to ingest`
            : "Saved ticketing sources"
        );
        // Connecting/removing a source flips the ticketing capability.
        refreshConfig();
      } catch (err) {
        errorPop(
          "Ticketing sources not saved",
          (err as Error).message || "the server rejected the ticketing settings"
        );
      }
    },
    []
  );

  if (sources === null) return <p className="set-hint">Loading…</p>;

  const uniqueId = (base: string) => {
    const taken = new Set(sources.map((s) => s.id));
    let cand = base,
      n = 1;
    while (taken.has(cand)) {
      n += 1;
      cand = `${base}-${n}`;
    }
    return cand;
  };

  const update = (id: string, patch: Record<string, string>) => {
    setSources((prev) => {
      const next = (prev || []).map((s) => (s.id === id ? ({ ...s, ...patch } as Source) : s));
      persist(next);
      return next;
    });
  };

  const remove = (id: string) => {
    setSources((prev) => {
      const next = (prev || []).filter((s) => s.id !== id);
      persist(next);
      return next;
    });
  };

  const add = () => {
    const provider = catalog[0]?.id || "shortcut";
    const id = uniqueId(provider);
    setSources((prev) => [...(prev || []), { id, provider } as Source]);
    setCollapsed((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  };

  return (
    <>
      <p className="set-hint set-block-hint">
        Every ticket assigned to you becomes a coding session. Add as many sources as you
        like — two of the same provider is fine; each keeps its own credentials, repo and
        agent. Credentials are stored in <code>~/.mindflock/settings.json</code>, never
        committed.
      </p>
      <IngestionToggle sourceCount={(sources || []).length} />
      <div className="set-row">
        <span className="set-label">Sources</span>
        <div id="ticketing-sources" className="ik-cards">
          {sources.map((src) => (
            <TicketSourceCard
              key={src.id}
              source={src}
              catalog={catalog}
              agents={agents}
              collapsed={collapsed.has(src.id)}
              onToggle={() =>
                setCollapsed((prev) => {
                  const next = new Set(prev);
                  if (next.has(src.id)) next.delete(src.id);
                  else next.add(src.id);
                  return next;
                })
              }
              onChange={(patch) => update(src.id, patch)}
              onRemove={() => remove(src.id)}
            />
          ))}
        </div>
        <div className="set-row">
          <button type="button" id="ticketing-add" className="btn-primary" onClick={add}>
            + Add source
          </button>
        </div>
      </div>
      {sources.length > 0 && (
        <AssignedTickets
          agents={agents.names}
          // Per source, what "Configured" resolves to on a Begin-work picker —
          // the source's own Agent CLI, else the app default.
          sourceAgents={Object.fromEntries(
            sources.map((s) => [s.id, s.agent || agents.fallback || ""])
          )}
          defaultAgent={agents.fallback}
          // Same idea for the depth picker: what "Configured" means on this
          // source's rows is the source's own automation depth.
          sourceDepths={Object.fromEntries(
            sources.map((s) => [s.id, s.depth || ""])
          )}
          // ...and for the effort picker, so a row's empty choice names the
          // queue's own rung instead of the CLI's default. Before the source had
          // an effort there was nothing to name, which is why that picker's blank
          // option used to read "Default effort" unconditionally.
          sourceEfforts={Object.fromEntries(
            sources.map((s) => [s.id, s.effort || ""])
          )}
        />
      )}
    </>
  );
}

/** Multi-state ingest picker: the field stores one or more state ids
 * comma-joined; each shows as a chip with a ✕, and the dropdown appends
 * another. Empty = ingest every state. */
function StatePicker({
  field,
  source,
  states,
  loadStates,
  onChange,
}: {
  field: CatalogField;
  source: Source;
  states: Array<{ id: string | number; name?: string }>;
  loadStates(): void;
  onChange(patch: Record<string, string>): void;
}) {
  const selected = (source[field.key] || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const nameOf = (id: string) => {
    const st = states.find((s) => String(s.id) === id);
    return st?.name || id;
  };
  const remaining = states.filter((s) => !selected.includes(String(s.id)));
  const commit = (list: string[]) => onChange({ [field.key]: list.join(",") });
  // "Anyone's" is carried entirely by this filter — with nothing selected there
  // is nothing to scope a whole-tracker search by, so the source stays on
  // assigned-to-me until a state is picked. Say that where the gap is.
  const needsState = source.assignee_scope === "anyone" && !selected.length;

  return (
    <div className="set-row">
      <span className="set-label">{field.label}</span>
      <div className="repo-list">
        {!selected.length ? (
          <div className="repo-empty">
            {needsState
              ? "Pick at least one state — Anyone's has nothing to go on without it, so this source is still only taking tickets assigned to you."
              : "Any state — every ticket assigned to you is auto-ingested."}
          </div>
        ) : (
          selected.map((id) => (
            <span className="repo-chip" key={id}>
              <span className="repo-chip-name">{nameOf(id)}</span>
              <button
                type="button"
                className="repo-chip-x"
                title={"Stop auto-ingesting " + nameOf(id)}
                aria-label={"Remove ingest state " + nameOf(id)}
                onClick={() => commit(selected.filter((x) => x !== id))}
              >
                ✕
              </button>
            </span>
          ))
        )}
      </div>
      <select
        className="tk-state"
        data-tk-field={field.key}
        value=""
        onFocus={() => {
          if (!states.length) loadStates();
        }}
        onChange={(e) => {
          if (e.target.value) commit([...selected, e.target.value]);
        }}
      >
        <option value="">+ Add state…</option>
        {remaining.map((st) => (
          <option key={String(st.id)} value={String(st.id)}>
            {st.name || String(st.id)}
          </option>
        ))}
      </select>
      <span className="set-hint">
        Tickets in any of these states are auto-ingested; empty = every state.
        Everything else starts manually from the Assigned tickets panel below.
      </span>
    </div>
  );
}

/** A catalog field with a fixed set of values, rendered as a select. */
function ChoicePicker({
  field,
  source,
  onChange,
}: {
  field: CatalogField;
  source: Source;
  onChange(patch: Record<string, string>): void;
}) {
  return (
    <label className="set-row">
      <span className="set-label">{field.label}</span>
      <select
        className="tk-choice"
        data-tk-field={field.key}
        value={source[field.key] || ""}
        onChange={(e) => onChange({ [field.key]: e.target.value })}
      >
        {(field.options || []).map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      {field.hint ? <span className="set-hint">{field.hint}</span> : null}
    </label>
  );
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
  eligible?: boolean;
  reasons?: string[];
  /** False only on a source that ingests anyone's tickets. Absent = yours. */
  mine?: boolean;
  /** Display name(s) of whoever it is assigned to, when the provider says. */
  assignee?: string;
  /** Present when a previous run of this ticket still has its workspace here. */
  workspace?: ItemWorkspace;
}

/** "A", "A and B", "A, B and C", then "A, B and 4 more" — provider state names
 * run long ("Product Development · Ready for Review"), so a hint that names them
 * has to stop somewhere rather than turning into a paragraph. */
function listNames(names: string[]): string {
  if (names.length <= 2) return names.join(" and ");
  if (names.length === 3) return names[0] + ", " + names[1] + " and " + names[2];
  return names.slice(0, 2).join(", ") + " and " + (names.length - 2) + " more";
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
  agents,
  sourceAgents,
  defaultAgent,
  sourceDepths,
  sourceEfforts,
}: {
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
    stale?: boolean;
  }>("tickets");
  const load = ticketsQuery.refresh; // Refresh button: force a sweep
  const relistTickets = ticketsQuery.refetch; // after a force start
  const tickets = ticketsQuery.data ? ticketsQuery.data.tickets || [] : null;
  const buckets = ticketsQuery.data?.buckets || [];
  const doneBuckets = ticketsQuery.data?.done_buckets || [];
  const ingestStates = ticketsQuery.data?.ingest_states || {};
  const sourceLabels = ticketsQuery.data?.source_labels || {};
  const bucketMeta = ticketsQuery.data?.bucket_meta || {};
  /** The workflow/board a bucket belongs to ("" = the source has only one, so
   * there is nothing to nest under). */
  const groupOf = (b: string) => bucketMeta[b]?.group || "";
  /** A bucket's own name. Providers qualify the bucket KEY to keep it unique
   * across workflows ("Product Development · Deferred"); inside its workflow
   * group the qualifier is already written above it, so the heading uses this. */
  const labelOf = (b: string) => bucketMeta[b]?.label || b;
  const listedSources = ticketsQuery.data?.sources || [];
  const sourceErrors = new Map(
    (ticketsQuery.data?.errors || []).map((e) => [e.source, e.error])
  );
  const error = ticketsQuery.error
    ? "Could not list tickets: " + (ticketsQuery.error.message || "error")
    : "";
  const note = panelNote({
    error,
    fetching: ticketsQuery.isFetching,
    loaded: !!tickets,
    // Per-source failures outrank the progress note: a source that can't be
    // reached is the thing worth reading, and it stays true after the sweep.
    detail: [...sourceErrors].map(([s, e]) => (sourceLabels[s] || s) + ": " + e).join(" · "),
  });

  const [shown, setShown] = useState<string[] | null>(loadShownBuckets);
  const [mineOnly, setMineOnly] = useState<boolean>(loadMineOnly);
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
  const sourceOrder = filter.active
    ? [...bySource.keys()]
    : [
        ...listedSources,
        ...[...sourceErrors.keys()].filter((s) => !listedSources.includes(s)),
        ...[...bySource.keys()].filter(
          (s) => !listedSources.includes(s) && !sourceErrors.has(s)
        ),
      ];

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

  /** Which states auto ingestion watches — stated per source, never merged.
   *
   * The merged version ("watches A, B" from one source's A and another's B) was
   * false for both of them: no source watched both. With several sources each
   * group heading already carries its own, so the panel-level hint points there
   * instead of inventing a union. */
  const ingestSummary = sourceOrder.length > 1 ? (
    <>
      Each source heading says which states <em>it</em> auto-ingests; everything else is
      started by hand, with <strong>Begin work</strong>.
    </>
  ) : ingestStates[sourceOrder[0]]?.length ? (
    <>
      Auto ingestion only watches{" "}
      <strong>{listNames(ingestStates[sourceOrder[0]])}</strong> — tickets in any other state
      are only started by hand, with <strong>Begin work</strong>.
    </>
  ) : (
    <>
      No ingest-state filter is set, so auto ingestion watches every state — set one on the
      source card above to narrow it. <strong>Begin work</strong> starts any ticket by hand.
    </>
  );

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
          {hasOthers ? (
            <select
              id="tk-mine-filter"
              className="tk-mine-filter"
              value={mineOnly ? "mine" : "all"}
              title="Whose tickets this panel lists"
              onChange={(e) => {
                const next = e.target.value === "mine";
                setMineOnly(next);
                saveMineOnly(next);
              }}
            >
              <option value="all">Everyone's tickets</option>
              <option value="mine">Only mine</option>
            </select>
          ) : null}
          <select
            id="tk-bucket-add"
            className="tk-bucket-add"
            value=""
            disabled={!hidden.length}
            title="Show another workflow state in this panel"
            onChange={(e) => addBucket(e.target.value)}
          >
            <option value="">{hidden.length ? "+ Add bucket…" : "All buckets shown"}</option>
            {hidden.map((b) => (
              <option key={b} value={b}>
                {b} ({countAll.get(b) || 0})
              </option>
            ))}
          </select>
        </>
      }
      hint={
        <>
          {hasOthers ? (
            <>
              Tickets from your sources — including other people's, from any source set to{" "}
              <strong>Anyone's</strong> — grouped by source and then by workflow state. Use{" "}
              <strong>Only mine</strong> to narrow it back down.{" "}
            </>
          ) : (
            "Your tickets, grouped by source and then by workflow state. "
          )}
          Click a heading to expand or collapse it; use <strong>+ Add bucket…</strong> / ✕ to
          choose which states appear at all.{" "}
          {parkedDone.length ? (
            <>
              {parkedDone.length === 1 ? "The done state " : "Done states "}
              <strong>{listNames(parkedDone)}</strong>{" "}
              {parkedDone.length === 1 ? "starts" : "start"} hidden, so this shows fewer than
              your all-time total.{" "}
            </>
          ) : null}
          {ingestSummary}
        </>
      }
    >
      {error ? (
        <div className="repo-empty">{error}</div>
      ) : tickets === null ? null : !visible.length && buckets.length ? (
        <div className="repo-empty">
          All buckets are hidden — pick one from the “+ Add bucket…” menu above.
        </div>
      ) : !sourceOrder.length ? (
        <div className="repo-empty">
          {filter.active
            ? "No ticket matches “" + filter.query + "”."
            : mineOnly && hasOthers
              ? "None of the tickets here are yours — switch to Everyone's tickets to see them."
              : "No tickets are assigned to you on the connected sources."}
        </div>
      ) : (
        sourceOrder.map((src) => {
          const inner = bySource.get(src) || new Map<string, AssignedTicket[]>();
          const shownBuckets = visible.filter((b) => (inner.get(b) || []).length);
          const total = shownBuckets.reduce((n, b) => n + (inner.get(b) || []).length, 0);
          const srcError = sourceErrors.get(src);
          const label = sourceLabels[src] || src;
          const states = ingestStates[src];
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
                    onHide={() => hideBucket(b)}
                    hideTitle={
                      "Hide the " + b + " bucket everywhere (re-add it from the dropdown)"
                    }
                  >
                    {rows.map((t) => (
                      <AssignedTicketRow
                        key={t.source + ":" + t.id}
                        t={t}
                        agents={agents}
                        // Falls back to the app default so the picker never
                        // offers a bare "Configured" with nothing named — which
                        // is what a ticket from a just-removed source would show.
                        configuredAgent={sourceAgents[t.source] || defaultAgent}
                        configuredDepth={sourceDepths[t.source] || ""}
                        configuredEffort={sourceEfforts[t.source] || ""}
                        onStarted={relistTickets}
                      />
                    ))}
                  </WorkGroup>
                );
              });

          const buckets = srcError ? (
            <div className="repo-empty">{srcError}</div>
          ) : !inner.size ? (
            // Nothing at all, vs. nothing in the buckets you chose to show —
            // two different situations, and only one of them is your filter.
            <div className="repo-empty">No tickets are assigned to you on this source.</div>
          ) : !shownBuckets.length ? (
            <div className="repo-empty">
              No tickets from this source in the buckets you're showing.
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
          return (
            <WorkGroup
              key={src}
              heading
              name={label}
              count={total}
              detail={
                srcError
                  ? "could not be reached"
                  : states
                    ? "auto-ingests " + states.join(", ")
                    : "auto-ingests every state"
              }
              open={openSources.isOpen(src)}
              onToggle={() => openSources.toggle(src)}
            >
              {buckets}
            </WorkGroup>
          );
        })
      )}
    </WorkListPanel>
  );
}

function AssignedTicketRow({
  t,
  agents,
  configuredAgent,
  configuredDepth,
  configuredEffort,
  onStarted,
}: {
  t: AssignedTicket;
  agents: string[];
  /** What this ticket's source is configured to run, for the picker's label. */
  configuredAgent: string;
  /** How far this ticket's source is configured to take its items. */
  configuredDepth: string;
  /** How hard this ticket's source is configured to think about its items. */
  configuredEffort: string;
  onStarted(): void;
}) {
  return (
    <WorkItemRow
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
      hasSession={t.has_session}
      eligible={t.eligible}
      eligibleLabel="queued for auto ingestion"
      reasons={t.reasons}
      actionLabel="Begin work"
      failPrefix="Begin work failed"
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
      onStart={async ({ agent, depth, effort }) => {
        const r = await api<{ title?: string }>("/api/tickets/start", {
          json: {
            source: t.source,
            id: t.id,
            ...(agent ? { agent } : {}),
            ...(depth ? { depth } : {}),
            ...(effort ? { effort } : {}),
          },
        });
        // The server already has a provisioning row for it: pull it now
        // instead of leaving the sidebar blank until the next poll.
        refreshInstances();
        setTimeout(onStarted, 5000);
        return "Session " + (r?.title || t.slug);
      }}
    />
  );
}

function TicketSourceCard({
  source,
  catalog,
  agents,
  collapsed,
  onToggle,
  onChange,
  onRemove,
}: {
  source: Source;
  catalog: CatalogEntry[];
  agents: AgentChoices;
  collapsed: boolean;
  onToggle(): void;
  onChange(patch: Record<string, string>): void;
  onRemove(): void;
}) {
  const meta = catalog.find((p) => p.id === source.provider) || null;
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
  const [states, setStates] = useState<Array<{ id: string | number; name?: string }>>([]);

  const provName = meta?.label || source.provider;
  const detail = (source.label || source.member_id || source.repo_url || "").trim();
  const base = detail ? provName + " — " + detail : provName;
  // Always show which CLI this queue runs, not just when it is overridden.
  // Saved sources render collapsed, so an agent shown only when explicitly set
  // made the common case — "it's on the app default" — indistinguishable from
  // "this source has no agent setting at all", which is what sent people
  // looking for the picker in the first place.
  const summary =
    base + " · " + (source.agent || (agents.fallback ? agents.fallback + " (default)" : "app default"));
  const repoMissing = !(source.repo_url || "").trim();

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
      const r = await api<{ ok?: boolean; states?: Array<{ id: string | number; name?: string }> }>(
        "/api/settings/ticketing/states",
        { json: testPayload() }
      );
      if (r?.ok && Array.isArray(r.states) && r.states.length) setStates(r.states);
    } catch {
      /* keep the stored value; Test connection to populate */
    }
  };

  return (
    <SourceCard
      sourceId={source.id}
      summary={summary}
      collapsed={collapsed}
      onToggle={onToggle}
      footer={
        <>
          <TestButton
            onTest={async () => {
              const r = await api<Record<string, unknown>>("/api/settings/test/ticketing", {
                json: testPayload(),
              });
              if (!r?.ok) throw new Error(String(r?.error || "test failed"));
              if (r.member_id && source.member_id !== r.member_id)
                onChange({ member_id: String(r.member_id) });
              loadStates(); // creds known-good — populate the state picker live
              return "Connected" + (r.name ? " — " + r.name : "");
            }}
          />
          <button type="button" className="test-btn tk-remove" onClick={onRemove}>
            Remove
          </button>
        </>
      }
    >
      <label className="set-row">
        <span className="set-label">Provider</span>
        <select
          className="tk-provider"
          value={source.provider}
          onChange={(e) => onChange({ provider: e.target.value })}
        >
          {catalog.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
        </select>
        <span className="set-hint tk-blurb">{meta?.blurb || ""}</span>
      </label>
      <label className="set-row">
        <span className="set-label">Label (optional)</span>
        <input
          type="text"
          className="tk-label"
          placeholder="e.g. Jira – EU"
          defaultValue={source.label || ""}
          onBlur={(e) => {
            if (e.target.value !== (source.label || "")) onChange({ label: e.target.value });
          }}
        />
        <span className="set-hint">
          Names this source's group in the Assigned tickets panel — worth setting when you
          have two of the same provider.
        </span>
      </label>
      <label className="set-row">
        <span className="set-label">Repo URL</span>
        <input
          type="text"
          autoComplete="off"
          required
          data-tk-field="repo_url"
          className={repoMissing ? "field-missing" : ""}
          placeholder="git@github.com:org/repo.git"
          defaultValue={source.repo_url || ""}
          onBlur={(e) => {
            if (e.target.value !== (source.repo_url || "")) onChange({ repo_url: e.target.value });
          }}
        />
        <span className="set-hint">Required — tickets from this source clone into this repo.</span>
      </label>
      <label className="set-row">
        <span className="set-label">Agent CLI</span>
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
        <span className="set-hint">
          Which coding CLI runs the sessions this source starts. Route one queue to a
          cloud CLI and another to a local model — pick a provider whose Connections
          row is green, or leave it on the app default.
        </span>
      </label>
      {/* THINKING EFFORT, directly under the CLI that will do the thinking —
          the two are one decision. "Which CLI" and "how hard should it think"
          are read together, and a rung means different things on different CLIs
          (each spells it its own way and clamps its own ceiling), so the picker
          has to sit where the CLI it is about to qualify is still on screen.
          Per SOURCE because that is where the property lives: a queue of
          one-line copy fixes and a queue of schema migrations deserve different
          answers, and neither deserves to be set per ticket forever. */}
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
              ? "CLI default — however it thinks on its own"
              : "No effort setting (" + (effortProvider || "this CLI") + ")"}
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
          How hard the agent thinks about <em>every</em> ticket from this source —
          both the ones the pipeline picks up on its own and the ones you start by
          hand. The rungs are neutral: whichever CLI runs the ticket translates
          them into its own spelling and never receives a rung it would reject, so
          this is safe to set higher than the CLI above can go. An individual
          ticket can still choose its own on its row.
        </span>
      </label>
      <label className="set-row">
        <span className="set-label">Take tickets as far as</span>
        <select
          className="tk-depth"
          data-tk-field="depth"
          value={source.depth || ""}
          onChange={(e) => onChange({ depth: e.target.value })}
        >
          <option value="">Off — stop after the agent works</option>
          {SOURCE_DEPTHS.map((d) => (
            <option key={d} value={d}>
              {DEPTH_LABELS[d]}
            </option>
          ))}
        </select>
        <span className="set-hint">
          How far every ticket from this source carries itself once the agent finishes:
          commit, push, open a PR. Merging is <strong>not</strong> offered here — a
          source default applies to every future ticket with nobody watching, and a
          merge cannot be undone. You can still pick Merge on one ticket's row.
        </span>
      </label>
      <div className="tk-fields">
        {(meta?.fields || []).map((f) =>
          f.type === "state" ? (
            <StatePicker
              key={f.key}
              field={f}
              source={source}
              states={states}
              loadStates={loadStates}
              onChange={onChange}
            />
          ) : f.type === "choice" ? (
            <ChoicePicker key={f.key} field={f} source={source} onChange={onChange} />
          ) : (
            <label className="set-row" key={f.key}>
              <span className="set-label">{f.label}</span>
              <input
                type={f.secret ? "password" : "text"}
                autoComplete="off"
                data-tk-field={f.key}
                placeholder={
                  f.secret && source[f.key] === "•••set" ? "•••set (saved)" : f.placeholder || ""
                }
                defaultValue={f.secret ? "" : source[f.key] || ""}
                onBlur={(e) => {
                  // Blank password => keep stored secret (omit from patch).
                  if (f.secret && e.target.value === "") return;
                  if (e.target.value !== (source[f.key] || ""))
                    onChange({ [f.key]: e.target.value });
                }}
              />
            </label>
          )
        )}
      </div>
    </SourceCard>
  );
}
