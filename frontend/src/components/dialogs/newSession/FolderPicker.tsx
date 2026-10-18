/** The New form's Folder field — a path field that is also a combobox for a
 * folder NAME — and everything that hangs under it: the name-search matches,
 * the git nudge, the suggestion strip and the inline folder browser. The
 * Folder field's state lives in NewSessionDialog (folderReducer); these read
 * it and write it through folderDo. */

import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type Dispatch,
  type MutableRefObject,
  type ReactNode,
  type SetStateAction,
} from "react";
import type { deviceApi } from "../../../lib/devices";
import {
  CHECK_DEBOUNCE_MS,
  SEARCH_DEBOUNCE_MS,
  SEARCH_MIN_CHARS,
  SUGGEST_SOURCES,
  foldChipRow,
  homeRelative,
  isNameQuery,
  looksLikePath,
  type FolderAction,
  type RepoSuggestion,
} from "../newSession";
import { useFolderPickerWhile } from "../../../lib/folderPicker";

type Set<T> = Dispatch<SetStateAction<T>>;

/** What /api/repos/search last answered, stamped with the query it answers. */
export type FolderSearch = {
  asked: string;
  matches: RepoSuggestion[];
  truncated: boolean;
  home: string;
} | null;

/** The Folder field's server lookups: the git probe for a path, the name
 * search for anything else, and keeping the highlighted match on screen. */
/** What /api/repos/check said about the folder in the field, stamped with
 * the text it answers (`asked`). */
export interface FolderCheck {
  asked: string;
  /** A real folder with no git repo in it. */
  plain: boolean;
  /** Nothing at that path yet: Start stays off until it exists (never made
   * implicitly — "Create this folder" is the one way). */
  missing?: boolean;
  isGit?: boolean;
  /** The server's resolved path (symlinks, ~). */
  resolved?: string;
  /** What a new worktree's branch starts with ("me/"). */
  branchPrefix?: string;
}

