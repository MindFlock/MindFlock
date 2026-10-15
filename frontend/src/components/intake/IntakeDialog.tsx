/** The Intake dialog — where sessions come from.
 *
 * Ticketing, PR review and issue handling used to be three screens in Settings.
 * They don't belong there: Settings is where you configure the app once and
 * forget it, while these three are somewhere you *visit* — you come to see what
 * came in, start something by hand, or switch a queue off. Sitting in the same left
 * nav as Appearance and System logs, they were both hard to find and mixed in
 * with things that never change.
 *
 * So they get their own top-bar entry, and inside it a tab strip with live
 * counts, because "how much is waiting" is the question the surface exists to
 * answer and you should not have to open a tab to find out.
 *
 * Each tab has the same anatomy — switch, grouped work list whose source
 * headings are where each source is edited, "+ Add source" in the head —
 * built from ./kit.tsx. The three legacy Settings screen keys still route here
 * (see SettingsDialog), so every old deep link lands on the right tab.
 */

import { useEffect, useRef, useState } from "react";
import { useUi } from "../../state/store";
import { Modal } from "../Modal";
import {
  prefetchIntakeMeta,
  prefetchIntakePanels,
  usePanelQuery,
  useTicketingSources,
} from "../../state/queries";
import { SettingsCtx, useSettingsModel } from "../settings/useSettings";
import { countInBuckets, loadMineOnly, loadShownBuckets, visibleBuckets } from "./buckets";
import { TicketsTab } from "./TicketsTab";
import { PullRequestsTab } from "./PullRequestsTab";
import { IssuesTab } from "./IssuesTab";
import { QueueTab } from "./QueueTab";
import { collectQueued, isQueued } from "./queue";
import { kindsText, useIntakeSwitches } from "./switches";
import type { RepoUse } from "./repoUses";
import { TourButton } from "../onboarding/TourButton";
import { useAutoTour } from "../onboarding/useAutoTour";

export type IntakeTabKey = "tickets" | "prs" | "issues" | "autostart";

/** What "+ Add source" was asked to add: a tracker (optionally a provider
 * already picked — a tracker tile pressed on another tab hands over here), a
 * repository with its uses pre-ticked, or nothing yet (the tiles decide). */
export interface AddIntent {
  kind: "tracker" | "repo" | null;
  provider?: string;
  uses?: RepoUse[];
}

export interface TabProps {
  /** Switch tab; `focus` names a source (id) or repository (owner/name) whose
   * heading to bring into view with its editor open. */
  gotoTab(key: IntakeTabKey, focus?: string): void;
  /** The source or repository a deep link asked for ("" = none). */
  focus?: string;
  /** The add flow, when one is open on this tab. */
  adding: AddIntent | null;
  startAdd(intent: AddIntent, tab?: IntakeTabKey): void;
  endAdd(): void;
}

/** A dialog target: a tab key, a legacy Settings screen key, or either with a
 * source after a colon — `tickets:jira` from Settings → Connections' "Open in
 * Intake", `prs:acme/api`. Unknown tabs fall back to Tickets. */
export function parseIntakeTarget(target: string | null | undefined): {
  tab: IntakeTabKey;
  focus: string;
} {
  const raw = (target || "").trim();
  const cut = raw.indexOf(":");
  const head = cut < 0 ? raw : raw.slice(0, cut);
  const focus = cut < 0 ? "" : raw.slice(cut + 1).trim();
  const wanted = (LEGACY_SCREEN_TABS[head] || head) as IntakeTabKey;
  const tab = TAB_KEYS.includes(wanted) ? wanted : "tickets";
  return { tab, focus };
}

const TAB_KEYS: IntakeTabKey[] = ["tickets", "prs", "issues", "autostart"];

/** Legacy Settings screen keys → the tab that took over from them. Kept so a
 * deep link recorded anywhere (a palette entry, a sidebar button, the welcome
 * tour, the server's `settings_screen` on a Connections card) keeps working. */
