/** Settings → People → "Let someone into a few of your sessions" (guest
 * links, docs/people.md): pick sessions on THIS device, name who it's for,
 * and hand them a link. They open it in a browser and work in those
 * sessions — on the rail every other session is crossed out for them.
 *
 * Needs the access gate on (with it off, a link is just the URL everyone can
 * already open), so with it off the block offers the same one-click turn-on
 * Settings → Your devices uses (lib/gateOn). Everything goes through
 * /api/guests; the URL comes back with the row and is shown to copy. */

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../../../api/client";
import type { GuestLink } from "../../../../api/types";
import { copyText } from "../../../../lib/clipboard";
import { GATE_ON_NOTE, turnGateOn } from "../../../../lib/gateOn";
import { toast } from "../../../../lib/toast";
import { refreshConfig, useConfig, useInstances } from "../../../../state/queries";
import { displayName } from "../../../../state/store";
import { errText } from "./useAct";

/** One line per link: who, which sessions. */
export function guestLinkLine(l: { label: string; sessions: string[] }): string {
  const who = l.label || "Guest";
  const n = l.sessions.length;
  return `${who} · ${n} session${n === 1 ? "" : "s"}: ${l.sessions.join(", ")}`;
}

export function GuestLinks() {
  const { data: config } = useConfig();
  const { data: instances } = useInstances();
  const [links, setLinks] = useState<GuestLink[] | null>(null);
  const [gateOn, setGateOn] = useState<boolean | null>(null);
  const [picked, setPicked] = useState<Set<string>>(() => new Set());
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [made, setMade] = useState<GuestLink | null>(null);
  // The owner must read the warning once per visit: a link is access to
  // this machine, not to a web page.
  const [understood, setUnderstood] = useState(false);

  // Sessions a link can name: this device's own work (not another device's
  // rows, not a verify run, not one still being created).
  const sessions = useMemo(
    () => (instances || []).filter((i) => !i.device && !i.hidden_by && !i.pending && !i.locked),
    [instances]
  );

  const load = useCallback(async () => {
    try {
      const r = await api<{ links: GuestLink[]; gate_on: boolean }>("/api/guests");
      setLinks(r.links || []);
      setGateOn(!!r.gate_on);
      setErr("");
    } catch (e) {
      setErr(errText(e));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

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
      const row = await api<GuestLink>("/api/guests", {
        json: { label: label.trim(), sessions: sessions.map((s) => s.title).filter((t) => picked.has(t)) },
      });
      setMade(row);
      setPicked(new Set());
      setLabel("");
      await load();
      const ok = await copyText(row.url);
      toast(ok ? "Guest link copied — send it to them" : "Guest link made — press Copy to send it");
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
      if (made?.id === id) setMade(null);
      await load();
      toast("Guest link revoked");
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

  const copy = (l: GuestLink) =>
    copyText(l.url).then((ok) => toast(ok ? "Guest link copied — send it to them" : "Copy failed"));

  const off = gateOn === false || (gateOn === null && config?.auth_enabled === false);

  return (
    <div className="peer-guests" id="peer-guests">
      <h4 className="set-subtitle">Let someone into a few of your sessions</h4>
      <p className="set-hint">
        No second MindFlock needed: they open the link in a browser and type into the sessions you pick, here on
        this device. Every other session is crossed out for them. Revoke the link whenever you like.
      </p>

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
            <strong>This gives another person access to your computer.</strong> In those sessions they type at
            your agent and your shell: they can read and change the files in that worktree, run commands as you,
            and spend your agent's credits. Only send a link to someone you would hand your keyboard to, and
            revoke it when you're done.
          </div>
          <label className="peer-guest-ack">
            <input
              type="checkbox"
              id="peer-guests-ack"
              checked={understood}
              onChange={(e) => setUnderstood(e.target.checked)}
            />{" "}
            I understand this lets them act on my machine
          </label>
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
          <div className="set-row">
            <input
              id="peer-guests-label"
              placeholder="who it's for (optional)"
              value={label}
              maxLength={80}
              onChange={(e) => setLabel(e.target.value)}
            />
            <button
              type="button"
              className="btn-primary"
              id="peer-guests-create"
              disabled={busy || picked.size === 0 || !understood}
              title={understood ? undefined : "Tick the warning above first"}
              onClick={() => void create()}
            >
              {busy ? "Making…" : "Make a link"}
            </button>
          </div>
        </div>
      )}

      {made && (
        <div className="peer-invite" id="peer-guests-made">
          <textarea readOnly rows={2} value={made.url} onClick={(e) => (e.target as HTMLTextAreaElement).select()} />
          <div className="set-row">
            <button type="button" className="test-btn" id="peer-guests-copy" onClick={() => void copy(made)}>
              Copy link
            </button>
            <button type="button" className="test-btn" onClick={() => setMade(null)}>
              Done
            </button>
          </div>
          <span className="set-hint">
            {guestLinkLine(made)}. On a phone the same link opens the phone page. Anyone with the link is in — send it
            to the one person it's for.
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
              <button type="button" className="test-btn" disabled={busy} onClick={() => void copy(l)}>
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
