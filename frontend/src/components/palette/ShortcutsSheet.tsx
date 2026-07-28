/** The "?" keyboard-shortcut cheat-sheet with click-to-rebind (port of app.js
 * section 22's sheet + rebinding editor). Rows come from the live keymap
 * (lib/keymap KEYMAP help entries + CHORDS) so the sheet can never drift
 * from the real bindings. */

import { useEffect, useMemo, useState } from "react";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";
import {
  CHORDS,
  KEYMAP,
  chordKeyFor,
  comboLabel,
  comboProblem,
  defaultCombosFor,
  getKeyOverride,
  hasOverrides,
  keymapVersion,
  resetAllOverrides,
  resetChordOverride,
  resetKeyOverride,
  sameCombo,
  setChordKey,
  setKeyCombos,
  setRebindCapturing,
  subscribeKeymap,
  type Combo,
} from "../../lib/keymap";

const SHORTCUT_EXTRAS: Array<[string, string, string]> = [
  ["View", "F11 / ⌃⌘F", "Toggle fullscreen (desktop app)"],
  ["View", "Ctrl++ / Ctrl+=", "Zoom in (desktop app)"],
  ["View", "Ctrl+-", "Zoom out (desktop app)"],
  ["View", "Ctrl+0", "Reset zoom (desktop app)"],
  ["View", "Ctrl+Shift+R", "Reload the desktop shell"],
  ["Dialogs", "Ctrl+Enter", "Submit the commit dialog / launch a new session"],
  ["Dialogs", "Esc", "Close palette / dialogs / this sheet; cancel a Ctrl+K chord"],
];

interface RowMeta {
  kind: "key" | "chord";
  id: string;
  custom: boolean;
}

type SheetRow = [string, string, RowMeta | null];

function buildSheet(): Array<[string, SheetRow[]]> {
  const order = ["Navigation", "View", "Focused session", "Dialogs"];
  const rows: Record<string, SheetRow[]> = {};
  for (const g of order) rows[g] = [];
  for (const b of KEYMAP) {
    if (!b.help) continue;
    let label = b.help[1];
    let meta: RowMeta | null = null;
    if (b.id) {
      const ov = getKeyOverride(b.id);
      if (ov) {
        label = ov.map(comboLabel).join(" / ");
        // A customized pair keeps its Shift-inverse partners visible.
        if (KEYMAP.some((x) => x.pairOf === b.id))
          label +=
            " / " +
            ov
              .map((o) => comboLabel({ key: o.key, mod: o.mod, shift: true, alt: o.alt }))
              .join(" / ");
      }
      meta = { kind: "key", id: b.id, custom: !!ov };
    }
    rows[b.help[0]].push([label, b.help[2], meta]);
  }
  for (const k of Object.keys(CHORDS)) {
    const suf = chordKeyFor(k);
    rows["Focused session"].push([
      "Ctrl+K " + suf.toUpperCase(),
      CHORDS[k].desc,
      { kind: "chord", id: k, custom: suf !== k },
    ]);
  }
  for (const x of SHORTCUT_EXTRAS) rows[x[0]].push([x[1], x[2], null]);
  return order.map((g) => [g, rows[g]]);
}

interface Capture {
  meta: RowMeta;
  mode: "set" | "add";
}

