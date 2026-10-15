/** Watch the session a peer shared with you (docs/peer-link.md, "Watching
 * their session"). Opened from the rail's "Shared with you" row or the
 * People screen's link card; the link id rides in the dialog target.
 *
 * Three tabs, each one request over the link:
 * - Session — their terminal as it looks right now (GET …/their-screen,
 *   re-asked every 1.5s while the tab is open). Colours only: the server
 *   strips every other escape before it gets here, and lib/ansiSpans renders
 *   runs as plain spans.
 * - Files — their shared folder (…/their-files), click a file to read it
 *   (…/their-file).
 * - Changes — their diff since the share started (…/diff).
 * Their link permissions decide what each tab may show; a refusal is the
 * server's sentence, naming the switch they turned off. Until the safety
 * number is confirmed the tabs are off (their side refuses them anyway) and
 * one line says what to do; messages work, so you can compare it over them.
 * Watching is read-only: what you type goes to them as a message, and the
 * conversation sits right above the send box (People's MessageThread), so a
 * reply shows where you are looking. */

import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { Modal } from "./Modal";
import { useUi } from "../state/store";
import { usePeerLinks } from "../state/queries";
import { ansiLines, runCss } from "../lib/ansiSpans";
import { errMsg } from "../lib/format";
import { toast } from "../lib/toast";
import { openSettings } from "../lib/openSettings";
import { PEER_SCREEN, viewerGateText } from "../lib/peer";
import { MessageThread } from "./settings/screens/peer/MessageThread";

type Tab = "session" | "files" | "changes";

const SCREEN_POLL_MS = 1500;

interface Screen {
  text: string;
  cols: number;
  rows: number;
  agent: string;
}

export function PeerSessionViewer() {
  const open = useUi((s) => s.openDialog === "peer-session");
  const linkId = useUi((s) => (s.openDialog === "peer-session" ? s.dialogTarget : null));
  const close = useUi((s) => s.closeDialog);
  if (!open || !linkId) return null;
  return <Viewer linkId={linkId} onClose={close} />;
}

function Viewer({ linkId, onClose }: { linkId: string; onClose(): void }) {
  const links = usePeerLinks().data?.links || [];
  const link = links.find((l) => l.link_id === linkId);
  const name = link?.peer_name || "them";
  const base = "/api/peer/links/" + encodeURIComponent(linkId);
  const [tab, setTab] = useState<Tab>("session");
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(0);
  // An older server doesn't send the flag: the tabs stay on, and a refusal
  // still says why.
  const gated = link?.sas_verified === false;

  const send = async () => {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    try {
      const r = await api<{ delivered: boolean }>(base + "/message", { json: { text } });
      setDraft("");
      setSent((n) => n + 1);
      if (!r.delivered) toast(name + " isn't taking messages right now — it's in your log");
    } catch (e) {
      toast("Couldn't send — " + errMsg(e));
    }
    setSending(false);
  };

  return (
    <Modal id="peer-session-dialog" onClose={onClose}>
      <div className="psv-card" role="dialog" aria-label={name + "'s shared session"}>
        <div className="psv-head">
          <span className="psv-title">{name}’s shared session</span>
          <span className={"psv-state" + (link?.connected ? " on" : "")}>
            {link?.connected ? "connected" : "offline"}
          </span>
          <div className="psv-tabs" role="tablist">
            {(["session", "files", "changes"] as Tab[]).map((t) => (
              <button
                key={t}
                type="button"
                role="tab"
                aria-selected={!gated && tab === t}
                disabled={gated}
                title={gated ? viewerGateText(name) : undefined}
                className={"psv-tab" + (!gated && tab === t ? " active" : "")}
                onClick={() => setTab(t)}
              >
                {t === "session" ? "Session" : t === "files" ? "Files" : "Changes"}
              </button>
            ))}
          </div>
          <button type="button" className="psv-close" aria-label="Close" onClick={onClose}>
            ×
          </button>
        </div>
        <div className="psv-body">
          {gated ? (
            <p className="psv-msg psv-gate">
              {viewerGateText(name)}{" "}
              <button type="button" className="linklike" onClick={() => openSettings(PEER_SCREEN)}>
                Open People
              </button>
            </p>
          ) : (
            <>
              {tab === "session" && <SessionTab base={base} name={name} />}
              {tab === "files" && <FilesTab base={base} />}
              {tab === "changes" && <ChangesTab base={base} />}
            </>
          )}
        </div>
        <div className="psv-thread" aria-label={"Messages with " + name}>
          <MessageThread linkId={linkId} peerName={name} connected={!!link?.connected} refresh={sent} />
        </div>
        <form
          className="psv-say"
          onSubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          <input
            placeholder={"Message " + name + " (their agent sees it too)"}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          <button type="submit" disabled={!draft.trim() || sending}>
            {sending ? "Sending…" : "Send"}
          </button>
        </form>
      </div>
    </Modal>
  );
}

