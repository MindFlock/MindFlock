/** The floating folder picker (lib/folderPicker + FolderPickerWindow) and the
 * PathField that opens it — the owner's "the file browser … another pop up
 * that can be dragged around, and a button to open it next to all spots where
 * you need to input a path". */

import { afterEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  PICKER_MARGIN,
  PICKER_MIN,
  PICKER_RECT_KEY,
  clampRect,
  closeFolderPicker,
  defaultRect,
  dragRect,
  getFolderPicker,
  openFolderPicker,
  parentPath,
  pickFolder,
  seedChain,
} from "../lib/folderPicker";
import { PathField, pathFieldRequest } from "../components/PathField";
import WINDOW from "../components/FolderPickerWindow.tsx?raw";
import PICKER from "../lib/folderPicker.ts?raw";
import PATHFIELD from "../components/PathField.tsx?raw";
import FOLDER from "../components/dialogs/newSession/FolderPicker.tsx?raw";
import DESCRIBE from "../components/dialogs/newSession/DescribeSection.tsx?raw";
import WORKSPACE from "../components/settings/screens/Workspace.tsx?raw";
import APP from "../App.tsx?raw";
import MANIFEST from "../styles/index.css?raw";
import CSS from "../components/FolderPickerWindow.css?raw";

afterEach(() => closeFolderPicker(undefined, false));

describe("drag clamp math", () => {
  const vw = 1280;
  const vh = 800;

  it("leaves a window that fits alone", () => {
    expect(clampRect({ x: 100, y: 50, w: 440, h: 440 }, vw, vh)).toEqual({ x: 100, y: 50, w: 440, h: 440 });
  });

  it("pulls a window dragged off any edge back on, by the margin", () => {
    expect(clampRect({ x: -300, y: -50, w: 440, h: 440 }, vw, vh)).toMatchObject({ x: PICKER_MARGIN, y: PICKER_MARGIN });
    expect(clampRect({ x: 5000, y: 5000, w: 440, h: 440 }, vw, vh)).toMatchObject({
      x: vw - PICKER_MARGIN - 440,
      y: vh - PICKER_MARGIN - 440,
    });
  });

  it("never grows past the viewport, never shrinks under the minimum", () => {
    expect(clampRect({ x: 0, y: 0, w: 4000, h: 4000 }, vw, vh)).toEqual({
      x: PICKER_MARGIN,
      y: PICKER_MARGIN,
      w: vw - 2 * PICKER_MARGIN,
      h: vh - 2 * PICKER_MARGIN,
    });
    expect(clampRect({ x: 10, y: 10, w: 20, h: 20 }, vw, vh)).toMatchObject(PICKER_MIN);
  });

  it("fits a 420 px phone: as wide as the screen less a margin each side", () => {
    const r = clampRect({ x: 600, y: 100, w: 440, h: 440 }, 420, 860);
    expect(r).toEqual({ x: PICKER_MARGIN, y: 100, w: 420 - 2 * PICKER_MARGIN, h: 440 });
    expect(r.x + r.w).toBeLessThanOrEqual(420 - PICKER_MARGIN);
  });

  it("drags by the pointer's travel, clamped", () => {
    const from = { x: 100, y: 100, w: 400, h: 300 };
    expect(dragRect(from, 10, 10, 60, 30, vw, vh)).toEqual({ x: 150, y: 120, w: 400, h: 300 });
    // Flung far past the right/bottom: the whole window stays on screen.
    expect(dragRect(from, 10, 10, 9000, 9000, vw, vh)).toEqual({ x: vw - 8 - 400, y: vh - 8 - 300, w: 400, h: 300 });
    // And past the top-left, the title bar is still reachable.
    expect(dragRect(from, 500, 500, 0, 0, vw, vh)).toMatchObject({ x: 8, y: 8 });
  });

  it("opens beside the button that opened it, else centred", () => {
    const right = defaultRect({ left: 100, right: 200, top: 300 }, vw, vh, { w: 440, h: 440 });
    expect(right.x).toBe(212);
    const left = defaultRect({ left: 1000, right: 1200, top: 300 }, vw, vh, { w: 440, h: 440 });
    expect(left.x).toBe(1000 - 12 - 440);
    expect(defaultRect(null, vw, vh, { w: 440, h: 440 })).toEqual({ x: 420, y: 180, w: 440, h: 440 });
    // A phone has room on neither side: centred, and inside the screen.
    const phone = defaultRect({ left: 300, right: 400, top: 200 }, 420, 860, { w: 440, h: 440 });
    expect(phone.x).toBe(8);
    expect(phone.w).toBe(404);
  });
});

describe("where the browse starts", () => {
  it("walks up to the nearest folder, then home", () => {
    expect(parentPath("/a/b/c")).toBe("/a/b");
    expect(parentPath("/a/b/")).toBe("/a");
    expect(parentPath("/a")).toBe("/");
    expect(parentPath("/")).toBe("");
    expect(parentPath("~/x")).toBe("~");
    expect(parentPath("~")).toBe("");
    expect(parentPath("./workspaces")).toBe(".");
    expect(seedChain("~/code/not-yet")).toEqual(["~/code/not-yet", "~/code", "~", ""]);
    expect(seedChain("/srv/x")).toEqual(["/srv/x", "/srv", "/", ""]);
    expect(seedChain("  ")).toEqual([""]);
  });
});

