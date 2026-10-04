/** Zones dialog — repo-level management outside any one session.
 *
 * A keep-out (red) zone is a path glob the agent may read but not create,
 * modify, delete or move. Repo zones live in ~/.mindflock/red_zones.json keyed
 * by the repo's identity (its normalized origin URL), so they follow the repo
 * into every worktree and every future session; the per-session Map adds
 * worktree-only zones, "allow here" waivers and only-here (green) zones on
 * top — green scopes a TASK, so it is never repo-wide and is set from the Map.
 * This dialog is where you set keep-out zones up before any session exists,
 * where the per-repo "Plan first" switch lives, and where a repo's derived
 * outputs ("companions": files a green-scoped agent may still write, like a
 * committed build bundle) are declared.
 *
 * Repos appear here once they have a zone or a flag, and also for every live
 * session (their identity is looked up per session), so a repo you are working
 * in is always one click from its first zone. Inline rows only — no
 * window.prompt, which is dead in the Electron app. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, instApi } from "../../api/client";
import type { RedZone } from "../../api/types";
import { fetchCompanions, saveCompanions, type CompanionsDoc } from "../../lib/codemapApi";
import { refreshInstances } from "../../state/queries";
import { displayName, useUi } from "../../state/store";
import { errMsg } from "../../lib/format";
import { instances } from "../../lib/sessionActions";

interface RepoDoc {
  label: string;
  zones: RedZone[];
  plan_first: boolean;
}

interface RepoRow extends RepoDoc {
  id: string;
  sessions: string[];
}

interface SessionZones {
  repo: { id: string; label: string } | null;
  plan_first: boolean;
  zones: RedZone[];
}

/** How many sessions we ask for their repo identity when the dialog opens —
 * a bound on fan-out, not a feature limit. */
const SESSION_LOOKUP_CAP = 24;

