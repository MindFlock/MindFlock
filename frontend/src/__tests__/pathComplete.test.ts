/** Shell-style Tab completion for folder fields (lib/pathComplete.ts) — the
 * owner's "auto-fill the next string by hitting Tab, like a Linux terminal". */

import { describe, expect, it, vi } from "vitest";
import {
  commonPrefix,
  completeFrom,
  completionHint,
  cycle,
  matchNames,
  splitForCompletion,
  tabComplete,
  type Listing,
} from "../lib/pathComplete";

describe("splitForCompletion", () => {
  it("cuts at the last slash: the folder to list, the name being typed", () => {
    expect(splitForCompletion("~/Mind")).toEqual({ dir: "~", partial: "Mind", prefix: "~/" });
    expect(splitForCompletion("/home/me/")).toEqual({ dir: "/home/me", partial: "", prefix: "/home/me/" });
    expect(splitForCompletion("/home/me/pro")).toEqual({ dir: "/home/me", partial: "pro", prefix: "/home/me/" });
    expect(splitForCompletion("~/a/b/c")).toEqual({ dir: "~/a/b", partial: "c", prefix: "~/a/b/" });
  });

  it("lists the root for a top-level name", () => {
    expect(splitForCompletion("/")).toEqual({ dir: "/", partial: "", prefix: "/" });
    expect(splitForCompletion("/ho")).toEqual({ dir: "/", partial: "ho", prefix: "/" });
  });

  it("reads a bare ~ (and ~Mi) as home", () => {
    expect(splitForCompletion("~")).toEqual({ dir: "~", partial: "", prefix: "~/" });
    expect(splitForCompletion("~Mi")).toEqual({ dir: "~", partial: "Mi", prefix: "~/" });
  });

  it("reads relative text from home (\"\" = the server's default) and writes it back as ~/", () => {
    expect(splitForCompletion("Mi")).toEqual({ dir: "", partial: "Mi", prefix: "~/" });
    expect(splitForCompletion("src/ap")).toEqual({ dir: "~/src", partial: "ap", prefix: "~/src/" });
  });
});

describe("matchNames", () => {
  const names = ["MindFlock", "mindmap", "Music", ".config", ".cache"];

  it("matches by prefix, exact case first", () => {
    expect(matchNames("Mi", names)).toEqual(["MindFlock"]);
    expect(matchNames("mi", names)).toEqual(["mindmap"]);
  });

  it("ignores case only when no name has the exact case", () => {
    expect(matchNames("MU", names)).toEqual(["Music"]);
    expect(matchNames("MIND", names)).toEqual(["MindFlock", "mindmap"]);
  });

  it("offers hidden folders only for a partial that starts with a dot", () => {
    expect(matchNames("", names)).toEqual(["MindFlock", "mindmap", "Music"]);
    expect(matchNames(".", names)).toEqual([".config", ".cache"]);
    expect(matchNames(".co", names)).toEqual([".config"]);
  });
});

describe("commonPrefix", () => {
  it("is the longest shared start, optionally case-folded", () => {
    expect(commonPrefix(["project-a", "project-b", "projects"])).toBe("project");
    expect(commonPrefix(["Alpha", "alpine"])).toBe("");
    expect(commonPrefix(["Alpha", "alpine"], true)).toBe("Alp");
    expect(commonPrefix([])).toBe("");
  });
});

