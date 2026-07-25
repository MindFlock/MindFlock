// Shared modal accessibility helper for addon-built dialogs.
//
// Addon modules (connections.js, templates.js) build their own modal DOM and
// reuse the app's `.modal` overlay. This gives those modals the keyboard +
// screen-reader behavior users expect from a dialog, without each addon
// re-implementing it: dialog semantics, focus moved inside on open, Tab/Shift+Tab
// trapped within, and focus returned to the opener on close.
//
// Usage:
//   const release = activateModalA11y(modalEl, openerButton, "Connections");
//   // ... on close:
//   release();

function focusables(root) {
  const sel =
    'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), ' +
    'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  // Visible only (offsetParent is null for display:none subtrees).
  return Array.from(root.querySelectorAll(sel)).filter((el) => el.offsetParent !== null);
}

export function activateModalA11y(modal, opener, label) {
  if (!modal) return () => {};
  modal.setAttribute("role", "dialog");
  modal.setAttribute("aria-modal", "true");
  if (label) modal.setAttribute("aria-label", label);
  if (!modal.hasAttribute("tabindex")) modal.tabIndex = -1;

  // Move focus inside — first control, else the modal container itself.
  const first = focusables(modal)[0];
  try {
    (first || modal).focus();
  } catch (e) {
    /* focus can throw if the node is detached mid-open; harmless */
  }

  function onKey(e) {
    if (e.key !== "Tab") return;
    const f = focusables(modal);
    if (!f.length) return;
    const idx = f.indexOf(document.activeElement);
    if (e.shiftKey && idx <= 0) {
      e.preventDefault();
      f[f.length - 1].focus();
    } else if (!e.shiftKey && idx === f.length - 1) {
      e.preventDefault();
      f[0].focus();
    }
  }
  modal.addEventListener("keydown", onKey);

  return function release() {
    modal.removeEventListener("keydown", onKey);
    if (opener && typeof opener.focus === "function") {
      try {
        opener.focus();
      } catch (e) {
        /* opener may be gone; ignore */
      }
    }
  };
}