export function RedZonesDialog() {
  const open = useUi((s) => s.openDialog === "red-zones");
  const closeDialog = useUi((s) => s.closeDialog);
  const [repos, setRepos] = useState<Record<string, RepoDoc>>({});
  const [sessionRepos, setSessionRepos] = useState<Record<string, { label: string; sessions: string[] }>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const seq = useRef(0);

  const load = useCallback(async () => {
    const my = ++seq.current;
    setLoading(true);
    setError("");
    const live = instances()
      .filter((i) => i.started !== false && !i.pending && !i.workspace_missing && i.status !== "loading" && !i.device)
      .slice(0, SESSION_LOOKUP_CAP);
    const [doc, perSession] = await Promise.all([
      api<{ repos: Record<string, RepoDoc> }>("/api/red-zones").catch((x) => {
        if (my === seq.current) setError(errMsg(x));
        return null;
      }),
      Promise.allSettled(live.map((i) => instApi<SessionZones>(i.title, "/red-zones").then((r) => [i.title, r] as const))),
    ]);
    if (my !== seq.current) return;
    if (doc) setRepos(doc.repos || {});
    const sr: Record<string, { label: string; sessions: string[] }> = {};
    for (const p of perSession) {
      if (p.status !== "fulfilled") continue;
      const [title, r] = p.value;
      if (!r || !r.repo || !r.repo.id) continue;
      const cur = sr[r.repo.id] || { label: r.repo.label, sessions: [] };
      cur.sessions.push(title);
      sr[r.repo.id] = cur;
    }
    setSessionRepos(sr);
    setLoading(false);
  }, []);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  // Escape closes from anywhere while open. Opened from the palette or a
  // toast, focus sits on <body>, so a keydown handler on the dialog itself
  // would never hear it (TodoDialog does the same).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeDialog();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, closeDialog]);

  // Move focus into the dialog (the first repo's pattern input, else the
  // panel) so keys land here and not on the page behind it.
  const panelRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const t = setTimeout(() => {
      const panel = panelRef.current;
      const ae = document.activeElement;
      if (!panel || (ae && ae !== panel && panel.contains(ae))) return;
      const first = panel.querySelector<HTMLInputElement>(".rzd-add-pattern");
      (first || panel).focus({ preventScroll: true });
    }, 0);
    return () => clearTimeout(t);
  }, [open, loading]);

  const rows = useMemo<RepoRow[]>(() => {
    const ids = new Set([...Object.keys(repos), ...Object.keys(sessionRepos)]);
    const out: RepoRow[] = [];
    for (const id of ids) {
      const d = repos[id];
      const s = sessionRepos[id];
      out.push({
        id,
        label: d?.label || s?.label || id,
        zones: d?.zones || [],
        plan_first: !!d?.plan_first,
        sessions: s?.sessions || [],
      });
    }
    // Repos with zones first, then repos you're working in, then by name.
    out.sort(
      (a, b) =>
        Number(b.zones.length > 0) - Number(a.zones.length > 0) ||
        Number(b.sessions.length > 0) - Number(a.sessions.length > 0) ||
        a.label.localeCompare(b.label)
    );
    return out;
  }, [repos, sessionRepos]);

  if (!open) return null;

  const zoneCount = rows.reduce((n, r) => n + r.zones.length, 0);

  return (
    <div
      id="red-zones-dialog"
      className="modal"
      onClick={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
    >
      <div
        id="red-zones-panel"
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby="rzd-title"
      >
        <div className="ws-head">
          <h2 id="rzd-title">Zones</h2>
          <span className="muted rzd-total">
            {zoneCount} zone{zoneCount === 1 ? "" : "s"} across {rows.filter((r) => r.zones.length).length} repo
            {rows.filter((r) => r.zones.length).length === 1 ? "" : "s"}
          </span>
          <button type="button" onClick={() => void load()} disabled={loading}>
            {loading ? "Loading…" : "Refresh"}
          </button>
          <button type="button" onClick={closeDialog}>
            Close
          </button>
        </div>
        <p className="rzd-intro muted">
          <b>⛔ Keep out</b> — paths the agent may read but not change. These apply to every session on the repo: Claude
          Code is stopped before the edit; other agents are detected, flagged and blocked at push. <b>✓ Only here</b>{" "}
          (scope a task to some paths), worktree-only zones and “allow here” are set from a session's <b>Map</b> tab.
        </p>
        {error && <p className="error">{error}</p>}
        <div className="rzd-list">
          {!rows.length && !loading && (
            <p className="muted rzd-empty">
              No repos yet. Open a session on a git repo and it shows up here — or add a zone from its Map tab.
            </p>
          )}
          {rows.map((r) => (
            <RepoCard
              key={r.id}
              row={r}
              onRepos={(next) => {
                setRepos(next);
                refreshInstances();
              }}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function RepoCard({ row, onRepos }: { row: RepoRow; onRepos: (r: Record<string, RepoDoc>) => void }) {
  const [pattern, setPattern] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const aliases = useUi((s) => s.aliases);

  const call = async (what: string, fn: () => Promise<{ repos?: Record<string, RepoDoc> }>) => {
    setBusy(what);
    setErr("");
    try {
      const r = await fn();
      if (r && r.repos) onRepos(r.repos);
      return true;
    } catch (x) {
      setErr(errMsg(x));
      return false;
    } finally {
      setBusy("");
    }
  };

  const add = async () => {
    const p = pattern.trim();
    if (!p) return;
    const ok = await call("add", () =>
      api("/api/red-zones", { json: { repo_id: row.id, pattern: p, name: name.trim(), note: "", label: row.label } })
    );
    if (ok) {
      setPattern("");
      setName("");
    }
  };

  return (
    <section className="rzd-repo">
      <div className="rzd-repo-head">
        <span className="rzd-repo-label" title={row.id}>
          {row.label}
        </span>
        {row.sessions.length > 0 && (
          <span className="rzd-sessions" title={row.sessions.map((t) => aliases[t] || displayName(t)).join("\n")}>
            {row.sessions.length} session{row.sessions.length === 1 ? "" : "s"}
          </span>
        )}
        <label
          className="rzd-planfirst"
          title={
            "Plan first: new sessions on this repo started from a ticket, issue or PR review are asked to list every " +
            "file they intend to touch (with intent) and wait for your go-ahead — the Map shows the plan and its blast " +
            "radius so you can red-zone arms before any edit."
          }
        >
          <input
            type="checkbox"
            checked={row.plan_first}
            disabled={busy === "plan"}
            onChange={(e) =>
              void call("plan", () =>
                api("/api/red-zones/plan-first", { json: { repo_id: row.id, on: e.target.checked, label: row.label } })
              )
            }
          />
          Plan first
        </label>
      </div>
      {row.zones.length > 0 ? (
        <ul className="rzd-zones">
          {row.zones.map((z) => (
            <li key={z.id}>
              <span className={"rzd-kind " + (z.kind === "green" ? "green" : "red")} title={z.kind === "green" ? "Only here" : "Keep out"}>
                {z.kind === "green" ? "✓" : "⛔"}
              </span>
              <span className="rzd-zone-main">
                <span className="rzd-zone-pattern">{z.pattern}</span>
                <span className="rzd-zone-name">{z.kind === "green" ? "only here" : "keep out"}</span>
                {z.name && <span className="rzd-zone-name">{z.name}</span>}
                {z.note && <span className="rzd-zone-note muted">{z.note}</span>}
              </span>
              <button
                type="button"
                className="rzd-x"
                aria-label={"Remove red zone " + (z.name || z.pattern)}
                title="Remove this red zone"
                disabled={busy === "del:" + z.id}
                onClick={() =>
                  void call("del:" + z.id, () => api("/api/red-zones/" + encodeURIComponent(z.id), { method: "DELETE" }))
                }
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted rzd-none">No zones.</p>
      )}
      <form
        className="rzd-add"
        onSubmit={(e) => {
          e.preventDefault();
          void add();
        }}
      >
        <input
          type="text"
          className="rzd-add-pattern"
          placeholder="config.toml, backend/athena, *.pem"
          aria-label={"New red zone for " + row.label}
          spellCheck={false}
          autoComplete="off"
          value={pattern}
          onChange={(e) => {
            setPattern(e.target.value);
            setErr("");
          }}
        />
        <input
          type="text"
          className="rzd-add-name"
          placeholder="name (optional)"
          aria-label="Zone name"
          spellCheck={false}
          autoComplete="off"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <button type="submit" disabled={!pattern.trim() || busy === "add"}>
          {busy === "add" ? "Adding…" : "Add keep-out zone"}
        </button>
      </form>
      {err && <p className="error rzd-err">{err}</p>}
      <Companions repoId={row.id} label={row.label} />
    </section>
  );
}

/** A repo's derived outputs: files outside a green zone the agent may still
 * write (they are generated from the zone — a committed bundle, a schema dump).
 * Lockfiles, snapshots and tests importing the zone are built in and shown,
 * not editable. Loaded when opened: most repos never need it. */
function Companions({ repoId, label }: { repoId: string; label: string }) {
  const [open, setOpen] = useState(false);
  const [doc, setDoc] = useState<CompanionsDoc | null>(null);
  const [val, setVal] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!open || doc) return;
    let dead = false;
    fetchCompanions(repoId)
      .then((d) => {
        if (!dead) setDoc(d);
      })
      .catch((x) => {
        if (!dead) setErr(errMsg(x));
      });
    return () => {
      dead = true;
    };
  }, [open, doc, repoId]);

  const save = async (next: string[]) => {
    setBusy(true);
    setErr("");
    try {
      setDoc(await saveCompanions(repoId, next, label));
      return true;
    } catch (x) {
      setErr(errMsg(x));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const mine = doc?.companions || [];
  return (
    <details className="rzd-comp" open={open} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary>
        Derived outputs <span className="muted">— files a scoped (✓ only here) agent may still write</span>
      </summary>
      {!doc && !err && <p className="muted rzd-none">Loading…</p>}
      {doc && (
        <>
          {doc.defaults.length > 0 && (
            <div className="rzd-comp-row">
              <span className="muted rzd-comp-lab">Built in</span>
              {doc.defaults.map((c) => (
                <span key={c} className="rzd-comp-chip builtin">
                  {c}
                </span>
              ))}
            </div>
          )}
          <div className="rzd-comp-row">
            <span className="muted rzd-comp-lab">This repo</span>
            {mine.length === 0 && <span className="muted">none</span>}
            {mine.map((c) => (
              <span key={c} className="rzd-comp-chip">
                {c}
                <button
                  type="button"
                  className="rzd-x"
                  aria-label={"Remove derived output " + c}
                  disabled={busy}
                  onClick={() => void save(mine.filter((x) => x !== c))}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
          <form
            className="rzd-add"
            onSubmit={(e) => {
              e.preventDefault();
              const v = val.trim();
              if (!v || mine.includes(v)) return;
              void save(mine.concat([v])).then((ok) => ok && setVal(""));
            }}
          >
            <input
              type="text"
              className="rzd-comp-input"
              placeholder="backend/web/static/app.js, docs/api/*.json"
              aria-label="New derived output"
              spellCheck={false}
              autoComplete="off"
              value={val}
              onChange={(e) => setVal(e.target.value)}
            />
            <button type="submit" disabled={!val.trim() || busy}>
              Add
            </button>
          </form>
        </>
      )}
      {err && <p className="error rzd-err">{err}</p>}
    </details>
  );
}
