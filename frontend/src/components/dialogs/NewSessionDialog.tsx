/** New-session dialog (ports section 16 + the new-form submit from section
 * 17 + the J4 preset row). Two pages, as in v0.7.
 *
 * Every opening lands on page 1, a compact card with one question — "What do
 * you want to work on?" — and three buttons in increasing order of
 * commitment: "Set it up myself instead" (the full form, empty, no model
 * turn), "Review details first ↵" (the model reads the sentence and fills the
 * form in for review; Enter in the box does this) and "Create session" (reads
 * it and creates straight away). Intake's "Start together…" opens the same
 * page as a list, one session per line or ticket.
 *
 * Page 2 is the full form, with ← Back to the sentence: the Templates strip
 * (one server-side list, managed in TemplatesDialog), Name pre-filled with the
 * template, agent and account the last session in that folder used (lib/store's
 * last-used memory) so Name → Enter creates with no model turn, then the folder
 * browser, Runs on, Fast-track, prompt presets, the Git & workspace fold with
 * its Base branch picker, and per-session launch flags behind progressive
 * disclosure. */

import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import type { Config, PlanAnswer, RepoBranches } from "../../api/types";
import {
  refreshConfig,
  queryClient,
  useAuthProfiles,
  useConfig,
  useDevices,
  useInstances,
} from "../../state/queries";
import {
  deviceApi,
  deviceLabel,
  startableDevices,
} from "../../lib/devices";
import {
  forgetNewSessionProfile,
  lastNewSessionChoice,
  useUi,
} from "../../state/store";
import {
  PRESETS_CHANGED,
  loadUserPresets,
  type Preset,
} from "../../lib/presets";
import {
  ensureTemplates,
  saveTemplate,
  templatesNow,
  templatesTarget,
  type Template,
} from "../../lib/templates";
import { runTemplateNow } from "../../lib/templateRun";
import { openSettings } from "../../lib/openSettings";
import { toast } from "../../lib/toast";
import { errMsg } from "../../lib/format";
import { TemplatesDialog } from "./TemplatesDialog";
import { useFileDropTextarea } from "../../lib/fileDropTextarea";
import { NewTicketPane } from "./NewTicketPane";
import { SplitCheck } from "./SplitCheck";
import { useRunDraft } from "./NewList";
import { Seg } from "./FastTrackChoice";
import {
  ASK_FIRST_DESC,
  ASK_FIRST_LABEL,
  LANE_DESC,
  LANE_LABEL,
  askFirstApplies,
} from "../../lib/laneActions";
import { FAST_TRACK_LEVELS } from "../../lib/vocab";
import { PR_FALLBACK_HINT, hasPrSupport } from "../../lib/sessionActions";
import { agentName } from "../../lib/agentNames";
import { splitGate } from "../../lib/playbooks";
import {
  SERVER_NO_SPLIT,
  teamRunCaps,
  type Lane,
} from "../../lib/laneActions";
import {
  SPLIT_DEFAULT,
  clampSplit,
  summarySentence,
} from "../../lib/runStart";
import { newSessionSubmit } from "../../lib/newSessionSubmit";
import {
  DESCRIBE_MAX_CHARS,
  FOLDER_INIT,
  START_LABEL,
  advancedSummary,
  homeRelative,
  prNeedsGithubReason,
  NEW_TABS,
  PLAN_FOLDER_NONE,
  cancelPlanRun,
  ctrlEnterAction,
  folderReducer,
  mayTakeOpeningFocus,
  newFolderBlockReason,
  newFolderGate,
  planFolderReducer,
  planGatePath,
  planNoteFor,
  provisionBlockReason,
  rememberedProfileVerdict,
  worktreeClampReason,
  agentMissingReason,
  hasTypedDraft,
  newSessionResume,
  openingMode,
  plannedName,
  sessionsInRepo,
  takenNames,
  preferInstalled,
  remoteBranchName,
  sessionsInFolder,
  templateFromForm,
  templateNameTaken,
  type InstalledMap,
  type NewTab,
  type PlanRun,
  type Provider,
  type RepoSuggestion,
} from "./newSession";
import { DescribeSection, describeFlow, useDescribeFocus } from "./newSession/DescribeSection";
import { TemplatesStrip, templateFiller } from "./newSession/TemplatesStrip";
import {
  FolderAids,
  FolderField,
  MissingFolderRow,
  matchPicker,
  useFolderLookups,
  type FolderCheck,
} from "./newSession/FolderPicker";
import { AgentFields, ModelRow, accountSetter } from "./newSession/AgentAccount";
import { GitOptions, WorksIn, useBaseBranches } from "./newSession/WorkspaceFold";
import { PlanFirstRow, TaskField } from "./newSession/PromptFold";
import { LaunchFold } from "./newSession/LaunchFold";
import { FooterActions, SaveTemplateRow } from "./newSession/FooterActions";
import { TourButton } from "../onboarding/TourButton";
import { useAutoTour } from "../onboarding/useAutoTour";

// The pure half (reducers, gates, the fast path's Enter rule) lives in
// newSession.ts; re-exported so every name this module ever exported still is.
export * from "./newSession";

