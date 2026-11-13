/** Paths on the SERVER's disk, whatever the browser runs on (a phone, the
 * desktop app, another computer): a folder typed into the New dialog is the
 * server's, so "is this a full path?" is the server's question.
 *
 * The server says where its home is (GET /api/config `home`); a home like
 * `C:\Users\you` means the server runs on native Windows (docs/windows.md),
 * where a full path is `C:\…`, `C:/…` or `\\server\share\…` as well as `~…`.
 * Everywhere else only `/…` and `~…` are full paths — unchanged. */

let windowsServer = false;

/** Record the server's home (from /api/config). */
export function noteServerHome(home: string | undefined | null): void {
  if (typeof home === "string" && home) windowsServer = isWindowsFullPath(home);
}

/** Whether the server runs on native Windows (as far as its home says). */
export function serverIsWindows(): boolean {
  return windowsServer;
}

/** `C:\…`, `C:/…` or a UNC `\\server\share…`. */
export function isWindowsFullPath(text: string): boolean {
  return /^[A-Za-z]:[\\/]/.test(text) || /^\\\\[^\\]/.test(text);
}

/** `path` cut at its LAST separator — `/` or `\`, whichever the server's OS
 * wrote — into the folder it is in and its own name, trailing separators
 * dropped first. A root keeps its separator: "/a" → "/" + "a",
 * "C:\\a" → "C:\\" + "a", "C:/a" → "C:/" + "a"; "~/a" → "~" + "a".
 * No separator (or a bare root) → parent "". The one splitter for a folder
 * the server named: cutting a `C:\…` path at "/" alone made the whole path
 * the "name" (`/api/mkdir` then refused it as "contains a path"). */
export function splitPath(path: string): { parent: string; name: string } {
  const t = path.length > 1 ? path.replace(/[\\/]+$/, "") || path.slice(0, 1) : path;
  const i = Math.max(t.lastIndexOf("/"), t.lastIndexOf("\\"));
  if (i < 0) return { parent: "", name: t };
  const head = t.slice(0, i);
  const name = t.slice(i + 1);
  if (i === 0) return { parent: name ? t[0] : "", name };
  // A drive root ("C:\") is a folder; "C:" alone is the drive's CURRENT folder.
  if (/^[A-Za-z]:$/.test(head)) return { parent: head + t[i], name };
  return { parent: head, name };
}

/** The last name in `path` ("C:\\Users\\me\\app" → "app", "/a/b/" → "b"). */
export function pathLeaf(path: string): string {
  return splitPath(path).name;
}

/** How the hints name a full path: the server's own kind. */
export function fullPathHint(): string {
  return windowsServer ? "a full path like C:\\Users\\you\\project" : "a full path starting with / or ~";
}

/** For tests: forget what the server said. */
export function resetServerPaths(): void {
  windowsServer = false;
}
