/** New-session dialog (ports section 16 + the new-form submit from section
 * 17 + the J4 preset row): quick path is name → Enter; folder browser,
 * templates strip, prompt presets, provisioning fold, and per-session launch
 * flags live behind progressive disclosure. */

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useReducer,
  useRef,
  useState,
} from "react";
import type { Config, Instance } from "../../api/types";
import { api } from "../../api/client";
import { refreshInstances, refreshConfig, queryClient } from "../../state/queries";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";
import {
  addPendingSession,
  clearStaleAlias,
  failPendingSession,
  selectSession,
} from "../../lib/sessionActions";
import {
  BUILTIN_PRESETS,
  findPreset,
  loadUserPresets,
  saveUserPresets,
  type Preset,
} from "../../lib/presets";
import { FlagChips, tokenize } from "./FlagChips";

interface Template {
  name: string;
  program?: string;
  repo_path?: string;
  prompt?: string;
  provisioned?: boolean;
  workspace_strategy?: string;
  in_place?: boolean;
  init_repo?: boolean;
}

interface Provider {
  name: string;
  aliases?: string[];
  command?: string;
}

/** One folder /api/repos/suggest thinks the user might mean. */
interface RepoSuggestion {
  path: string;
  name: string;
  is_git: boolean;
  source: string;
}

/** The three reasons the endpoint can have for offering a folder, in the order
 * it ranks them. Each one is labelled in the strip because an unexplained list
 * of folder names reads as noise: without the label there is no way to tell the
 * repo your last session ran in from some directory that merely happens to sit
 * under $HOME, and the user can't judge a suggestion they can't account for. */
const SUGGEST_SOURCES: Array<{ key: string; label: string; hint: string }> = [
  { key: "recent", label: "Recent", hint: "folders your recent sessions ran in" },
  { key: "cwd", label: "Here", hint: "the folder MindFlock itself was started in" },
  { key: "nearby", label: "Nearby", hint: "repos and folders sitting under your home directory" },
];

/** How long the Folder field must sit still before we ask the server what it
 * is. The endpoint answers 200-with-exists:false for half-typed paths, so a
 * per-keystroke call would be harmless but pointless traffic. */
const CHECK_DEBOUNCE_MS = 400;

/** Whether a child ending at ``childRight`` fits inside a row ending at
 * ``rowRight``. Both edges must come from the SAME coordinate space — mixing
 * an offsetParent-relative offset with a container width is what emptied the
 * folder rows once already. Half a pixel of slack, since sub-pixel layout
 * routinely puts a fitting child a hair past the edge. */
export function fitsWithin(childRight: number, rowRight: number): boolean {
  return childRight <= rowRight + 0.5;
}

/** A no-wrap chip row that hides whatever doesn't fit, whole chips only.
 *
 * The row is one line by design (see NewSessionDialog.css), and `overflow:
 * hidden` alone cuts the last pill down the middle — a chopped-off folder name
 * reads as a rendering fault, not as "there are more". Only measurement can
 * tell a chip that fits from one that doesn't, so after layout each child is
 * checked against the container's right edge and the ones past it are hidden
 * outright. Hiding a later child never moves an earlier one in a no-wrap row,
 * so this settles in a single pass; a ResizeObserver redoes it when the dialog
 * is resized.
 */
function FitRow({ children }: { children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement | null>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const fit = () => {
      const kids = Array.from(el.children) as HTMLElement[];
      // Unhide first: the row may have grown, and a chip hidden at the old
      // width has no geometry to measure at the new one.
      for (const k of kids) k.classList.remove("nt-clipped");
      // Viewport coordinates for BOTH sides. offsetLeft looked like the
      // obvious measure and is a trap here: it is relative to offsetParent,
      // which for these chips is .modal (position: fixed), so each chip's
      // offset carried the whole dialog's distance from the window edge and
      // every one of them compared as overflowing — the rows rendered empty.
      const right = el.getBoundingClientRect().right;
      if (!(right > 0)) return; // not laid out yet; hide nothing
      for (const k of kids) {
        // Half a pixel of slack: sub-pixel layout would otherwise drop a chip
        // that lands exactly on the edge.
        if (!fitsWithin(k.getBoundingClientRect().right, right)) {
          k.classList.add("nt-clipped");
        }
      }
    };
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  });
  return (
    <div className="nt-list" ref={ref}>
      {children}
    </div>
  );
}


/** Last path segment, for naming the folder the confirm row would use. Keeps
 * both separators so a Windows path doesn't come back as the whole string. */
