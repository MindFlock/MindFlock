/** The new-session block's two folds (docs/flights.md "As built: round 12
 * new block"): **More options** — the New dialog's account, model,
 * workspace, base branch, template, launch flags and device, drawn as the
 * Sky's chips (the dialog's own Agent / Account / Model fields are native
 * <select>s, which Flights never uses) — and **From a session**, the picker
 * of live sessions a new one can fork from.
 *
 * They write the block's `BlockOptions` (lib/newBlock), which the block hands
 * to the New dialog's body builder; the dialog's account steering
 * (`accountSetter`) and launch-flag chips (`FlagChips`) are reused as they are. */

import { useMemo, useState, type Dispatch, type SetStateAction } from "react";
import type { AuthProfilesResponse, RepoBranches } from "../../api/types";
import { FlagChips } from "../dialogs/FlagChips";
import { accountSetter } from "../dialogs/newSession/AgentAccount";
import type { Template } from "../../lib/templates";
import { branchChoices, filterParents, PARENT_FILTER_AT, type BlockOptions, type ParentChoice } from "../../lib/newBlock";
import { ChipRow } from "./controls";

type Set<T> = Dispatch<SetStateAction<T>>;

/** A setter for one field of the options, shaped like a useState setter. */
export function optionSetter<K extends keyof BlockOptions>(set: Set<BlockOptions>, k: K): Set<BlockOptions[K]> {
  return (v) => set((o) => ({ ...o, [k]: typeof v === "function" ? (v as (p: BlockOptions[K]) => BlockOptions[K])(o[k]) : v }));
}

export interface MoreOptionsProps {
  program: string;
  setProgram(name: string): void;
  options: BlockOptions;
  setOptions: Set<BlockOptions>;
  launchArgs: string;
  setLaunchArgs(args: string): void;
  authProfiles: AuthProfilesResponse | undefined;
  /** The base branch menu for the folder in the Repository row (null = none yet). */
  branches: RepoBranches | null;
  templates: Template[];
  onTemplate(t: Template | null): void;
  /** The devices a session can start on (empty = only this one: no row). */
  devices: Array<{ device: string; label: string }>;
  /** Forked from a session: its workspace, base and device are the fork's. */
  forkedFrom?: string;
}

