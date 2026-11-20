/** todo/add.js — the quick-add dialog (surface "add").
 *
 * Built for rapid capture: the text field has focus, Enter adds and CLEARS
 * the form for the next one (a small "Added" line confirms), Ctrl+Enter in
 * the details adds too, "Add and close" (or Esc) leaves. New items are tagged
 * "for <session>" — the session the person was looking at — unless they
 * remove the chip (remembered in api.storage, shared with the window's
 * quick-add). `host.ctx` may prefill {text, details, session}; its `onAdded`,
 * when given, runs after each successful add (the window clears the
 * quick-add text it handed over only then). */

import { autoGrow, call, el, errText, focusedSession, prioritySwitch, sessionTag } from "./ui.js";
import { parseDue, toDueString } from "./model.js";

export function renderAdd(api, host) {
  const root = host.el;
  root.classList.add("mfx-todo-dialog-root");
  const ctx = host.ctx && typeof host.ctx === "object" ? host.ctx : {};
  let disposed = false;
  let tier = "normal";
  let tagOn = api.storage.get("tagSession", true) !== false;
  // The session the person came from: read once at open (the dialog itself
  // takes focus next), a ctx session wins.
  const session = typeof ctx.session === "string" && ctx.session ? ctx.session : focusedSession(api);
  let statusTimer = null;

  const text = el("input", {
    class: "mfx-todo-input mfx-todo-dialog-text",
    type: "text",
    value: typeof ctx.text === "string" ? ctx.text : "",
    placeholder: "What do you need to do?",
    "aria-label": "To-do",
    maxLength: 500,
    onKeydown: (e) => {
      if (e.key === "Enter" && !e.isComposing) {
        e.preventDefault();
        void add(e.ctrlKey || e.metaKey);
      } else if (e.key === "Escape") onEscape(e);
    },
  });
  const details = el("textarea", {
    class: "mfx-todo-input mfx-todo-details-edit",
    rows: 3,
    value: typeof ctx.details === "string" ? ctx.details : "",
    maxLength: 20000,
    placeholder: "Details (optional): steps, a command in ``` fences, a link",
    "aria-label": "Details",
    onKeydown: (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        void add(false);
      } else if (e.key === "Escape") onEscape(e);
    },
  });
  const fitDetails = autoGrow(details);
  const prio = el("span", { class: "mfx-todo-field-slot" });
  const due = el("input", { class: "mfx-todo-input mfx-todo-due", type: "datetime-local", "aria-label": "Due (optional)" });
  if (typeof ctx.due === "string" && parseDue(ctx.due)) due.value = toDueString(parseDue(ctx.due));
  const tag = sessionTag(
    () => ({ session, on: tagOn }),
    () => {
      tagOn = !tagOn;
      api.storage.set("tagSession", tagOn);
      tag.refresh();
    }
  );
  const status = el("div", { class: "mfx-todo-dialog-status", role: "status", "aria-live": "polite" });
  const addBtn = el("button", { class: "mfx-todo-btn is-primary", type: "button", text: "Add", title: "Add and keep the dialog open (Enter)", onClick: () => add(false) });
  const closeBtn = el("button", { class: "mfx-todo-btn", type: "button", text: "Add and close", title: "Ctrl+Enter in the text field", onClick: () => add(true) });
  const openList = el("button", {
    class: "mfx-todo-link-btn",
    type: "button",
    text: "Open the list",
    onClick: () => {
      void api.commands.run("todo.open");
      host.close();
    },
  });

  function drawPriority() {
    prio.replaceChildren(
      prioritySwitch(tier, (t) => {
        tier = t;
        drawPriority();
      })
    );
  }
  drawPriority();

  root.replaceChildren(
    el(
      "div",
      { class: "mfx-todo-dialog" },
      text,
      details,
      el(
        "div",
        { class: "mfx-todo-fields" },
        el("div", { class: "mfx-todo-field" }, el("span", { class: "mfx-todo-field-label", text: "Priority" }), prio),
        el("label", { class: "mfx-todo-field" }, el("span", { class: "mfx-todo-field-label", text: "Due" }), due),
        tag.node
      ),
      status,
      el("div", { class: "mfx-todo-dialog-actions" }, openList, el("span", { class: "mfx-todo-spacer" }), closeBtn, addBtn)
    )
  );

  function onEscape(e) {
    e.preventDefault();
    e.stopPropagation();
    host.close();
  }

  function say(msg, isError) {
    status.textContent = msg;
    status.classList.toggle("is-error", !!isError);
    if (statusTimer) clearTimeout(statusTimer);
    statusTimer = isError ? null : setTimeout(() => (status.textContent = ""), 4000);
  }

  async function add(andClose) {
    const t = text.value.trim();
    if (!t) {
      say("Type what to do first.", true);
      text.focus();
      return;
    }
    const body = { text: t, source: "you" };
    const d = details.value.replace(/\s+$/, "");
    if (d) body.details = d;
    if (tier !== "normal") body.priority = tier;
    if (due.value) body.due = due.value;
    if (tagOn && session) body.session = session;
    // Clear at once for the next capture; put it back if the add fails.
    const kept = { text: text.value, details: details.value, due: due.value, tier };
    text.value = "";
    details.value = "";
    due.value = "";
    tier = "normal";
    drawPriority();
    fitDetails();
    text.focus();
    try {
      const r = await call(api, "/api/todo/items", { json: body });
      if (disposed) return;
      const updated = r && Array.isArray(r.updated) && r.updated.length && !(r.added && r.added.length);
      say((updated ? "Already on the list, updated: " : "Added: ") + t);
      // The window's quick-add box gave up its text to this dialog; it clears
      // only now that the dialog has added (Escape or ✕ keep it there).
      if (typeof ctx.onAdded === "function") {
        try {
          ctx.onAdded();
        } catch (err) {
          api.log.error("onAdded", err);
        }
      }
      if (andClose) host.close();
    } catch (err) {
      api.log.error("add", err);
      if (disposed) return;
      if (!text.value) {
        text.value = kept.text;
        details.value = kept.details;
        due.value = kept.due;
        tier = kept.tier;
        drawPriority();
        fitDetails();
      }
      say("Could not add it: " + errText(err), true);
    }
  }

  setTimeout(() => {
    if (disposed) return;
    text.focus();
    text.select();
  }, 0);

  return {
    dispose() {
      disposed = true;
      if (statusTimer) clearTimeout(statusTimer);
      root.classList.remove("mfx-todo-dialog-root");
      root.replaceChildren();
    },
  };
}
