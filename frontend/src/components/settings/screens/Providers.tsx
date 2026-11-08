/** Settings → Custom agents (key "providers"; was "Agent providers"): one row per agent CLI MindFlock knows (built-in
 * and custom) — its agent card (components/setup/AgentCard: the same status
 * and single Install → Sign in → Test action as Connections, Settings →
 * Agents and the Setup checklist, so no screen sends you to another for the
 * same thing), where it was found, and a **Binary path** field for every provider that
 * writes this device's `coding_cli.binary_paths[name]` (never synced: a path
 * is about this machine). A custom TOML that failed to load is a red row with
 * the file and the parse error, instead of a provider that silently isn't
 * there. Above the rows, the form to add / edit custom providers; an
 * edit patches only the fields the form shows, so a hand-written TOML keeps
 * its other tables. Row shapes are derived in lib/providerRows (unit-tested). */

import { useCallback, useEffect, useState } from "react";
import { api } from "../../../api/client";
import type { ProviderStatus, ProvidersStatusResponse } from "../../../api/types";
import { agentName } from "../../../lib/agentNames";
import { providerRows, type ProviderRow, type Row } from "../../../lib/providerRows";
import { toast } from "../../../lib/toast";
import { refreshProviders } from "../../../state/queries";
import { AgentCard } from "../../setup/AgentCard";
import { notifyConnectionsChanged } from "./Connections";
import { joinTokens, tokenize } from "../../dialogs/FlagChips";
import type { ScreenProps } from "../SettingsDialog";

interface ManagedProvider {
  name: string;
  source?: string;
  editable?: boolean;
  aliases?: string[];
  command?: string;
  binary_path?: string;
  resume_flag?: string;
  skip_perms_flag?: string;
  launch_args?: string[];
}

const FORM_KEYS = ["name", "program", "binary", "args", "resume", "skip"] as const;
type FormKey = (typeof FORM_KEYS)[number];
type Form = Record<FormKey, string>;

const EMPTY_FORM: Form = {
  name: "",
  program: "",
  binary: "",
  args: "",
  resume: "",
  skip: "",
};

/** What the form edits after a save answered with `warning` (saved, but it
 *  won't start as written): that provider, so fixing the field the warning
 *  names is "Save changes" — a PUT. Left on the Add form, the next click
 *  POSTed the same name again and died on "already exists". A clean save
 *  closes the form (null). */
export function editingAfterSave(name: string, warning: string): string | null {
  return warning ? name : null;
}

/** A provider's `source` as a badge: the registry's words ("builtin",
 *  "user") are code, not something a person reads. */
export function sourceBadge(source: string): string {
  if (source === "builtin") return "built in";
  if (source === "user") return "custom";
  return source;
}

/** The saved provider, back in form shape — Edit reopens the same fields the
 *  Add form wrote, so a mistake is a correction instead of delete-and-retype. */
function toForm(p: ManagedProvider): Form {
  return {
    name: p.name,
    // `program` is what the CLI is called; the registry reports it as aliases.
    program: (p.aliases || []).join(" ") || p.command || "",
    binary: p.binary_path || "",
    args: joinTokens(p.launch_args || []),
    resume: p.resume_flag || "",
    skip: p.skip_perms_flag || "",
  };
}

/** This device's binary path for one provider. Saves on blur / Enter when the
 *  value changed; an emptied field clears the override (PUT with ""). The
 *  server answers with the row's fresh status, so "installed" flips at once. */