export const LEGACY_SCREEN_TABS: Record<string, IntakeTabKey> = {
  ticketing: "tickets",
  repo: "prs",
  issues: "issues",
};

const TABS: Array<{
  key: IntakeTabKey;
  label: string;
  /** The caps this tab's content needs, as capsGate.css matches them. */
  caps?: string;
  /** Legacy DOM id kept for addon CSS that targeted the old screen. */
  legacyId?: string;
  el: (p: TabProps) => React.ReactNode;
}> = [
  { key: "tickets", label: "Tickets", el: (p) => <TicketsTab {...p} /> },
  // Git only: PR comments and issues run on their own switches and their own
  // GitHub sign-in — the pipeline boots for them with no ticket source at all —
  // so they are not locked behind connecting a tracker.
  {
    key: "prs",
    label: "GitHub PRs",
    caps: "git",
    legacyId: "pr-review-block",
    el: (p) => <PullRequestsTab {...p} />,
  },
  {
    key: "issues",
    label: "GitHub issues",
    caps: "git",
    legacyId: "git-issues-block",
    el: (p) => <IssuesTab {...p} />,
  },
  // Last, and not the landing tab: the three tabs before it are where you set a
  // source up, and this one only has anything to say once one of them is
  // working. Its badge carries the answer anyway, which is the point of the
  // counts being on the strip.
  //
  // "Auto-start" rather than "Queue": a queue of what, and queued behind what?
  // The rows are here precisely because the automations would take them on their
  // own, and the label should say that — including when the switch is off, where
  // the tab's job is to show exactly what would go the moment you turn it on.
  { key: "autostart", label: "Auto-start", el: (p) => <QueueTab {...p} /> },
];

/** The Auto-start tab's badge: how many will start on their own — or, when
 * none will (auto-start off), how many are waiting, in grey.
 *
 * Its own component because it needs all three panels, and because a hook call
 * cannot be conditional on which tab we are counting. Shares `collectQueued`
 * with the tab body, so the badge and the list can never disagree. */
function AutoStartCount({ on }: { on: boolean }) {
  const tickets = usePanelQuery<{ tickets?: unknown[]; stale?: boolean }>("tickets");
  const prs = usePanelQuery<{ prs?: unknown[]; stale?: boolean }>("github-prs");
  const issues = usePanelQuery<{ issues?: unknown[]; stale?: boolean }>("github-issues");
  const n = collectQueued({
    tickets: (tickets.data ?? null) as never,
    prs: (prs.data ?? null) as never,
    issues: (issues.data ?? null) as never,
  }).length;
  if (!n) return null;
  return (
    <span
      className={"ik-tab-count" + (on ? "" : " muted")}
      title={on ? n + " will start on their own" : n + " waiting — auto-start is off"}
    >
      {/* A word when it means something different from the other tabs'
          "ready to start" counts: these are waiting on a switch. */}
      {on ? n : n + " waiting"}
    </span>
  );
}

/** The count badge on a tab: how many rows there need you — the ones ready
 * to start (what auto-start would take), not every row the tab lists.
 *
 * Reads the same cached panel query the tab itself uses, so opening the dialog
 * costs one fan-out per panel either way — and, for tickets, applies the same
 * bucket filter the panel does, so a ticket in a hidden bucket isn't counted. */
function TabCount({ tab }: { tab: Exclude<IntakeTabKey, "autostart"> }) {
  const key = tab === "tickets" ? "tickets" : tab === "prs" ? "github-prs" : "github-issues";
  const q = usePanelQuery<{
    tickets?: Array<{ bucket?: string; mine?: boolean; eligible?: boolean; has_session?: boolean }>;
    prs?: Array<{ eligible?: boolean; has_session?: boolean }>;
    issues?: Array<{ eligible?: boolean; has_session?: boolean }>;
    buckets?: string[];
    done_buckets?: string[];
    stale?: boolean;
  }>(key as "tickets" | "github-prs" | "github-issues");
  if (!q.data) return null;
  const n =
    tab === "tickets"
      ? countInBuckets(
          (q.data.tickets || []).filter(isQueued),
          visibleBuckets(q.data.buckets || [], q.data.done_buckets || [], loadShownBuckets()),
          loadMineOnly()
        )
      : (q.data.prs || q.data.issues || []).filter(isQueued).length;
  // No badge at all on zero: a "0" that turns into "12" a second later reads as
  // "nothing to do", which is the one thing this surface must not say when it
  // doesn't know yet. An unconfigured panel 502s and stays silent too.
  if (!n) return null;
  return (
    <span className="ik-tab-count" title={n + " ready to start"}>
      {n}
    </span>
  );
}

