/** Clipboard + file-upload plumbing (port of app.js section 12). The agent
 * CLIs run on the server and can't see this machine's clipboard, so paste is
 * client-side: text goes to the PTY, images/files upload to /api/paste-image
 * and the saved PATH is pasted. Electron exposes a native bridge
 * (window.mfclip) because navigator.clipboard is blocked in its renderer. */

import type { Terminal } from "@xterm/xterm";
import { api } from "../api/client";
import { toast } from "./toast";

declare global {
  interface Window {
    mfclip?: {
      readText?: () => string;
      writeText?: (t: string) => void;
      readImagePNG?: () => string;
    };
  }
}

export function copyText(text: string): Promise<boolean> {
  if (!text) return Promise.resolve(false);
  if (window.mfclip?.writeText) {
    try {
      window.mfclip.writeText(text);
      return Promise.resolve(true);
    } catch {
      /* fall through */
    }
  }
  if (navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(text).then(
      () => true,
      () => fallbackCopy(text)
    );
  }
  return Promise.resolve(fallbackCopy(text));
}

/** True when the desktop bridge EXISTS but throws (an app binary built before
 * main.js pinned sandbox:false) — used to tell the user the truth instead of
 * "Clipboard is empty". */
let clipBridgeBroken = false;

export function readClipboardText(): Promise<string> {
  if (window.mfclip?.readText) {
    try {
      const v = window.mfclip.readText();
      clipBridgeBroken = false;
      return Promise.resolve(v);
    } catch {
      clipBridgeBroken = true;
    }
  }
  if (navigator.clipboard?.readText) {
    return navigator.clipboard.readText().catch(() => "");
  }
  return Promise.resolve("");
}

export function readClipboardImage(): Promise<Blob | null> {
  if (window.mfclip?.readImagePNG) {
    try {
      const b64 = window.mfclip.readImagePNG();
      if (!b64) return Promise.resolve(null);
      const bin = atob(b64);
      const buf = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
      return Promise.resolve(new Blob([buf], { type: "image/png" }));
    } catch {
      clipBridgeBroken = true;
      return Promise.resolve(null);
    }
  }
  if (navigator.clipboard?.read) {
    return navigator.clipboard.read().then(
      (items) => {
        for (const it of items) {
          const t = (it.types || []).find((x) => x.startsWith("image/"));
          if (t) return it.getType(t);
        }
        return null;
      },
      () => null
    );
  }
  return Promise.resolve(null);
}

/** Paste THIS machine's clipboard into a terminal: text straight to the PTY
 * (bracketed paste); an image uploads and its saved path is pasted. */
export async function pasteClipboard(term: Terminal, session?: string) {
  const text = await readClipboardText();
  if (text) {
    term.paste(text);
    toast("Pasted " + text.length + " chars");
    return;
  }
  const img = await readClipboardImage();
  if (!img) {
    toast(
      clipBridgeBroken
        ? "Clipboard is unavailable in this desktop-app build — update/rebuild " +
            "the app to fix paste (Ctrl+Shift+V may still work)"
        : "Clipboard is empty",
      clipBridgeBroken ? { duration: 6000 } : undefined
    );
    return;
  }
  try {
    const q = session ? "?session=" + encodeURIComponent(session) : "";
    const r = await api<{ path: string }>("/api/paste-image" + q, {
      method: "POST",
      headers: { "Content-Type": img.type || "image/png" },
      body: img,
    });
    term.paste(r.path);
    toast("Pasted image → " + r.path);
  } catch (err) {
    toast("Image paste failed: " + ((err as Error)?.message || "error"));
  }
}

export async function uploadFileToWorkspace(
  blob: Blob,
  session?: string,
  name?: string
): Promise<string> {
  let q = session ? "?session=" + encodeURIComponent(session) : "";
  if (name) q += (q ? "&" : "?") + "name=" + encodeURIComponent(name);
  const r = await api<{ path: string }>("/api/paste-image" + q, {
    method: "POST",
    headers: { "Content-Type": blob.type || "application/octet-stream" },
    body: blob,
  });
  return r.path;
}

/** Upload a FileList and paste the saved paths, space separated (quoted when
 * a path contains spaces) so the agent can read them. */
export async function pasteFilesAsPaths(
  files: FileList | File[] | null,
  term: Terminal,
  session?: string
) {
  const list = Array.from(files || []);
  if (!list.length) return;
  toast(
    "Uploading " + (list.length === 1 ? list[0].name || "file" : list.length + " files") + "…"
  );
  const paths: string[] = [];
  for (const f of list) {
    try {
      paths.push(await uploadFileToWorkspace(f, session, f.name));
    } catch (err) {
      toast("Upload failed: " + (f.name || "file") + " — " + ((err as Error)?.message || "error"));
    }
  }
  if (!paths.length) return;
  term.paste(paths.map((p) => (/\s/.test(p) ? '"' + p + '"' : p)).join(" ") + " ");
  toast(paths.length === 1 ? "File → " + paths[0] : paths.length + " files → workspace");
}

export const dtHasFiles = (dt: DataTransfer | null): boolean =>
  !!dt && Array.from(dt.types || []).indexOf("Files") !== -1;

/** A file dropped OUTSIDE a terminal must not navigate the page away. */
export function installGlobalDropGuards() {
  window.addEventListener("dragover", (ev) => {
    if (dtHasFiles(ev.dataTransfer)) ev.preventDefault();
  });
  window.addEventListener("drop", (ev) => {
    if (dtHasFiles(ev.dataTransfer)) ev.preventDefault();
  });
}

function fallbackCopy(text: string): boolean {
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}
