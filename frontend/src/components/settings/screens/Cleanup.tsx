/** Settings → Disk cleanup: the worktree garbage collector (backend
 * core/worktree_gc_driver.py; docs/web-ui.md, *Disk cleanup*).
 *
 * What exists, how much space it takes, what would be removed and why the
 * rest is kept; one button collects the provably safe set, and each row has
 * its own Remove — which, for anything holding work, asks first in the app's
 * own InlineConfirm (the desktop app has no window.confirm) and says where
 * the work was saved. A folder whose files can't be checked (its repository,
 * or git's record of it, is gone) has its own section: "Look inside" shows
 * what it holds (GET /api/cleanup/inspect), and it goes only from there —
 * at once when nothing in it could be lost, else after two in-page confirm
 * steps that name its size and how many files look like work, deleted for
 * good (`even_unchecked`). The mode (Automatic · Ask me · Off) and the age
 * line save through POST /api/cleanup/settings. */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../../api/client";
import { toast } from "../../../lib/toast";
import { InlineConfirm } from "../../InlineConfirm";
import type { ScreenProps } from "../SettingsDialog";
import {
  CLEANUP_MODES,
  canRemove,
  failSentence,
  groupFreesText,
  scanNote,
  sizeText,
  cleanupModeOf,
  confirmText,
  groupItems,
  humanBytes,
  isUnchecked,
  needsConfirm,
  uncheckedConfirm,
  removedText,
  STATE_LABEL,
  summaryLine,
  usedAgo,
  whyLine,
  type CleanupItem,
  type CleanupMode,
  type CleanupReport,
  type InspectFacts,
  type InspectResult,
  type RemoveResult,
} from "../../../lib/cleanup";
import { HubCleanup } from "../../hub/HubCleanup";
import "./cleanup.css";

