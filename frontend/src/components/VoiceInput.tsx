/** Voice input (port of section 19): Web Speech dictation into the focused
 * terminal or text box. Final phrases are typed but NOT submitted. */

import { useEffect, useRef, useState } from "react";
import { useUi } from "../state/store";
import { peekTerm } from "../lib/terminals";

type Recognition = {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((ev: SpeechRecognitionEventLike) => void) | null;
  onerror: ((ev: { error: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
};

interface SpeechRecognitionEventLike {
  resultIndex: number;
  results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }>;
}

const SpeechRec: (new () => Recognition) | undefined =
  (window as unknown as Record<string, new () => Recognition>).SpeechRecognition ||
  (window as unknown as Record<string, new () => Recognition>).webkitSpeechRecognition;

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

export function VoiceInput() {
  const [listening, setListening] = useState(false);
  const [caption, setCaption] = useState<string | null>(null);
  const recogRef = useRef<Recognition | null>(null);
  const listeningRef = useRef(false);
  const captionTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const flash = (text: string, sticky = false) => {
    setCaption(text);
    clearTimeout(captionTimer.current);
    if (!sticky) captionTimer.current = setTimeout(() => setCaption(null), 2500);
  };

  // Route a finalized phrase to wherever focus is: a plain text field (insert
  // at the cursor) or a terminal (raw send). Falls back to the focused pane.
  const route = (text: string) => {
    const ae = document.activeElement as HTMLElement | null;
    const isHelper = ae?.classList?.contains("xterm-helper-textarea");
    if (ae && !isHelper && (ae.tagName === "TEXTAREA" || ae.tagName === "INPUT")) {
      insertIntoField(ae as HTMLInputElement, text);
      return;
    }
    const paneEl = ae?.closest?.(".pane") as HTMLElement | null;
    const paneTitle = paneEl?.dataset.title;
    if (paneTitle) {
      const tab = useUi.getState().lastTab[paneTitle];
      const h = peekTerm(paneTitle, tab === "shell" ? "shell" : "agent");
      if (h?.send(text)) return;
    }
    const focused = useUi.getState().focused;
    if (focused) {
      const tab = useUi.getState().lastTab[focused];
      const h =
        peekTerm(focused, tab === "shell" ? "shell" : "agent") || peekTerm(focused, "agent");
      if (h?.send(text)) return;
    }
    flash("Nothing focused to dictate into — click a terminal or text box.");
  };

  useEffect(() => {
    if (!SpeechRec) return;
    const recog = new SpeechRec();
    recog.continuous = true;
    recog.interimResults = true;
    recog.lang = navigator.language || "en-US";
    recog.onresult = (ev) => {
      let interim = "";
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const res = ev.results[i];
        const txt = res[0].transcript;
        if (res.isFinal) route(txt.replace(/^\s+/, "") + " ");
        else interim += txt;
      }
      flash(interim ? "🎙 " + interim : "Listening…", true);
    };
    recog.onerror = (ev) => {
      if (ev.error === "not-allowed" || ev.error === "service-not-allowed") {
        flash("Microphone permission denied.");
        stop();
      } else if (ev.error !== "no-speech") {
        flash("Voice error: " + ev.error);
      }
    };
    // Chrome ends the session after a pause; restart while the user wants it.
    recog.onend = () => {
      if (listeningRef.current) {
        try {
          recog.start();
        } catch {
          /* already started */
        }
      }
    };
    recogRef.current = recog;
    return () => {
      listeningRef.current = false;
      try {
        recog.stop();
      } catch {
        /* not started */
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const stop = () => {
    listeningRef.current = false;
    setListening(false);
    setCaption(null);
    try {
      recogRef.current?.stop();
    } catch {
      /* not started */
    }
  };
  const start = () => {
    if (!recogRef.current) return;
    listeningRef.current = true;
    setListening(true);
    flash("Listening…", true);
    try {
      recogRef.current.start();
    } catch {
      /* start() throws if already started */
    }
  };

  return (
    <>
      <button
        id="mic-btn"
        type="button"
        className={(listening ? "listening" : "") + (!SpeechRec ? " unsupported" : "")}
        title={
          !SpeechRec
            ? "Voice input not supported in this browser (try Chrome/Edge)"
            : listening
              ? "Stop voice input"
              : "Voice input — dictate into the focused terminal or text box"
        }
        aria-label="Toggle voice input"
        onClick={() => {
          if (!SpeechRec) {
            flash("Voice input needs the Web Speech API (Chrome or Edge).");
            return;
          }
          listening ? stop() : start();
        }}
      >
        🎤
      </button>
      <div id="mic-caption" className={caption == null ? "hidden" : ""}>
        {caption || ""}
      </div>
    </>
  );
}
