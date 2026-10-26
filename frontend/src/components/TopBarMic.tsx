/** The top bar's mic, next to the bell: talk to the focused session.
 *
 * The words are typed into the session's input field as you speak — each
 * phrase the moment it is recognised — and the radio words end it: "over"
 * stops listening and leaves the words in the field, "submit" submits them
 * (Enter, after the server's dialog checks), "disregard" takes back what this
 * dictation typed and keeps listening, Escape does that and stops. Push to
 * talk — a click starts listening and a second click is "over" (or, in "Hold
 * to talk", the button is held while you speak; letting go is "over"). A
 * dictation's first phrase is first offered to the voice router
 * (lib/voiceDesktop routePhrase), so "switch to the login fix" selects that
 * session and "start a new session for…" starts one, instead of being typed
 * at whoever has focus; and it is not typed at all while the session shows a
 * dialog (typed words would answer it). With a text box focused, the words go
 * into the box at the cursor and Enter is never pressed (the old dictation
 * button's job).
 *
 * Replies are read aloud only when "Read replies aloud" is on (off by
 * default), for the drafts this mic submitted, prefixed with the session's
 * name. Right-click or Shift+click opens those options. In the desktop app
 * the words and the voice go through the server's engines (its Web Speech
 * fails there); in a browser tab, through the browser's unless Settings →
 * Voice says "server" (lib/voiceDesktop sttEngine). The server's recognizer
 * streams when it can (`server_ready.stt_stream`): the words are typed while
 * you speak, the last one held back in case it is a radio word, and the
 * unsure tail shows muted in the caption. The logic that isn't DOM
 * — the radio parser, the dictation machine and its live driver, the reply
 * tracker, the speech queue and the engines — lives in lib/voiceDesktop.ts. */

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { instApi } from "../api/client";
import { displayName, useUi } from "../state/store";
import { queryClient, refreshInstances, type EventEnvelope } from "../state/queries";
import type { Instance } from "../api/types";
import { effectiveActivity } from "../lib/sessionState";
import { selectSession } from "../lib/sessionActions";
import { openSettings, type SettingsScreen } from "../lib/openSettings";
import {
  DICT_HINT,
  DICT_IDLE_MS,
  DEFAULT_VOICE_CFG,
  HOLD_FINAL_WAIT_MS,
  LiveDictation,
  ReplyTracker,
  SpeechQueue,
  browserRecognizer,
  browserSpeaker,
  engineDiagnostics,
  loadVoiceCfg,
  loadVoicePrefs,
  noRecognizerLine,
  recognizerErrorLine,
  routePhrase,
  saveVoicePrefs,
  serverRecognizer,
  serverSpeaker,
  sessionTarget,
  sttEngine,
  ttsEngine,
  voiceAudioCtx,
  type DictTarget,
  type Recognizer,
  type RouteReply,
  type SttEngine,
  type VoiceCfg,
  type VoicePrefs,
} from "../lib/voiceDesktop";

// One of each for the page: the mic renders in one of two places (the mac
// mirror), and what it sent must outlive a remount.
const browserRec = browserRecognizer();
let cfgNow: VoiceCfg = DEFAULT_VOICE_CFG;
const serverRec = serverRecognizer({ stream: () => !!cfgNow.server_ready.stt_stream });
const browserSp = browserSpeaker();
const serverSp = serverSpeaker(browserSp);
const tracker = new ReplyTracker();
const speakingListeners = new Set<(on: boolean) => void>();
const queue = new SpeechQueue(
  () => (ttsEngine(cfgNow) === "server" ? serverSp : browserSp),
  (on) => speakingListeners.forEach((l) => l(on))
);

/** The recognizer this page has under `c`: "none" when it has none. */
function recEngine(c: VoiceCfg): SttEngine {
  const e = sttEngine(c, serverRec.available());
  return e === "browser" && !browserRec.available() ? "none" : e;
}

// The Voice tab of Settings (SettingsDialog's SCREENS) lands with the
// voice-settings-tab branch; until then the key falls back to the default screen.
const VOICE_SCREEN = "voice" as SettingsScreen;

/** A box the words can go into instead of a session (never the terminal's
 * hidden xterm textarea: that one IS the session). */
function textField(el: Element | null): HTMLInputElement | HTMLTextAreaElement | null {
  if (!el || el.classList.contains("xterm-helper-textarea")) return null;
  if (el instanceof HTMLTextAreaElement) return el.readOnly || el.disabled ? null : el;
  if (el instanceof HTMLInputElement) {
    const typed = ["text", "search", "url", "email", ""].includes(el.type);
    return typed && !el.readOnly && !el.disabled ? el : null;
  }
  return null;
}

