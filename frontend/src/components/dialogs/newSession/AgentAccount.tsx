/** The New form's Agent / Account / Model fields, and the account pick that
 * steers the agent. */

import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import { api } from "../../../api/client";
import type { AuthProfile, AuthProfilesResponse } from "../../../api/types";
import { openSettings } from "../../../lib/openSettings";
import type { InstalledMap, Provider } from "../newSession";

type Set<T> = Dispatch<SetStateAction<T>>;

export interface AccountCtx {
  authProfiles: AuthProfilesResponse | undefined;
  program: string;
  profileModels: Record<string, string[]>;
  canonAgent(raw: string): string;
  setAgent(value: string): void;
  setProfileId: Set<string>;
  setProfileModel: Set<string>;
  setProfileModels: Set<Record<string, string[]>>;
}

/** setAccount, built per render from the form it writes into. */
export function accountSetter(ctx: AccountCtx) {
  const {
    authProfiles,
    program,
    profileModels,
    canonAgent,
    setAgent,
    setProfileId,
    setProfileModel,
    setProfileModels,
  } = ctx;
  /** Picking an account steers the Agent field: with an OpenRouter (or any
   * key) account the identity is the choice that matters, so an agent the
   * account can't route is auto-swapped to one it can — the alternative is a
   * session that silently launches on the CLI's own login. A profile with raw
   * env overrides applies to every CLI, so it never steers. */
  const setAccount = (id: string, agentNow?: string) => {
    setProfileId(id);
    setProfileModel("");
    const prof = (authProfiles?.profiles || []).find((p) => p.id === id);
    if (!prof) return;
    const supported = prof.supported_agents || [];
    const hasEnv = !!prof.env && Object.keys(prof.env).length > 0;
    if (supported.length && !hasEnv && !supported.includes(canonAgent(agentNow || program))) {
      const preferred =
        prof.provider && supported.includes(prof.provider) ? prof.provider : supported[0];
      setAgent(preferred);
    }
    // OpenRouter accounts get a model picker: ask the key what it can reach
    // (cached per profile; a failed fetch just leaves the free-text field).
    if (prof.kind === "openrouter" && !profileModels[id]) {
      (async () => {
        try {
          const r = await api<{ ok?: boolean; models?: string[] }>(
            "/api/settings/test/openrouter",
            { json: { profile_id: id } }
          );
          if (r?.ok && r.models?.length)
            setProfileModels((m) => ({ ...m, [id]: r.models || [] }));
        } catch {
          /* the free-text input still works */
        }
      })();
    }
  };
  return setAccount;
}

export function AgentFields({
  program,
  providers,
  installed = null,
  setAgent,
  device,
  authProfiles,
  profileId,
  pendingProfile,
  setAccount,
  closeDialog,
}: {
  program: string;
  providers: Provider[];
  /** Which agents this device has (null = unknown): a missing one is
   * marked "not installed" in the list, and Create refuses it. */
  installed?: InstalledMap | null;
  setAgent(value: string): void;
  device: string;
  authProfiles: AuthProfilesResponse | undefined;
  profileId: string;
  pendingProfile: MutableRefObject<unknown>;
  setAccount(id: string, agentNow?: string): void;
  closeDialog(): void;
}) {
  return (
    <>
      <label className="nf-agent">
        <span className="nf-agent-head">
          Agent
          <button
            type="button"
            id="new-agent-manage"
            className="linklike"
            title="Install, sign in and pick your default agent in Settings → Agents"
            onClick={() => {
              closeDialog();
              openSettings("coding");
            }}
          >
            Manage
          </button>
        </span>
        <select
          id="new-program"
          title="The agent this session runs"
          value={program}
          onChange={(e) => setAgent(e.target.value)}
        >
          {!providers.some((p) => p.name === program) && program && (
            <option value={program}>{program}</option>
          )}
          {providers.map((p) => (
            <option key={p.name} value={p.name}>
              {p.name}
              {installed && installed[p.name] === false ? " (not installed)" : ""}
            </option>
          ))}
        </select>
      </label>
      {/* Accounts are THIS device's logins; a session on another
          device signs in with that device's own. */}
      {!device && (authProfiles?.profiles || []).length > 0 && (
        <label className="nf-agent">
          <span className="nf-agent-head">
            Account
            <button
              type="button"
              id="new-account-manage"
              className="linklike"
              title="Manage accounts in Settings"
              onClick={() => {
                closeDialog();
                openSettings("accounts");
              }}
            >
              Manage
            </button>
          </span>
          <select
            id="new-account"
            title="Which identity this session's CLI runs as"
            value={profileId}
            onChange={(e) => {
              pendingProfile.current = null;
              setAccount(e.target.value);
            }}
          >
            <option value="">
              {authProfiles?.default_profile
                ? `App default (${authProfiles.default_profile})`
                : "App default (CLI's own login)"}
            </option>
            <option value="default">CLI's own login</option>
            {(authProfiles?.profiles || []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.label || p.id}
              </option>
            ))}
          </select>
        </label>
      )}
    </>
  );
}

