/** The top bar's mic, next to the bell: talk to the focused session.
 *
 * Push to talk — a click starts listening and a second click sends (or, in
 * "Hold to talk", the button is held while you speak). Radio words work in
 * both: a phrase ending in "over" sends now, "disregard" empties the draft and
 * keeps listening, Escape drops it. One phrase is first offered to the voice
 * router (lib/voiceDesktop routePhrase), so "switch to the login fix" selects
 * that session and "start a new session for…" starts one, instead of being
 * typed at whoever has focus. With a text box focused, the words go into the
 * box at the cursor and nothing is sent (the old dictation button's job).
 *
 * Replies are read aloud only when "Read replies aloud" is on (off by
 * default), for the prompts this mic sent, prefixed with the session's name.
 * Right-click or Shift+click opens those options. The logic that isn't DOM —
 * the radio parser, the push-to-talk machine, the reply tracker, the speech
 * queue and the engines — lives in lib/voiceDesktop.ts. */

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ApiError, instApi } from "../api/client";
import { displayName, useUi } from "../state/store";
import { refreshInstances, type EventEnvelope } from "../state/queries";
import { selectSession } from "../lib/sessionActions";
import { openSettings } from "../lib/openSettings";
import {
  DICT_HINT,
  DICT_IDLE_MS,
  DEFAULT_VOICE_CFG,
  Dictation,
  HOLD_FINAL_WAIT_MS,
  ReplyTracker,
  SpeechQueue,
  browserRecognizer,
  browserSpeaker,
  loadVoiceCfg,
  loadVoicePrefs,
  parseRadio,
  recognizerErrorLine,
  routePhrase,
  saveVoicePrefs,
  serverRecognizer,
  serverSpeaker,
  sttEngine,
  ttsEngine,
  voiceAudioCtx,
  type DictOutcome,
  type Recognizer,
  type RouteReply,
  type VoiceCfg,
  type VoicePrefs,
} from "../lib/voiceDesktop";