/** One line above the tabs saying what auto-starts right now, across all
 * three switches (tickets, GitHub PRs, GitHub issues). With everything off,
 * "Turn on…" goes to the tab whose switch does it. Read-only: each switch is
 * flipped on its own tab (there is no "pause everything" — see ./switches.ts). */
function AutoStartLine({
  ticketSources,
  gotoTab,
}: {
  ticketSources: number;
  gotoTab(key: IntakeTabKey): void;
}) {
  const p = useIntakeSwitches(ticketSources);
  const line = p.onKinds.length
    ? "Auto-start is on for " + kindsText(p.onKinds) + "."
    : p.anyConfigured
      ? "Auto-start is off everywhere."
      : "Nothing auto-starts yet — set up a tab below first.";
  // Where "Turn on…" goes: the first tab with something set up but off.
  const offTab: IntakeTabKey | null = p.onKinds.length
    ? null
    : p.sw.configured.tickets
      ? "tickets"
      : p.sw.configured.prs
        ? "prs"
        : p.sw.configured.issues
          ? "issues"
          : null;
  return (
    <div className="ik-autostart-line" id="ik-autostart-row">
      <span className="ik-autostart-text" id="ik-autostart-status" role="status" aria-live="polite">
        {line}
      </span>
      {offTab && (
        <button
          type="button"
          id="ik-turn-on-btn"
          className="linklike"
          title="Go to the switch that turns auto-start on"
          onClick={() => gotoTab(offTab)}
        >
          Turn on…
        </button>
      )}
    </div>
  );
}

const panelId = (t: (typeof TABS)[number]) => t.legacyId || "intake-tabpanel-" + t.key;

/** The Auto-start badge, coloured by whether anything is switched on. */
function AutoStartOn() {
  const sources = useTicketingSources(true).data || [];
  const p = useIntakeSwitches(sources.length);
  return <AutoStartCount on={p.onKinds.length > 0} />;
}

