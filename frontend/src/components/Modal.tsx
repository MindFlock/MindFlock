/** The one modal shell. Every dialog used to hand-roll the same three things —
 * a `.modal` backdrop, a dismiss-on-backdrop handler and its own Escape
 * keydown listener — and each copy drifted: some closed on a backdrop CLICK
 * (so a text selection that started in the form and overshot the card threw
 * the dialog away), some stacked Escape listeners so one press closed two
 * dialogs, some took the keyboard from the terminal and some didn't.
 *
 * What it does:
 *  - Renders `<div className="modal …">` (the class lib/keymap.ts's
 *    OVERLAY_SELECTOR keys on, so Ctrl+W / Delete stay guarded while it is up)
 *    with whatever id/role/aria the dialog passes.
 *  - Backdrop: dismisses on a MOUSEDOWN that started on the backdrop itself,
 *    never on click — a press inside the card whose release lands outside is a
 *    selection, not a dismissal (CommitDialog learned this first).
 *  - Escape: closes the TOPMOST open modal only (a module-level stack, so a
 *    sign-in window over Settings closes the window, not both). `onEscape`
 *    runs first and returns true when it handled the key itself (closed a
 *    dropdown, cancelled an inline edit). A key some inner handler already
 *    claimed (defaultPrevented) is left alone. `escapeCapture` listens in the
 *    window capture phase and stops propagation (otherwise: window, bubble) — for overlays that sit on top
 *    of a dialog that still owns its own listener.
 *  - Focus: on open, if the dialog didn't focus something inside itself, the
 *    shell takes focus (so keys stop going to the terminal behind it); on
 *    close, focus goes back to where it was unless something else took it.
 *  - Announces itself (`MODAL_OPEN_EVENT`) on open: in a phone-narrow window
 *    the sidebar is a drawer drawn OVER the page (z-index 60), and a dialog
 *    opened from it (the Setup chip, Shortcuts, a row's Rename) opened under
 *    it, unclickable. The sidebar closes its drawer on this event.
 *
 * Electron has no window.prompt/alert/confirm — dialogs are DOM, always. */

import { useEffect, useRef, type HTMLAttributes, type MouseEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";

export interface ModalProps extends Omit<HTMLAttributes<HTMLDivElement>, "onMouseDown" | "onClick"> {
  /** Dismiss — the backdrop press and Escape both call it. */
  onClose(): void;
  /** Runs before Escape closes; return true to keep the modal open (the key
   * was spent on something inside it). */
  onEscape?(e: KeyboardEvent): boolean | void;
  /** Default true. */
  closeOnBackdrop?: boolean;
  /** Default true. False leaves Escape entirely to the dialog. */
  closeOnEscape?: boolean;
  /** Listen for Escape in the window capture phase and stop it there. */
  escapeCapture?: boolean;
  /** Render into document.body instead of in place. */
  portal?: boolean;
  children?: ReactNode;
}

/** Open modals, oldest first — only the last one answers Escape. */
const stack: object[] = [];

/** Fired on window when any modal opens (see the header: the narrow
 * sidebar drawer closes on it, so a dialog is never drawn under it). */
export const MODAL_OPEN_EVENT = "mf:modal-open";

/** The backdrop's press handler: only a mousedown whose target IS the
 * backdrop counts (a press bubbling up from the card never does). */
export function backdropPress(onClose: () => void) {
  return (e: MouseEvent<HTMLDivElement>) => {
    if (e.target === e.currentTarget) onClose();
  };
}

/** The Escape decision, exported for the test: true when this modal took the
 * key (and closed, unless onEscape spent it). */
export function modalEscape(
  token: object,
  e: KeyboardEvent,
  p: Pick<ModalProps, "onClose" | "onEscape" | "closeOnEscape" | "escapeCapture">
): boolean {
  if (e.key !== "Escape" || e.defaultPrevented) return false;
  if (stack[stack.length - 1] !== token) return false;
  const spent = p.onEscape?.(e) === true;
  if (!spent && p.closeOnEscape === false) return false;
  e.preventDefault();
  if (p.escapeCapture) e.stopPropagation();
  if (!spent) p.onClose();
  return true;
}

/** Push/pop helpers so the test can drive the stack without a DOM. */
export function pushModal(token: object): () => void {
  stack.push(token);
  return () => {
    const i = stack.lastIndexOf(token);
    if (i >= 0) stack.splice(i, 1);
  };
}

export function Modal({
  onClose,
  onEscape,
  closeOnBackdrop = true,
  closeOnEscape = true,
  escapeCapture = false,
  portal = false,
  className,
  children,
  ...rest
}: ModalProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  // Latest props for the one long-lived listener.
  const props = useRef({ onClose, onEscape, closeOnEscape, escapeCapture });
  props.current = { onClose, onEscape, closeOnEscape, escapeCapture };

  useEffect(() => {
    const token = {};
    const pop = pushModal(token);
    window.dispatchEvent(new CustomEvent(MODAL_OPEN_EVENT));
    const onKey = (e: KeyboardEvent) => {
      modalEscape(token, e, props.current);
    };
    // Bubble phase on window by default — the last stop, so a popover's own
    // document-level Escape (or a React handler) can claim the key first.
    window.addEventListener("keydown", onKey, escapeCapture);

    // Focus: take it only when the dialog's own autoFocus didn't land inside.
    // Dialogs that focus an input from a setTimeout run after this and win.
    const node = ref.current;
    const prev = document.activeElement as HTMLElement | null;
    if (node && !node.contains(prev)) node.focus({ preventScroll: true });

    return () => {
      window.removeEventListener("keydown", onKey, escapeCapture);
      pop();
      // Hand focus back only if nothing else claimed it on the way out (a
      // removed focused node leaves activeElement on <body>).
      const now = document.activeElement;
      const orphaned = !now || now === document.body || (node ? node.contains(now) : false);
      if (orphaned && prev && prev !== document.body && prev.isConnected) {
        prev.focus({ preventScroll: true });
      }
    };
    // Mount-scoped by design: the listener reads the latest props through ref.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const shell = (
    <div
      {...rest}
      ref={ref}
      tabIndex={-1}
      className={className ? "modal " + className : "modal"}
      onMouseDown={closeOnBackdrop ? backdropPress(onClose) : undefined}
    >
      {children}
    </div>
  );
  return portal ? createPortal(shell, document.body) : shell;
}
