/** Intake → Auto-start: everything the automations will pick up on their own.
 *
 * The other three tabs are per-source workbenches — every ticket, every open PR,
 * every open issue, grouped by where it lives, with the ones that will actually
 * be taken marked by a chip. This tab is the other question: not "what is there"
 * but "what happens next without me". So it shows only those rows, across all
 * three sources, oldest first — the order the pipeline draws in.
 *
 * Two rules it holds itself to:
 *
 * ONE VOCABULARY. Every section says "auto-start on" / "auto-start off" /
 * "not set up", whatever the underlying mechanism is. Tickets are switched by
 * the ingestion engine while PR review and issue handling have their own
 * toggles *inside* it, and an earlier version let that plumbing leak into the
 * labels — tickets read "ingestion paused" next to a PR reading "switched off",
 * which invites you to think two different things are wrong. The difference is
 * real but it belongs in ONE place: the sentence that names the switch to flip.
 *
 * IT DOESN'T ONLY WATCH. Every row can be started by hand, on the same
 * WorkItemRow the other tabs use, with the same per-launch agent, depth and
 * effort pickers. Waiting for the sweep is the default, not the only option — and when
 * auto-start is off, starting from here is the whole point.
 */

import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { refreshInstances, usePanelQuery } from "../../state/queries";
import { useSettings } from "../settings/useSettings";
import { useAgentChoices } from "../settings/screens/AgentPicker";
import { WorkItemRow, ageText, panelNote, useListFilter } from "./kit";
import type { RepoOverrides } from "./RepoSources";
import { queuedMatches } from "./search";
import {
  byGroup,
  collectQueued,
  queuedOf,
  runState,
  type QueuedItem,
  type QueueKind,
  type QueueRunState,
  type QueueTarget,
} from "./queue";
import type { IntakeTabKey, TabProps } from "./IntakeDialog";

/** Panel payloads, as this tab reads them. The queued-row fields live in
 * ./queue.ts; these add what the section headers need. */
interface TicketsPayload {
  tickets?: Array<Record<string, unknown>>;
  source_labels?: Record<string, string>;
  sources?: string[];
  stale?: boolean;
}
interface PrsPayload {
  prs?: Array<Record<string, unknown>>;
  repos?: string[];
  stale?: boolean;
}
interface IssuesPayload {
  issues?: Array<Record<string, unknown>>;
  repos?: string[];
  stale?: boolean;
}

interface Section {
  kind: QueueKind;
  label: string;
  tab: IntakeTabKey;
  state: QueueRunState;
  /** The switch that would make this section's rows move, named as the UI names
   * it. For a stopped engine that is Automated ingestion whatever the kind. */
  blockedOn: string;
  /** True when the blocking switch lives on another tab than this section's. */
  blockedElsewhere: boolean;
  items: QueuedItem[];
  note: string;
}

interface IngestionStatus {
  available: boolean;
  running: boolean;
  desired?: boolean;
  /** github.enabled AND repos configured, folded server-side. */
  pr_enabled?: boolean;
  issues_enabled?: boolean;
}

/** One status call answers all three switches plus the engine.
 *
 * Same query key, same 4s interval as the switch on the Tickets tab and the
 * sidebar's AutomationBar — a shared key with two different intervals lets
 * whichever component mounted first decide the poll rate. Preferred over the
 * panels' own `enabled` flags because the server folds "switched on" and "has
 * repos" together in one place here, and because the engine state has to come
 * from here anyway. */
function useIngestionStatus() {
  return useQuery({
    queryKey: ["mindflock-status"],
    queryFn: () => api<IngestionStatus>("/api/mindflock/status"),
    refetchInterval: 4_000,
    retry: false,
  });
}

/** The chip beside a section heading — one vocabulary for all three kinds. */
function StateChip({ state }: { state: QueueRunState }) {
  if (state === "unset") return <span className="pr-open-chip">not set up</span>;
  if (state === "on") return <span className="pr-open-chip ok">auto-start on</span>;
  // Neutral rather than a colour-coded warning: the words say it, the sentence
  // below says what to do, and a hue would need its own contrast pass in both
  // themes to add nothing.
  return <span className="pr-open-chip">auto-start off</span>;
}

/** Why these rows are not moving and which switch fixes it. One sentence shape
 * for every kind; only the switch's name changes. */
function StalledNote({ s }: { s: Section }) {
  const many = s.items.length !== 1;
  return (
    <div className="ik-queue-stalled">
      {many ? "These are" : "This one is"} ready, but auto-start is off —{" "}
      {many ? "they will" : "it will"} not begin until <strong>{s.blockedOn}</strong> is
      switched on
      {s.blockedElsewhere ? ", on the Tickets tab (this runs inside it)" : ""}. You can still
      start {many ? "any of them" : "it"} here, with <strong>Start now</strong>.
    </div>
  );
}

/** One kind's block: heading with its state, then its rows grouped by source or
 * repo. Group names only appear when there is more than one, since a single
 * heading repeating the section is noise. */
