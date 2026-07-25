/** New-session dialog (ports section 16 + the new-form submit from section
 * 17 + the J4 preset row): quick path is name → Enter; folder browser,
 * templates strip, prompt presets, provisioning fold, and per-session launch
 * flags live behind progressive disclosure. */

import { useCallback, useEffect, useRef, useState } from "react";
import type { Config, Instance } from "../../api/types";
import { api } from "../../api/client";
import { refreshInstances, refreshConfig, queryClient } from "../../state/queries";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";
import {
  addPendingSession,
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

export function NewSessionDialog() {
  const open = useUi((s) => s.openDialog === "new-session");
  const closeDialog = useUi((s) => s.closeDialog);

  const [title, setTitle] = useState("");
  const [program, setProgram] = useState("");
  const [providers, setProviders] = useState<Provider[]>([]);
  const [repoPath, setRepoPath] = useState("");
  const [prompt, setPrompt] = useState("");
  const [launchArgs, setLaunchArgs] = useState("");
  const [provision, setProvision] = useState(false);
  const [strategy, setStrategy] = useState("worktree");
  const [inPlace, setInPlace] = useState(true);
  const [initRepo, setInitRepo] = useState(false);
  const [error, setError] = useState("");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [browserOpen, setBrowserOpen] = useState(false);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [activeTemplate, setActiveTemplate] = useState("");
  const [provisioningAvailable, setProvisioningAvailable] = useState(false);
  const [homePath, setHomePath] = useState("");
  const [presetValue, setPresetValue] = useState("");
  const [savedPresets, setSavedPresets] = useState<Preset[]>([]);
  const launchDefaults = useRef<Record<string, string>>({});
  const titleRef = useRef<HTMLInputElement | null>(null);
  const launchRef = useRef<HTMLDetailsElement | null>(null);

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
    setAdvancedOpen(false);
    setBrowserOpen(false);
    setActiveTemplate("");
    setPresetValue("");
    setSavedPresets(loadUserPresets());
    (async () => {
      // Config + settings resolve in parallel; templates are optional sugar.
      const [cfgR, setR, tplR] = await Promise.allSettled([
        refreshConfig().then(() => queryClient.getQueryData<Config>(["config"])),
        api<{ settings?: { coding_cli?: { default_launch_args?: Record<string, string> } } }>(
          "/api/settings"
        ),
        api<{ templates?: Template[] }>("/api/templates"),
      ]);
      const cfg = cfgR.status === "fulfilled" ? cfgR.value : undefined;
      setHomePath(cfg?.home || "");
      setRepoPath(cfg?.home || "");
      setProvisioningAvailable(!!cfg?.provisioning_available);
      let provs: Provider[] = [];
      try {
        const d = await api<{ providers?: Provider[] }>("/api/providers/manage");
        provs = d.providers || [];
      } catch {
        /* providers are optional */
      }
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
      setTimeout(() => titleRef.current?.focus(), 0);
    })();
  }, [open]);

  const setAgent = useCallback((value: string) => {
    const v = (value || "").trim();
    setProgram(v);
    // Switching agents resets the flags to that provider's saved default.
    setLaunchArgs((launchDefaults.current[v.toLowerCase()] || "").trim());
  }, []);

  const fillFromTemplate = (t: Template) => {
    if (t.program) setAgent(t.program);
    if (t.repo_path) setRepoPath(t.repo_path);
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

  if (!open) return null;

  const offerProvision = provisioningAvailable || !!repoPath.trim();

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
      onClick={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          if (browserOpen) setBrowserOpen(false);
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
                  onChange={(e) => setRepoPath(e.target.value)}
                />
                <button
                  type="button"
                  id="repo-browse-btn"
                  title="Browse local folders"
                  onClick={() => setBrowserOpen((v) => !v)}
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

          {browserOpen && (
            <FolderBrowser
              initialPath={repoPath.trim() || homePath || ""}
              onPick={(p) => {
                setRepoPath(p);
                setBrowserOpen(false);
              }}
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
              placeholder="What should the agent do first?"
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
            onToggle={(e) => {
              // The fold is the last thing in the scroll region, so its revealed
              // fields open below the fold line — scroll them into view so it's
              // obvious the click did something and where to look.
              if ((e.target as HTMLDetailsElement).open)
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

/** Folder browser popover (port of loadBrowse, section 16). */
function FolderBrowser({
  initialPath,
  onPick,
}: {
  initialPath: string;
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

  const load = useCallback(async (path: string) => {
    setError("");
    try {
      const q = path ? "?path=" + encodeURIComponent(path) : "";
      setData(await api<BrowsePayload>("/api/browse" + q));
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    load(initialPath);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const mkdir = async () => {
    if (!data?.path) return;
    const name = window.prompt("New folder name (created in " + data.path + "):", "");
    if (!name || !name.trim()) return;
    setError("");
    try {
      const r = await api<{ path: string }>("/api/mkdir", {
        json: { path: data.path, name: name.trim() },
      });
      onPick(r.path);
      load(r.path);
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
      <div id="rb-list">
        {data && (
          <div
            className={"rb-item rb-use" + (data.is_git ? " is-git" : "")}
            onClick={() => onPick(data.path)}
          >
            {data.is_git ? "✓ use this repo" : "use this folder"}
          </div>
        )}
        {(data?.entries || []).map((e) => (
          <div key={e.path} className={"rb-item" + (e.is_git ? " is-git" : "")}>
            <span className="rb-name" onClick={() => load(e.path)}>
              {(e.is_git ? "📦 " : "📁 ") + e.name}
            </span>
            <button
              type="button"
              className="rb-pick"
              onClick={(ev) => {
                ev.stopPropagation();
                onPick(e.path);
              }}
            >
              select
            </button>
          </div>
        ))}
      </div>
      <p id="rb-error" className="error">{error}</p>
    </div>
  );
}