function leafName(path: string): string {
  return path.replace(/[/\\]+$/, "").split(/[/\\]/).pop() || path;
}

/** The Folder field's whole state, gathered into one value because both ways it
 * goes wrong are questions of ordering. Browsing writes every row the user
 * clicks straight into the field, so walking away from the browser has to put
 * back what the field held before — otherwise the last directory they merely
 * passed through becomes the session's repo_path. And the pre-fill resolves
 * after the dialog is already interactive, so it must never land on top of a
 * path typed in the meantime. folderReducer decides both, and is pure so those
 * sequences can be tested. */
export interface FolderState {
  /** What the Folder input shows, and what submit sends as repo_path. */
  path: string;
  /** Whether the folder browser sits open under the field. */
  browsing: boolean;
  /** The field as it stood when the browser opened — what Escape puts back.
   * Null when there is no browse to undo. */
  undo: { path: string; touched: boolean } | null;
  /** Whether the user has said where they want to work, by typing, clicking a
   * suggestion chip, choosing in the browser or applying a template. A pre-fill
   * that arrives after that has been outvoted. */
  touched: boolean;
}

export type FolderAction =
  /** The dialog opened: forget the last opening's browse and its touches. */
  | { t: "reopen" }
  /** The best-ranked /api/repos/suggest folder arrived. */
  | { t: "suggested"; path: string }
  /** $HOME, for a field that nothing better has filled. */
  | { t: "fallback"; path: string }
  /** The user named a folder outside the browser: typed it, clicked a
   * suggestion chip, or applied a template carrying one. */
  | { t: "user-set"; path: string }
  | { t: "browse-open" }
  /** A row was clicked or arrowed into — the selection shows in the field. */
  | { t: "browse-select"; path: string }
  /** "use this folder", a folder just created, or the panel toggled shut: the
   * deliberate finish, after which there is nothing left to undo. */
  | { t: "browse-commit"; path: string }
  /** Escape: put the field back the way the browser found it. */
  | { t: "browse-cancel" };

export const FOLDER_INIT: FolderState = {
  path: "",
  browsing: false,
  undo: null,
  touched: false,
};

/** The undo point after a pre-fill lands, which matters only mid-browse.
 *
 * Opening the browser snapshots the field as it stands, and on a first-ever open
 * that snapshot is the empty string — the user reaches for Browse… precisely
 * because the field is still blank. If the suggestion then arrives, Escape would
 * hand the field back to that empty string, and Create answers an empty folder
 * with "a folder is required" after it has already closed the dialog. So a
 * pre-fill that arrives mid-browse becomes the new "nothing happened" baseline,
 * unless the user already had a folder of their own to go back to. */
function prefillUndo(s: FolderState, path: string): FolderState["undo"] {
  if (!s.browsing) return s.undo;
  if (s.undo && s.undo.path) return s.undo;
  return { path, touched: false };
}

export function folderReducer(s: FolderState, a: FolderAction): FolderState {
  switch (a.t) {
    case "reopen":
      // The path itself survives on purpose: it is the folder the last session
      // was started in, which beats an empty field for the moment before this
      // opening's own pre-fill lands.
      return { ...s, browsing: false, undo: null, touched: false };
    case "suggested":
      // A filesystem walk that outran the user is welcome; one that comes back
      // after they typed leaves what they typed alone. An empty field has
      // nothing to lose either way, so it always gets filled.
      if (!a.path || (s.touched && s.path)) return s;
      return { ...s, path: a.path, undo: prefillUndo(s, a.path) };
    case "fallback":
      // $HOME is the weakest of the pre-fills — almost never where anyone
      // works, just somewhere the browser can start — so it only ever fills a
      // field that is otherwise empty.
      if (!a.path || s.path) return s;
      return { ...s, path: a.path, undo: prefillUndo(s, a.path) };
    case "user-set":
      // Typing while the browser is open moves the undo point with it: Escape
      // is there to cancel the browsing, and has no business also swallowing a
      // path the user wrote by hand.
      return {
        ...s,
        path: a.path,
        touched: true,
        undo: s.browsing ? { path: a.path, touched: true } : null,
      };
    case "browse-open":
      return { ...s, browsing: true, undo: { path: s.path, touched: s.touched } };
    case "browse-select":
      return { ...s, path: a.path, touched: true };
    case "browse-commit":
      return { path: a.path, browsing: false, undo: null, touched: true };
    case "browse-cancel":
      // Restores the touched flag too, so a browse that came to nothing also
      // hands the field back to a suggestion still in flight.
      return {
        path: s.undo ? s.undo.path : s.path,
        browsing: false,
        undo: null,
        touched: s.undo ? s.undo.touched : s.touched,
      };
  }
}

