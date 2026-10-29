/** One person you're connected with (Settings → People), in three groups:
 *
 *  - Trust — the safety number (It matches / It doesn't match → remove the
 *    connection, with what to do next), a NEW number after a reconnect called
 *    out, what they may do in the folder you share (✓ Saved on change), and
 *    Remove connection… in red.
 *  - Share — your folder (labelled Folder / Branch / Agent), or Reopen when
 *    it is still shared but its session was closed (peer links off, Close),
 *    Bring work home with its result next to the button (branch, repo and
 *    commit count, or the server's one sentence), the joint task (JointTask)
 *    and the Mirror row. Share, Reopen, Export and Mirror are off until the
 *    safety number is confirmed — the server refuses them too. Then
 *    **Share a session…** (ShareSessions: one of your sessions on their rail,
 *    gated or full) and what THEY share with you, each with Open.
 *  - Talk — the conversation (MessageThread) with its send box, Watch their
 *    session, Their changes.
 *
 * The next step leads: Trust first until the number is confirmed, then Share,
 * Talk, Trust. Each action has its own busy flag and inline error. Ids and
 * API calls are the ones the single card used. */

import { useEffect, useRef, useState } from "react";
import { api } from "../../../../api/client";
import { selectSession } from "../../../../lib/sessionActions";
import { useUi } from "../../../../state/store";
import { InlineConfirm } from "../../useSettings";
import { PathField } from "../../../PathField";
import { flashSaved } from "../../savedCue";
import { toast } from "../../../../lib/toast";
import {
  carrierText,
  exportDoneText,
  exportErrorText,
  joinPeerCode,
  newOpId,
  reconnectKind,
  rememberSas,
  roleText,
  sasChanged,
  shareClosed,
  type PeerExportResult,
  type PeerLink,
} from "../../../../lib/peer";
import { AgentSelect } from "./AgentSelect";
import { JointSection } from "./JointTask";
import { MessageThread } from "./MessageThread";
import { MirrorRow } from "./MirrorRow";
import { ShareSessionsDialog, TheirSessions, usePeerSessions } from "./ShareSessions";
import { errText, useAct } from "./useAct";

const PERM_LABELS: Array<[keyof PeerLink["perms"], string]> = [
  ["messages", "message me"],
  ["diff", "see my changes"],
  ["read_file", "read my files"],
];

type Confirm = "" | "unlink" | "unshare" | "mismatch";

