/** Settings → People → "Let someone into a few of your sessions" (guest
 * links, docs/people.md): pick sessions on THIS device, name who it's for,
 * and hand them a link. They open it in a browser and work in those
 * sessions — their rail lists those and nothing else. Sub-sessions are not
 * picked: they come with their parent, spawned before or after the link.
 *
 * The flow is warning first: ticking "I understand" opens the picker (a
 * dialog: root sessions, Make a link); cancelling it unticks. The owner does
 * not name the guest — the guest picks their own name when they open the
 * link (GuestNameDialog), unique on this rail.
 *
 * The link is opened at a PUBLIC address — a Cloudflare quick tunnel to this
 * server (backend.web.core.guest_tunnel) — because the guest is on some
 * other network; the tailnet/LAN address only reaches someone already on
 * yours. The tunnel takes a few seconds to come up (Cloudflare mints a
 * random name), so the block polls while it says "starting", and it needs
 * cloudflared, which the doctor's install plan offers from right here. The
 * name changes when the server restarts: the URL shown is always the
 * current one.
 *
 * Needs the access gate on (with it off, a tunnel would be the whole machine
 * on the internet), so with it off the block offers the same one-click
 * turn-on Settings → Your devices uses (lib/gateOn).
 *
 * The kill switch — DELETE /api/guests/tunnel — revokes every link and takes
 * the public address down; a guest's open terminal closes on its next
 * frame. It sits here and on the rail (sidebar/GuestBar). */

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../../../api/client";
import type { GuestLink, GuestTunnel } from "../../../../api/types";
import { copyText } from "../../../../lib/clipboard";
import { GATE_ON_NOTE, turnGateOn } from "../../../../lib/gateOn";
import { toast } from "../../../../lib/toast";
import { refreshConfig, reprobeDoctor, useConfig, useInstances } from "../../../../state/queries";
import { displayName } from "../../../../state/store";
import { InstallMissing, type InstallStep } from "../../../dialogs/InstallTerminal";
import { Modal } from "../../../Modal";
import { errText } from "./useAct";

/** One line per link: who (the name they picked when they opened it, else
 * "Guest · not opened yet"), which sessions. */
export function guestLinkLine(l: { label: string; name?: string; sessions: string[] }): string {
  const who = l.name || l.label || "Guest · not opened yet";
  const n = l.sessions.length;
  return `${who} · ${n} session${n === 1 ? "" : "s"}: ${l.sessions.join(", ")}`;
}

/** What the block says about the public address. */
export function tunnelLine(t: GuestTunnel | null | undefined, links: number): string {
  if (!t) return "";
  if (t.state === "up") return "Public link up — anyone with a link reaches these sessions from anywhere.";
  if (t.state === "starting") return "Starting the public link… (Cloudflare is minting its address)";
  if (t.state === "failed") return t.error || "The public link could not be started.";
  if (!t.installed) return "The public link needs cloudflared on this computer (free, no account).";
  return links > 0 ? "Public link off." : "";
}

/** The kill switch's toast. */
export function killedText(revoked: number): string {
  return revoked === 0
    ? "Guest access cut — public link closed."
    : `Guest access cut — ${revoked} link${revoked === 1 ? "" : "s"} revoked, public link closed.`;
}

interface GuestsResponse {
  links: GuestLink[];
  gate_on: boolean;
  tunnel: GuestTunnel;
}

