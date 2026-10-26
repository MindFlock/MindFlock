/** Settings → Voice (screen key "voice"): talking to sessions out loud, from
 * the desktop app's mic (top bar) and from the phone page (/m).
 *
 * What runs where: the phone uses its own engine by default; the desktop app
 * always uses the server's; the server's default is its built-in engine
 * (Windows' System.Speech — also from WSL — or macOS' Speech framework), so a
 * fresh install talks with no model download. Local models (faster-whisper /
 * piper) or an OpenAI-compatible host replace it only when someone picks one.
 *
 * In order: the intro; **Recognition** and **Voice** (the server's engine for
 * each, with the server's readiness line under it — `#voice-engines`, where
 * the mic and the phone page deep-link — and, under Recognition, the
 * one-click local-models installer when the built-in recognizer is basic or
 * local models are picked but missing); where the phone page runs each
 * (`voice.stt` / `voice.tts`, phone | server); **Approve dialogs by voice**;
 * **Try it** (speak a sentence, hear three seconds back); the privacy notes.
 *
 * The fields save through the regular settings model (POST /api/settings
 * {voice}); GET /api/voice — the server's offline verdict on the saved
 * fields, plus what the built-in engine has (`system`, absent on an older
 * server) — is re-read after every save. This used to be the bottom of the
 * phone screen (Mobile.tsx keeps a one-line pointer, `#mobile-voice`). */

import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, apiRaw } from "../../../api/client";
import { toast } from "../../../lib/toast";
import { startWavRecording } from "../../../lib/wavRecorder";
import type { ScreenProps } from "../SettingsDialog";
import { SECRET_MASK, SettingField, useSettings, type SettingsModel } from "../useSettings";
import "./voice.css";

/** GET /api/voice: where the phone page runs speech, the server's verdict on
 * the saved backend fields (core/speech.py describe()) and the built-in
 * engine's report. */
export interface VoiceView {
  stt?: string;
  tts?: string;
  approve_dialogs?: boolean;
  agent_words?: boolean;
  backends?: { stt?: string[]; tts?: string[] };
  server_ready?: {
    stt?: boolean;
    tts?: boolean;
    detail?: string;
    stt_backend?: string;
    stt_detail?: string;
    tts_backend?: string;
    tts_detail?: string;
  };
  /** The built-in engine; absent on a server older than it. */
  system?: {
    available?: boolean;
    platform?: "windows" | "wsl" | "mac" | "none" | string;
    detail?: string;
    voices?: string[];
    recognizers?: string[];
    /** How well its recognizer hears: "basic" (Windows' desktop SAPI one,
     * also from WSL) or "good" (macOS'); absent on an older server. */
    stt_quality?: "basic" | "good" | string;
  };
  /** Whether this viewer may change the server (auth.may_configure). Absent
   * means the server didn't say: assume yes — an owner-only POST answers a
   * refusal with its own 403 sentence anyway. */
  configure_allowed?: boolean;
}

export type VoiceKind = "stt" | "tts";

const KIND_WORD: Record<VoiceKind, string> = { stt: "recognition", tts: "voice" };

/** The backend picker, the built-in engine first. A saved "" means "the first
 * ready one" — the built-in engine wherever it exists (effectiveBackend). */
export const VOICE_BACKENDS = [
  { value: "system", label: "This computer (built in)" },
  { value: "openai", label: "OpenAI-compatible host" },
  { value: "local", label: "Local models (faster-whisper / piper)" },
];

/** The backend a kind's block shows: the saved one, else the one the server
 * says "" resolved to, else the built-in engine. */
export function effectiveBackend(kind: VoiceKind, saved: string, view: VoiceView | null | undefined): string {
  if (saved) return saved;
  const id = (kind === "stt" ? view?.server_ready?.stt_backend : view?.server_ready?.tts_backend) || "";
  return VOICE_BACKENDS.some((o) => o.value === id) ? id : "system";
}

/** Which fields a kind's block shows. Nothing while that kind runs on the
 * phone (the engine blocks here always describe the server, so they pass
 * "server"). On the server: the picker always; URL and key only for a host;
 * the model name for recognition on a host or local models (a whisper size)
 * and for a host's voice; the voice name only for the voice. The built-in
 * engine has nothing to fill in but which of its voices. */
