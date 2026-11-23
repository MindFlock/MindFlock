/** "Send to session…" for one hub item (lib/hub.ts): pick a session (each
 * says Idle / Working / Question, so a queued send is no surprise), then
 * either POINT ITS AGENT AT IT (the code goes into its prompt — typed in for
 * you to finish there, or sent at once with a message) or SAVE A COPY IN ITS
 * PROJECT FOLDER at a path (default hub/<name>; a taken path is said before
 * you press Save copy, and never overwritten unless "Replace it" is ticked),
 * telling the agent where it is. One dialog: open it, pick, Send. */

import { useEffect, useMemo, useState } from "react";
import { Modal } from "../Modal";
import { useInstances } from "../../state/queries";
import { displayName, useUi } from "../../state/store";
import { errMsg } from "../../lib/format";
import { effectiveActivity } from "../../lib/stage";
import { copyInto, freePath, putInPrompt, stateWord, useHubUi, type HubItem } from "../../lib/hub";
import type { Instance } from "../../api/types";

/** Sessions an item can go to: this device's own, not a shared-folder one. */
export function hubTargets(list: Instance[] | undefined): Instance[] {
  return (list || []).filter(
    (i) => !!i.title && !i.title.includes("::") && !(i as { peer_share?: boolean }).peer_share && !(i as { locked?: boolean }).locked
  );
}

export function HubSendDialog() {
  const item = useHubUi((s) => s.sending);
  const close = () => useHubUi.getState().openSend(null);
  if (!item) return null;
  return <SendDialog key={item.id} item={item} onClose={close} />;
}

function SendDialog({ item, onClose }: { item: HubItem; onClose(): void }) {
  const sessions = hubTargets(useInstances().data as Instance[] | undefined);
  const focused = useUi((s) => s.focused);
  const [title, setTitle] = useState<string>(() =>
    focused && sessions.some((s) => s.title === focused) ? focused : sessions[0]?.title || ""
  );
  const [mode, setMode] = useState<"prompt" | "copy">("prompt");
  const [message, setMessage] = useState("");
  const [path, setPath] = useState("hub/" + item.name);
  const [replace, setReplace] = useState(false);
  const [tell, setTell] = useState(true);
  const [taken, setTaken] = useState<string>("");
  const [pathErr, setPathErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [find, setFind] = useState("");
  const name = title ? displayName(title) : "the session";

  const shown = useMemo(() => {
    const f = find.trim().toLowerCase();
    return f ? sessions.filter((s) => (displayName(s.title) + " " + s.title).toLowerCase().includes(f)) : sessions;
  }, [sessions, find]);

  // Where a copy would land: say so BEFORE Save copy when the path is taken
  // (or is a file the hub never writes).
  useEffect(() => {
    if (mode !== "copy" || !title || !path.trim()) {
      setTaken("");
      setPathErr("");
      return;
    }
    let live = true;
    const t = setTimeout(() => {
      freePath(title, item, path.trim())
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
  }, [mode, title, path, item]);

  const go = async () => {
    if (!title || busy) return;
    setBusy(true);
    setErr("");
    try {
      if (mode === "prompt") {
        if (await putInPrompt(title, item, message, { focus: true })) onClose();
      } else {
        const dest = taken && !replace ? taken : path.trim();
        await copyInto(title, item, dest, replace, tell);
        onClose();
      }
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  const dest = taken && !replace ? taken : path.trim();

  return (
    <Modal onClose={onClose} className="hub-send-modal" role="dialog" aria-modal="true" aria-labelledby="hub-send-h">
      <div className="modal-card hub-send">
        <h3 id="hub-send-h">
          Send <span className="hub-send-name">{item.name}</span> to a session
        </h3>
        {!sessions.length ? (
          <p className="hub-empty">No sessions are running here yet — start one with New, then send it.</p>
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
                  <strong>Point the agent at it</strong>
                  <span className="muted"> — it reads the file from the hub.</span>
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
                    {" "}(queued if it is busy). Without one, {item.ref} is typed into its prompt for you to finish there.
                  </p>
                </div>
              )}
              <label className="hub-send-opt">
                <input type="radio" name="hub-send-mode" checked={mode === "copy"} onChange={() => setMode("copy")} />
                <span>
                  <strong>Save a copy in its project folder</strong>
                  <span className="muted"> — stays with the work; you still commit it.</span>
                </span>
              </label>
              {mode === "copy" && (
                <div className="hub-send-path">
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
                  <label className="hub-send-replace">
                    <input type="checkbox" checked={tell} onChange={(e) => setTell(e.target.checked)} /> Tell {name} it's at {dest || "…"}
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
        <div className="modal-actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="primary"
            disabled={!title || busy || (mode === "copy" && (!path.trim() || !!pathErr))}
            onClick={() => void go()}
          >
            {busy ? "Sending…" : mode === "copy" ? "Save copy" : "Send"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
