/** "Send to session…" for one hub item (lib/hub.ts): pick a session, then
 * either put the item's reference in its prompt (typed in, not sent — or
 * sent at once with a message), or copy the file into its folder at a path
 * (default hub/<name>; a taken path is never overwritten unless "Replace it"
 * is ticked). One dialog, three clicks: open it, pick, Send. */

import { useEffect, useMemo, useState } from "react";
import { Modal } from "../Modal";
import { useInstances } from "../../state/queries";
import { displayName, useUi } from "../../state/store";
import { errMsg } from "../../lib/format";
import { copyInto, freePath, putInPrompt, useHubUi, type HubItem } from "../../lib/hub";
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
  const [taken, setTaken] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [find, setFind] = useState("");

  const shown = useMemo(() => {
    const f = find.trim().toLowerCase();
    return f ? sessions.filter((s) => (displayName(s.title) + " " + s.title).toLowerCase().includes(f)) : sessions;
  }, [sessions, find]);

  // Where a copy would land: say so BEFORE Send when the path is taken.
  useEffect(() => {
    if (mode !== "copy" || !title || !path.trim()) {
      setTaken("");
      return;
    }
    let live = true;
    const t = setTimeout(() => {
      freePath(title, item, path.trim())
        .then((r) => live && setTaken(r.taken ? r.path : ""))
        .catch(() => live && setTaken(""));
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
        await copyInto(title, item, dest, replace);
        onClose();
      }
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

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
              {shown.map((s) => (
                <label key={s.title} className={"hub-send-row" + (s.title === title ? " on" : "")}>
                  <input
                    type="radio"
                    name="hub-send-session"
                    checked={s.title === title}
                    onChange={() => setTitle(s.title)}
                  />
                  <span className="hub-send-title">{displayName(s.title)}</span>
                  <span className="hub-send-sub muted">{s.repo || s.folder_label || ""}</span>
                </label>
              ))}
              {!shown.length && <p className="muted">No session matches "{find}".</p>}
            </div>
            <fieldset className="hub-send-how">
              <legend>How</legend>
              <label className="hub-send-opt">
                <input type="radio" name="hub-send-mode" checked={mode === "prompt"} onChange={() => setMode("prompt")} />
                <span>
                  <strong>Put {item.ref} in its prompt</strong>
                  <span className="muted"> — the agent gets the file's path; nothing is copied</span>
                </span>
              </label>
              {mode === "prompt" && (
                <textarea
                  className="hub-send-msg"
                  rows={2}
                  aria-label="Message to send with it (optional)"
                  placeholder="Optional: a message to send with it now, e.g. “fix the layout in this screenshot”. Leave empty to type the reference into its prompt and finish it there."
                  value={message}
                  onChange={(e) => setMessage(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                      e.preventDefault();
                      void go();
                    }
                  }}
                />
              )}
              <label className="hub-send-opt">
                <input type="radio" name="hub-send-mode" checked={mode === "copy"} onChange={() => setMode("copy")} />
                <span>
                  <strong>Copy the file into its folder</strong>
                  <span className="muted"> — for a file the work needs to keep (it is not committed for you)</span>
                </span>
              </label>
              {mode === "copy" && (
                <div className="hub-send-path">
                  <label>
                    <span>at</span>
                    <input
                      value={path}
                      aria-label="Where in the session's folder"
                      spellCheck={false}
                      onChange={(e) => setPath(e.target.value)}
                    />
                  </label>
                  {taken && (
                    <p className="hub-send-taken" role="status">
                      {path.trim()} already exists there —{" "}
                      {replace ? "it will be replaced." : "the copy goes to " + taken + "."}{" "}
                      <label className="hub-send-replace">
                        <input type="checkbox" checked={replace} onChange={(e) => setReplace(e.target.checked)} /> Replace it
                      </label>
                    </p>
                  )}
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
          <button type="button" className="primary" disabled={!title || busy || (mode === "copy" && !path.trim())} onClick={() => void go()}>
            {busy ? "Sending…" : mode === "copy" ? "Copy" : message.trim() ? "Send" : "Put in prompt"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
