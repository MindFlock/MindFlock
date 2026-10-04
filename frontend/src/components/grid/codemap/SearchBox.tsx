/** Find file or symbol — the toolbar's search (GET /code-map/search). Typing
 * lists matches; ↑/↓ move, Enter flies the tree there (a folder to its branch,
 * a file — or the file holding a symbol — to its leaf, with its card open),
 * Escape clears. `/` anywhere in the tab focuses it (CodeMapTab). With a paint
 * tool armed, a pick paints the rule on it instead. */

import { forwardRef, useEffect, useMemo, useState } from "react";
import type { SearchItem } from "../../../api/types";
import { searchCode } from "../../../lib/codemapApi";
import { searchController } from "../../../lib/codemapSearch";
import { KIND_GLYPH } from "../../../lib/codemap";

/** A symbol's kind as a one-letter glyph (C class, ƒ function, …). */
function Glyph({ kind }: { kind: string }) {
  return (
    <i className={"cm-g k-" + kind} aria-hidden="true">
      {KIND_GLYPH[kind] || "?"}
    </i>
  );
}

export const SearchBox = forwardRef<HTMLInputElement, { title: string; onPick: (it: SearchItem) => void }>(
  function SearchBox({ title, onPick }, ref) {
    const [q, setQ] = useState("");
    const [items, setItems] = useState<SearchItem[]>([]);
    const [err, setErr] = useState("");
    const [open, setOpen] = useState(false);
    const [cur, setCur] = useState(0);

    // One controller per session: it debounces, and drops any answer that a
    // newer query — clearing the box included — has made stale.
    const ctl = useMemo(
      () =>
        searchController<SearchItem>(
          (t) => searchCode(title, t),
          (r) => {
            setItems(r.items);
            setErr(r.err);
            setCur(0);
          }
        ),
      [title]
    );
    useEffect(() => () => ctl.dispose(), [ctl]);
    useEffect(() => ctl.set(q), [q, ctl]);

    const pick = (it: SearchItem | undefined) => {
      if (!it) return;
      onPick(it);
      setOpen(false);
      setQ("");
    };

    return (
      <span className="cm-search">
        <input
          ref={ref}
          type="search"
          placeholder="Find file or symbol"
          aria-label="Find file or symbol"
          aria-expanded={open && items.length > 0}
          aria-controls="cm-search-list"
          role="combobox"
          spellCheck={false}
          autoComplete="off"
          value={q}
          onFocus={() => setOpen(true)}
          onBlur={() => setTimeout(() => setOpen(false), 150)}
          onChange={(e) => {
            setQ(e.target.value);
            setOpen(true);
          }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setCur((c) => Math.min(items.length - 1, c + 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setCur((c) => Math.max(0, c - 1));
            } else if (e.key === "Enter") {
              e.preventDefault();
              pick(items[cur]);
            } else if (e.key === "Escape") {
              e.preventDefault();
              e.stopPropagation();
              setQ("");
              (e.target as HTMLInputElement).blur();
            }
          }}
        />
        <kbd aria-hidden="true">/</kbd>
        {open && q.trim() && (
          <ul className="cm-search-list" id="cm-search-list" role="listbox">
            {err && <li className="cm-err">{err}</li>}
            {!err && !items.length && <li className="muted">No matches.</li>}
            {items.map((it, i) => (
              <li
                key={it.path + ":" + it.name + ":" + it.line}
                role="option"
                aria-selected={i === cur}
                className={i === cur ? "cur" : ""}
                onMouseDown={(e) => {
                  e.preventDefault();
                  pick(it);
                }}
                onMouseEnter={() => setCur(i)}
              >
                {it.kind === "file" || it.kind === "dir" ? (
                  <span className="cm-search-k">{it.kind === "dir" ? "dir" : "file"}</span>
                ) : (
                  <Glyph kind={it.kind} />
                )}
                <span className="cm-search-n">{it.kind === "file" || it.kind === "dir" ? it.path : it.name}</span>
                {it.kind !== "file" && it.kind !== "dir" && (
                  <span className="cm-search-p">
                    {it.path}
                    {it.line ? ":" + it.line : ""}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </span>
    );
  }
);
