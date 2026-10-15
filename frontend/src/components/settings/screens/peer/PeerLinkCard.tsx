/** One person you're linked with (Settings → People): who, how, the safety
 * number, what they may do, the shared folder (or the share form), the joint
 * task (JointTask), messages, their changes, and — on a link you joined that
 * dropped — Reconnect. Each action has its own busy flag and its own inline
 * error. Split out of PeerLinks.tsx; every id, label and API call is the
 * same. */

import { useEffect, useState } from "react";
import { api } from "../../../../api/client";
import { toast } from "../../../../lib/toast";
import { selectSession } from "../../../../lib/sessionActions";
import { useUi } from "../../../../state/store";
import { InlineConfirm } from "../../useSettings";
import { PathField } from "../../../PathField";
import { carrierText, joinPeerCode, newOpId, reconnectKind, roleText, type PeerLink, type PeerMessage } from "../../../../lib/peer";
import { AgentSelect } from "./AgentSelect";
import { JointSection } from "./JointTask";
import { MirrorRow } from "./MirrorRow";
import { errText, useAct } from "./useAct";

const PERM_LABELS: Array<[keyof PeerLink["perms"], string]> = [
  ["messages", "message me"],
  ["diff", "see my changes"],
  ["read_file", "read my files"],
];

export function PeerLinkCard(props: {
  link: PeerLink;
  agents: string[];
  sandbox: boolean;
  tick: number;
  reload(): void;
  onVerified(): void;
}) {
  const { link, agents, sandbox, tick, reload, onVerified } = props;
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
  const [confirm, setConfirm] = useState<"" | "unlink" | "unshare">("");
  const [deleteFiles, setDeleteFiles] = useState(false);
  const [reconnect, setReconnect] = useState("");
  const [msgsOpen, setMsgsOpen] = useState(false);
  const [msgs, setMsgs] = useState<PeerMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [diff, setDiff] = useState<{ stat: unknown[]; diff: string; truncated: boolean } | null>(null);

  // The message log: fetched when opened and again whenever the screen
  // reloads (a peer.message event reloads it); opening it marks them read.
  useEffect(() => {
    if (!msgsOpen) return;
    let live = true;
    api<{ messages: PeerMessage[]; unread: number }>(base + "/messages")
      .then((r) => {
        if (!live) return;
        setMsgs(r.messages || []);
        if (r.unread) void api(base + "/messages/read", { json: {} }).catch(() => undefined);
      })
      .catch((e) => live && setErr(errText(e)));
    return () => {
      live = false;
    };
  }, [msgsOpen, tick, base]);

  const send = () =>
    act("send", async () => {
      const r = await api<{ delivered: boolean }>(base + "/message", { json: { text: draft.trim() } });
      setDraft("");
      if (!r.delivered) toast(`${link.peer_name} isn't taking messages right now — it's in your log`);
    });

  const showDiff = () =>
    act("diff", async () => {
      setDiff(await api<{ stat: unknown[]; diff: string; truncated: boolean }>(base + "/diff"));
    });

  const doReconnect = () => {
    const kind = reconnectKind(reconnect);
    if (!kind) {
      setErr("Paste a fresh invite from them, or their relay address (wss://…) or host:port.");
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

  const openSession = () => {
    if (!link.session_title) return;
    selectSession(link.session_title);
    useUi.getState().closeDialog();
  };

  const how = carrierText(link.carrier);
  const unread = link.unread || 0;

  return (
    <div className="peer-link-card" data-link-id={link.link_id}>
      <p>
        <strong>{link.peer_name}</strong> · {roleText(link.role)} ·{" "}
        <span className={link.connected ? "peer-on" : "peer-off"}>{link.connected ? "connected" : "offline"}</span>
        {how && link.connected ? " " + how : ""}
        {link.peer_app ? <span className="muted"> · MindFlock {link.peer_app}</span> : null}
      </p>
      {link.sas_verified ? (
        <p className="set-hint" data-tour="people-safety">
          Safety number <code className="peer-sas">{link.sas}</code> ✓ checked with them.
        </p>
      ) : (
        <div className="set-row" data-tour="people-safety">
          <span className="set-hint">
            Safety number <code className="peer-sas">{link.sas}</code> — read it to each other. If it differs,
            unlink now.
          </span>
          <button type="button" className="test-btn" onClick={onVerified}>
            It matches
          </button>
        </div>
      )}
      <div className="set-row">
        <span className="set-label">They may</span>
        {PERM_LABELS.map(([key, label]) => (
          <label className="check" key={key}>
            <input
              type="checkbox"
              checked={!!link.perms[key]}
              disabled={!!busy}
              onChange={(e) => act("perms", () => api(base + "/perms", { json: { [key]: e.target.checked } }))}
            />
            {label}
          </label>
        ))}
      </div>

      {link.role === "dialer" && !link.connected && (
        <div className="set-row peer-reconnect">
          <input
            placeholder="paste a fresh invite from them (or their relay address)"
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
      )}

      {link.shared ? (
        <>
          <div className="set-row" data-tour="people-share">
            <span className="set-hint">
              Shared folder in session <strong>{link.session_title || "?"}</strong>.
            </span>
            {link.session_title && (
              <button type="button" className="test-btn" onClick={openSession}>
                Open session
              </button>
            )}
          </div>
          <div className="set-row">
            <span className="set-label">Bring work home</span>
            <PathField placeholder="your repo to bring it into" value={target} onChange={setTarget} pickerTitle="Pick the repo to bring it into" />
            <input placeholder="peer/branch" value={exportBranch} onChange={(e) => setExportBranch(e.target.value)} />
            <button
              type="button"
              className="test-btn"
              disabled={!!busy || !target.trim() || !exportBranch.startsWith("peer/")}
              onClick={() =>
                act(
                  "export",
                  () => api(base + "/export", { json: { target_repo: target.trim(), branch_name: exportBranch } }),
                  "Brought home as " + exportBranch
                )
              }
            >
              Export
            </button>
          </div>
          <button type="button" className="test-btn" disabled={!!busy} onClick={() => setConfirm("unshare")}>
            Stop sharing…
          </button>
        </>
      ) : sandbox ? (
        <>
          <div className="set-row" data-tour="people-share">
            <PathField placeholder="folder to share" value={repo} onChange={setRepo} pickerTitle="Pick the folder to share" />
            <input placeholder="branch (optional)" value={branch} onChange={(e) => setBranch(e.target.value)} />
            <AgentSelect agents={agents} value={chosen} onChange={setProgram} />
            <button
              type="button"
              className="test-btn"
              disabled={!!busy || !repo.trim() || !chosen}
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
          </div>
        </>
      ) : (
        <p className="set-hint" data-tour="people-share">
          Here you can message {link.peer_name} and look at their changes; sharing a folder needs Linux or macOS.
        </p>
      )}

      <JointSection link={link} agents={agents} sandbox={sandbox} tick={tick} reload={reload} />
      {link.joint?.state !== "active" && (link.mirror || link.their_share?.shared) && <MirrorRow link={link} reload={reload} />}

      <div className="set-row">
        <button
          type="button"
          className="test-btn peer-msgs-toggle"
          aria-expanded={msgsOpen}
          onClick={() => setMsgsOpen((o) => !o)}
        >
          Messages{unread > 0 && <span className="queue-tab-badge">{unread > 99 ? "99+" : unread}</span>}
        </button>
        <button type="button" className="test-btn" disabled={!!busy || !link.connected} onClick={showDiff}>
          {busy === "diff" ? "Loading…" : "Their changes"}
        </button>
        <button
          type="button"
          className="test-btn"
          disabled={!link.connected}
          title={link.connected ? "Their screen, files and changes" : "They're offline"}
          onClick={() => useUi.getState().openDialogFor("peer-session", link.link_id)}
        >
          Watch their session
        </button>
        <button type="button" className="test-btn" disabled={!!busy} onClick={() => setConfirm("unlink")}>
          Unlink…
        </button>
      </div>

      {msgsOpen && (
        <div className="peer-msgs">
          {msgs.length ? (
            <ul className="peer-msg-log">
              {msgs.map((m) => (
                <li key={m.id} className={"peer-msg peer-msg-" + m.dir}>
                  <span className="muted">
                    {m.dir === "in" ? link.peer_name : m.by === "agent" ? "your agent" : "you"}
                    {m.dir === "in" && m.delivered_to ? " → " + m.delivered_to : ""}:
                  </span>{" "}
                  {m.text}
                </li>
              ))}
            </ul>
          ) : (
            <p className="set-hint">No messages yet.</p>
          )}
          <div className="set-row">
            <textarea
              rows={2}
              placeholder={`message ${link.peer_name}`}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && draft.trim() && !busy) send();
              }}
            />
            <button type="button" className="test-btn" disabled={!!busy || !draft.trim() || !link.connected} onClick={send}>
              {busy === "send" ? "Sending…" : "Send"}
            </button>
          </div>
          {!link.connected && <span className="set-hint">{link.peer_name} is offline — send once they're back.</span>}
        </div>
      )}

      {diff && (
        <div className="peer-diff">
          <div className="set-row">
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

      {err && <p className="error">{err}</p>}

      {confirm && (
        <InlineConfirm
          title={confirm === "unlink" ? `Unlink ${link.peer_name}?` : "Stop sharing this folder?"}
          body={
            <label className="check">
              <input type="checkbox" checked={deleteFiles} onChange={(e) => setDeleteFiles(e.target.checked)} />
              also delete the shared folder
            </label>
          }
          confirmLabel={confirm === "unlink" ? "Unlink" : "Stop sharing"}
          busy={!!busy}
          onCancel={() => setConfirm("")}
          onConfirm={() => {
            const q = deleteFiles ? "?delete_files=1" : "";
            const path = confirm === "unlink" ? base + q : base + "/share" + q;
            const unlinking = confirm === "unlink";
            setConfirm("");
            void act(unlinking ? "unlink" : "unshare", () => api(path, { method: "DELETE" }), unlinking ? "Unlinked" : "Stopped sharing");
          }}
        />
      )}
    </div>
  );
}
