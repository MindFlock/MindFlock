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
  it("counts -2…-99 as a copy number even when the bare name is closed", () => {
    expect(copyTitle("sitecheck-bot7-2", new Set(["sitecheck-bot7-2"]))).toBe("sitecheck-bot7-3");
    expect(copyTitle("bot-12", new Set(["bot-12"]))).toBe("bot-2");
  });
  it("keeps a long trailing number as part of the name", () => {
    expect(copyTitle("sc-25355", new Set(["sc-25355"]))).toBe("sc-25355-2");
    expect(copyTitle("bot7", new Set(["bot7"]))).toBe("bot7-2");
  });
});
