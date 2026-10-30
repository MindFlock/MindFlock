/** A path you can type OR pick: the text box plus a "Browse…" button that
 * opens the floating folder picker (FolderPickerWindow) on the field's value —
 * or the nearest folder above it that exists, or home.
 *
 * A pick goes through the field's own `onChange`, exactly as typing does, so a
 * screen's save logic stays what it was; `onPicked` is for a field that saves
 * on blur, which a pick never fires (the focus is in the window). Read-only
 * path displays (a resolved path with a Copy button) are not PathFields: there
 * is nothing to fill. */

import {
  useEffect,
  useRef,
  useState,
  type FocusEvent,
  type InputHTMLAttributes,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { api } from "../api/client";
import { closeFolderPicker, openFolderPicker, type FolderPickerRequest } from "../lib/folderPicker";
import { completionHint, cycle, tabComplete, type Listing } from "../lib/pathComplete";

/** The picker request a PathField's Browse… sends: seeded with the field's
 * value (the window walks up to the nearest folder that exists, then home),
 * and a pick goes through the field's own onChange — exactly what typing
 * calls — then `onPicked` for fields that save on blur. Exported for the test. */
export function pathFieldRequest(opts: {
  value: string;
  onChange(value: string): void;
  onPicked?(value: string): void;
  device?: string;
  title?: string;
  anchor?: HTMLElement | null;
  /** The pick or cancel landed (the field forgets its request id). */
  onDone?(): void;
}): Omit<FolderPickerRequest, "id"> {
  const start = opts.value.trim();
  return {
    title: opts.title,
    device: opts.device || "",
    start,
    selected: start,
    anchor: opts.anchor,
    onPick: (p) => {
      opts.onDone?.();
      opts.onChange(p);
      opts.onPicked?.(p);
    },
    onCancel: () => opts.onDone?.(),
  };
}

/** Lists one folder's subfolders by name: /api/browse ("" = home, ~ expanded
 * server-side), dotfolders included — the lib offers those only when the
 * partial starts with a dot. */
export function browseNames(dir: string, hidden = true): Promise<string[]> {
  return api<{ entries?: { name: string }[] }>("/api/browse?path=" + encodeURIComponent(dir) + (hidden ? "&hidden=1" : "")).then((r) =>
    (r.entries || []).map((e) => e.name)
  );
}

/** What a field with Tab completion is told about another device. */
export function remoteCompletionTitle(device: string): string {
  return `Tab completes folders on this computer only — on ${device}, type the path in full or use Browse…`;
}

/** Shell-style Tab completion for a folder field (rules: lib/pathComplete).
 * Tab with the caret at the end of a non-empty path lists its folder (one
 * request per folder: the last listing is kept until the field is focused
 * again) and fills the single match + "/", or the common prefix with the
 * matches shown as a row of chips; Tab / Shift+Tab then cycle them, ↑↓ move
 * the highlight, Enter takes it, Esc closes the row. A miss shakes the field
 * and keeps the focus in it. Anything else — an empty field, the caret
 * mid-text, Shift+Tab with no row, another device's disk — leaves Tab to move
 * the focus as usual. Every fill goes through `onChange`, exactly like typing. */
export function usePathCompletion({
  value,
  onChange,
  device = "",
  list = browseNames,
  rowId,
}: {
  value: string;
  onChange(value: string): void;
  device?: string;
  /** The listing (tests pass their own). */
  list?: (dir: string) => Promise<string[]>;
  /** The chips row's id, for the input's aria-controls. */
  rowId?: string;
}) {
  const listing = useRef<Listing | null>(null);
  // The text a late listing must still find in the field to be allowed to land.
  const latest = useRef(value);
  latest.current = value;
  const [row, setRow] = useState<{ prefix: string; matches: string[]; sel: number } | null>(null);
  const [hint, setHint] = useState("");
  const [missed, setMissed] = useState(false);

  const complete = async (text: string) => {
    const r = await tabComplete(text, list, listing);
    if (latest.current !== text) return; // typed on while the listing came
    const { hint: said, miss } = completionHint(text, r);
    setRow(r && r.matches.length > 1 ? { prefix: r.prefix, matches: r.matches, sel: -1 } : null);
    if (r && r.text !== text) onChange(r.text);
    setHint(said);
    if (miss) setMissed(true);
  };

  const take = (prefix: string, name: string) => {
    onChange(prefix + name + "/");
    setRow(null);
    setHint("completed to " + prefix + name + "/");
  };

  /** True when the key was completion's (the caller then leaves it alone). */
  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>): boolean => {
    if (e.nativeEvent.isComposing) return false;
    if (e.key === "Tab" && !e.ctrlKey && !e.metaKey && !e.altKey) {
      if (row) {
        e.preventDefault();
        const next = cycle(row.matches, latest.current, row.prefix, e.shiftKey ? -1 : 1);
        const sel = row.matches.findIndex((m) => next === row.prefix + m + "/");
        setRow({ ...row, sel });
        onChange(next);
        setHint("completed to " + next);
        return true;
      }
      if (e.shiftKey || device) return false;
      const el = e.currentTarget;
      const text = el.value;
      if (!text || el.selectionStart !== text.length || el.selectionEnd !== text.length) return false;
      e.preventDefault();
      void complete(text);
      return true;
    }
    if (!row) return false;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const n = row.matches.length;
      const step = e.key === "ArrowDown" ? 1 : -1;
      const sel = row.sel < 0 ? (step > 0 ? 0 : n - 1) : (row.sel + step + n) % n;
      setRow({ ...row, sel });
      setHint(row.matches[sel]);
      return true;
    }
    if (e.key === "Enter" && row.sel >= 0) {
      e.preventDefault();
      take(row.prefix, row.matches[row.sel]);
      return true;
    }
    if (e.key === "Escape") {
      // The row only: not the text, and not the dialog the field sits in.
      e.preventDefault();
      e.stopPropagation();
      setRow(null);
      return true;
    }
    return false;
  };

  return {
    onKeyDown,
    /** The user typed: an open row no longer answers the text. */
    onTyped: () => {
      setRow(null);
      setHint("");
    },
    onFocus: () => {
      listing.current = null; // folders may have come or gone since
    },
    onBlur: () => setRow(null),
    inputProps: {
      title: device ? remoteCompletionTitle(device) : undefined,
      "aria-controls": row ? rowId : undefined,
      className: missed ? "path-miss" : undefined,
      onAnimationEnd: () => setMissed(false),
    },
    /** The matches row and the polite hint, drawn under the field. */
    aids: (
      <>
        {row && (
          <PathCandidates
            id={rowId}
            prefix={row.prefix}
            matches={row.matches}
            sel={row.sel}
            onPick={(m) => take(row.prefix, m)}
          />
        )}
        <span className="path-field-live" role="status" aria-live="polite">
          {hint}
        </span>
      </>
    ),
  };
}

