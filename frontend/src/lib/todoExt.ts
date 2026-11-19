/** The To-do extension, as the app's own chrome reaches it. The list itself
 * (window, quick-add, history) is the `todo` extension's
 * (backend/web/addons/todo, /extensions/todo/index.js); what the app keeps is
 * the doors into it: the due-reminder popup's "Open the To-do list" and the
 * voice assistant's "open To-do". Both run the extension's own command, so
 * there is one way the list opens. */

import { runAppExtCommand } from "../extensions/host";

export const TODO_EXT_ID = "todo";
export const TODO_OPEN_COMMAND = "todo.open";
/** The word the person sees (bar, window title). */
export const TODO_LABEL = "To-do";

/** Open the To-do window. False (and a toast saying where to turn it on)
 * when the extension is off. */
export function openTodoList(): boolean {
  return runAppExtCommand(TODO_EXT_ID, TODO_OPEN_COMMAND, TODO_LABEL);
}
