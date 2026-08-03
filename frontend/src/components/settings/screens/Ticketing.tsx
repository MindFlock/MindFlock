/** Settings → Ticketing (partial 107 + the sources IIFE, section 21): an
 * add/remove list of source cards — provider select + per-provider credential
 * fields from the catalog + per-source Test + Remove. The whole list is PUT
 * to /api/settings/ticketing/sources on any change; blank secrets are
 * omitted so the server keeps stored values (matched by source id). */

import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../../api/client";
import { refreshConfig, refreshInstances, usePanelQuery } from "../../../state/queries";
import { toast } from "../../../lib/toast";
import type { ScreenProps } from "../SettingsDialog";

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

/** Master ticket-ingestion switch — the settings-screen twin of the sidebar's
 * AutomationBar. Same server contract (GET /api/mindflock/status for the
 * desired state, POST /api/mindflock/{start,stop} to flip it) and the same
 * ["mindflock-status"] query key, so the two switches stay in lock-step.
 * Clicking the row text does nothing — only the switch itself flips it. */
function IngestionToggle() {
  const [busy, setBusy] = useState(false);
  const [optimistic, setOptimistic] = useState<boolean | null>(null);
  const { data: status, refetch } = useQuery({
    queryKey: ["mindflock-status"],
    queryFn: () =>
      api<{ available: boolean; running: boolean; desired?: boolean }>("/api/mindflock/status"),
    refetchInterval: 10_000,
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

  return (
    <div
      className="set-row set-switch-row"
      id="tk-ingestion-toggle-row"
      title="Run or stop ticket ingestion — polls your connected sources and auto-creates a coding session for each assigned ticket. Stays in this state across restarts."
    >
      <span className="set-label">Automated ingestion</span>
      {/* label wraps only the switch, so clicking the row text no longer flips it */}
      <label className="ca-switch">
        <input
          type="checkbox"
          id="tk-ingestion-enabled"
          checked={desired}
          disabled={busy}
          onChange={(e) => toggle(e.target.checked)}
        />
        <span className="ca-slider" />
      </label>
    </div>
  );
}

export function Ticketing(_: ScreenProps) {
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

  if (sources === null)
    return (
      <>
        <h3 className="set-section-title">Ticketing</h3>
        <p className="set-hint">Loading…</p>
      </>
    );

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
      <h3 className="set-section-title">Ticketing</h3>
      <p className="set-hint set-block-hint">
        Connect one or more ticketing platforms and MindFlock auto-creates a coding session for
        each ticket assigned to you. Add several sources — even two of the same provider (e.g.
        two Jira sites) — each with its own credentials. Stored in ~/.mindflock/settings.json
        (never committed).
      </p>
      <IngestionToggle />
      <div id="ticketing-sources">
        {sources.map((src) => (
          <SourceCard
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
          + Add ticketing source
        </button>
      </div>
      {sources.length > 0 && <AssignedTickets />}
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

/** Which buckets the user wants in the panel. null (nothing saved yet) =
 * show every bucket; once they hide one, the explicit list takes over — so
 * brand-new buckets appearing later stay tucked away in the Add menu. */
const BUCKETS_LS_KEY = "mf_ticket_buckets";

function loadShownBuckets(): string[] | null {
  try {
    const raw = localStorage.getItem(BUCKETS_LS_KEY);
    if (raw === null) return null;
    const v = JSON.parse(raw);
    return Array.isArray(v) ? v.map(String) : null;
  } catch {
    return null;
  }
}

function saveShownBuckets(v: string[] | null) {
  try {
    if (v === null) localStorage.removeItem(BUCKETS_LS_KEY);
    else localStorage.setItem(BUCKETS_LS_KEY, JSON.stringify(v));
  } catch {
    /* storage unavailable */
  }
}

/** Which bucket sections are expanded (all start collapsed — the header rows
 * with their counts are the overview; open the ones you're working from). */
const BUCKETS_OPEN_LS_KEY = "mf_ticket_buckets_open";

function loadOpenBuckets(): Set<string> {
  try {
    const v = JSON.parse(localStorage.getItem(BUCKETS_OPEN_LS_KEY) || "[]");
    return new Set(Array.isArray(v) ? v.map(String) : []);
  } catch {
    return new Set();
  }
}

function saveOpenBuckets(v: Set<string>) {
  try {
    localStorage.setItem(BUCKETS_OPEN_LS_KEY, JSON.stringify([...v]));
  } catch {
    /* storage unavailable */
  }
}

function ticketAgeText(iso?: string): string {
  const t = Date.parse(iso || "");
  if (!isFinite(t)) return "";
  const mins = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (mins < 60) return mins + "m old";
  const h = Math.round(mins / 60);
  if (h < 48) return h + "h old";
  return Math.round(h / 24) + "d old";
}

/** Assigned-tickets panel — the ticket twin of PR review's open-PRs panel:
 * EVERY ticket assigned to you, split into workflow-state buckets. The Add
 * bucket dropdown + per-bucket ✕ choose which buckets this panel shows
 * (persisted); Begin work force-starts a session, bypassing auto filters. */
function AssignedTickets() {
  // The slowest of the settings fan-outs (a provider search per source plus a
  // ls-remote per repo), so it is the one worth keeping across dialog opens.
  const ticketsQuery = usePanelQuery<{
    tickets?: AssignedTicket[];
    buckets?: string[];
    done_buckets?: string[];
    ingest_states?: Record<string, string[]>;
    errors?: Array<{ source: string; error: string }>;
    stale?: boolean;
  }>("tickets");
  const load = ticketsQuery.refresh; // Refresh button: force a sweep
  const relistTickets = ticketsQuery.refetch; // after a force start
  const tickets = ticketsQuery.data ? ticketsQuery.data.tickets || [] : null;
  const buckets = ticketsQuery.data?.buckets || [];
  const doneBuckets = ticketsQuery.data?.done_buckets || [];
  const ingestStates = ticketsQuery.data?.ingest_states || {};
  const error = ticketsQuery.error
    ? "Could not list tickets: " + (ticketsQuery.error.message || "error")
    : "";
  const sourceErrors = (ticketsQuery.data?.errors || [])
    .map((e) => e.source + ": " + e.error)
    .join(" · ");
  const note =
    error || sourceErrors
      ? sourceErrors
      : ticketsQuery.isFetching
        ? tickets
          ? "Refreshing…"
          : "Loading…"
        : "";

  const [shown, setShown] = useState<string[] | null>(loadShownBuckets);
  const [openBuckets, setOpenBuckets] = useState<Set<string>>(loadOpenBuckets);

  const toggleOpen = (b: string) => {
    setOpenBuckets((prev) => {
      const next = new Set(prev);
      if (next.has(b)) next.delete(b);
      else next.add(b);
      saveOpenBuckets(next);
      return next;
    });
  };

  // Nothing chosen yet: show the actionable buckets; done-type ones
  // (Completed, Won't do, …) usually dwarf them and start in the Add menu.
  const visible =
    shown === null
      ? buckets.filter((b) => !doneBuckets.includes(b))
      : buckets.filter((b) => shown.includes(b));
  const hidden = buckets.filter((b) => !visible.includes(b));
  const byBucket = new Map<string, AssignedTicket[]>();
  for (const t of tickets || []) {
    const b = t.bucket || "No state";
    if (!byBucket.has(b)) byBucket.set(b, []);
    byBucket.get(b)!.push(t);
  }

  const hideBucket = (b: string) => {
    // First customization starts from what's on screen (the default view),
    // so hiding one bucket never surfaces the parked done-type ones.
    const next = (shown ?? visible).filter((x) => x !== b);
    setShown(next);
    saveShownBuckets(next);
  };
  const addBucket = (b: string) => {
    if (!b) return;
    const next = [...(shown ?? []), b];
    setShown(next);
    saveShownBuckets(next);
  };

  return (
    <div className="set-row" id="tk-assigned-row">
      <span className="set-label">Assigned tickets</span>
      <div className="pr-open-toolbar">
        <button type="button" id="tk-tickets-refresh" className="test-btn" onClick={load}>
          Refresh
        </button>
        <select
          id="tk-bucket-add"
          className="tk-bucket-add"
          value=""
          disabled={!hidden.length}
          title="Show another bucket in this panel"
          onChange={(e) => addBucket(e.target.value)}
        >
          <option value="">
            {hidden.length ? "+ Add bucket…" : "All buckets shown"}
          </option>
          {hidden.map((b) => (
            <option key={b} value={b}>
              {b} ({(byBucket.get(b) || []).length})
            </option>
          ))}
        </select>
        <span id="tk-tickets-note" className="pr-open-note">{note}</span>
      </div>
      <div id="tk-tickets-list">
        {error ? (
          <div className="repo-empty">{error}</div>
        ) : tickets === null ? null : !tickets.length ? (
          <div className="repo-empty">
            No tickets are assigned to you on the connected sources.
          </div>
        ) : !visible.length ? (
          <div className="repo-empty">
            All buckets are hidden — pick one from the “+ Add bucket…” menu above.
          </div>
        ) : (
          visible.map((b) => {
            const rows = byBucket.get(b) || [];
            const open = openBuckets.has(b);
            return (
              <div className="tk-bucket" key={b}>
                <div className="tk-bucket-head">
                  <button
                    type="button"
                    className="tk-bucket-toggle"
                    aria-expanded={open}
                    title={(open ? "Collapse" : "Expand") + " the " + b + " bucket"}
                    onClick={() => toggleOpen(b)}
                  >
                    <span className="tk-caret">{open ? "▾" : "▸"}</span>
                    <span className="tk-bucket-name">{b}</span>
                    <span className="tk-bucket-count">{rows.length}</span>
                  </button>
                  <button
                    type="button"
                    className="tk-bucket-x"
                    title={"Hide the " + b + " bucket (re-add it from the dropdown)"}
                    aria-label={"Hide bucket " + b}
                    onClick={() => hideBucket(b)}
                  >
                    ✕
                  </button>
                </div>
                {open && (
                  <div className="pr-open-list">
                    {rows.map((t) => (
                      <AssignedTicketRow
                        key={t.source + ":" + t.id}
                        t={t}
                        onStarted={relistTickets}
                      />
                    ))}
                    {!rows.length && (
                      <div className="repo-empty">No tickets in this bucket.</div>
                    )}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
      <span className="set-hint">
        Every ticket assigned to you across the sources above, split by workflow
        state. Click a bucket to expand or collapse it; use{" "}
        <strong>+ Add bucket…</strong> / ✕ to choose which buckets appear at all
        (both remembered on this device).{" "}
        {Object.keys(ingestStates).length ? (
          <>
            Auto ingestion only watches{" "}
            <strong>{Object.values(ingestStates).flat().join(", ")}</strong> —
            tickets in every other bucket are only started by hand, with{" "}
            <strong>Begin work</strong>.
          </>
        ) : (
          <>
            No ingest-state filter is set on the source, so auto ingestion watches
            every state — set one on the source card above to narrow it.{" "}
            <strong>Begin work</strong> starts any ticket by hand.
          </>
        )}
      </span>
    </div>
  );
}

function AssignedTicketRow({ t, onStarted }: { t: AssignedTicket; onStarted(): void }) {
  const [state, setState] = useState<"idle" | "starting" | "started">("idle");
  return (
    <div
      className="pr-open-item"
      title={t.slug + " — " + (t.name || "") + "\nfrom " + (t.source_label || t.source)}
    >
      <div className="pr-open-main">
        <a
          href={t.url || "#"}
          target="_blank"
          rel="noopener noreferrer"
          className="pr-open-ref"
          title={"Open " + t.slug + " in " + (t.source_label || t.source)}
        >
          {t.slug}
        </a>
        <span className="pr-open-title">{t.name || ""}</span>
      </div>
      <div className="pr-open-meta">
        <span>
          {(t.source_label || t.source) + " · " + ticketAgeText(t.created_at)}
        </span>
        {t.has_session ? (
          <span className="pr-open-chip on">session open</span>
        ) : t.eligible ? (
          <span className="pr-open-chip ok">queued for auto ingestion</span>
        ) : (
          (t.reasons || []).map((reason) => (
            <span className="pr-open-chip" key={reason}>
              {reason}
            </span>
          ))
        )}
      </div>
      {t.has_session ? (
        <button type="button" className="btn-primary pr-review-btn" disabled>
          Session open
        </button>
      ) : (
        <button
          type="button"
          className="btn-primary pr-review-btn"
          disabled={state !== "idle"}
          onClick={async () => {
            setState("starting");
            try {
              const r = await api<{ title?: string }>("/api/tickets/start", {
                json: { source: t.source, id: t.id },
              });
              toast("Session " + (r?.title || t.slug) + " — provisioning, see the sidebar");
              setState("started");
              // The server already has a provisioning row for it: pull it now
              // instead of leaving the sidebar blank until the next poll.
              refreshInstances();
              setTimeout(onStarted, 5000);
            } catch (err) {
              toast("Begin work failed: " + ((err as Error).message || "error"));
              setState("idle");
            }
          }}
        >
          {state === "starting" ? "Starting…" : state === "started" ? "Started" : "Begin work"}
        </button>
      )}
    </div>
  );
}

function SourceCard({
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
  const [test, setTest] = useState<{ testing: boolean; ok?: boolean; msg?: string }>({ testing: false });
  const [states, setStates] = useState<Array<{ id: string | number; name?: string }>>([]);

  const provName = meta?.label || source.provider;
  const detail = (source.label || source.member_id || source.repo_url || "").trim();
  const base = detail ? provName + " — " + detail : provName;
  // A non-default agent is worth seeing without expanding the card: it is the
  // difference between this queue running on a cloud CLI and on a local model.
  const summary = source.agent ? base + " · " + source.agent : base;
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

  const runTest = async () => {
    setTest({ testing: true });
    try {
      const r = await api<Record<string, unknown>>("/api/settings/test/ticketing", {
        json: testPayload(),
      });
      if (r?.ok) {
        setTest({ testing: false, ok: true, msg: "Connected" + (r.name ? " — " + r.name : "") });
        if (r.member_id && source.member_id !== r.member_id)
          onChange({ member_id: String(r.member_id) });
        loadStates(); // creds known-good — populate the state picker live
      } else {
        setTest({ testing: false, ok: false, msg: String(r?.error || "test failed") });
      }
    } catch (e) {
      setTest({ testing: false, ok: false, msg: (e as Error).message });
    }
  };

  return (
    <div className={"tk-source" + (collapsed ? " tk-collapsed" : "")} data-source-id={source.id}>
      <button type="button" className="tk-head" aria-expanded={!collapsed} onClick={onToggle}>
        <span className="tk-caret">{collapsed ? "▸" : "▾"}</span>
        <span className="tk-summary">{summary}</span>
      </button>
      <div className="tk-body">
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
        <div className="set-row">
          <span className="test-row">
            <button type="button" className="test-btn" onClick={runTest}>
              Test connection
            </button>
            <span className={"test-result" + (test.msg ? (test.ok ? " ok" : " bad") : "")}>
              {test.testing ? "testing…" : test.msg ? (test.ok ? "✓ " : "✗ ") + test.msg : ""}
            </span>
            <button type="button" className="test-btn tk-remove" onClick={onRemove}>
              Remove
            </button>
          </span>
        </div>
      </div>
    </div>
  );
}
