/** Settings → Devices ("Your devices"): the computers one person owns.
 *
 * A device joins by holding the fleet key, and only members talk to each
 * other for settings sync, sign-in and ticket claims — a MindFlock that is
 * merely on the same tailnet gets nothing. So this screen is where a second
 * computer becomes one of yours, three ways:
 *  - a code made here and typed there ("Add a device"),
 *  - the other computer asks and this one approves, after both screens show
 *    the same 6-digit code ("Ask to join" there, "Approve" here),
 *  - one click for a computer this one already holds a pasted access token
 *    for ("Add to my devices").
 * Settings sync lives here too (it used to sit under Security): it only ever
 * runs between these devices, so turning it on belongs next to adding them.
 *
 * Reachability comes first: a device the others can't reach (bound to
 * 127.0.0.1) can still make a code nobody can use and still join, then sit
 * "offline" on every other screen — so "This device" says where it listens,
 * and Make reachable (Tailscale mode + the access gate, one save) fixes it
 * wherever it matters: before a code is made, after a join, and in "Match my
 * other devices". When no other MindFlock shows, each tailnet device says
 * why (refused, timed out, asleep …) instead of one sentence for all.
 *
 * Everything is /api/fleet* and /api/settings/sync*. Polls every 3 s while
 * open and refetches on device.* / settings.synced / settings.sync_paused
 * events. No native
 * prompt/confirm anywhere: the desktop app has neither (InlineConfirm). */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../../api/client";
import type { FleetJoin, FleetStatus, SyncStatus } from "../../../api/types";
import { copyText } from "../../../lib/clipboard";
import { toast } from "../../../lib/toast";
import {
  ROTATE_TOKENS_LABEL,
  SYNC_RESUME,
  MAKE_REACHABLE_TEXT,
  addPairedNote,
  admitLine,
  admitToast,
  automationLine,
  candidateBlocker,
  candidateNote,
  fmtCountdown,
  formatCode,
  joinLine,
  joinSettingsNote,
  joinableCandidates,
  joinedToast,
  keyConflicts,
  leftLines,
  liveInvite,
  matchText,
  memberStatus,
  memberUpdateChips,
  pasteJoinBody,
  phoneLinkLine,
  pinChoices,
  plausibleCode,
  readmittedLines,
  removalLines,
  removeConfirmText,
  removedToast,
  requestNote,
  routeCode,
  rolloutLine,
  rolloutRowText,
  syncDeviceLine,
  thisDeviceLine,
  syncLabel,
  unpinReplaces,
  updateAllLine,
} from "../../../lib/fleet";
import { DEVICES_FOCUS_EVENT, joinPeerInvite, takePendingDevicesFocus } from "../../../lib/deviceActions";
import type { DevicesFocus } from "../../../lib/deviceActions";
import { GATE_ON_NOTE, turnGateOn } from "../../../lib/gateOn";
import { fetchSettingsDoc, refreshConfig } from "../../../state/queries";
import { InlineConfirm } from "../useSettings";
import { useMakeReachable } from "../useMakeReachable";
import type { MakeReachableResult } from "../useMakeReachable";
import type { ScreenProps } from "../SettingsDialog";
import { TailscaleCard } from "./TailscaleCard";
import "./devices.css";

