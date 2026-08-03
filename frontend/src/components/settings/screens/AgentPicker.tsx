/** A "which coding CLI runs this?" dropdown, shared by the surfaces that can
 * each pick their own agent: PR review, issue handling and the Assistant.
 *
 * Ticketing has its own copy inline because it renders one picker per source
 * row inside that screen's own grid; everything else is a single field and uses
 * this. The empty option is always offered and always means "fall back" — never
 * a provider literally named "" — and it names the fallback so the choice isn't
 * a mystery.
 */

import { useEffect, useState } from "react";
import { api } from "../../../api/client";

interface Choices {
  names: string[];
  fallback: string;
}

/** Providers the app knows about, plus the app-wide default's name. Shared by
 * every picker instance on a screen; cheap enough to fetch per mount. */
export function useAgentChoices(): Choices {
  const [choices, setChoices] = useState<Choices>({ names: [], fallback: "" });
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const p = await api<{ providers?: Array<{ name: string }>; default?: string }>(
          "/api/providers"
        );
        if (!alive) return;
        setChoices({
          names: (p?.providers || []).map((x) => x.name).filter(Boolean),
          fallback: p?.default || "",
        });
      } catch {
        /* keep the empty list — the picker still offers "app default" */
      }
    })();
    return () => {
      alive = false;
    };
  }, []);
  return choices;
}

export function AgentPicker({
  label,
  hint,
  value,
  choices,
  fallbackLabel,
  onChange,
}: {
  label: string;
  hint: React.ReactNode;
  value: string;
  choices: Choices;
  /** Overrides the empty option's text — the Assistant's fallback is the app
   *  default too, but it reads better spelled out on that screen. */
  fallbackLabel?: string;
  onChange(next: string): void;
}) {
  const empty =
    fallbackLabel ||
    (choices.fallback ? `App default (${choices.fallback})` : "App default");
  return (
    <label className="set-row">
      <span className="set-label">{label}</span>
      <select value={value || ""} onChange={(e) => onChange(e.target.value)}>
        <option value="">{empty}</option>
        {choices.names.map((n) => (
          <option key={n} value={n}>
            {n}
          </option>
        ))}
      </select>
      <span className="set-hint">{hint}</span>
    </label>
  );
}
