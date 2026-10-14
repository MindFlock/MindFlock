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

import { api } from "../../api/client";
import {
  refreshInstances,
  useMindflockStatus,
  usePanelQuery,
  useTicketingCatalog,
} from "../../state/queries";
import { sourceDisplayName } from "./TicketsTab";
import type { TicketingSource } from "../../api/types";
import { SettingField, useSettings } from "../settings/useSettings";
import { useAgentChoices } from "../settings/screens/AgentPicker";
import { WorkItemRow, ageText, panelNote, useListFilter } from "./kit";
import { useIntakePause } from "./pause";
import { friendlySourceError } from "./vocab";
import type { RepoOverrides } from "./RepoSources";
import { queuedMatches } from "./search";
import { ScheduledTemplates } from "./ScheduledTemplates";
import {
  byGroup,
  collectQueued,
  queuedOf,
  runState,
  runsOnText,
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
  /** The switch that would make this section's rows move, named as its tab
   * names it ("Auto-start tickets"). */
  blockedOn: string;
  /** Every auto-start is off because of Pause intake. */
  paused?: boolean;
  /** Set when every configured source of this kind can't connect: the
   * sentence saying which and where to fix it. */
  broken?: string;
  /** For `elsewhere`: the device that runs it ("" when the server can't say). */
  runsOn?: string;
  items: QueuedItem[];
  note: string;
}

/** The chip beside a section heading — one vocabulary for all three kinds. */
function StateChip({
  state,
  device,
  paused,
}: {
  state: QueueRunState | "broken";
  device?: string;
  paused?: boolean;
}) {
  if (state === "unset") return <span className="pr-open-chip">not set up</span>;
  if (state === "elsewhere")
    return (
      <span className="pr-open-chip" data-runs-on={device || ""}>
        {runsOnText(device).toLowerCase()}
      </span>
    );
  if (state === "on") return <span className="pr-open-chip ok">auto-start on</span>;
  if (state === "broken") return <span className="pr-open-chip">can't connect</span>;
  // Neutral rather than a colour-coded warning: the words say it, the sentence
  // below says what to do, and a hue would need its own contrast pass in both
  // themes to add nothing.
  return <span className="pr-open-chip">{paused ? "auto-start paused" : "auto-start off"}</span>;
}

/** Why these rows are not moving and which switch fixes it. One sentence shape
 * for every kind; only the switch's name changes. Run elsewhere, nothing here
 * needs fixing: say where they run, and that the switch isn't the reason. */