function SectionBlock({
  s,
  gotoTab,
  agents,
  configuredFor,
  onStart,
}: {
  s: Section;
  gotoTab(k: IntakeTabKey): void;
  agents: string[];
  configuredFor(t: QueueTarget): { agent: string; depth: string };
  onStart(
    item: QueuedItem,
    agent: string,
    depth: string,
    effort: string
  ): Promise<string>;
}) {
  const groups = byGroup(s.items);
  return (
    <div className="ik-queue-section">
      <div className="ik-queue-head">
        <span className="ik-queue-kind">{s.label}</span>
        <span className="ik-tab-count">{s.items.length}</span>
        <StateChip state={s.state} />
        <button
          type="button"
          className="test-btn ik-queue-goto"
          onClick={() => gotoTab(s.tab)}
          title={"Open the " + s.label + " tab"}
        >
          Open {s.label} ›
        </button>
      </div>
      {s.note ? <div className="pr-open-note">{s.note}</div> : null}
      {s.items.length === 0 ? (
        // A plain line, not the boxed `.repo-empty` the other tabs use for an
        // empty list: three kinds are always on screen here, and three empty
        // boxes make a quiet queue look like a broken one.
        <div className="ik-queue-empty">
          {s.state === "unset"
            ? "Nothing configured yet — set it up on the " + s.label + " tab."
            : "Nothing waiting."}
        </div>
      ) : (
        <>
          {s.state !== "on" && <StalledNote s={s} />}
          {groups.map((g) => (
            <div className="ik-queue-group" key={g.group}>
              {groups.length > 1 && <div className="ik-queue-group-name">{g.group}</div>}
              {g.items.map((item) => {
                const configured = configuredFor(item.target);
                return (
                  <WorkItemRow
                    key={item.key}
                    reference={item.reference}
                    url={item.url}
                    title={item.title}
                    linkTitle={"Open " + item.reference}
                    tooltip={
                      item.reference +
                      (item.title ? " — " + item.title : "") +
                      "\nfrom " +
                      item.group
                    }
                    meta={[item.state, ageText(item.created_at), item.assignee]
                      .filter(Boolean)
                      .join(" · ")}
                    eligible
                    eligibleLabel="will auto-start"
                    actionLabel="Start now"
                    failPrefix="Start failed"
                    agents={agents}
                    configuredAgent={configured.agent}
                    configuredDepth={configured.depth}
                    onStart={({ agent, depth, effort }) =>
                      onStart(item, agent, depth, effort)
                    }
                  />
                );
              })}
            </div>
          ))}
        </>
      )}
    </div>
  );
}

