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
import { refreshConfig, refreshInstances, usePanelQuery } from "../../state/queries";
import { toast } from "../../lib/toast";
import {
  AutomationSwitch,
  SourceCard,
  TestButton,
  WorkGroup,
  WorkItemRow,
  WorkListPanel,
  ageText,
  panelNote,
  useToggleSet,
} from "./kit";
import {
  NO_STATE_BUCKET,
  loadShownBuckets,
  saveShownBuckets,
  visibleBuckets,
} from "./buckets";
import type { TabProps } from "./IntakeDialog";

interface CatalogField {
  key: string;
  label: string;
  secret?: boolean;
  placeholder?: string;
  type?: string; // "state" = workflow-state picker
}

interface CatalogEntry {
  id: string;
  label: string;
  blurb?: string;
  fields: CatalogField[];
}

type Source = Record<string, string> & { id: string; provider: string };

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
      toast(`Ticket ingestion ${start ? "start" : "stop"} failed: ` + ((err as Error).message || ""));
    } finally {
      setBusy(false);
      setOptimistic(null);
      refetch();
    }
  };

  const n = sourceCount;
  return (
    <AutomationSwitch
      label="Automated ingestion"
      title="Run or stop ticket ingestion — polls your connected sources and auto-creates a coding session for each assigned ticket. Stays in this state across restarts."
      rowId="tk-ingestion-toggle-row"
      inputId="tk-ingestion-enabled"
      statusId="tk-ingestion-status"
      checked={desired}
      onChange={(next) => { if (!busy) toggle(next); }}
      tone={n > 0 && desired ? "on" : n > 0 ? "paused" : ""}
      status={
        !n
          ? "○ Add a ticketing source below to start turning tickets into sessions"
          : desired
            ? `● Active — polling ${n} ${n === 1 ? "source" : "sources"} for tickets assigned to you`
            : `‖ Paused — ${n} ${n === 1 ? "source" : "sources"} kept; turn Automated ingestion on to resume`
      }
    />
  );
}

export function TicketsTab(_: TabProps) {
  const [catalog, setCatalog] = useState<CatalogEntry[]>([]);
  const [sources, setSources] = useState<Source[] | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [agents, setAgents] = useState<AgentChoices>({ names: [], fallback: "" });
  const seq = useRef(0);

  useEffect(() => {
    (async () => {
      try {
        const c = await api<{ providers?: CatalogEntry[] }>("/api/settings/providers/ticketing");
        setCatalog(c?.providers || []);
        const r = await api<{ sources?: Source[] }>("/api/settings/ticketing/sources");
        const list = r?.sources || [];
        setSources(list);
        // Already-saved sources start collapsed (a summary chip).
        setCollapsed(new Set(list.map((s) => s.id)));
      } catch {
        setSources([]);
      }
      // Agent choices are enrichment: a failure just leaves the per-source
      // picker empty, and an unset agent still means "use the app default".
      try {
        const p = await api<{ providers?: Array<{ name: string }>; default?: string }>(
          "/api/providers"
        );
        setAgents({
          names: (p?.providers || []).map((x) => x.name).filter(Boolean),
          fallback: p?.default || "",
        });
      } catch {
        /* keep the empty list */
      }
    })();
  }, []);

  const persist = useCallback(
    async (list: Source[]) => {
      const mySeq = ++seq.current;
      const missingRepo = list.filter((s) => !(s.repo_url || "").trim()).length;
      try {
        await api("/api/settings/ticketing/sources", { method: "PUT", json: { sources: list } });
        if (mySeq !== seq.current) return;
        toast(
          missingRepo
            ? `Saved — but ${missingRepo} source(s) need a Repo URL to ingest`
            : "Saved ticketing sources"
        );
        // Connecting/removing a source flips the ticketing capability.
        refreshConfig();
      } catch (err) {
        toast("Save failed: " + ((err as Error).message || "ticketing"));
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

  return (
    <div className="set-row">
      <span className="set-label">{field.label}</span>
      <div className="repo-list">
        {!selected.length ? (
          <div className="repo-empty">
            Any state — every ticket assigned to you is auto-ingested.
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
}: {
  agents: string[];
  sourceAgents: Record<string, string>;
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
  // The key predates the source level, when an entry was a bare state name.
  // Those can never match a `source::state` key again, so they are dropped on
  // load rather than accumulating forever; the cost is that a previously
  // expanded bucket starts collapsed once.
  const openBuckets = useToggleSet(BUCKETS_OPEN_LS_KEY, false, (v) => v.includes("::"));
  const openSources = useToggleSet(SOURCES_CLOSED_LS_KEY, true);
  const openWorkflows = useToggleSet(WORKFLOWS_CLOSED_LS_KEY, true);

  const visible = visibleBuckets(buckets, doneBuckets, shown);
  const hidden = buckets.filter((b) => !visible.includes(b));

  // source -> bucket -> rows. Both levels keep the server's ordering: sources
  // in configured order, buckets in the provider's own workflow order.
  const bySource = new Map<string, Map<string, AssignedTicket[]>>();
  const countAll = new Map<string, number>();
  for (const t of tickets || []) {
    const src = t.source || "unknown";
    const b = t.bucket || NO_STATE_BUCKET;
    if (!bySource.has(src)) bySource.set(src, new Map());
    const inner = bySource.get(src)!;
    if (!inner.has(b)) inner.set(b, []);
    inner.get(b)!.push(t);
    countAll.set(b, (countAll.get(b) || 0) + 1);
  }
  const sourceOrder = [
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
      label="Assigned tickets"
      onRefresh={load}
      note={note}
      rowId="tk-assigned-row"
      refreshId="tk-tickets-refresh"
      noteId="tk-tickets-note"
      listId="tk-tickets-list"
      toolbarExtra={
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
      }
      hint={
        <>
          Your tickets, grouped by source and then by workflow state. Click a heading to
          expand or collapse it; use <strong>+ Add bucket…</strong> / ✕ to choose which
          states appear at all.{" "}
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
          No tickets are assigned to you on the connected sources.
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
  onStarted,
}: {
  t: AssignedTicket;
  agents: string[];
  /** What this ticket's source is configured to run, for the picker's label. */
  configuredAgent: string;
  onStarted(): void;
}) {
  return (
    <WorkItemRow
      agents={agents}
      configuredAgent={configuredAgent}
      reference={t.slug}
      url={t.url}
      title={t.name}
      linkTitle={"Open " + t.slug + " in " + (t.source_label || t.source)}
      tooltip={t.slug + " — " + (t.name || "") + "\nfrom " + (t.source_label || t.source)}
      meta={ageText(t.created_at)}
      hasSession={t.has_session}
      eligible={t.eligible}
      eligibleLabel="queued for auto ingestion"
      reasons={t.reasons}
      actionLabel="Begin work"
      failPrefix="Begin work failed"
      onStart={async (agent) => {
        const r = await api<{ title?: string }>("/api/tickets/start", {
          json: { source: t.source, id: t.id, ...(agent ? { agent } : {}) },
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
