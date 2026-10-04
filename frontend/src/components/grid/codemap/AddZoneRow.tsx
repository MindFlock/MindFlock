/** The add-zone row under the Map toolbar: kind (⛔ Keep out | ✓ Only here),
 * pattern, name, scope, "Tell the agent", Add — with a live preview of what
 * the pattern covers. Green is worktree-scope only (a green zone scopes a
 * TASK; as repo policy it would lock every other session out of its own
 * files), so choosing it pins the scope and says how many sessions share the
 * worktree. After a green add, files already changed outside the new scope
 * are exempt by default, with a one-click "Treat as breaches".
 *
 * Inline DOM only — window.prompt/confirm are dead in the Electron app. */

import type { RefObject } from "react";
import type { RedZonePreview } from "../../../api/types";

export interface AddState {
  kind: "red" | "green";
  pattern: string;
  name: string;
  scope: "repo" | "worktree";
  tell: boolean;
  busy: boolean;
  err: string;
  /** After a successful add. */
  done: {
    kind: "red" | "green";
    pattern: string;
    already: string[];
    told: string;
    exempt: string[];
    committedOutside: number;
    exemptState: "" | "busy" | "kept" | "breaches" | string;
  } | null;
}

export function newAdd(kind: "red" | "green", pattern: string, name = ""): AddState {
  return { kind, pattern, name, scope: kind === "green" ? "worktree" : "repo", tell: kind === "green", busy: false, err: "", done: null };
}

/** "N files already changed outside this scope — [Keep exempt] [Treat as
 * breaches]": shown after ANY green add or widen (+ Zone, and "Go — only the
 * planned files"), per SPEC3 §B "Exemptions". */
export function ExemptPrompt(p: {
  paths: string[];
  committed?: number;
  state: "" | "busy" | "kept" | "breaches" | string;
  onKeep: () => void;
  onTreatAsBreaches: () => void;
}) {
  const n = p.paths.length;
  if (n === 0 || p.state === "kept") return null;
  return (
    <>
      {" "}
      <span className="cm-warn-text" title={p.paths.join("\n")}>
        {n} file{n === 1 ? "" : "s"} already changed outside this scope
        {p.committed ? ` (${p.committed} committed)` : ""} —
      </span>{" "}
      {p.state === "breaches" ? (
        <span className="muted">treated as breaches.</span>
      ) : (
        <>
          <button type="button" className="cm-linkbtn" disabled={p.state === "busy"} onClick={p.onKeep}>
            Keep exempt
          </button>{" "}
          <button type="button" className="cm-linkbtn warn" disabled={p.state === "busy"} onClick={p.onTreatAsBreaches}>
            Treat as breaches
          </button>
        </>
      )}
    </>
  );
}

export interface AddRowProps {
  add: AddState;
  setAdd: (a: AddState | null) => void;
  inputRef: RefObject<HTMLInputElement | null>;
  preview: RedZonePreview | null;
  previewErr: string;
  clarify: boolean;
  repoLabel: string;
  sessionsHere: number;
  quick: Array<{ label: string; pattern: string; title: string }>;
  onSubmit: (tellOverride?: boolean) => void;
  onTreatAsBreaches: () => void;
}

