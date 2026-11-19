/** todo/ui.js — DOM helpers shared by the window (view.js) and the quick-add
 * dialog (add.js): the element builder, the monochrome icon set, the details
 * renderer (prose with links, fenced code with a Copy button), the
 * "for <session>" tag chip, the priority switch and the request wrapper that
 * turns the server's {"error": …} body into a thrown Error.
 *
 * Plain DOM, no template strings with user data: every string a person or an
 * agent wrote goes in through textContent or an attribute. */

import { parseDetails, TIERS, TIER_LABELS } from "./model.js";

/** el("div", {class, text, onClick, dataset, …}, child, "text", [more]). */
export function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, val] of Object.entries(attrs)) {
      if (val === undefined || val === null || val === false) continue;
      if (key === "class") node.className = val;
      else if (key === "text") node.textContent = val;
      else if (key === "dataset") Object.assign(node.dataset, val);
      else if (key === "for") node.htmlFor = val;
      else if (/^on[A-Z]/.test(key)) node.addEventListener(key.slice(2).toLowerCase(), val);
      else if (key.startsWith("aria-") || key.startsWith("data-") || key === "role") node.setAttribute(key, val === true ? "true" : val);
      else if (key in node) node[key] = val;
      else node.setAttribute(key, val === true ? "" : val);
    }
  }
  append(node, children);
  return node;
}

export function append(node, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) append(node, child);
    else if (child instanceof Node) node.appendChild(child);
    else node.appendChild(document.createTextNode(String(child)));
  }
  return node;
}

// 24×24, stroke-only, currentColor — they sit at 12–14px next to text.
const ICONS = {
  grip: '<circle cx="9" cy="6" r="1.2"/><circle cx="15" cy="6" r="1.2"/><circle cx="9" cy="12" r="1.2"/><circle cx="15" cy="12" r="1.2"/><circle cx="9" cy="18" r="1.2"/><circle cx="15" cy="18" r="1.2"/>',
  chevron: '<path d="M9 6l6 6-6 6"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1"/><path d="M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1"/>',
  notes: '<path d="M5 6h14M5 11h14M5 16h9"/>',
  clock: '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  restore: '<path d="M4 12a8 8 0 1 0 2.34-5.66"/><path d="M4 4v5h5"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  session: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 10l3 2-3 2M12 15h5"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
};

export function icon(name, cls) {
  const span = document.createElement("span");
  span.className = "mfx-todo-icon" + (cls ? " " + cls : "");
  span.setAttribute("aria-hidden", "true");
  span.innerHTML =
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
    (ICONS[name] || "") +
    "</svg>";
  return span;
}

/** The app's request, with a body-level {"error": …} turned into a throw and
 * a readable message on every failure. */
export async function call(api, path, opts) {
  const r = await api.request(path, opts);
  if (r && typeof r === "object" && typeof r.error === "string" && r.error) throw new Error(r.error);
  return r;
}

export function errText(err) {
  if (!err) return "unknown error";
  return (err && err.message) || String(err);
}

/** Copy text to the clipboard; the textarea fallback covers an insecure
 * origin (http on a LAN address) where navigator.clipboard is missing. */
export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) {
    /* fall through */
  }
  const ta = el("textarea", { value: text, readOnly: true, "aria-hidden": "true" });
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  ta.style.top = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch (_) {
    ok = false;
  }
  ta.remove();
  return ok;
}

/** Grow a textarea with its content, up to its CSS max-height (then it
 * scrolls). */
export function autoGrow(ta) {
  const fit = () => {
    ta.style.height = "auto";
    ta.style.height = ta.scrollHeight + 2 + "px";
  };
  ta.addEventListener("input", fit);
  requestAnimationFrame(fit);
  return fit;
}

/** Details → read-friendly DOM: prose (newlines kept, URLs as links) and
 * fenced code blocks, each with its own Copy button. */