export function voiceFields(
  kind: VoiceKind,
  where: string,
  backend: string
): { backend: boolean; url: boolean; key: boolean; model: boolean; voice: boolean } {
  if (where !== "server") return { backend: false, url: false, key: false, model: false, voice: false };
  if (backend === "system") return { backend: true, url: false, key: false, model: false, voice: kind === "tts" };
  const host = backend !== "local";
  return { backend: true, url: host, key: host, model: kind === "stt" || host, voice: kind === "tts" };
}

const noDot = (s: string) => s.replace(/\.\s*$/, "");

/** The line under a kind's fields: the server's verdict from GET /api/voice
 * (text "" while that kind runs on the phone — nothing to report). With the
 * built-in engine picked, its own sentence ("Windows via WSL — recognizer
 * en-US, voices David, Zira", the Linux one, macOS' permission one) is the
 * detail. */
export function readinessLine(
  kind: VoiceKind,
  view: VoiceView | null | undefined,
  where: string,
  backend = ""
): { ok: boolean; text: string } {
  if (where !== "server") return { ok: false, text: "" };
  const word = KIND_WORD[kind];
  if (!view) return { ok: false, text: "Checking the server…" };
  const sr = view.server_ready || {};
  const ok = !!(kind === "stt" ? sr.stt : sr.tts);
  const id = (kind === "stt" ? sr.stt_backend : sr.tts_backend) || "";
  let detail = (kind === "stt" ? sr.stt_detail : sr.tts_detail) || "";
  if (backend === "system" || (!backend && id === "system")) {
    if (view.system?.detail) detail = view.system.detail;
    else if (!view.system && !ok) detail = detail || "this server has no built-in engine yet — update MindFlock";
  }
  detail = noDot(detail);
  if (ok) return { ok, text: `Server ${word} ready (${id || "server"})${detail ? ": " + detail : ""}.` };
  return { ok, text: `Server ${word} not ready${detail ? ": " + detail : ""}.` };
}

/** Whether each Try-it button can run, and the reason it can't. */
export function tryState(
  view: VoiceView | null | undefined,
  canRecord: boolean
): { say: { ok: boolean; why: string }; hear: { ok: boolean; why: string } } {
  if (!view) {
    const why = "Checking the server…";
    return { say: { ok: false, why }, hear: { ok: false, why } };
  }
  const sr = view.server_ready || {};
  const say = sr.tts
    ? { ok: true, why: "" }
    : { ok: false, why: "No voice is ready on the server" + (sr.tts_detail ? ": " + noDot(sr.tts_detail) : "") + "." };
  const hear = !sr.stt
    ? { ok: false, why: "No recognition is ready on the server" + (sr.stt_detail ? ": " + noDot(sr.stt_detail) : "") + "." }
    : !canRecord
      ? { ok: false, why: "This browser can't record from a microphone." }
      : { ok: true, why: "" };
  return { say, hear };
}

export const TEST_SENTENCE = "This is MindFlock speaking.";
export const LISTEN_SECONDS = 3;

/** A Try-it outcome: the line shown under the buttons. */
export interface TryResult {
  ok: boolean;
  text: string;
}

async function errorOf(r: Response): Promise<string> {
  const j = (await r.json().catch(() => ({}))) as { error?: string; detail?: string };
  return j.error || j.detail || "HTTP " + r.status;
}

/** "Say a test sentence": POST /api/voice/speak, then play the bytes through
 * `ctx` (made inside the click — one made outside a gesture stays muted). */
export async function speakTest(ctx: AudioContext | null, backend: string, now = () => Date.now()): Promise<TryResult> {
  if (!ctx) return { ok: false, text: "This browser can't play audio from the server." };
  const t0 = now();
  try {
    const r = await apiRaw("/api/voice/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: TEST_SENTENCE }),
    });
    if (!r.ok) return { ok: false, text: "Couldn't speak: " + (await errorOf(r)) + "." };
    const bytes = await r.arrayBuffer();
    const ms = now() - t0;
    const audio = await ctx.decodeAudioData(bytes);
    const src = ctx.createBufferSource();
    src.buffer = audio;
    src.connect(ctx.destination);
    src.start(0);
    return { ok: true, text: `Spoke with ${backend || "the server"} in ${ms} ms.` };
  } catch (err) {
    return { ok: false, text: "Couldn't speak: " + noDot(String((err as Error)?.message || err)) + "." };
  }
}

