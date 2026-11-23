/** "Send to session…" — a STEP of the Shared files drawer, not a dialog
 * beside it (the owner: the drawer sat over the dialog's own buttons). The
 * drawer's content swaps to this form, with Back; one surface, so nothing
 * can cover it at any window width.
 *
 * For one item or several (the selection bar's Send to session…): pick a
 * session (each says Idle / Working / Question, so a queued send is no
 * surprise), then either POINT ITS AGENT AT THEM (their codes go into its
 * prompt — typed in for you to finish there, or sent at once in ONE message)
 * or SAVE COPIES IN ITS PROJECT FOLDER. One item: at a path (default
 * hub/<name>; a taken path is said before you press Save copy, and never
 * overwritten unless "Replace it" is ticked). Several: each at hub/<name>,
 * or the next free name beside it — never over a file (POST /api/hub/batch,
 * answered per item). Either way the agent can be told where they are. */

import { useEffect, useMemo, useRef, useState } from "react";
import { useInstances } from "../../state/queries";
import { displayName, useUi } from "../../state/store";
import { errMsg } from "../../lib/format";
import { effectiveActivity } from "../../lib/stage";
import { codesOf, copyInto, copyIntoMany, freePath, itemsWord, putInPrompt, rawUrl, stateWord, type HubItem } from "../../lib/hub";
import type { Instance } from "../../api/types";

/** Sessions an item can go to: this device's own, not a shared-folder one. */
export function hubTargets(list: Instance[] | undefined): Instance[] {
  return (list || []).filter(
    (i) => !!i.title && !i.title.includes("::") && !(i as { peer_share?: boolean }).peer_share && !(i as { locked?: boolean }).locked
  );
}

