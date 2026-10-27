/** The top bar's mic, next to the bell: talk to the focused session.
 *
 * The words are typed into the session's input field as you speak — each
 * phrase the moment it is recognised — and the radio words end it: "over"
 * sends them (Enter, after the server's dialog checks) and listening goes on
 * for the next message, "over and out" sends what is pending and stops,
 * "disregard" takes back what this message typed and keeps listening,
 * Escape does that and stops; each is confirmed out loud ("Sent.",
 * "Cleared." — Settings → Voice "Spoken confirmations"). Push to talk — a
 * click starts listening and it stays on (no idle timeout) until "over and
 * out", a stop phrase or a second click, which stops with the words left
 * in the field unsent (or, in "Hold to talk", the button is held while you
 * speak; letting go stops). A
 * dictation's first phrase is first offered to the voice router
 * (lib/voiceDesktop routePhrase), so "switch to the login fix" selects that
 * session and "start a new session for…" starts one, instead of being typed
 * at whoever has focus; and it is not typed at all while the session shows a
 * dialog (typed words would answer it). With a text box focused, the words go
 * into the box at the cursor and Enter is never pressed (the old dictation
 * button's job).
 *
 * Replies are read aloud only when "Read replies aloud" says so (off by
 * default): "Replies to what I said by voice" — the drafts this mic
 * submitted — or "Everything, every session" — every finished turn that
 * carries words, once. A line leads with its session's name unless it is
 * the focused one, and everything the desktop says goes through ONE speech
 * queue, one line at a time (lib/voiceDesktop SpeechQueue). Right-click or Shift+click opens those options. In the desktop app
 * the words and the voice go through the server's engines (its Web Speech
 * fails there); in a browser tab, through the browser's unless Settings →
 * Voice says "server" (lib/voiceDesktop sttEngine). The words are typed as
 * you speak, like a phone keyboard: the server's recognizer streams when it
 * can (`server_ready.stt_stream`) and Web Speech has interim words — either
 * way the phrase so far is typed as a guess and corrected (backspaces, then
 * the new end) as the recognizer changes its mind, and at the phrase's end
 * the server's accurate model re-hears the whole phrase and, when it heard it
 * otherwise, the whole typed phrase is corrected to that (the radio words and
 * the router read the corrected words); a trailing radio word or stop phrase
 * is never typed, not even as a guess. "Stop recording" stops at any
 * point; "focus three" / "next session" move by the rail's numbers; "yes" /
 * "no" / "always" answer the focused session's dialog or question; the Talk
 * hotkey (`mindflock:voice-talk`) is push to talk from anywhere; and with
 * replies on, a dialog or a question is read out and answered hands-free. The logic that isn't DOM
 * — the radio parser, the dictation machine and its live driver, the reply
 * tracker, the speech queue and the engines — lives in lib/voiceDesktop.ts.
 *
 * Seamless start (lib/voiceMic): the microphone is asked for before the press
 * lands (the options opening, the button going down, the ask pop-up's mic
 * appearing) and kept warm KEEP_STREAM_MS after a dictation, and the server's
 * stream opens while the mic gets ready. While listening, a ring on the button
 * stays still — only its opacity steps with the level (quiet / hearing you
 * / loud), never its size — and the first words are typed as soon as the first
 * chunk is back — sent after 150 ms of speech, later chunks paced by the
 * server's pass time (lib/voiceDesktop chunkMinS).
 *
 * What the bar shows: one small pill beside the mic (lib/voiceDesktop
 * micPill) — "Listening…", "Speaking…", "Quiet." for a second after "stop
 * talking", else nothing. No transcript and no box appears on its own, while
 * listening or while a line is read: the words are in the terminal already.
 * The options (right-click / Shift+click only) keep the level, the words so
 * far and the last line as diagnostics; the button's tooltip has that line.
 *
 * The wake word (lib/wakeWord): with "Listen for “MindFlock”" on, the mic
 * stays open in standby (a faint ring and a dot on the button; the ring still
 * follows the level) and a lone "MindFlock" — never one inside a sentence —
 * chimes, says "Yes?" and starts listening as a click would. Standby pauses
 * while the page is hidden or a dictation records, and stops on "mic off",
 * the switch, a refused microphone or a server without recognition (409). */

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { instApi } from "../api/client";
import { displayName, useUi } from "../state/store";
import { queryClient, refreshInstances, type EventEnvelope } from "../state/queries";
import type { Instance } from "../api/types";
import { effectiveActivity } from "../lib/sessionState";
import { selectSession } from "../lib/sessionActions";
import { fetchDialog } from "../lib/flockActions";
import { KEYMAP, overlayOpen, shortcutLabels } from "../lib/keymap";
import { askPopupOn } from "../lib/ask";
import { placeUnder } from "../lib/menuPlace";
import { askVoice, getMicState, publishMicState, registerAskMic, type AskVoice } from "../lib/askVoice";
import {
  Confirmer,
  DICT_HINT,
  DICT_IDLE_MS,
  HUSH_CAPTION_MS,
  HUSH_LINE,
  DEFAULT_VOICE_CFG,
  HOLD_FINAL_WAIT_MS,
  finalLine,
  micPill,
  HandsFree,
  HeardTurns,
  LiveDictation,
  REPLIES_CHOICES,
  ReplyTracker,
  SpeechQueue,
  TALK_EVENT,
  bargeIn,
  browserRecognizer,
  browserSpeaker,
  engineDiagnostics,
  passLine,
  loadVoiceCfg,
  loadVoicePrefs,
  noRecognizerLine,
  recognizerErrorLine,
  routePhrase,
  runVoiceCommand,
  saveVoicePrefs,
  sayAllMismatch,
  syncSayAll,
  onVoicePrefs,
  serverRecognizer,
  sentLine,
  serverSpeaker,
  sessionTarget,
  spokenLine,
  sttEngine,
  talkKeyAction,
  ttsEngine,
  ttsVoiceKey,
  localVoice,
  voiceAudioCtx,
  voiceName,
  type DictEnd,
  type DictTarget,
  type TalkPhase,
  type Recognizer,
  type RouteReply,
  type SttEngine,
  type VoiceCfg,
  type VoicePrefs,
} from "../lib/voiceDesktop";
import {
  lastLoudAt,
  meterFeed,
  meterPoll,
  meterRef,
  meterStart,
  meterStop,
  onHearing,
  rmsOf,
  voiceMark,
  voiceWake,
} from "../lib/voiceMic";
import {
  CHIME_MS,
  WAKE_HINT,
  WAKE_LISTEN_MS,
  WakeStandby,
  chime,
  wakeLine,
  wakeStatus,
  type WakeStatus,
} from "../lib/wakeWord";

