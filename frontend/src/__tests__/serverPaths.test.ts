import { afterEach, describe, expect, it } from "vitest";
import { fullPathHint, isWindowsFullPath, noteServerHome, resetServerPaths, serverIsWindows } from "../lib/serverPaths";
import { looksLikePath } from "../components/dialogs/newSession";
import { completeFrom, cycle, splitForCompletion } from "../lib/pathComplete";

afterEach(() => resetServerPaths());

describe("server paths (native Windows)", () => {
  it("knows a Windows full path", () => {
    expect(isWindowsFullPath("C:\\Users\\emand")).toBe(true);
    expect(isWindowsFullPath("c:/work")).toBe(true);
    expect(isWindowsFullPath("\\\\nas\\share")).toBe(true);
    expect(isWindowsFullPath("Downloads")).toBe(false);
    expect(isWindowsFullPath("/home/me")).toBe(false);
  });

  it("a Linux/macOS server keeps / and ~ only", () => {
    noteServerHome("/home/me");
    expect(serverIsWindows()).toBe(false);
    expect(looksLikePath("C:\\Users\\emand\\Downloads")).toBe(false);
    expect(looksLikePath("~/src")).toBe(true);
    expect(fullPathHint()).toBe("a full path starting with / or ~");
  });

  it("a Windows server takes C:\\…, C:/… and UNC paths as folders (the owner's Downloads)", () => {
    noteServerHome("C:\\Users\\emand");
    expect(serverIsWindows()).toBe(true);
    expect(looksLikePath("C:\\Users\\emand\\Downloads")).toBe(true);
    expect(looksLikePath(" C:/Users/emand ")).toBe(true);
    expect(looksLikePath("\\\\nas\\share\\repo")).toBe(true);
    expect(looksLikePath("~")).toBe(true);
    expect(looksLikePath("Downloads")).toBe(false);
    expect(fullPathHint()).toContain("C:\\Users\\you\\project");
  });

  it("Tab completion splits a Windows path at its last separator and keeps it", () => {
    noteServerHome("C:\\Users\\emand");
    expect(splitForCompletion("C:\\Users\\em")).toEqual({ dir: "C:\\Users", partial: "em", prefix: "C:\\Users\\" });
    expect(splitForCompletion("C:\\")).toEqual({ dir: "C:\\", partial: "", prefix: "C:\\" });
    expect(splitForCompletion("C:/work/mi")).toEqual({ dir: "C:/work", partial: "mi", prefix: "C:/work/" });
    expect(completeFrom("C:\\Users\\em", ["emand", "Public"]).text).toBe("C:\\Users\\emand\\");
    expect(completeFrom("C:/work/mi", ["mindflock"]).text).toBe("C:/work/mindflock/");
    expect(cycle(["a", "b"], "C:\\x\\a\\", "C:\\x\\")).toBe("C:\\x\\b\\");
  });

  it("completion on a Linux server is unchanged", () => {
    noteServerHome("/home/me");
    expect(splitForCompletion("/home/me/")).toEqual({ dir: "/home/me", partial: "", prefix: "/home/me/" });
    expect(completeFrom("~/Mi", ["MindFlock"]).text).toBe("~/MindFlock/");
  });
});
