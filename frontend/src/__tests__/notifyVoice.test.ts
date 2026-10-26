/** Pocket mode in Settings → Notifications: the "Include the agent's words
 * in pushes" switch inside the ntfy fold, its hint (the privacy sentence
 * only on the public server), the iPhone Announce line, and that the switch
 * saves through the ntfy view like the fields around it. */

import { describe, expect, it } from "vitest";
import {
  AGENT_WORDS_HINT,
  AGENT_WORDS_LABEL,
  AGENT_WORDS_PUBLIC_HINT,
  ANNOUNCE_TIP,
  agentWordsHint,
} from "../components/settings/screens/notifyCopy";
import { dropComments, src } from "./sourceText";

const SCREEN = dropComments(src("components/settings/screens/Notifications.tsx"));

describe("agentWordsHint", () => {
  it("names what travels and where, on any server", () => {
    expect(agentWordsHint(false)).toBe(AGENT_WORDS_HINT);
    expect(AGENT_WORDS_HINT).toContain("spoken summary of a finished turn");
    expect(AGENT_WORDS_HINT).toContain("question of a dialog");
    expect(AGENT_WORDS_HINT).toContain("your ntfy server");
  });

  it("adds the public-server consequence only on ntfy.sh", () => {
    expect(agentWordsHint(true)).toBe(AGENT_WORDS_HINT + " " + AGENT_WORDS_PUBLIC_HINT);
    expect(agentWordsHint(true)).toContain("a public server stores them");
    expect(agentWordsHint(false)).not.toContain("public server");
  });

  it("the iPhone line says where Announce Notifications lives", () => {
    expect(ANNOUNCE_TIP.startsWith("iPhone:")).toBe(true);
    expect(ANNOUNCE_TIP).toContain("Announce Notifications");
    expect(ANNOUNCE_TIP).toContain("AirPods");
  });
});

describe("the switch in the ntfy fold", () => {
  it("is a switch with the label, the hint for the configured server, and the iPhone line", () => {
    expect(SCREEN).toContain('id="ntfy-agent-words"');
    expect(SCREEN).toContain("{AGENT_WORDS_LABEL}");
    expect(SCREEN).toContain("agentWordsHint(!!view.public_server)");
    expect(SCREEN).toContain('id="ntfy-announce-tip"');
    expect(SCREEN).toContain("{ANNOUNCE_TIP}");
    expect(AGENT_WORDS_LABEL).toBe("Include the agent's words in pushes");
  });

  it("sits after the click URL field and saves through the ntfy view", () => {
    const click = SCREEN.indexOf('id="ntfy-click"');
    const words = SCREEN.indexOf('id="ntfy-agent-words"');
    const test = SCREEN.indexOf('id="ntfy-test"');
    expect(click).toBeGreaterThan(-1);
    expect(words).toBeGreaterThan(click);
    expect(test).toBeGreaterThan(words);
    // The same `save` the other ntfy fields use (POST /api/notify/ntfy).
    expect(SCREEN).toContain("save({ agent_words: e.target.checked }");
    // On by default: an older server that sends no flag reads as on.
    expect(SCREEN).toContain("checked={view.agent_words !== false}");
  });
});
