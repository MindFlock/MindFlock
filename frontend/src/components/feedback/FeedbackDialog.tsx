/** The feedback box (lib/feedback.ts): tell the MindFlock developers about a
 * problem or an idea. Opens blank from the top bar or the palette, or on a
 * draft an agent wrote (the MCP's `report_issue`). The person reads, edits
 * and presses Send; nothing leaves before that. What it says before Send:
 * where the report goes (a public GitHub issue), as whom, and the one line
 * MindFlock adds — which can be unticked. */

import { useEffect, useRef, useState } from "react";
import { Modal } from "../Modal";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";
import { displayName } from "../../state/store";
import {
  KIND_LABEL,
  bridgeFeedbackEvents,
  destinationLine,
  discardDraft,
  loadFeedback,
  pickDraft,
  sendFeedback,
  sentLine,
  useFeedbackUi,
  type FeedbackDraft,
  type FeedbackInfo,
  type FeedbackKind,
} from "../../lib/feedback";
import "./FeedbackDialog.css";

export function FeedbackDialog() {
  const open = useFeedbackUi((s) => s.open);
  const n = useFeedbackUi((s) => s.n);
  const draftId = useFeedbackUi((s) => s.draftId);
  // An agent's draft arriving offers itself (a toast), box open or not.
  useEffect(bridgeFeedbackEvents, []);
  if (!open) return null;
  return <Box key={n} want={draftId} onClose={() => useFeedbackUi.getState().close()} />;
}

function Box({ want, onClose }: { want: string | null; onClose(): void }) {
  const [info, setInfo] = useState<FeedbackInfo | null>(null);
  const [draft, setDraft] = useState<FeedbackDraft | null>(null);
  const [kind, setKind] = useState<FeedbackKind>("bug");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [about, setAbout] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  // Typed before the list came back: the person's words win over a draft.
  const touched = useRef(false);
  const bodyRef = useRef<HTMLTextAreaElement>(null);

  const take = (d: FeedbackDraft | null) => {
    setDraft(d);
    setKind(d?.kind || "bug");
    setTitle(d?.title || "");
    setBody(d?.body || "");
    touched.current = false;
  };

  useEffect(() => {
    let live = true;
    loadFeedback()
      .then((got) => {
        if (!live) return;
        setInfo(got);
        if (!touched.current) take(pickDraft(got.drafts || [], want));
      })
      .catch((e) => live && setErr(errMsg(e)));
    bodyRef.current?.focus();
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const others = (info?.drafts || []).filter((d) => d.id !== draft?.id);
  const canSend = !busy && (!!title.trim() || !!body.trim());

  const send = async () => {
    if (!canSend) return;
    setBusy(true);
    setErr("");
    try {
      const r = await sendFeedback({
        title: title.trim(),
        body: body.trim(),
        kind,
        include_about: about,
        draft_id: draft?.id,
      });
      if (r.via === "browser") {
        window.open(r.url, "_blank");
        // The pre-filled page holds it now: the draft has done its job.
        if (draft) void discardDraft(draft.id).catch(() => undefined);
      }
      toast(sentLine(r), r.via === "github" ? { action: { label: "Open", onClick: () => window.open(r.url, "_blank") } } : undefined);
      onClose();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  const discard = async () => {
    if (!draft || busy) return;
    setBusy(true);
    try {
      await discardDraft(draft.id);
    } catch {
      /* already gone: the same outcome */
    }
    setBusy(false);
    if (others.length) {
      setInfo(info ? { ...info, drafts: others } : info);
      take(others[0]);
    } else {
      onClose();
    }
  };

  const edit = <T,>(set: (v: T) => void) => (v: T) => {
    touched.current = true;
    set(v);
  };

  return (
    <Modal onClose={onClose} className="feedback-modal" role="dialog" aria-modal="true" aria-labelledby="feedback-h">
      <div className="modal-card feedback-card">
        <h3 id="feedback-h">Tell us about MindFlock</h3>
        {draft && (
          <p className="feedback-from" role="status">
            {draft.session ? displayName(draft.session) + "'s agent" : "An agent"} drafted this for you. Read it, change
            anything, then send it.
            {draft.removed > 0 && " " + draft.removed + (draft.removed === 1 ? " path or key was" : " paths or keys were") + " replaced."}
          </p>
        )}
        <div className="feedback-kinds" role="radiogroup" aria-label="What kind of report">
          {(Object.keys(KIND_LABEL) as FeedbackKind[]).map((k) => (
            <label key={k} className={"feedback-kind" + (k === kind ? " on" : "")}>
              <input type="radio" name="feedback-kind" checked={k === kind} onChange={() => edit(setKind)(k)} />
              {KIND_LABEL[k]}
            </label>
          ))}
        </div>
        <label className="feedback-label" htmlFor="feedback-title">
          Title <span className="muted">(optional)</span>
        </label>
        <input
          id="feedback-title"
          className="feedback-input"
          value={title}
          maxLength={200}
          placeholder="One line"
          onChange={(e) => edit(setTitle)(e.target.value)}
        />
        <label className="feedback-label" htmlFor="feedback-body">
          {kind === "bug" ? "What happened, and what did you expect?" : "What's on your mind?"}
        </label>
        <textarea
          id="feedback-body"
          ref={bodyRef}
          className="feedback-input feedback-body"
          rows={8}
          maxLength={20000}
          value={body}
          onChange={(e) => edit(setBody)(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
              e.preventDefault();
              void send();
            }
          }}
        />
        {info && (
          <label className="feedback-about">
            <input type="checkbox" checked={about} onChange={(e) => setAbout(e.target.checked)} />
            <span>
              Add <code>{info.about}</code>
            </span>
          </label>
        )}
        <p className="feedback-where muted">
          {destinationLine(info)} Don't include anything private: your code, company or project names, or keys.
        </p>
        {err && (
          <p className="feedback-err" role="alert">
            {err}
          </p>
        )}
        <div className="feedback-actions">
          {others.length > 0 && (
            <button type="button" className="feedback-next" disabled={busy} onClick={() => take(others[0])}>
              {others.length === 1 ? "1 more draft" : others.length + " more drafts"}
            </button>
          )}
          <span className="feedback-grow" />
          {draft && (
            <button type="button" disabled={busy} onClick={() => void discard()}>
              Discard draft
            </button>
          )}
          <button type="button" disabled={busy} onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="primary" disabled={!canSend} title="Ctrl+Enter" onClick={() => void send()}>
            {busy ? "Sending…" : info && !info.signed_in ? "Open on GitHub" : "Send"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