// One of each for the page: the mic renders in one of two places (the mac
// mirror), and what it sent must outlive a remount.
const browserRec = browserRecognizer();
let cfgNow: VoiceCfg = DEFAULT_VOICE_CFG;
const serverRec = serverRecognizer({ stream: () => !!cfgNow.server_ready.stt_stream });
const browserSp = browserSpeaker();
// Confirmations keep their audio per voice (a slow local voice must not
// make "Sent." wait seconds).
const serverSp = serverSpeaker(browserSp, { voiceKey: () => ttsVoiceKey(cfgNow) });
const tracker = new ReplyTracker();
/** "Everything": the turns already read (idle and turn_ended both carry one). */
const heard = new HeardTurns();
const speakingListeners = new Set<(on: boolean) => void>();
const queue = new SpeechQueue(
  () => (ttsEngine(cfgNow) === "server" ? serverSp : browserSp),
  (on) => speakingListeners.forEach((l) => l(on))
);

/** Standby (the wake word) runs the level meter. */
let standbyMeter = false;
/** A wake's dictation and when its word ended: its latency is taken when it
 * listens (the recognizer records). */
let wakeFrom: { d: LiveDictation; at: number } | null = null;
/** The last wake's latency (ms), the word's end → listening — diagnostics. */
let lastWakeMs: number | null = null;
/** The last "over" → Enter (ms): the word's end → the send — diagnostics. */
let lastSentMs: number | null = null;
/** `d` listens now: a wake's latency, once. */
function listened(d: LiveDictation) {
  if (wakeFrom?.d !== d) return;
  lastWakeMs = Math.max(0, Date.now() - wakeFrom.at);
  wakeFrom = null;
  voiceWake(lastWakeMs);
}

/** The browser recognizer has no level of its own: an AnalyserNode on the
 * kept microphone stream gives the meter one. Its undo, while it runs. */
let untap: (() => void) | null = null;
function tapLevel(ctx: AudioContext | null) {
  untap?.();
  if (!ctx) return;
  const mic = serverRec.mic;
  let gone = false;
  let src: MediaStreamAudioSourceNode | null = null;
  mic.hold();
  untap = () => {
    gone = true;
    untap = null;
    meterPoll(null);
    try {
      src?.disconnect();
    } catch {
      /* already gone */
    }
    mic.done();
  };
  void mic.get().then(
    (s) => {
      if (gone) return;
      voiceMark("mic");
      src = ctx.createMediaStreamSource(s);
      const an = ctx.createAnalyser();
      an.fftSize = 2048;
      src.connect(an);
      const buf = new Float32Array(an.fftSize);
      meterPoll(() => {
        an.getFloatTimeDomainData(buf);
        return rmsOf(buf);
      });
    },
    () => {}
  );
}

/** Ready the mic before the press lands: the audio engine (in a gesture — a
 * context made outside one stays suspended) and the microphone, kept warm.
 * Outside a gesture (the ask pop-up's mic appearing) only when the permission
 * is already given: never a prompt nobody asked for. */
function warmMic(gesture: boolean) {
  if (recEngine(cfgNow) === "none") return;
  if (gesture) voiceAudioCtx();
  void serverRec.mic.warm(!gesture);
}

/** The question each session's last Say line asks (GET /voice
 * `has_question`), while it waits: a lone "yes" / "two" answers it. */
const questions = new Map<string, string>();

/** A reply that asks something, being read out (resolves when said): the
 * ask pop-up waits for it instead of reading the question a second time. */
const asksSaid = new Map<string, { done: Promise<void>; at: number }>();
/** How long a reply's read-out stands in for the pop-up's. */
const SAID_FRESH_MS = 60_000;
/** The page checked the server's say-all marker against the pref once. */
let sayAllChecked = false;
/** The asks the pop-up had read out (by session): the next step of one
 * first silences what is left of the previous step's read-out. */
const asksRead = new Set<string>();

/** Re-read `title`'s last Say line: its question waits until it works again. */
function refreshQuestion(title: string) {
  if (!title) return;
  void instApi<{ say?: string; has_question?: boolean }>(title, "/voice")
    .then((j) => {
      if (j?.has_question && liveActivity(title) !== "working") questions.set(title, String(j.say || ""));
      else questions.delete(title);
    })
    .catch(() => {});
}