export function NewSessionDialog() {
  const open = useUi((s) => s.openDialog === "new-session");
  useAutoTour("new", open);
  const closeDialog = useUi((s) => s.closeDialog);

  // Which of the two things this dialog is making. Not persisted across opens:
  // the session tab is what Alt+N is for, and reopening into whatever was last
  // looked at is how a keyboard shortcut stops being predictable.
  const [tab, setTab] = useState<NewTab>("session");

  const [title, setTitle] = useState("");
  const [program, setProgram] = useState("");
  const [providers, setProviders] = useState<Provider[]>([]);
  const [prompt, setPrompt] = useState("");
  // Drop a file on the prompt box and its uploaded path is spliced in at the
  // caret. No session exists yet on this form, so the bytes land in
  // ~/.mindflock/pastes and the agent is handed an absolute path.
  //
  // The drop zone is the dialog CARD, and the same ref goes on both of them —
  // they are mutually exclusive, and each contains exactly one textarea for the
  // helper to find (the prompt here, the brief on the ticket tab). A callback
  // ref, not a useRef object, because the cards mount and unmount as the tab
  // changes and an effect bound once would bind to whichever was showing first.
  const dropZoneRef = useFileDropTextarea({
    // Only reached when the card holds no textarea — page 1, where the only
    // field is the one-sentence describe box and the prompt fold does not exist
    // yet. The path goes into THAT box, because it is the one on screen: an
    // earlier version appended to the prompt instead, which was correct and
    // completely invisible — you dropped a file, the cue flashed, and the
    // sentence field sat there empty as though nothing had happened.
    onInsert: (text) =>
      setDescribe((prev) => {
        const next = (prev && !/\s$/.test(prev) ? prev + " " : prev) + text + " ";
        return next.length > DESCRIBE_MAX_CHARS ? next.slice(0, DESCRIBE_MAX_CHARS) : next;
      }),
  });
  const [launchArgs, setLaunchArgs] = useState("");
  const [provision, setProvision] = useState(false);
  const [strategy, setStrategy] = useState("worktree");
  // A separate worktree is the default: a session on its own branch can't
  // overwrite another's edits. "This folder directly" is one click away.
  const [inPlace, setInPlace] = useState(false);
  const [initRepo, setInitRepo] = useState(false);
  // Plan first: the launch prompt also asks the agent to list every file it
  // means to touch (with intent) and wait — the Map tab turns that list into
  // ghost tiles and blast arms you can red-zone before a single edit.
  const [planFirst, setPlanFirst] = useState(false);
  // "Auto-split into up to N sessions if it's worth it" (page 1 only): the
  // start becomes an optional split run — MindFlock creates one session whose
  // agent decides; a split's pieces are proposed, approved, started and
  // merged back by the server. See SplitCheck.
  const [split, setSplit] = useState(false);
  const [maxPieces, setMaxPieces] = useState(SPLIT_DEFAULT);
  // Opened as a BATCH (Intake's "Start together…"): the box is a list, one
  // session per line or ticket. Typing never makes one — every other box is
  // one prompt.
  const [batch, setBatch] = useState(false);
  /** The list mode's "tasks start in" browser is open (page 1). */
  const [runBrowse, setRunBrowse] = useState(false);
  const [error, setError] = useState("");
  // "Git & workspace" starts OPEN — hiding those choices behind a click had
  // people launch with the wrong strategy rather than discover it, and they
  // are the settings this dialog exists to set. Launch flags stay
  // CLOSED: extra CLI flags are a per-session rarity, and the card is sized
  // for the form without them. Either fold's state is remembered for the life
  // of the dialog.
  const [advancedOpen, setAdvancedOpen] = useState(true);
  const [launchOpen, setLaunchOpen] = useState(false);
  /** More options → "Advanced": what a first-hour session can't use yet
   * (ask-first, auto-split, provisioning, plan first, base branch, launch
   * flags). Shut by default, its own summary naming what is inside. */
  const [deepOpen, setDeepOpen] = useState(false);
  /** "Create this folder": in flight, and why the last one failed. */
  const [mkBusy, setMkBusy] = useState(false);
  const [mkError, setMkError] = useState("");
  // The Task box is always on the form now (it is what a session is for), so
  // the plan and submit code's "open the Prompt fold" has nothing to open.
  const setPromptOpen: Dispatch<SetStateAction<boolean>> = () => {};
  const [templates, setTemplates] = useState<Template[]>([]);
  const [activeTemplate, setActiveTemplate] = useState("");
  /** Base branch for a NEW WORKTREE: "" = whatever the server would pick (the
   * folder's checked-out branch — shown in the picker's first row). Sent as
   * base_ref + base_branch only when chosen, and only for a plain worktree
   * session: the server refuses it for in-place and provisioned ones. */
  const [baseBranch, setBaseBranch] = useState("");
  /** The remote-tracking base for which "Work on x itself" was chosen ("" =
   * a new branch). Keyed by the pick, so any other pick reads as the default
   * without a reset of its own. */
  const [continueOn, setContinueOn] = useState("");
  /** GET /api/repos/branches for the folder in the field; null until asked. */
  const [branches, setBranches] = useState<RepoBranches | null>(null);
  // The one-sentence door at the top of the body: what the user typed, whether
  // a model turn is in flight, and whether that turn has gone on long enough
  // that the ring alone stops reading as progress. Deliberately NOT wired to
  // anything the form submits — the sentence's only output is the fields
  // applyPlan writes, and it is kept only so the box still shows it afterwards.
  /** Which of the two pages is on screen: 1 the sentence, 2 the form.
   *
   * An opening lands on 1, the compact card with the sentence box (as v0.7
   * did — the owner: "I like how it used to look in v0.7"). Page 1 carries an
   * explicit way through to the form that costs one click and no model turn
   * ("Set it up myself instead"), and a failed create reopens on page 2, where
   * the fields to fix it are. */
  const [page, setPage] = useState(1);
  /** The page this opening meant to land on — read by the focus effect, which
   * runs before the setPage above has rendered. */
  const openedPage = useRef(1);
  /** Where this opening puts the caret: the describe box, or Name for a failed
   * create's reopen (the form is what needs fixing then). */
  const openedFocus = useRef<"describe" | "title">("describe");
  /** The opening's target was not on screen yet (page 2 still mounted from
   * the last opening): focus it once it is. */
  const focusOpeningNext = useRef(false);
  const [describe, setDescribe] = useState("");
  const [describing, setDescribing] = useState(false);
  const [describeSlow, setDescribeSlow] = useState(false);
  // Which page-1 button started the read (its label carries the clock), when,
  // and whether it failed or ran out of time (the form is offered instead).
  const [describeMode, setDescribeMode] = useState<"fill" | "start">("fill");
  const [describeSince, setDescribeSince] = useState(0);
  const [describeFailed, setDescribeFailed] = useState(false);
  /** Agent name → installed on THIS device (GET /api/providers/status); null
   * = unknown (another device, an older server, still loading). */
  const [installed, setInstalled] = useState<InstalledMap | null>(null);
  /** "Save as template…": the inline name row (null = closed). */
  const [saveTplName, setSaveTplName] = useState<string | null>(null);
  const [saveTplBusy, setSaveTplBusy] = useState(false);
  // The server-composed sentence about what it chose and why, and the inline
  // failure. Two states rather than one because they are different registers
  // in the same place: the note is muted, the error is red, and a run that
  // fails must not leave the previous run's note underneath claiming a folder.
  const [planNote, setPlanNote] = useState("");
  const [planError, setPlanError] = useState("");
  // The folder the last plan was about — always its repo_path, existing or not
  // — and whether the user has said yes to making it. Both the gate and the
  // note are derived by comparing that path against the Folder field (see
  // newFolderGate / planNoteFor), which is why the plan is kept whole and why
  // its transitions live in a reducer that can be replayed in a test.
  const [planFolder, planFolderDo] = useReducer(planFolderReducer, PLAN_FOLDER_NONE);
  const [newFolderOk, setNewFolderOk] = useState(false);
  /** The last plan and the sentence it answered, so page 1's "Start session"
   * can be pressed twice without paying for a second model turn.
   *
   * It needs two presses whenever the plan lands on a folder that does not
   * exist: the first shows the question, the second acts on the answer. Without
   * this the second press would re-run the CLI and could come back with a
   * different folder than the one the user just agreed to — a confirmation for
   * one directory spent on another. */
  const lastAnswer = useRef<{ sentence: string; answer: PlanAnswer } | null>(null);
  /** Set when a fill has just moved to page 2 and the Folder field, which only
   * exists on that page, still has to take the caret. */
  const focusFolderNext = useRef(false);
  /** Set when applyPlan is about to open the Prompt fold, so that fold's
   * onToggle can tell a programmatic open from a click.
   *
   * A <details> fires `toggle` whichever way it was opened — React setting the
   * `open` attribute counts — and that handler scrolls the fold to the bottom
   * of the scroll region. So a plan that filled in a prompt scrolled page 2
   * down the moment it arrived, undoing the "land at the top of the form" the
   * focus effect had just done and cutting Name and Folder off above the view.
   * (The launch fold's own comment claims an open-by-default fires no toggle;
   * that is true of the initial render and not of this.) */
  const foldOpenedByPlan = useRef(false);
  const [provisioningAvailable, setProvisioningAvailable] = useState(false);
  const [homePath, setHomePath] = useState("");
  const [suggestions, setSuggestions] = useState<RepoSuggestion[]>([]);
  // The server's verdict on the folder in the field, stamped with the path we
  // asked about — see the debounce effect for why the stamp is load-bearing.
  const [folderCheck, setFolderCheck] = useState<FolderCheck | null>(null);
  // What /api/repos/search last answered, stamped with the query it answers.
  // Same trick as folderCheck, load-bearing for the same reason: a walk started
  // three keystrokes ago must not drop its matches under a field that has moved
  // on. `home` rides along so the rows can shorten their paths against the
  // server's idea of home rather than the config's.
  const [search, setSearch] = useState<{
    asked: string;
    matches: RepoSuggestion[];
    truncated: boolean;
    home: string;
  } | null>(null);
  // Whether the match list is on screen. Escape closes it and touches nothing
  // else: the list is a suggestion, not a modal, and dismissing it must not also
  // throw away the query the user typed to summon it.
  const [searchOpen, setSearchOpen] = useState(true);
  // Which match Enter takes. Always a real row rather than "nothing selected",
  // so what Enter will do is visible before it is pressed — the alternative is a
  // key that either fills the field or creates a session depending on state the
  // user can't see.
  const [searchSel, setSearchSel] = useState(0);
  const [presetValue, setPresetValue] = useState("");
  const [savedPresets, setSavedPresets] = useState<Preset[]>([]);
  // Auth profile pin: "" = inherit the app-wide default account; "default" =
  // explicitly the CLI's own login; anything else = a configured profile id.
  const [profileId, setProfileId] = useState("");
  // This session's model override of the account's own pin ("" = the pin).
  const [profileModel, setProfileModel] = useState("");
  // Model ids the selected OpenRouter key can reach, per profile id — fetched
  // lazily on selection so the Model field is a picker, not a guess.
  const [profileModels, setProfileModels] = useState<Record<string, string[]>>({});
  const authProfiles = useAuthProfiles().data;
  /** A remembered account waiting for the profile list to land before it is
   * applied or forgotten (rememberedProfileVerdict). */
  const pendingProfile = useRef<{ repo: string; id: string; program?: string } | null>(null);
  const selectedProfile = (authProfiles?.profiles || []).find((p) => p.id === profileId);
  const launchDefaults = useRef<Record<string, string>>({});
  const titleRef = useRef<HTMLInputElement | null>(null);
  const describeRef = useRef<HTMLTextAreaElement | null>(null);
  // #new-repo-path, so a landed plan can put the caret on the one field most
  // likely to be wrong — and, because focusing scrolls, actually on screen.
  const repoRef = useRef<HTMLInputElement | null>(null);
  /** The in-flight plan request: a seq bumped by every run, every open and
   * every CLOSE, so an answer that belonged to a sentence the user has moved on
   * from can never land on the form, plus the controller that drops its socket.
   * Same job as the `live` flags in the check and search effects below, and
   * needed for the same reason: this component never unmounts (it returns null
   * when shut), so a promise started before a close is still running after the
   * reopen. One object rather than two refs because every bug here has been one
   * of the two moving without the other — see startPlanRun / cancelPlanRun. */
  const planRun = useRef<PlanRun>({ seq: 0, abort: null });
  /** Epoch after which Create is armed again. See submitHoldReason. */
  const submitArmAt = useRef(0);
  // The match list's scroll box, so the keyboard highlight can be scrolled into
  // it — see useFolderLookups for why nothing else will do that.
  const searchListRef = useRef<HTMLDivElement | null>(null);
  // The modal's own element, so the opening focus can tell "nothing in here has
  // the caret yet" from "the user is already typing in one of these fields".
  const rootRef = useRef<HTMLDivElement | null>(null);
  // Armed by a failed create, for the reopen it is about to trigger. See the
  // reset effect, which is the thing that has to know.
  const failedReopen = useRef(false);
  /** The last close was a Create (or a run's start): the next opening starts
   * fresh. Any other close keeps what was typed — see openingMode. */
  const draftSpent = useRef(false);
  /** The text "Set it up myself" last carried from the sentence into Task:
   * a Task still holding exactly that is the sentence's, so a changed
   * sentence replaces it — a Task edited since is the user's and is kept. */
  const carried = useRef("");
  /** The Task the opening's last-used template wrote by itself (not typed). */
  const prefilledPrompt = useRef("");
  /** Silences the open's loads when the dialog closes (see loadFor). */
  const loadCleanup = useRef<(() => void) | null>(null);
  /** The open's main load (agents, defaults, templates) has landed. A draft
   * kept from an opening closed before it did loads again on reopen. */
  const loadDone = useRef(false);
  /** Agents installed here but known NOT signed in (GET /api/setup). */
  const [signedOut, setSignedOut] = useState<Record<string, boolean>>({});
  // The Folder field and the browser's visibility travel together: see
  // folderReducer for the two orderings that forced them into one value. These
  // aliases keep the form's many readers reading a plain value, while every
  // write goes through the reducer.
  const [folder, folderDo] = useReducer(folderReducer, FOLDER_INIT);
  const repoPath = folder.path;
  const browserOpen = folder.browsing;

  // WHERE the session will live: "" is this device, anything else a connected
  // tailnet device (sessions are equal wherever they run — the picker only
  // shows up once a second device is there to pick). Kept across openings
  // like the folder is. A batch from Intake always starts here: runs are
  // driven by the server that starts them.
  const { data: devicesResp } = useDevices();
  const startable = useMemo(() => startableDevices(devicesResp), [devicesResp]);
  const [devicePick, setDevicePick] = useState("");
  const device =
    batch || !startable.some((d) => d.device === devicePick) ? "" : devicePick;
  const dapi = useMemo(() => deviceApi(device), [device]);
  // Read by the loads, whose answers may land after the user picked another
  // device: an answer about the old device's disk must not fill the form.
  const deviceRef = useRef(device);
  deviceRef.current = device;
  const loadedDevice = useRef<string | null>(null);

  /** Which agents this device has installed and signed in to, asked again.
   * An opening that keeps the form (a typed draft, a hand-back) skips the
   * full load — and so kept saying "not installed" / "isn't signed in" after
   * the person fixed it in Settings → Agents and came back. This device
   * only, like the load's own copies of these two. */
  const refreshAgentState = () => {
    if (deviceRef.current) return;
    const here = () => !deviceRef.current;
    deviceApi("")<{ logins?: Array<{ name: string; logged_in: boolean | null }> }>("/api/setup")
      .then((st) => {
        if (!here()) return;
        const out: Record<string, boolean> = {};
        for (const l of st.logins || []) if (l.logged_in === false) out[l.name] = true;
        setSignedOut(out);
      })
      .catch(() => {});
    deviceApi("")<{ providers?: Array<{ name: string; installed?: boolean }> }>("/api/providers/status")
      .then((d) => {
        if (!here()) return;
        const have: InstalledMap = {};
        for (const x of d.providers || []) if (typeof x.installed === "boolean") have[x.name] = x.installed;
        setInstalled(have);
      })
      .catch(() => {});
  };

  /** Load what the form offers from the device the session will live on:
   * folder suggestions, config (home, default agent), launch-flag defaults,
   * templates and the agent list. Returns the cleanup that silences answers
   * nobody is waiting for any more. */
  const loadFor = (dev: string) => {
    const dapi = deviceApi(dev);
    let live = true;
    loadDone.current = false;
    const here = () => live && deviceRef.current === dev;
    // The folder suggestions get a request of their own rather than a place in
    // the barrier below, because the endpoint walks the filesystem: hundreds of
    // listdir/stat probes under $HOME plus a git rev-parse per surviving
    // candidate, which is milliseconds warm and seconds on a cold or
    // network-mounted home. Behind the barrier that walk would hold the agent
    // list, the launch flags and the templates hostage to it as well.
    (async () => {
      try {
        const d = await dapi<{ suggestions?: RepoSuggestion[] }>("/api/repos/suggest");
        if (!here()) return;
        const sug = d.suggestions || [];
        setSuggestions(sug);
        // Start on the best-ranked one — normally the folder the last session
        // used — so the common case needs no Browse trip at all. Unless the user
        // has already said where they want to work: see folderReducer.
        folderDo({ t: "suggested", path: sug[0]?.path || "" });
      } catch {
        // Suggestions are sugar. The field, the browser and Create all work
        // without them, so a failed walk must not cost the dialog anything.
        if (here()) setSuggestions([]);
      }
    })();
    // Which agents are signed in — this device only, and never in the way:
    // its own request, a note under the Agent field when one is not.
    if (!dev)
      (async () => {
        try {
          const st = await dapi<{ logins?: Array<{ name: string; logged_in: boolean | null }> }>("/api/setup");
          if (!here()) return;
          const out: Record<string, boolean> = {};
          for (const l of st.logins || []) if (l.logged_in === false) out[l.name] = true;
          setSignedOut(out);
        } catch {
          if (here()) setSignedOut({});
        }
      })();
    (async () => {
      // Config, settings and templates all answer out of memory, so waiting for
      // the slowest of the three costs the dialog nothing.
      const [cfgR, setR, tplR] = await Promise.allSettled([
        dev
          ? dapi<Config>("/api/config")
          : refreshConfig().then(() => queryClient.getQueryData<Config>(["config"])),
        dapi<{ settings?: { coding_cli?: { default_launch_args?: Record<string, string> } } }>(
          "/api/settings"
        ),
        // This device's list is the shared cache (one fetch, then the one-time
        // saved-prompt migration); another device's is its own.
        dev
          ? dapi<{ templates?: Template[] }>("/api/templates").then((d) => d.templates || [])
          : ensureTemplates(true),
      ]);
      if (!here()) return;
      const cfg = cfgR.status === "fulfilled" ? cfgR.value : undefined;
      setHomePath(cfg?.home || "");
      // $HOME is the fallback it always was: almost never what the user wants,
      // but a place the browser can start from when the suggestions had nothing
      // to offer (or failed, or are still walking).
      folderDo({ t: "fallback", path: cfg?.home || "" });
      setProvisioningAvailable(!!cfg?.provisioning_available);
      let provs: Provider[] = [];
      let have: InstalledMap | null = null;
      const [manageR, statusR] = await Promise.allSettled([
        dapi<{ providers?: Provider[] }>("/api/providers/manage"),
        // Which agents are installed — this device only: another device's
        // agents are its own business, and unknown never blocks.
        dev
          ? Promise.reject(new Error("remote"))
          : dapi<{ providers?: Array<{ name: string; installed?: boolean }> }>("/api/providers/status"),
      ]);
      if (manageR.status === "fulfilled") provs = manageR.value.providers || [];
      if (statusR.status === "fulfilled") {
        have = {};
        for (const p of statusR.value.providers || [])
          if (typeof p.installed === "boolean") have[p.name] = p.installed;
      }
      if (!here()) return;
      setProviders(provs);
      setInstalled(have);
      // Map the saved default (name / alias / raw command) to the provider NAME.
      const prev = cfg?.default_program || "";
      const lower = prev.toLowerCase();
      const match = provs.find(
        (p) =>
          (p.name || "").toLowerCase() === lower ||
          (p.aliases || []).some((a) => String(a).toLowerCase() === lower) ||
          String(p.command || "").toLowerCase() === lower
      );
      const agent = preferInstalled(
        match ? match.name : prev,
        provs.map((p) => p.name),
        have
      );
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
      const tpls = tplR.status === "fulfilled" ? tplR.value || [] : [];
      setTemplates(tpls);
      // The fast path's pre-fill: the template, agent and account the last
      // session in this folder was started with (lib/store, per browser).
      // After the defaults above, so a remembered agent outranks the config's
      // default — and only for this device: another device's agents and
      // accounts are its own.
      if (!dev) applyLastUsed.current(tpls, provs);
      loadDone.current = true;
    })();
    return () => {
      // Answers for a dialog that has since been closed (or closed and reopened)
      // have nothing to say about this opening, and the suggestion walk is slow
      // enough to still be running when that happens.
      live = false;
    };
  };

  /** Empty the form for a fresh start and load what it offers: every
   * opening that keeps nothing, and Clear. `prefill` is Intake's "Start
   * together…" list (a batch), "" otherwise. */
  const resetForm = (prefill: string) => {
    newSessionResume.keep = false;
    newSessionResume.prompt = undefined;
    draftSpent.current = false;
    carried.current = "";
    prefilledPrompt.current = "";
    openedFocus.current = "describe";
    // Back to Session on every open, for the reason the tab state's own
    // comment gives: Alt+N is a shortcut for starting work, and a shortcut
    // that lands somewhere different depending on what you last looked at is
    // not one. It also rides in this reset rather than on its own effect so
    // the failed-reopen guard above covers it too — a refused create must not
    // throw the user back to a tab they had left.
    setTab("session");
    setTitle("");
    setError("");
    setPrompt("");
    setLaunchArgs("");
    // The Describe box and everything it leaves behind. Each of these is here
    // because a useState without a reset line leaks from one opening to the
    // next (`strategy` and `continueOn` did, until they got theirs) — and a leaked note is
    // worse than a leaked field: it is a sentence about a folder the form is no
    // longer showing.
    // Intake's "Start together…" hands its ticked tickets over as the box's
    // text, once, and that opening is a batch; every other starts empty.
    setDescribe(prefill);
    setBatch(!!prefill);
    setBaseBranch("");
    // ...and what goes with the base branch: "continue on it" and how the
    // workspace is copied were the two that still leaked.
    setContinueOn("");
    setStrategy("worktree");
    setBranches(null);
    setDescribing(false);
    setDescribeSlow(false);
    setDescribeFailed(false);
    setSaveTplName(null);
    setSaveTplBusy(false);
    setPlanNote("");
    setPlanError("");
    // The tick, and ONLY the tick. Last opening's "yes, make that folder" is
    // consent to one sentence's folder and must never carry over, so it dies
    // here — but the question it answered does not, because the field the
    // question is about survives: the folder reducer's "reopen" deliberately
    // KEEPS folder.path. Clearing the plan alongside the tick is what disarmed
    // the gate over a Folder field still holding the model's not-yet-existing
    // folder, and Create then made a directory nobody confirmed. See
    // planFolderReducer's "reopen" for the asymmetry and why keeping it is
    // safe: the gate is derived from the live field, so a retained plan
    // self-clears the moment that field moves off it.
    // A fresh opening starts at the sentence (a batch at its list). An
    // opening that keeps a typed draft keeps its page too (see openingMode).
    openedPage.current = 1;
    setPage(1);
    setNewFolderOk(false);
    planFolderDo({ t: "reopen" });
    // An answer for last opening's sentence has nothing to say about this one,
    // and a model turn is slow enough to still be running when the dialog is
    // closed and reopened. Bumping the seq is what makes the late one a no-op;
    // the abort is the courtesy of not waiting for it.
    cancelPlanRun(planRun.current);
    // Nothing has been filled in yet, so nothing is holding Create — a hold
    // carried over from last opening would refuse the first Enter of this one.
    submitArmAt.current = 0;
    setProvision(false);
    setInPlace(false);
    setInitRepo(false);
    setPlanFirst(false);
    setSplit(false);
    setMaxPieces(SPLIT_DEFAULT);
    // "Git & workspace" starts SHUT, and its summary line says what it holds
    // (where the work happens, and the base branch), so the choice is read
    // without a click and changed with one. Set here, not only in useState:
    // this reset runs on every fresh open.
    setAdvancedOpen(false);
    setLaunchOpen(false);
    setDeepOpen(false);
    setMkBusy(false);
    setMkError("");
    folderDo({ t: "reopen" });
    // Last opening's matches answered last opening's query, and the reducer's
    // "reopen" keeps the folder itself — so the list would come back up over a
    // field nobody has typed into yet.
    setSearch(null);
    setSearchOpen(true);
    setSearchSel(0);
    setActiveTemplate("");
    setPresetValue("");
    setProfileId("");
    setProfileModel("");
    // Refetched per open: the dialog outlives the page's whole session, and a
    // rotated OpenRouter key must not keep offering the old key's catalog.
    setProfileModels({});
    setSavedPresets(loadUserPresets());
    loadedDevice.current = deviceRef.current;
    loadCleanup.current?.();
    loadCleanup.current = loadFor(deviceRef.current);
  };

  // What each opening does with the form (openingMode): a failed create's
  // own reopen and a hand-back from Templates/Prompts keep it exactly; so
  // does any opening while something TYPED is still in it — closing New,
  // or a trip to Intake or Settings, never throws a sentence away. Only a
  // Create, Clear or Intake's "Start together…" starts it fresh.
  //
  // The failed-create path arrives here a tick after `submit` set the error,
  // so a reset would wipe the only report of the failure the user ever gets,
  // along with the name, the prompt and the Provision tick they would have
  // had to retype. Nothing needs redoing on the keeping paths either: the
  // component never unmounts, so every list the open loaded is still in state.
  useEffect(() => {
    if (!open) {
      // A CLOSE cancels the plan too, and nothing used to do it. The reset
      // below was the only thing that bumped the seq, and it runs on OPEN — so
      // closing the dialog registered no cleanup at all, and since this
      // component never unmounts the answer landed ~15s later with its
      // staleness check still passing, writing the model's folder into a form
      // that was not on screen. Cancel first, return second.
      cancelPlanRun(planRun.current);
      loadCleanup.current?.();
      loadCleanup.current = null;
      return;
    }
    if (failedReopen.current) {
      failedReopen.current = false;
      newSessionResume.keep = false;
      // failCreate has already put the form (page 2) back on screen. Name
      // lives under More options, so open it for the caret to land there.
      openedPage.current = 2;
      openedFocus.current = "title";
      setAdvancedOpen(true);
      refreshAgentState();
      return;
    }
    const prefill = useUi.getState().takeNewPrefill();
    const mode = openingMode({
      failedReopen: false,
      resume: newSessionResume.keep,
      prefill: !!prefill,
      draft: hasTypedDraft({ describe, title, prompt, prefilledPrompt: prefilledPrompt.current }),
      spent: draftSpent.current,
    });
    if (mode === "resume") {
      // The form exactly as it was left, with the lists it reads fetched
      // again (Templates/Prompts are where they get edited). A prompt picked
      // in Prompts (New → Manage…) lands in the Task box.
      newSessionResume.keep = false;
      const picked = newSessionResume.prompt;
      newSessionResume.prompt = undefined;
      if (picked !== undefined) {
        setPrompt(picked);
        setPresetValue("");
      }
      openedPage.current = page;
      openedFocus.current = page === 2 ? "title" : "describe";
      setTab("session");
      // The close cancelled any read in flight; its spinner goes with it.
      setDescribing(false);
      setDescribeSlow(false);
      setSaveTplName(null);
      setSavedPresets(loadUserPresets());
      if (!loadDone.current) {
        // Closed before its first load landed: load now (it never writes
        // over what was typed — see applyLastUsed).
        loadCleanup.current = loadFor(deviceRef.current);
        return;
      }
      if (!deviceRef.current)
        void ensureTemplates(true)
          .then((list) => setTemplates(list))
          .catch(() => {});
      refreshAgentState();
      return;
    }
    resetForm(prefill);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // The saved-prompt list is the templates list: repaint it when the fetch
  // lands or anything (this dialog, the Templates dialog, a sync pull)
  // changes it while the form is open.
  useEffect(() => {
    if (!open) return;
    const reload = () => {
      setSavedPresets(loadUserPresets());
      // The strip too (Save as template…, the Templates dialog, a sync pull)
      // — for this device's list; another device's is its own.
      if (!deviceRef.current) setTemplates(templatesNow());
    };
    document.addEventListener(PRESETS_CHANGED, reload);
    return () => document.removeEventListener(PRESETS_CHANGED, reload);
  }, [open]);

  // A different device picked while the dialog is open: its folders, its
  // agents, its defaults. The opening's own load (above) covers the device
  // the dialog opened on, which is what loadedDevice records.
  useEffect(() => {
    if (!open || loadedDevice.current === device) return;
    loadedDevice.current = device;
    folderDo({ t: "device" });
    setSuggestions([]);
    setSearch(null);
    setFolderCheck(null);
    // Accounts are this device's settings; the other device has its own.
    setProfileId("");
    setProfileModel("");
    return loadFor(device);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, device]);

  // The opening focus belongs to the opening itself, not to the back of the
  // loads above: the dialog paints and is typeable the moment `open` flips, and
  // a focus() that waits for a filesystem walk lands seconds later, yanking the
  // caret out of the Prompt box mid-sentence so the rest of the sentence goes
  // into Name. Even at this range something can get there first — a click that
  // beats this effect's commit — and whatever did keeps the caret.
  useEffect(() => {
    if (!open) return;
    // Page 1's sentence box — or Name after a failed create. Only once this
    // render is showing the page the opening asked for: an opening that
    // focused nothing would leave the caret wherever the app last had it,
    // outside the modal.
    const toTitle = openedFocus.current === "title";
    const shown = page === openedPage.current;
    const el = shown ? (toTitle ? titleRef.current : describeRef.current) : null;
    const active = document.activeElement;
    if (!el) {
      // Not on screen yet (the previous opening ended on page 2, which is what
      // this render still shows): focus it once it is.
      focusOpeningNext.current = true;
      return;
    }
    if (
      mayTakeOpeningFocus({
        activeIsTarget: active === el,
        activeInsideDialog: !!active && !!rootRef.current?.contains(active),
      })
    )
      el.focus();
    // `page` is deliberately NOT a dependency: this is the OPENING's focus, and
    // re-running it on every page change would fight the two focus moves that
    // belong to the flow itself — the Folder field after a fill, and whatever
    // the user had clicked before pressing Back.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useFolderLookups({
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
  });

  const setAgent = useCallback((value: string) => {
    const v = (value || "").trim();
    setProgram(v);
    // Switching agents resets the flags to that provider's saved default.
    setLaunchArgs((launchDefaults.current[v.toLowerCase()] || "").trim());
  }, []);

  /** The canonical provider name behind whatever is in the Agent field —
   * mirrors the backend's resolve(): basename of the executable token,
   * matched against each provider's name/aliases/command. Without this a
   * path ("/usr/local/bin/claude") or an alias ("agy") the backend routes
   * fine would trip the no-route warning and get auto-overwritten. */
  const canonAgent = useCallback(
    (raw: string): string => {
      const tok = (raw.trim().split(/\s+/)[0] || "").toLowerCase();
      const base = tok.split("/").pop() || tok;
      const m = providers.find(
        (p) =>
          p.name.toLowerCase() === base ||
          (p.aliases || []).some((a) => String(a).toLowerCase() === base) ||
          String(p.command || "").toLowerCase() === base
      );
      return m ? m.name : base;
    },
    [providers]
  );

  /** Plan first needs a CLI that can declare a plan the Map shows with a Go
   * button (Claude). Any other CLI would list its files and then wait on a Go
   * the UI can't send, so the option is off for it. An unknown agent (or an
   * older server without the field) keeps the option. */
  const planOk = useMemo(() => {
    const name = canonAgent(program);
    const prov = providers.find((p) => p.name === name);
    return !prov || prov.plan_supported !== false;
  }, [canonAgent, program, providers]);

  /** Split across workers needs an agent that gets the MindFlock tools. The
   * cap missing (an older server, config not loaded yet) is "unknown", not
   * "off": the box stays usable and the server's 400 is the backstop. */
  const { data: config } = useConfig();
  const mcpOk = useMemo(
    () =>
      device
        ? { ok: false, reason: "Auto-split only starts sessions on this device" }
        : teamRunCaps(config?.caps).split
          ? splitGate(config?.caps, canonAgent(program))
          : { ok: false, reason: SERVER_NO_SPLIT },
    [config?.caps, canonAgent, program, device]
  );
  const togetherOk = teamRunCaps(config?.caps).together;
  const splitLimit = teamRunCaps(config?.caps).maxPieces;
  /** The Ticket tab files a ticket in a connected tracker, so with no tracker
   * there is nothing behind it: the strip is not drawn at all, and a dialog
   * somehow left on Ticket (the tracker disconnected mid-open) shows Session.
   * A two-tab strip whose second tab can only fail is noise on the one dialog
   * every new user opens first. */
  const ticketingOk = !!config?.caps?.ticketing;
  const shownTab: NewTab = ticketingOk ? tab : "session";

  /** A batch's box read as a list, and every choice about how the things in
   * it ship. Any other box is one prompt: unticked, the single-session flow
   * below runs exactly as it always has. */
  const draft = useRunDraft({
    open,
    // Page 1's box is a batch's list; on the form, the Prompt is what an
    // auto-split's lead is given.
    text: page === 1 ? describe : prompt,
    repoPath,
    program: canonAgent(program),
    // What a BATCH starts on (Settings → Workspace; Off when unset). Never
    // the resolved `fasttrack_depth`, which reads "pr" when nothing is set.
    fasttrackDefault: config?.fasttrack_default,
    // The "Set it up myself" form makes ONE session: it starts Off.
    single: page !== 1,
    batch: page === 1 && batch,
    split: page === 2 && split && mcpOk.ok,
    togetherOk,
  });
  /** An auto-split run: ticked on the form (page 2) with an agent that has
   * the tools. Page 1 is only the prompt — the owner's rule: "I should be
   * able to just prompt and it fills out the second menu" — so the box, like
   * Fast-track, lives with the rest of the details. The Prompt is the task. */
  const splitOn = page === 2 && split && mcpOk.ok;
  /** Create starts a RUN (POST /api/runs) rather than one session: a batch
   * (Intake's tickets, page 1) or an auto-split (the form). */
  const runMode = (page === 1 && draft.listMode) || splitOn;
  /** What a ticked Auto-split will do, in a sentence, under the box. */
  const splitSum = splitOn
    ? summarySentence({
        n: 1,
        concurrency: draft.concurrency,
        lane: draft.lane,
        askFirst: draft.askFirst,
        grouping: draft.grouping,
        split: true,
        maxPieces: clampSplit(maxPieces, splitLimit),
      })
    : null;
  const toggleSplit = (on: boolean) => setSplit(on);
  useEffect(() => {
    if (!open) setRunBrowse(false);
  }, [open]);
  const laneAtPress = useRef<{ lane: Lane; askFirst: boolean }>({ lane: "leave", askFirst: false });
  laneAtPress.current = { lane: draft.lane, askFirst: draft.askFirst };
  // Fast-track COMMITS for this session: it never drives a checkout other
  // sessions share (two in-place sessions on main had one approval card, and
  // approving it committed both sessions' files). With fast-track on, the
  // session gets its own worktree.
  const laneNeedsWorktree = draft.lane !== "leave";

  /** Picking an account steers the Agent field: with an OpenRouter (or any
   * key) account the identity is the choice that matters, so an agent the
   * account can't route is auto-swapped to one it can — the alternative is a
   * session that silently launches on the CLI's own login. A profile with raw
   * env overrides applies to every CLI, so it never steers. */
  const setAccount = accountSetter({
    authProfiles,
    program,
    profileModels,
    canonAgent,
    setAgent,
    setProfileId,
    setProfileModel,
    setProfileModels,
  });

  /** Put a template into the form. `quiet` is the opening's own pre-fill
   * (the last-used template): the fields change, nothing else does — no
   * caret move, no fold opened, no title written over the one the person is
   * about to type. A click on a chip is the loud version. */
  const fillFromTemplate = templateFiller({
    setAgent,
    folderDo,
    setPrompt,
    setProvision,
    setStrategy,
    setInPlace,
    setInitRepo,
    setActiveTemplate,
    setAdvancedOpen,
    setLane: draft.setLane,
    setAskFirst: draft.setAskFirst,
  });

  /** The opening's pre-fill from the last session started in this folder:
   * its template (quietly), its agent (if still installed) and its account.
   * Through a ref because loadFor, which calls it once the lists have
   * landed, is declared above the setters this needs. */
  const applyLastUsed = useRef<(tpls: Template[], provs: Provider[]) => void>(() => {});
  applyLastUsed.current = (tpls, provs) => {
    const { choice } = lastNewSessionChoice(repoPath);
    if (!choice) return;
    // A kept draft's late load: what was typed outranks the memory.
    if (prompt.trim() || title.trim()) return;
    const t = choice.template ? tpls.find((x) => x.name === choice.template) : null;
    if (t) {
      fillFromTemplate(t, { quiet: true });
      prefilledPrompt.current = t.prompt || "";
    }
    if (choice.program && provs.some((p) => p.name === choice.program)) setAgent(choice.program);
    // Carry the agent being applied this tick: setAccount would otherwise read
    // the previous render's `program` and could swap the remembered agent away.
    pendingProfile.current = choice.profile_id
      ? { repo: repoPath, id: choice.profile_id, program: choice.program || undefined }
      : null;
    settleProfile.current();
  };

  /** Apply a remembered account only while it still exists — through
   * setAccount, like a pick — and forget one that was removed. */
  const settleProfile = useRef<() => void>(() => {});
  settleProfile.current = () => {
    const pend = pendingProfile.current;
    if (!pend) return;
    const verdict = rememberedProfileVerdict(pend.id, authProfiles ? authProfiles.profiles || [] : null);
    if (verdict === "wait") return;
    pendingProfile.current = null;
    if (verdict === "apply") setAccount(pend.id, pend.program);
    else if (verdict === "drop") forgetNewSessionProfile(pend.repo);
  };
  useEffect(() => {
    settleProfile.current();
  }, [authProfiles]);
  useDescribeFocus({
    open,
    page,
    focusFolderNext,
    focusOpeningNext,
    openedFocus,
    openedPage,
    repoRef,
    titleRef,
    describeRef,
  });

  // Base branch menu data for the folder in the field: see useBaseBranches.
  useBaseBranches({ open, page, repoPath, dapi, setBaseBranch, setBranches });

  /** The git nudge's action drives the real "Create a git repo in this folder"
   * checkbox instead of a flag of its own — two switches for one behaviour is
   * how a submitted form ends up disagreeing with what the user was shown. The
   * fold is opened at the same time so the box it just ticked is visible and
   * untickable, rather than changing state out of sight.
   *
   * It pointedly does NOT touch "work directly in this folder" any more. It used
   * to have to: the two boxes disabled each other, so arming one meant clearing
   * the other or leaving the form in a state its own controls forbade. They are
   * combinable now — `git init` here and then work here is the ordinary reading
   * of both boxes together, and the server does exactly that — so silently
   * turning in-place off would be the nudge changing a mode nobody asked it to
   * change. */
  const armInitRepo = () => {
    setInitRepo(true);
    setAdvancedOpen(true);
  };

  // Live sessions, for "is anyone already editing this folder directly?",
  // "who else works in this repo?" and the names a new one can't take.
  const { data: liveRows } = useInstances();
  const aliases = useUi((s) => s.aliases);

  // The Templates dialog is this dialog's manager (New → Manage… hands over
  // to it and it hands back), so it rides here, in the one component that is
  // always mounted — the two never show at once, and nothing else has to
  // know it exists.
  if (!open) return <TemplatesDialog />;

  const folderPath = repoPath.trim();
  const offerProvision = provisioningAvailable || !!folderPath;
  // Only an answer about the path now in the field earns a line on screen; a
  // reply for an older path is stale by definition, whether it arrived late or
  // is simply what we last learned before the current keystrokes.
  const plainFolder = folderCheck?.asked === folderPath && !!folderCheck?.plain;
  // The matches that are actually on screen: only for the text now in the field
  // (an answer to an older query is stale by definition), only while the user
  // hasn't dismissed them, and never underneath the folder browser — that is a
  // folder picker too, and two of them stacked under one field, both answering
  // Escape, is one picker too many.
  const searchHits =
    search && search.asked === folderPath && searchOpen && !browserOpen ? search : null;
  // Clamped rather than stored clamped, because the list it indexes into is
  // replaced whole every time an answer lands. -1 when there is nothing to
  // highlight, which is also what tells Enter to fall through to the form.
  const selIndex =
    searchHits && searchHits.matches.length
      ? Math.min(searchSel, searchHits.matches.length - 1)
      : -1;
  // Provisioning and working in place are the two that cannot both be true —
  // provisioning builds a separate worktree or clone, so there is no "this
  // folder" left to work in, and the server enforces exactly that
  // (`in_place = … and not is_provisioned`). Read through offerProvision so a
  // provision box that is not on screen cannot disable a box that is: an
  // in-place checkbox greyed out by an invisible control is unfixable from the
  // dialog.
  const provisionOn = offerProvision && provision;
  /** A plain new worktree: the one mode a Base branch applies to. */
  const worktreeMode = (!inPlace || laneNeedsWorktree) && !provisionOn;
  /** The Base branch pick is a remote-tracking name: the one that offers
   * "Work on x itself". */
  const baseIsRemote = !!(branches?.branches || []).find((b) => b.name === baseBranch)?.remote;
  /** "Work on x itself" is offered for origin's branches only: push, the
   * divergence check and the rebase all go through origin. */
  const canContinue = baseIsRemote && baseBranch.startsWith("origin/");
  /** The folder in the field is a git repo (as the server last said). */
  const isGit = folderCheck?.asked === folderPath && !!folderCheck?.isGit;
  /** Sessions on this device already editing the folder directly. */
  const sharers = device
    ? []
    : sessionsInFolder(liveRows || [], [folderPath, folderCheck?.asked === folderPath ? folderCheck.resolved || "" : ""]);
  const continueBranch =
    !!baseBranch && continueOn === baseBranch && canContinue ? remoteBranchName(baseBranch) : "";
  /** What Create will call the session — numbered past the names already
   * taken, as the server does — and the branch it cuts. */
  const planned = plannedName({
    prefix: (folderCheck?.asked === folderPath && folderCheck?.branchPrefix) || "",
    title,
    prompt,
    taken: device ? new Set<string>() : takenNames(liveRows || [], aliases),
    folder: folderPath,
  });
  const branchKnown = isGit && folderCheck?.branchPrefix !== undefined;
  /** Sessions on this device already working in this repo, any way. */
  const others = device
    ? []
    : sessionsInRepo(liveRows || [], [folderPath, folderCheck?.asked === folderPath ? folderCheck.resolved || "" : ""]);
  const baseLabel = baseBranch || branches?.current || "";
  /** Why Create must not launch the agent in the field ("" = go ahead). */
  const agentMissing = device ? "" : agentMissingReason(canonAgent(program), installed);
  /** Installed but known not signed in: it would stop at its sign-in. */
  const agentSignedOut = !device && !agentMissing && !!signedOut[canonAgent(program)];
  /** More options' summary line: what in it is NOT the default. */
  const moreBits: string[] = [];
  if (title.trim()) moreBits.push("named " + title.trim());
  if (draft.lane !== "leave") moreBits.push("Fast-track: " + LANE_LABEL[draft.lane]);
  if (splitOn) moreBits.push("auto-split");
  if (provisionOn) moreBits.push("provisioned");
  if (baseBranch) moreBits.push("from " + baseBranch);
  if (planFirst && planOk) moreBits.push("plan first");
  if (launchArgs.trim()) moreBits.push("flags");
  const moreSummary = moreBits.length ? moreBits.join(" · ") : "name, Fast-track, save as template";
  const gitOn = config?.caps?.git !== false;
  /** Open PR / Merge can be done for you (GitHub connected) — another
   * device's session is that device's business, so never greyed there. */
  const prOk = !!device || hasPrSupport(config?.caps);
  const prWhy = prNeedsGithubReason(PR_FALLBACK_HINT);
  /** Why a Fast-track rung can't be picked here ("" = it can). */
  const laneWhy = (lane: string): string =>
    plainFolder && !initRepo && lane !== "leave"
      ? "No git repo here, so nothing to commit — pick “Make it a git repo first” to use Fast-track"
      : splitOn && lane === "leave"
      ? "An auto-split commits each piece into its branch — Commit keeps it all on this machine"
      : !prOk && (lane === "pr" || lane === "merge")
      ? prWhy
      : "";
  /** Why "Ask me before it ships" can't be used ("" = it can). */
  const askWhy = splitOn
    ? "An auto-split always asks you before anything leaves this machine"
    : askFirstApplies(draft.lane)
    ? ""
    : "Fast-track is off: nothing is committed, pushed or merged for you";
  const deepSummary = advancedSummary([
    ["ask me first", gitOn],
    ["auto-split", gitOn && mcpOk.reason !== SERVER_NO_SPLIT],
    ["provisioning", gitOn && offerProvision],
    // Only while the picker is (or is about to be) drawn: a repo whose one
    // branch is the checked-out one has nothing to pick.
    ["base branch", gitOn && worktreeMode && isGit && (branches === null || (!!branches.is_git && branches.branches.length > 0))],
    ["plan first", true],
    ["launch flags", true],
  ]);
  // Computed once for both readers: the aside inside the Git & workspace fold,
  // and submit's refusal to post a create the server is certain to refuse.
  const provisionBlocked = provisionBlockReason({
    provision: provisionOn,
    plainFolder,
    initRepo,
    folderPath,
  });
  // Why the selected "New worktree" will not survive the create, or "" when it
  // will. Derived beside provisionBlocked because it is the same kind of fact
  // about the same folder, and because the radio above it is now a positive
  // claim that has to be honest.
  const worktreeClamped = worktreeClampReason({
    inPlace,
    provisionOn,
    plainFolder,
    initRepo,
  });
  // Recomputed from the Folder field on every render rather than remembered, so
  // there is no state to forget to clear: see newFolderGate for the six writers
  // that would each have had to remember. Both readers come off the one value —
  // the confirm row (newFolderRow, below), and submit's refusal.
  const newFolderAsk = newFolderGate({
    // planGatePath answers the "is it already there" half, so the gate itself
    // only ever compares paths — one place for that question, so the confirm
    // row and submit's refusal cannot come to different conclusions about it.
    planPath: planGatePath(planFolder),
    planDisplay: planFolder.display,
    folderPath,
  });
  const newFolderBlocked = newFolderBlockReason({
    gate: newFolderAsk,
    confirmed: newFolderOk,
  });
  // The note is derived from the same fact as the gate above, and for the same
  // reason: nothing cleared it when the Folder field moved off the plan's path,
  // so a suggestion chip clicked after a `new:` plan left a muted line still
  // saying "Using ~/code/invoice-parser — a new folder" over a form whose
  // field, git nudge and Create had all moved on to somewhere else.
  const planNoteShown = planNoteFor({
    note: planNote,
    planPath: planFolder.path,
    folderPath,
  });

  const pickMatch = matchPicker(folderDo, setSearchOpen);

  /** The Folder field names a path that is not there (the server's check, for
   * the text now in the field). Start stays off until "Create this folder"
   * makes it — a mistyped path must never quietly become a new repo. */
  const folderMissing = folderCheck?.asked === folderPath && !!folderCheck?.missing;
  const missingLabel = homeRelative(
    (folderCheck?.asked === folderPath && folderCheck?.resolved) || folderPath,
    homePath
  );
  /** A plan's proposed folder still waiting to be made reads the same way. */
  const askFolder = newFolderAsk && !newFolderOk ? newFolderAsk : folderMissing ? missingLabel : "";
  const startBlocked =
    askFolder && !runMode
      ? "No folder at " + askFolder + " — press “Create this folder”, or pick a folder that already exists."
      : "";
  const createFolder = async () => {
    const full = ((folderCheck?.asked === folderPath && folderCheck?.resolved) || folderPath).replace(/\/+$/, "");
    const cut = full.lastIndexOf("/");
    const parent = cut > 0 ? full.slice(0, cut) : "/";
    const name = full.slice(cut + 1);
    if (!name || mkBusy) return;
    setMkBusy(true);
    setMkError("");
    try {
      const r = await dapi<{ path: string }>("/api/mkdir", { json: { path: parent, name } });
      // Exactly that folder now exists, empty and without git: the form reads
      // it as one (the non-git question appears), and a plan that proposed it
      // has had its yes.
      setFolderCheck({ asked: folderPath, plain: true, isGit: false, missing: false, resolved: r.path || full });
      if (newFolderAsk) setNewFolderOk(true);
      setError("");
      setPlanError("");
      // The button that had the caret is gone with the question: keep the
      // caret in the dialog (Esc, Enter) — on the field it was about, or on
      // page 1's sentence box.
      (page === 2 ? repoRef.current : describeRef.current)?.focus({ preventScroll: true });
    } catch (err) {
      const msg = errMsg(err);
      setMkError(
        /not a directory/i.test(msg)
          ? "The folder it would go in doesn't exist either — use Browse… to pick a place."
          : /already exists/i.test(msg)
          ? "Something is already there — pick it with Browse…."
          : "Couldn't create it: " + msg
      );
    } finally {
      setMkBusy(false);
    }
  };
  const missingRow = askFolder ? (
    <MissingFolderRow label={askFolder} onCreate={() => void createFolder()} busy={mkBusy} error={mkError} />
  ) : null;

  const {
    buildBody,
    postCreate: postCreateRaw,
    startRun,
    submit: submitRaw,
  } = newSessionSubmit({
    title,
    repoPath,
    prompt,
    program,
    planFirst,
    planOk,
    launchArgs,
    profileId,
    profileModel,
    strategy,
    inPlace,
    initRepo,
    provision,
    baseBranch,
    baseIsRemote,
    continueOn,
    activeTemplate,
    maxPieces,
    splitLimit,
    runMode,
    splitOn,
    laneNeedsWorktree,
    newFolderBlocked,
    provisionBlocked,
    draft,
    laneAtPress,
    deviceRef,
    failedReopen,
    submitArmAt,
    setError,
    setPlanError,
    setPromptOpen,
    setPage,
    planFolderDo,
    // A create (or a run's start) closes the dialog: the draft it was made
    // from is spent, so the next opening starts fresh.
    closeDialog: () => {
      draftSpent.current = true;
      closeDialog();
    },
  });
  // Never launch an agent this device doesn't have: the session would sit
  // "Idle" on "command not found". Said where the button was pressed.
  const submit = () => {
    if (startBlocked) {
      setError(startBlocked);
      return;
    }
    if (agentMissing && !runMode) {
      // The why is already under the Agent field; this says it was Create
      // that refused.
      setError("Can't start yet — pick an agent that's installed.");
      return;
    }
    void submitRaw();
  };
  const postCreate = async (body: Record<string, unknown>) => {
    if (agentMissing) {
      setPlanError(agentMissing);
      return;
    }
    await postCreateRaw(body);
  };

  /** Save as template…: the whole form, under the typed name. */
  // Another template's name is refused, not saved over: the server upserts
  // by name, so it would silently replace that one.
  const saveTplBlocked = !(saveTplName || "").trim()
    ? "Give it a name."
    : templateNameTaken(saveTplName || "", device ? templates : templatesNow()) ||
      (!folderPath && !prompt.trim() ? "Pick a folder or write a task first." : "");
  const saveAsTemplate = async () => {
    if (saveTplBlocked || saveTplName === null) return;
    const body = templateFromForm(
      {
        name: saveTplName,
        program: canonAgent(program),
        repoPath: folderPath,
        prompt,
        inPlace: inPlace && !laneNeedsWorktree,
        provision: provisionOn,
        strategy,
        initRepo,
        lane: draft.lane,
        askFirst: draft.askFirst,
      },
      device ? templates : templatesNow()
    );
    setSaveTplBusy(true);
    try {
      if (device) {
        const d = await dapi<{ templates?: Template[] }>("/api/templates", { json: body });
        setTemplates(d.templates || []);
      } else {
        setTemplates(await saveTemplate(body as unknown as Template));
      }
      setActiveTemplate(String(body.name));
      setSaveTplName(null);
      // Points at the strip it just appeared in (▶ / ⏰ on its chip); a click
      // opens Templates itself. Up for 6s — a link needs time to be read.
      toast(
        `Saved template “${body.name}” — ▶ on its chip at the top of New runs it, ⏰ schedules it. Click to open Schedule.`,
        {
          onClick: () =>
            useUi.getState().openDialogFor("templates", templatesTarget({ fromNew: true, schedule: null })),
        }
      );
    } catch (err) {
      setError("Couldn't save the template: " + errMsg(err));
    } finally {
      setSaveTplBusy(false);
    }
  };

  /** Page 1's Create session: start NOW, in the folder the form holds, with
   * the sentence as typed as its task — no model turn. The sentence is also
   * written into the Task box, so any refusal lands on a form that has it. */
  const startRaw = () => {
    const text = describe.trim();
    if (!text) {
      setPlanError("Type what you want to work on first.");
      return;
    }
    cancelPlanRun(planRun.current);
    setDescribing(false);
    setDescribeSlow(false);
    setPlanError("");
    setPrompt(text);
    carried.current = text;
    if (!folderPath) {
      setPage(2);
      setError("Pick a folder for it, then " + START_LABEL + ".");
      return;
    }
    if (startBlocked) {
      setPlanError(startBlocked);
      return;
    }
    if (agentMissing) {
      setPage(2);
      setError("Can't start yet — pick an agent that's installed.");
      return;
    }
    void submitRaw({ prompt: text }).then((sent) => {
      // A guard refused (a name instead of a folder, a folder to confirm…):
      // the form is where that gets fixed.
      if (!sent) setPage(2);
    });
  };

  /** Clear: the explicit way to throw a draft away (Create is the other). */
  const clearDraft = () => {
    resetForm("");
    window.setTimeout(() => describeRef.current?.focus(), 0);
  };
  const draftTyped = hasTypedDraft({ describe, title, prompt, prefilledPrompt: prefilledPrompt.current });

  const { startNow, cancelDescribe, runDescribe, setUpMyself } = describeFlow({
    describe,
    prompt,
    carried,
    startRaw,
    describing,
    newFolderOk,
    dapi,
    planRun,
    lastAnswer,
    submitArmAt,
    focusFolderNext,
    foldOpenedByPlan,
    folderDo,
    planFolderDo,
    setError,
    setTitle,
    setPrompt,
    setInPlace,
    setInitRepo,
    setNewFolderOk,
    setProvision,
    setAdvancedOpen,
    setPromptOpen,
    setPlanError,
    setPlanNote,
    setDescribing,
    setDescribeSlow,
    setDescribeMode,
    setDescribeSince,
    setDescribeFailed,
    setPage,
    buildBody,
    postCreate,
  });

  /** The agent would stop at its own sign-in: said before Create, with the
   * way to fix it. */
  const signedOutNote = (
    <p className="nf-git-nudge nf-agent-missing" id="new-agent-signin" role="status">
      {canonAgent(program)} isn't signed in yet — a session would stop at its sign-in screen.{" "}
      <button
        type="button"
        className="linklike"
        onClick={() => {
          closeDialog();
          openSettings("coding");
        }}
      >
        Sign in…
      </button>
    </p>
  );

  /** What page 1's "Runs in [folder]" line says after the folder: that a
   * worktree session gets a branch of its own, or that it runs in the folder
   * itself. Deliberately NOT the branch's name: that is named from the
   * sentence, so it rewrote itself on every keystroke and made the whole line
   * look like it was reacting to the prompt. The name is in the tooltip. */
  const startsIn = !folderPath ? null : plainFolder ? (
    <span className="nf-starts-sub"> · in the folder itself (no git, so no diff or PR)</span>
  ) : branchKnown ? (
    <span
      className="nf-starts-sub"
      title={
        "The agent works in its own copy on a new branch (" +
        (folderCheck?.branchPrefix || "") +
        "…, named from your task), so your checkout is never touched. You review the diff and open a PR from it."
      }
    >
      {" · on a new branch"}
    </span>
  ) : null;

  /** Page 1's sentence box. */
  const describeProps = {
    batch,
    describe,
    setDescribe,
    describing,
    describeRef,
    runMode,
    runDescribe,
    cancelDescribe,
    draft,
    planNoteShown,
    planError,
    setPlanError,
    describeFailed,
    fillFormInstead: setUpMyself,
    newFolderRow: missingRow,
    runBrowse,
    setRunBrowse,
    repoPath,
    folderDo,
    suggestions,
    device,
    homePath,
    onClear: draftTyped ? clearDraft : undefined,
    agentNote: agentSignedOut ? signedOutNote : null,
    startsIn,
    agentLabel: agentName(canonAgent(program)),
    describeSince,
    startInstead: startRaw,
    onEnterStart: startNow,
  };

  /** The template strip's ▶ and ⏰ (this device's list only — another
   * device's template runs there). ▶ starts it now; New steps aside and
   * keeps what was typed for next time. */
  const runTpl = device
    ? undefined
    : (t: Template) => {
        closeDialog();
        void runTemplateNow(t.name);
      };
  const scheduleTpl = device
    ? undefined
    : (t: Template) =>
        useUi.getState().openDialogFor("templates", templatesTarget({ fromNew: true, schedule: t.name }));
  /** A template's name: fill the form from it (on page 1, then show it). */
  const pickTpl = (t: Template) => {
    fillFromTemplate(t);
    if (page === 1) {
      // Its own task wins; a template without one takes the sentence.
      if (!t.prompt && describe.trim()) {
        setPrompt(describe.trim());
        carried.current = describe.trim();
      }
      setPage(2);
    }
  };

  return (
    <div
      id="new-dialog"
      // Every pane — page 1, the full form and the Ticket tab — hangs from
      // the top-bar dialogs' one fixed top edge at its own natural height
      // (.top-dialog, Modal.css), so neither a tab click nor More options
      // ever moves the card's top, and New opens where Intake, Verify and
      // Schedule do.
      className="modal top-dialog"
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
          // Only the session tab. The Ticket pane owns its own Ctrl+Enter and
          // stops it here, and a fallthrough would submit an empty session form
          // out from under a tab that is not showing one.
          if (shownTab !== "session") return;
          e.preventDefault();
          // Page 1 has no visible form to submit: Start from the sentence in
          // the folder its line shows (see ctrlEnterAction), never an unseen
          // "untitled" session.
          if (ctrlEnterAction({ page, runMode }) === "start") startNow();
          else submit();
        }
      }}
    >
      {shownTab === "ticket" ? (
        /* A sibling of the form, not a branch inside it: nesting a second form
           is invalid HTML, and hanging the ticket pane off #new-form's submit
           would mean its Enter key and its validation belong to a form whose
           fields are not on screen. The two panes share only the head. */
        <div
          id="new-ticket-form"
          className="ta-drop"
          ref={dropZoneRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby="new-dialog-title"
        >
          <NewHead tab={shownTab} tabs={ticketingOk} onTab={setTab} onClose={closeDialog} />
          <NewTicketPane />
        </div>
      ) : (
      <form
        id="new-form"
        ref={dropZoneRef}
        className="ta-drop"
        role="dialog"
        aria-modal="true"
        aria-labelledby="new-dialog-title"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <NewHead tab={shownTab} tabs={ticketingOk} onTab={setTab} onClose={closeDialog} />

        <div className="nf-body">
          {startable.length > 0 && !batch && (
            // Only once a second device is there to pick: with one device
            // there is nothing to choose and nothing worth saying.
            <div id="new-device-row" className="rt-row rt-row-top">
              <span className="rt-label">Runs on</span>
              <div className="rt-ctl">
                <select
                  id="new-device"
                  value={device}
                  onChange={(e) => setDevicePick(e.target.value)}
                  title="The device the session lives on — its folders, its agents, its accounts"
                >
                  <option value="">
                    {devicesResp?.self?.host ? devicesResp.self.host + " (this device)" : "This device"}
                  </option>
                  {startable.map((d) => (
                    <option key={d.device} value={d.device}>
                      {deviceLabel(d, startable, devicesResp?.self?.host || "")}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          )}
          {page === 1 ? (
            <>
              <DescribeSection {...describeProps} />
              {/* The second door, under the first: a saved setup, run now,
                  scheduled, or filled in. Not for a batch (that is a list). */}
              {!batch && (
                <TemplatesStrip
                  landing
                  templates={templates}
                  activeTemplate={activeTemplate}
                  onPick={pickTpl}
                  onRun={runTpl}
                  onSchedule={scheduleTpl}
                />
              )}
            </>
          ) : (
            <>
              <TemplatesStrip
                templates={templates}
                activeTemplate={activeTemplate}
                onPick={pickTpl}
                onRun={runTpl}
                onSchedule={scheduleTpl}
              />
              <p className="nf-tpl-hint muted" id="new-templates-hint">
                Templates save this whole form (folder, agent, task, fast-track) to run again or schedule.
              </p>

              <div className="nf-quick">
                <FolderField
                  repoRef={repoRef}
                  repoPath={repoPath}
                  folderPath={folderPath}
                  folderDo={folderDo}
                  search={search}
                  searchHits={searchHits}
                  searchSel={searchSel}
                  setSearchSel={setSearchSel}
                  setSearchOpen={setSearchOpen}
                  selIndex={selIndex}
                  browserOpen={browserOpen}
                  pickMatch={pickMatch}
                />
                <AgentFields
                  program={program}
                  providers={providers}
                  installed={installed}
                  setAgent={setAgent}
                  device={device}
                  authProfiles={authProfiles}
                  profileId={profileId}
                  pendingProfile={pendingProfile}
                  setAccount={setAccount}
                  closeDialog={closeDialog}
                  foldMissing
                />
              </div>
              {agentMissing && (
                <p className="nf-git-nudge nf-agent-missing" id="new-agent-missing" role="status">
                  {agentMissing}
                </p>
              )}
              {agentSignedOut && signedOutNote}

              <ModelRow
                selectedProfile={selectedProfile}
                profileId={profileId}
                profileModel={profileModel}
                setProfileModel={setProfileModel}
                profileModels={profileModels}
                program={program}
                canonAgent={canonAgent}
              />

              <FolderAids
                searchHits={searchHits}
                selIndex={selIndex}
                searchListRef={searchListRef}
                setSearchSel={setSearchSel}
                pickMatch={pickMatch}
                homePath={homePath}
                newFolderRow={missingRow}
                gitAsked={config?.caps?.git !== false}
                plainFolder={plainFolder}
                initRepo={initRepo}
                armInitRepo={armInitRepo}
                suggestions={suggestions}
                folderPath={folderPath}
                folderDo={folderDo}
                browserOpen={browserOpen}
                device={device}
              />

              <TaskField
                prompt={prompt}
                setPrompt={setPrompt}
                presetValue={presetValue}
                setPresetValue={setPresetValue}
                savedPresets={savedPresets}
              />

              {config?.caps?.git !== false && (
                <WorksIn
                  inPlace={inPlace}
                  setInPlace={setInPlace}
                  setProvision={setProvision}
                  provisionOn={provisionOn}
                  laneNeedsWorktree={laneNeedsWorktree}
                  draft={draft}
                  splitOn={splitOn}
                  setSplit={setSplit}
                  worktreeClamped={worktreeClamped}
                  isGit={isGit}
                  name={branchKnown ? planned : null}
                  prefix={(folderCheck?.branchPrefix as string) || ""}
                  baseLabel={baseLabel}
                  continueBranch={continueBranch}
                  sharers={sharers}
                  others={others}
                  plainFolder={plainFolder}
                  initRepo={initRepo}
                  setInitRepo={setInitRepo}
                />
              )}

              {/* Everything a first session doesn't need: shut by default,
                  with a summary of anything in it that is not the default. */}
              <details
                id="new-advanced"
                className="nf-advanced"
                open={advancedOpen}
                onToggle={(e) => {
                  const el = e.target as HTMLDetailsElement;
                  setAdvancedOpen(el.open);
                  // Opened: bring what it holds on screen, rather than leave
                  // it below the fold of the scrolling body.
                  if (el.open && typeof el.scrollIntoView === "function")
                    el.scrollIntoView({ behavior: "smooth", block: "start" });
                }}
              >
                <summary>
                  More options
                  <span className="nf-fold-sum" id="new-advanced-sum">
                    — {moreSummary}
                  </span>
                </summary>
                <div className="nf-advanced-body">
                  <label>
                    <span>
                      Name{" "}
                      <span className="muted">
                        — optional; empty names it from the task
                      </span>
                    </span>
                    <input
                      id="new-title"
                      ref={titleRef}
                      autoComplete="off"
                      placeholder={planned.from === "none" ? "named from the task" : planned.title}
                      value={title}
                      onChange={(e) => setTitle(e.target.value)}
                    />
                  </label>

                  {gitOn && (
                    // The one Fast-track choice for a single session: a
                    // session made from the form goes as far as this says.
                    // Open PR / Merge need GitHub: greyed, with the menu's
                    // own sentence as the reason, until it is connected.
                    <div id="new-ft-row" className="rt-row rt-row-top" data-caps="git">
                      <span className="rt-label">Fast-track</span>
                      <div className="rt-ctl">
                        <Seg
                          id="new-lane-form"
                          label="Fast-track"
                          value={draft.lane}
                          onChange={draft.setLane}
                          options={FAST_TRACK_LEVELS.map(({ lane, label }) => {
                            const why = laneWhy(lane);
                            return { v: lane, label, title: why || LANE_DESC[lane], disabled: !!why };
                          })}
                        />
                        {!prOk && (
                          <p className="nf-opt-help muted" id="new-ft-github" title={prWhy}>
                            Open PR and Merge when green need GitHub — connect it in Settings → Connections.
                          </p>
                        )}
                      </div>
                    </div>
                  )}

                  <SaveTemplateRow
                    saveTpl={{
                      name: saveTplName,
                      setName: setSaveTplName,
                      busy: saveTplBusy,
                      save: () => void saveAsTemplate(),
                      blocked: saveTplBlocked,
                    }}
                  />

                  {/* What a first-hour session can't use yet, folded once
                      more. The summary names only what is inside. */}
                  <details
                    id="new-deep"
                    className="nf-advanced nf-deep"
                    open={deepOpen}
                    onToggle={(e) => {
                      const el = e.target as HTMLDetailsElement;
                      // A <details> inside a <details>: the inner one's
                      // toggle bubbles to the outer handler, so only ours.
                      e.stopPropagation();
                      setDeepOpen(el.open);
                      if (el.open && typeof el.scrollIntoView === "function")
                        el.scrollIntoView({ behavior: "smooth", block: "nearest" });
                    }}
                  >
                    <summary>
                      Advanced
                      <span className="nf-fold-sum" id="new-deep-sum">
                        — {deepSummary}
                      </span>
                    </summary>
                    <div className="nf-advanced-body">
                      {gitOn && (
                        <label
                          className={"check rt-ask" + (askWhy ? " disabled" : "")}
                          id="new-ask-row"
                          title={askWhy || ASK_FIRST_DESC}
                        >
                          <input
                            type="checkbox"
                            id="new-lane-form-ask"
                            checked={!askWhy && draft.askFirst}
                            disabled={!!askWhy}
                            onChange={(e) => draft.setAskFirst(e.target.checked)}
                          />
                          {ASK_FIRST_LABEL}
                          {askWhy && !splitOn && <span className="muted"> (needs Fast-track)</span>}
                        </label>
                      )}

                      {gitOn && (
                        // "Auto-split into up to N sessions if it's worth it":
                        // the form's folder and Task become the lead's.
                        <div id="new-split-row" data-caps="git">
                          <SplitCheck
                            id="new-split"
                            split={split}
                            onSplit={toggleSplit}
                            gate={mcpOk}
                            maxPieces={clampSplit(maxPieces, splitLimit)}
                            onMaxPieces={(n) => setMaxPieces(clampSplit(n, splitLimit))}
                            limit={splitLimit}
                            text={prompt || describe}
                          />
                          {!splitOn && mcpOk.reason !== SERVER_NO_SPLIT && (
                            <p className="nf-opt-help muted" id="new-split-help">
                              Lets the agent break a big task into up to {clampSplit(maxPieces, splitLimit)} parallel
                              sessions — you approve the split first.
                            </p>
                          )}
                          {splitSum && (
                            <p className="rt-sum" aria-live="polite">
                              {splitSum.lead} {splitSum.tail && <span className="muted">{splitSum.tail}</span>}{" "}
                              <span className="muted">
                                The account, plan-first, launch flags and git options don't apply to a split.
                              </span>
                            </p>
                          )}
                        </div>
                      )}

                      {gitOn && (
                        <GitOptions
                          provision={provision}
                          setProvision={setProvision}
                          setInPlace={setInPlace}
                          provisionOn={provisionOn}
                          offerProvision={offerProvision}
                          provisionBlocked={provisionBlocked}
                          armInitRepo={armInitRepo}
                          strategy={strategy}
                          setStrategy={setStrategy}
                          worktreeMode={worktreeMode}
                          branches={branches}
                          baseBranch={baseBranch}
                          setBaseBranch={setBaseBranch}
                          canContinue={canContinue}
                          continueOn={continueOn}
                          setContinueOn={setContinueOn}
                          initRepo={initRepo}
                          setInitRepo={setInitRepo}
                          isGit={isGit}
                          plainFolder={plainFolder}
                        />
                      )}

                      <PlanFirstRow
                        prompt={prompt}
                        planOk={planOk}
                        planFirst={planFirst}
                        setPlanFirst={setPlanFirst}
                      />

                      <LaunchFold
                        launchOpen={launchOpen}
                        setLaunchOpen={setLaunchOpen}
                        launchArgs={launchArgs}
                        setLaunchArgs={setLaunchArgs}
                        program={program}
                      />
                    </div>
                  </details>
                </div>
              </details>
            </>
          )}
        </div>
        <FooterActions
          error={error}
          page={page}
          setPage={setPage}
          runMode={runMode}
          describing={describing}
          describeSlow={describeSlow}
          describeMode={describeMode}
          draft={draft}
          runDescribe={runDescribe}
          startRun={startRun}
          startNow={startNow}
          setUpMyself={setUpMyself}
          startBlocked={startBlocked}
        />
      </form>
      )}
    </div>
  );
}

/** The New dialog's head: title, the two tabs (only once a tracker is
 * connected — see ticketingOk), and Close.

 * Shared by both panes rather than written twice, because the strip is the one
 * thing that has to look identical from either side of it — a tab row that
 * shifts by a pixel when you switch tabs reads as the whole dialog being
 * replaced, which (structurally) it is.
 *
 * "New" rather than "New session": the heading now names the dialog and the
 * tabs name the thing being made. */
function NewHead({
  tab,
  tabs,
  onTab,
  onClose,
}: {
  tab: NewTab;
  /** Draw the Session/Ticket strip at all. */
  tabs: boolean;
  onTab(t: NewTab): void;
  onClose(): void;
}) {
  return (
    <div className="ws-head nf-head">
      <h2 id="new-dialog-title">New</h2>
      {tabs && (
      <nav className="nf-tabs" aria-label="New">
        {NEW_TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            className={"nf-tab" + (tab === t.key ? " active" : "")}
            data-new-tab={t.key}
            aria-current={tab === t.key ? "page" : undefined}
            onClick={() => onTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </nav>
      )}
      <TourButton tour="new" />
      <button type="button" id="new-close" title="Close (Esc)" onClick={onClose}>
        Close
      </button>
    </div>
  );
}
