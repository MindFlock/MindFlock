/** The Voice tour: the top-bar mic in eight steps — talk, watch the words go
 * in, send, stop and start over, switch sessions, answer a question, the
 * wake word, replies read aloud. The words in its copy are the CURRENT voice
 * words (lib/voiceWords), so a person who changed "over" is taught their own
 * word: `steps` is built each time it is read.
 *
 * It opens by itself the first time the mic is used (a click or the Talk
 * hotkey — components/TopBarMic `voiceTourDue`) while `tours_seen` lacks
 * "voice", and from "Show me how" in Settings → Voice and the "?" in the
 * mic's options. Its last two steps point into those options: their
 * `screen` "mic-pop" opens them (TopBarMic follows the store's `coachTour`
 * step), and they close again when the tour leaves them. */
import type { TourDef, TourStep } from ".";
import { quoted, voiceWords, wakeName, type VoiceWords } from "../voiceWords";

/** The Talk hotkey as the keymap shows it ("Ctrl+T"); TopBarMic registers
 * its reader, so this data file needs no keymap (and no store). */
let talkKey: () => string = () => "";
export function setTalkKeyLabel(fn: () => string): void {
  talkKey = fn;
}

/** The screen key the mic's options answer to. */
export const MIC_POP_SCREEN = "mic-pop";

/** The eight steps in `w`'s words (one sentence each, like every tour's). */
export function voiceTourSteps(w: VoiceWords = voiceWords(), key = talkKey()): TourStep[] {
  return [
    {
      target: '[data-tour="voice-mic"], #mic-btn',
      title: "Press or hold to talk",
      body:
        "Click the mic" + (key ? " or press " + key : "") +
        " and talk to the session in front of you — with Hold to talk, hold it while you speak.",
    },
    {
      target: ".pane.focused .agent-term, .pane.focused, #grid",
      title: "Your words go in as you speak",
      body: "They are typed into the session as you say them, and corrected when the accurate pass hears one otherwise.",
    },
    {
      target: '#mic-pill, [data-tour="voice-mic"], #mic-btn',
      title: "Say " + quoted(w.send[0]) + " to send",
      body: "End a sentence with " + quoted(w.send[0]) + " to send it — listening stays on, so the next sentence is the next message.",
    },
    {
      target: '[data-tour="voice-mic"], #mic-btn',
      title: "Stop, or start over",
      body:
        "Say " + quoted(w.end[0]) + " to send what is left and stop, or " + quoted(w.clear[0]) +
        " to take back the message and start it over.",
    },
    {
      target: '[data-tour="session-row"], li.inst, #sidebar',
      title: "Switch sessions by voice",
      body: "Say “session three”, “next session” or “switch to” and a session's name — the numbers are the sidebar's.",
    },
    {
      target: "#ask-mic, #notif-btn",
      title: "Answer a question",
      body: "When a session asks you something, answer with its card's mic or say “yes”, “no” or a number — the bell keeps it for later.",
    },
    {
      target: "#mic-wake-row",
      screen: MIC_POP_SCREEN,
      title: "The wake word",
      body: "Turn this on, then say " + quoted(wakeName(w)) + " on its own — not inside a sentence — to open the mic without a click.",
    },
    {
      target: "#mic-replies-row",
      screen: MIC_POP_SCREEN,
      title: "Replies read aloud",
      body:
        "Off by default: Mine reads the replies to what you sent by voice, Everything every session's, and " +
        quoted(w.hush[0]) + " cuts it short.",
    },
  ];
}

export const VOICE_TOUR: TourDef = {
  id: "voice",
  title: "Voice",
  get steps() {
    return voiceTourSteps();
  },
};