/** The folders an ambiguous Tab matched: a wrapping row of chips under the
 * field (never a native <datalist>, which the app can't style or drive). A
 * chip keeps the focus in the field (mousedown is swallowed), so typing just
 * carries on after a pick. */
export function PathCandidates({
  id,
  prefix,
  matches,
  sel,
  onPick,
}: {
  id?: string;
  prefix: string;
  matches: string[];
  sel: number;
  onPick(name: string): void;
}): ReactNode {
  return (
    <span id={id} className="path-cands" role="group" aria-label="Folders that match">
      {matches.map((m, i) => (
        <button
          key={m}
          type="button"
          tabIndex={-1}
          className={"path-cand" + (i === sel ? " active" : "")}
          aria-pressed={i === sel}
          title={prefix + m + "/"}
          onMouseDown={(e) => e.preventDefault()}
          onClick={(e) => {
            // Inside a <label>, a click would also be forwarded to the input.
            e.preventDefault();
            onPick(m);
          }}
        >
          {m}/
        </button>
      ))}
      <span className="path-cands-legend">Tab cycles · Enter takes · Esc closes</span>
    </span>
  );
}

export function PathField({
  value,
  onChange,
  onPicked,
  device = "",
  pickerTitle,
  browseLabel = "Browse…",
  className,
  onKeyDown,
  onFocus,
  onBlur,
  ...input
}: Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange" | "className"> & {
  [data: `data-${string}`]: string | undefined;
} & {
  value: string;
  /** Typing and picking both land here, as the new text. */
  onChange(value: string): void;
  /** After a pick has gone through onChange — for fields that save on blur. */
  onPicked?(value: string): void;
  /** Whose disk the picker browses ("" = this computer). */
  device?: string;
  /** The picker window's title (default "Pick a folder"). */
  pickerTitle?: string;
  browseLabel?: string;
  /** On the wrapper; every other prop is the input's. */
  className?: string;
}) {
  const btnRef = useRef<HTMLButtonElement | null>(null);
  // The request this field has open, so leaving the screen closes ITS window
  // and never one a later field re-targeted.
  const openId = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (openId.current !== null) closeFolderPicker(openId.current, false);
    },
    []
  );

  const comp = usePathCompletion({
    value,
    onChange,
    device,
    rowId: input.id ? input.id + "-matches" : undefined,
  });

  const browse = () => {
    openId.current = openFolderPicker(
      pathFieldRequest({
        value,
        onChange,
        onPicked,
        device,
        title: pickerTitle,
        anchor: btnRef.current,
        onDone: () => {
          openId.current = null;
        },
      })
    );
  };

  return (
    <span className={"path-field" + (className ? " " + className : "")}>
      <input
        type="text"
        autoComplete="off"
        spellCheck={false}
        title={comp.inputProps.title}
        {...input}
        aria-controls={comp.inputProps["aria-controls"] ?? input["aria-controls"]}
        className={comp.inputProps.className}
        onAnimationEnd={comp.inputProps.onAnimationEnd}
        value={value}
        onChange={(e) => {
          comp.onTyped();
          onChange(e.target.value);
        }}
        // Completion first: Enter on a highlighted match takes it rather than
        // reaching a caller that saves (or blurs) on Enter.
        onKeyDown={(e) => {
          if (!comp.onKeyDown(e)) onKeyDown?.(e);
        }}
        onFocus={(e: FocusEvent<HTMLInputElement>) => {
          comp.onFocus();
          onFocus?.(e);
        }}
        onBlur={(e: FocusEvent<HTMLInputElement>) => {
          comp.onBlur();
          onBlur?.(e);
        }}
      />
      <button
        type="button"
        ref={btnRef}
        className="path-field-browse"
        id={input.id ? input.id + "-browse" : undefined}
        title="Pick a folder in a window you can move"
        onClick={(e) => {
          // Inside a <label>, a click would also be forwarded to the input.
          e.preventDefault();
          browse();
        }}
      >
        <span aria-hidden="true">📁 </span>
        {browseLabel}
      </button>
      {comp.aids}
    </span>
  );
}