export function MoreOptions({
  program,
  setProgram,
  options: o,
  setOptions,
  launchArgs,
  setLaunchArgs,
  authProfiles,
  branches,
  templates,
  onTemplate,
  devices,
  forkedFrom,
}: MoreOptionsProps) {
  const [profileModels, setProfileModels] = useState<Record<string, string[]>>({});
  const [otherBranch, setOtherBranch] = useState("");
  const profiles = authProfiles?.profiles || [];
  const setAccount = accountSetter({
    authProfiles,
    program,
    profileModels,
    canonAgent: (raw) => (raw.trim().split(/\s+/)[0] || "").toLowerCase(),
    setAgent: setProgram,
    setProfileId: optionSetter(setOptions, "profileId"),
    setProfileModel: optionSetter(setOptions, "profileModel"),
    setProfileModels,
  });
  const selected = profiles.find((p) => p.id === o.profileId);
  const chips = useMemo(() => branchChoices(branches), [branches]);
  const remoteOf = (name: string) => !!branches?.branches.some((b) => b.name === name && b.remote);
  const setBase = (name: string) => setOptions((p) => ({ ...p, baseBranch: name, baseRemote: remoteOf(name) }));
  const baseDefault = branches?.default ? "Default (" + branches.default + ")" : "Default";
  const baseOptions = [{ value: "", label: baseDefault }, ...chips];
  if (o.baseBranch && !baseOptions.some((c) => c.value === o.baseBranch)) baseOptions.push({ value: o.baseBranch, label: o.baseBranch, remote: o.baseRemote });

  return (
    <div className="sky-new-more-body">
      {!o.device && profiles.length > 0 && (
        <div className="fl-fld fl-fld-wide">
          <span className="fl-fld-label">Account</span>
          <ChipRow
            className="sky-new-account"
            label="Account"
            value={o.profileId}
            options={[
              { value: "", label: authProfiles?.default_profile ? "App default (" + authProfiles.default_profile + ")" : "App default" },
              { value: "default", label: "CLI's own login" },
              ...profiles.map((p) => ({ value: p.id, label: p.label || p.id })),
            ]}
            onChange={(id) => setAccount(id)}
          />
        </div>
      )}
      {selected && selected.kind !== "account" && (
        <label className="fl-fld fl-fld-wide">
          <span className="fl-fld-label">Model</span>
          <input
            className="sky-new-input sky-new-model"
            type="text"
            autoComplete="off"
            list="sky-new-models"
            placeholder={selected.model ? "Account default (" + selected.model + ")" : "Account default"}
            value={o.profileModel}
            onChange={(e) => setOptions((p) => ({ ...p, profileModel: e.target.value }))}
          />
          <datalist id="sky-new-models">
            {(profileModels[selected.id] || []).map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        </label>
      )}
      <div className="fl-fld fl-fld-wide">
        <span className="fl-fld-label">Workspace</span>
        {forkedFrom ? (
          <span className="sky-new-note">A worktree of its own, cut at “{forkedFrom}”'s HEAD</span>
        ) : o.provision ? (
          <span className="sky-new-note">Provisioned ({o.strategy}) — from the template</span>
        ) : (
          <ChipRow
            className="sky-new-workspace"
            label="Workspace"
            value={o.inPlace ? "in-place" : "worktree"}
            options={[
              { value: "worktree", label: "Worktree", title: "Its own branch, so several sessions can run at once" },
              { value: "in-place", label: "In place", title: "Edits your checkout as it is, on its current branch" },
            ]}
            onChange={(v) => setOptions((p) => ({ ...p, inPlace: v === "in-place" }))}
          />
        )}
      </div>
      {!forkedFrom && !o.inPlace && !o.provision && (
        <div className="fl-fld fl-fld-wide">
          <span className="fl-fld-label">Base branch</span>
          <ChipRow className="sky-new-base" label="Base branch" value={o.baseBranch} options={baseOptions} onChange={setBase} />
          {(branches?.branches.length || 0) > chips.length && (
            <input
              className="sky-new-input sky-new-base-other"
              type="text"
              autoComplete="off"
              list="sky-new-branches"
              aria-label="Another base branch"
              placeholder="Another branch…"
              value={otherBranch}
              onChange={(e) => {
                setOtherBranch(e.target.value);
                if (branches?.branches.some((b) => b.name === e.target.value.trim())) setBase(e.target.value.trim());
              }}
            />
          )}
          <datalist id="sky-new-branches">
            {(branches?.branches || []).map((b) => (
              <option key={b.name} value={b.name} />
            ))}
          </datalist>
        </div>
      )}
      {templates.length > 0 && (
        <div className="fl-fld fl-fld-wide">
          <span className="fl-fld-label">Template</span>
          <ChipRow
            className="sky-new-templates"
            label="Template"
            value={o.template}
            options={[{ value: "", label: "None" }, ...templates.map((t) => ({ value: t.name, label: t.name, title: t.prompt || undefined }))]}
            onChange={(name) => onTemplate(templates.find((t) => t.name === name) || null)}
          />
        </div>
      )}
      <div className="fl-fld fl-fld-wide">
        <span className="fl-fld-label">Launch flags</span>
        <input
          className="sky-new-input sky-new-args"
          type="text"
          autoComplete="off"
          aria-label="Launch flags"
          placeholder="e.g. --verbose — after the agent's saved defaults"
          value={launchArgs}
          onChange={(e) => setLaunchArgs(e.target.value)}
        />
        <FlagChips provider={program} value={launchArgs} onChange={setLaunchArgs} />
      </div>
      {devices.length > 0 && !forkedFrom && (
        <div className="fl-fld fl-fld-wide">
          <span className="fl-fld-label">Device</span>
          <ChipRow
            className="sky-new-device"
            label="Device"
            value={o.device}
            options={[{ value: "", label: "This device" }, ...devices.map((d) => ({ value: d.device, label: d.label }))]}
            onChange={(device) => setOptions((p) => ({ ...p, device, profileId: device ? "" : p.profileId, profileModel: device ? "" : p.profileModel, baseBranch: "", baseRemote: false }))}
          />
        </div>
      )}
    </div>
  );
}

/** From a session: the live sessions on this device, a filter past
 * `PARENT_FILTER_AT` of them; a click forks from that one. */
export function ParentPicker({ choices, value, onPick }: { choices: ParentChoice[]; value: string; onPick(c: ParentChoice): void }) {
  const [q, setQ] = useState("");
  const shown = filterParents(choices, q);
  if (!choices.length) return <p className="sky-new-note sky-new-parents-none">No sessions running on this device to fork from.</p>;
  return (
    <div className="sky-new-parents">
      {choices.length > PARENT_FILTER_AT && (
        <input
          className="sky-new-input sky-new-parent-filter"
          type="search"
          autoComplete="off"
          aria-label="Filter sessions"
          placeholder={"Filter " + choices.length + " sessions…"}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      )}
      <div className="fl-chiprow sky-new-parent-list" role="radiogroup" aria-label="Fork from a session">
        {shown.map((c) => (
          <button
            key={c.title}
            type="button"
            role="radio"
            aria-checked={c.title === value}
            className={"fl-pick" + (c.title === value ? " on" : "")}
            data-value={c.title}
            title={c.repo + (c.branch ? " · " + c.branch : "")}
            onClick={() => onPick(c)}
          >
            {c.name}
          </button>
        ))}
        {!shown.length && <span className="sky-new-note">No session matches “{q}”.</span>}
      </div>
    </div>
  );
}
