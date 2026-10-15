/** The Verify tour: the r5-verify judge's five steps — the row chip, who
 * checks each step, answering, Check now / Fix, and merging. Every step names
 * its data-tour hook first and a stable fallback after it:
 *
 * - `verify-chip`    a sidebar row's verdict chip (SidebarRow.tsx);
 * - `verify-badges`  a step's YOU / AGENT badge (VerifyStep.tsx);
 * - `verify-answer`  a step's Pass · Fail · Can't check (VerifyStep.tsx);
 * - `verify-check`   a card's Check now, primary or secondary (VerifyPlan.tsx);
 * - `verify-merge`   a card's merge pill, "not merged" / "merged → main".
 *
 * On an empty Verify only the first has anything to point at; the rest centre
 * until a checklist exists. */
import type { TourDef } from ".";

export const VERIFY_TOUR: TourDef = {
  id: "verify",
  title: "Verify",
  steps: [
    {
      target: '[data-tour="verify-chip"], #verify-title',
      title: "Does the change work?",
      body: "This chip is a session's checklist: the steps that prove its change works — click it to open Verify on that branch.",
    },
    {
      target: '[data-tour="verify-badges"]',
      title: "You or the agent",
      body: "The agent checks the AGENT steps from a shell; the YOU steps need your eyes.",
    },
    {
      target: '[data-tour="verify-answer"]',
      title: "Answer a step",
      body: "Pass, Fail or Can't check — only Pass counts as working, and pressing it again takes it back.",
    },
    {
      target: '[data-tour="verify-check"], .vf-fix',
      title: "Check now, or fix it",
      body: "Check now has an agent run the steps on the branch; if one fails, Fix starts a session with the failure attached.",
    },
    {
      target: '[data-tour="verify-merge"]',
      title: "Then merge",
      body: "The verdict shows beside Open PR, and after merge the checklist comes back to be checked on the live branch.",
    },
  ],
};
