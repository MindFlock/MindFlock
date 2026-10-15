import { useCallback, useEffect, useRef, useState } from "react";
import { deviceApi } from "../../../lib/devices";
import { focusRowIndex, leafName } from "../newSession";
import { seedChain } from "../../../lib/folderPicker";

/** Folder browser popover (port of loadBrowse, section 16), on Finder's rules:
 * a single click SELECTS a row and a double click opens it. It used to be the
 * other way round — the name navigated and a per-row "select" button was the
 * only way to choose anything — which meant the one gesture everybody tries
 * first did the one thing they didn't ask for. Selecting deliberately does NOT
 * close the popover: the user is mid-browse, and a picker that vanishes on the
 * first click is unusable for comparing two folders.
 *
 * Selecting writes into the Folder field itself rather than keeping a copy in
 * here, so the highlight, the git nudge and the suggestion chips all follow the
 * row that was clicked. That is what makes the dialog's snapshot load-bearing:
 * see folderReducer for how Escape hands the field back. */
export function FolderBrowser({
  device = "",
  initialPath,
  selected,
  onSelect,
  onPick,
  fallback = false,
  onListing,
}: {
  /** Whose disk is being browsed: "" here, else the tailnet device the
   * session will be started on. */
  device?: string;
  initialPath: string;
  /** The path the Folder field holds, so the highlight always matches the form
   * rather than a copy of the selection kept in here. */
  selected: string;
  /** Fill the field, stay open — undone if the browse is cancelled. */
  onSelect(path: string): void;
  /** Fill the field and close — the explicit "I'm done" gesture. */
  onPick(path: string): void;
  /** Open on the nearest folder that exists when `initialPath` does not (its
   * parents, then home) — the picker window, seeded from a field the user may
   * have typed a not-yet-made folder into. */
  fallback?: boolean;
  /** Every listing that lands: the folder now shown and its rows. */
  onListing?(path: string, entries: string[]): void;
}) {
  interface BrowsePayload {
    path: string;
    parent?: string | null;
    is_git?: boolean;
    entries?: Array<{ name: string; path: string; is_git?: boolean }>;
  }
  const [data, setData] = useState<BrowsePayload | null>(null);
  const [error, setError] = useState("");
  // The new-folder row: null when it is closed, the typed name when it is open.
  // A separate piece of state from the name itself so that "" is a legal thing
  // to have typed (an open row with an empty box) rather than a closed row.
  const [newFolder, setNewFolder] = useState<string | null>(null);
  const [making, setMaking] = useState(false);
  const listRef = useRef<HTMLDivElement | null>(null);
  const newFolderRef = useRef<HTMLInputElement | null>(null);
  // The folder a keyboard navigation is stepping out of, or null when the
  // pointer drove it (or nothing is pending) — read by the effect below.
  const leaving = useRef<string | null>(null);

  const load = useCallback(async (path: string, quiet = false): Promise<boolean> => {
    setError("");
    try {
      const q = path ? "?path=" + encodeURIComponent(path) : "";
      setData(await deviceApi(device)<BrowsePayload>("/api/browse" + q));
      return true;
    } catch (err) {
      if (quiet) return false;
      // A listing that never arrived leaves the old rows (and the row that has
      // focus) on screen, so it must also drop any pending handoff rather than
      // leave it to fire on somebody else's navigation.
      leaving.current = null;
      setError((err as Error).message);
      return false;
    }
  }, [device]);

  useEffect(() => {
    if (!fallback) {
      load(initialPath);
      return;
    }
    let live = true;
    (async () => {
      const chain = seedChain(initialPath);
      for (let i = 0; i < chain.length && live; i++) {
        // Only the last try (home) reports its failure: the earlier ones are
        // expected to miss for a folder that does not exist yet.
        if (await load(chain[i], i < chain.length - 1)) return;
      }
    })();
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (data) onListing?.(data.path, (data.entries || []).map((e) => e.path));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  // Navigating swaps every row in the list, which unmounts the button holding
  // focus — and focus falls to document.body, so → and ← worked exactly once and
  // getting back into the list meant Tabbing in from the top of the document,
  // past the whole modal. So a keyboard move claims a row in the NEW listing
  // (see focusRowIndex), and an empty folder hands focus to "use this folder",
  // which down there is the only thing left to do anyway. Pointer navigation
  // names nothing and so moves nothing: the mouse has not lost its place.
  useEffect(() => {
    const from = leaving.current;
    if (from === null || !data) return;
    leaving.current = null;
    const idx = focusRowIndex((data.entries || []).map((e) => e.path), from);
    const rows = listRef.current?.querySelectorAll<HTMLButtonElement>(".rb-item:not(.rb-use)");
    const row =
      idx >= 0 ? rows?.[idx] : listRef.current?.querySelector<HTMLButtonElement>(".rb-use");
    row?.focus();
  }, [data]);

  /** Navigate the way the keyboard has to: name the folder being left before the
   * rows are replaced, so the new listing knows where to put focus. */
  const keyLoad = (to: string) => {
    leaving.current = data?.path || "";
    load(to);
  };

  /** Create the folder named in the inline row, in the folder being browsed.
   *
   * This used to be a ``window.prompt``, which is why it had never worked in
   * the desktop app: Electron does not implement prompt() at all — it logs
   * "prompt() is and will not be supported" and returns null — so the button
   * was dead for every user who wasn't in a browser tab, silently and with no
   * error to report. An inline row is the only shape that works in both.
   *
   * The name goes to the server exactly as typed, trimmed. It is NOT sanitized
   * here: ``/api/mkdir`` refuses anything that isn't a single path segment, and
   * a client-side repair would create a folder under a name the user did not
   * ask for — which is the whole class of bug the explicit parent exists to
   * end. */
  const mkdir = async () => {
    const name = (newFolder || "").trim();
    if (!data?.path || !name || making) return;
    setError("");
    setMaking(true);
    try {
      const r = await deviceApi(device)<{ path: string }>("/api/mkdir", {
        json: { path: data.path, name },
      });
      setNewFolder(null);
      // Naming a brand-new folder IS the deliberate choice, so this stays a
      // pick: field filled, browser closed. (Nothing re-lists afterwards for
      // that reason — the popover is already gone.)
      onPick(r.path);
    } catch (err) {
      // The row stays open with the name still in it: every refusal here
      // (already exists, permission denied, a slash in the name) is one the
      // user fixes by editing what they typed, and clearing the box would make
      // them type it again to find out.
      setError((err as Error).message);
    } finally {
      setMaking(false);
    }
  };

  return (
    <div id="repo-browser">
      <div className="rb-head">
        {/* ← rather than ↑: every file browser people already use — Finder,
            Explorer, a web page — spells "out of here" as back, and the row
            list reads as a place you stepped into, not a level you climbed. */}
        <button
          type="button"
          id="rb-up"
          title="Parent folder"
          aria-label="Parent folder"
          disabled={!data?.parent}
          onClick={() => data?.parent && load(data.parent)}
        >
          ←
        </button>
        <span id="rb-cwd" className="rb-cwd" title={data?.path || ""}>
          {/* LTR run in the RTL (head-clipping) line, or the leading "/" of
              the path is drawn at its far end. */}
          <span dir="ltr">{data?.path || ""}</span>
        </span>
        <button
          type="button"
          id="rb-mkdir"
          title="Create a new folder here"
          aria-expanded={newFolder !== null}
          onClick={() => {
            // Toggling closed discards the typed name deliberately: the button
            // is the cancel, and a row that reopened holding a half-typed name
            // from five folders ago would create it HERE.
            setNewFolder(newFolder === null ? "" : null);
            setError("");
          }}
        >
          + Folder
        </button>
      </div>
      {newFolder !== null && (
        /* The row sits under the path and above the list, where the folder it
           will be created in is the thing directly above it — the parent is
           never in question, which is the one property this control has to
           have. */
        <div className="rb-new">
          <input
            ref={newFolderRef}
            type="text"
            className="rb-new-name"
            value={newFolder}
            autoFocus
            autoComplete="off"
            spellCheck={false}
            placeholder="New folder name"
            aria-label={"New folder in " + (data?.path || "")}
            readOnly={making}
            onChange={(e) => setNewFolder(e.target.value)}
            onKeyDown={(e) => {
              // Both keys stop here. The popover lives inside the new-session
              // modal, whose own handler reads Escape as "cancel the browse"
              // and Enter as "submit the form" — either of which would take
              // the whole dialog somewhere while the user is naming a folder.
              e.stopPropagation();
              if (e.key === "Enter") {
                e.preventDefault();
                mkdir();
              } else if (e.key === "Escape") {
                e.preventDefault();
                setNewFolder(null);
              }
            }}
          />
          <button
            type="button"
            className="rb-new-go"
            disabled={!newFolder.trim() || making}
            onClick={mkdir}
          >
            {making ? "Creating…" : "Create"}
          </button>
        </div>
      )}
      <div id="rb-list" ref={listRef}>
        {data && (
          /* Names the folder it means. Child rows are selectable now, so "use
             this folder" on its own would be ambiguous about which folder — the
             one you're standing in, or the one you just highlighted. */
          <button
            type="button"
            className={
              "rb-item rb-use" +
              (data.is_git ? " is-git" : "") +
              (selected === data.path ? " rb-sel" : "")
            }
            title={"Use " + data.path + " and close the browser"}
            onClick={() => onPick(data.path)}
          >
            {(data.is_git ? "✓ use this repo — " : "use this folder — ") + leafName(data.path)}
          </button>
        )}
        {(data?.entries || []).map((e) => (
          // Rows are real buttons: they're the primary control now, so keyboard
          // reach, Enter/Space activation and a focus ring should come from the
          // platform rather than a div wearing tabIndex and a key handler.
          // Selecting is idempotent, which is what makes the double click safe —
          // its first click writes the same path the second one would, so there
          // is no second visible effect to notice.
          <button
            key={e.path}
            type="button"
            className={
              "rb-item" + (e.is_git ? " is-git" : "") + (selected === e.path ? " rb-sel" : "")
            }
            aria-pressed={selected === e.path}
            title={e.path}
            data-path={e.path}
            onClick={() => onSelect(e.path)}
            onDoubleClick={() => {
              onSelect(e.path);
              load(e.path);
            }}
            onKeyDown={(ev) => {
              // → is the keyboard's double click and ← is its ↑ button: a
              // pointer has both gestures, and Shift+Tabbing out of a long list
              // to reach the parent button is not a substitute.
              if (ev.key === "ArrowRight") {
                ev.preventDefault();
                onSelect(e.path);
                keyLoad(e.path);
              } else if (ev.key === "ArrowLeft" && data?.parent) {
                ev.preventDefault();
                keyLoad(data.parent);
              }
            }}
          >
            <span className="rb-name">{(e.is_git ? "📦 " : "📁 ") + e.name}</span>
          </button>
        ))}
      </div>
      {/* "cancels", not "closes": Esc puts the folder the field started with
          back, and a legend that said otherwise is what made passing through the
          wrong tree cost anything. The Browse button's tooltip spells the pair
          out; this line has to stay one line, since #repo-browser pays for its
          height out of the list. */}
      <p className="rb-hint">Click selects · double-click or → opens · ← up · Esc cancels</p>
      <p id="rb-error" className="error">{error}</p>
    </div>
  );
}
