/** Settings → Disk cleanup: the worktree garbage collector (backend
 * core/worktree_gc_driver.py; docs/web-ui.md, *Disk cleanup*).
 *
 * What exists, how much space it takes, what would be removed and why the
 * rest is kept; one button collects the provably safe set, and each row has
 * its own Remove — which, for anything holding work, asks first in the app's
 * own InlineConfirm (the desktop app has no window.confirm) and says where
 * the work was saved. The mode (Automatic · Ask me · Off) and the age line
 * save through POST /api/cleanup/settings. */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../../api/client";
import { toast } from "../../../lib/toast";
import { InlineConfirm } from "../../InlineConfirm";
import type { ScreenProps } from "../SettingsDialog";
import {
  CLEANUP_MODES,
  canRemove,
  failSentence,
  cleanupModeOf,
  confirmText,
  groupItems,
  humanBytes,
  needsConfirm,
  removedText,
  STATE_LABEL,
  summaryLine,
  usedAgo,
  whyLine,
  type CleanupItem,
  type CleanupMode,
  type CleanupReport,
  type RemoveResult,
} from "../../../lib/cleanup";
import "./cleanup.css";

export function Cleanup({ active }: ScreenProps) {
  const [rep, setRep] = useState<CleanupReport | null>(null);
  const [busy, setBusy] = useState("");
  const [asking, setAsking] = useState<CleanupItem | null>(null);
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

  return (
    <CleanupView
      rep={rep}
      busy={busy}
      asking={asking}
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
  );
}

/** The screen's markup from plain props (the vitest suite renders it). */
export function CleanupView(p: {
  rep: CleanupReport | null;
  busy: string;
  asking: CleanupItem | null;
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
          {rep?.scanning && rep?.items != null ? " Checking again…" : ""}
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
                ? "Remove " + s.safe_count + " safe (" + humanBytes(s.safe_bytes) + ")"
                : "Nothing safe to remove"}
          </button>
          <button type="button" className="test-btn" disabled={!!rep?.scanning} onClick={p.onRefresh}>
            {rep?.scanning ? "Checking…" : "Check again"}
          </button>
        </span>
      </div>
      {rep?.error && <p className="set-hint cleanup-error">{rep.error}</p>}

      {groupItems(items).map((g) => (
        <details className="cleanup-group" key={g.key} open={g.key === "safe" || g.key === "kept"}>
          <summary>
            {g.title} <span className="cleanup-count">{g.items.length}</span>
            <span className="cleanup-size">{humanBytes(g.items.reduce((a, i) => a + (i.size_bytes || 0), 0))}</span>
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
                    {humanBytes(it.size_bytes)}
                    {it.last_used ? " · " + usedAgo(it.last_used) : ""}
                  </span>
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
        removes the safe set.
      </p>
    </>
  );
}