export function useFolderLookups({
  open,
  repoPath,
  dapi,
  setFolderCheck,
  setSearch,
  setSearchSel,
  searchListRef,
  searchSel,
  search,
  searchOpen,
}: {
  open: boolean;
  repoPath: string;
  dapi: ReturnType<typeof deviceApi>;
  setFolderCheck: Set<FolderCheck | null>;
  setSearch: Set<FolderSearch>;
  setSearchSel: Set<number>;
  searchListRef: MutableRefObject<HTMLDivElement | null>;
  searchSel: number;
  search: FolderSearch;
  searchOpen: boolean;
}) {
  // Whether the folder is a git repo is only knowable server-side, and the
  // field is free text, so it has to be re-asked as the user types. Two things
  // keep that honest: the timer, which waits for the typing to settle, and the
  // `live` flag, which the cleanup clears so a slow answer for an abandoned
  // path can never land on top of a newer one — the nudge appearing under a
  // folder that already has a repo is exactly the wrong kind of wrong.
  useEffect(() => {
    const asked = repoPath.trim();
    // Only a PATH is probed. The field also takes a name to look up now, and a
    // name is not a location: `check_repo` resolves whatever it is handed
    // against the SERVER's working directory, so typing `backend` while the
    // server runs from its own checkout answered "exists, no git repo here" —
    // about MindFlock's own backend/ — and the nudge underneath offered to git
    // init it. The search list is the right answer to a name; a status line
    // about some directory beside the server is not.
    if (!open || !asked || !looksLikePath(asked)) {
      setFolderCheck(null);
      return;
    }
    let live = true;
    const timer = window.setTimeout(async () => {
      try {
        const r = await dapi<{
          path?: string;
          exists?: boolean;
          is_dir?: boolean;
          is_git?: boolean;
          branch_prefix?: string;
        }>("/api/repos/check?path=" + encodeURIComponent(asked));
        if (live)
          setFolderCheck({
            asked,
            plain: !!r.exists && !!r.is_dir && !r.is_git,
            missing: r.exists === false,
            isGit: !!r.is_git,
            resolved: r.path || "",
            branchPrefix: r.branch_prefix || "",
          });
      } catch {
        // A folder we can't probe simply gets no aside. Guessing "no git here"
        // from a failed request would nag people into initialising repos they
        // already have.
        if (live) setFolderCheck(null);
      }
    }, CHECK_DEBOUNCE_MS);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [open, repoPath, dapi]);

  // A NAME in the Folder field is a question — "where is the repo called api?"
  // — and only the server can answer it: the suggestion sweep above is depth-1
  // by design, so a repo three levels down is invisible to it. Same machinery as
  // the check probe above (a timer so the walk waits for the typing to settle, a
  // `live` flag so an overtaken answer can never land) plus the one guard that
  // keeps this field a path field: text starting with / or ~ never reaches the
  // endpoint at all, so a typed path behaves exactly as it did before this list
  // existed.
  useEffect(() => {
    const asked = repoPath.trim();
    if (!open || looksLikePath(asked) || asked.length < SEARCH_MIN_CHARS) {
      setSearch(null);
      return;
    }
    let live = true;
    const timer = window.setTimeout(async () => {
      try {
        const r = await dapi<{ matches?: RepoSuggestion[]; truncated?: boolean; home?: string }>(
          "/api/repos/search?q=" + encodeURIComponent(asked)
        );
        if (!live) return;
        setSearch({
          asked,
          matches: r.matches || [],
          truncated: !!r.truncated,
          home: r.home || "",
        });
        // The newly ranked top row is what Enter should take. Leaving the
        // highlight where the last query left it would aim it at whichever
        // folder happens to sit at that index now — a different folder, chosen
        // by nobody.
        setSearchSel(0);
      } catch {
        // A search that failed says nothing rather than "no matches": there is a
        // difference between "your folder isn't there" and "we couldn't look",
        // and only the first of those should send someone to Browse….
        if (live) setSearch(null);
      }
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [open, repoPath, dapi]);

  // The highlighted match has to be ON SCREEN, and only this can put it there.
  // The list is a short scroller (about five rows, see .nf-search-list) holding
  // up to twenty matches, so a few presses of ArrowDown walk the highlight out
  // of the visible box: every row still on screen looks unselected, and Enter
  // then fills the field from a folder the user was never shown. Nothing
  // scrolls by itself either — the caret stays in the input and the list is
  // driven by aria-activedescendant, which moves no viewport.
  useEffect(() => {
    const row = searchListRef.current?.querySelector<HTMLElement>('[aria-selected="true"]');
    // "nearest" scrolls the minimum: the highlight comes into view without the
    // list jumping under a user who is only stepping one row at a time. Guarded
    // because scrollIntoView is a real-browser nicety jsdom does not implement,
    // and a missing scroll must not throw inside a render commit.
    if (row && typeof row.scrollIntoView === "function") row.scrollIntoView({ block: "nearest" });
  }, [searchSel, search, searchOpen]);
}

/** Take one match. The path goes through the reducer like every other way of
 * naming a folder — typing, a chip, the browser, a template — so the git
 * nudge, the chip highlight and Create all follow it, and nothing has to know
 * that this particular folder arrived from a search. The list is shut
 * explicitly rather than left to the effect: the field now holds a path, so
 * the effect will clear the matches anyway, and waiting a render for it to do
 * so leaves the list flashing under the folder it has just filled in. */
export function matchPicker(folderDo: Dispatch<FolderAction>, setSearchOpen: Set<boolean>) {
  return (path: string) => {
    folderDo({ t: "user-set", path });
    setSearchOpen(false);
  };
}

/** The folder suggestions: ONE quiet line of chips under the Folder row —
 * recent folders first, then the rest, each run headed by a small label
 * ("Recent", "Home") — and an "N more…" chip that unfolds the line into all
 * of them. It used to be a filled card with a heading and one row per source,
 * the heaviest thing on the form, for what is a shortcut: the list arrives
 * ranked, so the folder you want is at the left, and Browse… is right there.
 *
 * Folded, the line is one line by design, and `overflow: hidden` alone cuts
 * the last pill down the middle — a chopped-off folder name reads as a
 * rendering fault, not as "there are more". Only measurement can tell a chip
 * that fits from one that doesn't, so after layout every child is measured
 * against the row's right edge and foldChipRow (newSession.ts) picks the ones
 * to hide; the "N more…" chip's count is written in the same pass, so the
 * row settles without a second render. A ResizeObserver redoes it when the
 * dialog is resized. */
function SuggestRow({
  scope,
  suggestions,
  folderPath,
  folderDo,
}: {
  scope: string;
  suggestions: RepoSuggestion[];
  folderPath: string;
  folderDo: Dispatch<FolderAction>;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  const moreRef = useRef<HTMLButtonElement | null>(null);
  // Empty groups drop out, so a machine with no recent sessions starts at
  // "Home" instead of a label over nothing.
  const groups = SUGGEST_SOURCES.map((g) => ({
    ...g,
    items: suggestions.filter((s) => s.source === g.key),
  })).filter((g) => g.items.length > 0);
  const total = groups.reduce((n, g) => n + g.items.length, 0);

  useLayoutEffect(() => {
    const el = ref.current;
    const more = moreRef.current;
    if (!el || !more) return;
    // The line breaks only exist unfolded (display: none folded): never measured.
    const kids = Array.from(el.children).filter(
      (k) => k !== more && !k.classList.contains("nf-suggest-break")
    ) as HTMLElement[];
    const fit = () => {
      // Unhide first: the row may have grown, and a chip hidden at the old
      // width has no geometry to measure at the new one.
      for (const k of kids) k.classList.remove("nt-clipped");
      more.classList.remove("nt-clipped");
      if (open) {
        more.textContent = "Fewer";
        return;
      }
      // Viewport coordinates for BOTH sides. offsetLeft looked like the
      // obvious measure and is a trap here: it is relative to offsetParent,
      // which for these chips is .modal (position: fixed), so each chip's
      // offset carried the whole dialog's distance from the window edge and
      // every one of them compared as overflowing — the rows rendered empty.
      const right = el.getBoundingClientRect().right;
      if (!(right > 0)) return; // not laid out yet; hide nothing
      more.textContent = total + " more…"; // the widest it will need to be
      const gap = parseFloat(getComputedStyle(el).columnGap) || 0;
      const hidden = foldChipRow(
        kids.map((k) => ({ label: k.classList.contains("nf-suggest-label"), right: k.getBoundingClientRect().right })),
        right,
        more.getBoundingClientRect().width + gap
      );
      kids.forEach((k, i) => k.classList.toggle("nt-clipped", hidden[i]));
      const n = kids.filter((k, i) => hidden[i] && !k.classList.contains("nf-suggest-label")).length;
      if (n) more.textContent = n + " more…";
      else more.classList.add("nt-clipped");
    };
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  });

  return (
    <div
      id={scope + "-suggest"}
      ref={ref}
      className={"nf-suggest nf-folders" + (open ? " open" : "")}
      role="group"
      aria-label="Folders you might mean"
    >
      {groups.flatMap((g, gi) => [
        // Unfolded, each run starts its own line, so a label never dangles at
        // the end of the previous run's line.
        ...(gi ? [<span key={"b:" + g.key} className="nf-suggest-break" aria-hidden="true" />] : []),
        <span key={"l:" + g.key} className="nf-suggest-label" title={g.hint}>
          {g.label}
        </span>,
        ...g.items.map((s) => {
          const active = folderPath === s.path;
          return (
            <button
              key={s.path}
              type="button"
              className={"nt-chip" + (s.is_git ? " is-git" : "") + (active ? " active" : "")}
              data-path={s.path}
              aria-pressed={active}
              title={s.path + (s.is_git ? "" : "\nno git repo here yet")}
              onClick={() => {
                folderDo({ t: "user-set", path: s.path });
                setOpen(false);
              }}
            >
              {(s.is_git ? "📦 " : "📁 ") + s.name}
            </button>
          );
        }),
      ])}
      {/* Its text is written by the fit pass above (the count is only known
          after measuring), so React renders it empty and never touches it. */}
      <button
        type="button"
        ref={moreRef}
        className="nt-chip nf-more"
        id={scope + "-suggest-more"}
        aria-expanded={open}
        title={open ? "Back to one line" : "Show all " + total + " folders"}
        onClick={() => setOpen((o) => !o)}
      />
    </div>
  );
}

/** The ids a Folder field's parts wear: the New form's ("new" — the
 * browse button keeps its old #repo-browse-btn), or a template editor's
 * ("tpl"). The two dialogs never show at once, but the e2e journeys and the
 * form's own aria wiring address these by id, so each gets its own. */
export function folderIds(scope: string) {
  return {
    input: scope + "-repo-path",
    browse: scope === "new" ? "repo-browse-btn" : scope + "-browse",
    search: scope + "-search",
    list: scope + "-search-list",
    hit: scope + "-search-hit-",
  };
}

export function FolderField({
  scope = "new",
  placeholder = "/home/me/projects/foo — or a folder name to look up",
  repoRef,
  repoPath,
  folderPath,
  folderDo,
  search,
  searchHits,
  searchSel,
  setSearchSel,
  setSearchOpen,
  selIndex,
  browserOpen,
  pickMatch,
}: {
  /** Whose field: "new" (the New form) or "tpl" (a template's editor). */
  scope?: string;
  placeholder?: string;
  repoRef?: MutableRefObject<HTMLInputElement | null>;
  repoPath: string;
  folderPath: string;
  folderDo: Dispatch<FolderAction>;
  search: FolderSearch;
  /** The matches actually on screen (see NewSessionDialog's searchHits). */
  searchHits: FolderSearch;
  searchSel: number;
  setSearchSel: Set<number>;
  setSearchOpen: Set<boolean>;
  selIndex: number;
  browserOpen: boolean;
  pickMatch(path: string): void;
}) {
  const ids = folderIds(scope);
  // The END of a long path is the part that names the repo, so while the
  // field isn't being edited it shows its tail ("…/r5-new/sbx/repo"), not
  // "/home/me/MindFlo…". The full path is the tooltip.
  const inputRef = useRef<HTMLInputElement | null>(null);
  useLayoutEffect(() => {
    const el = inputRef.current;
    if (el && document.activeElement !== el) el.scrollLeft = el.scrollWidth;
  }, [repoPath]);
  // A <div>, not a <label> around everything: a label wrapping Browse… made
  // the button's text part of the field's name ("Folder 📁 Browse…").
  return (
    <div
      className="nf-folder"
      title="Any folder works — git features (diff / commit / PR) turn on automatically when it's a git repo."
    >
      <label htmlFor={ids.input}>Folder</label>
      <span className="repo-path-row">
        <input
          id={ids.input}
          ref={(el) => {
            inputRef.current = el;
            if (repoRef) repoRef.current = el;
          }}
          autoComplete="off"
          placeholder={placeholder}
          value={repoPath}
          title={repoPath || undefined}
          onBlur={(e) => {
            e.currentTarget.scrollLeft = e.currentTarget.scrollWidth;
          }}
          // Announced as a combobox because it is one now: without these
          // a screen reader hears an ordinary text box, the arrow keys
          // move a highlight nothing reports, and Enter fills the field
          // from a list that was never mentioned.
          role="combobox"
          aria-expanded={!!searchHits}
          aria-controls={ids.list}
          aria-autocomplete="list"
          aria-activedescendant={selIndex >= 0 ? ids.hit + selIndex : undefined}
          onChange={(e) => {
            folderDo({ t: "user-set", path: e.target.value });
            // Typing is what brings a dismissed list back: the query has
            // changed, so the reason it was dismissed went with it.
            setSearchOpen(true);
          }}
          onKeyDown={(e) => {
            const hits = searchHits?.matches || [];
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
              if (!searchHits && search && search.asked === folderPath && !browserOpen) {
                // Matches exist for exactly this text and were merely
                // dismissed: the first arrow brings them back, rather
                // than making the user type a character and delete it
                // again to see the list they just closed.
                e.preventDefault();
                setSearchOpen(true);
                return;
              }
              if (!hits.length) return;
              // Otherwise the arrows would take the caret to the ends of
              // the path instead of moving the highlight.
              e.preventDefault();
              const step = e.key === "ArrowDown" ? 1 : -1;
              const at = Math.min(searchSel, hits.length - 1) + step;
              setSearchSel(Math.max(0, Math.min(hits.length - 1, at)));
            } else if (
              e.key === "Enter" &&
              !e.ctrlKey &&
              !e.metaKey &&
              isNameQuery(folderPath)
            ) {
              // This input sits inside the form whose submit CREATES the
              // session, so a plain Enter must not leak out of a field
              // that is holding a NAME — with or without matches under
              // it. With a highlight it takes the highlighted folder;
              // preventDefault then stops the session being created in a
              // folder the user had only just chosen, before they had
              // seen the choice land. Without one — the search found
              // nothing, or has not answered yet, or the list was
              // dismissed with Escape — it does nothing, which is the
              // whole point: gating this branch on the matches meant
              // Enter on "notathing" submitted the SEARCH TERM as
              // repo_path, and the server resolves a bare name against
              // its own working directory and creates it. A search that
              // came up empty must not be one keystroke from a session in
              // a stray folder next to the server.
              //
              // Ctrl/Cmd+Enter is excluded on purpose: that chord means
              // "create now" everywhere else in the dialog, the modal's
              // own handler owns it, and submit() refuses a name there.
              // A field holding a real path keeps plain Enter as submit,
              // exactly as it behaved before this field could search.
              e.preventDefault();
              if (hits.length) pickMatch(hits[Math.min(searchSel, hits.length - 1)].path);
              else if (search && search.asked === folderPath && !browserOpen)
                // Matches exist and were merely dismissed: Enter brings
                // them back rather than swallowing the keystroke, the
                // same courtesy the arrows do above.
                setSearchOpen(true);
            } else if (e.key === "Escape" && searchHits) {
              // Escape dismisses the list and nothing else: not the typed
              // text, which is the query and the one thing the user would
              // resent retyping, and not the dialog — so it must not
              // reach the modal's Escape handler above.
              e.preventDefault();
              e.stopPropagation();
              setSearchOpen(false);
            }
          }}
        />
        <button
          type="button"
          id={ids.browse}
          className="repo-browse"
          title={
            browserOpen
              ? "Close the picker and keep this folder (Esc puts the old one back)"
              : "Pick a folder in a window you can move"
          }
          aria-expanded={browserOpen}
          onClick={() =>
            // Toggling the panel shut keeps whatever the field now holds:
            // it is on screen, the user has been looking at it, and only
            // Escape claims to undo anything.
            folderDo(
              browserOpen ? { t: "browse-commit", path: repoPath } : { t: "browse-open" }
            )
          }
        >
          <span aria-hidden="true">📁 </span>
          Browse…
        </button>
      </span>
    </div>
  );
}

/** Everything under the Folder field, in the order it is drawn. */
export function FolderAids({
  scope = "new",
  searchHits,
  selIndex,
  searchListRef,
  setSearchSel,
  pickMatch,
  homePath,
  newFolderRow,
  plainFolder,
  initRepo,
  armInitRepo,
  suggestions,
  folderPath,
  folderDo,
  browserOpen,
  device,
  gitAsked = false,
}: {
  scope?: string;
  /** The form asks "Make it a git repo first / Just run in the folder" in
   * Works in, so the one-line git nudge here would ask it twice. */
  gitAsked?: boolean;
  searchHits: FolderSearch;
  selIndex: number;
  searchListRef: MutableRefObject<HTMLDivElement | null>;
  setSearchSel: Set<number>;
  pickMatch(path: string): void;
  homePath: string;
  newFolderRow: ReactNode;
  plainFolder: boolean;
  initRepo: boolean;
  armInitRepo(): void;
  suggestions: RepoSuggestion[];
  folderPath: string;
  folderDo: Dispatch<FolderAction>;
  browserOpen: boolean;
  device: string;
}) {
  // Browse… opens the floating picker window (FolderPickerWindow) for as long
  // as the form says the browse is open. Same reducer actions as the inline
  // browser had: a click writes the field (browse-select), "Use this folder"
  // commits, and ✕ / Escape / another field taking the window cancel — which
  // hands the field back the way the browse found it.
  useFolderPickerWhile(browserOpen, () => ({
    title: scope === "tpl" ? "Pick the template's folder" : "Pick a folder",
    device,
    start: folderPath || homePath || "",
    selected: folderPath,
    anchor: document.getElementById(folderIds(scope).browse),
    onSelect: (p) => folderDo({ t: "browse-select", path: p }),
    onPick: (p) => folderDo({ t: "browse-commit", path: p }),
    onCancel: () => folderDo({ t: "browse-cancel" }),
  }));
  // A row whose only suggestion is the folder already in the field repeats
  // the field above it: nothing to choose, so nothing drawn.
  const onlyCurrent =
    suggestions.length > 0 && suggestions.every((s) => s.path === folderPath);
  const ids = folderIds(scope);
  return (
    <>
      {searchHits && (
        /* The name-search results. Wears .new-templates and .nf-suggest for
           the same reason the suggestion strip below does — same card, same
           head, same pills, a different source — and adds only what a search
           hit needs that a suggestion chip doesn't: its path. Two folders
           called `api` are told apart by where they live, and telling them
           apart is the entire point of having searched. */
        <div id={ids.search} className="new-templates nf-suggest nf-search">
          <div className="nt-head">
            <span>Matches for “{searchHits.asked}”</span>
            <span className="nf-suggest-legend">↑↓ choose · Enter fills · Esc closes</span>
          </div>
          {searchHits.matches.length > 0 && (
            <div
              id={ids.list}
              className="nf-search-list"
              ref={searchListRef}
              role="listbox"
              aria-label="Folder matches"
            >
              {searchHits.matches.map((m, i) => (
                <button
                  key={m.path}
                  id={ids.hit + i}
                  type="button"
                  role="option"
                  aria-selected={i === selIndex}
                  /* .active is the suggestion strip's "this is the folder
                     you get" treatment — accent border and tint, light
                     theme included — and that is precisely what the
                     highlight means here, so it reuses it rather than
                     inventing a second way to say the same thing. */
                  className={
                    "nt-chip" + (m.is_git ? " is-git" : "") + (i === selIndex ? " active" : "")
                  }
                  data-path={m.path}
                  title={m.path + (m.is_git ? "" : "\nno git repo here yet")}
                  // Hovering moves the highlight so the mouse and the
                  // keyboard never disagree about which row Enter takes.
                  onMouseMove={() => setSearchSel(i)}
                  onClick={() => pickMatch(m.path)}
                >
                  <span className="nf-search-name">{(m.is_git ? "📦 " : "📁 ") + m.name}</span>
                  <span className="nf-search-path">
                    {homeRelative(m.path, searchHits.home || homePath)}
                  </span>
                </button>
              ))}
            </div>
          )}
          {searchHits.matches.length === 0 && (
            /* An empty result has to name the ways out, because the field
               looks identical whether the search found nothing or was never
               a search at all. Both sentences are written to claim only what
               the walk can actually support. A budget that tripped before
               finding anything did not look everywhere, so "not here" would
               be a claim it never got far enough to make — and it does not
               ask the user to type more of the name, because the walk is
               query-independent (the needle only RANKS what was already
               reached) and a longer query re-walks the same directories for
               the same budget. Nor does the complete-walk sentence say
               "nothing under your home directory is called X": the search
               stops at three levels, never enters a git repo and skips
               hidden and node_modules-shaped folders, so a `widget` inside
               the monorepo the user lives in is plainly under home and
               plainly not something this walk can see. */
            <p className="nf-search-empty muted">
              {searchHits.truncated ? (
                <>
                  The search stopped at its time and size limit before it found “
                  {searchHits.asked}” — Browse… walks straight to it, and a path starting with /
                  or ~ is used exactly as typed.
                </>
              ) : (
                <>
                  No folder called “{searchHits.asked}” in the first three levels under your
                  home directory — the search doesn’t look inside git repos or hidden folders.
                  Browse… reaches those and the rest of the disk, and a path starting with / or
                  ~ is used exactly as typed.
                </>
              )}
            </p>
          )}
          {searchHits.matches.length > 0 && searchHits.truncated && (
            /* Deliberately does NOT say "more folders matched": truncation
               is one flag over three causes (the scan cap, the 1.5s deadline
               and the row limit), and under either of the first two no extra
               match is known to exist — the walk simply stopped. "May not be
               everything" is true of all three. */
            <p className="nf-search-note muted">
              The search stopped early, so this list may not be everything — Browse… if the
              folder you want isn’t here.
            </p>
          )}
        </div>
      )}

      {newFolderRow}

      {plainFolder && !gitAsked && (
        <p className="nf-git-nudge">
          {initRepo ? (
            <>A git repo will be created here — diff, commit and PR will work.</>
          ) : (
            <>
              No git repo in this folder, so diff, commit and PR stay off.{" "}
              <button
                type="button"
                className="linklike"
                title="Ticks “Create a git repo in this folder”"
                onClick={armInitRepo}
              >
                Create one
              </button>
            </>
          )}
        </p>
      )}

      {suggestions.length > 0 && !onlyCurrent && (
        <SuggestRow scope={scope} suggestions={suggestions} folderPath={folderPath} folderDo={folderDo} />
      )}

    </>
  );
}

/** "No folder at …" — the Folder field names a path that does not exist.
 * Start stays off: MindFlock never makes a folder by itself. The one way to
 * get it is the button, which creates exactly that folder (POST /api/mkdir)
 * and then the field reads as an ordinary empty folder. */
export function MissingFolderRow({
  label,
  onCreate,
  busy,
  error,
}: {
  /** The path as the user should read it (~-relative when known). */
  label: string;
  onCreate(): void;
  busy: boolean;
  /** Why the last Create this folder failed ("" = it didn't). */
  error: string;
}) {
  return (
    <p className="nf-folder-missing" id="new-folder-missing" role="alert">
      <span className="nf-folder-missing-msg">No folder at {label}.</span>{" "}
      <button
        type="button"
        id="new-create-folder"
        className="nf-fallback"
        disabled={busy}
        title="Make this folder now, then start in it"
        onClick={onCreate}
      >
        {busy ? "Creating…" : "Create this folder"}
      </button>
      {error && <span className="nf-folder-missing-err"> {error}</span>}
    </p>
  );
}