function insertIntoField(el: HTMLInputElement | HTMLTextAreaElement, text: string) {
  const start = el.selectionStart != null ? el.selectionStart : el.value.length;
  const end = el.selectionEnd != null ? el.selectionEnd : el.value.length;
  el.value = el.value.slice(0, start) + text + el.value.slice(end);
  const pos = start + text.length;
  try {
    el.selectionStart = el.selectionEnd = pos;
  } catch {
    /* readonly */
  }
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

/** A text box as a dictation's target: insert at the cursor; a disregard
 * removes the words only while they still sit right before the cursor (the
 * person may have edited the box since). */
function fieldTarget(el: HTMLInputElement | HTMLTextAreaElement): DictTarget {
  return {
    kind: "field",
    title: "",
    name: "the text box",
    type: (text) => {
      insertIntoField(el, text);
      return true;
    },
    erase: (text) => {
      const at = el.selectionStart != null ? el.selectionStart : el.value.length;
      if (!text || el.value.slice(Math.max(0, at - text.length), at) !== text) return false;
      el.value = el.value.slice(0, at - text.length) + el.value.slice(at);
      try {
        el.selectionStart = el.selectionEnd = at - text.length;
      } catch {
        /* readonly */
      }
      el.dispatchEvent(new Event("input", { bubbles: true }));
      return true;
    },
  };
}

/** The session's live activity, from the rows the app already polls. */
function liveActivity(title: string): string {
  const inst = (queryClient.getQueryData<Instance[]>(["instances"]) || []).find((i) => i.title === title);
  return effectiveActivity(inst || { title });
}

/** Monochrome mic (currentColor), never the 🎤 emoji — see the bell's
 * BellGlyph: an emoji paints its own colours and vanishes on an amber bar. */
function MicGlyph() {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <rect x="9" y="3" width="6" height="11" rx="3" fill="currentColor" stroke="none" />
      <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7" />
    </svg>
  );
}