// One of each for the page: the mic renders in one of two places (the mac
// mirror), and what it sent must outlive a remount.
const browserRec = browserRecognizer();
const serverRec = serverRecognizer();
const browserSp = browserSpeaker();
const serverSp = serverSpeaker(browserSp);
const tracker = new ReplyTracker();
let cfgNow: VoiceCfg = DEFAULT_VOICE_CFG;
const speakingListeners = new Set<(on: boolean) => void>();
const queue = new SpeechQueue(
  () => (ttsEngine(cfgNow) === "server" ? serverSp : browserSp),
  (on) => speakingListeners.forEach((l) => l(on))
);

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
  const [menuOpen, setMenuOpen] = useState(false);
  const focusedName = useUi((s) => (s.focused ? s.aliases[s.focused] || s.focused : ""));

  const btnRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const dict = useRef(new Dictation()).current;
  const rec = useRef<Recognizer | null>(null);
  const field = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null);
  const pressedField = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null);
  /** Words a round left unsent; the next click continues from them. */
  const kept = useRef("");
  const deciding = useRef<(() => void) | null>(null);
  const idleTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const captionTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const holding = useRef(false);

  /** The caption under the mic. `sticky`: stays until replaced (listening, or
   * words waiting to be continued); otherwise it fades after a few seconds. */
  const say = (text: string, sticky = false) => {
    setCaptionState(text);
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
    return () => {
      speakingListeners.delete(l);
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
  const supported = browserRec.available() || (!!cfg && sttEngine(cfg, serverRec.available()) === "server");

  const armIdle = () => {
    clearTimeout(idleTimer.current);
    idleTimer.current = setTimeout(() => {
      if (!dict.listening && !field.current) return;
      halt();
      if (field.current) {
        field.current = null;
        say("");
        return;
      }
      outcome(dict.expire());
    }, DICT_IDLE_MS);
  };

  /** The recognizer down, nothing reported; the queue may talk again. */
  const halt = () => {
    clearTimeout(idleTimer.current);
    deciding.current = null;
    rec.current?.stop();
    rec.current = null;
    setListening(false);
    queue.release();
  };

  const outcome = (out: DictOutcome) => {
    switch (out.kind) {
      case "send":
        halt();
        void dispatch(out.text, out.phrases);
        return;
      case "cleared":
        say("Disregarded — go on", true);
        return;
      case "continue":
        say(dict.shown || DICT_HINT, true);
        return;
      case "stopped":
        halt();
        say("");
        return;
      case "dropped":
        halt();
        kept.current = "";
        say("Dropped");
        return;
      case "kept":
        halt();
        kept.current = out.text;
        say("“" + out.text + "” — Not sent — click the mic to continue", true);
        return;
    }
  };

  const start = (into: HTMLInputElement | HTMLTextAreaElement | null) => {
    if (dict.listening || field.current) return;
    if (!supported) {
      say("Voice input needs Chrome, Edge or Safari — or server recognition (Settings → Your phone → Voice)");
      return;
    }
    const c = cfgNow;
    const r = sttEngine(c, serverRec.available()) === "server" ? serverRec : browserRec;
    // Inside the gesture: a context made here runs (server playback and the
    // server recognizer's silence detector need it running).
    if (r === serverRec || ttsEngine(c) === "server") voiceAudioCtx();
    queue.hold();
    field.current = into;
    if (!into) dict.begin(kept.current);
    kept.current = "";
    rec.current = r;
    setListening(true);
    say(into ? "Listening — the words go into the text box" : dict.shown || DICT_HINT, true);
    armIdle();
    const ok = r.start({
      onInterim: (t) => {
        if (rec.current !== r) return;
        armIdle();
        if (field.current) {
          say(t || "Listening…", true);
          return;
        }
        dict.hearing(t);
        say(dict.shown || DICT_HINT, true);
      },
      onStatus: (t) => {
        if (rec.current === r && !dict.shown && !field.current) say(t + " · " + DICT_HINT, true);
      },
      onSpeech: () => {
        if (rec.current !== r) return;
        armIdle();
        if (!dict.shown && !field.current) say("Hearing you… · " + DICT_HINT, true);
      },
      onFinal: (t) => {
        if (rec.current !== r) return;
        armIdle();
        const box = field.current;
        if (box) {
          const p = parseRadio(t);
          if (p.kind === "disregard") say("Disregarded", true);
          else if (p.text) insertIntoField(box, p.text + " ");
          if (p.kind === "over") {
            field.current = null;
            halt();
            say("");
          } else if (p.kind !== "disregard") say("Listening — the words go into the text box", true);
          return;
        }
        // While finishing, the tail lands in the draft; the decision follows.
        if (deciding.current) {
          dict.hearing("");
          const p = parseRadio(t);
          if (p.kind === "disregard") dict.phrases = [];
          else if (p.text) dict.phrases.push(p.text);
          return;
        }
        outcome(dict.heard(t));
      },
      onError: (code, detail) => {
        if (rec.current !== r) return;
        const line = recognizerErrorLine(code, detail);
        const words = dict.text;
        field.current = null;
        dict.cancel();
        halt();
        kept.current = words;
        say(words ? line + " — “" + words + "” not sent" : line, true);
      },
      onEnd: () => {
        if (rec.current !== r) return;
        if (deciding.current) {
          deciding.current();
          return;
        }
        // Down without being asked: keep what was said.
        field.current = null;
        outcome(dict.expire());
      },
    });
    // Settings may have moved the engines since: the next round knows.
    void refreshCfg();
    if (!ok) {
      rec.current = null;
      field.current = null;
      dict.cancel();
      setListening(false);
      queue.release();
      say("Speech recognition did not start");
    }
  };

  /** The toggle's second click / the hold's release: let the recognizer
   * deliver the phrase in flight (up to HOLD_FINAL_WAIT_MS; a server clip
   * until its transcript is back), then send what was heard. */
  const finish = () => {
    const r = rec.current;
    if (!r) return;
    if (field.current) {
      field.current = null;
      halt();
      say("");
      return;
    }
    if (deciding.current) return;
    let done = false;
    const decide = () => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      deciding.current = null;
      outcome(dict.finish());
    };
    deciding.current = decide;
    const timer = setTimeout(decide, r.name === "server" ? 45_000 : HOLD_FINAL_WAIT_MS);
    r.finish();
  };

  const cancel = () => {
    if (field.current) {
      field.current = null;
      halt();
      say("");
      return;
    }
    outcome(dict.cancel());
  };

  // Escape drops what is being said — the mic's Escape, not a dialog's below.
  useEffect(() => {
    if (!listening) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      e.preventDefault();
      cancel();
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [listening]);

  // --- what was said: the router, then a prompt -------------------------- //
  const announce = (line: string) => {
    if (!line) return;
    say(line);
    if (prefsRef.current.replies) queue.say(line);
  };

  const dispatch = async (text: string, phrases: number) => {
    const session = useUi.getState().focused || "";
    if (phrases === 1) {
      say("“" + text + "”…", true);
      const r = await routePhrase(text, session);
      if (r.kind !== "prompt") {
        routed(r, text);
        return;
      }
    }
    await sendPrompt(text);
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
        // the router's words, nothing sent.
        announce(r.say || "");
        if (!r.say) {
          kept.current = text;
          say("“" + text + "” — Not sent — click the mic to continue", true);
        }
    }
  };

  const sendPrompt = async (text: string) => {
    const title = useUi.getState().focused;
    if (!title) {
      kept.current = text;
      say("Click a session first — “" + text + "” not sent", true);
      return;
    }
    const name = displayName(title);
    const wantSay = prefsRef.current.replies;
    say("Sending to " + name + "…", true);
    try {
      const r = await instApi<{ queued?: boolean }>(title, "/send", {
        json: { text, dialog_safe: true, say: wantSay },
      });
      tracker.sent(title, wantSay);
      say(r?.queued ? "Queued for " + name + "." : "Sent to " + name + ".");
    } catch (err) {
      // The words stay: the next click continues from them.
      kept.current = text;
      const body = (err instanceof ApiError ? err.body : null) as
        | { waking?: boolean; paused?: boolean; queued?: boolean }
        | null;
      let line = "Couldn't send: " + ((err as Error)?.message || "error");
      if (err instanceof ApiError && err.status === 409) {
        if (body?.waking) line = name + " is waking up — click the mic to send again in a moment";
        else if (body?.paused) line = name + " is paused — resume it first";
        else if (body?.queued) line = "Queued for " + name + ".";
      }
      say(line + " · “" + text + "”", true);
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
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [menuOpen]);

  const who = focusedName || "a session";
  const title = !supported
    ? "Voice input needs Chrome, Edge or Safari, or server recognition — right-click for options"
    : listening
      ? hold
        ? "Listening — release to send"
        : "Listening — click to send, Esc to drop"
      : "Talk to " + who + " — " + (hold ? "hold to talk" : "click to talk") + ", right-click for options";

  const rect = menuOpen ? btnRef.current?.getBoundingClientRect() : undefined;
  const popStyle = rect
    ? { top: rect.bottom + 6 + "px", left: Math.max(8, Math.min(rect.left, window.innerWidth - 288)) + "px" }
    : undefined;
  const capRect = caption ? btnRef.current?.getBoundingClientRect() : undefined;
  const capStyle = capRect
    ? { top: capRect.bottom + 6 + "px", left: Math.max(8, Math.min(capRect.left, window.innerWidth - 348)) + "px" }
    : undefined;

  const sttName = !supported
    ? "none in this browser"
    : cfg && sttEngine(cfg, serverRec.available()) === "server"
      ? "the server"
      : "this browser (Web Speech)";
  const ttsName = cfg && ttsEngine(cfg) === "server" ? "the server" : browserSp.available() ? "this browser" : "none";
  const sttNotReady = cfg?.stt === "server" && !cfg.server_ready.stt;

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
        aria-label={listening ? "Listening — stop and send" : "Talk to " + who}
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
          if (hold) return; // the pointer (or the key) did it
          if (dict.listening || field.current) finish();
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
          </div>,
          document.body
        )}
      {menuOpen &&
        createPortal(
          <div className="mic-pop" id="mic-pop" ref={menuRef} style={popStyle} role="menu" aria-label="Voice options">
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
            <div className="mic-hint">{DICT_HINT}. Esc drops it.</div>
            <div className="mic-diag">
              Recognition: {sttName}
              {sttNotReady ? " (server recognition isn't ready" + (cfg?.server_ready.stt_detail ? ": " + cfg.server_ready.stt_detail : "") + ")" : ""}
              {" · "}Replies voice: {ttsName}
            </div>
            <button
              type="button"
              className="linklike mic-link"
              onClick={() => {
                setMenuOpen(false);
                openSettings("mobile");
                // The Voice section sits below the phone rows: bring it up.
                setTimeout(() => document.getElementById("mobile-voice")?.scrollIntoView({ block: "start" }), 300);
              }}
            >
              Speech engines: Settings → Your phone → Voice
            </button>
          </div>,
          document.body
        )}
    </>
  );
}