describe("one window at a time", () => {
  it("re-targets: the earlier field's browse is cancelled, the later one owns it", () => {
    const a = { onPick: vi.fn(), onCancel: vi.fn() };
    const b = { onPick: vi.fn(), onCancel: vi.fn() };
    const idA = openFolderPicker({ start: "/a", ...a });
    const idB = openFolderPicker({ start: "/b", ...b });
    expect(a.onCancel).toHaveBeenCalledTimes(1);
    expect(getFolderPicker()?.id).toBe(idB);
    // A late close from the first field never closes the second's window.
    closeFolderPicker(idA);
    expect(getFolderPicker()?.id).toBe(idB);
    pickFolder("/b/c");
    expect(b.onPick).toHaveBeenCalledWith("/b/c");
    expect(b.onCancel).not.toHaveBeenCalled();
    expect(getFolderPicker()).toBeNull();
  });

  it("✕ / Escape is a cancel; an owner closing its own browse is silent", () => {
    const r = { onPick: vi.fn(), onCancel: vi.fn() };
    openFolderPicker({ start: "", ...r });
    closeFolderPicker();
    expect(r.onCancel).toHaveBeenCalledTimes(1);
    const id = openFolderPicker({ start: "", ...r });
    closeFolderPicker(id, false);
    expect(r.onCancel).toHaveBeenCalledTimes(1);
    expect(r.onPick).not.toHaveBeenCalled();
  });
});

describe("PathField", () => {
  it("round-trips a pick through the field's own onChange", () => {
    let field = "~/not/made/yet";
    const onChange = vi.fn((v: string) => (field = v));
    const onPicked = vi.fn();
    openFolderPicker(pathFieldRequest({ value: "  " + field + " ", onChange, onPicked }));
    // Seeded with the field's value (the window walks up from there).
    expect(getFolderPicker()?.start).toBe("~/not/made/yet");
    expect(getFolderPicker()?.selected).toBe("~/not/made/yet");
    pickFolder("/home/me/work");
    expect(onChange).toHaveBeenCalledWith("/home/me/work");
    expect(onPicked).toHaveBeenCalledWith("/home/me/work");
    expect(field).toBe("/home/me/work");
    expect(onChange.mock.invocationCallOrder[0]).toBeLessThan(onPicked.mock.invocationCallOrder[0]);
  });

  it("a cancelled pick leaves the field alone", () => {
    const onChange = vi.fn();
    const onDone = vi.fn();
    openFolderPicker(pathFieldRequest({ value: "/x", onChange, onDone }));
    closeFolderPicker();
    expect(onChange).not.toHaveBeenCalled();
    expect(onDone).toHaveBeenCalled();
  });

  it("is the input it replaces, plus a Browse… button", () => {
    const html = renderToStaticMarkup(
      createElement(PathField, {
        id: "workspace-dir-input",
        "data-field": "workspace_dir",
        placeholder: "./workspaces",
        value: "~/ws",
        onChange: () => undefined,
      })
    );
    expect(html).toMatch(/<span class="path-field"><input[^>]*id="workspace-dir-input"/);
    expect(html).toContain('data-field="workspace_dir"');
    expect(html).toContain('value="~/ws"');
    expect(html).toContain('placeholder="./workspaces"');
    expect(html).toMatch(/<button type="button" class="path-field-browse" id="workspace-dir-input-browse"[^>]*><span aria-hidden="true">📁 <\/span>Browse…<\/button>/);
  });
});

describe("the window", () => {
  it("is mounted once by App and styled through the manifest", () => {
    expect(APP).toContain("<FolderPickerWindow />");
    expect(MANIFEST).toContain('@import "../components/FolderPickerWindow.css" layer(components);');
    expect(MANIFEST).toContain('@import "../components/PathField.css" layer(components);');
    // Above dialogs, under the toast (#cs-toast is 1000).
    const z = Number(/\.fpw \{[^}]*z-index: (\d+)/.exec(CSS)?.[1]);
    expect(z).toBeGreaterThan(100);
    expect(z).toBeLessThan(1000);
  });

  it("takes Escape before any dialog: window, capture phase, at module load", () => {
    expect(WINDOW).toMatch(/window\.addEventListener\(\s*"keydown",[\s\S]*?true\s*\);/);
    // …except inside the + Folder name box, which closes only its row.
    expect(WINDOW).toContain('closest(".fpw .rb-new")');
    expect(WINDOW).toContain('aria-modal="false"');
    expect(WINDOW).toContain("createPortal(win, document.body)");
    expect(WINDOW).toContain("Use this folder");
    expect(WINDOW).toContain('"Pick a folder"');
  });

  it("remembers where it was in ONE localStorage key, never a synced pref", () => {
    expect(PICKER_RECT_KEY).toBe("mf_folder_picker_rect");
    expect(PICKER.match(/localStorage\.(get|set)Item\(([^)]*)\)/g)?.every((c) => c.includes("PICKER_RECT_KEY"))).toBe(true);
    expect(PICKER + WINDOW).not.toMatch(/from "[^"]*prefs"|savePref/);
  });

  it("never asks through a browser dialog (Electron has none)", () => {
    for (const src of [WINDOW, PICKER, PATHFIELD]) expect(src).not.toMatch(/window\.prompt|\bprompt\(|confirm\(|alert\(/);
  });
});

describe("every editable path field opens it", () => {
  it("New / template Folder field and New's Runs in", () => {
    expect(FOLDER).toContain("useFolderPickerWhile(browserOpen,");
    expect(FOLDER).not.toContain("<FolderBrowser");
    expect(DESCRIBE).toContain("useFolderPickerWhile(runBrowse,");
  });

  it("Settings → Workspace folder, keeping its id, field and save", () => {
    expect(WORKSPACE).toMatch(/<PathField\s+data-group="repository"\s+data-field="workspace_dir"\s+id="workspace-dir-input"/);
    expect(WORKSPACE).toContain('onPicked={(p) => p.trim() !== stored && s.saveField("repository", "workspace_dir", p.trim())}');
  });
});
