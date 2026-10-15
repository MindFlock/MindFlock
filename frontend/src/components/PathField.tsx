/** A path you can type OR pick: the text box plus a "Browse…" button that
 * opens the floating folder picker (FolderPickerWindow) on the field's value —
 * or the nearest folder above it that exists, or home.
 *
 * A pick goes through the field's own `onChange`, exactly as typing does, so a
 * screen's save logic stays what it was; `onPicked` is for a field that saves
 * on blur, which a pick never fires (the focus is in the window). Read-only
 * path displays (a resolved path with a Copy button) are not PathFields: there
 * is nothing to fill. */

import { useEffect, useRef, type InputHTMLAttributes } from "react";
import { closeFolderPicker, openFolderPicker, type FolderPickerRequest } from "../lib/folderPicker";

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

export function PathField({
  value,
  onChange,
  onPicked,
  device = "",
  pickerTitle,
  browseLabel = "Browse…",
  className,
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
        {...input}
        value={value}
        onChange={(e) => onChange(e.target.value)}
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
    </span>
  );
}