export function Cleanup({ active }: ScreenProps) {
  const [rep, setRep] = useState<CleanupReport | null>(null);
  const [busy, setBusy] = useState("");
  const [asking, setAsking] = useState<CleanupItem | null>(null);
  const [looks, setLooks] = useState<Record<string, InspectFacts | "loading">>({});
  const [deleting, setDeleting] = useState<{ id: string; step: 1 | 2 } | null>(null);
  const [days, setDays] = useState("");
  const timer = useRef<number | undefined>(undefined);

  const load = useCallback(async (refresh = false) => {
    window.clearTimeout(timer.current);
    try {
      const r = await api<CleanupReport>("/api/cleanup" + (refresh ? "?refresh=1" : ""));
      setRep(r);
      setDays((d) => d || String(r.settings?.after_days || 14));
      if (r.scanning) timer.current = window.setTimeout(() => void load(), 2000);
    } catch (err) {
      toast(failSentence("Couldn't read the cleanup list", err));
    }
  }, []);

  useEffect(() => {
    if (active === false) return;
    void load();
    return () => window.clearTimeout(timer.current);
  }, [active, load]);

  const saveSettings = async (patch: { mode?: CleanupMode; after_days?: number }) => {
    try {
      const s = await api<{ mode: CleanupMode; after_days: number }>("/api/cleanup/settings", { json: patch });
      setRep((r) => (r ? { ...r, settings: s } : r));
      toast("Saved");
      if (patch.after_days !== undefined) void load(true);
    } catch (err) {
      toast(failSentence("Couldn't save the cleanup setting", err));
    }
  };

  const collect = async () => {
    setBusy("collect");
    try {
      const r = await api<{ removed: unknown[]; freed_bytes: number; skipped: unknown[] }>("/api/cleanup/collect", {
        json: {},
      });
      const n = r.removed.length;
      toast(
        n
          ? "Removed " + n + (n === 1 ? " worktree" : " worktrees") + ", " + humanBytes(r.freed_bytes) + " freed."
          : "Nothing was removed: everything left holds work or is in use.",
      );
    } catch (err) {
      toast(failSentence("Couldn't clean up", err));
    } finally {
      setBusy("");
      void load();
    }
  };

  const remove = async (it: CleanupItem, confirmed: boolean) => {
    if (needsConfirm(it) && !confirmed) {
      setAsking(it);
      return;
    }
    setAsking(null);
    setBusy(it.id);
    try {
      const r = await api<RemoveResult>("/api/cleanup/remove", { json: { id: it.id, confirm: confirmed } });
      toast(removedText(it, r), { duration: r.saved?.patch || r.saved?.bundle ? 15000 : undefined });
    } catch (err) {
      toast(failSentence("Couldn't remove " + it.name, err));
    } finally {
      setBusy("");
      void load();
    }
  };

  const inspect = async (it: CleanupItem) => {
    if (looks[it.id] && looks[it.id] !== "loading") {
      setLooks(({ [it.id]: _gone, ...rest }) => rest);
      setDeleting(null);
      return;
    }
    setLooks((l) => ({ ...l, [it.id]: "loading" }));
    try {
      const r = await api<InspectResult>("/api/cleanup/inspect?id=" + encodeURIComponent(it.id));
      if (r.facts) setLooks((l) => ({ ...l, [it.id]: r.facts as InspectFacts }));
    } catch (err) {
      setLooks(({ [it.id]: _gone, ...rest }) => rest);
      toast(failSentence("Couldn't look inside " + it.name, err));
    }
  };

  const removeUnchecked = async (it: CleanupItem, anyway: boolean) => {
    setDeleting(null);
    setBusy(it.id);
    try {
      const r = await api<RemoveResult>("/api/cleanup/remove", {
        json: anyway ? { id: it.id, confirm: true, even_unchecked: true } : { id: it.id, confirm: true },
      });
      toast(removedText(it, r));
      setLooks(({ [it.id]: _gone, ...rest }) => rest);
    } catch (err) {
      toast(failSentence("Couldn't remove " + it.name, err));
    } finally {
      setBusy("");
      void load();
    }
  };

  return (
    <>
    <CleanupView
      rep={rep}
      busy={busy}
      asking={asking}
      looks={looks}
      deleting={deleting}
      onInspect={(it) => void inspect(it)}
      onDeleteStep={(it, step) => setDeleting(step ? { id: it.id, step } : null)}
      onRemoveUnchecked={(it, anyway) => void removeUnchecked(it, anyway)}
      days={days}
      onDays={setDays}
      onSaveDays={() => {
        const n = parseInt(days, 10);
        if (n >= 1 && n !== rep?.settings.after_days) void saveSettings({ after_days: n });
      }}
      onMode={(m) => void saveSettings({ mode: m })}
      onRefresh={() => void load(true)}
      onCollect={() => void collect()}
      onRemove={(it, ok) => void remove(it, ok)}
      onCancel={() => setAsking(null)}
    />
    {/* The hub's own line (components/hub/HubCleanup). */}
    <HubCleanup />
    </>
  );
}

