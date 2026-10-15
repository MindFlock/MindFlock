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
import type { RepoOverrides } from "./RepoSources";
import { queuedMatches } from "./search";
import { ScheduledTemplates, nextScheduledText, useScheduledTemplates } from "./ScheduledTemplates";
import { AutoStartSwitches, useBrokenTicketSources } from "./AutoStartSwitches";
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
  /** The switch that would make this section's rows move, named as the
   * Auto-start tab's switches name it ("Tickets", "PR review comments"). */
  switchName: string;
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
}: {
  state: QueueRunState | "broken";
  device?: string;
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
  return <span className="pr-open-chip">auto-start off</span>;
}

/** Why these rows are not moving, derived from the SAME state the section's
 * chip shows — so the chip and the sentence can never disagree (round 5: a
 * "not set up" chip sat over "auto-start is off — turn on Auto-start tickets"
 * while that switch was on). Run elsewhere, nothing here needs fixing: say
 * where they run, and that the switch isn't the reason. */
export function stalledText(
  s: Pick<Section, "state" | "label" | "switchName" | "runsOn" | "broken">,
  many: boolean
): string {
  const they = many ? "They" : "It";
  const any = many ? "any of them" : "it";
  if (s.broken) return s.broken;
  if (s.state === "elsewhere")
    return (
      runsOnText(s.runsOn) + " — " + (s.runsOn || "that device") + " starts " + (many ? "these" : "this one") +
      " on its own; this computer doesn't (to move them here, choose Run here in Settings → Your devices). You can still start " +
      any + " here, with Start."
    );
  if (s.state === "unset")
    return "Not set up — auto-start isn't available for " + s.label + " here yet. You can still start " + any + " with Start.";
  return (
    they + " won't start on " + (many ? "their" : "its") + " own: " + s.switchName +
    " is off (above). You can still start " + any + " here, with Start."
  );
}

function StalledNote({ s }: { s: Section }) {
  return (
    <div className="ik-queue-stalled" data-runs-on={s.state === "elsewhere" ? s.runsOn || "" : undefined}>
      {stalledText(s, s.items.length !== 1)}
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
        <StateChip state={s.broken ? "broken" : s.state} device={s.runsOn} />
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
              ? "Not set up yet — set it up on the " + s.label + " tab."
              : "Nothing waiting."}
        </div>
      ) : (
        <>
          {/* A section that can't connect already said so, once, above. */}
          {s.state !== "on" && !s.broken && <StalledNote s={s} />}
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
                    // switched-off or half-set-up one already says why above
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
    return { message: title ? "Session " + title : "Session for " + item.reference, title };
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
  const catalog = useTicketingCatalog(true).data || [];
  const nameOfSource = (id: string) => {
    const src = (ticketSources || []).find((x) => x.id === id);
    return src ? sourceDisplayName(src as unknown as TicketingSource, catalog) : id;
  };
  // Every saved source can't connect: the section says so by name and where
  // to fix it — "Can't connect — Jira: No Jira site at … — press Fix on the
  // Tickets tab" — never "Nothing waiting" or "auto-start is off".
  const brokenSources = useBrokenTicketSources();
  const ticketsBroken =
    configuredTickets && brokenSources.length >= configuredTickets
      ? "Can't connect — " +
        brokenSources.map((b) => b.why.replace(/ — press Fix on its heading\.$/, ".")).join(" ") +
        " Press Fix on the Tickets tab."
      : "";

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
      switchName: "Tickets",
      broken: ticketsBroken,
      // Grouped under the source's name as the Tickets tab says it ("Jira —
      // payments"), never its id ("JIRA-1").
      items: queuedOf("tickets", payloads).map((i) =>
        i.target.kind === "tickets" ? { ...i, group: nameOfSource(i.target.source) } : i
      ),
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
      switchName: "PR review comments",
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
      switchName: "New issues",
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
  // a promise in the toolbar that every switched-off section's own note denied.
  const autoTotal = sections
    .filter((sec) => sec.state === "on")
    .reduce((n, sec) => n + sec.items.length, 0);

  const refreshAll = () => {
    ticketsQ.refresh();
    prsQ.refresh();
    issuesQ.refresh();
  };

  const held = status?.held_for_slot;
  // Scheduled templates start on their own too: the count says so.
  const sched = useScheduledTemplates();
  const nextSched = nextScheduledText(sched.templates, sched.status);

  return (
    <>
      <AutoStartSwitches gotoTab={gotoTab} />
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
              ? nextSched
                ? "Next to start on its own: " + nextSched
                : "Nothing waiting to start"
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
