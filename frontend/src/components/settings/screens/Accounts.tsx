/** Settings → Accounts: auth profiles — run different sessions as different
 * identities without logging any CLI out.
 *
 * Three kinds: `account` (a second login of the CLI itself, isolated in its
 * own config dir — the personal-vs-work Claude case), `api_key` (a vendor key
 * injected at launch), `openrouter` (an OpenRouter key, with Test reporting
 * the key's real spend and the models it can reach — which also turns the
 * model field into a dropdown). The list round-trips masked: a key showing
 * "•••set" stays stored until replaced. */

import { useEffect, useMemo, useState } from "react";
import { api } from "../../../api/client";
import type { AuthProfile } from "../../../api/types";
import { toast } from "../../../lib/toast";
import { refreshAuthProfiles, useAuthProfiles } from "../../../state/queries";
import { SECRET_MASK } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";

const KINDS: Array<{ id: string; label: string; hint: string }> = [
  {
    id: "account",
    label: "CLI account",
    hint: "A separate login of the CLI itself (e.g. a work Claude subscription) in its own config dir.",
  },
  {
    id: "api_key",
    label: "API key",
    hint: "A vendor API key injected for the session's CLI (metered, no subscription).",
  },
  {
    id: "openrouter",
    label: "OpenRouter",
    hint: "Route the CLI through OpenRouter under its own key, with an optional model pin.",
  },
];

const AGENTS_BY_KIND: Record<string, string[]> = {
  account: ["claude", "codex"],
  api_key: ["claude", "codex", "aider", "goose"],
  openrouter: ["", "claude", "codex", "aider", "goose"],
};

interface OrProbe {
  ok?: boolean;
  usage?: number | null;
  limit?: number | null;
  models?: string[];
  error?: string;
}

const ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

