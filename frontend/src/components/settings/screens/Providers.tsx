/** Settings → Agent providers: one row per agent CLI MindFlock knows (built-in
 * and custom) — installed (with the resolved path), signed in (from the status
 * API's login evidence) with a **Sign in** button that opens the CLI's own
 * login in an in-app terminal (dialogs/AgentSignIn), the install command when
 * the CLI is missing, and a **Binary path** field for every provider that
 * writes this device's `coding_cli.binary_paths[name]` (never synced: a path
 * is about this machine). A custom TOML that failed to load is a red row with
 * the file and the parse error, instead of a provider that silently isn't
 * there. Below it, the manager to add / edit / remove custom providers; an
 * edit patches only the fields the form shows, so a hand-written TOML keeps
 * its other tables. Row shapes are derived in lib/providerRows (unit-tested). */

import { useCallback, useEffect, useState } from "react";
import { api } from "../../../api/client";
import type { ProviderStatus, ProvidersStatusResponse } from "../../../api/types";
import { copyText } from "../../../lib/clipboard";
import { providerRows, type ProviderRow, type Row } from "../../../lib/providerRows";
import { toast } from "../../../lib/toast";
import { refreshProviders } from "../../../state/queries";
import { AgentSignIn } from "../../dialogs/AgentSignIn";
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
      toast(path ? "Binary path saved for " + row.name : "Binary path cleared for " + row.name);
      if (r?.status) onSaved(r.status);
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

export function Providers(_: ScreenProps) {
  const [statuses, setStatuses] = useState<ProviderStatus[]>([]);
  const [loadErrors, setLoadErrors] = useState<ProvidersStatusResponse["errors"]>([]);
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
      api<ProvidersStatusResponse>("/api/providers/status"),
      api<{ providers?: ManagedProvider[] }>("/api/providers/manage"),
    ]);
    const statusList = s.status === "fulfilled" ? s.value?.providers || [] : [];
    setStatuses(statusList);
    setLoadErrors(s.status === "fulfilled" ? s.value?.errors || [] : []);
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
      toast("Removed provider " + name);
      load();
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
      toast((editing ? "Saved provider " : "Added provider ") + body.name);
      // Saved either way — a launch warning is advice, not a failed write.
      setWarning(res?.warning || "");
      if (!res?.warning) {
        setEditing(null);
        setForm(EMPTY_FORM);
      }
      load();
      window.reloadProviderPicker?.();
      void refreshProviders();
    } catch (err) {
      setError((err as Error).message || (editing ? "save failed" : "add failed"));
    }
  };

  const set = (k: FormKey) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  const copyInstall = async (cmd: string) => {
    if (await copyText(cmd)) toast("Copied install command");
  };

  /** One row's fresh status (after a binary-path save), in place. */
  const replaceStatus = (fresh: ProviderStatus) =>
    setStatuses((list) => list.map((p) => (p.name === fresh.name ? fresh : p)));

  const rows: Row[] = providerRows(statuses, loadErrors);

  return (
    <>
      <h3 className="set-section-title">Agent providers</h3>
      <p className="set-hint">
        Agent CLIs new sessions can launch. Install a CLI to make it available,
        then sign in here — or point MindFlock at a build that isn't on PATH.
      </p>

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
                <span className="prov-name">{row.name}</span>
                {row.isDefault && <span className="prov-badge">default</span>}
                {mgr?.source && <span className="prov-badge prov-badge-src">{mgr.source}</span>}
                <span
                  className={"prov-dot " + (row.installed ? "ok" : "bad")}
                  title={row.installTitle}
                >
                  {row.installLabel}
                </span>
                {row.signInLabel && (
                  <span
                    className={
                      "prov-dot prov-signin " +
                      (row.signIn === "signed-in" ? "ok" : row.signIn === "not-signed-in" ? "bad" : "unknown")
                    }
                    title={row.signInDetail}
                  >
                    {row.signInLabel}
                  </span>
                )}
              </div>

              {/* Where the CLI was found — the path the launcher will run. */}
              {row.installed && row.installTitle && (
                <p className="set-hint prov-path">
                  <code>{row.installTitle}</code>
                  {row.signIn === "signed-in" && row.signInDetail && (
                    <> · signed in: {row.signInDetail}</>
                  )}
                </p>
              )}

              {row.installHint && (
                <div className="prov-install">
                  <code>{row.installHint}</code>
                  <button type="button" onClick={() => copyInstall(row.installHint)}>
                    Copy
                  </button>
                </div>
              )}

              {/* Why a custom CLI can't be found — usually a shell alias, which
                  the non-interactive launch shell cannot see. */}
              {row.launchHint && <p className="prov-launch-hint">{row.launchHint}</p>}

              <BinaryPathField row={row} onSaved={replaceStatus} />

              {(row.canSignIn || mgr?.editable) && (
                <div className="prov-conn-actions">
                  {row.canSignIn && (
                    <AgentSignIn provider={row.name} label="Sign in" onDone={() => void load()} />
                  )}
                  {mgr?.editable && (
                    <>
                      <button type="button" onClick={() => edit(mgr)}>
                        Edit
                      </button>
                      <button type="button" onClick={() => del(row.name)}>
                        Delete
                      </button>
                    </>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <details id="prov-add" open={!!editing}>
        <summary>{editing ? "Edit " + editing : "+ Add a custom provider"}</summary>
        <div className="prov-form">
          {/* Renaming would orphan sessions already pointing at the old name, so
              the name is fixed once saved — delete and re-add to rename. */}
          <input type="text" id="prov-new-name" placeholder="name (e.g. mycli)" autoComplete="off" value={form.name} onChange={set("name")} disabled={!!editing} />
          <input type="text" id="prov-new-program" placeholder="program (default: name)" autoComplete="off" value={form.program} onChange={set("program")} />
          <input type="text" id="prov-new-binary" placeholder="binary path (optional, e.g. /home/me/bin/mycli)" autoComplete="off" value={form.binary} onChange={set("binary")} />
          <input type="text" id="prov-new-args" placeholder="saved args (e.g. --dangerously-skip-permissions)" autoComplete="off" value={form.args} onChange={set("args")} />
          <input type="text" id="prov-new-resume" placeholder="resume flag (e.g. --continue)" autoComplete="off" value={form.resume} onChange={set("resume")} />
          <input type="text" id="prov-new-skip" placeholder="skip-perms flag (e.g. --yolo)" autoComplete="off" value={form.skip} onChange={set("skip")} />
          <div className="prov-form-actions">
            <button type="button" id="prov-add-btn" onClick={save}>
              {editing ? "Save changes" : "Add provider"}
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
        </div>
      </details>
    </>
  );
}
