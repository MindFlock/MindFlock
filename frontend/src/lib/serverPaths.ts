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

/** How the hints name a full path: the server's own kind. */
export function fullPathHint(): string {
  return windowsServer ? "a full path like C:\\Users\\you\\project" : "a full path starting with / or ~";
}

/** For tests: forget what the server said. */
export function resetServerPaths(): void {
  windowsServer = false;
}