/** The options' width (CSS .mic-pop), never wider than the window less 32 px. */
const POP_W = 280;
/** "Read replies aloud" in the options' few words (the full wording is the
 * choice's tooltip and Settings → Voice's). */
const REPLIES_SHORT: Record<VoicePrefs["replies"], string> = { off: "Off", mine: "Mine", all: "Everything" };

/** The engines, the server's pass and final times and the last wake's
 * latency, as one line — Settings → Voice (This computer's mic) shows it;
 * the options stay controls only. */
export function micDiagnostics(): string {
  const c = cfgNow;
  const stt = recEngine(c);
  const tts = ttsEngine(c) === "server" ? "server" : browserSp.available() ? "browser" : "none";
  const pass =
    stt === "server" ? [passLine(c.server_ready.stt_backend, serverRec.passMs), finalLine(serverRec.finalMs)] : [];
  return [engineDiagnostics(c, stt, tts), ...pass, wakeLine(lastWakeMs), sentLine(lastSentMs)].filter(Boolean).join(" · ");
}

/** The push-to-talk hotkey as the keymap shows it (the entry whose help says
 * "talk"); "" when it has none — the ask pop-up's "Hotkey: Ctrl+T" hint. */
export function talkHotkey(): string {
  const e = KEYMAP.find((b) => !b.aliasOf && /\btalk\b/i.test(String(b.help?.[2] || "")));
  return (e?.id && shortcutLabels(e.id)[0]) || "";
}


