/** The New dialog's list mode (SPEC §7.C.1): the describe box read as "one
 * thing per line, or ticket IDs", shown back as a list of rows, plus the
 * choices that decide how far each one goes and how they ship.
 *
 * `useRunDraft` owns the state and the server's reading of the box
 * (`POST /api/runs/preview`, debounced, stamped with the text it answers so an
 * overtaken reply can never land); `RunOptions` draws it. NewSessionDialog
 * stays the owner of the box, the buttons and the single-session flow, which
 * is untouched: one plain line never reaches the preview at all.
 *
 * Starting is `POST /api/runs`. MindFlock owns those sessions from then on —
 * the queue, the retries and the shipping are the server's, so the dialog's
 * job ends at a request that says exactly what the sentence under it says. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api/client";
import { errMsg } from "../../lib/format";
import { SERVER_NO_TOGETHER, LANE_CHOICES, LANE_LABEL, type Lane } from "../../lib/laneActions";
import {
  CONCURRENCY_DEFAULT,
  CONCURRENCY_MAX,
  CONCURRENCY_MIN,
  clampConcurrency,
  defaultLaneFor,
  fallbackName,
  isListMode,
  itemRows,
  localItems,
  oneForAllLane,
  requestItems,
  runBody,
  splitApplies,
  summarySentence,
  type Grouping,
  type ItemRow,
  type PreviewResponse,
} from "../../lib/runStart";

/** How long the box must sit still before the server is asked to read it. */
export const PREVIEW_DEBOUNCE_MS = 300;

/** "jira-payments" → "Jira · payments": a source KEY made readable. The
 * server's own label wins when it sends one (the preview carries keys only). */
export function prettySource(key: string): string {
  const k = String(key || "");
  const i = k.indexOf("-");
  const head = i > 0 ? k.slice(0, i) : k;
  const rest = i > 0 ? k.slice(i + 1) : "";
  const cap = head.charAt(0).toUpperCase() + head.slice(1);
  return rest ? cap + " · " + rest : cap;
}

export interface RunDraft {
  /** The box, read locally. */
  items: ReturnType<typeof localItems>;
  listMode: boolean;
  /** Exactly one plain task line — the only shape a split applies to. */
  oneTask: boolean;
  rows: ItemRow[];
  /** How many sessions a start would make, as the rows show it. */
  count: number;
  previewError: string;
  warnings: string[];
  lane: Lane;
  setLane(l: Lane): void;
  askFirst: boolean;
  setAskFirst(on: boolean): void;
  grouping: Grouping;
  setGrouping(g: Grouping): void;
  /** The server takes "One for all" (caps.team_runs.together). */
  togetherOk: boolean;
  /** One PR for all (or a split): every line is committed into the group's
   * branch, so "Leave it" and "ask me first" do not apply (the lane shown is
   * the one sent — see runStart.oneForAllLane). */
  oneForAll: boolean;
  concurrency: number;
  setConcurrency(n: number): void;
  name: string;
  setName(n: string): void;
  starting: boolean;
  /** Start them: preview first if the reading is stale, refuse with the reason
   * (returned) when a row can't start, else POST /api/runs. */
  start(o: {
    split: boolean;
  }): Promise<
    { ok: true; body: Record<string, unknown>; run: unknown } | { ok: false; error: string }
  >;
}

