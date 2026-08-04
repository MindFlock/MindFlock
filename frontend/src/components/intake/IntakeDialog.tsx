/** The Intake dialog — where sessions come from.
 *
 * Ticketing, PR review and issue handling used to be three screens in Settings.
 * They don't belong there: Settings is where you configure the app once and
 * forget it, while these three are somewhere you *visit* — you come to see what
 * came in, start something by hand, or pause a queue. Sitting in the same left
 * nav as Appearance and System logs, they were both hard to find and mixed in
 * with things that never change.
 *
 * So they get their own top-bar entry, and inside it a tab strip with live
 * counts, because "how much is waiting" is the question the surface exists to
 * answer and you should not have to open a tab to find out.
 *
 * Each tab has the same anatomy — switch, source cards, grouped work list —
 * built from ./kit.tsx. The three legacy Settings screen keys still route here
 * (see SettingsDialog), so every old deep link lands on the right tab.
 */

import { useEffect, useState } from "react";
import { useUi } from "../../state/store";
import { prefetchIntakePanels, useConfig, usePanelQuery } from "../../state/queries";
import { SettingsCtx, useSettingsModel } from "../settings/useSettings";
import { countInBuckets, loadShownBuckets, visibleBuckets } from "./buckets";
import { TicketsTab } from "./TicketsTab";
import { PullRequestsTab } from "./PullRequestsTab";
import { IssuesTab } from "./IssuesTab";

export type IntakeTabKey = "tickets" | "prs" | "issues";

export interface TabProps {
  gotoTab(key: IntakeTabKey): void;
}

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
  {
    key: "prs",
    label: "Pull requests",
    caps: "git ticketing",
    legacyId: "pr-review-block",
    el: (p) => <PullRequestsTab {...p} />,
  },
  {
    key: "issues",
    label: "Issues",
    caps: "git ticketing",
    legacyId: "git-issues-block",
    el: (p) => <IssuesTab {...p} />,
  },
];

/** The count badge on a tab: how many rows you will find when you open it.
 *
 * Reads the same cached panel query the tab itself uses, so opening the dialog
 * costs one fan-out per panel either way — and, for tickets, applies the same
 * bucket filter the panel does. Without that it counted every ticket the
 * provider ever assigned you, Completed and Won't-do included, and read `1221`
 * over a list of 52. */
function TabCount({ tab }: { tab: IntakeTabKey }) {
  const key = tab === "tickets" ? "tickets" : tab === "prs" ? "github-prs" : "github-issues";
  const q = usePanelQuery<{
    tickets?: Array<{ bucket?: string }>;
    prs?: unknown[];
    issues?: unknown[];
    buckets?: string[];
    done_buckets?: string[];
    stale?: boolean;
  }>(key as "tickets" | "github-prs" | "github-issues");
  if (!q.data) return null;
  const n =
    tab === "tickets"
      ? countInBuckets(
          q.data.tickets || [],
          visibleBuckets(q.data.buckets || [], q.data.done_buckets || [], loadShownBuckets())
        )
      : (q.data.prs || q.data.issues || []).length;
  // No badge at all on zero: a "0" that turns into "12" a second later reads as
  // "nothing to do", which is the one thing this surface must not say when it
  // doesn't know yet. An unconfigured panel 502s and stays silent too.
  if (!n) return null;
  return <span className="ik-tab-count">{n}</span>;
}

export function IntakeDialog() {
  const open = useUi((s) => s.openDialog === "intake");
  const target = useUi((s) => s.dialogTarget);
  const closeDialog = useUi((s) => s.closeDialog);
  const [tab, setTab] = useState<IntakeTabKey>("tickets");
  const model = useSettingsModel(open);
  const { data: config } = useConfig();

  // The target may be a tab key or one of the legacy Settings screen keys.
  useEffect(() => {
    if (!open) return;
    const wanted = (target && (LEGACY_SCREEN_TABS[target] || target)) as IntakeTabKey | undefined;
    setTab(wanted && TABS.some((t) => t.key === wanted) ? wanted : "tickets");
  }, [open, target]);

  // Warm all three panels while the first tab is being read, so switching to
  // one doesn't start with a spinner — and so the tab counts fill in.
  useEffect(() => {
    if (open) prefetchIntakePanels();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        closeDialog();
        e.preventDefault();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, closeDialog]);

  if (!open) return null;

  const props: TabProps = { gotoTab: setTab };
  // Ticket ingestion is the only tab that works without git, and even it needs
  // a ticketing source; the per-tab caps gates below say so in place of the
  // content. Mirrors what the Settings screens did via `data-caps-need`.
  const noTicketing = !!config?.caps && !config.caps.ticketing;

  return (
    <SettingsCtx.Provider value={model}>
      <div
        id="intake-dialog"
        className="modal"
        onClick={(e) => {
          if (e.target === e.currentTarget) closeDialog();
        }}
      >
        <div id="intake-panel">
          <div className="ws-head">
            <h2>Intake</h2>
            <span className="ik-subtitle">Where your sessions come from</span>
            <button type="button" id="intake-close" onClick={closeDialog}>
              Close
            </button>
          </div>
          <nav id="intake-tabs" aria-label="Intake tabs">
            {TABS.map((t) => (
              <button
                key={t.key}
                type="button"
                className={"ik-tab" + (tab === t.key ? " active" : "")}
                data-intake-tab={t.key}
                aria-current={tab === t.key ? "page" : undefined}
                onClick={() => setTab(t.key)}
              >
                {t.label}
                <TabCount tab={t.key} />
              </button>
            ))}
          </nav>
          <div id="intake-body">
            {TABS.map((t) => (
              <section
                key={t.key}
                className={"ik-panel" + (tab === t.key ? " active" : "")}
                data-intake-tab={t.key}
                id={t.legacyId}
                data-caps-need={t.caps}
              >
                {tab === t.key && t.el(props)}
              </section>
            ))}
          </div>
          {noTicketing && (
            <p className="set-hint ik-foot">
              Nothing is connected yet — start on <strong>Tickets</strong>. PR review and
              issue handling ride along with ticket ingestion, so they light up once a
              ticketing source is connected.
            </p>
          )}
        </div>
      </div>
    </SettingsCtx.Provider>
  );
}