/** The screen's markup from plain props (the vitest suite renders it). */
export function CleanupView(p: {
  rep: CleanupReport | null;
  busy: string;
  asking: CleanupItem | null;
  /** Looks inside folders that can't be checked, by item id. */
  looks?: Record<string, InspectFacts | "loading">;
  /** The unchecked deletion being confirmed, and its step. */
  deleting?: { id: string; step: 1 | 2 } | null;
  onInspect?(it: CleanupItem): void;
  onDeleteStep?(it: CleanupItem, step: 1 | 2 | null): void;
  onRemoveUnchecked?(it: CleanupItem, anyway: boolean): void;
  days: string;
  onDays(v: string): void;
  onSaveDays(): void;
  onMode(m: CleanupMode): void;
  onRefresh(): void;
  onCollect(): void;
  onRemove(it: CleanupItem, confirmed: boolean): void;
  onCancel(): void;
}) {
  const { rep } = p;
  const mode = cleanupModeOf(rep?.settings?.mode);
  const s = rep?.summary;
  const items = rep?.items || [];
  return (
    <>
      <h3 className="set-section-title">Disk cleanup</h3>
      <p className="set-hint">
        Agents leave worktrees behind. A worktree is removed only when no session, route or run uses it, nothing in it
        is uncommitted, every commit is in the base branch or on a remote (squash merges count), and nobody has used it
        for {rep?.settings?.after_days || 14} days. Its branch goes too only when it is merged. Everything else stays,
        with the reason.
      </p>
      <div className="set-row" id="cleanup-mode-row">
        <span className="set-label" id="cleanup-mode-label">
          Clean up
        </span>
        <div className="upd-mode-choices" role="radiogroup" aria-labelledby="cleanup-mode-label">
          {CLEANUP_MODES.map((m) => (
            <label className="upd-mode-choice" key={m.value} title={m.hint}>
              <input
                type="radio"
                name="cleanup-mode"
                value={m.value}
                checked={mode === m.value}
                onChange={() => p.onMode(m.value)}
              />
              <span>{m.label}</span>
            </label>
          ))}
        </div>
        <span className="set-hint">{CLEANUP_MODES.find((m) => m.value === mode)?.hint}</span>
      </div>
      <div className="set-row">
        <label className="set-label" htmlFor="cleanup-days">
          Unused for
        </label>
        <span className="cleanup-days">
          <input
            id="cleanup-days"
            type="number"
            min={1}
            max={3650}
            value={p.days}
            onChange={(e) => p.onDays(e.currentTarget.value)}
            onBlur={p.onSaveDays}
          />{" "}
          days before a worktree counts as safe
        </span>
      </div>

      <div className="set-row cleanup-head">
        <span className="cleanup-summary" id="cleanup-summary">
          {rep?.items == null ? "Looking at your disk…" : summaryLine(s)}
          {scanNote(rep)}
        </span>
        <span className="test-row">
          <button
            type="button"
            className="test-btn"
            id="cleanup-collect"
            disabled={!s?.safe_count || !!p.busy}
            onClick={p.onCollect}
          >
            {p.busy === "collect"
              ? "Removing…"
              : s?.safe_count
                ? "Remove " + s.safe_count + " safe (frees " + humanBytes(s.safe_bytes) + ")"
                : "Nothing safe to remove"}
          </button>
          <button type="button" className="test-btn" disabled={!!rep?.scanning} onClick={p.onRefresh}>
            {rep?.scanning ? "Checking…" : "Check again"}
          </button>
        </span>
      </div>
      {rep?.error && <p className="set-hint cleanup-error">{rep.error}</p>}

      {groupItems(items).map((g) => (
        <details
          className="cleanup-group"
          key={g.key}
          open={g.key === "safe" || g.key === "kept" || g.key === "unchecked"}
        >
          <summary>
            {g.title} <span className="cleanup-count">{g.items.length}</span>
            <span className="cleanup-size">{groupFreesText(g.items)}</span>
          </summary>
          <ul className="cleanup-list">
            {g.items.map((it) => (
              <li key={it.id} className={"cleanup-item cleanup-" + it.state}>
                <div className="cleanup-line">
                  <span className="cleanup-name" title={it.path}>
                    {it.name}
                  </span>
                  <span className="cleanup-badge">{STATE_LABEL[it.state] || it.state}</span>
                  <span className="cleanup-meta">
                    {sizeText(it)}
                    {it.last_used ? " · " + usedAgo(it.last_used) : ""}
                  </span>
                  {isUnchecked(it) && !it.in_use && (
                    <button
                      type="button"
                      className="test-btn cleanup-inspect"
                      disabled={!!p.busy || p.looks?.[it.id] === "loading"}
                      onClick={() => p.onInspect?.(it)}
                    >
                      {p.busy === it.id
                        ? "Removing…"
                        : p.looks?.[it.id] === "loading"
                          ? "Looking…"
                          : p.looks?.[it.id]
                            ? "Hide"
                            : "Look inside"}
                    </button>
                  )}
                  {canRemove(it) && (
                    <button
                      type="button"
                      className="test-btn cleanup-remove"
                      disabled={!!p.busy}
                      onClick={() => p.onRemove(it, false)}
                    >
                      {p.busy === it.id ? "Removing…" : "Remove"}
                    </button>
                  )}
                </div>
                <div className="cleanup-why">
                  {it.branch ? <code>{it.branch}</code> : null} {whyLine(it)}
                </div>
                {isUnchecked(it) && p.looks?.[it.id] && p.looks[it.id] !== "loading" && (
                  <UncheckedLook
                    it={it}
                    f={p.looks[it.id] as InspectFacts}
                    busy={!!p.busy}
                    step={p.deleting?.id === it.id ? p.deleting.step : null}
                    onStep={(step) => p.onDeleteStep?.(it, step)}
                    onRemove={(anyway) => p.onRemoveUnchecked?.(it, anyway)}
                  />
                )}
                {p.asking?.id === it.id && (
                  <InlineConfirm
                    id="cleanup-confirm"
                    title={"Remove " + it.name + "?"}
                    body={confirmText(it, p.rep?.kept_dir)}
                    confirmLabel="Remove anyway"
                    onConfirm={() => p.onRemove(it, true)}
                    onCancel={p.onCancel}
                  />
                )}
              </li>
            ))}
          </ul>
        </details>
      ))}
      <p className="set-hint">
        Also from a terminal: <code>mindflock clean</code> lists the same, <code>mindflock clean --collect</code>{" "}
        removes the safe set, <code>mindflock clean --inspect ITEM</code> looks inside a folder that can't be checked.
      </p>
    </>
  );
}