function BinaryPathField({
  row,
  onSaved,
}: {
  row: ProviderRow;
  onSaved(status: ProviderStatus): void;
}) {
  const [value, setValue] = useState(row.binaryPath);
  const [warning, setWarning] = useState("");
  // A reload (another window saved, the row re-rendered) resets the draft.
  useEffect(() => setValue(row.binaryPath), [row.binaryPath]);

  const save = async () => {
    const path = value.trim();
    if (path === row.binaryPath) return;
    try {
      const r = await api<{ warning?: string; status?: ProviderStatus }>(
        "/api/providers/" + encodeURIComponent(row.name) + "/binary-path",
        { method: "PUT", json: { path } }
      );
      setWarning(r?.warning || "");
      toast((path ? "Binary path saved for " : "Binary path cleared for ") + agentName(row.name));
      if (r?.status) onSaved(r.status);
      // A new path can flip installed / signed in: every agent card re-reads.
      notifyConnectionsChanged();
      window.reloadProviderPicker?.();
      void refreshProviders();
    } catch (err) {
      setWarning((err as Error).message || "save failed");
    }
  };

  return (
    <label className="set-row prov-binary-row">
      <span className="set-label">Binary path</span>
      <input
        type="text"
        className="prov-binary-input"
        data-provider={row.name}
        autoComplete="off"
        placeholder={row.installed ? "on PATH — set to use another build" : "/full/path/to/" + row.name}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onBlur={() => void save()}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
        }}
      />
      <span className="set-hint">
        {row.binaryPinnedBy
          ? `Pinned by ${row.binaryPinnedBy} in the server's environment, which wins over this field.`
          : "This device only — never synced to your other devices. Leave empty to use PATH."}
      </span>
      {warning && <span className="prov-launch-hint">{warning}</span>}
    </label>
  );
}