/** Whether the dialog's opening focus() may still claim the caret. It may not
 * once focus sits on another control inside the dialog: the user got there
 * first, and taking the caret back mid-word is how the tail of a sentence ends
 * up in the Name box. Focus outside the dialog — the menu button that opened
 * it, the terminal behind — is exactly what the dialog is there to take. */
export function mayTakeOpeningFocus(where: {
  activeIsTarget: boolean;
  activeInsideDialog: boolean;
}): boolean {
  return where.activeIsTarget || !where.activeInsideDialog;
}

export function NewSessionDialog() {
  const open = useUi((s) => s.openDialog === "new-session");
  const closeDialog = useUi((s) => s.closeDialog);

  const [title, setTitle] = useState("");
  const [program, setProgram] = useState("");
  const [providers, setProviders] = useState<Provider[]>([]);
  const [prompt, setPrompt] = useState("");
  const [launchArgs, setLaunchArgs] = useState("");
  const [provision, setProvision] = useState(false);
  const [strategy, setStrategy] = useState("worktree");
  const [inPlace, setInPlace] = useState(true);
  const [initRepo, setInitRepo] = useState(false);
  const [error, setError] = useState("");
  // "More options" starts OPEN — hiding the git/workspace choices behind a
  // click had people launch with the wrong strategy rather than discover it,
  // and those are the settings this dialog exists to set. Launch flags stay
  // CLOSED: extra CLI flags are a per-session rarity, and the card is sized
  // for the form without them. Either fold's state is remembered for the life
  // of the dialog.
  const [advancedOpen, setAdvancedOpen] = useState(true);
  const [launchOpen, setLaunchOpen] = useState(false);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [activeTemplate, setActiveTemplate] = useState("");
  const [provisioningAvailable, setProvisioningAvailable] = useState(false);
  const [homePath, setHomePath] = useState("");
  const [suggestions, setSuggestions] = useState<RepoSuggestion[]>([]);
  // The server's verdict on the folder in the field, stamped with the path we
  // asked about — see the debounce effect for why the stamp is load-bearing.
  const [folderCheck, setFolderCheck] = useState<{ asked: string; plain: boolean } | null>(null);
  const [presetValue, setPresetValue] = useState("");
  const [savedPresets, setSavedPresets] = useState<Preset[]>([]);
  const launchDefaults = useRef<Record<string, string>>({});
  const titleRef = useRef<HTMLInputElement | null>(null);
  const launchRef = useRef<HTMLDetailsElement | null>(null);
  // The modal's own element, so the opening focus can tell "nothing in here has
  // the caret yet" from "the user is already typing in one of these fields".
  const rootRef = useRef<HTMLDivElement | null>(null);
  // The Folder field and the browser's visibility travel together: see
  // folderReducer for the two orderings that forced them into one value. These
  // aliases keep the form's many readers reading a plain value, while every
  // write goes through the reducer.
  const [folder, folderDo] = useReducer(folderReducer, FOLDER_INIT);
  const repoPath = folder.path;
  const browserOpen = folder.browsing;

  // Reset + load fresh data on every open (matches openDialog()).
  useEffect(() => {
    if (!open) return;
    setTitle("");
    setError("");
    setPrompt("");
    setLaunchArgs("");
    setProvision(false);
    setInPlace(true);
    setInitRepo(false);
    // Matches the initial state, and has to be set here too: this reset runs
    // on EVERY open, so a useState default alone left the fold shut from the
    // second open onward.
    setAdvancedOpen(true);
    setLaunchOpen(false);
    folderDo({ t: "reopen" });
    setActiveTemplate("");
    setPresetValue("");
    setSavedPresets(loadUserPresets());
    let live = true;
    // The folder suggestions get a request of their own rather than a place in
    // the barrier below, because the endpoint walks the filesystem: hundreds of
    // listdir/stat probes under $HOME plus a git rev-parse per surviving
    // candidate, which is milliseconds warm and seconds on a cold or
    // network-mounted home. Behind the barrier that walk would hold the agent
    // list, the launch flags and the templates hostage to it as well.
    (async () => {
      try {
        const d = await api<{ suggestions?: RepoSuggestion[] }>("/api/repos/suggest");
        if (!live) return;
        const sug = d.suggestions || [];
        setSuggestions(sug);
        // Start on the best-ranked one — normally the folder the last session
        // used — so the common case needs no Browse trip at all. Unless the user
        // has already said where they want to work: see folderReducer.
        folderDo({ t: "suggested", path: sug[0]?.path || "" });
      } catch {
        // Suggestions are sugar. The field, the browser and Create all work
        // without them, so a failed walk must not cost the dialog anything.
        if (live) setSuggestions([]);
      }
    })();
    (async () => {
      // Config, settings and templates all answer out of memory, so waiting for
      // the slowest of the three costs the dialog nothing.
      const [cfgR, setR, tplR] = await Promise.allSettled([
        refreshConfig().then(() => queryClient.getQueryData<Config>(["config"])),
        api<{ settings?: { coding_cli?: { default_launch_args?: Record<string, string> } } }>(
          "/api/settings"
        ),
        api<{ templates?: Template[] }>("/api/templates"),
      ]);
      if (!live) return;
      const cfg = cfgR.status === "fulfilled" ? cfgR.value : undefined;
      setHomePath(cfg?.home || "");
      // $HOME is the fallback it always was: almost never what the user wants,
      // but a place the browser can start from when the suggestions had nothing
      // to offer (or failed, or are still walking).
      folderDo({ t: "fallback", path: cfg?.home || "" });
      setProvisioningAvailable(!!cfg?.provisioning_available);
      let provs: Provider[] = [];
      try {
        const d = await api<{ providers?: Provider[] }>("/api/providers/manage");
        provs = d.providers || [];
      } catch {
        /* providers are optional */
      }
      if (!live) return;
      setProviders(provs);
      // Map the saved default (name / alias / raw command) to the provider NAME.
      const prev = cfg?.default_program || "";
      const lower = prev.toLowerCase();
      const match = provs.find(
        (p) =>
          (p.name || "").toLowerCase() === lower ||
          (p.aliases || []).some((a) => String(a).toLowerCase() === lower) ||
          String(p.command || "").toLowerCase() === lower
      );
      const agent = match ? match.name : prev;
      setProgram(agent);
      // Per-provider default launch flags pre-fill the field so the default
      // chips start ON; the field is sent explicitly, so toggling one off
      // for this session is honored server-side.
      const raw =
        (setR.status === "fulfilled" && setR.value?.settings?.coding_cli?.default_launch_args) ||
        {};
      launchDefaults.current = {};
      for (const k of Object.keys(raw))
        launchDefaults.current[k.toLowerCase()] = String(raw[k] || "");
      setLaunchArgs((launchDefaults.current[agent.trim().toLowerCase()] || "").trim());
      setTemplates(tplR.status === "fulfilled" ? tplR.value?.templates || [] : []);
    })();
    return () => {
      // Answers for a dialog that has since been closed (or closed and reopened)
      // have nothing to say about this opening, and the suggestion walk is slow
      // enough to still be running when that happens.
      live = false;
    };
  }, [open]);

  // The opening focus belongs to the opening itself, not to the back of the
  // loads above: the dialog paints and is typeable the moment `open` flips, and
  // a focus() that waits for a filesystem walk lands seconds later, yanking the
  // caret out of the Prompt box mid-sentence so the rest of the sentence goes
  // into Name. Even at this range something can get there first — a click that
  // beats this effect's commit — and whatever did keeps the caret.
  useEffect(() => {
    if (!open) return;
    const el = titleRef.current;
    const active = document.activeElement;
    if (
      el &&
      mayTakeOpeningFocus({
        activeIsTarget: active === el,
        activeInsideDialog: !!active && !!rootRef.current?.contains(active),
      })
    )
      el.focus();
  }, [open]);

  // Whether the folder is a git repo is only knowable server-side, and the
  // field is free text, so it has to be re-asked as the user types. Two things
  // keep that honest: the timer, which waits for the typing to settle, and the
  // `live` flag, which the cleanup clears so a slow answer for an abandoned
  // path can never land on top of a newer one — the nudge appearing under a
  // folder that already has a repo is exactly the wrong kind of wrong.
  useEffect(() => {
    const asked = repoPath.trim();
    if (!open || !asked) {
      setFolderCheck(null);
      return;
    }
    let live = true;
    const timer = window.setTimeout(async () => {
      try {
        const r = await api<{ exists?: boolean; is_dir?: boolean; is_git?: boolean }>(
          "/api/repos/check?path=" + encodeURIComponent(asked)
        );
        if (live) setFolderCheck({ asked, plain: !!r.exists && !!r.is_dir && !r.is_git });
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
  }, [open, repoPath]);

  const setAgent = useCallback((value: string) => {
    const v = (value || "").trim();
    setProgram(v);
    // Switching agents resets the flags to that provider's saved default.
    setLaunchArgs((launchDefaults.current[v.toLowerCase()] || "").trim());
  }, []);

  const fillFromTemplate = (t: Template) => {
    if (t.program) setAgent(t.program);
    if (t.repo_path) folderDo({ t: "user-set", path: t.repo_path });
    if (t.prompt) setPrompt(t.prompt);
    setProvision(!!t.provisioned);
    if (t.workspace_strategy) setStrategy(t.workspace_strategy);
    setInPlace(!!t.in_place);
    setInitRepo(!!t.init_repo);
    setAdvancedOpen(!!(t.provisioned || t.init_repo || !t.in_place));
    setActiveTemplate(t.name);
    if (!title.trim()) setTitle(t.name || "");
    titleRef.current?.focus();
  };

  /** The git nudge's action drives the real "Create a git repo in this folder"
   * checkbox instead of a flag of its own — two switches for one behaviour is
   * how a submitted form ends up disagreeing with what the user was shown — and
   * so it inherits that checkbox pair's mutual exclusion with "work directly in
   * this folder". The fold is opened at the same time so the box it just ticked
   * is visible and untickable, rather than changing state out of sight. */
  const armInitRepo = () => {
    setInitRepo(true);
    setInPlace(false);
    setAdvancedOpen(true);
  };

  if (!open) return null;

  const folderPath = repoPath.trim();
  const offerProvision = provisioningAvailable || !!folderPath;
  // Only an answer about the path now in the field earns a line on screen; a
  // reply for an older path is stale by definition, whether it arrived late or
  // is simply what we last learned before the current keystrokes.
  const plainFolder = folderCheck?.asked === folderPath && !!folderCheck?.plain;
  // Empty groups drop out, so a machine with no recent sessions shows "Nearby"
  // alone instead of two blank label columns.
  const suggestRows = SUGGEST_SOURCES.map((g) => ({
    ...g,
    items: suggestions.filter((s) => s.source === g.key),
  })).filter((g) => g.items.length > 0);

  const submit = async () => {
    setError("Creating…");
    const body: Record<string, unknown> = {
      title: title.trim(),
      program: program.trim(),
      repo_path: repoPath.trim(),
    };
    const promptVal = prompt.trim();
    if (promptVal) body.prompt = promptVal;
    // Sent EXPLICITLY (even empty) so a toggled-off default is honored.
    body.launch_args = tokenize(launchArgs);
    if (provision) {
      body.provisioned = true;
      body.workspace_strategy = strategy;
      if (body.repo_path) body.init_repo = initRepo;
    } else {
      body.init_repo = initRepo;
      body.in_place = inPlace;
    }
    // Close NOW with an optimistic "provisioning" row — the POST can take
    // seconds; on failure the dialog re-opens with fields intact.
    const guess = addPendingSession((body.title as string) || "untitled");
    closeDialog();
    try {
      const inst = await api<Instance>("/api/instances", { json: body });
      // Same reason as the guess in addPendingSession: the server's real title
      // must not arrive wearing a closed session's rename.
      clearStaleAlias(inst.title);
      await refreshInstances();
      selectSession(inst.title);
    } catch (err) {
      failPendingSession(guess);
      setError((err as Error).message);
      useUi.getState().openDialogFor("new-session");
    }
  };

  const savePreset = () => {
    const text = prompt.trim();
    if (!text) {
      toast("Type a prompt first, then save it as a preset");
      return;
    }
    const name = window.prompt("Preset name:", "");
    if (!name || !name.trim()) return;
    const list = loadUserPresets().filter((p) => p.name !== name.trim());
    list.push({ name: name.trim(), prompt: text });
    saveUserPresets(list);
    setSavedPresets(list);
    setPresetValue("u:" + name.trim());
    toast(`Saved preset “${name.trim()}”`);
  };

  return (
    <div
      id="new-dialog"
      className="modal"
      ref={rootRef}
      onClick={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          // Escape out of the browser is a cancel, not merely a close: browsing
          // writes each selected row into the Folder field, so leaving it has to
          // hand the field back the way the browser found it.
          if (browserOpen) folderDo({ t: "browse-cancel" });
          else closeDialog();
        } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
          e.preventDefault();
          submit();
        }
      }}
    >
      <form
        id="new-form"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <div className="ws-head">
          <h2>New session</h2>
          <button type="button" id="new-close" title="Close (Esc)" onClick={closeDialog}>
            Close
          </button>
        </div>

        <div className="nf-body">
          {templates.length > 0 && (
            <div id="new-templates" className="new-templates">
              <div className="nt-head">
                <span>Templates</span>
                <button
                  type="button"
                  id="new-templates-manage"
                  className="linklike"
                  onClick={() => {
                    const w = window as unknown as {
                      mindflockAddons?: { templates?: { open?: () => void } };
                    };
                    const t = w.mindflockAddons?.templates;
                    if (t && typeof t.open === "function") {
                      closeDialog();
                      t.open();
                    } else toast("Templates manager isn't loaded");
                  }}
                >
                  Manage…
                </button>
              </div>
              <div id="new-templates-list" className="nt-list">
                {templates.map((t) => (
                  <button
                    key={t.name}
                    type="button"
                    className={"nt-chip" + (activeTemplate === t.name ? " active" : "")}
                    data-name={t.name}
                    title={(t.program ? "[" + t.program + "] " : "") + (t.prompt || "launch this recipe")}
                    onClick={() => fillFromTemplate(t)}
                  >
                    {t.name}
                  </button>
                ))}
              </div>
            </div>
          )}

          <label>
            <span>
              Name <span className="muted">— optional; empty starts an untitled session</span>
            </span>
            <input
              id="new-title"
              ref={titleRef}
              autoComplete="off"
              placeholder="untitled"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </label>

          <div className="nf-quick">
            <label
              className="nf-folder"
              title="Any folder works — git features (diff / commit / PR) turn on automatically when it's a git repo."
            >
              Folder
              <span className="repo-path-row">
                <input
                  id="new-repo-path"
                  autoComplete="off"
                  placeholder="/home/me/projects/foo"
                  value={repoPath}
                  onChange={(e) => folderDo({ t: "user-set", path: e.target.value })}
                />
                <button
                  type="button"
                  id="repo-browse-btn"
                  title={
                    browserOpen
                      ? "Close the browser and keep this folder (Esc puts the old one back)"
                      : "Browse local folders"
                  }
                  onClick={() =>
                    // Toggling the panel shut keeps whatever the field now holds:
                    // it is on screen, the user has been looking at it, and only
                    // Escape claims to undo anything.
                    folderDo(
                      browserOpen ? { t: "browse-commit", path: repoPath } : { t: "browse-open" }
                    )
                  }
                >
                  Browse…
                </button>
              </span>
            </label>
            <label className="nf-agent">
              <span className="nf-agent-head">
                Agent
                <button
                  type="button"
                  id="new-agent-manage"
                  className="linklike"
                  title="Manage coding CLIs in Settings"
                  onClick={() => {
                    closeDialog();
                    useUi.getState().openDialogFor("settings", "coding");
                  }}
                >
                  Manage
                </button>
              </span>
              <select
                id="new-program"
                title="The coding CLI this session runs"
                value={program}
                onChange={(e) => setAgent(e.target.value)}
              >
                {!providers.some((p) => p.name === program) && program && (
                  <option value={program}>{program}</option>
                )}
                {providers.map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {/* The datalist mount slots.js populates from /api/providers. */}
          <datalist id="provider-list"></datalist>

          {plainFolder && (
            <p className="nf-git-nudge">
              {initRepo ? (
                <>A git repo will be created here — diff, commit and PR will work.</>
              ) : (
                <>
                  No git repo in this folder, so diff, commit and PR stay off.{" "}
                  <button
                    type="button"
                    className="linklike"
                    title="Ticks “Create a git repo in this folder” under More options"
                    onClick={armInitRepo}
                  >
                    Create one
                  </button>
                </>
              )}
            </p>
          )}

          {suggestRows.length > 0 && (
            /* Wears .new-templates as well as its own class on purpose: this is
               the same chip strip as Templates, filled from a different source,
               and sharing the class is what stops the two from drifting apart. */
            <div id="new-suggest" className="new-templates nf-suggest">
              <div className="nt-head">
                <span>Folders</span>
                <span className="nf-suggest-legend">📦 git repo · 📁 plain folder</span>
              </div>
              {suggestRows.map((g) => (
                <div key={g.key} className="nf-suggest-row">
                  <span className="nf-suggest-label" title={g.hint}>
                    {g.label}
                  </span>
                  <FitRow>
                    {g.items.map((s) => {
                      const active = folderPath === s.path;
                      return (
                        <button
                          key={s.path}
                          type="button"
                          className={
                            "nt-chip" + (s.is_git ? " is-git" : "") + (active ? " active" : "")
                          }
                          data-path={s.path}
                          aria-pressed={active}
                          title={s.path + (s.is_git ? "" : "\nno git repo here yet")}
                          onClick={() => folderDo({ t: "user-set", path: s.path })}
                        >
                          {(s.is_git ? "📦 " : "📁 ") + s.name}
                        </button>
                      );
                    })}
                  </FitRow>
                </div>
              ))}
            </div>
          )}

          {browserOpen && (
            <FolderBrowser
              initialPath={folderPath || homePath || ""}
              selected={folderPath}
              onSelect={(p) => folderDo({ t: "browse-select", path: p })}
              onPick={(p) => folderDo({ t: "browse-commit", path: p })}
            />
          )}

          <label>
            <span>
              Prompt{" "}
              <span className="muted">— optional; sent to the agent at launch · Ctrl+Enter creates</span>
            </span>
            <span className="preset-row">
              <select
                id="new-preset"
                title="Prompt presets — pick one to fill the prompt below (editable after)"
                value={presetValue}
                onChange={(e) => {
                  setPresetValue(e.target.value);
                  const p = findPreset(e.target.value);
                  if (p) setPrompt(p.prompt);
                }}
              >
                <option value="">Preset…</option>
                {BUILTIN_PRESETS.length > 0 && (
                  <optgroup label="Built-in">
                    {BUILTIN_PRESETS.map((p) => (
                      <option key={"b:" + p.name} value={"b:" + p.name} title={p.prompt}>
                        {p.name}
                      </option>
                    ))}
                  </optgroup>
                )}
                {savedPresets.length > 0 && (
                  <optgroup label="Saved">
                    {savedPresets.map((p) => (
                      <option key={"u:" + p.name} value={"u:" + p.name} title={p.prompt}>
                        {p.name}
                      </option>
                    ))}
                  </optgroup>
                )}
              </select>
              <button type="button" id="preset-save" title="Save current prompt as preset…" onClick={savePreset}>
                Save…
              </button>
              {presetValue.startsWith("u:") && (
                <button
                  type="button"
                  id="preset-del"
                  title="Delete the selected saved preset"
                  onClick={() => {
                    const p = findPreset(presetValue);
                    if (!p) return;
                    const list = loadUserPresets().filter((q) => q.name !== p.name);
                    saveUserPresets(list);
                    setSavedPresets(list);
                    setPresetValue("");
                  }}
                >
                  ✕
                </button>
              )}
            </span>
            <textarea
              id="new-prompt"
              rows={2}
              autoComplete="off"
              spellCheck={false}
              placeholder="What should the agent do first? Leave blank if you don’t want to kick anything off just yet."
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
            />
          </label>

          <details
            id="new-advanced"
            className="nf-advanced"
            data-caps="git"
            open={advancedOpen}
            onToggle={(e) => setAdvancedOpen((e.target as HTMLDetailsElement).open)}
          >
            <summary>
              More options <span className="muted">— git &amp; workspace</span>
            </summary>
            <div className="nf-advanced-body">
              <label className="check">
                <input
                  type="checkbox"
                  id="new-in-place"
                  checked={inPlace}
                  disabled={initRepo}
                  onChange={(e) => {
                    setInPlace(e.target.checked);
                    if (e.target.checked) setInitRepo(false);
                  }}
                />
                Work directly in this folder{" "}
                <span className="muted">
                  (no worktree — edits the original; multiple sessions can share it)
                </span>
              </label>
              <label className="check">
                <input
                  type="checkbox"
                  id="new-init-repo"
                  checked={initRepo}
                  disabled={inPlace}
                  onChange={(e) => {
                    setInitRepo(e.target.checked);
                    if (e.target.checked) setInPlace(false);
                  }}
                />
                Create a git repo in this folder{" "}
                <span className="muted">(git init + initial commit — enables diff/commit/PR)</span>
              </label>

              {offerProvision && (
                <label id="new-provision-row" className="check">
                  <input
                    type="checkbox"
                    id="new-provision"
                    checked={provision}
                    onChange={(e) => setProvision(e.target.checked)}
                  />
                  Provision workspace{" "}
                  <span className="muted">— run repo setup &amp; warm test caches</span>
                </label>
              )}
              {offerProvision && provision && (
                <div id="provision-opts">
                  <label>
                    Workspace strategy
                    <select
                      id="new-workspace-strategy"
                      value={strategy}
                      onChange={(e) => setStrategy(e.target.value)}
                    >
                      <option value="worktree">shared base clone (worktree) — fast, default</option>
                      <option value="clone">full clone — standalone</option>
                    </select>
                  </label>
                  <p className="muted provision-hint">
                    Tip: paste a full branch in <b>Name</b> (e.g.{" "}
                    <code>feature/sc-17436/grafana-dashboard-…</code>) to use it as the branch
                    verbatim — the session name becomes its last segment.
                  </p>
                </div>
              )}
            </div>
          </details>

          <details
            id="new-launch-advanced"
            className="nf-advanced"
            ref={launchRef}
            open={launchOpen}
            onToggle={(e) => {
              const open = (e.target as HTMLDetailsElement).open;
              setLaunchOpen(open);
              // The fold is the last thing in the scroll region, so its revealed
              // fields open below the fold line — scroll them into view so it's
              // obvious the click did something and where to look. Only on a
              // real click: open-by-default fires no toggle, and scrolling the
              // body on arrival would bury the Name field.
              if (open)
                launchRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
            }}
          >
            <summary>
              Launch flags <span className="muted">— extra CLI flags for this session</span>
            </summary>
            <div className="nf-advanced-body">
              <label>
                <span>
                  Flags{" "}
                  <span className="muted">
                    — e.g. --dangerously-skip-permissions; appended after the agent's saved defaults
                  </span>
                </span>
                <input
                  type="text"
                  id="new-launch-args"
                  autoComplete="off"
                  placeholder="--dangerously-skip-permissions"
                  value={launchArgs}
                  onChange={(e) => setLaunchArgs(e.target.value)}
                />
              </label>
              <FlagChips provider={program} value={launchArgs} onChange={setLaunchArgs} />
            </div>
          </details>
        </div>

        <div className="modal-actions nf-actions">
          <p id="new-error" className="error">{error}</p>
          <button type="submit">Create</button>
        </div>
      </form>
    </div>
  );
}

/** Which row a keyboard navigation should land on once the new listing is up:
 * the folder it just stepped out of, if that folder is in the listing — which is
 * the case when stepping UP, so a wrong turn is one ← away from where it started
 * — and otherwise the first row, since a folder is never among its own children.
 * -1 means the listing has no rows to land on at all. */
export function focusRowIndex(paths: string[], leaving: string): number {
  if (!paths.length) return -1;
  const i = paths.indexOf(leaving);
  return i >= 0 ? i : 0;
}

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
function FolderBrowser({
  initialPath,
  selected,
  onSelect,
  onPick,
}: {
  initialPath: string;
  /** The path the Folder field holds, so the highlight always matches the form
   * rather than a copy of the selection kept in here. */
  selected: string;
  /** Fill the field, stay open — undone if the browse is cancelled. */
  onSelect(path: string): void;
  /** Fill the field and close — the explicit "I'm done" gesture. */
  onPick(path: string): void;
}) {
  interface BrowsePayload {
    path: string;
    parent?: string | null;
    is_git?: boolean;
    entries?: Array<{ name: string; path: string; is_git?: boolean }>;
  }
  const [data, setData] = useState<BrowsePayload | null>(null);
  const [error, setError] = useState("");
  const listRef = useRef<HTMLDivElement | null>(null);
  // The folder a keyboard navigation is stepping out of, or null when the
  // pointer drove it (or nothing is pending) — read by the effect below.
  const leaving = useRef<string | null>(null);

  const load = useCallback(async (path: string) => {
    setError("");
    try {
      const q = path ? "?path=" + encodeURIComponent(path) : "";
      setData(await api<BrowsePayload>("/api/browse" + q));
    } catch (err) {
      // A listing that never arrived leaves the old rows (and the row that has
      // focus) on screen, so it must also drop any pending handoff rather than
      // leave it to fire on somebody else's navigation.
      leaving.current = null;
      setError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    load(initialPath);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  const mkdir = async () => {
    if (!data?.path) return;
    const name = window.prompt("New folder name (created in " + data.path + "):", "");
    if (!name || !name.trim()) return;
    setError("");
    try {
      const r = await api<{ path: string }>("/api/mkdir", {
        json: { path: data.path, name: name.trim() },
      });
      // Naming a brand-new folder in a prompt IS the deliberate choice, so this
      // stays a pick: field filled, browser closed. (Nothing re-lists afterwards
      // for that reason — the popover is already gone.)
      onPick(r.path);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <div id="repo-browser">
      <div className="rb-head">
        <button
          type="button"
          id="rb-up"
          title="Parent folder"
          disabled={!data?.parent}
          onClick={() => data?.parent && load(data.parent)}
        >
          ↑
        </button>
        <span id="rb-cwd" className="rb-cwd" title={data?.path || ""}>
          {data?.path || ""}
        </span>
        <button type="button" id="rb-mkdir" title="Create a new folder here" onClick={mkdir}>
          + Folder
        </button>
      </div>
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