export function GuestLinks() {
  const { data: config } = useConfig();
  const { data: instances } = useInstances();
  const [links, setLinks] = useState<GuestLink[] | null>(null);
  const [tunnel, setTunnel] = useState<GuestTunnel | null>(null);
  const [gateOn, setGateOn] = useState<boolean | null>(null);
  const [picked, setPicked] = useState<Set<string>>(() => new Set());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [made, setMade] = useState<string | null>(null);
  const [install, setInstall] = useState<InstallStep[]>([]);
  // The owner must read the warning before every link: a link is access to
  // this machine, not to a web page. Ticking it opens the picker.
  const [understood, setUnderstood] = useState(false);
  const [pickOpen, setPickOpen] = useState(false);

  // Sessions a link can name: this device's own ROOT sessions (not another
  // device's rows, not a verify run, not one still being created, and not a
  // sub-session — those always come with their parent).
  const sessions = useMemo(
    () => (instances || []).filter((i) => !i.device && !i.hidden_by && !i.pending && !i.locked && !i.parent),
    [instances]
  );

  const ack = (on: boolean) => {
    setUnderstood(on);
    setPickOpen(on);
  };
  const cancelPick = () => {
    setPickOpen(false);
    setUnderstood(false);
  };

  const load = useCallback(async () => {
    try {
      const r = await api<GuestsResponse>("/api/guests");
      setLinks(r.links || []);
      setTunnel(r.tunnel || null);
      setGateOn(!!r.gate_on);
      setErr("");
    } catch (e) {
      setErr(errText(e));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  // While the tunnel is coming up, follow it: the URL appears when it does.
  const starting = tunnel?.state === "starting";
  useEffect(() => {
    if (!starting) return;
    const t = setInterval(() => void load(), 2000);
    return () => clearInterval(t);
  }, [starting, load]);
  // cloudflared missing: the doctor's install plan (the same one-shot
  // install the setup checklist runs) carries it once the server knows the
  // owner wants the public link (POST /api/guests/tunnel said so).
  const needsInstall = !!tunnel && !tunnel.installed && (links?.length || 0) > 0;
  useEffect(() => {
    if (!needsInstall) {
      setInstall([]);
      return;
    }
    let live = true;
    reprobeDoctor()
      .then((d) => live && setInstall(d.install?.steps || []))
      .catch(() => live && setInstall([]));
    return () => {
      live = false;
    };
  }, [needsInstall]);

  const toggle = (title: string) =>
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(title)) next.delete(title);
      else next.add(title);
      return next;
    });

  const create = async () => {
    setBusy(true);
    setErr("");
    try {
      const row = await api<GuestLink & { tunnel: GuestTunnel }>("/api/guests", {
        json: { label: "", sessions: sessions.map((s) => s.title).filter((t) => picked.has(t)) },
      });
      setMade(row.id);
      setPicked(new Set());
      setPickOpen(false);
      setUnderstood(false);
      await load();
      if (row.url) {
        const ok = await copyText(row.url);
        toast(ok ? "Guest link copied — send it to them" : "Guest link made — press Copy to send it");
      } else if (row.tunnel?.installed === false) {
        toast("Guest link made — install cloudflared below to get its public address");
      } else {
        toast("Guest link made — its public address appears in a few seconds");
      }
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (id: string) => {
    setBusy(true);
    try {
      await api("/api/guests/" + encodeURIComponent(id), { method: "DELETE" });
      if (made === id) setMade(null);
      await load();
      toast("Guest link revoked");
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy(false);
    }
  };

  const kill = async () => {
    setBusy(true);
    try {
      const r = await api<{ revoked: number }>("/api/guests/tunnel", { method: "DELETE" });
      setMade(null);
      await load();
      toast(killedText(r.revoked || 0));
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy(false);
    }
  };

  const retryTunnel = async () => {
    setBusy(true);
    setErr("");
    try {
      setTunnel(await api<GuestTunnel>("/api/guests/tunnel", { json: {} }));
      await load();
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy(false);
    }
  };

  const gate = async () => {
    setBusy(true);
    setErr("");
    try {
      await turnGateOn();
      toast(GATE_ON_NOTE);
      await refreshConfig();
      await load();
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy(false);
    }
  };

  const copy = (l: GuestLink) => {
    if (!l.url) {
      toast("No public address yet — wait for the public link, or copy the on-your-network link");
      return;
    }
    void copyText(l.url).then((ok) => toast(ok ? "Guest link copied — send it to them" : "Copy failed"));
  };
  const copyLocal = (l: GuestLink) =>
    void copyText(l.local_url).then((ok) => toast(ok ? "On-your-network link copied" : "Copy failed"));

  const off = gateOn === false || (gateOn === null && config?.auth_enabled === false);
  const live = (links?.length || 0) > 0 || tunnel?.state === "up" || tunnel?.state === "starting";
  const madeRow = made ? links?.find((l) => l.id === made) || null : null;

  return (
    <div className="peer-guests" id="peer-guests">
      <h4 className="set-subtitle">Let someone into a few of your sessions</h4>
      <p className="set-hint">
        No second MindFlock needed: they open the link in a browser — from anywhere, it goes through a Cloudflare
        tunnel to this computer — and type into the sessions you pick, under the names you gave them. Sub-sessions
        come with their parent. They see those sessions and nothing else.
      </p>

      {live && (
        <div className="peer-row peer-guest-kill" id="peer-guests-kill">
          <span className="set-hint">
            {tunnelLine(tunnel, links?.length || 0)}
            {tunnel?.state === "failed" && (
              <>
                {" "}
                <button type="button" className="linklike" disabled={busy} onClick={() => void retryTunnel()}>
                  Try again
                </button>
              </>
            )}
          </span>
          <button
            type="button"
            className="test-btn peer-danger"
            id="peer-guests-kill-btn"
            disabled={busy}
            title="Revoke every guest link and close the public address — open guest terminals close at once"
            onClick={() => void kill()}
          >
            ⏻ Kill the connection
          </button>
        </div>
      )}

      {off ? (
        <div className="peer-row" id="peer-guests-gate">
          <span className="set-hint">
            A guest link only limits anything while every visitor needs a token — turn the access gate on first.
          </span>
          <button type="button" className="test-btn" id="peer-guests-gate-on" disabled={busy} onClick={() => void gate()}>
            {busy ? "Turning on…" : "Turn the gate on"}
          </button>
        </div>
      ) : (
        <div className="peer-guest-make" id="peer-guests-make">
          <div className="peer-danger peer-guest-warn" id="peer-guests-warn" role="alert">
            <strong>This gives another person access to your computer, from anywhere on the internet.</strong> In
            those sessions they type at your agent and your shell: they can read and change the files in that
            worktree, run commands as you, and spend your agent's credits. Only send a link to someone you would
            hand your keyboard to, and press <em>Kill the connection</em> when you're done.
          </div>
          <label className="peer-guest-ack">
            <input
              type="checkbox"
              id="peer-guests-ack"
              checked={understood}
              onChange={(e) => ack(e.target.checked)}
            />{" "}
            I understand this lets them act on my machine — pick the sessions
          </label>
        </div>
      )}

      {pickOpen && (
        <Modal id="peer-guests-pick" role="dialog" aria-modal="true" portal onClose={cancelPick}>
          <form
            id="peer-guests-pick-form"
            onSubmit={(e) => {
              e.preventDefault();
              if (picked.size > 0 && understood) void create();
            }}
          >
            <h2>Which sessions?</h2>
            <p className="set-hint">
              The guest works in the sessions you tick, under the names you see here. Sub-sessions aren't listed —
              they always come with their parent, whether they exist now or get spawned later. They pick their own
              name when they open the link; that's the pill you'll see them as.
            </p>
            {sessions.length === 0 ? (
              <span className="set-hint">Start a session first — a link opens sessions that exist.</span>
            ) : (
              <ul className="peer-guest-pick">
                {sessions.map((s) => (
                  <li key={s.title}>
                    <label>
                      <input
                        type="checkbox"
                        checked={picked.has(s.title)}
                        onChange={() => toggle(s.title)}
                        data-session={s.title}
                      />{" "}
                      {displayName(s.title)}
                      {s.branch && s.branch !== s.title ? <span className="set-hint"> · {s.branch}</span> : null}
                    </label>
                  </li>
                ))}
              </ul>
            )}
            <div className="modal-actions">
              <button type="button" id="peer-guests-cancel" onClick={cancelPick}>
                Cancel
              </button>
              <button
                type="submit"
                className="btn-primary"
                id="peer-guests-create"
                disabled={busy || picked.size === 0 || !understood}
                title={picked.size === 0 ? "Tick at least one session" : undefined}
              >
                {busy ? "Making…" : "Make a link"}
              </button>
            </div>
          </form>
        </Modal>
      )}

      {needsInstall && (
        <div className="peer-needs" id="peer-guests-install">
          <p className="set-hint">
            To reach someone on another network this computer needs <strong>cloudflared</strong> (free, no account;
            MindFlock never downloads it for you). Install it and the public address appears by itself.
          </p>
          <InstallMissing steps={install} onDone={() => void retryTunnel()} />
        </div>
      )}

      {madeRow && (
        <div className="peer-invite" id="peer-guests-made">
          {madeRow.url ? (
            <textarea
              readOnly
              rows={2}
              value={madeRow.url}
              onClick={(e) => (e.target as HTMLTextAreaElement).select()}
            />
          ) : (
            <span className="set-hint" id="peer-guests-made-wait">
              {tunnel?.state === "starting"
                ? "Its public address appears here in a few seconds…"
                : "No public address yet — see above."}
            </span>
          )}
          <div className="set-row">
            <button
              type="button"
              className="test-btn"
              id="peer-guests-copy"
              disabled={!madeRow.url}
              onClick={() => copy(madeRow)}
            >
              Copy link
            </button>
            <button type="button" className="test-btn" onClick={() => copyLocal(madeRow)}>
              Copy on-your-network link
            </button>
            <button type="button" className="test-btn" onClick={() => setMade(null)}>
              Done
            </button>
          </div>
          <span className="set-hint">
            {guestLinkLine(madeRow)}. On a phone the same link opens the phone page. Anyone with the link is in —
            send it to the one person it's for. The address changes when this server restarts; copy it again from
            here.
          </span>
        </div>
      )}

      {err && (
        <p className="error" id="peer-guests-error">
          {err}
        </p>
      )}

      {links && links.length > 0 && (
        <ul className="peer-guest-links" id="peer-guests-list">
          {links.map((l) => (
            <li key={l.id} data-guest={l.id}>
              <span className="set-hint">{guestLinkLine(l)}</span>{" "}
              <button type="button" className="test-btn" disabled={busy} onClick={() => copy(l)}>
                Copy
              </button>{" "}
              <button type="button" className="test-btn" disabled={busy} onClick={() => void revoke(l.id)}>
                Revoke
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