export function ShortcutsSheet() {
  const open = useUi((s) => s.openDialog === "shortcuts");
  const closeDialog = useUi((s) => s.closeDialog);
  const [capturing, setCapturing] = useState<Capture | null>(null);
  const [, setVersion] = useState(keymapVersion());

  useEffect(() => subscribeKeymap(() => setVersion(keymapVersion())), []);
  useEffect(() => {
    if (!open) setCapturing(null);
  }, [open]);

  // The recorder listens on window in the capture phase — it runs BEFORE the
  // keymap's document-level listener, and the dispatcher bails while the
  // capture flag is set, so recording a combo never triggers its action.
  useEffect(() => {
    setRebindCapturing(!!capturing);
    if (!capturing) return;
    const onKey = (e: KeyboardEvent) => {
      const k = e.key || "";
      if (k === "Control" || k === "Meta" || k === "Shift" || k === "Alt") return;
      e.preventDefault();
      e.stopImmediatePropagation();
      if (k === "Escape") {
        setCapturing(null);
        return;
      }
      const norm = k.length === 1 ? k.toLowerCase() : k;
      const { meta, mode } = capturing;
      if (meta.kind === "chord") {
        if (e.ctrlKey || e.metaKey || e.altKey || norm.length !== 1) {
          toast("Chords take one plain second key — press a single letter or digit");
          return; // stay in capture
        }
        const clash = Object.keys(CHORDS).find((c) => c !== meta.id && chordKeyFor(c) === norm);
        if (clash) {
          toast("Ctrl+K " + norm.toUpperCase() + " is already “" + CHORDS[clash].desc + "”");
          return;
        }
        if (norm === meta.id) resetChordOverride(meta.id);
        else setChordKey(meta.id, norm);
      } else {
        const combo: Combo = { key: norm, mod: e.ctrlKey || e.metaKey, shift: e.shiftKey, alt: e.altKey };
        const problem = comboProblem(combo, meta.id);
        if (problem) {
          toast(problem);
          return; // stay in capture
        }
        if (mode === "add") {
          const cur = getKeyOverride(meta.id) || defaultCombosFor(meta.id);
          if (cur.some((c) => sameCombo(c, combo))) {
            toast(comboLabel(combo) + " is already bound to this action");
            return;
          }
          setKeyCombos(meta.id, cur.concat([combo]));
        } else {
          setKeyCombos(meta.id, [combo]);
        }
      }
      setCapturing(null);
      setVersion(keymapVersion());
      toast("Shortcut saved — ↺ or Restore defaults undoes it");
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [capturing]);

  const sheet = useMemo(() => (open ? buildSheet() : []), [open, capturing, keymapVersion()]);

  if (!open) return null;

  const anyCustom = hasOverrides();

  return (
    <div
      className="shortcuts-overlay"
      onClick={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
    >
      <div className="shortcuts-card">
        <div className="shortcuts-head">
          <span>Keyboard shortcuts</span>
          {anyCustom && (
            <button
              className="shortcuts-restore"
              type="button"
              title="Forget every custom binding and go back to the defaults"
              onClick={() => {
                setCapturing(null);
                resetAllOverrides();
                setVersion(keymapVersion());
                toast("Shortcuts restored to defaults");
              }}
            >
              Restore defaults
            </button>
          )}
          <button className="shortcuts-close" type="button" aria-label="Close" onClick={closeDialog}>
            ✕
          </button>
        </div>
        <p className="shortcuts-hint">
          Click a highlighted shortcut and press the new keys to replace it · <b>+</b> adds an
          extra combo · <b>↺</b> restores its default · Esc cancels.
        </p>
        <div className="shortcuts-body">
          {sheet.map(([group, rows]) => (
            <div className="shortcuts-group" key={group}>
              <h4>{group}</h4>
              {rows.map(([keysLabel, desc, meta], ri) => {
                const isCapturing =
                  capturing && meta && capturing.meta.kind === meta.kind && capturing.meta.id === meta.id;
                return (
                  <div
                    key={ri}
                    className={
                      "shortcut-row" +
                      (meta ? " rebindable" : "") +
                      (meta?.custom ? " customized" : "") +
                      (isCapturing ? " capturing" : "")
                    }
                    title={
                      meta
                        ? meta.kind === "chord"
                          ? "Click, then press a new second key for this Ctrl+K chord"
                          : "Click, then press the new key combo (+ adds another combo)"
                        : undefined
                    }
                    onClick={meta ? () => setCapturing({ meta, mode: "set" }) : undefined}
                  >
                    <span className="shortcut-keys">
                      {isCapturing
                        ? meta!.kind === "chord"
                          ? "Press the new second key…"
                          : capturing!.mode === "add"
                            ? "Press the combo to add…"
                            : "Press the new keys…"
                        : keysLabel.split(" / ").map((combo, ci) => (
                            <span key={ci}>
                              {ci > 0 && <span className="kbd-sep">/</span>}
                              <kbd>{combo}</kbd>
                            </span>
                          ))}
                    </span>
                    <span className="shortcut-desc">{desc}</span>
                    <span className="shortcut-controls">
                      {meta?.kind === "key" && (
                        <button
                          type="button"
                          className="shortcut-add"
                          title="Add another key combo for this action"
                          onClick={(e) => {
                            e.stopPropagation();
                            setCapturing({ meta, mode: "add" });
                          }}
                        >
                          +
                        </button>
                      )}
                      {meta?.custom && (
                        <button
                          type="button"
                          className="shortcut-reset"
                          title="Restore this shortcut’s default"
                          onClick={(e) => {
                            e.stopPropagation();
                            setCapturing(null);
                            if (meta.kind === "chord") resetChordOverride(meta.id);
                            else resetKeyOverride(meta.id);
                            setVersion(keymapVersion());
                          }}
                        >
                          ↺
                        </button>
                      )}
                    </span>
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