const POLL_MS = 3000;
const JOIN_POLL_MS = 2000;

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function Devices(p: ScreenProps) {
  const [st, setSt] = useState<FleetStatus | null>(null);
  const [sync, setSync] = useState<SyncStatus | null>(null);
  const [loadErr, setLoadErr] = useState("");
  /** Which action is in flight ("" = none) — one at a time, so a slow join
   * can't be double-clicked into two. */
  const [busy, setBusy] = useState("");
  const [confirm, setConfirm] = useState<string | null>(null);
  const [codeFor, setCodeFor] = useState<string | null>(null);
  /** The candidate whose "Add to my devices" is waiting on its confirm. */
  const [addFor, setAddFor] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [pasted, setPasted] = useState("");
  /** Remove's "also replace every device's access token" — on by default. */
  const [rotateTokens, setRotateTokens] = useState(true);
  const [now, setNow] = useState(() => Date.now() / 1000);
  /** Make reachable's confirm is open (the This-device row, Add a device,
   * or the joined toast's click). */
  const [reachAsk, setReachAsk] = useState(false);
  /** "Match my other devices": its confirm, and the shared-link checklist
   * steps still failing after it ran. */
  const [matchAsk, setMatchAsk] = useState(false);
  const [matchLeft, setMatchLeft] = useState<{ id: string; title: string; reason: string }[]>([]);
  /** A candidate the sidebar's "Add to my devices…" pointed at. */
  const [focusDevice, setFocusDevice] = useState("");
  const pasteRef = useRef<HTMLInputElement | null>(null);
  const thisRef = useRef<HTMLDivElement | null>(null);
  /** The newest released version ("" until known / GitHub unreachable) —
   * what "Update all my devices" offers. */
  const [latest, setLatest] = useState("");

  const loadFleet = useCallback(async () => {
    try {
      setSt(await api<FleetStatus>("/api/fleet"));
      setLoadErr("");
    } catch (e) {
      setLoadErr(errText(e));
    }
  }, []);
  const loadSync = useCallback(async () => {
    try {
      setSync(await api<SyncStatus>("/api/settings/sync"));
    } catch {
      setSync(null);
    }
  }, []);
  const loadAll = useCallback(() => {
    void loadFleet();
    void loadSync();
  }, [loadFleet, loadSync]);
  const reach = useMakeReachable(loadAll);

  // Opened aimed at something (lib/deviceActions): a candidate's row, or a
  // code for the paste box.
  useEffect(() => {
    const apply = (f: DevicesFocus | null) => {
      if (!f) return;
      if (f.device) setFocusDevice(f.device);
      if (f.paste) setPasted(f.paste);
      if (f.paste || f.focusPaste) setTimeout(() => pasteRef.current?.focus(), 0);
    };
    apply(takePendingDevicesFocus());
    const on = (e: Event) => {
      takePendingDevicesFocus();
      apply((e as CustomEvent<DevicesFocus | null>).detail || null);
    };
    document.addEventListener(DEVICES_FOCUS_EVENT, on);
    return () => document.removeEventListener(DEVICES_FOCUS_EVENT, on);
  }, []);
  // Scroll to it once, when its row first exists (not on every poll).
  const scrolledTo = useRef("");
  useEffect(() => {
    if (!focusDevice || !st || scrolledTo.current === focusDevice) return;
    const el = document.querySelector('[data-candidate="' + CSS.escape(focusDevice) + '"]');
    if (!el) return;
    scrolledTo.current = focusDevice;
    el.scrollIntoView({ block: "center" });
  }, [focusDevice, st]);

  // Poll while the screen is open (it is only mounted while active); the
  // sync status is the slower half, so it rides every other tick.
  useEffect(() => {
    if (!p.active) return;
    loadAll();
    let tick = 0;
    const t = setInterval(() => {
      if (document.hidden) return;
      tick++;
      void loadFleet();
      if (tick % 2 === 0) void loadSync();
    }, POLL_MS);
    return () => clearInterval(t);
  }, [p.active, loadAll, loadFleet, loadSync]);

  // The newest release, once per visit (the server caches it 15 minutes).
  useEffect(() => {
    if (!p.active) return;
    api<{ latest?: string }>("/api/update/check")
      .then((c) => setLatest(String(c?.latest || "")))
      .catch(() => {});
  }, [p.active]);

  // A request arriving, a device joining or leaving, or a sync pass that
  // adopted something: refetch now instead of on the next tick.
  useEffect(() => {
    const ev = window.mindflock?.events;
    if (!ev) return;
    const offs = [
      "device.join_requested",
      "device.joined",
      "device.removed",
      "settings.synced",
      "settings.sync_paused",
    ].map(
      (name) => ev.subscribe(name, () => loadAll())
    );
    return () => offs.forEach((off) => off());
  }, [loadAll]);

  // The invite countdown.
  const invite = liveInvite(st, now);
  const inviteCode = invite?.code || "";
  useEffect(() => {
    if (!inviteCode) return;
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, [inviteCode]);

  // This device's own join attempt: follow it faster than the screen poll
  // while it waits on the other computer, and say how it ended.
  const joinState = st?.join?.state || "idle";
  const lastJoin = useRef<FleetJoin | null>(null);
  useEffect(() => {
    if (joinState !== "waiting" && joinState !== "joining") return;
    const t = setInterval(async () => {
      try {
        const j = await api<FleetJoin>("/api/fleet/request");
        setSt((prev) => (prev ? { ...prev, join: j } : prev));
      } catch {
        /* the screen poll still runs */
      }
    }, JOIN_POLL_MS);
    return () => clearInterval(t);
  }, [joinState]);
  useEffect(() => {
    const j = st?.join || null;
    const was = lastJoin.current?.state;
    lastJoin.current = j;
    if (!j || !was || was === j.state) return;
    if (j.state === "joined") {
      // A local-only joiner is a member the others can't reach: the toast
      // says so, and its click opens Make reachable's confirm right here.
      const t = joinedToast(j);
      toast(
        t.text,
        t.offerReach
          ? {
              duration: 12000,
              onClick: () => {
                setReachAsk(true);
                thisRef.current?.scrollIntoView({ block: "center" });
              },
            }
          : { duration: 6000 }
      );
      // The joined device's settings were just adopted here.
      void fetchSettingsDoc().catch(() => {});
      void refreshConfig();
      void loadAll();
    } else if (j.state === "denied" || j.state === "expired") toast(joinLine(j));
  }, [st?.join, loadAll]);

  /** Run one action: busy while it runs, its error as a toast, then refetch. */
  const run = async (key: string, fn: () => Promise<unknown>, ok?: string) => {
    setBusy(key);
    try {
      await fn();
      if (ok) toast(ok);
    } catch (e) {
      toast(errText(e));
    } finally {
      setBusy("");
      loadAll();
    }
  };

  if (!st) {
    return (
      <>
        <h3 className="set-section-title">Your devices</h3>
        <p className="set-hint">{loadErr ? "Couldn't load your devices: " + loadErr : "Loading…"}</p>
      </>
    );
  }

  const self = st.members.find((m) => m.self);
  const selfHost = st.self.host || st.self.key;
  const selfVersion = self?.version || "";
  const selfCommit = self?.commit || "";
  // A finished rollout stays on screen for a day, then only its effect does.
  const rollout =
    st.update &&
    (st.update.state === "running" ||
      (st.update.state !== "idle" && now - (st.update.finished_at || 0) < 86400))
      ? st.update
      : null;
  const rolloutRunning = rollout?.state === "running";
  const behindLine = st.in_fleet ? updateAllLine(st.members, latest) : "";
  const updateAll = () =>
    run(
      "update-all",
      () => api("/api/fleet/update", { json: latest ? { tag: "v" + latest } : {} }),
      "Updating your devices one at a time — this one last"
    );
  const others = st.members.filter((m) => !m.self);
  // A member whose hello lags the group still comes back as a candidate:
  // it is already yours, so it gets no join buttons.
  const candidates = joinableCandidates(st.candidates);
  const conflicts = keyConflicts(st.members);
  const removals = removalLines(st);
  const lefts = leftLines(st);
  const readmitted = readmittedLines(st);
  const join = st.join;
  const joinBusy = join && (join.state === "waiting" || join.state === "joining");

  const askToJoin = (device: string) =>
    run("ask:" + device, () => api("/api/fleet/request", { json: { device } }));
  const cancelJoin = () =>
    run("cancel-join", () => api("/api/fleet/request", { method: "DELETE" }));
  const joinWithCode = (device: string) =>
    run(
      "code:" + device,
      async () => {
        await api("/api/fleet/join", { json: { device, code: formatCode(code) } });
        setCode("");
        setCodeFor(null);
      }
    );
  const joinWithText = () => {
    // ONE paste box for every code: someone's peer-link invite (mfp1:/mfp2:)
    // is joined as a link to that person, not sent here.
    if (routeCode(pasted).kind === "peer") {
      const code = routeCode(pasted).code;
      setPasted("");
      return void joinPeerInvite(code);
    }
    // A bare code names no device: send it to the one it can be for, or ask.
    const { body, error } = pasteJoinBody(pasted, candidates, codeFor || focusDevice || null);
    if (!body) {
      if (error) toast(error);
      return;
    }
    return run("paste", async () => {
      await api("/api/fleet/join", { json: body });
      setPasted("");
    });
  };
  // One device runs PR review and issue handling, named by the synced
  // `github.automation_device`: "Run here" moves them to this device (every
  // device follows within seconds). There is no "off here": moving them is
  // done from the device that should take them.
  const runHere = () =>
    run(
      "run-here",
      () => api("/api/settings", { json: { github: { automation_device: st.self.key } } }),
      "PR review and issue handling run on this device now"
    );
  const auto = st.in_fleet ? automationLine(st.members) : null;
  const local = st.self_reachable === false;
  const here = thisDeviceLine(st);
  const unreachable = (st.admitted || []).filter((a) => a.state === "unreachable_joiner");
  const match = matchText(st.match);
  const phone = phoneLinkLine(st.phone_link);
  const peers = st.tailnet_peers || [];
  const makeReachable = () =>
    void (async () => {
      try {
        await reach.apply({ reach: true });
        setReachAsk(false);
      } catch (e) {
        toast(errText(e));
      }
    })();
  const reachConfirm = reachAsk && (
    <InlineConfirm
      id="devices-reach-confirm"
      title="Make this device reachable?"
      body={MAKE_REACHABLE_TEXT}
      confirmLabel={reach.busy ? (reach.restarting ? "Restarting…" : "Saving…") : "Make reachable"}
      busy={reach.busy}
      onConfirm={makeReachable}
      onCancel={() => setReachAsk(false)}
    />
  );

  return (
    <>
      <TailscaleCard active={p.active} />
      {/* 0. This device: where it listens, and the fix when the others
          can't reach it. */}
      {here && (
        <div
          className={local ? "devices-warn" : "devices-this"}
          id="devices-this-device"
          ref={thisRef}
          role={local ? "alert" : undefined}
        >
          {local && st.in_fleet && <p className="devices-this-title">Your other devices can't reach this one</p>}
          <p>{here}</p>
          {local && !reachAsk && (
            <button
              type="button"
              className="test-btn devices-primary"
              id="devices-make-reachable"
              disabled={reach.busy}
              onClick={() => setReachAsk(true)}
            >
              Make reachable
            </button>
          )}
          {reachConfirm}
          {reach.timedOut && (
            <p className="devices-note warn">MindFlock hasn't come back yet — reload in a moment.</p>
          )}
        </div>
      )}
      {unreachable.length > 0 && (
        <div className="devices-warn" id="devices-unreachable-joiners" role="alert">
          {unreachable.map((a) => (
            <p key={a.device} data-unreachable={a.device}>
              {admitLine(a)}
            </p>
          ))}
        </div>
      )}
      {/* 1. What needs fixing first. */}
      {st.gate_warning && (
        <div className="devices-warn" id="devices-gate-warning" role="alert">
          <p>
            This device's access gate is off and it's reachable on your tailnet — anyone there can
            control it, and through it your other devices. With the gate on, your other devices
            keep working (they use your devices' key); a phone signs in again with the QR in Mobile.
          </p>
          <div className="devices-actions">
            <button
              type="button"
              className="test-btn"
              id="devices-gate-on"
              disabled={!!busy}
              onClick={() => void run("gate-on", () => turnGateOn(), GATE_ON_NOTE)}
            >
              {busy === "gate-on" ? "Turning on…" : "Turn the gate on"}
            </button>
            <button type="button" className="test-btn" onClick={() => p.gotoScreen("security")}>
              Open Security
            </button>
          </div>
        </div>
      )}
      {st.stale_key && (
        <div className="devices-warn" id="devices-stale-key" role="alert">
          <p>
            {conflicts.length
              ? "This device and " +
                conflicts.map((m) => m.host || m.key).join(" and ") +
                " hold different keys for your devices — they were set up apart. Rejoin this one from " +
                (conflicts.length > 1 ? "one of them" : "it") +
                " to make them one group again."
              : "This device was removed or its device key changed while it was offline. Ask to rejoin."}
          </p>
          <div className="devices-actions">
            {/* In a key conflict, rejoin the group that kept its key — the
                device(s) holding the other one; otherwise any that's up. */}
            {(conflicts.length ? conflicts : others)
              .filter((m) => m.reachable)
              .map((m) => (
                <button
                  key={m.key}
                  type="button"
                  className="test-btn"
                  data-rejoin={m.key}
                  disabled={!!busy || !!joinBusy}
                  onClick={() => void askToJoin(m.key)}
                >
                  Ask {m.host || m.key} to rejoin
                </button>
              ))}
          </div>
        </div>
      )}

      {!st.stale_key && conflicts.length > 0 && (
        <div className="devices-warn" id="devices-key-conflict" role="alert">
          <p>
            {conflicts.map((m) => m.host || m.key).join(" and ")}{" "}
            {conflicts.length > 1 ? "have" : "has"} a different key for your devices — the two
            halves were set up apart. Rejoin one from the other: on{" "}
            {conflicts.length > 1 ? "each of them" : conflicts[0].host || conflicts[0].key}, use
            "Ask {selfHost} to rejoin" in Settings → Devices.
          </p>
        </div>
      )}
      {removals.length > 0 && (
        <div className="devices-warn" id="devices-removals" role="alert">
          {removals.map((r) => (
            <p key={r.key} data-removed={r.key}>
              {r.text}
            </p>
          ))}
          <p className="devices-note">
            Your devices all hold the same key, so any of them can remove another. If you didn't
            make this removal, someone else may be using that device — taking it off your tailnet
            (Tailscale admin console) cuts it off everywhere at once.
          </p>
        </div>
      )}
      {readmitted.length > 0 && (
        <div className="devices-warn" id="devices-readmitted" role="alert">
          {readmitted.map((r) => (
            <div className="devices-row" key={r.key} data-readmitted={r.key}>
              <p>{r.text}</p>
              <button
                type="button"
                className="test-btn"
                data-allow={r.key}
                disabled={!!busy}
                onClick={() =>
                  void run(
                    "allow:" + r.key,
                    () => api("/api/fleet/members/" + encodeURIComponent(r.key) + "/allow", { method: "POST" }),
                    r.host + " is one of your devices here again"
                  )
                }
              >
                {busy === "allow:" + r.key ? "Allowing…" : "Allow it here"}
              </button>
            </div>
          ))}
        </div>
      )}
      {lefts.length > 0 && (
        <div className="devices-left" id="devices-left">
          {lefts.map((r) => (
            <p key={r.key} className="set-hint" data-left={r.key}>
              {r.text}
            </p>
          ))}
        </div>
      )}

      {/* 2. The roster. */}
      <h3 className="set-section-title">Your devices</h3>
      {st.in_fleet ? (
        <ul className="devices-list" id="devices-members">
          {st.members.map((m) => {
            const status = memberStatus(m, selfVersion, selfCommit);
            const chips = memberUpdateChips(m, latest);
            const warn = !m.self && (!!m.error || (m.reachable && !!selfVersion && !!m.version && m.version !== selfVersion));
            return (
              <li key={m.key} data-member={m.key} className={m.self ? "is-self" : ""}>
                <div className="devices-row">
                  <span
                    className={"devices-dot" + (m.self || m.reachable ? " on" : "")}
                    aria-hidden="true"
                  />
                  <span className="devices-name">
                    <span className="devices-name-line">
                      <strong>{m.host || m.key}</strong>
                      {m.automation && (
                        <span
                          className="devices-badge"
                          data-automation={m.key}
                          title="This device runs PR review and issue handling for your repos"
                        >
                          runs PR review &amp; issues
                        </span>
                      )}
                      {chips.map((c) => (
                        <span
                          key={c.text}
                          className={"devices-badge" + (c.warn ? " warn" : "")}
                          data-update-chip={m.key}
                        >
                          {c.text}
                        </span>
                      ))}
                    </span>
                    <span className={"devices-note" + (warn ? " warn" : "")}>{status}</span>
                  </span>
                  <button
                    type="button"
                    className="test-btn"
                    data-remove={m.key}
                    disabled={!!busy || confirm === m.key}
                    onClick={() => {
                      setConfirm(m.key);
                      setRotateTokens(true);
                    }}
                  >
                    {m.self ? "Leave" : "Remove"}
                  </button>
                </div>
                {confirm === m.key &&
                  (m.self ? (
                    <InlineConfirm
                      id="devices-leave-confirm"
                      title="Leave your devices?"
                      body={
                        "This device stops sharing settings, sign-in and ticket claims with the " +
                        "others, and settings sync turns off here. Its own settings stay as they are."
                      }
                      confirmLabel={busy === "leave" ? "Leaving…" : "Leave"}
                      busy={busy === "leave"}
                      onConfirm={() =>
                        void run(
                          "leave",
                          async () => {
                            await api("/api/fleet/leave", { method: "POST" });
                            setConfirm(null);
                          },
                          "This device left your devices"
                        )
                      }
                      onCancel={() => setConfirm(null)}
                    />
                  ) : (
                    <InlineConfirm
                      id="devices-remove-confirm"
                      title={"Remove " + (m.host || m.key) + "?"}
                      body={
                        <>
                          {removeConfirmText(m.host || m.key)}
                          <label className="devices-check" id="devices-remove-rotate">
                            <input
                              type="checkbox"
                              checked={rotateTokens}
                              disabled={busy === "remove:" + m.key}
                              onChange={(e) => setRotateTokens(e.target.checked)}
                            />
                            <span>{ROTATE_TOKENS_LABEL}</span>
                          </label>
                          {!rotateTokens && (
                            <span className="devices-note warn devices-check-warn">
                              Without it, any access token {m.host || m.key} already holds keeps
                              working on your other devices.
                            </span>
                          )}
                        </>
                      }
                      confirmLabel={busy === "remove:" + m.key ? "Removing…" : "Remove"}
                      busy={busy === "remove:" + m.key}
                      onConfirm={() => {
                        const rotate = rotateTokens;
                        void run("remove:" + m.key, async () => {
                          const r = await api<{
                            rekeyed?: string[];
                            missed?: string[];
                            rotated?: string[];
                            rotate_failed?: string[];
                          }>("/api/fleet/members/" + encodeURIComponent(m.key) + "/remove", {
                            json: { rotate_tokens: rotate },
                          });
                          setConfirm(null);
                          toast(removedToast(m.host || m.key, r, rotate), { duration: 12000 });
                        });
                      }}
                      onCancel={() => setConfirm(null)}
                    />
                  ))}
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="set-hint set-block-hint" id="devices-intro">
          Your devices share settings, sign-in and ticket claims. Add a computer you own:
        </p>
      )}
      {st.in_fleet && (behindLine || rollout) && (
        <div className="devices-update" id="devices-update" data-rollout={rollout?.state || "idle"}>
          {rollout && (
            <p className={"set-hint" + (rollout.state === "halted" ? " devices-hint-warn" : "")} id="devices-rollout-line">
              {rolloutLine(rollout)}
            </p>
          )}
          {behindLine && !rolloutRunning && (
            <p className="set-hint" id="devices-update-line">
              {behindLine}
            </p>
          )}
          {rollout && (
            <ul className="devices-update-rows" id="devices-update-rows">
              {rollout.members.map((r) => (
                <li
                  key={r.key}
                  data-rollout-row={r.key}
                  data-step={r.step}
                  className={"devices-note" + (r.step === "failed" ? " warn" : "")}
                >
                  {rolloutRowText(r)}
                </li>
              ))}
            </ul>
          )}
          {behindLine && !rolloutRunning && (
            <button
              type="button"
              className="test-btn"
              id="devices-update-all"
              disabled={!!busy}
              onClick={() => void updateAll()}
            >
              {busy === "update-all" ? "Starting…" : "Update all my devices to v" + latest}
            </button>
          )}
        </div>
      )}
      {auto && (
        <div className="set-row set-switch-row" id="devices-run-here" data-runs-here={auto.here ? "1" : "0"}>
          <span className="devices-run-here-text">
            <span className="set-label">PR review and issue handling</span>
            <span className={"set-hint" + (auto.runner ? "" : " devices-hint-warn")} id="devices-run-here-hint">
              {auto.text}
            </span>
          </span>
          {auto.canMove && (
            <button
              type="button"
              className="test-btn"
              id="devices-run-here-btn"
              disabled={!!busy}
              onClick={() => void runHere()}
            >
              {busy === "run-here" ? "Moving…" : "Run here"}
            </button>
          )}
        </div>
      )}

      {match && (
        <div className="set-row set-switch-row" id="devices-match">
          <span className="devices-run-here-text">
            <span className="set-label">{match}</span>
            {matchLeft.length > 0 && (
              <span className="set-hint devices-hint-warn" id="devices-match-left">
                Still to do here for the phone link:{" "}
                {matchLeft.map((m) => m.title + (m.reason ? " (" + m.reason + ")" : "")).join("; ")} — see
                Settings → Mobile.
              </span>
            )}
          </span>
          {!matchAsk && (
            <button
              type="button"
              className="test-btn"
              id="devices-match-btn"
              disabled={reach.busy}
              onClick={() => setMatchAsk(true)}
            >
              Match my other devices
            </button>
          )}
        </div>
      )}
      {match && matchAsk && (
        <InlineConfirm
          id="devices-match-confirm"
          title={match + "?"}
          body={
            (st.match?.reachable ? MAKE_REACHABLE_TEXT + " " : "") +
            (st.match?.shared_link
              ? "This device also answers your phone link “" + st.match.shared_link + "”, so your phone reaches whichever of your devices is awake."
              : "")
          }
          confirmLabel={reach.busy ? "Applying…" : "Match"}
          busy={reach.busy}
          onConfirm={() =>
            void (async () => {
              try {
                const res: MakeReachableResult | null = await reach.apply({
                  reach: !!st.match?.reachable,
                  sharedLink: st.match?.shared_link || "",
                });
                setMatchAsk(false);
                setMatchLeft(
                  (res?.shared_link?.steps || [])
                    .filter((x) => x.state === "fail")
                    .map((x) => ({ id: x.id, title: x.title, reason: x.reason }))
                );
              } catch (e) {
                toast(errText(e));
              }
            })()
          }
          onCancel={() => setMatchAsk(false)}
        />
      )}
      {phone && st.in_fleet && (
        <div className="set-row" id="devices-phone-link">
          <span className="set-hint">{phone.text}</span>
          {phone.hostHere && (
            <button
              type="button"
              className="test-btn"
              id="devices-phone-host-here"
              disabled={!!busy}
              onClick={() =>
                void run(
                  "host-here",
                  () => api("/api/settings", { json: { general: { shared_link: st.phone_link?.name || "" } } }),
                  "This device answers your phone link too — Settings → Mobile shows what's left"
                )
              }
            >
              Host here
            </button>
          )}
        </div>
      )}

      {/* 3. Someone asking to join. */}
      {st.requests.length > 0 && (
        <div className="devices-requests" id="devices-requests">
          {st.requests.map((r) => (
            <div className="devices-request" key={r.id} data-request={r.id}>
              <div className="devices-row">
                <span className="devices-name">
                  <span>
                    <strong>{r.host || r.device}</strong> wants to join your devices
                  </span>
                  <span className="devices-pin" aria-label={"code " + r.code}>
                    {r.code}
                  </span>
                  <span className="devices-note">{requestNote(r)}</span>
                </span>
                <button
                  type="button"
                  className="test-btn devices-primary"
                  data-approve={r.id}
                  disabled={!!busy}
                  onClick={() =>
                    void run("approve:" + r.id, async () => {
                      // One waiting on another member: the answer goes
                      // there, with the code this screen showed.
                      const res = await api<{ sync_error?: string }>(
                        "/api/fleet/requests/" + encodeURIComponent(r.id) + "/approve",
                        { json: { via: r.via || "", code: r.code } }
                      );
                      toast(
                        admitToast(r.host || r.device, (r.host || r.device) + " is joining your devices", res?.sync_error),
                        res?.sync_error ? { duration: 8000 } : undefined
                      );
                    })
                  }
                >
                  Approve
                </button>
                <button
                  type="button"
                  className="test-btn"
                  data-deny={r.id}
                  disabled={!!busy}
                  onClick={() =>
                    void run("deny:" + r.id, () =>
                      api("/api/fleet/requests/" + encodeURIComponent(r.id) + "/deny", {
                        json: { via: r.via || "" },
                      })
                    )
                  }
                >
                  Deny
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 4. Add a device: a code for the new computer to type. */}
      <h4 className="set-subtitle">Add a device</h4>
      {local && !invite ? (
        <div className="devices-warn" id="devices-invite-blocked">
          <p>
            A new computer couldn't use a code made here yet: this one only listens on 127.0.0.1,
            so nothing else can reach it. Make it reachable first, then add the device.
          </p>
          {!reachAsk && (
            <button
              type="button"
              className="test-btn devices-primary"
              disabled={reach.busy}
              onClick={() => {
                setReachAsk(true);
                thisRef.current?.scrollIntoView({ block: "center" });
              }}
            >
              Make reachable
            </button>
          )}
        </div>
      ) : invite ? (
        <div className="devices-invite" id="devices-invite">
          <div className="devices-code" id="devices-invite-code">
            {invite.code}
          </div>
          <div className="devices-command">
            <code id="devices-invite-command">{invite.command}</code>
            <button
              type="button"
              className="test-btn"
              id="devices-invite-copy"
              onClick={() =>
                copyText(invite.command).then((ok) => toast(ok ? "Command copied" : "Copy failed"))
              }
            >
              Copy
            </button>
          </div>
          <span className="set-hint">
            On the new computer: Settings → Devices → choose {selfHost} → Enter code (or run the
            command). Works once · expires in{" "}
            <span id="devices-invite-countdown">{fmtCountdown(invite.expires_at, now)}</span>.
          </span>
          <button
            type="button"
            className="test-btn devices-self-start"
            id="devices-invite-cancel"
            disabled={!!busy}
            onClick={() => void run("cancel-invite", () => api("/api/fleet/invite", { method: "DELETE" }))}
          >
            Cancel
          </button>
        </div>
      ) : (
        <div className="devices-add">
          <button
            type="button"
            className="test-btn"
            id="devices-invite-new"
            disabled={!!busy}
            onClick={() =>
              void run("invite", async () => {
                const inv = await api<{ warning?: string }>("/api/fleet/invite", { json: {} });
                setNow(Date.now() / 1000);
                if (inv?.warning === "local_only") setReachAsk(true);
              })
            }
          >
            {busy === "invite" ? "Making a code…" : "Add a device"}
          </button>
          <span className="set-hint">
            Shows a one-time code to type on the other computer.
          </span>
        </div>
      )}

      {/* 5. Join another computer (or pull one in). */}
      <div className="devices-subhead">
        <h4 className="set-subtitle">Join another computer</h4>
        <button
          type="button"
          className="test-btn"
          id="devices-refresh"
          disabled={!!busy}
          onClick={() => void run("refresh", () => api("/api/devices/refresh", { method: "POST" }))}
        >
          {busy === "refresh" ? "Looking…" : "Refresh"}
        </button>
      </div>
      {join && join.state !== "idle" && (
        <div
          className={"devices-join devices-join-" + join.state}
          id="devices-join-status"
          role="status"
        >
          <span>{joinLine(join)}</span>
          {/* Only while it waits: once the other side approved, the join is
              already happening and the server refuses to cancel it. */}
          {join.state === "waiting" && (
            <button type="button" className="test-btn" id="devices-join-cancel" disabled={busy === "cancel-join"} onClick={() => void cancelJoin()}>
              Cancel
            </button>
          )}
        </div>
      )}
      {candidates.length ? (
        <ul className="devices-list" id="devices-candidates">
          {candidates.map((c) => {
            const blocker = candidateBlocker(c);
            const name = c.host || c.device;
            const note = blocker || candidateNote(c);
            return (
              <li
                key={c.device}
                data-candidate={c.device}
                className={focusDevice === c.device ? "devices-focus" : ""}
              >
                <div className="devices-row">
                  <span className={"devices-dot" + (c.reachable ? " on" : "")} aria-hidden="true" />
                  <span className="devices-name">
                    <strong>{name}</strong>
                    {note && <span className={"devices-note" + (blocker && c.reachable ? " warn" : "")}>{note}</span>}
                  </span>
                  {!blocker && (
                    <span className="devices-actions">
                      <button
                        type="button"
                        className="test-btn"
                        data-ask={c.device}
                        disabled={!!busy || !!joinBusy}
                        onClick={() => void askToJoin(c.device)}
                      >
                        Ask to join
                      </button>
                      <button
                        type="button"
                        className="test-btn"
                        data-enter-code={c.device}
                        disabled={!!busy}
                        onClick={() => {
                          setCodeFor(codeFor === c.device ? null : c.device);
                          setCode("");
                        }}
                      >
                        Enter code
                      </button>
                      {c.has_token && (
                        <button
                          type="button"
                          className="test-btn"
                          data-add-paired={c.device}
                          disabled={!!busy || addFor === c.device}
                          onClick={() => setAddFor(c.device)}
                        >
                          Add to my devices
                        </button>
                      )}
                    </span>
                  )}
                </div>
                {/* Two directions, said apart: joining takes ITS settings;
                    adding pulls it in, so it takes THIS one's. */}
                {!blocker && (
                  <span className="set-hint devices-join-note" data-join-note={c.device}>
                    {(c.has_token ? "Ask to join or Enter code: " : "") + joinSettingsNote(name)}
                  </span>
                )}
                {!blocker && addFor === c.device && (
                  <InlineConfirm
                    id="devices-add-confirm"
                    title={"Add " + name + " to your devices?"}
                    body={<span data-add-note={c.device}>{addPairedNote(name)}</span>}
                    confirmLabel={busy === "add:" + c.device ? "Adding…" : "Add"}
                    busy={busy === "add:" + c.device}
                    onConfirm={() =>
                      void run("add:" + c.device, async () => {
                        const res = await api<{ sync_error?: string }>("/api/fleet/add-paired", {
                          json: { device: c.device },
                        });
                        setAddFor(null);
                        toast(
                          admitToast(name, name + " is one of your devices now", res?.sync_error),
                          res?.sync_error ? { duration: 8000 } : undefined
                        );
                      })
                    }
                    onCancel={() => setAddFor(null)}
                  />
                )}
                {codeFor === c.device && (
                  <div className="set-row devices-code-row">
                    <input
                      className="devices-code-input"
                      data-code-for={c.device}
                      placeholder="XXXX-XXXX"
                      autoComplete="off"
                      spellCheck={false}
                      autoFocus
                      value={code}
                      onChange={(e) => setCode(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && plausibleCode(code) && !busy) void joinWithCode(c.device);
                        if (e.key === "Escape") {
                          e.stopPropagation();
                          setCodeFor(null);
                        }
                      }}
                    />
                    <button
                      type="button"
                      className="test-btn"
                      disabled={!!busy || !plausibleCode(code)}
                      onClick={() => void joinWithCode(c.device)}
                    >
                      Join
                    </button>
                    <span className="set-hint">
                      The code {name} shows under Settings → Devices → Add a device.
                    </span>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      ) : peers.length === 0 ? (
        <p className="set-hint" id="devices-no-candidates">
          No other device shows on your tailnet. Make sure the other computer is signed in to the
          same Tailscale (Tailscale on this device, above), then Refresh.
        </p>
      ) : null}
      {/* Why each other tailnet device isn't listed above. */}
      {peers.length > 0 && (
        <ul className="devices-list" id="devices-tailnet-peers">
          {peers.map((t) => (
            <li key={t.device} data-peer={t.device} data-outcome={t.outcome}>
              <div className="devices-row">
                <span className="devices-dot" aria-hidden="true" />
                <span className="devices-name">
                  <strong>{t.host || t.device}</strong>
                  <span className="devices-note">{t.reason || "not answering"}</span>
                </span>
                {t.outcome === "timeout" && st.policy_grant && (
                  <button
                    type="button"
                    className="test-btn"
                    data-copy-grant={t.device}
                    onClick={() =>
                      copyText(st.policy_grant || "").then((ok) =>
                        toast(
                          ok
                            ? "Policy lines copied — paste them into Tailscale's admin console → Access controls"
                            : "Copy failed",
                          { duration: 6000 }
                        )
                      )
                    }
                  >
                    Copy grant
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      <div className="set-row devices-paste-row">
        <input
          id="devices-paste"
          ref={pasteRef}
          placeholder="Paste a code — from your other computer, or an invite someone sent you"
          autoComplete="off"
          spellCheck={false}
          value={pasted}
          onChange={(e) => setPasted(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && pasted.trim() && !busy) void joinWithText();
          }}
        />
        <button
          type="button"
          className="test-btn"
          id="devices-paste-join"
          disabled={!!busy || !pasted.trim()}
          onClick={() => void joinWithText()}
        >
          Join
        </button>
        {pasted.trim() && (
          <span className="set-hint devices-join-note" id="devices-paste-note">
            {routeCode(pasted).kind === "peer"
              ? "That's an invite from another person — Join links you to them (Work with someone), not to your devices."
              : joinSettingsNote("")}
          </span>
        )}
      </div>

      {/* 6. Settings sync. */}
      <SettingsSyncRows st={st} sync={sync} setSync={setSync} reload={loadSync} />
    </>
  );
}

/** "Settings sync": share the shareable settings with your other devices
 * (two-way, the latest edit wins). Only between your devices — so it needs
 * one to talk to before it can turn on. Turning it on picks where to start
 * from: the device whose settings everyone takes first. */
function SettingsSyncRows(props: {
  st: FleetStatus;
  sync: SyncStatus | null;
  setSync(s: SyncStatus | null): void;
  reload(): Promise<void>;
}) {
  const { st, sync, setSync, reload } = props;
  const [from, setFrom] = useState("");
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState("");
  /** The kept-separate source whose Unpin waits on its confirm. */
  const [unpinFor, setUnpinFor] = useState<string | null>(null);

  if (!sync) return null;

  const act = async (key: string, fn: () => Promise<SyncStatus | null | undefined>, ok?: string) => {
    setBusy(key);
    try {
      const r = await fn();
      // Every sync route answers with the status; anything else, re-read it.
      if (r && typeof r === "object" && "enabled" in r) setSync(r);
      else await reload();
      if (ok) toast(ok);
    } catch (e) {
      toast("Settings sync: " + errText(e));
    } finally {
      setBusy("");
    }
  };

  const setEnabled = (body: { enabled: boolean; from?: string }) =>
    act(
      body.enabled ? "on" : "off",
      async () => {
        const r = await api<SyncStatus & { adopted?: string[] }>("/api/settings/sync", { json: body });
        if (body.enabled && body.from) {
          // The device we started from just handed us its settings.
          void fetchSettingsDoc().catch(() => {});
          void refreshConfig();
        }
        return r;
      },
      body.enabled
        ? body.from
          ? "Settings sync on — started from " + (sync.devices.find((d) => d.key === body.from)?.label || body.from)
          : "Settings sync on"
        : "Settings sync off"
    );
  const setPinned = (path: string, pinned: boolean) =>
    act("pin:" + path, () => api<SyncStatus>("/api/settings/sync/pin", { json: { path, pinned } }));

  const choices = pinChoices(sync);
  const canSync = st.in_fleet || sync.in_fleet;

  return (
    <>
      <h3 className="set-section-title">Settings sync</h3>
      {sync.error && (
        <div className="devices-warn" id="settings-sync-error" role="alert">
          <p>{sync.error}. Fix or delete it, and the next sync picks up again.</p>
        </div>
      )}
      {/* First: a pause is the one thing here that needs an answer. */}
      {sync.paused && (
        <div className="devices-warn" id="settings-sync-paused" role="alert">
          <p>
            {sync.paused}. Most of this device's settings went back to their defaults at once — a
            reset or replaced settings.json, usually — so nothing was sent to your other devices,
            and nothing comes in from them until you choose. "{SYNC_RESUME.theirs.label}" also
            replaces anything changed here since.
          </p>
          <div className="devices-actions">
            {(sync.choices?.length ? sync.choices : ["theirs", "mine"])
              .filter((k): k is keyof typeof SYNC_RESUME => k in SYNC_RESUME)
              .map((k) => (
                <button
                  key={k}
                  type="button"
                  className={"test-btn" + (k === "theirs" ? " devices-primary" : "")}
                  data-sync-resume={k}
                  disabled={!!busy}
                  onClick={() =>
                    void act(
                      "resume:" + k,
                      async () => {
                        const r = await api<SyncStatus>("/api/settings/sync/resume", {
                          json: { keep: SYNC_RESUME[k].keep },
                        });
                        if (k === "theirs") {
                          // The fleet's values were just adopted here.
                          void fetchSettingsDoc().catch(() => {});
                          void refreshConfig();
                        }
                        return r;
                      },
                      k === "theirs"
                        ? "Settings sync resumed — this device took your other devices' settings"
                        : "Settings sync resumed — this device's settings now go to the others"
                    )
                  }
                >
                  {SYNC_RESUME[k].label}
                </button>
              ))}
          </div>
        </div>
      )}

      <div className="set-row" id="settings-sync-row">
        <span className="set-label">Share settings with my other devices</span>
        {!canSync && !sync.enabled ? (
          <span className="set-hint" id="settings-sync-needs-fleet">
            Settings sync only talks to your own devices — add or join one above first.
          </span>
        ) : sync.enabled ? (
          <div className="settings-sync-on">
            <ul className="devices-list settings-sync-devices">
              {sync.devices.length ? (
                sync.devices.map((d) => (
                  <li key={d.key} data-sync-device={d.key}>
                    <div className="devices-row">
                      <span className={"devices-dot" + (d.syncing && !d.error ? " on" : "")} aria-hidden="true" />
                      <span className="devices-name">
                        <strong>{d.label}</strong>
                        <span className={"devices-note" + (d.error ? " warn" : "")}>{syncDeviceLine(d)}</span>
                      </span>
                    </div>
                  </li>
                ))
              ) : (
                <li className="muted">None of your other devices is online right now.</li>
              )}
            </ul>
            <div className="devices-actions">
              <button
                type="button"
                className="test-btn"
                id="settings-sync-now"
                disabled={!!busy}
                onClick={() =>
                  void act("now", () => api<SyncStatus>("/api/settings/sync/now", { method: "POST" }), "Synced")
                }
              >
                {busy === "now" ? "Syncing…" : "Sync now"}
              </button>
              <button
                type="button"
                className="test-btn"
                id="settings-sync-off"
                disabled={!!busy}
                onClick={() => void setEnabled({ enabled: false })}
              >
                Turn off
              </button>
            </div>
          </div>
        ) : (
          <div className="settings-sync-off devices-actions">
            <select
              id="settings-sync-from"
              value={from}
              onChange={(e) => setFrom(e.target.value)}
              title="Whose settings everyone starts with — pick your longest-used machine"
            >
              <option value="">Start from this device's settings</option>
              {sync.devices.map((d) => (
                <option key={d.key} value={d.key}>
                  Start from {d.label}'s settings{d.syncing ? " (already syncing)" : ""}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="test-btn"
              id="settings-sync-on"
              disabled={!!busy}
              onClick={() => void setEnabled({ enabled: true, from })}
            >
              Turn on
            </button>
          </div>
        )}
        <span className="set-hint">
          Ticket sources, GitHub repos, notifications, agent limits, saved prompts, shortcuts, theme,
          accent, templates and zones stay the same on every one of your devices — change one
          anywhere and the others follow within seconds (the latest change wins). Repo paths are
          matched to each computer's own checkout. Ports, the access token, the IDE and signed-in
          accounts stay per device.
        </span>
      </div>

      {sync.warnings?.length > 0 && (
        <div className="devices-warn" id="settings-sync-warnings">
          {sync.warnings.map((w, i) => (
            <p key={i}>{w}</p>
          ))}
        </div>
      )}

      {sync.deferred?.length > 0 && (
        <div className="set-row" id="settings-sync-deferred">
          <span className="set-label">Not applied here</span>
          <ul className="devices-plain devices-deferred">
            {sync.deferred.map((d) => (
              <li key={d.path} data-deferred={d.path}>
                <span>
                  <strong>{syncLabel(d.path, sync)}</strong> <span className="muted">— {d.reason}</span>
                </span>
              </li>
            ))}
          </ul>
          <span className="set-hint">Install it here and the next sync applies it.</span>
        </div>
      )}

      {(sync.enabled || sync.pinned?.length > 0) && (
        <div className="set-row" id="settings-sync-pinned">
          <span className="set-label">Kept different on this device</span>
          {sync.pinned?.length ? (
            <ul className="devices-plain">
              {sync.pinned.map((path) => {
                // A source kept separate at join: unpinning swaps it (and its
                // token) for the other device's different one — confirm first.
                const replaces = unpinReplaces(path, sync);
                return (
                  <li key={path} data-pinned={path}>
                    <span>{syncLabel(path, sync)}</span>
                    <button
                      type="button"
                      className="test-btn"
                      data-unpin={path}
                      disabled={!!busy || unpinFor === path}
                      onClick={() => (replaces ? setUnpinFor(path) : void setPinned(path, false))}
                    >
                      Unpin
                    </button>
                    {replaces && unpinFor === path && (
                      <InlineConfirm
                        id="settings-sync-unpin-confirm"
                        title={"Unpin " + syncLabel(path, sync) + "?"}
                        body={replaces}
                        confirmLabel={busy === "pin:" + path ? "Unpinning…" : "Unpin"}
                        busy={busy === "pin:" + path}
                        onConfirm={() => {
                          setUnpinFor(null);
                          void setPinned(path, false);
                        }}
                        onCancel={() => setUnpinFor(null)}
                      />
                    )}
                  </li>
                );
              })}
            </ul>
          ) : null}
          {choices.length > 0 && (
            <div className="devices-actions">
              <select id="settings-sync-pin-pick" value={pin} onChange={(e) => setPin(e.target.value)}>
                <option value="">Choose a setting…</option>
                {choices.map((g) => (
                  <optgroup key={g.group} label={g.group}>
                    {g.items.map((it) => (
                      <option key={it.path} value={it.path}>
                        {it.label}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>
              <button
                type="button"
                className="test-btn"
                id="settings-sync-pin"
                disabled={!!busy || !pin}
                onClick={() => {
                  const path = pin;
                  setPin("");
                  void setPinned(path, true);
                }}
              >
                Keep different
              </button>
            </div>
          )}
          <span className="set-hint">
            A setting kept different keeps this device's own value: it isn't sent to your other
            devices, and theirs isn't applied here. Unpin it to take the others' value again.
          </span>
        </div>
      )}
    </>
  );
}