export function HubSendStep({ items, onBack, onDone }: { items: HubItem[]; onBack(): void; onDone(): void }) {
  const sessions = hubTargets(useInstances().data as Instance[] | undefined);
  const focused = useUi((s) => s.focused);
  const many = items.length > 1;
  const one = items[0];
  const [title, setTitle] = useState<string>(() =>
    focused && sessions.some((s) => s.title === focused) ? focused : sessions[0]?.title || ""
  );
  const [mode, setMode] = useState<"prompt" | "copy">("prompt");
  const [message, setMessage] = useState("");
  const [path, setPath] = useState("hub/" + one.name);
  const [replace, setReplace] = useState(false);
  const [tell, setTell] = useState(true);
  const [taken, setTaken] = useState<string>("");
  const [pathErr, setPathErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [find, setFind] = useState("");
  const name = title ? displayName(title) : "the session";
  const ref = useRef<HTMLDivElement>(null);

  // The step takes focus (Escape is Back, from anywhere inside it).
  useEffect(() => {
    ref.current?.querySelector<HTMLElement>("input[type=radio]:checked, button")?.focus({ preventScroll: true });
  }, []);

  const shown = useMemo(() => {
    const f = find.trim().toLowerCase();
    return f ? sessions.filter((s) => (displayName(s.title) + " " + s.title).toLowerCase().includes(f)) : sessions;
  }, [sessions, find]);

  // Where one copy would land: say so BEFORE Save copy when the path is
  // taken (or is a file Shared files never writes).
  useEffect(() => {
    if (many || mode !== "copy" || !title || !path.trim()) {
      setTaken("");
      setPathErr("");
      return;
    }
    let live = true;
    const t = setTimeout(() => {
      freePath(title, one, path.trim())
        .then((r) => {
          if (!live) return;
          setTaken(r.taken ? r.path : "");
          setPathErr("");
        })
        .catch((e) => {
          if (!live) return;
          setTaken("");
          setPathErr(errMsg(e));
        });
    }, 200);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [many, mode, title, path, one]);

  const go = async () => {
    if (!title || busy) return;
    setBusy(true);
    setErr("");
    try {
      if (mode === "prompt") {
        if (await putInPrompt(title, items, message, { focus: true })) onDone();
      } else if (many) {
        await copyIntoMany(title, items, tell);
        onDone();
      } else {
        const dest = taken && !replace ? taken : path.trim();
        await copyInto(title, one, dest, replace, tell);
        onDone();
      }
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  const dest = taken && !replace ? taken : path.trim();
  const them = many ? "them" : "it";

  return (
    <div
      className="hub-send hub-send-step"
      ref={ref}
      role="region"
      aria-labelledby="hub-send-h"
      onKeyDown={(e) => {
        if (e.key === "Escape" && !e.defaultPrevented) {
          e.preventDefault();
          e.stopPropagation();
          onBack();
        }
      }}
    >
      <div className="hub-send-head">
        <button type="button" className="hub-btn hub-back" onClick={onBack} title="Back to the list (Esc)">
          ← Back
        </button>
        <h3 id="hub-send-h">
          Send <span className="hub-send-name">{itemsWord(items)}</span> to a session
        </h3>
      </div>
      {many && (
        <ul className="hub-send-items" aria-label="Being sent">
          {items.map((it) => (
            <li key={it.id} title={it.name + " — " + it.ref}>
              {it.kind === "image" ? (
                <img src={rawUrl(it)} alt="" className="hub-att-thumb" />
              ) : (
                <span className="hub-att-ext" aria-hidden="true">
                  {(it.name.match(/\.([A-Za-z0-9]{1,4})$/) || [])[1] || (it.kind === "text" ? "txt" : "file")}
                </span>
              )}
              <span className="hub-att-name">{it.name}</span>
            </li>
          ))}
        </ul>
      )}
      {!sessions.length ? (
        <p className="hub-empty">No sessions are running here yet — start one with New, then send {them}.</p>
      ) : (
        <>
          {sessions.length > 6 && (
            <input
              className="hub-filter"
              type="search"
              placeholder="Find a session…"
              aria-label="Find a session"
              value={find}
              onChange={(e) => setFind(e.target.value)}
            />
          )}
          <div className="hub-send-list" role="radiogroup" aria-label="Session">
            {shown.map((s) => {
              const st = stateWord(effectiveActivity(s));
              return (
                <label key={s.title} className={"hub-send-row" + (s.title === title ? " on" : "")}>
                  <input type="radio" name="hub-send-session" checked={s.title === title} onChange={() => setTitle(s.title)} />
                  <span className="hub-send-title">{displayName(s.title)}</span>
                  <span className={"hub-send-state is-" + st.toLowerCase().replace(/\s+/g, "-")}>{st}</span>
                  <span className="hub-send-sub muted">{s.repo || s.folder_label || ""}</span>
                </label>
              );
            })}
            {!shown.length && <p className="muted">No session matches "{find}".</p>}
          </div>
          <fieldset className="hub-send-how">
            <legend>How</legend>
            <label className="hub-send-opt">
              <input type="radio" name="hub-send-mode" checked={mode === "prompt"} onChange={() => setMode("prompt")} />
              <span>
                <strong>Point the agent at {them}</strong>
                <span className="muted"> — it reads {many ? "the files" : "the file"} from Shared files.</span>
              </span>
            </label>
            {mode === "prompt" && (
              <div className="hub-send-msg-wrap">
                <label className="hub-send-label" htmlFor="hub-send-msg">
                  Add a message (optional)
                </label>
                <textarea
                  id="hub-send-msg"
                  className="hub-send-msg"
                  rows={2}
                  value={message}
                  onChange={(e) => setMessage(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                      e.preventDefault();
                      void go();
                    }
                  }}
                />
                <p className="hub-send-help muted">
                  With a message, it is sent now{name ? " to " + name : ""}
                  {" "}(queued if it is busy){many ? ", all " + items.length + " codes in one message" : ""}. Without one,{" "}
                  {many ? codesOf(items) + " are" : one.ref + " is"} typed into its prompt for you to finish there.
                </p>
              </div>
            )}
            <label className="hub-send-opt">
              <input type="radio" name="hub-send-mode" checked={mode === "copy"} onChange={() => setMode("copy")} />
              <span>
                <strong>{many ? "Save copies in its project folder" : "Save a copy in its project folder"}</strong>
                <span className="muted"> — stays with the work; you still commit {them}.</span>
              </span>
            </label>
            {mode === "copy" && (
              <div className="hub-send-path">
                {many ? (
                  <p className="hub-send-help muted">
                    Each lands at hub/‹its name› inside {name}'s folder — or the next free name beside it, never over a file.
                  </p>
                ) : (
                  <>
                    <label className="hub-send-label" htmlFor="hub-send-path">
                      Save as (inside {name}'s folder)
                    </label>
                    <input
                      id="hub-send-path"
                      value={path}
                      spellCheck={false}
                      aria-invalid={!!pathErr}
                      onChange={(e) => setPath(e.target.value)}
                    />
                    {pathErr ? (
                      <p className="hub-send-err" role="alert">
                        {pathErr}
                      </p>
                    ) : (
                      taken && (
                        <p className="hub-send-taken" role="status">
                          {path.trim()} already exists there — {replace ? "it will be replaced." : "the copy goes to " + taken + "."}
                          <label className="hub-send-replace">
                            <input type="checkbox" checked={replace} onChange={(e) => setReplace(e.target.checked)} /> Replace it
                          </label>
                        </p>
                      )
                    )}
                  </>
                )}
                <label className="hub-send-replace">
                  <input type="checkbox" checked={tell} onChange={(e) => setTell(e.target.checked)} />{" "}
                  {many ? "Tell " + name + " where they are" : <>Tell {name} it's at {dest || "…"}</>}
                </label>
              </div>
            )}
          </fieldset>
        </>
      )}
      {err && (
        <p className="hub-send-err" role="alert">
          {err}
        </p>
      )}
      <div className="hub-send-actions">
        <button type="button" className="hub-btn" onClick={onBack}>
          Back
        </button>
        <button
          type="button"
          className="hub-btn primary"
          disabled={!title || busy || (mode === "copy" && !many && (!path.trim() || !!pathErr))}
          onClick={() => void go()}
        >
          {busy ? "Sending…" : mode === "copy" ? (many ? "Save copies" : "Save copy") : "Send"}
        </button>
      </div>
    </div>
  );
}
