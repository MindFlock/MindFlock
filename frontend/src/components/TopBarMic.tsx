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
 * Replies are read aloud only when "Read replies aloud" says so (off by
 * default): "Replies to what I said by voice" — the drafts this mic
 * submitted — or "Everything, every session" — every finished turn that
 * carries words, once. A line leads with its session's name unless it is
 * the focused one, and everything the desktop says goes through ONE speech
 * queue, one line at a time (lib/voiceDesktop SpeechQueue). Right-click or Shift+click opens those options. In the desktop app
 * the words and the voice go through the server's engines (its Web Speech
 * fails there); in a browser tab, through the browser's unless Settings →
 * Voice says "server" (lib/voiceDesktop sttEngine). The server's recognizer
 * streams when it can (`server_ready.stt_stream`): the words are typed while
 * you speak, the last one held back in case it is a radio word, and the
 * unsure tail shows muted in the caption. "Stop recording" stops at any
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
 * follows the level, the caption says "Hearing you…" once the level says
 * speech, and the unsure words show greyed as soon as the first chunk is back
 * (never typed). */

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { instApi } from "../api/client";
import { displayName, useUi } from "../state/store";
import { queryClient, refreshInstances, type EventEnvelope } from "../state/queries";
import type { Instance } from "../api/types";
import { effectiveActivity } from "../lib/sessionState";
import { selectSession } from "../lib/sessionActions";
import { openSettings, type SettingsScreen } from "../lib/openSettings";
import { fetchDialog } from "../lib/flockActions";
import { KEYMAP, shortcutLabels } from "../lib/keymap";
import { askPopupOn } from "../lib/ask";
import { askVoice, getMicState, publishMicState, registerAskMic, type AskVoice } from "../lib/askVoice";
import type { Dialog } from "../api/types";
import {
  DICT_HINT,
  DICT_IDLE_MS,
  DEFAULT_VOICE_CFG,
  HOLD_FINAL_WAIT_MS,
  HandsFree,
  HeardTurns,
  LiveDictation,
  REPLIES_CHOICES,
  ReplyTracker,
  SpeechQueue,
  TALK_EVENT,
  browserRecognizer,
  browserSpeaker,
  engineDiagnostics,
  loadVoiceCfg,
  loadVoicePrefs,
  noRecognizerLine,
  pendingDiagnostics,
  recognizerErrorLine,
  routePhrase,
  runVoiceCommand,
  saveVoicePrefs,
  onVoicePrefs,
  serverRecognizer,
  serverSpeaker,
  sessionTarget,
  spokenLine,
  sttEngine,
  talkKeyAction,
  ttsEngine,
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
  liveTail,
  meterFeed,
  meterPoll,
  meterRef,
  meterStart,
  meterStop,
  onHearing,
  rmsOf,
  voiceMark,
} from "../lib/voiceMic";

// One of each for the page: the mic renders in one of two places (the mac
// mirror), and what it sent must outlive a remount.
const browserRec = browserRecognizer();
let cfgNow: VoiceCfg = DEFAULT_VOICE_CFG;
const serverRec = serverRecognizer({ stream: () => !!cfgNow.server_ready.stt_stream });
const browserSp = browserSpeaker();
const serverSp = serverSpeaker(browserSp);
const tracker = new ReplyTracker();
/** "Everything": the turns already read (idle and turn_ended both carry one). */
const heard = new HeardTurns();
const speakingListeners = new Set<(on: boolean) => void>();
const queue = new SpeechQueue(
  () => (ttsEngine(cfgNow) === "server" ? serverSp : browserSp),
  (on) => speakingListeners.forEach((l) => l(on))
);

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

/** The push-to-talk hotkey as the keymap shows it (the entry whose help says
 * "talk"); "" when it has none — the ask pop-up's "Hotkey: Ctrl+T" hint. */
export function talkHotkey(): string {
  const e = KEYMAP.find((b) => !b.aliasOf && /\btalk\b/i.test(String(b.help?.[2] || "")));
  return (e?.id && shortcutLabels(e.id)[0]) || "";
}