export function Providers({ gotoScreen }: ScreenProps) {
  const [statuses, setStatuses] = useState<ProviderStatus[]>([]);
  const [loadErrors, setLoadErrors] = useState<ProvidersStatusResponse["errors"]>([]);
  /** Where this server reads custom-agent TOMLs ("" = an older server). */
  const [tomlDir, setTomlDir] = useState("");
  const [managed, setManaged] = useState<Record<string, ManagedProvider>>({});
  const [form, setForm] = useState<Form>(EMPTY_FORM);
  /** null = the Add form; a name = editing that existing provider (PUT). */
  const [editing, setEditing] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [warning, setWarning] = useState("");

  const load = useCallback(async () => {
    // Fetch independently: the status endpoint is newer than /manage, so on a
    // server that predates it (not yet restarted) the manage list must still
    // render instead of the whole screen blanking to "add a provider".
    const [s, m] = await Promise.allSettled([
      api<ProvidersStatusResponse & { providers_dir?: string }>("/api/providers/status"),
      api<{ providers?: ManagedProvider[] }>("/api/providers/manage"),
    ]);
    const statusList = s.status === "fulfilled" ? s.value?.providers || [] : [];
    setStatuses(statusList);
    setLoadErrors(s.status === "fulfilled" ? s.value?.errors || [] : []);
    setTomlDir(s.status === "fulfilled" ? s.value?.providers_dir || "" : "");
    const byName: Record<string, ManagedProvider> = {};
    if (m.status === "fulfilled") {
      for (const p of m.value?.providers || []) byName[p.name] = p;
    }
    setManaged(byName);
    // Fall back to the manage list when the status endpoint isn't available yet,
    // so built-ins still show (without install/login detail) rather than nothing.
    if (statusList.length === 0 && Object.keys(byName).length > 0) {
      setStatuses(
        Object.values(byName).map((p) => ({
          name: p.name,
          installed: false,
          authenticated: false,
          auth_known: false,
        }))
      );
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const del = async (name: string) => {
    try {
      await api("/api/providers/" + encodeURIComponent(name), { method: "DELETE" });
      toast("Removed " + agentName(name));
      if (editing === name) cancelEdit();
      load();
      notifyConnectionsChanged();
      window.reloadProviderPicker?.();
      void refreshProviders();
    } catch (err) {
      toast("Delete failed: " + ((err as Error).message || name));
    }
  };

  const edit = (p: ManagedProvider) => {
    setError("");
    setWarning("");
    setEditing(p.name);
    setForm(toForm(p));
    // The form lives in a <details>; opening it from a row has to unfold it.
    document.getElementById("prov-add")?.setAttribute("open", "");
    document.getElementById("prov-new-binary")?.focus();
  };

  const cancelEdit = () => {
    setEditing(null);
    setForm(EMPTY_FORM);
    setError("");
    setWarning("");
  };

  const save = async () => {
    setError("");
    setWarning("");
    const body = {
      name: form.name.trim(),
      program: form.program.trim(),
      binary_path: form.binary.trim(),
      launch_args: tokenize(form.args.trim()),
      resume_flag: form.resume.trim(),
      skip_perms_flag: form.skip.trim(),
    };
    if (!body.name) {
      setError("name is required");
      return;
    }
    try {
      // Editing PUTs to the existing name; the name field is locked there, so a
      // rename is a delete + add rather than a silent second provider.
      const res = await (editing
        ? api<{ warning?: string }>("/api/providers/" + encodeURIComponent(editing), {
            method: "PUT",
            json: body,
          })
        : api<{ warning?: string }>("/api/providers", { json: body }));
      toast((editing ? "Saved " : "Added ") + agentName(body.name));
      // Saved either way — a launch warning is advice, not a failed write.
      setWarning(res?.warning || "");
      const next = editingAfterSave(body.name, res?.warning || "");
      setEditing(next);
      if (!next) setForm(EMPTY_FORM);
      load();
      // The new agent's card (and every other agent card) re-reads, instead of
      // staying blank until the shared rows go stale.
      notifyConnectionsChanged();
      window.reloadProviderPicker?.();
      void refreshProviders();
    } catch (err) {
      setError((err as Error).message || (editing ? "save failed" : "add failed"));
    }
  };

  const set = (k: FormKey) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  /** One row's fresh status (after a binary-path save), in place. */
  const replaceStatus = (fresh: ProviderStatus) =>
    setStatuses((list) => list.map((p) => (p.name === fresh.name ? fresh : p)));

  const rows: Row[] = providerRows(statuses, loadErrors);

  return (
    <>
      <h3 className="set-section-title">Custom agents</h3>
      <p className="set-hint">
        Every agent MindFlock knows and where it runs from. Point one at a build MindFlock can't
        find, or add an agent MindFlock doesn't ship.
      </p>

      {/* The form first: adding an agent is what a person comes here for, and
          below the eight built-in rows it sat a long scroll away. */}
      <details id="prov-add" open={!!editing}>
        <summary>{editing ? "Edit " + editing : "+ Add a custom agent"}</summary>
        <div className="prov-form">
          {/* Every field is labelled: placeholders alone vanished as you typed,
              leaving a column of unnamed values ("claudekiro", "--kiro"). */}
          <label className="set-row">
            <span className="set-label">Name</span>
            {/* Renaming would orphan sessions already pointing at the old name, so
                the name is fixed once saved — delete and re-add to rename. */}
            <input type="text" id="prov-new-name" placeholder="mycli" autoComplete="off" value={form.name} onChange={set("name")} disabled={!!editing} />
            <span className="set-hint">
              {editing ? "Fixed once saved — delete and add it again to rename." : "What you pick in the New dialog. Also the command MindFlock runs, unless you set a binary path."}
            </span>
          </label>
          <label className="set-row">
            <span className="set-label">Binary path</span>
            <input type="text" id="prov-new-binary" placeholder="/home/me/bin/mycli" autoComplete="off" value={form.binary} onChange={set("binary")} />
            <span className="set-hint">
              Optional: the executable to run. Needed when the name is a shell alias or function
              (<code>type mycli</code> says “aliased to”) — sessions start in a plain shell
              that has none of those.
            </span>
          </label>
          <label className="set-row">
            <span className="set-label">Launch flags</span>
            <input type="text" id="prov-new-args" placeholder="--model fast" autoComplete="off" value={form.args} onChange={set("args")} />
            <span className="set-hint">Optional: added every time it starts, right after the command.</span>
          </label>
          <label className="set-row">
            <span className="set-label">Resume flag</span>
            <input type="text" id="prov-new-resume" placeholder="--continue" autoComplete="off" value={form.resume} onChange={set("resume")} />
            <span className="set-hint">Optional: continues the last conversation when a session restarts. Leave empty if it can't.</span>
          </label>
          <label className="set-row">
            <span className="set-label">Skip-permissions flag</span>
            <input type="text" id="prov-new-skip" placeholder="--yolo" autoComplete="off" value={form.skip} onChange={set("skip")} />
            <span className="set-hint">Optional: added only to sessions started with permission prompts skipped.</span>
          </label>
          <label className="set-row">
            <span className="set-label">Also matches</span>
            <input type="text" id="prov-new-program" placeholder="my-cli mycli-beta" autoComplete="off" value={form.program} onChange={set("program")} />
            <span className="set-hint">
              Optional: other command names that mean this agent, space-separated. It doesn't
              change what runs — that's the binary path.
            </span>
          </label>
          <div className="prov-form-actions">
            <button type="button" id="prov-add-btn" onClick={save}>
              {editing ? "Save changes" : "Add agent"}
            </button>
            {editing && (
              <button type="button" id="prov-cancel-btn" onClick={cancelEdit}>
                Cancel
              </button>
            )}
          </div>
          <p id="prov-error" className="error">{error}</p>
          {/* Saved, but it won't start as written — the fix is one field up. */}
          {warning && <p id="prov-warning" className="prov-launch-hint">{warning}</p>}
          <p className="set-hint" id="prov-toml-hint">
            Each custom agent is a TOML file in <code>{tomlDir || "~/.mindflock-assistant/providers/"}</code>
            {tomlDir ? "" : " (or $MINDFLOCK_PROVIDERS_DIR)"}. Edit it for what this form doesn't
            cover — busy/idle screen patterns, trust prompts, effort levels, sign-in — as
            docs/providers.md describes; saving here keeps those keys.
          </p>
        </div>
      </details>

      <div className="prov-conn-list">
        {rows.map((row) => {
          if (row.kind === "error") {
            // A TOML in the providers dir that didn't load: say which and why,
            // so the fix is a file edit rather than a hunt for a missing CLI.
            return (
              <div className="prov-conn prov-conn-error" key={"error:" + row.file} data-error-file={row.file}>
                <div className="prov-conn-head">
                  <span className="prov-name error">{row.file}</span>
                  <span className="prov-badge prov-badge-src">custom</span>
                  <span className="prov-dot bad" title="this provider TOML did not load">
                    failed to load
                  </span>
                </div>
                <p className="error">{row.error}</p>
              </div>
            );
          }
          const mgr = managed[row.name];
          return (
            <div className="prov-conn" key={row.name} data-provider={row.name}>
              <div className="prov-conn-head">
                <span className="prov-name">{agentName(row.name)}</span>
                {row.isDefault && <span className="prov-badge">default</span>}
                {mgr?.source && <span className="prov-badge prov-badge-src">{sourceBadge(mgr.source)}</span>}
              </div>

              {/* Status and the one next action: the same card as everywhere. */}
              <AgentCard provider={row.name} here="providers" gotoScreen={gotoScreen} onChanged={() => void load()} />

              {/* Where the CLI was found — the path the launcher will run. */}
              {row.installed && row.installTitle && (
                <p className="set-hint prov-path">
                  Runs <code>{row.installTitle}</code>
                </p>
              )}

              {/* Why a custom CLI can't be found (usually a shell alias the
                  launch shell cannot see) is the card's detail line — the row
                  repeating it printed the same paragraph twice. */}

              <BinaryPathField row={row} onSaved={replaceStatus} />

              {mgr?.editable && (
                <div className="prov-conn-actions">
                  <button type="button" onClick={() => edit(mgr)}>
                    Edit
                  </button>
                  <button type="button" onClick={() => del(row.name)}>
                    Delete
                  </button>
                </div>
              )}
            </div>
          );
        })}
      </div>

    </>
  );
}