/** "Try recognition", after the recording: POST /api/voice/transcribe with
 * the WAV, and say what came back. */
export async function transcribeTest(wav: Blob, lang: string): Promise<TryResult> {
  try {
    const r = await apiRaw("/api/voice/transcribe?lang=" + encodeURIComponent(lang), {
      method: "POST",
      headers: { "Content-Type": "audio/wav" },
      body: wav,
    });
    if (!r.ok) return { ok: false, text: "Couldn't transcribe: " + (await errorOf(r)) + "." };
    const j = (await r.json().catch(() => ({}))) as { text?: string; backend?: string; ms?: number };
    const how = `(${j.backend || "server"}${typeof j.ms === "number" ? ", " + Math.round(j.ms) + " ms" : ""})`;
    const text = String(j.text || "").trim();
    return text ? { ok: true, text: `Heard: “${text}” ${how}` } : { ok: false, text: `Heard nothing ${how}. Speak up, or try again.` };
  } catch (err) {
    return { ok: false, text: "Couldn't reach the server: " + noDot(String((err as Error)?.message || err)) + "." };
  }
}

/** The sentence for a getUserMedia refusal. */
export function micErrorText(err: unknown): string {
  const name = (err as { name?: string })?.name || "";
  if (name === "NotAllowedError" || name === "SecurityError")
    return "MindFlock may not use the microphone. Allow it for this page (and in your computer's privacy settings), then try again.";
  if (name === "NotFoundError" || name === "OverconstrainedError") return "No microphone was found.";
  return "Couldn't open the microphone: " + noDot(String((err as Error)?.message || err)) + ".";
}

function newAudioCtx(): AudioContext | null {
  const AC =
    typeof window === "undefined"
      ? undefined
      : window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!AC) return null;
  try {
    return new AC();
  } catch {
    return null;
  }
}

export function Voice(p: ScreenProps) {
  return <VoiceSection active={p.active} />;
}

/** The screen's live state: the settings document (every field saves on its
 * own, like the rest of Settings) and the server's verdict, re-read after
 * each save because it is about the saved fields. */
