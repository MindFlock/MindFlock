/** Pane-header account chip: which auth profile this session's agent runs as,
 * click to hot-swap. Renders nothing until at least one profile exists, so the
 * header stays unchanged for anyone not using the feature. A swap persists the
 * pin and restarts the agent under the new identity server-side (the worktree,
 * shell pane and diff survive).
 *
 * For OpenRouter-style profiles the popover also carries a Model picker fed by
 * the key's own FULL model list. The overlay also enables Claude Code's
 * gateway model discovery, so its /model menu shows the gateway's curated
 * top-ranked set — but a model pinned here (ANTHROPIC_MODEL) bypasses that
 * menu, and only this picker offers the whole catalog and a per-session pin. */

import { useEffect, useRef, useState } from "react";
import { api, instApi } from "../../api/client";
import type { Instance } from "../../api/types";
import { toast } from "../../lib/toast";
import { refreshInstances, useAuthProfiles } from "../../state/queries";
import { UsagePopover } from "../usage/UsagePopover";

export function AccountChip({ inst }: { inst: Instance }) {
  const chipRef = useRef<HTMLSpanElement | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [models, setModels] = useState<string[] | null>(null);
  const { data } = useAuthProfiles();
  const profiles = data?.profiles || [];

  const effective = inst.profile_effective || "";
  const label = inst.profile_label || "default";
  const effectiveProfile = profiles.find((p) => p.id === effective);
  const modelCapable = !!effectiveProfile && effectiveProfile.kind !== "account";

  // The OpenRouter catalog is fetched once per popover-open (the key answers
  // in ~1s; a failure just leaves the picker out — the swap rows still work).
  useEffect(() => {
    if (!open || !modelCapable || models !== null) return;
    if (effectiveProfile?.kind !== "openrouter") {
      setModels([]);
      return;
    }
    let live = true;
    (async () => {
      try {
        const r = await api<{ ok?: boolean; models?: string[] }>(
          "/api/settings/test/openrouter",
          { json: { profile_id: effective } }
        );
        if (live) setModels(r?.ok ? r.models || [] : []);
      } catch {
        if (live) setModels([]);
      }
    })();
    return () => {
      live = false;
    };
  }, [open, modelCapable, models, effective, effectiveProfile?.kind]);

  if (!profiles.length) return null;

  const swap = async (profileId: string, profileModel?: string) => {
    setBusy(true);
    setOpen(false);
    try {
      const body: Record<string, unknown> = { profile_id: profileId };
      if (profileModel !== undefined) body.profile_model = profileModel;
      const r = await instApi<{ ok: boolean; note?: string }>(inst.title, "/profile", {
        json: body,
      });
      toast(
        r?.note ||
          (profileModel !== undefined
            ? "Now running " + (profileModel || "the account's default model")
            : "Now running as " +
              (profileId === "default"
                ? "the CLI's own login"
                : profiles.find((p) => p.id === profileId)?.label || profileId))
      );
      void refreshInstances();
    } catch (err) {
      toast("Swap failed: " + (err as Error).message);
    } finally {
      setBusy(false);
      setModels(null); // refetch next open (the key's catalog can change)
    }
  };

  return (
    <>
      <span
        ref={chipRef}
        className="tok usage-trigger acct-chip"
        title="Account this agent runs as — click to swap"
        onClick={(ev) => {
          ev.stopPropagation();
          if (!busy) setOpen((o) => !o);
        }}
      >
        <span className="usage-head">{busy ? "swapping…" : "@" + label}</span>
        <span className="caret">▾</span>
      </span>
      {open && chipRef.current && (
        <UsagePopover anchor={chipRef.current} onClose={() => setOpen(false)}>
          <div className="usage-pop-head">Run this session as</div>
          <table className="usage-pop-tbl">
            <tbody>
              <tr
                className="acct-pop-row"
                style={{ cursor: "pointer" }}
                onClick={() => swap("default")}
              >
                <td>CLI's own login</td>
                <td className="num">{effective === "" ? "✓" : ""}</td>
              </tr>
              {profiles.map((p) => (
                <tr
                  key={p.id}
                  className="acct-pop-row"
                  style={{ cursor: "pointer" }}
                  onClick={() => swap(p.id)}
                >
                  <td>{p.label || p.id}</td>
                  <td className="num">{effective === p.id ? "✓" : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {modelCapable && (
            <>
              <div className="usage-pop-head">Model</div>
              <div style={{ padding: "4px 8px" }}>
                {models && models.length ? (
                  <select
                    id="acct-chip-model"
                    style={{ width: "100%" }}
                    value={
                      models.includes(inst.profile_model || "")
                        ? inst.profile_model
                        : ""
                    }
                    onClick={(ev) => ev.stopPropagation()}
                    onChange={(ev) =>
                      swap(inst.profile_id || "", ev.target.value)
                    }
                  >
                    <option value="">
                      {effectiveProfile?.model
                        ? `Account default (${effectiveProfile.model})`
                        : "Account default"}
                    </option>
                    {models.map((m) => (
                      <option key={m} value={m}>
                        {m}
                      </option>
                    ))}
                  </select>
                ) : (
                  <span className="usage-pop-note">
                    {models === null ? "loading models…" : inst.profile_model || effectiveProfile?.model || "account default"}
                  </span>
                )}
              </div>
              <div className="usage-pop-note">
                Full catalog from the account's key; picking one pins it for
                this session (restarts the agent). On "Account default" with no
                pin, Claude Code's own /model shows the gateway's curated
                picker instead — a pinned model bypasses that menu.
              </div>
            </>
          )}
          <div className="usage-pop-note">
            Swapping restarts the agent under the new identity. Files, diff and
            terminal stay; the conversation continues only if the new account
            has seen it before (conversations live per account).
          </div>
        </UsagePopover>
      )}
    </>
  );
}