export function PeerLinkCard(props: {
  link: PeerLink;
  agents: string[];
  sandbox: boolean;
  tick: number;
  reload(): void;
  onVerified(): void;
  /** Just joined: scroll here and light the card up once. */
  fresh?: boolean;
}) {
  const { link, agents, sandbox, tick, reload, onVerified, fresh } = props;
  const base = "/api/peer/links/" + encodeURIComponent(link.link_id);
  const { busy, err, setErr, act } = useAct(reload);
  const [repo, setRepo] = useState("");
  const [branch, setBranch] = useState("");
  const [program, setProgram] = useState("");
  // The first offered agent is the default (the server orders it so).
  const chosen = program || agents[0] || "";
  const [target, setTarget] = useState("");
  const [exportBranch, setExportBranch] = useState(
    "peer/" + (link.peer_name || "work").replace(/[^A-Za-z0-9._-]+/g, "-")
  );
  const [exported, setExported] = useState<{ ok: boolean; text: string } | null>(null);
  const [busyExport, setBusyExport] = useState(false);
  const [reopenErr, setReopenErr] = useState("");
  // Shared sessions (docs/peer-link.md "Shared sessions"): what I share on
  // this link and what they share with me; the dialog that picks.
  const sessions = usePeerSessions(link.link_id, tick);
  const [sharingOpen, setSharingOpen] = useState(false);
  const [busyReopen, setBusyReopen] = useState(false);
  const [confirm, setConfirm] = useState<Confirm>("");
  const [deleteFiles, setDeleteFiles] = useState(false);
  const [reconnect, setReconnect] = useState("");
  const [diff, setDiff] = useState<{ stat: unknown[]; diff: string; truncated: boolean } | null>(null);
  const root = useRef<HTMLDivElement | null>(null);

  // The number they confirmed is remembered here, so a new one after a
  // reconnect is called out rather than silently swapped in.
  useEffect(() => {
    if (link.sas_verified && link.sas) rememberSas(link.link_id, link.sas);
  }, [link.sas_verified, link.sas, link.link_id]);

  useEffect(() => {
    if (fresh) root.current?.scrollIntoView?.({ block: "center", behavior: "smooth" });
  }, [fresh]);

  const showDiff = () =>
    act("diff", async () => {
      setDiff(await api<{ stat: unknown[]; diff: string; truncated: boolean }>(base + "/diff"));
    });

  const doReconnect = () => {
    const kind = reconnectKind(reconnect);
    if (!kind) {
      setErr(`Paste a fresh invite from ${link.peer_name} (or their relay address).`);
      return;
    }
    void act(
      "reconnect",
      async () => {
        if (kind === "invite") await joinPeerCode(reconnect, newOpId());
        else await api(base + "/address", { json: { address: reconnect.trim() } });
        setReconnect("");
      },
      "Reconnecting to " + link.peer_name + "…"
    );
  };

  const doExport = async () => {
    setExported(null);
    setBusyExport(true);
    try {
      const r = await api<PeerExportResult>(base + "/export", {
        json: { target_repo: target.trim(), branch_name: exportBranch },
      });
      setExported({ ok: true, text: exportDoneText(r, target.trim()) });
    } catch (e) {
      setExported({ ok: false, text: exportErrorText(errText(e)) });
    }
    setBusyExport(false);
    reload();
  };
  // A shared folder whose session was closed (peer links off, or Close):
  // start a new session in the kept folder. A refusal is the server's
  // sentence, shown next to the button.
  const reopen = async () => {
    setReopenErr("");
    setBusyReopen(true);
    try {
      await api(base + "/share/reopen", { json: chosen ? { program: chosen } : {} });
      toast("Reopened — the shared session is starting");
    } catch (e) {
      setReopenErr(errText(e));
    }
    setBusyReopen(false);
    reload();
  };
  const openSession = () => {
    if (!link.session_title) return;
    selectSession(link.session_title);
    useUi.getState().closeDialog();
  };

  const how = carrierText(link.carrier);
  const verified = !!link.sas_verified;
  const changed = sasChanged(link);
  const gate = `Confirm the safety number with ${link.peer_name} first.`;

  const trust = (
    <section className="peer-group peer-group-trust" aria-label="Trust" key="trust">
      <h5 className="peer-group-title">Trust</h5>
      {verified ? (
        <p className="set-hint" data-tour="people-safety">
          Safety number <code className="peer-sas">{link.sas}</code> ✓ checked with them.
        </p>
      ) : (
        <div className="peer-safety" data-tour="people-safety">
          {changed && (
            <p className="peer-warn peer-sas-changed">
              New safety number after reconnecting — confirm it with {link.peer_name} again.
            </p>
          )}
          <p className="set-hint">
            Safety number <code className="peer-sas">{link.sas}</code> — read it to each other. The same number on both
            screens means nobody is in between.
          </p>
          <div className="peer-row">
            <button type="button" className="test-btn peer-sas-match" onClick={onVerified}>
              It matches
            </button>
            <button type="button" className="test-btn peer-sas-mismatch" disabled={!!busy} onClick={() => setConfirm("mismatch")}>
              It doesn't match
            </button>
          </div>
        </div>
      )}
      <div className="set-row peer-perms">
        <span className="set-label">In the folder you share, they may</span>
        <div className="peer-row">
          {PERM_LABELS.map(([key, label]) => (
            <label className="check" key={key}>
              <input
                type="checkbox"
                checked={!!link.perms[key]}
                disabled={!!busy}
                onChange={(e) => {
                  const el = e.currentTarget;
                  void act("perms", () => api(base + "/perms", { json: { [key]: e.target.checked } })).then(
                    (ok) => ok && flashSaved(el)
                  );
                }}
              />
              {label}
            </label>
          ))}
        </div>
      </div>
      <div className="peer-row">
        <button type="button" className="test-btn peer-danger" disabled={!!busy} onClick={() => setConfirm("unlink")}>
          Remove connection…
        </button>
      </div>
    </section>
  );

  const share = (
    <section className="peer-group peer-group-share" aria-label="Share" key="share">
      <h5 className="peer-group-title">Share</h5>
      {link.shared ? (
        <>
          <div data-tour="people-share">
            {shareClosed(link) ? (
              <div className="peer-field peer-reopen">
                <span className="set-hint">Your shared session is closed — its folder is kept.</span>
                <div className="peer-row">
                  {sandbox && (
                    <label className="peer-labelled">
                      <span className="set-hint">Agent</span>
                      <AgentSelect agents={agents} value={chosen} onChange={setProgram} />
                    </label>
                  )}
                  <button
                    type="button"
                    className="test-btn peer-reopen-btn"
                    disabled={busyReopen || !!busy || !verified}
                    title={verified ? "Start a new session in the kept folder" : gate}
                    onClick={() => void reopen()}
                  >
                    {busyReopen ? "Reopening…" : "Reopen"}
                  </button>
                  {!verified && <span className="set-hint peer-gate">{gate}</span>}
                  {reopenErr && <span className="error peer-reopen-error">{reopenErr}</span>}
                </div>
              </div>
            ) : (
              <div className="peer-row">
                <span className="set-hint">
                  Your folder is shared in session <strong>{link.session_title || "?"}</strong>.
                </span>
                {link.session_title && (
                  <button type="button" className="test-btn" onClick={openSession}>
                    Open session
                  </button>
                )}
              </div>
            )}
          </div>
          <div className="peer-field">
            <span className="set-label">Bring work home</span>
            <div className="peer-row">
              <label className="peer-labelled peer-grow">
                <span className="set-hint">Your repo</span>
                <PathField placeholder="the repo to bring it into" value={target} onChange={setTarget} pickerTitle="Pick the repo to bring it into" />
              </label>
              <label className="peer-labelled">
                <span className="set-hint">As branch</span>
                <input placeholder="peer/branch" value={exportBranch} onChange={(e) => setExportBranch(e.target.value)} />
              </label>
              <button
                type="button"
                className="test-btn peer-export"
                disabled={busyExport || !!busy || !verified || !target.trim() || !exportBranch.startsWith("peer/")}
                title={verified ? undefined : gate}
                onClick={() => void doExport()}
              >
                {busyExport ? "Bringing home…" : "Export"}
              </button>
              {!verified && !exported && <span className="set-hint peer-gate">{gate}</span>}
              {exported && (
                <span role="status" className={exported.ok ? "set-hint peer-export-done" : "error peer-export-error"}>
                  {exported.text}
                </span>
              )}
            </div>
          </div>
          <div className="peer-row">
            <button type="button" className="test-btn" disabled={!!busy} onClick={() => setConfirm("unshare")}>
              Stop sharing…
            </button>
          </div>
        </>
      ) : sandbox ? (
        <div className="peer-field" data-tour="people-share">
          {!verified && <p className="set-hint peer-share-gate">Confirm the safety number before you share.</p>}
          <div className="peer-row">
            <label className="peer-labelled peer-grow">
              <span className="set-hint">Folder</span>
              <PathField placeholder="the folder to share" value={repo} onChange={setRepo} pickerTitle="Pick the folder to share" />
            </label>
            <label className="peer-labelled">
              <span className="set-hint">Branch</span>
              <input placeholder="optional" value={branch} onChange={(e) => setBranch(e.target.value)} />
            </label>
            <label className="peer-labelled">
              <span className="set-hint">Agent</span>
              <AgentSelect agents={agents} value={chosen} onChange={setProgram} />
            </label>
          </div>
          <div className="peer-row">
            <button
              type="button"
              className="test-btn peer-share-btn"
              disabled={!!busy || !verified || !repo.trim() || !chosen}
              onClick={() =>
                act(
                  "share",
                  () =>
                    api(base + "/share", {
                      json: { repo_path: repo.trim(), branch: branch.trim() || undefined, program: chosen },
                    }),
                  "Shared — the sandboxed session is starting"
                )
              }
            >
              {busy === "share" ? "Sharing…" : "Share a folder"}
            </button>
            <span className="set-hint">An agent works on a sandboxed copy; nothing else on your machine is visible.</span>
          </div>
        </div>
      ) : (
        <p className="set-hint" data-tour="people-share">
          Here you can message {link.peer_name} and look at their changes; sharing a folder needs Linux or macOS.
        </p>
      )}

      <div className="peer-row peer-sessions" data-tour="people-share-session">
        <button
          type="button"
          className="test-btn"
          id={"peer-share-session-" + link.link_id}
          disabled={!verified}
          title={verified ? `Put one of your sessions on ${link.peer_name}’s rail` : gate}
          onClick={() => setSharingOpen(true)}
        >
          Share a session…
        </button>
        <span className="set-hint">
          {sessions.view.mine.length
            ? `${sessions.view.mine.length} shared with ${link.peer_name}`
            : `${link.peer_name} opens it on their rail and types into it.`}
        </span>
      </div>
      <TheirSessions link={link} view={sessions.view} />
      {sharingOpen && (
        <ShareSessionsDialog
          link={link}
          sandbox={sandbox}
          view={sessions.view}
          onChange={(v) => {
            sessions.setView(v);
            reload();
          }}
          onClose={() => setSharingOpen(false)}
        />
      )}

      <JointSection link={link} agents={agents} sandbox={sandbox} tick={tick} reload={reload} />
      {link.joint?.state !== "active" && (link.mirror || link.their_share?.shared) && <MirrorRow link={link} reload={reload} />}
    </section>
  );

  const talk = (
    <section className="peer-group peer-group-talk" aria-label="Talk" key="talk">
      <h5 className="peer-group-title">Talk</h5>
      <MessageThread linkId={link.link_id} peerName={link.peer_name} connected={link.connected} withSend refresh={tick} />
      <div className="peer-row">
        <button
          type="button"
          className="test-btn"
          disabled={!link.connected}
          title={link.connected ? "Their screen, files and changes" : "They're offline"}
          onClick={() => useUi.getState().openDialogFor("peer-session", link.link_id)}
        >
          Watch their session
        </button>
        <button type="button" className="test-btn" disabled={!!busy || !link.connected} onClick={showDiff}>
          {busy === "diff" ? "Loading…" : "Their changes"}
        </button>
      </div>
      {diff && (
        <div className="peer-diff">
          <div className="peer-row">
            <span className="set-hint">
              {link.peer_name}'s changes (read only){diff.truncated ? " — cut short" : ""}
            </span>
            <button type="button" className="test-btn" onClick={() => setDiff(null)}>
              Close
            </button>
          </div>
          {diff.stat?.length ? (
            <pre className="peer-diff-stat">
              {diff.stat
                .map((x) => (typeof x === "string" ? x : Object.values(x as Record<string, unknown>).join(" ")))
                .join("\n")}
            </pre>
          ) : null}
          <pre className="peer-diff-body">{diff.diff || "No changes."}</pre>
        </div>
      )}
    </section>
  );

  const confirmView =
    confirm === "mismatch" ? (
      <InlineConfirm
        title={`It doesn't match — remove the connection to ${link.peer_name}?`}
        body={
          <span>
            Someone may be between you. Remove it, then make a new invite and send it a different way (a call, another chat),
            and compare again.
          </span>
        }
        confirmLabel="Remove connection"
        busy={!!busy}
        onCancel={() => setConfirm("")}
        onConfirm={() => {
          setConfirm("");
          void act("unlink", () => api(base, { method: "DELETE" }), "Connection removed — make a new invite to try again");
        }}
      />
    ) : confirm ? (
      <InlineConfirm
        title={confirm === "unlink" ? `Remove the connection to ${link.peer_name}?` : "Stop sharing this folder?"}
        body={
          <>
            <span>
              {confirm === "unlink"
                ? `${link.peer_name} can no longer reach you. Their shared session is closed, not deleted; yours too unless you tick delete.`
                : "Your shared session is closed, not deleted, unless you tick delete. Your own folder is untouched."}
            </span>{" "}
            <label className="check">
              <input type="checkbox" checked={deleteFiles} onChange={(e) => setDeleteFiles(e.target.checked)} />
              also delete the sandbox copy of the shared folder
            </label>
          </>
        }
        confirmLabel={confirm === "unlink" ? "Remove connection" : "Stop sharing"}
        busy={!!busy}
        onCancel={() => setConfirm("")}
        onConfirm={() => {
          const q = deleteFiles ? "?delete_files=1" : "";
          const path = confirm === "unlink" ? base + q : base + "/share" + q;
          const unlinking = confirm === "unlink";
          setConfirm("");
          void act(
            unlinking ? "unlink" : "unshare",
            () => api(path, { method: "DELETE" }),
            unlinking ? "Connection removed" : deleteFiles ? "Stopped sharing — the shared folder is deleted" : "Stopped sharing — the shared folder is kept"
          );
        }}
      />
    ) : null;

  return (
    <div className={"peer-link-card" + (fresh ? " peer-card-fresh" : "")} data-link-id={link.link_id} ref={root}>
      <p className="peer-card-head">
        <strong>{link.peer_name}</strong> · {roleText(link.role)} ·{" "}
        <span className={link.connected ? "peer-on" : "peer-off"}>{link.connected ? "connected" : "offline"}</span>
        {how && link.connected ? " " + how : ""}
        {link.peer_app ? <span className="muted"> · MindFlock {link.peer_app}</span> : null}
      </p>

      {!link.connected &&
        (link.role === "dialer" ? (
          <div className="peer-field peer-reconnect">
            <span className="set-hint">
              {link.peer_name} is offline. It usually comes back by itself; if not, ask {link.peer_name} for a fresh invite and paste
              it here.
            </span>
            <div className="peer-row">
              <input
                placeholder={`a fresh invite from ${link.peer_name}`}
                value={reconnect}
                onChange={(e) => setReconnect(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && reconnect.trim() && !busy) doReconnect();
                }}
              />
              <button type="button" className="test-btn" disabled={!!busy || !reconnect.trim()} onClick={doReconnect}>
                {busy === "reconnect" ? "Reconnecting…" : "Reconnect"}
              </button>
            </div>
          </div>
        ) : (
          <p className="set-hint">{link.peer_name} is offline — it reconnects by itself when they're back.</p>
        ))}

      {verified ? [share, talk, trust] : [trust, share, talk]}

      {err && <p className="error">{err}</p>}
      {confirmView}
    </div>
  );
}