export function useRunDraft(o: {
  open: boolean;
  text: string;
  repoPath: string;
  program: string;
  fasttrackDepth: string | undefined;
  /** The split box is ticked (and the agent can take one). */
  split: boolean;
  /** caps.team_runs.together — false: every group is one PR per line. */
  togetherOk?: boolean;
}): RunDraft {
  const { open, text, repoPath, program } = o;
  const items = useMemo(() => localItems(text), [text]);
  const listMode = isListMode(items);
  const oneTask = splitApplies(items);
  const asked = text.trim();

  // The server's reading, stamped with the exact text (and folder) it read.
  const [preview, setPreview] = useState<{
    asked: string;
    repo: string;
    data: PreviewResponse | null;
    error: string;
  } | null>(null);
  // Explicit choices; null = follow the default for what is on screen.
  const [laneChoice, setLaneChoice] = useState<Lane | null>(null);
  const [askFirst, setAskFirst] = useState(false);
  const [groupingRaw, setGrouping] = useState<Grouping>("each");
  const togetherOk = o.togetherOk === true;
  // One-for-all is never sent to a server that would refuse it.
  const grouping: Grouping = togetherOk ? groupingRaw : "each";
  const [concurrency, setConcurrencyRaw] = useState(CONCURRENCY_DEFAULT);
  const [name, setNameRaw] = useState("");
  const nameTouched = useRef(false);
  const [starting, setStarting] = useState(false);

  // Every opening starts clean — the dialog component never unmounts.
  useEffect(() => {
    if (!open) return;
    setPreview(null);
    setLaneChoice(null);
    setAskFirst(false);
    setGrouping("each");
    setConcurrencyRaw(CONCURRENCY_DEFAULT);
    setNameRaw("");
    nameTouched.current = false;
    setStarting(false);
  }, [open]);

  const ask = useCallback(
    async (t: string, repo: string): Promise<PreviewResponse> =>
      api<PreviewResponse>("/api/runs/preview", {
        json: { text: t, repo_path: repo, program },
      }),
    [program]
  );

  useEffect(() => {
    if (!open || !listMode || !asked) return;
    if (preview && preview.asked === asked && preview.repo === repoPath) return;
    let live = true;
    const timer = window.setTimeout(async () => {
      try {
        const data = await ask(asked, repoPath);
        if (live) setPreview({ asked, repo: repoPath, data, error: "" });
      } catch (err) {
        if (live) setPreview({ asked, repo: repoPath, data: null, error: errMsg(err) });
      }
    }, PREVIEW_DEBOUNCE_MS);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
    // `preview` is read for its stamp only; depending on it would re-arm the
    // timer on every answer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, listMode, asked, repoPath, ask]);

  const fresh = preview && preview.asked === asked && preview.repo === repoPath ? preview : null;

  // The server's suggested name, never over one the user typed. Between
  // answers (the box just changed) the last suggestion stays rather than
  // flickering to the local fallback and back.
  useEffect(() => {
    if (nameTouched.current || !listMode) return;
    const sug = fresh?.data?.name_suggestion || "";
    if (sug) setNameRaw(sug);
    else setNameRaw((cur) => cur || fallbackName(items));
  }, [fresh?.data?.name_suggestion, listMode, items]);

  const repoLabel = repoPath.replace(/\/+$/, "").split("/").pop() || "";
  const rows = useMemo(
    () => (listMode ? itemRows(items, fresh?.data ?? null, repoLabel, prettySource) : []),
    [listMode, items, fresh, repoLabel]
  );

  // One line keeps today's behaviour (nothing ships unless the user says so);
  // a list or a split starts on the server's suggestion or the fast-track
  // default. An explicit pick wins either way.
  const oneForAll =
    (o.split && oneTask) || (grouping === "together" && (rows.length || items.length) >= 2);
  const chosen =
    laneChoice ??
    defaultLaneFor(listMode || (o.split && oneTask), fresh?.data?.lane_default, o.fasttrackDepth);
  const lane = oneForAll ? oneForAllLane(chosen) : chosen;

  const start: RunDraft["start"] = async ({ split }) => {
    if (starting) return { ok: false, error: "" };
    setStarting(true);
    try {
      let data = fresh?.data ?? null;
      if (!data) {
        try {
          data = await ask(asked, repoPath);
          setPreview({ asked, repo: repoPath, data, error: "" });
        } catch (err) {
          return { ok: false, error: "Couldn't read the list: " + errMsg(err) };
        }
      }
      const bad = data.items.filter((i) => i.error);
      if (bad.length) {
        const refs = bad.map((b) => b.ref || b.id || b.text || "?").join(", ");
        return {
          ok: false,
          error: `${refs} couldn't be found — remove ${bad.length === 1 ? "it" : "them"} (✕) or fix the ID first.`,
        };
      }
      const req = requestItems(data);
      if (!req.length)
        return {
          ok: false,
          error: "Nothing to start — type a line or a ticket ID.",
        };
      const body = runBody({
        name: name || fallbackName(items),
        items: req,
        lane,
        askFirst,
        grouping: req.length >= 2 ? grouping : "each",
        concurrency,
        program,
        repoPath,
        split,
      });
      try {
        const r = await api<{ run?: unknown }>("/api/runs", { json: body });
        return { ok: true, body, run: r?.run ?? null };
      } catch (err) {
        return { ok: false, error: errMsg(err) };
      }
    } finally {
      setStarting(false);
    }
  };

  return {
    items,
    listMode,
    oneTask,
    rows,
    count: rows.length || items.length,
    previewError: listMode && fresh?.error ? fresh.error : "",
    warnings: fresh?.data?.warnings || [],
    lane,
    setLane: setLaneChoice,
    askFirst: oneForAll ? false : askFirst,
    setAskFirst,
    grouping,
    setGrouping,
    togetherOk,
    oneForAll,
    concurrency,
    setConcurrency: (n) => setConcurrencyRaw(clampConcurrency(n)),
    name,
    setName: (n) => {
      nameTouched.current = true;
      setNameRaw(n);
    },
    starting,
    start,
  };
}

// --- Drawing it -------------------------------------------------------------------

/** The parsed list: kind chip, ref, title, where it comes from, ✕. A ticket
 * that didn't resolve is red with the server's reason — never quietly a task. */
export function RunItems({ rows, onRemove }: { rows: ItemRow[]; onRemove(row: ItemRow): void }) {
  if (!rows.length) return null;
  return (
    <div className="rt-items" role="list" aria-label="What will start">
      {rows.map((r) => (
        <div
          key={r.key}
          role="listitem"
          className={
            "rt-item rt-" + r.kind + (r.error ? " rt-err" : "") + (r.pending ? " rt-pending" : "")
          }
          title={r.error || undefined}
        >
          <span className="rt-kind">{r.kind === "ticket" ? "TICKET" : "TASK"}</span>
          {r.kind === "ticket" && <span className="rt-ref">{r.ref}</span>}
          <span className="rt-title">
            {r.error ? (
              r.error
            ) : r.pending ? (
              <span className="muted">looking it up…</span>
            ) : (
              r.title
            )}
          </span>
          <span className="rt-where">{r.where}</span>
          <button
            type="button"
            className="rt-x"
            aria-label={"Remove " + (r.ref || r.title)}
            title="Take this one out of the list"
            onClick={() => onRemove(r)}
          >
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}

/** A small segmented control: one pressed button out of a few. */
function Seg<T extends string>({
  value,
  options,
  onChange,
  label,
  id,
}: {
  value: T;
  options: ReadonlyArray<{ v: T; label: string; title?: string; disabled?: boolean }>;
  onChange(v: T): void;
  label: string;
  id?: string;
}) {
  return (
    <div className="rt-seg" role="group" aria-label={label} id={id}>
      {options.map((o) => (
        <button
          key={o.v}
          type="button"
          className={o.v === value ? "on" : undefined}
          aria-pressed={o.v === value}
          title={o.title}
          disabled={o.disabled || undefined}
          onClick={() => onChange(o.v)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

const LANE_TITLES: Record<Lane, string> = {
  leave: "MindFlock doesn't commit anything — you take it from there",
  commit: "Commit with a message written from the diff once the agent stops and your hooks pass",
  push: "Commit and push once the agent stops and your hooks pass",
  pr: "Commit, push and open a PR once the agent stops and your hooks pass",
  merge: "…and merge the PR once its checks pass",
};

/** Everything under the list: the lane (always), the batch rows (two or
 * more), the split box (passed in — it is shared with the single flow), and
 * the sentence that says what all of it means. */
export function RunOptions({
  draft,
  n,
  split,
  splitBox,
  repoPicker,
}: {
  draft: RunDraft;
  /** How many sessions this would start (1 for the single flow). */
  n: number;
  split: boolean;
  splitBox: React.ReactNode;
  /** The folder the typed tasks (or a split's lead) start in — only when one
   * needs a folder. A list carries it on the Group as row; a split gets a
   * row of its own. */
  repoPicker?: React.ReactNode;
}) {
  const many = n >= 2 && !split;
  const oneForAll = split || (n >= 2 && draft.grouping === "together");
  const laneOpts = LANE_CHOICES.map((l) => ({
    v: l,
    label: LANE_LABEL[l],
    title:
      oneForAll && l === "leave"
        ? "One PR for all commits each line into the group's branch — Commit keeps it all on this machine"
        : LANE_TITLES[l],
    disabled: oneForAll && l === "leave",
  }));
  if (draft.lane === "push")
    laneOpts.splice(2, 0, {
      v: "push",
      label: LANE_LABEL.push,
      title: LANE_TITLES.push,
      disabled: false,
    });
  const sum = summarySentence({
    n,
    concurrency: draft.concurrency,
    lane: draft.lane,
    askFirst: draft.askFirst,
    grouping: draft.grouping,
    split,
  });
  return (
    <div className="rt-opts">
      <div className="rt-row rt-row-top">
        <span className="rt-label">{many ? "When each is done" : "When it's done"}</span>
        <div className="rt-ctl">
          <Seg
            id="new-lane"
            label={many ? "When each is done" : "When it's done"}
            value={draft.lane}
            options={laneOpts}
            onChange={draft.setLane}
          />
          <label
            className={"check rt-ask" + (draft.lane === "leave" || oneForAll ? " disabled" : "")}
            title={
              oneForAll
                ? "One PR for all always asks you before the one PR — its lines are committed into the group's branch as they finish"
                : draft.lane === "leave"
                  ? "Nothing ships while it's “Leave it”"
                  : "Stop one step before the first commit and show it in the Outbox first"
            }
          >
            <input
              type="checkbox"
              id="new-ask-first"
              checked={!oneForAll && draft.lane !== "leave" && draft.askFirst}
              disabled={draft.lane === "leave" || oneForAll}
              onChange={(e) => draft.setAskFirst(e.target.checked)}
            />
            Ask me before it ships
          </label>
        </div>
      </div>
      {many && (
        <>
          <div className="rt-row">
            <span className="rt-label">PRs</span>
            <div className="rt-ctl">
              <Seg
                id="new-grouping"
                label="PRs"
                value={draft.grouping}
                options={[
                  { v: "each", label: "One per line" },
                  {
                    v: "together",
                    label: "One for all",
                    disabled: !draft.togetherOk,
                    title: draft.togetherOk
                      ? "Merge them into one branch first, then open one PR"
                      : SERVER_NO_TOGETHER,
                  },
                ]}
                onChange={draft.setGrouping}
              />
              <span className="rt-hint">
                {draft.togetherOk ? "one-for-all merges them into one branch first" : SERVER_NO_TOGETHER}
              </span>
            </div>
          </div>
          <div className="rt-row">
            <span className="rt-label">At a time</span>
            <div className="rt-ctl">
              <div className="rt-step" role="group" aria-label="At a time">
                <button
                  type="button"
                  aria-label="Fewer at a time"
                  disabled={draft.concurrency <= CONCURRENCY_MIN}
                  onClick={() => draft.setConcurrency(draft.concurrency - 1)}
                >
                  −
                </button>
                <span id="new-concurrency" aria-live="polite">
                  {draft.concurrency}
                </span>
                <button
                  type="button"
                  aria-label="More at a time"
                  disabled={draft.concurrency >= CONCURRENCY_MAX}
                  onClick={() => draft.setConcurrency(draft.concurrency + 1)}
                >
                  +
                </button>
              </div>
              <span className="rt-hint">the rest wait in the group, not in your grid</span>
            </div>
          </div>
          <div className="rt-row">
            <span className="rt-label">Group as</span>
            <div className="rt-ctl">
              <input
                id="new-group-name"
                className="rt-name"
                type="text"
                value={draft.name}
                maxLength={60}
                spellCheck={false}
                autoComplete="off"
                onChange={(e) => draft.setName(e.target.value)}
              />
              {repoPicker && (
                <>
                  <span className="rt-hint">tasks start in</span>
                  {repoPicker}
                </>
              )}
            </div>
          </div>
        </>
      )}
      {split && repoPicker && (
        <div className="rt-row">
          <span className="rt-label">Starts in</span>
          <div className="rt-ctl">
            {repoPicker}
            <span className="rt-hint">the lead gets a new worktree there</span>
          </div>
        </div>
      )}
      {splitBox}
      {sum && (
        <p className="rt-sum" aria-live="polite">
          {sum.lead} {sum.tail && <span className="muted">{sum.tail}</span>}
        </p>
      )}
    </div>
  );
}