export function Accounts(_: ScreenProps) {
  const { data } = useAuthProfiles();
  const [profiles, setProfiles] = useState<AuthProfile[]>([]);
  const [defaultId, setDefaultId] = useState("");
  const [addOpen, setAddOpen] = useState(false);
  const [draft, setDraft] = useState<AuthProfile>({ id: "", kind: "account" });
  const [error, setError] = useState("");
  const [probes, setProbes] = useState<Record<string, OrProbe>>({});
  const [testing, setTesting] = useState<string>("");

  useEffect(() => {
    setProfiles(data?.profiles || []);
    setDefaultId(data?.default_profile || "");
  }, [data]);

  const save = async (next: AuthProfile[], nextDefault?: string) => {
    setError("");
    try {
      const body: Record<string, unknown> = { profiles: next };
      if (nextDefault !== undefined) body.default_profile = nextDefault;
      const r = await api<{ profiles: AuthProfile[]; default_profile: string }>(
        "/api/settings/auth-profiles",
        { json: body, method: "PUT" }
      );
      setProfiles(r.profiles || []);
      setDefaultId(r.default_profile || "");
      void refreshAuthProfiles();
      toast("Accounts saved");
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const patch = (id: string, p: Partial<AuthProfile>) =>
    save(profiles.map((x) => (x.id === id ? { ...x, ...p } : x)));

  const addDraft = () => {
    const id = (draft.id || "").trim().toLowerCase();
    if (!ID_RE.test(id)) {
      setError("id must be lowercase letters/digits/-/_ (max 64)");
      return;
    }
    if (profiles.some((p) => p.id === id)) {
      setError(`'${id}' already exists`);
      return;
    }
    if (draft.kind !== "account" && !(draft.api_key || "").trim()) {
      setError("this kind needs an API key");
      return;
    }
    void save([...profiles, { ...draft, id }]);
    setDraft({ id: "", kind: "account" });
    setAddOpen(false);
  };

  const testOpenrouter = async (p: AuthProfile) => {
    setTesting(p.id);
    try {
      const r = await api<OrProbe>("/api/settings/test/openrouter", {
        json: { profile_id: p.id, api_key: p.api_key, base_url: p.base_url },
      });
      setProbes((m) => ({ ...m, [p.id]: r }));
      toast(r?.ok ? "OpenRouter key works" : "OpenRouter test failed");
    } catch (err) {
      setProbes((m) => ({ ...m, [p.id]: { ok: false, error: (err as Error).message } }));
    } finally {
      setTesting("");
    }
  };

  const kindMeta = useMemo(
    () => Object.fromEntries(KINDS.map((k) => [k.id, k])),
    []
  );

  return (
    <>
      <h3 className="set-section-title">Accounts</h3>
      <p className="set-hint set-block-hint">
        Run different sessions as different identities — a personal Claude
        subscription next to a work one, or an OpenRouter key with its own model —
        without logging any CLI out. Pick one per session in the New dialog or the
        pane header's account chip; swapping a live session just restarts its agent
        under the new identity. Each Claude account's usage is tracked separately in
        the cost panel.
      </p>

      <label className="set-row">
        <span className="set-label">Default for new sessions</span>
        <select
          id="acct-default"
          value={defaultId}
          onChange={(e) => save(profiles, e.target.value)}
        >
          <option value="">Each CLI's own login (no profile)</option>
          {profiles.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label || p.id}
            </option>
          ))}
        </select>
        <span className="set-hint">
          Sessions created without an explicit account run as this identity.
        </span>
      </label>

      <div className="prov-conn-list" id="acct-list">
        {profiles.map((p) => {
          const probe = probes[p.id];
          const models = probe?.models || [];
          return (
            <div className="prov-conn" key={p.id} data-account={p.id}>
              <div className="prov-conn-head">
                <span className="prov-name">{p.label || p.id}</span>
                <span className="prov-badge">{kindMeta[p.kind]?.label || p.kind}</span>
                {p.kind !== "openrouter" && (
                  <span className="prov-badge prov-badge-src">{p.provider || "claude"}</span>
                )}
                {p.id === defaultId && <span className="prov-badge">default</span>}
              </div>

              {p.kind === "account" ? (
                <>
                  <p className="set-hint">
                    Lives in <code>{p.resolved_config_dir || p.config_dir || "…"}</code>.
                    Log it in by running this in a terminal (the CLI's own sign-in flow,
                    scoped to this account):
                  </p>
                  {p.login_command && (
                    <p className="set-hint">
                      <code>{p.login_command}</code>{" "}
                      <button
                        type="button"
                        className="linklike"
                        onClick={() => {
                          void navigator.clipboard?.writeText(p.login_command || "");
                          toast("Login command copied");
                        }}
                      >
                        Copy
                      </button>{" "}
                      — or <code>mindflock accounts login {p.id}</code>
                    </p>
                  )}
                </>
              ) : (
                <>
                  <label className="set-row">
                    <span className="set-label">API key</span>
                    <input
                      type="password"
                      autoComplete="off"
                      placeholder={p.api_key === SECRET_MASK ? "•••set (saved)" : "paste a key"}
                      defaultValue=""
                      onBlur={(e) => {
                        if (e.target.value.trim()) patch(p.id, { api_key: e.target.value.trim() });
                      }}
                    />
                  </label>
                  <label className="set-row">
                    <span className="set-label">Model</span>
                    {models.length ? (
                      <select
                        value={models.includes(p.model || "") ? p.model : ""}
                        onChange={(e) => patch(p.id, { model: e.target.value })}
                      >
                        <option value="">CLI's own default</option>
                        {models.map((m) => (
                          <option key={m} value={m}>
                            {m}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <input
                        type="text"
                        autoComplete="off"
                        placeholder={
                          p.kind === "openrouter" ? "anthropic/claude-sonnet-4.5" : "model id"
                        }
                        defaultValue={p.model || ""}
                        onBlur={(e) => {
                          if (e.target.value !== (p.model || ""))
                            patch(p.id, { model: e.target.value.trim() });
                        }}
                      />
                    )}
                    {p.kind === "openrouter" && (
                      <span className="set-hint">
                        Test lists the models this key can reach, turning this into a picker.
                      </span>
                    )}
                  </label>
                </>
              )}

              <div className="prov-conn-actions">
                {p.kind === "openrouter" && (
                  <span className="test-row">
                    <button
                      type="button"
                      className="test-btn"
                      disabled={testing === p.id}
                      onClick={() => testOpenrouter(p)}
                    >
                      Test key
                    </button>
                    <span className={"test-result" + (probe ? (probe.ok ? " ok" : " bad") : "")}>
                      {testing === p.id
                        ? "testing…"
                        : probe
                          ? probe.ok
                            ? `✓ spent $${Number(probe.usage ?? 0).toFixed(2)}` +
                              (probe.limit != null ? ` of $${probe.limit}` : "") +
                              ` · ${models.length} models`
                            : "✗ " + (probe.error || "failed")
                          : ""}
                    </span>
                  </span>
                )}
                {p.id !== defaultId && (
                  <button type="button" className="linklike" onClick={() => save(profiles, p.id)}>
                    Make default
                  </button>
                )}
                <button
                  type="button"
                  className="linklike"
                  onClick={() => save(profiles.filter((x) => x.id !== p.id))}
                >
                  Remove
                </button>
              </div>
            </div>
          );
        })}
      </div>

      {addOpen ? (
        <div className="prov-form" id="acct-add-form">
          <label className="set-row">
            <span className="set-label">Kind</span>
            <select
              value={draft.kind}
              onChange={(e) =>
                setDraft((d) => ({ ...d, kind: e.target.value, provider: "" }))
              }
            >
              {KINDS.map((k) => (
                <option key={k.id} value={k.id}>
                  {k.label}
                </option>
              ))}
            </select>
            <span className="set-hint">{kindMeta[draft.kind]?.hint || ""}</span>
          </label>
          <label className="set-row">
            <span className="set-label">Id</span>
            <input
              type="text"
              autoComplete="off"
              placeholder="work"
              value={draft.id}
              onChange={(e) => setDraft((d) => ({ ...d, id: e.target.value }))}
            />
          </label>
          <label className="set-row">
            <span className="set-label">Label</span>
            <input
              type="text"
              autoComplete="off"
              placeholder="Work"
              value={draft.label || ""}
              onChange={(e) => setDraft((d) => ({ ...d, label: e.target.value }))}
            />
          </label>
          <label className="set-row">
            <span className="set-label">Agent CLI</span>
            <select
              value={draft.provider || ""}
              onChange={(e) => setDraft((d) => ({ ...d, provider: e.target.value }))}
            >
              {(AGENTS_BY_KIND[draft.kind] || ["claude"]).map((a) => (
                <option key={a || "any"} value={a}>
                  {a || "any (route by session's CLI)"}
                </option>
              ))}
            </select>
          </label>
          {draft.kind !== "account" && (
            <label className="set-row">
              <span className="set-label">API key</span>
              <input
                type="password"
                autoComplete="off"
                placeholder={draft.kind === "openrouter" ? "sk-or-…" : "key"}
                value={draft.api_key || ""}
                onChange={(e) => setDraft((d) => ({ ...d, api_key: e.target.value }))}
              />
            </label>
          )}
          <div className="prov-form-actions">
            <button type="button" className="test-btn" onClick={addDraft}>
              Add account
            </button>
            <button type="button" className="linklike" onClick={() => setAddOpen(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="set-row">
          <button type="button" id="acct-add" className="test-btn" onClick={() => setAddOpen(true)}>
            Add account
          </button>
        </div>
      )}

      {error && <p className="error">{error}</p>}
    </>
  );
}
