/** Where a stops-only chain runs — a popup with a search (the owner: "fly on
 * session should pop up the search for folder menu, not a weird tab on the
 * top"). A session is a place in the file tree, so the places are: the
 * DEFAULT session (MindFlock's own folder), the sessions without a flight,
 * and folders — recent ones (`/api/repos/suggest`), a name search
 * (`/api/repos/search`), or a typed path — where a new session is made.
 * ↑ ↓ pick, Enter runs, Esc closes. */
import { useEffect, useMemo, useState, type JSX, type KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { api } from "../../api/client";
import { windowName } from "../../lib/windowName";

export type Place = { kind: "default" } | { kind: "session"; title: string } | { kind: "folder"; path: string };

interface Folder {
  path: string;
  name: string;
}

interface Row {
  key: string;
  label: string;
  hint: string;
  place: Place;
  section: "Default" | "Sessions" | "Folders";
}

const homeShort = (p: string, home: string) => (home && p.startsWith(home) ? "~" + p.slice(home.length) : p);
const looksLikePath = (q: string) => /^(~|\/|\.{1,2}\/)/.test(q.trim());

export function placeRows(q: string, defaultFolder: string, sessions: readonly { title: string; folder: string }[], folders: readonly Folder[], home = ""): Row[] {
  const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
  const hit = (s: string) => terms.every((t) => s.toLowerCase().includes(t));
  const rows: Row[] = [];
  if (!terms.length || hit("default session " + defaultFolder)) rows.push({ key: "default", label: "Default session", hint: defaultFolder, place: { kind: "default" }, section: "Default" });
  for (const s of sessions) if (!terms.length || hit(s.title + " " + windowName(s.title) + " " + s.folder)) rows.push({ key: "s:" + s.title, label: windowName(s.title), hint: homeShort(s.folder, home), place: { kind: "session", title: s.title }, section: "Sessions" });
  const typed = q.trim();
  if (looksLikePath(typed)) rows.push({ key: "p:" + typed, label: "New session in " + typed, hint: "a typed folder", place: { kind: "folder", path: typed }, section: "Folders" });
  for (const f of folders) if (!terms.length || looksLikePath(typed) || hit(f.name + " " + f.path)) rows.push({ key: "f:" + f.path, label: "New session in " + f.name, hint: homeShort(f.path, home), place: { kind: "folder", path: f.path }, section: "Folders" });
  return rows;
}

export function PlacePicker(p: {
  name: string;
  sessions: readonly { title: string; folder: string }[];
  onPick(place: Place): void;
  onClose(): void;
}): JSX.Element {
  const [q, setQ] = useState("");
  const [at, setAt] = useState(0);
  const [defaultFolder, setDefaultFolder] = useState("~/.mindflock/default");
  const [recent, setRecent] = useState<Folder[]>([]);
  const [found, setFound] = useState<{ q: string; folders: Folder[]; home: string } | null>(null);
  useEffect(() => {
    void api<{ folder?: string }>("/api/flights/default-place").then((d) => d?.folder && setDefaultFolder(d.folder)).catch(() => {});
    void api<{ suggestions?: Folder[] }>("/api/repos/suggest").then((d) => setRecent(d?.suggestions || [])).catch(() => {});
  }, []);
  // A name search once two characters are typed (not for a typed path).
  useEffect(() => {
    const asked = q.trim();
    if (asked.length < 2 || looksLikePath(asked)) return setFound(null);
    let live = true;
    const t = window.setTimeout(() => {
      void api<{ matches?: Folder[]; home?: string }>("/api/repos/search?q=" + encodeURIComponent(asked))
        .then((d) => live && setFound({ q: asked, folders: d?.matches || [], home: d?.home || "" }))
        .catch(() => live && setFound(null));
    }, 180);
    return () => {
      live = false;
      window.clearTimeout(t);
    };
  }, [q]);
  // The name search's finds first, then the recent folders (the filter below
  // keeps only those that match) — one list, no duplicates.
  const folders = useMemo(() => {
    const out: Folder[] = [];
    const seen = new Set<string>();
    for (const f of [...(found && found.q === q.trim() ? found.folders : []), ...recent]) if (!seen.has(f.path)) seen.add(f.path) && out.push(f);
    return out;
  }, [found, recent, q]);
  const rows = useMemo(() => placeRows(q, defaultFolder, p.sessions, folders.slice(0, 30), found?.home || ""), [q, defaultFolder, p.sessions, folders, found]);
  const sel = Math.min(at, Math.max(0, rows.length - 1));
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setAt((i) => Math.min(rows.length - 1, i + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setAt((i) => Math.max(0, i - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (rows[sel]) p.onPick(rows[sel].place);
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      p.onClose();
    }
  };
  let last = "";
  return createPortal(
    <div className="sky-place-scrim" onPointerDown={(e) => e.target === e.currentTarget && p.onClose()}>
      <div className="sky-place" role="dialog" aria-label={"Where should “" + p.name + "” run?"}>
        <div className="sky-place-head">
          Run <b>{p.name}</b> in…
        </div>
        <input
          className="sky-quick-field"
          autoFocus
          placeholder="Search a session or a folder, or type a path"
          aria-label="Search places"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setAt(0);
          }}
          onKeyDown={onKey}
        />
        <div className="sky-place-list" role="listbox" aria-label="Places">
          {rows.map((r, i) => {
            const head = r.section !== last ? ((last = r.section), <div key={"h:" + r.section} className="sky-place-section">{r.section}</div>) : null;
            return [
              head,
              <button
                key={r.key}
                type="button"
                role="option"
                aria-selected={i === sel}
                className={"fl-stopmenu-item sky-place-row" + (i === sel ? " is-sel" : "")}
                onMouseEnter={() => setAt(i)}
                onClick={() => p.onPick(r.place)}
              >
                <span className="sky-quick-label">{r.label}</span>
                {r.hint && <span className="sky-quick-hint">{r.hint}</span>}
              </button>,
            ];
          })}
          {!rows.length && <div className="sky-quick-none">Nothing matches “{q}” — type a path (~/… or /…) for a new session there</div>}
        </div>
        <div className="sky-place-foot">
          <button type="button" className="linklike" onClick={p.onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
}