export function AddZoneRow(p: AddRowProps) {
  const { add, setAdd, preview } = p;
  const green = add.kind === "green";
  const changedOutside = preview
    ? Array.isArray(preview.changed_outside)
      ? preview.changed_outside.length
      : Number(preview.changed_outside) || 0
    : 0;
  const status = (() => {
    if (add.err) return <span className="cm-err">{add.err}</span>;
    if (add.done) {
      const d = add.done;
      if (d.kind === "green") {
        return (
          <span>
            <b>{d.pattern}</b> is now a green zone — agents may only change files inside the green zones.
            {d.told ? " " + d.told : ""}
            <ExemptPrompt
              paths={d.exempt}
              committed={d.committedOutside}
              state={d.exemptState}
              onKeep={() => setAdd({ ...add, done: { ...d, exemptState: "kept" } })}
              onTreatAsBreaches={p.onTreatAsBreaches}
            />
          </span>
        );
      }
      return (
        <span>
          <b>{d.pattern}</b> is now a keep-out zone.
          {d.told ? " " + d.told : ""}
          {d.already.length > 0 && (
            <>
              {" "}
              <span className="cm-warn-text">
                {d.already.length} file{d.already.length === 1 ? " here is" : "s here are"} already changed —
              </span>{" "}
              <button
                type="button"
                className="cm-linkbtn"
                disabled={p.clarify || add.busy}
                title={p.clarify ? "Answer the prompt in the terminal first" : d.already.join("\n")}
                onClick={() => p.onSubmit(true)}
              >
                ask the agent to revert?
              </button>
            </>
          )}
        </span>
      );
    }
    if (p.previewErr) return <span className="cm-err">{p.previewErr}</span>;
    if (!preview || !add.pattern.trim()) return null;
    if (green) {
      const w = preview.writable_files ?? preview.count;
      return (
        <span className="muted">
          {w} file{w === 1 ? "" : "s"} writable
          {preview.roots && preview.roots.length ? " under " + preview.roots.slice(0, 3).join(", ") + (preview.roots.length > 3 ? "…" : "") : ""}
          {changedOutside ? ` · ${changedOutside} already changed outside` : ""}
          {preview.committed_outside ? ` (${preview.committed_outside} committed)` : ""}
          {w === 0 ? " — nothing exists here yet; the agent may only create new files under it" : ""}
          {preview.unanchored && (
            <>
              {" · "}
              <span className="cm-warn-text">matches at any depth</span>
              {preview.anchored && (
                <>
                  {" "}
                  <button type="button" className="cm-linkbtn" onClick={() => setAdd({ ...add, pattern: preview.anchored!, done: null })}>
                    anchor to {preview.anchored}
                  </button>
                </>
              )}
            </>
          )}
          {(preview.warnings || []).map((w2) => (
            <span key={w2} className="cm-warn-text">
              {" · " + w2}
            </span>
          ))}
        </span>
      );
    }
    return (
      <span className="muted">
        matches {preview.count} file{preview.count === 1 ? "" : "s"}
        {preview.ignored_count ? `, ${preview.ignored_count} git-ignored` : ""}
        {preview.changed && preview.changed.length ? ` · ${preview.changed.length} already changed` : ""}
        {preview.count === 0 ? " — kept anyway, it guards files created later" : ""}
      </span>
    );
  })();

  return (
    <form
      className={"cm-add" + (green ? " green" : "")}
      onSubmit={(ev) => {
        ev.preventDefault();
        p.onSubmit();
      }}
    >
      <span className="cm-seg cm-kind" role="radiogroup" aria-label="Zone kind">
        <button
          type="button"
          role="radio"
          aria-checked={!green}
          className={!green ? "on red" : ""}
          title="Keep out: agents may read these paths but not change them"
          onClick={() => setAdd({ ...add, kind: "red", scope: add.kind === "green" ? "repo" : add.scope, done: null })}
        >
          ⛔ Keep out
        </button>
        <button
          type="button"
          role="radio"
          aria-checked={green}
          className={green ? "on green" : ""}
          title="Only here: agents may change ONLY these paths (and other green zones); everything else is read-only"
          onClick={() => setAdd({ ...add, kind: "green", scope: "worktree", tell: true, done: null })}
        >
          ✓ Only here
        </button>
      </span>
      <input
        ref={p.inputRef}
        className="cm-add-pattern"
        type="text"
        spellCheck={false}
        autoComplete="off"
        placeholder={green ? "the scope — backend/providers, /src/api" : "path, folder or glob — config.toml, backend/athena, *.pem"}
        aria-label="Pattern"
        value={add.pattern}
        onChange={(ev) => setAdd({ ...add, pattern: ev.target.value, err: "", done: null })}
        onKeyDown={(ev) => {
          if (ev.key === "Escape") {
            ev.preventDefault();
            setAdd(null);
          }
        }}
      />
      {p.quick.length > 0 && (
        <span className="cm-add-quick">
          {p.quick.map((q) => (
            <button
              type="button"
              key={q.label}
              className={"cm-chip" + (add.pattern === q.pattern ? " on" : "")}
              title={q.title}
              onClick={() => setAdd({ ...add, pattern: q.pattern, done: null })}
            >
              {q.label}
            </button>
          ))}
        </span>
      )}
      <input
        className="cm-add-name"
        type="text"
        spellCheck={false}
        autoComplete="off"
        placeholder="name (optional)"
        aria-label="Zone name"
        value={add.name}
        onChange={(ev) => setAdd({ ...add, name: ev.target.value })}
      />
      {green ? (
        <span
          className="cm-add-scope-note"
          title="A green zone scopes a task, so it is set per worktree — and every session in the worktree shares it."
        >
          {p.sessionsHere <= 1
            ? "This worktree (1 session)"
            : `This worktree — applies to all ${p.sessionsHere} sessions in it`}
        </span>
      ) : (
        <select
          className="cm-add-scope"
          aria-label="Scope"
          value={add.scope}
          onChange={(ev) => setAdd({ ...add, scope: ev.target.value as "repo" | "worktree" })}
        >
          <option value="repo">Whole repo{p.repoLabel ? ` (${p.repoLabel})` : ""}</option>
          <option value="worktree">This worktree</option>
        </select>
      )}
      <label
        className={"cm-add-tell" + (p.clarify ? " disabled" : "")}
        title={p.clarify ? "Answer the prompt in the terminal first" : "Also tell the agent right away"}
      >
        <input
          type="checkbox"
          checked={add.tell && !p.clarify}
          disabled={p.clarify}
          onChange={(ev) => setAdd({ ...add, tell: ev.target.checked })}
        />
        Tell the agent
      </label>
      <button type="submit" className="cm-add-go" disabled={add.busy || !add.pattern.trim()}>
        {add.busy ? "Adding…" : "Add"}
      </button>
      <span className="cm-add-status">{status}</span>
    </form>
  );
}