export function QueueTab({ gotoTab }: TabProps) {
  const ticketsQ = usePanelQuery<TicketsPayload>("tickets");
  const prsQ = usePanelQuery<PrsPayload>("github-prs");
  const issuesQ = usePanelQuery<IssuesPayload>("github-issues");
  const statusQ = useIngestionStatus();
  const status = statusQ.data;
  const engineAvailable = !!status?.available;
  // `desired` is the switch's own last position; `running` can lag it by a boot.
  const engineOn = !!(status?.desired ?? status?.running);

  const agentChoices = useAgentChoices();
  const s = useSettings();
  const gh = (s.settings.github || {}) as {
    agent?: string;
    issue_agent?: string;
    repo_settings?: RepoOverrides;
    issue_repo_settings?: RepoOverrides;
  };
  const ticketSources = ((s.settings.ticketing || {}) as {
    sources?: Array<{ id?: string; agent?: string; depth?: string }>;
  }).sources;

  /** What "Configured" resolves to on a row's pickers — this item's own source
   * or repo card, else the app default. Same resolution the item's home tab
   * does, so the label can't say one thing there and another here. */
  const configuredFor = (t: QueueTarget): { agent: string; depth: string } => {
    if (t.kind === "tickets") {
      const src = (ticketSources || []).find((x) => x.id === t.source);
      return { agent: src?.agent || agentChoices.fallback || "", depth: src?.depth || "" };
    }
    const overrides = (t.kind === "prs" ? gh.repo_settings : gh.issue_repo_settings) || {};
    const fallbackAgent = t.kind === "prs" ? gh.agent : gh.issue_agent;
    return {
      agent: overrides[t.repo]?.agent || String(fallbackAgent || agentChoices.fallback || ""),
      depth: overrides[t.repo]?.depth || "",
    };
  };

  /** Force-start one row now. Same endpoints and same follow-up as the tab the
   * item lives on: pull the new session immediately so the sidebar isn't blank
   * until the next poll, then re-list once the provisioning row exists. */
  const startItem = async (
    item: QueuedItem,
    agent: string,
    depth: string,
    effort: string
  ) => {
    const extra = {
      ...(agent ? { agent } : {}),
      ...(depth ? { depth } : {}),
      ...(effort ? { effort } : {}),
    };
    const t = item.target;
    let title = "";
    if (t.kind === "tickets") {
      const r = await api<{ title?: string }>("/api/tickets/start", {
        json: { source: t.source, id: t.id, ...extra },
      });
      title = "Session " + (r?.title || item.reference);
    } else if (t.kind === "prs") {
      const r = await api<{ title?: string }>("/api/github/prs/review", {
        json: { repo: t.repo, number: t.number, ...extra },
      });
      title = "Review session " + (r?.title || "");
    } else {
      const r = await api<{ title?: string }>("/api/github/issues/start", {
        json: { repo: t.repo, number: t.number, ...extra },
      });
      title = "Issue session " + (r?.title || "");
    }
    refreshInstances();
    const panel =
      item.kind === "tickets" ? ticketsQ : item.kind === "prs" ? prsQ : issuesQ;
    setTimeout(() => void panel.refetch(), 5000);
    return title;
  };

  const payloads = {
    tickets: ticketsQ.data ?? null,
    prs: prsQ.data ?? null,
    issues: issuesQ.data ?? null,
  };
  const total = collectQueued(payloads).length;
  const filter = useListFilter(
    "ik-queue-filter",
    "Filter by item, title, source, or state…  ( Ctrl+F )",
  );

  const noteFor = (q: { error: Error | null; isFetching: boolean; data: unknown }, what: string) =>
    panelNote({
      error: q.error ? "Could not list " + what + ": " + (q.error.message || "error") : "",
      fetching: q.isFetching,
      loaded: !!q.data,
    });

  const sections: Section[] = [
    {
      kind: "tickets",
      label: "Tickets",
      tab: "tickets",
      state: runState({
        configured: (ticketsQ.data?.sources || []).length > 0,
        engineAvailable,
        engineOn,
        // Tickets have no switch of their own — the engine is it.
        switchOn: true,
      }),
      blockedOn: "Automated ingestion",
      blockedElsewhere: false,
      items: queuedOf("tickets", payloads),
      note: noteFor(ticketsQ, "tickets"),
    },
    {
      kind: "prs",
      label: "Pull requests",
      tab: "prs",
      state: runState({
        configured: (prsQ.data?.repos || []).length > 0,
        engineAvailable,
        engineOn,
        switchOn: status?.pr_enabled !== false,
      }),
      // Its own toggle is off => name that; the engine being down => name the
      // engine, because turning PR review on would still start nothing.
      blockedOn: status?.pr_enabled === false ? "Automated PR review" : "Automated ingestion",
      blockedElsewhere: status?.pr_enabled !== false,
      items: queuedOf("prs", payloads),
      note: noteFor(prsQ, "PRs"),
    },
    {
      kind: "issues",
      label: "Issues",
      tab: "issues",
      state: runState({
        configured: (issuesQ.data?.repos || []).length > 0,
        engineAvailable,
        engineOn,
        switchOn: status?.issues_enabled === true,
      }),
      blockedOn:
        status?.issues_enabled !== true ? "Automated issue handling" : "Automated ingestion",
      blockedElsewhere: status?.issues_enabled === true,
      items: queuedOf("issues", payloads),
      note: noteFor(issuesQ, "issues"),
    },
  ];

  // FILTERED PER SECTION, and a section with nothing left drops out: three
  // empty blocks under a search that matched two rows is three answers to a
  // question nobody asked. The unfiltered `total` still governs the note above,
  // because "what is waiting to start" is a fact about the queue rather than
  // about the box someone is typing in — the note says both when they differ.
  const shownSections = filter.active
    ? sections
        .map((sec) => ({
          ...sec,
          items: sec.items.filter((i) => queuedMatches(i, filter.tokens)),
        }))
        .filter((sec) => sec.items.length)
    : sections;
  const shownTotal = shownSections.reduce((n, sec) => n + sec.items.length, 0);

  const refreshAll = () => {
    ticketsQ.refresh();
    prsQ.refresh();
    issuesQ.refresh();
  };

  return (
    <div className="set-row" id="ik-queue-row">
      <span className="set-label">Starts automatically</span>
      <div className="pr-open-toolbar">
        <button type="button" className="test-btn" id="ik-queue-refresh" onClick={refreshAll}>
          Refresh
        </button>
        {total ? filter.control : null}
        <span className="pr-open-note">
          {total === 0
            ? "Nothing waiting to start"
            : filter.active
              ? shownTotal + " of " + total + " shown"
              : total + (total === 1 ? " item will auto-start" : " items will auto-start")}
        </span>
      </div>
      <div className="ik-queue" id="ik-queue-list">
        {filter.active && !shownSections.length ? (
          <div className="ik-queue-empty">No queued item matches “{filter.query}”.</div>
        ) : null}
        {shownSections.map((sec) => (
          <SectionBlock
            key={sec.kind}
            s={sec}
            gotoTab={gotoTab}
            agents={agentChoices.names}
            configuredFor={configuredFor}
            onStart={startItem}
          />
        ))}
      </div>
      <span className="set-hint">
        Everything the automations would start on their next sweep, oldest first — the order
        they are actually drawn in. An item is here because it passed its own kind's filters
        (ingest state, min age, author, base branch, and not already in the processed ledger);
        the other tabs explain, per row, why anything else was skipped. Where a section says
        auto-start is off, these are exactly the items that would go the moment you switch it
        on — or you can start any of them here now. Items the pipeline has already taken are no
        longer waiting: they are sessions, in the sidebar.
      </span>
    </div>
  );
}