export function renderDetails(details) {
  const blocks = parseDetails(details);
  return el(
    "div",
    { class: "mfx-todo-details" },
    blocks.map((b) => {
      if (b.type === "code") {
        const btn = el(
          "button",
          { class: "mfx-todo-copy", type: "button", "aria-label": "Copy this block", title: "Copy" },
          icon("copy"),
          el("span", { class: "mfx-todo-copy-word", text: "Copy" })
        );
        btn.addEventListener("click", async (e) => {
          e.stopPropagation();
          const ok = await copyText(b.code);
          const word = btn.querySelector(".mfx-todo-copy-word");
          btn.classList.toggle("is-done", ok);
          if (word) word.textContent = ok ? "Copied" : "Copy failed";
          setTimeout(() => {
            btn.classList.remove("is-done");
            if (word) word.textContent = "Copy";
          }, 1500);
        });
        return el(
          "div",
          { class: "mfx-todo-code" + (b.open ? " is-open" : "") },
          el("pre", { class: "mfx-todo-pre" }, el("code", { text: b.code })),
          btn
        );
      }
      return el(
        "p",
        { class: "mfx-todo-prose" },
        b.parts.map((p) =>
          p.type === "link"
            ? el("a", { href: p.href, target: "_blank", rel: "noopener noreferrer", text: p.text, onClick: (e) => e.stopPropagation() })
            : p.text
        )
      );
    })
  );
}

/** High / Normal / Low as a three-button switch. `onPick(tier)`. */
export function prioritySwitch(current, onPick, keyPrefix) {
  return el(
    "div",
    { class: "mfx-todo-seg", role: "group", "aria-label": "Priority" },
    TIERS.map((t) =>
      el("button", {
        class: "mfx-todo-seg-btn is-" + t + (current === t ? " is-on" : ""),
        type: "button",
        text: TIER_LABELS[t],
        "aria-pressed": current === t ? "true" : "false",
        "data-key": keyPrefix ? keyPrefix + ":" + t : null,
        onClick: (e) => {
          e.stopPropagation();
          onPick(t);
        },
      })
    )
  );
}

/** The "for <session>" tag chip next to a quick-add box. `state()` returns
 * {session, on}; `toggle()` flips the remembered choice. Returns {node,
 * refresh}. Nothing shows when no session is focused (or the host is level
 * 1 and cannot say). */
export function sessionTag(state, toggle) {
  const node = el("span", { class: "mfx-todo-tag-wrap" });
  const refresh = () => {
    const { session, on } = state();
    if (!session) {
      node.replaceChildren();
      return;
    }
    const want = (on ? "on:" : "off:") + session;
    if (node.dataset.state === want) return;
    node.dataset.state = want;
    node.replaceChildren(
      on
        ? el(
            "span",
            { class: "mfx-todo-tag", title: "New items are tagged with " + session },
            el("span", { class: "mfx-todo-tag-text", text: "for " + session }),
            el("button", { class: "mfx-todo-tag-x", type: "button", "aria-label": "Don't tag new items with " + session, title: "Don't tag", onClick: toggle }, icon("close"))
          )
        : el("button", { class: "mfx-todo-tag is-off", type: "button", title: "Tag new items with " + session, onClick: toggle }, icon("plus"), el("span", { class: "mfx-todo-tag-text", text: "for " + session }))
    );
  };
  refresh();
  return { node, refresh };
}

/** The session the person is looking at (host level 2), or "". */
export function focusedSession(api) {
  try {
    const f = api.ui && api.ui.focusedSession;
    const s = typeof f === "function" ? f() : null;
    return typeof s === "string" ? s : "";
  } catch (_) {
    return "";
  }
}

/** Is this element a text-entry field (where every key is the person's,
 * never a shortcut)? Checkboxes and buttons are not. */
export function isTextEntry(node) {
  if (!node || !node.tagName) return false;
  if (node.isContentEditable) return true;
  if (node.tagName === "TEXTAREA" || node.tagName === "SELECT") return true;
  if (node.tagName !== "INPUT") return false;
  const t = (node.type || "text").toLowerCase();
  return !["checkbox", "radio", "button", "submit", "reset", "range", "color", "file"].includes(t);
}