function StalledNote({ s }: { s: Section }) {
  const many = s.items.length !== 1;
  if (s.state === "elsewhere")
    return (
      <div className="ik-queue-stalled" data-runs-on={s.runsOn || ""}>
        {runsOnText(s.runsOn)} — {s.runsOn || "that device"} starts{" "}
        {many ? "these" : "this one"} on its own; this computer doesn't (to move them here, choose{" "}
        <strong>Run here</strong> in Settings → Your devices). You can
        still start {many ? "any of them" : "it"} here, with <strong>Start</strong>.
      </div>
    );
  if (s.paused)
    return (
      <div className="ik-queue-stalled">
        Intake is paused — {many ? "these" : "this one"} won't start on {many ? "their" : "its"}{" "}
        own until you <strong>Resume intake</strong> (top of this dialog). You can still start{" "}
        {many ? "any of them" : "it"} with <strong>Start</strong>.
      </div>
    );
  return (
    <div className="ik-queue-stalled">
      {many ? "These are" : "This one is"} ready, but auto-start is off —{" "}
      {many ? "they" : "it"} won't start on {many ? "their" : "its"} own until{" "}
      <strong>{s.blockedOn}</strong> is on (the {s.label} tab). You can still start{" "}
      {many ? "any of them" : "it"} here, with <strong>Start</strong>.
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
  ): Promise<{ message: string; title?: string }>;
}) {
  const groups = byGroup(s.items);
  return (
    <div className="ik-queue-section">
      <div className="ik-queue-head">
        <span className="ik-queue-kind">{s.label}</span>
        <span className="ik-tab-count">{s.items.length}</span>
        <StateChip state={s.broken ? "broken" : s.state} device={s.runsOn} paused={s.paused} />
        <button
          type="button"
          className="test-btn ik-queue-goto"
          onClick={() => gotoTab(s.tab)}
          title={"Open the " + s.label + " tab"}
        >
          Open {s.label} ›
        </button>
      </div>
      {s.broken ? (
        <div className="pr-open-note">{s.broken}</div>
      ) : s.note ? (
        <div className="pr-open-note">{s.note}</div>
      ) : null}
      {s.items.length === 0 ? (
        // A plain line, not the boxed `.repo-empty` the other tabs use for an
        // empty list: three kinds are always on screen here, and three empty
        // boxes make a quiet queue look like a broken one.
        <div className="ik-queue-empty">
          {s.broken
            ? ""
            : s.state === "unset"
              ? "Nothing set up yet — set it up on the " + s.label + " tab."
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
                    route={item.route}
                    // Only a section that is ON will auto-start anything; a
                    // paused or half-set-up one already says why above
                    // (StalledNote), and a green "will auto-start" chip under
                    // it would contradict that sentence on every row.
                    eligible={s.state === "on"}
                    eligibleLabel={s.state === "on" ? "will auto-start" : ""}
                    actionLabel="Start"
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
  // One status call answers all three switches plus the engine — the shared
  // query the Tickets tab's switch and the sidebar's AutomationBar read too.
  // Preferred over the panels' own `enabled` flags because the server folds
  // "switched on" and "has repos" together in one place here, and because the
  // engine state has to come from here anyway.
  const statusQ = useMindflockStatus();
  const status = statusQ.data;
  const engineAvailable = !!status?.available;
  // `desired` is the switch's own last position; `running` can lag it by a boot.
  const engineOn = !!(status?.desired ?? status?.running);
  // PR review and issue handling run on ONE of your devices; on the others
  // their sections say where, not "switch it on" (that switch is shared, and
  // the server folds "not here" into pr_enabled/issues_enabled).
  const runsElsewhere = status?.automation_here === false;
  const runsOn = runsElsewhere ? status?.automation_device || "" : "";

  const agentChoices = useAgentChoices();
  const s = useSettings();
  const gh = (s.settings.github || {}) as {
    enabled?: boolean;
    issues_enabled?: boolean;
    agent?: string;
    issue_agent?: string;
    repo_settings?: RepoOverrides;
    issue_repo_settings?: RepoOverrides;
  };
  const ticketSources = ((s.settings.ticketing || {}) as {
    sources?: Array<{ id?: string; agent?: string; depth?: string; label?: string; provider?: string }>;
  }).sources;
  const pause = useIntakePause((ticketSources || []).length);

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
      title = r?.title || item.reference;
    } else if (t.kind === "prs") {
      const r = await api<{ title?: string }>("/api/github/prs/review", {
        json: { repo: t.repo, number: t.number, ...extra },
      });
      title = r?.title || "";
    } else {
      const r = await api<{ title?: string }>("/api/github/issues/start", {
        json: { repo: t.repo, number: t.number, ...extra },
      });
      title = r?.title || "";
    }
    refreshInstances();
    const panel =
      item.kind === "tickets" ? ticketsQ : item.kind === "prs" ? prsQ : issuesQ;
    setTimeout(() => void panel.refetch(), 5000);
    return { message: "Session " + title, title };
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

  // Ticket sources that are saved but couldn't be listed: the section says
  // so by name ("Shortcut can't connect"), rather than "not set up" — which
  // sends you to set up something that already is.
  const configuredTickets = (ticketSources || []).length;
  const ticketErrors = ((ticketsQ.data as { errors?: Array<{ source: string; error: string }> } | null)?.errors || [])
    .filter((e) => (ticketSources || []).some((x) => x.id === e.source));
  const listedTickets = (ticketsQ.data?.sources || []).length;
  const catalog = useTicketingCatalog(true).data || [];
  const nameOfSource = (id: string) => {
    const src = (ticketSources || []).find((x) => x.id === id);
    return src ? sourceDisplayName(src as unknown as TicketingSource, catalog) : id;
  };
  const ticketsBroken =
    configuredTickets && ticketErrors.length && !listedTickets
      ? ticketErrors
          .map((e) => friendlySourceError(nameOfSource(e.source), e.error).replace(/ — open its card below to fix it\.$/, "."))
          .join(" ") + " Fix it on the Tickets tab."
      : "";
  const paused = pause.paused;

  const sections: Section[] = [
    {
      kind: "tickets",
      label: "Tickets",
      tab: "tickets",
      state: runState({
        configured: configuredTickets > 0,
        engineAvailable,
        engineOn,
        // Tickets have no switch of their own — the engine is it.
        switchOn: true,
      }),
      blockedOn: "Auto-start tickets",
      paused,
      broken: ticketsBroken,
      items: queuedOf("tickets", payloads),
      note: noteFor(ticketsQ, "tickets"),
    },
    {
      kind: "prs",
      label: "GitHub PRs",
      tab: "prs",
      // PR comments and issues run on their own switches: the pipeline boots
      // for whichever halves are on, so the ticket switch is not their gate
      // (it used to be read as one, which said "off" for a PR switch that was
      // on and working).
      state: runState({
        configured: (prsQ.data?.repos || []).length > 0,
        engineAvailable: true,
        engineOn: true,
        switchOn: status?.pr_enabled === true,
        // The shared switch itself: when it is off it is off on every device,
        // and that IS the thing to fix.
        runsElsewhere: runsElsewhere && gh.enabled !== false,
      }),
      blockedOn: "Auto-address review comments",
      paused,
      runsOn,
      items: queuedOf("prs", payloads),
      note: noteFor(prsQ, "PRs"),
    },
    {
      kind: "issues",
      label: "GitHub issues",
      tab: "issues",
      state: runState({
        configured: (issuesQ.data?.repos || []).length > 0,
        engineAvailable: true,
        engineOn: true,
        switchOn: status?.issues_enabled === true,
        runsElsewhere: runsElsewhere && gh.issues_enabled === true,
      }),
      blockedOn: "Auto-start new issues",
      paused,
      runsOn,
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
  // Only the sections that are ON will start anything by themselves — the
  // same rule as each row's chip. Counting the rest as "will auto-start" put
  // a promise in the toolbar that every paused section's own note denied.
  const autoTotal = sections
    .filter((sec) => sec.state === "on")
    .reduce((n, sec) => n + sec.items.length, 0);

  const refreshAll = () => {
    ticketsQ.refresh();
    prsQ.refresh();
    issuesQ.refresh();
  };

  const held = status?.held_for_slot;

  return (
    <>
      <label className="set-row" id="ik-max-sessions-row">
        <span className="set-label">Max ticket sessions at once</span>
        <SettingField group="engine" field="max_sessions" type="number" placeholder="no limit" />
        <span className="set-hint">
          {held ? (
            <>
              <strong>
                Holding: {held.live} of {held.max} ticket sessions are running.
              </strong>{" "}
              The next ticket starts when one of them is closed.{" "}
            </>
          ) : null}
          Past this, the rest wait their turn. Blank = no limit.
        </span>
      </label>
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
                : autoTotal === total
                  ? total + (total === 1 ? " item will auto-start" : " items will auto-start")
                  : total + " waiting · " + autoTotal + " will auto-start"}
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
          {/* Not a work queue, but the same answer: what starts by itself. */}
          {!filter.active && <ScheduledTemplates />}
        </div>
        <span className="set-hint">
          What would start on its own next, oldest first. Each other tab says why anything
          else is skipped.
        </span>
      </div>
    </>
  );
}
