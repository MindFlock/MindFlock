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
import { ApiError, api } from "../../api/client";
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
import { useUi } from "../../state/store";
import { startTogetherText } from "../../lib/runStart";
import {
  AutomationSwitch,
  SourceCard,
  SourcePill,
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
  type TestResult,
} from "./kit";
import {
  TEST_STATE_LABELS,
  clearSecretPatch,
  fieldErrorsFrom,
  hasSecret,
  lastTestOf,
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
import { DEPTH_LABELS, SOURCE_DEPTHS } from "../../lib/autopilot";
import {
  EFFORTS,
  effortOptionLabel,
  effortTitle,
  supportsEffort,
} from "../../lib/effort";
import { useMindflockStatus, useProviderEfforts } from "../../state/queries";
import type { TabProps } from "./IntakeDialog";
import { ticketMatches } from "./search";
import { MergeButton, MergeDrawer } from "./MergeTicket";
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

/** Master ticket-ingestion switch — the Intake-tab twin of the sidebar's
 * AutomationBar. Same server contract (GET /api/mindflock/status for the
 * desired state, POST /api/mindflock/{start,stop} to flip it) and the same
 * shared status query (useMindflockStatus), so the two switches stay in
 * lock-step. Clicking the row text does nothing — only the switch
 * itself flips it. */
function IngestionToggle({ sourceCount }: { sourceCount: number }) {
  const [busy, setBusy] = useState(false);
  const [optimistic, setOptimistic] = useState<boolean | null>(null);
  const { data: status, refetch } = useMindflockStatus();

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
  // The ids the server has stored, as of the last load or save: a card outside
  // it that is still missing a typed required field is not sent (splitForSave).
  const storedIds = useRef<Set<string>>(
    new Set((savedSources.data || []).map((s) => s.id))
  );
  const seq = useRef(0);
  // Per source: what the server refused on the last save (shown inline, and
  // the card reads "not saved" until it passes), and what its Test is doing
  // or last said — one line for a click's test and a save's test alike.
  const [errors, setErrors] = useState<Record<string, FieldErrors>>({});
  const [tests, setTests] = useState<Record<string, TestResult>>({});

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
    setCollapsed(new Set(savedSources.data.map((s) => s.id)));
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
      const msg = String(r?.message || r?.error || (r?.ok ? "Connected" : "test failed"));
      setTests((prev) => ({ ...prev, [src.id]: { testing: false, ok: !!r?.ok, msg } }));
      if (!r?.ok) throw new Error(msg);
      refreshConfig();
      return msg;
    } catch (err) {
      const msg = (err as Error).message || "test failed";
      setTests((prev) => ({ ...prev, [src.id]: { testing: false, ok: false, msg } }));
      throw err;
    }
  }, []);

  const persist = useCallback(
    async (
      list: Source[],
      opts: { nulls?: Record<string, string[]>; autoTest?: string } = {}
    ) => {
      const mySeq = ++seq.current;
      // A new card still missing a typed required field stays local with its
      // inline errors; the rest of the list saves without it.
      const { send, held } = splitForSave(list, catalog, storedIds.current);
      const heldCount = Object.keys(held).length;
      if (!send.length && !storedIds.current.size) {
        // Nothing stored and nothing ready: no request to make.
        setErrors(held);
        return;
      }
      const missingRepo = send.filter((s) => !(s.repo_url || "").trim()).length;
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
        toast(
          heldCount
            ? `Saved — ${heldCount} new source(s) wait for their required fields`
            : missingRepo
              ? `Saved — but ${missingRepo} source(s) need a Repo URL to ingest`
              : "Saved ticketing sources"
        );
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
          // Not saved: the fields are marked on their cards, which stay as
          // typed — the next blur retries the whole list.
          if (mySeq === seq.current) setErrors({ ...held, ...fieldErrors });
          toast("Not saved — fix the marked fields");
          return;
        }
        errorPop(
          "Ticketing sources not saved",
          (err as Error).message || "the server rejected the ticketing settings"
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

  const update = (id: string, patch: Record<string, string | null>) => {
    setSources((prev) => {
      const list = prev || [];
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
      const next = list.map((s) => {
        if (s.id !== id) return s;
        const merged = { ...s, ...strings } as Source;
        // Switching the provider of a card whose id is still the seeded one
        // re-seeds it, or "Add source → pick Jira" branches Jira tickets under
        // whatever provider the card happened to open on.
        if (patch.provider && patch.provider !== s.provider && isSeededId(s.id, s.provider)) {
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
        return merged;
      });
      persist(next, { nulls, autoTest });
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
    const id = uniqueId(prefixFor(provider));
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
      {/* WORK FIRST. Once a source exists the tab opens on what it yielded —
          the tickets are the reason you came, the source cards are setup you
          revisit. With none yet, the card to add one is all there is. */}
      {sources.length > 0 && (
        <AssignedTickets
          agents={agents.names}
          // Per source, what "Default" resolves to on a Begin-work picker —
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
          // queue's own rung instead of the CLI's default. Before the source had
          // an effort there was nothing to name, which is why that picker's blank
          // option used to read "Default effort" unconditionally.
          sourceEfforts={Object.fromEntries(
            sources.map((s) => [s.id, s.effort || ""])
          )}
        />
      )}
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
              fieldErrors={errors[src.id]}
              test={tests[src.id]}
              onTest={() => runTest(src, catalog.find((p) => p.id === src.provider) || null)}
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
  states: TicketingWorkflowState[];
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
  // "Anyone's" is carried by this filter (or, on Shortcut, an ingest label) —
  // with neither there is nothing to scope a whole-tracker search by, so the
  // source stays on assigned-to-me until one is set. Say that where the gap is.
  const hasLabels = !!(source.ingest_labels || "").trim();
  const needsState = source.assignee_scope === "anyone" && !selected.length && !hasLabels;

  return (
    <div className="set-row">
      <span className="set-label">{field.label}</span>
      <div className="repo-list">
        {!selected.length ? (
          <div className="repo-empty">
            {needsState
              ? "Pick at least one state — Anyone's has nothing to go on without it, so this source is still only taking tickets assigned to you."
              : hasLabels
                ? "Any state — every ticket carrying an ingest label is auto-ingested."
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
        // Opens with what the last Test saw; every focus asks the tracker
        // again, so a column added this morning is there without a retest.
        onFocus={loadStates}
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
  return (
    <label className="set-row">
      <FieldLabel field={field} />
      <select
        className={"tk-choice" + (error ? " field-missing" : "")}
        data-tk-field={field.key}
        aria-invalid={error ? true : undefined}
        value={source[field.key] || ""}
        onChange={(e) => onChange({ [field.key]: e.target.value })}
      >
        {(field.options || []).map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      {/* The hint stays under the error: the rule is worth reading either way. */}
      {error ? <span className="ik-field-error" data-tk-error={field.key}>{error}</span> : null}
      {field.hint ? <span className="set-hint">{field.hint}</span> : null}
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
                        pick={{
                          checked: picked.has(pickKey(t)),
                          onChange: (on) => setPick(t, on),
                        }}
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
  pick,
}: {
  t: AssignedTicket;
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
      hasSession={t.has_session}
      elsewhere={t.elsewhere?.label}
      eligible={t.eligible}
      eligibleLabel="queued for auto ingestion"
      reasons={t.reasons}
      actionExtra={
        canMerge ? (
          <MergeButton
            reference={t.slug}
            open={merging}
            onToggle={() => setMerging((v) => !v)}
          />
        ) : null
      }
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
        const start = (override: boolean) =>
          api<{ title?: string }>("/api/tickets/start", {
            json: {
              source: t.source,
              id: t.id,
              ...(agent ? { agent } : {}),
              ...(depth ? { depth } : {}),
              ...(effort ? { effort } : {}),
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
            errorPop("Begin work failed — " + t.slug, (err as Error).message, [
              {
                label: "Override",
                primary: true,
                title: "Start it here anyway — the ticket stays assigned to them",
                run: () =>
                  void start(true).then(
                    (ok) => {
                      refreshInstances();
                      setTimeout(onStarted, 5000);
                      toast("Session " + (ok?.title || t.slug) + " — provisioning, see the sidebar");
                    },
                    (e) => errorPop("Begin work failed — " + t.slug, (e as Error).message || "error")
                  ),
              },
            ]);
          throw err;
        }
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
  fieldErrors,
  test,
  onTest,
  onToggle,
  onChange,
  onRemove,
}: {
  source: Source;
  catalog: CatalogEntry[];
  agents: AgentChoices;
  collapsed: boolean;
  /** What the server refused on the last save, per field — the card is unsaved. */
  fieldErrors?: FieldErrors;
  /** The Test in flight or its last word (a click's or a save's). */
  test?: TestResult;
  /** Test the SAVED source and record the verdict on it. */
  onTest(): Promise<string>;
  onToggle(): void;
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
  const pillState = unsaved ? "needs_attention" : verdict.state;
  const pillLabel = unsaved ? "Not saved" : TEST_STATE_LABELS[verdict.state];
  const pillMessage = unsaved ? "fix the marked fields" : verdict.message;
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

  const provName = meta?.label || source.provider;
  const detail = (source.label || source.member_id || source.repo_url || "").trim();
  // Said once. A label that already names its provider ("Jira — payments") is
  // the whole title; prefixing it again read "jira — Jira — payments".
  // A whole word only: "Linearity board" does not name Linear.
  const namesProvider = [provName, source.provider].some(
    (n) =>
      !!n &&
      detail.toLowerCase().startsWith(n.toLowerCase()) &&
      !/[a-z0-9]/i.test(detail.charAt(n.length))
  );
  const base = !detail ? provName : namesProvider ? detail : provName + " — " + detail;
  // Always show which CLI this queue runs, not just when it is overridden.
  // Saved sources render collapsed, so an agent shown only when explicitly set
  // made the common case — "it's on the app default" — indistinguishable from
  // "this source has no agent setting at all", which is what sent people
  // looking for the picker in the first place.
  const summary =
    base + " · " + (source.agent || (agents.fallback ? agents.fallback + " (default)" : "app default"));
  const repoMissing = !(source.repo_url || "").trim();
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

  return (
    <SourceCard
      sourceId={source.id}
      summary={summary}
      badge={<SourcePill state={pillState} label={pillLabel} message={pillMessage} />}
      collapsed={collapsed}
      onToggle={onToggle}
      footer={
        <>
          <TestButton label={stored ? "Retest" : "Test connection"} onTest={onTest} result={test} />
          {!test?.msg && !test?.testing ? (
            <span className="ik-source-message" title={pillMessage}>
              {pillMessage}
            </span>
          ) : null}
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
      {ownGithubToken && (
        <p className="set-hint tk-own-token" role="note" data-tk-notice="own-token">
          This source still has its own token — move it to Settings → Connections → GitHub →
          Connect (read as a fallback this release, ignored from the next)
        </p>
      )}
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
        <span className="set-label">Fast-track tickets to</span>
        <select
          className="tk-depth"
          data-tk-field="depth"
          value={source.depth === "agent" ? "" : source.depth || ""}
          onChange={(e) => onChange({ depth: e.target.value })}
        >
          {/* One ladder, the same words as every other Fast-track control.
              A stored "agent" (stop once the agent stops) IS Off, so it reads
              as Off rather than as a sixth rung. */}
          <option value="">{DEPTH_LABELS.off}</option>
          {SOURCE_DEPTHS.filter((d) => d !== "agent").map((d) => (
            <option key={d} value={d}>
              {DEPTH_LABELS[d]}
            </option>
          ))}
        </select>
        <span className="set-hint">
          How far each ticket goes after its agent finishes. Merge is per-ticket only —
          a source default runs with nobody watching.
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
              error={errs[f.key]}
              onChange={onChange}
            />
          )
        )}
      </div>
    </SourceCard>
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
          {marker === "required" ? "*" : marker}
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
  error,
  onChange,
}: {
  field: CatalogField;
  source: Source;
  error?: string;
  onChange(patch: Record<string, string | null>): void;
}) {
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
  const stored = hasSecret(source, f);
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
      defaultValue={f.secret ? "" : source[f.key] || ""}
      onBlur={(e) => {
        // Blank password => keep stored secret (omit from patch); Clear is
        // the one way to drop it.
        if (f.secret && e.target.value === "") return;
        if (e.target.value !== (source[f.key] || "")) onChange({ [f.key]: e.target.value });
      }}
    />
  );
  return (
    <label className="set-row">
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
    </label>
  );
}
