/** The Verify tour: what a checklist is, who checks each step, answering, and
 * Check now. Seeded from the r5-verify judge's tutorial draft. The step,
 * answer and Check now hooks are Verify's owner's to add; until then those
 * steps centre (and on an empty Verify there is nothing to point at yet). */
import type { TourDef } from ".";

export const VERIFY_TOUR: TourDef = {
  id: "verify",
  title: "Verify",
  steps: [
    {
      target: "#verify-title",
      title: "Does the change work?",
      body: "Each session's change gets a checklist: the steps that prove it actually works.",
    },
    {
      target: '[data-tour="verify-step-who"]',
      title: "You or the agent",
      body: "The agent checks the AGENT steps from a shell; the YOU steps need your eyes.",
    },
    {
      target: '[data-tour="verify-answer"]',
      title: "Answer a step",
      body: "Pass, Fail or Can't check — only Pass counts as working, and you can undo.",
    },
    {
      target: '[data-tour="verify-check-now"]',
      title: "Check now",
      body: "Check now has an agent run the steps on the branch; if one fails, Fix starts a session with the failure attached.",
    },
  ],
};
