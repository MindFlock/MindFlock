import { afterEach, describe, expect, it } from "vitest";
import {
  fullPathHint,
  isWindowsFullPath,
  noteServerHome,
  pathLeaf,
  resetServerPaths,
  serverIsWindows,
  splitPath,
} from "../lib/serverPaths";
import { isHomeFolder, looksLikePath, sessionsInFolder } from "../components/dialogs/newSession";
import { parentPath, seedChain } from "../lib/folderPicker";
import { runRepoOptions } from "../lib/runStart";
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

  it("“Create this folder” splits a Windows path into parent + one name (the friend's C:\\Users\\maldo\\snake-game)", () => {
    // The "/"-only cut sent parent "/" and the whole path as the name, which
    // /api/mkdir refuses as "folder name must not contain a path".
    expect(splitPath("C:\\Users\\maldo\\snake-game")).toEqual({ parent: "C:\\Users\\maldo", name: "snake-game" });
    expect(splitPath("C:\\Users\\maldo\\snake-game\\")).toEqual({ parent: "C:\\Users\\maldo", name: "snake-game" });
    expect(splitPath("C:/Users/maldo/snake-game")).toEqual({ parent: "C:/Users/maldo", name: "snake-game" });
    // A drive root keeps its separator: "C:" alone is the drive's CURRENT folder.
    expect(splitPath("C:\\snake-game")).toEqual({ parent: "C:\\", name: "snake-game" });
    expect(splitPath("D:/x")).toEqual({ parent: "D:/", name: "x" });
    expect(splitPath("C:\\")).toEqual({ parent: "", name: "C:" });
    // UNC and ~ spellings.
    expect(splitPath("\\\\nas\\share\\repo")).toEqual({ parent: "\\\\nas\\share", name: "repo" });
    expect(splitPath("~\\snake-game")).toEqual({ parent: "~", name: "snake-game" });
    // POSIX unchanged.
    expect(splitPath("/home/me/app/")).toEqual({ parent: "/home/me", name: "app" });
    expect(splitPath("/app")).toEqual({ parent: "/", name: "app" });
    expect(splitPath("/")).toEqual({ parent: "", name: "" });
    expect(splitPath("~/x")).toEqual({ parent: "~", name: "x" });
    expect(splitPath("app")).toEqual({ parent: "", name: "app" });
  });

  it("the folder picker walks up a Windows path to its drive root", () => {
    expect(parentPath("C:\\Users\\maldo")).toBe("C:\\Users");
    expect(parentPath("C:\\Users")).toBe("C:\\");
    expect(parentPath("C:\\")).toBe("");
    expect(seedChain("C:\\Users\\maldo\\snake-game")).toEqual([
      "C:\\Users\\maldo\\snake-game",
      "C:\\Users\\maldo",
      "C:\\Users",
      "C:\\",
      "",
    ]);
  });

  it("a folder's name is its last segment whichever separator wrote it", () => {
    expect(pathLeaf("C:\\Users\\maldo\\snake-game")).toBe("snake-game");
    expect(pathLeaf("C:\\Users\\maldo\\snake-game\\")).toBe("snake-game");
    expect(pathLeaf("/home/me/app/")).toBe("app");
    expect(runRepoOptions("C:\\Users\\maldo\\snake-game", [])[0]).toEqual({
      value: "C:\\Users\\maldo\\snake-game",
      label: "snake-game",
    });
  });

  it("a Windows server's folder is the same folder whatever its case or trailing separator", () => {
    const rows = [{ title: "one", path: "C:\\Users\\maldo\\snake-game", in_place: true }];
    noteServerHome("C:\\Users\\maldo");
    expect(sessionsInFolder(rows, ["c:\\users\\maldo\\snake-game\\"])).toEqual(["one"]);
    resetServerPaths();
    noteServerHome("/home/me");
    expect(sessionsInFolder([{ title: "a", path: "/r/App", in_place: true }], ["/r/app"])).toEqual([]);
  });

  it("Tab completion of a ~\\ path on a Windows server keeps the backslash", () => {
    noteServerHome("C:\\Users\\maldo");
    expect(splitForCompletion("~\\Doc")).toEqual({ dir: "~", partial: "Doc", prefix: "~\\" });
    expect(completeFrom("~\\Doc", ["Documents"]).text).toBe("~\\Documents\\");
    expect(splitForCompletion("~/Doc")).toEqual({ dir: "~", partial: "Doc", prefix: "~/" });
  });

  it("warns when the Folder field is the home folder itself (the friend picked C:\\Users\\maldo)", () => {
    noteServerHome("C:\\Users\\maldo");
    expect(isHomeFolder("C:\\Users\\maldo", "C:\\Users\\maldo")).toBe(true);
    expect(isHomeFolder("c:\\users\\maldo\\", "C:\\Users\\maldo")).toBe(true);
    expect(isHomeFolder("~", "C:\\Users\\maldo")).toBe(true);
    expect(isHomeFolder("C:\\Users\\maldo\\snake-game", "C:\\Users\\maldo")).toBe(false);
    expect(isHomeFolder("", "C:\\Users\\maldo")).toBe(false);
    resetServerPaths();
    noteServerHome("/home/me");
    expect(isHomeFolder("/home/me/", "/home/me")).toBe(true);
    expect(isHomeFolder("/home/Me", "/home/me")).toBe(false);
    expect(isHomeFolder("/home/me/app", "/home/me")).toBe(false);
    expect(isHomeFolder("/home/me", "")).toBe(false);
  });
});
