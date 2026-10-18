/** Generic settings-field engine (port of section 21's [data-group]
 * [data-field] wiring): populate from GET /api/settings, persist per-field on
 * change via POST {group:{field:value}}. Secrets never receive the stored
 * value — "•••set" marks a saved one and an empty submit keeps it. */

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api } from "../../api/client";
import type { Json } from "../../api/types";
import { toast } from "../../lib/toast";
import { flashSaved } from "./savedCue";
import {
  fetchSettingsDoc,
  putSettingsDoc,
  refreshConfig,
  useSettingsDoc,
} from "../../state/queries";

export const SECRET_MASK = "•••set";

export interface SettingsModel {
  settings: Json;
  reload(): Promise<void>;
  /** Persist one field; re-applies the server's echo. Resolves false when the
   * save failed (already toasted) so a caller never reports success for it. */
  saveField(group: string, field: string, value: unknown): Promise<boolean>;
  /** Persist a whole group patch (e.g. the PR-review repos array). */
  saveGroup(group: string, patch: Json, okMsg?: string): Promise<boolean>;
  /** Persist several groups in ONE `POST /api/settings` — one echo, one
   * settings-hook pass, so a change that spans `github` and `repository` (a
   * repository's "Use it for" ticks) restarts the pipeline once, and can't
   * land half-applied. Resolves false on failure — "Save failed" is already
   * toasted, and the caller must not follow it with its own success line. */
  saveGroups(patches: Record<string, Json>, okMsg?: string): Promise<boolean>;
  get(group: string, field: string): unknown;
}

/** The settings document lives in the query cache rather than in this hook's
 * state, so the three dialogs that mount this model share one copy — and, more
 * to the point, so the shell can warm it before any of them is opened. Held in
 * component state, every open of Intake or Settings paid for a fresh
 * `/api/settings` round trip with the fields blank until it landed. */
/** Stable identity for "not loaded yet", so `get`'s memo doesn't churn on every
 * render before the document lands. */
const EMPTY_SETTINGS: Json = {};

export function useSettingsModel(open: boolean): SettingsModel {
  const settings = useSettingsDoc(open).data ?? EMPTY_SETTINGS;

  const reload = useCallback(async () => {
    try {
      await fetchSettingsDoc();
    } catch {
      /* leave the last known document in place on error */
    }
  }, []);

  const saveField = useCallback(
    async (group: string, field: string, value: unknown) => {
      try {
        const r = await api<{ settings?: Json }>("/api/settings", {
          json: { [group]: { [field]: value } },
        });
        putSettingsDoc(r?.settings || {});
        // Some settings are also reported on /api/config, which the whole app
        // reads (caps, ide_name, the resolved fast-track rung). Invalidating it
        // here — rather than per call site — is what makes a saved setting take
        // effect in already-open windows instead of waiting for a reload. It was
        // hand-rolled in exactly one screen before, which is precisely how the
        // fast-track rows shipped without it.
        void refreshConfig();
        // "✓ Saved" by the control you touched (savedCue.ts); a toast only
        // when there is no such control on screen. Just "Saved" either way:
        // the raw key ("Saved window budget usd") was config vocabulary.
        if (!flashSaved()) toast("Saved");
        return true;
      } catch (err) {
        toast("Save failed: " + ((err as Error).message || field));
        reload(); // re-sync on failure
        return false;
      }
    },
    [reload]
  );

  const saveGroups = useCallback(
    async (patches: Record<string, Json>, okMsg?: string) => {
      const groups = Object.keys(patches);
      if (!groups.length) return true;
      try {
        const r = await api<{ settings?: Json }>("/api/settings", { json: patches });
        putSettingsDoc(r?.settings || {});
        void refreshConfig();
        if (okMsg) toast(okMsg);
        return true;
      } catch (err) {
        toast("Save failed: " + ((err as Error).message || groups.join(", ")));
        reload();
        return false;
      }
    },
    [reload]
  );

  const saveGroup = useCallback(
    (group: string, patch: Json, okMsg?: string) => saveGroups({ [group]: patch }, okMsg),
    [saveGroups]
  );

  const get = useCallback(
    (group: string, field: string) => {
      const g = settings[group];
      return g && typeof g === "object" ? (g as Json)[field] : undefined;
    },
    [settings]
  );

  return { settings, reload, saveField, saveGroup, saveGroups, get };
}

export const SettingsCtx = createContext<SettingsModel | null>(null);

export function useSettings(): SettingsModel {
  const ctx = useContext(SettingsCtx);
  if (!ctx) throw new Error("useSettings outside SettingsDialog");
  return ctx;
}

/** Why `raw` is not a number this field takes ("" when it is fine). Blank is
 * always fine — it means the field's default, which the placeholder names
 * ("no limit"), so the message points at it instead of a negative number
 * silently meaning the same thing. */
export function numberFieldError(raw: string, min = 0, placeholder = ""): string {
  const v = raw.trim();
  if (!v) return "";
  const n = Number(v);
  if (Number.isFinite(n) && n >= min) return "";
  const blank = placeholder && !/^[\d.]+$/.test(placeholder) ? ", or leave it blank for " + placeholder : "";
  return "Enter a number, " + min + " or more" + blank + ".";
}

/** One generic settings field bound to group/field. Uncontrolled between
 * saves (mirrors the vanilla change-event flow): value seeds from settings,
 * commits on change/blur. A number field refuses a value below `min`
 * (default 0) with the reason under it, rather than saving it. */
export function SettingField(props: {
  group: string;
  field: string;
  type?: string;
  placeholder?: string;
  title?: string;
  className?: string;
  options?: Array<{ value: string; label: string }>;
  /** Lowest number a `type="number"` field takes (default 0). */
  min?: number;
}) {
  const s = useSettings();
  const stored = s.get(props.group, props.field);
  const isSecret = props.type === "password";
  const display = isSecret ? "" : stored == null ? "" : String(stored);
  const [value, setValue] = useState(display);
  const [error, setError] = useState("");
  useEffect(() => setValue(display), [display]);

  const commit = () => {
    if (isSecret && value === "") return; // blank secret = keep existing
    if (props.type === "number") {
      const why = numberFieldError(value, props.min ?? 0, props.placeholder || "");
      setError(why);
      if (why) return;
    }
    if (value === display && !isSecret) return;
    s.saveField(props.group, props.field, value);
    if (isSecret) setValue("");
  };

  if (props.options) {
    return (
      <select
        data-group={props.group}
        data-field={props.field}
        className={props.className}
        title={props.title}
        value={display}
        onChange={(e) => s.saveField(props.group, props.field, e.target.value)}
      >
        {props.options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    );
  }

  const input = (
    <input
      data-group={props.group}
      data-field={props.field}
      type={props.type || "text"}
      min={props.type === "number" ? props.min ?? 0 : undefined}
      aria-invalid={error ? true : undefined}
      className={props.className}
      autoComplete="off"
      spellCheck={false}
      placeholder={
        isSecret && stored === SECRET_MASK ? "•••set (saved)" : props.placeholder
      }
      title={props.title}
      value={value}
      onChange={(e) => {
        setValue(e.target.value);
        if (error) setError("");
      }}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
      }}
    />
  );
  if (!error) return input;
  return (
    <>
      {input}
      <span className="set-field-error" role="alert">
        {error}
      </span>
    </>
  );
}

// The inline "Are you sure?" row moved to components/InlineConfirm.tsx (it is
// not Settings-only); re-exported so the screens' imports stay as they are.
export { InlineConfirm } from "../InlineConfirm";