/** The recognizer this page has under `c`: "none" when it has none. */
function recEngine(c: VoiceCfg): SttEngine {
  const e = sttEngine(c, serverRec.available());
  return e === "browser" && !browserRec.available() ? "none" : e;
}


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
    // A correction sets the value once: `old` before the cursor becomes
    // `text` (edited since — the cursor moved off it — `text` just goes in).
    replace: (old, text) => {
      const at = el.selectionStart != null ? el.selectionStart : el.value.length;
      const from = old && el.value.slice(Math.max(0, at - old.length), at) === old ? at - old.length : at;
      el.value = el.value.slice(0, from) + text + el.value.slice(at);
      try {
        el.selectionStart = el.selectionEnd = from + text.length;
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

/** The ask pop-up's answer field as a dictation's target: the words go in at
 * the cursor like any text box, and "over" sends them (`ask.submit`). */
function askTarget(ask: AskVoice): DictTarget | null {
  const el = ask.field();
  if (!el) return null;
  return { ...fieldTarget(el), title: ask.title, name: displayName(ask.title), submit: (say) => ask.submit(say) };
}

/** Monochrome mic (currentColor), never the 🎤 emoji — see the bell's
 * BellGlyph: an emoji paints its own colours and vanishes on an amber bar. */
export function MicGlyph() {
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
  /** "Quiet." in the pill, for a second after "stop talking". */
  const [quiet, setQuiet] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  /** The window's size while the options are open (they re-fit to it). */
  const [, setViewport] = useState("");
  const focusedName = useUi((s) => (s.focused ? s.aliases[s.focused] || s.focused : ""));

  const btnRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  // The ring follows the level (--mic-level, written by lib/voiceMic).
  useEffect(() => meterRef(btnRef.current), []);
  const rec = useRef<Recognizer | null>(null);
  /** "Sent.", "Cleared.", a switch's line: said at the queue's front with
   * the recorder muted for it (lib/voiceDesktop Confirmer). */
  const confirmer = useRef<Confirmer | null>(null);
  confirmer.current ||= new Confirmer({
    queue,
    on: () => prefsRef.current.confirm !== false,
    mic: () => rec.current,
    server: () => ttsEngine(cfgNow) === "server",
    local: () => localVoice(cfgNow),
    speaker: serverSp,
  });
  /** The dictation under way (null when not listening). */
  const live = useRef<LiveDictation | null>(null);
  const pressedField = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null);
  const deciding = useRef<(() => void) | null>(null);
  const idleTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const captionTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const quietTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const holding = useRef(false);
  /** The hotkey's own press is under way (a blur's `up` ends only that). */
  const keyHeld = useRef(false);
  /** Reading a dialog or a question out and listening for the answer. */
  const hands = useRef<HandsFree | null>(null);
  /** The dictation hands-free listening opened (an ask read out). */
  const handsLive = useRef<LiveDictation | null>(null);
  /** Listening for the wake word (lib/wakeWord); its status for the button
   * and the options. */
  const standby = useRef<WakeStandby | null>(null);
  const [wake, setWake] = useState<WakeStatus>(wakeStatus);

  /** The mic's last line — never on the bar: the options' diagnostics, the
   * tooltip and the ask pop-up (lib/askVoice) show it. `sticky`: stays until
   * replaced (listening, or words waiting to be continued); otherwise it
   * fades after `ms`. `tail`: streamed words the recognizer may still change. */
  const say = (text: string, sticky = false, tail = "", ms = 6000) => {
    setCaptionState(text);
    publishMicState({ caption: text, unsure: tail });
    clearTimeout(captionTimer.current);
    if (text && !sticky) captionTimer.current = setTimeout(() => setCaptionState(""), ms);
  };

  /** "Stop talking" (the words, Esc, the options' button): everything the
   * app is saying or has queued, every session, dropped — the mic goes on
   * listening if it was. */
  const hush = () => {
    queue.cancel();
    quietPill();
    if (!live.current) say(HUSH_LINE, false, "", HUSH_CAPTION_MS);
  };

  /** The pill says "Quiet." for {@link HUSH_CAPTION_MS}. */
  const quietPill = () => {
    setQuiet(true);
    clearTimeout(quietTimer.current);
    quietTimer.current = setTimeout(() => setQuiet(false), HUSH_CAPTION_MS);
  };

  // Settings → Voice edits the same prefs: follow it.
  useEffect(() => onVoicePrefs((p) => setPrefsState(p)), []);
  const setPrefs = (p: VoicePrefs) => {
    setPrefsState(p);
    saveVoicePrefs(p);
    if (p.replies === "off") queue.cancel();
  };
  /** The standby switch (in a click: the mic and the audio engine may start). */
  const setWakePref = (on: boolean) => {
    if (on) voiceAudioCtx();
    standby.current?.set(on, true);
    setPrefs({ ...prefsRef.current, wake: on });
  };

  const refreshCfg = () =>
    loadVoiceCfg(cfgNow === DEFAULT_VOICE_CFG ? null : cfgNow).then((c) => {
      cfgNow = c;
      setCfg(c);
      return c;
    });

  useEffect(() => {
    void refreshCfg().then((c) => {
      // On load the pref wins: the server's say-all marker set to match it, once.
      if (sayAllChecked || typeof c.say_all !== "boolean") return;
      sayAllChecked = true;
      const want = sayAllMismatch(prefsRef.current.replies, c.say_all);
      if (want !== null) void syncSayAll(want);
    });
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
      clearTimeout(quietTimer.current);
      rec.current?.stop();
      untap?.();
      serverRec.mic.releaseIdle();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // --- replies: the turns of prompts this mic sent, or every turn --------- //
  useEffect(() => {
    let unsubs: Array<() => void> = [];
    let retry: ReturnType<typeof setTimeout> | undefined;
    const ended = (title: string, data: Record<string, unknown>, ts: number) => {
      if (title === useUi.getState().focused) refreshQuestion(title);
      const mode = prefsRef.current.replies;
      const inline = String(data.say || "").trim();
      const ours = tracker.waiting(title);
      // Into the one queue, worded when its turn comes: the name first
      // unless it is the session you are looking at by then. A reply that
      // asks something: once it (and whatever was queued before it) has been
      // said, the mic opens for the answer — for a prompt this mic sent.
      const speak = (words: string | null, ask = "", listen = true) => {
        if (!words) return;
        say(spokenLine(displayName(title), words));
        if (mode === "off") return;
        if (ask) questions.set(title, ask);
        const said = queue.say(() => spokenLine(voiceName(title, useUi.getState().focused, displayName(title)), words), { title });
        if (!ask) return;
        // Its Reply opens the ask pop-up, which waits for this read-out
        // instead of reading the question a second time.
        asksSaid.set(title, { done: said, at: Date.now() });
        if (listen) void said.then(() => hands.current?.question(title, ask));
      };
      // "Everything": every session's turn that carries words, once.
      if (mode === "all" && inline) {
        if (!heard.fresh(title, inline, Number(data.ts) || null)) return;
        const mine = ours && tracker.take(title, inline, ts) !== null;
        speak(inline, data.has_question ? inline : "", mine);
        return;
      }
      if (!ours) return;
      if (inline) {
        speak(tracker.take(title, inline, ts), data.has_question ? inline : "");
        return;
      }
      void instApi<{ say?: string; ts?: number; has_question?: boolean }>(title, "/voice")
        .then((j) => {
          const said = String(j?.say || "");
          speak(tracker.take(title, said, Number(j?.ts) || null), j?.has_question ? said : "");
        })
        .catch(() => speak(tracker.take(title, "", null)));
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
          if (env.new === "working") {
            tracker.working(env.session);
            questions.delete(env.session);
          } else if (env.new === "idle") ended(env.session, env.data || {}, Number(env.ts) || 0);
          // A dialog came up: read out (focused session, replies on) — by
          // the ask pop-up instead when it is on (it reads any session's).
          else if (env.new === "clarify" && !askPopupOn()) void hands.current?.dialog(env.session);
        }),
        ev.subscribe("session.turn_ended", (env) => {
          if (!replay(env)) ended(env.session, env.data || {}, Number(env.ts) || 0);
        }),
      ];
    };
    attach();
    // The focused session's question, read when focus moves to it.
    refreshQuestion(useUi.getState().focused || "");
    const unsubFocus = useUi.subscribe((s, prev) => {
      if (s.focused !== prev.focused) refreshQuestion(s.focused || "");
    });
    return () => {
      clearTimeout(retry);
      unsubs.forEach((u) => u());
      unsubFocus();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // --- listening ---------------------------------------------------------- //
  const sttNow = recEngine(cfg || cfgNow);
  const supported = sttNow !== "none";

  /** The dictation under way stops after DICT_IDLE_MS of silence — only a
   * hands-free listen or "Hold to talk"; a click (or the wake word) listens
   * until "over and out". */
  const idles = useRef(true);
  const armIdle = () => {
    clearTimeout(idleTimer.current);
    if (idles.current) idleTimer.current = setTimeout(() => void live.current?.expire(), DICT_IDLE_MS);
  };

  /** The recognizer down, nothing reported; the queue may talk again. */
  /** A dictation that chimed its start: its end chimes too. */
  const chimed = useRef(false);
  /** The mic's chime (Settings → Voice "Sounds"): never over a line being
   * said — it waits for that line's end. */
  const sound = (kind: "start" | "stop") => {
    if (prefsRef.current.sounds === false) return;
    if (!queue.busy) return chime(voiceAudioCtx(), kind);
    const after = (on: boolean) => {
      if (on) return;
      speakingListeners.delete(after);
      chime(voiceAudioCtx(), kind);
    };
    speakingListeners.add(after);
  };

  const halt = () => {
    if (chimed.current) {
      chimed.current = false;
      // Before the queue lets the replies go: the stop comes first.
      sound("stop");
    }
    clearTimeout(idleTimer.current);
    deciding.current = null;
    live.current = null;
    rec.current?.stop();
    rec.current = null;
    untap?.();
    meterStop();
    standbyMeter = false; // standby, resuming, starts it again
    setListening(false);
    publishMicState({ listening: false, title: "" });
    queue.release();
  };

  /** The phrases' target: the text box, else the focused session's agent
   * terminal (null: none focused — a first phrase may still be a command). */
  const target = (into: HTMLInputElement | HTMLTextAreaElement | null, session = ""): DictTarget | null => {
    if (into) return fieldTarget(into);
    // The ask pop-up's session: the words go into its answer field (a
    // question's, or the field "Type something" opened).
    const ask = askVoice();
    if (ask && session === ask.title) {
      const t = askTarget(ask);
      if (t) return t;
    }
    const title = session || useUi.getState().focused;
    return title ? sessionTarget(title, displayName(title)) : null;
  };

  /** Start listening into `into` (a text box), else `opts.title` (hands-free:
   * the session read out), else the focused session. Null: it didn't start.
   * `continuous` (the default): "over" sends and listening goes on until
   * "over and out" — false for a hands-free answer (one message). `idle`:
   * DICT_IDLE_MS of silence stops it (default: in "Hold to talk", or when
   * not continuous). */
  const start = (
    into: HTMLInputElement | HTMLTextAreaElement | null,
    opts: {
      title?: string;
      hint?: string;
      skipMs?: number;
      done?: (how: DictEnd) => void;
      continuous?: boolean;
      idle?: boolean;
    } = {}
  ): LiveDictation | null => {
    if (live.current) return null;
    // The ask pop-up is up: the mic answers THAT session — unless a box other
    // than its answer field has the keyboard (that one gets the words).
    const ask = askVoice();
    if (ask && !opts.title && (!into || into === ask.field())) {
      into = null;
      opts = { ...opts, title: ask.title, hint: opts.hint || ask.hint() };
    }
    const c = cfgNow;
    const engine = recEngine(c);
    if (engine === "none") {
      say(noRecognizerLine());
      void refreshCfg();
      return null;
    }
    const r = engine === "server" ? serverRec : browserRec;
    voiceMark("start");
    // Inside the gesture: a context made here runs (server playback, the
    // server recognizer's recording and the browser one's level need it).
    const ctx = voiceAudioCtx();
    // The confirmations' audio, made ahead for a local voice (once).
    confirmer.current?.prime();
    // Barge-in: the app stops talking BEFORE the mic opens — answering the
    // ask being read drops the rest of its read-out — and the first moment
    // of audio (its voice dying away) is skipped.
    const skipMs = Math.max(bargeIn(queue, ask && opts.title === ask.title ? ask.title : ""), opts.skipMs || 0);
    // The start chime is not the person: skipped like the app's own voice.
    const chimeSkip = prefsRef.current.sounds !== false ? CHIME_MS + 30 : 0;
    const continuous = opts.continuous !== false;
    idles.current = opts.idle ?? (!continuous || prefsRef.current.ptt === "hold");
    const d = new LiveDictation(
      target(into, opts.title),
      {
        caption: say,
        halt: () => {
          if (live.current === d) halt();
        },
        route: async (text) => {
          const session = opts.title || useUi.getState().focused || "";
          // The pop-up's own words ("type something" on Claude's question
          // prompt opens that field): listen again, into the field.
          const ask = askVoice();
          if (ask && ask.title === session && ask.phrase?.(text)) {
            setTimeout(() => {
              if (!live.current && askVoice()?.field()) start(null, { continuous });
            }, 150);
            return "end";
          }
          const reply = await routePhrase(text, session);
          if (reply.kind === "prompt") return false;
          if (reply.kind === "hush") return "hush";
          await routed(reply, text, session);
          return reply.kind === "stop" || reply.kind === "off" || reply.kind === "cancel" ? "end" : true;
        },
        // The next message: the text box still, else the ask pop-up's field
        // while it asks the same session, else the focused session.
        retarget: () => {
          if (into) return null;
          const ask = askVoice();
          const title = ask && ask.title === opts.title ? ask.title : "";
          opts = { ...opts, title: title || undefined };
          publishMicState({ listening: true, title: title || useUi.getState().focused || "" });
          return { target: target(null, title), hint: title ? ask?.hint() : undefined };
        },
        hush: () => {
          queue.cancel();
          quietPill();
        },
        activity: liveActivity,
        replies: () => prefsRef.current.replies !== "off",
        submitted: (title, wantSay) => {
          tracker.sent(title, wantSay);
          questions.delete(title);
        },
        pending: (title) => liveActivity(title) === "clarify" || questions.has(title),
        done: (how) => opts.done?.(how),
        confirm: (kind) => void confirmer.current?.say(kind),
        // A control word at the end of the guess acts after a moment's quiet
        // (lib/voiceDesktop CONTROL_TAIL_MS), the phrase ended at once.
        quietMs: () => {
          const at = lastLoudAt();
          return at ? Date.now() - at : Infinity;
        },
        endPhrase: () => {
          if (live.current === d) rec.current?.endPhrase?.();
        },
        sentIn: (ms) => void (lastSentMs = ms),
      },
      { hint: opts.hint, continuous }
    );
    live.current = d;
    rec.current = r;
    setListening(true);
    publishMicState({ listening: true, title: into ? "" : opts.title || useUi.getState().focused || "" });
    d.show();
    armIdle();
    meterStart();
    const ok = r.start(
      {
        // Web Speech's interim words: typed as a guess, corrected as they change.
        onInterim: (t) => {
          if (live.current !== d) return;
          armIdle();
          if (t) voiceMark("partial");
          void d.hearing(t);
        },
        onStatus: (t) => {
          if (live.current === d && !d.dict.shown && !into) say(t + " · " + DICT_HINT, true);
        },
        onReady: () => {
          if (live.current !== d) return;
          d.ready();
          listened(d);
        },
        // The ring, the level bar and "Hearing you…" (lib/voiceMic).
        onLevel: (rms) => {
          if (live.current === d) meterFeed(rms);
        },
        onSpeech: () => {
          if (live.current === d) armIdle();
        },
        // Each phrase is typed as it arrives — also the tail while finishing.
        onFinal: (t) => {
          if (live.current !== d) return;
          armIdle();
          voiceMark("commit");
          void d.heard(t);
        },
        // Streaming: each chunk's committed words and unsure tail, typed as
        // you speak and corrected once per answer; a phrase's end; the tail
        // cleared (a stream's end); the one line when it falls back to clips.
        onStream: (w, p) => {
          if (live.current !== d) return;
          if (w || p) armIdle();
          if (p) voiceMark("partial");
          if (w) voiceMark("commit");
          void d.stream(w, p);
        },
        // A phrase's end: its last words, and the accurate final when the
        // server heard the phrase otherwise (the whole phrase retyped to it).
        onFlush: (t, final) => {
          if (live.current === d) void d.flush(t, final);
        },
        onPartial: (t) => {
          if (live.current === d) d.unsure(t);
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
      },
      { skipMs: Math.max(skipMs, chimeSkip) }
    );
    // Settings may have moved the engines since: the next round knows.
    void refreshCfg();
    if (!ok) {
      halt();
      say("Speech recognition did not start");
      return null;
    }
    // Every start chimes (a click, the hotkey, the wake word, the ask card's mic).
    sound("start");
    chimed.current = true;
    // Web Speech listens from here, and reports no level: tap one.
    if (r === browserRec) {
      d.ready();
      listened(d);
      tapLevel(ctx);
    }
    return d;
  };

  // "Hearing you…" while the level says speech: the ring's opacity step
  // (lib/voiceMic), never words on the bar.
  useEffect(
    () =>
      onHearing((on) => {
        if (on) voiceMark("speech");
        live.current?.loud(on);
      }),
    []
  );

  /** Hands-free: the mic on `title` for `ms`; nothing said by then is a miss. */
  const listenFor = (title: string, hint: string, ms: number) =>
    new Promise<DictEnd>((resolve) => {
      let timer: ReturnType<typeof setTimeout> | undefined;
      const d = start(null, {
        title,
        hint,
        continuous: false,
        done: (how) => {
          clearTimeout(timer);
          resolve(how);
        },
      });
      if (!d) return resolve("stopped");
      handsLive.current = d;
      timer = setTimeout(() => {
        if (live.current === d && !d.heardAnything) d.drop();
      }, ms);
    });

  /** The toggle's second click / the hold's release: stop — once the
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

  // Escape while the app is talking (not listening, no dialog or popover
  // taking it): stop talking — everything, every session.
  useEffect(() => {
    if (!speaking || listening || menuOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || overlayOpen()) return;
      e.stopPropagation();
      e.preventDefault();
      hush();
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [speaking, listening, menuOpen]);

  // --- the router's answers ------------------------------------------------ //
  const announce = (line: string) => {
    if (!line) return;
    say(line);
    if (prefsRef.current.replies !== "off") queue.say(line);
  };

  /** `session`: the one the phrase was routed for (the focused one, or the
   * one hands-free listening read out) — an answer without a title is its. */
  const routed = async (r: RouteReply, text: string, session: string) => {
    // Numbered sessions, answers and questions (lib/voiceDesktop).
    const handled = await runVoiceCommand(r, text, {
      railOrder: () => useUi.getState().railOrder,
      focused: () => session,
      select: (title) => selectSession(title, { noKeyboard: true }),
      name: displayName,
      caption: say,
      announce,
      route: routePhrase,
      approveDialogs: () => refreshCfg().then((c) => !!c.approve_dialogs),
      activity: liveActivity,
      replies: () => prefsRef.current.replies !== "off",
      submitted: (title, wantSay) => {
        tracker.sent(title, wantSay);
        questions.delete(title);
      },
    });
    if (handled) return;
    switch (r.kind) {
      case "switch":
      case "new":
        if (r.title) {
          const title = r.title;
          if (r.kind === "new") void refreshInstances().then(() => selectSession(title, { noKeyboard: true }));
          else selectSession(title, { noKeyboard: true });
        }
        {
          const line = r.say || (r.kind === "new" ? "Started " : "Now talking to ") + (r.name || r.title || "") + ".";
          // Your own action: a confirmation (said now, the mic muted for it),
          // with replies off too; with confirmations off, as before.
          if (prefsRef.current.confirm !== false) {
            say(line);
            void confirmer.current?.say({ line });
          } else announce(line);
        }
        return;
      case "cancel":
        say("Dropped");
        return;
      case "stop":
        say("");
        return;
      case "hush":
        hush();
        return;
      case "off":
        queue.cancel();
        // The standby's open mic too: off means off, until switched on again.
        if (prefsRef.current.wake) setPrefs({ ...prefsRef.current, wake: false });
        standby.current?.set(false);
        untap?.();
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

  // --- hands-free: a dialog or a question read out, the mic opened -------- //
  // This render's functions, for the one HandsFree the mic keeps.
  const latest = useRef({ listenFor, refreshCfg });
  latest.current = { listenFor, refreshCfg };
  if (!hands.current)
    hands.current = new HandsFree({
      replies: () => prefsRef.current.replies !== "off",
      focused: () => useUi.getState().focused || "",
      name: displayName,
      busy: () => !!live.current,
      fetchDialog: (title) => fetchDialog(title),
      approveDialogs: () => latest.current.refreshCfg().then((c) => !!c.approve_dialogs),
      voiceName: (title) => voiceName(title, useUi.getState().focused, displayName(title)),
      speak: (line, title) => {
        say(line, true);
        return queue.say(line, { title });
      },
      drained: () => queue.drained(),
      caption: say,
      listen: (title, hint, ms) => latest.current.listenFor(title, hint, ms),
    });

  /** Nothing more read out or listened for hands-free for `title`'s ask:
   * its lines (being said and queued) dropped, the listen it opened ended.
   * Other sessions' lines play on; a dictation the person keyed stays. */
  const quietAsk = (title: string) => {
    queue.cancel(title);
    hands.current?.stop(title);
    const d = live.current;
    if (d && d === handsLive.current && getMicState().title === title) void d.expire();
  };

  // The ask pop-up's side of the mic (lib/askVoice): read its ask out and
  // listen hands-free, go quiet when it is answered by hand, stop for a
  // session it moved on from, Escape.
  useEffect(
    () =>
      registerAskMic({
        handsFree: (title, a) => {
          const said = asksSaid.get(title);
          asksSaid.delete(title);
          const fresh = said && Date.now() - said.at < SAID_FRESH_MS ? said.done : null;
          // The next step of an ask: the previous step's read-out (and its
          // listen) is over before the new one is read.
          if (asksRead.has(title)) quietAsk(title);
          asksRead.add(title);
          const h = hands.current;
          void h?.settled().then(() => h.ask(title, { ...a, said: fresh }));
        },
        quiet: (title) => quietAsk(title),
        stop: (title) => {
          asksRead.delete(title);
          queue.cancel(title);
          hands.current?.stop(title);
          const d = live.current;
          if (d && getMicState().title === title) void d.expire();
        },
        cancel: () => {
          if (!live.current) return false;
          void live.current.cancel();
          return true;
        },
        supported: () => recEngine(cfgNow) !== "none",
        warm: () => warmMic(false),
      }),
    []
  );

  // --- the wake word: standby, and a wake starting listening -------------- //
  /** A lone "MindFlock": listening at once, as a click starts it (a text box
   * with the keyboard gets the words) — the microphone is already open, so
   * the recognizer's stream opens now — and start()'s chime in the same
   * moment (its sound skipped from the recording). Nothing heard within
   * WAKE_LISTEN_MS is a quiet miss. `soundEnd`: the word's end (latency). */
  const woken = (soundEnd: number) => {
    if (live.current) return;
    const d = start(textField(document.activeElement), { hint: WAKE_HINT, idle: false });
    if (!d) return;
    wakeFrom = { d, at: soundEnd };
    setTimeout(() => {
      if (live.current === d && !d.heardAnything) d.drop();
    }, WAKE_LISTEN_MS);
  };
  const latestWake = useRef(woken);
  latestWake.current = woken;
  if (!standby.current)
    standby.current = new WakeStandby({
      mic: serverRec.mic,
      ctx: voiceAudioCtx,
      speaking: () => queue.busy,
      recording: () => !!live.current,
      onWake: (soundEnd) => latestWake.current(soundEnd),
      onLevel: (rms) => {
        if (!live.current) meterFeed(rms);
      },
      onStatus: (s) => {
        setWake(s);
        // The ring follows the level in standby too (lib/voiceMic's meter),
        // from its start to its stop — a dictation runs its own.
        if (s.on === standbyMeter || live.current) return;
        standbyMeter = s.on;
        if (s.on) meterStart();
        else meterStop();
      },
    });
  // The pref (this switch, Settings → Voice, another window) turns it on/off.
  useEffect(() => {
    standby.current?.set(!!prefs.wake);
  }, [prefs.wake]);
  // A dictation pauses it; its end resumes it — so does the page coming back.
  useEffect(() => {
    standby.current?.sync();
  }, [listening]);
  useEffect(() => {
    const onVis = () => standby.current?.sync();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      document.removeEventListener("visibilitychange", onVis);
      standby.current?.set(false);
    };
  }, []);
  // Standby stopped with a reason (no recognizer, a refused mic): say it once.
  useEffect(() => {
    if (wake.line && prefsRef.current.wake && !live.current) say(wake.line);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wake.line]);

  // --- the gestures -------------------------------------------------------- //
  const hold = prefs.ptt === "hold";

  const openMenu = () => {
    setMenuOpen(true);
    // The options are often the step before talking: ready the mic now.
    warmMic(true);
    void refreshCfg();
  };

  // The push-to-talk hotkey (the keymap's Talk action): works wherever the
  // focus is — a terminal pane too — and its keydown is the gesture that
  // lets start() make the AudioContext.
  const keyRef = useRef({ hold, start, finish });
  keyRef.current = { hold, start, finish };
  useEffect(() => {
    const onTalk = (e: Event) => {
      const phase = (e as CustomEvent<{ phase?: TalkPhase }>).detail?.phase;
      if (phase !== "down" && phase !== "up") return;
      const k = keyRef.current;
      const act = talkKeyAction(phase, k.hold, !!live.current, keyHeld.current);
      if (phase === "up") keyHeld.current = false;
      // The options stay open: they show the level and the words too.
      if (act === "start") {
        if (k.start(textField(document.activeElement)) && k.hold) keyHeld.current = true;
      } else if (act === "finish") k.finish();
    };
    window.addEventListener(TALK_EVENT, onTalk);
    return () => window.removeEventListener(TALK_EVENT, onTalk);
  }, []);

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
    const onResize = () => setViewport(window.innerWidth + "x" + window.innerHeight);
    // Capture phase: a click on a terminal pane must close it too, and xterm
    // keeps pointer events to itself once they reach it.
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", onResize);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("resize", onResize);
    };
  }, [menuOpen]);

  const who = focusedName || "a session";
  const base = !supported
    ? noRecognizerLine() + " — right-click for options"
    : listening
      ? hold
        ? "Listening — release to stop (the words stay), Esc to take them back"
        : "Listening — click to stop (the words stay), Esc to take them back"
      : "Talk to " + who + " — " + (hold ? "hold to talk" : "click to talk") +
        (wake.on ? ", or say “MindFlock”" : "") + ", right-click for options";
  // The last line (an error, "Sent to…") is there on hover — never a box.
  const title = caption && !listening ? base + "\n" + caption : base;
  const pill = micPill({ listening, speaking, quiet });

  // Under the mic, all of it inside the window (8 px from every edge; it
  // scrolls inside when the window is short).
  const rect = menuOpen ? btnRef.current?.getBoundingClientRect() : undefined;
  const popW = Math.min(POP_W, window.innerWidth - 32);
  const place = rect ? placeUnder(rect, popW, window.innerWidth, window.innerHeight) : null;
  const popStyle = place
    ? { top: place.top + "px", left: place.left + "px", width: popW + "px", maxHeight: place.maxHeight + "px" }
    : undefined;

  return (
    <>
      <span className="mic-wrap">
        <button
          id="mic-btn"
          ref={btnRef}
          type="button"
          className={[
            "tb-item",
            "mic-ring",
            listening && "listening",
            wake.on && !listening && "standby",
            speaking && !listening && "speaking",
            !supported && "unsupported",
          ]
            .filter(Boolean)
            .join(" ")}
          title={title}
          aria-label={listening ? "Listening — stop" : "Talk to " + who}
          aria-pressed={listening}
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          onPointerDown={(e) => {
            // Before the click moves focus: a text box that had it gets the words.
            pressedField.current = textField(document.activeElement);
            // The click that starts listening comes at the release: the mic
            // opens during the press.
            if (!live.current) warmMic(true);
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
        {/* The bar's only voice words: the state, never a transcript. Always
            in the DOM (an aria-live region must exist before it speaks). */}
        <span id="mic-pill" className={pill ? "on" : ""} role="status" aria-live="polite">
          {pill}
        </span>
      </span>
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
            {/* Controls only (the owner: keep it simple) — the explainers and
                the diagnostics are in Settings → Voice and the docs. */}
            <div className="mic-pop-row" id="mic-replies-row">
              <span className="mic-pop-label" id="mic-replies-label">
                Read replies aloud
              </span>
              <div className="mic-seg" role="radiogroup" aria-labelledby="mic-replies-label">
                {REPLIES_CHOICES.map((c) => (
                  <label className="mic-seg-opt" key={c.value} title={c.label}>
                    <input
                      type="radio"
                      name="mic-replies"
                      checked={prefs.replies === c.value}
                      onChange={() => setPrefs({ ...prefs, replies: c.value })}
                    />
                    <span>{REPLIES_SHORT[c.value]}</span>
                  </label>
                ))}
              </div>
            </div>
            <div className="mic-pop-row" id="mic-ptt-row">
              <span className="mic-pop-label" id="mic-ptt-label">
                Push to talk
                {talkHotkey() && <span className="mic-pop-key"> {talkHotkey()}</span>}
              </span>
              <div className="mic-seg" role="radiogroup" aria-labelledby="mic-ptt-label">
                <label className="mic-seg-opt" title="Click to start, click again to stop">
                  <input type="radio" name="mic-ptt" checked={!hold} onChange={() => setPrefs({ ...prefs, ptt: "toggle" })} />
                  <span>Click</span>
                </label>
                <label className="mic-seg-opt" title="Hold the mic (or the hotkey) while you speak">
                  <input type="radio" name="mic-ptt" checked={hold} onChange={() => setPrefs({ ...prefs, ptt: "hold" })} />
                  <span>Hold</span>
                </label>
              </div>
            </div>
            <label className="mic-pop-row mic-switch-row" id="mic-wake-row" title={(prefs.wake && wake.line) || ""}>
              <span className="mic-pop-label">Listen for “MindFlock”</span>
              <input type="checkbox" id="mic-wake" checked={!!prefs.wake} onChange={(e) => setWakePref(e.target.checked)} />
            </label>
            {speaking && (
              <button type="button" className="mic-stop-talking" id="mic-stop-talking" title="Stop talking (Esc)" onClick={hush}>
                Stop talking
              </button>
            )}
          </div>,
          document.body
        )}
    </>
  );
}