/** The Model row for a key account, the route warning, and the provider datalist. */
export function ModelRow({
  selectedProfile,
  profileId,
  profileModel,
  setProfileModel,
  profileModels,
  program,
  canonAgent,
}: {
  selectedProfile: AuthProfile | undefined;
  profileId: string;
  profileModel: string;
  setProfileModel: Set<string>;
  profileModels: Record<string, string[]>;
  program: string;
  canonAgent(raw: string): string;
}) {
  // The route warning for the CURRENT combination (the auto-swap above keeps
  // this rare — it appears when the user manually re-picks an unrouted agent).
  const routeWarning = (() => {
    if (!selectedProfile) return "";
    const supported = selectedProfile.supported_agents || [];
    const hasEnv = !!selectedProfile.env && Object.keys(selectedProfile.env).length > 0;
    if (hasEnv || !supported.length) return "";
    if (supported.includes(canonAgent(program))) return "";
    return (
      `“${selectedProfile.label || selectedProfile.id}” has no route for ${program || "this agent"} — ` +
      `the session would run on the CLI's own login. It works with: ${supported.join(", ")}.`
    );
  })();
  return (
    <>
      {selectedProfile && selectedProfile.kind !== "account" && (
        <div className="nf-quick">
          <label className="nf-agent" style={{ flex: 1 }}>
            <span className="nf-agent-head">Model</span>
            {(profileModels[profileId] || []).length ? (
              <select
                id="new-account-model"
                title="Model this session runs on (through the selected account)"
                value={profileModel}
                onChange={(e) => setProfileModel(e.target.value)}
              >
                <option value="">
                  {selectedProfile.model
                    ? `Account default (${selectedProfile.model})`
                    : "Account default"}
                </option>
                {/* A value typed before the catalog landed (or absent from
                    it) stays VISIBLE and selected — coercing the display
                    to "Account default" while still submitting it would
                    launch a model the form no longer shows. */}
                {profileModel &&
                  !(profileModels[profileId] || []).includes(profileModel) && (
                    <option value={profileModel}>{profileModel} (custom)</option>
                  )}
                {(profileModels[profileId] || []).map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            ) : (
              <input
                id="new-account-model"
                type="text"
                autoComplete="off"
                placeholder={
                  selectedProfile.model
                    ? `Account default (${selectedProfile.model})`
                    : "anthropic/claude-sonnet-4.5"
                }
                value={profileModel}
                onChange={(e) => setProfileModel(e.target.value)}
              />
            )}
          </label>
        </div>
      )}

      {routeWarning && <p className="nf-git-nudge">{routeWarning}</p>}
      {/* The datalist mount slots.js populates from /api/providers. */}
      <datalist id="provider-list"></datalist>
    </>
  );
}
