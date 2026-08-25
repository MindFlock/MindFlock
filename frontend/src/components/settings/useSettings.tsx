/** Generic settings-field engine (port of section 21's [data-group]
 * [data-field] wiring): populate from GET /api/settings, persist per-field on
 * change via POST {group:{field:value}}. Secrets never receive the stored
 * value — "•••set" marks a saved one and an empty submit keeps it. */

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api } from "../../api/client";
import type { Json } from "../../api/types";
import { toast } from "../../lib/toast";
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
  /** Persist one field; re-applies the server's echo. */
  saveField(group: string, field: string, value: unknown): Promise<void>;
  /** Persist a whole group patch (e.g. the PR-review repos array). */
  saveGroup(group: string, patch: Json, okMsg?: string): Promise<void>;
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
        toast("Saved " + field.replace(/_/g, " "));
      } catch (err) {
        toast("Save failed: " + ((err as Error).message || field));
        reload(); // re-sync on failure
      }
    },
    [reload]
  );

  const saveGroup = useCallback(
    async (group: string, patch: Json, okMsg?: string) => {
      try {
        const r = await api<{ settings?: Json }>("/api/settings", { json: { [group]: patch } });
        putSettingsDoc(r?.settings || {});
        void refreshConfig();
        if (okMsg) toast(okMsg);
      } catch (err) {
        toast("Save failed: " + ((err as Error).message || group));
        reload();
      }
    },
    [reload]
  );

  const get = useCallback(
    (group: string, field: string) => {
      const g = settings[group];
      return g && typeof g === "object" ? (g as Json)[field] : undefined;
    },
    [settings]
  );

  return { settings, reload, saveField, saveGroup, get };
}

export const SettingsCtx = createContext<SettingsModel | null>(null);

export function useSettings(): SettingsModel {
  const ctx = useContext(SettingsCtx);
  if (!ctx) throw new Error("useSettings outside SettingsDialog");
  return ctx;
}

/** One generic settings field bound to group/field. Uncontrolled between
 * saves (mirrors the vanilla change-event flow): value seeds from settings,
 * commits on change/blur. */
export function SettingField(props: {
  group: string;
  field: string;
  type?: string;
  placeholder?: string;
  title?: string;
  className?: string;
  options?: Array<{ value: string; label: string }>;
}) {
  const s = useSettings();
  const stored = s.get(props.group, props.field);
  const isSecret = props.type === "password";
  const display = isSecret ? "" : stored == null ? "" : String(stored);
  const [value, setValue] = useState(display);
  useEffect(() => setValue(display), [display]);

  const commit = () => {
    if (isSecret && value === "") return; // blank secret = keep existing
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

  return (
    <input
      data-group={props.group}
      data-field={props.field}
      type={props.type || "text"}
      className={props.className}
      autoComplete="off"
      spellCheck={false}
      placeholder={
        isSecret && stored === SECRET_MASK ? "•••set (saved)" : props.placeholder
      }
      title={props.title}
      value={value}
      onChange={(e) => setValue(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
      }}
    />
  );
}

/** A checkbox settings field (persists booleans). */
export function SettingCheck(props: { group: string; field: string; label: React.ReactNode }) {
  const s = useSettings();
  const stored = s.get(props.group, props.field);
  const checked = stored === true || stored === "true" || stored === "1";
  return (
    <label className="check">
      <input
        type="checkbox"
        data-group={props.group}
        data-field={props.field}
        checked={checked}
        onChange={(e) => s.saveField(props.group, props.field, e.target.checked)}
      />
      {props.label}
    </label>
  );
}