export function VoiceSection({ active }: { active: boolean }) {
  const model = useSettings();
  const [view, setView] = useState<VoiceView | null>(null);
  // Bumped after an install finishes: re-read the verdict even when the
  // settings doc came back unchanged.
  const [reread, setReread] = useState(0);
  const doc = model.settings;
  useEffect(() => {
    if (!active) return;
    let live = true;
    api<VoiceView>("/api/voice")
      .then((v) => live && setView(v))
      .catch((err: Error) => {
        if (!live) return;
        const why = "could not ask the server: " + (err.message || "no answer");
        setView({ server_ready: { stt: false, tts: false, stt_detail: why, tts_detail: why } });
      });
    return () => {
      live = false;
    };
    // `doc` changes identity on every save, which is exactly when the verdict
    // may change.
  }, [active, doc, reread]);

  const [installSupported, setInstallSupported] = useState(false);
  const [installStatus, setInstallStatus] = useState<InstallStatus>({ state: "idle" });
  const [installTouched, setInstallTouched] = useState(false);
  const following = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const alive = () => mounted.current;
  const settle = async (s: InstallStatus) => {
    following.current = false;
    // On success the server switched voice.stt_backend to "local" itself:
    // re-read the settings and the verdict, the way a save does.
    if (s.state === "done" && mounted.current) {
      await model.reload();
      setReread((n) => n + 1);
    }
  };
  // What the installer is doing now (an older server has none: 404, no
  // button); one already running — reopened mid-install — is followed.
  useEffect(() => {
    if (!active) return;
    api<InstallStatus>("/api/voice/install-local")
      .then((s) => {
        if (!mounted.current) return;
        setInstallSupported(true);
        if (s?.state === "running" && !following.current) {
          following.current = true;
          setInstallTouched(true);
          setInstallStatus(s);
          void pollInstall(setInstallStatus, { alive }).then(settle);
        } else if (s?.state === "failed" && !following.current) setInstallStatus(s);
      })
      .catch(() => {});
    // `model` is read inside `settle` only; re-running on its identity would
    // re-ask the server on every save.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);
  const onInstall = () => {
    if (following.current) return;
    following.current = true;
    setInstallTouched(true);
    void installLocalModels(setInstallStatus, { alive }).then(settle);
  };
  const install: InstallProps = { supported: installSupported, status: installStatus, touched: installTouched, onInstall };

  const clearKey = async (kind: VoiceKind) => {
    // POST /api/settings keeps a secret sent blank (the app-wide convention),
    // so forgetting one is its own owner-only call.
    try {
      await api("/api/voice/clear-key", { json: { kind } });
      toast("Key removed");
      await model.reload();
    } catch (err) {
      toast((err as Error).message || "Couldn't remove the key");
    }
  };

  return (
    <VoiceSectionView model={model} view={view} onClearKey={clearKey} install={install} tryRow={<TryRow view={view} />} />
  );
}

/** The screen's markup, given the model and the server's verdict (so a test
 * can render it without a dialog or a fetch). `tryRow` defaults to the
 * buttons with no live handlers' state. */
export function VoiceSectionView({
  model,
  view,
  onClearKey,
  install,
  tryRow,
}: {
  model: SettingsModel;
  view: VoiceView | null;
  onClearKey(kind: VoiceKind): void;
  /** The local-models installer; none (a test, an older server) draws no button. */
  install?: InstallProps;
  tryRow?: ReactNode;
}) {
  const approve = model.get("voice", "approve_dialogs") !== false;
  return (
    <section className="voice-section" id="voice-screen">
      <h3 className="set-section-title">Voice</h3>
      <p className="set-hint set-block-hint" id="voice-intro">
        Talk to your sessions and hear them answer. The desktop app's mic (in the top bar) always uses this
        computer's engines; the phone page uses the phone's own unless you choose otherwise below. Out of the
        box this computer uses its built-in engine, so there is nothing to download; pick local models or a
        hosted service only if you want them.
      </p>
      <div id="voice-engines">
        <VoiceKindBlock kind="stt" title="Recognition" model={model} view={view} onClearKey={onClearKey} install={install} />
        <VoiceKindBlock kind="tts" title="Voice" model={model} view={view} onClearKey={onClearKey} />
      </div>
      <div className="voice-kind" id="voice-phone">
        <h4 className="voice-sub">On the phone page</h4>
        <PhoneWhere kind="stt" title="Recognition runs on" />
        <PhoneWhere kind="tts" title="The voice runs on" />
        <p className="set-hint" id="voice-phone-hint">
          The phone's own engine needs nothing set up. The desktop app always uses this computer.
        </p>
      </div>
      <div className="set-row set-switch-row" id="voice-approve-row">
        <span className="notif-rule-text">
          <span className="set-label" id="voice-approve-label">
            Approve dialogs by voice
          </span>
          <span className="set-hint notif-rule-desc">
            A spoken yes or no answers a permission dialog after its command is read aloud. 'Always' is
            only accepted when you say the word.
          </span>
        </span>
        {/* label wraps only the switch, so clicking the row text never flips it */}
        <label className="ca-switch">
          <input
            type="checkbox"
            id="voice-approve"
            aria-labelledby="voice-approve-label"
            checked={approve}
            onChange={(e) => {
              model.saveField("voice", "approve_dialogs", e.target.checked);
            }}
          />
          <span className="ca-slider" />
        </label>
      </div>
      {tryRow ?? <TryButtons view={view} />}
      <div className="voice-notes">
        <p className="set-hint" id="voice-privacy-local">
          The built-in engines (Windows' and Apple's on-device speech) and local models keep your audio on
          this computer. An OpenAI-compatible host receives what you send it.
        </p>
        <p className="set-hint" id="voice-privacy">
          The phone's own recognition sends audio to Google on Android and to Apple on iOS when no on-device
          model exists.
        </p>
        <p className="set-hint" id="voice-iphone">
          On iPhone, voice needs a Safari tab: the home-screen icon cannot hear you. Recognition on this
          computer fixes that.
        </p>
      </div>
    </section>
  );
}

/** The phone page's preference for one kind: its own engine, or this server
 * (unset reads as the phone, like the server's default). */
function PhoneWhere({ kind, title }: { kind: VoiceKind; title: string }) {
  const model = useSettings();
  const where = String(model.get("voice", kind) || "phone");
  return (
    <div className="voice-field-row" data-phone-kind={kind}>
      <span className="set-label">{title}</span>
      <select data-group="voice" data-field={kind} value={where} onChange={(e) => model.saveField("voice", kind, e.target.value)}>
        <option value="phone">The phone</option>
        <option value="server">This computer</option>
      </select>
      <span />
    </div>
  );
}

/** One kind — recognition or the voice: the server's backend, the fields that
 * apply to it, then the server's verdict. */
function VoiceKindBlock({
  kind,
  title,
  model,
  view,
  onClearKey,
  install,
}: {
  kind: VoiceKind;
  title: string;
  model: SettingsModel;
  view: VoiceView | null;
  onClearKey(kind: VoiceKind): void;
  install?: InstallProps;
}) {
  const saved = String(model.get("voice", kind + "_backend") || "");
  const backend = effectiveBackend(kind, saved, view);
  const f = voiceFields(kind, "server", backend);
  const line = readinessLine(kind, view, "server", backend);
  const keySaved = model.get("voice", kind + "_api_key") === SECRET_MASK;
  const local = backend === "local";
  const voices = view?.system?.voices || [];
  const offer = kind === "stt" ? installOffer(backend, view) : null;
  // Shown while it runs, and after this page ran it (its outcome), even once
  // local models are ready and the offer is gone.
  const showInstall =
    kind === "stt" && !!install?.supported && (!!offer || install.status.state === "running" || install.touched);
  return (
    <div className="voice-kind" data-kind={kind}>
      <h4 className="voice-sub">{title}</h4>
      {f.backend && (
        <div className="voice-field-row">
          <span className="set-label">Engine</span>
          <select
            data-group="voice"
            data-field={kind + "_backend"}
            value={backend}
            onChange={(e) => model.saveField("voice", kind + "_backend", e.target.value)}
          >
            {VOICE_BACKENDS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
          <span />
          <span className="set-hint">
            {kind === "stt" ? "What turns your words into text." : "What reads the agent's words aloud."}
          </span>
        </div>
      )}
      {f.url && (
        <div className="voice-field-row">
          <span className="set-label">URL</span>
          <SettingField group="voice" field={kind + "_url"} placeholder="https://api.openai.com/v1" />
          <span />
          <span className="set-hint">
            The host's base URL; blank means OpenAI. Groq: https://api.groq.com/openai/v1.
          </span>
        </div>
      )}
      {f.key && (
        <div className="voice-field-row">
          <span className="set-label">API key</span>
          <SettingField group="voice" field={kind + "_api_key"} type="password" placeholder="paste the host's key" />
          {keySaved ? (
            <button
              type="button"
              className="test-btn"
              id={"voice-clear-" + kind}
              title="Forget the saved key"
              onClick={() => onClearKey(kind)}
            >
              Clear
            </button>
          ) : (
            <span />
          )}
          <span className="set-hint">Leave blank to keep the saved one.</span>
        </div>
      )}
      {f.model && (
        <div className="voice-field-row">
          <span className="set-label">Model</span>
          <SettingField
            group="voice"
            field={kind + "_model"}
            placeholder={local ? "small" : kind === "stt" ? "gpt-transcribe" : "gpt-4o-mini-tts"}
          />
          <span />
          <span className="set-hint">
            {local
              ? "A faster-whisper size: tiny, base, small or medium."
              : "The host's model name (Groq: whisper-large-v3-turbo)."}
          </span>
        </div>
      )}
      {f.voice && (
        <div className="voice-field-row">
          <span className="set-label">Voice name</span>
          {backend === "system" && voices.length ? (
            <SettingField group="voice" field="tts_voice" options={systemVoiceOptions(voices, model.get("voice", "tts_voice"))} />
          ) : (
            <SettingField
              group="voice"
              field="tts_voice"
              placeholder={backend === "system" ? "the system's default" : local ? "en_US-lessac-medium" : "alloy"}
            />
          )}
          <span />
        </div>
      )}
      {line.text && (
        <p className={"set-hint voice-ready st-" + (line.ok ? "ok" : "fail")} id={"voice-ready-" + kind}>
          <span className={"mobile-row-glyph st-" + (line.ok ? "ok" : "fail")} aria-hidden="true">
            {line.ok ? "✓" : "✗"}
          </span>
          {line.text}
        </p>
      )}
      {showInstall && install && <InstallRow offer={offer} install={install} allowed={view?.configure_allowed !== false} />}
    </div>
  );
}

/** The built-in voices as select options: the system default first, and a
 * saved name this computer doesn't have kept (said so) rather than silently
 * shown as another. */
export function systemVoiceOptions(voices: string[], saved: unknown): Array<{ value: string; label: string }> {
  const out = [{ value: "", label: "The system's default" }, ...voices.map((v) => ({ value: v, label: v }))];
  const s = saved == null ? "" : String(saved);
  if (s && !voices.includes(s)) out.push({ value: s, label: s + " (not on this computer)" });
  return out;
}

/** GET /api/voice/install-local: the one-click local-models install (the
 * packages into the server's own environment, then the whisper model and the
 * piper voice). `already` and `refused` are this page's own: the POST got a
 * 409 (someone else's install is running) or a 403 (not the owner). */
export interface InstallStatus {
  state: "idle" | "running" | "done" | "failed" | string;
  step?: string;
  log?: string[];
  started?: number | string | null;
  finished?: number | string | null;
  error?: string | null;
  already?: boolean;
  refused?: boolean;
}

/** The installer's live state, as the screen hands it to the markup:
 * `supported` once GET /api/voice/install-local answered (an older server has
 * no installer, so no button), `touched` once this page started or resumed
 * one (a "done" from before it opened says nothing about now). */
export interface InstallProps {
  supported: boolean;
  status: InstallStatus;
  touched: boolean;
  onInstall(): void;
}

export const INSTALL_LABEL = "Install local models (about 300 MB)";
export const INSTALL_BASIC_LINE =
  "Windows' built-in recognizer is basic. Local models (faster-whisper) recognise far better and stay on this computer.";
export const INSTALL_DONE = "Installed — recognition now uses local models";
export const INSTALL_REFUSED = "Only the owner can install models: do it on that computer itself.";
export const INSTALL_POLL_MS = 2000;

/** Whether the built-in recognizer is the basic one: the server says so in
 * `system.stt_quality`, or (a server between the two) in the built-in
 * engine's readiness detail. */
export function basicRecognizer(view: VoiceView | null | undefined): boolean {
  if (view?.system?.stt_quality) return view.system.stt_quality === "basic";
  const sr = view?.server_ready;
  return sr?.stt_backend === "system" && /basic accuracy/i.test(sr.stt_detail || "");
}

/** Why the installer shows under Recognition: "basic" (the built-in engine
 * is picked and hears poorly — with the recommendation line) or "local"
 * (local models are picked but not ready); null when local models already
 * recognise or there is nothing to recommend. */
export function installOffer(backend: string, view: VoiceView | null | undefined): "basic" | "local" | null {
  if (!view) return null;
  const sr = view.server_ready || {};
  if (sr.stt_backend === "local" && sr.stt) return null;
  if (backend === "local") return "local";
  if (backend === "system" && basicRecognizer(view)) return "basic";
  return null;
}

/** The installer row's button and line for a status (`allowed` false: the
 * viewer may not change the server). */
export function installView(
  st: InstallStatus,
  allowed: boolean
): { button: "install" | "retry" | "busy" | null; disabled: boolean; text: string; last: string } {
  const last = st.state === "running" ? (st.log || []).filter(Boolean).slice(-1)[0] || "" : "";
  if (st.state === "running") {
    const head = st.already ? "Already installing" : "Installing";
    return { button: "busy", disabled: true, text: st.step ? `${head}: ${noDot(st.step)}` : head, last };
  }
  if (st.state === "done") return { button: null, disabled: true, text: INSTALL_DONE + ".", last };
  if (st.refused) return { button: "install", disabled: true, text: noDot(st.error || INSTALL_REFUSED) + ".", last };
  if (!allowed) return { button: "install", disabled: true, text: INSTALL_REFUSED, last };
  if (st.state === "failed")
    return { button: "retry", disabled: false, text: "Couldn't install: " + noDot(st.error || "the server gave no reason") + ".", last };
  return { button: "install", disabled: false, text: "", last };
}

type Call = <T = unknown>(path: string, opts?: RequestInit & { json?: unknown }) => Promise<T>;
const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** Follow a running install: GET every `every` ms until it isn't running,
 * reporting each read. Three failed reads in a row end it as failed. */
export async function pollInstall(
  onStatus: (s: InstallStatus) => void,
  {
    call = api as Call,
    wait = sleep,
    every = INSTALL_POLL_MS,
    alive = (): boolean => true,
    already = false,
  }: { call?: Call; wait?: (ms: number) => Promise<void>; every?: number; alive?: () => boolean; already?: boolean } = {}
): Promise<InstallStatus> {
  let misses = 0;
  for (;;) {
    await wait(every);
    if (!alive()) return { state: "running", already };
    let s: InstallStatus;
    try {
      s = { ...(await call<InstallStatus>("/api/voice/install-local")), already };
      misses = 0;
    } catch (err) {
      if (++misses < 3) continue;
      s = { state: "failed", error: "lost track of the install: " + ((err as Error)?.message || "no answer") };
    }
    if (!alive()) return s;
    onStatus(s);
    if (s.state !== "running") return s;
  }
}

/** The button: POST /api/voice/install-local, then follow it. A 409 means an
 * install is already running — follow that one; a 403 is the owner check. */
export async function installLocalModels(
  onStatus: (s: InstallStatus) => void,
  opts: { call?: Call; wait?: (ms: number) => Promise<void>; every?: number; alive?: () => boolean } = {}
): Promise<InstallStatus> {
  const call = opts.call || (api as Call);
  let already = false;
  try {
    await call("/api/voice/install-local", { method: "POST" });
    onStatus({ state: "running", step: "Starting" });
  } catch (err) {
    const status = (err as { status?: number })?.status;
    const error = (err as Error)?.message || "no answer";
    if (status === 409) {
      already = true;
      onStatus({ state: "running", already });
    } else {
      const s: InstallStatus = { state: "failed", error, refused: status === 403 };
      onStatus(s);
      return s;
    }
  }
  return pollInstall(onStatus, { ...opts, call, already });
}

/** The installer row under Recognition: the recommendation (for a basic
 * recognizer), then the button with the step and the last log line beside
 * it while it runs. */
function InstallRow({ offer, install, allowed }: { offer: "basic" | "local" | null; install: InstallProps; allowed: boolean }) {
  const v = installView(install.status, allowed);
  return (
    <div className="voice-install" id="voice-install" data-offer={offer || ""}>
      {offer === "basic" && (
        <p className="set-hint" id="voice-install-why">
          {INSTALL_BASIC_LINE}
        </p>
      )}
      <div className="voice-install-row">
        {v.button && (
          <button
            type="button"
            className="test-btn"
            id="voice-install-btn"
            disabled={v.disabled}
            title={!allowed || install.status.refused ? v.text : undefined}
            onClick={install.onInstall}
          >
            {v.button === "retry" ? "Try again" : v.button === "busy" ? "Installing…" : INSTALL_LABEL}
          </button>
        )}
        {v.button === "busy" && <progress className="voice-install-progress" aria-label="Installing local models" />}
        {v.text && (
          <span
            className={"set-hint voice-install-status" + (install.status.state === "done" ? " st-ok" : install.status.state === "failed" ? " st-fail" : "")}
            id="voice-install-status"
            role="status"
          >
            {v.text}
          </span>
        )}
        {v.last && (
          <span className="set-hint voice-install-log" id="voice-install-log" title={v.last}>
            {v.last}
          </span>
        )}
      </div>
    </div>
  );
}

const canRecord = () => typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia;

/** The Try-it row's buttons and result line (stateless: the live handlers and
 * their state come from TryRow). */
function TryButtons({
  view,
  busy = "",
  left = 0,
  result = null,
  onSay,
  onHear,
}: {
  view: VoiceView | null;
  busy?: "" | "say" | "hear";
  left?: number;
  result?: TryResult | null;
  onSay?(): void;
  onHear?(): void;
}) {
  const st = tryState(view, canRecord());
  const why = [st.say.ok ? "" : st.say.why, st.hear.ok ? "" : st.hear.why].filter(Boolean);
  return (
    <div className="voice-try" id="voice-try">
      <h4 className="voice-sub">Try it</h4>
      <div className="voice-try-buttons">
        <button
          type="button"
          className="test-btn"
          id="voice-try-say"
          disabled={!st.say.ok || !!busy}
          title={st.say.why || undefined}
          onClick={onSay}
        >
          {busy === "say" ? "Speaking…" : "Say a test sentence"}
        </button>
        <button
          type="button"
          className="test-btn"
          id="voice-try-hear"
          disabled={!st.hear.ok || !!busy}
          title={st.hear.why || undefined}
          onClick={onHear}
        >
          {busy === "hear" ? (left > 0 ? `Listening… ${left}` : "Recognizing…") : "Try recognition"}
        </button>
      </div>
      {result ? (
        <p className={"set-hint voice-try-result st-" + (result.ok ? "ok" : "fail")} id="voice-try-result" role="status">
          {result.text}
        </p>
      ) : why.length ? (
        <p className="set-hint" id="voice-try-why">
          {[...new Set(why)].join(" ")}
        </p>
      ) : (
        <p className="set-hint" id="voice-try-hint">
          Recognition listens for {LISTEN_SECONDS} seconds: say a short sentence.
        </p>
      )}
    </div>
  );
}

/** Try it, live: one AudioContext made inside the first click (one made
 * outside a gesture stays suspended), the mic opened per try and released. */
function TryRow({ view }: { view: VoiceView | null }) {
  const ctxRef = useRef<AudioContext | null>(null);
  const [busy, setBusy] = useState<"" | "say" | "hear">("");
  const [left, setLeft] = useState(0);
  const [result, setResult] = useState<TryResult | null>(null);
  useEffect(
    () => () => {
      void ctxRef.current?.close().catch(() => {});
      ctxRef.current = null;
    },
    []
  );
  const ctx = () => {
    if (!ctxRef.current) ctxRef.current = newAudioCtx();
    const c = ctxRef.current;
    if (c && c.state === "suspended") void c.resume().catch(() => {});
    return c;
  };

  const onSay = async () => {
    const c = ctx();
    setBusy("say");
    setResult(null);
    setResult(await speakTest(c, view?.server_ready?.tts_backend || ""));
    setBusy("");
  };

  const onHear = async () => {
    const c = ctx();
    setBusy("hear");
    setResult(null);
    if (!c) {
      setResult({ ok: false, text: "This browser can't record audio." });
      setBusy("");
      return;
    }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      setResult({ ok: false, text: micErrorText(err) });
      setBusy("");
      return;
    }
    try {
      const rec = startWavRecording(stream, c);
      for (let s = LISTEN_SECONDS; s > 0; s--) {
        setLeft(s);
        await new Promise((r) => setTimeout(r, 1000));
      }
      setLeft(0);
      const wav = await rec.stop();
      stream.getTracks().forEach((t) => t.stop());
      const lang = String(navigator.language || "en").split(";")[0];
      setResult(await transcribeTest(wav, lang));
    } catch (err) {
      stream.getTracks().forEach((t) => t.stop());
      setResult({ ok: false, text: "Couldn't record: " + noDot(String((err as Error)?.message || err)) + "." });
    } finally {
      setLeft(0);
      setBusy("");
    }
  };

  return (
    <TryButtons
      view={view}
      busy={busy}
      left={left}
      result={result}
      onSay={() => void onSay()}
      onHear={() => void onHear()}
    />
  );
}
