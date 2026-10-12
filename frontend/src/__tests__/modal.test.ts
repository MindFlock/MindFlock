/** The one modal shell (components/Modal.tsx). The vitest environment is node
 * (no DOM), so the shell is checked through what it renders — its element's
 * props — with React's hooks stubbed, and its Escape decision is driven
 * directly against the shared stack.
 *
 * The regression this pins: RenameDialog closed on a backdrop CLICK, so a text
 * selection that started in the name box and was released outside the card
 * threw the dialog away. The shell dismisses on a mousedown that STARTED on
 * the backdrop, and renders no onClick at all. */

import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import KEYMAP_SRC from "../lib/keymap.ts?raw";

vi.mock("react", async (orig) => {
  const real = await orig<typeof import("react")>();
  const stubs = {
    useState: (v: unknown) => [typeof v === "function" ? (v as () => unknown)() : v, () => {}],
    useRef: (v: unknown) => ({ current: v }),
    useEffect: () => {},
    useLayoutEffect: () => {},
    useMemo: (f: () => unknown) => f(),
    useCallback: (f: unknown) => f,
  };
  return { ...real, ...stubs, default: { ...real, ...stubs } };
});

const closeDialog = vi.fn();
vi.mock("../state/store", () => ({
  useUi: (sel: (s: unknown) => unknown) =>
    sel({ openDialog: "rename", dialogTarget: "s1", closeDialog, setAlias: vi.fn() }),
}));
vi.mock("../lib/windowName", () => ({ windowName: () => "s1", nameTakenBy: () => "" }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { Modal, backdropPress, modalEscape, pushModal } = await import("../components/Modal");
const { RenameDialog } = await import("../components/dialogs/RenameDialog");

type Props = Record<string, unknown> & {
  onMouseDown?: (e: { target: unknown; currentTarget: unknown }) => void;
};

function key(k: string, prevented = false) {
  return {
    key: k,
    defaultPrevented: prevented,
    preventDefault: vi.fn(),
    stopPropagation: vi.fn(),
  } as unknown as KeyboardEvent & { preventDefault: ReturnType<typeof vi.fn>; stopPropagation: ReturnType<typeof vi.fn> };
}

describe("RenameDialog — the backdrop dismisses on press, never on click", () => {
  it("renders through the shared Modal", () => {
    const el = RenameDialog() as ReactElement<Props>;
    expect(el.type).toBe(Modal);
    expect(el.props.id).toBe("rename-dialog");
  });

  it("a press that started inside the card (a selection overshooting the edge) keeps it open", () => {
    closeDialog.mockClear();
    const el = RenameDialog() as ReactElement<Props>;
    const shell = Modal(el.props as never) as ReactElement<Props>;
    // The overlay guard (Ctrl+W / Delete) keys on .modal — the class survives.
    expect(shell.props.className).toBe("modal");
    expect(shell.props.id).toBe("rename-dialog");
    expect(shell.props.onClick).toBeUndefined();
    const backdrop = {};
    shell.props.onMouseDown!({ target: { inside: "the input" }, currentTarget: backdrop });
    expect(closeDialog).not.toHaveBeenCalled();
    shell.props.onMouseDown!({ target: backdrop, currentTarget: backdrop });
    expect(closeDialog).toHaveBeenCalledTimes(1);
  });
});

describe("Modal shell", () => {
  it("keeps the .modal class the overlay guard reads, plus the dialog's own", () => {
    const shell = Modal({ onClose: () => {}, className: "wide", id: "x" }) as ReactElement<Props>;
    expect(shell.props.className).toBe("modal wide");
    expect(KEYMAP_SRC).toMatch(/OVERLAY_SELECTOR = '[^']*\.modal\b/);
  });

  it("closeOnBackdrop={false} renders no press handler", () => {
    const shell = Modal({ onClose: () => {}, closeOnBackdrop: false }) as ReactElement<Props>;
    expect(shell.props.onMouseDown).toBeUndefined();
  });

  it("backdropPress ignores presses that bubbled up from the card", () => {
    const onClose = vi.fn();
    const h = backdropPress(onClose);
    const bd = {};
    h({ target: {}, currentTarget: bd } as never);
    expect(onClose).not.toHaveBeenCalled();
    h({ target: bd, currentTarget: bd } as never);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("Modal Escape — the topmost modal only", () => {
  it("closes the top modal and leaves the one under it open", () => {
    const under = {};
    const top = {};
    const popUnder = pushModal(under);
    const popTop = pushModal(top);
    const closeUnder = vi.fn();
    const closeTop = vi.fn();
    const e = key("Escape");
    expect(modalEscape(under, e, { onClose: closeUnder })).toBe(false);
    expect(modalEscape(top, e, { onClose: closeTop })).toBe(true);
    expect(closeTop).toHaveBeenCalledTimes(1);
    expect(closeUnder).not.toHaveBeenCalled();
    expect(e.preventDefault).toHaveBeenCalled();
    popTop();
    // With the top one gone the next Escape belongs to the one under it.
    expect(modalEscape(under, key("Escape"), { onClose: closeUnder })).toBe(true);
    expect(closeUnder).toHaveBeenCalledTimes(1);
    popUnder();
  });

  it("onEscape returning true spends the key without closing (a dropdown, an inline edit)", () => {
    const t = {};
    const pop = pushModal(t);
    const onClose = vi.fn();
    expect(modalEscape(t, key("Escape"), { onClose, onEscape: () => true })).toBe(true);
    expect(onClose).not.toHaveBeenCalled();
    expect(modalEscape(t, key("Escape"), { onClose, onEscape: () => false })).toBe(true);
    expect(onClose).toHaveBeenCalledTimes(1);
    pop();
  });

  it("a key an inner handler already claimed is left alone; other keys are ignored", () => {
    const t = {};
    const pop = pushModal(t);
    const onClose = vi.fn();
    expect(modalEscape(t, key("Escape", true), { onClose })).toBe(false);
    expect(modalEscape(t, key("Enter"), { onClose })).toBe(false);
    expect(onClose).not.toHaveBeenCalled();
    pop();
  });

  it("escapeCapture stops the key so nothing under the window hears it", () => {
    const t = {};
    const pop = pushModal(t);
    const e = key("Escape");
    modalEscape(t, e, { onClose: () => {}, escapeCapture: true });
    expect(e.stopPropagation).toHaveBeenCalled();
    const e2 = key("Escape");
    modalEscape(t, e2, { onClose: () => {} });
    expect(e2.stopPropagation).not.toHaveBeenCalled();
    pop();
  });

  it("closeOnEscape={false} leaves Escape to the dialog", () => {
    const t = {};
    const pop = pushModal(t);
    const onClose = vi.fn();
    const e = key("Escape");
    expect(modalEscape(t, e, { onClose, closeOnEscape: false })).toBe(false);
    expect(onClose).not.toHaveBeenCalled();
    expect(e.preventDefault).not.toHaveBeenCalled();
    pop();
  });
});

describe("dialogs use the shell instead of hand-rolling one", () => {
  const SOURCES = import.meta.glob(["../components/**/*.tsx", "../extensions/**/*.tsx"], {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>;
  // Owned by other work; still hand-rolled on purpose.
  const EXEMPT = ["NewSessionDialog.tsx", "VerifyDialog.tsx", "Modal.tsx"];

  it('no component renders its own className="modal" backdrop', () => {
    const offenders = Object.entries(SOURCES)
      .filter(([path]) => !EXEMPT.some((x) => path.endsWith("/" + x)))
      .filter(([, src]) => /className="modal"/.test(src))
      .map(([path]) => path);
    expect(offenders).toEqual([]);
  });

  it("RenameDialog no longer dismisses on a backdrop click", () => {
    const src = SOURCES["../components/dialogs/RenameDialog.tsx"];
    expect(src).toContain("<Modal");
    expect(src).not.toMatch(/e\.target === e\.currentTarget/);
  });
});
