/** Plain sentences for what /api/mkdir and /api/browse refuse with. The
 * server answers in its own terms ("could not create folder: [Errno 13]
 * Permission denied: '/x/y'", "not a directory: /x"); the New dialog's
 * "Create this folder" button and the folder browser both say it this way. */

/** The OS error inside a server message, without the Python dressing:
 * "could not create folder: [Errno 28] No space left on device: '/x'" →
 * "No space left on device". */
export function osErrorText(msg: string): string {
  return msg
    .replace(/^could not create folder:\s*/i, "")
    .replace(/^\[Errno \d+\]\s*/i, "")
    .replace(/:\s*'[^']*'\s*$/, "")
    .trim();
}

/** Where the refusal happened: the dialog's "Create this folder" button, the
 * browser's New folder row, or the browser opening a folder. */
export type FolderErrorFrom = "field" | "browser" | "list";

export function folderErrorText(msg: string, from: FolderErrorFrom): string {
  if (/permission denied/i.test(msg))
    return from === "list"
      ? "You don't have permission to open that folder."
      : "You don't have permission to create a folder there — pick another place.";
  if (/not a directory/i.test(msg))
    return from === "field"
      ? "The folder it would go in doesn't exist either — use Browse… to pick a place."
      : from === "browser"
      ? "The folder it would go in doesn't exist anymore — pick another place."
      : "That folder doesn't exist anymore — pick another one.";
  if (/already exists/i.test(msg))
    return from === "field"
      ? "Something is already there — pick it with Browse…."
      : "Something called that is already here — give it another name.";
  if (/must not contain a path/i.test(msg)) return "Use a single folder name, without / or \\.";
  return (from === "list" ? "Couldn't open it: " : "Couldn't create it: ") + osErrorText(msg);
}