export function TopBarMic() {
  const [prefs, setPrefsState] = useState<VoicePrefs>(loadVoicePrefs);
  const prefsRef = useRef(prefs);
  prefsRef.current = prefs;
  const [cfg, setCfg] = useState<VoiceCfg | null>(null);
  const [listening, setListening] = useState(false);
  const [speaking, setSpeaking] = useState(queue.busy);
  const [caption, setCaptionState] = useState("");
  const [unsure, setUnsure] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const focusedName = useUi((s) => (s.focused ? s.aliases[s.focused] || s.focused : ""));

  const btnRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const rec = useRef<Recognizer | null>(null);
  /** The dictation under way (null when not listening). */
  const live = useRef<LiveDictation | null>(null);
  const pressedField = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null);
  const deciding = useRef<(() => void) | null>(null);
  const idleTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const captionTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const holding = useRef(false);

  /** The caption under the mic. `sticky`: stays until replaced (listening, or
   * words waiting to be continued); otherwise it fades after a few seconds.
   * `tail`: streamed words the recognizer may still change, shown muted. */
  const say = (text: string, sticky = false, tail = "") => {
    setCaptionState(text);
    setUnsure(tail);
    clearTimeout(captionTimer.current);
    if (text && !sticky) captionTimer.current = setTimeout(() => setCaptionState(""), 6000);
  };

  const setPrefs = (p: VoicePrefs) => {
    setPrefsState(p);
    saveVoicePrefs(p);
    if (!p.replies) queue.cancel();
  };

  const refreshCfg = () =>
    loadVoiceCfg(cfgNow === DEFAULT_VOICE_CFG ? null : cfgNow).then((c) => {
      cfgNow = c;
      setCfg(c);
      return c;
    });

  useEffect(() => {
    void refreshCfg();
    const l = (on: boolean) => setSpeaking(on);
    speakingListeners.add(l);
    // Where speech runs moves with Settings (another window, an engine just
    // installed): re-read it when the window comes back, and after a settings
    // save here — every writer puts the server's echo into ["settings"].
    const onFocus = () => void refreshCfg();
    window.addEventListener("focus", onFocus);
    const unsubSettings = queryClient.getQueryCache().subscribe((event) => {
      if (event.query.queryKey[0] !== "settings" || event.type !== "updated") return;
      if ((event as { action?: { type?: string } }).action?.type === "success") void refreshCfg();
    });
    return () => {
      speakingListeners.delete(l);
      window.removeEventListener("focus", onFocus);
      unsubSettings();
      clearTimeout(idleTimer.current);
      clearTimeout(captionTimer.current);
      rec.current?.stop();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // --- replies: the turns of prompts this mic sent ------------------------ //
  useEffect(() => {
    let unsubs: Array<() => void> = [];
    let retry: ReturnType<typeof setTimeout> | undefined;
    const ended = (title: string, data: Record<string, unknown>, ts: number) => {
      if (!tracker.waiting(title)) return;
      const speak = (line: string | null) => {
        if (!line) return;
        say(line);
        if (prefsRef.current.replies) queue.say(line);
      };
      const inline = String(data.say || "").trim();
      if (inline) {
        speak(tracker.take(title, displayName(title), inline, ts));
        return;
      }
      void instApi<{ say?: string; ts?: number }>(title, "/voice")
        .then((j) => speak(tracker.take(title, displayName(title), String(j?.say || ""), Number(j?.ts) || null)))
        .catch(() => speak(tracker.take(title, displayName(title), "", null)));
    };
    const attach = () => {
      const ev = window.mindflock?.events;
      if (!ev) {
        retry = setTimeout(attach, 1000); // events.js not loaded yet
        return;
      }
      const replay = (env: EventEnvelope) => typeof ev.isReplay === "function" && ev.isReplay(env);
      unsubs = [
        ev.subscribe("session.activity_changed", (env) => {
          if (replay(env)) return;
          if (env.new === "working") tracker.working(env.session);
          else if (env.new === "idle") ended(env.session, env.data || {}, Number(env.ts) || 0);
        }),
        ev.subscribe("session.turn_ended", (env) => {
          if (!replay(env)) ended(env.session, env.data || {}, Number(env.ts) || 0);
        }),
      ];
    };
    attach();
    return () => {
      clearTimeout(retry);
      unsubs.forEach((u) => u());
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // --- listening ---------------------------------------------------------- //
  const sttNow = recEngine(cfg || cfgNow);
  const supported = sttNow !== "none";

  const armIdle = () => {
    clearTimeout(idleTimer.current);
    idleTimer.current = setTimeout(() => void live.current?.expire(), DICT_IDLE_MS);
  };

  /** The recognizer down, nothing reported; the queue may talk again. */
  const halt = () => {
    clearTimeout(idleTimer.current);
    deciding.current = null;
    live.current = null;
    rec.current?.stop();
    rec.current = null;
    setListening(false);
    queue.release();
  };

  /** The phrases' target: the text box, else the focused session's agent
   * terminal (null: none focused — a first phrase may still be a command). */
  const target = (into: HTMLInputElement | HTMLTextAreaElement | null): DictTarget | null => {
    if (into) return fieldTarget(into);
    const title = useUi.getState().focused;
    return title ? sessionTarget(title, displayName(title)) : null;
  };

  const start = (into: HTMLInputElement | HTMLTextAreaElement | null) => {
    if (live.current) return;
    const c = cfgNow;
    const engine = recEngine(c);
    if (engine === "none") {
      say(noRecognizerLine());
      void refreshCfg();
      return;
    }
    const r = engine === "server" ? serverRec : browserRec;
    // Inside the gesture: a context made here runs (server playback and the
    // server recognizer's recording need it running).
    if (r === serverRec || ttsEngine(c) === "server") voiceAudioCtx();
    queue.hold();
    const d = new LiveDictation(target(into), {
      caption: say,
      halt: () => {
        if (live.current === d) halt();
      },
      route: async (text) => {
        const reply = await routePhrase(text, useUi.getState().focused || "");
        if (reply.kind === "prompt") return false;
        routed(reply, text);
        return true;
      },
      activity: liveActivity,
      replies: () => prefsRef.current.replies,
      submitted: (title, wantSay) => tracker.sent(title, wantSay),
    });
    live.current = d;
    rec.current = r;
    setListening(true);
    d.show();
    armIdle();
    const ok = r.start({
      onInterim: (t) => {
        if (live.current !== d) return;
        armIdle();
        d.hearing(t);
      },
      onStatus: (t) => {
        if (live.current === d && !d.dict.shown && !into) say(t + " · " + DICT_HINT, true);
      },
      onSpeech: () => {
        if (live.current !== d) return;
        armIdle();
        if (!d.dict.shown && !into) say("Hearing you… · " + DICT_HINT, true);
      },
      // Each phrase is typed as it arrives — also the tail while finishing.
      onFinal: (t) => {
        if (live.current !== d) return;
        armIdle();
        void d.heard(t);
      },
      // Streaming: committed words as they come, a phrase's end, the unsure
      // tail (never typed), and the one line when it falls back to clips.
      onWords: (t) => {
        if (live.current !== d) return;
        armIdle();
        void d.words(t);
      },
      onFlush: (t) => {
        if (live.current === d) void d.flush(t);
      },
      onPartial: (t) => {
        if (live.current !== d) return;
        if (t) armIdle();
        d.unsure(t);
      },
      onNotice: (t) => {
        if (live.current === d) d.notice(t);
      },
      onError: (code, detail) => {
        if (live.current !== d) return;
        d.fail(recognizerErrorLine(code, detail));
      },
      onEnd: () => {
        if (live.current !== d) return;
        if (deciding.current) {
          deciding.current();
          return;
        }
        // Down without being asked: the words stay where they are.
        void d.expire();
      },
    });
    // Settings may have moved the engines since: the next round knows.
    void refreshCfg();
    if (!ok) {
      halt();
      say("Speech recognition did not start");
    }
  };

  /** The toggle's second click / the hold's release: "over" — once the
   * recognizer has delivered the phrase in flight (up to HOLD_FINAL_WAIT_MS;
   * a server clip until its transcript is back), which is typed like the rest. */
  const finish = () => {
    const r = rec.current;
    const d = live.current;
    if (!r || !d || deciding.current) return;
    let done = false;
    const decide = () => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      deciding.current = null;
      void d.finish();
    };
    deciding.current = decide;
    const timer = setTimeout(decide, r.name === "server" ? 45_000 : HOLD_FINAL_WAIT_MS);
    r.finish();
  };

  // Escape on the mic: "disregard", and stop — the mic's Escape, not a
  // dialog's below.
  useEffect(() => {
    if (!listening) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      e.preventDefault();
      void live.current?.cancel();
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [listening]);

  // --- the router's answers ------------------------------------------------ //
  const announce = (line: string) => {
    if (!line) return;
    say(line);
    if (prefsRef.current.replies) queue.say(line);
  };

  const routed = (r: RouteReply, text: string) => {
    switch (r.kind) {
      case "switch":
      case "new":
        if (r.title) {
          const title = r.title;
          if (r.kind === "new") void refreshInstances().then(() => selectSession(title, { noKeyboard: true }));
          else selectSession(title, { noKeyboard: true });
        }
        announce(r.say || (r.kind === "new" ? "Started " : "Now talking to ") + (r.name || r.title || "") + ".");
        return;
      case "cancel":
        say("Dropped");
        return;
      case "stop":
        say("");
        return;
      case "off":
        queue.cancel();
        browserRec.release();
        serverRec.release();
        say("Mic off");
        return;
      case "repeat":
        if (queue.last) announce(queue.last);
        else say("Nothing to repeat yet");
        return;
      case "condense":
        say("");
        return;
      default:
        // ambiguous / no-session / new-failed / list / needs / status:
        // the router's words, nothing typed.
        announce(r.say || "");
        if (!r.say) say("“" + text + "” — not typed", true);
    }
  };

  // --- the gestures -------------------------------------------------------- //
  const hold = prefs.ptt === "hold";

  const openMenu = () => {
    setMenuOpen(true);
    void refreshCfg();
  };

  useEffect(() => {
    if (!menuOpen) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!menuRef.current?.contains(t) && !btnRef.current?.contains(t)) setMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      e.preventDefault();
      setMenuOpen(false);
      btnRef.current?.focus();
    };
    // Capture phase: a click on a terminal pane must close it too, and xterm
    // keeps pointer events to itself once they reach it.
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [menuOpen]);

  const who = focusedName || "a session";
  const title = !supported
    ? noRecognizerLine() + " — right-click for options"
    : listening
      ? hold
        ? "Listening — release to stop (the words stay), Esc to take them back"
        : "Listening — click to stop (the words stay), Esc to take them back"
      : "Talk to " + who + " — " + (hold ? "hold to talk" : "click to talk") + ", right-click for options";

  const rect = menuOpen ? btnRef.current?.getBoundingClientRect() : undefined;
  const popStyle = rect
    ? { top: rect.bottom + 6 + "px", left: Math.max(8, Math.min(rect.left, window.innerWidth - 288)) + "px" }
    : undefined;
  const capRect = caption ? btnRef.current?.getBoundingClientRect() : undefined;
  const capStyle = capRect
    ? { top: capRect.bottom + 6 + "px", left: Math.max(8, Math.min(capRect.left, window.innerWidth - 348)) + "px" }
    : undefined;

  const ttsNow = ttsEngine(cfg || cfgNow) === "server" ? "server" : browserSp.available() ? "browser" : "none";
  const diagnostics = engineDiagnostics(cfg || cfgNow, sttNow, ttsNow);

  return (
    <>
      <button
        id="mic-btn"
        ref={btnRef}
        type="button"
        className={
          "tb-item" + (listening ? " listening" : "") + (speaking && !listening ? " speaking" : "") + (!supported ? " unsupported" : "")
        }
        title={title}
        aria-label={listening ? "Listening — stop" : "Talk to " + who}
        aria-pressed={listening}
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        onPointerDown={(e) => {
          // Before the click moves focus: a text box that had it gets the words.
          pressedField.current = textField(document.activeElement);
          if (!hold || e.button !== 0 || e.shiftKey) return;
          holding.current = true;
          try {
            e.currentTarget.setPointerCapture(e.pointerId);
          } catch {
            /* synthetic pointer */
          }
          start(pressedField.current);
        }}
        onPointerUp={() => {
          if (!holding.current) return;
          holding.current = false;
          finish();
        }}
        onPointerCancel={() => {
          if (!holding.current) return;
          holding.current = false;
          finish();
        }}
        // Keep the focus where it was (the terminal, the text box).
        onMouseDown={(e) => e.preventDefault()}
        onClick={(e) => {
          if (e.shiftKey) {
            menuOpen ? setMenuOpen(false) : openMenu();
            return;
          }
          if (menuOpen) {
            setMenuOpen(false); // a click on the mic closes the options, never talks
            return;
          }
          if (hold) return; // the pointer (or the key) did it
          if (live.current) finish();
          else start(pressedField.current);
          pressedField.current = null;
        }}
        onContextMenu={(e) => {
          e.preventDefault();
          menuOpen ? setMenuOpen(false) : openMenu();
        }}
        onKeyDown={(e) => {
          if (!hold || (e.key !== " " && e.key !== "Enter")) return;
          e.preventDefault();
          if (e.repeat || holding.current) return;
          holding.current = true;
          start(null);
        }}
        onKeyUp={(e) => {
          if (!hold || (e.key !== " " && e.key !== "Enter")) return;
          e.preventDefault();
          if (!holding.current) return;
          holding.current = false;
          finish();
        }}
      >
        <MicGlyph />
      </button>
      {caption &&
        createPortal(
          <div id="mic-caption" role="status" aria-live="polite" style={capStyle}>
            {caption}
            {unsure && <span className="mic-unsure"> {unsure}</span>}
          </div>,
          document.body
        )}
      {menuOpen &&
        createPortal(
          <div className="mic-pop" id="mic-pop" ref={menuRef} style={popStyle} role="menu" aria-label="Voice options">
            <div className="mic-pop-head">
              <span className="mic-pop-title">Voice</span>
              <button
                type="button"
                className="mic-pop-close"
                id="mic-pop-close"
                aria-label="Close"
                title="Close (Esc)"
                onClick={() => {
                  setMenuOpen(false);
                  btnRef.current?.focus();
                }}
              >
                ×
              </button>
            </div>
            <label className="mic-row">
              <input
                type="checkbox"
                role="switch"
                checked={prefs.replies}
                onChange={(e) => setPrefs({ ...prefs, replies: e.target.checked })}
              />
              <span>Read replies aloud</span>
            </label>
            <div className="mic-sub">Push to talk</div>
            <label className="mic-row">
              <input
                type="radio"
                name="mic-ptt"
                checked={!hold}
                onChange={() => setPrefs({ ...prefs, ptt: "toggle" })}
              />
              <span>Click to talk</span>
            </label>
            <label className="mic-row">
              <input
                type="radio"
                name="mic-ptt"
                checked={hold}
                onChange={() => setPrefs({ ...prefs, ptt: "hold" })}
              />
              <span>Hold to talk</span>
            </label>
            <div className="mic-hint">
              The words are typed into the session as you speak. {DICT_HINT}; Esc takes them back.
            </div>
            <div className="mic-diag">{diagnostics}</div>
            <button
              type="button"
              className="linklike mic-link"
              onClick={() => {
                setMenuOpen(false);
                openSettings(VOICE_SCREEN);
              }}
            >
              Speech engines: Settings → Voice
            </button>
          </div>,
          document.body
        )}
    </>
  );
}