/** The Talk hotkey, else where to find it. */
function talkShortcut(): string {
  return talkHotkey() || "the Talk shortcut in Keyboard shortcuts";
}

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

/** The ask pop-up's answer field as a dictation's target: the words go in at
 * the cursor like any text box, and "submit" sends them (`ask.submit`). */
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
  const [unsure, setUnsure] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const focusedName = useUi((s) => (s.focused ? s.aliases[s.focused] || s.focused : ""));

  const btnRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  // The ring follows the level (--mic-level, written by lib/voiceMic).
  useEffect(() => meterRef(btnRef.current), []);
  const rec = useRef<Recognizer | null>(null);
  /** The dictation under way (null when not listening). */
  const live = useRef<LiveDictation | null>(null);
  const pressedField = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null);
  const deciding = useRef<(() => void) | null>(null);
  const idleTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const captionTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const holding = useRef(false);
  /** The hotkey's own press is under way (a blur's `up` ends only that). */
  const keyHeld = useRef(false);
  /** Reading a dialog or a question out and listening for the answer. */
  const hands = useRef<HandsFree | null>(null);
  /** The options' Diagnostics line for what waits on the focused session. */
  const [pendingLine, setPendingLine] = useState("");

  /** The caption under the mic. `sticky`: stays until replaced (listening, or
   * words waiting to be continued); otherwise it fades after a few seconds.
   * `tail`: streamed words the recognizer may still change, shown muted. */
  const say = (text: string, sticky = false, tail = "") => {
    setCaptionState(text);
    setUnsure(tail);
    publishMicState({ caption: text, unsure: tail });
    clearTimeout(captionTimer.current);
    if (text && !sticky) captionTimer.current = setTimeout(() => setCaptionState(""), 6000);
  };

  // Settings → Voice edits the same prefs: follow it.
  useEffect(() => onVoicePrefs((p) => setPrefsState(p)), []);
  const setPrefs = (p: VoicePrefs) => {
    setPrefsState(p);
    saveVoicePrefs(p);
    if (p.replies === "off") queue.cancel();
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
        const said = queue.say(() => spokenLine(voiceName(title, useUi.getState().focused, displayName(title)), words));
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
    untap?.();
    meterStop();
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
   * the session read out), else the focused session. Null: it didn't start. */
  const start = (
    into: HTMLInputElement | HTMLTextAreaElement | null,
    opts: { title?: string; hint?: string; done?: (how: DictEnd) => void } = {}
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
    queue.hold();
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
              if (!live.current && askVoice()?.field()) start(null);
            }, 150);
            return true;
          }
          const reply = await routePhrase(text, session);
          if (reply.kind === "prompt") return false;
          await routed(reply, text, session);
          return true;
        },
        activity: liveActivity,
        replies: () => prefsRef.current.replies !== "off",
        submitted: (title, wantSay) => {
          tracker.sent(title, wantSay);
          questions.delete(title);
        },
        pending: (title) => liveActivity(title) === "clarify" || questions.has(title),
        done: (how) => opts.done?.(how),
      },
      { hint: opts.hint }
    );
    live.current = d;
    rec.current = r;
    setListening(true);
    publishMicState({ listening: true, title: into ? "" : opts.title || useUi.getState().focused || "" });
    d.show();
    armIdle();
    meterStart();
    const ok = r.start({
      onInterim: (t) => {
        if (live.current !== d) return;
        armIdle();
        if (t) voiceMark("partial");
        d.hearing(t);
      },
      onStatus: (t) => {
        if (live.current === d && !d.dict.shown && !into) say(t + " · " + DICT_HINT, true);
      },
      onReady: () => {
        if (live.current === d) d.ready();
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
      // Streaming: committed words as they come, a phrase's end, the unsure
      // tail (never typed), and the one line when it falls back to clips.
      onWords: (t) => {
        if (live.current !== d) return;
        armIdle();
        voiceMark("commit");
        void d.words(t);
      },
      onFlush: (t) => {
        if (live.current === d) void d.flush(t);
      },
      onPartial: (t) => {
        if (live.current !== d) return;
        if (t) {
          armIdle();
          voiceMark("partial");
        }
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
      return null;
    }
    // Web Speech listens from here, and reports no level: tap one.
    if (r === browserRec) {
      d.ready();
      tapLevel(ctx);
    }
    return d;
  };

  // Before the first word: "Hearing you…" while the level says speech.
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
        done: (how) => {
          clearTimeout(timer);
          resolve(how);
        },
      });
      if (!d) return resolve("stopped");
      timer = setTimeout(() => {
        if (live.current === d && !d.heardAnything) d.drop();
      }, ms);
    });

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
      speak: (line) => {
        say(line, true);
        return queue.say(line);
      },
      drained: () => queue.drained(),
      caption: say,
      listen: (title, hint, ms) => latest.current.listenFor(title, hint, ms),
    });

  // The ask pop-up's side of the mic (lib/askVoice): read its ask out and
  // listen hands-free, stop for a session it moved on from, Escape.
  useEffect(
    () =>
      registerAskMic({
        handsFree: (title, a) => {
          const said = asksSaid.get(title);
          asksSaid.delete(title);
          const fresh = said && Date.now() - said.at < SAID_FRESH_MS ? said.done : null;
          void hands.current?.ask(title, { ...a, said: fresh });
        },
        stop: (title) => {
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

  // --- the gestures -------------------------------------------------------- //
  const hold = prefs.ptt === "hold";

  const openMenu = () => {
    setMenuOpen(true);
    // The options are often the step before talking: ready the mic now.
    warmMic(true);
    void refreshCfg();
    void readPending();
  };

  /** Diagnostics: the focused session's dialog or question, read now. */
  const readPending = async () => {
    const title = useUi.getState().focused || "";
    const up = !!title && liveActivity(title) === "clarify";
    let d: Dialog | null = null;
    if (up) d = await fetchDialog(title).catch(() => null);
    setPendingLine(pendingDiagnostics(title ? displayName(title) : "", d, up, questions.get(title) || ""));
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
  // Under the options, the caption would sit behind them: they show it.
  const showCaption = !!(caption || unsure) && !menuOpen;
  const capRect = showCaption ? btnRef.current?.getBoundingClientRect() : undefined;
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
        className={["tb-item", "mic-ring", listening && "listening", speaking && !listening && "speaking", !supported && "unsupported"]
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
      {showCaption &&
        createPortal(
          <div id="mic-caption" className={listening ? "live" : ""} role="status" aria-live="polite" style={capStyle}>
            {listening ? liveTail(caption, unsure) : caption}
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
            {(listening || caption || unsure) && (
              <div className="mic-live" id="mic-live" role="status" aria-live="polite">
                {listening && (
                  <div className="mic-level" ref={meterRef}>
                    <div className="mic-level-bar" />
                  </div>
                )}
                <div className="mic-live-text">
                  {caption}
                  {unsure && <span className="mic-unsure"> {unsure}</span>}
                </div>
              </div>
            )}
            <div className="mic-sub">Read replies aloud</div>
            {REPLIES_CHOICES.map((c) => (
              <label className="mic-row" key={c.value}>
                <input
                  type="radio"
                  name="mic-replies"
                  checked={prefs.replies === c.value}
                  onChange={() => setPrefs({ ...prefs, replies: c.value })}
                />
                <span>{c.label}</span>
              </label>
            ))}
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
              Say “stop recording” to stop, “focus 3” or “next session” to switch, and “yes”, “no”,
              “always” or a number to answer a dialog. Hotkey: {talkShortcut()}.
            </div>
            <div className="mic-hint" id="mic-handsfree">
              With Read replies aloud on, a dialog on the focused session — or a question in a reply
              to what you sent — is read out and the mic listens 12 s for your answer. Every line
              names its session unless it is the one in front of you, and only one plays at a time.
            </div>
            <div className="mic-diag">{diagnostics}</div>
            {pendingLine && <div className="mic-diag mic-pending">{pendingLine}</div>}
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
