/** Make-PR dialog — asks which branch to merge into before opening the PR.
 *
 * A combobox: the input filters a dropdown of the repo's real branches (remote
 * heads on origin, fetched from /api/instances/{title}/branches) as you type,
 * and you can also just type a name. The last branch chosen for a repo is
 * remembered (store.prBaseByRepo, persisted) and pre-selected next time.
 *
 * The list stays closed until you actually ask for it — typing, or ArrowDown.
 * Focus alone must not open it: the pre-filled branch usually matches several
 * others by substring, so opening on focus buried the buttons behind a list
 * you had to dismiss on every single PR. */

import { useEffect, useMemo, useRef, useState } from "react";
import { instApi } from "../../api/client";
import type { Instance } from "../../api/types";
import { queryClient } from "../../state/queries";
import { useUi } from "../../state/store";
import { submitMakePr } from "../../lib/sessionActions";

interface BranchInfo {
  branches: string[];
  current: string;
  default: string;
}

export function MakePrDialog() {
  const open = useUi((s) => s.openDialog === "make-pr");
  const target = useUi((s) => s.dialogTarget);
  const closeDialog = useUi((s) => s.closeDialog);
  const prBaseByRepo = useUi((s) => s.prBaseByRepo);
  const setPrBase = useUi((s) => s.setPrBase);

  const [value, setValue] = useState("");
  const [info, setInfo] = useState<BranchInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [showList, setShowList] = useState(false);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // The session this PR is for, and its repo (persistence key).
  const inst = useMemo(() => {
    const list = queryClient.getQueryData<Instance[]>(["instances"]) || [];
    return list.find((i) => i.title === target) || null;
  }, [target]);
  const repo = inst?.repo || "";

  // On open: reset, load the branch list, pre-fill from the per-repo memory
  // (else the server-computed default), focus.
  useEffect(() => {
    if (!open || !target) return;
    let alive = true;
    setInfo(null);
    setShowList(false);
    setActive(0);
    setValue(repo ? prBaseByRepo[repo] || "" : "");
    setLoading(true);
    instApi<BranchInfo>(target, "/branches")
      .then((data) => {
        if (!alive) return;
        setInfo(data);
        // No remembered choice → fall back to the server default.
        setValue((prev) => prev || data.default || "");
      })
      .catch(() => alive && setInfo({ branches: [], current: "", default: "" }))
      .finally(() => alive && setLoading(false));
    setTimeout(() => inputRef.current?.focus(), 0);
    return () => {
      alive = false;
    };
  }, [open, target, repo, prBaseByRepo]);

  // Escape dismisses the open dropdown first, then the dialog.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        if (showList) setShowList(false);
        else closeDialog();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, closeDialog, showList]);

  const filtered = useMemo(() => {
    const all = info?.branches || [];
    const q = value.trim().toLowerCase();
    if (!q) return all;
    return all.filter((b) => b.toLowerCase().includes(q));
  }, [info, value]);

  // Keep the highlighted row in range as the filter narrows.
  useEffect(() => {
    setActive((a) => Math.min(a, Math.max(0, filtered.length - 1)));
  }, [filtered.length]);

  if (!open || !target) return null;

  const base = value.trim();
  const onBase = !!info && base === info.current && !!base;

  function choose(branch: string) {
    setValue(branch);
    setShowList(false);
    inputRef.current?.focus();
  }

  function submit() {
    if (!target || !base || onBase) return;
    if (repo) setPrBase(repo, base);
    closeDialog();
    void submitMakePr(target, base);
  }

  function onInputKey(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      // First ArrowDown just reveals the list; later ones move the highlight.
      if (!showList) setShowList(true);
      else setActive((a) => Math.min(a + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (showList && filtered[active]) choose(filtered[active]);
      else submit();
    }
  }

  return (
    <div
      id="make-pr-dialog"
      className="modal"
      onClick={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
    >
      <form
        id="make-pr-form"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <h2>Open pull request</h2>
        <label>
          Merge{" "}
          <span className="muted">
            {info?.current ? `“${info.current}”` : "this branch"}
          </span>{" "}
          into
          <div className="mpr-combo">
            <input
              type="text"
              id="make-pr-base"
              ref={inputRef}
              autoComplete="off"
              spellCheck={false}
              placeholder={loading ? "Loading branches…" : "Type a branch, or ↓ to pick…"}
              value={value}
              onChange={(e) => {
                setValue(e.target.value);
                setShowList(true);
              }}
              onBlur={() => setShowList(false)}
              onKeyDown={onInputKey}
            />
            {showList && filtered.length > 0 && (
              <ul className="mpr-list">
                {filtered.map((b, i) => (
                  <li
                    key={b}
                    className={
                      "mpr-opt" +
                      (i === active ? " active" : "") +
                      (info && b === info.current ? " disabled" : "")
                    }
                    // onMouseDown (not onClick) so it fires before the input blur.
                    onMouseDown={(e) => {
                      e.preventDefault();
                      choose(b);
                    }}
                    onMouseEnter={() => setActive(i)}
                  >
                    {b}
                    {info && b === info.current && (
                      <span className="mpr-tag">current</span>
                    )}
                    {info && b === info.default && b !== info.current && (
                      <span className="mpr-tag">default</span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </label>
        {onBase ? (
          <p className="error" id="make-pr-error">
            Can’t PR a branch into itself — pick a different target.
          </p>
        ) : (
          <p className="set-hint" id="make-pr-hint">
            Remembered for <strong>{repo || "this repo"}</strong> next time.
          </p>
        )}
        <div className="modal-actions">
          <button type="button" id="make-pr-cancel" onClick={closeDialog}>
            Cancel
          </button>
          <button type="submit" disabled={!base || onBase}>
            Open PR
          </button>
        </div>
      </form>
    </div>
  );
}
