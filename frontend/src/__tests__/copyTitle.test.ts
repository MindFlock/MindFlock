import { describe, expect, it } from "vitest";
import { copyTitle } from "../lib/sessionActions";

describe("a copied session's name", () => {
  it("is the family name plus the lowest free number", () => {
    expect(copyTitle("sitecheck-bot", new Set(["sitecheck-bot"]))).toBe("sitecheck-bot-2");
    const fam = new Set(["sitecheck-bot", "sitecheck-bot-3"]);
    expect(copyTitle("sitecheck-bot", fam)).toBe("sitecheck-bot-2");
    expect(copyTitle("sitecheck-bot-3", fam)).toBe("sitecheck-bot-2");
    expect(copyTitle("sitecheck-bot-2", new Set(["sitecheck-bot", "sitecheck-bot-2", "sitecheck-bot-3"]))).toBe("sitecheck-bot-4");
  });
  it("keeps a trailing number that has no bare name open", () => {
    expect(copyTitle("sc-25355", new Set(["sc-25355"]))).toBe("sc-25355-2");
    expect(copyTitle("bot7", new Set(["bot7"]))).toBe("bot7-2");
  });
});
