/** The Sky's weather, live: the plan window from the top bar's own usage query
 * (useUsage — no new endpoint, the same cache entry and event-driven
 * refreshes), the quiet hours from the settings document, the local hour on a
 * minute's tick, and the reduced-motion preference. lib/weather.ts decides. */

import { useEffect, useState } from "react";
import { useSettingsDoc, useUsage } from "../../state/queries";
import { localHour, quietHours, weatherFor, windowUsagePct, type Weather } from "../../lib/weather";
import { useNow } from "../../lib/useNow";
import { prefersReducedMotion } from "./Bird";

/** The reduced-motion preference, following it if it changes while mounted. */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(prefersReducedMotion);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const on = () => setReduced(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return reduced;
}

export function useWeather(): Weather {
  const { data: usage } = useUsage();
  const { data: settings } = useSettingsDoc(true);
  const now = useNow(60_000);
  const reducedMotion = useReducedMotion();
  return weatherFor({ usagePct: windowUsagePct(usage), hour: localHour(now), quiet: quietHours(settings), reducedMotion });
}
