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
import { tokenize } from "../../dialogs/FlagChips";
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
  is_default?: boolean;
}

interface ManagedProvider {
  name: string;
  source?: string;
  editable?: boolean;
}

const FORM_KEYS = ["name", "program", "binary", "args", "resume", "skip"] as const;
type FormKey = (typeof FORM_KEYS)[number];

export function Providers(_: ScreenProps) {
  const [statuses, setStatuses] = useState<ProviderStatus[]>([]);
  const [managed, setManaged] = useState<Record<string, ManagedProvider>>({});
  const [form, setForm] = useState<Record<FormKey, string>>({
    name: "",
    program: "",
    binary: "",
    args: "",
    resume: "",
    skip: "",
  });
  const [error, setError] = useState("");

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
    } catch (err) {
      toast("Delete failed: " + ((err as Error).message || name));
    }
  };

  const add = async () => {
    setError("");
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
      await api("/api/providers", { json: body });
      toast("Added provider " + body.name);
      setForm({ name: "", program: "", binary: "", args: "", resume: "", skip: "" });
      load();
      window.reloadProviderPicker?.();
    } catch (err) {
      setError((err as Error).message || "add failed");
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

              {mgr?.editable && (
                <div className="prov-conn-actions">
                  <button type="button" onClick={() => del(p.name)}>
                    Delete
                  </button>
                </div>
              )}
            </div>
          );
        })}
      </div>

      <details id="prov-add">
        <summary>+ Add a custom provider</summary>
        <div className="prov-form">
          <input type="text" id="prov-new-name" placeholder="name (e.g. mycli)" autoComplete="off" value={form.name} onChange={set("name")} />
          <input type="text" id="prov-new-program" placeholder="program (default: name)" autoComplete="off" value={form.program} onChange={set("program")} />
          <input type="text" id="prov-new-binary" placeholder="binary path (optional)" autoComplete="off" value={form.binary} onChange={set("binary")} />
          <input type="text" id="prov-new-args" placeholder="saved args (e.g. --dangerously-skip-permissions)" autoComplete="off" value={form.args} onChange={set("args")} />
          <input type="text" id="prov-new-resume" placeholder="resume flag (e.g. --continue)" autoComplete="off" value={form.resume} onChange={set("resume")} />
          <input type="text" id="prov-new-skip" placeholder="skip-perms flag (e.g. --yolo)" autoComplete="off" value={form.skip} onChange={set("skip")} />
          <button type="button" id="prov-add-btn" onClick={add}>
            Add provider
          </button>
          <p id="prov-error" className="error">{error}</p>
        </div>
      </details>
    </>
  );
}