export function IntakeDialog() {
  const open = useUi((s) => s.openDialog === "intake");
  useAutoTour("intake", open);
  const target = useUi((s) => s.dialogTarget);
  const closeDialog = useUi((s) => s.closeDialog);
  const [tab, setTab] = useState<IntakeTabKey>("tickets");
  const [focus, setFocus] = useState("");
  const [adding, setAdding] = useState<AddIntent | null>(null);
  const model = useSettingsModel(open);
  const savedSources = useTicketingSources(open).data || [];
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  // The target may be a tab key, one of the legacy Settings screen keys, or
  // either with a source after a colon (see parseIntakeTarget).
  useEffect(() => {
    if (!open) {
      setAdding(null);
      setFocus("");
      return;
    }
    const t = parseIntakeTarget(target);
    setTab(t.tab);
    setFocus(t.focus);
  }, [open, target]);

  // The panels are normally already warm before this dialog exists (App's
  // useIntakeWarm), which is what makes an open land on a list rather than a
  // spinner. This is the fallback for the case that loop deliberately skips —
  // no ticketing source connected when the page loaded, then one gets
  // connected — plus a top-up for anything that went stale between warms.
  useEffect(() => {
    if (!open) return;
    prefetchIntakePanels();
    prefetchIntakeMeta();
  }, [open]);

  if (!open) return null;

  const switchTab = (key: IntakeTabKey, nextFocus = "") => {
    setTab(key);
    setFocus(nextFocus);
    setAdding(null);
  };
  /** "+ Add source": the flow opens at the top of the tab that owns the kind,
   * scrolled into view. */
  const startAdd = (intent: AddIntent, on?: IntakeTabKey) => {
    if (on) setTab(on);
    setFocus("");
    setAdding(intent);
    document.getElementById("intake-body")?.scrollTo({ top: 0 });
  };
  const props: TabProps = {
    gotoTab: switchTab,
    focus,
    adding,
    startAdd,
    endAdd: () => setAdding(null),
  };
  // What the head button adds from each tab: the kind that tab shows.
  const addFrom = (): [AddIntent, IntakeTabKey] =>
    tab === "prs"
      ? [{ kind: "repo", uses: ["pr"] }, "prs"]
      : tab === "issues"
        ? [{ kind: "repo", uses: ["issue"] }, "issues"]
        : [{ kind: null }, "tickets"];
  // Arrow keys move along the tab strip (the WAI tablist pattern), so the
  // strip is one Tab stop rather than four.
  const onTabKey = (e: React.KeyboardEvent, i: number) => {
    const n = TABS.length;
    const to =
      e.key === "ArrowRight" ? (i + 1) % n
      : e.key === "ArrowLeft" ? (i - 1 + n) % n
      : e.key === "Home" ? 0
      : e.key === "End" ? n - 1
      : -1;
    if (to < 0) return;
    e.preventDefault();
    switchTab(TABS[to].key);
    tabRefs.current[TABS[to].key]?.focus();
  };

  return (
    <SettingsCtx.Provider value={model}>
      <Modal id="intake-dialog" className="top-dialog" onClose={closeDialog}>
        <div id="intake-panel">
          <div className="ws-head">
            <h2>Intake</h2>
            <span className="ik-subtitle">Where your sessions come from</span>
            {/* ONE door for every kind of source — a tracker, or a repository
                with its uses as ticks. It replaced four "+ Add" buttons at the
                bottom of four card lists in two dialogs. */}
            <button
              type="button"
              id="intake-add-source"
              className="btn-primary"
              disabled={!!adding}
              title="Connect a tracker, or watch a GitHub repository"
              onClick={() => {
                const [intent, on] = addFrom();
                startAdd(intent, on);
              }}
            >
              + Add source
            </button>
            <TourButton tour="intake" />
            <button type="button" id="intake-close" onClick={closeDialog}>
              Close
            </button>
          </div>
          <AutoStartLine ticketSources={savedSources.length} gotoTab={switchTab} />
          <div id="intake-tabs" role="tablist" aria-label="Intake">
            {TABS.map((t, i) => (
              <button
                key={t.key}
                ref={(el) => {
                  tabRefs.current[t.key] = el;
                }}
                type="button"
                role="tab"
                id={"intake-tab-" + t.key}
                aria-selected={tab === t.key}
                aria-controls={panelId(t)}
                tabIndex={tab === t.key ? 0 : -1}
                className={"ik-tab" + (tab === t.key ? " active" : "")}
                data-intake-tab={t.key}
                onClick={() => switchTab(t.key)}
                onKeyDown={(e) => onTabKey(e, i)}
              >
                {t.label}
                {t.key === "autostart" ? (
                  <AutoStartOn />
                ) : (
                  <TabCount tab={t.key as Exclude<IntakeTabKey, "autostart">} />
                )}
              </button>
            ))}
          </div>
          <div id="intake-body">
            {TABS.map((t) => (
              <section
                key={t.key}
                className={"ik-panel" + (tab === t.key ? " active" : "")}
                data-intake-tab={t.key}
                // Two panels keep their legacy ids (addon CSS targets them);
                // the rest get one so the tab can point at its panel.
                id={panelId(t)}
                data-caps-need={t.caps}
                role="tabpanel"
                aria-labelledby={"intake-tab-" + t.key}
              >
                {tab === t.key && t.el(props)}
              </section>
            ))}
          </div>
        </div>
      </Modal>
    </SettingsCtx.Provider>
  );
}