function SessionTab({ base, name }: { base: string; name: string }) {
  const [screen, setScreen] = useState<Screen | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const s = await api<Screen>(base + "/their-screen");
        if (!live) return;
        setScreen(s);
        setErr("");
      } catch (e) {
        if (live) setErr(errMsg(e));
      }
      if (live) timer = setTimeout(() => void tick(), SCREEN_POLL_MS);
    };
    void tick();
    return () => {
      live = false;
      if (timer) clearTimeout(timer);
    };
  }, [base]);
  const lines = useMemo(() => ansiLines(screen?.text || ""), [screen?.text]);
  if (err && !screen) return <p className="error psv-msg">{err}</p>;
  if (!screen) return <p className="psv-msg">Asking {name} for their screen…</p>;
  if (screen.agent !== "running")
    return (
      <p className="psv-msg">
        {screen.agent === "stopped"
          ? name + "’s shared session isn’t running right now."
          : name + " hasn’t started a shared session."}
      </p>
    );
  return (
    <>
      {err && <p className="error psv-msg">{err} (showing the last screen)</p>}
      <pre className="psv-screen" style={screen.cols ? { minWidth: screen.cols + "ch" } : undefined}>
        {lines.map((runs, i) => (
          <div key={i} className="psv-line">
            {runs.length
              ? runs.map((r, j) => (
                  <span key={j} style={runCss(r.style)}>
                    {r.text}
                  </span>
                ))
              : " "}
          </div>
        ))}
      </pre>
    </>
  );
}

interface FileBody {
  path: string;
  size: number;
  encoding: string;
  content: string;
  truncated: boolean;
}

function FilesTab({ base }: { base: string }) {
  const [files, setFiles] = useState<string[] | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [filter, setFilter] = useState("");
  const [open, setOpen] = useState<FileBody | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let live = true;
    api<{ files: string[]; truncated: boolean }>(base + "/their-files")
      .then((r) => {
        if (!live) return;
        setFiles(r.files || []);
        setTruncated(!!r.truncated);
      })
      .catch((e) => live && setErr(errMsg(e)));
    return () => {
      live = false;
    };
  }, [base]);
  const read = async (path: string) => {
    setErr("");
    try {
      setOpen(await api<FileBody>(base + "/their-file?path=" + encodeURIComponent(path)));
    } catch (e) {
      setErr(errMsg(e));
    }
  };
  if (err && !files) return <p className="error psv-msg">{err}</p>;
  if (!files) return <p className="psv-msg">Listing their folder…</p>;
  const q = filter.trim().toLowerCase();
  const shown = q ? files.filter((f) => f.toLowerCase().includes(q)) : files;
  return (
    <div className="psv-files">
      <div className="psv-filelist">
        <input placeholder="Filter files" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <ul>
          {shown.slice(0, 2000).map((f) => (
            <li key={f}>
              <button
                type="button"
                className={open?.path === f ? "active" : ""}
                title={f}
                onClick={() => void read(f)}
              >
                {f}
              </button>
            </li>
          ))}
        </ul>
        {(truncated || shown.length > 2000) && <p className="psv-note">Not every file is listed — filter to narrow it.</p>}
      </div>
      <div className="psv-file">
        {err && <p className="error psv-msg">{err}</p>}
        {!open ? (
          <p className="psv-msg">Pick a file to read it.</p>
        ) : open.encoding !== "utf-8" ? (
          <p className="psv-msg">
            {open.path} is a binary file ({open.size} bytes).
          </p>
        ) : (
          <>
            <div className="psv-filehead">
              {open.path}
              {open.truncated ? " — only the first part is shown" : ""}
            </div>
            <pre className="psv-code">{open.content}</pre>
          </>
        )}
      </div>
    </div>
  );
}

function ChangesTab({ base }: { base: string }) {
  const [diff, setDiff] = useState<{ diff: string; truncated: boolean } | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let live = true;
    api<{ diff: string; truncated: boolean }>(base + "/diff")
      .then((r) => live && setDiff(r))
      .catch((e) => live && setErr(errMsg(e)));
    return () => {
      live = false;
    };
  }, [base]);
  if (err) return <p className="error psv-msg">{err}</p>;
  if (!diff) return <p className="psv-msg">Asking for their changes…</p>;
  if (!diff.diff.trim()) return <p className="psv-msg">No changes yet.</p>;
  return (
    <pre className="psv-code psv-diff">
      {diff.diff.split("\n").map((l, i) => (
        <div
          key={i}
          className={
            l.startsWith("+") && !l.startsWith("+++")
              ? "add"
              : l.startsWith("-") && !l.startsWith("---")
                ? "del"
                : l.startsWith("@@")
                  ? "hunk"
                  : ""
          }
        >
          {l || " "}
        </div>
      ))}
      {diff.truncated && <div className="psv-note">… the rest is cut off</div>}
    </pre>
  );
}