describe("completeFrom", () => {
  const home = ["MindFlock", "mindflock-site", "Music", "projects", "proto"];

  it("fills a single match whole, with a trailing slash (Tab, Tab, Tab walks down)", () => {
    expect(completeFrom("~/Mu", home)).toEqual({ text: "~/Music/", matches: ["Music"], prefix: "~/" });
    expect(completeFrom("/home/me/Min", home).text).toBe("/home/me/MindFlock/");
    // and the next Tab lists the folder it just named
    expect(splitForCompletion("~/Music/")).toMatchObject({ dir: "~/Music", partial: "" });
  });

  it("fills the common prefix of several and returns them all", () => {
    const r = completeFrom("~/pr", home);
    expect(r.text).toBe("~/pro");
    expect(r.matches).toEqual(["projects", "proto"]);
  });

  it("leaves the text alone when already at the common prefix", () => {
    const r = completeFrom("~/pro", home);
    expect(r.text).toBe("~/pro");
    expect(r.matches).toHaveLength(2);
  });

  it("completes in the case the folders have when the typed case matches none", () => {
    expect(completeFrom("~/mu", home).text).toBe("~/Music/");
    // "mind" matches "mindflock-site" exactly: MindFlock is not offered.
    expect(completeFrom("~/mind", home).text).toBe("~/mindflock-site/");
    // Folded on both: the shared prefix keeps the first name's spelling.
    const r = completeFrom("~/MINDF", ["MindFlock", "mindflock-site"]);
    expect(r.text).toBe("~/MindFlock");
    expect(r.matches).toEqual(["MindFlock", "mindflock-site"]);
  });

  it("never shortens what was typed", () => {
    expect(completeFrom("~/ALP", ["Alpha", "alpine"]).text).toBe("~/ALP");
  });

  it("returns no matches (and the same text) on a miss", () => {
    expect(completeFrom("~/zzz", home)).toEqual({ text: "~/zzz", matches: [], prefix: "~/" });
  });

  it("writes relative text back from home", () => {
    expect(completeFrom("Mu", home).text).toBe("~/Music/");
    expect(completeFrom("src/a", ["api", "app"]).text).toBe("~/src/ap");
    // Nothing to add: the typed text stays as it was.
    expect(completeFrom("src/ap", ["api", "app"]).text).toBe("src/ap");
  });

  it("turns a bare ~ into ~/, and lists home on the next Tab", () => {
    expect(completeFrom("~", home).text).toBe("~/");
    expect(completeFrom("~", []).text).toBe("~/");
  });

  it("hides dotfolders unless asked for", () => {
    expect(completeFrom("~/", [".config", "src"]).text).toBe("~/src/");
    expect(completeFrom("~/.", [".config", "src"]).text).toBe("~/.config/");
  });
});

describe("cycle", () => {
  const m = ["projects", "proto", "prow"];

  it("walks forward from the common prefix, then wraps", () => {
    expect(cycle(m, "~/pro", "~/")).toBe("~/projects/");
    expect(cycle(m, "~/projects/", "~/")).toBe("~/proto/");
    expect(cycle(m, "~/proto/", "~/")).toBe("~/prow/");
    expect(cycle(m, "~/prow/", "~/")).toBe("~/projects/");
  });

  it("walks backward with step -1 (Shift+Tab), starting from the last", () => {
    expect(cycle(m, "~/pro", "~/", -1)).toBe("~/prow/");
    expect(cycle(m, "~/projects/", "~/", -1)).toBe("~/prow/");
  });

  it("knows a match written without its slash", () => {
    expect(cycle(m, "~/proto", "~/")).toBe("~/prow/");
  });

  it("does nothing with no matches", () => {
    expect(cycle([], "~/x", "~/")).toBe("~/x");
  });
});

describe("tabComplete", () => {
  it("lists the text's folder once, then answers the same folder from the cache", async () => {
    const list = vi.fn(async () => ["Music", "MindFlock"]);
    const cache: { current: Listing | null } = { current: null };
    expect((await tabComplete("~/Mu", list, cache))?.text).toBe("~/Music/");
    expect((await tabComplete("~/Mi", list, cache))?.text).toBe("~/MindFlock/");
    expect(list).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledWith("~");
    await tabComplete("~/Music/", list, cache);
    expect(list).toHaveBeenLastCalledWith("~/Music");
    expect(list).toHaveBeenCalledTimes(2);
  });

  it("asks for home with an empty dir for a bare name", async () => {
    const list = vi.fn(async () => ["Music"]);
    expect((await tabComplete("Mu", list, { current: null }))?.text).toBe("~/Music/");
    expect(list).toHaveBeenCalledWith("");
  });

  it("is null when the folder can't be listed, and caches nothing", async () => {
    const cache: { current: Listing | null } = { current: null };
    expect(await tabComplete("/nope/x", async () => Promise.reject(new Error("400")), cache)).toBeNull();
    expect(cache.current).toBeNull();
  });
});

describe("completionHint", () => {
  it("says what happened, for the polite live region", () => {
    expect(completionHint("~/pr", completeFrom("~/pr", ["projects", "proto", "prow"]))).toEqual({
      hint: "3 folders match",
      miss: false,
    });
    expect(completionHint("~/Mu", completeFrom("~/Mu", ["Music"]))).toEqual({
      hint: "completed to ~/Music/",
      miss: false,
    });
  });

  it("flags a miss (the field shakes) and an unlistable folder", () => {
    expect(completionHint("~/zz", completeFrom("~/zz", ["Music"]))).toEqual({ hint: "No folder matches", miss: true });
    expect(completionHint("/nope/x", null)).toEqual({ hint: "No folder at /nope", miss: true });
  });
});
