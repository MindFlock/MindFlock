/** The top bar's view of Schedule: the template list and its run rows, kept
 * fresh while the page is open so the Schedule badge can say "a scheduled run
 * failed" without the surface being open.
 *
 * Reads the one shared cache (lib/templates) — no fetch of its own beyond the
 * first `ensureTemplates()` — and refetches when the server says a template
 * run started or failed (`template.run_started` / `template.run_failed` on the
 * event bus; a replay of old history never refetches). A start matters as
 * much as a failure: a good run is what clears "Last run failed". */

import { useEffect, useState } from "react";
import {
  TEMPLATES_CHANGED,
  ensureTemplates,
  templateRunsNow,
  templatesNow,
  type Template,
  type TemplateRunsStatus,
} from "./templates";

export function useScheduleStatus(): { templates: Template[]; status: TemplateRunsStatus } {
  const [templates, setTemplates] = useState<Template[]>(() => templatesNow());
  const [status, setStatus] = useState<TemplateRunsStatus>(() => templateRunsNow());

  useEffect(() => {
    const reload = () => {
      setTemplates(templatesNow());
      setStatus(templateRunsNow());
    };
    document.addEventListener(TEMPLATES_CHANGED, reload);
    void ensureTemplates()
      .then(reload)
      .catch(() => {});
    const bus = window.mindflock?.events;
    const offs: Array<() => void> = [];
    if (bus) {
      for (const name of ["template.run_started", "template.run_failed"]) {
        offs.push(
          bus.subscribe(name, (env) => {
            if (typeof bus.isReplay === "function" && bus.isReplay(env)) return;
            void ensureTemplates(true)
              .then(reload)
              .catch(() => {});
          })
        );
      }
    }
    return () => {
      document.removeEventListener(TEMPLATES_CHANGED, reload);
      offs.forEach((off) => off());
    };
  }, []);

  return { templates, status };
}