/** A look inside a folder that can't be checked: the server's sentences,
 * the top-level entries, the files that look like work, how it compares
 * with its branch, and the way out — Remove when nothing could be lost,
 * else "Remove anyway…" and two in-page confirm steps. */
function UncheckedLook(p: {
  it: CleanupItem;
  f: InspectFacts;
  busy: boolean;
  step: 1 | 2 | null;
  onStep(step: 1 | 2 | null): void;
  onRemove(anyway: boolean): void;
}) {
  const { it, f } = p;
  const cmp = f.git.compare;
  const ask = p.step ? uncheckedConfirm(it, f, p.step) : null;
  return (
    <div className="cleanup-look" id={"cleanup-look-" + it.id}>
      <ul className="cleanup-look-lines">
        {f.summary.map((ln, i) => (
          <li key={i}>{ln}</li>
        ))}
      </ul>
      {f.top.length > 0 && (
        <div className="cleanup-look-block">
          <span className="cleanup-look-head">Top level</span>
          <ul className="cleanup-look-entries">
            {f.top.map((t) => (
              <li key={t.name} className={"cleanup-entry-" + t.kind}>
                <code>{t.name + (t.dir ? "/" : "")}</code> {t.files} {t.files === 1 ? "file" : "files"},{" "}
                {humanBytes(t.bytes)}
                {t.kind === "dependency" ? " · dependencies/caches" : t.kind === "git" ? " · git's" : ""}
              </li>
            ))}
            {f.top_more > 0 && <li>… and {f.top_more} more</li>}
          </ul>
        </div>
      )}
      {f.work_sample.length > 0 && (
        <div className="cleanup-look-block">
          <span className="cleanup-look-head">Files that look like work, newest first</span>
          <ul className="cleanup-look-entries">
            {f.work_sample.map((w) => (
              <li key={w.path}>
                <code>{w.path}</code> {humanBytes(w.bytes)} · {new Date(w.mtime * 1000).toLocaleString()}
              </li>
            ))}
          </ul>
        </div>
      )}
      {cmp && (cmp.changed.length > 0 || cmp.extra.length > 0 || cmp.missing.length > 0) && (
        <div className="cleanup-look-block">
          <span className="cleanup-look-head">Against branch {f.git.branch}</span>
          <ul className="cleanup-look-entries">
            {cmp.changed.length > 0 && <li>Changed: {cmp.changed.join(", ")}</li>}
            {cmp.extra.length > 0 && <li>Only here: {cmp.extra.join(", ")}</li>}
            {cmp.missing.length > 0 && <li>Missing here: {cmp.missing.join(", ")}</li>}
          </ul>
        </div>
      )}
      {!ask && (
        <span className="test-row">
          {f.safe ? (
            <button type="button" className="test-btn cleanup-remove" disabled={p.busy} onClick={() => p.onRemove(false)}>
              Remove
            </button>
          ) : f.mounts.length ? null : (
            <button type="button" className="test-btn cleanup-remove-anyway" disabled={p.busy} onClick={() => p.onStep(1)}>
              Remove anyway…
            </button>
          )}
        </span>
      )}
      {ask && (
        <InlineConfirm
          id="cleanup-unchecked-confirm"
          title={ask.title}
          body={ask.body}
          confirmLabel={ask.confirmLabel}
          cancelLabel="Keep it"
          busy={p.busy}
          onConfirm={() => (p.step === 1 ? p.onStep(2) : p.onRemove(true))}
          onCancel={() => p.onStep(null)}
        />
      )}
    </div>
  );
}
