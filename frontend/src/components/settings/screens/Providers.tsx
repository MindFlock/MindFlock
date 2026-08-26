/** Settings → Agent providers: see which agent CLIs are installed + manage
 * custom ones.
 *
 * Top section is the *connection* view — for every provider (built-in and
 * custom) show whether its CLI is installed and, when missing, a copy-paste
 * install command. Login is intentionally NOT surfaced here: each CLI prompts
 * for sign-in on its own the first time a session launches it. Below it, the
 * manager to add / remove custom providers. */

import { useCallback, useEffect, useState } from "react";
import { api } from "../../../api/client";
import { copyText } from "../../../lib/clipboard";
import { toast } from "../../../lib/toast";
import { refreshProviders } from "../../../state/queries";
import { joinTokens, tokenize } from "../../dialogs/FlagChips";
import type { ScreenProps } from "../SettingsDialog";

interface ProviderStatus {
  name: string;
  aliases?: string[];
  binary?: string;
  installed: boolean;
  path?: string;
  authenticated: boolean;
  auth_known: boolean;
  auth_detail?: string;
  login_command?: string;
  install_hint?: string;
  /** Why a custom provider's CLI can't be resolved (shell alias, bad path). */
  launch_hint?: string;
  is_default?: boolean;
}

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

export function Providers(_: ScreenProps) {
  const [statuses, setStatuses] = useState<ProviderStatus[]>([]);
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
      api<{ providers?: ProviderStatus[] }>("/api/providers/status"),
      api<{ providers?: ManagedProvider[] }>("/api/providers/manage"),
    ]);
    const statusList = s.status === "fulfilled" ? s.value?.providers || [] : [];
    setStatuses(statusList);
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

  return (
    <>
      <h3 className="set-section-title">Agent providers</h3>
      <p className="set-hint">
        Agent CLIs new sessions can launch. Install a CLI to make it available —
        it'll prompt you to sign in on its own the first time a session opens it.
      </p>

      <div className="prov-conn-list">
        {statuses.map((p) => {
          const mgr = managed[p.name];
          return (
            <div className="prov-conn" key={p.name}>
              <div className="prov-conn-head">
                <span className="prov-name">{p.name}</span>
                {p.is_default && <span className="prov-badge">default</span>}
                {mgr?.source && <span className="prov-badge prov-badge-src">{mgr.source}</span>}
                <span
                  className={"prov-dot " + (p.installed ? "ok" : "bad")}
                  title={p.installed ? p.path || "on PATH" : "not found on PATH"}
                >
                  {p.installed ? "installed" : "not installed"}
                </span>
              </div>

              {!p.installed && p.install_hint && (
                <div className="prov-install">
                  <code>{p.install_hint}</code>
                  <button type="button" onClick={() => copyInstall(p.install_hint!)}>
                    Copy
                  </button>
                </div>
              )}

              {/* Why a custom CLI can't be found — usually a shell alias, which
                  the non-interactive launch shell cannot see. */}
              {!p.installed && p.launch_hint && (
                <p className="prov-launch-hint">{p.launch_hint}</p>
              )}

              {mgr?.editable && (
                <div className="prov-conn-actions">
                  <button type="button" onClick={() => edit(mgr)}>
                    Edit
                  </button>
                  <button type="button" onClick={() => del(p.name)}>
                    Delete
                  </button>
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
